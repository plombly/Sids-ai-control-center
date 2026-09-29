#!/usr/bin/env python3

import json
import math
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
REVIEW_SCRIPT = "scripts/job-review.py"

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


def _text(value, fallback="-"):
    """Return a printable, non-empty value from a possibly bad hash field."""
    if value is None:
        return fallback
    value = str(value).strip()
    return value or fallback


def _key_text(value):
    """Return a deterministic printable representation of a Redis key."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return _text(value)


def _key_suffix(value):
    key = _key_text(value)
    return key.rsplit(":", 1)[-1] or "-"


def _json_list(value):
    if isinstance(value, (list, tuple)):
        values = value
    else:
        try:
            values = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(values, (list, tuple)):
        return []
    ids = {
        str(item).strip()
        for item in values
        if isinstance(item, (str, int, float)) and str(item).strip()
    }
    return sorted(ids)


def normalize_orchestrator(key, data):
    """Normalize one orchestrator hash without making assumptions about it."""
    data = data if isinstance(data, dict) else {}
    return {
        "id": _text(data.get("id"), _key_suffix(key)),
        "status": _text(data.get("status"), "unknown"),
        "model": _text(data.get("model")),
        "active_goal": _text(data.get("goal_id") or data.get("active_goal")),
        "heartbeat_age": heartbeat_age(data.get("last_seen")),
    }


def orchestrators():
    """Read-only, deterministically ordered orchestrator records."""
    result = []
    for key in sorted(r.scan_iter("sid:orchestrators:*"), key=_key_text):
        try:
            data = r.hgetall(key)
        except Exception:
            data = {}
        result.append(normalize_orchestrator(key, data))
    return sorted(result, key=lambda item: (item["id"], item["status"], item["model"]))


def normalize_goal(goal_id, data, job_statuses=None):
    """Normalize a goal and derive progress solely from its child job statuses."""
    data = data if isinstance(data, dict) else {}
    child_ids = _json_list(data.get("jobs") or data.get("job_ids"))
    statuses = {
        job_id: _text((job_statuses or {}).get(job_id), "unknown")
        for job_id in child_ids
    }
    status_counts = {}
    for status in statuses.values():
        status_counts[status] = status_counts.get(status, 0) + 1
    status_counts = dict(sorted(status_counts.items()))
    complete_statuses = {"completed", "completed_no_changes", "merged", "done", "succeeded"}
    progress = {
        "total": len(child_ids),
        "completed": sum(status in complete_statuses for status in statuses.values()),
        "status_counts": status_counts,
    }
    return {
        "id": _text(data.get("id"), goal_id),
        "status": _text(data.get("status"), "unknown"),
        "summary": _text(data.get("summary") or data.get("text") or data.get("goal")),
        "created_at": _text(data.get("created_at")),
        "updated_at": _text(data.get("updated_at")),
        "child_job_ids": child_ids,
        "child_job_progress": progress,
        "sort_time": timestamp(data.get("updated_at") or data.get("created_at")),
    }


def recent_goals(limit=8):
    """Read a bounded, newest-first view of goals and their child jobs."""
    result = []
    for key in sorted(r.scan_iter("sid:goals:*"), key=_key_text):
        goal_id = _key_suffix(key)
        try:
            data = r.hgetall(key)
        except Exception:
            data = {}
        data = data if isinstance(data, dict) else {}
        child_ids = _json_list(data.get("jobs") or data.get("job_ids"))
        statuses = {}
        for job_id in child_ids:
            try:
                job = r.hgetall(f"sid:jobs:{job_id}")
            except Exception:
                job = {}
            statuses[job_id] = job.get("status") if isinstance(job, dict) else None
        result.append(normalize_goal(goal_id, data, statuses))
    result.sort(key=lambda item: (-item["sort_time"], item["id"]))
    return result[:max(0, int(limit))]


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
                    "role": data.get("job_role") or data.get("role", "builder"),
                    "model": data.get("model", "-"),
                    "review_status": data.get("review_status", ""),
                    "review_verdict": data.get("review_verdict", ""),
                    "review_job_id": data.get("review_job_id", ""),
                    "input_tokens": data.get("input_tokens", "-"),
                    "cached_input_tokens": data.get("cached_input_tokens", "-"),
                    "output_tokens": data.get("output_tokens", "-"),
                    # Newer records may provide total_tokens; older records
                    # store the same value under tokens.
                    "total_tokens": data.get("total_tokens") or data.get(
                        "tokens", "-"
                    ),
                    "duration": data.get("duration_seconds", "-"),
                    "branch": data.get("branch", "-"),
                    "sort_time": timestamp(
                        data.get("updated_at") or data.get("created_at")
                    ),
                }
            )

    result.sort(key=lambda job: job["sort_time"], reverse=True)
    return result[:8]


def timestamp(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def format_timestamp(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(parsed):
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(parsed))
    except (OverflowError, OSError, ValueError, TypeError):
        return "-"


def heartbeat_age(value):
    seen_at = timestamp(value)
    if not seen_at:
        return -1

    return max(int(time.time() - seen_at), 0)


def format_heartbeat_age(value):
    age = value if isinstance(value, (int, float)) else heartbeat_age(value)
    return f"{int(age)}s ago" if age >= 0 else "unknown"


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
        max([len(column)] + [len(str(values[index])) for values in rows])
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


def format_goal_progress(progress):
    """Keep child completion and the status mix visible in one compact cell."""
    completed = progress.get("completed", 0)
    total = progress.get("total", 0)
    status_counts = progress.get("status_counts", {})
    breakdown = ",".join(
        f"{status}:{count}" for status, count in sorted(status_counts.items())
    )
    return f"{completed}/{total}" + (f" ({breakdown})" if breakdown else "")


def review_display(job):
    if job["role"] == "reviewer":
        verdict = job["review_verdict"]
        if verdict == "pass":
            return "PASS"
        if verdict == "changes_required":
            return "CHANGES_REQUIRED"
        if job["status"] == "review_complete":
            return "UNKNOWN"
        return "-"

    verdict = job["review_verdict"]
    if verdict == "pass":
        return "PASS"
    if verdict == "changes_required":
        return "CHANGES_REQUIRED"
    if job["review_status"] == "complete":
        return "UNKNOWN"
    if job["review_status"] in {"queued", "reviewing"}:
        return "PENDING"
    if job["review_status"] in {"failed", "queue_failed"}:
        return "FAILED"
    if job["status"] == "awaiting_review":
        return "NEEDED"
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
    orch_rows = [
        (
            item["id"],
            item["status"],
            item["model"],
            item["active_goal"],
            format_heartbeat_age(item["heartbeat_age"]),
        )
        for item in orchestrators()
    ]
    table(
        "ORCHESTRATOR",
        ("ORCHESTRATOR ID", "STATUS", "MODEL", "ACTIVE GOAL", "HEARTBEAT"),
        orch_rows,
        "No orchestrators online",
    )

    print()
    goal_rows = []
    for goal in recent_goals():
        progress = goal["child_job_progress"]
        goal_rows.append(
            (
                goal["id"],
                goal["status"],
                goal["summary"],
                format_timestamp(goal["created_at"]),
                format_timestamp(goal["updated_at"]),
                format_goal_progress(progress),
                ",".join(goal["child_job_ids"]) or "-",
            )
        )
    table(
        "RECENT GOALS",
        (
            "GOAL ID",
            "STATUS",
            "SUMMARY",
            "CREATED",
            "UPDATED",
            "DONE",
            "CHILD JOBS",
        ),
        goal_rows,
        "No goals yet",
    )

    print()
    jobs = recent_jobs()
    job_rows = [
        (
            job["id"],
            job["status"],
            job["role"],
            review_display(job),
            job["worker"],
            job["model"],
            format_tokens(job["total_tokens"]),
            format_duration(job["duration"]),
        )
        for job in jobs
    ]
    table(
        "RECENT JOBS",
        (
            "JOB ID",
            "STATUS",
            "ROLE",
            "REVIEW",
            "WORKER",
            "MODEL",
            "TOKENS",
            "DURATION",
        ),
        job_rows,
        "No jobs yet",
    )

    review_jobs = [
        job
        for job in jobs
        if job["status"] == "awaiting_review"
    ]

    needs_reviewer = [
        job for job in review_jobs
        if job["review_verdict"] not in {"pass", "changes_required"}
        and job["review_status"] not in {"queued", "reviewing"}
    ]

    ready_for_human = [
        job for job in review_jobs
        if job["review_verdict"] == "pass"
    ]

    changes_required = [
        job for job in review_jobs
        if job["review_verdict"] == "changes_required"
    ]

    if needs_reviewer:
        print()
        print("AGENT REVIEW REQUIRED")
        print(rule())
        for job in needs_reviewer:
            job_id = job["id"]
            print(f" Job {job_id} needs independent review.")
            print(f" Review : ./scripts/submit-review.py {job_id}")

    if ready_for_human:
        print()
        print("READY FOR HUMAN APPROVAL")
        print(rule())
        for job in ready_for_human:
            job_id = job["id"]
            print(f" Job {job_id} passed independent review.")
            print(f" Approve: python3 {REVIEW_SCRIPT} approve {job_id}")
            print(f" Reject : python3 {REVIEW_SCRIPT} reject {job_id}")

    if changes_required:
        print()
        print("CHANGES REQUIRED")
        print(rule())
        for job in changes_required:
            job_id = job["id"]
            print(f" Job {job_id} did not pass independent review.")
            print(f" Review job: {job['review_job_id'] or '-'}")
            print(f" Reject    : python3 {REVIEW_SCRIPT} reject {job_id}")

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
