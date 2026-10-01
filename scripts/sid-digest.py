#!/usr/bin/env python3
"""Post the weekly digest (apps/api/digest.py) on the schedule from the
dashboard settings (default Sunday 18:00, server time). Runs hourly from
deploy/systemd/sid-ai-digest.timer and posts at most once per week
(sid:digest:last). `--now` posts immediately, `--print` only prints.
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path

import redis as redis_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import sid_redis  # noqa: E402
import sid_projects  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
import digest  # noqa: E402
import notify_core  # noqa: E402

LAST_KEY = "sid:digest:last"


def collect(r):
    def hashes(pattern):
        out = {}
        for key in r.scan_iter(pattern):
            if key.count(":") == 2 and r.type(key) == "hash":
                out[key.split(":", 2)[2]] = r.hgetall(key)
        return out
    projects = [(p.id, "SID" if p.is_sid else p.name) for p in sid_projects.all_projects(r)]
    events = {pid: r.lrange(f"sid:events:{pid}", 0, 499) for pid, _ in projects}
    apps = {pid: r.hgetall(f"sid:app-status:{pid}") for pid, _ in projects}
    load = lambda key: json.loads(r.get(key) or "null")
    return {"projects": projects, "goals": hashes("sid:goals:*"), "jobs": hashes("sid:jobs:*"), "events": events,
            "apps": apps, "backup": load("sid:backup:last"), "restore": load("sid:backup:restore-check")}


def due(settings, now, last_week):
    """True in the scheduled hour of the scheduled day, once per ISO week."""
    schedule = settings["digest"]
    week = now.strftime("%G-W%V")
    hour = int(schedule["time"].split(":")[0])
    return (notify_core.DAYS[now.weekday()] == schedule["day"] and now.hour == hour and last_week != week), week


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    r = redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                 password=sid_redis.password(), decode_responses=True)
    settings = notify_core.load_settings(r)
    targets = notify_core.load_targets()
    now = datetime.datetime.now()
    is_due, week = due(settings, now, r.get(LAST_KEY))
    if not (is_due or "--now" in argv or "--print" in argv):
        return 0
    data = collect(r)
    title, text = digest.build(time.time(), dashboard=targets.get("DASHBOARD_URL", ""), **data)
    if "--print" in argv:
        print(title)
        print(text)
        return 0
    mode = settings["events"].get("digest", "post")
    if mode != "off" and (targets.get("DISCORD_WEBHOOK") or targets.get("NTFY_URL")):
        if not notify_core.send(targets, title, text, "", mode, log=lambda line: print(line, flush=True)):
            return 1  # try again next hour
        print(f"[sid-digest] sent {title}", flush=True)
    r.set(LAST_KEY, week)
    return 0


if __name__ == "__main__":
    sys.exit(main())
