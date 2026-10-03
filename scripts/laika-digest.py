#!/usr/bin/env python3
"""Post the weekly digest (apps/api/digest.py) on the schedule from the
dashboard settings (default Sunday 18:00, server time). Runs hourly from
deploy/systemd/laika-digest.timer and posts at most once per week
(laika:digest:last). `--now` posts immediately, `--print` only prints.

People with their own notifications (Settings → My notifications) get their
own digest on their own schedule, covering only the projects they may see
(laika:digest:last:<name>); backups and restore checks only for
administrators.
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path

import redis as redis_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment, before any configuration is read)
import laika_redis  # noqa: E402
import laika_projects  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
import digest  # noqa: E402
import notify_core  # noqa: E402
import access  # noqa: E402

LAST_KEY = "laika:digest:last"


def collect(r):
    def hashes(pattern):
        out = {}
        for key in r.scan_iter(pattern):
            if key.count(":") == 2 and r.type(key) == "hash":
                out[key.split(":", 2)[2]] = r.hgetall(key)
        return out
    found = laika_projects.all_projects(r)
    if os.environ.get("LAIKA_BUILTIN_PROJECT", "1") == "0":
        found = [p for p in found if not p.is_builtin]  # production: no built-in project
    projects = [(p.id, "LAIka" if p.is_builtin else p.name) for p in found]
    parents = {p.id: p.parent for p in found}
    events = {pid: r.lrange(f"laika:events:{pid}", 0, 499) for pid, _ in projects}
    apps = {pid: r.hgetall(f"laika:app-status:{pid}") for pid, _ in projects}
    load = lambda key: json.loads(r.get(key) or "null")
    return {"projects": projects, "parents": parents, "goals": hashes("laika:goals:*"), "jobs": hashes("laika:jobs:*"), "events": events,
            "apps": apps, "backup": load("laika:backup:last"), "restore": load("laika:backup:restore-check")}


def for_person(r, data, user):
    """The digest data limited to what this person may see."""
    ctx = access.context_for(user)
    levels = access.levels(r, ctx)
    if levels is None:
        return data
    keep = lambda record: (record.get("project_id") or "laika") in levels
    projects = [(pid, name) for pid, name in data["projects"] if pid in levels]
    return {**data, "projects": projects, "parents": {k: v for k, v in data["parents"].items() if k in levels},
            "goals": {k: v for k, v in data["goals"].items() if keep(v)},
            "jobs": {k: v for k, v in data["jobs"].items() if keep(v)},
            "events": {k: v for k, v in data["events"].items() if k in levels},
            "apps": {k: v for k, v in data["apps"].items() if k in levels}, "backup": None, "restore": None}


def people(r, now, data_once, dashboard, force=False, sender=None, person_targets=None):
    """Each person's own digest when it is due for them; returns who got one."""
    sender = sender or (lambda targets, title, text, mode: notify_core.send(
        targets, title, text, "", mode, log=lambda line: print(line, flush=True)))
    load = person_targets or (lambda name: notify_core.load_targets(person=name))
    sent = []
    for user in access.all_users(r):
        if user["disabled"] or not user.get("password"):
            continue
        targets = load(user["name"])
        settings = notify_core.load_settings(r, user["name"])
        mode = settings["events"].get("digest", "post")
        if mode == "off" or not notify_core.has_target(targets):
            continue
        key = f"{LAST_KEY}:{user['name']}"
        is_due, week = due(settings, now, r.get(key))
        if not (is_due or force):
            continue
        title, text = digest.build(time.time(), dashboard=dashboard, **for_person(r, data_once(), user))
        if sender(targets, title, text, mode):
            r.set(key, week)
            sent.append(user["name"])
    return sent


def due(settings, now, last_week):
    """True in the scheduled hour of the scheduled day, once per ISO week."""
    schedule = settings["digest"]
    week = now.strftime("%G-W%V")
    hour = int(schedule["time"].split(":")[0])
    return (notify_core.DAYS[now.weekday()] == schedule["day"] and now.hour == hour and last_week != week), week


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    r = redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                 password=laika_redis.password(), decode_responses=True)
    settings = notify_core.load_settings(r)
    targets = notify_core.load_targets()
    now = datetime.datetime.now()
    cache = {}
    data_once = lambda: cache.setdefault("data", collect(r))
    dashboard = targets.get("DASHBOARD_URL", "")
    if "--print" in argv:
        title, text = digest.build(time.time(), dashboard=dashboard, **data_once())
        print(title)
        print(text)
        return 0
    failed = False
    is_due, week = due(settings, now, r.get(LAST_KEY))
    if is_due or "--now" in argv:
        title, text = digest.build(time.time(), dashboard=dashboard, **data_once())
        mode = settings["events"].get("digest", "post")
        if mode != "off" and notify_core.has_target(targets):
            if notify_core.send(targets, title, text, "", mode, log=lambda line: print(line, flush=True)):
                print(f"[laika-digest] sent {title}", flush=True)
                r.set(LAST_KEY, week)
            else:
                failed = True  # try again next hour
        else:
            r.set(LAST_KEY, week)
    for name in people(r, now, data_once, dashboard, force="--now" in argv):
        print(f"[laika-digest] sent {name}'s digest", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
