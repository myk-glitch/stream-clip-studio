"""Render a hook caption (white Arial Black, thick black outline, COLOUR emoji) to a transparent
1080-wide PNG with headless Microsoft Edge, which draws emoji in colour via DirectWrite.
usage: hook_edge.py OUT.png "HOOK TEXT WITH EMOJI 😭" [--size 76]"""
import html, os, subprocess, sys, tempfile

EDGE = os.environ.get("EDGE_PATH") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
out = os.path.abspath(sys.argv[1]); text = open(sys.argv[2], encoding="utf-8").read().strip()  # text file: avoids shell emoji mangling
size = int(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 76
H = 340

page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:transparent;width:1080px;height:{H}px;overflow:hidden}}
.box{{width:1080px;height:{H}px;display:flex;align-items:center;justify-content:center}}
.t{{width:960px;text-align:center;font-family:'Arial Black',Arial,sans-serif;font-size:{size}px;line-height:1.18;
   color:#fff;text-transform:uppercase;-webkit-text-stroke:{round(size*0.2)}px #000;paint-order:stroke fill}}
.e{{-webkit-text-stroke:0;font-family:'Segoe UI Emoji'}}
</style></head><body><div class="box"><div class="t">@@BODY@@</div></div></body></html>"""

# keep emoji out of the outline so they stay in full colour
parts, buf = [], ""
for ch in text:
    if ord(ch) > 0x2000 and not ch.isalnum():
        if buf: parts.append(html.escape(buf)); buf = ""
        parts.append(f'<span class="e">{html.escape(ch)}</span>')
    else:
        buf += ch
if buf: parts.append(html.escape(buf))
page = page.replace("@@BODY@@", "".join(parts))

d = tempfile.mkdtemp()
src = os.path.join(d, "hook.html")
open(src, "w", encoding="utf-8").write(page)
subprocess.run([EDGE, "--headless", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                f"--window-size=1080,{H}", f"--screenshot={out}", f"--user-data-dir={os.path.join(d, 'profile')}",
                "file:///" + src.replace("\\", "/")], check=True, capture_output=True, timeout=60)
print("ok", out, os.path.getsize(out))
