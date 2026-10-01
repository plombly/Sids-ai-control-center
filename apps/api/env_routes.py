"""Per-project secrets / environment variables for the running app.

Values are write-only: the API stores them and lists names, never values.
They live in one file per project, /etc/sid-ai/project-env/<id>.env on the
host (root-only; mounted here at /project-env), in systemd EnvironmentFile
syntax, and only the app's systemd unit reads them (services/apps). They
never pass through Redis, so they are not in operator audit records, and
builders, tests, previews of other projects and logs never see them.
"""

import os
import re
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import project_routes as projects

router = APIRouter()

ENV_DIR = Path(os.environ.get("SID_PROJECT_ENV_DIR", "/project-env"))
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Set by SID for every app; a project value would be ignored or break it.
RESERVED = {"PORT", "HOST", "HOME", "DATA_DIR", "PATH", "TMPDIR", "LANG", "NODE_ENV", "PYTHONDONTWRITEBYTECODE"}
MAX_VARIABLES = 200


def env_path(project_id):
    return ENV_DIR / f"{project_id}.env"


def quote(value):
    """systemd EnvironmentFile double-quoted value."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unquote(text):
    if len(text) >= 2 and text[0] == text[-1] == '"':
        out, chars = [], iter(text[1:-1])
        for char in chars:
            out.append(next(chars, "\\") if char == "\\" else char)
        return "".join(out)
    return text


def read_env(project_id):
    path = env_path(project_id)
    values = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        name, sep, raw = line.partition("=")
        if sep and NAME.fullmatch(name):
            values[name] = unquote(raw)
    return values


def write_env(project_id, values):
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(ENV_DIR, 0o700)
    body = "".join(f"{name}={quote(value)}\n" for name, value in sorted(values.items()))
    handle = tempfile.NamedTemporaryFile("w", dir=ENV_DIR, prefix=f".{project_id}-", delete=False)
    try:
        os.chmod(handle.name, 0o600)
        handle.write(body)
        handle.close()
        os.replace(handle.name, env_path(project_id))
    except BaseException:
        handle.close()
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise
    # Restart the app with the new values (services/apps watches restart_at).
    projects._redis().redis.hset(f"sid:projects:{project_id}", "restart_at", str(time.time()))


def _project(project_id):
    project_id = projects._id(project_id)
    if project_id == "sid":
        raise HTTPException(status_code=404, detail="SID's own settings are not managed here")
    if not projects._known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return project_id


@router.get("/api/projects/{project_id}/env")
def list_env(project_id: str):
    project_id = _project(project_id)
    values = read_env(project_id)
    try:
        updated = env_path(project_id).stat().st_mtime
    except OSError:
        updated = None
    return {"variables": [{"name": name, "length": len(value)} for name, value in sorted(values.items())],
            "updated_at": updated}


class Variable(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=8192)


@router.put("/api/projects/{project_id}/env")
def set_env(project_id: str, payload: Variable):
    project_id = _project(project_id)
    if not NAME.fullmatch(payload.name):
        raise HTTPException(status_code=422, detail="Names use letters, digits and _ and do not start with a digit")
    if payload.name.upper() in RESERVED:
        raise HTTPException(status_code=422, detail=f"{payload.name} is set by SID for every app")
    if any(c in payload.value for c in "\n\r\0"):
        raise HTTPException(status_code=422, detail="Values must be a single line")
    values = read_env(project_id)
    if payload.name not in values and len(values) >= MAX_VARIABLES:
        raise HTTPException(status_code=422, detail=f"At most {MAX_VARIABLES} variables")
    values[payload.name] = payload.value
    write_env(project_id, values)
    return {"name": payload.name, "length": len(payload.value), "saved": True}


@router.delete("/api/projects/{project_id}/env/{name}")
def delete_env(project_id: str, name: str):
    project_id = _project(project_id)
    values = read_env(project_id)
    if name not in values:
        raise HTTPException(status_code=404, detail="No such variable")
    del values[name]
    write_env(project_id, values)
    return {"name": name, "deleted": True}
