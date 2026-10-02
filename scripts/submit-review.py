#!/usr/bin/env python3

import argparse
import json
import os
import time
import uuid

import sys
from pathlib import Path
import redis
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_redis  # noqa: E402  (services/laika_redis.py)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Queue a LAIka review of an existing builder job"
    )
    parser.add_argument("builder_job_id", help="Builder job to review")
    parser.add_argument(
        "--model",
        default=os.environ.get("DEFAULT_MODEL", "gpt-5.6-luna"),
        help="Model to use (default: $DEFAULT_MODEL or gpt-5.6-luna)",
    )
    parser.add_argument("--provider", default="codex")
    return parser


def main():
    args = build_parser().parse_args()

    r = redis.Redis.from_url(
        "redis://127.0.0.1:6379/0",
        password=laika_redis.password(), decode_responses=True,
    )

    builder_key = f"laika:jobs:{args.builder_job_id}"
    builder = r.hgetall(builder_key)

    if not builder:
        raise SystemExit(f"Builder job not found: {args.builder_job_id}")

    if builder.get("status") != "awaiting_review":
        raise SystemExit(
            f"Builder job status is {builder.get('status')!r}; "
            "expected 'awaiting_review'"
        )

    worktree = builder.get("integration_worktree")
    if not worktree:
        raise SystemExit("Builder job has no integrated worktree")

    candidate_commit = builder.get("integrated_candidate_commit")
    if not candidate_commit:
        raise SystemExit("Builder job has no integrated candidate commit")

    existing_review = builder.get("review_job_id")
    if existing_review:
        existing = r.hgetall(f"laika:jobs:{existing_review}")
        status = existing.get("status", "unknown") if existing else "missing"
        raise SystemExit(
            f"Builder job already has reviewer {existing_review} ({status})"
        )

    job_id = uuid.uuid4().hex[:8]

    # Reserve the reviewer slot atomically so manual and automatic dispatch
    # cannot enqueue duplicate reviews for the same builder.
    if not r.hsetnx(builder_key, "review_job_id", job_id):
        raise SystemExit(
            f"Builder job already has reviewer {r.hget(builder_key, 'review_job_id')}"
        )

    prompt = f"""You are the review agent for LAIka.

Review builder job {args.builder_job_id}.
Review immutable integrated candidate commit {candidate_commit}.

Original task:
{builder.get("prompt", "")}

You are operating inside the builder's completed worktree.

Review the implementation for:
- correctness
- missed requirements
- regressions
- integration problems
- security or unsafe behavior
- maintainability issues

Inspect the Git diff and relevant surrounding code.

Do not modify any files.
Do not commit anything.

End your response with exactly one verdict line:
VERDICT: PASS
or
VERDICT: CHANGES_REQUIRED

Before the verdict, provide concise actionable findings. If there are no
material findings, explicitly say so.
"""

    job = {
        "id": job_id,
        "prompt": prompt,
        "provider": args.provider,
        "model": args.model,
        "role": "reviewer",
        "builder_job_id": args.builder_job_id,
        "worktree": worktree,
        "candidate_commit": candidate_commit,
        "created_at": time.time(),
    }

    try:
        r.hset(
            f"laika:jobs:{job_id}",
            mapping={
                "status": "queued",
                "provider": args.provider,
                "model": args.model,
                "role": "reviewer",
                "builder_job_id": args.builder_job_id,
                "worktree": worktree,
                "candidate_commit": candidate_commit,
                "prompt": prompt,
                "created_at": str(job["created_at"]),
            },
        )
        r.hset(
            builder_key,
            mapping={
                "review_status": "queued",
                "updated_at": str(time.time()),
            },
        )
        r.rpush("laika:jobs", json.dumps(job))
    except Exception:
        if r.hget(builder_key, "review_job_id") == job_id:
            r.hdel(builder_key, "review_job_id", "review_status")
        r.delete(f"laika:jobs:{job_id}")
        raise

    print(job_id)


if __name__ == "__main__":
    main()
