#!/usr/bin/env python3
"""Health watchdog for SID (run by sid-ai-watchdog.timer every 2 minutes).

Checks the things that silently stop the pipeline and publishes the result
to Redis (sid:health, JSON, 15 min TTL) for the dashboard. Logs only when a
check changes state, so the journal stays readable. Never changes anything.

Levels: ok, warn (needs attention soon), fail (pipeline impaired).
"""

import calendar
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import redis as redis_lib

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REPO_ROOT = os.getenv("REPO_ROOT", "/opt/sids-ai-command-center")
EXPECTED_WORKERS = int(os.getenv("WATCHDOG_WORKERS", "6"))
BACKUP_MAX_AGE_HOURS = float(os.getenv("WATCHDOG_BACKUP_MAX_AGE_HOURS", "36"))
DISK_MIN_FREE_PERCENT = float(os.getenv("WATCHDOG_DISK_MIN_FREE_PERCENT", "10"))
UNITS = ["sid-ai-orchestrator", "sid-ai-operator"] + [
    f"sid-ai-worker@{n:02d}" for n in range(1, EXPECTED_WORKERS + 1)]
HEALTH_KEY = "sid:health"
HEALTH_TTL = 900
IN_FLIGHT = {"queued", "claimed", "running", "testing", "reviewing", "repairing", "integrating"}


def check(name, level, detail):
    return {"name": name, "level": level, "detail": detail}


def unit_states(runner=subprocess.run):
    result = runner(["systemctl", "is-active", *UNITS], text=True, capture_output=True)
    return dict(zip(UNITS, result.stdout.split()))


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode()
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body.strip()}


def check_units(states):
    down = [unit for unit in UNITS if states.get(unit) != "active"]
    if not down:
        return check("units", "ok", f"{len(UNITS)} units active")
    return check("units", "fail", "not active: " + ", ".join(f"{u} ({states.get(u, 'unknown')})" for u in down))


def check_heartbeats(r):
    workers = list(r.scan_iter("sid:workers:*"))
    missing = []
    if not list(r.scan_iter("sid:orchestrators:*")):
        missing.append("orchestrator")
    if not list(r.scan_iter("sid:operator-service:*")):
        missing.append("operator service")
    if len(workers) < EXPECTED_WORKERS:
        missing.append(f"workers {len(workers)}/{EXPECTED_WORKERS}")
    if missing:
        return check("heartbeats", "fail", "stale or missing: " + ", ".join(missing))
    busy = sum(1 for key in workers if r.hget(key, "status") == "working")
    return check("heartbeats", "ok", f"orchestrator, operator and {len(workers)} workers live ({busy} busy)")


def check_endpoint(name, url, getter=http_json):
    try:
        body = getter(url)
    except Exception as exc:
        return check(name, "fail", f"{url} unreachable: {exc}")
    if name == "api" and body.get("status") != "healthy":
        return check(name, "fail", f"api reports {body.get('status')!r}")
    return check(name, "ok", "responding")


def check_live_tree(runner=subprocess.run):
    result = runner(["git", "-C", REPO_ROOT, "status", "--porcelain", "--untracked-files=all"],
                    text=True, capture_output=True)
    if result.returncode != 0:
        return check("live_tree", "fail", f"git status failed: {result.stderr.strip()[:200]}")
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    if dirty:
        return check("live_tree", "fail",
                     f"{len(dirty)} uncommitted/untracked path(s); integration refuses a dirty main: "
                     + ", ".join(line[3:] for line in dirty[:5]))
    return check("live_tree", "ok", "clean")


def check_disk(paths=("/", "/var/log"), usage=shutil.disk_usage):
    worst = None
    for path in paths:
        try:
            total, _, free = usage(path)
        except OSError:
            continue
        percent = 100.0 * free / total if total else 0.0
        if worst is None or percent < worst[1]:
            worst = (path, percent)
    if worst is None:
        return check("disk", "warn", "could not read disk usage")
    level = "fail" if worst[1] < DISK_MIN_FREE_PERCENT / 2 else "warn" if worst[1] < DISK_MIN_FREE_PERCENT else "ok"
    return check("disk", level, f"{worst[1]:.0f}% free on {worst[0]}")


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def read_meminfo():
    with open("/proc/meminfo") as source:
        return source.read()


def check_memory(meminfo_reader=read_meminfo):
    try:
        values = meminfo_reader()
        if hasattr(values, "items"):
            available = float(values["MemAvailable"])
            total = float(values["MemTotal"])
        else:
            parsed = {}
            for line in values.splitlines():
                name, value, *_ = line.split()
                parsed[name.rstrip(":")] = float(value)
            available = parsed["MemAvailable"]
            total = parsed["MemTotal"]
        if total <= 0:
            raise ValueError("MemTotal is zero or negative")
        percent = 100.0 * available / total
        warn = _env_float("WATCHDOG_MEM_WARN_PERCENT", 15)
        fail = _env_float("WATCHDOG_MEM_FAIL_PERCENT", 7)
        level = "fail" if percent < fail else "warn" if percent < warn else "ok"
        detail = f"{available / 1024 / 1024:.1f} GiB available of {total / 1024 / 1024:.1f} GiB ({percent:.0f}%)"
        return check("memory", level, detail)
    except Exception as exc:
        return check("memory", "warn", f"cannot read /proc/meminfo: {exc}")


def check_load(loadavg=os.getloadavg, cpu_count=os.cpu_count):
    try:
        loads = loadavg()
        cpus = cpu_count()
        if cpus is None or cpus <= 0:
            raise ValueError("CPU count is unavailable")
        load5 = loads[1]
        warn = _env_float("WATCHDOG_LOAD_WARN_PER_CPU", 1.5) * cpus
        fail = _env_float("WATCHDOG_LOAD_FAIL_PER_CPU", 3) * cpus
        level = "fail" if load5 > fail else "warn" if load5 > warn else "ok"
        return check("load", level, f"load {loads[0]:.2f}/{load5:.2f}/{loads[2]:.2f} on {cpus} CPUs")
    except Exception as exc:
        return check("load", "warn", f"cannot read load average: {exc}")


def check_backup(r, now):
    raw = r.get("sid:backup:last")
    if not raw:
        return check("backup", "warn", "no backup recorded yet")
    try:
        last = json.loads(raw)
        taken = calendar.timegm(time.strptime(last["at"], "%Y%m%dT%H%M%SZ"))  # UTC stamp
    except (ValueError, KeyError, TypeError):
        return check("backup", "warn", "unreadable backup record")
    age_hours = (now - taken) / 3600
    if not last.get("ok"):
        return check("backup", "fail", f"last backup {last['at']} failed: {last.get('errors')}")
    if age_hours > BACKUP_MAX_AGE_HOURS:
        return check("backup", "warn", f"last backup is {age_hours:.0f}h old")
    return check("backup", "ok", f"last backup {age_hours:.1f}h ago")


def check_pipeline(r):
    notes, level = [], "ok"
    cooldown = r.get("sid:provider-cooldown:claude")
    if cooldown:
        notes.append(f"claude cooling down ({cooldown[:80]})")
        level = "warn"
    needs_human, invalid = [], []
    for key in r.scan_iter("sid:jobs:*"):
        job = r.hgetall(key)
        if job.get("status") == "needs_human":
            needs_human.append(key.rsplit(":", 1)[-1])
        if job.get("merge_queue_state") == "invalid" and job.get("status") == "awaiting_review":
            invalid.append(key.rsplit(":", 1)[-1])
    if needs_human:
        notes.append(f"{len(needs_human)} job(s) need a human: {', '.join(needs_human[:5])}")
        level = "warn"
    if invalid:
        notes.append(f"{len(invalid)} queued approval(s) voided, approve again: {', '.join(invalid[:5])}")
        level = "warn"
    return check("pipeline", level, "; ".join(notes) or "no hand-offs pending")


def run_checks(r, now=None, runner=subprocess.run, getter=http_json, usage=shutil.disk_usage,
               meminfo_reader=read_meminfo, loadavg=os.getloadavg, cpu_count=os.cpu_count):
    now = time.time() if now is None else now
    checks = [check_units(unit_states(runner))]
    try:
        r.ping()
        checks += [check("redis", "ok", "responding"), check_heartbeats(r), check_backup(r, now),
                   check_pipeline(r)]
    except Exception as exc:
        checks.append(check("redis", "fail", f"unreachable: {exc}"))
    checks += [
        check_endpoint("api", "http://127.0.0.1:8000/health", getter),
        check_endpoint("web", "http://127.0.0.1:8080/health", getter),
        check_live_tree(runner),
        check_disk(usage=usage),
        check_memory(meminfo_reader),
        check_load(loadavg, cpu_count),
    ]
    order = {"ok": 0, "warn": 1, "fail": 2}
    overall = max((c["level"] for c in checks), key=order.__getitem__)
    return {"status": overall, "checked_at": now, "checks": checks}


def publish(r, report):
    """Store the report; return lines describing checks whose level changed."""
    changes = []
    try:
        previous = json.loads(r.get(HEALTH_KEY) or "{}")
    except (ValueError, TypeError):
        previous = {}
    before = {c["name"]: c["level"] for c in previous.get("checks", [])}
    for c in report["checks"]:
        if before.get(c["name"]) != c["level"]:
            changes.append(f"{c['name']}: {before.get(c['name'], 'new')} -> {c['level']} ({c['detail']})")
    r.set(HEALTH_KEY, json.dumps(report), ex=HEALTH_TTL)
    return changes


def main():
    r = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    report = run_checks(r)
    try:
        for line in publish(r, report):
            print(f"[sid-watchdog] {line}", flush=True)
    except Exception as exc:
        print(f"[sid-watchdog] could not publish: {exc}; status {report['status']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
