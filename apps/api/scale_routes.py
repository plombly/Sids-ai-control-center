"""Worker scaling for the dashboard (services/scaler/laika_scaler.py does
the work on the host; this API only reads its state and moves the target).

GET  /api/workers/scale            state, recent decisions
POST /api/workers/scale {delta}    one more / one fewer worker. With
     automatic scaling on this sets the target and pauses automatic
     decisions for MANUAL_HOLD seconds; with it off it changes the fixed
     number of workers (Settings → Workers).
"""

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import project_routes as projects
import settings_schema

router = APIRouter()
MANUAL_HOLD = 600
UNIT_LIMIT = 32


def _redis():
    return projects._redis().redis


def _state(r):
    try:
        return json.loads(r.get("laika:scaler:state") or "null")
    except (TypeError, ValueError):
        return None


@router.get("/api/workers/scale")
def scale_state():
    r = _redis()
    log = []
    for raw in r.lrange("laika:scaler:log", 0, 19):
        try:
            log.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return {"state": _state(r), "log": log, "hold": r.get("laika:scaler:hold") or ""}


class Scale(BaseModel):
    delta: int = Field(ge=-1, le=1)


@router.post("/api/workers/scale")
def scale(payload: Scale):
    if payload.delta == 0:
        raise HTTPException(status_code=422, detail="delta must be 1 or -1")
    r = _redis()
    state = _state(r)
    if not state:
        raise HTTPException(status_code=503, detail="The worker scaler is not running (laika-scaler.service)")
    target = int(r.get("laika:scaler:target") or state.get("target") or 1)
    if state.get("auto"):
        new = max(1, min(int(state.get("max") or UNIT_LIMIT), target + payload.delta))
        if new == target:
            limit = "most" if payload.delta > 0 else "fewest"
            raise HTTPException(status_code=409, detail=f"Already at the {limit} workers allowed (Settings → Workers)")
        r.set("laika:scaler:manual-until", str(time.time() + MANUAL_HOLD))
    else:
        new = max(1, min(UNIT_LIMIT, int(state.get("fixed") or target) + payload.delta))
        cleaned, errors = settings_schema.validate({"WORKER_COUNT": new})
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        settings_schema.save(r, cleaned)
    r.set("laika:scaler:target", str(new))
    r.lpush("laika:scaler:log", json.dumps({"at": time.time(), "target": new,
                                            "reason": f"{'added' if payload.delta > 0 else 'removed'} by hand"}))
    r.ltrim("laika:scaler:log", 0, 99)
    return {"target": new, "auto": bool(state.get("auto"))}
