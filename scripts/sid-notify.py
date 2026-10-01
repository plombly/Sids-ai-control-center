#!/usr/bin/env python3
"""Send Discord / ntfy notifications when SID needs you or finished something.

Runs every minute (deploy/systemd/sid-ai-notify.timer). Each event is sent
once (sid:notify:sent remembers them for 14 days) according to the settings
page (apps/api/notify_core.py): ping, post or off per event type, and quiet
hours that hold non-urgent events until they end. Events:
  approval      a change is ready for your approval (not already queued)
  needs_human   a job is stuck
  goal_done / goal_failed
  app_problem   a project app crashed or could not install its dependencies
  backup_failed a failed backup
  health_red    the health watchdog turned red (incl. a failed restore check)
The first run only records what already exists; events while nothing is
configured or switched off are recorded too, so turning things on later
never floods you. `--test` sends a test message (with a ping).
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
import notify_core  # noqa: E402  (apps/api/notify_core.py, shared with the API)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
SENT_KEY = "sid:notify:sent"  # zset: event key -> time sent
READY_KEY = "sid:notify:initialized"
KEEP_SECONDS = 14 * 24 * 3600
FINAL_GOAL = {"completed": "finished", "failed": "failed", "planning_failed": "could not be planned"}
APP_PROBLEMS = {"crashed": "crashed", "setup_failed": "could not install its dependencies"}


def load_config():
    config = notify_core.load_targets()
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
    """Every current event as (key, type, title, message, link)."""
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
            found.append((f"approval:{job_id}:{job['integrated_candidate_commit']}", "approval",
                          f"Ready for approval · {project}", _title(job), f"{dashboard}/#/"))
        elif job.get("status") == "needs_human":
            kind = job.get("needs_human_kind") or "repair"
            if kind == "network":
                reason = (job.get("network_request_reason") or "").strip().splitlines()
                found.append((f"needs_human:{job_id}:{job.get('updated_at', '')[:10]}", "needs_human",
                              f"Wants internet access · {project}",
                              f"{_title(job)}: its tests need the internet" + (f" ({reason[0][:160]})" if reason else "")
                              + ". Allow it once, always, or keep tests offline.", link))
            else:
                found.append((f"needs_human:{job_id}:{job.get('updated_at', '')[:10]}", "needs_human",
                              f"Needs you · {project}", f"{_title(job)} (gave up after {kind} attempts)", link))
    for goal_id, goal in _hashes(r, "sid:goals:*"):
        status = goal.get("status")
        if status in FINAL_GOAL:
            project = goal.get("project_id") or "sid"
            text = (goal.get("summary") or goal.get("prompt") or goal_id).strip().splitlines()[0][:90]
            kind = "goal_done" if status == "completed" else "goal_failed"
            found.append((f"goal:{goal_id}:{status}", kind, f"Goal {FINAL_GOAL[status]} · {project}", text,
                          f"{dashboard}/#/projects/{project}"))
    for key in r.scan_iter("sid:app-status:*"):
        app = r.hgetall(key)
        state = app.get("state")
        if state in APP_PROBLEMS:
            project = key.split(":", 2)[2]
            found.append((f"app:{project}:{state}:{app.get('commit', '')}", "app_problem",
                          f"App {APP_PROBLEMS[state]} · {project}",
                          (app.get("error") or "See the log on its project page")[:200],
                          f"{dashboard}/#/projects/{project}"))
    try:
        backup = json.loads(r.get("sid:backup:last") or "{}")
    except ValueError:
        backup = {}
    if backup and backup.get("ok") is False:
        errors = ", ".join(f"{k}: {v}" for k, v in (backup.get("errors") or {}).items())[:200]
        found.append((f"backup:{backup.get('at')}", "backup_failed", "Backup failed", errors or "See sid:backup:last",
                      f"{dashboard}/#/"))
    try:
        health = json.loads(r.get("sid:health") or "{}")
    except ValueError:
        health = {}
    failing = sorted(c["name"] for c in health.get("checks", []) if c.get("level") == "fail")
    if failing:
        details = "; ".join(f"{c['name']}: {c.get('detail', '')}" for c in health["checks"] if c.get("level") == "fail")
        found.append((f"health:{','.join(failing)}", "health_red", "SID health is red", details[:300], f"{dashboard}/#/"))
    return found


def send(config, title, message, link, mode="post", opener=urllib.request.urlopen):
    return notify_core.send(config, title, message, link, mode, opener=opener,
                            log=lambda line: print(line, flush=True))


def run(r, config, now=None, sender=send, settings=None, clock=None):
    now = time.time() if now is None else now
    settings = settings or notify_core.load_settings(r)
    current = events(r, config["DASHBOARD_URL"])
    r.zremrangebyscore(SENT_KEY, "-inf", now - KEEP_SECONDS)
    first_run = not r.exists(READY_KEY)
    has_target = bool(config.get("NTFY_URL") or config.get("DISCORD_WEBHOOK"))
    sent, held = [], []
    for key, kind, title, message, link in current:
        if r.zscore(SENT_KEY, key) is not None:
            continue
        mode = notify_core.mode_for(settings, kind, clock)
        if not first_run and has_target and mode == "hold":
            held.append(key)  # quiet hours: it goes out when they end
            continue
        # Off, or nowhere to send yet: still remember it, so switching it on
        # later does not flood you with everything that happened before.
        if not first_run and has_target and mode in ("ping", "post"):
            if not sender(config, title, message, link, mode):
                continue  # try again next minute
            sent.append(key)
        r.zadd(SENT_KEY, {key: now})
    if first_run:
        r.set(READY_KEY, str(now))
    return {"events": len(current), "sent": sent, "held": held, "first_run": first_run}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = load_config()
    if "--test" in argv:
        if not (config.get("NTFY_URL") or config.get("DISCORD_WEBHOOK")):
            print(f"no Discord webhook or ntfy topic in {notify_core.env_file()}")
            return 1
        ok = send(config, "SID test notification", "Notifications from your AI Command Center work. Major issues will ping you like this.", config["DASHBOARD_URL"], mode="ping")
        print("sent" if ok else "failed")
        return 0 if ok else 1
    r = redis_lib.Redis.from_url(REDIS_URL, password=sid_redis.password(), decode_responses=True)
    result = run(r, config)
    if result["sent"] or result["first_run"]:
        print(f"[sid-notify] {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
