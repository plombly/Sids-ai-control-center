"""Project resolution shared by the worker, orchestrator and job-review.py.

SID runs several fully separated projects. Each has its own git repository,
worktree directory, log directory, test gate, merge queue, approval lock and
main-head record; nothing is shared but the worker pool. Projects are
registered by scripts/sid-project.py as Redis hashes sid:projects:<id>.

The SID codebase itself is project "sid". Its paths come from the existing
environment variables and its Redis keys keep their original names, so
single-project behavior is unchanged when nothing else is registered.
"""

import json
import os
import re
from pathlib import Path

SID_PROJECT = "sid"
# A project's app data (uploads, databases) lives outside its directory so
# the API container can be given write access to it and nothing else.
DATA_BASE = Path(os.environ.get("SID_PROJECT_DATA", "/opt/sid-project-data"))
PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
IMPORTANCE = ("high", "medium", "low")


def _bounded(value, default, low, high, kind=int):
    try:
        number = kind(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


class Project:
    """A resolved project: paths, gate, importance and its Redis key names."""

    def __init__(self, fields):
        self.id = fields["id"]
        self.name = fields.get("name") or self.id
        self.repo = Path(fields["repo"])
        # The project's own directory (holds repo, worktrees, logs, deploy key).
        self.root = Path(fields.get("root") or Path(fields["repo"]).parent)
        self.worktrees = Path(fields["worktrees"])
        self.logs = Path(fields["logs"])
        self.default_branch = fields.get("default_branch") or "main"
        # Project groups (one level): a child names its parent; see effective_fields().
        self.parent = fields.get("parent") or ""
        self.gate_command = fields.get("gate_command") or ""
        # Installs dependencies (with network) before builds and gates; empty
        # means detect from the worktree (detect_setup).
        self.setup_command = fields.get("setup_command") or ""
        # Run the app from main (services/apps/sid_apps.py); "" = not run.
        self.run_command = fields.get("run_command") or ""
        try:
            self.run_port = int(fields.get("run_port") or 0)
        except (TypeError, ValueError):
            self.run_port = 0
        # Limits for the running app (systemd MemoryMax / CPUQuota / TasksMax).
        self.run_memory_mb = _bounded(fields.get("run_memory_mb"), 1024, 64, 65536)
        self.run_cpus = _bounded(fields.get("run_cpus"), 1.0, 0.1, 64.0, float)
        self.run_tasks = _bounded(fields.get("run_tasks"), 512, 16, 32768)
        importance = fields.get("importance") or "medium"
        self.importance = importance if importance in IMPORTANCE else "medium"
        self.status = fields.get("status") or "active"

    @property
    def is_sid(self):
        return self.id == SID_PROJECT

    # Redis keys: SID keeps its original names.
    @property
    def merge_queue_key(self):
        return "sid:merge-queue" if self.is_sid else f"sid:merge-queue:{self.id}"

    @property
    def approval_lock_key(self):
        # Repository-wide (per project), never per job: approval serializes
        # every advancement of that project's main.
        return "sid:approval-lock:main" if self.is_sid else f"sid:approval-lock:{self.id}"

    @property
    def main_head_key(self):
        return "sid:main-head" if self.is_sid else f"sid:main-head:{self.id}"

    def __repr__(self):
        return f"Project({self.id!r}, repo={str(self.repo)!r})"


def sid_defaults():
    return {
        "id": SID_PROJECT,
        "name": "SID AI Command Center",
        "repo": os.getenv("REPO_ROOT", "/opt/sids-ai-command-center"),
        "worktrees": os.getenv("WORKTREE_ROOT", "/opt/sid-worktrees"),
        "logs": os.getenv("LOG_ROOT", "/var/log/sid-ai/jobs"),
        "default_branch": "main",
        "gate_command": "",  # SID uses its own gate (scripts/integration-check.py)
        "importance": "medium",
    }


def load(redis_client, project_id):
    """The project for this id. Unknown, empty or unregistered "sid" resolve
    to SID's defaults (registry fields override them); any other unknown id
    raises, so work never runs in the wrong repository."""
    project_id = project_id or SID_PROJECT
    fields = {}
    try:
        fields = redis_client.hgetall(f"sid:projects:{project_id}") or {}
    except Exception:
        if project_id != SID_PROJECT:
            raise
    if project_id == SID_PROJECT:
        merged = sid_defaults()
        # Only descriptive fields may be overridden for SID; its paths and its
        # gate (scripts/integration-check.py) stay the configured ones so a
        # registry entry cannot move or weaken the control plane.
        for key in ("name", "importance", "status"):  # never paths or the gate
            if fields.get(key):
                merged[key] = fields[key]
        return Project(merged)
    if not fields:
        raise LookupError(f"unknown project: {project_id}")
    return Project(inherit(redis_client, fields))


# --- project groups -----------------------------------------------------------------
# A parent project controls its children (one level, one parent per child;
# SID may be a parent, e.g. of its own app, but never a child): children follow the parent's
# importance and test internet access, and an archived parent archives the
# group. Each child stays its own repository with its own gate, builds and
# approvals; a goal given to the parent may plan jobs in any member.

INHERITED = ("importance", "gate_network")


def inherit(redis_client, fields):
    """A child's registry fields with what its parent controls applied."""
    parent_id = fields.get("parent") or ""
    if not parent_id or parent_id == fields.get("id"):
        return fields
    try:
        parent = redis_client.hgetall(f"sid:projects:{parent_id}") or {}
    except Exception:
        parent = {}
    if not parent:
        return fields
    merged = dict(fields)
    for key in INHERITED:
        merged[key] = parent.get(key) or ""
    if parent.get("status") == "archived" and merged.get("status") == "active":
        merged["status"] = "archived"
    return merged


def effective_fields(redis_client, project_id):
    return inherit(redis_client, redis_client.hgetall(f"sid:projects:{project_id}") or {})


def children(redis_client, project_id):
    """Ids of the projects whose parent is project_id, sorted."""
    found = []
    try:
        ids = redis_client.smembers("sid:projects") or []
    except Exception:
        return []
    for other in ids:
        if other != project_id and redis_client.hget(f"sid:projects:{other}", "parent") == project_id:
            found.append(other)
    return sorted(found)


def group(redis_client, project):
    """The members a goal of this project may plan work in: the project and,
    for a parent, its active children (the project first)."""
    members = [project]
    if project.parent:
        return members
    for child_id in children(redis_client, project.id):
        try:
            child = load(redis_client, child_id)
        except LookupError:
            continue
        if child.status == "active":
            members.append(child)
    return members


def check_parent(redis_client, child_id, parent_id):
    """Why child_id cannot become a child of parent_id, or "" if it can."""
    if child_id == SID_PROJECT:
        return "SID itself cannot be a child project"
    if child_id == parent_id:
        return "a project cannot be its own parent"
    parent = redis_client.hgetall(f"sid:projects:{parent_id}") or {}
    if not parent and parent_id == SID_PROJECT:
        parent = {"id": SID_PROJECT, "status": "active"}  # SID needs no registry entry
    if not parent:
        return f"unknown project: {parent_id}"
    if parent.get("status") not in ("active", None, ""):
        return f"{parent_id} is {parent.get('status')}"
    if parent.get("parent"):
        return f"{parent_id} is itself a child project (groups have one level)"
    if children(redis_client, child_id):
        return f"{child_id} has child projects of its own (groups have one level)"
    return ""


def job_project_id(redis_client, job):
    """A job's project: its own field, else its builder's (reviews, repairs
    and integrations act on a builder), else SID."""
    project_id = job.get("project_id")
    if project_id:
        return project_id
    for field in ("builder_job_id", "target_builder_id"):
        builder_id = job.get(field)
        if builder_id:
            try:
                value = redis_client.hget(f"sid:jobs:{builder_id}", "project_id")
            except Exception:
                value = None
            if value:
                return value
    job_id = job.get("id")
    if job_id:
        try:
            value = redis_client.hget(f"sid:jobs:{job_id}", "project_id")
        except Exception:
            value = None
        if value:
            return value
    return SID_PROJECT


def all_projects(redis_client):
    """Every active project, SID first."""
    ids = set()
    try:
        ids = set(redis_client.smembers("sid:projects") or [])
    except Exception:
        pass
    ids.discard(SID_PROJECT)
    projects = [load(redis_client, SID_PROJECT)]
    for project_id in sorted(ids):
        try:
            project = load(redis_client, project_id)
        except LookupError:
            continue
        if project.status == "active":
            projects.append(project)
    return projects


# --- build commands detected from a checkout ---------------------------------------
# Used when a project has no explicit command, so a project that starts empty
# gets its tests and dependency install as soon as the builder adds them.

def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def detect_gate(path):
    """The test command a checkout implies, or ""."""
    path = Path(path)
    package = path / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text())
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return "npm test"
        except (OSError, ValueError, TypeError):
            pass
    if any((path / name).exists() for name in ("pyproject.toml", "pytest.ini", "setup.cfg")) or (path / "tests").is_dir():
        return "python3 -m pytest -q"
    if (path / "Cargo.toml").is_file():
        return "cargo test"
    if (path / "go.mod").is_file():
        return "go test ./..."
    if (path / "pubspec.yaml").is_file():
        # Packages come from the setup step; tests run offline.
        flutter = "flutter:" in _read(path / "pubspec.yaml")
        tool = "flutter" if flutter else "dart"
        return f"{tool} analyze --no-pub && {tool} test --no-pub" if (path / "test").is_dir() else f"{tool} analyze --no-pub"
    makefile = path / "Makefile"
    if makefile.is_file():
        try:
            if any(re.match(r"^test:", line) for line in makefile.read_text().splitlines()):
                return "make test"
        except OSError:
            pass
    return ""


def detect_setup(path):
    """The dependency install a checkout implies, or "". Python projects get
    their own .venv (with pytest) in the worktree; gates put it first on PATH."""
    path = Path(path)
    steps = []
    if (path / "package-lock.json").is_file():
        steps.append("npm ci --no-audit --no-fund")
    elif (path / "package.json").is_file():
        steps.append("npm install --no-audit --no-fund")
    if (path / "pubspec.yaml").is_file():
        steps.append("flutter pub get" if "flutter:" in _read(path / "pubspec.yaml") else "dart pub get")
    if (path / "requirements.txt").is_file():
        steps.append("python3 -m venv .venv && .venv/bin/pip install -q pytest -r requirements.txt")
    elif (path / "pyproject.toml").is_file():
        steps.append("python3 -m venv .venv && .venv/bin/pip install -q pytest -e .")
    return " && ".join(steps)


def verify_worktree_pointer(top, repo):
    """Raise unless top/.git is the pointer git wrote: a regular file naming a
    directory in repo/.git/worktrees. Project code can write its worktree;
    a replaced pointer (or a planted .git directory) would make SID's own,
    unsandboxed git commands load that project's hooks and config."""
    top = Path(top)
    pointer = top / ".git"
    if not os.path.lexists(pointer):
        raise RuntimeError(f"worktree {top.name} has no .git pointer; refusing to run git in it")
    expected_base = (Path(repo) / ".git" / "worktrees").resolve()
    try:
        if pointer.is_symlink() or not pointer.is_file():
            raise ValueError("not a regular file")
        content = pointer.read_text().strip()
        if not content.startswith("gitdir: "):
            raise ValueError("not a gitdir pointer")
        target = Path(content[len("gitdir: "):])
        target = (target if target.is_absolute() else top / target).resolve()
        if target.parent != expected_base:
            raise ValueError(f"points to {target}")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"worktree {top.name} .git pointer was tampered with ({exc}); refusing to run git in it")


PROJECTS_BASE = Path(os.environ.get("SID_PROJECTS_BASE", "/opt/sid-projects"))


def builds_dir(project_id):
    """<PROJECTS_BASE>/<id>/builds: downloadable build results (scripts/sid-build.py)."""
    if not PROJECT_ID.fullmatch(project_id or "") or project_id == SID_PROJECT:
        raise ValueError(f"invalid project id: {project_id!r}")
    return PROJECTS_BASE / project_id / "builds"


def data_dir(project_id):
    """<DATA_BASE>/<id>: the project's app data (HOME/DATA_DIR of its app,
    the "Data" area of the dashboard file browser)."""
    if not PROJECT_ID.fullmatch(project_id or ""):
        raise ValueError(f"invalid project id: {project_id!r}")
    return DATA_BASE / project_id


# --- activity log ------------------------------------------------------------------------
# Things that leave no other record (dashboard file changes, undos, app
# deploys/crashes, previews, restores) go into a short per-project log the
# Activity tab merges with goals and jobs (apps/api/activity_routes.py).

EVENTS_KEEP = 500


def record_event(redis_client, project_id, kind, title, detail="", ref="", when=None):
    """Best effort: never let logging break the action it describes."""
    import time as _time
    entry = {"at": when or _time.time(), "kind": kind, "title": str(title)[:200], "detail": str(detail)[:300],
             "ref": str(ref)[:80]}
    try:
        key = f"sid:events:{project_id}"
        redis_client.lpush(key, json.dumps(entry))
        redis_client.ltrim(key, 0, EVENTS_KEEP - 1)
    except Exception:
        pass
    return entry
