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


def saved_keys(base=None, name="providers.env"):
    path = Path(base or PROVIDERS_DIR) / name
    names = set()
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and value.strip():
                names.add(key.strip())
    except OSError:
        pass
    return names


def save_keys(changes, base=None, name="providers.env"):
    """Merge {ENV_NAME: value} into providers.env, or accounts.env for extra
    accounts' keys (root-only, atomic); an empty value removes the key."""
    folder = Path(base or PROVIDERS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
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
            "keys": {field: env in keys for field, env in KEYS.items()},
            "holds": {name: hold(name) for name in ("claude", "codex")},
            "accounts": {name: account_rows(name) for name in ("claude", "codex")},
            "usage": {"claude": _json("laika:provider-usage:claude")}}


def hold(provider, account=None):
    """A provider (account None: every account of it) or one account LAIka
    is not sending work to right now (services/provider_health.py):
    {held, kind, reason, until, seconds_left}."""
    main = projects._redis()
    suffix = f":{account}" if account else ""
    try:
        reason = main.redis.get(f"laika:provider-cooldown:{provider}{suffix}")
        if not reason:
            return {"held": False}
        left = main.redis.ttl(f"laika:provider-cooldown:{provider}{suffix}")
    except Exception:
        return {"held": False}
    detail = _json(f"laika:provider-hold:{provider}{suffix}") or {}
    seconds = left if isinstance(left, int) and left > 0 else 0
    return {"held": True, "kind": detail.get("kind") or "unavailable", "reason": detail.get("reason") or reason,
            "until": detail.get("until") or (time.time() + seconds), "seconds_left": seconds}


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


# --- several accounts per provider (services/provider_accounts.py) ---------------------
#
# The registry lives in Redis; credentials never do: sign-ins happen on the
# host (operator action provider_login "claude:<id>"), API keys of extra
# accounts are written to the root-only accounts.env and put in place by
# the host (provider_apply_keys), which also removes deleted accounts'.

PROVIDERS = ("claude", "codex")
DEFAULT_ACCOUNT = "default"
ACCOUNT_ID = r"^[a-z0-9][a-z0-9-]{0,23}$"
KEY_PATTERN = {"claude": r"^sk-ant-[A-Za-z0-9_-]{20,}$", "openai": r"^sk-(?!ant-)[A-Za-z0-9_-]{20,}$"}
MAX_ACCOUNTS = 20


class NewAccount(BaseModel):
    id: str = Field(pattern=ACCOUNT_ID)
    label: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern=r"^(login|key)$")
    max_concurrent: int = Field(default=2, ge=0, le=20)


class AccountChange(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=60)
    max_concurrent: Optional[int] = Field(default=None, ge=0, le=20)
    enabled: Optional[bool] = None


class AccountKey(BaseModel):
    key: str = Field(min_length=20, max_length=300, pattern=r"^sk-[A-Za-z0-9_-]{20,}$")


class AccountLogin(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
    account: str = Field(default=DEFAULT_ACCOUNT, pattern=ACCOUNT_ID)


def _provider(provider):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return provider


def default_limit(provider):
    if provider != "claude":
        return 0
    try:
        return max(1, int(os.environ.get("CLAUDE_MAX_CONCURRENT", "2")))
    except ValueError:
        return 2


def _slots_key(provider, account):
    return f"laika:provider-slots:{provider}" + ("" if account == DEFAULT_ACCOUNT else f":{account}")


def _usage_key(provider, account):
    return f"laika:provider-usage:{provider}" + ("" if account == DEFAULT_ACCOUNT else f":{account}")


def account_ids(provider):
    try:
        return sorted(_redis().smembers(f"laika:provider-accounts:{provider}") or [])
    except Exception:
        return []


def account_rows(provider, now=None):
    """Every account of provider for Settings → AI, "default" first:
    {id, label, kind, max_concurrent, enabled, running, hold, usage, signed_in, method, key_saved}."""
    now = time.time() if now is None else now
    r = _redis()
    status = _json("laika:providers:status") or {}
    signed = (status.get("accounts") or {}).get(provider) or {}
    saved = saved_keys(name="accounts.env")
    rows = []
    for account in [DEFAULT_ACCOUNT, *[a for a in account_ids(provider) if a != DEFAULT_ACCOUNT]]:
        try:
            record = r.hgetall(f"laika:provider-account:{provider}:{account}") or {}
        except Exception:
            record = {}
        if not record and account != DEFAULT_ACCOUNT:
            continue
        try:
            running = int(r.zcount(_slots_key(provider, account), now, "+inf"))
        except Exception:
            running = 0
        try:
            limit = max(0, int(record.get("max_concurrent")))
        except (TypeError, ValueError):
            limit = default_limit(provider)
        row = {"id": account, "label": record.get("label") or ("Default" if account == DEFAULT_ACCOUNT else account),
               "kind": record.get("kind") if record.get("kind") in ("login", "key") else "login",
               "max_concurrent": limit, "enabled": record.get("enabled", "1") == "1", "running": running,
               "hold": hold(provider, account), "usage": _json(_usage_key(provider, account))}
        if account == DEFAULT_ACCOUNT:
            row.update(signed_in=bool((status.get(provider) or {}).get("signed_in")),
                       method=(status.get(provider) or {}).get("method") or "")
        else:
            row.update(signed_in=bool((signed.get(account) or {}).get("signed_in")),
                       method=(signed.get(account) or {}).get("method") or "",
                       key_saved=account_key_name(provider, account) in saved)
        rows.append(row)
    return rows


def capacity(provider, now=None):
    """{free, total, unlimited} over the usable accounts (the why line, Routing)."""
    free = total = 0
    unlimited = False
    for row in account_rows(provider, now):
        if not row["enabled"] or row["hold"].get("held"):
            continue
        if not row["max_concurrent"]:
            unlimited = True
            continue
        total += row["max_concurrent"]
        free += max(0, row["max_concurrent"] - row["running"])
    return {"free": free, "total": total, "unlimited": unlimited}


def account_key_name(provider, account):
    return ("ANTHROPIC_API_KEY__" if provider == "claude" else "OPENAI_API_KEY__") + account.upper().replace("-", "_")


def _apply_on_host():
    """Ask the host to put keys in place / remove deleted accounts' credentials."""
    try:
        projects._operator_request("provider_apply_keys", f"accounts-{int(time.time() * 1000)}", {"what": ""})
        return True
    except HTTPException:
        return False


@router.get("/api/ai-providers/accounts")
def list_accounts():
    return {provider: account_rows(provider) for provider in PROVIDERS}


@router.post("/api/ai-providers/{provider}/accounts", status_code=201)
def add_account(provider: str, payload: NewAccount):
    _provider(provider)
    if payload.id == DEFAULT_ACCOUNT:
        raise HTTPException(status_code=409, detail="The default account always exists")
    ids = account_ids(provider)
    if payload.id in ids:
        raise HTTPException(status_code=409, detail=f"An account named {payload.id} exists")
    if len(ids) >= MAX_ACCOUNTS:
        raise HTTPException(status_code=409, detail=f"At most {MAX_ACCOUNTS} accounts per provider")
    r = _redis()
    r.hset(f"laika:provider-account:{provider}:{payload.id}", mapping={
        "label": payload.label.strip(), "kind": payload.kind, "max_concurrent": str(payload.max_concurrent),
        "enabled": "1", "created_at": str(time.time())})
    r.sadd(f"laika:provider-accounts:{provider}", payload.id)
    # A new account can serve: the provider is no longer paused as a whole.
    r.delete(f"laika:provider-cooldown:{provider}", f"laika:provider-hold:{provider}")
    return next(row for row in account_rows(provider) if row["id"] == payload.id)


@router.patch("/api/ai-providers/{provider}/accounts/{account}")
def change_account(provider: str, account: str, payload: AccountChange):
    _provider(provider)
    key = f"laika:provider-account:{provider}:{account}"
    r = _redis()
    if account != DEFAULT_ACCOUNT and account not in account_ids(provider):
        raise HTTPException(status_code=404, detail="Unknown account")
    changes = {}
    if payload.label is not None:
        changes["label"] = payload.label.strip()
    if payload.max_concurrent is not None:
        changes["max_concurrent"] = str(payload.max_concurrent)
    if payload.enabled is not None:
        changes["enabled"] = "1" if payload.enabled else ""
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    r.hset(key, mapping=changes)
    if payload.enabled:
        r.delete(f"laika:provider-cooldown:{provider}", f"laika:provider-hold:{provider}")
    return next(row for row in account_rows(provider) if row["id"] == account)


@router.delete("/api/ai-providers/{provider}/accounts/{account}")
def delete_account(provider: str, account: str):
    _provider(provider)
    if account == DEFAULT_ACCOUNT:
        raise HTTPException(status_code=409, detail="The default account cannot be removed; disable it instead")
    if account not in account_ids(provider):
        raise HTTPException(status_code=404, detail="Unknown account")
    r = _redis()
    r.srem(f"laika:provider-accounts:{provider}", account)
    r.delete(f"laika:provider-account:{provider}:{account}", f"laika:provider-cooldown:{provider}:{account}",
             f"laika:provider-hold:{provider}:{account}", _usage_key(provider, account))
    save_keys({account_key_name(provider, account): ""}, name="accounts.env")
    return {"deleted": account, "credentials_removed": _apply_on_host()}


@router.put("/api/ai-providers/{provider}/accounts/{account}/key")
def put_account_key(provider: str, account: str, payload: AccountKey):
    _provider(provider)
    import re
    if account == DEFAULT_ACCOUNT:
        raise HTTPException(status_code=409, detail="The default account's key is set under Accounts above")
    if account not in account_ids(provider):
        raise HTTPException(status_code=404, detail="Unknown account")
    if not re.fullmatch(KEY_PATTERN["claude" if provider == "claude" else "openai"], payload.key.strip()):
        raise HTTPException(status_code=422, detail="That does not look like a key for this provider")
    save_keys({account_key_name(provider, account): payload.key.strip()}, name="accounts.env")
    return {"key_saved": True, "applied": _apply_on_host()}


@router.post("/api/ai-providers/{provider}/accounts/{account}/login", status_code=202)
def login_account(provider: str, account: str, payload: ProviderRequest):
    _provider(provider)
    if account != DEFAULT_ACCOUNT and account not in account_ids(provider):
        raise HTTPException(status_code=404, detail="Unknown account")
    _redis().delete(f"laika:provider-login:{provider}")
    what = provider if account == DEFAULT_ACCOUNT else f"{provider}:{account}"
    return projects._operator_request("provider_login", payload.request_id, {"what": what})
