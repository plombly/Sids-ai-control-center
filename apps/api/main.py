import json
import math
import os
import subprocess
import time

from fastapi import FastAPI, Query
from sqlalchemy import create_engine, text
from redis import Redis
import os

from database import init_database

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
FAILURE_STATUSES = {"failed", "error", "integration_failed", "queue_failed"}


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


def _job(key, data):
    data = data if isinstance(data, dict) else {}
    return {
        "id": _text(data.get("id"), _key_suffix(key)),
        "status": _text(data.get("status"), "unknown"),
        "role": _text(data.get("job_role", data.get("role"))),
        "worker": _text(data.get("worker_id", data.get("worker"))),
        "provider": _text(data.get("provider")),
        "model": _text(data.get("model")),
        "review_status": _text(data.get("review_status")),
        "review_verdict": _text(data.get("review_verdict")),
        "effective_tokens": _effective_tokens(data),
        "cached_input_tokens": _number(data.get("cached_input_tokens", data.get("cached_tokens"))),
        "command_count": _number(data.get("command_count")),
        "duration": _duration(data),
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
    return {
        "id": worker_id,
        "role": _text(data.get("role", data.get("job_role")), job.get("role")),
        "job_id": _text(data.get("job_id", data.get("current_job_id", data.get("active_job_id"))), job.get("id")),
        "status": _text(data.get("status"), job.get("status"),),
        "provider": _text(data.get("provider"), job.get("provider")),
        "model": _text(data.get("model"), job.get("model")),
        "last_seen": _number(data.get("last_seen", data.get("heartbeat"))),
        "heartbeat_age": max(int(time.time() - _timestamp(data.get("last_seen", data.get("heartbeat")))), 0) if _timestamp(data.get("last_seen", data.get("heartbeat"))) else None,
        "effective_tokens": _effective_tokens({**data, **_hash(f"sid:jobs:{job.get('id')}")} if job else data),
        "cached_input_tokens": _number(data.get("cached_input_tokens", data.get("cached_tokens"))),
        "command_count": _number(data.get("command_count")),
        "duration": _duration(data),
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
        "workers": api_workers(),
        "heartbeat": {"workers": api_workers(), "orchestrators": orchestrators},
        "goals": {"recent": goals[:API_DEFAULT_LIMIT]},
        "jobs": {"recent": jobs[:API_DEFAULT_LIMIT], "failures": [job for job in jobs if job["status"] in FAILURE_STATUSES][:API_DEFAULT_LIMIT]},
        "pending_human_approvals": [job for job in jobs if job["status"] == "awaiting_review" and job["review_status"] == "complete" and job["review_verdict"] == "pass"],
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
def api_goals(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return _all_goals()[:_limit(limit)]


@app.get("/api/goals/recent")
def api_recent_goals(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return api_goals(limit)


@app.get("/api/jobs")
def api_jobs(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return _all_jobs()[:_limit(limit)]


@app.get("/api/jobs/recent")
def api_recent_jobs(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return api_jobs(limit)


@app.get("/api/approvals")
def api_approvals():
    return [job for job in _all_jobs() if job["status"] == "awaiting_review" and job["review_status"] == "complete" and job["review_verdict"] == "pass"]


@app.get("/api/jobs/approvals")
def api_job_approvals():
    return api_approvals()


@app.get("/api/failures")
def api_failures(limit: int = Query(API_DEFAULT_LIMIT, ge=0, le=API_MAX_LIMIT)):
    return [job for job in _all_jobs() if job["status"] in FAILURE_STATUSES][:_limit(limit)]


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
