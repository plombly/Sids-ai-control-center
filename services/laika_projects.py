"""Project resolution shared by the worker, orchestrator and job-review.py.

LAIka runs several fully separated projects. Each has its own git repository,
worktree directory, log directory, test gate, merge queue, approval lock and
main-head record; nothing is shared but the worker pool. Projects are
registered by scripts/laika-project.py as Redis hashes laika:projects:<id>.

The LAIka codebase itself is project "laika". Its paths come from the existing
environment variables and its Redis keys keep their original names, so
single-project behavior is unchanged when nothing else is registered.
"""

import json
import os
import re
from pathlib import Path

BUILTIN_PROJECT = "laika"
# A project's app data (uploads, databases) lives outside its directory so
# the API container can be given write access to it and nothing else.
DATA_BASE = Path(os.environ.get("LAIKA_PROJECT_DATA", "/var/lib/laika/project-data"))
PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
IMPORTANCE = ("high", "medium", "low")


def _env_number(name, default, kind=int):
    try:
        return kind(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


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
        self.push_remote = fields.get("push_remote") or ""
        # Project groups (one level): a child names its parent; see effective_fields().
        self.parent = fields.get("parent") or ""
        self.gate_command = fields.get("gate_command") or ""
        # Installs dependencies (with network) before builds and gates; empty
        # means detect from the worktree (detect_setup).
        self.setup_command = fields.get("setup_command") or ""
        # Run the app from main (services/apps/laika_apps.py); "" = not run.
        self.run_command = fields.get("run_command") or ""
        try:
            self.run_port = int(fields.get("run_port") or 0)
        except (TypeError, ValueError):
            self.run_port = 0
        # Limits for the running app (systemd MemoryMax / CPUQuota / TasksMax).
        # Defaults from Settings → Project defaults (services/laika_env.py).
        self.run_memory_mb = _bounded(fields.get("run_memory_mb"), _env_number("DEFAULT_APP_MEMORY_MB", 1024), 64, 65536)
        self.run_cpus = _bounded(fields.get("run_cpus"), _env_number("DEFAULT_APP_CPUS", 1.0, float), 0.1, 64.0, float)
        self.run_tasks = _bounded(fields.get("run_tasks"), _env_number("DEFAULT_APP_TASKS", 512), 16, 32768)
        importance = fields.get("importance") or "medium"
        self.importance = importance if importance in IMPORTANCE else "medium"
        self.status = fields.get("status") or "active"

    @property
    def is_builtin(self):
        return self.id == BUILTIN_PROJECT

    # Redis keys: LAIka keeps its original names.
    @property
    def merge_queue_key(self):
        return "laika:merge-queue" if self.is_builtin else f"laika:merge-queue:{self.id}"

    @property
    def approval_lock_key(self):
        # Repository-wide (per project), never per job: approval serializes
        # every advancement of that project's main.
        return "laika:approval-lock:main" if self.is_builtin else f"laika:approval-lock:{self.id}"

    @property
    def main_head_key(self):
        return "laika:main-head" if self.is_builtin else f"laika:main-head:{self.id}"

    def __repr__(self):
        return f"Project({self.id!r}, repo={str(self.repo)!r})"


def builtin_defaults():
    return {
        "id": BUILTIN_PROJECT,
        "name": "LAIka",
        "repo": os.getenv("REPO_ROOT", "/opt/laika"),
        "worktrees": os.getenv("WORKTREE_ROOT", "/var/lib/laika/worktrees"),
        "logs": os.getenv("LOG_ROOT", "/var/log/laika/jobs"),
        "default_branch": "main",
        "gate_command": "",  # LAIka uses its own gate (scripts/integration-check.py)
        "importance": "medium",
    }


def load(redis_client, project_id):
    """The project for this id. Unknown, empty or unregistered "laika" resolve
    to LAIka's defaults (registry fields override them); any other unknown id
    raises, so work never runs in the wrong repository."""
    project_id = project_id or BUILTIN_PROJECT
    fields = {}
    try:
        fields = redis_client.hgetall(f"laika:projects:{project_id}") or {}
    except Exception:
        if project_id != BUILTIN_PROJECT:
            raise
    if project_id == BUILTIN_PROJECT:
        merged = builtin_defaults()
        # Only descriptive fields may be overridden for LAIka; its paths and its
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
# LAIka may be a parent, e.g. of its own app, but never a child): children follow the parent's
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
        parent = redis_client.hgetall(f"laika:projects:{parent_id}") or {}
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
    return inherit(redis_client, redis_client.hgetall(f"laika:projects:{project_id}") or {})


def children(redis_client, project_id):
    """Ids of the projects whose parent is project_id, sorted."""
    found = []
    try:
        ids = redis_client.smembers("laika:projects") or []
    except Exception:
        return []
    for other in ids:
        if other != project_id and redis_client.hget(f"laika:projects:{other}", "parent") == project_id:
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
    if child_id == BUILTIN_PROJECT:
        return "LAIka itself cannot be a child project"
    if child_id == parent_id:
        return "a project cannot be its own parent"
    parent = redis_client.hgetall(f"laika:projects:{parent_id}") or {}
    if not parent and parent_id == BUILTIN_PROJECT:
        parent = {"id": BUILTIN_PROJECT, "status": "active"}  # LAIka needs no registry entry
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
    and integrations act on a builder), else LAIka."""
    project_id = job.get("project_id")
    if project_id:
        return project_id
    for field in ("builder_job_id", "target_builder_id"):
        builder_id = job.get(field)
        if builder_id:
            try:
                value = redis_client.hget(f"laika:jobs:{builder_id}", "project_id")
            except Exception:
                value = None
            if value:
                return value
    job_id = job.get("id")
    if job_id:
        try:
            value = redis_client.hget(f"laika:jobs:{job_id}", "project_id")
        except Exception:
            value = None
        if value:
            return value
    return BUILTIN_PROJECT


def all_projects(redis_client):
    """Every active project, LAIka first."""
    ids = set()
    try:
        ids = set(redis_client.smembers("laika:projects") or [])
    except Exception:
        pass
    ids.discard(BUILTIN_PROJECT)
    projects = [load(redis_client, BUILTIN_PROJECT)]
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

    def read(name):
        try:
            return (path / name).read_text()
        except (OSError, UnicodeDecodeError):
            return None
    return _detect_gate(lambda name: (path / name).exists(), lambda name: (path / name).is_dir(), read)


def detect_gate_at(repo, rev, runner=None):
    """detect_gate for a commit instead of a checkout: the test command the
    trusted base implies (the gate for an AI-written change is chosen from
    main as it was, never from the change itself)."""
    import subprocess as _subprocess
    run = runner or (lambda args: _subprocess.run(["git", "-c", "core.hooksPath=/dev/null/laika-no-hooks", *args],
                                                  cwd=str(repo), capture_output=True, text=True))
    listing = run(["ls-tree", rev])
    if listing.returncode != 0:
        return ""
    entries = {}
    for line in listing.stdout.splitlines():
        meta, _, name = line.partition("\t")
        entries[name] = meta.split()[1] if len(meta.split()) > 1 else "blob"

    def read(name):
        shown = run(["show", f"{rev}:{name}"])
        return shown.stdout if shown.returncode == 0 else None
    return _detect_gate(lambda name: name in entries, lambda name: entries.get(name) == "tree", read)


def _detect_gate(exists, is_dir, read):
    package = read("package.json") if exists("package.json") else None
    if package:
        try:
            data = json.loads(package)
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return "npm test"
        except (ValueError, TypeError):
            pass
    if any(exists(name) for name in ("pyproject.toml", "pytest.ini", "setup.cfg")) or is_dir("tests"):
        return "python3 -m pytest -q"
    if exists("Cargo.toml"):
        return "cargo test"
    if exists("go.mod"):
        return "go test ./..."
    if exists("pubspec.yaml"):
        # Packages come from the setup step; tests run offline.
        flutter = "flutter:" in (read("pubspec.yaml") or "")
        tool = "flutter" if flutter else "dart"
        return f"{tool} analyze --no-pub && {tool} test --no-pub" if is_dir("test") else f"{tool} analyze --no-pub"
    makefile = read("Makefile") if exists("Makefile") else None
    if makefile and any(re.match(r"^test:", line) for line in makefile.splitlines()):
        return "make test"
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


TEMPLATES = Path(__file__).resolve().parents[1] / "apps/api/project_templates"


def templates(base=None):
    """{id: metadata} of the starter templates (apps/api/project_templates)."""
    found = {}
    for meta in sorted(Path(base or TEMPLATES).glob("*/template.json")):
        try:
            data = json.loads(meta.read_text())
        except ValueError:
            continue
        found[meta.parent.name] = {"id": meta.parent.name, "name": data.get("name", meta.parent.name),
                                   "description": data.get("description", ""), "type": data.get("type", ""),
                                   "run_command": data.get("run_command", ""),
                                   # "lnmt": built here, run by LNMT (its Dockerfile), never by LAIka's apps service
                                   "deploys_to": data.get("deploys_to", "")}
    return found


def apply_template(template_id, dest, project_id, name, base=None):
    """Copy a template into dest with {{NAME}}, {{ID}} and {{PKG}} (the id
    as a Python package name) filled in, in file contents and paths. Returns
    the template's metadata."""
    base = Path(base or TEMPLATES)
    known = templates(base)
    if template_id not in known:
        raise ValueError(f"unknown template {template_id!r}")
    values = {"{{NAME}}": name, "{{ID}}": project_id, "{{PKG}}": re.sub(r"[^a-z0-9_]", "_", project_id.lower())}
    fill = lambda text: re.sub(r"\{\{(NAME|ID|PKG)\}\}", lambda m: values[m.group(0)], text)
    source = base / template_id
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if path.is_dir() or path.is_symlink() or rel.as_posix() == "template.json" or "__pycache__" in rel.parts:
            continue
        target = Path(dest) / fill(rel.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(fill(path.read_text()))
        except UnicodeDecodeError:
            target.write_bytes(path.read_bytes())
    return known[template_id]


def detect_run(read):
    """The command that runs a project's app, from its files, or "".
    read(name) returns a file's text on main (or None). Used when the owner
    left the run command empty ("off" there means: never run it)."""
    import json as _json
    package = read("package.json")
    if package:
        try:
            scripts = (_json.loads(package) or {}).get("scripts") or {}
        except ValueError:
            scripts = {}
        if isinstance(scripts, dict) and scripts.get("start"):
            return "npm start"
    procfile = read("Procfile")
    for line in (procfile or "").splitlines():
        if line.strip().startswith("web:"):
            return line.split(":", 1)[1].strip()
    python = "$(test -x .venv/bin/python && echo .venv/bin/python || echo python3)"
    if read("manage.py") is not None:
        return f"{python} manage.py runserver 0.0.0.0:$PORT"
    for name in ("app.py", "main.py", "server.py"):
        text = read(name)
        if text and "PORT" in text:  # a server that listens where LAIka tells it
            return f"{python} {name}"
    # A package started with `python -m <package>` (its __main__.py), named by
    # pyproject.toml's project name or its [project.scripts] entries.
    for package in _python_packages(read("pyproject.toml") or ""):
        for path in (f"{package}/__main__.py", f"src/{package}/__main__.py"):
            text = read(path)
            if text and "PORT" in text:
                return f"{python} -m {package}"
    return ""


def _python_packages(pyproject):
    """Candidate import names from pyproject.toml (no TOML parser needed on 3.10)."""
    import re as _re
    names = []
    section = ""
    for raw in pyproject.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line.strip("[] ").lower()
            continue
        if section == "project" and line.startswith("name"):
            found = _re.match(r"name\s*=\s*[\"']([A-Za-z0-9_.-]+)[\"']", line)
            if found:
                names.append(found.group(1).replace("-", "_").replace(".", "_").lower())
        elif section == "project.scripts":
            found = _re.match(r"[A-Za-z0-9_.-]+\s*=\s*[\"']([A-Za-z0-9_]+)", line)
            if found:
                names.append(found.group(1))
    return list(dict.fromkeys(name for name in names if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)))


PUSH_TIMEOUT = 120


def push_main(project, runner=None):
    """Push a project's main to its GitHub remote after it moved (approval,
    dashboard change, undo). Best effort: the merge is already recorded;
    returns "" or a warning. Uses the remote and deploy key set up by
    laika-project.py push-setup (origin + core.sshCommand); LAIka itself and
    projects without a push remote are never pushed."""
    import subprocess
    if project.is_builtin or not getattr(project, "push_remote", ""):
        return ""
    run = runner or subprocess.run
    try:
        result = run(["git", "-C", str(project.repo), "push", "-q", "origin",
                      f"refs/heads/{project.default_branch}:refs/heads/{project.default_branch}"],
                     capture_output=True, text=True, timeout=PUSH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"push to GitHub failed: {exc}"
    if result.returncode != 0:
        return f"push to GitHub failed: {(result.stderr or result.stdout).strip()[-300:]}"
    return ""


def verify_worktree_pointer(top, repo):
    """Raise unless top/.git is the pointer git wrote: a regular file naming a
    directory in repo/.git/worktrees. Project code can write its worktree;
    a replaced pointer (or a planted .git directory) would make LAIka's own,
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


PROJECTS_BASE = Path(os.environ.get("LAIKA_PROJECTS_BASE", "/var/lib/laika/projects"))


def builds_dir(project_id):
    """<PROJECTS_BASE>/<id>/builds: downloadable build results (scripts/laika-build.py)."""
    if not PROJECT_ID.fullmatch(project_id or "") or project_id == BUILTIN_PROJECT:
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
        key = f"laika:events:{project_id}"
        redis_client.lpush(key, json.dumps(entry))
        redis_client.ltrim(key, 0, EVENTS_KEEP - 1)
    except Exception:
        pass
    return entry
