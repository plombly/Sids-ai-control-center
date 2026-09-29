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

    review_status = data.get("review_status")
    review_verdict = data.get("review_verdict")
    review_job_id = data.get("review_job_id")

    if review_status != "complete":
        fail(
            f"Job {job_id} cannot be approved: "
            "independent review is not complete"
        )

    if review_verdict != "pass":
        fail(
            f"Job {job_id} cannot be approved: "
            f"review verdict is {review_verdict or 'missing'}"
        )

    if not review_job_id:
        fail(
            f"Job {job_id} cannot be approved: "
            "review job ID is missing"
        )

    review = job_record(review_job_id)
    if review.get("role") != "reviewer":
        fail(f"linked job {review_job_id} is not a reviewer")
    if review.get("builder_job_id") != job_id:
        fail(f"reviewer {review_job_id} is linked to another builder")
    if review.get("status") != "review_complete":
        fail(f"reviewer {review_job_id} is not complete")
    if review.get("review_verdict") != "pass":
        fail(f"reviewer {review_job_id} verdict is not pass")

    candidate_commit = data.get("candidate_commit")
    if not candidate_commit:
        fail(f"Job {job_id} has no immutable candidate commit")
    if data.get("reviewed_commit") != candidate_commit:
        fail(f"Job {job_id} review does not match candidate commit")
    if review.get("candidate_commit") != candidate_commit:
        fail(f"reviewer {review_job_id} candidate does not match builder")
    if review.get("reviewed_commit") != candidate_commit:
        fail(f"reviewer {review_job_id} did not review current candidate")

    ensure_main_clean()

    worktree = safe_worktree(job_id, data)
    branch = safe_branch(job_id, data)

    head = git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if head != candidate_commit:
        fail("candidate worktree HEAD changed after review")
    if git("status", "--porcelain", cwd=worktree).stdout.strip():
        fail("candidate worktree changed after review")

    branch_head = git("rev-parse", branch).stdout.strip()
    if branch_head != candidate_commit:
        fail("candidate branch changed after review")

    print(f"Approved candidate: {candidate_commit}")

    git(
        "merge",
        "--no-ff",
        branch,
        "-m",
        f"Merge SID job {job_id}",
        cwd=REPO_ROOT,
    )

    commit_sha = git("rev-parse", "HEAD").stdout.strip()

    print()
    print("Running integration gate...")
    integration = subprocess.run(
        [str(REPO_ROOT / "scripts/integration-check.py")],
        cwd=REPO_ROOT,
        text=True,
    )

    now = str(time.time())

    if integration.returncode != 0:
        r.hset(
            key,
            mapping={
                "status": "integration_failed",
                "merged_at": now,
                "merge_commit": commit_sha,
                "integration_status": "failed",
                "updated_at": now,
            },
        )

        print()
        print(f"INTEGRATION FAILED: {job_id}")
        print(f"Merge commit retained for diagnosis: {commit_sha}")
        raise SystemExit(1)

    git("worktree", "remove", str(worktree))
    git("branch", "-d", branch)

    r.hset(
        key,
        mapping={
            "status": "merged",
            "merged_at": now,
            "merge_commit": commit_sha,
            "integration_status": "passed",
            "updated_at": now,
        },
    )

    print()
    print(f"APPROVED + INTEGRATED: {job_id}")
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
