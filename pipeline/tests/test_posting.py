import csv, json, subprocess, sys
from datetime import datetime, timedelta

import pytest

import common
import post_everywhere as pe
import post_queue as pq
import settings

CONFIG = '''
[game]
name = "Some Game"
tag = "SomeGame"
[posting]
privacy = "public"
max_posts_per_day = 2
min_hours_between_posts = 4
[streamers.demo]
tag = "DEMO"
cta = "twitch.tv/demo"
'''


def seed(rows):
    path = common.post_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["posted_at", "platform", "privacy", "url", "title", "file"])
        for when, privacy in rows:
            w.writerow([when.isoformat(timespec="seconds"), "youtube_shorts", privacy, "https://y/1", "t", "a.mp4"])


@pytest.fixture
def fake_tools(monkeypatch):
    """Records uploader and notifier calls instead of running them."""
    calls = []

    def run(cmd, *a, **k):
        calls.append([str(c) for c in cmd])
        text = " ".join(calls[-1])
        if "autotok" in text:
            return subprocess.CompletedProcess(cmd, 0, "Published ok id: 999\n", "")
        return subprocess.CompletedProcess(cmd, 0, "Uploaded: https://youtube.com/shorts/NEW (public)\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def post(monkeypatch, video, *args):
    monkeypatch.setattr(sys, "argv", ["post_everywhere.py", str(video), *args])
    with pytest.raises(SystemExit) as e:
        pe.main()
    return e.value.code


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video")
    return path


def test_limit_per_day_and_spacing(configure):
    configure(CONFIG)
    assert pe.cap_reason() is None
    now = datetime.now()
    seed([(now - timedelta(minutes=30), "public")])
    assert "min ago" in pe.cap_reason()
    midnight = now.replace(hour=0, minute=0, second=1)
    seed([(midnight, "public"), (midnight, "public")])
    assert "2 posted today" == pe.cap_reason()
    seed([(now - timedelta(minutes=30), "private")])
    assert pe.cap_reason() is None
    assert "min ago" in pe.cap_reason(include_private=True)


def test_limits_are_configurable(configure):
    configure(CONFIG.replace("max_posts_per_day = 2", "max_posts_per_day = 5").replace("min_hours_between_posts = 4", "min_hours_between_posts = 0"))
    midnight = datetime.now().replace(hour=0, minute=0, second=1)
    seed([(midnight, "public")] * 4)
    assert pe.cap_reason() is None
    seed([(midnight, "public")] * 5)
    assert pe.cap_reason() == "5 posted today"


def test_title_and_tags_come_from_config(configure, monkeypatch, clip, fake_tools):
    configure(CONFIG)
    assert post(monkeypatch, clip, "--hook", "Big moment", "--streamer", "DEMO") == 0
    upload = fake_tools[0]
    assert upload[upload.index("--title") + 1] == "Big moment #DEMO #SomeGame"
    assert upload[upload.index("--tags") + 1] == "DEMO,SomeGame,Shorts,gaming"
    assert upload[upload.index("--privacy") + 1] == "public"
    assert "twitch.tv/demo" in upload[upload.index("--description") + 1]
    caption = (settings.path("clips_dir") / "TIKTOK_TO_POST" / "clip.txt").read_text(encoding="utf-8")
    assert caption == "Big moment twitch.tv/demo #DEMO #SomeGame #gaming #fyp\n"


def test_empty_game_tag_leaves_no_stray_hashtag(configure):
    configure(CONFIG.replace('tag = "SomeGame"', 'tag = ""'))
    assert pe.render_title("Moment", "DEMO") == "Moment #DEMO"
    assert pe.render_tags(["{tag}", "{game_tag}", "Shorts"], "DEMO") == ["DEMO", "Shorts"]
    assert pe.render_title("x" * 99, "DEMO") == "x" * 99


def test_capped_clip_is_queued_not_uploaded(configure, monkeypatch, clip, fake_tools):
    configure(CONFIG)
    seed([(datetime.now() - timedelta(minutes=5), "public")])
    assert post(monkeypatch, clip, "--hook", "Later", "--streamer", "DEMO") == 0
    assert fake_tools == []
    saved = json.loads((settings.path("clips_dir") / "QUEUE" / "clip.json").read_text(encoding="utf-8"))
    assert saved["hook"] == "Later" and saved["streamer"] == "DEMO"


def test_requeueing_a_queued_clip_keeps_it_intact(configure, monkeypatch, fake_tools):
    configure(CONFIG)
    seed([(datetime.now() - timedelta(minutes=5), "public")])
    q = settings.path("clips_dir") / "QUEUE"
    q.mkdir(parents=True)
    (q / "kept.mp4").write_bytes(b"video")
    details = {"hook": "Original", "streamer": "DEMO", "cta": "", "queued_at": "2026-01-01T00:00:00"}
    (q / "kept.json").write_text(json.dumps(details), encoding="utf-8")
    assert post(monkeypatch, q / "kept.mp4", "--hook", "Original", "--streamer", "DEMO") == 0
    assert json.loads((q / "kept.json").read_text(encoding="utf-8")) == details and (q / "kept.mp4").read_bytes() == b"video"


def test_unofficial_tiktok_upload_is_refused_until_enabled(configure, monkeypatch, clip, fake_tools):
    configure(CONFIG)
    code = post(monkeypatch, clip, "--hook", "H", "--tiktok", "api", "--only", "tiktok")
    assert "unofficial" in str(code) and fake_tools == []


def test_enabled_autotok_uses_the_users_account(configure, monkeypatch, clip, fake_tools, tmp_path):
    tool = tmp_path / "autotok.exe"
    tool.write_bytes(b"")
    configure(CONFIG + f'[unofficial]\ntiktok_autotok = true\n[tiktok]\nmode = "autotok"\nautotok_account = "someone"\nautotok_path = "{tool.as_posix()}"\n')
    assert post(monkeypatch, clip, "--hook", "H", "--only", "tiktok") == 0
    assert fake_tools[0][fake_tools[0].index("-u") + 1] == "someone"
    assert list(csv.reader(open(common.post_log(), encoding="utf-8")))[1][1] == "tiktok"


def queue_clip(name, when, hook="Hook"):
    q = settings.path("clips_dir") / "QUEUE"
    q.mkdir(parents=True, exist_ok=True)
    (q / f"{name}.mp4").write_bytes(b"video")
    (q / f"{name}.json").write_text(json.dumps({"hook": hook, "streamer": "DEMO", "cta": "", "queued_at": when}), encoding="utf-8")


AUTO = CONFIG + "[auto_post]\nenabled = true\nstart_hour = 0\nend_hour = 24\n"


def run_queue(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["post_queue.py", *args])
    return pq.main()


def test_auto_post_is_off_by_default(configure, monkeypatch, fake_tools, capsys):
    configure(CONFIG)
    queue_clip("a", "2026-01-01T00:00:00")
    assert run_queue(monkeypatch) == 0 and fake_tools == [] and "off" in capsys.readouterr().out


def test_auto_post_publishes_only_the_oldest_clip(configure, monkeypatch, fake_tools):
    configure(AUTO)
    queue_clip("newer", "2026-01-02T00:00:00")
    queue_clip("older", "2026-01-01T00:00:00", hook="First")
    assert run_queue(monkeypatch) == 0
    assert len(fake_tools) == 1 and "older.mp4" in fake_tools[0][2] and "First" in fake_tools[0]
    clips = settings.path("clips_dir")
    assert (clips / "POSTED" / "older.mp4").exists() and (clips / "POSTED" / "older.json").exists()
    assert (clips / "QUEUE" / "newer.mp4").exists()


def test_auto_post_respects_limit_hours_and_dry_run(configure, monkeypatch, fake_tools, capsys):
    configure(AUTO)
    queue_clip("a", "2026-01-01T00:00:00")
    assert run_queue(monkeypatch, "--dry-run") == 0 and fake_tools == [] and "Would post" in capsys.readouterr().out
    seed([(datetime.now() - timedelta(minutes=5), "private")])
    assert run_queue(monkeypatch) == 0 and fake_tools == []
    assert (settings.path("clips_dir") / "QUEUE" / "a.mp4").exists()


@pytest.mark.parametrize("start,end,hour,expected", [(9, 22, 9, True), (9, 22, 22, False), (22, 6, 23, True), (22, 6, 3, True), (22, 6, 12, False)])
def test_posting_window(configure, start, end, hour, expected):
    configure(f"[auto_post]\nstart_hour = {start}\nend_hour = {end}\n")
    assert pq.in_posting_hours(hour) is expected


def test_failed_auto_post_stays_in_queue(configure, monkeypatch):
    configure(AUTO)
    queue_clip("a", "2026-01-01T00:00:00")
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    assert run_queue(monkeypatch) == 1
    assert (settings.path("clips_dir") / "QUEUE" / "a.mp4").exists()
