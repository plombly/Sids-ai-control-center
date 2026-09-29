import json
import os
import re
import signal
import socket
import subprocess
import time
import uuid
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
ROLE_RUNTIME_DEFAULTS = {"builder": 300, "reviewer": 150, "repair": 180}
ROLE_TOKEN_DEFAULTS = {"builder": 250000, "reviewer": 120000, "repair": 150000}
POLL_SECONDS = int(os.environ.get("CODEX_POLL_SECONDS", "2"))


def role_limit(role, kind, defaults):
    name = f"{role.upper()}_{kind}"
    return int(os.environ.get(name, str(defaults[role])))


def efficiency_prefix(role):
    return f"""SID execution contract ({role}):
- Work narrowly on the requested task. Do not inventory or read the whole repository.
- Start with git status/diff and targeted rg/sed reads of likely files only.
- Expand scope only when a concrete dependency requires it.
- Do not run broad test suites; SID runs deterministic gates after builders/repairs.
- Avoid repeated reads and verbose narration. Make the smallest correct change/review.
- Stop as soon as the task and focused validation are complete.

"""

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
    role = job.get("role", "builder")

    if role not in {"builder", "reviewer", "repair"}:
        raise RuntimeError(f"Unsupported job role: {role}")

    sandbox = "read-only" if role == "reviewer" else "workspace-write"
    runtime_limit = int(job.get("timeout_seconds") or role_limit(
        role, "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS
    ))
    token_budget = int(job.get("token_budget") or role_limit(
        role, "TOKEN_BUDGET", ROLE_TOKEN_DEFAULTS
    ))
    prompt = efficiency_prefix(role) + prompt

    command = [
        "codex",
        "exec",
        "--model", model,
        "--sandbox", sandbox,
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

            if time.time() - started >= runtime_limit:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)

            _, live_usage = parse_codex_log(log_path)
            live_total = int(live_usage.get("total_tokens") or 0)
            if live_total >= token_budget:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise RuntimeError(
                    f"Codex token budget exceeded: {live_total} >= {token_budget}"
                )

                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

                break

            time.sleep(POLL_SECONDS)

        if timed_out:
            raise RuntimeError(
                f"Codex exceeded {runtime_limit} second {role} timeout"
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


def queue_review_job(builder_job_id):
    builder_key = f"sid:jobs:{builder_job_id}"
    builder = redis.hgetall(builder_key)

    if not builder:
        raise RuntimeError(f"Builder job not found: {builder_job_id}")

    if builder.get("status") != "awaiting_review":
        raise RuntimeError(
            f"Builder job status is {builder.get('status')!r}; "
            "expected 'awaiting_review'"
        )

    worktree = builder.get("worktree")
    if not worktree:
        raise RuntimeError("Builder job has no worktree")

    candidate_commit = builder.get("candidate_commit")
    if not candidate_commit:
        raise RuntimeError("Builder job has no candidate commit")

    review_job_id = uuid.uuid4().hex[:8]

    # Atomic duplicate guard. Only the process that successfully creates
    # review_job_id is allowed to enqueue the reviewer.
    if not redis.hsetnx(builder_key, "review_job_id", review_job_id):
        return builder.get("review_job_id", ""), False

    diff_result = run_git(
        "diff", "--no-ext-diff", "--unified=60",
        f"{candidate_commit}^", candidate_commit,
        cwd=Path(worktree), check=False,
    )
    candidate_diff = (diff_result.stdout or "")[:30000]
    if len(diff_result.stdout or "") > 30000:
        candidate_diff += "\n... [diff truncated by SID at 30000 chars]"

    prompt = f"""You are the review agent for SID's AI Command Center.

Review builder job {builder_job_id}.
Review immutable candidate commit {candidate_commit}.

Original task:
{builder.get("prompt", "")}

SID candidate diff (inspect this first; it is the primary review context):
{candidate_diff}

You are operating inside the builder's completed worktree.

Review the implementation for:
- correctness
- missed requirements
- regressions
- integration problems
- security or unsafe behavior
- maintainability issues

Use the SID-provided candidate diff above first. Do not rerun broad repository
discovery merely to reconstruct it. Read surrounding code only when needed to
validate a concrete concern. Keep the review focused on the requested task.

The builder's deterministic test gate has already run. Do not broadly rerun
the entire repository test suite merely to repeat that gate. If a focused
Python test is necessary, use SID's existing environment explicitly:

/tmp/sid-agent-venv/bin/python -m pytest <focused-test-path> -q

Do not modify any files.
Do not commit anything.

End your response with exactly one verdict line:
VERDICT: PASS
or
VERDICT: CHANGES_REQUIRED

Before the verdict, provide concise actionable findings. If there are no
material findings, explicitly say so.
"""

    created_at = time.time()

    review_job = {
        "id": review_job_id,
        "prompt": prompt,
        "provider": builder.get("provider", DEFAULT_PROVIDER),
        "model": builder.get("model", DEFAULT_MODEL),
        "role": "reviewer",
        "builder_job_id": builder_job_id,
        "worktree": worktree,
        "candidate_commit": candidate_commit,
        "created_at": created_at,
    }

    try:
        redis.hset(
            f"sid:jobs:{review_job_id}",
            mapping={
                "status": "queued",
                "provider": review_job["provider"],
                "model": review_job["model"],
                "role": "reviewer",
                "builder_job_id": builder_job_id,
                "worktree": worktree,
                "candidate_commit": candidate_commit,
                "prompt": prompt,
                "created_at": str(created_at),
            },
        )

        redis.hset(
            builder_key,
            mapping={
                "review_status": "queued",
                "updated_at": str(time.time()),
            },
        )

        redis.rpush(QUEUE_NAME, json.dumps(review_job))

    except Exception:
        # Release the reservation only if it is still ours.
        current = redis.hget(builder_key, "review_job_id")
        if current == review_job_id:
            redis.hdel(builder_key, "review_job_id", "review_status")
        redis.delete(f"sid:jobs:{review_job_id}")
        raise

    return review_job_id, True


def process_review_job(job, key, log_path):
    job_id = str(job["id"])
    builder_job_id = str(job.get("builder_job_id", ""))
    worktree_value = job.get("worktree")

    if not builder_job_id:
        raise RuntimeError("Reviewer job missing builder_job_id")

    if not worktree_value:
        raise RuntimeError("Reviewer job missing builder worktree")

    worktree = Path(worktree_value).resolve()
    expected = (WORKTREE_ROOT / f"job-{builder_job_id}").resolve()

    if worktree != expected:
        raise RuntimeError(f"Unexpected reviewer worktree: {worktree}")

    if not worktree.exists():
        raise RuntimeError(f"Builder worktree does not exist: {worktree}")

    builder = redis.hgetall(f"sid:jobs:{builder_job_id}")
    if not builder:
        raise RuntimeError(f"Builder job not found: {builder_job_id}")

    if builder.get("status") != "awaiting_review":
        raise RuntimeError(
            f"Builder job status is {builder.get('status')!r}; "
            "expected 'awaiting_review'"
        )

    candidate_commit = str(job.get("candidate_commit", ""))
    if not candidate_commit:
        raise RuntimeError("Reviewer job missing candidate_commit")
    if builder.get("candidate_commit") != candidate_commit:
        raise RuntimeError("Reviewer candidate does not match builder candidate")

    head = run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if head != candidate_commit:
        raise RuntimeError("Builder worktree HEAD does not match candidate commit")
    if run_git("status", "--porcelain", cwd=worktree).stdout.strip():
        raise RuntimeError("Builder worktree changed after candidate commit")

    redis.hset(
        key,
        mapping={
            "status": "reviewing",
            "worker_id": WORKER_ID,
            "worker_role": WORKER_ROLE,
            "job_role": "reviewer",
            "provider": job.get("provider", DEFAULT_PROVIDER),
            "model": job.get("model", DEFAULT_MODEL),
            "builder_job_id": builder_job_id,
            "worktree": str(worktree),
            "candidate_commit": candidate_commit,
            "log": str(log_path),
            "token_budget": str(job.get("token_budget") or role_limit("reviewer", "TOKEN_BUDGET", ROLE_TOKEN_DEFAULTS)),
            "timeout_seconds": str(job.get("timeout_seconds") or role_limit("reviewer", "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS)),
            "updated_at": str(time.time()),
        },
    )

    returncode, duration = run_codex(job, worktree, log_path)
    session_id, usage = parse_codex_log(log_path)

    head_after = run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    dirty_after = run_git("status", "--porcelain", cwd=worktree).stdout.strip()
    if head_after != candidate_commit or dirty_after:
        raise RuntimeError("Candidate changed during read-only review")

    verdict = "unknown"

    for line in log_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "item.completed":
            continue

        item = event.get("item", {})
        if item.get("type") != "agent_message":
            continue

        for message_line in item.get("text", "").splitlines():
            normalized = message_line.strip()

            if normalized == "VERDICT: PASS":
                verdict = "pass"
            elif normalized == "VERDICT: CHANGES_REQUIRED":
                verdict = "changes_required"

    common = {
        "review_verdict": verdict,
        "reviewed_commit": candidate_commit,
        "codex_exit_code": str(returncode),
        "duration_seconds": f"{duration:.2f}",
        "session_id": session_id,
        "tokens": usage["total_tokens"],
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "updated_at": str(time.time()),
    }

    if returncode != 0:
        redis.hset(key, mapping={"status": "failed", **common})
        redis.hset(
            f"sid:jobs:{builder_job_id}",
            mapping={
                "review_job_id": job_id,
                "review_status": "failed",
                "review_verdict": verdict,
                "reviewed_commit": candidate_commit,
                "review_log": str(log_path),
                "updated_at": str(time.time()),
            },
        )
        raise RuntimeError(f"Reviewer Codex exited with status {returncode}")

    redis.hset(key, mapping={"status": "review_complete", **common})
    redis.hset(
        f"sid:jobs:{builder_job_id}",
        mapping={
            "review_job_id": job_id,
            "review_status": "complete",
            "review_verdict": verdict,
            "reviewed_commit": candidate_commit,
            "review_log": str(log_path),
            "updated_at": str(time.time()),
        },
    )



def process_repair_job(job, key, log_path, test_log):
    job_id = str(job["id"])
    builder_job_id = str(job.get("target_builder_id", ""))

    if not builder_job_id:
        raise RuntimeError("Repair job missing target_builder_id")

    builder_key = f"sid:jobs:{builder_job_id}"
    builder = redis.hgetall(builder_key)

    if not builder:
        raise RuntimeError(f"Repair target not found: {builder_job_id}")

    if builder.get("status") != "awaiting_review":
        raise RuntimeError(
            f"Repair target status is {builder.get('status')!r}; "
            "expected 'awaiting_review'"
        )

    if builder.get("review_verdict") != "changes_required":
        raise RuntimeError(
            "Repair target does not currently require changes"
        )

    worktree_value = builder.get("worktree")
    if not worktree_value:
        raise RuntimeError("Repair target has no worktree")

    worktree = Path(worktree_value).resolve()
    expected = (WORKTREE_ROOT / f"job-{builder_job_id}").resolve()

    if worktree != expected:
        raise RuntimeError(f"Unexpected repair worktree: {worktree}")

    if not worktree.exists():
        raise RuntimeError(f"Repair worktree missing: {worktree}")

    candidate_before = builder.get("candidate_commit")
    if not candidate_before:
        raise RuntimeError("Repair target has no candidate commit")

    head = run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()

    if head != candidate_before:
        raise RuntimeError(
            "Repair worktree HEAD does not match reviewed candidate"
        )

    if run_git("status", "--porcelain", cwd=worktree).stdout.strip():
        raise RuntimeError("Repair worktree is dirty before repair")

    redis.hset(
        key,
        mapping={
            "status": "repairing",
            "worker_id": WORKER_ID,
            "worker_role": WORKER_ROLE,
            "job_role": "repair",
            "target_builder_id": builder_job_id,
            "worktree": str(worktree),
            "candidate_before": candidate_before,
            "log": str(log_path),
            "token_budget": str(job.get("token_budget") or role_limit("repair", "TOKEN_BUDGET", ROLE_TOKEN_DEFAULTS)),
            "timeout_seconds": str(job.get("timeout_seconds") or role_limit("repair", "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS)),
            "updated_at": str(time.time()),
        },
    )

    redis.hset(
        builder_key,
        mapping={
            "repair_status": "running",
            "updated_at": str(time.time()),
        },
    )

    returncode, duration = run_codex(job, worktree, log_path)
    session_id, usage = parse_codex_log(log_path)

    common = {
        "codex_exit_code": str(returncode),
        "duration_seconds": f"{duration:.2f}",
        "session_id": session_id,
        "tokens": usage["total_tokens"],
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "updated_at": str(time.time()),
    }

    redis.hset(key, mapping=common)

    if returncode != 0:
        raise RuntimeError(
            f"Repair Codex exited with status {returncode}"
        )

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
        redis.hset(
            builder_key,
            mapping={
                "repair_status": "test_failed",
                "updated_at": str(time.time()),
            },
        )
        return

    diff = run_git("status", "--porcelain", cwd=worktree).stdout

    if not diff.strip():
        redis.hset(
            key,
            mapping={
                "status": "repair_no_changes",
                "test_log": str(test_log),
                "updated_at": str(time.time()),
            },
        )
        redis.hset(
            builder_key,
            mapping={
                "repair_status": "no_changes",
                "updated_at": str(time.time()),
            },
        )
        return

    run_git("add", "-A", cwd=worktree)
    run_git(
        "commit",
        "-m",
        f"Repair SID job {builder_job_id} via {job_id}",
        cwd=worktree,
    )

    candidate_after = run_git(
        "rev-parse", "HEAD", cwd=worktree
    ).stdout.strip()

    if candidate_after == candidate_before:
        raise RuntimeError("Repair did not create a new candidate")

    if run_git("status", "--porcelain", cwd=worktree).stdout.strip():
        raise RuntimeError("Repair candidate is dirty after commit")

    old_review = builder.get("review_job_id", "")

    history = builder.get("review_history", "")
    history_item = (
        f"{old_review}:{candidate_before}:"
        f"{builder.get('review_verdict', '')}"
    )

    history = (
        f"{history},{history_item}"
        if history
        else history_item
    )

    # Remove the old review reservation so queue_review_job() can
    # atomically reserve a fresh independent reviewer.
    redis.hdel(
        builder_key,
        "review_job_id",
        "review_status",
        "review_verdict",
        "reviewed_commit",
        "review_error",
    )

    redis.hset(
        builder_key,
        mapping={
            "candidate_commit": candidate_after,
            "review_history": history,
            "repair_status": "completed",
            "last_repair_job_id": job_id,
            "updated_at": str(time.time()),
        },
    )

    redis.hset(
        key,
        mapping={
            "status": "repair_complete",
            "candidate_before": candidate_before,
            "candidate_after": candidate_after,
            "test_log": str(test_log),
            "updated_at": str(time.time()),
        },
    )

    review_job_id, queued = queue_review_job(builder_job_id)

    print(
        f"[{WORKER_ID}] repair={job_id} "
        f"builder={builder_job_id} "
        f"candidate={candidate_after[:12]} "
        f"review={review_job_id} "
        f"{'queued' if queued else 'already queued'}",
        flush=True,
    )

def process_job(raw_job):
    job = json.loads(raw_job)
    job_id = str(job["id"])
    key = f"sid:jobs:{job_id}"

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"{job_id}.jsonl"
    test_log = LOG_ROOT / f"{job_id}-tests.log"

    try:
        heartbeat("working")

        if job.get("role", "builder") == "reviewer":
            process_review_job(job, key, log_path)
            return

        if job.get("role") == "repair":
            process_repair_job(
                job,
                key,
                log_path,
                test_log,
            )
            return

        redis.hset(
            key,
            mapping={
                "status": "claimed",
                "worker_id": WORKER_ID,
                "worker_role": WORKER_ROLE,
                "job_role": job.get("role", "builder"),
                "provider": job.get("provider", DEFAULT_PROVIDER),
                "model": job.get("model", DEFAULT_MODEL),
                "token_budget": str(job.get("token_budget") or role_limit("builder", "TOKEN_BUDGET", ROLE_TOKEN_DEFAULTS)),
                "timeout_seconds": str(job.get("timeout_seconds") or role_limit("builder", "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS)),
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

        if not diff.strip():
            redis.hset(
                key,
                mapping={
                    "status": "completed_no_changes",
                    "test_log": str(test_log),
                    "changes": "",
                    "updated_at": str(time.time()),
                },
            )

            run_git("worktree", "remove", str(worktree))
            run_git("branch", "-D", branch)

            print(
                f"[{WORKER_ID}] job={job_id} completed with no changes",
                flush=True,
            )
            return

        run_git("add", "-A", cwd=worktree)
        run_git(
            "commit",
            "-m",
            f"Apply SID job {job_id}",
            cwd=worktree,
        )
        candidate_commit = run_git(
            "rev-parse", "HEAD", cwd=worktree
        ).stdout.strip()

        if run_git("status", "--porcelain", cwd=worktree).stdout.strip():
            raise RuntimeError("Candidate worktree is dirty after commit")

        redis.hset(
            key,
            mapping={
                "status": "awaiting_review",
                "test_log": str(test_log),
                "changes": diff,
                "candidate_commit": candidate_commit,
                "updated_at": str(time.time()),
            },
        )

        try:
            review_job_id, queued = queue_review_job(job_id)
        except Exception as exc:
            redis.hset(
                key,
                mapping={
                    "review_status": "queue_failed",
                    "review_error": str(exc),
                    "updated_at": str(time.time()),
                },
            )
            print(
                f"[{WORKER_ID}] job={job_id} review queue FAILED: {exc}",
                flush=True,
            )
            return

        print(
            f"[{WORKER_ID}] job={job_id} review={review_job_id} "
            f"{'queued' if queued else 'already queued'}",
            flush=True,
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

        if job.get("role", "builder") == "repair":
            target_builder_id = str(
                job.get("target_builder_id", "")
            )
            if target_builder_id:
                redis.hset(
                    f"sid:jobs:{target_builder_id}",
                    mapping={
                        "repair_status": "failed",
                        "repair_error": str(exc),
                        "updated_at": str(time.time()),
                    },
                )
                redis.hdel(
                    f"sid:jobs:{target_builder_id}",
                    "repair_job_id",
                )

        if job.get("role", "builder") == "reviewer":
            builder_job_id = str(job.get("builder_job_id", ""))
            if builder_job_id:
                redis.hset(
                    f"sid:jobs:{builder_job_id}",
                    mapping={
                        "review_status": "failed",
                        "review_error": str(exc),
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
