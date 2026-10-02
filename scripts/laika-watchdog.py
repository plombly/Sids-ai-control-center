#!/usr/bin/env python3
"""Health watchdog for LAIka (run by laika-watchdog.timer every 2 minutes).

Checks the things that silently stop the pipeline and publishes the result
to Redis (laika:health, JSON, 15 min TTL) for the dashboard. Logs only when a
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

from pathlib import Path
import redis as redis_lib
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment, before any configuration is read)
import laika_redis  # noqa: E402  (services/laika_redis.py)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REPO_ROOT = os.getenv("REPO_ROOT", "/opt/laika")
EXPECTED_WORKERS = int(os.getenv("WATCHDOG_WORKERS", "6"))
BACKUP_MAX_AGE_HOURS = float(os.getenv("WATCHDOG_BACKUP_MAX_AGE_HOURS", "36"))
DISK_MIN_FREE_PERCENT = float(os.getenv("WATCHDOG_DISK_MIN_FREE_PERCENT", "10"))
UNITS = ["laika-orchestrator", "laika-operator"] + [
    f"laika-worker@{n:02d}" for n in range(1, EXPECTED_WORKERS + 1)]
HEALTH_KEY = "laika:health"
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
    workers = list(r.scan_iter("laika:workers:*"))
    missing = []
    if not list(r.scan_iter("laika:orchestrators:*")):
        missing.append("orchestrator")
    if not list(r.scan_iter("laika:operator-service:*")):
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
    raw = r.get("laika:backup:last")
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


RESTORE_CHECK_MAX_DAYS = 40  # monthly timer, with slack


def check_restore(r, now):
    """The monthly backup restore check (scripts/backup-restore-check.py)."""
    raw = r.get("laika:backup:restore-check")
    if not raw:
        return check("restore_check", "warn", "backups have not been test-restored yet")
    try:
        last = json.loads(raw)
        age_days = (now - float(last["at"])) / 86400
    except (ValueError, KeyError, TypeError):
        return check("restore_check", "warn", "unreadable restore-check record")
    if not last.get("ok"):
        failed = ", ".join(f"{c['name']}: {c['detail']}" for c in last.get("checks", []) if not c.get("ok"))
        return check("restore_check", "fail", f"backup {last.get('snapshot')} did not restore: {failed}"[:300])
    if age_days > RESTORE_CHECK_MAX_DAYS:
        return check("restore_check", "warn", f"last restore check was {age_days:.0f} days ago")
    return check("restore_check", "ok", f"backup {last.get('snapshot')} restored fine {age_days:.0f} days ago")


def check_pipeline(r):
    notes, level = [], "ok"
    cooldown = r.get("laika:provider-cooldown:claude")
    if cooldown:
        notes.append(f"claude cooling down ({cooldown[:80]})")
        level = "warn"
    needs_human, invalid = [], []
    for key in r.scan_iter("laika:jobs:*"):
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
        checks += [check("redis", "ok", "responding"), check_heartbeats(r), check_backup(r, now), check_restore(r, now),
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
        check_exposure(host_info(runner, meminfo_reader, cpu_count)),
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


SYSTEM_INFO_KEY = "laika:system-info"
HOST_KEY = "laika:host-info"


def host_addresses(runner=subprocess.run):
    """IPv4 addresses on this server's interfaces, each marked public or not."""
    import ipaddress
    result = runner(["ip", "-j", "-4", "addr"], text=True, capture_output=True)
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    found = []
    for interface in data:
        name = interface.get("ifname", "")
        if name == "lo" or name.startswith(("docker", "br-", "veth", "laika-build")):
            continue
        for info in interface.get("addr_info", []):
            try:
                address = ipaddress.ip_address(info.get("local", ""))
            except ValueError:
                continue
            found.append({"interface": name, "address": str(address),
                          "public": address.is_global})
    return found


def host_info(runner=subprocess.run, meminfo_reader=read_meminfo, cpu_count=os.cpu_count):
    """For the web setup and the exposure check: CPUs, memory, addresses."""
    memory_kb = 0
    try:
        values = meminfo_reader()
        if hasattr(values, "items"):
            memory_kb = float(values.get("MemTotal", 0))
        else:
            line = next((l for l in values.splitlines() if l.startswith("MemTotal:")), "MemTotal: 0")
            memory_kb = float(line.split()[1])
    except (OSError, TypeError, ValueError, IndexError):
        memory_kb = 0
    addresses = host_addresses(runner)
    return {"cpus": cpu_count() or 1, "memory_gb": round(memory_kb / 1048576, 1), "addresses": addresses,
            "public": [a["address"] for a in addresses if a["public"]], "checked_at": time.time()}


def check_exposure(info):
    """LAIka must only be reached over a LAN or VPN (docs/security.md)."""
    if info.get("public"):
        return check("exposure", "warn", f"this server has a public address ({', '.join(info['public'])}): make sure "
                     "port 8080 is not reachable from the internet (firewall, or a LAN/VPN-only address)")
    return check("exposure", "ok", "no public addresses on this server")


def system_info(runner=subprocess.run):
    """What the dashboard shows on LAIka's own project page (the API container
    has no git): its GitHub remote, branch and current commit."""
    def git(*args):
        result = runner(["git", "-C", REPO_ROOT, *args], text=True, capture_output=True)
        return result.stdout.strip() if result.returncode == 0 else ""
    remote = git("remote", "get-url", "origin")
    # Never publish credentials embedded in an https remote.
    if "@" in remote and remote.startswith("http"):
        remote = "https://" + remote.split("@", 1)[1]
    return {"remote": remote, "branch": git("symbolic-ref", "--short", "HEAD"),
            "head": git("rev-parse", "--short", "HEAD"), "subject": git("log", "-1", "--format=%s"),
            "checked_at": str(time.time())}


def main():
    r = redis_lib.Redis.from_url(REDIS_URL, password=laika_redis.password(), decode_responses=True)
    try:
        r.set(SYSTEM_INFO_KEY, json.dumps(system_info()), ex=HEALTH_TTL)
    except Exception as exc:
        print(f"[laika-watchdog] could not publish system info: {exc}", flush=True)
    try:
        r.set(HOST_KEY, json.dumps(host_info()), ex=HEALTH_TTL)
    except Exception as exc:
        print(f"[laika-watchdog] could not publish host info: {exc}", flush=True)
    report = run_checks(r)
    try:
        for line in publish(r, report):
            print(f"[laika-watchdog] {line}", flush=True)
    except Exception as exc:
        print(f"[laika-watchdog] could not publish: {exc}; status {report['status']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
