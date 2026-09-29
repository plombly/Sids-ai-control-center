#!/usr/bin/env python3

import json
import os
import subprocess
import time

try:
    import redis
except ImportError:
    print("Missing dependency: redis")
    print("Install with: python3 -m pip install redis")
    raise SystemExit(1)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
QUEUE = os.getenv("WORKER_QUEUE", "sid:jobs")

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def clear():
    print("\033[2J\033[H", end="")


def git_info():
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

        return branch or "(detached)", "dirty" if status else "clean"
    except Exception:
        return "unknown", "unknown"


def workers():
    result = []

    for key in sorted(r.scan_iter("sid:workers:*")):
        data = r.hgetall(key)

        try:
            age = int(time.time() - float(data.get("last_seen", 0)))
        except Exception:
            age = -1

        result.append(
            (
                data.get("id", key),
                data.get("status", "unknown"),
                age,
            )
        )

    return result


def recent_jobs():
    result = []

    for key in sorted(r.scan_iter("sid:jobs:*")):
        data = r.hgetall(key)

        if data:
            result.append(
                (
                    key.split(":")[-1],
                    data.get("status", "?"),
                    data.get("provider", "?"),
                    data.get("worker_id", "-"),
                )
            )

    return result[-8:]


def draw():
    branch, state = git_info()

    clear()

    print("=" * 72)
    print(" SID'S AI COMMAND CENTER")
    print("=" * 72)

    print()
    print(f" Repository : {branch} ({state})")
    print(f" Queue      : {r.llen(QUEUE)} waiting")
    print()

    print(" WORKERS")
    print("-" * 72)

    ws = workers()

    if not ws:
        print(" No workers online")
    else:
        for worker_id, status, age in ws:
            freshness = f"{age}s ago" if age >= 0 else "unknown"
            print(f" {worker_id:<35} {status:<12} heartbeat {freshness}")

    print()
    print(" RECENT JOBS")
    print("-" * 72)

    jobs = recent_jobs()

    if not jobs:
        print(" No jobs yet")
    else:
        for job_id, status, provider, worker in jobs:
            print(
                f" {job_id:<12} "
                f"{status:<12} "
                f"{provider:<10} "
                f"{worker}"
            )

    print()
    print("-" * 72)
    print(" Ctrl-C to exit")


def main():
    while True:
        try:
            draw()
            time.sleep(1)
        except KeyboardInterrupt:
            print()
            break
        except redis.RedisError as exc:
            clear()
            print("SID'S AI COMMAND CENTER")
            print()
            print(f"Redis error: {exc}")
            time.sleep(2)


if __name__ == "__main__":
    main()
