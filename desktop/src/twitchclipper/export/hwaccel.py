"""Encoder detection with a real test encode per hardware encoder, cached per ffmpeg build."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import ffmpeg as ft

CACHE_KEY = "encoder_report_v1"


@dataclass
class EncoderReport:
    ffmpeg_path: str
    ffmpeg_version: str
    tests: list[dict] = field(default_factory=list)
    auto_choice: str = ft.CPU_ENCODER

    def works(self, name: str) -> bool:
        if name == ft.CPU_ENCODER:
            return any(t["name"] == name and t["works"] for t in self.tests) or not self.tests
        return any(t["name"] == name and t["works"] for t in self.tests)

    def resolve(self, preference: str) -> tuple[str, str]:
        """(encoder to use, note). Falls back to CPU if the preferred one doesn't work."""
        if preference in ("", "auto"):
            return self.auto_choice, "auto"
        if self.works(preference):
            return preference, "user choice"
        return ft.CPU_ENCODER, f"{preference} unavailable on this PC; using CPU (libx264)"


def detect(install: ft.FfmpegInstall) -> EncoderReport:
    listed = ft.list_encoders(install.ffmpeg)
    tests = [ft.test_encoder(install.ffmpeg, n, n in listed) for n in ft.HW_ENCODERS + [ft.CPU_ENCODER]]
    return EncoderReport(str(install.ffmpeg), install.version_string,
                         [t.__dict__ for t in tests], ft.choose_encoder(tests))


def cached_or_detect(db, install: ft.FfmpegInstall, force: bool = False) -> EncoderReport:
    """Re-test only when ffmpeg changes (path or version) or when forced."""
    cached = db.kv_get(CACHE_KEY) if db is not None else None
    if (not force and cached and cached.get("ffmpeg_path") == str(install.ffmpeg)
            and cached.get("ffmpeg_version") == install.version_string):
        return EncoderReport(**cached)
    report = detect(install)
    if db is not None:
        db.kv_set(CACHE_KEY, asdict(report))
    return report
