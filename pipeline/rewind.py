"""Rewind a live clip: re-cut the same moment from the stream's VOD with more context before/after.
Run it after the stream ends: Twitch's in-progress VODs don't line up by time.

usage: python rewind.py CLIPDIR [--before 45] [--after 15] [--quality 720p60,720p,best]
Reads channel + spike_time from CLIPDIR/info.json, finds the VOD through the official Twitch API, and runs vod.py clip
around it. The moment is located by clock time. For exact matching against the chat replay, enable
unofficial.twitch_chat_replay in config.toml. The new folder is <clips>/<channel>_vod<ID>_<hhmmss>/.
"""
import argparse, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import settings
import twitch_api
import vod
from common import HERE, utf8_console

CHAT_LAG = 6          # seconds: chat (and our wall clock) trail the broadcast by roughly the HLS delay
LIVE_DELAY = 4        # live_clipper's --delay default: the moment is ~4 s before the chat spike


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("clipdir", type=Path)
    ap.add_argument("--before", type=int, default=45)
    ap.add_argument("--after", type=int, default=15)
    ap.add_argument("--quality", default="720p60,720p,best")
    a = ap.parse_args()
    info = json.loads((a.clipdir / "info.json").read_text(encoding="utf-8"))
    ch = info["channel"]
    spike = datetime.fromisoformat(info["spike_time"]).astimezone(timezone.utc)   # saved as local wall time
    sample = [tuple(s.split(": ", 1)) for s in info.get("chat_sample", []) if ": " in s]
    if twitch_api.streams([ch]):
        sys.exit(f"{ch} is still live: Twitch's in-progress VOD playlists don't line up by time (tested 2026-11-21, "
                 "same offset gave different moments in different renditions). Rewind after the stream ends.")
    for v in vod.list_vods(ch, n=10):
        start = datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00"))
        guess = (spike - start).total_seconds() - CHAT_LAG
        if not (0 <= guess <= v["lengthSeconds"] + 120):
            continue
        # Wall clock drifts from VOD time when the stream dropped and reconnected (the VOD skips the gap), so
        # find the clip's own chat messages in the VOD's chat replay: their offsets are exact VOD time.
        spike_off = None
        use_chat = settings.unofficial("twitch_chat_replay")
        if sample and use_chat:
            msgs = vod.fetch_chat(v["id"], max(0, int(guess) - 2400), int(guess) + 120, seg=300, workers=8)
            hits = sorted(o for o, u, t in msgs if (u, t[:200]) in {(su, st[:200]) for su, st in sample})
            if len(hits) >= 2:
                spike_off = hits[len(hits) // 2]
                print(f"matched {len(hits)} chat messages in the VOD replay")
        if spike_off is None:
            print(("chat match failed, falling back" if use_chat else "chat replay is off, using") +
                  " the wall clock (may be off if the stream reconnected)")
            spike_off = guess
        off = spike_off - LIVE_DELAY
        at = vod.hms(off)
        print(f"{ch}: moment is at {at} in VOD {v['id']} ({v['title'][:50]})")
        subprocess.run([sys.executable, str(HERE / "vod.py"), "clip", ch, "--vod", v["id"], "--at", at,
                        "--before", str(a.before), "--after", str(a.after), "--quality", a.quality,
                        "--out", str(settings.path("clips_dir"))], check=False)
        return
    sys.exit(f"no VOD of {ch} covers {spike.isoformat()} (VODs may be disabled or not processed yet)")


if __name__ == "__main__":
    main()
