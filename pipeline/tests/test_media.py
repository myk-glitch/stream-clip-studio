"""Clip detection filters and real FFmpeg renders with a fake Whisper model."""
import json, shutil, subprocess, sys, types
from types import SimpleNamespace as NS

import pytest

import common
import live_clipper as lc
import settings
import split_layout as sl
import vod

WORDS = [(" Hello", .2, .6), (" um,", .6, .8), (" world!", .8, 1.2), ("  ", 1.2, 1.3), (" again", 1.3, 1.8)]


class FakeModel:
    def __init__(self, *a, **k):
        pass

    def transcribe(self, path, **k):
        return iter([NS(words=[NS(word=w, start=s, end=e) for w, s, e in WORDS]), NS(words=None)]), None


@pytest.fixture(autouse=True)
def fake_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))


@pytest.fixture(scope="session")
def source(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("media") / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return tuple(int(v) for v in out.split(","))


def test_spike_filter_ignores_bots_commands_and_low_content():
    assert not lc.counts_toward_spike("nightbot", "hello")
    assert not lc.counts_toward_spike("viewer", "!command")
    assert not lc.counts_toward_spike("viewer", "yes")
    assert lc.counts_toward_spike("viewer", "what a play")


def test_word_parsing_can_drop_filler():
    assert [w[0] for w in common.spoken_words(FakeModel(), "x")] == ["Hello", "um,", "world!", "again"]
    assert [w[0] for w in common.spoken_words(FakeModel(), "x", skip=sl.FILLER)] == ["Hello", "world!", "again"]


def test_spike_finder_picks_the_burst():
    msgs = [(float(i), f"u{i}", "what a play") for i in range(100, 160)] + [(float(i * 7), f"b{i}", "nice") for i in range(60)]
    assert 90 <= vod.find_spikes(msgs, 600)[0]["t"] <= 160


def test_hms_round_trip():
    assert vod.hms(3725) == "1:02:05" and vod.parse_hms("1:02:05") == 3725


def test_chat_replay_is_refused_until_enabled(configure):
    configure()
    with pytest.raises(SystemExit, match="private web API"):
        vod.fetch_chat("1", 0, 10)


@pytest.mark.parametrize("label,kind,no_captions", [("captions", "moment", False), ("no_captions", "moment", True), ("realism", "realism", False)])
def test_live_clip_render(configure, tmp_path, source, label, kind, no_captions):
    configure()
    ts = tmp_path / "chunk.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c", "copy", "-f", "mpegts", str(ts)], check=True)
    a = NS(before=1, after=1, delay=0, channel="demo", cutscene_share=1.1, no_captions=no_captions, model="base", realism_center=0.5)
    out = tmp_path / "out"
    out.mkdir()
    lc.Clipper(a, NS(window=lambda t0, t1: ([str(ts)], t0 - 0.5)), str(out)).make(lc.new_spike(1_700_000_000.0, 5, 2.0, ["a: b"], 1, final=True, kind=kind))
    folder = next(out.iterdir())
    assert size(folder / "clip_9x16.mp4") == (1080, 1920)
    assert json.loads((folder / "info.json").read_text(encoding="utf-8"))["channel"] == "demo"
    assert (folder / "captions.ass").exists() is (not no_captions and kind != "realism")


@pytest.mark.parametrize("extra", [["--channel", "demo"], ["--channel", "DEMO", "--box", "--words", "2"], ["--full", "--start", "0.5", "--end", "2.5"],
                                   ["--cam", "0,0,320,180", "--game", "400,130,700,560"]])
def test_split_layout_render(configure, tmp_path, source, monkeypatch, extra):
    configure('[streamers.demo]\ntag = "DEMO"\ncam = [0, 0, 384, 216]\ngame = [400, 130, 700, 560]\n')
    clip = tmp_path / "clip"
    clip.mkdir()
    shutil.copy(source, clip / "clip_16x9.mp4")
    target = tmp_path / "out.mp4"
    monkeypatch.setattr(sys, "argv", ["split_layout.py", str(clip), *extra, "--out", str(target)])
    sl.main()
    assert size(target) == (1080, 1920)


def test_split_layout_uses_your_sound_effect(configure, tmp_path, source, monkeypatch):
    sfx = tmp_path / "my_sounds"
    sfx.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=880:d=0.3", str(sfx / "boom.wav")], check=True)
    configure(f'[streamers.demo]\ncam = [0, 0, 384, 216]\n')
    settings.get()["paths"]["sfx_dir"] = str(sfx)
    clip = tmp_path / "clip"
    clip.mkdir()
    shutil.copy(source, clip / "clip_16x9.mp4")
    target = tmp_path / "out.mp4"
    monkeypatch.setattr(sys, "argv", ["split_layout.py", str(clip), "--channel", "demo", "--sfx", "boom@1", "--out", str(target)])
    sl.main()
    assert target.exists()
    monkeypatch.setattr(sys, "argv", ["split_layout.py", str(clip), "--channel", "demo", "--sfx", "missing@1", "--out", str(target)])
    with pytest.raises(SystemExit, match="no sound effect"):
        sl.main()
