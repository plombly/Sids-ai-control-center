import json
import io
import math
import os
import re
import subprocess
import time
import uuid
import zipfile

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from redis import Redis
import os

from database import init_database
from schemas import GoalAccepted, GoalSubmit, OperatorActionRequest, PromptSubmit, WorkerAction

app = FastAPI(
    title="SID's AI Command Center",
    version="0.1.0"
)

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

engine = create_engine(DATABASE_URL)
redis = Redis.from_url(REDIS_URL, decode_responses=True)

API_DEFAULT_LIMIT = 8
API_MAX_LIMIT = 100
GOAL_SUMMARY_LIMIT = 240
JOB_TERMINAL_STATUSES = {
    "completed", "completed_no_changes", "merged", "done", "succeeded",
}
FAILURE_STATUSES = {"failed", "error", "integration_failed", "queue_failed", "test_failed", "rejected", "repair_exhausted", "blocked_failed_dependency", "planning_failed"}


def _text(value, default=None):
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return int(number) if number.is_integer() else number


def _timestamp(value):
    return _number(value, 0) or 0


def _json_list(value):
    if isinstance(value, (list, tuple)):
        values = value
    else:
        try:
            values = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return sorted({str(item).strip() for item in values
                   if isinstance(item, (str, int, float)) and str(item).strip()}) \
        if isinstance(values, (list, tuple)) else []


def _bounded_summary(value):
    value = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(value) <= GOAL_SUMMARY_LIMIT:
        return value
    return value[:GOAL_SUMMARY_LIMIT - 1] + "…"


def _effective_tokens(data):
    explicit = _number(data.get("effective_tokens"))
    if explicit is not None:
        return explicit
    uncached = _number(data.get("uncached_input_tokens"))
    output = _number(data.get("output_tokens"))
    if uncached is None:
        inputs = _number(data.get("input_tokens"))
        cached = _number(data.get("cached_input_tokens"), 0)
        if inputs is not None and cached is not None:
            uncached = max(inputs - min(cached, inputs), 0)
    return uncached + output if uncached is not None and output is not None else None


def _duration(data):
    direct = _number(data.get("duration_seconds", data.get("duration")))
    if direct is not None:
        return direct
    started = _number(data.get("started_at", data.get("start_time")))
    ended = _number(data.get("ended_at", data.get("completed_at", data.get("finished_at"))))
    return ended - started if started is not None and ended is not None and ended >= started else None


def _key_suffix(key):
    return str(key).rsplit(":", 1)[-1]


def _hash(key):
    try:
        value = redis.hgetall(key)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _keys(pattern):
    try:
        return sorted(redis.scan_iter(pattern), key=str)
    except Exception:
        return []


def _limit(value):
    return max(0, min(int(value), API_MAX_LIMIT))


def _page(items, offset, limit):
    return items[offset:offset + _limit(limit)]


def _job(key, data):
    data = data if isinstance(data, dict) else {}
    return {
        "id": _text(data.get("id"), _key_suffix(key)),
        "goal_id": _text(data.get("goal_id")),
        "status": _text(data.get("status"), "unknown"),
        "role": _text(data.get("job_role", data.get("role"))),
        "worker": _text(data.get("worker_id", data.get("worker"))),
        "provider": _text(data.get("provider")),
        "model": _text(data.get("model")),
        "review_status": _text(data.get("review_status")),
        "review_verdict": _text(data.get("review_verdict")),
        "review_job_id": _text(data.get("review_job_id")),
        "integration_status": _text(data.get("integration_status")),
        "integration_base_commit": _text(data.get("integration_base_commit")),
        "integrated_candidate_commit": _text(data.get("integrated_candidate_commit")),
        "reviewed_commit": _text(data.get("reviewed_commit")),
        "integration_worktree": _text(data.get("integration_worktree")),
        "integration_branch": _text(data.get("integration_branch")),
        "input_tokens": _number(data.get("input_tokens")),
        "effective_tokens": _effective_tokens(data),
        "cached_input_tokens": _number(data.get("cached_input_tokens", data.get("cached_tokens"))),
        "output_tokens": _number(data.get("output_tokens")),
        "uncached_input_tokens": _number(data.get("uncached_input_tokens")),
        "command_count": _number(data.get("command_count")),
        "total_tokens": _number(data.get("total_tokens", data.get("tokens"))),
        "duration": _duration(data),
        "branch": _text(data.get("branch")),
        "base": _text(data.get("integration_base_commit", data.get("base", data.get("base_commit")))),
        "candidate": _text(data.get("integrated_candidate_commit", data.get("candidate", data.get("candidate_commit")))),
        "files": _number(data.get("files_changed", data.get("file_count"))),
        "tests": _text(data.get("test_status", data.get("tests"))),
        "error": _text(data.get("error", data.get("failure", data.get("failure_reason")))),
        "sort_time": _timestamp(data.get("updated_at", data.get("created_at"))),
    }


def _all_jobs():
    result = []
    for key in _keys("sid:jobs:*"):
        data = _hash(key)
        if data:
            result.append(_job(key, data))
    return sorted(result, key=lambda item: (-item["sort_time"], item["id"]))


def _goal(key, data):
    data = data if isinstance(data, dict) else {}
    child_ids = _json_list(data.get("jobs", data.get("job_ids")))
    counts = {}
    for child_id in child_ids:
        status = _text(_hash(f"sid:jobs:{child_id}").get("status"), "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts = dict(sorted(counts.items()))
    return {
        "id": _text(data.get("id"), _key_suffix(key)),
        "status": _text(data.get("status"), "unknown"),
        "summary": _bounded_summary(data.get("summary", data.get("text", data.get("goal")))),
        "prompt": _bounded_summary(data.get("goal", data.get("prompt", data.get("text")))),
        "created_at": _text(data.get("created_at")),
        "updated_at": _text(data.get("updated_at")),
        "child_job_ids": child_ids,
        "progress": {
            "total": len(child_ids),
            "completed": sum(counts.get(status, 0) for status in JOB_TERMINAL_STATUSES),
            "status_counts": counts,
        },
        "sort_time": _timestamp(data.get("updated_at", data.get("created_at"))),
    }


def _all_goals():
    result = []
    for key in _keys("sid:goals:*"):
        data = _hash(key)
        if data:
            result.append(_goal(key, data))
    return sorted(result, key=lambda item: (-item["sort_time"], item["id"]))


def _worker(key, data, jobs):
    data = data if isinstance(data, dict) else {}
    worker_id = _text(data.get("id", data.get("worker_id")), _key_suffix(key))
    matches = [job for job in jobs if job["worker"] == worker_id]
    active = [job for job in matches if job["status"] not in JOB_TERMINAL_STATUSES | FAILURE_STATUSES]
    job = max(active or matches, key=lambda item: (item["sort_time"], item["id"]), default={})
    active_job = max(active, key=lambda item: (item["sort_time"], item["id"]), default={})
    job_data = _hash(f"sid:jobs:{job.get('id')}") if job else {}
    merged = {**data, **job_data}
    return {
        "id": worker_id,
        "role": _text(data.get("role", data.get("job_role")), job.get("role")),
        # Keep latest-job telemetry, while exposing an unambiguous busy signal.
        "job_id": _text(data.get("job_id", data.get("current_job_id", data.get("active_job_id"))), job.get("id")),
        "active_job_id": active_job.get("id"),
        "status": _text(data.get("status"), job.get("status"),),
        "provider": _text(data.get("provider"), job.get("provider")),
        "model": _text(data.get("model"), job.get("model")),
        "last_seen": _number(data.get("last_seen", data.get("heartbeat"))),
        "heartbeat_age": max(int(time.time() - _timestamp(data.get("last_seen", data.get("heartbeat")))), 0) if _timestamp(data.get("last_seen", data.get("heartbeat"))) else None,
        "effective_tokens": _effective_tokens(merged),
        "cached_input_tokens": _number(merged.get("cached_input_tokens", merged.get("cached_tokens"))),
        "command_count": _number(merged.get("command_count")),
        "duration": _duration(merged),
    }


def _orchestrator(key, data):
    data = data if isinstance(data, dict) else {}
    active_goal = _text(data.get("goal_id", data.get("active_goal")))
    status = _text(data.get("status"), "unknown")
    if active_goal and status == "idle":
        status = "active"
    return {
        "id": _text(data.get("id"), _key_suffix(key)),
        "status": status,
        "provider": _text(data.get("provider")),
        "model": _text(data.get("model")),
        "active_goal": active_goal,
        "last_seen": _number(data.get("last_seen")),
        "heartbeat_age": max(int(time.time() - _timestamp(data.get("last_seen"))), 0) if _timestamp(data.get("last_seen")) else None,
    }


def _repository():
    try:
        branch = subprocess.check_output(["git", "branch", "--show-current"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL).strip())
        return {"branch": branch or "(detached)", "status": "dirty" if dirty else "clean"}
    except Exception:
        return {"branch": "unknown", "status": "unknown"}


@app.get("/")
def root():
    return {
        "name": "SID's AI Command Center",
        "version": "0.1.0",
        "status": "online"
    }


@app.get("/health")
def health():
    services = {}

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        services["postgres"] = "healthy"
    except Exception as exc:
        services["postgres"] = f"error: {exc}"

    try:
        redis.ping()
        services["redis"] = "healthy"
    except Exception as exc:
        services["redis"] = f"error: {exc}"

    healthy = all(value == "healthy" for value in services.values())

    return {
        "status": "healthy" if healthy else "degraded",
        "services": services
    }


# Read-only dashboard data. These routes only read Redis hashes/lists and never
# expose the Redis client or connection details to callers.
@app.get("/api/status")
def api_status():
    jobs = _all_jobs()
    goals = _all_goals()
    workers = api_workers()
    orchestrators = [_orchestrator(key, _hash(key)) for key in _keys("sid:orchestrators:*")]
    active_ids = {item["active_goal"] for item in orchestrators if item["active_goal"]}
    active_goal = next((goal for goal in goals if goal["id"] in active_ids), None)
    if active_goal is None:
        active_goal = next((goal for goal in goals if goal["status"] in {"active", "running", "in_progress"}), None)
    try:
        queue_depth = redis.llen(os.getenv("WORKER_QUEUE", "sid:jobs"))
    except Exception:
        queue_depth = None
    return {
        "repository": _repository(),
        "queue": {"name": os.getenv("WORKER_QUEUE", "sid:jobs"), "depth": queue_depth},
        "orchestrators": orchestrators,
        "active_goal": active_goal,
        "workers": workers,
        "heartbeat": {"workers": workers, "orchestrators": orchestrators},
        "goals": {"recent": goals[:API_DEFAULT_LIMIT]},
        "jobs": {"recent": jobs[:API_DEFAULT_LIMIT], "failures": [job for job in jobs if job["status"] in FAILURE_STATUSES][:API_DEFAULT_LIMIT]},
        "pending_human_approvals": [job for job in jobs if _approval_ready(job)][:API_DEFAULT_LIMIT],
    }


@app.get("/api/repository")
def api_repository():
    return _repository()


@app.get("/api/queue")
def api_queue():
    name = os.getenv("WORKER_QUEUE", "sid:jobs")
    try:
        depth = redis.llen(name)
    except Exception:
        depth = None
    return {"name": name, "depth": depth}


@app.get("/api/orchestrators")
def api_orchestrators():
    return [_orchestrator(key, _hash(key)) for key in _keys("sid:orchestrators:*")]


def api_workers():
    jobs = _all_jobs()
    return sorted([_worker(key, _hash(key), jobs) for key in _keys("sid:workers:*")], key=lambda item: item["id"])


@app.get("/api/workers")
def get_api_workers():
    return api_workers()


@app.get("/api/heartbeat")
def api_heartbeat():
    return {"workers": api_workers(), "orchestrators": api_orchestrators()}


@app.get("/api/goals")
def api_goals(
    limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    return _page(_all_goals(), offset, limit)


@app.get("/api/goals/recent")
def api_recent_goals(
    limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    return api_goals(limit, offset)


@app.get("/api/jobs")
def api_jobs(
    limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    return _page(_all_jobs(), offset, limit)


@app.get("/api/jobs/recent")
def api_recent_jobs(
    limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    return api_jobs(limit, offset)


def _approval_ready(job):
    """Return whether persisted state is ready for host-side approval validation.

    This deliberately does not inspect Git or host worktrees. The API container
    has no repository authority. scripts/job-review.py is the authoritative
    final gate for main freshness, repository cleanliness, worktree state,
    branch state, and the merge itself.
    """
    if (
        job.get("status") != "awaiting_review"
        or job.get("review_status") != "complete"
        or job.get("review_verdict") != "pass"
        or job.get("integration_status") != "passed"
    ):
        return False

    review_job_id = job.get("review_job_id")
    integrated_commit = job.get("integrated_candidate_commit")
    base_commit = job.get("integration_base_commit")

    if not all((review_job_id, integrated_commit, base_commit)):
        return False

    if job.get("reviewed_commit") != integrated_commit:
        return False

    review = _hash(f"sid:jobs:{review_job_id}")
    if (
        not review
        or review.get("role") != "reviewer"
        or review.get("builder_job_id") != job.get("id")
        or review.get("status") != "review_complete"
        or review.get("review_verdict") != "pass"
        or review.get("candidate_commit") != integrated_commit
        or review.get("reviewed_commit") != integrated_commit
    ):
        return False

    return True


@app.get("/api/approvals")
def api_approvals(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    approvals = [job for job in _all_jobs() if _approval_ready(job)]
    return approvals[:_limit(limit)]


@app.get("/api/jobs/approvals")
def api_job_approvals(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return api_approvals(limit)


@app.get("/api/failures")
def api_failures(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return [job for job in _all_jobs() if job["status"] in FAILURE_STATUSES][:_limit(limit)]


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUSY_WORKER_STATUSES = {"working", "busy", "claimed", "running", "active", "stopping"}
_REDACT_KEY = re.compile(
    r"(?:password|passwd|secret|credential|api.?key|private.?key|authorization|cookie|"
    r"(?:api|access|refresh|id|auth|bearer|session)[_-]?token|(?<![A-Za-z0-9_])token(?![A-Za-z0-9_]))",
    re.I,
)
_REDACT_VALUE = re.compile(
    r"(?is)(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_-])*"
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key|authorization|cookie)"
    r"(?:[_-][A-Za-z0-9]+)*"
    r"\s*[:=]\s*(?:[^\s,;]+|\"[^\"]*\"|'[^']*')|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----)"
)


def _valid_identifier(value, label="identifier"):
    value = _text(value)
    if not value or not _IDENTIFIER.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"Invalid {label}")
    return value


def _goal_record(goal_id):
    key = f"sid:goals:{_valid_identifier(goal_id, 'goal id')}"
    data = _hash(key)
    if not data:
        raise HTTPException(status_code=404, detail="Goal not found")
    return key, data


def _job_record(job_id):
    key = f"sid:jobs:{_valid_identifier(job_id, 'job id')}"
    data = _hash(key)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return key, data


def _submit_goal(payload):
    goal = " ".join(payload.goal.split())
    if not goal:
        raise HTTPException(status_code=422, detail="Goal cannot be blank")
    request_id = _text(payload.request_id)
    if request_id:
        _valid_identifier(request_id, "request id")
        marker = f"sid:goal-requests:{request_id}"
        try:
            if not redis.set(marker, "reserved", nx=True, ex=86400):
                existing = redis.get(marker)
                if existing and existing != "reserved":
                    existing_goal = _hash(f"sid:goals:{existing}")
                    existing_atomic = str(
                        existing_goal.get("atomic", "false")
                    ).lower() in {"1", "true", "yes"}
                    if existing_atomic != bool(payload.atomic):
                        raise HTTPException(
                            status_code=409,
                            detail="Request id already belongs to a goal with a different atomic mode",
                        )
                    return {
                        "id": existing,
                        "status": "accepted",
                        "atomic": existing_atomic,
                        "duplicate": True,
                    }
                raise HTTPException(status_code=409, detail="A goal with this request id is already being submitted")
        except HTTPException:
            raise
        except Exception:
            pass

    # A small duplicate guard for clients that omit request_id. Atomic and
    # non-atomic submissions are intentionally distinct workflows.
    for key in _keys("sid:goals:*"):
        old = _hash(key)
        old_atomic = str(old.get("atomic", "false")).lower() in {"1", "true", "yes"}
        if (
            old.get("goal") == goal
            and old.get("status") in {"queued", "planning", "running"}
            and old_atomic == bool(payload.atomic)
        ):
            return {"id": _text(old.get("id"), _key_suffix(key)), "status": old.get("status"), "atomic": old_atomic, "duplicate": True}

    goal_id = uuid.uuid4().hex[:12]
    record = {
        "id": goal_id, "goal": goal, "prompt": goal,
        "status": "queued", "atomic": str(bool(payload.atomic)).lower(),
        "created_at": str(time.time()), "updated_at": str(time.time()),
    }
    redis.hset(f"sid:goals:{goal_id}", mapping=record)
    try:
        redis.rpush(os.getenv("GOAL_QUEUE", "sid:goals"), json.dumps({"id": goal_id, "goal": goal, "atomic": payload.atomic}))
    except Exception:
        redis.hset(f"sid:goals:{goal_id}", mapping={"status": "queue_failed", "updated_at": str(time.time())})
        if request_id:
            try:
                marker = f"sid:goal-requests:{request_id}"
                if redis.get(marker) == "reserved":
                    redis.delete(marker)
            except Exception:
                pass
        raise HTTPException(status_code=503, detail="Goal queue is unavailable")
    if request_id:
        try:
            redis.set(f"sid:goal-requests:{request_id}", goal_id, ex=86400)
        except Exception:
            pass
    return {"id": goal_id, "status": "accepted", "atomic": bool(payload.atomic)}


@app.post("/api/goals", response_model=GoalAccepted, status_code=202)
def submit_goal(payload: GoalSubmit):
    return _submit_goal(payload)


@app.post("/api/goals/submit", response_model=GoalAccepted, status_code=202)
def submit_goal_compat(payload: GoalSubmit):
    return _submit_goal(payload)


@app.post("/api/goals/submit-atomic", response_model=GoalAccepted, status_code=202)
def submit_atomic_goal(payload: GoalSubmit):
    return _submit_goal(payload.model_copy(update={"atomic": True}))


@app.post("/api/prompts", response_model=GoalAccepted, status_code=202)
def submit_prompt(payload: PromptSubmit):
    return _submit_goal(GoalSubmit(
        goal=payload.prompt,
        atomic=payload.atomic,
        request_id=payload.request_id,
    ))


@app.get("/api/goals/{goal_id}")
def get_goal_detail(goal_id: str):
    key, data = _goal_record(goal_id)
    result = _goal(key, data)
    result["atomic"] = str(data.get("atomic", "false")).lower() in {"1", "true", "yes"}
    result["jobs"] = [_job(f"sid:jobs:{job_id}", _hash(f"sid:jobs:{job_id}")) for job_id in result["child_job_ids"] if _hash(f"sid:jobs:{job_id}")]
    return result


@app.get("/api/jobs/{job_id}")
def get_job_detail(job_id: str):
    key, data = _job_record(job_id)
    result = _job(key, data)
    # Detail-only fields: bounded text that is too large for list views.
    for field in ("review_findings", "needs_human_reason", "repair_status",
                  "integration_error", "title"):
        result[field] = _text(data.get(field))
    for field in ("repair_attempts", "max_repair_attempts"):
        result[field] = _number(data.get(field))
    return _redact(result)


@app.get("/api/action-required")
@app.get("/api/actions-required")
def api_action_required(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    jobs = _all_jobs()
    items = [job for job in jobs if (
        job["status"] in FAILURE_STATUSES or
        job["status"] in {"awaiting_review", "needs_human"} or
        job["review_status"] in {"changes_required", "failed"}
    )]
    return items[:_limit(limit)]


@app.get("/api/agents/activity")
@app.get("/api/agent-activity")
def api_agent_activity(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return [job for job in _all_jobs() if job["worker"] or job["role"]][: _limit(limit)]


def _worker_record(worker_id):
    worker_id = _valid_identifier(worker_id, "worker id")
    key = f"sid:workers:{worker_id}"
    data = _hash(key)
    if not data:
        raise HTTPException(status_code=404, detail="Worker not found")
    return key, data


@app.post("/api/workers/{worker_id}/stop")
def stop_worker(worker_id: str, action: WorkerAction | None = None):
    key, data = _worker_record(worker_id)
    jobs = _all_jobs()
    active = [job for job in jobs if job["worker"] == worker_id and job["status"] not in JOB_TERMINAL_STATUSES | FAILURE_STATUSES]
    if active:
        raise HTTPException(status_code=409, detail="Worker is busy; it cannot be stopped")
    # The worker process reads this control key between jobs; writing the
    # heartbeat hash alone was overwritten by the next heartbeat.
    redis.set(f"sid:worker-control:{worker_id}", "disabled")
    redis.hset(key, mapping={"status": "disabled", "stop_reason": _text(action.reason if action else None, "requested"), "updated_at": str(time.time())})
    return _worker(key, {**data, "status": "disabled"}, jobs)


@app.delete("/api/workers/{worker_id}")
def remove_worker(worker_id: str):
    key, data = _worker_record(worker_id)
    jobs = _all_jobs()
    active = [job for job in jobs if job["worker"] == worker_id and job["status"] not in JOB_TERMINAL_STATUSES | FAILURE_STATUSES]
    if active or _text(data.get("status")).lower() in _BUSY_WORKER_STATUSES:
        raise HTTPException(status_code=409, detail="Busy worker cannot be removed")
    redis.delete(key)
    return {"id": worker_id, "removed": True}


@app.post("/api/workers/{worker_id}/start")
def start_worker(worker_id: str):
    key, data = _worker_record(worker_id)
    if _text(data.get("status")).lower() in _BUSY_WORKER_STATUSES:
        raise HTTPException(status_code=409, detail="Worker is already active")
    redis.delete(f"sid:worker-control:{worker_id}")
    redis.hset(key, mapping={"status": "idle", "updated_at": str(time.time())})
    return _worker(key, {**data, "status": "idle"}, _all_jobs())



# Operator actions. The API has no repository authority (no git, no
# shell): it records a request and the host-side operator service
# (services/operator/sid_operator.py) validates and executes it with
# scripts/job-review.py. Nothing here writes job state.
OPERATOR_STREAM = "sid:operator-requests"
OPERATOR_STREAM_MAXLEN = 10000
OPERATOR_REQUEST_FIELDS = ("request_id", "job_id", "action", "expected_status", "expected_candidate", "extra")
_OPERATOR_JOB_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_OPERATOR_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _operator_service():
    """The live operator heartbeat (30s TTL), or None when offline."""
    for key in _keys("sid:operator-service:*"):
        data = _hash(key)
        if data:
            return data
    return None


def _operator_result(data):
    return _redact({field: _text(data.get(field), "") for field in (
        *OPERATOR_REQUEST_FIELDS, "status", "message", "output",
        "requested_from", "created_at", "started_at", "finished_at",
    )})


@app.get("/api/operator/status")
def operator_status():
    service = _operator_service()
    if not service:
        return {"online": False, "allowed_actions": []}
    return {
        "online": True,
        "id": _text(service.get("id")),
        "status": _text(service.get("status")),
        "allowed_actions": [a for a in _text(service.get("allowed_actions"), "").split(",") if a],
        "request_ttl": _number(service.get("request_ttl")),
        "last_seen": _timestamp(service.get("last_seen")),
    }


@app.post("/api/jobs/{job_id}/actions", status_code=202)
def request_job_action(job_id: str, payload: OperatorActionRequest, request: Request, response: Response):
    if not _OPERATOR_JOB_ID.fullmatch(job_id):
        raise HTTPException(status_code=422, detail="Invalid job id")
    fields = {
        "request_id": payload.request_id,
        "job_id": job_id,
        "action": payload.action,
        "expected_status": payload.expected_status,
        "expected_candidate": payload.expected_candidate or "",
        "extra": str(payload.extra) if payload.extra is not None else "",
    }
    key = f"sid:operator-results:{payload.request_id}"

    # A retried request (same id) returns its existing result, never a
    # second execution. The same id for a different action is a conflict.
    existing = _hash(key)
    if existing:
        if any(_text(existing.get(f), "") != fields[f] for f in OPERATOR_REQUEST_FIELDS):
            raise HTTPException(status_code=409, detail="Request id already used for a different request")
        response.status_code = 200
        return _operator_result(existing)

    service = _operator_service()
    if not service:
        raise HTTPException(status_code=503, detail="Operator service is offline; use scripts/job-review.py on the host")
    allowed = _text(service.get("allowed_actions"), "").split(",")
    if payload.action not in allowed:
        raise HTTPException(status_code=403, detail=f"Action {payload.action} is disabled on the host (OPERATOR_ALLOWED_ACTIONS)")

    # Early feedback only; the operator service re-checks at execution time.
    _, job = _job_record(job_id)
    if _text(job.get("status"), "") != payload.expected_status:
        raise HTTPException(status_code=409, detail=f"Job status is now {job.get('status')!r}; refresh and decide again")

    if not redis.hsetnx(key, "request_id", payload.request_id):
        raise HTTPException(status_code=409, detail="A request with this id is already being submitted")
    # Audit only: forwarded headers are client-controlled without auth.
    requested_from = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    now = str(time.time())
    redis.hset(key, mapping={**fields, "status": "pending", "requested_from": requested_from, "created_at": now})
    try:
        redis.xadd(OPERATOR_STREAM, {**fields, "requested_from": requested_from},
                   maxlen=OPERATOR_STREAM_MAXLEN, approximate=True)
    except Exception:
        redis.hset(key, mapping={"status": "queue_failed", "message": "operator request stream is unavailable"})
        raise HTTPException(status_code=503, detail="Operator request stream is unavailable")
    return _operator_result(_hash(key))


@app.get("/api/operator-requests/{request_id}")
def get_operator_request(request_id: str):
    if not _OPERATOR_REQUEST_ID.fullmatch(request_id):
        raise HTTPException(status_code=422, detail="Invalid request id")
    data = _hash(f"sid:operator-results:{request_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Operator request not found")
    return _operator_result(data)

def _redact(value, key=""):
    if _REDACT_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, str):
        return _REDACT_VALUE.sub("[REDACTED]", value)
    return value


@app.get("/api/goals/{goal_id}/handoff")
@app.get("/api/goals/{goal_id}/handoff-bundle")
@app.get("/api/handoff/{goal_id}")
def download_handoff(goal_id: str):
    detail = get_goal_detail(goal_id)
    bundle = {
        "bundle_type": "PRE-MERGE REVIEW",
        "goal": detail,
        "repository": _repository(),
        "generated_at": time.time(),
    }
    content = json.dumps(_redact(bundle), indent=2, sort_keys=True).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("PRE-MERGE REVIEW.json", content)
    archive.seek(0)
    return StreamingResponse(archive, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="handoff-{_valid_identifier(goal_id, "goal id")}.zip"'})


@app.get("/api/goals/{goal_id}/handoff-data")
def handoff_data(goal_id: str):
    detail = get_goal_detail(goal_id)
    return _redact({
        "bundle_type": "PRE-MERGE REVIEW",
        "goal": detail,
        "repository": _repository(),
    })


@app.on_event("startup")
def startup():
    init_database()


# ---------------------------------------------------------------------------
# Projects / Tasks API
# ---------------------------------------------------------------------------

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Project, Task
from schemas import (
    ProjectCreate,
    ProjectResponse,
    TaskCreate,
    TaskResponse,
    WorkerAction,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/projects", response_model=ProjectResponse)
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    record = Project(**project.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.get("/projects", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.id.desc()).all()


@app.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db)):
    record = db.get(Project, project_id)

    if not record:
        raise HTTPException(status_code=404, detail="Project not found")

    return record


@app.post("/tasks", response_model=TaskResponse)
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    project = db.get(Project, task.project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    record = Task(**task.model_dump())

    db.add(record)
    db.commit()
    db.refresh(record)

    return record


@app.get("/tasks", response_model=list[TaskResponse])
def list_tasks(db: Session = Depends(get_db)):
    return db.query(Task).order_by(Task.priority.desc(), Task.id.asc()).all()


@app.get("/projects/{project_id}/tasks", response_model=list[TaskResponse])
def project_tasks(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.priority.desc(), Task.id.asc())
        .all()
    )


# Provider inspection and persistent agent definitions (execution is internal only).
from agent_routes import router as agent_router

app.include_router(agent_router)
