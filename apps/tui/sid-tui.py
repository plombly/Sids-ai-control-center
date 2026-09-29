#!/usr/bin/env python3

import os
import shutil
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

        result.append(
            {
                "id": data.get("id") or key.rsplit(":", 1)[-1],
                "role": data.get("role", "-"),
                "provider": data.get("provider", "-"),
                "model": data.get("model", "-"),
                "status": data.get("status", "unknown"),
                "heartbeat_age": heartbeat_age(data.get("last_seen")),
            }
        )

    return result


def recent_jobs():
    result = []

    for key in sorted(r.scan_iter("sid:jobs:*")):
        data = r.hgetall(key)

        if data:
            result.append(
                {
                    "id": key.rsplit(":", 1)[-1],
                    "status": data.get("status", "?"),
                    "worker": data.get("worker_id", "-"),
                    "model": data.get("model", "-"),
                    "tokens": data.get("tokens", "-"),
                    "duration": data.get("duration_seconds", "-"),
                    "branch": data.get("branch", "-"),
                    "sort_time": timestamp(data.get("updated_at") or data.get("created_at")),
                }
            )

    result.sort(key=lambda job: job["sort_time"], reverse=True)
    return result[:8]


def timestamp(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def heartbeat_age(value):
    seen_at = timestamp(value)
    if not seen_at:
        return -1

    return max(int(time.time() - seen_at), 0)


def clip(value, width):
    value = str(value)
    if len(value) <= width:
        return value
    if width < 2:
        return value[:width]
    return value[: width - 1] + "…"


def row(values, widths):
    return " " + "  ".join(clip(value, width).ljust(width) for value, width in zip(values, widths))


def rule():
    return "-" * min(shutil.get_terminal_size((110, 24)).columns, 120)


def table(title, columns, rows, empty_message):
    widths = [
        max(len(column), *(len(str(values[index])) for values in rows))
        for index, column in enumerate(columns)
    ]

    print(f" {title}")
    print(rule())

    if not rows:
        print(f" {empty_message}")
        return

    print(row(columns, widths))
    print(row(["-" * width for width in widths], widths))
    for values in rows:
        print(row(values, widths))


def format_duration(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "-"

    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def format_tokens(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


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

    ws = workers()
    worker_rows = [
        (
            worker["id"],
            worker["role"],
            worker["provider"],
            worker["model"],
            worker["status"],
            f"{worker['heartbeat_age']}s ago" if worker["heartbeat_age"] >= 0 else "unknown",
        )
        for worker in ws
    ]
    table(
        "WORKERS",
        ("WORKER ID", "ROLE", "PROVIDER", "MODEL", "STATUS", "HEARTBEAT"),
        worker_rows,
        "No workers online",
    )

    print()
    jobs = recent_jobs()
    job_rows = [
        (
            job["id"],
            job["status"],
            job["worker"],
            job["model"],
            format_tokens(job["tokens"]),
            format_duration(job["duration"]),
            job["branch"],
        )
        for job in jobs
    ]
    table(
        "RECENT JOBS",
        ("JOB ID", "STATUS", "WORKER", "MODEL", "TOKENS", "DURATION", "GIT BRANCH"),
        job_rows,
        "No jobs yet",
    )

    print()
    print(rule())
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
