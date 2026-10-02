#!/usr/bin/env python3
"""Prove the newest backup can actually be restored (monthly timer).

A backup nobody has restored is a hope, not a backup. This takes the newest
snapshot made by scripts/laika-backup.py and restores every part into
throwaway places, then checks what came back:
  repo.bundle          cloned; HEAD resolves and `git fsck` passes
  projects/*.bundle    the same for every project repository
  redis.rdb            loaded into a temporary Redis container; keys counted
  postgres.sql.gz      loaded into a temporary Postgres container; tables counted
  config.tar.gz        opened; must contain /etc/laika
  project-data.tar.gz  opened (when present)
Nothing touches the live system: temporary containers have no published
ports and are removed, temporary files live in a root-only directory that is
deleted afterwards. The result goes to Redis laika:backup:restore-check, which
the health watchdog reports (and a failure pings you through notifications).
"""

import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_redis  # noqa: E402  (services/laika_redis.py)

BACKUP_ROOT = Path(os.getenv("BACKUP_ROOT", "/var/backups/laika/snapshots"))
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z")
REDIS_IMAGE = os.getenv("RESTORE_CHECK_REDIS_IMAGE", "redis:8-alpine")
POSTGRES_IMAGE = os.getenv("RESTORE_CHECK_POSTGRES_IMAGE", "postgres:18-alpine")
RESULT_KEY = "laika:backup:restore-check"


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=kwargs.pop("text", True), **kwargs)


def newest_snapshot(root=BACKUP_ROOT):
    snapshots = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and SNAPSHOT_NAME.fullmatch(p.name)) if root.is_dir() else []
    return snapshots[-1] if snapshots else None


def check_bundle(bundle, workdir):
    target = workdir / f"clone-{bundle.stem}"
    cloned = run(["git", "clone", "-q", str(bundle), str(target)])
    if cloned.returncode:
        raise RuntimeError(f"clone failed: {cloned.stderr.strip()[:200]}")
    head = run(["git", "-C", str(target), "rev-parse", "--short", "HEAD"])
    if head.returncode:
        raise RuntimeError("no HEAD in the restored repository")
    fsck = run(["git", "-C", str(target), "fsck", "--no-dangling"])
    if fsck.returncode:
        raise RuntimeError(f"git fsck failed: {fsck.stderr.strip()[:200]}")
    commits = run(["git", "-C", str(target), "rev-list", "--count", "HEAD"]).stdout.strip()
    return f"HEAD {head.stdout.strip()}, {commits} commits"


def check_tar(archive, must_contain=None):
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    if must_contain and not any(name.startswith(must_contain) for name in names):
        raise RuntimeError(f"{must_contain} missing")
    return f"{len(names)} entries"


def _wait(check, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(1)
    return False


def check_redis(rdb, workdir):
    data = workdir / "redis"
    data.mkdir(mode=0o700)
    shutil.copy2(rdb, data / "dump.rdb")
    name = f"laika-restore-check-redis-{secrets.token_hex(4)}"
    started = run(["docker", "run", "-d", "--rm", "--name", name, "-v", f"{data}:/data", REDIS_IMAGE,
                   "redis-server", "--dir", "/data", "--dbfilename", "dump.rdb", "--appendonly", "no"])
    if started.returncode:
        raise RuntimeError(f"could not start a temporary Redis: {started.stderr.strip()[:200]}")
    try:
        cli = lambda *args: run(["docker", "exec", name, "redis-cli", *args])
        if not _wait(lambda: cli("ping").stdout.strip() == "PONG"):
            raise RuntimeError("temporary Redis did not start")
        if not _wait(lambda: "loading:0" in cli("info", "persistence").stdout):
            raise RuntimeError("the dump did not finish loading")
        keys = int(cli("dbsize").stdout.strip() or 0)
        jobs = len([k for k in cli("--scan", "--pattern", "laika:jobs:*").stdout.split() if k.count(":") == 2])
        if keys == 0:
            raise RuntimeError("the restored Redis is empty")
        return f"{keys} keys, {jobs} jobs"
    finally:
        run(["docker", "rm", "-f", name])


def check_postgres(dump, workdir):
    sql = gzip.decompress(dump.read_bytes()).decode(errors="replace")
    owners = sorted(set(re.findall(r"OWNER TO ([A-Za-z_][A-Za-z0-9_]*)", sql)) - {"postgres"})
    name = f"laika-restore-check-pg-{secrets.token_hex(4)}"
    started = run(["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={secrets.token_hex(16)}",
                   POSTGRES_IMAGE])
    if started.returncode:
        raise RuntimeError(f"could not start a temporary Postgres: {started.stderr.strip()[:200]}")
    try:
        psql = lambda *args, **kw: run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", *args], **kw)
        if not _wait(lambda: run(["docker", "exec", name, "pg_isready", "-U", "postgres"]).returncode == 0, 90):
            raise RuntimeError("temporary Postgres did not start")
        time.sleep(2)  # the image restarts once after its first init
        _wait(lambda: run(["docker", "exec", name, "pg_isready", "-U", "postgres"]).returncode == 0, 60)
        for role in owners:  # the dump assigns tables to the live database's user
            psql("-c", f'CREATE ROLE "{role}"')
        restored = psql("-q", input=sql)
        if restored.returncode:
            raise RuntimeError(f"restore failed: {restored.stderr.strip()[:300]}")
        tables = psql("-tA", "-c", "select count(*) from information_schema.tables where table_schema = 'public'")
        return f"{tables.stdout.strip() or '?'} tables"
    finally:
        run(["docker", "rm", "-f", name])


def run_checks(snapshot):
    checks = []

    def check(name, fn, *args):
        try:
            detail = fn(*args)
            checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)[:300]})

    with tempfile.TemporaryDirectory(prefix="laika-restore-check-") as tmp:
        workdir = Path(tmp)
        os.chmod(workdir, 0o700)
        check("repo", check_bundle, snapshot / "repo.bundle", workdir)
        projects = snapshot / "projects"
        for bundle in sorted(projects.glob("*.bundle")) if projects.is_dir() else []:
            check(f"project {bundle.stem}", check_bundle, bundle, workdir)
        if (projects / "project-data.tar.gz").exists():
            check("project data", check_tar, projects / "project-data.tar.gz")
        check("config", check_tar, snapshot / "config.tar.gz", "etc/laika")
        check("redis", check_redis, snapshot / "redis.rdb", workdir)
        check("postgres", check_postgres, snapshot / "postgres.sql.gz", workdir)
    return checks


def main():
    snapshot = newest_snapshot()
    if snapshot is None:
        result = {"at": time.time(), "ok": False, "snapshot": None,
                  "checks": [{"name": "snapshot", "ok": False, "detail": f"no snapshot in {BACKUP_ROOT}"}]}
    else:
        checks = run_checks(snapshot)
        result = {"at": time.time(), "ok": all(c["ok"] for c in checks), "snapshot": snapshot.name, "checks": checks}
    try:
        import redis as redis_lib
        r = redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                     password=laika_redis.password(), decode_responses=True)
        r.set(RESULT_KEY, json.dumps(result))
    except Exception as exc:
        print(f"could not record the result: {exc}", file=sys.stderr)
    for item in result["checks"]:
        print(f"{'ok  ' if item['ok'] else 'FAIL'} {item['name']}: {item['detail']}")
    print(f"restore check {'passed' if result['ok'] else 'FAILED'} for snapshot {result['snapshot']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
