"""LAIka's own version and updates (scripts/laika-update.py does the work
on the host; the watchdog checks once a day).

GET  /api/system/update              version, newer release, progress
POST /api/system/update {request_id} install the newer release (operator
                                     action system_update: backup first,
                                     waits for running jobs, rolls back on
                                     failure)
"""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import project_routes as projects

router = APIRouter()


def _json(key):
    try:
        return json.loads(projects._redis().redis.get(key) or "null")
    except (TypeError, ValueError):
        return None


@router.get("/api/system/update")
def update_state():
    system = _json("laika:system-info") or {}
    return {"version": system.get("version") or "", "available": _json("laika:update:available"),
            "status": _json("laika:update:status")}


class UpdateRequest(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


@router.post("/api/system/update", status_code=202)
def request_update(payload: UpdateRequest):
    available = _json("laika:update:available") or {}
    if not available.get("newer"):
        raise HTTPException(status_code=409, detail="No newer release is known; check again later")
    status = _json("laika:update:status") or {}
    if status.get("state") in ("checking", "downloading", "backing_up", "waiting", "installing", "restarting", "rolling_back"):
        raise HTTPException(status_code=409, detail="An update is already running")
    return projects._operator_request("system_update", payload.request_id, {"what": ""})
