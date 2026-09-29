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


def safe_integration_worktree(job_id, data):
    expected = (WORKTREE_ROOT / f"job-{job_id}-integration").resolve()
    actual = Path(data.get("integration_worktree", "")).resolve()
    if actual != expected:
        fail(f"unexpected integration worktree: {actual}")
    if not actual.exists():
        fail(f"integration worktree does not exist: {actual}")
    return actual


def safe_integration_branch(job_id, data):
    expected = f"sid/integration-{job_id}"
    actual = data.get("integration_branch")
    if actual != expected:
        fail(f"unexpected integration branch: {actual}")
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

    integrated_commit = data.get("integrated_candidate_commit")
    if data.get("integration_status") != "passed":
        fail(f"Job {job_id} cannot be approved: integration did not pass")
    if not integrated_commit:
        fail(f"Job {job_id} has no integrated candidate commit")
    if data.get("reviewed_commit") != integrated_commit:
        fail("review did not target the exact integrated candidate")
    if review.get("candidate_commit") != integrated_commit:
        fail(f"reviewer {review_job_id} candidate does not match integrated candidate")
    if review.get("reviewed_commit") != integrated_commit:
        fail("reviewer did not target the exact integrated candidate")

    ensure_main_clean()

    base_commit = data.get("integration_base_commit")
    if not base_commit:
        fail("integration base commit is missing")
    current_main = git("rev-parse", "HEAD").stdout.strip()
    if current_main != base_commit:
        fail(
            "stale main: current main no longer equals integration base; "
            "reintegration and fresh review are required"
        )

    worktree = safe_integration_worktree(job_id, data)
    branch = safe_integration_branch(job_id, data)

    head = git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if head != integrated_commit:
        fail("integrated worktree HEAD changed after review")
    if git("status", "--porcelain", cwd=worktree).stdout.strip():
        fail("integrated worktree changed after review")

    branch_head = git("rev-parse", branch).stdout.strip()
    if branch_head != integrated_commit:
        fail("integrated branch changed after review")

    print(f"Approved integrated candidate: {integrated_commit}")

    git(
        "merge",
        "--ff-only",
        branch,
        cwd=REPO_ROOT,
    )

    commit_sha = git("rev-parse", "HEAD").stdout.strip()

    now = str(time.time())

    git("worktree", "remove", str(worktree))
    git("branch", "-d", branch)

    builder_worktree = safe_worktree(job_id, data)
    builder_branch = safe_branch(job_id, data)
    git("worktree", "remove", "--force", str(builder_worktree))
    git("branch", "-D", builder_branch)

    r.hset(
        key,
        mapping={
            "status": "merged",
            "merged_at": now,
            "merge_commit": commit_sha,
            "integration_status": "passed",
            "integrated_candidate_commit": integrated_commit,
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
    integration_worktree = safe_integration_worktree(job_id, data)
    integration_branch = safe_integration_branch(job_id, data)

    git("worktree", "remove", "--force", str(integration_worktree))
    git("branch", "-D", integration_branch)
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
