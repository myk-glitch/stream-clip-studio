"""Optional Discord notifications. Webhook URLs are secrets (see docs/credentials.md); a channel without one is skipped.

usage:
  notify.py topost  VIDEO --hook "..." --caption "..." --streamer TAG   # clip for you to post by hand
  notify.py posted  VIDEO --hook "..." --streamer TAG --youtube URL [--tiktok URL|phone] [--submitted youtube,tiktok]
  notify.py ask     "question" [--choices "A|B|C"] [--context "..."]
  notify.py summary --text "..."                                        # report to the posted channel
  notify.py test    [ask|topost|posted|live|recap]                      # send a test message
"""
import argparse, json, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

import settings
from common import utf8_console

MAX_UPLOAD = 9.5 * 1024 * 1024           # Discord's free upload limit is 10 MB per message
CHANNELS = ("ask", "topost", "posted", "live", "recap")


class NotifyError(SystemExit):
    """A Discord failure, shown without the webhook URL."""


def bot():
    return settings.get()["brand"]["name"]


def webhook(kind):
    return settings.secret(f"DISCORD_WEBHOOK_{kind.upper()}")


def info(tag):
    """(display name, embed colour, campaign, emoji) for a streamer tag."""
    s = settings.streamer_by_tag(tag)
    if not s:
        return tag or "Clip", 0x5865F2, "—", "🎮"
    return s["display_name"], int(s["color"].lstrip("#"), 16), s["campaign"] or "—", s["emoji"]


def thumb(video: Path, out: Path):
    """One frame from ~40% into the clip, 540 px wide."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                               capture_output=True, text=True).stdout.strip() or 4)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * 0.4:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=540:-2", "-q:v", "3", str(out)], check=True)
    return dur


def send(kind, payload, files=()):
    """Post to a webhook. Returns False when that channel isn't configured."""
    url = webhook(kind)
    if not url:
        print(f"discord {kind}: not configured, skipped")
        return False
    payload.setdefault("username", bot())
    payload.setdefault("allowed_mentions", {"parse": []})    # clip text can never ping @everyone, roles or users
    multipart = {"payload_json": (None, json.dumps(payload), "application/json")}
    handles = []
    try:
        for i, (name, path) in enumerate(files):
            handles.append(open(path, "rb"))
            multipart[f"files[{i}]"] = (name, handles[-1])
        try:
            r = requests.post(url + "?wait=true", files=multipart, timeout=120)
        except requests.RequestException as e:      # the exception text contains the secret URL
            raise NotifyError(f"discord {kind}: request failed ({type(e).__name__})") from None
    finally:
        for h in handles:
            h.close()
    if r.status_code == 413 and len(files) > 1:     # video too big: retry with just the thumbnail
        return send(kind, payload, files[:1])
    if r.status_code >= 400:
        raise NotifyError(f"discord {kind}: HTTP {r.status_code}")
    print(f"discord {kind}: sent")
    return True


def now():
    return datetime.now(timezone.utc).isoformat()


def cmd_topost(a):
    name, colour, campaign, dot = info(a.streamer)
    video = Path(a.video).resolve()
    with tempfile.TemporaryDirectory() as td:
        jpg = Path(td) / "thumb.jpg"
        dur = thumb(video, jpg)
        files = [("thumb.jpg", jpg)]
        if video.stat().st_size <= MAX_UPLOAD:
            files.append((video.name, video))
        embed = {
            "title": "📱 Post this on TikTok",
            "description": f"**{a.hook}**\n\nCaption, ready to copy:\n```{a.caption}```",
            "color": colour,
            "fields": [
                {"name": "Streamer", "value": f"{dot} {name}", "inline": True},
                {"name": "Length", "value": f"{dur:.0f} s", "inline": True},
                {"name": "Campaign", "value": campaign, "inline": True},
                {"name": "Steps", "value": "1. Save the video below\n2. Upload it in the TikTok app with the caption\n"
                                           "3. Keep duets & stitches on\n4. Submit it to your campaign, if you're in one",
                 "inline": False},
            ],
            "image": {"url": "attachment://thumb.jpg"},
            "footer": {"text": f"{bot()} · {video.name}"},
            "timestamp": now(),
        }
        if len(files) == 1:
            embed["fields"].append({"name": "Video file", "value": f"Too big for Discord, it's in `TIKTOK_TO_POST/{video.name}`"})
        send("topost", {"content": a.caption, "embeds": [embed]}, files)


def cmd_posted(a):
    name, colour, campaign, dot = info(a.streamer)
    video = Path(a.video).resolve()
    done = {s.strip().lower() for s in (a.submitted or "").split(",") if s.strip()}
    tt = a.tiktok or "phone"
    yt_line = f"[Watch on YouTube]({a.youtube})" if a.youtube else "—"
    tt_line = (f"[Watch on TikTok]({tt})" if tt.startswith("http") else "📱 waiting for you to post")
    sub = " · ".join(f"{p.title()} {'✅' if p in done else '⏳'}" for p in ("youtube", "tiktok"))
    with tempfile.TemporaryDirectory() as td:
        jpg = Path(td) / "thumb.jpg"
        dur = thumb(video, jpg)
        embed = {
            "title": f"✅ {a.hook}",
            "url": a.youtube or None,
            "color": colour,
            "fields": [
                {"name": "YouTube Shorts", "value": yt_line, "inline": True},
                {"name": "TikTok", "value": tt_line, "inline": True},
                {"name": "Streamer", "value": f"{dot} {name}", "inline": True},
                {"name": "Campaign", "value": campaign, "inline": True},
                {"name": "Submitted", "value": sub, "inline": True},
                {"name": "Length", "value": f"{dur:.0f} s", "inline": True},
            ],
            "thumbnail": {"url": "attachment://thumb.jpg"},
            "footer": {"text": f"{bot()} · posted"},
            "timestamp": now(),
        }
        send("posted", {"embeds": [embed]}, [("thumb.jpg", jpg)])


def cmd_ask(a):
    fields = []
    if a.choices:
        fields.append({"name": "Options", "value": "\n".join(f"**{chr(65 + i)}.** {c.strip()}"
                                                               for i, c in enumerate(a.choices.split("|"))), "inline": False})
    if a.context:
        fields.append({"name": "Context", "value": a.context[:1000], "inline": False})
    embed = {"title": "❓ Your answer is needed", "description": a.question, "color": 0xFACC15, "fields": fields,
             "footer": {"text": bot()}, "timestamp": now()}
    send("ask", {"embeds": [embed]})


def cmd_summary(a):
    embed = {"title": a.title, "description": a.text[:4000], "color": 0x22C55E,
             "footer": {"text": f"{bot()} · report"}, "timestamp": now()}
    send("posted", {"embeds": [embed]})


def cmd_test(a):
    for kind in ([a.channel] if a.channel else CHANNELS):
        send(kind, {"embeds": [{"title": f"✅ Test message for the {kind} channel", "color": 0x22C55E,
                                "footer": {"text": bot()}, "timestamp": now()}]})


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("topost"); p.add_argument("video"); p.add_argument("--hook", required=True)
    p.add_argument("--caption", required=True); p.add_argument("--streamer")
    p = sub.add_parser("posted"); p.add_argument("video"); p.add_argument("--hook", required=True)
    p.add_argument("--streamer"); p.add_argument("--youtube"); p.add_argument("--tiktok"); p.add_argument("--submitted")
    p = sub.add_parser("ask"); p.add_argument("question"); p.add_argument("--choices"); p.add_argument("--context")
    p = sub.add_parser("summary"); p.add_argument("--title", default="📊 Clip report"); p.add_argument("--text", required=True)
    p = sub.add_parser("test"); p.add_argument("channel", nargs="?", choices=CHANNELS)
    a = ap.parse_args()
    {"topost": cmd_topost, "posted": cmd_posted, "ask": cmd_ask, "summary": cmd_summary, "test": cmd_test}[a.cmd](a)


if __name__ == "__main__":
    main()
