#!/usr/bin/env python3

import argparse
import json
import time
import uuid

import sys
from pathlib import Path
import redis
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_redis  # noqa: E402  (services/laika_redis.py)


def main():
    parser = argparse.ArgumentParser(
        description="Submit a high-level goal to LAIka"
    )
    parser.add_argument("goal")
    parser.add_argument("--atomic", action="store_true", help="Require exactly one implementation job")
    parser.add_argument("--project", default="", help="Project id (default: LAIka itself)")
    args = parser.parse_args()

    r = redis.Redis.from_url(
        "redis://127.0.0.1:6379/0",
        password=laika_redis.password(), decode_responses=True,
    )

    project_id = args.project.strip()
    if project_id and project_id != "laika" and not r.sismember("laika:projects", project_id):
        parser.error(f"unknown project: {project_id}")
    goal_id = uuid.uuid4().hex[:8]
    created = str(time.time())

    record = {
        "id": goal_id,
        "goal": args.goal,
        "status": "queued",
        "created_at": created,
        "updated_at": created,
        "atomic": "1" if args.atomic else "0",
    }
    queued = {"id": goal_id, "goal": args.goal, "created_at": created, "atomic": args.atomic}
    if project_id:
        record["project_id"] = queued["project_id"] = project_id

    r.hset(f"laika:goals:{goal_id}", mapping=record)
    r.rpush("laika:goals", json.dumps(queued))

    print(f"Goal queued: {goal_id}")
    print(f"Goal: {args.goal}")
    print(f"Atomic: {args.atomic}")
    if project_id:
        print(f"Project: {project_id}")


if __name__ == "__main__":
    main()
