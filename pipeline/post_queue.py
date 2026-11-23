"""Automatic posting: publish the oldest clip in clips/QUEUE when your schedule allows it.

Off until you set auto_post.enabled = true in config.toml. Run it on a schedule (every 30 minutes works well).
Only clips you put in the queue are ever posted. Raw clips from the watcher are not.
usage: python post_queue.py [--dry-run]
"""
import argparse, json, shutil, subprocess, sys
from datetime import datetime

import settings
from common import HERE, utf8_console
from post_everywhere import cap_reason


def queued():
    """Queued clips with their saved details, oldest first."""
    q = settings.path("clips_dir") / "QUEUE"
    items = []
    for details in q.glob("*.json") if q.exists() else []:
        video = details.with_suffix(".mp4")
        if video.exists():
            info = json.loads(details.read_text(encoding="utf-8"))
            items.append((info.get("queued_at", ""), video.name, video, details, info))
    return [(v, d, i) for _, _, v, d, i in sorted(items, key=lambda x: x[:2])]


def in_posting_hours(hour):
    a = settings.get()["auto_post"]
    start, end = a["start_hour"], a["end_hour"]
    return start <= hour < end if start <= end else hour >= start or hour < end     # a window can cross midnight


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would be posted, post nothing")
    a = ap.parse_args()
    if not settings.get()["auto_post"]["enabled"]:
        print("Automatic posting is off (auto_post.enabled in config.toml).")
        return 0
    items = queued()
    if not items:
        print("Queue is empty.")
        return 0
    if not in_posting_hours(datetime.now().hour):
        print("Outside your posting hours.")
        return 0
    why = cap_reason(include_private=True)
    if why:
        print(f"Waiting: {why}.")
        return 0
    video, details, info = items[0]
    if a.dry_run:
        print(f"Would post {video.name}: {info.get('hook')}")
        return 0
    cmd = [sys.executable, str(HERE / "post_everywhere.py"), str(video), "--hook", info["hook"]]
    if info.get("streamer"):
        cmd += ["--streamer", info["streamer"]]
    if info.get("cta"):
        cmd += ["--cta", info["cta"]]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.strip() or r.stderr.strip()[-300:])
    if r.returncode != 0 or r.stdout.startswith("QUEUED"):
        return r.returncode
    posted = settings.path("clips_dir") / "POSTED"
    posted.mkdir(parents=True, exist_ok=True)
    shutil.move(str(video), posted / video.name)
    shutil.move(str(details), posted / details.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
