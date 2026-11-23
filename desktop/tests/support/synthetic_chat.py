"""Synthetic chat with known event times, for testing the detector.

This checks that the detector and timing code behave as designed. It does NOT
calibrate real viewer delay or reaction time; that needs real chat (p0_chat) and
real footage (p0_clip_timing).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from twitchclipper.detection.detector import ChatMessage

FILLER = ["hi", "hello chat", "what game is this", "nice", "gg", "lol", "true", "o7", "hmm",
          "first time here", "W stream", "monkaS", "?", "LUL", "Pog"]
BURST = ["KEKW", "KEKW KEKW", "OMEGALUL", "LMAO", "CLIP IT", "clip it", "PogChamp", "WTF",
         "LUL LUL", "POGGERS", "no way", "KEKW clip it"]


@dataclass
class SyntheticEvent:
    event_t: float          # true moment on the broadcast timeline (E)
    size: int               # number of reacting viewers
    first_reaction_t: float = 0.0  # earliest chat message caused by the event


@dataclass
class SyntheticChat:
    messages: list[ChatMessage]
    events: list[SyntheticEvent]
    duration_s: float


def generate(
    duration_s: float = 3600.0,
    base_rate: float = 1.5,
    n_users: int = 400,
    n_events: int = 12,
    event_size: tuple[int, int] = (25, 120),
    viewer_delay: tuple[float, float] = (2.0, 8.0),
    reaction_mean: float = 3.0,
    spammer_rate: float = 4.0,
    drift: float = 1.0,
    seed: int = 7,
) -> SyntheticChat:
    rng = np.random.default_rng(seed)
    users = [f"user{i:04d}" for i in range(n_users)]
    delays = rng.uniform(*viewer_delay, size=n_users)
    msgs: list[ChatMessage] = []

    # Background chat with a slow rise in activity (raids, growing audience).
    t = 0.0
    while t < duration_s:
        rate = base_rate * (1.0 + drift * t / duration_s)
        t += rng.exponential(1.0 / rate)
        if t < duration_s:
            msgs.append(ChatMessage(t, users[rng.integers(n_users)], str(rng.choice(FILLER))))

    # One spammer posting constantly during a 5-minute stretch: must NOT trigger.
    s0 = duration_s * 0.3
    t = s0
    while t < s0 + 300:
        t += rng.exponential(1.0 / spammer_rate)
        msgs.append(ChatMessage(t, "spam_bot_fan", "KEKW KEKW KEKW"))

    # Events spaced out so they don't overlap.
    margin = 120.0
    slots = np.linspace(margin, duration_s - margin, n_events)
    jitter = rng.uniform(-40, 40, size=n_events)
    events: list[SyntheticEvent] = []
    for et in slots + jitter:
        size = int(rng.integers(*event_size))
        reactors = rng.choice(n_users, size=min(size, n_users), replace=False)
        first = float("inf")
        for u in reactors:
            reaction = 1.0 + rng.gamma(2.0, (reaction_mean - 1.0) / 2.0)
            mt = float(et + delays[u] + reaction)
            first = min(first, mt)
            msgs.append(ChatMessage(mt, users[u], str(rng.choice(BURST))))
            if rng.random() < 0.3:  # some viewers post twice
                msgs.append(ChatMessage(mt + rng.uniform(0.5, 4.0), users[u], str(rng.choice(BURST))))
        events.append(SyntheticEvent(float(et), size, first))

    msgs.sort(key=lambda m: m.t)
    return SyntheticChat(msgs, events, duration_s)
