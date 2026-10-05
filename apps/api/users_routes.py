"""People who use this LAIka (administrators only, except invites).

GET    /api/users                  everyone, their role and project access
POST   /api/users                  {username, role, access} -> an invite link
PATCH  /api/users/{name}           {role?, access?, disabled?}
POST   /api/users/{name}/invite    a new invite link (sign-in reset: old
                                   password and sessions end)
DELETE /api/users/{name}           remove (never yourself, never the last
                                   administrator)
GET    /api/invites/{token}        public: whose invite, still valid?
POST   /api/invites/{token}        public: {password} -> account ready, signed in
GET    /api/me                     the signed-in person and what they may do
GET    /api/sftp                   how to reach the SFTP server (address, port, host key)
GET    /api/me/ssh-keys            the signed-in person's SSH keys for SFTP
POST   /api/me/ssh-keys            {name, key}: add one (OpenSSH public key)
DELETE /api/me/ssh-keys/{id}       remove one

Invites: a random token shown once to the administrator (laika:invites:
<sha256> -> user, 24 h). The person sets their own password; nobody else
ever knows it. A new invite or a reset makes older ones invalid.
"""

import hashlib
import secrets
import time
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

import access
import auth

router = APIRouter()
INVITE_HOURS = 24


def _redis():
    import main
    return main.redis


def _ctx(request):
    return getattr(request.state, "ctx", None)


def _public(redis, user):
    sessions = [redis.hgetall(f"laika:sessions:{sid}") or {} for sid in redis.smembers("laika:session-ids") or []]
    seen = [float(s.get("last_seen") or 0) for s in sessions if (s.get("user") or "").lower() == user["name"]]
    return {"name": user["name"], "username": user.get("username") or user["name"], "role": user.get("role") or "member",
            "access": user.get("access") or {}, "disabled": user["disabled"], "invited": not user.get("password"),
            "created_at": float(user.get("created_at") or 0), "last_seen": max(seen) if seen else 0,
            "spending": _spending(user)}


def _limits_off():
    import main
    import spending
    return spending.policy(main.redis) == "off"


def _spending(user):
    try:
        import project_routes
        return project_routes.person_spending(user["name"])
    except Exception:  # a convenience on the Users page
        return None


def _check_access(redis, grants):
    import managed
    known = set(redis.smembers("laika:projects") or [])
    for project, level in (grants or {}).items():
        if project not in known or managed.view_only(redis, project):
            raise HTTPException(status_code=422, detail=f"Unknown project: {project}")
        if level not in access.LEVELS:
            raise HTTPException(status_code=422, detail=f"Access must be one of {', '.join(access.LEVELS)}")
    return dict(grants or {})


def _invite(redis, name, created_by):
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    old = redis.hget(access.user_key(name), "invite")
    if old:
        redis.delete(f"laika:invites:{old}")
    redis.hset(f"laika:invites:{digest}", mapping={"user": name, "created_by": created_by, "created_at": str(time.time())})
    redis.expire(f"laika:invites:{digest}", INVITE_HOURS * 3600)
    access.save_user(redis, name, invite=digest)
    return {"token": token, "path": f"#/invite/{token}", "expires_at": time.time() + INVITE_HOURS * 3600}


def _not_last_admin(redis, name):
    if [u for u in access.active_admins(redis) if u["name"] != name] == []:
        raise HTTPException(status_code=409, detail="LAIka needs at least one administrator who can sign in")


class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[A-Za-z0-9._-]{2,40}$")
    role: str = Field(default="member", pattern=r"^(admin|member)$")
    access: Dict[str, str] = Field(default_factory=dict, max_length=200)


class UserChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Optional[str] = Field(default=None, pattern=r"^(admin|member)$")
    access: Optional[Dict[str, str]] = Field(default=None, max_length=200)
    disabled: Optional[bool] = None
    budget_usd: Optional[float] = Field(default=None, ge=0, le=1000000)  # monthly, 0 = no limit
    budget_mode: Optional[str] = Field(default=None, pattern=r"^(block|warn)$")


@router.get("/api/users")
def list_users():
    redis = _redis()
    access.migrate(redis)
    return {"users": [_public(redis, u) for u in access.all_users(redis)], "levels": list(access.LEVELS)}


@router.post("/api/users", status_code=201)
def add_user(payload: NewUser, request: Request):
    redis = _redis()
    name = payload.username.lower()
    if access.get_user(redis, name):
        raise HTTPException(status_code=409, detail="That username is taken")
    grants = _check_access(redis, payload.access)
    access.save_user(redis, name, username=payload.username, password="", role=payload.role, access=grants,
                     created_at=time.time(), created_by=_ctx(request).name)
    invite = _invite(redis, name, _ctx(request).name)
    return {"user": _public(redis, access.get_user(redis, name)), "invite": invite}


@router.patch("/api/users/{name}")
def change_user(name: str, payload: UserChange, request: Request):
    redis = _redis()
    user = access.get_user(redis, name)
    if not user:
        raise HTTPException(status_code=404, detail="No such user")
    demoting = payload.role == "member" and user.get("role") == "admin"
    if (demoting or payload.disabled) and user.get("role") == "admin":
        _not_last_admin(redis, user["name"])
    fields = {}
    if payload.role is not None:
        fields["role"] = payload.role
    if payload.access is not None:
        fields["access"] = _check_access(redis, payload.access)
    if payload.disabled is not None:
        fields["disabled"] = "1" if payload.disabled else ""
    if (payload.budget_usd is not None or payload.budget_mode is not None) and _limits_off():
        raise HTTPException(status_code=409, detail="Spending limits are turned off (Settings -> AI & pipeline)")
    if payload.budget_usd is not None:
        fields["budget_usd"] = f"{payload.budget_usd:g}"
    if payload.budget_mode is not None:
        fields["budget_mode"] = payload.budget_mode
    access.save_user(redis, user["name"], **fields)
    if payload.disabled:
        auth.end_sessions(redis, user=user["name"])
    return {"user": _public(redis, access.get_user(redis, user["name"]))}


@router.post("/api/users/{name}/invite")
def reinvite(name: str, request: Request):
    """A new invite link: the old password stops working and every session ends."""
    redis = _redis()
    user = access.get_user(redis, name)
    if not user:
        raise HTTPException(status_code=404, detail="No such user")
    if user.get("role") == "admin" and user.get("password"):
        _not_last_admin(redis, user["name"])
    access.save_user(redis, user["name"], password="")
    auth.end_sessions(redis, user=user["name"])
    return {"user": _public(redis, access.get_user(redis, user["name"])), "invite": _invite(redis, user["name"], _ctx(request).name)}


@router.delete("/api/users/{name}")
def remove_user(name: str, request: Request):
    redis = _redis()
    user = access.get_user(redis, name)
    if not user:
        raise HTTPException(status_code=404, detail="No such user")
    if user["name"] == _ctx(request).name:
        raise HTTPException(status_code=409, detail="You cannot remove yourself")
    if user.get("role") == "admin":
        _not_last_admin(redis, user["name"])
    auth.end_sessions(redis, user=user["name"])
    if user.get("invite"):
        redis.delete(f"laika:invites:{user['invite']}")
    for device_id in list(redis.smembers("laika:devices") or []):  # their phones stop working
        if (redis.hget(f"laika:devices:{device_id}", "owner") or "") == user["name"]:
            redis.hset(f"laika:devices:{device_id}", "revoked_at", str(time.time()))
    import notify_core  # their own notifications go with them
    notify_core.remove_person(user["name"])
    for key in (notify_core.settings_key(user["name"]), f"laika:notify:sent:{user['name']}",
                f"laika:notify:initialized:{user['name']}", f"laika:digest:last:{user['name']}"):
        redis.delete(key)
    redis.delete(access.user_key(user["name"]))
    redis.srem(access.USERS, user["name"])
    return {"removed": user["name"]}


def _invite_user(redis, token):
    if not token or len(token) > 100:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    name = redis.hget(f"laika:invites:{digest}", "user")
    user = access.get_user(redis, name) if name else None
    if not user or user.get("invite") != digest or user["disabled"]:
        return None
    return user, digest


@router.get("/api/invites/{token}")
def invite_state(token: str):
    found = _invite_user(_redis(), token)
    if not found:
        return {"valid": False}
    return {"valid": True, "username": found[0].get("username")}


class AcceptInvite(BaseModel):
    password: str = Field(min_length=auth.MIN_PASSWORD, max_length=200)


@router.post("/api/invites/{token}")
def accept_invite(token: str, payload: AcceptInvite, request: Request, response: Response):
    redis = _redis()
    found = _invite_user(redis, token)
    if not found:
        raise HTTPException(status_code=404, detail="This invite link is used up or expired. Ask for a new one.")
    user, digest = found
    access.save_user(redis, user["name"], password=auth.hash_password(payload.password), invite="")
    redis.delete(f"laika:invites:{digest}")
    auth.new_session(redis, request, response, user["name"])
    auth.audit(redis, request, 200, f"user {user['name']} accepted their invite")
    return {"signed_in": True, "user": user.get("username")}


SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                 "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
MAX_SSH_KEYS = 10


def parse_ssh_key(text):
    """(type, base64) of an OpenSSH public key line, or ValueError."""
    import base64
    import struct
    parts = (text or "").strip().split()
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES or len(parts[1]) > 4096:
        raise ValueError("Paste an OpenSSH public key (the .pub file: it starts with ssh-ed25519 or ssh-rsa)")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        length = struct.unpack(">I", blob[:4])[0]
        inner = blob[4:4 + length].decode()
    except Exception:
        raise ValueError("That key is damaged: copy the whole line from the .pub file")
    if inner != parts[0]:
        raise ValueError("That key is damaged: its type does not match")
    if "PRIVATE" in text.upper():
        raise ValueError("That is a private key: never share it. Paste the .pub file instead")
    return parts[0], parts[1]


def _key_fingerprint(b64):
    import base64
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("=")


def _my_account(request):
    ctx = _ctx(request)
    user = access.get_user(_redis(), ctx.name) if ctx is not None else None
    if not user:
        raise HTTPException(status_code=409, detail="Sign in with your own account to manage your keys")
    return user


def _keys(user):
    import json
    try:
        keys = json.loads(user.get("ssh_keys") or "[]")
    except ValueError:
        keys = []
    return keys if isinstance(keys, list) else []


@router.get("/api/sftp")
def sftp_info(request: Request):
    import json
    ctx = _ctx(request)
    try:
        info = json.loads(_redis().get("laika:sftp:info") or "null") or {}
    except ValueError:
        info = {}
    return {"online": bool(info), "enabled": bool(info.get("enabled")), "port": info.get("port") or 2222,
            "fingerprint": info.get("fingerprint", ""), "commit_seconds": info.get("commit_seconds") or 30,
            "username": getattr(ctx, "display", "") if access.get_user(_redis(), getattr(ctx, "name", "")) else ""}


@router.get("/api/me/ssh-keys")
def my_ssh_keys(request: Request):
    user = _my_account(request)
    return {"keys": [{k: item.get(k) for k in ("id", "name", "type", "fingerprint", "added")} for item in _keys(user)]}


class NewKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    key: str = Field(min_length=20, max_length=8000)


@router.post("/api/me/ssh-keys", status_code=201)
def add_ssh_key(payload: NewKey, request: Request):
    import json
    user = _my_account(request)
    try:
        kind, b64 = parse_ssh_key(payload.key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    keys = _keys(user)
    if len(keys) >= MAX_SSH_KEYS:
        raise HTTPException(status_code=409, detail=f"At most {MAX_SSH_KEYS} keys; remove one first")
    if any(item.get("key", "").split()[1:2] == [b64] for item in keys):
        raise HTTPException(status_code=409, detail="You added this key already")
    item = {"id": secrets.token_hex(6), "name": payload.name.strip(), "type": kind, "key": f"{kind} {b64}",
            "fingerprint": _key_fingerprint(b64), "added": time.time()}
    access.save_user(_redis(), user["name"], ssh_keys=json.dumps(keys + [item]))
    auth.audit(_redis(), request, 201, f"user {user['name']} added SSH key {item['fingerprint']}")
    return {k: item[k] for k in ("id", "name", "type", "fingerprint", "added")}


@router.delete("/api/me/ssh-keys/{key_id}")
def remove_ssh_key(key_id: str, request: Request):
    import json
    user = _my_account(request)
    keys = _keys(user)
    kept = [item for item in keys if item.get("id") != key_id]
    if len(kept) == len(keys):
        raise HTTPException(status_code=404, detail="No such key")
    access.save_user(_redis(), user["name"], ssh_keys=json.dumps(kept))
    return {"removed": key_id}


@router.get("/api/me")
def me(request: Request):
    ctx = _ctx(request)
    redis = _redis()
    levels = access.levels(redis, ctx)
    return {"name": ctx.name, "username": ctx.display, "role": ctx.role, "admin": ctx.is_admin,
            "projects": None if levels is None else {pid: access.level_name(level) for pid, level in levels.items()}}
