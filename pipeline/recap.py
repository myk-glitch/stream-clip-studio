"""Daily recap to Discord (channel "recap"): today's posts, YouTube views, optional TikTok totals, and campaign progress.

YouTube views use the official Data API with your YOUTUBE_API_KEY. Schedule it once a day (see docs/configuration.md).
usage: python recap.py [--print]
"""
import argparse, csv, json, re
from collections import defaultdict
from datetime import datetime, timezone

import requests

import notify
import settings
from common import post_log, utf8_console

YOUTUBE_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept-Language": "en-US"}


def video_id(url):
    return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def yt_views(urls):
    """url -> view count. A failed lookup is None, so a failure can never read as 0 views."""
    views = {u: None for u in urls}
    key = settings.secret("YOUTUBE_API_KEY")
    if not key:
        return views
    by_id = {video_id(u): u for u in urls}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        try:
            r = requests.get(YOUTUBE_VIDEOS, params={"part": "statistics", "id": ",".join(ids[i:i + 50])},
                             headers={"X-Goog-Api-Key": key}, timeout=20)     # a header keeps the key out of URLs and errors
            if r.status_code != 200:
                continue
            for item in r.json().get("items", []):
                count = item.get("statistics", {}).get("viewCount")
                if count is not None and item.get("id") in by_id:
                    views[by_id[item["id"]]] = int(count)
        except (requests.RequestException, ValueError):
            continue
    return views


def tiktok_totals():
    """(followers, likes, videos), or None if the profile couldn't be read. Unofficial, so off by default."""
    profile = settings.get()["tiktok"]["profile"].lstrip("@")
    try:
        t = requests.get(f"https://www.tiktok.com/@{profile}", headers=UA, timeout=20).text
    except requests.RequestException:
        return None
    found = [re.search(rf'"{k}":(\d+)', t) for k in ("followerCount", "heartCount", "videoCount")]
    return tuple(int(m.group(1)) for m in found) if all(found) else None


def tiktok_field():
    if not (settings.unofficial("tiktok_profile_stats") and settings.get()["tiktok"]["profile"]):
        return "Not configured"
    totals = tiktok_totals()
    return "{:,} followers · {:,} likes · {} posts".format(*totals) if totals else "Unavailable (lookup failed)"


def campaign_of(title):
    t = title.lower()
    return next((key for key, c in settings.get()["campaigns"].items() if c["match"] in t), None)


def failed_note(n):
    return f" ({n} lookup{'' if n == 1 else 's'} failed, not counted)" if n else ""


def build_embed():
    rows = list(csv.DictReader(open(post_log(), encoding="utf-8"))) if post_log().exists() else []
    shorts = {}                                    # url -> (title, date); the latest row wins
    for r in rows:
        if r["platform"] == "youtube_shorts" and r["url"].startswith("http"):
            shorts[r["url"]] = (r["title"], r["posted_at"][:10])
    today = datetime.now().strftime("%Y-%m-%d")
    views = yt_views(list(shorts))
    known = {u: v for u, v in views.items() if v is not None}
    per = defaultdict(lambda: [0, 0, 0.0, 0])       # campaign -> [clips, views, est $, failed lookups]
    campaigns = settings.get()["campaigns"]
    for u, (title, _) in shorts.items():
        key = campaign_of(title)
        if not key:
            continue
        rule = campaigns[key]
        per[key][0] += 1
        if u not in known:
            per[key][3] += 1
            continue
        per[key][1] += known[u]
        if rule["kind"] == "cpm" and known[u] >= rule["min_views"]:
            per[key][2] += known[u] / 1000 * rule["rate_per_1k"]
    today_posts = [(t, u) for u, (t, d) in shorts.items() if d == today]
    top = sorted(known, key=known.get, reverse=True)[:3]

    lines = []
    for key, rule in campaigns.items():
        clips, v, est, failed = per[key]
        if not clips:
            continue
        if rule["kind"] == "pool" and rule["pool_threshold"]:
            pct = v / rule["pool_threshold"] * 100
            lines.append(f"**{rule['name']}**: {clips} clips · {v:,} views{failed_note(failed)} · {pct:.2f}% of the "
                         f"{rule['pool_threshold']:,} needed for a share")
        else:
            lines.append(f"**{rule['name']}**: {clips} clips · {v:,} views{failed_note(failed)} · est. **${est:.2f}** "
                         f"(clips under {rule['min_views']:,} views pay $0)")
    total = f"{sum(known.values()):,} views · {len(shorts)} Shorts{failed_note(len(shorts) - len(known))}"
    if not settings.secret("YOUTUBE_API_KEY"):
        total = f"{len(shorts)} Shorts · add YOUTUBE_API_KEY to see views"
    fields = [
        {"name": f"📤 Posted today ({len(today_posts)})",
         "value": "\n".join(f"• [{t[:60]}]({u}) · " + (f"{views[u]:,} views" if u in known else "views unavailable")
                            for t, u in today_posts)[:1024] or "Nothing today", "inline": False},
        {"name": "🏆 Top Shorts (all time)",
         "value": "\n".join(f"{i + 1}. [{shorts[u][0][:55]}]({u}) · **{known[u]:,}**" for i, u in enumerate(top))[:1024] or "—",
         "inline": False},
        {"name": "💰 Campaigns", "value": "\n".join(lines)[:1024] or "—", "inline": False},
        {"name": "YouTube total", "value": total, "inline": True},
        {"name": "TikTok account", "value": tiktok_field(), "inline": True},
    ]
    return {"title": f"📊 Daily recap · {datetime.now().strftime('%a %b %d')}", "color": 0x22C55E, "fields": fields,
            "footer": {"text": f"{notify.bot()} · YouTube view counts can lag a few hours"},
            "timestamp": datetime.now(timezone.utc).isoformat()}


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="print instead of sending")
    a = ap.parse_args()
    embed = build_embed()
    if a.print:
        print(json.dumps(embed, indent=1, ensure_ascii=False))
    else:
        notify.send("recap", {"embeds": [embed]})


if __name__ == "__main__":
    main()
