#!/usr/bin/env python3
"""Prune old SID logs, planner files, and integration reports safely."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import time
from pathlib import Path


PRUNABLE_STATUSES = {
    "merged",
    "rejected",
    "completed_no_changes",
    "failed",
    "test_failed",
    "integration_failed",
    "review_complete",
    "repair_complete",
    "integrate_complete",
    "blocked_failed_dependency",
}
JOB_ID_RE = re.compile(
    r"^([A-Za-z0-9_-]+?)(?:-tests\.log|\.jsonl|\.json|\.[A-Za-z0-9_-]+\.json)$"
)


def _inside_root(path: Path, root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(root)
    except ValueError:
        return False


def _regular_entry(path: Path, root: Path) -> tuple[Path, int, float] | None:
    if os.path.islink(path) or not _inside_root(path, root):
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or not _inside_root(path, root):
        return None
    return path, info.st_size, info.st_mtime


def _entries(directory: Path, root: Path) -> list[tuple[Path, int, float]]:
    if os.path.islink(directory) or not directory.is_dir():
        return []
    try:
        paths = list(directory.iterdir())
    except OSError:
        return []
    result = []
    for path in paths:
        entry = _regular_entry(path, root)
        if entry is not None:
            result.append(entry)
    return result


def _job_id(path: Path) -> str | None:
    match = JOB_ID_RE.match(path.name)
    return match.group(1) if match else None


def _status(data) -> str | None:
    if not data:
        return None
    for key, value in data.items():
        if isinstance(key, bytes):
            key = key.decode("utf-8", "replace")
        if key == "status":
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            return value
    return None


def collect_candidates(
    log_root: Path, days: int, redis_client, now: float | None = None
) -> list[tuple[Path, int]]:
    root = Path(log_root).resolve()
    cutoff = (time.time() if now is None else now) - days * 86400
    candidates: list[tuple[Path, int]] = []
    job_cache = {}

    jobs = _entries(root / "jobs", root)
    for path, size, mtime in jobs:
        if mtime > cutoff:
            continue
        job_id = _job_id(path)
        if job_id is None:
            continue
        if job_id not in job_cache:
            job_cache[job_id] = redis_client.hgetall(f"sid:jobs:{job_id}")
        data = job_cache[job_id]
        if not data or _status(data) in PRUNABLE_STATUSES:
            candidates.append((path, size))

    for path, size, mtime in _entries(root / "planner", root):
        if path.suffix == ".json" and mtime <= cutoff:
            candidates.append((path, size))

    reports = sorted(
        (entry for entry in _entries(root / "integration", root) if entry[0].suffix == ".json"),
        key=lambda entry: (-entry[2], entry[0].name),
    )
    for path, size, mtime in reports[20:]:
        if mtime <= cutoff:
            candidates.append((path, size))

    return sorted(candidates, key=lambda item: str(item[0]))


def prune(
    log_root,
    days,
    redis_client,
    apply: bool,
    now: float | None = None,
    out=sys.stdout,
) -> tuple[int, int]:
    root = Path(log_root).resolve()
    candidates = collect_candidates(root, days, redis_client, now)
    count = 0
    total = 0
    for path, size in candidates:
        if apply:
            if os.path.islink(path) or not _inside_root(path, root):
                continue
            try:
                os.unlink(path)
            except OSError as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                continue
            print(f"removed {path}", file=out)
        else:
            print(f"would remove {path} ({size} bytes)", file=out)
        count += 1
        total += size
    label = "APPLIED" if apply else "DRY RUN"
    suffix = "removed" if apply else "would be removed"
    print(f"{label}: {count} files, {total} bytes {suffix}", file=out)
    return count, total


def main(argv=None, redis_client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--log-root", default="/var/log/sid-ai")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if redis_client is None:
        import redis

        redis_client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    prune(args.log_root, args.days, redis_client, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
