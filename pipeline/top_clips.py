"""List a channel's most-viewed Twitch clips through the official API (viewers already found the best moments).

usage: python top_clips.py CHANNEL [LAST_DAY|LAST_WEEK|LAST_MONTH|ALL_TIME]
Each row: views, length, created, game, vod=ID@offset_seconds (clip start in the VOD), slug, title.
Cut one with: vod.py clip CHANNEL --vod ID --at <offset+18 as H:MM:SS> --before 18 --after 14
"""
import sys

import twitch_api
from common import utf8_console

PERIODS = {"LAST_DAY": 1, "LAST_WEEK": 7, "LAST_MONTH": 30, "ALL_TIME": None}


def main():
    utf8_console()
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    login = sys.argv[1].lower()
    period = sys.argv[2].upper() if len(sys.argv) > 2 else "LAST_WEEK"
    if period not in PERIODS:
        sys.exit(f"Unknown period {period}. Use {', '.join(PERIODS)}.")
    found = twitch_api.clips(login, days=PERIODS[period], limit=25)
    names = twitch_api.games(c.get("game_id") for c in found)
    for c in found:
        game = names.get(c.get("game_id"), "?")
        print(f'{c["view_count"]:>7} {c["duration"]:>4.0f}s {c["created_at"][:16]} {game[:12]:12} '
              f'vod={c.get("video_id") or None}@{c.get("vod_offset")} {c["id"]} | {c["title"][:60]}')


if __name__ == "__main__":
    main()
