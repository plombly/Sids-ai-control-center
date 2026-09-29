#!/usr/bin/env python3

import json
import math
import os
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager

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

    jobs = []
    for job_key in sorted(r.scan_iter("sid:jobs:*"), key=_key_text):
        try:
            job = r.hgetall(job_key)
        except Exception:
            job = {}
        if isinstance(job, dict) and job:
            jobs.append((job_key, job))

    for key in sorted(r.scan_iter("sid:workers:*"), key=_key_text):
        try:
            data = r.hgetall(key)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        worker_id = _text(_first(data, "id", "worker_id", fallback=None), _key_suffix(key))
        matching = [
            (job_key, job)
            for job_key, job in jobs
            if _text(_first(job, "worker_id", "worker", fallback=None)) == worker_id
        ]
        active = [
            item for item in matching
            if _text(item[1].get("status"), "unknown")
            not in {"completed", "completed_no_changes", "merged", "done", "succeeded", "failed", "error"}
        ]
        candidates = active or matching
        job = max(
            candidates,
            key=lambda item: (timestamp(_first(item[1], "updated_at", "created_at", fallback=None)), _key_text(item[0])),
            default=None,
        )
        result.append(normalize_worker(key, data, job[1] if job else None, job[0] if job else None))

    return sorted(result, key=lambda item: item["id"])


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


def _number(value, fallback="-"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return int(number) if number.is_integer() else number


def _first(data, *names, fallback="-"):
    for name in names:
        value = data.get(name)
        if value is not None and str(value).strip():
            return value
    return fallback


def _derived_effective_tokens(data):
    explicit = _first(data, "effective_tokens", fallback=None)
    if explicit is not None:
        return _number(explicit)
    uncached = _number(data.get("uncached_input_tokens"), None)
    output = _number(data.get("output_tokens"), None)
    if uncached is None:
        input_tokens = _number(data.get("input_tokens"), None)
        cached = _number(data.get("cached_input_tokens"), 0)
        if input_tokens is not None and cached is not None:
            uncached = max(input_tokens - min(cached, input_tokens), 0)
    if uncached is not None and output is not None:
        return uncached + output
    return "-"


def _duration(data):
    direct = _first(data, "duration_seconds", "duration", fallback=None)
    if direct is not None:
        return _number(direct)
    started = _number(_first(data, "started_at", "start_time", fallback=None), None)
    ended = _number(_first(data, "ended_at", "completed_at", "finished_at", fallback=None), None)
    if started is not None and ended is not None and ended >= started:
        return ended - started
    return "-"


def normalize_worker(key, data, job=None, job_key=None):
    """Normalize a worker hash, retaining explicit unknowns for absent fields."""
    data = data if isinstance(data, dict) else {}
    job = job if isinstance(job, dict) else {}
    job_id = _text(
        _first(job, "id", "job_id", fallback=None),
        _key_suffix(job_key) if job_key else "-",
    ) if job_key else None
    merged = {**data, **job}
    return {
        "id": _text(_first(data, "id", "worker_id", fallback=None), _key_suffix(key)),
        "role": _text(_first(data, "role", "job_role", fallback=None), _text(_first(job, "job_role", "role"))),
        "provider": _text(data.get("provider"), _text(job.get("provider"))),
        "job_id": _text(_first(data, "job_id", "current_job_id", "active_job_id", fallback=None), job_id or "-"),
        "model": _text(data.get("model"), _text(job.get("model"))),
        "status": _text(data.get("status"), _text(job.get("status"), "unknown")),
        "heartbeat_age": heartbeat_age(_first(data, "last_seen", "heartbeat", "last_heartbeat", fallback=None)),
        "effective_tokens": _derived_effective_tokens(merged),
        "cached_input_tokens": _number(_first(merged, "cached_input_tokens", "cached_tokens", fallback=None)),
        "command_count": _number(merged.get("command_count")),
        "duration": _duration(merged),
    }


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
    active_goal = _text(data.get("goal_id") or data.get("active_goal"))
    status = _text(data.get("status"), "unknown")
    if active_goal != "-" and status == "idle":
        status = "active"
    return {
        "id": _text(data.get("id"), _key_suffix(key)),
        "status": status,
        "model": _text(data.get("model")),
        "active_goal": active_goal,
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


def goal_by_id(goal_id):
    """Read one explicitly referenced goal, including its child-job progress."""
    goal_id = _text(goal_id)
    if goal_id == "-":
        return None
    try:
        data = r.hgetall(f"sid:goals:{goal_id}")
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        return None
    child_ids = _json_list(data.get("jobs") or data.get("job_ids"))
    statuses = {}
    for job_id in child_ids:
        try:
            job = r.hgetall(f"sid:jobs:{job_id}")
        except Exception:
            job = {}
        statuses[job_id] = job.get("status") if isinstance(job, dict) else None
    return normalize_goal(goal_id, data, statuses)


def recent_jobs(limit=8):
    result = []

    for key in sorted(r.scan_iter("sid:jobs:*"), key=_key_text):
        try:
            data = r.hgetall(key)
        except Exception:
            data = {}
        if data:
            result.append(normalize_job(key, data))

    result.sort(key=lambda job: (-job["sort_time"], job["id"]))
    return result if limit is None else result[:max(0, int(limit))]


def normalize_job(key, data):
    """Normalize a job hash without changing its workflow meaning."""
    data = data if isinstance(data, dict) else {}
    job_id = _key_suffix(key)
    return {
        "id": _text(data.get("id"), job_id),
        "status": _text(data.get("status"), "unknown"),
        "worker": _text(_first(data, "worker_id", "worker")),
        "role": _text(_first(data, "job_role", "role")),
        "model": _text(data.get("model")),
        "provider": _text(data.get("provider")),
        "review_status": _text(data.get("review_status"), ""),
        "review_verdict": _text(data.get("review_verdict"), ""),
        "review_job_id": _text(data.get("review_job_id"), ""),
        "input_tokens": _number(data.get("input_tokens")),
        "cached_input_tokens": _number(_first(data, "cached_input_tokens", "cached_tokens", fallback=None)),
        "output_tokens": _number(data.get("output_tokens")),
        "uncached_input_tokens": _number(data.get("uncached_input_tokens")),
        "effective_tokens": _derived_effective_tokens(data),
        "command_count": _number(data.get("command_count")),
        "total_tokens": _number(_first(data, "total_tokens", "tokens", fallback=None)),
        "duration": _duration(data),
        "branch": _text(data.get("branch")),
        "error": _text(_first(data, "error", "failure", "failure_reason", fallback=None)),
        "sort_time": timestamp(_first(data, "updated_at", "created_at", fallback=None)),
    }


FAILURE_STATUSES = {"failed", "error", "integration_failed", "queue_failed"}


def failures(limit=8):
    """Return failed jobs in the same deterministic shape as recent jobs."""
    return [job for job in recent_jobs(limit=None) if job["status"] in FAILURE_STATUSES][:max(0, int(limit))]


def pending_human_approvals(limit=8):
    """Identify only jobs that have passed review and await the human gate."""
    jobs = recent_jobs(limit=None)
    approvals = [
        job for job in jobs
        if job["status"] == "awaiting_review"
        and job["review_status"] == "complete"
        and job["review_verdict"] == "pass"
    ]
    return approvals[:max(0, int(limit))]


def snapshot(limit=8):
    """Build one bounded, read-only view of the TUI's Redis-backed state."""
    branch, state = git_info()
    goals = recent_goals(limit)
    orch = orchestrators()
    jobs = recent_jobs()
    active_goals = [goal for goal in goals if goal["status"] in {"active", "running", "in_progress"}]
    active_goal_ids = {item["active_goal"] for item in orch if item["active_goal"] != "-"}
    active_goals.sort(key=lambda goal: (goal["id"] not in active_goal_ids, -goal["sort_time"], goal["id"]))
    explicit_goal = next((goal for goal in goals if goal["id"] in active_goal_ids), None)
    if explicit_goal is None:
        for goal_id in sorted(active_goal_ids):
            explicit_goal = goal_by_id(goal_id)
            if explicit_goal is not None:
                break
    try:
        queue_depth = r.llen(QUEUE)
    except Exception:
        queue_depth = None
    return {
        "repository": {"branch": _text(branch), "status": _text(state, "unknown")},
        "queue": {"name": QUEUE, "waiting": queue_depth if queue_depth is not None else "-"},
        "orchestrators": orch,
        "active_goal": explicit_goal or (active_goals[0] if active_goals else None),
        "workers": workers(),
        "goals": {"active": active_goals, "recent": goals},
        "jobs": {"recent": jobs, "failures": failures(limit)},
        "pending_human_approvals": pending_human_approvals(limit),
    }


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


def goal_summary(prompt, width=36):
    """Return one deterministic, width-bounded line for a goal prompt."""
    return clip(_text(prompt).replace("\r", " ").replace("\n", " "), width)


def row(values, widths):
    rendered = " " + "  ".join(
        clip(value, width).ljust(width)
        for value, width in zip(values, widths)
    )
    terminal_width = max(1, min(shutil.get_terminal_size((110, 24)).columns, 120))
    return clip(rendered, terminal_width)


def rule():
    return "-" * min(shutil.get_terminal_size((110, 24)).columns, 120)


def bounded_print(value=""):
    width = min(shutil.get_terminal_size((110, 24)).columns, 120)
    print(clip(value, width - 1))


def table(title, columns, rows, empty_message):
    widths = [
        max([len(column)] + [len(str(values[index])) for values in rows])
        for index, column in enumerate(columns)
    ]
    available = max(1, len(rule()) - 1 - 2 * (len(columns) - 1))
    while sum(widths) > available:
        widest = max(range(len(widths)), key=lambda index: widths[index])
        if widths[widest] <= 1:
            break
        widths[widest] -= 1

    bounded_print(f" {title}")
    print(rule())

    if not rows:
        bounded_print(f" {empty_message}")
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

    print(rule())
    bounded_print(" SID'S AI COMMAND CENTER")
    print(rule())

    print()
    bounded_print(f" Repository : {branch} ({state})")
    bounded_print(f" Queue      : {r.llen(QUEUE)} waiting")
    print()

    goals = recent_goals()
    orch = orchestrators()
    active_goal = next((item["active_goal"] for item in orch if item["active_goal"] != "-"), "-")
    active_record = next((goal for goal in goals if goal["id"] == active_goal), None)
    if active_record is None:
        active_record = next(
            (goal for goal in goals if goal["status"] in {"active", "running", "in_progress"}),
            None,
        )
        if active_record is not None:
            active_goal = active_record["id"]
    active_label = active_goal
    if active_record is not None:
        active_label = f"{active_goal} — {goal_summary(active_record['summary'], 42)}"
    bounded_print(f" Active goal: {active_label}")
    table(
        "ORCHESTRATOR",
        ("ID", "STATUS", "MODEL", "ACTIVE GOAL", "HEARTBEAT"),
        [
            (item["id"], item["status"], item["model"], item["active_goal"], format_heartbeat_age(item["heartbeat_age"]))
            for item in orch
        ],
        "No orchestrators online",
    )

    ws = workers()
    worker_rows = [
        (
            worker["id"],
            worker["role"],
            worker["job_id"],
            worker["status"],
            format_tokens(worker["effective_tokens"]),
        )
        for worker in ws
    ]
    table(
        "WORKERS",
        ("WORKER", "ROLE", "JOB", "STATUS", "TOKENS"),
        worker_rows,
        "No workers online",
    )

    print()
    goal_rows = []
    for goal in goals:
        progress = goal["child_job_progress"]
        goal_rows.append(
            (
                goal["id"],
                goal["status"],
                goal_summary(goal["summary"]),
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
            "DONE",
            "CHILD JOBS",
        ),
        goal_rows,
        "No goals yet",
    )
    for goal in goals:
        bounded_print(
            f" Goal {goal['id']} created {format_timestamp(goal['created_at'])} "
            f"updated {format_timestamp(goal['updated_at'])}"
        )
        bounded_print(f" Goal {goal['id']} progress {format_goal_progress(goal['child_job_progress'])}")

    print()
    jobs = recent_jobs()
    job_rows = [
        (
            job["id"],
            job["status"],
            job["role"],
            review_display(job),
            job["worker"],
            format_tokens(job["effective_tokens"]),
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
            "EFFECTIVE",
            "DURATION",
        ),
        job_rows,
        "No jobs yet",
    )

    # Operational actions must not disappear merely because a job falls
    # outside the compact recent-jobs view.
    review_jobs = [
        job
        for job in recent_jobs(limit=None)
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
        bounded_print(" ACTION REQUIRED: AGENT REVIEW REQUIRED")
        print(rule())
        for job in needs_reviewer:
            job_id = job["id"]
            bounded_print(f" Job {job_id} needs independent review.")
            bounded_print(f" Review : ./scripts/submit-review.py {job_id}")

    if ready_for_human:
        print()
        bounded_print(" ACTION REQUIRED: READY FOR HUMAN APPROVAL")
        print(rule())
        for job in ready_for_human:
            job_id = job["id"]
            bounded_print(f" Job {job_id} passed independent review.")
            bounded_print(f" Approve: python3 {REVIEW_SCRIPT} approve {job_id}")
            bounded_print(f" Reject : python3 {REVIEW_SCRIPT} reject {job_id}")

    if changes_required:
        print()
        bounded_print(" ACTION REQUIRED: CHANGES REQUIRED")
        print(rule())
        for job in changes_required:
            job_id = job["id"]
            bounded_print(f" Job {job_id} did not pass independent review.")
            bounded_print(f" Review job: {job['review_job_id'] or '-'}")
            bounded_print(f" Reject    : python3 {REVIEW_SCRIPT} reject {job_id}")

    print()
    print(rule())
    bounded_print(" q to exit, r to refresh, Ctrl-C to exit")


@contextmanager
def _terminal_mode():
    """Put an interactive terminal in cbreak mode and always restore it."""
    if not sys.stdin.isatty():
        yield
        return

    settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        yield
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)


def _wait_for_key(timeout):
    """Return one pending control key, or None when the refresh interval ends."""
    readable, _, _ = select.select([sys.stdin], [], [], timeout)
    if not readable:
        return None
    key = sys.stdin.read(1)
    return key.lower() if key else "q"


def main():
    try:
        with _terminal_mode():
            while True:
                try:
                    draw()
                except redis.RedisError as exc:
                    clear()
                    bounded_print("SID'S AI COMMAND CENTER")
                    print()
                    bounded_print(f"Redis error: {exc}")

                try:
                    key = _wait_for_key(2)
                except KeyboardInterrupt:
                    break
                if key == "q":
                    break
    except KeyboardInterrupt:
        pass
    finally:
        print()


if __name__ == "__main__":
    main()
