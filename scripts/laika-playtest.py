#!/usr/bin/env python3
"""Play-test a project's web app the way a person would see it.

    laika-playtest.py PROJECT_ID [--port N] [--commit SHA]

Started by the apps service (unit laika-playtest-<id>) after the app was
deployed from a new commit. It opens the app's page in headless Chrome
(Docker image PLAYTEST_IMAGE, its own bridge network, LAIka's host reached
as host.docker.internal; the app listens on 0.0.0.0 anyway), waits for it to
settle, and records:
  - console errors and uncaught exceptions
  - requests that failed or answered 4xx/5xx
  - whether the page is blank (no visible text and no canvas/img/svg)
  - a screenshot (<project root>/playtest/latest.png)
in laika:playtest:<id> (JSON). "ok" is false when the page did not load, is
blank, or threw errors; the project page shows the result and the notifier
reports a page that looks broken. Chrome is driven over its DevTools
protocol with a small standard-library WebSocket client, so nothing extra is
installed on the host.
"""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401
import laika_projects  # noqa: E402
import laika_redis  # noqa: E402

IMAGE = os.environ.get("PLAYTEST_IMAGE", "chromedp/headless-shell:stable")
SETTLE_SECONDS = float(os.environ.get("PLAYTEST_SETTLE_SECONDS", "4"))
LOAD_TIMEOUT = float(os.environ.get("PLAYTEST_TIMEOUT_SECONDS", "30"))
KEEP_ITEMS = 20


# --- a tiny WebSocket client (enough for Chrome's DevTools protocol) -------------------------------

class WebSocket:
    def __init__(self, url, timeout=10):
        assert url.startswith("ws://")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buffer = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(payload))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        message = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            data = self._read(length)
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError("websocket closed")
            if opcode in (0x9, 0xA):
                continue  # ping / pong
            message += data
            if first & 0x80:
                return message.decode(errors="replace")


class Chrome:
    """Commands and events over one DevTools page connection."""

    def __init__(self, ws):
        self.ws, self.next_id, self.events = ws, 0, []

    def call(self, method, timeout=15, **params):
        self.next_id += 1
        wanted = self.next_id
        self.ws.send(json.dumps({"id": wanted, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv())
            if message.get("id") == wanted:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)
        raise TimeoutError(method)

    def pump(self, seconds):
        """Collect events for a while."""
        self.ws.sock.settimeout(0.5)
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                message = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                continue
            if "method" in message:
                self.events.append(message)
        self.ws.sock.settimeout(15)


# --- the test ---------------------------------------------------------------------------------------

def summarize(events, status, base):
    console, exceptions, failed = [], [], []
    requests = {}
    for event in events:
        method, params = event.get("method"), event.get("params", {})
        if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            text = " ".join(str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", []))
            console.append(text[:300])
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            text = (details.get("exception") or {}).get("description") or details.get("text") or "exception"
            exceptions.append(text.splitlines()[0][:300])
        elif method == "Network.requestWillBeSent":
            requests[params.get("requestId")] = params.get("request", {}).get("url", "")
        elif method == "Network.responseReceived":
            response = params.get("response", {})
            if int(response.get("status") or 0) >= 400:
                failed.append(f"{response.get('status')} {response.get('url', '')[:200]}")
        elif method == "Network.loadingFailed" and not params.get("canceled"):
            url = requests.get(params.get("requestId"), "")
            failed.append(f"{params.get('errorText', 'failed')} {url[:200]}")
    short = lambda items: [item.replace(base, "") for item in items][:KEEP_ITEMS]
    return {"console_errors": short(console), "page_errors": short(exceptions), "failed_requests": short(failed),
            "status": status}


def run_test(url, shot_path, docker=subprocess.run, connect=WebSocket):
    """The play-test result dict (without project fields)."""
    name = f"laika-playtest-{os.getpid()}"
    started = docker(["docker", "run", "-d", "--rm", "--name", name, "--memory", "1g", "--cpus", "1",
                      "--pids-limit", "512", "--add-host", "host.docker.internal:host-gateway",
                      "-p", "127.0.0.1::9222", IMAGE], capture_output=True, text=True)
    if started.returncode:
        return {"ok": False, "error": f"could not start the browser: {(started.stderr or '').strip()[:300]}"}
    try:
        port = docker(["docker", "port", name, "9222/tcp"], capture_output=True, text=True).stdout.split(":")[-1].strip()
        target = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/new?about:blank", timeout=2) as response:
                    target = json.load(response)
                break
            except Exception:
                try:  # newer Chrome wants PUT for /json/new
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
                    with urllib.request.urlopen(request, timeout=2) as response:
                        target = json.load(response)
                    break
                except Exception:
                    time.sleep(0.5)
        if not target:
            return {"ok": False, "error": "the browser did not start"}
        ws_url = target["webSocketDebuggerUrl"].replace("127.0.0.1:9222", f"127.0.0.1:{port}").replace(
            "localhost:9222", f"127.0.0.1:{port}")
        chrome = Chrome(connect(ws_url))
        for domain in ("Page", "Runtime", "Network", "Log"):
            chrome.call(f"{domain}.enable")
        chrome.call("Emulation.setDeviceMetricsOverride", width=1280, height=800, deviceScaleFactor=1, mobile=False)
        navigated = chrome.call("Page.navigate", url=url, timeout=LOAD_TIMEOUT)
        if navigated.get("errorText"):
            return {"ok": False, "error": f"the page did not load: {navigated['errorText']}"}
        chrome.pump(SETTLE_SECONDS)
        page = chrome.call("Runtime.evaluate", returnByValue=True, expression=(
            "({title: document.title, text: (document.body && document.body.innerText || '').trim().length, "
            "media: document.querySelectorAll('canvas, img, svg, video').length})"))["result"].get("value", {})
        shot = chrome.call("Page.captureScreenshot", format="png", timeout=30)
        Path(shot_path).parent.mkdir(parents=True, exist_ok=True)
        Path(shot_path).write_bytes(base64.b64decode(shot["data"]))
        status = next((int(e["params"]["response"].get("status") or 0) for e in chrome.events
                       if e.get("method") == "Network.responseReceived" and e["params"].get("type") == "Document"), 0)
        result = summarize(chrome.events, status, url.rstrip("/"))
        result.update(title=str(page.get("title") or "")[:200], blank=not page.get("text") and not page.get("media"))
        result["ok"] = bool(status and status < 400 and not result["blank"] and not result["page_errors"])
        return result
    except Exception as exc:
        return {"ok": False, "error": f"play-test failed: {exc}"[:400]}
    finally:
        docker(["docker", "rm", "-f", name], capture_output=True, text=True)


def main(argv):
    project_id = argv[1]
    port = argv[argv.index("--port") + 1] if "--port" in argv else ""
    commit = argv[argv.index("--commit") + 1] if "--commit" in argv else ""
    import redis as redis_lib
    r = redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                 password=laika_redis.password(), decode_responses=True)
    project = laika_projects.load(r, project_id)
    port = port or (r.hget(f"laika:app-status:{project_id}", "port") or "")
    shot = Path(project.root) / "playtest" / "latest.png"
    result = run_test(f"http://host.docker.internal:{port}/", shot)
    result.update(project=project_id, commit=commit, port=port, at=time.time(),
                  screenshot=str(shot) if shot.exists() and not result.get("error") else "")
    r.set(f"laika:playtest:{project_id}", json.dumps(result))
    if not result["ok"]:
        why = result.get("error") or ("the page is blank" if result.get("blank") else
                                      f"{len(result.get('page_errors', []))} error(s) on the page")
        laika_projects.record_event(r, project_id, "app_problem", f"Play-test: the app looks broken ({why})",
                                    ref=commit[:12])
    print(json.dumps({k: v for k, v in result.items() if k != "screenshot"})[:2000], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
