"""Detector quality on synthetic chat with known events (the Phase 0 bar, kept as a regression test)."""

import pytest

from tests.support import synthetic_chat
from twitchclipper.config.settings import Settings
from twitchclipper.detection.detector import ChatMessage, detect, rank


def _score(seed, **kw):
    cfg = Settings().detector_config()
    syn = synthetic_chat.generate(seed=seed, **kw)
    events, _ = detect(syn.messages, cfg, 0.0, syn.duration_s)
    used, matched = set(), 0
    for truth in syn.events:
        for i, ev in enumerate(events):
            if i not in used and -5.0 <= ev.burst_start - truth.first_reaction_t <= 10.0:
                used.add(i)
                matched += 1
                break
    spam_start = syn.duration_s * 0.3
    spam = sum(1 for i, e in enumerate(events) if i not in used and spam_start - 5 <= e.burst_start <= spam_start + 305)
    return matched, len(syn.events), len(events) - len(used), spam


@pytest.mark.parametrize("name,kw", [
    ("medium", dict(duration_s=3600, base_rate=1.5, n_users=400)),
    ("small", dict(duration_s=3600, base_rate=0.15, n_users=60, event_size=(8, 20))),
    ("large", dict(duration_s=3600, base_rate=12.0, n_users=3000, event_size=(80, 400))),
])
def test_recall_and_false_positives(name, kw):
    total = found = fps = spam = 0
    for seed in (1, 2):
        m, n, fp, sp = _score(seed, **kw)
        found, total, fps, spam = found + m, total + n, fps + fp, spam + sp
    assert found / total >= 0.9, (name, found, total)
    assert fps <= 1, (name, fps)
    assert spam == 0


def test_offline_detects_highlight_in_first_minute_but_live_warms_up():
    msgs = [ChatMessage(t * 0.8, f"u{t % 40}", "hi") for t in range(150)]
    msgs += [ChatMessage(30 + i * 0.1, f"r{i}", "KEKW") for i in range(60)]
    msgs.sort(key=lambda m: m.t)
    cfg = Settings().detector_config()
    offline, _ = detect(msgs, cfg, 0.0, 120.0)
    live, _ = detect(msgs, cfg, 0.0, 120.0, offline=False)
    assert len(offline) == 1 and 29 <= offline[0].burst_start <= 32
    assert live == []   # live monitoring has a documented ~60 s warm-up


def test_bursts_25s_apart_are_both_kept():
    cfg = Settings().detector_config()
    msgs = [ChatMessage(t * 0.8, f"u{t % 40}", "hi") for t in range(400)]
    for start in (150.0, 175.0):
        msgs += [ChatMessage(start + i * 0.1, f"r{int(start)}_{i}", "KEKW") for i in range(50)]
    msgs.sort(key=lambda m: m.t)
    events, _ = detect(msgs, cfg, 0.0, 320.0)
    assert len(rank(events, cfg)) == 2
