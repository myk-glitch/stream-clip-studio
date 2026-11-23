"""Clip timing: chat burst start -> estimated event -> clip window, per mode.

All times are seconds on one timeline (Twitch server / broadcast time for live
modes, VOD offset for VOD modes).

Twitch Create Clip capture model (from the user's spec; VERIFIED ONLY BY p0_clip_timing):
    the API captures roughly [T - 85 s, T + 5 s] around the request time T.
    A clip of `duration` d is assumed to be the LAST d seconds of that capture,
    i.e. [T + 5 - d, T + 5]. (Consistent with the documented default of
    "the last 30 seconds" when d = 30.)
The documented API takes only `duration` (5-60 s); it has no start/end
timestamps, so Mode A can never guarantee the exact 27 s window. Any further
adjustment is manual, through the clip's edit_url (valid up to 24 h).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class TimingConfig:
    pre_roll_s: float = 22.0
    post_roll_s: float = 5.0
    viewer_delay_s: float = 5.0
    reaction_s: float = 3.0
    capture_before_s: float = 85.0
    capture_after_s: float = 5.0
    min_clip_s: float = 5.0
    max_clip_s: float = 60.0
    mode_b_margin_s: float = 1.0

    @property
    def target_length_s(self) -> float:
        return self.pre_roll_s + self.post_roll_s


@dataclass(frozen=True)
class Window:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start

    def contains(self, other: "Window", tol: float = 0.0) -> bool:
        return self.start <= other.start + tol and self.end >= other.end - tol


def estimate_event(burst_start: float, cfg: TimingConfig) -> float:
    """E' = burst start - viewer delay - reaction time."""
    return burst_start - cfg.viewer_delay_s - cfg.reaction_s


def target_window(event: float, cfg: TimingConfig) -> Window:
    """[E' - pre_roll, E' + post_roll]; 22 s + 5 s = 27 s by default."""
    return Window(event - cfg.pre_roll_s, event + cfg.post_roll_s)


def capture_window(call_time: float, cfg: TimingConfig) -> Window:
    return Window(call_time - cfg.capture_before_s, call_time + cfg.capture_after_s)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class ModeAStatus(str, Enum):
    # Clip starts at or before E'-22 and ends at T+5 (>= E'+5): full window, but longer than 27 s.
    COVERS_WINDOW_LONGER = "covers_window_longer_than_target"
    # Clip covers the window and is exactly the target length (only when T == E').
    COVERS_WINDOW_EXACT_LENGTH = "covers_window_target_length"
    # 60 s cap reached: pre-roll truncated, but the full window is inside the 90 s capture,
    # so it can be recovered manually with edit_url.
    PREROLL_TRUNCATED_EDIT_RECOVERABLE = "preroll_truncated_edit_url_recoverable"
    # Window starts before the capture: that footage cannot be recovered from this clip.
    OUTSIDE_CAPTURE = "window_outside_capture"
    # Event estimated after the call time (should not happen live).
    EVENT_AFTER_CALL = "event_after_call"


@dataclass(frozen=True)
class ModeAPlan:
    duration: float          # value sent as Create Clip `duration`
    approx_clip: Window      # assumed published clip [T+5-d, T+5]
    target: Window
    capture: Window
    status: ModeAStatus
    extra_tail_s: float      # footage after E'+5 included in the clip
    missing_preroll_s: float  # pre-roll not included in the published clip
    full_preroll_deadline: float      # latest T that still gets the full pre-roll (E'+33)
    edit_recoverable_deadline: float  # latest T whose capture still holds the window (E'+63)


def plan_mode_a(event: float, call_time: float, cfg: TimingConfig) -> ModeAPlan:
    """Twitch-hosted clip. Approximate by design; never an exact 27 s window."""
    target = target_window(event, cfg)
    capture = capture_window(call_time, cfg)
    clip_end = capture.end  # T + 5
    needed = clip_end - target.start  # = T - E' + 27
    duration = round(_clamp(needed, cfg.min_clip_s, cfg.max_clip_s), 1)
    clip = Window(clip_end - duration, clip_end)
    missing = max(0.0, clip.start - target.start)
    extra_tail = max(0.0, clip.end - target.end)

    if target.end > clip_end:
        status = ModeAStatus.EVENT_AFTER_CALL
    elif not capture.contains(target):
        status = ModeAStatus.OUTSIDE_CAPTURE
    elif missing > 0:
        status = ModeAStatus.PREROLL_TRUNCATED_EDIT_RECOVERABLE
    elif abs(clip.length - target.length) < 0.05:
        status = ModeAStatus.COVERS_WINDOW_EXACT_LENGTH
    else:
        status = ModeAStatus.COVERS_WINDOW_LONGER

    return ModeAPlan(
        duration=duration,
        approx_clip=clip,
        target=target,
        capture=capture,
        status=status,
        extra_tail_s=extra_tail,
        missing_preroll_s=missing,
        full_preroll_deadline=event - cfg.pre_roll_s + cfg.max_clip_s - cfg.capture_after_s,
        edit_recoverable_deadline=event - cfg.pre_roll_s + cfg.capture_before_s,
    )


@dataclass(frozen=True)
class ModeBRequest:
    duration: float
    assumed_clip: Window
    target: Window


def plan_mode_b(event: float, call_time: float, cfg: TimingConfig) -> ModeBRequest:
    """Same Create Clip call as Mode A, with a small safety margin for local trimming."""
    target = target_window(event, cfg)
    clip_end = call_time + cfg.capture_after_s
    needed = clip_end - target.start + cfg.mode_b_margin_s
    duration = round(_clamp(needed, cfg.min_clip_s, cfg.max_clip_s), 1)
    return ModeBRequest(duration, Window(clip_end - duration, clip_end), target)


@dataclass(frozen=True)
class Coverage:
    covered: bool
    missing_start_s: float
    missing_end_s: float
    trim_in_s: float   # offset into the downloaded clip where the target starts
    trim_out_s: float  # offset where the target ends


def check_coverage(target: Window, actual_clip: Window, tol_s: float = 0.05) -> Coverage:
    """Mode B: verify the downloaded clip really contains the whole target before trimming.

    `actual_clip` must be measured (clock overlay in Phase 0; calibrated mapping +
    ffprobe duration in the app), not assumed.
    """
    missing_start = max(0.0, actual_clip.start - target.start)
    missing_end = max(0.0, target.end - actual_clip.end)
    covered = missing_start <= tol_s and missing_end <= tol_s
    trim_in = max(0.0, target.start - actual_clip.start)
    return Coverage(covered, missing_start, missing_end, trim_in, trim_in + target.length)
