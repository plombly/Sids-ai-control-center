"""{{NAME}}: a small JSON web service using only Python's standard library.

LAIka runs it with PORT set; data is kept in DATA_DIR (the app's data folder).
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def data_file():
    folder = Path(os.environ.get("DATA_DIR", "data"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "items.json"


def load_items():
    try:
        return json.loads(data_file().read_text())
    except (OSError, ValueError):
        return []


def add_item(name):
    name = str(name or "").strip()
    if not name or len(name) > 200:
        raise ValueError("name must be 1-200 characters")
    items = load_items()
    item = {"id": len(items) + 1, "name": name, "created_at": time.time()}
    data_file().write_text(json.dumps(items + [item], indent=1))
    return item


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"status": "ok"})
        if self.path == "/items":
            return self.send(200, load_items())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/items":
            return self.send(404, {"error": "not found"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 100000)
            item = add_item(json.loads(self.rfile.read(length) or b"{}").get("name"))
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        self.send(201, item)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((os.environ.get("HOST", "0.0.0.0"), port), Handler)
    print(f"{{NAME}} listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
