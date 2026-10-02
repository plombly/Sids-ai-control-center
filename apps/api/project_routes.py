import json
import os
import re
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field


router = APIRouter()
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_IMPORTANCE = {"high": 0, "medium": 1, "low": 2}
_GOAL_KEY = re.compile(r"^sid:goals:([a-z0-9][a-z0-9-]{0,39})$")
_JOB_KEY = re.compile(r"^sid:jobs:([a-z0-9][a-z0-9-]{0,39})$")


class ProjectGoal(BaseModel):
    goal: str = Field(min_length=1, max_length=100_000)
    atomic: bool = False
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


_COMMAND = r"^[^\r\n]*$"


class ProjectPatch(BaseModel):
    """Any subset of the editable settings. Commands are one line; an empty
    string clears one (gate/setup: detect automatically; run: app off)."""
    importance: Optional[Literal["high", "medium", "low"]] = None
    gate_command: Optional[str] = Field(default=None, max_length=300, pattern=_COMMAND)
    setup_command: Optional[str] = Field(default=None, max_length=300, pattern=_COMMAND)
    run_command: Optional[str] = Field(default=None, max_length=300, pattern=_COMMAND)
    run_port: Optional[int] = Field(default=None, ge=8100, le=8199)
    run_memory_mb: Optional[int] = Field(default=None, ge=64, le=65536)
    run_cpus: Optional[float] = Field(default=None, ge=0.1, le=64)
    run_tasks: Optional[int] = Field(default=None, ge=16, le=32768)
    # What the project is ("" = detect automatically) and how to build it
    # ("" = the detected stack's recipe, apps/api/project_catalog.py).
    type: Optional[str] = Field(default=None, max_length=40)
    type_description: Optional[str] = Field(default=None, max_length=500, pattern=_COMMAND)
    build_command: Optional[str] = Field(default=None, max_length=500, pattern=_COMMAND)
    build_image: Optional[str] = Field(default=None, max_length=300)
    build_output: Optional[str] = Field(default=None, max_length=200, pattern=_COMMAND)
    # Internet access (services/network_access.py, scripts/sid-build.py):
    # tests "" = offline, ask when they need it / "always"; builds "" or
    # "internet" = internet but not this server or the LAN / "none".
    gate_network: Optional[Literal["", "always"]] = None
    build_network: Optional[Literal["", "internet", "none"]] = None


_REQUEST_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"
# Network git URLs only; the host-side operator validates again.
_GIT_URL = r"^(git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+|ssh://\S+|https://\S+)$"


class ProjectCreate(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    importance: Literal["high", "medium", "low"] = "medium"
    source: Literal["empty", "clone"]
    url: Optional[str] = Field(default=None, pattern=_GIT_URL)
    push_remote: Optional[str] = Field(default=None, pattern=_GIT_URL)
    gate: Optional[str] = Field(default=None, max_length=200, pattern=r"^[^\r\n]*$")
    request_id: str = Field(pattern=_REQUEST_ID)


class ProjectPushSetup(BaseModel):
    url: str = Field(pattern=_GIT_URL)
    request_id: str = Field(pattern=_REQUEST_ID)


class ProjectRequest(BaseModel):
    request_id: str = Field(pattern=_REQUEST_ID)


class ProjectDelete(BaseModel):
    confirm: str = Field(max_length=40)
    request_id: str = Field(pattern=_REQUEST_ID)


def _text(value, default=None):
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _clean(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _data(value):
    return {_clean(k): _clean(v) for k, v in (value or {}).items()}


def _id(value):
    if not _ID.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail="Invalid project id")
    return value


def _redis():
    import main
    return main


def _members(main):
    return {_text(_clean(value)) for value in (main.redis.smembers("sid:projects") or set())}


def _project_data(project_id):
    main = _redis()
    data = _data(main.redis.hgetall(f"sid:projects:{project_id}"))
    if project_id == "sid" and not data:
        data = {"id": "sid", "name": "SID AI Command Center", "importance": "medium", "status": "active"}
    return data


def _numeric(value):
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _scan_hashes(main, pattern, matcher):
    for raw_key, raw_data in main._hashes(pattern):
        key = _text(_clean(raw_key), "")
        if matcher.fullmatch(key):
            data = _data(raw_data)
            if data:
                yield key, data


_ZERO_COUNTS = {"goals_active": 0, "jobs_queued": 0, "jobs_running": 0,
                "jobs_awaiting_approval": 0, "jobs_needs_human": 0, "jobs_merged": 0}
_counts_cache = {"rows": None, "counts": {}}


def _all_counts():
    """Counts for every project in one pass over goals and jobs, reused while
    the same 1-second snapshot is served (main._hashes)."""
    main = _redis()
    goal_rows, job_rows = main._hashes("sid:goals:*"), main._hashes("sid:jobs:*")
    cached = _counts_cache["rows"]
    if cached and cached[0] is goal_rows and cached[1] is job_rows:
        return _counts_cache["counts"]
    counts = {}
    bucket = lambda pid: counts.setdefault(pid, dict(_ZERO_COUNTS))
    for _, data in _scan_hashes(main, "sid:goals:*", _GOAL_KEY):
        if data.get("status") in {"queued", "planning", "running"}:
            bucket(_text(data.get("project_id"), "sid"))["goals_active"] += 1
    for _, data in _scan_hashes(main, "sid:jobs:*", _JOB_KEY):
        role = _text(data.get("job_role", data.get("role")))
        if role and role != "builder":
            continue
        status = _text(data.get("status"))
        field = ("jobs_queued" if status in {"queued", "blocked"}
                 else "jobs_running" if status in {"claimed", "running", "testing"}
                 else "jobs_awaiting_approval" if status == "awaiting_review" and _text(data.get("review_verdict")) == "pass"
                 else "jobs_needs_human" if status == "needs_human"
                 else "jobs_merged" if status == "merged" else None)
        if field:
            bucket(_text(data.get("project_id"), "sid"))[field] += 1
    _counts_cache.update(rows=(goal_rows, job_rows), counts=counts)
    return counts


def _counts(project_id):
    return dict(_all_counts().get(project_id, _ZERO_COUNTS))


def _system_info():
    try:
        info = json.loads(_redis().redis.get("sid:system-info") or "{}")
    except (TypeError, ValueError):
        info = {}
    info = info if isinstance(info, dict) else {}
    return {"remote": _text(info.get("remote")), "branch": _text(info.get("branch")),
            "head": _text(info.get("head")), "subject": _text(info.get("subject")),
            "gate": "scripts/integration-check.py (tests, self-tests, diagnostics)"}


def _app_status(project_id):
    status = _data(_redis().redis.hgetall(f"sid:app-status:{project_id}"))
    if not status:
        return None
    return {"state": _text(status.get("state"), "unknown"), "port": _numeric(status.get("port")),
            "commit": _text(status.get("commit")), "error": _text(status.get("error")),
            "log": _text(status.get("log")), "updated_at": _numeric(status.get("updated_at"))}


# --- project groups (services/sid_projects.py: one level, the parent controls
# its children's importance and test internet access) -------------------------
_INHERITED = ("importance", "gate_network")


def _group_info(project_id, data):
    """(effective data, group fields) for a project."""
    main = _redis()
    parent_id = _text(data.get("parent"), "")
    group = {"parent": parent_id, "parent_name": "", "children": [], "managed": []}
    effective = dict(data)
    if parent_id:
        parent = _project_data(parent_id)
        if parent:
            group["parent_name"] = _text(parent.get("name"), parent_id)
            group["managed"] = list(_INHERITED)
            for key in _INHERITED:
                effective[key] = parent.get(key) or ""
            if parent.get("status") == "archived" and effective.get("status", "active") == "active":
                effective["status"] = "archived"
    else:
        for other in sorted(_members(main)):
            if other != project_id and main.redis.hget(f"sid:projects:{other}", "parent") == project_id:
                child = _project_data(other)
                group["children"].append({"id": other, "name": _text(child.get("name"), other),
                                          "status": _text(child.get("status"), "active")})
    return effective, group


def _view_only(project_id):
    import managed
    return managed.view_only(_redis().redis, project_id)


def _item(project_id, data=None):
    import build_routes
    main = _redis()
    data = data if data is not None else _project_data(project_id)
    data, group = _group_info(project_id, data)
    stats = _data(main.redis.hgetall(f"sid:project-stats:{project_id}"))
    return {
        "id": project_id,
        "name": _text(data.get("name"), "SID AI Command Center" if project_id == "sid" else project_id),
        "importance": _text(data.get("importance"), "medium"),
        "status": _text(data.get("status"), "active"),
        "gate_command": "scripts/integration-check.py" if project_id == "sid" else _text(data.get("gate_command")),
        "setup_command": _text(data.get("setup_command")),
        "run_command": _text(data.get("run_command")),
        "run_port": _numeric(data.get("run_port")),
        "run_memory_mb": _numeric(data.get("run_memory_mb")) or 1024,
        "run_cpus": _numeric(data.get("run_cpus")) or 1,
        "run_tasks": _numeric(data.get("run_tasks")) or 512,
        "app": _app_status(project_id),
        "project_type": build_routes.type_info(project_id, data),
        "build_command": _text(data.get("build_command")),
        "build_image": _text(data.get("build_image")),
        "build_output": _text(data.get("build_output")),
        "gate_network": _text(data.get("gate_network"), ""),
        "build_network": _text(data.get("build_network"), "") or "internet",
        # SID itself: the control plane, configured on the host.
        "system": _system_info() if project_id == "sid" else None,
        "push_remote": _text(data.get("push_remote")),
        "created_at": _numeric(data.get("created_at")) or 0,
        "counts": _counts(project_id),
        "view_only": _view_only(project_id),
        **group,
        "stats": {key: _numeric(stats.get(key)) for key in ("remaining_effort", "waiting_jobs", "running_jobs")},
    }


# Development installs show the built-in project (LAIka itself, view-only);
# production installs have no such project at all.
BUILTIN_PROJECT = os.environ.get("SID_BUILTIN_PROJECT", "1") != "0"


def _known(project_id):
    if project_id == "sid":
        return BUILTIN_PROJECT
    return project_id in _members(_redis())


@router.get("/api/projects")
def list_projects(include_archived: bool = False):
    main = _redis()
    ids = _members(main) - {"sid"}
    if BUILTIN_PROJECT:
        ids.add("sid")
    result = []
    for project_id in ids:
        item = _item(project_id)
        if item["status"] != "archived" or include_archived:
            result.append(item)
    return sorted(result, key=lambda item: (_IMPORTANCE.get(item["importance"], 1), item["name"]))


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, limit: int = Query(25, ge=1, le=100)):
    project_id = _id(project_id)
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    main = _redis()
    item = _item(project_id)
    goals = [(key, data) for key, data in _scan_hashes(main, "sid:goals:*", _GOAL_KEY)
             if _text(data.get("project_id"), "sid") == project_id]
    jobs = [(key, data) for key, data in _scan_hashes(main, "sid:jobs:*", _JOB_KEY)
            if _text(data.get("project_id"), "sid") == project_id
            and (not _text(data.get("job_role", data.get("role")))
                 or _text(data.get("job_role", data.get("role"))) == "builder")]
    goals.sort(key=lambda pair: (-(_numeric(pair[1].get("updated_at")) or 0), pair[0]))
    jobs.sort(key=lambda pair: (-(_numeric(pair[1].get("updated_at")) or 0), pair[0]))
    item["goals"] = [main._goal(key, data) for key, data in goals[:limit]]
    item["jobs"] = [main._job(key, data) for key, data in jobs[:limit]]
    return item


@router.post("/api/projects/{project_id}/goals", status_code=202)
def submit_project_goal(project_id: str, payload: ProjectGoal):
    project_id = _id(project_id)
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    status = _project_data(project_id).get("status", "active")
    if status in {"archived", "pending_key"}:
        raise HTTPException(status_code=409, detail="Project is not accepting goals")
    main = _redis()
    result = main._submit_goal(payload, project_id=project_id)
    result["project_id"] = project_id
    return result


@router.patch("/api/projects/{project_id}")
def patch_project(project_id: str, payload: ProjectPatch):
    project_id = _id(project_id)
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    main = _redis()
    key = f"sid:projects:{project_id}"
    if project_id == "sid" and not main.redis.hgetall(key):
        main.redis.hset(key, mapping={"id": "sid", "name": "SID AI Command Center", "status": "active"})
    changes = payload.model_dump(exclude_none=True)
    if project_id == "sid":
        # SID's commands are its own gate and control plane: never editable.
        changes = {k: v for k, v in changes.items() if k in ("importance", "type", "type_description")}
    _check_build_fields(changes)
    managed = sorted(set(changes) & set(_INHERITED))
    parent_id = _text(_project_data(project_id).get("parent"), "")
    if managed and parent_id:
        raise HTTPException(status_code=409, detail=f"{', '.join(managed)} is managed by the parent project {parent_id}")
    if "run_port" in changes:
        for other in _members(main):
            if other != project_id and _project_data(other).get("run_port") == str(changes["run_port"]):
                raise HTTPException(status_code=409, detail=f"Port {changes['run_port']} is used by {other}")
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    main.redis.hset(key, mapping={**{k: str(v).strip() for k, v in changes.items()}, "updated_at": str(time.time())})
    return _item(project_id)


def _check_build_fields(changes):
    import project_catalog
    if changes.get("type") and changes["type"] not in project_catalog.TYPES:
        raise HTTPException(status_code=422, detail="Unknown project type")
    if changes.get("build_image") and not project_catalog.valid_image(changes["build_image"].strip()):
        raise HTTPException(status_code=422, detail="Build image must be a Docker image name like node:22-bookworm")
    output = (changes.get("build_output") or "").strip()
    if output and (output.startswith("/") or ".." in output.split("/") or "\\" in output):
        raise HTTPException(status_code=422, detail="Build output must be a folder inside the project")


class ProjectParent(BaseModel):
    parent: str = Field(default="", max_length=40, pattern=r"^([a-z0-9][a-z0-9-]{0,39})?$")


@router.post("/api/projects/{project_id}/parent")
def set_parent(project_id: str, payload: ProjectParent):
    """Make the project a child of parent (attach), or a normal project again ("")."""
    project_id = _id(project_id)
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    main = _redis()
    key = f"sid:projects:{project_id}"
    current = _text(_project_data(project_id).get("parent"), "")
    if payload.parent:
        reason = _parent_problem(project_id, payload.parent)
        if reason:
            raise HTTPException(status_code=409, detail=reason)
        main.redis.hset(key, mapping={"parent": payload.parent, "updated_at": str(time.time())})
        _event(project_id, "group", f"Joined the group of {payload.parent}")
        _event(payload.parent, "group", f"{project_id} joined this group as a child project")
    elif current:
        main.redis.hdel(key, "parent")
        main.redis.hset(key, "updated_at", str(time.time()))
        _event(project_id, "group", f"Left the group of {current}")
        _event(current, "group", f"{project_id} left this group")
    return _item(project_id)


def _parent_problem(child_id, parent_id):
    """Mirrors services/sid_projects.check_parent (the host's rules)."""
    if child_id == "sid":
        return "SID itself cannot be a child project"
    if child_id == parent_id:
        return "A project cannot be its own parent"
    if parent_id != "sid" and parent_id not in _members(_redis()):
        return f"Unknown project: {parent_id}"
    parent = _project_data(parent_id)
    if parent.get("status", "active") != "active":
        return f"{parent_id} is {parent.get('status')}"
    if parent.get("parent"):
        return f"{parent_id} is itself a child project (groups have one level)"
    main = _redis()
    if any(main.redis.hget(f"sid:projects:{other}", "parent") == child_id for other in _members(main) if other != child_id):
        return f"{child_id} has child projects of its own (groups have one level)"
    return ""


def _event(project_id, kind, title):
    entry = {"at": time.time(), "kind": kind, "title": title[:200], "detail": "", "ref": ""}
    try:
        _redis().redis.lpush(f"sid:events:{project_id}", json.dumps(entry))
        _redis().redis.ltrim(f"sid:events:{project_id}", 0, 499)
    except Exception:
        pass


@router.post("/api/projects/{project_id}/app/restart", status_code=202)
def restart_app(project_id: str):
    """Ask the apps service to redeploy this project's app now."""
    project_id = _id(project_id)
    if project_id == "sid" or not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    _redis().redis.hset(f"sid:projects:{project_id}", "restart_at", str(time.time()))
    return {"id": project_id, "restart_requested": True}


# --- host-side project management (via the operator service) -------------------

def _operator_request(action, request_id, fields):
    """Queue a project action for the host operator service (the API has no
    filesystem or git authority). Same idempotency as job actions."""
    main = _redis()
    service = main._operator_service()
    if not service:
        raise HTTPException(status_code=503, detail="Operator service is offline; use scripts/sid-project.py on the host")
    if action not in _text(service.get("allowed_actions"), "").split(","):
        raise HTTPException(status_code=403, detail=f"Action {action} is disabled on the host (OPERATOR_ALLOWED_ACTIONS)")
    key = f"sid:operator-results:{request_id}"
    fields = {"request_id": request_id, "action": action, "job_id": "", **{k: v or "" for k, v in fields.items()}}
    existing = main._hash(key)
    if existing:
        if existing.get("action") != action or existing.get("project_id") != fields.get("project_id"):
            raise HTTPException(status_code=409, detail="Request id already used for a different request")
        return main._operator_result(existing)
    if not main.redis.hsetnx(key, "request_id", request_id):
        raise HTTPException(status_code=409, detail="A request with this id is already being submitted")
    main.redis.hset(key, mapping={**fields, "status": "pending", "created_at": str(time.time())})
    try:
        main.redis.xadd(main.OPERATOR_STREAM, fields, maxlen=main.OPERATOR_STREAM_MAXLEN, approximate=True)
    except Exception:
        main.redis.hset(key, mapping={"status": "queue_failed", "message": "operator request stream is unavailable"})
        raise HTTPException(status_code=503, detail="Operator request stream is unavailable")
    result = main._operator_result(main._hash(key))
    result["project_id"] = fields.get("project_id", "")
    return result


@router.post("/api/projects", status_code=202)
def create_project(payload: ProjectCreate):
    if _known(payload.id):
        raise HTTPException(status_code=409, detail="Project already exists")
    if payload.source == "clone" and not payload.url:
        raise HTTPException(status_code=422, detail="A clone needs a git URL")
    return _operator_request("create_project", payload.request_id, {
        "project_id": payload.id, "name": payload.name, "importance": payload.importance,
        "source": payload.source, "url": payload.url if payload.source == "clone" else "",
        "push_remote": payload.push_remote, "gate": payload.gate})


@router.post("/api/projects/{project_id}/retry-clone", status_code=202)
def retry_clone(project_id: str, payload: ProjectRequest):
    project_id = _id(project_id)
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if _project_data(project_id).get("status") != "pending_key":
        raise HTTPException(status_code=409, detail="Project is not waiting for a deploy key")
    return _operator_request("project_retry_clone", payload.request_id, {"project_id": project_id})


@router.post("/api/projects/{project_id}/push-setup", status_code=202)
def push_setup(project_id: str, payload: ProjectPushSetup):
    project_id = _id(project_id)
    if project_id == "sid":
        # SID's own GitHub remote and key are configured on the host; this
        # would replace them and could break pushing.
        raise HTTPException(status_code=403, detail="SID's GitHub setup is managed on the host, not from the dashboard")
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return _operator_request("project_push_setup", payload.request_id,
                             {"project_id": project_id, "url": payload.url})


@router.get("/api/projects/{project_id}/history")
def history(project_id: str):
    """main's recent changes (published by the host's apps service)."""
    project_id = _id(project_id)
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        data = json.loads(_redis().redis.get(f"sid:history:{project_id}") or "{}")
    except (TypeError, ValueError):
        data = {}
    return {"project_id": project_id, "head": data.get("head"), "changes": data.get("changes") or [],
            "can_undo": not _view_only(project_id)}


class ProjectUndo(BaseModel):
    job: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9]{1,64}$")
    commit: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    request_id: str = Field(pattern=_REQUEST_ID)


@router.post("/api/projects/{project_id}/undo", status_code=202)
def undo(project_id: str, payload: ProjectUndo):
    """Undo one change (a whole SID job or one commit) with a new commit on main."""
    project_id = _id(project_id)
    if project_id == "sid":
        raise HTTPException(status_code=403, detail="SID's own changes are not undone from the dashboard")
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if bool(payload.job) == bool(payload.commit):
        raise HTTPException(status_code=422, detail="Choose a job or a commit to undo")
    return _operator_request("project_revert", payload.request_id, {
        "project_id": project_id, "undo_job": payload.job or "", "undo_commit": payload.commit or ""})


@router.get("/api/projects-trash")
def trash():
    """Deleted projects that can still be restored (newest first)."""
    main = _redis()
    items = []
    for trash_id in main.redis.smembers("sid:trash") or []:
        info = _data(main.redis.hgetall(f"sid:trash:{_text(_clean(trash_id))}"))
        if info:
            items.append({"trash_id": _text(info.get("trash_id")), "project_id": _text(info.get("project_id")),
                          "name": _text(info.get("name")), "deleted_at": _numeric(info.get("deleted_at")),
                          "expires_at": _numeric(info.get("expires_at"))})
    return sorted(items, key=lambda item: -(item["deleted_at"] or 0))


class ProjectRestore(BaseModel):
    request_id: str = Field(pattern=_REQUEST_ID)


@router.post("/api/projects-trash/{trash_id}/restore", status_code=202)
def restore(trash_id: str, payload: ProjectRestore):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}-\d{8}T\d{6}Z", trash_id or ""):
        raise HTTPException(status_code=422, detail="Invalid trash id")
    main = _redis()
    info = _data(main.redis.hgetall(f"sid:trash:{trash_id}"))
    if not info:
        raise HTTPException(status_code=404, detail="Not in the trash (it may have been emptied)")
    project_id = _text(info.get("project_id"))
    if _known(project_id):
        raise HTTPException(status_code=409, detail=f"A project named {project_id} exists now")
    return _operator_request("restore_project", payload.request_id, {"project_id": project_id, "trash_id": trash_id})


@router.post("/api/projects/{project_id}/delete", status_code=202)
def delete_project(project_id: str, payload: ProjectDelete):
    """Wipe a project from the server (host runs sid-project.py delete)."""
    project_id = _id(project_id)
    if project_id == "sid":
        raise HTTPException(status_code=403, detail="SID itself cannot be deleted")
    if not _known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.confirm != project_id:
        raise HTTPException(status_code=422, detail="Type the project ID exactly to confirm")
    return _operator_request("delete_project", payload.request_id,
                             {"project_id": project_id, "confirm": payload.confirm})
