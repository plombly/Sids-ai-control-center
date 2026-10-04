"""Remote access through Tailscale (administrators; scripts/laika-remote.py
on the host does the work through the operator service).

GET  /api/remote          status, install progress and the sign-in link
POST /api/remote/{action} refresh | install | login | logout  {request_id}
"""

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import project_routes as projects

router = APIRouter()
STALE_SECONDS = 120
ACTIONS = {"refresh": "remote_status", "install": "remote_install", "login": "remote_login", "logout": "remote_logout"}


def _json(key):
    try:
        return json.loads(projects._redis().redis.get(key) or "null")
    except ValueError:
        return None


class RemoteRequest(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


@router.get("/api/remote")
def remote():
    status = _json("laika:remote:status") or {}
    return {"status": status, "stale": time.time() - float(status.get("checked_at") or 0) > STALE_SECONDS,
            "progress": _json("laika:remote:progress"), "login": _json("laika:remote:login")}


@router.post("/api/remote/{action}", status_code=202)
def remote_action(action: str, payload: RemoteRequest):
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown action")
    if action == "login":
        projects._redis().redis.delete("laika:remote:login")
    return projects._operator_request(ACTIONS[action], payload.request_id, {"what": ""})
