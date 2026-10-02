#!/usr/bin/env python3
"""Snapshot everything needed to rebuild LAIka on this host.

Each run writes BACKUP_ROOT/<UTC timestamp>/ (root-only) containing:
  repo.bundle        git bundle of every ref in REPO_ROOT (verified)
  redis.rdb          point-in-time Redis dump (jobs, goals, queues, audit)
  postgres.sql.gz    projects/tasks/agents database
  config.tar.gz      /etc/laika (incl. project deploy keys), LAIka systemd
                     units and drop-ins, repo .env
  projects/          <id>.bundle for every project repository, and
                     project-data.tar.gz (every project's app data)
  manifest.json      sizes, checks, and what to do to restore
then keeps the newest BACKUP_KEEP snapshots (default 14).

Secrets (.env, operator token) are copied byte-for-byte into the root-only
snapshot and never read or printed. The snapshots are on this host's disk:
they protect against mistakes and corruption, not against losing the disk.
Set BACKUP_REMOTE (an rsync destination) to also copy each snapshot off-host.

Restore: git clone repo.bundle; stop LAIka units; docker cp redis.rdb into the
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment)

REPO_ROOT = Path(os.getenv("REPO_ROOT", "/opt/laika"))
# A directory of its own: /var/backups/laika also holds hand-made backups.
BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/var/backups/laika/snapshots"))
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z")
KEEP = int(os.getenv("BACKUP_KEEP", "14"))
REDIS_CONTAINER = os.getenv("REDIS_CONTAINER", "laika-redis")
# redis-cli inside the container, authenticated with the container's own
# REDIS_PASSWORD (docker-compose env_file), so it never appears in argv.
REDIS_CLI_SH = 'if [ -n "$REDIS_PASSWORD" ]; then export REDISCLI_AUTH="$REDIS_PASSWORD"; fi; exec redis-cli "$@"'


def redis_cli(*args):
    return ["docker", "exec", REDIS_CONTAINER, "sh", "-c", REDIS_CLI_SH, "redis-cli", *args]
POSTGRES_CONTAINER = os.getenv("POSTGRES_CONTAINER", "laika-postgres")
BACKUP_REMOTE = os.getenv("BACKUP_REMOTE", "")
# Off-host copy of the code: push these branches to this git remote (e.g.
# "origin", a private GitHub repo). Fast-forward only; never force.
BACKUP_GIT_REMOTE = os.getenv("BACKUP_GIT_REMOTE", "")
BACKUP_GIT_BRANCHES = os.getenv("BACKUP_GIT_BRANCHES", "main").split(",")
CONFIG_PATHS = [
    Path("/etc/laika"),
    Path("/etc/systemd/system/laika-orchestrator.service"),
    Path("/etc/systemd/system/laika-orchestrator.service.d"),
    Path("/etc/systemd/system/laika-worker@.service"),
    Path("/etc/systemd/system/laika-worker@.service.d"),
    Path("/etc/systemd/system/laika-operator.service"),
    Path("/etc/systemd/system/laika-backup.service"),
    Path("/etc/systemd/system/laika-backup.timer"),
    Path("/etc/systemd/system/laika-watchdog.service"),
    Path("/etc/systemd/system/laika-watchdog.timer"),
    Path("/etc/systemd/system/laika-apps.service"),
    Path("/etc/systemd/system/laika-scaler.service"),
    Path("/etc/systemd/system/laika-notify.service"),
    Path("/etc/systemd/system/laika-notify.timer"),
    Path("/etc/systemd/system/laika-restore-check.service"),
    Path("/etc/systemd/system/laika-restore-check.timer"),
    Path("/etc/systemd/system/laika-digest.service"),
    Path("/etc/systemd/system/laika-digest.timer"),
    REPO_ROOT / ".env",
]
LAST_BACKUP_KEY = "laika:backup:last"
PROJECTS_BASE = Path(os.getenv("LAIKA_PROJECTS_BASE", "/var/lib/laika/projects"))
PROJECT_DATA = Path(os.getenv("LAIKA_PROJECT_DATA", "/var/lib/laika/project-data"))


def run(command, **kwargs):
    return subprocess.run(command, text=kwargs.pop("text", True), capture_output=True, **kwargs)


def backup_repo(dest):
    """LAIka's own repository, when it runs from a git checkout (development
    servers). An install from a release has nothing to bundle: its program
    files come back by installing the same version (recorded here)."""
    if not (REPO_ROOT / ".git").exists():
        try:
            version = (REPO_ROOT / "VERSION").read_text().strip()
        except OSError:
            version = "unknown"
        return {"skipped": "installed from a release", "version": version}
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
    inside = "/tmp/laika-backup.rdb"
    result = run(redis_cli("--rdb", inside))
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


def backup_projects(dest):
    """A verified git bundle of each project repository (with commits) and
    one tarball of all projects' app data."""
    out = dest / "projects"
    out.mkdir(mode=0o700, exist_ok=True)
    bundles = {}
    for repo in sorted(PROJECTS_BASE.glob("*/repo")):
        if not (repo / ".git").is_dir() or run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"]).returncode:
            continue
        target = out / f"{repo.parent.name}.bundle"
        made = run(["git", "-C", str(repo), "bundle", "create", str(target), "--all"])
        if made.returncode or run(["git", "-C", str(repo), "bundle", "verify", str(target)]).returncode:
            raise RuntimeError(f"bundle of {repo.parent.name} failed: {made.stderr.strip()[:200]}")
        bundles[repo.parent.name] = target.stat().st_size
    data_bytes = 0
    if PROJECT_DATA.is_dir() and any(PROJECT_DATA.iterdir()):
        archive = out / "project-data.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(str(PROJECT_DATA), arcname=str(PROJECT_DATA).lstrip("/"))
        data_bytes = archive.stat().st_size
    return {"bundles": bundles, "data_bytes": data_bytes}


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
    run(redis_cli("SET", LAST_BACKUP_KEY, json.dumps(status)))


def main():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_ROOT, 0o700)
    dest = BACKUP_ROOT / stamp
    dest.mkdir(mode=0o700)
    manifest = {"created": stamp, "repo_root": str(REPO_ROOT), "parts": {}, "errors": {}}
    for name, step in (("repo", backup_repo), ("redis", backup_redis),
                       ("postgres", backup_postgres), ("config", backup_config),
                       ("projects", backup_projects)):
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
