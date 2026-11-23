"""
Live Clipper: clips a Twitch stream WHILE it is still live.

  1. Streamlink records the stream into 10-second chunks (a rolling buffer on disk).
  2. It reads Twitch chat anonymously (no login) and counts messages every 10 seconds.
  3. When chat explodes (a burst well above its recent average), FFmpeg cuts the moment.
  4. Whisper adds captions and you get a ready-to-post 9:16 clip, while the stream is still going.

Usage:
    python live_clipper.py CHANNEL
    python live_clipper.py CHANNEL --model small --spike 2.5 --before 25 --after 5

Needs: Python 3.11+, FFmpeg on PATH, and `pip install -r requirements.txt`.
It only works on channels that are LIVE. Start it before (or during) the stream.
Only clip and post streams you have permission to use.
"""
import argparse, collections, csv, json, os, queue, random, re, shutil, signal, socket, ssl, statistics, subprocess, sys, threading, time
from datetime import datetime

import settings
from common import spoken_words

SEG = 10  # seconds per recorded chunk and per chat bucket
CUTSCENE_SHARE = 0.35  # drop a clip when this share of it has cutscene black bars

# --- Lessons from rejected clips (2026-11-20): chat activity that is NOT a reaction to a moment. ---
# Bots and !commands; bare answers to a question or poll ("yes", "5AM", "1", counting during a break);
# W/L spam the streamer asked for; thirst-posting at cutscenes ("GYATT", "OOOO"). These don't count
# toward a spike, and the spike counts DIFFERENT chatters, so one person spamming can't trigger it.
BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "sery_bot", "soundalerts",
        "streamstickers", "pokemoncommunitygame", "theclipbott", "kofistreambot", "botrixoficial"}
LOW_CONTENT = re.compile(r"^(?:yes+|yea+h?|ye+|yep|yup|y|no+|nope|nah+|ok(?:ay)?|k+|\d{1,3}(?:\s?(?:am|pm))?"
                         r"|[wl]+|here|first)$", re.IGNORECASE)
THIRST = re.compile(r"gya+t+|^o{3,}|^o+u+$|^ou+$|\bsexy+\b|\bthick\b|\byum+\b|crack her|\bfreak\b|🍑|\bbad{3,}",
                    re.IGNORECASE)

def counts_toward_spike(user: str, text: str) -> bool:
    t = text.strip()
    if user.lower() in BOTS or t.startswith("!"):
        return False
    norm = re.sub(r"[^\w\s]", "", t).strip()
    return not (LOW_CONTENT.match(norm) or THIRST.search(t))

# Chat comments about how real / good the game looks (graphics, physics, NPC detail). Plain "real" is
# left out on purpose: "for real", "real one" etc. say nothing about the visuals.
REALISM = re.compile(
    r"\b(?:looks?|looking|lookin)\s+(?:so\s+|too\s+|hella\s+|mad\s+|crazy\s+)?(?:real|realistic|insane|amazing)\b"
    r"|\btoo\s+real\b|\brealistic\b|\breal\s*life\b|\bphoto\s*-?\s*real|\bgraphics?\b|\bvisuals?\b"
    r"|\bnext\s*-?\s*gen\b|\bcinematic\b|\bphysics\b|\breflections?\b|\blighting\b|\bray\s*tracing\b"
    r"|\bdetails?\b|\bthe\s+water\b|\bwater\s+looks?\b|\bnpcs?\s+(?:are|be|is)\b|\brockstar\s+(?:cooked|really|went)\b"
    r"|\bstunning\b|\bunreal\b",
    re.IGNORECASE)
# comments about a PERSON's looks (Jason/Lucia/outfits) are thirst/fashion chat, not scenery
PEOPLE_LOOKS = re.compile(r"\b(?:he|she|him|her|jason|lucia|jas|fit|outfit|hair|fine|fye|hot|cute|body|face|ugly|drip)\b", re.IGNORECASE)

def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)

# ---------------------------------------------------------------- recorder
class Recorder:
    """streamlink -> ffmpeg segmenter. Keeps a list of finished chunks with the wall-clock time they ended."""
    def __init__(self, channel, quality, workdir, keep_minutes):
        self.channel, self.quality, self.dir = channel, quality, workdir
        self.keep = keep_minutes * 60
        self.segs = []  # dicts: file, dur, end_wall
        self.lock = threading.Lock()
        self.stop = threading.Event()
        os.makedirs(self.dir, exist_ok=True)
        self.csv = os.path.join(self.dir, "segments.csv")

    def start(self):
        url = f"https://www.twitch.tv/{self.channel}"
        sl = [sys.executable, "-m", "streamlink", "--stdout", "--twitch-low-latency", "--loglevel", "warning", url, self.quality]
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
              "-f", "segment", "-segment_time", str(SEG), "-reset_timestamps", "1",
              "-segment_list", self.csv, "-segment_list_type", "csv", "-segment_list_flags", "+live",
              os.path.join(self.dir, "chunk_%06d.ts")]
        self.p_sl = subprocess.Popen(sl, stdout=subprocess.PIPE, stderr=sys.stderr)
        self.p_ff = subprocess.Popen(ff, stdin=self.p_sl.stdout)
        self.p_sl.stdout.close()
        threading.Thread(target=self._watch, daemon=True).start()

    def alive(self):
        return self.p_ff.poll() is None and self.p_sl.poll() is None

    def _watch(self):
        seen = 0
        while not self.stop.is_set():
            time.sleep(0.5)
            if not os.path.exists(self.csv):
                continue
            with open(self.csv, newline="") as f:
                rows = list(csv.reader(f))
            now = time.time()
            with self.lock:
                for r in rows[seen:]:
                    if len(r) < 3: continue
                    self.segs.append({"file": os.path.join(self.dir, r[0]), "dur": float(r[2]) - float(r[1]), "end_wall": now})
                seen = len(rows)
                # rolling buffer: delete old chunks from disk (never ones a clip may still need)
                while self.segs and now - self.segs[0]["end_wall"] > self.keep:
                    old = self.segs.pop(0)
                    try: os.remove(old["file"])
                    except OSError: pass

    def window(self, t0, t1):
        """chunks overlapping wall-clock [t0, t1] -> (files, wall time where the first file starts)"""
        with self.lock:
            pick = [s for s in self.segs if s["end_wall"] > t0 and s["end_wall"] - s["dur"] < t1]
        if not pick: return [], None
        return [s["file"] for s in pick], pick[0]["end_wall"] - pick[0]["dur"]

    def close(self):
        self.stop.set()
        for p in (getattr(self, "p_sl", None), getattr(self, "p_ff", None)):
            if p and p.poll() is None:
                p.terminate()

# ---------------------------------------------------------------- chat
class Chat:
    """Anonymous, read-only Twitch chat (IRC). No account or token needed."""
    def __init__(self, channel):
        self.channel = channel.lower()
        self.buckets = collections.deque(maxlen=60)  # (bucket_end_wall, count, sample msgs, {realism user: msg})
        self.cur, self.cur_msgs, self.cur_real = set(), [], {}   # cur: different chatters whose messages count
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._tick, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                # Encrypted port 6697: the plain port 6667 is blocked on some networks.
                s = ssl.create_default_context().wrap_socket(
                    socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=30), server_hostname="irc.chat.twitch.tv")
                s.sendall(f"PASS SCHMOOPIIE\r\nNICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                buf, joined, quiet = "", False, 0
                while not self.stop.is_set():
                    try:
                        data = s.recv(65536)
                    except socket.timeout:
                        quiet += 1  # a quiet chat is normal: ping the server to keep the link up
                        s.sendall(b"PING :tmi.twitch.tv\r\n")
                        if quiet >= 4: break
                        continue
                    quiet = 0
                    if not data: break
                    buf += data.decode("utf-8", "ignore")
                    *lines, buf = buf.split("\r\n")
                    for ln in lines:
                        if " 376 " in ln and not joined:  # JOIN only after the welcome, or Twitch ignores it
                            s.sendall(f"JOIN #{self.channel}\r\n".encode()); joined = True
                            log(f"chat: reading #{self.channel} (anonymous, read-only, no login)")
                        if ln.startswith("PING"):
                            s.sendall(ln.replace("PING", "PONG", 1).encode() + b"\r\n")
                        elif " PRIVMSG " in ln:
                            m = re.match(r":(\w+)!.* PRIVMSG #\w+ :(.*)", ln)
                            with self.lock:
                                if m and counts_toward_spike(m.group(1), m.group(2)):
                                    self.cur.add(m.group(1).lower())
                                if m and len(self.cur_msgs) < 40: self.cur_msgs.append(f"{m.group(1)}: {m.group(2)[:80]}")
                                if m and REALISM.search(m.group(2)) and not PEOPLE_LOOKS.search(m.group(2)):
                                    self.cur_real[m.group(1).lower()] = m.group(2)[:80]   # one per chatter
            except OSError as e:
                if not self.stop.is_set():
                    log("chat: reconnecting after", type(e).__name__); time.sleep(3)

    def _tick(self):
        nxt = time.time() + SEG
        while not self.stop.is_set():
            time.sleep(max(0, nxt - time.time()))
            with self.lock:
                self.buckets.append((nxt, len(self.cur), self.cur_msgs, self.cur_real))
                self.cur, self.cur_msgs, self.cur_real = set(), [], {}
            nxt += SEG

# ---------------------------------------------------------------- clip maker
class Clipper:
    def __init__(self, args, rec, outdir):
        self.a, self.rec, self.out = args, rec, outdir
        self.q = queue.Queue()
        self.model = None
        self.busy = False
        threading.Thread(target=self._worker, daemon=True).start()

    def _load_whisper(self):
        if self.model is not None or self.a.no_captions: return
        from faster_whisper import WhisperModel
        try:
            import ctranslate2
            gpu = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            gpu = False
        dev, ct = ("cuda", "float16") if gpu else ("cpu", "int8")
        log(f"whisper: loading '{self.a.model}' on {dev}")
        try:
            self.model = WhisperModel(self.a.model, device=dev, compute_type=ct)
        except Exception as e:
            log(f"whisper: GPU not usable ({type(e).__name__}), using CPU")
            self.model = WhisperModel(self.a.model, device="cpu", compute_type="int8")

    def transcribe(self, path):
        try:
            return spoken_words(self.model, path)
        except Exception as e:  # e.g. NVIDIA card present but CUDA libraries missing
            log(f"whisper: GPU failed ({type(e).__name__}), switching to CPU")
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.a.model, device="cpu", compute_type="int8")
            return spoken_words(self.model, path)

    def add(self, spike):
        self.q.put(spike)

    def _worker(self):
        while True:
            sp = self.q.get()
            self.busy = True
            sp["final"].wait()  # the clip's end can still move while chat stays hot (see extend_spike)
            # wait until the chunks AFTER the spike exist
            ready_at = sp["wall"] + sp["after"] + SEG + 2
            while time.time() < ready_at: time.sleep(0.5)
            try:
                self.make(sp)
            except Exception as e:
                log("clip failed:", repr(e))
            finally:
                self.busy = False

    def make(self, sp):
        t0, t1 = sp["wall"] - self.a.before - self.a.delay, sp["wall"] + sp["after"] - self.a.delay
        files, first_start = self.rec.window(t0, t1)
        if not files:
            log("clip skipped: no recorded video for that moment yet"); return
        realism = sp.get("kind") == "realism"
        name = f"{self.a.channel}_{datetime.fromtimestamp(sp['wall']).strftime('%Y%m%d_%H%M%S')}" + ("_realism" if realism else "")
        d = os.path.join(self.out, name); os.makedirs(d, exist_ok=True)
        lst = os.path.join(d, "chunks.txt")
        with open(lst, "w") as f:
            for fp in files: f.write(f"file '{os.path.abspath(fp)}'\n")
        ss = max(0.0, t0 - first_start); dur = t1 - t0
        raw = os.path.join(d, "clip_16x9.mp4")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
             "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", raw])
        os.remove(lst)
        lb = letterbox_share(raw)
        if lb >= self.a.cutscene_share:
            # Story cutscenes (black bars) were the most common rejection, and Rockstar takes them down.
            rej = os.path.join(self.out, "_rejected", "auto"); os.makedirs(rej, exist_ok=True)
            shutil.move(d, os.path.join(rej, name))
            with open(os.path.join(self.out, "_rejected", "REASONS.txt"), "a", encoding="utf-8") as f:
                f.write(f"auto/{name}  auto-dropped: cutscene ({lb:.0%} letterboxed), no captions/render made\n")
            log(f"clip dropped: cutscene ({lb:.0%} of it has black bars)"); return
        ass = None
        if not self.a.no_captions and not realism:   # realism clips are about the visuals, not what's said
            self._load_whisper()
            ass = write_captions(self.transcribe(raw), d)
        vert = os.path.join(d, "clip_9x16.mp4")
        if realism:
            # full-screen gameplay: crop the middle 9:16 slice (leaves out a facecam / chat overlay at the edges)
            vf = f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)*{self.a.realism_center}:0,scale=1080:1920:flags=lanczos,setsar=1[v]"
        else:
            vf = blur_9x16_filter(ass)
        render_9x16(d, vf)
        json.dump({"channel": self.a.channel, "kind": sp.get("kind", "moment"),
                   "realism_comments": sp.get("realism", {}),
                   "spike_time": datetime.fromtimestamp(sp["wall"]).isoformat(timespec="seconds"),
                   "msgs_in_10s": sp["count"], "baseline_10s": sp["base"], "clip_seconds": round(dur, 1),
                   "extended_by_s": sp["after"] - self.a.after, "chat_sample": sp["msgs"][:15]},
                  open(os.path.join(d, "info.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        log(f"CLIP READY -> {vert}")

def letterbox_share(path):
    """Share of seconds (0..1) where the picture has black bars top and bottom, like a game cutscene."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf", "fps=1,cropdetect=limit=24:round=2:reset=1",
                        "-f", "null", "-"], capture_output=True, text=True)
    sizes = [(int(w), int(h)) for w, h in re.findall(r"crop=(\d+):(\d+):\d+:\d+", r.stderr)]
    frame = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", r.stderr)
    if not sizes or not frame:
        return 0.0
    full_h = int(frame.group(2))   # the video's real height (not the tallest crop: a clip can be letterboxed throughout)
    return sum(1 for _, h in sizes if h < full_h * 0.9) / len(sizes)

def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[-400:]}")

def write_captions(words, clip_dir):
    """Write captions.srt, plus captions.ass if anything was said; returns the .ass name or None."""
    write_srt(words, os.path.join(clip_dir, "captions.srt"))
    if not words:
        return None
    write_ass(words, os.path.join(clip_dir, "captions.ass"))
    return "captions.ass"

def blur_9x16_filter(ass=None):
    """9:16: blurred full-frame background + the full 16:9 picture in the middle + big captions."""
    return ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,eq=brightness=-0.08[bg];"
            "[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2" + (f",ass={ass}" if ass else "") + "[v]")

def render_9x16(clip_dir, vf):
    """Render clip_9x16.mp4 from the folder's clip_16x9.mp4 with the given filter graph."""
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "clip_16x9.mp4", "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", "-movflags", "+faststart", "clip_9x16.mp4"], cwd=clip_dir)

def chunks3(words, n=3):
    out, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) == n or re.search(r"[.!?,]$", w[0]):
            out.append(cur); cur = []
    if cur: out.append(cur)
    return out

def ts(t, sep):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", sep) if sep == "," else f"{h:d}:{m:02d}:{s:05.2f}"

def write_srt(words, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(chunks3(words), 1):
            f.write(f"{i}\n{ts(c[0][1], ',')} --> {ts(c[-1][2], ',')}\n{' '.join(w[0] for w in c)}\n\n")

def write_ass(words, path):
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,84,&H00FFFFFF,&H0000D8FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,440,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        cs = chunks3(words)
        for i, c in enumerate(cs):
            end = c[-1][2] + 0.15
            if i + 1 < len(cs): end = min(end, cs[i + 1][0][1])  # never overlap the next line
            # karaoke: each word turns yellow while it is spoken
            txt = "".join(f"{{\\kf{max(1, int((w[2] - w[1]) * 100))}}}{w[0].upper().replace('{', '').replace('}', '')} " for w in c)
            f.write(f"Dialogue: 0,{ts(c[0][1], '.')},{ts(end, '.')},Cap,,0,0,0,,{txt.strip()}\n")

def new_spike(wall, count, base, msgs, after, final=False, kind="moment"):
    sp = {"wall": wall, "count": count, "base": base, "msgs": msgs, "after": after, "final": threading.Event(),
          "kind": kind}
    if final: sp["final"].set()
    return sp

def realism_trigger(buckets, a):
    """Realism comments from different chatters over the last two buckets (~20 s), or None if too few.
    Realism moments are calm, so chat doesn't spike; chat *comments* on the visuals instead."""
    users = {}
    for bk in list(buckets)[-2:]:
        if len(bk) > 3:
            users.update(bk[3])
    return users if len(users) >= a.realism_min else None

def extend_spike(sp, cnt, a):
    """Called with each chat bucket that follows a spike. While chat stays hot, push the clip's end
    out by one bucket (capped by --max-extend). Returns True once the end is final."""
    hot = cnt >= a.min_msgs / 2 and cnt >= a.extend_ratio * max(sp["base"], 1)
    if hot and (sp["after"] - a.after) + SEG <= a.max_extend:
        sp["after"] += SEG
        return False
    sp["final"].set()
    return True

# ---------------------------------------------------------------- main loop
def main():
    ap = argparse.ArgumentParser(description="Clip a live Twitch stream the moment chat explodes.")
    ap.add_argument("channel", help="Twitch channel name, e.g. xqc")
    ap.add_argument("--quality", default="720p60,720p,best", help="stream quality (default 720p60,720p,best)")
    ap.add_argument("--spike", type=float, default=2.5, help="chat must hit this many times its recent average (default 2.5)")
    ap.add_argument("--min-msgs", type=int, default=20, help="minimum messages in 10 s to count as a spike (default 20)")
    ap.add_argument("--before", type=float, default=25, help="seconds kept before the spike (default 25)")
    ap.add_argument("--after", type=float, default=5, help="seconds kept after the spike (default 5)")
    ap.add_argument("--delay", type=float, default=4, help="chat reacts this many seconds after the moment (default 4)")
    ap.add_argument("--cooldown", type=float, default=60, help="seconds between clips (default 60)")
    ap.add_argument("--max-extend", type=float, default=0, help="if chat stays hot after a spike, add up to this many seconds to the clip's end, 10 s at a time (default 0 = off)")
    ap.add_argument("--extend-ratio", type=float, default=1.5, help="chat counts as still hot at this many times its average (default 1.5)")
    ap.add_argument("--model", default="small", help="Whisper model: tiny, base, small, medium, large-v3 (default small)")
    ap.add_argument("--no-captions", action="store_true", help="skip Whisper captions")
    ap.add_argument("--out", default=str(settings.path("clips_dir")), help="output folder (default: paths.clips_dir in config.toml)")
    ap.add_argument("--buffer-minutes", type=float, default=5, help="minutes of video kept on disk (default 5)")
    ap.add_argument("--test-clip-after", type=float, default=0, help="TEST: also force one clip after N seconds")
    ap.add_argument("--cutscene-share", type=float, default=CUTSCENE_SHARE, help=f"drop a clip if this share of it has cutscene black bars (default {CUTSCENE_SHARE}; 1.1 = never)")
    ap.add_argument("--realism", action="store_true", help="also clip moments chat calls realistic (graphics, physics, details) as full-screen 9:16 gameplay")
    ap.add_argument("--realism-min", type=int, default=4, help="different chatters commenting on realism within ~20 s (default 4)")
    ap.add_argument("--realism-cooldown", type=float, default=120, help="seconds between realism clips (default 120)")
    ap.add_argument("--realism-center", type=float, default=0.5, help="horizontal position of the realism crop, 0=left 1=right (default 0.5 = middle)")
    a = ap.parse_args()
    a.channel = a.channel.lower().lstrip("#").split("/")[-1]
    if not shutil.which("ffmpeg"):
        sys.exit("FFmpeg not found. Install it and make sure `ffmpeg` runs in a terminal.")

    work = os.path.join(a.out, f".buffer_{a.channel}")
    shutil.rmtree(work, ignore_errors=True)
    rec = Recorder(a.channel, a.quality, work, a.buffer_minutes)
    chat = Chat(a.channel)
    clipper = Clipper(a, rec, a.out)
    log(f"watching twitch.tv/{a.channel} live. Clips go to {os.path.abspath(a.out)}  (Ctrl+C to stop)")
    rec.start(); chat.start()
    threading.Thread(target=clipper._load_whisper, daemon=True).start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    start, last_clip, last_bucket, forced = time.time(), 0.0, None, False
    last_realism = 0.0
    pending = None  # spike whose clip end may still be extended
    try:
        while not stop.is_set():
            time.sleep(1)
            if not rec.alive():
                log("stream ended or is offline. Live only: no rewind."); break
            with chat.lock:
                b = list(chat.buckets)
            if b and b[-1][0] != last_bucket:
                last_bucket = b[-1][0]
                end, cnt, msgs = b[-1][:3]
                hist = [x[1] for x in b[:-1]][-30:]
                base = statistics.median(hist) if len(hist) >= 3 else None
                bar = "#" * min(40, cnt // 2)
                log(f"chat {cnt:4d} msgs/10s  avg {base if base is not None else '...':>4}  {bar}")
                if pending is not None:
                    if extend_spike(pending, cnt, a):
                        log(f"clip end set: {a.before:.0f}s before + {pending['after']:.0f}s after the spike")
                        pending = None
                    else:
                        log(f"chat still hot -> clip extended to {pending['after']:.0f}s after the spike")
                elif base is not None and cnt >= a.min_msgs and cnt >= a.spike * max(base, 1) and end - last_clip > a.cooldown:
                    last_clip = end
                    sp = new_spike(end, cnt, base, msgs, a.after, final=a.max_extend <= 0)
                    if a.max_extend > 0: pending = sp
                    log(f"CHAT EXPLODED ({cnt} vs avg {base}) -> cutting the last {a.before:.0f}s + {a.after:.0f}s"
                        + (" (longer if chat stays hot)" if pending else ""))
                    clipper.add(sp)
                if a.realism and end - last_realism > a.realism_cooldown:
                    real = realism_trigger(b, a)
                    if real:
                        last_realism = end
                        log(f"REALISM ({len(real)} chatters: {'; '.join(list(real.values())[:3])}) -> full-screen gameplay clip")
                        clipper.add(new_spike(end, cnt, base or 0, msgs, a.after, final=True, kind="realism") | {"realism": real})
            if a.test_clip_after and not forced and time.time() - start > a.test_clip_after:
                forced = True
                log("TEST: forcing one clip now")
                with chat.lock:
                    msgs = chat.buckets[-1][2] if chat.buckets else []
                clipper.add(new_spike(time.time(), 0, 0, msgs, a.after, final=True))
    finally:
        if pending is not None:
            pending["final"].set()  # stopping: cut with the end we have so far
        # let a clip that is being made finish
        t = time.time()
        while (clipper.busy or not clipper.q.empty()) and time.time() - t < 120:
            time.sleep(1)
        chat.stop.set(); rec.close()
        log("stopped.")

if __name__ == "__main__":
    main()
