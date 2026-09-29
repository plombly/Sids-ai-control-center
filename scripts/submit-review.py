#!/usr/bin/env python3

import argparse
import json
import time
import uuid

import redis


parser = argparse.ArgumentParser(
    description="Queue a SID review of an existing builder job"
)
parser.add_argument("builder_job_id", help="Builder job to review")
parser.add_argument("--model", default="gpt-5.6-luna")
parser.add_argument("--provider", default="codex")
args = parser.parse_args()

r = redis.Redis.from_url(
    "redis://127.0.0.1:6379/0",
    decode_responses=True,
)

builder_key = f"sid:jobs:{args.builder_job_id}"
builder = r.hgetall(builder_key)

if not builder:
    raise SystemExit(f"Builder job not found: {args.builder_job_id}")

if builder.get("status") != "awaiting_review":
    raise SystemExit(
        f"Builder job status is {builder.get('status')!r}; "
        "expected 'awaiting_review'"
    )

worktree = builder.get("worktree")
if not worktree:
    raise SystemExit("Builder job has no worktree")

job_id = uuid.uuid4().hex[:8]

prompt = f"""You are the review agent for SID's AI Command Center.

Review builder job {args.builder_job_id}.

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
    "created_at": time.time(),
}

r.hset(
    f"sid:jobs:{job_id}",
    mapping={
        "status": "queued",
        "provider": args.provider,
        "model": args.model,
        "role": "reviewer",
        "builder_job_id": args.builder_job_id,
        "worktree": worktree,
        "prompt": prompt,
        "created_at": str(job["created_at"]),
    },
)

r.rpush("sid:jobs", json.dumps(job))

print(job_id)
