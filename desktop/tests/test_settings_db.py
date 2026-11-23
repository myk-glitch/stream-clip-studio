import pytest

from twitchclipper.config import settings as cfg
from twitchclipper.export.hwaccel import EncoderReport
from twitchclipper.storage.db import MIGRATIONS, Database
from twitchclipper.storage.paths import app_paths


def test_defaults_match_approved_design():
    s = cfg.Settings()
    assert (s.timing.pre_roll_s, s.timing.post_roll_s) == (22.0, 5.0)
    assert (s.timing.viewer_delay_s, s.timing.reaction_s) == (5.0, 3.0)
    assert s.limits.policy == "stop"
    assert s.publish.auto_create_other is False
    assert s.export.encoder == "auto"
    t = s.timing_config()
    assert t.target_length_s == 27.0


def test_settings_roundtrip_and_clamping(app_home):
    p = app_paths().ensure()
    s = cfg.Settings()
    s.timing.pre_roll_s = 30
    s.limits.clip_limit = 9
    s.detection.keywords = {"pog": 2.0}
    cfg.save(s, p.settings_file)
    loaded, warnings = cfg.load(p.settings_file)
    assert warnings == []
    assert loaded.timing.pre_roll_s == 30 and loaded.limits.clip_limit == 9 and loaded.detection.keywords == {"pog": 2.0}

    p.settings_file.write_text('[limits]\nclip_limit = 99999\npolicy = "bogus"\n[detection]\nlow = 9.0\nhigh = 2.0\n',
                               encoding="utf-8")
    loaded, warnings = cfg.load(p.settings_file)
    assert loaded.limits.clip_limit == 500 and loaded.limits.policy == "stop"
    assert loaded.detection.low < loaded.detection.high
    assert len(warnings) >= 3


def test_corrupt_settings_fall_back_to_defaults(app_home):
    p = app_paths().ensure()
    p.settings_file.write_text("this is [not toml", encoding="utf-8")
    loaded, warnings = cfg.load(p.settings_file)
    assert loaded == cfg.Settings() and "unreadable" in warnings[0]


def test_database_schema_and_operations(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.version == len(MIGRATIONS)
    db2 = Database(tmp_path / "t.db")  # re-open: no re-migration
    assert db2.version == len(MIGRATIONS)
    db2.close()

    db.add_channel("SomeStreamer")
    db.add_channel("somestreamer", "own")  # duplicate (case-insensitive) is ignored
    assert [c["login"] for c in db.channels()] == ["somestreamer"]
    for bad in ["", "has space", "x" * 26, "semi;colon"]:
        with pytest.raises(ValueError):
            db.add_channel(bad)

    hl = [{"rank": 1, "burst_start": 10.0, "peak_t": 12.0, "score": 7.5, "unique_at_peak": 20, "event_t": 2.0,
           "window_start": 0.0, "window_end": 7.0, "clamped": True}]
    aid = db.save_analysis("local", "v.mp4", "v.mp4", "c.jsonl", 0.0, 120.0, 500, {"k": 1}, hl)
    assert hl[0]["id"] and db.highlights(aid)[0]["clamped"] == 1
    cid = db.add_clip(analysis_id=aid, highlight_id=hl[0]["id"], path="x.mp4", orientation="portrait",
                      window_start=0.0, window_end=7.0, duration_s=7.0, encoder="libx264", score=7.5)
    assert db.clips()[0]["analysis_title"] == "v.mp4"
    assert db.stats() == {"clips": 1, "analyses": 1, "channels": 1}
    db.delete_clip(cid)
    assert db.clips() == []
    db.kv_set("a", {"b": [1, 2]})
    assert db.kv_get("a") == {"b": [1, 2]} and db.kv_get("missing", 5) == 5


def test_encoder_report_falls_back_to_cpu():
    rep = EncoderReport("ff", "v", [
        {"name": "h264_nvenc", "listed": True, "works": False, "seconds": 0.2, "error": "no device"},
        {"name": "h264_qsv", "listed": True, "works": True, "seconds": 1.0, "error": ""},
        {"name": "libx264", "listed": True, "works": True, "seconds": 0.9, "error": ""},
    ], auto_choice="h264_qsv")
    assert rep.resolve("auto") == ("h264_qsv", "auto")
    assert rep.resolve("h264_qsv") == ("h264_qsv", "user choice")
    enc, note = rep.resolve("h264_nvenc")
    assert enc == "libx264" and "unavailable" in note
