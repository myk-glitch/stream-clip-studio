"""Serve ONE folder of finished clips to the browser pane so a web uploader (TikTok Studio) can take a file.

Safety: listens on 127.0.0.1 only (never the network), read-only, serves only files directly inside the
folder (no subfolders, no ".."), and every URL needs a random secret token, so other websites can't read
your files while it runs. Stops by itself after --minutes (default 15).

usage: python serve_for_upload.py [FOLDER] [--minutes 15]
prints:  READY http://127.0.0.1:<port>/<token>/
"""
import argparse, http.server, secrets, socket, socketserver, sys, threading, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import settings  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("folder", nargs="?", default=str(settings.path("clips_dir") / "READY_TO_POST"))
ap.add_argument("--minutes", type=float, default=15)
a = ap.parse_args()
ROOT = Path(a.folder).resolve()
TOKEN = secrets.token_urlsafe(24)


class Handler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")   # Chrome's private-network check
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        parts = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).strip("/").split("/", 1)
        if len(parts) != 2 or not secrets.compare_digest(parts[0], TOKEN) or "/" in parts[1] or "\\" in parts[1]:
            self.send_response(404); self.end_headers(); return
        f = (ROOT / parts[1]).resolve()
        if f.parent != ROOT or not f.is_file() or f.suffix.lower() != ".mp4":
            self.send_response(404); self.end_headers(); return
        data = f.read_bytes()
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "video/mp4"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def log_message(self, fmt, *args):   # keep the token out of logs
        sys.stderr.write("request %s\n" % (args[1] if len(args) > 1 else ""))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Server6(Server):
    address_family = socket.AF_INET6


# Listen on BOTH loopback addresses with the same port: browsers resolve "localhost" to ::1 first, and
# some sites (TikTok) only allow "localhost" in their security policy, not 127.0.0.1. Never all interfaces.
srv = Server(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
servers = [srv]
try:
    servers.append(Server6(("::1", port), Handler))
except OSError:
    pass
for s in servers[1:]:
    threading.Thread(target=s.serve_forever, daemon=True).start()
threading.Timer(a.minutes * 60, lambda: [s.shutdown() for s in servers]).start()
print(f"READY http://localhost:{port}/{TOKEN}/", flush=True)
srv.serve_forever()
print("stopped", flush=True)
