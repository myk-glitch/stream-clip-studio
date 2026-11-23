"""Live alerts: check the streamers in config.toml through the official Twitch API. When one goes live, post a Discord
embed (channel "live") and start a clip watcher if they're playing the configured game.

Runs once per call, so schedule it every couple of minutes (see docs/configuration.md).
usage: python live_alert.py [--test LOGIN]
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import psutil

import notify
import settings
import twitch_api
from common import HERE


def state_file() -> Path:
    return settings.path("state_dir") / "live_state.json"


def watched():
    return {login: s for login, s in settings.streamers().items() if s["watch"]}


def streams():
    """login -> user details with the current stream, or stream None when offline."""
    logins = list(watched())
    live = {s["user_login"].lower(): s for s in twitch_api.streams(logins)}
    out = {}
    for u in twitch_api.users(logins):
        s = live.get(u["login"].lower())
        out[u["login"].lower()] = {
            "login": u["login"].lower(), "displayName": u["display_name"], "profileImageURL": u.get("profile_image_url"),
            "stream": s and {"id": s["id"], "title": s.get("title"), "viewersCount": s.get("viewer_count", 0),
                             "createdAt": s["started_at"], "game": s.get("game_name") or ""},
        }
    return out


def watcher_running(login):
    for p in psutil.process_iter(["cmdline"]):
        cmd = p.info.get("cmdline") or []
        if login in cmd and any(Path(c).name == "keepalive.py" for c in cmd):
            return True
    return False


def watcher_args(login):
    w, s = settings.get()["watcher"], settings.streamers()[login]
    args = ["--min-msgs", str(s["min_msgs"])] if s["min_msgs"] else []
    args += ["--model", w["model"], "--before", str(w["before"]), "--after", str(w["after"]), "--max-extend", str(w["max_extend"]),
             "--cooldown", str(w["cooldown"]), "--out", str(settings.path("clips_dir"))]
    return args + [str(x) for x in w["extra_args"]]


def console_python():
    """The windowless pythonw.exe can't host a watcher's console output."""
    exe = Path(sys.executable)
    return str(exe.with_name("python.exe")) if exe.name.lower() == "pythonw.exe" else str(exe)


def start_watcher(login):
    logs = settings.path("logs_dir")
    logs.mkdir(parents=True, exist_ok=True)
    detach = ({"creationflags": 0x00000008 | 0x00000200 | 0x08000000} if os.name == "nt"   # detached, no window
              else {"start_new_session": True})
    with open(logs / f"{login}.log", "a", encoding="utf-8") as log:
        subprocess.Popen([console_python(), "-u", str(HERE / "keepalive.py"), login, "--hours", str(settings.get()["watcher"]["hours"]),
                          "--", *watcher_args(login)], cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}, **detach)


def alert(u, watcher_note):
    tag = settings.streamers()[u["login"]]["tag"]
    name, colour, campaign, dot = notify.info(tag)
    s = u["stream"]
    game = s["game"] or "?"
    started = datetime.fromisoformat(s["createdAt"].replace("Z", "+00:00"))
    embed = {
        "author": {"name": f"{u['displayName']} is LIVE on Twitch", "url": f"https://twitch.tv/{u['login']}",
                   "icon_url": u.get("profileImageURL")},
        "title": s.get("title") or "Live now",
        "url": f"https://twitch.tv/{u['login']}",
        "color": colour,
        "fields": [
            {"name": "Playing", "value": ("🎮 " if settings.game_matches(game) else "⚠️ ") + game, "inline": True},
            {"name": "Viewers", "value": f"{s.get('viewersCount', 0):,}", "inline": True},
            {"name": "Live since", "value": f"<t:{int(started.timestamp())}:R>", "inline": True},
            {"name": "Campaign", "value": f"{dot} {campaign}", "inline": True},
            {"name": "Clipper", "value": watcher_note, "inline": True},
        ],
        "image": {"url": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{u['login']}-1280x720.jpg?t={int(time.time())}"},
        "thumbnail": {"url": u.get("profileImageURL")},
        "footer": {"text": f"{notify.bot()} · live alert"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    notify.send("live", {"embeds": [embed]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="send an alert for this login now if live (no state change, no watcher)")
    a = ap.parse_args()
    if not watched():
        print("No streamers to watch: add one under [streamers] in config.toml.")
        return
    users = streams()
    if a.test:
        u = users.get(a.test.lower())
        if u and u.get("stream"):
            alert(u, "test alert")
        else:
            print(a.test, "is offline or not listed in config.toml")
        return
    path = state_file()
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for login, u in users.items():
        s = u.get("stream")
        if not s:
            state.pop(login, None)
            continue
        in_game = settings.game_matches(s["game"])
        if state.get(login) == s["id"]:                  # already alerted for this stream
            if in_game and not watcher_running(login):
                start_watcher(login)                     # watcher gave up after a drop: restart it quietly
            continue
        state[login] = s["id"]
        if not in_game:
            note = "not the configured game, no watcher"
        elif watcher_running(login):
            note = "watcher already running ✅"
        else:
            start_watcher(login)
            note = "watcher started ✅"
        alert(u, note)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
