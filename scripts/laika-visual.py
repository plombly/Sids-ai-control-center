#!/usr/bin/env python3
"""Render one candidate on LNMT and look at it (services/visual_preview.py).

    laika-visual.py JOB_ID

Started by the apps service (unit laika-visual-<job>, as root) for each job
in laika:visual-queue: pushes the job's integrated candidate to the LNMT
app as branch lnmt-preview/<job>, waits for LNMT's preview address, opens
the front page and up to three more pages from its menu in headless Chrome
at desktop and phone width, saves full-page screenshots in
<project root>/visual/<job>/ and records what it found on the job.
"""

import base64
import importlib.util
import json
import os
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
import visual_preview  # noqa: E402

_spec = importlib.util.spec_from_file_location("laika_playtest", ROOT / "scripts" / "laika-playtest.py")
playtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(playtest)

PUSH_TIMEOUT = 600
MAX_HEIGHT = 7000


def push_preview(project, job_id, commit, runner=subprocess.run):
    """Push the candidate as lnmt-preview/<job>; returns (url, error)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if Path("/etc/laika/gitconfig").exists():
        env["GIT_CONFIG_GLOBAL"] = "/etc/laika/gitconfig"
    ref = f"refs/heads/lnmt-preview/{job_id}"
    try:
        result = runner(["git", "-C", str(project.repo), "-c", "core.hooksPath=/dev/null", "push", "--force", "origin",
                         f"{commit}:{ref}"], capture_output=True, text=True, timeout=PUSH_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return "", "LNMT took too long to make the preview"
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and "LNMT preview" not in output:
        return "", f"push to LNMT failed: {output.strip()[-300:]}"
    return visual_preview.preview_url(output)


def browse(base, out_dir, docker=subprocess.run, connect=playtest.WebSocket):
    """Screenshots and checks of the front page and its menu's pages."""
    name = f"laika-visual-{os.getpid()}"
    started = docker(["docker", "run", "-d", "--rm", "--name", name, "--memory", "1g", "--cpus", "1",
                      "--pids-limit", "512", "-p", "127.0.0.1::9222", playtest.IMAGE], capture_output=True, text=True)
    if started.returncode:
        raise RuntimeError(f"could not start the browser: {(started.stderr or '').strip()[:300]}")
    try:
        port = docker(["docker", "port", name, "9222/tcp"], capture_output=True, text=True).stdout.split(":")[-1].strip()
        target = None
        for _ in range(40):
            for method in ("GET", "PUT"):
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method=method)
                    with urllib.request.urlopen(request, timeout=2) as response:
                        target = json.load(response)
                    break
                except Exception:
                    continue
            if target:
                break
            time.sleep(0.5)
        if not target:
            raise RuntimeError("the browser did not start")
        ws_url = target["webSocketDebuggerUrl"].replace("127.0.0.1:9222", f"127.0.0.1:{port}").replace(
            "localhost:9222", f"127.0.0.1:{port}")
        chrome = playtest.Chrome(connect(ws_url))
        for domain in ("Page", "Runtime", "Network"):
            chrome.call(f"{domain}.enable")
        pages, errors, queue, seen = [], [], ["/"], set()
        while queue and len(pages) < visual_preview.MAX_PAGES:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            page = {"path": path, "shots": {}, "problems": {}}
            for width_name, width, height in visual_preview.WIDTHS:
                chrome.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1,
                            mobile=width < 600)
                chrome.events.clear()
                navigated = chrome.call("Page.navigate", url=base.rstrip("/") + path, timeout=playtest.LOAD_TIMEOUT)
                if navigated.get("errorText"):
                    page["problems"][width_name] = [f"the page did not load: {navigated['errorText']}"]
                    continue
                chrome.pump(playtest.SETTLE_SECONDS)
                found = chrome.call("Runtime.evaluate", returnByValue=True,
                                    expression=visual_preview.CHECKS_JS)["result"].get("value", {}) or {}
                page["title"] = found.get("title", "")
                page["problems"][width_name] = found.get("problems", [])
                summary = playtest.summarize(chrome.events, 0, base.rstrip("/"))
                errors += [e for e in summary["page_errors"] + summary["console_errors"] if e not in errors]
                metrics = chrome.call("Page.getLayoutMetrics")
                full = (metrics.get("cssContentSize") or metrics.get("contentSize") or {}).get("height", height)
                shot = chrome.call("Page.captureScreenshot", format="png", captureBeyondViewport=True, timeout=60,
                                   clip={"x": 0, "y": 0, "width": width, "height": min(max(int(full), height), MAX_HEIGHT),
                                         "scale": 1})
                file = out_dir / f"{(path.strip('/') or 'home').replace('/', '-')}-{width_name}.png"
                file.write_bytes(base64.b64decode(shot["data"]))
                os.chmod(file, 0o640)
                page["shots"][width_name] = str(file)
                if width_name == "desktop":
                    queue += [link for link in found.get("links", []) if link not in seen and link not in queue]
            pages.append(page)
        return pages, errors[:20]
    finally:
        docker(["docker", "rm", "-f", name], capture_output=True, text=True)


def main(argv):
    job_id = argv[1]
    import redis as redis_lib
    r = redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                 password=laika_redis.password(), decode_responses=True)
    key = f"laika:jobs:{job_id}"
    job = r.hgetall(key) or {}
    commit = job.get("visual_commit", "")
    if job.get("visual_status") != "pending" or not commit:
        return 0
    r.hset(key, mapping={"visual_status": "running", "visual_started_at": str(time.time())})
    try:
        project = laika_projects.load(r, job.get("project_id") or "")
        url, error = push_preview(project, job_id, commit)
        if not url:
            raise RuntimeError(error)
        out_dir = Path(project.root) / "visual" / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(out_dir, 0o2750)
        pages, errors = browse(url, out_dir)
        if r.hget(key, "visual_commit") != commit:
            return 0  # a newer candidate asked for its own preview meanwhile
        r.hset(key, mapping={"visual_status": "done", "visual_error": "", "visual_finished_at": str(time.time()),
                             "visual_preview": json.dumps({"url": url, "commit": commit, "pages": pages,
                                                           "errors": errors})})
    except Exception as exc:
        r.hset(key, mapping={"visual_status": "failed", "visual_error": str(exc)[:400],
                             "visual_finished_at": str(time.time())})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
