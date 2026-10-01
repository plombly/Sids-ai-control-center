#!/usr/bin/env python3
"""Manage the SID project registry and project checkouts."""

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
import sid_redis  # noqa: E402  (services/sid_redis.py)
import sid_projects  # noqa: E402  (services/sid_projects.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
import file_ops  # noqa: E402  (apps/api/file_ops.py, shared with the API)


PROJECT_SET = "sid:projects"
# Directories under this base are the only ones delete ever removes.
PROJECTS_BASE = Path(os.environ.get("SID_PROJECTS_BASE", "/opt/sid-projects"))
SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")
KEYS_BASE = Path(os.environ.get("SID_PROJECT_KEYS", "/etc/sid-ai/project-keys"))
UPLOADS_BASE = Path(os.environ.get("SID_UPLOADS", "/opt/sid-uploads"))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ProjectError(Exception):
    pass


def get_redis():
    import redis
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=sid_redis.password(), decode_responses=True,
    )


def detect_gate(repo):
    return sid_projects.detect_gate(repo)


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
    return f"sid:projects:{project_id}"


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
                     f"sid-project {project_id} deploy key", "-f", str(key)])
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
                output["retry_command"] = f"python3 scripts/sid-project.py retry-clone {args.id}"
                return output
            raise exc
        record["gate_command"] = args.gate if args.gate is not None else detect_gate(repo)
    else:
        run_git(["init", "-b", "main"], cwd=repo)
        run_git(["config", "user.name", "SID"], cwd=repo)
        run_git(["config", "user.email", "sid@localhost"], cwd=repo)
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
    """Hash keys only: sid:goals:<id>:planning is a string lock."""
    return [key for key in redis_client.scan_iter(pattern)
            if key.count(":") == 2 and redis_client.type(key) == "hash"]


def project_work(redis_client, project_id):
    """(job ids, goal ids) belonging to a project. Reviews, repairs and
    integrations may lack project_id and belong to their builder's project."""
    jobs = {key.split(":")[2]: redis_client.hgetall(key) for key in _hash_keys(redis_client, "sid:jobs:*")}
    owned = {jid for jid, job in jobs.items() if job.get("project_id") == project_id}
    for jid, job in jobs.items():
        if not job.get("project_id") and (job.get("builder_job_id") in owned or job.get("target_builder_id") in owned):
            owned.add(jid)
    goals = {key.split(":")[2] for key in _hash_keys(redis_client, "sid:goals:*")
             if redis_client.hget(key, "project_id") == project_id}
    return owned, goals


def busy_work(redis_client, project_id, job_ids, goal_ids):
    """Work of this project that is running right now and cannot be dropped."""
    busy = []
    for key in redis_client.scan_iter("sid:workers:*"):
        if redis_client.type(key) == "hash":
            held = redis_client.hget(key, "job_id")
            if held and held in job_ids:
                busy.append(f"job {held} (running on {key.split(':', 2)[2]})")
    for goal_id in sorted(goal_ids):
        if redis_client.exists(f"sid:goals:{goal_id}:planning"):
            busy.append(f"goal {goal_id} (being planned)")
    return busy


def owned_root(record):
    """The project's directory if SID created it (exactly <PROJECTS_BASE>/<id>,
    a real directory, not a symlink), else None. Registered existing
    checkouts live elsewhere and are never deleted."""
    root = Path(record.get("root") or "")
    expected = PROJECTS_BASE / record["id"]
    if str(root) != str(expected) or root.is_symlink() or not root.is_dir():
        return None
    if root.resolve() != expected.resolve() or expected.resolve().parent != PROJECTS_BASE.resolve():
        return None
    return root


def delete(args):
    """Remove a project from the server: its queued work, job/goal records,
    Redis keys and (only if SID created it) its directory with repo,
    worktrees, logs and deploy key. Refuses while its work is running."""
    validate_id(args.id)
    if args.id == "sid":
        raise ProjectError("SID itself cannot be deleted")
    if args.confirm != args.id:
        raise ProjectError("confirmation does not match the project id")
    r = get_redis()
    record = project(r, args.id)
    previous = record.get("status") or "active"
    # Stop new work first: workers drop jobs of a deleting project.
    r.hset(key_for(args.id), mapping={"status": "deleting", "updated_at": now()})
    job_ids, goal_ids = project_work(r, args.id)
    busy = busy_work(r, args.id, job_ids, goal_ids)
    if busy:
        r.hset(key_for(args.id), mapping={"status": previous, "updated_at": now()})
        raise ProjectError("still running: " + ", ".join(busy) + ". Nothing was deleted; try again when it finishes")

    # Its running app (services/apps/sid_apps.py), if any.
    try:
        subprocess.run([SYSTEMCTL, "stop", f"sid-app-{args.id}"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    for queue, owned in (("sid:jobs", job_ids), ("sid:goals", goal_ids)):
        for raw in r.lrange(queue, 0, -1):
            payload = _json(raw)
            if payload.get("id") in owned or payload.get("project_id") == args.id:
                r.lrem(queue, 0, raw)
    keys = [f"sid:jobs:{jid}" for jid in job_ids] + [f"sid:integration-lock:{jid}" for jid in job_ids]
    keys += [f"sid:goals:{gid}" for gid in goal_ids]
    keys += [key_for(args.id), f"sid:project-stats:{args.id}", f"sid:merge-queue:{args.id}",
             f"sid:main-head:{args.id}", f"sid:approval-lock:{args.id}", f"sid:app-status:{args.id}"]
    for key in keys:
        r.delete(key)
    r.srem(PROJECT_SET, args.id)

    result = {"id": args.id, "status": "deleted", "jobs_removed": len(job_ids), "goals_removed": len(goal_ids)}
    root = owned_root(record)
    if root is not None:
        shutil.rmtree(root)
        result["removed_path"] = str(root)
    else:
        result["kept_paths"] = sorted({record.get(k, "") for k in ("repo", "worktrees", "logs") if record.get(k)})
    # Its app data and deploy key: exactly <base>/<id>, real directories only.
    for base, name in ((sid_projects.DATA_BASE, "data"), (KEYS_BASE, "key")):
        target = base / args.id
        if target.is_dir() and not target.is_symlink() and target.resolve().parent == base.resolve():
            shutil.rmtree(target)
            result[f"removed_{name}"] = str(target)
    if record.get("push_remote"):
        result["note"] = "Remove the project's deploy key from the GitHub repository settings"
    return result


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
    changed as "SID operator" (hooks off). Holds the project's approval lock
    so it never races a merge; refuses unless the checkout is clean on main.
    change returns a result dict; message_for(result) is the commit message."""
    repo = Path(record["repo"])
    r = get_redis()
    lock_key, token = f"sid:approval-lock:{project_id}", uuid.uuid4().hex
    if not r.set(lock_key, token, nx=True, ex=300):
        raise ProjectError("main is being advanced by an approval right now; try again in a moment")
    try:
        branch = run_git(["symbolic-ref", "--short", "HEAD"], cwd=repo).stdout.strip()
        if branch != (record.get("default_branch") or "main"):
            raise ProjectError(f"the project's checkout is on {branch!r}, not its main branch")
        if run_git(["status", "--porcelain"], cwd=repo).stdout.strip():
            raise ProjectError("the project's main checkout has uncommitted changes")
        result = change(repo)
        run_git(["add", "-A"], cwd=repo)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
            return {"id": project_id, "status": "unchanged", **result}
        who = {"GIT_AUTHOR_NAME": "SID operator", "GIT_AUTHOR_EMAIL": "operator@sid.local",
               "GIT_COMMITTER_NAME": "SID operator", "GIT_COMMITTER_EMAIL": "operator@sid.local"}
        run_git(["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message_for(result)],
                cwd=repo, env={**os.environ, **who})
        head = run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        return {"id": project_id, "status": "committed", "commit": head, **result}
    finally:
        r.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
               "else return 0 end", 1, lock_key, token)


def commit_upload(args):
    """Commit a file uploaded from the dashboard (staged by the API in
    UPLOADS_BASE/<upload>/file) to the project's main, as the operator."""
    validate_id(args.id)
    if args.id == "sid":
        raise ProjectError("SID's own code cannot be changed by upload")
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
            if dest.is_symlink() or dest.is_dir():
                raise ProjectError(f"{args.path} is a folder or a link")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, dest)
            return {"path": "/".join(parts)}

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


def code_change(args):
    """A file-browser operation (file_ops) on the project's main, committed."""
    validate_id(args.id)
    if args.id == "sid":
        raise ProjectError("SID's own code cannot be changed from the file browser")
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
    p = sub.add_parser("create"); p.add_argument("--id", required=True); p.add_argument("--name", required=True); p.add_argument("--importance", default="medium"); p.add_argument("--gate"); p.add_argument("--push-remote"); p.add_argument("--root-base", default="/opt/sid-projects"); source = p.add_mutually_exclusive_group(required=True); source.add_argument("--empty", action="store_true"); source.add_argument("--clone")
    p = sub.add_parser("register-existing"); p.add_argument("--id", required=True); p.add_argument("--name", required=True); p.add_argument("--repo", required=True); p.add_argument("--worktrees", required=True); p.add_argument("--logs", required=True); p.add_argument("--gate"); p.add_argument("--importance", default="medium")
    p = sub.add_parser("retry-clone"); p.add_argument("id")
    p = sub.add_parser("push-setup"); p.add_argument("id"); p.add_argument("url")
    p = sub.add_parser("set-importance"); p.add_argument("id"); p.add_argument("level")
    p = sub.add_parser("archive"); p.add_argument("id")
    p = sub.add_parser("delete"); p.add_argument("id"); p.add_argument("--confirm", required=True, help="the project id again")
    p = sub.add_parser("commit-upload"); p.add_argument("id"); p.add_argument("--path", required=True); p.add_argument("--upload", required=True)
    p = sub.add_parser("code-change"); p.add_argument("id"); p.add_argument("--op", required=True); p.add_argument("--path", required=True); p.add_argument("--dest", default="")
    p = sub.add_parser("show"); p.add_argument("id")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        if args.command == "create": result = create(args)
        elif args.command == "register-existing": result = register_existing(args)
        elif args.command == "retry-clone": result = retry_clone(args)
        elif args.command == "push-setup":
            if args.id == "sid":
                raise ProjectError("SID's own remote and key are set up by hand on the host (git remote in its checkout)")
            validate_id(args.id); r = get_redis(); record = project(r, args.id); record, public = do_push_setup(r, record, args.url); result = dict(record, public_key=public, instruction="Add this public key to the GitHub repository as a deploy key with write access")
        elif args.command == "set-importance":
            validate_id(args.id); validate_importance(args.level); r = get_redis(); record = project(r, args.id); record["importance"] = args.level; record["updated_at"] = now(); r.hset(key_for(args.id), mapping=record); result = record
        elif args.command == "archive":
            validate_id(args.id); r = get_redis(); record = project(r, args.id); record["status"] = "archived"; record["updated_at"] = now(); r.hset(key_for(args.id), mapping=record); result = record
        elif args.command == "delete":
            result = delete(args)
        elif args.command == "commit-upload":
            result = commit_upload(args)
        elif args.command == "code-change":
            result = code_change(args)
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
