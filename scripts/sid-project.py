#!/usr/bin/env python3
"""Manage the SID project registry and project checkouts."""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


PROJECT_SET = "sid:projects"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ProjectError(Exception):
    pass


def get_redis():
    import redis
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        decode_responses=True,
    )


def detect_gate(repo):
    repo = Path(repo)
    package = repo / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text())
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return "npm test"
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    if any((repo / name).exists() for name in ("pyproject.toml", "pytest.ini", "setup.cfg")) or (repo / "tests").is_dir():
        return "python3 -m pytest -q"
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    makefile = repo / "Makefile"
    if makefile.is_file():
        try:
            if any(re.match(r"^test:", line) for line in makefile.read_text().splitlines()):
                return "make test"
        except OSError:
            pass
    return ""


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
    key = Path(root) / "deploy_key"
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
    p = sub.add_parser("show"); p.add_argument("id")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        if args.command == "create": result = create(args)
        elif args.command == "register-existing": result = register_existing(args)
        elif args.command == "retry-clone": result = retry_clone(args)
        elif args.command == "push-setup":
            validate_id(args.id); r = get_redis(); record = project(r, args.id); record, public = do_push_setup(r, record, args.url); result = dict(record, public_key=public, instruction="Add this public key to the GitHub repository as a deploy key with write access")
        elif args.command == "set-importance":
            validate_id(args.id); validate_importance(args.level); r = get_redis(); record = project(r, args.id); record["importance"] = args.level; record["updated_at"] = now(); r.hset(key_for(args.id), mapping=record); result = record
        elif args.command == "archive":
            validate_id(args.id); r = get_redis(); record = project(r, args.id); record["status"] = "archived"; record["updated_at"] = now(); r.hset(key_for(args.id), mapping=record); result = record
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
