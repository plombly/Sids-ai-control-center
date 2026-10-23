"""LNMT (LAIka NGINx Management Terminal): the web hosting control panel
that sits next to LAIka. Once connected, the two work together:

- People sign in to LNMT with their LAIka account ("Sign in with LAIka"),
  and LAIka's accounts appear in LNMT by themselves.
- LNMT can start a LAIka project for a site ("Build with LAIka"): LAIka
  creates the project for that person, sets up its git push to the LNMT app
  and LNMT accepts LAIka's deploy key for that one app.
- A LAIka project can deploy to an LNMT app (project Settings → Deploy to
  LNMT): every approved change is pushed there and LNMT builds it.

Connecting: an LNMT administrator makes a pairing code (LNMT's address, a
token LAIka uses to call LNMT, and the fingerprint of LNMT's certificate);
a LAIka administrator pastes it here. LAIka then calls LNMT once over the
pinned certificate and hands it a token of its own for calling LAIka.

Secrets: LINK_DIR/lnmt.json (root-only /etc/laika/link on the host,
mounted into the API only): LNMT's address and certificate fingerprint,
the token LAIka sends, and the SHA-256 of the token LNMT sends. Sign-in
codes: Redis laika:link:code:<sha256> (one use, 2 minutes).

LNMT's calls arrive under /api/link/service/ (no session: the token is the
caller) and act for one LAIka user at a time, with that user's own access.
"""

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()
LINK_DIR = Path(os.environ.get("LAIKA_LINK_DIR", "/etc/laika/link"))
CODE_SECONDS = 120
SERVICE_PREFIX = "/api/link/service/"
TOKEN_PREFIX = "lnmtlink_"
_REQUEST_ID = r"^[A-Za-z0-9_-]{8,64}$"
PID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# What a person gets on a project LNMT creates for them (LNMT_PROJECT_ACCESS).
DEFAULT_ACCESS = "approve"


def _main():
    import main
    return main


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# --- the stored connection -------------------------------------------------------------------

def _path():
    return LINK_DIR / "lnmt.json"


def connection():
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("url") and data.get("token_out") and data.get("inbound_sha256") else None


def _save(data):
    LINK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, _path())


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS to LNMT that trusts exactly its (self-signed) certificate, checked
    on the connection itself before anything is sent."""

    def __init__(self, host, port, fingerprint, timeout):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the fingerprint is the check
        super().__init__(host, port, timeout=timeout, context=context)
        self.fingerprint = fingerprint

    def connect(self):
        super().connect()
        seen = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(seen, self.fingerprint):
            self.close()
            raise HTTPException(status_code=502, detail="LNMT's certificate changed: connect it again with a new pairing code")


def call_lnmt(method, path, body=None, conn=None, timeout=60):
    """A request to LNMT over its pinned certificate; returns the JSON answer."""
    conn = conn or connection()
    if not conn:
        raise HTTPException(status_code=409, detail="LNMT is not connected (Settings → Connected services)")
    match = re.fullmatch(r"https://([A-Za-z0-9.-]+)(?::(\d+))?", conn["url"].rstrip("/"))
    if not match:
        raise HTTPException(status_code=502, detail="LNMT's address must be https://host[:port]")
    connection_ = _PinnedConnection(match.group(1), int(match.group(2) or 443), conn["cert_sha256"], timeout)
    data = json.dumps(body).encode() if body is not None else None
    try:
        connection_.request(method, path, body=data, headers={
            "Authorization": f"Bearer {conn['token_out']}", "Content-Type": "application/json", "User-Agent": "LAIka"})
        answer = connection_.getresponse()
        raw = answer.read()
    except (OSError, http.client.HTTPException) as error:
        raise HTTPException(status_code=502, detail=f"LNMT did not answer: {error}")
    finally:
        connection_.close()
    try:
        result = json.loads(raw or b"{}")
    except ValueError:
        result = {}
    if answer.status >= 400:
        detail = result.get("error") or f"HTTP {answer.status}"
        raise HTTPException(status_code=502 if answer.status >= 500 else 400, detail=f"LNMT: {detail}")
    return result


# --- administrators: connect / disconnect ----------------------------------------------------

class Connect(BaseModel):
    code: str = Field(min_length=20, max_length=2000)


def _decode_code(code):
    try:
        data = json.loads(base64.urlsafe_b64decode(code.strip() + "=" * (-len(code.strip()) % 4)))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="That is not an LNMT pairing code")
    if data.get("v") != 1 or not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", str(data.get("url", ""))) \
            or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("cert_sha256", ""))) \
            or not str(data.get("token", "")).startswith(TOKEN_PREFIX):
        raise HTTPException(status_code=422, detail="That is not an LNMT pairing code")
    return data


def _own_url(request):
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", host):
        raise HTTPException(status_code=400, detail="Open LAIka by its address (not through another site) to connect")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
    return f"{scheme}://{host}"


@router.get("/api/link/lnmt")
def link_status():
    conn = connection()
    if not conn:
        return {"connected": False}
    return {"connected": True, "url": conn["url"], "name": conn.get("name", "LNMT"),
            "connected_at": conn.get("connected_at"), "laika_url": conn.get("laika_url", "")}


@router.post("/api/link/lnmt")
def link_connect(payload: Connect, request: Request):
    data = _decode_code(payload.code)
    token_in = TOKEN_PREFIX + secrets.token_urlsafe(32)
    laika_url = _own_url(request)
    conn = {"url": data["url"].rstrip("/"), "cert_sha256": data["cert_sha256"], "token_out": data["token"],
            "inbound_sha256": _sha(token_in), "name": str(data.get("name") or "LNMT")[:40],
            "laika_url": laika_url, "connected_at": time.time()}
    answer = call_lnmt("POST", "/api/link/hello", {"laika_url": laika_url, "token": token_in}, conn=conn)
    if not answer.get("ok"):
        raise HTTPException(status_code=502, detail="LNMT refused the pairing; make a new code there")
    _save(conn)
    return link_status()


@router.delete("/api/link/lnmt")
def link_disconnect():
    conn = connection()
    if conn:
        try:
            call_lnmt("POST", "/api/link/bye", {}, conn=conn, timeout=10)
        except HTTPException:
            pass  # LNMT away: forget it here anyway
    _path().unlink(missing_ok=True)
    return {"connected": False}


# --- people: sign in to LNMT with LAIka --------------------------------------------------------

class Authorize(BaseModel):
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")


@router.post("/api/connect/authorize")
def authorize(payload: Authorize):
    """The signed-in person agrees to sign in to LNMT: a one-use code LNMT
    exchanges (server to server) for who they are."""
    import access
    conn = connection()
    if not conn:
        raise HTTPException(status_code=409, detail="LNMT is not connected to this LAIka")
    name = access.current_name()
    if not name or name == "operator":
        raise HTTPException(status_code=403, detail="Sign in to LAIka with your own account first")
    code = secrets.token_urlsafe(32)
    _main().redis.set(f"laika:link:code:{_sha(code)}", name, ex=CODE_SECONDS)
    return {"redirect": f"{conn['url']}/api/laika/return?code={code}&state={payload.state}", "service": conn.get("name", "LNMT")}


# --- the current person's LNMT apps (project Settings → Deploy to LNMT) -----------------------

@router.get("/api/link/lnmt/apps")
def my_lnmt_apps():
    import access
    return call_lnmt("GET", f"/api/link/apps?user={access.current_name()}")


class DeployTo(BaseModel):
    customer: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    app: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    request_id: str = Field(pattern=_REQUEST_ID)


def _wait(request_id, seconds=90):
    """Wait for an operator request to finish; returns its result hash."""
    main = _main()
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = main._hash(f"laika:operator-results:{request_id}")
        if data.get("status") not in (None, "", "pending", "running"):
            return data
        time.sleep(1)
    raise HTTPException(status_code=504, detail="The host did not finish in time; try again in a minute")


def _public_key(result):
    try:
        return json.loads(result.get("output") or "{}").get("public_key", "").strip()
    except ValueError:
        return ""


def _push_to_lnmt(project_id, git_url, request_id):
    """Set the project's git push to the LNMT app and return LAIka's public deploy key."""
    import project_routes
    project_routes._operator_request("project_push_setup", request_id, {"project_id": project_id, "url": git_url})
    result = _wait(request_id)
    if result.get("status") != "succeeded":
        raise HTTPException(status_code=502, detail=f"Push setup failed: {result.get('message', '')}")
    key = _public_key(result)
    if not key:
        raise HTTPException(status_code=502, detail="Push setup gave no deploy key")
    return key


@router.post("/api/projects/{project_id}/deploy-lnmt")
def deploy_to_lnmt(project_id: str, payload: DeployTo):
    """Every approved change of this project goes to an LNMT app of the person
    asking (LNMT checks the app is theirs), which builds and serves it."""
    import access
    if not PID.fullmatch(project_id) or project_id == "laika":
        raise HTTPException(status_code=404, detail="Project not found")
    user = access.current_name()
    apps = call_lnmt("GET", f"/api/link/apps?user={user}").get("apps", [])
    target = next((a for a in apps if a["customer"] == payload.customer and a["name"] == payload.app), None)
    if not target or not target.get("git_url"):
        raise HTTPException(status_code=404, detail="No such app of yours on LNMT (only apps built from git can be deployed to)")
    key = _push_to_lnmt(project_id, target["git_url"], payload.request_id)
    call_lnmt("POST", "/api/link/deploy-key", {"user": user, "customer": payload.customer, "app": payload.app,
                                              "public_key": key, "label": f"laika-{project_id}"[:40],
                                              "project": project_id})
    _main().redis.set(f"laika:link:deploy:{project_id}", json.dumps({"customer": payload.customer, "app": payload.app,
                                                                    "site": target.get("sites", [])}))
    return {"project": project_id, "app": payload.app, "git_url": target["git_url"]}


@router.get("/api/projects/{project_id}/deploy-lnmt")
def deploy_target(project_id: str):
    raw = _main().redis.get(f"laika:link:deploy:{project_id}") if PID.fullmatch(project_id) else None
    return {"connected": bool(connection()), "target": json.loads(raw) if raw else None}


# --- LNMT's own calls (/api/link/service/…, the token is the caller) ----------------------------

def _service(request):
    conn = connection()
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not conn or not supplied or not hmac.compare_digest(_sha(supplied), conn["inbound_sha256"]):
        raise HTTPException(status_code=401, detail="Unknown caller")
    return conn


def _person(name):
    """A LAIka user LNMT acts for, as an access context (refuses unknown/disabled)."""
    import access
    user = access.get_user(_main().redis, str(name or ""))
    if not user or user["disabled"]:
        raise HTTPException(status_code=404, detail="No such LAIka user")
    return access.context_for(user)


@router.get("/api/link/service/users")
def service_users(request: Request):
    _service(request)
    import access
    return {"users": [{"username": u.get("username") or u["name"], "name": u["name"], "role": u.get("role", "member"),
                       "disabled": u["disabled"]} for u in access.all_users(_main().redis)]}


class Exchange(BaseModel):
    code: str = Field(min_length=20, max_length=100)


@router.post("/api/link/service/token")
def service_token(payload: Exchange, request: Request):
    _service(request)
    redis = _main().redis
    key = f"laika:link:code:{_sha(payload.code)}"
    name = redis.get(key)
    redis.delete(key)  # one use
    if not name:
        raise HTTPException(status_code=400, detail="Sign-in code expired or already used")
    ctx = _person(name)
    import auth
    auth.audit(redis, request, 200, f"LNMT sign-in as {ctx.name}")
    return {"username": ctx.display, "name": ctx.name, "role": ctx.role}


class ServiceProject(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    template: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    for_user: str = Field(pattern=r"^[A-Za-z0-9._-]{2,40}$")
    git_url: str = Field(pattern=r"^ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._/-]+$")
    request_id: str = Field(pattern=_REQUEST_ID)


@router.post("/api/link/service/projects")
def service_create_project(payload: ServiceProject, request: Request):
    """"Build with LAIka" from LNMT: a new project for one person, with its
    push set to the LNMT app; returns LAIka's deploy key for LNMT to accept."""
    _service(request)
    import access
    import project_routes
    redis = _main().redis
    ctx = _person(payload.for_user)
    access.CURRENT.set(ctx)
    if project_routes._known(payload.id):
        raise HTTPException(status_code=409, detail="A LAIka project with that id already exists")
    if payload.template and payload.template not in {t["id"] for t in project_routes.project_templates()}:
        raise HTTPException(status_code=422, detail="Unknown LAIka template")
    project_routes._operator_request("create_project", payload.request_id, {
        "project_id": payload.id, "name": payload.name, "importance": "medium", "source": "empty", "url": "",
        "push_remote": None, "gate": None, "template": payload.template or ""})
    result = _wait(payload.request_id, seconds=180)
    if result.get("status") != "succeeded":
        raise HTTPException(status_code=502, detail=f"LAIka could not create the project: {result.get('message', '')}")
    if not ctx.is_admin:  # the person who asked may work on (and approve) their own project
        level = os.environ.get("LNMT_PROJECT_ACCESS", DEFAULT_ACCESS)
        user = access.get_user(redis, ctx.name)
        access.save_user(redis, ctx.name, access={**user["access"], payload.id: level if level in access.LEVELS else DEFAULT_ACCESS})
    key = _push_to_lnmt(payload.id, payload.git_url, payload.request_id + "-push")
    import auth
    auth.audit(redis, request, 200, f"LNMT created project {payload.id} for {ctx.name}")
    redis.set(f"laika:link:deploy:{payload.id}", json.dumps({"git_url": payload.git_url}))
    return {"project": payload.id, "public_key": key, "url": f"{_service(request).get('laika_url', '')}/#/projects/{payload.id}"}


@router.get("/api/link/service/projects/{project_id}")
def service_project(project_id: str, request: Request, for_user: str = ""):
    """What LNMT shows on the app's page: the project and its latest work."""
    conn = _service(request)
    import access
    redis = _main().redis
    if not PID.fullmatch(project_id) or not redis.sismember("laika:projects", project_id):
        raise HTTPException(status_code=404, detail="No such project")
    ctx = _person(for_user)
    if not access.may(redis, ctx, project_id, "view"):
        raise HTTPException(status_code=404, detail="No such project")
    import project_routes
    access.CURRENT.set(ctx)
    item = project_routes.get_project(project_id, limit=5)
    return {"project": project_id, "name": item.get("name", project_id), "status": item.get("status", ""),
            "url": f"{conn.get('laika_url', '')}/#/projects/{project_id}",
            "goals": [{"id": g.get("id") or g.get("goal_id"), "status": g.get("status"), "title": str(g.get("title") or g.get("summary") or "")[:120],
                       "updated_at": g.get("updated_at")} for g in item.get("goals", [])],
            "jobs": [{"id": j.get("id") or j.get("job_id"), "status": j.get("status"), "title": str(j.get("title") or "")[:120],
                      "updated_at": j.get("updated_at")} for j in item.get("jobs", [])]}
