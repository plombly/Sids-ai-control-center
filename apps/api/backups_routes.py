"""Settings -> System, Backups: the snapshots on this server, the last backup
and restore check, and buttons to run either now (administrators).

GET  /api/backups
POST /api/backups/run {what: "backup"|"restore_check", request_id}

The API cannot read /var/backups: scripts/laika-backup.py publishes the
catalogue (laika:backup:catalog) after every run; runs go through the
operator service (actions backup_now / restore_check_now).
"""

import json

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class Run(BaseModel):
    what: str = Field(pattern=r"^(backup|restore_check)$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _load(key):
    import main
    try:
        raw = main.redis.get(key)
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None
    except Exception:
        return None


@router.get("/api/backups")
def backups():
    return {"snapshots": _load("laika:backup:catalog") or [], "last": _load("laika:backup:last"),
            "restore_check": _load("laika:backup:restore-check")}


@router.post("/api/backups/run", status_code=202)
def run(payload: Run):
    import project_routes as projects
    action = "backup_now" if payload.what == "backup" else "restore_check_now"
    return projects._operator_request(action, payload.request_id, {"what": ""})
