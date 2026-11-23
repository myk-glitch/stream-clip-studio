"""Mode F: local video + imported chat log -> ranked highlights -> clips.

No Twitch access and no sign-in needed.

Time alignment: `chat_offset_s` is added to every chat timestamp to get the video time.
For a chat log exported for the same VOD (offsets from VOD start) and the VOD's own
video file, it is 0. For a local recording that started later than the stream, use a
negative offset (e.g. recording began 90 s after the stream -> -90).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..chatlog.formats import ChatLog, load_any
from ..detection.detector import ChatMessage, DetectorConfig, Event, detect, rank
from ..export import ffmpeg as ft
from ..export.crop import portrait_filter
from ..timing.window import TimingConfig, Window, estimate_event, target_window

Progress = Callable[[float, str], None]


@dataclass
class Highlight:
    rank: int
    burst_start: float
    peak_t: float
    score: float
    unique_at_peak: int
    event_t: float
    window_start: float
    window_end: float
    clamped: bool = False
    id: int | None = None

    @property
    def window(self) -> Window:
        return Window(self.window_start, self.window_end)


@dataclass
class AnalysisResult:
    video: Path
    chat: Path
    chat_offset_s: float
    duration_s: float
    message_count: int
    messages_outside_video: int
    chat_format: str
    candidates: int
    highlights: list[Highlight]
    times: np.ndarray = field(repr=False)
    scores: np.ndarray = field(repr=False)
    warnings: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)


def _noop(_: float, __: str) -> None:
    pass


def analyze(ffprobe: Path, video: Path, chat: Path, chat_offset_s: float, clip_limit: int,
            dcfg: DetectorConfig, tcfg: TimingConfig, progress: Progress = _noop) -> AnalysisResult:
    progress(0.05, "Reading video information")
    info = ft.video_info(ffprobe, video)
    duration = info["duration"]
    if duration <= 0:
        raise ValueError("couldn't read the video's duration")

    progress(0.15, "Loading chat log")
    log: ChatLog = load_any(chat)
    warnings: list[str] = []
    if log.skipped:
        warnings.append(f"{log.skipped} chat lines couldn't be read and were skipped")
    shifted = [ChatMessage(m.t + chat_offset_s, m.user, m.text) for m in log.messages]
    inside = [m for m in shifted if 0.0 <= m.t <= duration]
    outside = len(shifted) - len(inside)
    if not inside:
        raise ValueError("no chat messages fall inside the video; check the chat offset")
    if outside > 0.2 * len(shifted):
        warnings.append(f"{outside} of {len(shifted)} chat messages fall outside the video; "
                        "the chat offset may be wrong")

    progress(0.35, f"Scoring {len(inside):,} chat messages")
    events, det = detect(inside, dcfg, 0.0, duration)
    progress(0.8, "Ranking highlights")
    top: list[Event] = rank(events, dcfg, top_n=clip_limit)
    highlights: list[Highlight] = []
    for i, ev in enumerate(top, start=1):
        e = estimate_event(ev.burst_start, tcfg)
        w = target_window(e, tcfg)
        clamped = w.start < 0 or w.end > duration
        ws, we = max(0.0, w.start), min(duration, w.end)
        highlights.append(Highlight(i, ev.burst_start, ev.peak_t, ev.peak_score, ev.unique_at_peak, e, ws, we, clamped))
        if clamped:
            warnings.append(f"highlight #{i} is near the start/end of the video; its clip is shorter than "
                            f"{tcfg.target_length_s:.0f} s")
    times = np.arange(len(det.scores)) * dcfg.bin_s
    progress(1.0, f"Found {len(events)} candidate moments; kept the top {len(highlights)}")
    return AnalysisResult(video, chat, chat_offset_s, duration, len(inside), outside, log.source_format,
                          len(events), highlights, times, np.asarray(det.scores), warnings,
                          {"detector": {k: sorted(v) if isinstance(v, (set, frozenset)) else v
                                        for k, v in asdict(dcfg).items() if k != "keywords"},
                           "timing": asdict(tcfg), "clip_limit": clip_limit})


@dataclass
class ExportJob:
    video: Path
    window: Window
    out: Path
    orientation: str = "landscape"     # landscape | portrait
    portrait_center: float = 0.5
    encoder: str = ft.CPU_ENCODER


@dataclass
class ExportResult:
    out: Path
    duration_s: float
    seconds: float
    encoder: str
    width: int
    height: int


DURATION_TOLERANCE_S = 0.3


def check_clip(probe: dict, expected_s: float, expect_audio: bool) -> str | None:
    """What is wrong with an exported clip (from its ffprobe JSON), or None if it is usable.
    ffmpeg can exit 0 and still write a clip that is too short (window past the end of the
    video), has no video, or lost its audio, so its exit code alone is not proof."""
    streams = probe.get("streams") or []
    if not any(s.get("codec_type") == "video" for s in streams):
        return "it has no video stream"
    if expect_audio and not any(s.get("codec_type") == "audio" for s in streams):
        return "the source has audio but the clip has none"
    try:
        duration = float((probe.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < expected_s - DURATION_TOLERANCE_S:
        return f"it is {duration:.2f} s long, shorter than the requested {expected_s:.2f} s"
    if duration > expected_s + DURATION_TOLERANCE_S:
        return f"it is {duration:.2f} s long, longer than the requested {expected_s:.2f} s (wrong duration)"
    return None


def export(ffmpeg: Path, ffprobe: Path, job: ExportJob,
           progress: Progress = _noop, cancel: Callable[[], bool] = lambda: False) -> ExportResult:
    """Frame-accurate cut (and optional 9:16 crop) of one window, at below-normal priority.

    Seeks coarsely before the input (fast) and finely after it (exact): the fine seek
    decodes from a keyframe and discards frames, which is accurate for any container.
    The finished file is probed and checked before it gets its final name; an existing
    file at `job.out` is never overwritten.
    """
    if job.out.exists():
        raise FileExistsError(f"{job.out} already exists; not overwriting it")
    job.out.parent.mkdir(parents=True, exist_ok=True)
    source_has_audio = any(s.get("codec_type") == "audio" for s in ft.probe(ffprobe, job.video).get("streams", []))
    coarse = max(0.0, job.window.start - 10.0)
    fine = job.window.start - coarse
    tmp = job.out.with_name(job.out.stem + ".part" + job.out.suffix)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1",
           "-ss", f"{coarse:.3f}", "-i", str(job.video), "-ss", f"{fine:.3f}", "-t", f"{job.window.length:.3f}"]
    if job.orientation == "portrait":
        cmd += ["-vf", portrait_filter(job.portrait_center)]
    cmd += ft.encoder_args(job.encoder) + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                                            "-movflags", "+faststart", str(tmp)]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=ft.BELOW_NORMAL_PRIORITY)
    assert proc.stdout is not None
    total_us = job.window.length * 1_000_000
    for line in proc.stdout:
        if cancel():
            proc.kill()
            proc.wait()
            tmp.unlink(missing_ok=True)
            raise InterruptedError("export cancelled")
        if line.startswith("out_time_us="):
            try:
                progress(min(1.0, int(line.split("=", 1)[1]) / total_us), "Encoding")
            except ValueError:
                pass
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {err.strip()[-500:]}")
    try:
        problem = check_clip(ft.probe(ffprobe, tmp), job.window.length, source_has_audio)
    except (RuntimeError, ValueError, OSError) as exc:   # unreadable / corrupted output
        problem = f"it can't be read ({exc})"
    if problem:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"The exported clip was not saved because {problem}.")
    try:
        tmp.rename(job.out)        # on Windows this refuses to replace a file that appeared meanwhile
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        raise
    info = ft.video_info(ffprobe, job.out)
    return ExportResult(job.out, info["duration"], time.perf_counter() - t0, job.encoder, info["width"], info["height"])


def clip_filename(video: Path, h: Highlight, orientation: str) -> str:
    def ts(sec: float) -> str:
        s = int(sec)
        return f"{s // 3600:02d}h{s % 3600 // 60:02d}m{s % 60:02d}s"
    suffix = "_9x16" if orientation == "portrait" else ""
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in video.stem)[:60].strip()
    return f"{safe}_#{h.rank}_{ts(h.event_t)}{suffix}.mp4"


def extract_frame(ffmpeg: Path, video: Path, at_s: float, out: Path, width: int = 960) -> Path:
    """Grab one frame (for the crop editor preview)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{max(0.0, at_s):.3f}",
                    "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)],
                   check=True, capture_output=True, timeout=60)
    return out
