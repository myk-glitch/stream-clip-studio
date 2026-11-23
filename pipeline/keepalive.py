"""Run live_clipper.py and restart it after connection drops, while the channel is still live.

usage: python keepalive.py CHANNEL [--hours 2] -- <live_clipper options...>
Example: python keepalive.py example_streamer --hours 3 -- --model base --before 18 --after 8 --realism

live_clipper.py stops on ANY break in the stream connection, even a few seconds of network trouble.
This wrapper checks with Streamlink whether the channel is still live: if yes it restarts at once; if not
it rechecks 3 times over ~3 minutes (streams sometimes blip off and back) before giving up.
Ctrl+C stops everything (the running clip is allowed to finish).
"""
import argparse, json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def log(*a):
    print(datetime.now().strftime("[%H:%M:%S] keepalive:"), *a, flush=True)


def is_live(channel: str) -> bool:
    try:
        r = subprocess.run([PY, "-m", "streamlink", "--loglevel", "error", "--json", f"https://www.twitch.tv/{channel}"],
                           capture_output=True, text=True, timeout=60)
        return bool(json.loads(r.stdout or "{}").get("streams"))
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return False


def main():
    argv = sys.argv[1:]
    passthrough = argv[argv.index("--") + 1:] if "--" in argv else []
    own = argv[:argv.index("--")] if "--" in argv else argv
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--hours", type=float, default=2.0)
    a = ap.parse_args(own)
    deadline = time.time() + a.hours * 3600
    restarts = 0
    while time.time() < deadline:
        proc = subprocess.Popen([PY, "-u", str(HERE / "live_clipper.py"), a.channel] + passthrough)
        try:
            while proc.poll() is None:
                if time.time() >= deadline:
                    log("time is up, stopping")
                    proc.terminate(); proc.wait(timeout=180)
                    return
                time.sleep(2)
        except KeyboardInterrupt:
            proc.wait(timeout=180)
            return
        if time.time() >= deadline:
            return
        for attempt in range(4):                       # now, then +60 s, +120 s, +180 s
            if is_live(a.channel):
                restarts += 1
                log(f"{a.channel} is still live: restarting the clipper (restart #{restarts})")
                time.sleep(3)
                break
            if attempt < 3:
                log(f"{a.channel} looks offline, checking again in 60 s")
                time.sleep(60)
        else:
            log(f"{a.channel} has gone offline. Stopped.")
            return


if __name__ == "__main__":
    main()
