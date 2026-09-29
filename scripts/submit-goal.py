#!/usr/bin/env python3

import argparse
import json
import time
import uuid

import redis


def main():
    parser = argparse.ArgumentParser(
        description="Submit a high-level goal to SID"
    )
    parser.add_argument("goal")
    parser.add_argument("--atomic", action="store_true", help="Require exactly one implementation job")
    args = parser.parse_args()

    r = redis.Redis.from_url(
        "redis://127.0.0.1:6379/0",
        decode_responses=True,
    )

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

    r.hset(f"sid:goals:{goal_id}", mapping=record)
    r.rpush(
        "sid:goals",
        json.dumps({
            "id": goal_id,
            "goal": args.goal,
            "created_at": created,
            "atomic": args.atomic,
        }),
    )

    print(f"Goal queued: {goal_id}")
    print(f"Goal: {args.goal}")
    print(f"Atomic: {args.atomic}")


if __name__ == "__main__":
    main()
