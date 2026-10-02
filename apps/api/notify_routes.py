"""Notification settings for the dashboard (apps/api/notify_core.py).

Targets are write-only: the page shows whether a Discord webhook / ntfy
topic is set (and its last 4 characters), never the value. The target file
is the host's /etc/laika/notify/notify.env, mounted here at /notify.
"""

import json
import re
import time
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import digest
import notify_core
import project_routes as projects

router = APIRouter()

DISCORD = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_-]+$")
NTFY = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9_-]{4,64}$")


def _redis():
    import main
    return main.redis


@router.get("/api/notifications")
def get_notifications():
    return {"targets": notify_core.masked(notify_core.load_targets()),
            "settings": notify_core.load_settings(_redis()),
            "events": [{"type": name, "label": label, "urgent": urgent}
                       for name, (label, _, urgent) in notify_core.EVENTS.items()],
            "days": list(notify_core.DAYS)}


class Settings(BaseModel):
    events: Dict[str, str] = Field(default_factory=dict)
    quiet: Dict[str, object] = Field(default_factory=dict)
    digest: Dict[str, str] = Field(default_factory=dict)


@router.put("/api/notifications/settings")
def put_settings(payload: Settings):
    return {"settings": notify_core.save_settings(_redis(), payload.model_dump())}


class Targets(BaseModel):
    discord_webhook: Optional[str] = Field(default=None, max_length=300)
    discord_mention: Optional[str] = Field(default=None, max_length=30)
    ntfy_url: Optional[str] = Field(default=None, max_length=200)
    dashboard_url: Optional[str] = Field(default=None, max_length=200)


@router.put("/api/notifications/targets")
def put_targets(payload: Targets):
    changes = {}
    if payload.discord_webhook is not None:
        if payload.discord_webhook and not DISCORD.fullmatch(payload.discord_webhook.strip()):
            raise HTTPException(status_code=422, detail="That is not a Discord webhook URL")
        changes["DISCORD_WEBHOOK"] = payload.discord_webhook.strip()
    if payload.discord_mention is not None:
        if payload.discord_mention and not payload.discord_mention.strip().isdigit():
            raise HTTPException(status_code=422, detail="The Discord user ID is a number (Developer Mode → Copy User ID)")
        changes["DISCORD_MENTION"] = payload.discord_mention.strip()
    if payload.ntfy_url is not None:
        if payload.ntfy_url and not NTFY.fullmatch(payload.ntfy_url.strip()):
            raise HTTPException(status_code=422, detail="Use the full topic address, e.g. https://ntfy.sh/your-topic")
        changes["NTFY_URL"] = payload.ntfy_url.strip()
    if payload.dashboard_url is not None:
        if payload.dashboard_url and not re.fullmatch(r"https?://\S+", payload.dashboard_url.strip()):
            raise HTTPException(status_code=422, detail="The dashboard address starts with http:// or https://")
        changes["DASHBOARD_URL"] = payload.dashboard_url.strip()
    notify_core.save_targets(changes)
    return {"targets": notify_core.masked(notify_core.load_targets())}


@router.post("/api/notifications/test")
def send_test():
    targets = notify_core.load_targets()
    if not (targets.get("DISCORD_WEBHOOK") or targets.get("NTFY_URL")):
        raise HTTPException(status_code=409, detail="Add a Discord webhook or an ntfy topic first")
    delivered = notify_core.send(targets, "LAIka test notification",
                                 "This is how LAIka reaches you. Events set to 'post + ping' mention you like this.",
                                 targets.get("DASHBOARD_URL", ""), "ping")
    if not delivered:
        raise HTTPException(status_code=502, detail="Discord / ntfy did not accept the message; check the address")
    return {"sent": delivered}


def _digest_data():
    import main
    hashes = lambda pattern: {key.split(":", 2)[2]: data for key, data in main._hashes(pattern)}
    redis = main.redis
    ids = (["laika"] if projects.BUILTIN_PROJECT else []) + sorted(i for i in projects._members(main) if i != "laika")
    project_list, parents = [], {}
    for project_id in ids:
        data = projects._project_data(project_id)
        if project_id != "laika" and (data.get("status") or "active") != "active":
            continue
        project_list.append((project_id, "LAIka" if project_id == "laika" else data.get("name") or project_id))
        parents[project_id] = data.get("parent") or ""
    load = lambda key: json.loads(redis.get(key) or "null")
    return {"projects": project_list, "parents": parents, "goals": hashes("laika:goals:*"), "jobs": hashes("laika:jobs:*"),
            "events": {pid: redis.lrange(f"laika:events:{pid}", 0, 499) or [] for pid, _ in project_list},
            "apps": {pid: redis.hgetall(f"laika:app-status:{pid}") or {} for pid, _ in project_list},
            "backup": load("laika:backup:last"), "restore": load("laika:backup:restore-check")}


@router.get("/api/notifications/digest-preview")
def digest_preview():
    title, text = digest.build(time.time(), dashboard=notify_core.load_targets().get("DASHBOARD_URL", ""),
                               **_digest_data())
    return {"title": title, "text": text}
