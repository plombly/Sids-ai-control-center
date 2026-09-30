#!/usr/bin/env python3
"""Snapshot everything needed to rebuild SID on this host.

Each run writes BACKUP_ROOT/<UTC timestamp>/ (root-only) containing:
  repo.bundle        git bundle of every ref in REPO_ROOT (verified)
  redis.rdb          point-in-time Redis dump (jobs, goals, queues, audit)
  postgres.sql.gz    projects/tasks/agents database
  config.tar.gz      /etc/sid-ai, SID systemd units and drop-ins, repo .env
  manifest.json      sizes, checks, and what to do to restore
then keeps the newest BACKUP_KEEP snapshots (default 14).

Secrets (.env, operator token) are copied byte-for-byte into the root-only
snapshot and never read or printed. The snapshots are on this host's disk:
they protect against mistakes and corruption, not against losing the disk.
Set BACKUP_REMOTE (an rsync destination) to also copy each snapshot off-host.

Restore: git clone repo.bundle; stop SID units; docker cp redis.rdb into the
redis container's /data/dump.rdb with appendonly disabled for the first
start (or replay); gunzip postgres.sql.gz | psql; untar config.tar.gz at /.
"""

import datetime
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(os.getenv("REPO_ROOT", "/opt/sids-ai-command-center"))
# A directory of its own: /var/backups/sid-ai also holds hand-made backups.
BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/var/backups/sid-ai/snapshots"))
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z")
KEEP = int(os.getenv("BACKUP_KEEP", "14"))
REDIS_CONTAINER = os.getenv("REDIS_CONTAINER", "sid-ai-redis")
POSTGRES_CONTAINER = os.getenv("POSTGRES_CONTAINER", "sid-ai-postgres")
BACKUP_REMOTE = os.getenv("BACKUP_REMOTE", "")
# Off-host copy of the code: push these branches to this git remote (e.g.
# "origin", a private GitHub repo). Fast-forward only; never force.
BACKUP_GIT_REMOTE = os.getenv("BACKUP_GIT_REMOTE", "")
BACKUP_GIT_BRANCHES = os.getenv("BACKUP_GIT_BRANCHES", "main").split(",")
CONFIG_PATHS = [
    Path("/etc/sid-ai"),
    Path("/etc/systemd/system/sid-ai-orchestrator.service"),
    Path("/etc/systemd/system/sid-ai-orchestrator.service.d"),
    Path("/etc/systemd/system/sid-ai-worker@.service"),
    Path("/etc/systemd/system/sid-ai-worker@.service.d"),
    Path("/etc/systemd/system/sid-ai-operator.service"),
    Path("/etc/systemd/system/sid-ai-backup.service"),
    Path("/etc/systemd/system/sid-ai-backup.timer"),
    Path("/etc/systemd/system/sid-ai-watchdog.service"),
    Path("/etc/systemd/system/sid-ai-watchdog.timer"),
    REPO_ROOT / ".env",
]
LAST_BACKUP_KEY = "sid:backup:last"


def run(command, **kwargs):
    return subprocess.run(command, text=kwargs.pop("text", True), capture_output=True, **kwargs)


def backup_repo(dest):
    bundle = dest / "repo.bundle"
    result = run(["git", "-C", str(REPO_ROOT), "bundle", "create", str(bundle), "--all"])
    if result.returncode != 0:
        raise RuntimeError(f"git bundle failed: {result.stderr.strip()}")
    verify = run(["git", "-C", str(REPO_ROOT), "bundle", "verify", str(bundle)])
    if verify.returncode != 0:
        raise RuntimeError(f"git bundle verify failed: {verify.stderr.strip()}")
    return {"bytes": bundle.stat().st_size, "verified": True}


def backup_redis(dest):
    # --rdb asks the server for a fresh RDB snapshot and writes it client-side.
    inside = "/tmp/sid-backup.rdb"
    result = run(["docker", "exec", REDIS_CONTAINER, "redis-cli", "--rdb", inside])
    if result.returncode != 0:
        raise RuntimeError(f"redis dump failed: {(result.stderr or result.stdout).strip()}")
    target = dest / "redis.rdb"
    copied = run(["docker", "cp", f"{REDIS_CONTAINER}:{inside}", str(target)])
    run(["docker", "exec", REDIS_CONTAINER, "rm", "-f", inside])
    if copied.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"redis dump copy failed: {copied.stderr.strip()}")
    return {"bytes": target.stat().st_size}


def backup_postgres(dest):
    # Credentials stay inside the container's own environment.
    result = subprocess.run(
        ["docker", "exec", POSTGRES_CONTAINER, "sh", "-c", 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'],
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()[:300]}")
    target = dest / "postgres.sql.gz"
    with gzip.open(target, "wb") as out:
        out.write(result.stdout)
    return {"bytes": target.stat().st_size, "dump_bytes": len(result.stdout)}


def backup_config(dest):
    target = dest / "config.tar.gz"
    included = []
    with tarfile.open(target, "w:gz") as tar:
        for path in CONFIG_PATHS:
            if path.exists():
                tar.add(str(path), arcname=str(path).lstrip("/"))
                included.append(str(path))
    return {"bytes": target.stat().st_size, "paths": included}


def push_git_remote():
    branches = [b.strip() for b in BACKUP_GIT_BRANCHES if b.strip()]
    result = run(["git", "-C", str(REPO_ROOT), "push", BACKUP_GIT_REMOTE, *branches])
    if result.returncode != 0:
        raise RuntimeError(f"git push {BACKUP_GIT_REMOTE} failed: {result.stderr.strip()[-300:]}")
    return {"remote": BACKUP_GIT_REMOTE, "branches": branches}


def rotate(root, keep):
    """Delete this tool's oldest snapshots beyond `keep`. Only directories
    named exactly like its own snapshots are ever considered: anything else
    in the directory (hand-made backups, other tools) is never touched."""
    snapshots = sorted(p for p in root.iterdir()
                       if p.is_dir() and not p.is_symlink() and SNAPSHOT_NAME.fullmatch(p.name))
    removed = []
    for old in snapshots[:-keep] if keep > 0 else []:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def record_status(status):
    """Best effort: let the watchdog and dashboard see when backups last ran."""
    run(["docker", "exec", REDIS_CONTAINER, "redis-cli", "SET", LAST_BACKUP_KEY, json.dumps(status)])


def main():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_ROOT, 0o700)
    dest = BACKUP_ROOT / stamp
    dest.mkdir(mode=0o700)
    manifest = {"created": stamp, "repo_root": str(REPO_ROOT), "parts": {}, "errors": {}}
    for name, step in (("repo", backup_repo), ("redis", backup_redis),
                       ("postgres", backup_postgres), ("config", backup_config)):
        try:
            manifest["parts"][name] = step(dest)
        except Exception as exc:
            manifest["errors"][name] = str(exc)
    if BACKUP_GIT_REMOTE:
        try:
            manifest["parts"]["git_remote"] = push_git_remote()
        except Exception as exc:
            manifest["errors"]["git_remote"] = str(exc)
    if BACKUP_REMOTE and not manifest["errors"]:
        synced = run(["rsync", "-a", f"{dest}/", f"{BACKUP_REMOTE.rstrip('/')}/{stamp}/"])
        manifest["remote"] = {"target": BACKUP_REMOTE, "ok": synced.returncode == 0,
                              "error": synced.stderr.strip()[:300] if synced.returncode else ""}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    manifest["rotated"] = rotate(BACKUP_ROOT, KEEP)
    ok = not manifest["errors"] and manifest.get("remote", {}).get("ok", True)
    record_status({"at": stamp, "ok": ok, "path": str(dest), "errors": manifest["errors"]})
    print(json.dumps({"ok": ok, "path": str(dest), "errors": manifest["errors"],
                      "rotated": manifest["rotated"]}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
