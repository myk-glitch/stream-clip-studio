"""Twitch, Discord, alerts and recap, against fake servers. Credentials must never reach output or errors."""
import csv, json, subprocess, sys
from datetime import date

import pytest
import requests

import common
import live_alert
import notify
import recap
import settings
import top_clips
import twitch_api
from conftest import Response

CLIENT, SECRET, TOKEN = "client-id-abc", "client-secret-xyz", "app-token-123"
WEBHOOK = "https://discord.invalid/api/webhooks/1/hook-secret"


@pytest.fixture
def twitch(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", CLIENT)
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", SECRET)
    log = {"tokens": 0, "gets": []}

    def post(url, data=None, timeout=None):
        assert url == twitch_api.TOKEN_URL and data == {"client_id": CLIENT, "client_secret": SECRET, "grant_type": "client_credentials"}
        log["tokens"] += 1
        return Response(data={"access_token": TOKEN, "expires_in": 5000000, "token_type": "bearer"})
    monkeypatch.setattr(requests, "post", post)
    return log


def helix(monkeypatch, log, routes):
    def get(url, params=None, headers=None, timeout=None):
        assert headers == {"Client-Id": CLIENT, "Authorization": f"Bearer {TOKEN}"}
        path = url.rsplit("/", 1)[-1]
        log["gets"].append((path, params))
        result = routes[path]
        return result(params) if callable(result) else Response(data=result)
    monkeypatch.setattr(requests, "get", get)


def test_missing_credentials_explain_what_to_do():
    with pytest.raises(twitch_api.TwitchError, match="TWITCH_CLIENT_ID"):
        twitch_api.users(["a"])


def test_token_is_cached_and_refreshed_after_401(twitch, monkeypatch):
    answers = [Response(401), Response(data={"data": [{"login": "a"}]}), Response(data={"data": [{"login": "b"}]})]
    helix(monkeypatch, twitch, {"users": lambda p: answers.pop(0)})
    assert twitch_api.users(["a"])[0]["login"] == "a" and twitch["tokens"] == 2
    assert twitch_api.users(["b"])[0]["login"] == "b" and twitch["tokens"] == 2


@pytest.mark.parametrize("failure", ["token", "api", "network"])
def test_errors_never_contain_credentials(twitch, monkeypatch, failure):
    if failure == "token":
        monkeypatch.setattr(requests, "post", lambda *a, **k: Response(403, text=SECRET))
    elif failure == "api":
        monkeypatch.setattr(requests, "get", lambda *a, **k: Response(403, text=TOKEN))
    else:
        monkeypatch.setattr(twitch_api.time, "sleep", lambda s: None)
        def down(*a, **k):
            raise requests.ConnectionError(f"failed for {TOKEN} {SECRET}")
        monkeypatch.setattr(requests, "get", down)
    with pytest.raises(twitch_api.TwitchError) as e:
        twitch_api.users(["a"])
    assert SECRET not in str(e.value) and TOKEN not in str(e.value)


def test_rate_limit_waits_and_retries(twitch, monkeypatch):
    waits = []
    monkeypatch.setattr(twitch_api.time, "sleep", waits.append)
    answers = [Response(429, headers={"Ratelimit-Reset": "0"}), Response(data={"data": [{"id": "1", "name": "G"}]})]
    helix(monkeypatch, twitch, {"games": lambda p: answers.pop(0)})
    assert twitch_api.games(["1"]) == {"1": "G"} and waits == [1.0]


def test_clips_use_a_date_window_and_sort_by_views(twitch, monkeypatch):
    clips = [{"id": "low", "view_count": 1}, {"id": "high", "view_count": 9}]
    helix(monkeypatch, twitch, {"users": {"data": [{"id": "42", "login": "demo"}]}, "clips": {"data": clips, "pagination": {}}})
    assert [c["id"] for c in twitch_api.clips("demo", days=1)] == ["high", "low"]
    params = twitch["gets"][-1][1]
    assert params["broadcaster_id"] == "42" and "started_at" in params and "ended_at" in params


def test_videos_are_paged_and_normalised(twitch, monkeypatch):
    video = {"id": "7", "title": "VOD", "created_at": "2026-01-01T00:00:00Z", "duration": "1h0m5s"}
    pages = [{"data": [video], "pagination": {"cursor": "next"}}, {"data": [{**video, "id": "8", "duration": "3m21s"}], "pagination": {}}]
    helix(monkeypatch, twitch, {"users": {"data": [{"id": "42"}]}, "videos": lambda p: Response(data=pages.pop(0))})
    found = twitch_api.videos("demo", limit=5)
    assert [(v["id"], v["lengthSeconds"], v["game"]) for v in found] == [("7", 3605, None), ("8", 201, None)]
    assert twitch["gets"][-1][1]["after"] == "next"


def test_top_clips_help_and_output(twitch, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["top_clips.py", "--help"])
    top_clips.main()
    assert "usage" in capsys.readouterr().out
    clip = {"id": "Slug", "view_count": 12, "duration": 20.0, "created_at": "2026-01-01T10:00:00Z", "game_id": "5", "video_id": "77",
            "vod_offset": 9, "title": "Big moment"}
    helix(monkeypatch, twitch, {"users": {"data": [{"id": "42"}]}, "clips": {"data": [clip], "pagination": {}},
                                "games": {"data": [{"id": "5", "name": "Some Game"}]}})
    monkeypatch.setattr(sys, "argv", ["top_clips.py", "demo", "LAST_DAY"])
    top_clips.main()
    out = capsys.readouterr().out
    assert "vod=77@9" in out and "Some Game" in out and "Slug" in out


def test_discord_is_skipped_without_a_webhook(capsys):
    assert notify.send("posted", {"embeds": []}) is False
    assert "not configured" in capsys.readouterr().out


def test_discord_errors_hide_the_webhook(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_POSTED", WEBHOOK)
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(404))
    with pytest.raises(notify.NotifyError) as e:
        notify.send("posted", {})
    assert "hook-secret" not in str(e.value)

    def down(url, **k):
        raise requests.ConnectionError(f"failed for {url}")
    monkeypatch.setattr(requests, "post", down)
    with pytest.raises(notify.NotifyError) as e:
        notify.send("posted", {})
    assert "hook-secret" not in str(e.value) and e.value.__suppress_context__


def test_discord_retries_large_upload_with_thumbnail_only(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_WEBHOOK_TOPOST", WEBHOOK)
    a, b = tmp_path / "t.jpg", tmp_path / "v.mp4"
    a.write_bytes(b"x"); b.write_bytes(b"y")
    sizes = []

    def post(url, files=None, timeout=None):
        sizes.append(len(files) - 1)
        return Response(413 if len(files) > 2 else 200)
    monkeypatch.setattr(requests, "post", post)
    assert notify.send("topost", {}, [("t.jpg", a), ("v.mp4", b)]) and sizes == [2, 1]


LIVE_CONFIG = '''
[brand]
name = "Test Brand"
[game]
name = "Some Game"
[watcher]
extra_args = ["--realism"]
[streamers.gamer]
tag = "GAMER"
min_msgs = 8
[streamers.chatter]
tag = "CHATTER"
[streamers.ignored]
watch = false
'''


def stream(i, game):
    return {"id": i, "user_login": "gamer", "title": "t", "viewer_count": 5, "started_at": "2026-01-01T00:00:00Z", "game_name": game}


def test_live_alert_state_changes(configure, monkeypatch):
    configure(LIVE_CONFIG)
    now_live, running, spawned, alerts = [], [False], [], []
    monkeypatch.setattr(live_alert.twitch_api, "streams", lambda logins: list(now_live))
    monkeypatch.setattr(live_alert.twitch_api, "users", lambda logins: [{"login": l, "display_name": l, "profile_image_url": ""} for l in logins])
    monkeypatch.setattr(live_alert, "watcher_running", lambda login: running[0])
    monkeypatch.setattr(live_alert, "start_watcher", spawned.append)
    monkeypatch.setattr(live_alert.notify, "send", lambda kind, payload, files=(): alerts.append((kind, payload)))
    monkeypatch.setattr(sys, "argv", ["live_alert.py"])

    now_live[:] = [stream("1", "Some Game")]
    live_alert.main()
    assert spawned == ["gamer"] and len(alerts) == 1 and alerts[0][0] == "live"
    assert alerts[0][1]["embeds"][0]["footer"]["text"].startswith("Test Brand")

    live_alert.main()                               # same stream, watcher died: restart quietly
    assert spawned == ["gamer", "gamer"] and len(alerts) == 1

    running[0] = True
    live_alert.main()
    assert len(spawned) == 2 and len(alerts) == 1

    now_live[:] = [stream("2", "Other Game")]       # new stream, another game: alert but no watcher
    running[0] = False
    live_alert.main()
    assert len(spawned) == 2 and len(alerts) == 2

    now_live[:] = []
    live_alert.main()
    assert json.loads(live_alert.state_file().read_text(encoding="utf-8")) == {}


def test_watcher_uses_configured_options(configure):
    configure(LIVE_CONFIG)
    args = live_alert.watcher_args("gamer")
    assert args[:2] == ["--min-msgs", "8"] and args[-1] == "--realism" and str(settings.path("clips_dir")) in args
    assert "--min-msgs" not in live_alert.watcher_args("chatter")
    assert "ignored" not in live_alert.watched()


def write_log(urls_titles):
    path = common.post_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["posted_at", "platform", "privacy", "url", "title", "file"])
        for u, t in urls_titles:
            w.writerow([f"{date.today()}T01:00:00", "youtube_shorts", "public", u, t, "x.mp4"])


CAMPAIGNS = '''
[campaigns.alpha]
name = "Alpha"
match = "alpha"
kind = "cpm"
rate_per_1k = 1.0
min_views = 1000
[campaigns.beta]
name = "Beta"
kind = "pool"
pool_threshold = 1000
'''


def field(embed, start):
    return next(f["value"] for f in embed["fields"] if f["name"].startswith(start))


def test_recap_uses_the_official_api_and_never_shows_failure_as_zero(configure, monkeypatch):
    configure(CAMPAIGNS)
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key-secret")
    write_log([("https://y/shorts/a1", "Alpha one"), ("https://y/shorts/a2", "Alpha two"), ("https://y/shorts/b1", "beta one")])

    def get(url, params=None, headers=None, timeout=None):
        assert url == recap.YOUTUBE_VIDEOS and headers == {"X-Goog-Api-Key": "yt-key-secret"} and "key" not in params
        return Response(data={"items": [{"id": "a1", "statistics": {"viewCount": "2000"}}, {"id": "b1", "statistics": {"viewCount": "500"}}]})
    monkeypatch.setattr(requests, "get", get)
    embed = recap.build_embed()
    assert "views unavailable" in field(embed, "📤") and "2,000 views" in field(embed, "📤")
    assert "(1 lookup failed, not counted)" in field(embed, "YouTube total")
    assert "est. **$2.00**" in field(embed, "💰") and "50.00% of the 1,000" in field(embed, "💰")
    assert "yt-key-secret" not in json.dumps(embed)


def test_recap_without_an_api_key_says_so(configure):
    configure()
    write_log([("https://y/shorts/a1", "Clip")])
    embed = recap.build_embed()
    assert "add YOUTUBE_API_KEY" in field(embed, "YouTube total") and "views unavailable" in field(embed, "📤")
    assert field(embed, "TikTok") == "Not configured"


def test_tiktok_stats_are_opt_in_and_failure_is_not_zero(configure, monkeypatch):
    configure('[tiktok]\nprofile = "@someone"\n[unofficial]\ntiktok_profile_stats = true\n')
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(text="blocked"))
    assert recap.tiktok_field() == "Unavailable (lookup failed)"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(text='"followerCount":3,"heartCount":1200,"videoCount":4'))
    assert recap.tiktok_field() == "3 followers · 1,200 likes · 4 posts"
