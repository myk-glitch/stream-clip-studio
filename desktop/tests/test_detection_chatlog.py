import json

import numpy as np

from twitchclipper.chatlog.formats import load_csv, load_jsonl, load_twitchdownloader_json
from twitchclipper.detection.detector import ChatMessage, DetectorConfig, detect, rank
from tests.support.timecode import decode_strip, draw_strip


def test_timecode_roundtrip_and_noise():
    rng = np.random.default_rng(1)
    for ms in [0, 1, 123456789, 1_795_000_000_000, (1 << 48) - 1]:
        for (h, w) in [(360, 640), (1080, 1920), (240, 426)]:
            f = np.full((h, w, 3), 90, np.uint8)
            draw_strip(f, ms)
            noisy = np.clip(f.astype(int) + rng.integers(-40, 40, f.shape), 0, 255).astype(np.uint8)
            assert decode_strip(noisy) == ms


def test_timecode_rejects_garbage():
    f = np.random.default_rng(2).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    assert decode_strip(f) is None
    assert decode_strip(np.zeros((360, 640, 3), np.uint8)) is None


def test_importers(tmp_path):
    td = {"video": {"id": "123", "title": "t"}, "comments": [
        {"content_offset_seconds": 12.5, "commenter": {"_id": "u1", "name": "a"}, "message": {"body": "KEKW"}},
        {"content_offset_seconds": 3.0, "commenter": {"_id": "u2"}, "message": {"fragments": [{"text": "hi "}, {"text": "there"}]}},
        {"broken": True},
    ]}
    p = tmp_path / "chat.json"
    p.write_text(json.dumps(td), encoding="utf-8")
    log = load_twitchdownloader_json(p)
    assert [(m.t, m.user, m.text) for m in log.messages] == [(3.0, "u2", "hi there"), (12.5, "u1", "KEKW")]
    assert log.skipped == 1 and log.meta["id"] == "123"

    c = tmp_path / "chat.csv"
    c.write_text("offset_seconds,user,message\n1.5,bob,hello\nx,bad,row\n", encoding="utf-8")
    log = load_csv(c)
    assert len(log.messages) == 1 and log.skipped == 1

    j = tmp_path / "chat.jsonl"
    rows = [
        {"message_timestamp": "2026-11-20T12:00:01Z", "user_id": "1", "text": "a", "source_broadcaster_user_id": None},
        {"message_timestamp": "2026-11-20T12:00:03Z", "user_id": "2", "text": "b", "source_broadcaster_user_id": "999"},
        {"message_timestamp": "2026-11-20T12:00:00Z", "user_id": "3", "text": "c", "source_broadcaster_user_id": "42"},
    ]
    j.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    log = load_jsonl(j, channel_id="42")
    assert [m.text for m in log.messages] == ["c", "a"]   # shared-chat message from 999 filtered
    assert log.skipped == 2


def test_spammer_alone_does_not_trigger():
    msgs = [ChatMessage(t * 0.7, f"u{t % 50}", "hi") for t in range(0, 900)]
    msgs += [ChatMessage(400 + i * 0.2, "spammer", "KEKW KEKW") for i in range(300)]
    events, _ = detect(sorted(msgs, key=lambda m: m.t), DetectorConfig())
    assert events == []


def test_burst_detected_with_onset_before_peak():
    msgs = [ChatMessage(t * 0.7, f"u{t % 50}", "hi") for t in range(0, 900)]
    msgs += [ChatMessage(400 + i * 0.1, f"r{i}", "KEKW") for i in range(80)]
    events, _ = detect(sorted(msgs, key=lambda m: m.t), DetectorConfig())
    assert len(events) == 1
    ev = events[0]
    assert 398 <= ev.burst_start <= 402 and ev.burst_start <= ev.peak_t
    assert rank(events, DetectorConfig(), top_n=1) == [ev]
