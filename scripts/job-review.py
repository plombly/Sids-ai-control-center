#!/usr/bin/env python3

import os
import subprocess
import sys
import time
from pathlib import Path

import redis


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REPO_ROOT = Path(
    os.getenv("REPO_ROOT", "/opt/sids-ai-command-center")
).resolve()
WORKTREE_ROOT = Path(
    os.getenv("WORKTREE_ROOT", "/opt/sid-worktrees")
).resolve()

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def git(*args, cwd=REPO_ROOT, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )

    if check and result.returncode != 0:
        fail(result.stderr.strip() or result.stdout.strip())

    return result


def job_record(job_id):
    data = r.hgetall(f"sid:jobs:{job_id}")
    if not data:
        fail(f"job not found: {job_id}")
    return data


def safe_worktree(job_id, data):
    expected = (WORKTREE_ROOT / f"job-{job_id}").resolve()
    actual = Path(data.get("worktree", "")).resolve()

    if actual != expected:
        fail(f"unexpected worktree path: {actual}")

    if not actual.exists():
        fail(f"worktree does not exist: {actual}")

    return actual


def safe_branch(job_id, data):
    expected = f"sid/job-{job_id}"
    actual = data.get("branch")

    if actual != expected:
        fail(f"unexpected branch: {actual}")

    return actual


def ensure_main_clean():
    status = git("status", "--porcelain").stdout.strip()
    if status:
        fail("main worktree is not clean")


def approve(job_id):
    key = f"sid:jobs:{job_id}"
    data = job_record(job_id)

    if data.get("status") != "awaiting_review":
        fail(
            f"job status is {data.get('status')!r}; "
            "expected 'awaiting_review'"
        )

    ensure_main_clean()

    worktree = safe_worktree(job_id, data)
    branch = safe_branch(job_id, data)

    changes = git("status", "--porcelain", cwd=worktree).stdout.strip()
    if not changes:
        fail("job worktree contains no changes")

    print("Changes being approved:")
    print(changes)
    print()

    git("add", "-A", cwd=worktree)

    commit = git(
        "commit",
        "-m",
        f"Apply SID job {job_id}",
        cwd=worktree,
    )

    print(commit.stdout.strip())

    git(
        "merge",
        "--no-ff",
        branch,
        "-m",
        f"Merge SID job {job_id}",
        cwd=REPO_ROOT,
    )

    commit_sha = git("rev-parse", "HEAD").stdout.strip()

    git("worktree", "remove", str(worktree))
    git("branch", "-d", branch)

    r.hset(
        key,
        mapping={
            "status": "merged",
            "merged_at": str(time.time()),
            "merge_commit": commit_sha,
            "updated_at": str(time.time()),
        },
    )

    print()
    print(f"APPROVED: {job_id}")
    print(f"Merge commit: {commit_sha}")


def reject(job_id):
    key = f"sid:jobs:{job_id}"
    data = job_record(job_id)

    if data.get("status") != "awaiting_review":
        fail(
            f"job status is {data.get('status')!r}; "
            "expected 'awaiting_review'"
        )

    worktree = safe_worktree(job_id, data)
    branch = safe_branch(job_id, data)

    git("worktree", "remove", "--force", str(worktree))
    git("branch", "-D", branch)

    r.hset(
        key,
        mapping={
            "status": "rejected",
            "rejected_at": str(time.time()),
            "updated_at": str(time.time()),
        },
    )

    print(f"REJECTED: {job_id}")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"approve", "reject"}:
        print(
            "Usage: job-review.py approve|reject JOB_ID",
            file=sys.stderr,
        )
        raise SystemExit(2)

    action = sys.argv[1]
    job_id = sys.argv[2]

    if not job_id.isalnum():
        fail("invalid job ID")

    if action == "approve":
        approve(job_id)
    else:
        reject(job_id)


if __name__ == "__main__":
    main()
