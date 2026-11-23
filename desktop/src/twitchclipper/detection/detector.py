"""Chat-only highlight detection.

One causal pipeline serves both modes, so live and VOD results are identical for
the same chat; they differ only in when events are emitted and how they are ranked.

    messages -> 1 s bins -> features -> trailing robust z-scores -> weighted score
    -> causal smoothing -> hysteresis (low/high thresholds) -> events
    events -> overlap suppression -> ranking -> top N (VOD)

The burst start (first bin of the region above the low threshold) is the anchor
for the event-time estimate in timing.py.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

DEFAULT_KEYWORDS: dict[str, float] = {
    "kekw": 1.0, "lul": 1.0, "lol": 0.6, "lmao": 0.8, "omegalul": 1.2, "pog": 1.0,
    "pogchamp": 1.0, "poggers": 1.0, "pogu": 1.0, "monkas": 0.8, "wtf": 0.8,
    "clip": 1.2, "clipit": 1.5, "clip it": 1.5, "w": 0.4, "?": 0.3, "lmfao": 0.8,
}
FEATURES = ("count", "unique", "keywords", "repetition")

# Chat bots post on timers and in reply to commands; their messages say nothing about the stream.
DEFAULT_IGNORED_USERS = frozenset({
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "sery_bot",
    "botrixoficial", "kofistreambot", "soundalerts", "streamstickers", "blerp", "pokemoncommunitygame",
})
# Bare answers ("Can you hear me?" -> "yes", polls -> "1"/"2"): many chatters at once, but no moment.
LOW_CONTENT_REPLIES = frozenset({
    "yes", "yeah", "yea", "ye", "yep", "yup", "y", "no", "nope", "nah", "n", "ok", "okay", "k", "kk",
    "loud", "quiet", "lag", "laggy", "here",
})
_digits_only = re.compile(r"^\d{1,3}$")


@dataclass(frozen=True)
class ChatMessage:
    t: float        # seconds on the session/VOD timeline
    user: str
    text: str


@dataclass
class DetectorConfig:
    bin_s: float = 1.0
    window_s: int = 5              # features are counted over a sliding window (reactions spread over seconds)
    baseline_window_s: float = 300.0
    baseline_update_s: int = 5     # recompute median/MAD every N bins (baseline moves slowly)
    min_baseline_s: float = 60.0
    smooth_s: int = 3
    high: float = 4.0
    low: float = 1.5
    min_unique_at_peak: int = 5    # distinct chatters inside the sliding window
    max_msgs_per_user_per_bin: int = 2
    min_separation_s: float = 20.0   # separate bursts this far apart give clips overlapping <= 7 s
    weights: dict[str, float] = field(
        default_factory=lambda: {"count": 1.0, "unique": 1.5, "keywords": 1.0, "repetition": 0.5}
    )
    keywords: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_KEYWORDS))
    # False-positive filters (messages that are skipped entirely, see counts_toward_detection)
    ignore_commands: bool = True               # "!enter", "!join", "!sr ...": giveaways, song requests
    ignore_low_content_replies: bool = True    # mass "yes" / "no" / "1" answers
    ignored_users: frozenset[str] = DEFAULT_IGNORED_USERS   # lower-case logins of chat bots


@dataclass
class Event:
    burst_start: float
    detected_at: float     # end of the bin in which the high threshold was crossed
    peak_t: float
    peak_score: float
    end_t: float
    area: float
    unique_at_peak: int


_ws = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _ws.sub(" ", text.strip().lower())


def _keyword_weight(text: str, keywords: dict[str, float]) -> float:
    norm = _normalize(text)
    tokens = set(norm.split(" "))
    best = 0.0
    for kw, w in keywords.items():
        if (" " in kw and kw in norm) or kw in tokens or (kw == "?" and "?" in norm):
            best = max(best, w)
    return best


def counts_toward_detection(m: ChatMessage, cfg: DetectorConfig) -> bool:
    """False for messages that are chat activity but not a reaction: bot accounts, !commands
    (giveaway entries, song requests) and bare one-word answers to a question or poll."""
    if cfg.ignored_users and m.user.lower() in cfg.ignored_users:
        return False
    text = m.text.strip()
    if cfg.ignore_commands and text.startswith("!"):
        return False
    if cfg.ignore_low_content_replies:
        norm = _normalize(text).strip(".!?,")
        if norm in LOW_CONTENT_REPLIES or _digits_only.match(norm):
            return False
    return True


def bin_features(messages: list[ChatMessage], t0: float, n_bins: int, cfg: DetectorConfig) -> dict[str, np.ndarray]:
    """Causal sliding-window features (window = cfg.window_s bins ending at each bin)."""
    per_bin: dict[int, list[ChatMessage]] = defaultdict(list)
    for m in messages:
        if not counts_toward_detection(m, cfg):
            continue
        i = int((m.t - t0) // cfg.bin_s)
        if 0 <= i < n_bins:
            per_bin[i].append(m)

    # Pre-compute per-message values once.
    prepared: dict[int, list[tuple[str, float, str]]] = {
        i: [(m.user, _keyword_weight(m.text, cfg.keywords), _normalize(m.text)) for m in msgs]
        for i, msgs in per_bin.items()
    }
    w = max(1, int(cfg.window_s))
    cap = cfg.max_msgs_per_user_per_bin
    out = {f: np.zeros(n_bins) for f in FEATURES}
    occupied = sorted(prepared)
    j = 0
    for i in range(n_bins):
        # Only bins with messages inside (i-w, i] matter; skip quiet stretches quickly.
        while j < len(occupied) and occupied[j] <= i - w:
            j += 1
        k = j
        if k >= len(occupied) or occupied[k] > i:
            continue
        msgs_per_user: Counter = Counter()
        best_kw: dict[str, float] = {}
        text_users: dict[str, set[str]] = defaultdict(set)
        while k < len(occupied) and occupied[k] <= i:
            for user, kw, text in prepared[occupied[k]]:
                msgs_per_user[user] += 1
                if kw > best_kw.get(user, 0.0):
                    best_kw[user] = kw
                text_users[text].add(user)
            k += 1
        # Each chatter contributes at most `cap` messages and one keyword weight per window,
        # so one person spamming can't look like a crowd.
        out["count"][i] = sum(min(c, cap) for c in msgs_per_user.values())
        out["keywords"][i] = sum(best_kw.values())
        out["unique"][i] = len(msgs_per_user)
        # Distinct users posting the most common identical message (copy-pasta / emote walls).
        best = max((len(u) for u in text_users.values()), default=0)
        out["repetition"][i] = best if best > 1 else 0
    return out


class StreamingDetector:
    """Causal detector: push one bin of features at a time."""

    def __init__(self, cfg: DetectorConfig, t0: float = 0.0, lookahead: dict[str, np.ndarray] | None = None):
        """`lookahead`: full feature arrays, for offline analysis only. During the first
        `min_baseline_s` the baseline then comes from the first `baseline_window_s` of the
        whole log, so highlights in the first minute aren't missed. Live monitoring passes
        None and has an unavoidable warm-up period."""
        self.cfg = cfg
        self.t0 = t0
        self.lookahead = lookahead
        self._cap = 4096
        self.hist = {f: np.zeros(self._cap) for f in FEATURES}
        self._n = 0
        self._base: dict[str, tuple[float, float]] = {}
        self.scores: list[float] = []
        self.raw: list[float] = []
        self.events: list[Event] = []
        self._region: dict | None = None

    def _append(self, feats: dict[str, float]) -> None:
        if self._n >= self._cap:
            self._cap *= 2
            for f in FEATURES:
                grown = np.zeros(self._cap)
                grown[: self._n] = self.hist[f][: self._n]
                self.hist[f] = grown
        for f in FEATURES:
            self.hist[f][self._n] = feats[f]
        self._n += 1

    def _refresh_baseline(self) -> None:
        cfg = self.cfg
        w = int(cfg.baseline_window_s / cfg.bin_s)
        for f in FEATURES:
            window = self.hist[f][max(0, self._n - w) : self._n]
            med = float(np.median(window))
            mad = float(np.median(np.abs(window - med))) * 1.4826
            self._base[f] = (med, max(mad, np.sqrt(max(med, 1.0))))  # Poisson floor keeps tiny chats sane

    def _lookahead_baseline(self) -> None:
        w = int(self.cfg.baseline_window_s / self.cfg.bin_s)
        for f in FEATURES:
            window = self.lookahead[f][:w]
            med = float(np.median(window))
            mad = float(np.median(np.abs(window - med))) * 1.4826
            self._base[f] = (med, max(mad, np.sqrt(max(med, 1.0))))

    def _z(self, name: str, value: float) -> float:
        warm = self._n < int(self.cfg.min_baseline_s / self.cfg.bin_s)
        if (warm and self.lookahead is None) or name not in self._base:
            return 0.0
        med, sigma = self._base[name]
        return (value - med) / sigma

    def push(self, feats: dict[str, float]) -> Event | None:
        cfg = self.cfg
        i = len(self.raw)
        warm_bins = int(cfg.min_baseline_s / cfg.bin_s)
        if i < warm_bins:
            if self.lookahead is not None and not self._base:
                self._lookahead_baseline()
        elif i % max(1, cfg.baseline_update_s) == 0 or i == warm_bins:
            self._refresh_baseline()
        wsum = sum(cfg.weights.values())
        raw = sum(cfg.weights[f] * max(self._z(f, feats[f]), 0.0) for f in FEATURES) / wsum
        self._append(feats)
        self.raw.append(raw)
        score = float(np.mean(self.raw[-cfg.smooth_s :]))
        self.scores.append(score)
        t = self.t0 + i * cfg.bin_s
        emitted = None

        if self._region is None:
            if score > cfg.low:
                self._region = {"start": i, "confirmed": None, "peak": i, "peak_score": score,
                                "area": score, "unique_peak": int(feats["unique"])}
        else:
            r = self._region
            if score > cfg.low:
                r["area"] += score
                if score > r["peak_score"]:
                    r["peak"], r["peak_score"] = i, score
                r["unique_peak"] = max(r["unique_peak"], int(feats["unique"]))
            else:
                self._close(i)
        r = self._region
        if r is not None and r["confirmed"] is None and score >= cfg.high and feats["unique"] >= cfg.min_unique_at_peak:
            r["confirmed"] = i
            ev = Event(
                burst_start=self.t0 + r["start"] * cfg.bin_s,
                detected_at=t + cfg.bin_s,
                peak_t=self.t0 + r["peak"] * cfg.bin_s,
                peak_score=r["peak_score"],
                end_t=t + cfg.bin_s,
                area=r["area"],
                unique_at_peak=r["unique_peak"],
            )
            r["event"] = ev
            self.events.append(ev)
            emitted = ev
        return emitted

    def _close(self, i: int) -> None:
        r = self._region
        self._region = None
        if r and r.get("event"):
            ev: Event = r["event"]
            ev.peak_t = self.t0 + r["peak"] * self.cfg.bin_s
            ev.peak_score = r["peak_score"]
            ev.end_t = self.t0 + i * self.cfg.bin_s
            ev.area = r["area"]
            ev.unique_at_peak = r["unique_peak"]

    def finish(self) -> None:
        if self._region is not None:
            self._close(len(self.raw))


def detect(messages: list[ChatMessage], cfg: DetectorConfig, t0: float = 0.0, t1: float | None = None,
           offline: bool = True) -> tuple[list[Event], StreamingDetector]:
    """Offline (VOD/local file) by default: the warm-up uses a look-ahead baseline."""
    if t1 is None:
        t1 = max((m.t for m in messages), default=t0) + cfg.bin_s
    n = int(np.ceil((t1 - t0) / cfg.bin_s))
    feats = bin_features(messages, t0, n, cfg)
    det = StreamingDetector(cfg, t0, lookahead=feats if offline else None)
    for i in range(n):
        det.push({f: float(feats[f][i]) for f in FEATURES})
    det.finish()
    return det.events, det


def rank(events: list[Event], cfg: DetectorConfig, top_n: int | None = None) -> list[Event]:
    """Strongest first; drop events whose peaks are closer than min_separation_s to a stronger one."""
    kept: list[Event] = []
    for ev in sorted(events, key=lambda e: e.peak_score, reverse=True):
        if all(abs(ev.peak_t - k.peak_t) >= cfg.min_separation_s for k in kept):
            kept.append(ev)
    return kept[:top_n] if top_n else kept
