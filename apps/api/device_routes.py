"""Phones and other apps: per-device keys and the app API.

The operator adds a device in Settings ("Add a phone"); SID issues a random
key, shows it once (as a QR code the SID app scans) and stores only its
SHA-256. A request carrying `Authorization: Bearer <key>` is that device.
A device may read everything and do the safe actions in DEVICE_WRITES
(give goals, use the goal assistant, answer stuck jobs and internet
requests, request builds); it can never approve a merge, change settings
or manage devices. Keys are revoked from Settings.

Redis: sid:devices:<id> (hash: name, key_hash, created_at, last_used_at,
last_seen_from, revoked_at), sid:device-key:<sha256> -> id, set sid:devices.
"""

import hashlib
import json
import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()
API_VERSION = 1
KEY_PREFIX = "sidk_"
DEVICE_ID = re.compile(r"^[a-f0-9]{12}$")
# Writes a device key may make (method, path). Job actions are narrowed
# further to DEVICE_JOB_ACTIONS in the actions endpoint.
DEVICE_WRITES = (
    ("POST", re.compile(r"^/api/projects/[a-z0-9][a-z0-9-]{0,39}/goals$")),
    ("POST", re.compile(r"^/api/projects/[a-z0-9][a-z0-9-]{0,39}/assistant$")),
    ("POST", re.compile(r"^/api/assistant/[a-f0-9]{16}/(reply|cancel|submit)$")),
    ("POST", re.compile(r"^/api/jobs/[A-Za-z0-9_-]{1,64}/actions$")),
    ("POST", re.compile(r"^/api/projects/[a-z0-9][a-z0-9-]{0,39}/builds$")),
)
DEVICE_JOB_ACTIONS = {"extend", "reject", "network_once", "network_always", "network_deny"}
USED_EVERY = 60  # seconds between last-used updates per device


def _main():
    import main
    return main


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def device_for(request):
    """The device record of a request's bearer key, or None."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    key = header[7:].strip()
    if not key.startswith(KEY_PREFIX) or len(key) > 100:
        return None
    redis = _main().redis
    device_id = redis.get(f"sid:device-key:{key_hash(key)}")
    if not device_id:
        return None
    record = redis.hgetall(f"sid:devices:{device_id}") or {}
    if not record or record.get("revoked_at"):
        return None
    now = time.time()
    if now - float(record.get("last_used_at") or 0) > USED_EVERY:
        forwarded = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
        redis.hset(f"sid:devices:{device_id}", mapping={"last_used_at": str(now), "last_seen_from": forwarded[:64]})
    return record


def device_may_write(method, path):
    return any(method == allowed and pattern.fullmatch(path) for allowed, pattern in DEVICE_WRITES)


def _view(record):
    return {"id": record.get("id"), "name": record.get("name"), "created_at": float(record.get("created_at") or 0),
            "last_used_at": float(record.get("last_used_at") or 0) or None,
            "last_seen_from": record.get("last_seen_from") or "", "revoked": bool(record.get("revoked_at"))}


def server_name():
    return (_main().redis.get("sid:server-name") or "SID").strip()[:60] or "SID"


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60, pattern=r"^[^\r\n]+$")
    # The address the app uses to reach this API (the dashboard suggests one).
    url: str = Field(min_length=8, max_length=200, pattern=r"^https?://[^\s/]+(:\d+)?/?$")
    server_name: Optional[str] = Field(default=None, max_length=60, pattern=r"^[^\r\n]*$")


@router.get("/api/devices")
def list_devices():
    redis = _main().redis
    records = [redis.hgetall(f"sid:devices:{device_id}") or {} for device_id in sorted(redis.smembers("sid:devices") or [])]
    items = [_view(r) for r in records if r and not r.get("revoked_at")]
    return {"devices": sorted(items, key=lambda d: -d["created_at"]), "server_name": server_name()}


@router.post("/api/devices", status_code=201)
def create_device(payload: DeviceCreate, request: Request):
    if getattr(request.state, "device", None):
        raise HTTPException(status_code=403, detail="Devices cannot add devices")
    redis = _main().redis
    device_id = secrets.token_hex(6)
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    if payload.server_name and payload.server_name.strip():
        redis.set("sid:server-name", payload.server_name.strip())
    redis.hset(f"sid:devices:{device_id}", mapping={"id": device_id, "name": payload.name.strip(),
                                                    "key_hash": key_hash(key), "created_at": str(now)})
    redis.set(f"sid:device-key:{key_hash(key)}", device_id)
    redis.sadd("sid:devices", device_id)
    pairing = {"v": 1, "name": server_name(), "url": payload.url.rstrip("/"), "key": key}
    text = json.dumps(pairing, separators=(",", ":"))
    return {"device": _view(redis.hgetall(f"sid:devices:{device_id}")), "key": key, "pairing": pairing,
            "pairing_text": text, "qr_svg": _qr_svg(text)}


def _qr_svg(text):
    try:
        import segno
    except ImportError:  # the pairing text still works without the picture
        return ""
    import io
    buffer = io.BytesIO()
    segno.make(text, error="m", micro=False).save(buffer, kind="svg", scale=6, border=2, dark="#000", light="#fff",
                                                  xmldecl=False, svgns=True)
    return buffer.getvalue().decode()


@router.delete("/api/devices/{device_id}")
def revoke_device(device_id: str, request: Request):
    if getattr(request.state, "device", None):
        raise HTTPException(status_code=403, detail="Devices cannot revoke devices")
    if not DEVICE_ID.fullmatch(device_id or ""):
        raise HTTPException(status_code=404, detail="Device not found")
    redis = _main().redis
    record = redis.hgetall(f"sid:devices:{device_id}") or {}
    if not record:
        raise HTTPException(status_code=404, detail="Device not found")
    redis.delete(f"sid:device-key:{record.get('key_hash', '')}")
    redis.hset(f"sid:devices:{device_id}", mapping={"revoked_at": str(time.time())})
    return {"id": device_id, "revoked": True}


# --- the app API ---------------------------------------------------------------------------

@router.get("/api/app/info")
def app_info(request: Request):
    """What a SID app needs first: who this server is, which API version it
    speaks, and whether its key is valid."""
    main = _main()
    device = device_for(request)
    try:
        system = json.loads(main.redis.get("sid:system-info") or "{}")
    except (TypeError, ValueError):
        system = {}
    return {"server_name": server_name(), "api_version": API_VERSION, "min_api_version": 1,
            "sid_commit": str(system.get("head") or "")[:12], "server_time": time.time(),
            "device": {"id": device.get("id"), "name": device.get("name")} if device else None,
            "token_required": bool(main.OPERATOR_TOKEN),
            "device_actions": sorted(DEVICE_JOB_ACTIONS)}


@router.get("/api/app/summary")
def app_summary():
    """One call for the app's home screen: health, what needs you, what is
    in progress, projects and what finished recently."""
    import project_routes
    main = _main()
    jobs = main._all_jobs()
    goals = main._all_goals()
    health = main.api_system_health()
    report = health.get("report") or {}
    ready = [job for job in jobs if main._approval_ready(job)]
    stuck = [job for job in jobs if job.get("status") == "needs_human"]
    active = [goal for goal in goals if goal.get("status") in ("queued", "planning", "running")]
    finished = [goal for goal in goals if goal.get("status") in ("completed", "failed", "planning_failed")][:10]
    keep = ("id", "title", "project_id", "status", "needs_human_kind", "network_request_reason",
            "network_request_step", "integrated_candidate_commit", "goal_id", "sort_time")
    return {
        "server_name": server_name(),
        "health": {"status": report.get("status") or report.get("overall") or "unknown",
                   "age_seconds": report.get("age_seconds")},
        "needs_you": {"ready": [{k: job.get(k) for k in keep} for job in ready],
                      "stuck": [{k: job.get(k) for k in keep} for job in stuck]},
        "in_progress": active[:20],
        "recent": finished,
        "projects": project_routes.list_projects(),
    }
