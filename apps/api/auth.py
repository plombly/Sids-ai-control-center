"""Sign-in for the dashboard: one administrator account, sessions, audit log.

LAIka is only ever reached over your LAN or VPN, but other people and other
programs on that network must not be able to drive it. So:

- Until the administrator account exists, everything except health, sign-in
  state and first-run setup answers 401 {"setup_required": true}. The
  account is created in the web setup with a one-time setup code that only
  the host can produce (scripts/laika-setup-code.py).
- Afterwards every request needs one of: a session cookie (dashboard), a
  device key (phones, apps/api/device_routes.py) or the operator token
  (host tools). Devices may only make the safe writes.
- Passwords are stored as scrypt hashes; sessions as SHA-256 of a random
  token in an HttpOnly, SameSite=Strict cookie; writes from another origin
  are refused; repeated failed sign-ins from one address are slowed down.
- Every write is recorded in the audit log (laika:audit, newest first).

Redis: laika:auth:admin (hash), laika:sessions:<sha256> (hash, TTL),
laika:session-ids (set), laika:auth:fails:<ip> (counter, TTL),
laika:setup:code (sha256 of the code, TTL), laika:audit (list, capped).
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter()
COOKIE = "laika_session"
SESSION_DAYS = 30
FAILS_ALLOWED = 10
FAILS_WINDOW = 900
AUDIT_KEEP = 5000
PUBLIC = {"/health", "/api/auth/state", "/api/auth/login", "/api/setup/state", "/api/setup/admin"}
MIN_PASSWORD = 10


def _main():
    import main
    return main


def setup_lock_enabled():
    return os.environ.get("LAIKA_SETUP_LOCK", "1") != "0"


# --- passwords ------------------------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(check.hex(), digest)


def admin(redis):
    return redis.hgetall("laika:auth:admin") or {}


def create_admin(redis, username, password):
    redis.hset("laika:auth:admin", mapping={"username": username, "password": hash_password(password),
                                            "created_at": str(time.time())})


# --- sessions -------------------------------------------------------------------------------

def _sid(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(redis, request, response, username):
    token = secrets.token_urlsafe(32)
    sid = _sid(token)
    now = time.time()
    redis.hset(f"laika:sessions:{sid}", mapping={
        "user": username, "created_at": str(now), "last_seen": str(now),
        "ip": client_ip(request)[:64], "agent": (request.headers.get("user-agent") or "")[:200]})
    redis.expire(f"laika:sessions:{sid}", SESSION_DAYS * 86400)
    redis.sadd("laika:session-ids", sid)
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="strict", path="/")
    return sid


def session_for(redis, request):
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 100:
        return None
    sid = _sid(token)
    record = redis.hgetall(f"laika:sessions:{sid}") or {}
    if not record:
        return None
    now = time.time()
    if now - float(record.get("last_seen") or 0) > 60:
        redis.hset(f"laika:sessions:{sid}", "last_seen", str(now))
        redis.expire(f"laika:sessions:{sid}", SESSION_DAYS * 86400)
    return {**record, "id": sid}


def client_ip(request):
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


# --- audit ----------------------------------------------------------------------------------

def audit(redis, request, status, actor):
    entry = {"at": time.time(), "method": request.method, "path": request.url.path[:300], "status": status,
             "actor": actor, "ip": client_ip(request)[:64]}
    try:
        redis.lpush("laika:audit", json.dumps(entry))
        redis.ltrim("laika:audit", 0, AUDIT_KEEP - 1)
    except Exception:
        pass


def same_origin(request):
    """Writes from a browser must come from the dashboard itself."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # not a browser cross-site request (CLI, phone app)
    host = request.headers.get("host", "")
    return origin.split("://", 1)[-1].rstrip("/") == host


# --- endpoints ------------------------------------------------------------------------------

class Login(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=MIN_PASSWORD, max_length=200)


@router.get("/api/auth/state")
def auth_state(request: Request):
    redis = _main().redis
    account = admin(redis)
    session = session_for(redis, request) if account else None
    return {"setup_required": not account and setup_lock_enabled(), "admin_exists": bool(account),
            "signed_in": bool(session), "user": session.get("user") if session else None}


@router.post("/api/auth/login")
def login(payload: Login, request: Request, response: Response):
    redis = _main().redis
    ip = client_ip(request)
    fails_key = f"laika:auth:fails:{ip}"
    if int(redis.get(fails_key) or 0) >= FAILS_ALLOWED:
        raise HTTPException(status_code=429, detail="Too many failed sign-ins from this address. Try again in 15 minutes.")
    account = admin(redis)
    ok = bool(account) and hmac.compare_digest(payload.username.strip(), account.get("username", "")) \
        and verify_password(payload.password, account.get("password", ""))
    if not ok:
        redis.incr(fails_key)
        redis.expire(fails_key, FAILS_WINDOW)
        audit(redis, request, 401, f"sign-in failed: {payload.username.strip()[:40]}")
        raise HTTPException(status_code=401, detail="Wrong username or password")
    redis.delete(fails_key)
    new_session(redis, request, response, account["username"])
    audit(redis, request, 200, f"user {account['username']} signed in")
    return {"signed_in": True, "user": account["username"]}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    redis = _main().redis
    session = session_for(redis, request)
    if session:
        redis.delete(f"laika:sessions:{session['id']}")
        redis.srem("laika:session-ids", session["id"])
    response.delete_cookie(COOKIE, path="/")
    return {"signed_in": False}


@router.post("/api/auth/password")
def change_password(payload: PasswordChange, request: Request):
    redis = _main().redis
    account = admin(redis)
    if not account or not verify_password(payload.current, account.get("password", "")):
        raise HTTPException(status_code=403, detail="Current password is wrong")
    redis.hset("laika:auth:admin", "password", hash_password(payload.new))
    current = session_for(redis, request)
    for sid in list(redis.smembers("laika:session-ids") or []):  # sign out everywhere else
        if not current or sid != current["id"]:
            redis.delete(f"laika:sessions:{sid}")
            redis.srem("laika:session-ids", sid)
    return {"changed": True}


@router.get("/api/auth/sessions")
def sessions(request: Request):
    redis = _main().redis
    current = session_for(redis, request)
    items = []
    for sid in sorted(redis.smembers("laika:session-ids") or []):
        record = redis.hgetall(f"laika:sessions:{sid}") or {}
        if not record:
            redis.srem("laika:session-ids", sid)
            continue
        items.append({"id": sid[:16], "user": record.get("user"), "ip": record.get("ip"), "agent": record.get("agent"),
                      "created_at": float(record.get("created_at") or 0), "last_seen": float(record.get("last_seen") or 0),
                      "current": bool(current and current["id"] == sid)})
    return {"sessions": sorted(items, key=lambda item: -item["last_seen"])}


@router.delete("/api/auth/sessions/{short_id}")
def end_session(short_id: str):
    redis = _main().redis
    for sid in list(redis.smembers("laika:session-ids") or []):
        if sid.startswith(short_id) and len(short_id) == 16:
            redis.delete(f"laika:sessions:{sid}")
            redis.srem("laika:session-ids", sid)
            return {"ended": True}
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/audit")
def audit_log(limit: int = 100):
    redis = _main().redis
    limit = max(1, min(int(limit), 1000))
    entries = []
    for raw in redis.lrange("laika:audit", 0, limit - 1) or []:
        try:
            entries.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return {"entries": entries}


# --- first-run setup: the administrator account ------------------------------------------------

class AdminSetup(BaseModel):
    code: str = Field(min_length=4, max_length=40)
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


def setup_code_ok(redis, code):
    stored = redis.get("laika:setup:code") or ""
    given = hashlib.sha256(code.strip().upper().replace("-", "").encode()).hexdigest()
    return bool(stored) and hmac.compare_digest(stored, given)


@router.get("/api/setup/state")
def setup_state():
    redis = _main().redis
    return {"admin_exists": bool(admin(redis)), "code_issued": bool(redis.get("laika:setup:code"))}


@router.post("/api/setup/admin")
def setup_admin(payload: AdminSetup, request: Request, response: Response):
    redis = _main().redis
    if admin(redis):
        raise HTTPException(status_code=409, detail="The administrator account already exists; sign in instead")
    ip = client_ip(request)
    fails_key = f"laika:auth:fails:{ip}"
    if int(redis.get(fails_key) or 0) >= FAILS_ALLOWED:
        raise HTTPException(status_code=429, detail="Too many wrong codes from this address. Try again in 15 minutes.")
    if not setup_code_ok(redis, payload.code):
        redis.incr(fails_key)
        redis.expire(fails_key, FAILS_WINDOW)
        raise HTTPException(status_code=403, detail="Wrong or expired setup code. Run 'sudo laika setup-code' on the server for a new one.")
    create_admin(redis, payload.username, payload.password)
    redis.delete("laika:setup:code")
    new_session(redis, request, response, payload.username)
    audit(redis, request, 200, f"administrator {payload.username} created")
    return {"signed_in": True, "user": payload.username}


# --- the gate every request passes (called from main's middleware) ----------------------------

def actor_for(request, token_ok, device):
    """(actor description, error status or 0, error body)."""
    redis = _main().redis
    path = request.url.path
    if path in PUBLIC or path.startswith("/docs") or path == "/openapi.json":
        return "public", 0, None
    try:
        account_exists = bool(admin(redis))
    except Exception:
        if not setup_lock_enabled():
            return "open", 0, None
        return "", 503, {"detail": "LAIka's database is not reachable"}
    if not account_exists:
        if setup_lock_enabled():
            return "", 401, {"detail": "Set up LAIka first", "setup_required": True}
        return "open", 0, None  # tests / explicitly unlocked installs
    if token_ok:
        return "operator token", 0, None
    if device is not None:
        return f"device {device.get('name')}", 0, None
    session = session_for(redis, request)
    if session:
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
            return "", 403, {"detail": "Request from another site refused"}
        return f"user {session.get('user')}", 0, None
    return "", 401, {"detail": "Sign in required", "signed_in": False}
