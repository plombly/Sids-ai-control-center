"""AI providers for the web setup and Settings → AI & pipeline.

Status and sign-in progress come from the host (scripts/laika-providers.py,
run by the operator service); this API only shows them and passes the
operator's requests on. API keys are write-only: saved to the root-only
providers.env (mounted at /providers), never returned, never put in Redis.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import project_routes as projects

router = APIRouter()
PROVIDERS_DIR = Path(os.environ.get("LAIKA_PROVIDERS_DIR", "/providers"))
KEYS = {"anthropic_api_key": "ANTHROPIC_API_KEY", "openai_api_key": "OPENAI_API_KEY"}
STALE_SECONDS = 600


def _redis():
    return projects._redis().redis


def _json(key):
    try:
        value = json.loads(_redis().get(key) or "null")
    except (TypeError, ValueError):
        return None
    return value


def saved_keys(base=None):
    path = Path(base or PROVIDERS_DIR) / "providers.env"
    names = set()
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and value.strip():
                names.add(key.strip())
    except OSError:
        pass
    return names


def save_keys(changes, base=None):
    """Merge {ENV_NAME: value} into providers.env (root-only, atomic); an
    empty value removes the key."""
    folder = Path(base or PROVIDERS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "providers.env"
    current = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                current[key.strip()] = value.strip()
    except OSError:
        pass
    for key, value in changes.items():
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    body = "# AI provider keys for LAIka. Root-only; written from Settings.\n" + "".join(f"{k}={v}\n" for k, v in sorted(current.items()))
    handle = tempfile.NamedTemporaryFile("w", dir=folder, prefix=".providers-", delete=False)
    try:
        os.chmod(handle.name, 0o600)
        handle.write(body)
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


class ProviderKeys(BaseModel):
    anthropic_api_key: Optional[str] = Field(default=None, max_length=300, pattern=r"^$|^sk-ant-[A-Za-z0-9_-]{20,}$")
    openai_api_key: Optional[str] = Field(default=None, max_length=300, pattern=r"^$|^sk-[A-Za-z0-9_-]{20,}$")


class ProviderRequest(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


class ClaudeCode(BaseModel):
    code: str = Field(min_length=4, max_length=400, pattern=r"^[A-Za-z0-9#_.~-]+$")


@router.get("/api/ai-providers")
def providers():
    status = _json("laika:providers:status") or {}
    keys = saved_keys()
    # Older than 10 minutes (or never checked): the page asks the host to look again.
    stale = time.time() - float(status.get("checked_at") or 0) > STALE_SECONDS
    return {"status": status, "stale": stale,
            "login": {name: _json(f"laika:provider-login:{name}") for name in ("claude", "codex")},
            "keys": {field: env in keys for field, env in KEYS.items()}}


@router.post("/api/ai-providers/refresh", status_code=202)
def refresh(payload: ProviderRequest):
    return projects._operator_request("provider_status", payload.request_id, {"what": ""})


@router.post("/api/ai-providers/{provider}/login", status_code=202)
def login(provider: str, payload: ProviderRequest):
    if provider not in ("claude", "codex"):
        raise HTTPException(status_code=404, detail="Unknown provider")
    _redis().delete(f"laika:provider-login:{provider}")
    return projects._operator_request("provider_login", payload.request_id, {"what": provider})


@router.post("/api/ai-providers/claude/code")
def claude_code(payload: ClaudeCode):
    progress = _json("laika:provider-login:claude") or {}
    if progress.get("state") != "waiting":
        raise HTTPException(status_code=409, detail="Start the Claude sign-in first")
    _redis().set("laika:provider-login:claude:code", payload.code.strip(), ex=600)
    return {"sent": True}


@router.put("/api/ai-providers/keys")
def put_keys(payload: ProviderKeys):
    changes = {KEYS[field]: value.strip() for field, value in payload.model_dump(exclude_none=True).items()}
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to save")
    save_keys(changes)
    return {"keys": {field: env in saved_keys() for field, env in KEYS.items()}}
