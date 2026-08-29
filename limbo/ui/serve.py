"""Serve the staging panel on :8081. Process #6.

Static files only — the panel talks to Limbo on :8080 from the browser. Kept
separate from the proxy so the panel is visibly just a client of the same
/state endpoint anyone else could poll.
"""
import functools
import http.server
import os
import socketserver
from pathlib import Path

PORT = int(os.environ.get("LIMBO_UI_PORT", "8081"))
ROOT = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Never let the browser cache the panel between takes.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass  # a poll every 500ms would bury the terminal


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    handler = functools.partial(Handler, directory=str(ROOT))
    with socketserver.TCPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"staging panel on http://127.0.0.1:{PORT}", flush=True)
        httpd.serve_forever()
