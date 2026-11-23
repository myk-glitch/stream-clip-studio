"""Debug CLI (the desktop app is the main interface).

  python -m twitchclipper doctor                       FFmpeg + encoders + paths
  python -m twitchclipper hwtest                       re-test encoders
  python -m twitchclipper analyze VIDEO CHAT [--offset S] [--limit N]
  python -m twitchclipper export VIDEO START END OUT [--portrait] [--encoder NAME]
  python -m twitchclipper import VIDEO                 copy a video into the Results library
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import settings as cfg
from .export import ffmpeg as ft
from .export.hwaccel import detect
from .pipeline import library
from .pipeline import local as pipeline
from .storage.paths import app_paths
from .timing.window import Window


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="twitchclipper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("hwtest")
    a = sub.add_parser("analyze")
    a.add_argument("video", type=Path)
    a.add_argument("chat", type=Path)
    a.add_argument("--offset", type=float, default=0.0)
    a.add_argument("--limit", type=int, default=None)
    e = sub.add_parser("export")
    e.add_argument("video", type=Path)
    e.add_argument("start", type=float)
    e.add_argument("end", type=float)
    e.add_argument("out", type=Path)
    e.add_argument("--portrait", action="store_true")
    e.add_argument("--encoder", default=ft.CPU_ENCODER)
    i = sub.add_parser("import")
    i.add_argument("video", type=Path)
    args = ap.parse_args(argv)

    paths = app_paths()
    settings, warnings = cfg.load(paths.settings_file)
    inst = ft.best_install(settings.ffmpeg.path_override or None)
    if args.cmd in ("doctor", "hwtest"):
        print(f"settings: {paths.settings_file}{' (warnings: ' + '; '.join(warnings) + ')' if warnings else ''}")
        print(f"database: {paths.database}")
        print(f"clips:    {settings.export.output_dir or paths.default_clips_dir}")
        if not inst:
            print("ffmpeg:   NOT FOUND")
            return 1
        print(f"ffmpeg:   {inst.version_string} ({inst.source}: {inst.ffmpeg})")
        rep = detect(inst)
        for t in rep.tests:
            print(f"  {t['name']:<11} listed={t['listed']!s:<5} works={t['works']!s:<5} {t['error']}")
        print(f"auto encoder: {rep.auto_choice}")
        return 0
    if not inst:
        print("ffmpeg not found")
        return 1
    if args.cmd == "analyze":
        res = pipeline.analyze(inst.ffprobe, args.video, args.chat, args.offset, args.limit or settings.limits.clip_limit,
                               settings.detector_config(), settings.timing_config(),
                               lambda f, m: print(f"[{f * 100:5.1f}%] {m}"))
        print(f"{res.message_count} messages, {res.candidates} candidates")
        for h in res.highlights:
            print(f"#{h.rank} event {h.event_t:8.1f}s  window {h.window_start:8.1f}-{h.window_end:8.1f}  "
                  f"score {h.score:5.1f}  chatters {h.unique_at_peak}")
        for w in res.warnings:
            print("warning:", w)
        return 0
    if args.cmd == "export":
        job = pipeline.ExportJob(args.video, Window(args.start, args.end), args.out,
                                 "portrait" if args.portrait else "landscape", settings.export.portrait_center, args.encoder)
        r = pipeline.export(inst.ffmpeg, inst.ffprobe, job)
        print(f"wrote {r.out} ({r.duration_s:.2f}s, {r.width}x{r.height}, {r.seconds:.1f}s with {r.encoder})")
        return 0
    if args.cmd == "import":
        from .storage.db import Database
        paths.ensure()
        clips_dir = Path(settings.export.output_dir) if settings.export.output_dir else paths.default_clips_dir
        db = Database(paths.database)
        try:
            r = library.import_video(inst.ffprobe, args.video, clips_dir, db)
        finally:
            db.close()
        print(f"imported {r.original} -> {r.path} (clip id {r.clip_id}, {r.width}x{r.height}, "
              f"{r.duration_s:.1f} s, audio: {'yes' if r.has_audio else 'no'})")
        return 0
    return 2
