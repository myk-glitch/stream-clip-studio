"""Set up and check your configuration.

  python configure.py init               create config.toml and .env from the examples (never overwrites)
  python configure.py doctor [--online]  check tools, settings and credentials (never prints a secret)
  python configure.py secret NAME        store a credential in the OS keychain (hidden prompt)
  python configure.py secret NAME --delete
"""
import argparse, getpass, importlib.util, shutil, subprocess, sys

import settings
from common import HERE, utf8_console

OK, WARN, BAD = "[ ok ]", "[note]", "[FAIL]"


def cmd_init(_):
    for example, target in ((HERE / "config.example.toml", settings.config_path()), (HERE / ".env.example", settings.env_path())):
        if target.exists():
            print(f"{target.name} already exists, left unchanged")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example, target)
            print(f"created {target}")
    print("\nNext: edit config.toml, add your credentials (docs/credentials.md), then run: python configure.py doctor")
    return 0


def _module(name):
    return importlib.util.find_spec(name) is not None


def cmd_doctor(a):
    failed = 0

    def line(mark, text):
        nonlocal failed
        failed += mark == BAD
        print(f"{mark} {text}")

    line(OK if sys.version_info >= (3, 11) else BAD, f"Python {sys.version.split()[0]} (3.11 or newer needed)")
    for tool in ("ffmpeg", "ffprobe"):
        line(OK if shutil.which(tool) else BAD, f"{tool} {'found' if shutil.which(tool) else 'not found on PATH'}")
    for module, package in (("streamlink", "streamlink"), ("faster_whisper", "faster-whisper"), ("requests", "requests"),
                            ("keyring", "keyring"), ("dotenv", "python-dotenv"), ("psutil", "psutil"),
                            ("googleapiclient", "google-api-python-client"), ("google_auth_oauthlib", "google-auth-oauthlib")):
        line(OK if _module(module) else BAD, f"Python package {package}")
    cfg = settings.reload()
    path = settings.config_path()
    line(OK if path.exists() else WARN, f"config: {path.name}" + ("" if path.exists() else " missing, using defaults (run: python configure.py init)"))
    watched = [login for login, s in cfg["streamers"].items() if s["watch"]]
    line(OK if watched else WARN, f"streamers watched: {', '.join(watched) or 'none yet'}")
    line(OK, f"clips folder: {settings.path('clips_dir')}")
    line(OK, f"default privacy: {cfg['posting']['privacy']}; limit {cfg['posting']['max_posts_per_day']}/day, "
             f"{cfg['posting']['min_hours_between_posts']:g} h apart; automatic posting {'on' if cfg['auto_post']['enabled'] else 'off'}")

    twitch = bool(settings.secret("TWITCH_CLIENT_ID") and settings.secret("TWITCH_CLIENT_SECRET"))
    line(OK if twitch else WARN, "Twitch app credentials " + ("set" if twitch else "not set (needed for alerts, top clips and VOD listing)"))
    if a.online and twitch:
        import twitch_api
        try:
            twitch_api.forget_token()
            twitch_api.get("games", {"name": "Just Chatting"})
            line(OK, "Twitch API sign-in works")
        except SystemExit as e:
            line(BAD, str(e))
    client = settings.youtube_client_file()
    line(OK if client.is_file() else WARN, "YouTube OAuth client file " + (f"found" if client.is_file() else f"not found (expected at {client})"))
    signed_in = bool(settings.keychain_get("youtube-oauth-token"))
    line(OK if signed_in else WARN, "YouTube " + ("signed in" if signed_in else "not signed in (run: python post_youtube.py auth)"))
    line(OK if settings.secret("YOUTUBE_API_KEY") else WARN, "YouTube API key " + ("set" if settings.secret("YOUTUBE_API_KEY") else "not set (needed for view counts in the recap)"))
    for kind in ("ask", "topost", "posted", "live", "recap"):
        name = f"DISCORD_WEBHOOK_{kind.upper()}"
        line(OK if settings.secret(name) else WARN, f"Discord {kind} channel " + ("set" if settings.secret(name) else "not set (optional)"))
    enabled = [k for k, v in cfg["unofficial"].items() if v]
    line(WARN if enabled else OK, "unofficial features " + (f"ENABLED: {', '.join(enabled)}" if enabled else "off"))
    print(f"\n{'Problems found: ' + str(failed) if failed else 'Everything required is ready.'}")
    return 1 if failed else 0


def cmd_secret(a):
    if a.name not in settings.SECRET_NAMES:
        sys.exit(f"Unknown secret. Choose one of: {', '.join(settings.SECRET_NAMES)}")
    if a.delete:
        print("deleted" if settings.keychain_delete(a.name) else "nothing to delete")
        return 0
    value = getpass.getpass(f"{a.name} (input is hidden): ").strip()
    if not value:
        sys.exit("Nothing entered.")
    if not settings.keychain_set(a.name, value):
        sys.exit("No OS keychain is available here. Put the value in .env instead.")
    print(f"{a.name} stored in the OS keychain.")
    return 0


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description="Set up and check your configuration.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("doctor"); p.add_argument("--online", action="store_true", help="also test your Twitch credentials")
    p = sub.add_parser("secret"); p.add_argument("name"); p.add_argument("--delete", action="store_true")
    a = ap.parse_args()
    return {"init": cmd_init, "doctor": cmd_doctor, "secret": cmd_secret}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
