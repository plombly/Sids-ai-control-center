#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import redis


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REPO_ROOT = Path(
    os.getenv("REPO_ROOT", "/opt/sids-ai-command-center")
).resolve()
WORKTREE_ROOT = Path(
    os.getenv("WORKTREE_ROOT", "/opt/sid-worktrees")
).resolve()

JOB_QUEUE = os.getenv("WORKER_QUEUE", "sid:jobs")

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


def release_approval_lock(lock_key, token):
    r.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1,
        lock_key,
        token,
    )


def approve(job_id):
    # Approval advances the shared main branch, so this lock must be
    # repository-wide rather than per job. Holding it across validation
    # and merge makes the integration-base check and main advancement
    # one serialized operation.
    lock_key = "sid:approval-lock:main"
    token = uuid.uuid4().hex
    if not r.set(lock_key, token, nx=True, ex=300):
        fail("another approval is already advancing main")
    try:
        _approve_unlocked(job_id)
    finally:
        release_approval_lock(lock_key, token)


def _approve_unlocked(job_id):
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

    # Record the merge immediately after main advances. Cleanup happens
    # afterwards and is best-effort: if it failed first, main would have
    # moved while the job still looked unmerged, and every retry would then
    # be refused as "stale main", stranding the job and its dependents.
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

    cleanup_after_merge(job_id, data, worktree, branch)

    print()
    print(f"APPROVED + INTEGRATED: {job_id}")
    print(f"Merge commit: {commit_sha}")


def cleanup_after_merge(job_id, data, worktree, branch):
    """Best-effort removal of merged worktrees/branches. Never fails."""
    steps = [
        ("worktree", "remove", str(worktree)),
        ("branch", "-d", branch),
    ]
    try:
        builder_worktree = safe_worktree(job_id, data)
        builder_branch = safe_branch(job_id, data)
        steps += [
            ("worktree", "remove", "--force", str(builder_worktree)),
            ("branch", "-D", builder_branch),
        ]
    except SystemExit:
        print("WARNING: builder worktree/branch not found; skipped its cleanup",
              file=sys.stderr)
    for step in steps:
        result = git(*step, check=False)
        if result.returncode != 0:
            print(
                f"WARNING: cleanup step failed (merge is recorded): git {' '.join(step)}: "
                f"{(result.stderr or result.stdout).strip()}",
                file=sys.stderr,
            )


REJECTABLE = {"awaiting_review", "needs_human", "repair_exhausted"}


def reject(job_id):
    key = f"sid:jobs:{job_id}"
    data = job_record(job_id)

    if data.get("status") not in REJECTABLE:
        fail(
            f"job status is {data.get('status')!r}; "
            f"expected one of {sorted(REJECTABLE)}"
        )

    worktree = safe_worktree(job_id, data)
    branch = safe_branch(job_id, data)

    # An exhausted or stale job may have no live integration worktree.
    # Validate the recorded path when there is one; skip when absent.
    if data.get("integration_worktree"):
        integration_worktree = (
            WORKTREE_ROOT / f"job-{job_id}-integration"
        ).resolve()
        if Path(data["integration_worktree"]).resolve() != integration_worktree:
            fail(f"unexpected integration worktree: {data['integration_worktree']}")
        if integration_worktree.exists():
            git("worktree", "remove", "--force", str(integration_worktree))
        integration_branch = f"sid/integration-{job_id}"
        if git("show-ref", "--verify", f"refs/heads/{integration_branch}",
               check=False).returncode == 0:
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


EXHAUSTED = {"needs_human", "repair_exhausted"}

# Derived state that a fresh integration + review must rebuild. Source
# candidates, repair attempts, findings history and audit fields are kept.
DERIVED_REVIEW_FIELDS = (
    "review_job_id", "review_status", "review_verdict", "reviewed_commit",
    "review_error", "review_findings",
    "integration_status", "integration_base_commit",
    "integrated_candidate_commit", "integration_result", "integration_error",
)


def extend(job_id, extra=1):
    """Grant more repair attempts to a job that exhausted them."""
    key = f"sid:jobs:{job_id}"
    data = job_record(job_id)
    if data.get("status") not in EXHAUSTED:
        fail(f"job status is {data.get('status')!r}; expected one of {sorted(EXHAUSTED)}")
    if not (data.get("review_status") == "complete"
            and data.get("review_verdict") == "changes_required"
            and data.get("review_job_id")):
        fail("job has no current CHANGES_REQUIRED review to repair; "
             "use 'reintegrate' to get a fresh review of its candidate")
    if not 1 <= extra <= 5:
        fail("extra attempts must be between 1 and 5")
    attempts = int(data.get("repair_attempts", "0") or "0")
    limit = attempts + extra
    r.hset(key, mapping={
        "status": "awaiting_review",
        "max_repair_attempts": str(limit),
        "repair_status": "extended",
        "needs_human_reason": "",
        "updated_at": str(time.time()),
    })
    print(f"EXTENDED: {job_id} may use {extra} more repair attempt(s) (limit {limit})")


def legacy_sources(data):
    """Ordered source commits for jobs created before integration existed."""
    candidate = data.get("candidate_commit", "")
    if not candidate:
        fail("job has no candidate commit")
    base = git("merge-base", "main", candidate).stdout.strip()
    commits = git("rev-list", "--reverse", f"{base}..{candidate}").stdout.split()
    if not commits:
        fail("candidate has no commits beyond main; nothing to integrate")
    return commits


def reintegrate(job_id):
    """Queue a fresh isolated integration against current main + fresh review.

    For stale candidates (main moved) and for exhausted jobs whose latest
    candidate should be reviewed again. Never touches main.
    """
    key = f"sid:jobs:{job_id}"
    data = job_record(job_id)
    allowed = {"awaiting_review"} | EXHAUSTED
    if data.get("status") not in allowed:
        fail(f"job status is {data.get('status')!r}; expected one of {sorted(allowed)}")
    if data.get("role", "builder") not in {"", "builder"}:
        fail("only builder jobs can be reintegrated")
    if data.get("review_status") in {"queued", "running"}:
        fail("a review is already queued or running for this job")
    if data.get("repair_status") in {"queued", "running"}:
        fail("a repair is queued or running for this job")
    safe_worktree(job_id, data)
    safe_branch(job_id, data)

    raw = data.get("source_candidate_commits", "")
    try:
        sources = json.loads(raw) if raw else []
    except ValueError:
        sources = []
    if not isinstance(sources, list) or not sources:
        sources = legacy_sources(data)

    integrate_id = uuid.uuid4().hex[:8]
    now = str(time.time())
    r.hdel(key, *DERIVED_REVIEW_FIELDS)
    update = {
        "status": "awaiting_review",
        "source_candidate_commits": json.dumps(sources, separators=(",", ":")),
        "last_integrate_job_id": integrate_id,
        "needs_human_reason": "",
        "updated_at": now,
    }
    # Repair attempts are preserved, so an exhausted job whose fresh review
    # still requires changes returns to needs_human instead of re-repairing.
    r.hset(key, mapping=update)
    r.hset(f"sid:jobs:{integrate_id}", mapping={
        "id": integrate_id,
        "status": "queued",
        "role": "integrate",
        "target_builder_id": job_id,
        "goal_id": data.get("goal_id", ""),
        "created_at": now,
        "updated_at": now,
    })
    r.rpush(JOB_QUEUE, json.dumps({
        "id": integrate_id,
        "role": "integrate",
        "target_builder_id": job_id,
        "created_at": now,
    }))
    print(f"REINTEGRATE QUEUED: {job_id} via {integrate_id} "
          f"({len(sources)} source commit(s)); a fresh review follows automatically")


FAILED_DEPENDENCY_STATES = {
    "failed", "test_failed", "integration_failed", "rejected",
    "repair_exhausted", "blocked_failed_dependency",
}


def reopen(job_id):
    """Re-open a job blocked by a dependency that has since recovered."""
    key = f"sid:jobs:{job_id}"
    data = job_record(job_id)
    if data.get("status") != "blocked_failed_dependency":
        fail(f"job status is {data.get('status')!r}; expected 'blocked_failed_dependency'")
    try:
        deps = json.loads(data.get("dependencies") or "[]")
    except ValueError:
        fail("job has unreadable dependencies")
    still_failed = [
        f"{dep} ({r.hget(f'sid:jobs:{dep}', 'status')})" for dep in deps
        if r.hget(f"sid:jobs:{dep}", "status") in FAILED_DEPENDENCY_STATES
    ]
    if still_failed:
        fail("dependencies are still failed: " + ", ".join(still_failed))
    r.hset(key, mapping={"status": "blocked", "updated_at": str(time.time())})
    goal_id = data.get("goal_id")
    if goal_id and r.hget(f"sid:goals:{goal_id}", "status") == "failed":
        r.hset(f"sid:goals:{goal_id}", mapping={
            "status": "running", "error": "", "updated_at": str(time.time()),
        })
    print(f"REOPENED: {job_id}; the orchestrator dispatches it once all "
          "dependencies are merged")


USAGE = """Usage:
  job-review.py approve JOB_ID
  job-review.py reject JOB_ID
  job-review.py extend JOB_ID [EXTRA_ATTEMPTS]   grant more repairs (default 1)
  job-review.py reintegrate JOB_ID               fresh integration on current main + fresh review
  job-review.py reopen JOB_ID                    un-block a job whose failed dependency recovered"""


def main():
    actions = {"approve", "reject", "extend", "reintegrate", "reopen"}
    if len(sys.argv) < 3 or sys.argv[1] not in actions or (
        len(sys.argv) > 3 and sys.argv[1] != "extend"
    ) or len(sys.argv) > 4:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    action = sys.argv[1]
    job_id = sys.argv[2]

    if not job_id.isalnum():
        fail("invalid job ID")

    if action == "approve":
        approve(job_id)
    elif action == "reject":
        reject(job_id)
    elif action == "extend":
        try:
            extra = int(sys.argv[3]) if len(sys.argv) == 4 else 1
        except ValueError:
            fail("EXTRA_ATTEMPTS must be a number")
        extend(job_id, extra)
    elif action == "reintegrate":
        reintegrate(job_id)
    else:
        reopen(job_id)


if __name__ == "__main__":
    main()
