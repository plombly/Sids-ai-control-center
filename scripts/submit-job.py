#!/usr/bin/env python3

import argparse
import json
import time
import uuid

import redis


parser = argparse.ArgumentParser(description="Queue a job for the SID worker")
parser.add_argument("task", help="Task description")
parser.add_argument("--model", default="gpt-5.6-luna", help="Model to use")
parser.add_argument("--provider", default="codex", help="Provider to use")
parser.add_argument(
    "--priority",
    type=int,
    default=0,
    help="Integer job priority (default: 0)",
)
args = parser.parse_args()

r = redis.Redis.from_url(
    "redis://127.0.0.1:6379/0",
    decode_responses=True,
)

job_id = uuid.uuid4().hex[:8]

job = {
    "id": job_id,
    "prompt": args.task,
    "provider": args.provider,
    "model": args.model,
    "priority": args.priority,
    "created_at": time.time(),
}

r.hset(
    f"sid:jobs:{job_id}",
    mapping={
        "status": "queued",
        "provider": job["provider"],
        "model": job["model"],
        "priority": str(job["priority"]),
        "prompt": job["prompt"],
        "created_at": str(job["created_at"]),
    },
)

r.rpush("sid:jobs", json.dumps(job))

print(job_id)
