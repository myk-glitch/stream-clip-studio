"""Re-cut a clip into the format the top streamer Shorts use: facecam on top, gameplay on the bottom,
short captions on the seam between them.

usage: python split_layout.py CLIPDIR --cam X,Y,W,H [--game X,Y,W,H] [--start S] [--end E]
                              [--words 1] [--box] [--out OUT.mp4]

CLIPDIR has clip_16x9.mp4 (1280x720). --cam / --game are boxes in that 1280x720 frame; each is
center-cropped to its slot's aspect (top 1080x860, bottom 1080x1060). --start/--end trim (seconds).
--words: words per caption (1 = punchy single words). --box puts a black box behind the text instead of an
outline. Writes OUT (default CLIPDIR/clip_split.mp4). Each streamer's boxes can be saved in config.toml (cam, game).
"""
import argparse, subprocess, sys, tempfile
from pathlib import Path

import settings
from common import spoken_words, utf8_console

TOP_H, BOT_H, W = 860, 1060, 1080
FILLER = {"um", "uh", "uhh", "umm", "hmm", "mm", "er", "ah"}


def fit(box, aspect):
    """Center-crop box (x, y, w, h) to the given width/height aspect; returns ffmpeg crop args."""
    x, y, w, h = box
    if w / h > aspect:
        nw = int(h * aspect) // 2 * 2
        x, w = x + (w - nw) // 2, nw
    else:
        nh = int(w / aspect) // 2 * 2
        y, h = y + (h - nh) // 2, nh
    return f"{w}:{h}:{x}:{y}"


def ts(t):
    t = max(0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(words, path, per, box, y=TOP_H):
    border = "3,0,0" if box else "1,8,0"         # BorderStyle, Outline, Shadow
    back = "&H00000000" if box else "&H64000000"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Seam,Arial Black,78,&H00FFFFFF,&H00FFFFFF,&H00000000,{back},-1,0,0,0,100,100,0,0,{border},5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    groups = [words[i:i + per] for i in range(0, len(words), per)]
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        for i, g in enumerate(groups):
            end = g[-1][2] + 0.1
            if i + 1 < len(groups):
                end = min(end, groups[i + 1][0][1])
            text = " ".join(w[0] for w in g).upper().replace("{", "").replace("}", "")
            box_pad = r"\bord14" if box else ""
            f.write(f"Dialogue: 0,{ts(g[0][1])},{ts(end)},Seam,,0,0,0,,{{\\pos(540,{y}){box_pad}}}{text}\n")


def transcribe(path, model="base"):
    from faster_whisper import WhisperModel
    return spoken_words(WhisperModel(model, device="cpu", compute_type="int8"), path, skip=FILLER)


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("clipdir", type=Path)
    ap.add_argument("--cam")
    ap.add_argument("--game")
    ap.add_argument("--channel", help="streamer login or tag from config.toml, for its cam and game boxes")
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--end", type=float)
    ap.add_argument("--words", type=int, default=1)
    ap.add_argument("--box", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="streamer switched to full-screen cam: center-crop the whole frame to 9:16 instead of splitting")
    ap.add_argument("--sfx", action="append", default=[],
                    help="your sound effect at a time in the OUTPUT clip, e.g. boom@6.2 plays sfx/boom.mp3 (or .wav). "
                         "One or two per clip, on the payoff, works best")
    ap.add_argument("--sfx-volume", type=float, default=0.3, help="sound effect level vs the streamer (default 0.3)")
    ap.add_argument("--model", default="base")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    preset = settings.streamers().get((a.channel or "").lower()) or settings.streamer_by_tag(a.channel) or {}
    cam = [int(v) for v in a.cam.split(",")] if a.cam else preset.get("cam")
    game = [int(v) for v in a.game.split(",")] if a.game else preset.get("game", [0, 0, 1280, 720])
    if not cam and not a.full:
        sys.exit("need --cam X,Y,W,H or a --channel with a cam box in config.toml")
    cam = cam or [0, 0, 1280, 720]
    src = a.clipdir / "clip_16x9.mp4"
    out = a.out or a.clipdir / "clip_split.mp4"
    width = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                                "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout.strip() or 1280)
    k = width / 1280                       # boxes are given in 1280x720; scale them for 1080p sources
    cam = [int(v * k) for v in cam]
    game = [int(v * k) for v in game]

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cut = td / "cut.mp4"
        trim = ["-ss", str(a.start)] + (["-to", str(a.end)] if a.end else [])
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *trim, "-c:v", "libx264", "-preset",
                        "veryfast", "-crf", "18", "-c:a", "aac", str(cut)], check=True)
        words = transcribe(cut, a.model)
        ass = td / "cap.ass"
        write_ass(words, ass, a.words, a.box, y=1380 if a.full else TOP_H)
        if a.full:
            vf = "[0:v]crop=ih*9/16:ih,scale=1080:1920:flags=lanczos,ass=cap.ass[v]"
        else:
            vf = (f"[0:v]split[a][b];[a]crop={fit(cam, W / TOP_H)},scale={W}:{TOP_H}:flags=lanczos[top];"
                  f"[b]crop={fit(game, W / BOT_H)},scale={W}:{BOT_H}:flags=lanczos[bot];"
                  f"[top][bot]vstack,ass=cap.ass[v]")
        inputs, amap = ["-i", str(cut)], ["-map", "0:a?"]
        if a.sfx:
            parts, labels = [], []
            for i, spec in enumerate(a.sfx, start=1):
                name, at = spec.split("@")
                path = next((p for p in (settings.path("sfx_dir") / f"{name}{ext}" for ext in (".mp3", ".wav")) if p.exists()), None)
                if path is None:
                    sys.exit(f"no sound effect {name}.mp3 or {name}.wav in {settings.path('sfx_dir')}")
                inputs += ["-i", str(path)]
                ms = int(float(at) * 1000)
                parts.append(f"[{i}:a]volume={a.sfx_volume},adelay={ms}|{ms}[s{i}]")
                labels.append(f"[s{i}]")
            vf += ";" + ";".join(parts) + f";[0:a]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:normalize=0[aout]"
            amap = ["-map", "[aout]"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", vf, "-map", "[v]",
                        *amap, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
                        "-movflags", "+faststart", str(out.resolve())], check=True, cwd=td)
    print("wrote", out, f"({len(words)} words)")


if __name__ == "__main__":
    main()
