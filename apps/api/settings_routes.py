"""Settings pages: read and change apps/api/settings_schema.py values.

Saving stores the values at once. Live settings (appearance, general) take
effect immediately; the rest are listed as "waiting to be applied" until the
operator presses Apply, which asks the host operator service to restart
LAIka's services safely and, for worker count and schedules, change the
host (scripts/laika-system.py).
"""

from typing import Any, Dict, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import project_routes as projects
import settings_schema

router = APIRouter()
PENDING = "laika:settings:pending"


class SettingsChange(BaseModel):
    changes: Dict[str, Any] = Field(default_factory=dict)


class SettingsApply(BaseModel):
    what: Literal["apply", "restart", "timers"] = "apply"
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _redis():
    return projects._redis().redis


def _view():
    redis = _redis()
    pending = sorted(redis.smembers(PENDING) or [])
    return {**settings_schema.public(), "values": settings_schema.values(redis), "pending": pending}


@router.get("/api/settings")
def get_settings():
    return _view()


@router.put("/api/settings")
def put_settings(payload: SettingsChange):
    if not payload.changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    cleaned, errors = settings_schema.validate(payload.changes)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    needs = settings_schema.save(_redis(), cleaned)
    if needs:
        _redis().sadd(PENDING, *needs)
    return _view()


@router.post("/api/settings/apply", status_code=202)
def apply_settings(payload: SettingsApply):
    result = projects._operator_request("apply_settings", payload.request_id, {"what": payload.what})
    if payload.what == "apply":
        _redis().delete(PENDING)
    return result
