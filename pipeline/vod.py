"""VOD scanner + clipper for any Twitch channel (companion to live_clipper.py).

  python vod.py list    CHANNEL [--game "Some Game"] [--since-hours 48]
  python vod.py scan    CHANNEL [--vod ID | latest VOD] [--top 12] [--from-min 0 --to-min 999]
  python vod.py clip    CHANNEL --vod ID --at 1:23:45 [--at ...]   (--at = moment in the VOD)
  python vod.py preview CHANNEL --vod ID --at 1:23:45

list uses the official Twitch API. It can only filter by game through the video title, because that API doesn't report
a video's game. scan reads the chat replay through Twitch's private web API, so it is off until you set
unofficial.twitch_chat_replay = true in config.toml. clip downloads just that window with streamlink and builds the same
9:16 captioned folder the live clipper makes (<clips>/<channel>_vod<id>_<hhmmss>/), cutscene auto-drop included.
"""
import argparse, json, os, re, shutil, statistics, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import settings  # noqa: E402
import twitch_api  # noqa: E402
from common import spoken_words  # noqa: E402
from live_clipper import (CUTSCENE_SHARE, blur_9x16_filter, counts_toward_spike, letterbox_share,  # noqa: E402
                          render_9x16, run, write_captions)

# Twitch's private web API, used only by the opt-in chat replay features. It can change or stop working at any time.
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_HEADERS = {"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"}
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
BUCKET = 10
# gifted-sub / donation reactions ("W 50", "W gifted", "WWW 100") are thanks spam, not a game moment
GIFT = re.compile(r"^\W*w+\W*\s*\d+\b|\bgift(?:ed|ing)?\b|\bsubs?\b|\bbits?\b|\bdonat", re.IGNORECASE)


def counts(user, text):
    return counts_toward_spike(user, text) and not GIFT.search(text)


def gql(payload):
    for i in range(4):
        try:
            r = requests.post(TWITCH_GQL, json=payload, headers=TWITCH_HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1 + i)
    raise RuntimeError("Twitch GQL failed")


def require_chat_replay():
    if not settings.unofficial("twitch_chat_replay"):
        sys.exit("Chat replay uses Twitch's private web API, so it is off. It can break at any time and may violate Twitch's "
                 "terms. To accept that, set unofficial.twitch_chat_replay = true in config.toml.")


def list_vods(channel, game=None, since_hours=None, n=20):
    if settings.unofficial("twitch_chat_replay"):      # the private API also reports each video's game
        q = ('{user(login:"%s"){videos(first:%d,type:ARCHIVE,sort:TIME){edges{node{id title lengthSeconds createdAt game{name}}}}}}'
             % (channel, n))
        found = [e["node"] for e in gql({"query": q})["data"]["user"]["videos"]["edges"]]
    else:
        found = twitch_api.videos(channel, limit=n)
    out = []
    for v in found:
        created = datetime.fromisoformat(v["createdAt"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if game and game.lower() not in ((v["game"] or {}).get("name") or "").lower() and game.lower() not in v["title"].lower():
            continue
        if since_hours and age_h > since_hours:
            continue
        out.append(v)
    return out


def fetch_segment(vid, start, end):
    """Chat comments with start <= offset < end. Twitch now refuses cursor paging without an integrity
    token, so we jump by offset: each request returns ~60 comments from that second on, the next request
    starts just after the last one returned. Dedupe by comment id."""
    msgs, seen, off_req = [], set(), start
    while off_req < end:
        p = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": {"videoID": vid, "contentOffsetSeconds": off_req},
              "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH}}}]
        c = ((gql(p)[0].get("data") or {}).get("video") or {}).get("comments") or {}
        edges = c.get("edges") or []
        if not edges: break
        last = off_req
        for e in edges:
            n = e["node"]
            off = n["contentOffsetSeconds"]
            last = max(last, off)
            if off < start or off >= end or n["id"] in seen: continue
            seen.add(n["id"])
            user = (n.get("commenter") or {}).get("login") or ""
            text = "".join(f.get("text", "") for f in (n["message"]["fragments"] or []))
            msgs.append((off, user, text))
        off_req = last + 1 if last >= off_req else off_req + BUCKET
    return msgs


def fetch_chat(vid, start, end, seg=600, workers=6):
    require_chat_replay()
    segs = [(s, min(s + seg, end)) for s in range(start, end, seg)]
    done = [0]
    def job(se):
        r = fetch_segment(vid, *se); done[0] += 1
        print(f"\r  chat {done[0]}/{len(segs)} segments", end="", flush=True)
        return r
    with ThreadPoolExecutor(workers) as ex:
        parts = list(ex.map(job, segs))
    print()
    return [m for p in parts for m in p]


def find_spikes(msgs, length, top=12, min_ratio=1.8, min_count=None, gap=240):
    nb = int(length // BUCKET) + 1
    users = [set() for _ in range(nb)]
    for off, user, text in msgs:
        if counts(user, text):
            users[int(off // BUCKET)].add(user.lower())
    cnt = [len(u) for u in users]
    if min_count is None:   # small chats need a lower floor than big ones
        nz = [x for x in cnt if x] or [1]
        min_count = max(6, min(25, int(3 * 2 * statistics.median(nz))))
    cand = []
    win = 30  # 5 min each side for the baseline
    for i in range(nb):
        lo, hi = max(0, i - win), min(nb, i + win + 1)
        base = max(statistics.median(cnt[lo:hi]), 1.5)
        # 20 s window (this + next bucket) so the burst isn't split
        c = cnt[i] + (cnt[i + 1] if i + 1 < nb else 0)
        ratio = c / (2 * base)
        if c >= min_count and ratio >= min_ratio:
            cand.append((ratio * (1 + min(c, 200) / 200), i, c, base))
    cand.sort(reverse=True)
    picked = []
    for score, i, c, base in cand:
        t = i * BUCKET
        if all(abs(t - p["t"]) >= gap for p in picked):
            picked.append({"t": t, "score": round(score, 2), "chatters_20s": c, "baseline_10s": base})
        if len(picked) >= top: break
    for p in picked:
        sample = [f"{u}: {x[:70]}" for o, u, x in msgs if p["t"] <= o < p["t"] + 20 and counts(u, x)][:12]
        p["sample"] = sample
    return sorted(picked, key=lambda p: -p["score"])


def hms(t):
    return f"{int(t//3600)}:{int(t%3600//60):02d}:{int(t%60):02d}"


def parse_hms(s):
    parts = [float(x) for x in s.split(":")]
    t = 0
    for x in parts: t = t * 60 + x
    return t


def game_filter(a):
    """--game, else the configured game when video games are known (the official API doesn't report them)."""
    if a.game is not None:
        return a.game
    return settings.get()["game"]["name"] if settings.unofficial("twitch_chat_replay") else ""


def scans_dir():
    return settings.path("state_dir") / "vod_scans"


def cmd_list(a):
    for v in list_vods(a.channel, game_filter(a), a.since_hours):
        print(v["id"], v["createdAt"][:16], hms(v["lengthSeconds"]), (v["game"] or {}).get("name"), "|", v["title"][:50])


def cmd_scan(a):
    require_chat_replay()
    if a.vod:
        vods = [v for v in list_vods(a.channel, n=40) if v["id"] == a.vod]
    else:
        vods = list_vods(a.channel, game_filter(a), a.since_hours or 72)[:1]
    if not vods: sys.exit("no matching VOD")
    v = vods[0]
    start = int(a.from_min * 60); end = int(min(v["lengthSeconds"], a.to_min * 60))
    print(f"VOD {v['id']} {v['title'][:50]} ({hms(v['lengthSeconds'])}), scanning {hms(start)}-{hms(end)}")
    msgs = fetch_chat(v["id"], start, end)
    spikes = find_spikes(msgs, end, a.top)
    scans_dir().mkdir(parents=True, exist_ok=True)
    with open(scans_dir() / f"{v['id']}.json", "w", encoding="utf-8") as f:
        json.dump({"vod": v, "spikes": spikes}, f, indent=1, ensure_ascii=False)
    print(f"{len(msgs)} chat messages; top {len(spikes)} spikes (score = how far above local baseline):")
    for p in spikes:
        print(f"\n  {hms(p['t'])}  score {p['score']}  {p['chatters_20s']} chatters/20s (baseline {p['baseline_10s']}/10s)")
        for s in p["sample"][:6]: print("     ", s.encode("ascii", "replace").decode())


def cmd_clip(a):
    from faster_whisper import WhisperModel
    out_root = a.out
    model = None
    for at in a.at:
        t = parse_hms(at)
        before, after = a.before, a.after
        t0 = max(0, t - before); dur = before + after
        name = f"{a.channel}_vod{a.vod}_{hms(t).replace(':', '')}"
        d = os.path.join(out_root, name); os.makedirs(d, exist_ok=True)
        ts_file = os.path.join(d, "dl.ts")
        raw = os.path.join(d, "clip_16x9.mp4")
        print(f"[{name}] downloading {hms(t0)} +{dur}s ...")
        r = subprocess.run([sys.executable, "-m", "streamlink", "--hls-start-offset", hms(t0), "--stream-segmented-duration", hms(dur + 2),
                            f"https://www.twitch.tv/videos/{a.vod}", a.quality, "-o", ts_file, "--force", "--loglevel", "warning"],
                           capture_output=True, text=True)
        if not os.path.exists(ts_file) or os.path.getsize(ts_file) < 1000:
            print("  download failed:", r.stderr[-300:]); continue
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", ts_file, "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", raw])
        os.remove(ts_file)
        lb = letterbox_share(raw)
        if lb >= CUTSCENE_SHARE:
            rej = os.path.join(out_root, "_rejected", "auto"); os.makedirs(rej, exist_ok=True)
            shutil.move(d, os.path.join(rej, name))
            print(f"  dropped: cutscene ({lb:.0%} letterboxed)"); continue
        if model is None: model = WhisperModel(a.model, device="cpu", compute_type="int8")
        ass = write_captions(spoken_words(model, raw), d)
        render_9x16(d, blur_9x16_filter(ass))
        scan = scans_dir() / f"{a.vod}.json"
        sample = []
        if os.path.exists(scan):
            for p in json.load(open(scan, encoding="utf-8"))["spikes"]:
                if abs(p["t"] - t) < 30: sample = p["sample"]
        json.dump({"channel": a.channel, "kind": "moment", "source": f"vod {a.vod}", "realism_comments": {},
                   "spike_time": f"vod+{hms(t)}", "clip_seconds": dur, "chat_sample": sample},
                  open(os.path.join(d, "info.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"  CLIP READY -> {os.path.join(d, 'clip_9x16.mp4')}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("list", "scan", "clip"):
        p = sub.add_parser(n); p.add_argument("channel")
        if n != "clip":
            p.add_argument("--game", default=None); p.add_argument("--since-hours", type=float, default=None)
        if n in ("scan", "clip"): p.add_argument("--vod")
        if n == "scan":
            p.add_argument("--top", type=int, default=12); p.add_argument("--from-min", type=float, default=0); p.add_argument("--to-min", type=float, default=99999)
        if n == "clip":
            p.add_argument("--at", action="append", required=True); p.add_argument("--before", type=int, default=18); p.add_argument("--after", type=int, default=12)
            p.add_argument("--model", default="base"); p.add_argument("--out", default=str(settings.path("clips_dir")))
            p.add_argument("--quality", default="720p60,720p,best", help="e.g. 1080p60,best for a sharper facecam in split_layout")
    p = sub.add_parser("preview"); p.add_argument("channel"); p.add_argument("--vod", required=True)
    p.add_argument("--at", action="append", required=True); p.add_argument("--span", type=int, default=90)
    p.add_argument("--out", default=str(scans_dir() / "previews"))
    a = ap.parse_args()
    {"list": cmd_list, "scan": cmd_scan, "clip": cmd_clip, "preview": cmd_preview}[a.cmd](a)


def cmd_preview(a):
    """Cheap look at a spike: 160p download of [at-span/2, at+span/2], one tile every 3 s (6 per row = 18 s/row).
    Tile k (0-based) is at  at - span/2 + 3k  seconds. Use it to pick exact --at/--before/--after for clip."""
    os.makedirs(a.out, exist_ok=True)
    for at in a.at:
        t = parse_hms(at); t0 = max(0, t - a.span / 2)
        ts_file = os.path.join(a.out, "p.ts"); jpg = os.path.join(a.out, f"{a.vod}_{hms(t).replace(':', '')}.jpg")
        subprocess.run([sys.executable, "-m", "streamlink", "--hls-start-offset", hms(t0), "--stream-segmented-duration", hms(a.span),
                        f"https://www.twitch.tv/videos/{a.vod}", "160p,360p,worst", "-o", ts_file, "--force", "--loglevel", "error"],
                       capture_output=True, text=True)
        cols = 6; rows = -(-a.span // 3 // cols)
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", ts_file, "-vf", f"fps=1/3,scale=240:-1,tile={cols}x{rows}",
             "-frames:v", "1", jpg])
        os.remove(ts_file)
        print(f"{jpg}  (tile k = {hms(t0)} + 3k s)")


if __name__ == "__main__":
    main()
