#!/usr/bin/env python3

import argparse
import json

import sys
from pathlib import Path
import redis
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import laika_redis  # noqa: E402  (services/laika_redis.py)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("goal_id")
    args = parser.parse_args()

    r = redis.Redis.from_url(
        "redis://127.0.0.1:6379/0",
        password=laika_redis.password(), decode_responses=True,
    )

    goal = r.hgetall(f"laika:goals:{args.goal_id}")

    if not goal:
        raise SystemExit("Goal not found")

    print(f"GOAL   {args.goal_id}")
    print(f"STATUS {goal.get('status', '-')}")
    print(f"TEXT   {goal.get('goal', '-')}")

    if goal.get("error"):
        print(f"ERROR  {goal['error']}")

    try:
        jobs = json.loads(goal.get("jobs", "[]"))
    except json.JSONDecodeError:
        jobs = []

    if jobs:
        print()
        print("JOBS")

        for job_id in jobs:
            job = r.hgetall(f"laika:jobs:{job_id}")
            print(
                f"{job_id}  "
                f"{job.get('status', '-'):26}  "
                f"{job.get('title', '-')}"
            )


if __name__ == "__main__":
    main()
