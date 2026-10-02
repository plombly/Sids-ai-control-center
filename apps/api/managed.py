"""Projects the LAIka builder manages: view-only inside LAIka.

LAIka itself (the built-in "sid" project) and its companion app are
developed by the system builder from the host, never from inside LAIka:
nobody can give them goals, change their settings, files or builds, or act
on their jobs through the API (dashboard or phone). Their pages stay
readable for an at-a-glance review of what changed. A project is marked
with the registry field view_only = "1"; "sid" always is.
"""

import re

MESSAGE = "This project is managed by the LAIka builder and is view-only here."
_PROJECT = re.compile(r"^/api/projects/([a-z0-9][a-z0-9-]{0,39})(?:/.*)?$")
_JOB = re.compile(r"^/api/jobs/([A-Za-z0-9_-]{1,64})/(?:actions|preview)$")
_ASSISTANT = re.compile(r"^/api/assistant/([a-f0-9]{16})/")
_SID_GOALS = {"/api/goals", "/api/goals/submit", "/api/goals/submit-atomic", "/api/prompts"}


def view_only(redis, project_id):
    if not project_id or project_id == "sid":
        return True
    try:
        return redis.hget(f"sid:projects:{project_id}", "view_only") == "1"
    except Exception:
        return False


def write_target(redis, path):
    """The project a write request would change, or None if none."""
    if path in _SID_GOALS:
        return "sid"  # the legacy goal endpoints submit to the built-in project
    match = _PROJECT.match(path)
    if match:
        return match.group(1)
    match = _JOB.match(path)
    if match:
        key = f"sid:jobs:{match.group(1)}"
        if redis.hget(key, "status") is None:
            return None  # unknown job: the endpoint answers 404
        return redis.hget(key, "project_id") or "sid"
    match = _ASSISTANT.match(path)
    if match:
        return redis.hget(f"sid:assist:{match.group(1)}", "project_id") or None
    return None


def refused(redis, method, path):
    """MESSAGE when this write touches a view-only project, else ""."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return ""
    target = write_target(redis, path)
    return MESSAGE if target is not None and view_only(redis, target) else ""
