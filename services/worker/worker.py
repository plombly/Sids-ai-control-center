import json
import os
import socket
import time

from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
QUEUE_NAME = os.environ.get("WORKER_QUEUE", "sid:jobs")

WORKER_ID = os.environ.get(
    "WORKER_ID",
    f"{socket.gethostname()}-{os.getpid()}",
)

WORKER_ROLE = os.environ.get("WORKER_ROLE", "builder")
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "codex")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-5.6-luna")

redis = Redis.from_url(REDIS_URL, decode_responses=True)


def worker_key():
    return f"sid:workers:{WORKER_ID}"


def register_worker():
    redis.hset(
        worker_key(),
        mapping={
            "id": WORKER_ID,
            "role": WORKER_ROLE,
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "status": "idle",
            "started_at": str(time.time()),
            "last_seen": str(time.time()),
        },
    )
    redis.expire(worker_key(), 30)


def heartbeat(status="idle"):
    redis.hset(
        worker_key(),
        mapping={
            "status": status,
            "last_seen": str(time.time()),
        },
    )
    redis.expire(worker_key(), 30)


def process_job(raw_job):
    job = json.loads(raw_job)

    job_id = job.get("id", "unknown")
    provider = job.get("provider", DEFAULT_PROVIDER)
    model = job.get("model", DEFAULT_MODEL)

    heartbeat("working")

    print(
        f"[worker={WORKER_ID}] "
        f"role={WORKER_ROLE} "
        f"job={job_id} "
        f"provider={provider} "
        f"model={model}",
        flush=True,
    )

    redis.hset(
        f"sid:jobs:{job_id}",
        mapping={
            "status": "received",
            "worker_id": WORKER_ID,
            "worker_role": WORKER_ROLE,
            "provider": provider,
            "model": model,
            "updated_at": str(time.time()),
        },
    )

    # Actual provider execution is intentionally added next.
    heartbeat("idle")


def main():
    print(
        f"SID worker starting: {WORKER_ID} "
        f"role={WORKER_ROLE} "
        f"default={DEFAULT_PROVIDER}/{DEFAULT_MODEL}",
        flush=True,
    )

    while True:
        try:
            register_worker()

            while True:
                heartbeat()

                item = redis.blpop(QUEUE_NAME, timeout=5)

                if item:
                    _, raw_job = item
                    process_job(raw_job)

        except KeyboardInterrupt:
            break

        except Exception as exc:
            print(f"Worker error: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
