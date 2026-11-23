"""Upload a finished clip to YouTube as a Short, through the official YouTube Data API.

One-time setup (details in docs/credentials.md):
  1. Create your own OAuth client (Desktop app) and save its file where `python configure.py doctor` says.
  2. python post_youtube.py auth      -> your browser opens Google's own sign-in; you approve "upload videos"

Then:
  python post_youtube.py upload "clips/READY_TO_POST/x.mp4" --title "..." [--privacy private|unlisted|public]
  python post_youtube.py upload ... --dry-run      (checks everything, uploads nothing)

Safety: uploads are PRIVATE unless --privacy says otherwise. Only the "youtube.upload" permission is
requested. The sign-in token is kept in your OS keychain, never in a file or the log.
"""
from __future__ import annotations

import argparse, json, random, subprocess, time
from pathlib import Path

import settings
from common import log_post, utf8_console

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
KEYRING_SERVICE, KEYRING_USER = settings.KEYRING_SERVICE, "youtube-oauth-token"
SHORTS_MAX_S = 180.0          # YouTube Shorts: up to 3 minutes, vertical or square
RETRY_STATUS = {500, 502, 503, 504}


# --- checks ---------------------------------------------------------------------------------
def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise SystemExit(f"Can't read {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def check_short(path: Path) -> tuple[float, int, int]:
    """Refuse anything YouTube wouldn't treat as a Short (or that is broken)."""
    if not path.is_file():
        raise SystemExit(f"Not found: {path}")
    info = probe(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise SystemExit("The file has no video stream.")
    if not any(s.get("codec_type") == "audio" for s in info["streams"]):
        print("warning: the clip has no audio")
    w, h = int(v["width"]), int(v["height"])
    dur = float(info["format"].get("duration") or 0)
    if h < w:
        raise SystemExit(f"Not vertical ({w}x{h}); YouTube would publish it as a normal video, not a Short.")
    if not 1.0 <= dur <= SHORTS_MAX_S:
        raise SystemExit(f"{dur:.1f} s long; Shorts must be 3 minutes or less.")
    return dur, w, h


def shorts_text(title: str, description: str) -> tuple[str, str]:
    title = " ".join(title.split())
    if "#shorts" not in (title + " " + description).lower():
        description = (description + "\n\n#Shorts").strip()
    if len(title) > 100:
        raise SystemExit(f"Title is {len(title)} characters; YouTube allows 100.")
    if "<" in title or ">" in title:
        raise SystemExit("YouTube titles can't contain < or >.")
    return title, description


# --- sign-in --------------------------------------------------------------------------------
def _load_creds():
    import keyring
    from google.oauth2.credentials import Credentials
    raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if not raw:
        return None
    try:
        return Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except (ValueError, KeyError):
        return None


def _save_creds(creds) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, creds.to_json())


def authorize() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    client_file = settings.youtube_client_file()
    if not client_file.is_file():
        raise SystemExit(f"Missing {client_file}. Download your OAuth client (Desktop app) from Google Cloud "
                         "Console and save it there first (see docs/credentials.md).")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    print("Your browser will open Google's sign-in page. Sign in and allow 'upload videos'.")
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="", success_message="Done. You can close this tab.")
    _save_creds(creds)
    print("Signed in. The token is stored in your OS keychain.")


def get_creds():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    creds = _load_creds()
    if creds is None:
        raise SystemExit("Not signed in yet: run  python post_youtube.py auth")
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError:
            raise SystemExit("Your YouTube sign-in expired (apps in Google's 'testing' mode expire after 7 days). "
                             "Run  python post_youtube.py auth  again.")
        _save_creds(creds)
    return creds


# --- upload ---------------------------------------------------------------------------------
def upload(path: Path, title: str, description: str, tags: list[str], privacy: str) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    yt = build("youtube", "v3", credentials=get_creds(), cache_discovery=False)
    body = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": settings.get()["posting"]["youtube_category_id"]},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
    response, attempt = None, 0
    while response is None:
        try:
            status, response = req.next_chunk()
            if status:
                print(f"  uploaded {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in RETRY_STATUS and attempt < 5:
                attempt += 1
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            if b"youtubeSignupRequired" in (e.content or b""):
                raise SystemExit("This Google account has no YouTube channel yet. Create one at youtube.com "
                                 "(profile picture -> Create a channel), then try again. If your channel is a Brand "
                                 "Account, run  python post_youtube.py auth  again and pick the channel.")
            if e.resp.status == 403 and b"quotaExceeded" in (e.content or b""):
                raise SystemExit("YouTube's daily upload quota for your Google Cloud project is used up. "
                                 "Try again after midnight Pacific time.")
            raise SystemExit(f"YouTube refused the upload ({e.resp.status}): {e._get_reason()}")
    vid = response["id"]
    status = response.get("status", {})
    if status.get("privacyStatus") != privacy:
        print(f"note: YouTube set this upload to {status.get('privacyStatus')!r}, not {privacy!r} "
              "(your account or channel settings can override it).")
    return vid


def main() -> None:
    utf8_console()
    ap = argparse.ArgumentParser(description="Upload a clip to YouTube as a Short.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="sign in with Google (one time)")
    up = sub.add_parser("upload", help="upload one clip as a Short")
    up.add_argument("video", type=Path)
    up.add_argument("--title", required=True)
    up.add_argument("--description", default="")
    up.add_argument("--tags", default=",".join(settings.get()["posting"]["default_tags"]), help="comma-separated")
    up.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    up.add_argument("--dry-run", action="store_true", help="check the clip and text, upload nothing")
    a = ap.parse_args()

    if a.cmd == "auth":
        authorize()
        return
    dur, w, h = check_short(a.video)
    title, desc = shorts_text(a.title, a.description)
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    print(f"Short OK: {a.video.name}  {w}x{h}, {dur:.1f} s\n  title: {title}\n  privacy: {a.privacy}")
    if a.dry_run:
        print("dry run: nothing uploaded")
        return
    vid = upload(a.video, title, desc, tags, a.privacy)
    log_post("youtube_shorts", a.privacy, f"https://youtube.com/shorts/{vid}", title, a.video.name)
    print(f"Uploaded: https://youtube.com/shorts/{vid}  ({a.privacy})")


if __name__ == "__main__":
    main()
