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
            "created_at": float(user.get("created_at") or 0), "last_seen": max(seen) if seen else 0}


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


@router.get("/api/me")
def me(request: Request):
    ctx = _ctx(request)
    redis = _redis()
    levels = access.levels(redis, ctx)
    return {"name": ctx.name, "username": ctx.display, "role": ctx.role, "admin": ctx.is_admin,
            "projects": None if levels is None else {pid: access.level_name(level) for pid, level in levels.items()}}
