#!/usr/bin/env python3

import json
import sys
import time
import uuid

import redis

if len(sys.argv) < 2:
    print('Usage: submit-job.py "task description"')
    raise SystemExit(1)

r = redis.Redis.from_url(
    "redis://127.0.0.1:6379/0",
    decode_responses=True,
)

job_id = uuid.uuid4().hex[:8]

job = {
    "id": job_id,
    "prompt": sys.argv[1],
    "provider": "codex",
    "model": "gpt-5.6-luna",
    "created_at": time.time(),
}

r.hset(
    f"sid:jobs:{job_id}",
    mapping={
        "status": "queued",
        "provider": job["provider"],
        "model": job["model"],
        "prompt": job["prompt"],
        "created_at": str(job["created_at"]),
    },
)

r.rpush("sid:jobs", json.dumps(job))

print(job_id)
