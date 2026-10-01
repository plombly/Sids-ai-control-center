"""Project types, goal templates and builds for the dashboard.

The apps service detects each project's type (sid:project-type:<id>) and
starts builds the dashboard asks for (sid:build-request:<id>) as their own
host units (scripts/sid-build.py). This API only reads results and leaves
requests; the build zips and logs are read from the read-only /projects
mount (/opt/sid-projects/<id>/builds).
"""

import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

import project_catalog
import project_routes as projects

router = APIRouter()
PROJECTS_MOUNT = Path(os.environ.get("SID_PROJECTS_MOUNT", "/projects"))
BUILD_ID = re.compile(r"^[A-Za-z0-9]{1,40}$")
LOG_TAIL = 200 * 1024
PENDING = ("queued", "running")


def detection(project_id):
    try:
        value = json.loads(projects._redis().redis.get(f"sid:project-type:{project_id}") or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def type_info(project_id, data):
    """What the project is: the owner's choice, else what SID detected, else
    a guess from the owner's description."""
    found = detection(project_id)
    chosen = projects._text(data.get("type"))
    description = projects._text(data.get("type_description"))
    detected = projects._text(found.get("type"), "")
    if chosen in project_catalog.TYPES:
        effective, source = chosen, "chosen"
    elif detected and detected != "unknown":
        effective, source = detected, "detected"
    elif description:
        effective, source = project_catalog.type_from_description(description), "described"
    else:
        effective, source = ("unknown" if found else "checking"), "detected"
    return {"type": effective, "source": source, "chosen": chosen or "", "description": description or "",
            "detected": detected or "", "stack": projects._text(found.get("stack"), ""),
            "confidence": found.get("confidence") or 0,
            "evidence": [str(e) for e in (found.get("evidence") or [])][:8],
            "candidates": found.get("candidates") or [], "checked_at": projects._numeric(found.get("checked_at"))}


def recipe_for(project_id, data):
    recipe = project_catalog.resolve_recipe(data, detection(project_id), project_id)
    return {k: recipe.get(k) for k in ("label", "image", "command", "output", "note", "needs", "unsupported", "source", "stack")
            if recipe.get(k)}


def _project(project_id, builds=False):
    project_id = projects._id(project_id)
    if not projects._known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if builds and project_id == "sid":
        raise HTTPException(status_code=404, detail="SID itself is deployed by its operator, not built here")
    return project_id


def _build(project_id, build_id):
    record = projects._data(projects._redis().redis.hgetall(f"sid:build:{project_id}:{build_id}"))
    if not record:
        return None
    return {"id": build_id, "status": projects._text(record.get("status"), "unknown"),
            "commit": projects._text(record.get("commit"), ""), "recipe": projects._text(record.get("recipe"), ""),
            "error": projects._text(record.get("error"), ""), "size": projects._numeric(record.get("size")),
            "files": projects._numeric(record.get("files")),
            **{key: projects._numeric(record.get(key)) for key in ("requested_at", "started_at", "finished_at")}}


@router.get("/api/project-catalog")
def project_catalog_view():
    return project_catalog.catalog()


@router.post("/api/projects/{project_id}/recheck", status_code=202)
def recheck(project_id: str):
    """Forget the detected type; the apps service detects it again within a few seconds."""
    project_id = _project(project_id)
    projects._redis().redis.delete(f"sid:project-type:{project_id}")
    return {"id": project_id, "recheck_requested": True}


@router.get("/api/projects/{project_id}/builds")
def builds(project_id: str):
    project_id = _project(project_id, builds=True)
    main = projects._redis()
    ids = main.redis.lrange(f"sid:builds:{project_id}", 0, 19) or []
    items = [item for item in (_build(project_id, projects._text(b)) for b in ids) if item]
    return {"recipe": recipe_for(project_id, projects._project_data(project_id)), "builds": items,
            "requested": bool(main.redis.get(f"sid:build-request:{project_id}"))}


@router.post("/api/projects/{project_id}/builds", status_code=202)
def request_build(project_id: str):
    project_id = _project(project_id, builds=True)
    main = projects._redis()
    data = projects._project_data(project_id)
    if data.get("status") in ("archived", "deleting", "pending_key"):
        raise HTTPException(status_code=409, detail="This project is not active")
    recipe = recipe_for(project_id, data)
    if recipe.get("unsupported"):
        raise HTTPException(status_code=409, detail=recipe["unsupported"])
    for existing in main.redis.lrange(f"sid:builds:{project_id}", 0, 4) or []:
        item = _build(project_id, projects._text(existing))
        if item and item["status"] in PENDING:
            raise HTTPException(status_code=409, detail="A build is already running")
    build_id = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + secrets.token_hex(2)
    if not main.redis.set(f"sid:build-request:{project_id}", json.dumps({"build_id": build_id}), nx=True, ex=3600):
        raise HTTPException(status_code=409, detail="A build was already requested")
    return {"id": project_id, "build_id": build_id, "status": "requested"}


def _file(project_id, build_id, suffix):
    if not BUILD_ID.fullmatch(build_id or ""):
        raise HTTPException(status_code=404, detail="Build not found")
    folder = (PROJECTS_MOUNT / project_id / "builds").resolve()
    target = (folder / f"{build_id}{suffix}")
    if target.is_symlink() or not target.is_file() or target.resolve().parent != folder:
        raise HTTPException(status_code=404, detail="Not available")
    return target


@router.get("/api/projects/{project_id}/builds/{build_id}/download")
def download(project_id: str, build_id: str):
    project_id = _project(project_id, builds=True)
    item = _build(project_id, build_id) if BUILD_ID.fullmatch(build_id or "") else None
    if not item or item["status"] != "succeeded":
        raise HTTPException(status_code=404, detail="Build not found")
    target = _file(project_id, build_id, ".zip")
    name = f"{project_id}-{build_id}" + (f"-{item['commit'][:8]}" if item["commit"] else "") + ".zip"
    return FileResponse(target, media_type="application/zip", filename=name)


@router.get("/api/projects/{project_id}/builds/{build_id}/log", response_class=PlainTextResponse)
def build_log(project_id: str, build_id: str):
    project_id = _project(project_id, builds=True)
    target = _file(project_id, build_id, ".log")
    with target.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - LOG_TAIL))
        text = handle.read().decode("utf-8", "replace")
    return ("… (earlier output cut)\n" if size > LOG_TAIL else "") + text
