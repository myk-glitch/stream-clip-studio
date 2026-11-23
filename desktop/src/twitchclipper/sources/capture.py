"""Capture sources for Mode D (local ring buffer): what can be recorded, and the FFmpeg arguments.

Supported source types (all through FFmpeg's own Windows input devices; nothing else is assumed):
  * DirectShow video devices ("dshow"): webcams, capture cards, OBS Virtual Camera and similar.
    Listed with `ffmpeg -list_devices true -f dshow -i dummy`. A DirectShow audio device
    (microphone, line in, "Stereo Mix" if Windows has it enabled) can be added.
  * Screen capture ("gdigrab"): the whole desktop, or one monitor when there are several.
  * A built-in test pattern (FFmpeg's lavfi generator, showing the PC's clock), so Mode D can be tried
    without any device. It is labelled as a test source, not a device.

Not supported: capturing what the PC plays (system audio) unless a DirectShow device for it exists,
since FFmpeg has no WASAPI loopback input; ddagrab/gfxcapture are available in some builds but
aren't used yet.

Capture devices deliver raw frames, which can't be stream-copied into the ring buffer's MPEG-TS
segments, so live sources are encoded (H.264 + AAC) with a keyframe every segment.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FPS = 30
SEGMENT_S = 2.0
TEST_KEY = "test:pattern"
DESKTOP_KEY = "screen:desktop"
FONT = "C\\:/Windows/Fonts/consola.ttf"

_DEVICE = re.compile(r'"(?P<name>[^"]+)"\s+\((?P<kind>[^)]+)\)')
_ALT = re.compile(r'Alternative name\s+"(?P<alt>[^"]+)"')


@dataclass(frozen=True)
class CaptureSource:
    key: str                 # stable id kept in settings
    kind: str                # "dshow" | "screen" | "test"
    label: str               # shown in the picker
    device: str = ""         # dshow: name FFmpeg opens (alternative name if the friendly name is ambiguous)
    rect: tuple[int, int, int, int] | None = None   # screen: x, y, width, height in physical pixels


@dataclass(frozen=True)
class AudioDevice:
    name: str
    device: str


@dataclass
class SourceList:
    video: list[CaptureSource] = field(default_factory=list)
    audio: list[AudioDevice] = field(default_factory=list)
    dshow_error: str | None = None       # DirectShow listing failed (FFmpeg missing, timed out...)

    def find(self, key: str) -> CaptureSource | None:
        return next((s for s in self.video if s.key == key), None)

    def find_audio(self, name: str) -> AudioDevice | None:
        return next((a for a in self.audio if a.name == name), None)

    @property
    def devices(self) -> list[CaptureSource]:
        """Real capture sources (everything except the test pattern)."""
        return [s for s in self.video if s.kind != "test"]


def parse_dshow_list(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(video, audio) lists of (friendly name, alternative name) from FFmpeg's device listing."""
    video: list[tuple[str, str]] = []
    audio: list[tuple[str, str]] = []
    last: list | None = None
    for line in text.splitlines():
        m = _DEVICE.search(line)
        if m and "Alternative name" not in line:
            kind = m.group("kind").strip().lower()
            entry = [m.group("name"), ""]
            target = video if "video" in kind else audio if "audio" in kind else None
            if target is not None:
                target.append(entry)
                last = entry
            else:
                last = None
            continue
        a = _ALT.search(line)
        if a and last is not None and not last[1]:
            last[1] = a.group("alt")
    return [tuple(v) for v in video], [tuple(a) for a in audio]


def list_dshow(ffmpeg: Path, timeout: float = 15.0) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str | None]:
    try:
        r = subprocess.run([str(ffmpeg), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [], [], f"Listing DirectShow devices took longer than {timeout:.0f} s."
    except OSError as exc:
        return [], [], f"Couldn't run FFmpeg to list devices: {exc.strerror or exc}"
    video, audio = parse_dshow_list(r.stderr + r.stdout)
    return video, audio, None


def _open_names(entries: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(label, key name, name to open) — duplicated friendly names are opened by alternative name."""
    out = []
    counts: dict[str, int] = {}
    for name, alt in entries:
        counts[name] = counts.get(name, 0) + 1
    seen: dict[str, int] = {}
    for name, alt in entries:
        if counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            label = f"{name} #{seen[name]}"
            out.append((label, label, alt or name))
        else:
            out.append((name, name, name))
    return out


def list_screens() -> list[CaptureSource]:
    """The whole desktop, plus one entry per monitor when there are several (needs a Qt GUI app)."""
    out = [CaptureSource(DESKTOP_KEY, "screen", "Screen: entire desktop")]
    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        screens = QGuiApplication.screens() if app else []
    except Exception:  # noqa: BLE001
        screens = []
    if len(screens) > 1:
        for i, sc in enumerate(screens, 1):
            g, dpr = sc.geometry(), sc.devicePixelRatio()
            rect = (round(g.x() * dpr), round(g.y() * dpr), round(g.width() * dpr) // 2 * 2,
                    round(g.height() * dpr) // 2 * 2)
            out.append(CaptureSource(f"screen:{i}", "screen", f"Screen {i}: {sc.name()} ({rect[2]}×{rect[3]})",
                                     rect=rect))
    return out


def enumerate_sources(ffmpeg: Path | None, include_test: bool = True) -> SourceList:
    """Everything the picker can offer. Slow-ish (runs FFmpeg): call from a worker thread."""
    res = SourceList()
    if ffmpeg is None:
        res.dshow_error = "FFmpeg isn't installed, so capture devices can't be listed."
    else:
        video, audio, err = list_dshow(ffmpeg)
        res.dshow_error = err
        for label, key, dev in _open_names(video):
            res.video.append(CaptureSource(f"dshow:{key}", "dshow", f"Camera / capture device: {label}", device=dev))
        res.audio = [AudioDevice(label, dev) for label, _, dev in _open_names(audio)]
    res.video += list_screens()
    if include_test:
        res.video.append(CaptureSource(TEST_KEY, "test", "Test pattern (built in, no device needed)"))
    return res


# --- FFmpeg arguments ------------------------------------------------------------------------
def input_args(src: CaptureSource, audio: AudioDevice | None = None, fps: int = FPS) -> list[str]:
    if src.kind == "dshow":
        spec = f"video={src.device}" + (f":audio={audio.device}" if audio else "")
        return ["-f", "dshow", "-rtbufsize", "256M", "-i", spec]
    if src.kind == "screen":
        args = ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1"]
        if src.rect:
            x, y, w, h = src.rect
            args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
        args += ["-i", "desktop"]
    elif src.kind == "test":
        pattern = (f"testsrc2=size=1280x720:rate={fps},"
                   f"drawtext=fontfile='{FONT}':text='PC clock %{{localtime\\:%X}}':"
                   "fontsize=56:fontcolor=white:box=1:boxcolor=black@0.7:x=(w-tw)/2:y=h-th-40")
        args = ["-re", "-f", "lavfi", "-i", pattern]
    else:
        raise ValueError(f"unknown source kind {src.kind!r}")
    if audio:
        args += ["-f", "dshow", "-rtbufsize", "64M", "-use_wallclock_as_timestamps", "1", "-i", f"audio={audio.device}"]
    elif src.kind == "test":
        args += ["-re", "-f", "lavfi", "-i", "sine=frequency=440:beep_factor=4:sample_rate=48000"]
    return args


def has_audio(src: CaptureSource, audio: AudioDevice | None) -> bool:
    return audio is not None or src.kind == "test"


def output_args(src: CaptureSource, audio: AudioDevice | None, encoder_args: list[str],
                fps: int = FPS, segment_s: float = SEGMENT_S) -> list[str]:
    """Encode to H.264/AAC with a keyframe at every segment boundary (the segmenter cuts on keyframes)."""
    gop = int(round(fps * segment_s))
    args = ["-map", "0:v"]
    if src.kind == "dshow" and audio:
        args += ["-map", "0:a"]
    elif has_audio(src, audio):
        args += ["-map", "1:a"]
    args += ["-vf", f"fps={fps},scale='min(1920,iw)':-2,format=yuv420p", *encoder_args,
             "-g", str(gop), "-force_key_frames", f"expr:gte(t,n_forced*{segment_s:g})"]
    if "libx264" in encoder_args:
        args += ["-sc_threshold", "0", "-keyint_min", str(gop)]
    if has_audio(src, audio):
        args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    return args
