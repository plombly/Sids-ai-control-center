#!/usr/bin/env python3
"""Remote access through Tailscale: reach LAIka and its apps from your own
devices anywhere, without opening a port to the internet.

    laika-remote.py status    what Tailscale is doing (laika:remote:status)
    laika-remote.py install   install Tailscale (its official installer)
    laika-remote.py login     connect this server to your tailnet: the
                              sign-in link appears on the dashboard
    laika-remote.py logout    disconnect and remove this server from the tailnet

Run as root by the operator service (Settings -> Remote access). LAIka never
turns on Tailscale Funnel or Serve (they publish to the internet) or
Tailscale SSH (a root shell over the tailnet), and the status reports them
if someone else did. Progress for the dashboard: laika:remote:progress
(install), laika:remote:login (sign-in link).
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401
import laika_redis  # noqa: E402

INSTALLER = "https://tailscale.com/install.sh"
LOGIN_WAIT_SECONDS = 600
URL = re.compile(r"https://login\.tailscale\.com/\S+")


def redis_client():
    import redis as redis_lib
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def put(r, key, **fields):
    r.set(key, json.dumps({**fields, "at": time.time()}), ex=3600)


def tailscale(*args, runner=subprocess.run, timeout=30):
    return runner(["tailscale", *args], capture_output=True, text=True, timeout=timeout)


def hostname():
    """This server's name on the tailnet: the dashboard's server name, else the host's."""
    wanted = os.environ.get("SERVER_NAME", "") or socket.gethostname()
    name = re.sub(r"[^a-z0-9-]+", "-", wanted.lower()).strip("-")[:60]
    return name or "laika"


def status(runner=subprocess.run, which=shutil.which):
    """What the dashboard shows. Never raises."""
    if not which("tailscale"):
        return {"installed": False, "state": "not_installed", "checked_at": time.time()}
    info = {"installed": True, "checked_at": time.time()}
    version = tailscale("version", runner=runner)
    info["version"] = (version.stdout.splitlines() or [""])[0].strip()
    result = tailscale("status", "--json", runner=runner)
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        data = {}
    if not data:
        info["state"] = "stopped" if "not running" in (result.stderr + result.stdout).lower() else "unknown"
        info["error"] = (result.stderr or result.stdout).strip()[:300]
        return info
    me = data.get("Self") or {}
    info.update(state={"Running": "connected", "NeedsLogin": "needs_login", "Stopped": "stopped",
                       "NoState": "starting", "Starting": "starting"}.get(data.get("BackendState"), "unknown"),
                ips=me.get("TailscaleIPs") or [], dns_name=(me.get("DNSName") or "").rstrip("."),
                hostname=me.get("HostName", ""), tailnet=(data.get("CurrentTailnet") or {}).get("Name", ""),
                magic_dns=bool((data.get("CurrentTailnet") or {}).get("MagicDNSEnabled")))
    # Anything that would publish LAIka beyond the tailnet, or open a root shell.
    for kind in ("funnel", "serve"):
        shown = tailscale(kind, "status", "--json", runner=runner)
        try:
            config = json.loads(shown.stdout or "{}")
        except ValueError:
            config = {}
        info[kind] = bool(config.get("AllowFunnel") if kind == "funnel" else (config.get("TCP") or config.get("Web")))
    prefs = tailscale("debug", "prefs", runner=runner)
    try:
        info["ssh"] = bool(json.loads(prefs.stdout or "{}").get("RunSSH"))
    except ValueError:
        info["ssh"] = False
    return info


def publish_status(r, runner=subprocess.run, which=shutil.which):
    info = status(runner, which)
    r.set("laika:remote:status", json.dumps(info))
    return info


def install(r, runner=subprocess.run, fetch=None):
    if shutil.which("tailscale"):
        put(r, "laika:remote:progress", state="done", message="Tailscale is already installed")
        return publish_status(r)
    put(r, "laika:remote:progress", state="installing", message="Downloading Tailscale's installer")
    try:
        script = (fetch or (lambda url: urllib.request.urlopen(url, timeout=60).read()))(INSTALLER)
        with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as handle:
            handle.write(script)
        put(r, "laika:remote:progress", state="installing", message="Installing Tailscale (a minute or two)")
        result = runner(["sh", handle.name], capture_output=True, text=True, timeout=900)
        os.unlink(handle.name)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[-400:])
        runner(["systemctl", "enable", "--now", "tailscaled"], capture_output=True, text=True, timeout=60)
    except Exception as exc:
        put(r, "laika:remote:progress", state="failed", message=f"Installing Tailscale failed: {exc}"[:500])
        return publish_status(r)
    put(r, "laika:remote:progress", state="done", message="Tailscale is installed. Connect it to your account next.")
    return publish_status(r)


def login(r, popen=subprocess.Popen, clock=time.time):
    """`tailscale up` until the owner opens the sign-in link and approves."""
    if not shutil.which("tailscale"):
        put(r, "laika:remote:login", state="failed", message="Install Tailscale first")
        return 1
    put(r, "laika:remote:login", state="starting", message="Asking Tailscale for a sign-in link")
    process = popen(["tailscale", "up", "--reset", f"--hostname={hostname()}", "--accept-dns=false",
                     "--ssh=false", "--accept-routes=false", f"--timeout={LOGIN_WAIT_SECONDS}s"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = clock() + LOGIN_WAIT_SECONDS
    sent = False
    for line in process.stdout:
        found = URL.search(line)
        if found and not sent:
            put(r, "laika:remote:login", state="waiting", url=found.group(0),
                message="Open the link, sign in to Tailscale and approve this server")
            sent = True
        if clock() > deadline:
            process.kill()
            break
    code = process.wait()
    info = publish_status(r)
    if code == 0 and info.get("state") == "connected":
        put(r, "laika:remote:login", state="done", message=f"Connected as {info.get('dns_name') or hostname()}")
        return 0
    put(r, "laika:remote:login", state="failed",
        message="The sign-in did not finish (the link expires after 10 minutes). Try again.")
    return 1


def logout(r, runner=subprocess.run):
    if shutil.which("tailscale"):
        tailscale("logout", runner=runner)
    put(r, "laika:remote:login", state="idle", message="Disconnected")
    return publish_status(r)


def main(argv):
    if os.geteuid() != 0:
        print("Run it as root (the operator service does).", file=sys.stderr)
        return 2
    command = argv[1] if len(argv) > 1 else "status"
    r = redis_client()
    if command == "status":
        print(json.dumps(publish_status(r)))
        return 0
    if command == "install":
        print(json.dumps(install(r)))
        return 0
    if command == "login":
        return login(r)
    if command == "logout":
        print(json.dumps(logout(r)))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
