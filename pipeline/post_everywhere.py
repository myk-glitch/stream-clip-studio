"""Post one finished clip to YouTube Shorts (official API) and prepare or upload it for TikTok, then log it.

usage: python post_everywhere.py clips/READY_TO_POST/clip.mp4 --hook "What happened 😭" --streamer TAG
       [--privacy private|unlisted|public] [--private] [--cta "twitch.tv/name"] [--only youtube|tiktok]
       [--tiktok phone|api] [--force]

Titles, tags, privacy and the posting limits come from config.toml. TikTok defaults to a ready-to-post pack in
clips/TIKTOK_TO_POST that you upload in the app. The `api` mode uses the unofficial autotok tool and must be enabled
in config.toml first (see docs/configuration.md). No music is added on either platform.
"""
import argparse, csv, json, os, re, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

import settings
from common import HERE, log_post, post_log, utf8_console


def cap_reason(include_private=False):
    """Why a new post must wait under [posting] max_posts_per_day / min_hours_between_posts, or None."""
    p = settings.get()["posting"]
    posts = []
    if post_log().exists():
        with open(post_log(), encoding="utf-8") as f:
            posts = [r for r in csv.DictReader(f)
                     if r["platform"] == "youtube_shorts" and (include_private or r["privacy"] == "public")
                     and r["url"].startswith("http") and r["file"] and not r["file"].startswith("(")]
    today = datetime.now().strftime("%Y-%m-%d")
    todays = [r for r in posts if r["posted_at"].startswith(today)]
    if len(todays) >= p["max_posts_per_day"]:
        return f"{len(todays)} posted today"
    last = max((datetime.fromisoformat(r["posted_at"]) for r in posts), default=None)
    if last is not None and (datetime.now() - last).total_seconds() < p["min_hours_between_posts"] * 3600:
        return f"last post {int((datetime.now() - last).total_seconds() / 60)} min ago"
    return None


def queue_clip(video, a, why):
    """Hold a clip in clips/QUEUE with what's needed to post it later. A clip already there keeps its saved details."""
    p = settings.get()["posting"]
    q = settings.path("clips_dir") / "QUEUE"
    q.mkdir(parents=True, exist_ok=True)
    dest, details = q / video.name, q / (video.stem + ".json")
    already_queued = dest.exists() and dest.samefile(video)
    if not already_queued:
        shutil.copy2(video, dest)
    if not already_queued or not details.exists():
        details.write_text(json.dumps({"hook": a.hook, "streamer": a.streamer, "cta": a.cta,
                                       "queued_at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    print(f"QUEUED (limit: {p['max_posts_per_day']}/day, {p['min_hours_between_posts']:g} h apart; {why}) -> {dest}")


def render_tags(templates, tag):
    game_tag = settings.get()["game"]["tag"]
    rendered = [t.format(tag=tag, game_tag=game_tag).lstrip("#") for t in templates]
    return list(dict.fromkeys(t for t in rendered if t))


def render_title(hook, tag):
    template = settings.get()["posting"]["title_template"]
    title = template.format(hook=hook, tag=tag, game_tag=settings.get()["game"]["tag"])
    title = re.sub(r"\s*#(?=\s|$)", "", title).strip()           # drop a hashtag whose value is empty
    return title if len(title) <= 100 else hook[:100]


def build_text(a):
    """(YouTube title, YouTube tags, TikTok tags)."""
    p = settings.get()["posting"]
    if a.streamer:
        tag = a.streamer.lstrip("#")
        return render_title(a.hook, tag), render_tags(p["youtube_tags"], tag), render_tags(p["tiktok_tags"], tag)
    tags = [t.strip().lstrip("#") for t in a.tags.split(",") if t.strip()] if a.tags else list(p["default_tags"])
    return a.hook, tags, tags + ["fyp"]


def autotok_path():
    configured = settings.get()["tiktok"]["autotok_path"]
    return Path(configured).expanduser() if configured else shutil.which("autotok")


def main():
    utf8_console()
    cfg = settings.get()
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--hook", required=True, help="the clip's hook text, used as title/caption")
    ap.add_argument("--streamer", help="streamer tag from config.toml; builds the title and hashtags")
    ap.add_argument("--tags", help="manual comma-separated tags, used when --streamer is not given")
    ap.add_argument("--cta", default=None, help="extra caption line a campaign requires, e.g. 'twitch.tv/name'")
    ap.add_argument("--tiktok", choices=["phone", "api"], default="api" if cfg["tiktok"]["mode"] == "autotok" else "phone",
                    help="phone: make a pack for you to upload in the app. api: unofficial autotok upload (must be enabled)")
    ap.add_argument("--only", choices=["youtube", "tiktok"])
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default=cfg["posting"]["privacy"])
    ap.add_argument("--private", action="store_true", help="shortcut for --privacy private")
    ap.add_argument("--force", action="store_true", help="ignore the posting limits")
    a = ap.parse_args()
    if a.private:
        a.privacy = "private"
    is_private = a.privacy != "public"
    video = a.video.resolve()
    if a.cta is None:
        known = settings.streamer_by_tag(a.streamer)
        a.cta = known["cta"] if known else ""
    wants_tiktok_api = a.only in (None, "tiktok") and a.tiktok == "api"
    if wants_tiktok_api and not settings.unofficial("tiktok_autotok"):
        sys.exit("TikTok API upload uses the unofficial autotok tool and is off. It may break or violate TikTok's terms. "
                 "To accept that, set unofficial.tiktok_autotok = true in config.toml. Otherwise use --tiktok phone.")
    if wants_tiktok_api and not (cfg["tiktok"]["autotok_account"] and autotok_path()):
        sys.exit("Set tiktok.autotok_account in config.toml and install autotok (or set tiktok.autotok_path).")
    if a.only in (None, "youtube") and a.privacy == "public" and not a.force:
        why = cap_reason()
        if why:
            queue_clip(video, a, why)
            sys.exit(0)

    title, yt_tags, tt_tags = build_text(a)
    failures = []
    caption = a.hook + (" " + a.cta if a.cta else "") + " " + " ".join("#" + t for t in tt_tags)

    if a.only in (None, "youtube"):
        footer = cfg["posting"]["description_footer"]
        description = a.hook + ("\n" + a.cta if a.cta else "") + ("\n" + footer if footer else "") + "\n\n" + " ".join("#" + t for t in yt_tags)
        r = subprocess.run([sys.executable, str(HERE / "post_youtube.py"), "upload", str(video), "--title", title,
                            "--description", description, "--tags", ",".join(yt_tags), "--privacy", a.privacy],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[-300:])
        if r.returncode != 0:
            failures.append("youtube")          # post_youtube.py logs successful uploads itself

    if a.only in (None, "tiktok") and a.tiktok == "phone":
        out = settings.path("clips_dir") / "TIKTOK_TO_POST"
        out.mkdir(parents=True, exist_ok=True)
        if not ((out / video.name).exists() and (out / video.name).samefile(video)):
            shutil.copy2(video, out / video.name)
        (out / (video.stem + ".txt")).write_text(caption + "\n", encoding="utf-8")
        print(f"TikTok: ready for phone upload -> {out / video.name}\nCaption: {caption}")
        n = subprocess.run([sys.executable, str(HERE / "notify.py"), "topost", str(out / video.name), "--hook", a.hook,
                            "--caption", caption, "--streamer", a.streamer or ""],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print((n.stdout or n.stderr).strip()[-200:])
    elif a.only in (None, "tiktok"):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([str(autotok_path()), "upload", "-u", cfg["tiktok"]["autotok_account"], "-v", str(video), "-t", caption,
                            "-vi", "1" if is_private else "0", "-ct", "1", "-d", "1", "-st", "1"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        out = (r.stdout + r.stderr).strip().splitlines()
        done = [l for l in out if l.startswith("Published")]
        if r.returncode == 0 and done:
            vid = done[-1].split("id:")[-1].strip()
            log_post("tiktok", "private" if is_private else "public", f"(upload id {vid})", a.hook, video.name)
            print("TikTok: " + done[-1])
        else:
            failures.append("tiktok")
            print("TikTok failed: " + " | ".join(l for l in out[-3:] if "cookie" not in l.lower() and "session" not in l.lower()))

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
