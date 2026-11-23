"""Deterministic chat log (TwitchClipper JSONL format) with known highlight moments."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


def write_sample_chat(path: Path, duration_s: float, event_times: list[float], seed: int = 3,
                      base_rate: float = 1.2, reactors: int = 40) -> tuple[Path, list[float]]:
    rng = random.Random(seed)
    rows = []
    t = 0.0
    while t < duration_s:
        t += rng.expovariate(base_rate)
        rows.append((t, f"viewer{rng.randrange(80)}", rng.choice(["hi", "nice", "lol", "gg", "o7", "true"])))
    for e in event_times:
        for i in range(reactors):
            # viewer delay 2-6 s + reaction 1-4 s
            rows.append((e + rng.uniform(2, 6) + rng.uniform(1, 4), f"fan{i}", rng.choice(["KEKW", "CLIP IT", "LUL", "no way"])))
    rows.sort()
    with open(path, "w", encoding="utf-8") as f:
        for t, user, text in rows:
            if t > duration_s:
                continue
            ts = (BASE + timedelta(seconds=t)).isoformat().replace("+00:00", "Z")
            f.write(json.dumps({"message_timestamp": ts, "received_at": None, "user_id": user, "user": user,
                                "text": text, "broadcaster_user_id": "1", "source_broadcaster_user_id": None}) + "\n")
    return path, event_times
