#!/usr/bin/env python3
"""Manage the LAIka project registry and project checkouts."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment, before any configuration is read)
import laika_redis  # noqa: E402  (services/laika_redis.py)
import laika_projects  # noqa: E402  (services/laika_projects.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
import file_ops  # noqa: E402  (apps/api/file_ops.py, shared with the API)


PROJECT_SET = "laika:projects"
# Directories under this base are the only ones delete ever removes.
PROJECTS_BASE = Path(os.environ.get("LAIKA_PROJECTS_BASE", "/var/lib/laika/projects"))
SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")
DOCKER = os.environ.get("LAIKA_DOCKER", "docker")
KEYS_BASE = Path(os.environ.get("LAIKA_PROJECT_KEYS", "/etc/laika/project-keys"))
UPLOADS_BASE = Path(os.environ.get("LAIKA_UPLOADS", "/var/lib/laika/uploads"))
# Deleted projects wait here (root-only) for TRASH_HOURS before purge-trash
# removes them for good.
TRASH_BASE = Path(os.environ.get("LAIKA_TRASH", "/var/lib/laika/trash"))
TRASH_HOURS = float(os.environ.get("LAIKA_TRASH_HOURS", "24"))
TRASH_SET = "laika:trash"
ENV_BASE = Path(os.environ.get("LAIKA_PROJECT_ENV_DIR", "/etc/laika/project-env"))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ProjectError(Exception):
    pass


def get_redis():
    import redis
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=laika_redis.password(), decode_responses=True,
    )


def detect_gate(repo):
    return laika_projects.detect_gate(repo)


def run_git(args, cwd=None, env=None):
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, check=False,
                                capture_output=True, text=True)
    except OSError as exc:
        raise ProjectError(str(exc)) from exc
    if result.returncode:
        raise ProjectError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result


def validate_id(project_id):
    if not ID_RE.fullmatch(project_id):
        raise ProjectError("invalid project id")


def validate_importance(value):
    if value not in {"high", "medium", "low"}:
        raise ProjectError("importance must be high, medium or low")


def key_for(project_id):
    return f"laika:projects:{project_id}"


def now():
    return str(int(time.time()))


def project(redis_client, project_id):
    record = redis_client.hgetall(key_for(project_id))
    if not record:
        raise ProjectError("project not found")
    return record


def duplicate(redis_client, project_id):
    return redis_client.sismember(PROJECT_SET, project_id) or redis_client.exists(key_for(project_id))


def register(redis_client, record):
    redis_client.hset(key_for(record["id"]), mapping=record)
    redis_client.sadd(PROJECT_SET, record["id"])


def make_key(root, project_id):
    """The project's deploy key. New keys live in KEYS_BASE/<id>/ (root-only,
    outside the project directory, which the API container can read); a key
    made before that stays where it is."""
    legacy = Path(root) / "deploy_key"
    if legacy.exists():
        return legacy
    key_dir = KEYS_BASE / project_id
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = key_dir / "deploy_key"
    if not key.exists():
        run_command(["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
                     f"laika-project {project_id} deploy key", "-f", str(key)])
    try:
        key.chmod(0o600)
    except OSError as exc:
        raise ProjectError(str(exc)) from exc
    return key


def run_command(args, cwd=None, env=None):
    try:
        result = subprocess.run(args, cwd=cwd, env=env, check=False,
                                capture_output=True, text=True)
    except OSError as exc:
        raise ProjectError(str(exc)) from exc
    if result.returncode:
        raise ProjectError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result


def ssh_env(key):
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def base_record(project_id, name, importance, root, repo, worktrees, logs, gate=""):
    stamp = now()
    return {
        "id": project_id, "name": name, "importance": importance,
        "root": str(root), "repo": str(repo), "worktrees": str(worktrees),
        "logs": str(logs), "default_branch": "main", "gate_command": gate,
        "push_remote": "", "deploy_key": "", "status": "active",
        "created_at": stamp, "updated_at": stamp,
    }


def do_push_setup(redis_client, record, url):
    key = make_key(record["root"], record["id"])
    try:
        run_git(["remote", "get-url", "origin"], cwd=record["repo"])
        run_git(["remote", "set-url", "origin", url], cwd=record["repo"])
    except ProjectError:
        run_git(["remote", "add", "origin", url], cwd=record["repo"])
    run_git(["config", "core.sshCommand", f"ssh -i {key} -o IdentitiesOnly=yes"], cwd=record["repo"])
    record["push_remote"] = url
    record["deploy_key"] = str(key)
    record["updated_at"] = now()
    redis_client.hset(key_for(record["id"]), mapping=record)
    return record, Path(str(key) + ".pub").read_text()


def create(args):
    validate_id(args.id)
    validate_importance(args.importance)
    redis_client = get_redis()
    if duplicate(redis_client, args.id):
        raise ProjectError("project id already registered")
    root = Path(args.root_base).resolve() / args.id
    if root.exists():
        raise ProjectError("project root already exists")
    root.mkdir(parents=True)
    repo, worktrees, logs = root / "repo", root / "worktrees", root / "logs"
    repo.mkdir(); worktrees.mkdir(); logs.mkdir()
    record = base_record(args.id, args.name, args.importance, root, repo, worktrees, logs)
    clone_env = None
    is_ssh = False
    if args.clone:
        record["clone_url"] = args.clone
        if args.clone.startswith(("git@", "ssh://")):
            key = make_key(root, args.id)
            record["deploy_key"] = str(key)
            clone_env = ssh_env(key)
            is_ssh = True
        else:
            clone_env = os.environ.copy()
            clone_env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            run_git(["clone", args.clone, str(repo)], env=clone_env)
        except ProjectError as exc:
            if is_ssh:
                record["status"] = "pending_key"
                record["gate_command"] = ""
                record["updated_at"] = now()
                register(redis_client, record)
                output = dict(record)
                output["public_key"] = Path(record["deploy_key"] + ".pub").read_text()
                output["retry_command"] = f"python3 scripts/laika-project.py retry-clone {args.id}"
                return output
            raise exc
        record["gate_command"] = args.gate if args.gate is not None else detect_gate(repo)
    else:
        run_git(["init", "-b", "main"], cwd=repo)
        run_git(["config", "user.name", "LAIka"], cwd=repo)
        run_git(["config", "user.email", "laika@localhost"], cwd=repo)
        run_git(["commit", "--allow-empty", "-m", "Initialize project"], cwd=repo)
        record["gate_command"] = args.gate if args.gate is not None else detect_gate(repo)
    register(redis_client, record)
    if args.push_remote:
        record, _ = do_push_setup(redis_client, record, args.push_remote)
    return record


def register_existing(args):
    validate_id(args.id); validate_importance(args.importance)
    redis_client = get_redis()
    if duplicate(redis_client, args.id):
        raise ProjectError("project id already registered")
    repo = Path(args.repo).resolve()
    result = run_git(["-C", str(repo), "rev-parse", "--is-inside-work-tree"])
    if result.stdout.strip() != "true":
        raise ProjectError("repo is not a git work tree")
    record = base_record(args.id, args.name, args.importance, repo.parent, repo,
                         Path(args.worktrees).resolve(), Path(args.logs).resolve(),
                         args.gate if args.gate is not None else detect_gate(repo))
    register(redis_client, record)
    return record


def _json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _hash_keys(redis_client, pattern):
    """Hash keys only: laika:goals:<id>:planning is a string lock."""
    return [key for key in redis_client.scan_iter(pattern)
            if key.count(":") == 2 and redis_client.type(key) == "hash"]


def project_work(redis_client, project_id):
    """(job ids, goal ids) belonging to a project. Reviews, repairs and
    integrations may lack project_id and belong to their builder's project."""
    jobs = {key.split(":")[2]: redis_client.hgetall(key) for key in _hash_keys(redis_client, "laika:jobs:*")}
    owned = {jid for jid, job in jobs.items() if job.get("project_id") == project_id}
    for jid, job in jobs.items():
        if not job.get("project_id") and (job.get("builder_job_id") in owned or job.get("target_builder_id") in owned):
            owned.add(jid)
    goals = {key.split(":")[2] for key in _hash_keys(redis_client, "laika:goals:*")
             if redis_client.hget(key, "project_id") == project_id}
    return owned, goals


def busy_work(redis_client, project_id, job_ids, goal_ids):
    """Work of this project that is running right now and cannot be dropped."""
    busy = []
    for key in redis_client.scan_iter("laika:workers:*"):
        if redis_client.type(key) == "hash":
            held = redis_client.hget(key, "job_id")
            if held and held in job_ids:
                busy.append(f"job {held} (running on {key.split(':', 2)[2]})")
    for goal_id in sorted(goal_ids):
        if redis_client.exists(f"laika:goals:{goal_id}:planning"):
            busy.append(f"goal {goal_id} (being planned)")
    return busy


def forget_builds(r, project_id):
    """Build records, the detected type and the package cache volume. The
    build zips live in the project directory and go to the trash with it."""
    build_ids = r.lrange(f"laika:builds:{project_id}", 0, -1) or []
    for key in [f"laika:build:{project_id}:{b}" for b in build_ids] + [
            f"laika:builds:{project_id}", f"laika:build-request:{project_id}", f"laika:project-type:{project_id}"]:
        r.delete(key)
    try:
        subprocess.run([DOCKER, "volume", "rm", "-f", f"laika-build-cache-{project_id}"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pass


def owned_root(record):
    """The project's directory if LAIka created it (exactly <PROJECTS_BASE>/<id>,
    a real directory, not a symlink), else None. Registered existing
    checkouts live elsewhere and are never deleted."""
    root = Path(record.get("root") or "")
    expected = PROJECTS_BASE / record["id"]
    if str(root) != str(expected) or root.is_symlink() or not root.is_dir():
        return None
    if root.resolve() != expected.resolve() or expected.resolve().parent != PROJECTS_BASE.resolve():
        return None
    return root


TRASH_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}-\d{8}T\d{6}Z$")


def _move_into(source, target):
    """Move a directory (same filesystem: an instant rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _owned_dir(base, project_id):
    target = base / project_id
    if target.is_dir() and not target.is_symlink() and target.resolve().parent == base.resolve():
        return target
    return None


def delete(args):
    """Move a project to the trash (TRASH_BASE, kept TRASH_HOURS): its
    records, queued work and (only if LAIka created them) its directory, app
    data and deploy key, so `restore` can bring it all back. Refuses while
    its work is running. The trash is emptied by purge-trash."""
    validate_id(args.id)
    if args.id == "laika":
        raise ProjectError("LAIka itself cannot be deleted")
    if args.confirm != args.id:
        raise ProjectError("confirmation does not match the project id")
    r = get_redis()
    record = project(r, args.id)
    kids = laika_projects.children(r, args.id)
    if kids:
        raise ProjectError(f"{args.id} has child projects ({', '.join(kids)}); detach or delete them first")
    previous = record.get("status") or "active"
    # Stop new work first: workers drop jobs of a deleting project.
    r.hset(key_for(args.id), mapping={"status": "deleting", "updated_at": now()})
    job_ids, goal_ids = project_work(r, args.id)
    busy = busy_work(r, args.id, job_ids, goal_ids)
    if busy:
        r.hset(key_for(args.id), mapping={"status": previous, "updated_at": now()})
        raise ProjectError("still running: " + ", ".join(busy) + ". Nothing was deleted; try again when it finishes")

    # Its running app (services/apps/laika_apps.py) and builds, if any.
    for verb in ("stop", "reset-failed"):  # a crashed app's unit stays "failed" until reset
        for unit in (f"laika-app-{args.id}", f"laika-build-{args.id}-*"):
            try:
                subprocess.run([SYSTEMCTL, verb, unit], capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
    forget_builds(r, args.id)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    trash_id = f"{args.id}-{stamp}"
    trash = TRASH_BASE / trash_id
    trash.mkdir(parents=True, mode=0o700)
    saved = {"project": {**record, "status": previous}, "stats": r.hgetall(f"laika:project-stats:{args.id}"),
             "jobs": {jid: r.hgetall(f"laika:jobs:{jid}") for jid in sorted(job_ids)},
             "goals": {gid: r.hgetall(f"laika:goals:{gid}") for gid in sorted(goal_ids)},
             "merge_queue": r.lrange(f"laika:merge-queue:{args.id}", 0, -1), "queued": {}}
    for queue, owned in (("laika:jobs", job_ids), ("laika:goals", goal_ids)):
        for raw in r.lrange(queue, 0, -1):
            payload = _json(raw)
            if payload.get("id") in owned or payload.get("project_id") == args.id:
                saved["queued"].setdefault(queue, []).append(raw)
                r.lrem(queue, 0, raw)

    result = {"id": args.id, "status": "trashed", "trash_id": trash_id,
              "jobs_removed": len(job_ids), "goals_removed": len(goal_ids)}
    moved = {}
    root = owned_root(record)
    if root is not None:
        _move_into(root, trash / "project")
        moved["project"] = str(root)
    else:
        result["kept_paths"] = sorted({record.get(k, "") for k in ("repo", "worktrees", "logs") if record.get(k)})
    for base, name in ((laika_projects.DATA_BASE, "data"), (KEYS_BASE, "key")):
        target = _owned_dir(base, args.id)
        if target is not None:
            _move_into(target, trash / name)
            moved[name] = str(target)
    env_file = ENV_BASE / f"{args.id}.env"
    if env_file.is_file() and not env_file.is_symlink():
        _move_into(env_file, trash / "env")
        moved["env"] = str(env_file)
    saved["moved"] = moved
    (trash / "records.json").write_text(json.dumps(saved))

    keys = [f"laika:jobs:{jid}" for jid in job_ids] + [f"laika:integration-lock:{jid}" for jid in job_ids]
    keys += [f"laika:goals:{gid}" for gid in goal_ids]
    keys += [key_for(args.id), f"laika:project-stats:{args.id}", f"laika:merge-queue:{args.id}",
             f"laika:main-head:{args.id}", f"laika:approval-lock:{args.id}", f"laika:app-status:{args.id}"]
    for key in keys:
        r.delete(key)
    r.srem(PROJECT_SET, args.id)
    deleted_at = time.time()
    r.hset(f"laika:trash:{trash_id}", mapping={
        "trash_id": trash_id, "project_id": args.id, "name": record.get("name") or args.id,
        "deleted_at": str(deleted_at), "expires_at": str(deleted_at + TRASH_HOURS * 3600)})
    r.sadd(TRASH_SET, trash_id)
    result["expires_in_hours"] = TRASH_HOURS
    if record.get("push_remote"):
        result["note"] = "If you restore nothing, remove the project's deploy key from GitHub afterwards"
    return result


def restore(args):
    """Bring a trashed project back exactly as it was deleted."""
    if not TRASH_ID.fullmatch(args.trash_id or ""):
        raise ProjectError("invalid trash id")
    r = get_redis()
    trash = TRASH_BASE / args.trash_id
    if not r.sismember(TRASH_SET, args.trash_id) or not (trash / "records.json").is_file():
        raise ProjectError("not in the trash (it may have been emptied)")
    saved = json.loads((trash / "records.json").read_text())
    record = saved["project"]
    project_id = record["id"]
    if duplicate(r, project_id):
        raise ProjectError(f"a project named {project_id} exists now; delete or rename it first")
    for name, original in saved.get("moved", {}).items():
        if os.path.lexists(original):
            raise ProjectError(f"{original} exists now; move it away first")
    for name, original in saved.get("moved", {}).items():
        _move_into(trash / name, Path(original))
    # A port taken by another project meanwhile is given up (reassigned).
    port = record.get("run_port")
    if port and any(r.hget(key_for(other), "run_port") == port for other in r.smembers(PROJECT_SET)):
        record.pop("run_port")
    record["updated_at"] = now()
    parent = record.get("parent")
    if parent and (parent == project_id or not r.hgetall(key_for(parent))):
        record.pop("parent")  # its parent is gone: it comes back on its own
    r.hset(key_for(project_id), mapping=record)
    if saved.get("stats"):
        r.hset(f"laika:project-stats:{project_id}", mapping=saved["stats"])
    for prefix, items in (("laika:jobs", saved.get("jobs", {})), ("laika:goals", saved.get("goals", {}))):
        for item_id, data in items.items():
            if data:
                r.hset(f"{prefix}:{item_id}", mapping=data)
    for job_id in saved.get("merge_queue", []):
        r.rpush(f"laika:merge-queue:{project_id}", job_id)
    for queue, entries in saved.get("queued", {}).items():
        for raw in entries:
            r.rpush(queue, raw)
    r.sadd(PROJECT_SET, project_id)
    laika_projects.record_event(r, project_id, "restored", "Restored from the trash")
    r.delete(f"laika:trash:{args.trash_id}")
    r.srem(TRASH_SET, args.trash_id)
    shutil.rmtree(trash)
    return {"id": project_id, "status": "restored", "trash_id": args.trash_id,
            "jobs_restored": len(saved.get("jobs", {})), "goals_restored": len(saved.get("goals", {}))}


def purge_trash(args=None, clock=time.time):
    """Permanently remove trash older than its expiry (and stray trash
    directories with no record, after TRASH_HOURS). Only names that look
    like trash ids, directly inside TRASH_BASE, are ever removed."""
    r = get_redis()
    removed = []
    known = set(r.smembers(TRASH_SET))
    for trash_id in sorted(known):
        info = r.hgetall(f"laika:trash:{trash_id}")
        try:
            expired = float(info.get("expires_at") or 0) <= clock()
        except ValueError:
            expired = True
        if not expired:
            continue
        target = TRASH_BASE / trash_id
        if TRASH_ID.fullmatch(trash_id) and target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        r.delete(f"laika:trash:{trash_id}")
        r.srem(TRASH_SET, trash_id)
        removed.append(trash_id)
    if TRASH_BASE.is_dir():
        for entry in TRASH_BASE.iterdir():
            if (entry.name not in known and TRASH_ID.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
                    and entry.stat().st_mtime < clock() - TRASH_HOURS * 3600):
                shutil.rmtree(entry)
                removed.append(entry.name)
    return {"status": "purged", "removed": removed}


def list_trash(args=None):
    r = get_redis()
    items = [r.hgetall(f"laika:trash:{trash_id}") for trash_id in sorted(r.smembers(TRASH_SET))]
    return [item for item in items if item]


UPLOAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def repo_path_parts(rel):
    """A file path inside a repository, as parts; refuses anything that is
    absolute, climbs out, touches .git or contains control characters."""
    rel = rel or ""
    parts = PurePosixPath(rel).parts
    if (not rel or len(rel) > 400 or rel.startswith("/") or "\\" in rel
            or any(ord(c) < 32 for c in rel) or not parts
            or any(part in ("", ".", "..", ".git") for part in parts)):
        raise ProjectError(f"invalid path: {rel!r}")
    return parts


def change_main(record, project_id, change, message_for):
    """Run change(repo) on the project's main checkout and commit whatever it
    changed as "LAIka operator" (hooks off). Holds the project's approval lock
    so it never races a merge; refuses unless the checkout is clean on main.
    change returns a result dict; message_for(result) is the commit message."""
    repo = Path(record["repo"])
    r = get_redis()
    lock_key, token = f"laika:approval-lock:{project_id}", uuid.uuid4().hex
    if not r.set(lock_key, token, nx=True, ex=300):
        raise ProjectError("main is being advanced by an approval right now; try again in a moment")
    try:
        branch = run_git(["symbolic-ref", "--short", "HEAD"], cwd=repo).stdout.strip()
        if branch != (record.get("default_branch") or "main"):
            raise ProjectError(f"the project's checkout is on {branch!r}, not its main branch")
        if run_git(["status", "--porcelain"], cwd=repo).stdout.strip():
            raise ProjectError("the project's main checkout has uncommitted changes")
        try:
            result = change(repo)
            run_git(["add", "-A"], cwd=repo)
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
                return {"id": project_id, "status": "unchanged", **result}
            who = {"GIT_AUTHOR_NAME": "LAIka operator", "GIT_AUTHOR_EMAIL": "operator@laika.local",
                   "GIT_COMMITTER_NAME": "LAIka operator", "GIT_COMMITTER_EMAIL": "operator@laika.local"}
            run_git(["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message_for(result)],
                    cwd=repo, env={**os.environ, **who})
        except BaseException:
            # All or nothing: the checkout was clean before, so put it back.
            subprocess.run(["git", "reset", "-q", "--hard", "HEAD"], cwd=repo, capture_output=True)
            subprocess.run(["git", "clean", "-q", "-fd"], cwd=repo, capture_output=True)
            raise
        head = run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        kind = "undo" if message_for(result).startswith("Undo ") else "code_change"
        laika_projects.record_event(r, project_id, kind, message_for(result).removesuffix(" from the dashboard"),
                                  ref=head[:12])
        warning = (laika_projects.push_main(laika_projects.load(r, project_id))
                   if r.hget(key_for(project_id), "push_remote") else "")
        return {"id": project_id, "status": "committed", "commit": head, **result,
                **({"push_warning": warning} if warning else {})}
    finally:
        r.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
               "else return 0 end", 1, lock_key, token)


def commit_upload(args):
    """Commit a file uploaded from the dashboard (staged by the API in
    UPLOADS_BASE/<upload>/file) to the project's main, as the operator."""
    validate_id(args.id)
    if args.id == "laika":
        raise ProjectError("LAIka's own code cannot be changed by upload")
    if not UPLOAD_ID.fullmatch(args.upload or ""):
        raise ProjectError("invalid upload id")
    staged_dir = UPLOADS_BASE / args.upload
    staged = staged_dir / "file"
    try:
        parts = repo_path_parts(args.path)
        record = project(get_redis(), args.id)
        if staged.is_symlink() or not staged.is_file():
            raise ProjectError("uploaded file not found (it may have expired)")

        def place(repo):
            current = repo
            for part in parts[:-1]:
                current = current / part
                if current.is_symlink() or (current.exists() and not current.is_dir()):
                    raise ProjectError(f"{args.path}: a parent is a file or a link")
            dest = repo.joinpath(*parts)
            if os.path.lexists(dest):
                choice = args.on_conflict
                if choice == "skip":
                    return {"path": "/".join(parts), "skipped": True}
                if choice == "keep":
                    dest = dest.parent / file_ops.free_name(dest.parent, dest.name)
                elif choice == "overwrite":
                    if dest.is_symlink() or dest.is_dir():
                        raise ProjectError(f"{args.path} is a folder or a link")
                else:
                    raise ProjectError(f"conflict: {json.dumps([dest.name])}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, dest)
            return {"path": dest.relative_to(repo).as_posix()}

        return change_main(record, args.id, place, lambda result: f"Upload {result['path']} from the dashboard")
    finally:
        if staged_dir.is_dir() and not staged_dir.is_symlink() and staged_dir.parent == UPLOADS_BASE:
            shutil.rmtree(staged_dir, ignore_errors=True)


CHANGE_MESSAGES = {
    "mkdir": lambda r: f"Create folder {r['path']} from the dashboard",
    "rename": lambda r: f"Rename {r['from']} to {r['path']} from the dashboard",
    "move": lambda r: f"Move {r['from']} to {r['path']} from the dashboard",
    "copy": lambda r: f"Copy {r['from']} to {r['path']} from the dashboard",
    "delete": lambda r: f"Delete {r['path']} from the dashboard",
    "zip": lambda r: f"Zip {r['from']} into {r['path']} from the dashboard",
    "unzip": lambda r: f"Unzip {r['from']} into {r['path']} from the dashboard",
}


def _count(n, what="item"):
    return f"{n} {what}{'' if n == 1 else 's'}"


def code_batch(args):
    """A dashboard batch that changes a project's code, as one commit on main
    (all or nothing). spec (JSON): op copy|move|delete|zip|rename, from_area
    and to_area (code|data), paths, dest (folder, or new name for rename),
    name (zip), resolutions {name: overwrite|skip|keep}, default."""
    validate_id(args.id)
    if args.id == "laika":
        raise ProjectError("LAIka's own code cannot be changed from the file browser")
    try:
        spec = json.loads(args.spec)
    except ValueError as exc:
        raise ProjectError(f"invalid batch: {exc}") from exc
    if not isinstance(spec, dict):
        raise ProjectError("invalid batch")
    op = spec.get("op")
    src_area, dst_area = spec.get("from_area", "code"), spec.get("to_area") or spec.get("from_area", "code")
    paths = spec.get("paths") or []
    if op not in ("copy", "move", "delete", "zip", "rename") or src_area not in ("code", "data") \
            or dst_area not in ("code", "data") or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ProjectError("invalid batch")
    if "code" not in (dst_area, src_area if op == "move" else dst_area):
        raise ProjectError("this batch does not change code; the dashboard does it directly")
    record = project(get_redis(), args.id)
    data_root = laika_projects.data_dir(args.id)
    data_root.mkdir(parents=True, exist_ok=True)
    dest, resolutions, default = spec.get("dest") or "", spec.get("resolutions") or {}, spec.get("default")
    moved_data_sources = []

    def root_of(area, repo):
        return repo if area == "code" else data_root

    def change(repo):
        try:
            if op == "delete":
                return file_ops.delete_many(repo, paths, forbid_git=True)
            if op == "zip":
                return file_ops.zip_many(repo, paths, dest, spec.get("name") or "Archive.zip", True, default)
            if op == "rename":
                if len(paths) != 1:
                    raise ProjectError("rename takes exactly one item")
                return {"done": [file_ops.rename(repo, paths[0], dest, True, default)], "skipped": []}
            src, dst = root_of(src_area, repo), root_of(dst_area, repo)
            if op == "move" and src_area == "data":
                # Data -> code: copy now, delete the data originals only
                # after the commit succeeded (never lose them).
                result = file_ops.transfer(src, paths, dst, dest, "copy", False, True, resolutions, default)
                moved_data_sources.extend(item["from"] for item in result["done"])
                return result
            if op == "move" and dst_area == "data":
                # Code -> data: copy out, then delete from the code (committed).
                result = file_ops.transfer(src, paths, dst, dest, "copy", True, False, resolutions, default)
                file_ops.delete_many(repo, [item["from"] for item in result["done"]], forbid_git=True)
                return result
            return file_ops.transfer(src, paths, dst, dest, op, src_area == "code", dst_area == "code",
                                     resolutions, default)
        except file_ops.Conflict as exc:
            raise ProjectError(f"conflict: {json.dumps(exc.conflicts)}") from exc
        except file_ops.FileOpError as exc:
            raise ProjectError(str(exc)) from exc

    def message(result):
        n = len(result.get("done", []))
        if op == "delete":
            return f"Delete {_count(n)} from the dashboard"
        if op == "zip":
            return f"Zip {_count(n)} into {result.get('path')} from the dashboard"
        if op == "rename":
            item = result["done"][0]
            return f"Rename {item['from']} to {item['path']} from the dashboard"
        origin = " from app data" if src_area == "data" else (" to app data" if dst_area == "data" else "")
        verb = "Move" if op == "move" else "Copy"
        where = f" into {dest}/" if dest and dst_area == "code" else ""
        return f"{verb} {_count(n)}{origin}{where} from the dashboard"

    result = change_main(record, args.id, change, message)
    left = []
    for rel in moved_data_sources:
        try:
            file_ops.remove(file_ops.resolve(data_root, rel)[0])
        except (OSError, file_ops.FileOpError):
            left.append(rel)
    if left:
        result["not_removed_from_data"] = left
    return result


SHA = re.compile(r"^[0-9a-f]{7,40}$")


def revert(args):
    """Undo one change on the project's main with a new commit (history is
    kept): a LAIka job (every commit it merged, as one undo) or a single
    commit. Refuses when later changes touch the same lines; nothing is
    changed then."""
    validate_id(args.id)
    if args.id == "laika":
        raise ProjectError("LAIka's own changes are undone through its normal review, not from the dashboard")
    r = get_redis()
    record = project(r, args.id)
    if args.job:
        job = r.hgetall(f"laika:jobs:{args.job}") if re.fullmatch(r"[A-Za-z0-9]{1,64}", args.job) else {}
        if not job or (job.get("project_id") or "laika") != args.id or job.get("status") != "merged":
            raise ProjectError("not a merged job of this project")
        base, merged = job.get("integration_base_commit", ""), job.get("merge_commit", "")
        if not (SHA.fullmatch(base) and SHA.fullmatch(merged)):
            raise ProjectError("this job's merge is not recorded; undo its commits one by one")
        target, label = f"{base}..{merged}", f"{job.get('title') or args.job} (job {args.job})"
    else:
        if not SHA.fullmatch(args.commit or ""):
            raise ProjectError("invalid commit")
        target, label = args.commit, None

    def change(repo):
        tip = target.split("..")[-1]
        if subprocess.run(["git", "merge-base", "--is-ancestor", tip, "HEAD"], cwd=repo).returncode != 0:
            raise ProjectError("that change is not on main")
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "revert", "--no-commit", "--no-edit", target],
                                cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            subprocess.run(["git", "revert", "--abort"], cwd=repo, capture_output=True)
            raise ProjectError("it cannot be undone automatically: later changes touch the same lines")
        subject = label or run_git(["log", "-1", "--format=%s", target], cwd=repo).stdout.strip()
        return {"undone": target, "title": subject.removesuffix(" from the dashboard")}

    def message(result):
        return f"Undo {result['title']} from the dashboard"

    return change_main(record, args.id, change, message)


def code_change(args):
    """A file-browser operation (file_ops) on the project's main, committed."""
    validate_id(args.id)
    if args.id == "laika":
        raise ProjectError("LAIka's own code cannot be changed from the file browser")
    if args.op not in CHANGE_MESSAGES:
        raise ProjectError(f"unknown operation: {args.op}")
    record = project(get_redis(), args.id)

    def change(repo):
        try:
            return file_ops.apply(repo, args.op, args.path, args.dest or "", forbid_git=True, keep_file=True)
        except file_ops.FileOpError as exc:
            raise ProjectError(str(exc)) from exc

    return change_main(record, args.id, change, CHANGE_MESSAGES[args.op])


def retry_clone(args):
    validate_id(args.id)
    redis_client = get_redis(); record = project(redis_client, args.id)
    if record.get("status") != "pending_key":
        raise ProjectError("project is not pending_key")
    repo = Path(record["repo"]); key = record.get("deploy_key", "")
    url = record.get("clone_url", "")
    if repo.exists() and any(repo.iterdir()):
        try:
            origin = run_git(["-C", str(repo), "config", "--get", "remote.origin.url"]).stdout.strip()
        except ProjectError:
            raise ProjectError("clone directory is non-empty and not a git repo")
        if not url: url = origin
        run_git(["-C", str(repo), "fetch", "origin"], env=ssh_env(key))
    else:
        if not url: raise ProjectError("stored clone URL is missing")
        run_git(["clone", url, str(repo)], env=ssh_env(key))
    record["status"] = "active"
    if not record.get("gate_command"):
        record["gate_command"] = detect_gate(repo)
    record["updated_at"] = now()
    redis_client.hset(key_for(args.id), mapping=record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create"); p.add_argument("--id", required=True); p.add_argument("--name", required=True); p.add_argument("--importance", default="medium"); p.add_argument("--gate"); p.add_argument("--push-remote"); p.add_argument("--root-base", default="/var/lib/laika/projects"); source = p.add_mutually_exclusive_group(required=True); source.add_argument("--empty", action="store_true"); source.add_argument("--clone")
    p = sub.add_parser("register-existing"); p.add_argument("--id", required=True); p.add_argument("--name", required=True); p.add_argument("--repo", required=True); p.add_argument("--worktrees", required=True); p.add_argument("--logs", required=True); p.add_argument("--gate"); p.add_argument("--importance", default="medium")
    p = sub.add_parser("retry-clone"); p.add_argument("id")
    p = sub.add_parser("push-setup"); p.add_argument("id"); p.add_argument("url")
    p = sub.add_parser("set-importance"); p.add_argument("id"); p.add_argument("level")
    p = sub.add_parser("archive"); p.add_argument("id")
    p = sub.add_parser("delete"); p.add_argument("id"); p.add_argument("--confirm", required=True, help="the project id again")
    p = sub.add_parser("restore"); p.add_argument("trash_id")
    sub.add_parser("purge-trash")
    sub.add_parser("trash")
    p = sub.add_parser("commit-upload"); p.add_argument("id"); p.add_argument("--path", required=True); p.add_argument("--upload", required=True); p.add_argument("--on-conflict", default="ask", choices=["ask", "overwrite", "skip", "keep"])
    p = sub.add_parser("code-change"); p.add_argument("id"); p.add_argument("--op", required=True); p.add_argument("--path", required=True); p.add_argument("--dest", default="")
    p = sub.add_parser("code-batch"); p.add_argument("id"); p.add_argument("--spec", required=True, help="JSON batch from the dashboard")
    p = sub.add_parser("revert"); p.add_argument("id"); target = p.add_mutually_exclusive_group(required=True); target.add_argument("--job"); target.add_argument("--commit")
    p = sub.add_parser("show"); p.add_argument("id")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        if args.command == "create": result = create(args)
        elif args.command == "register-existing": result = register_existing(args)
        elif args.command == "retry-clone": result = retry_clone(args)
        elif args.command == "push-setup":
            if args.id == "laika":
                raise ProjectError("LAIka's own remote and key are set up by hand on the host (git remote in its checkout)")
            validate_id(args.id); r = get_redis(); record = project(r, args.id); record, public = do_push_setup(r, record, args.url); result = dict(record, public_key=public, instruction="Add this public key to the GitHub repository as a deploy key with write access")
        elif args.command == "set-importance":
            validate_id(args.id); validate_importance(args.level); r = get_redis(); record = project(r, args.id); record["importance"] = args.level; record["updated_at"] = now(); r.hset(key_for(args.id), mapping=record); result = record
        elif args.command == "archive":
            validate_id(args.id); r = get_redis(); record = project(r, args.id); record["status"] = "archived"; record["updated_at"] = now(); r.hset(key_for(args.id), mapping=record); result = record
        elif args.command == "delete":
            result = delete(args)
        elif args.command == "restore":
            result = restore(args)
        elif args.command == "purge-trash":
            result = purge_trash(args)
        elif args.command == "trash":
            result = list_trash(args)
        elif args.command == "commit-upload":
            result = commit_upload(args)
        elif args.command == "code-change":
            result = code_change(args)
        elif args.command == "code-batch":
            result = code_batch(args)
        elif args.command == "revert":
            result = revert(args)
        elif args.command == "show":
            validate_id(args.id); result = project(get_redis(), args.id)
        else:
            r = get_redis(); result = [r.hgetall(key_for(i)) for i in sorted(r.smembers(PROJECT_SET))]
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ProjectError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
