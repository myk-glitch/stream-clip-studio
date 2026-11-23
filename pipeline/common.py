"""Helpers shared by the pipeline scripts."""
import csv, sys
from datetime import datetime
from pathlib import Path

import settings

HERE = Path(__file__).resolve().parent


def post_log() -> Path:
    return settings.path("clips_dir") / "posted_log.csv"


def utf8_console():
    """Keep emoji titles and chat from crashing the Windows console."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def log_post(platform, privacy, url, title, filename):
    """Append one published post to posted_log.csv in the clips folder."""
    path = post_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["posted_at", "platform", "privacy", "url", "title", "file"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), platform, privacy, url, title, filename])


def spoken_words(model, path, skip=frozenset()):
    """Whisper word timings as (word, start, end), leaving out words in `skip` (lowercase, no punctuation)."""
    segments, _ = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    return [(w.word.strip(), w.start, w.end) for s in segments for w in (s.words or [])
            if w.word.strip() and w.word.strip().lower().strip(".,!?") not in skip]
