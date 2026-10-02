#!/usr/bin/env python3
"""LAIka worker scaler (host service, unit laika-scaler.service).

Owns how many workers run: workers 1..target run, the rest are stopped.
The target (Redis laika:scaler:target) changes by

- the operator: + / - on the dashboard (POST /api/workers/scale), or
  WORKER_COUNT when automatic scaling is off;
- automatic scaling (AUTOSCALE, on by default), within MIN_WORKERS and
  MAX_WORKERS (0 = suggested from this server's CPUs and memory):
  * up, only when work could run in parallel right now: jobs in the ready
    queue (dependency-waiting and scope-blocked jobs are not there), with
    Claude-bound jobs counted only up to the free Claude slots, outnumber
    the idle workers for SCALE_UP_WAIT_MINUTES, and the server has room;
  * down, when workers have sat idle with nothing to run for
    SCALE_DOWN_IDLE_MINUTES;
- resource pressure, whatever the mode: available memory under
  PRESSURE_MEMORY_PERCENT (30 s) or the 5-minute load above PRESSURE_LOAD x
  CPUs (2 min) drains the newest worker, one at a time; under
  CRITICAL_MEMORY_PERCENT no worker claims new work (laika:scaler:hold)
  until memory is back above the pressure level.

A worker above the target is drained, never killed: laika:worker-drain:<id>
makes it stop claiming, and its unit is stopped only once its heartbeat
says "draining" with no job (the worker writes that only after it has
seen the drain key, so it can no longer pick a job up). If this service
stops, workers simply keep running as they are; the hold key expires.

State for the dashboard: laika:scaler:state (JSON, 60 s), decisions in
laika:scaler:log (newest first, 100 kept).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment)

sys.path.insert(0, str(ROOT / "apps" / "api"))
import settings_schema  # noqa: E402

TARGET_KEY = "laika:scaler:target"
HOLD_KEY = "laika:scaler:hold"
MANUAL_KEY = "laika:scaler:manual-until"
STATE_KEY = "laika:scaler:state"
LOG_KEY = "laika:scaler:log"
QUEUE = "laika:jobs"
UNIT_LIMIT = 32
LOOP_SECONDS = 15
ADD_COOLDOWN = 60
PRESSURE_COOLDOWN = 120
IDLE_STEP = 300          # after one idle removal, the next waits 5 more minutes
CALM_AFTER_DROP = 600    # no growing for 10 min after a pressure drop (no flapping)
MEMORY_PRESSURE_FOR = 30
LOAD_PRESSURE_FOR = 120
SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")


def drain_key(n):
    return f"laika:worker-drain:{worker_id(n)}"


def worker_id(n):
    return f"laika-worker-{n:02d}"


def unit(n):
    return f"laika-worker@{n:02d}.service"


class Config:
    def __init__(self, env, cpus, memory_gb):
        def num(key, default, kind=float):
            try:
                return kind(env.get(key, default))
            except (TypeError, ValueError):
                return kind(default)
        self.auto = str(env.get("AUTOSCALE", "1")).lower() in ("1", "true", "yes", "on")
        self.fixed = num("WORKER_COUNT", 8, int)
        suggested = settings_schema.suggested_workers(cpus, memory_gb)
        self.max = min(UNIT_LIMIT, num("MAX_WORKERS", 0, int) or suggested)
        self.min = max(1, min(num("MIN_WORKERS", 1, int), self.max))
        self.up_wait = num("SCALE_UP_WAIT_MINUTES", 3) * 60
        self.down_idle = num("SCALE_DOWN_IDLE_MINUTES", 15) * 60
        self.pressure_memory = num("PRESSURE_MEMORY_PERCENT", 15)
        self.critical_memory = num("CRITICAL_MEMORY_PERCENT", 7)
        self.pressure_load = num("PRESSURE_LOAD", 1.5)


# --- what the queue could run in parallel --------------------------------------------------

def ready_work(queue_items, role_provider, free_claude, claude_cooling):
    """(runnable, projects): ready jobs that could start right now if a
    worker were free, and how many projects they belong to."""
    codex, claude, projects = 0, 0, set()
    for raw in queue_items:
        try:
            job = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(job, dict):
            continue
        role = job.get("role") or "builder"
        try:
            provider = role_provider(role)
        except ValueError:
            provider = "codex"
        if provider == "claude" and not claude_cooling:
            claude += 1
        else:
            codex += 1
        projects.add(job.get("project_id") or "laika")
    return codex + min(claude, max(0, free_claude)), len(projects)


# --- the decision (pure) ----------------------------------------------------------------------

def _since(memo, key, now):
    return now if memo.get(key) is None else memo[key]


def decide(cfg, s, memo, now):
    """One step. s: target, idle (idle workers), runnable, projects,
    memory_percent, memory_free_gb, load5, cpus, manual_until.
    memo carries timers between steps (updated in place).
    Returns (new_target, hold, reason) — reason is "" when nothing changed."""
    target, hold = s["target"], s.get("hold", False)
    memory, load = s["memory_percent"], s["load5"]

    # Pressure first, in every mode.
    if memory < cfg.critical_memory:
        hold = True
    elif memory >= cfg.pressure_memory:
        hold = False
    memory_low = memory < cfg.pressure_memory
    load_high = load > cfg.pressure_load * s["cpus"]
    memo["memory_low_since"] = _since(memo, "memory_low_since", now) if memory_low else None
    memo["load_high_since"] = _since(memo, "load_high_since", now) if load_high else None
    pressed = ((memory_low and now - memo["memory_low_since"] >= MEMORY_PRESSURE_FOR)
               or (load_high and now - memo["load_high_since"] >= LOAD_PRESSURE_FOR))
    if pressed and target > 1 and now - memo.get("last_drop", float("-inf")) >= PRESSURE_COOLDOWN:
        memo["last_drop"] = memo["last_change"] = now
        why = (f"memory low ({memory:.0f}% free)" if memory_low else f"load high ({load:.1f} on {s['cpus']} CPUs)")
        return target - 1, hold, f"draining a worker: {why}"
    if pressed or memory_low or load_high:
        memo["waiting_since"] = None  # never add while the server is under pressure
        return target, hold, ""

    calm = now - memo.get("last_drop", -CALM_AFTER_DROP) >= CALM_AFTER_DROP
    if not cfg.auto:
        wanted = max(1, min(cfg.fixed, UNIT_LIMIT))
        if wanted > target and not calm:
            return target, hold, ""
        return wanted, hold, (f"fixed count {wanted}" if wanted != target else "")
    if target > cfg.max:
        return cfg.max, hold, f"above the maximum ({cfg.max})"
    if target < cfg.min and calm:
        return cfg.min, hold, f"below the minimum ({cfg.min})"
    if now < s.get("manual_until", 0):
        return target, hold, ""

    shortage = s["runnable"] - s["idle"]
    if shortage > 0:
        memo["idle_since"] = None
        memo["waiting_since"] = _since(memo, "waiting_since", now)
        roomy = s["memory_free_gb"] >= 1.0 and memory >= cfg.pressure_memory + 10 and load < s["cpus"]
        if (now - memo["waiting_since"] >= cfg.up_wait and target < cfg.max and roomy and calm
                and now - memo.get("last_change", float("-inf")) >= ADD_COOLDOWN):
            memo["last_change"] = now
            where = f"{s['projects']} projects" if s["projects"] > 1 else "1 project"
            return target + 1, hold, (f"adding a worker: {s['runnable']} jobs ready to run in parallel ({where}), "
                                      f"{s['idle']} idle")
        return target, hold, ""
    memo["waiting_since"] = None
    if s["runnable"] == 0 and s["idle"] > 0 and target > cfg.min:
        memo["idle_since"] = _since(memo, "idle_since", now)
        if now - memo["idle_since"] >= cfg.down_idle:
            memo["idle_since"] = now - cfg.down_idle + IDLE_STEP
            memo["last_change"] = now
            return target - 1, hold, f"removing a worker: idle for {cfg.down_idle / 60:.0f} min"
    else:
        memo["idle_since"] = None
    return target, hold, ""


# --- the host -----------------------------------------------------------------------------------

def run(*argv):
    return subprocess.run(list(argv), capture_output=True, text=True)


def active_units():
    out = run(SYSTEMCTL, "list-units", "--plain", "--no-legend", "--state=active,activating,reloading",
              "laika-worker@*.service").stdout
    found = set()
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        try:
            found.add(int(name.split("@", 1)[1].split(".", 1)[0]))
        except (IndexError, ValueError):
            pass
    return found


def memory_info(path="/proc/meminfo"):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            name, value, *_ = line.split()
            values[name.rstrip(":")] = float(value)
    except (OSError, ValueError):
        return 100.0, 99.0, 0.0
    total = values.get("MemTotal") or 1
    available = values.get("MemAvailable", total)
    return 100.0 * available / total, available / 1048576, total / 1048576


def converge(r, target, active, runner=run, log=print):
    """Start workers 1..target; drain and stop the ones above it."""
    for n in range(1, UNIT_LIMIT + 1):
        if n <= target:
            if r.get(drain_key(n)):
                r.delete(drain_key(n))
            if n not in active:
                runner(SYSTEMCTL, "enable", "--now", unit(n))
                log(f"started {unit(n)}")
            continue
        if n not in active:
            if r.get(drain_key(n)):
                r.delete(drain_key(n))
            continue
        r.set(drain_key(n), "1")
        beat = r.hgetall(f"laika:workers:{worker_id(n)}") or {}
        if not beat or (beat.get("status") in ("draining", "disabled") and not beat.get("job_id")):
            runner(SYSTEMCTL, "disable", "--now", unit(n))
            r.delete(drain_key(n))
            log(f"stopped {unit(n)}")


def live_workers(r, active):
    idle = 0
    for n in active:
        beat = r.hgetall(f"laika:workers:{worker_id(n)}") or {}
        if beat.get("status") == "idle" and not beat.get("job_id"):
            idle += 1
    return idle


def free_claude_slots(r, now):
    import agent_cli
    try:
        used = r.zcount(agent_cli.SLOTS_KEY, now, "+inf")
    except Exception:
        used = 0
    return agent_cli.claude_slot_limit() - int(used), agent_cli.claude_cooling_down(r)


def stored_env(r):
    """Settings change while this runs: read them every step."""
    env = dict(os.environ)
    try:
        env.update(settings_schema.environment(r))
        # Worker settings apply "live" (no restart), so environment() skips them.
        values = settings_schema.values(r)
        env.update({f["key"]: values[f["key"]] for f in settings_schema.FIELDS if f["section"] == "workers"})
    except Exception:
        pass
    return env


def record(r, target, reason, now):
    r.lpush(LOG_KEY, json.dumps({"at": now, "target": target, "reason": reason}))
    r.ltrim(LOG_KEY, 0, 99)


def step(r, memo, now=None, cpus=None, memory=None, load=None, runner=run, units=None):
    import agent_cli
    now = time.time() if now is None else now
    cpus = cpus or os.cpu_count() or 1
    percent, free_gb, total_gb = memory or memory_info()
    load5 = (load if load is not None else os.getloadavg()[1])
    env = stored_env(r)
    for key in ("CLAUDE_MAX_CONCURRENT", "ROLE_PROVIDERS"):
        if key in env:
            os.environ[key] = env[key]
    cfg = Config(env, cpus, total_gb)
    active = units if units is not None else active_units()
    try:
        target = int(r.get(TARGET_KEY) or 0)
    except ValueError:
        target = 0
    if target <= 0:  # first run: keep what is running now
        target = max(1, min(len(active) or cfg.fixed, cfg.max if cfg.auto else UNIT_LIMIT))
        memo.setdefault("last_change", now)
        r.set(TARGET_KEY, target)
    free, cooling = free_claude_slots(r, now)
    runnable, projects = ready_work(r.lrange(QUEUE, 0, -1), agent_cli.role_provider, free, cooling)
    state = {"target": target, "idle": live_workers(r, active), "runnable": runnable, "projects": projects,
             "memory_percent": percent, "memory_free_gb": free_gb, "load5": load5, "cpus": cpus,
             "manual_until": float(r.get(MANUAL_KEY) or 0), "hold": bool(r.get(HOLD_KEY))}
    new_target, hold, reason = decide(cfg, state, memo, now)
    if new_target != target:
        r.set(TARGET_KEY, new_target)
        record(r, new_target, reason, now)
    if hold:
        if not state["hold"]:
            record(r, new_target, f"holding new work: memory critical ({percent:.0f}% free)", now)
        r.set(HOLD_KEY, f"memory critical ({percent:.0f}% free)", ex=90)
    elif state["hold"]:
        r.delete(HOLD_KEY)
        record(r, new_target, "new work allowed again", now)
    converge(r, new_target, active, runner)
    r.set(STATE_KEY, json.dumps({
        "target": new_target, "running": len(active), "auto": cfg.auto, "min": cfg.min, "max": cfg.max,
        "fixed": cfg.fixed, "idle": state["idle"], "runnable": runnable, "projects": projects,
        "memory_percent": round(percent, 1), "load5": round(load5, 2), "cpus": cpus, "hold": hold,
        "manual_until": state["manual_until"], "checked_at": now}), ex=60)
    return new_target, reason


def redis_client():
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def main():
    r = redis_client()
    memo = {}
    print("LAIka scaler starting", flush=True)
    while True:
        try:
            target, reason = step(r, memo)
            if reason:
                print(f"[laika-scaler] target {target}: {reason}", flush=True)
        except Exception as exc:
            print(f"[laika-scaler] error: {exc}", flush=True)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
