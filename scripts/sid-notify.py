#!/usr/bin/env python3
"""Send phone/desktop notifications when SID needs you or finished something.

Runs every minute (deploy/systemd/sid-ai-notify.timer). Looks at Redis for:
  - a change ready for your approval (not already queued to merge)
  - a job that needs a human (repairs or rebuilds exhausted)
  - a goal that finished or failed
  - a project app that crashed or whose dependency setup failed
  - a failed backup, and the health watchdog turning red
and sends each event once (sid:notify:sent remembers them for 14 days). The
first run only records what is already there, so you are not flooded with
old news.

Where to: /etc/sid-ai/notify.env (root-only), either or both of
  NTFY_URL=https://ntfy.sh/<a long random topic>   (ntfy phone/desktop app)
  DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
plus optional DASHBOARD_URL=http://10.0.0.59:8080 and DISCORD_MENTION=<user
id> (that user is pinged on Discord for major issues: something that needs
you, a failed goal, a crashed app, a failed backup, red health; not for
approvals or finished goals). With neither target set nothing is sent. `sid-notify.py --test` sends a test message.
"""

import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

import redis as redis_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import sid_redis  # noqa: E402  (services/sid_redis.py)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CONFIG_FILE = os.getenv("SID_NOTIFY_ENV", "/etc/sid-ai/notify.env")
SENT_KEY = "sid:notify:sent"  # zset: event key -> time sent
READY_KEY = "sid:notify:initialized"
KEEP_SECONDS = 14 * 24 * 3600
FINAL_GOAL = {"completed": "finished", "failed": "failed", "planning_failed": "could not be planned"}
APP_PROBLEMS = {"crashed": "crashed", "setup_failed": "could not install its dependencies"}


def load_config(path=CONFIG_FILE):
    config = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                config[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    for key in ("NTFY_URL", "DISCORD_WEBHOOK", "DASHBOARD_URL", "DISCORD_MENTION"):
        if os.environ.get(key):
            config[key] = os.environ[key]
    if not config.get("DASHBOARD_URL"):
        config["DASHBOARD_URL"] = f"http://{_host_address()}:8080"
    return config


def _host_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return "localhost"


def _hashes(r, pattern):
    for key in r.scan_iter(pattern):
        if key.count(":") == 2 and r.type(key) == "hash":
            yield key.split(":")[2], r.hgetall(key)


def _title(job):
    text = (job.get("title") or job.get("prompt") or job.get("id") or "").strip().splitlines()
    text = text[0] if text else ""
    return text if len(text) <= 90 else text[:87] + "…"


def events(r, dashboard):
    """Every current event as (key, title, message, link, priority, major)."""
    found = []
    queued = set()
    for key in list(r.scan_iter("sid:merge-queue*")):
        if r.type(key) == "list":
            queued.update(r.lrange(key, 0, -1))
    for job_id, job in _hashes(r, "sid:jobs:*"):
        project = job.get("project_id") or "sid"
        link = f"{dashboard}/#/projects/{project}"
        if (job.get("status") == "awaiting_review" and job.get("review_status") == "complete"
                and job.get("review_verdict") == "pass" and job.get("integration_status") == "passed"
                and job.get("reviewed_commit") and job.get("reviewed_commit") == job.get("integrated_candidate_commit")
                and job_id not in queued):
            found.append((f"approval:{job_id}:{job['integrated_candidate_commit']}", f"Ready for approval · {project}",
                          _title(job), f"{dashboard}/#/", "high", False))
        elif job.get("status") == "needs_human":
            kind = job.get("needs_human_kind") or "repair"
            found.append((f"needs_human:{job_id}:{job.get('updated_at', '')[:10]}", f"Needs you · {project}",
                          f"{_title(job)} (gave up after {kind} attempts)", link, "high", True))
    for goal_id, goal in _hashes(r, "sid:goals:*"):
        status = goal.get("status")
        if status in FINAL_GOAL:
            project = goal.get("project_id") or "sid"
            text = (goal.get("summary") or goal.get("prompt") or goal_id).strip().splitlines()[0][:90]
            failed = status != "completed"
            found.append((f"goal:{goal_id}:{status}", f"Goal {FINAL_GOAL[status]} · {project}", text,
                          f"{dashboard}/#/projects/{project}", "high" if failed else "default", failed))
    for key in r.scan_iter("sid:app-status:*"):
        app = r.hgetall(key)
        state = app.get("state")
        if state in APP_PROBLEMS:
            project = key.split(":", 2)[2]
            found.append((f"app:{project}:{state}:{app.get('commit', '')}", f"App {APP_PROBLEMS[state]} · {project}",
                          (app.get("error") or "See the log on its project page")[:200],
                          f"{dashboard}/#/projects/{project}", "high", True))
    try:
        backup = json.loads(r.get("sid:backup:last") or "{}")
    except ValueError:
        backup = {}
    if backup and backup.get("ok") is False:
        errors = ", ".join(f"{k}: {v}" for k, v in (backup.get("errors") or {}).items())[:200]
        found.append((f"backup:{backup.get('at')}", "Backup failed", errors or "See sid:backup:last", f"{dashboard}/#/", "high", True))
    try:
        health = json.loads(r.get("sid:health") or "{}")
    except ValueError:
        health = {}
    failing = sorted(c["name"] for c in health.get("checks", []) if c.get("level") == "fail")
    if failing:
        details = "; ".join(f"{c['name']}: {c.get('detail', '')}" for c in health["checks"] if c.get("level") == "fail")
        found.append((f"health:{','.join(failing)}", "SID health is red", details[:300], f"{dashboard}/#/", "high", True))
    return found


def send(config, title, message, link, priority="default", major=False, opener=urllib.request.urlopen):
    """Deliver to every configured target; returns how many accepted it."""
    delivered = 0
    if config.get("NTFY_URL"):
        # JSON publishing (POST to the server root) keeps non-ASCII titles intact.
        server, _, topic = config["NTFY_URL"].rstrip("/").rpartition("/")
        body = json.dumps({"topic": topic, "title": title, "message": message, "click": link,
                           "priority": 4 if priority == "high" else 3, "tags": ["robot"]}).encode()
        request = urllib.request.Request(server + "/", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=10):
                delivered += 1
        except Exception as exc:
            print(f"[sid-notify] ntfy failed: {exc}", flush=True)
    if config.get("DISCORD_WEBHOOK"):
        mention = config.get("DISCORD_MENTION", "")
        ping = major and mention.isdigit()
        content = (f"<@{mention}> " if ping else "") + f"**{title}**\n{message}\n{link}"
        # allowed_mentions: only that user can ever be pinged (never @everyone
        # from text in a goal or error message).
        body = json.dumps({"content": content[:1900],
                           "allowed_mentions": {"parse": [], "users": [mention] if ping else []}}).encode()
        request = urllib.request.Request(config["DISCORD_WEBHOOK"], data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "sid-notify"})
        try:
            with opener(request, timeout=10):
                delivered += 1
        except Exception as exc:
            print(f"[sid-notify] discord failed: {exc}", flush=True)
    return delivered


def run(r, config, now=None, sender=send):
    now = time.time() if now is None else now
    current = events(r, config["DASHBOARD_URL"])
    r.zremrangebyscore(SENT_KEY, "-inf", now - KEEP_SECONDS)
    first_run = not r.exists(READY_KEY)
    sent = []
    for key, title, message, link, priority, major in current:
        if r.zscore(SENT_KEY, key) is not None:
            continue
        # Nowhere to send yet: still remember it, so configuring a target
        # later does not flood you with everything that happened before.
        if not first_run and (config.get("NTFY_URL") or config.get("DISCORD_WEBHOOK")):
            if not sender(config, title, message, link, priority, major):
                continue  # try again next minute
            sent.append(key)
        r.zadd(SENT_KEY, {key: now})
    if first_run:
        r.set(READY_KEY, str(now))
    return {"events": len(current), "sent": sent, "first_run": first_run}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = load_config()
    if "--test" in argv:
        if not (config.get("NTFY_URL") or config.get("DISCORD_WEBHOOK")):
            print(f"no NTFY_URL or DISCORD_WEBHOOK in {CONFIG_FILE}")
            return 1
        ok = send(config, "SID test notification", "Notifications from your AI Command Center work. Major issues will ping you like this.", config["DASHBOARD_URL"], major=True)
        print("sent" if ok else "failed")
        return 0 if ok else 1
    r = redis_lib.Redis.from_url(REDIS_URL, password=sid_redis.password(), decode_responses=True)
    result = run(r, config)
    if result["sent"] or result["first_run"]:
        print(f"[sid-notify] {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
