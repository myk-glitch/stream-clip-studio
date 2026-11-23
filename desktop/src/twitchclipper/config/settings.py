"""User settings, stored as TOML. Values are validated and clamped when loaded."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import tomli_w

from ..detection.detector import DEFAULT_KEYWORDS, DetectorConfig
from ..limits.policy import LimitPolicy, PublishSettings
from ..timing.window import TimingConfig

ENCODER_CHOICES = ("auto", "h264_nvenc", "h264_qsv", "h264_amf", "libx264")
ORIENTATIONS = ("landscape", "portrait", "both")


@dataclass
class DetectionSettings:
    window_s: int = 5
    high: float = 4.0
    low: float = 1.5
    min_unique: int = 5
    min_separation_s: float = 20.0
    keywords: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_KEYWORDS))


@dataclass
class TimingSettings:
    pre_roll_s: float = 22.0
    post_roll_s: float = 5.0
    viewer_delay_s: float = 5.0
    reaction_s: float = 3.0


@dataclass
class LimitSettings:
    clip_limit: int = 5
    policy: str = LimitPolicy.STOP.value


@dataclass
class PublishSettingsCfg:
    auto_create_own: bool = True
    auto_create_editor: bool = False
    auto_create_other: bool = False   # required default: off; clips need manual approval


@dataclass
class ExportSettings:
    output_dir: str = ""
    encoder: str = "auto"
    orientation: str = "landscape"
    portrait_center: float = 0.5
    precise: bool = True


@dataclass
class FfmpegSettings:
    path_override: str = ""


@dataclass
class TwitchSettings:
    client_id: str = ""


@dataclass
class CaptureSettings:
    """Mode D local capture: source, buffer length and the manual "Clip that" window."""
    source: str = ""              # CaptureSource.key last used
    audio: str = ""               # DirectShow audio device name, "" = none
    clip_before_s: float = 22.0   # footage kept from before the button press
    clip_after_s: float = 5.0     # footage recorded after it
    buffer_s: float = 120.0       # rolling buffer length


CAPTURE_LIMITS = {"clip_before_s": (1.0, 120.0), "clip_after_s": (0.0, 60.0), "buffer_s": (60.0, 1800.0)}
BUFFER_MARGIN_S = 30.0            # buffer must hold the whole clip window plus this much


@dataclass
class Settings:
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    timing: TimingSettings = field(default_factory=TimingSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
    publish: PublishSettingsCfg = field(default_factory=PublishSettingsCfg)
    export: ExportSettings = field(default_factory=ExportSettings)
    ffmpeg: FfmpegSettings = field(default_factory=FfmpegSettings)
    twitch: TwitchSettings = field(default_factory=TwitchSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)

    # --- conversions into the engine's config objects ----------------------
    def detector_config(self) -> DetectorConfig:
        d = self.detection
        return DetectorConfig(window_s=d.window_s, high=d.high, low=d.low, min_unique_at_peak=d.min_unique,
                              min_separation_s=d.min_separation_s, keywords=dict(d.keywords))

    def timing_config(self) -> TimingConfig:
        t = self.timing
        return TimingConfig(pre_roll_s=t.pre_roll_s, post_roll_s=t.post_roll_s,
                            viewer_delay_s=t.viewer_delay_s, reaction_s=t.reaction_s)

    def publish_settings(self) -> PublishSettings:
        p = self.publish
        return PublishSettings(p.auto_create_own, p.auto_create_editor, p.auto_create_other)

    def validate(self) -> list[str]:
        """Clamp out-of-range values in place; return human-readable warnings."""
        warn: list[str] = []

        def clamp(obj, name, lo, hi):
            v = getattr(obj, name)
            nv = min(hi, max(lo, v))
            if nv != v:
                warn.append(f"{type(obj).__name__}.{name}={v} out of range, using {nv}")
                setattr(obj, name, type(v)(nv))

        d, t, l, e = self.detection, self.timing, self.limits, self.export
        clamp(d, "window_s", 1, 30)
        clamp(d, "high", 0.5, 50.0)
        clamp(d, "low", 0.1, 50.0)
        if d.low >= d.high:
            warn.append("detection.low must be below detection.high; resetting both to defaults")
            d.low, d.high = DetectionSettings.low, DetectionSettings.high
        clamp(d, "min_unique", 1, 1000)
        clamp(d, "min_separation_s", 0.0, 3600.0)
        for name in ("pre_roll_s", "post_roll_s", "viewer_delay_s", "reaction_s"):
            clamp(t, name, 0.0, 120.0)
        if t.pre_roll_s + t.post_roll_s < 1.0:
            warn.append("clip window shorter than 1 s; restoring 22 s + 5 s")
            t.pre_roll_s, t.post_roll_s = 22.0, 5.0
        clamp(l, "clip_limit", 1, 500)
        if l.policy not in {p.value for p in LimitPolicy}:
            warn.append(f"unknown limit policy {l.policy!r}; using 'stop'")
            l.policy = LimitPolicy.STOP.value
        if e.encoder not in ENCODER_CHOICES:
            warn.append(f"unknown encoder {e.encoder!r}; using auto")
            e.encoder = "auto"
        if e.orientation not in ORIENTATIONS:
            e.orientation = "landscape"
        clamp(e, "portrait_center", 0.0, 1.0)
        c = self.capture
        for name, (lo, hi) in CAPTURE_LIMITS.items():
            v = getattr(c, name)
            if not isinstance(v, (int, float)) or v != v:          # NaN never compares equal
                warn.append(f"capture.{name} invalid; using default")
                setattr(c, name, getattr(CaptureSettings, name))
            else:
                clamp(c, name, lo, hi)
        need = c.clip_before_s + c.clip_after_s + BUFFER_MARGIN_S
        if c.buffer_s < need:
            warn.append(f"capture buffer {c.buffer_s:g} s can't hold a {c.clip_before_s:g} s + {c.clip_after_s:g} s clip; "
                        f"using {need:g} s")
            c.buffer_s = need
        return warn


def _merge(dc: Any, data: dict) -> None:
    for f in fields(dc):
        if f.name not in data:
            continue
        cur = getattr(dc, f.name)
        val = data[f.name]
        if is_dataclass(cur) and isinstance(val, dict):
            _merge(cur, val)
        elif isinstance(cur, dict) and isinstance(val, dict):
            setattr(dc, f.name, {str(k): float(v) for k, v in val.items()})
        elif isinstance(cur, bool):
            setattr(dc, f.name, bool(val))
        elif isinstance(cur, (int, float)) and isinstance(val, (int, float)) and not isinstance(val, bool):
            setattr(dc, f.name, type(cur)(val))
        elif isinstance(cur, str) and isinstance(val, str):
            setattr(dc, f.name, val)


def load(path: Path) -> tuple[Settings, list[str]]:
    s = Settings()
    warnings: list[str] = []
    if path.exists():
        try:
            _merge(s, tomllib.loads(path.read_text(encoding="utf-8")))
        except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
            warnings.append(f"settings file unreadable ({exc}); using defaults")
            s = Settings()
    warnings += s.validate()
    return s, warnings


def save(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(asdict(settings)), encoding="utf-8")
    tmp.replace(path)
