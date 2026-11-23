"""Regression tests for the reliability audit (2026-11-20).

Each test reproduces a problem found by reading the code, and was run against the
unfixed code first to confirm it failed there.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest
import websockets

from twitchclipper.detection.detector import ChatMessage, DetectorConfig, bin_features, detect
from twitchclipper.twitch import auth as auth_mod
from twitchclipper.twitch.auth import Token
from twitchclipper.twitch.eventsub import EventSubClient

FILLER = ["hi", "hello chat", "what game is this", "nice", "gg", "lol", "true", "o7", "hmm", "W stream", "LUL"]
REACTIONS = ["KEKW", "OMEGALUL", "LMAO", "CLIP IT", "no way", "WTF", "KEKW KEKW"]


def _quiet_chat(seed: int, duration: float = 900.0, rate: float = 1.5, n_users: int = 300) -> list[ChatMessage]:
    rng = np.random.default_rng(seed)
    msgs, t = [], 0.0
    while True:
        t += rng.exponential(1.0 / rate)
        if t >= duration:
            return msgs
        msgs.append(ChatMessage(t, f"viewer{rng.integers(n_users):03d}", str(rng.choice(FILLER))))


def _burst(at: float, texts: list[str], n: int = 80, seed: int = 1) -> list[ChatMessage]:
    """n different chatters each posting once within ~8 s after `at`."""
    rng = np.random.default_rng(seed)
    return [ChatMessage(at + float(rng.uniform(0, 8)), f"reactor{i:03d}", texts[i % len(texts)]) for i in range(n)]


def _events_near(msgs: list[ChatMessage], at: float, cfg: DetectorConfig | None = None) -> list:
    cfg = cfg or DetectorConfig()
    events, _ = detect(sorted(msgs, key=lambda m: m.t), cfg, 0.0, 900.0)
    return [e for e in events if at - 10 <= e.burst_start <= at + 15]


# --- 1. detection: false positives ---------------------------------------------------------
def test_real_reaction_burst_still_detected():
    """Control: a crowd reacting with real reactions must still be found."""
    msgs = _quiet_chat(3) + _burst(450.0, REACTIONS)
    assert _events_near(msgs, 450.0), "a genuine reaction burst must still trigger"


@pytest.mark.parametrize("command", ["!enter", "!join", "!giveaway", "!sr never gonna give you up"])
def test_giveaway_command_spam_does_not_trigger(command):
    """Giveaway entries are many different chatters typing the same !command: not a moment."""
    msgs = _quiet_chat(4) + _burst(450.0, [command])
    assert not _events_near(msgs, 450.0)


@pytest.mark.parametrize("replies", [["yes", "YES", "Yes", "yea"], ["1", "2", "1", "1"], ["no", "nope", "NO"]])
def test_mass_one_word_replies_do_not_trigger(replies):
    """"Can you hear me?" / polls: chat answering yes/no/1/2 en masse is not a clippable moment."""
    msgs = _quiet_chat(5) + _burst(450.0, replies)
    assert not _events_near(msgs, 450.0)


def test_replies_mixed_with_real_reactions_still_trigger():
    """Filtering replies must not hide a real moment where some people also type 'yes'."""
    msgs = _quiet_chat(6) + _burst(450.0, ["yes", "KEKW", "LMAO", "OMEGALUL", "no way", "WTF"])
    assert _events_near(msgs, 450.0)


def test_known_bot_accounts_are_ignored():
    cfg = DetectorConfig()
    bots = [ChatMessage(1.0 + i * 0.01, name, "Follow the stream and join the discord! https://x.y")
            for i, name in enumerate(["Nightbot", "StreamElements", "Moobot", "Fossabot", "Sery_Bot", "streamlabs"])]
    feats = bin_features(bots, 0.0, 5, cfg)
    assert feats["count"].max() == 0 and feats["unique"].max() == 0


def test_filters_can_be_turned_off():
    cfg = DetectorConfig(ignore_commands=False, ignore_low_content_replies=False, ignored_users=frozenset())
    msgs = [ChatMessage(1.0, "a", "!enter"), ChatMessage(1.1, "b", "yes"), ChatMessage(1.2, "nightbot", "hi")]
    assert bin_features(msgs, 0.0, 3, cfg)["unique"].max() == 3


# --- 2. local export must validate its output ----------------------------------------------
@pytest.fixture(scope="module")
def ff():
    from twitchclipper.export import ffmpeg as ft
    inst = ft.best_install()
    if inst is None:
        pytest.skip("ffmpeg not installed")
    return inst


@pytest.fixture(scope="module")
def short_video(ff, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("media") / "src_12s.mp4"
    subprocess.run([str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=f=440",
                    "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)],
                   check=True, capture_output=True, timeout=120)
    return out


def test_analysis_params_are_saved_to_the_database(ff, short_video, tmp_path):
    """The analysis is stored with json.dumps(params); the new bot list (a frozenset) must not break that."""
    from twitchclipper.pipeline import local as pipeline
    from twitchclipper.storage.db import Database
    from twitchclipper.timing.window import TimingConfig
    chat = tmp_path / "chat.csv"
    chat.write_text("offset_seconds,user,message\n" + "".join(f"{i * 0.5},u{i},KEKW\n" for i in range(20)), encoding="utf-8")
    res = pipeline.analyze(ff.ffprobe, short_video, chat, 0.0, 5, DetectorConfig(), TimingConfig())
    assert "nightbot" in json.loads(json.dumps(res.params))["detector"]["ignored_users"]
    db = Database(tmp_path / "t.db")
    try:
        db.save_analysis("local", "test", str(res.video), str(res.chat), res.chat_offset_s, res.duration_s,
                         res.message_count, res.params, [])
    finally:
        db.close()


def test_export_rejects_output_shorter_than_requested(ff, short_video, tmp_path):
    """ffmpeg exits 0 when the window runs past the end of the video, writing a short clip.
    That clip must not be reported as a successful 12 s export or left on disk."""
    from twitchclipper.pipeline import local as pipeline
    from twitchclipper.timing.window import Window
    out = tmp_path / "clip.mp4"
    job = pipeline.ExportJob(short_video, Window(8.0, 20.0), out)
    with pytest.raises(RuntimeError, match="shorter|duration"):
        pipeline.export(ff.ffmpeg, ff.ffprobe, job)
    assert not out.exists()
    assert not list(tmp_path.glob("*.part*")), "temporary file must be cleaned up"


def test_export_valid_clip_passes_validation(ff, short_video, tmp_path):
    from twitchclipper.pipeline import local as pipeline
    from twitchclipper.timing.window import Window
    out = tmp_path / "ok.mp4"
    r = pipeline.export(ff.ffmpeg, ff.ffprobe, pipeline.ExportJob(short_video, Window(2.0, 9.0), out))
    assert out.exists() and abs(r.duration_s - 7.0) < 0.2


def test_export_never_overwrites_an_existing_file(ff, short_video, tmp_path):
    from twitchclipper.pipeline import local as pipeline
    from twitchclipper.timing.window import Window
    out = tmp_path / "keep.mp4"
    out.write_bytes(b"user's existing clip")
    with pytest.raises(FileExistsError):
        pipeline.export(ff.ffmpeg, ff.ffprobe, pipeline.ExportJob(short_video, Window(1.0, 4.0), out))
    assert out.read_bytes() == b"user's existing clip"


def test_export_reports_missing_audio(ff, tmp_path):
    """A source with audio must not silently produce a clip without audio (checked via the probe)."""
    from twitchclipper.pipeline import local as pipeline
    assert pipeline.check_clip({"streams": [{"codec_type": "video"}], "format": {"duration": "5.0"}},
                               expected_s=5.0, expect_audio=True)
    assert pipeline.check_clip({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                                "format": {"duration": "5.0"}}, expected_s=5.0, expect_audio=True) is None
    assert pipeline.check_clip({"streams": [{"codec_type": "audio"}], "format": {"duration": "5.0"}},
                               expected_s=5.0, expect_audio=True)


# --- 3. auth: a refresh failure must fall back to signing in again ------------------------
def _fake_keyring(monkeypatch, stored: str | None):
    store = {"v": stored}
    monkeypatch.setattr(auth_mod, "load_token", auth_mod.load_token)  # keep real parsing
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *_: store["v"])
    monkeypatch.setattr(keyring, "set_password", lambda _s, _u, v: store.__setitem__("v", v))
    monkeypatch.setattr(keyring, "delete_password", lambda *_: store.__setitem__("v", None))
    return store


def _auth_transport(refresh_status: int):
    calls = {"device": 0, "refresh": 0}

    def handler(req: httpx.Request):
        body = req.content.decode()
        if req.url.path.endswith("/device"):
            calls["device"] += 1
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD",
                                             "verification_uri": "https://www.twitch.tv/activate",
                                             "expires_in": 1800, "interval": 0})
        if req.url.path.endswith("/token") and "refresh_token" in body and "grant_type=refresh_token" in body:
            calls["refresh"] += 1
            return httpx.Response(refresh_status, json={"status": refresh_status, "message": "Invalid refresh token"})
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "rt2", "expires_in": 14400,
                                             "scope": ["user:read:chat"]})
        if req.url.path.endswith("/validate"):
            return httpx.Response(200, json={"login": "me"})
        return httpx.Response(404)
    return handler, calls


async def test_ensure_token_falls_back_to_sign_in_when_refresh_is_rejected(monkeypatch):
    expired = Token("old", "dead-refresh", 0.0, ["user:read:chat"])
    store = _fake_keyring(monkeypatch, json.dumps(expired.__dict__))
    handler, calls = _auth_transport(refresh_status=400)
    real_init = auth_mod.DeviceAuth.__init__
    monkeypatch.setattr(auth_mod.DeviceAuth, "__init__",
                        lambda self, cid, transport=None: real_init(self, cid, httpx.MockTransport(handler)))
    lines = []
    tok = await auth_mod.ensure_token("cid", ["user:read:chat"], printer=lines.append)
    assert tok.access_token == "new"
    assert calls["refresh"] == 1 and calls["device"] == 1
    assert json.loads(store["v"])["access_token"] == "new"
    assert "dead-refresh" not in " ".join(lines), "never print a refresh token"


async def test_corrupted_stored_token_is_treated_as_signed_out(monkeypatch):
    _fake_keyring(monkeypatch, "{not json")
    assert auth_mod.load_token("cid") is None
    _fake_keyring(monkeypatch, json.dumps({"access_token": "x"}))  # missing fields
    assert auth_mod.load_token("cid") is None


# --- 4. EventSub: bad frames and backoff ----------------------------------------------------
def _msg(mtype, payload, mid):
    return json.dumps({"metadata": {"message_id": mid, "message_type": mtype,
                                    "message_timestamp": "2026-11-20T12:00:00.000Z"}, "payload": payload})


async def test_eventsub_survives_a_malformed_frame():
    """One broken frame from the server must cause a reconnect, not kill the chat client."""
    count = {"n": 0}

    async def server(ws):
        count["n"] += 1
        await ws.send(_msg("session_welcome", {"session": {"id": f"s{count['n']}", "keepalive_timeout_seconds": 10}},
                           f"w{count['n']}"))
        if count["n"] == 1:
            await ws.send("{this is not json")
        else:
            await ws.send(_msg("notification", {"event": {"message": {"text": "after"}}}, "n1"))
        await asyncio.sleep(10)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stop, texts = asyncio.Event(), []

        async def on_welcome(sid, survive):
            pass

        async def on_note(meta, payload):
            texts.append(payload["event"]["message"]["text"])
            stop.set()

        client = EventSubClient(on_welcome, on_note, url=f"ws://127.0.0.1:{port}/", keepalive_s=None)
        await asyncio.wait_for(client.run(stop), timeout=15)
    assert texts == ["after"] and client.stats.reconnects_unplanned == 1


def test_eventsub_backoff_resets_after_a_good_connection():
    """Backoff grows with consecutive failures only; an earlier drop hours ago must not
    make every later reconnect wait the maximum 30 s."""
    c = EventSubClient(None, None, url="ws://x", keepalive_s=None)
    c.stats.reconnects_unplanned = 7      # seven drops over a long session...
    c._consecutive_failures = 1           # ...but the last connection worked
    assert c._backoff_s() == 2.0
    c._consecutive_failures = 6
    assert c._backoff_s() == 30.0
