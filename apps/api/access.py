"""Who may see and do what (LAIka 1.1: teams).

Users: laika:users (set of lower-case names) and laika:users:<name> (hash:
username as created, password scrypt hash ("" until an invite is
accepted), role admin|member, access JSON {project: view|build|approve},
created_at, disabled "1"). The single administrator of LAIka 1.0
(laika:auth:admin) becomes the first user on first use.

Administrators may do everything. Members see and do only what their
project access allows; access to a parent covers its children. LAIka's own
project and other projects the builder manages stay administrator-only.

Every request is classified by one table (rule()): public and signed-in
paths, project paths with the level they need, and everything else is
administrator-only, so an endpoint nobody listed fails safe. Lists are
filtered for members in one place (prune()).
"""

import contextvars
import json
import re
import time

# The person behind the request being handled (set by main's middleware), for
# code deep in a request that records who did something.
CURRENT = contextvars.ContextVar("laika_current_user", default=None)


def current_name():
    ctx = CURRENT.get()
    return ctx.name if ctx is not None else ""

LEVELS = {"view": 1, "build": 2, "approve": 3}
ROLES = ("admin", "member")
USERS = "laika:users"
NAME = re.compile(r"^[A-Za-z0-9._-]{2,40}$")


def user_key(name):
    return f"laika:users:{name.lower()}"


# --- users ---------------------------------------------------------------------------------------

def migrate(redis):
    """LAIka 1.0 had one administrator in laika:auth:admin: make it a user."""
    if redis.smembers(USERS):
        return
    old = redis.hgetall("laika:auth:admin") or {}
    if not old.get("username"):
        return
    name = old["username"].lower()
    redis.hset(user_key(name), mapping={"username": old["username"], "password": old.get("password", ""),
                                        "role": "admin", "access": "{}",
                                        "created_at": old.get("created_at") or str(time.time())})
    redis.sadd(USERS, name)
    redis.delete("laika:auth:admin")


def get_user(redis, name):
    if not name:
        return None
    record = redis.hgetall(user_key(name)) or {}
    if not record:
        return None
    try:
        access = json.loads(record.get("access") or "{}")
    except ValueError:
        access = {}
    return {**record, "name": name.lower(), "access": access if isinstance(access, dict) else {},
            "disabled": record.get("disabled") == "1"}


def all_users(redis):
    return [user for user in (get_user(redis, name) for name in sorted(redis.smembers(USERS) or [])) if user]


def any_user(redis):
    migrate(redis)
    return bool(redis.smembers(USERS))


def active_admins(redis):
    return [u for u in all_users(redis) if u.get("role") == "admin" and not u["disabled"] and u.get("password")]


def save_user(redis, name, **fields):
    fields = {k: (json.dumps(v) if k == "access" else str(v)) for k, v in fields.items() if v is not None}
    redis.hset(user_key(name), mapping=fields)
    redis.sadd(USERS, name.lower())


# --- who is asking --------------------------------------------------------------------------------

class Context:
    """The person behind a request (or the host's operator token: admin)."""

    def __init__(self, name, role, access=None, display=None):
        self.name, self.role, self.access, self.display = name, role, access or {}, display or name

    @property
    def is_admin(self):
        return self.role == "admin"

    def __repr__(self):
        return f"Context({self.name!r}, {self.role!r})"


ADMIN = Context("operator", "admin")


def context_for(user):
    if not user:
        return None
    return Context(user["name"], user.get("role") or "member", user.get("access"), user.get("username"))


def _registry(redis):
    ids = set(redis.smembers("laika:projects") or [])
    return {pid: redis.hget(f"laika:projects:{pid}", "parent") or "" for pid in ids}


def levels(redis, ctx):
    """{project: level number} for a member (children inherit their parent's
    grant); None for an administrator (everything)."""
    if ctx is None:
        return {}
    if ctx.is_admin:
        return None
    import managed
    granted = {pid: LEVELS.get(level, 0) for pid, level in (ctx.access or {}).items()}
    result = dict(granted)
    for pid, parent in _registry(redis).items():
        if parent and parent in granted:
            result[pid] = max(result.get(pid, 0), granted[parent])
    return {pid: level for pid, level in result.items() if level and not managed.view_only(redis, pid)}


def level_name(number):
    return next((name for name, value in LEVELS.items() if value == number), "")


# --- the rule table ----------------------------------------------------------------------------

ID = r"[A-Za-z0-9_-]{1,64}"
PID = r"[a-z0-9][a-z0-9-]{0,39}"
SIGNED_IN = [
    ("GET", rf"/api/auth/state|/api/auth/sessions|/api/app/info|/api/app/summary|/api/project-catalog|/api/workers"
            rf"|/api/heartbeat|/api/orchestrators|/api/status|/api/system-health|/api/workers/scale|/api/system/update"
            rf"|/api/goals|/api/goals/recent|/api/jobs|/api/jobs/recent|/api/approvals|/api/jobs/approvals|/api/failures"
            rf"|/api/action-required|/api/actions-required|/api/queue|/api/merge-queue|/api/dismissals|/api/projects"
            rf"|/api/usage|/api/devices|/api/repository|/api/operator/status|/api/operator-requests/{ID}|/api/me"
            rf"|/api/me/notifications|/api/me/notifications/digest-preview|/api/sftp|/api/me/ssh-keys"),
    ("POST", r"/api/auth/logout|/api/auth/password|/api/devices|/api/dismissals|/api/me/notifications/test"
             r"|/api/me/ssh-keys"),
    ("PUT", r"/api/me/notifications/(?:settings|targets)"),
    ("DELETE", rf"/api/auth/sessions/[a-f0-9]{{16}}|/api/devices/{ID}|/api/dismissals/.+|/api/me/ssh-keys/[a-f0-9]{{12}}"),
]
# (method, path pattern, what the id names, level). First match wins.
PROJECT_RULES = [
    ("POST", rf"/api/projects/({PID})/delete", "project", "admin"),
    ("GET", rf"/api/projects/({PID})/env", "project", "approve"),
    ("GET", rf"/api/projects/({PID})(?:/.*)?", "project", "view"),
    ("POST", rf"/api/projects/({PID})/(?:goals|assistant|builds|builds/all|recheck|app/restart)", "project", "build"),
    ("POST", rf"/api/projects/({PID})/files/.+", "project", "build"),
    ("PUT", rf"/api/projects/({PID})/files/(?:code|data)", "project", "build"),
    ("DELETE", rf"/api/projects/({PID})/files/data", "project", "build"),
    ("PATCH", rf"/api/projects/({PID})", "project", "approve"),
    ("PUT", rf"/api/projects/({PID})/env", "project", "approve"),
    ("DELETE", rf"/api/projects/({PID})/env/.+", "project", "approve"),
    ("POST", rf"/api/projects/({PID})/(?:parent|push-setup|undo|retry-clone)", "project", "approve"),
    ("GET", rf"/api/jobs/({ID})(?:/log)?", "job", "view"),
    ("POST", rf"/api/jobs/({ID})/preview", "job", "build"),
    ("DELETE", rf"/api/jobs/({ID})/preview", "job", "build"),
    ("POST", rf"/api/jobs/({ID})/actions", "job", "build"),  # approving needs more: checked in the endpoint
    ("GET", rf"/api/goals/({ID})(?:/handoff|/handoff-bundle|/handoff-data)?", "goal", "view"),
    ("GET", rf"/api/handoff/({ID})", "goal", "view"),
    ("POST", rf"/api/goals/({ID})/approve-all", "goal", "approve"),
    ("GET", r"/api/assistant/([a-f0-9]{16})", "assistant", "view"),
    ("POST", r"/api/assistant/([a-f0-9]{16})/(?:reply|cancel|submit|write-goal|retry)", "assistant", "build"),
]
_SIGNED_IN = [(m, re.compile(p)) for m, p in SIGNED_IN]
_PROJECT_RULES = [(m, re.compile(p), kind, level) for m, p, kind, level in PROJECT_RULES]


def project_of(redis, kind, ident):
    if kind == "project":
        return ident
    if kind == "job":
        status = redis.hget(f"laika:jobs:{ident}", "status")
        return (redis.hget(f"laika:jobs:{ident}", "project_id") or "laika") if status is not None else None
    if kind == "goal":
        exists = bool(redis.hgetall(f"laika:goals:{ident}"))
        return (redis.hget(f"laika:goals:{ident}", "project_id") or "laika") if exists else None
    if kind == "assistant":
        return redis.hget(f"laika:assist:{ident}", "project_id") or None
    return None


def rule(method, path):
    """("signed_in",) | ("project", kind, id, level) | ("admin",)."""
    method = "GET" if method == "HEAD" else method
    for allowed, pattern in _SIGNED_IN:
        if method == allowed and pattern.fullmatch(path):
            return ("signed_in",)
    for allowed, pattern, kind, level in _PROJECT_RULES:
        match = pattern.fullmatch(path) if method == allowed else None
        if match:
            return ("project", kind, match.group(1), level)
    return ("admin",)


def check(redis, ctx, method, path):
    """None if allowed, else (status, detail). Administrators pass everything."""
    if ctx is None:
        return (401, "Sign in required")
    if ctx.is_admin:
        return None
    found = rule(method, path)
    if found[0] == "signed_in":
        return None
    if found[0] == "admin":
        return (403, "Only an administrator can do that")
    _, kind, ident, level = found
    project = project_of(redis, kind, ident)
    have = (levels(redis, ctx) or {}).get(project, 0) if project else 0
    if not have:
        return (404, "Not found")  # no hint that a project exists
    if level == "admin":
        return (403, "Only an administrator can do that")
    if have < LEVELS[level]:
        return (403, f"You need {level} access to this project")
    return None


def may(redis, ctx, project_id, level):
    """For endpoints that check more than the path says (approving, a group)."""
    if ctx is None:
        return False
    if ctx.is_admin:
        return True
    return (levels(redis, ctx) or {}).get(project_id, 0) >= LEVELS[level]


# --- filtering lists for members ----------------------------------------------------------------

JOB_LISTS = {"/api/goals", "/api/goals/recent", "/api/jobs", "/api/jobs/recent", "/api/approvals", "/api/jobs/approvals",
             "/api/failures", "/api/action-required", "/api/actions-required", "/api/queue", "/api/merge-queue"}
FILTERED = JOB_LISTS | {"/api/projects", "/api/workers", "/api/heartbeat", "/api/app/summary", "/api/repository"}


def _visible_item(item, visible):
    return (item.get("project_id") or "laika") in visible


def prune(redis, path, body, visible):
    """The response a member may see. visible: {project: level}."""
    if path == "/api/repository":
        return {}
    if path == "/api/projects" and isinstance(body, list):
        kept = []
        for item in body:
            if isinstance(item, dict) and item.get("id") in visible:
                item = dict(item)
                if isinstance(item.get("children"), list):
                    item["children"] = [c for c in item["children"] if isinstance(c, dict) and c.get("id") in visible]
                item["my_access"] = level_name(visible[item["id"]])
                kept.append(item)
        return kept
    if path in ("/api/workers", "/api/heartbeat"):
        return _redact_workers(redis, body, visible)
    if path == "/api/app/summary" and isinstance(body, dict):
        projects = prune(redis, "/api/projects", body.get("projects") or [], visible)
        return {**_prune_jobs({k: v for k, v in body.items() if k != "projects"}, visible), "projects": projects}
    return _prune_jobs(body, visible)


def _prune_jobs(value, visible):
    if isinstance(value, list):
        return [_prune_jobs(item, visible) for item in value
                if not (isinstance(item, dict) and _looks_like_work(item) and not _visible_item(item, visible))]
    if isinstance(value, dict):
        return {key: _prune_jobs(item, visible) for key, item in value.items()}
    return value


def _looks_like_work(item):
    """A job, goal or approval record (they carry a project, or are LAIka's own)."""
    return "project_id" in item or any(key in item for key in ("goal_id", "role", "prompt", "goal"))


JOB_FIELDS = ("job_id", "active_job_id", "job_title", "title", "goal_id", "job_started_at", "prompt")


def _redact_workers(redis, value, visible):
    def worker(item):
        if not isinstance(item, dict):
            return item
        job = item.get("job_id") or item.get("active_job_id")
        if job:
            project = redis.hget(f"laika:jobs:{job}", "project_id") or "laika"
            if project not in visible:
                item = {key: ("" if key in JOB_FIELDS else val) for key, val in item.items()}
        return item
    if isinstance(value, list):
        return [worker(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_workers(redis, item, visible) for key, item in value.items()}
    return value
