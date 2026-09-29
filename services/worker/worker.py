import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
QUEUE_NAME = os.environ.get("WORKER_QUEUE", "sid:jobs")
REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/sids-ai-command-center"))
WORKTREE_ROOT = Path(os.environ.get("WORKTREE_ROOT", "/opt/sid-worktrees"))
LOG_ROOT = Path(os.environ.get("LOG_ROOT", "/var/log/sid-ai/jobs"))

WORKER_ID = os.environ.get("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
WORKER_ROLE = os.environ.get("WORKER_ROLE", "builder")
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "codex")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-5.6-luna")

MAX_RUNTIME = int(os.environ.get("MAX_JOB_RUNTIME", "1800"))

redis = Redis.from_url(REDIS_URL, decode_responses=True)


def worker_key():
    return f"sid:workers:{WORKER_ID}"


def heartbeat(status="idle"):
    redis.hset(
        worker_key(),
        mapping={
            "id": WORKER_ID,
            "role": WORKER_ROLE,
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "status": status,
            "last_seen": str(time.time()),
        },
    )
    redis.expire(worker_key(), 30)


def run_git(*args, cwd=REPO_ROOT, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def create_worktree(job_id):
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    branch = f"sid/job-{job_id}"
    path = WORKTREE_ROOT / f"job-{job_id}"

    if path.exists():
        raise RuntimeError(f"Worktree already exists: {path}")

    run_git("worktree", "add", "-b", branch, str(path), "main")
    return branch, path


def parse_codex_log(log_path):
    session_id = ""
    usage = {
        "input_tokens": "",
        "cached_input_tokens": "",
        "output_tokens": "",
        "reasoning_tokens": "",
        "total_tokens": "",
    }

    try:
        for line in log_path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not session_id:
                session_id = str(
                    event.get("thread_id")
                    or event.get("session_id")
                    or event.get("conversation_id")
                    or ""
                )

            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                input_tokens = event_usage.get("input_tokens")
                cached_tokens = event_usage.get("cached_input_tokens")
                output_tokens = event_usage.get("output_tokens")
                reasoning_tokens = event_usage.get("reasoning_output_tokens")

                if input_tokens is not None:
                    usage["input_tokens"] = str(input_tokens)

                if cached_tokens is not None:
                    usage["cached_input_tokens"] = str(cached_tokens)

                if output_tokens is not None:
                    usage["output_tokens"] = str(output_tokens)

                if reasoning_tokens is not None:
                    usage["reasoning_tokens"] = str(reasoning_tokens)

                if input_tokens is not None or output_tokens is not None:
                    usage["total_tokens"] = str(
                        int(input_tokens or 0) + int(output_tokens or 0)
                    )

    except OSError:
        pass

    return session_id, usage


def run_codex(job, worktree, log_path):
    model = job.get("model", DEFAULT_MODEL)
    prompt = job["prompt"]

    command = [
        "codex",
        "exec",
        "--model", model,
        "--sandbox", "workspace-write",
        "--ephemeral",
        "--color", "never",
        "--json",
        "--cd", str(worktree),
        "-",
    ]

    env = os.environ.copy()
    env["HOME"] = "/root"

    started = time.time()

    with log_path.open("w") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )

        process.stdin.write(prompt)
        process.stdin.close()

        timed_out = False

        while process.poll() is None:
            try:
                heartbeat("working")
            except Exception as exc:
                print(
                    f"[{WORKER_ID}] heartbeat warning: {exc}",
                    flush=True,
                )

            if time.time() - started >= MAX_RUNTIME:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)

                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

                break

            time.sleep(5)

        if timed_out:
            raise RuntimeError(
                f"Codex exceeded {MAX_RUNTIME} second job timeout"
            )

    return process.returncode, time.time() - started


def run_tests(worktree):
    commands = [
        [
            "/tmp/sid-agent-venv/bin/python",
            "-m",
            "pytest",
            "apps/api/tests",
            "-q",
        ],
        [
            "/tmp/sid-agent-venv/bin/python",
            "-m",
            "compileall",
            "-q",
            "apps/api",
        ],
    ]

    output = []

    for command in commands:
        result = subprocess.run(
            command,
            cwd=worktree,
            text=True,
            capture_output=True,
            timeout=300,
        )

        output.append(
            "$ " + " ".join(command) + "\n"
            + result.stdout
            + result.stderr
        )

        if result.returncode != 0:
            return False, "\n".join(output)

    return True, "\n".join(output)


def process_job(raw_job):
    job = json.loads(raw_job)
    job_id = str(job["id"])
    key = f"sid:jobs:{job_id}"

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"{job_id}.jsonl"
    test_log = LOG_ROOT / f"{job_id}-tests.log"

    try:
        heartbeat("working")

        redis.hset(
            key,
            mapping={
                "status": "claimed",
                "worker_id": WORKER_ID,
                "worker_role": WORKER_ROLE,
                "provider": job.get("provider", DEFAULT_PROVIDER),
                "model": job.get("model", DEFAULT_MODEL),
                "updated_at": str(time.time()),
            },
        )

        branch, worktree = create_worktree(job_id)

        redis.hset(
            key,
            mapping={
                "status": "running",
                "branch": branch,
                "worktree": str(worktree),
                "log": str(log_path),
                "updated_at": str(time.time()),
            },
        )

        returncode, duration = run_codex(job, worktree, log_path)
        session_id, usage = parse_codex_log(log_path)

        redis.hset(
            key,
            mapping={
                "codex_exit_code": str(returncode),
                "duration_seconds": f"{duration:.2f}",
                "session_id": session_id,
                "tokens": usage["total_tokens"],
                "input_tokens": usage["input_tokens"],
                "cached_input_tokens": usage["cached_input_tokens"],
                "output_tokens": usage["output_tokens"],
                "reasoning_tokens": usage["reasoning_tokens"],
                "updated_at": str(time.time()),
            },
        )

        if returncode != 0:
            raise RuntimeError(f"Codex exited with status {returncode}")

        redis.hset(key, mapping={"status": "testing"})

        tests_ok, tests_output = run_tests(worktree)
        test_log.write_text(tests_output)

        if not tests_ok:
            redis.hset(
                key,
                mapping={
                    "status": "test_failed",
                    "test_log": str(test_log),
                    "updated_at": str(time.time()),
                },
            )
            return

        diff = run_git("status", "--porcelain", cwd=worktree).stdout

        redis.hset(
            key,
            mapping={
                "status": "awaiting_review",
                "test_log": str(test_log),
                "changes": diff,
                "updated_at": str(time.time()),
            },
        )

    except Exception as exc:
        redis.hset(
            key,
            mapping={
                "status": "failed",
                "error": str(exc),
                "updated_at": str(time.time()),
            },
        )
        print(f"[{WORKER_ID}] job={job_id} FAILED: {exc}", flush=True)

    finally:
        heartbeat("idle")


def main():
    print(
        f"SID worker starting: {WORKER_ID} "
        f"role={WORKER_ROLE} "
        f"default={DEFAULT_PROVIDER}/{DEFAULT_MODEL}",
        flush=True,
    )

    heartbeat()

    while True:
        try:
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
