import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from redis import Redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_cli  # noqa: E402  (services/agent_cli.py)

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
ROLE_RUNTIME_DEFAULTS = {"builder": 600, "reviewer": 240, "repair": 360}
ROLE_TOKEN_DEFAULTS = {"builder": 100000, "reviewer": 75000, "repair": 100000}
ROLE_ENFORCEMENT_DEFAULTS = {"builder": 100000, "reviewer": 60000, "repair": 80000}
POLL_SECONDS = int(os.environ.get("CODEX_POLL_SECONDS", "2"))
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "10"))
REVIEW_DIFF_CHARS = int(os.environ.get("REVIEW_DIFF_CHARS", "60000"))
REVIEW_GATE_CHARS = int(os.environ.get("REVIEW_GATE_CHARS", "3000"))
REVIEW_PRIOR_CHARS = int(os.environ.get("REVIEW_PRIOR_CHARS", "4000"))
FINDINGS_CHARS = 6000


def resolve_sid_python():
    """Python used for SID test gates.

    /tmp is cleared on reboot, so the durable default is /opt/sid-venv.
    The legacy /tmp location is only used when nothing else is configured.
    """
    configured = os.environ.get("SID_PYTHON")
    if configured:
        return configured
    durable = Path("/opt/sid-venv/bin/python")
    if durable.exists():
        return str(durable)
    return "/tmp/sid-agent-venv/bin/python"


SID_PYTHON = resolve_sid_python()


def role_limit(role, kind, defaults):
    name = f"{role.upper()}_{kind}"
    return int(os.environ.get(name, str(defaults[role])))


def efficiency_prefix(role):
    return f"""SID execution contract ({role}):
- Work narrowly on the requested task. Do not inventory or read the whole repository.
- Start with git status/diff and targeted rg/sed reads of likely files only.
- Expand scope only when a concrete dependency requires it.
- Do not run broad test suites; SID runs deterministic gates after builders/repairs.
- For focused Python tests, use {SID_PYTHON} -m pytest; do not probe python/pytest executables.
- Avoid repeated reads and verbose narration. Make the smallest correct change/review.
- Stop as soon as the task and focused validation are complete.

"""

redis = Redis.from_url(REDIS_URL, decode_responses=True)

# The job this process is working on, published in every heartbeat. The
# orchestrator treats work as in flight only while a live worker holds it
# (or it is still queued), which is how it detects jobs orphaned by a
# worker restart or crash.
CURRENT_JOB_ID = ""
# Role, provider and model of the held job, published in the heartbeat so the
# dashboard shows what is actually running (any worker takes any role).
CURRENT_JOB_ROLE = ""
CURRENT_AGENT = {}


def worker_key():
    return f"sid:workers:{WORKER_ID}"


def control_key(worker_id=None):
    # Operator control lives outside the heartbeat hash so that heartbeats
    # can never overwrite a stop request.
    return f"sid:worker-control:{worker_id or WORKER_ID}"


def worker_disabled():
    try:
        return redis.get(control_key()) == "disabled"
    except Exception:
        return False


def heartbeat(status="idle"):
    if status == "idle" and worker_disabled():
        status = "disabled"
    redis.hset(
        worker_key(),
        mapping={
            "id": WORKER_ID,
            "role": CURRENT_JOB_ROLE or "any",
            "provider": CURRENT_AGENT.get("provider") or "per role",
            "model": CURRENT_AGENT.get("model") or agent_cli.routing_summary(DEFAULT_MODEL),
            "status": status,
            "job_id": CURRENT_JOB_ID,
            "last_seen": str(time.time()),
        },
    )
    redis.expire(worker_key(), 30)


@contextmanager
def keep_alive(status="working"):
    """Heartbeat in the background during long steps (tests, integration).

    Without this the 30s heartbeat key expires while a busy worker runs its
    test gate, and the worker vanishes from every dashboard.
    """
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                heartbeat(status)
            except Exception as exc:
                print(f"[{WORKER_ID}] heartbeat warning: {exc}", flush=True)

    thread = threading.Thread(target=beat, name="sid-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=HEARTBEAT_SECONDS + 1)


def run_git(*args, cwd=REPO_ROOT, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def integration_worktree(job_id):
    return (WORKTREE_ROOT / f"job-{job_id}-integration").resolve()


def cleanup_integration(job_id, data=None):
    data = data or redis.hgetall(f"sid:jobs:{job_id}")
    path = integration_worktree(job_id)
    branch = f"sid/integration-{job_id}"
    recorded_value = data.get("integration_worktree", "")
    recorded = Path(recorded_value).resolve() if recorded_value else None
    if recorded is not None and recorded != path:
        raise RuntimeError(f"Unexpected integration worktree: {recorded}")
    if path.exists():
        run_git("worktree", "remove", "--force", str(path))
    branch_exists = run_git("show-ref", "--verify", f"refs/heads/{branch}", check=False)
    if branch_exists.returncode == 0:
        run_git("branch", "-D", branch)


def prepare_integration(job_id):
    """Integrate source candidates without touching main, then run the gate."""
    key = f"sid:jobs:{job_id}"
    lock_key = f"sid:integration-lock:{job_id}"
    lock_owner = f"{WORKER_ID}:{uuid.uuid4().hex}"
    lock_ttl = max(MAX_RUNTIME + 300, 7200)

    # Exactly one worker may create/replace a job's integration candidate.
    # The TTL provides crash recovery; ownership-safe release prevents an
    # expired/reacquired lock from being deleted by the previous owner.
    acquired = redis.set(lock_key, lock_owner, nx=True, ex=lock_ttl)
    if not acquired:
        return False

    try:
        return _prepare_integration_locked(job_id, key)
    finally:
        redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            lock_key,
            lock_owner,
        )


def _prepare_integration_locked(job_id, key):
    data = redis.hgetall(key)
    integration_metadata = {}
    if data.get("integration_status") == "passed" and data.get("integrated_candidate_commit"):
        return True

    if run_git("status", "--porcelain").stdout.strip():
        redis.hset(key, mapping={"status": "integration_failed", "integration_status": "failed",
                                  "integration_error": "main worktree is not clean",
                                  "updated_at": str(time.time())})
        return False

    try:
        cleanup_integration(job_id, data)
        base = run_git("rev-parse", "HEAD").stdout.strip()
        raw_sources = data.get("source_candidate_commits", "")
        if raw_sources:
            sources = json.loads(raw_sources)
        else:
            sources = [data.get("candidate_commit", "")]
        if not isinstance(sources, list) or not sources or not all(sources):
            raise RuntimeError("missing ordered source candidate commits")

        path = integration_worktree(job_id)
        branch = f"sid/integration-{job_id}"
        WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
        run_git("worktree", "add", "-b", branch, str(path), base)
        redis.hset(key, mapping={
            "integration_worktree": str(path),
            "integration_branch": branch,
            "integration_base_commit": base,
            "source_candidate_commits": json.dumps(sources, separators=(",", ":")),
            "integration_status": "running",
            "updated_at": str(time.time()),
        })

        for source in sources:
            result = run_git("cherry-pick", "--no-edit", source, cwd=path, check=False)
            if result.returncode != 0:
                run_git("cherry-pick", "--abort", cwd=path, check=False)
                raise RuntimeError(f"cannot apply source candidate {source}: {result.stderr.strip()}")

        env = os.environ.copy()
        env["REPO_ROOT"] = str(path)
        gate = subprocess.run(
            [str(path / "scripts/integration-check.py")],
            cwd=path, env=env, text=True, capture_output=True,
        )
        integrated = run_git("rev-parse", "HEAD", cwd=path).stdout.strip()
        integration_metadata = {
            "returncode": gate.returncode,
            "stdout": gate.stdout[-4000:],
            "stderr": gate.stderr[-4000:],
            "commit": integrated,
        }
        if gate.returncode != 0:
            raise RuntimeError("deterministic integration gate failed")

        redis.hset(key, mapping={
            "status": "awaiting_review",
            "integration_status": "passed",
            "integrated_candidate_commit": integrated,
            "integration_result": json.dumps(integration_metadata, separators=(",", ":")),
            "updated_at": str(time.time()),
        })
        return True
    except Exception as exc:
        current = redis.hgetall(key)
        try:
            cleanup_integration(job_id, current)
        except Exception:
            pass
        redis.hset(key, mapping={
            "status": "integration_failed",
            "integration_status": "failed",
            "integration_error": str(exc),
            "integration_result": json.dumps(
                {**integration_metadata, "error": str(exc)},
                separators=(",", ":"),
            ),
            "updated_at": str(time.time()),
        })
        return False


def create_worktree(job_id, suffix=""):
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    branch = f"sid/job-{job_id}{suffix}"
    path = WORKTREE_ROOT / f"job-{job_id}{suffix}"

    # A retried or orphaned build leaves this job's own worktree and branch
    # behind. Once the job is claimed again they are stale by definition.
    run_git("worktree", "prune", check=False)
    if path.exists():
        run_git("worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            raise RuntimeError(f"Stale worktree could not be removed: {path}")
    if run_git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
        run_git("branch", "-D", branch)

    run_git("worktree", "add", "-b", branch, str(path), "main")
    return branch, path


def parse_codex_log(log_path):
    """(session_id, usage) from a job log: Codex JSONL or a Claude result."""
    claude_result = agent_cli.read_claude_result(log_path)
    if claude_result is not None:
        return str(claude_result.get("session_id") or ""), agent_cli.claude_usage(claude_result)
    session_id = ""
    usage = {
        "input_tokens": "",
        "cached_input_tokens": "",
        "output_tokens": "",
        "reasoning_tokens": "",
        "uncached_input_tokens": "",
        "effective_tokens": "",
        "total_tokens": "",
        "command_count": "0",
        "turn_completed": "0",
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

            if event.get("type") == "turn.completed":
                usage["turn_completed"] = "1"

            item = event.get("item")
            if (
                event.get("type") == "item.started"
                and isinstance(item, dict)
                and item.get("type") == "command_execution"
            ):
                usage["command_count"] = str(int(usage["command_count"]) + 1)

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
                    raw_input = int(input_tokens or 0)
                    cached = min(int(cached_tokens or 0), raw_input)
                    output = int(output_tokens or 0)
                    usage["uncached_input_tokens"] = str(raw_input - cached)
                    usage["effective_tokens"] = str(raw_input - cached + output)
                    usage["total_tokens"] = str(raw_input + output)

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
    enforcement_budget = int(job.get("enforcement_budget") or role_limit(
        role, "ENFORCEMENT_BUDGET", ROLE_ENFORCEMENT_DEFAULTS
    ))
    enforcement_budget = min(enforcement_budget, token_budget)
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
            live_effective = int(live_usage.get("effective_tokens") or 0)
            # Codex currently reports authoritative usage at turn.completed. Never
            # kill a successfully completed turn after the work is already done.
            # If future Codex versions emit mid-turn usage, this remains a useful
            # live guard over non-cached input + output.
            if (
                live_effective >= enforcement_budget
                and live_usage.get("turn_completed") != "1"
                and process.poll() is None
            ):
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise RuntimeError(
                    f"Codex effective-token budget exceeded: {live_effective} >= {enforcement_budget} "
                    f"(configured ceiling {token_budget}; cached input excluded)"
                )

            time.sleep(POLL_SECONDS)

        if timed_out:
            raise RuntimeError(
                f"Codex exceeded {runtime_limit} second {role} timeout"
            )

    return process.returncode, time.time() - started


def repair_allowed_bash():
    """Shell commands a Claude builder/repair may run: focused validation and
    read-only git. Anything else is refused (print mode never prompts)."""
    return (
        f"Bash({SID_PYTHON} -m pytest *)",
        "Bash(node apps/web/app.test.js)",
        "Bash(git status)", "Bash(git status *)",
        "Bash(git diff)", "Bash(git diff *)",
        "Bash(git log *)", "Bash(git show *)",
    )


def run_review_aspects(job, worktree, log_path, timeout):
    """Run the configured specialist reviews concurrently and combine them
    into one Claude result at log_path (see agent_cli.combined_review_result).
    Returns an unavailable/failed run as-is so run_agent can fall back."""
    aspects = agent_cli.review_aspects()
    runs, errors = {}, []

    def review(aspect):
        model = agent_cli.aspect_model(aspect)
        prompt = (efficiency_prefix("reviewer") + job["prompt"] + "\n\n"
                  + agent_cli.REVIEW_ASPECT_FOCUS[aspect])
        run = agent_cli.run_claude(
            "reviewer", prompt, worktree, log_path.with_name(f"{log_path.stem}.{aspect}.json"),
            timeout, model=model, tick=lambda: heartbeat("working"))
        if run.result is not None:
            run.result["_model"] = model
        runs[aspect] = run

    threads = [threading.Thread(target=review, args=(a,), daemon=True) for a in aspects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for aspect in aspects:
        run = runs.get(aspect)
        if run is None or not run.ok:
            return run or agent_cli.ClaudeRun(1, 0.0, None)  # unavailable/failed: caller decides
    verdicts = {a: parse_review_verdict([runs[a].text])[0] for a in aspects}
    missing = [a for a, v in verdicts.items() if v == "unknown"]
    if missing:
        raise RuntimeError(f"specialist review(s) gave no verdict: {', '.join(missing)}")
    combined = agent_cli.combined_review_result(runs, verdicts)
    log_path.write_text(json.dumps(combined))
    return agent_cli.ClaudeRun(0, max(r.duration for r in runs.values()), combined)


# --- best-of-N builds -------------------------------------------------------------
#
# A build that already failed once is built again by the configured builder
# AND by the other provider in parallel, in separate worktrees. Each result
# goes through the test gate; the winner must pass, then the smaller change
# wins (ties: the configured builder). The winning change is applied to the
# job's normal worktree, so integration and review are unchanged.

BEST_OF_FROM_ATTEMPT = int(os.environ.get("BEST_OF_FROM_ATTEMPT", "2"))


def best_of_enabled(job):
    if BEST_OF_FROM_ATTEMPT <= 0 or job.get("role", "builder") != "builder":
        return False
    attempt = redis.hget(f"sid:jobs:{job['id']}", "build_attempt") or job.get("build_attempt") or "1"
    try:
        return int(attempt) >= BEST_OF_FROM_ATTEMPT
    except ValueError:
        return False


def change_size(worktree):
    """Lines added + removed, untracked files included (stages everything)."""
    run_git("add", "-A", cwd=worktree)
    total = 0
    for line in run_git("diff", "--cached", "--numstat", cwd=worktree).stdout.splitlines():
        added, removed = (line.split("\t") + ["0", "0"])[:2]
        total += (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
    return total


def run_alternate_builder(job, provider, worktree, log_path, timeout):
    """(ok, duration, cost_usd, model) for the alternate provider's build."""
    started = time.time()
    if provider == "claude":
        model = agent_cli.claude_model("builder")
        run = agent_cli.run_claude(
            "builder", efficiency_prefix("builder") + job["prompt"], worktree, log_path, timeout,
            model=model, allowed_bash=repair_allowed_bash(), tick=lambda: heartbeat("working"))
        cost = agent_cli.claude_usage(run.result)["cost_usd"] if run.result else ""
        return run.ok, run.duration, cost, model
    returncode, duration = run_codex({**job, "role": "builder"}, worktree, log_path)
    return returncode == 0, duration, "", job.get("model", DEFAULT_MODEL)


def run_best_of(job, worktree, log_path):
    """Build with both providers in parallel; keep the better passing change
    in `worktree`. Returns (returncode, duration) like run_agent."""
    job_id = str(job["id"])
    key = f"sid:jobs:{job_id}"
    primary_provider = agent_cli.role_provider("builder")
    alt_provider = "codex" if primary_provider == "claude" else "claude"
    timeout = int(job.get("timeout_seconds") or role_limit(
        "builder", "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS))
    slot = f"job:{job_id}:alt"
    if alt_provider == "claude" and (
        agent_cli.claude_cooling_down(redis)
        or not agent_cli.wait_for_claude_slot(redis, slot, timeout + 120, 60,
                                              tick=lambda: heartbeat("working"))
    ):
        redis.hset(key, "best_of", json.dumps({"skipped": "claude unavailable or at capacity"}))
        return run_agent(job, worktree, log_path)

    _, alt_worktree = create_worktree(job_id, "-alt")
    alt_log = log_path.with_name(f"{log_path.stem}.alt{log_path.suffix}")
    results = {}

    def primary():
        try:
            results["primary"] = run_agent(job, worktree, log_path)
        except Exception as exc:
            results["primary_error"] = str(exc)

    def alternate():
        try:
            results["alt"] = run_alternate_builder(job, alt_provider, alt_worktree, alt_log, timeout)
        except Exception as exc:
            results["alt_error"] = str(exc)

    try:
        threads = [threading.Thread(target=primary, daemon=True),
                   threading.Thread(target=alternate, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        if alt_provider == "claude":
            agent_cli.release_claude_slot(redis, slot)

    report = {"primary": {"provider": primary_provider}, "alt": {"provider": alt_provider}}
    candidates = {}
    if "primary" in results and results["primary"][0] == 0:
        candidates["primary"] = worktree
    else:
        report["primary"]["error"] = results.get("primary_error", "agent failed")
    alt = results.get("alt")
    if alt and alt[0]:
        candidates["alt"] = alt_worktree
        report["alt"].update(model=alt[3], cost_usd=alt[2])
    else:
        report["alt"]["error"] = results.get("alt_error", "agent failed")
    for name, path in candidates.items():
        tests_ok, _ = run_tests(path)
        report[name].update(tests=tests_ok, lines=change_size(path) if tests_ok else None)
    passing = [n for n in ("primary", "alt") if report[n].get("tests")]
    chosen = min(passing, key=lambda n: (report[n]["lines"] or 0, n != "primary")) if passing else "primary"
    report["chosen"] = chosen

    try:
        if chosen == "alt":
            patch = run_git("diff", "--cached", "--binary", cwd=alt_worktree).stdout
            run_git("reset", "--hard", "HEAD", cwd=worktree)
            run_git("clean", "-fd", cwd=worktree)
            applied = subprocess.run(["git", "apply", "--binary", "--index", "-"], cwd=worktree,
                                     input=patch, text=True, capture_output=True)
            if applied.returncode != 0:
                raise RuntimeError(f"could not apply the alternate build: {applied.stderr.strip()}")
            run_git("reset", "-q", cwd=worktree)  # leave changes unstaged, as a builder would
            redis.hset(key, mapping={"provider": alt_provider, "model": report["alt"].get("model", "")})
        elif chosen == "primary":
            run_git("reset", "-q", cwd=worktree)
    finally:
        redis.hset(key, mapping={"best_of": json.dumps(report), "updated_at": str(time.time())})
        run_git("worktree", "remove", "--force", str(alt_worktree), check=False)
        run_git("branch", "-D", f"sid/job-{job_id}-alt", check=False)
    print(f"[{WORKER_ID}] job={job_id} best-of: {json.dumps(report)}", flush=True)

    if chosen == "primary" and "primary" not in candidates:
        if "primary" in results:
            return results["primary"]
        raise RuntimeError(f"best-of build failed: {report}")
    return 0, max(results.get("primary", (0, 0))[1], (alt or (0, 0))[1])


def run_agent(job, worktree, log_path):
    """Run the provider configured for this job's role (ROLE_PROVIDERS).

    Claude falls back to Codex when it cannot serve the call (plan limit,
    auth, overload) and then cools down for CLAUDE_COOLDOWN_SECONDS so later
    jobs skip straight to Codex. Records the provider and model that ran.
    """
    role = job.get("role", "builder")
    key = f"sid:jobs:{job['id']}"
    provider = agent_cli.role_provider(role)
    fallback = ""
    if provider == "claude" and agent_cli.claude_cooling_down(redis):
        provider, fallback = "codex", "claude cooling down after an unavailable response"

    slot = f"job:{job['id']}"
    if provider == "claude":
        timeout = int(job.get("timeout_seconds") or role_limit(
            role, "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS))
        redis.hset(key, mapping={"provider_wait": "waiting for a claude slot",
                                 "updated_at": str(time.time())})
        if not agent_cli.wait_for_claude_slot(
                redis, slot, timeout + 120,
                int(os.environ.get("CLAUDE_SLOT_WAIT_SECONDS", "900")),
                tick=lambda: heartbeat("working")):
            provider, fallback = "codex", "claude at capacity (CLAUDE_MAX_CONCURRENT)"
        redis.hset(key, "provider_wait", "")

    if provider == "claude":
        try:
            model = agent_cli.claude_model(role)
            redis.hset(key, mapping={"provider": "claude", "model": model,
                                     "updated_at": str(time.time())})
            CURRENT_AGENT.update(provider="claude", model=model)
            heartbeat("working")
            if role == "reviewer" and len(agent_cli.review_aspects()) > 1:
                run = run_review_aspects(job, worktree, log_path, timeout)
            else:
                run = agent_cli.run_claude(
                    role, efficiency_prefix(role) + job["prompt"], worktree, log_path, timeout,
                    model=model,
                    allowed_bash=repair_allowed_bash() if role in ("builder", "repair") else (),
                    tick=lambda: heartbeat("working"),
                )
            if run.unavailable:
                fallback = f"claude unavailable: {run.describe_error()}"
                agent_cli.start_cooldown(redis, fallback)
                print(f"[{WORKER_ID}] job={job['id']} {fallback}; falling back to codex", flush=True)
            elif run.timed_out:
                raise RuntimeError(f"Claude exceeded {timeout} second {role} timeout")
            elif not run.ok:
                raise RuntimeError(f"Claude {role} failed: {run.describe_error()}")
            else:
                redis.hset(key, mapping={
                    "cost_usd": agent_cli.claude_usage(run.result)["cost_usd"],
                    "updated_at": str(time.time()),
                })
                return 0, run.duration
        finally:
            agent_cli.release_claude_slot(redis, slot)

    model = job.get("model", DEFAULT_MODEL)
    redis.hset(key, mapping={"provider": "codex", "model": model,
                             "provider_fallback": fallback,
                             "updated_at": str(time.time())})
    CURRENT_AGENT.update(provider="codex", model=model)
    heartbeat("working")
    return run_codex(job, worktree, log_path)


def run_tests(worktree):
    commands = [
        [
            SID_PYTHON,
            "-m",
            "pytest",
            "apps/api/tests",
            "-q",
        ],
        [
            SID_PYTHON,
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


def review_diff_packet(builder, candidate_commit, worktree):
    """Return the complete candidate change for review.

    The review range starts at the integration base, so a candidate built
    from several source commits (builder + repairs) is reviewed as a whole
    rather than as its last commit only. Files are included whole-file-first
    until the budget is spent, and any omitted file is named explicitly.
    """
    base = builder.get("integration_base_commit") or f"{candidate_commit}^"
    stat = run_git(
        "diff", "--stat=200", base, candidate_commit,
        cwd=worktree, check=False,
    ).stdout.strip() or "(no file changes reported)"
    names = [
        name for name in run_git(
            "diff", "--name-only", base, candidate_commit,
            cwd=worktree, check=False,
        ).stdout.splitlines() if name.strip()
    ]
    chunks, used, omitted = [], 0, []
    for name in names:
        piece = run_git(
            "diff", "--no-ext-diff", "--unified=20", base, candidate_commit,
            "--", name, cwd=worktree, check=False,
        ).stdout or ""
        if used + len(piece) > REVIEW_DIFF_CHARS:
            omitted.append(name)
            continue
        chunks.append(piece)
        used += len(piece)
    diff = "".join(chunks) or "(empty diff)"
    if omitted:
        diff += (
            "\n[SID: diff budget reached; these changed files were not "
            "inlined. Read them from the worktree if they matter: "
            + ", ".join(omitted) + "]"
        )
    return diff, stat, base


def integration_gate_summary(builder):
    raw = builder.get("integration_result", "")
    try:
        result = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        result = {}
    if not result:
        return "(no integration gate result recorded)"
    code = result.get("returncode")
    status = "PASSED" if code == 0 else f"FAILED (exit {code})"
    output = (result.get("stdout") or "")[-REVIEW_GATE_CHARS:]
    return f"Gate status: {status}\n{output}".rstrip()


def prior_findings_packet(builder):
    try:
        history = json.loads(builder.get("review_findings_history") or "[]")
    except (TypeError, ValueError):
        history = []
    if not isinstance(history, list) or not history:
        return ""
    parts = []
    for item in history[-2:]:
        if isinstance(item, dict):
            parts.append(
                f"Review {item.get('review_job_id', '?')} of "
                f"{str(item.get('candidate', ''))[:12]}:\n"
                f"{item.get('findings', '')}"
            )
    body = "\n\n".join(parts)[-REVIEW_PRIOR_CHARS:]
    return (
        "Previous review findings for this job (the repair was meant to "
        f"address these):\n{body}\n\n"
    )


def parse_review_verdict(messages):
    """Return (verdict, findings) from reviewer agent messages.

    PASS_WITH_NOTES is recorded as verdict "pass": notes never block and the
    approval contract (review_verdict == "pass") is unchanged.
    """
    verdict = "unknown"
    for text in messages:
        for message_line in str(text).splitlines():
            normalized = message_line.strip()
            if normalized in ("VERDICT: PASS", "VERDICT: PASS_WITH_NOTES"):
                verdict = "pass"
            elif normalized == "VERDICT: CHANGES_REQUIRED":
                verdict = "changes_required"
    findings = str(messages[-1]).strip()[-FINDINGS_CHARS:] if messages else ""
    return verdict, findings


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

    worktree = builder.get("integration_worktree")
    if not worktree:
        raise RuntimeError("Builder job has no integrated worktree")

    candidate_commit = builder.get("integrated_candidate_commit")
    if not candidate_commit:
        raise RuntimeError("Builder job has no integrated candidate commit")

    review_job_id = uuid.uuid4().hex[:8]

    # Atomic duplicate guard. Only the process that successfully creates
    # review_job_id is allowed to enqueue the reviewer.
    if not redis.hsetnx(builder_key, "review_job_id", review_job_id):
        return builder.get("review_job_id", ""), False

    candidate_diff, diff_stat, review_base = review_diff_packet(
        builder, candidate_commit, Path(worktree)
    )
    gate_summary = integration_gate_summary(builder)
    prior_findings = prior_findings_packet(builder)

    prompt = f"""You are the review agent for SID's AI Command Center.

Review builder job {builder_job_id}.
Review immutable integrated candidate commit {candidate_commit}.
The change under review is everything from {review_base} to {candidate_commit}.

Original task:
{builder.get("prompt", "")}

Files changed by this candidate (git diff --stat {review_base} {candidate_commit}):
{diff_stat}

SID candidate diff (the complete change under review; inspect this first):
{candidate_diff}

SID deterministic integration gate result (already executed by SID on this exact candidate):
{gate_summary}

{prior_findings}
You are operating inside the integrated candidate worktree.

Do NOT run tests. Your sandbox is read-only and has no writable temporary
directory, so pytest and similar tools will fail there. SID already ran the
deterministic gate on this exact candidate; its result is above.

Decide the verdict using this bar:

BLOCKING (verdict CHANGES_REQUIRED) only for defects in code this candidate
adds or changes:
- incorrect behavior or a crash on realistic input
- a stated requirement of the original task that is not met
- a regression of existing behavior
- a security or safety problem

NON-BLOCKING (report as notes, never a reason for CHANGES_REQUIRED):
- style, naming, refactoring, or maintainability preferences
- speculative or extreme edge cases (for example terminals under 20 columns)
- pre-existing issues in code this candidate did not change
- test coverage suggestions when the gate passed

If previous findings are listed above, first confirm whether each one is now
resolved. Do not invent new blocking findings in areas that previous reviews
already examined unless the latest change introduced them.

Read surrounding code only to validate a concrete concern. Do not modify any
files. Do not commit anything.

End your response with exactly one verdict line:
VERDICT: PASS
or
VERDICT: PASS_WITH_NOTES
or
VERDICT: CHANGES_REQUIRED

Before the verdict, list findings with each marked BLOCKING or NOTE. If there
are no material findings, explicitly say so.
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
        "integrated_candidate_commit": candidate_commit,
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
                "integrated_candidate_commit": candidate_commit,
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
    expected = integration_worktree(builder_job_id)

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

    candidate_commit = str(job.get("integrated_candidate_commit") or job.get("candidate_commit", ""))
    if not candidate_commit:
        raise RuntimeError("Reviewer job missing candidate_commit")
    if builder.get("integrated_candidate_commit") != candidate_commit:
        raise RuntimeError("Reviewer candidate does not match integrated candidate")

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
            "integrated_candidate_commit": candidate_commit,
            "log": str(log_path),
            "token_budget": str(job.get("token_budget") or role_limit("reviewer", "TOKEN_BUDGET", ROLE_TOKEN_DEFAULTS)),
            "enforcement_budget": str(job.get("enforcement_budget") or role_limit("reviewer", "ENFORCEMENT_BUDGET", ROLE_ENFORCEMENT_DEFAULTS)),
            "prompt_chars": str(len(job.get("prompt", ""))),
            "timeout_seconds": str(job.get("timeout_seconds") or role_limit("reviewer", "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS)),
            "updated_at": str(time.time()),
        },
    )

    returncode, duration = run_agent(job, worktree, log_path)
    session_id, usage = parse_codex_log(log_path)

    head_after = run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    dirty_after = run_git("status", "--porcelain", cwd=worktree).stdout.strip()
    if head_after != candidate_commit or dirty_after:
        raise RuntimeError("Candidate changed during read-only review")

    messages = agent_cli.agent_messages(log_path)

    verdict, findings = parse_review_verdict(messages)

    common = {
        "review_verdict": verdict,
        "review_findings": findings,
        "reviewed_commit": candidate_commit,
        "codex_exit_code": str(returncode),
        "duration_seconds": f"{duration:.2f}",
        "session_id": session_id,
        "tokens": usage["total_tokens"],
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "uncached_input_tokens": usage["uncached_input_tokens"],
        "effective_tokens": usage["effective_tokens"],
        "command_count": usage["command_count"],
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
    builder_update = {
        "review_job_id": job_id,
        "review_status": "complete",
        "review_verdict": verdict,
        "review_findings": findings,
        "reviewed_commit": candidate_commit,
        "review_log": str(log_path),
        "updated_at": str(time.time()),
    }
    if verdict == "changes_required":
        # Persist findings across repairs so the next independent reviewer
        # can verify them instead of starting over with new nitpicks.
        try:
            history = json.loads(builder.get("review_findings_history") or "[]")
        except (TypeError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        history.append({
            "review_job_id": job_id,
            "candidate": candidate_commit,
            "findings": findings[-3000:],
        })
        builder_update["review_findings_history"] = json.dumps(history[-5:])
    redis.hset(f"sid:jobs:{builder_job_id}", mapping=builder_update)



def restore_candidate(worktree, candidate):
    """Return a builder worktree to its committed candidate.

    A failed or interrupted repair leaves uncommitted edits (or, if the agent
    committed despite instructions, extra commits) in the builder worktree.
    Without this, every later repair refuses to start on the dirty tree and
    the remaining attempts are burnt without a real try. Only moves HEAD back
    along its own history; the committed candidate itself is never changed.
    """
    head = run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if head != candidate and run_git(
        "merge-base", "--is-ancestor", candidate, head, cwd=worktree, check=False
    ).returncode != 0:
        raise RuntimeError("Repair worktree HEAD does not match reviewed candidate")
    run_git("reset", "--hard", candidate, cwd=worktree)
    run_git("clean", "-fd", cwd=worktree)


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
    if head != candidate_before or run_git(
        "status", "--porcelain", cwd=worktree
    ).stdout.strip():
        # Leftovers of an earlier failed or interrupted repair.
        restore_candidate(worktree, candidate_before)

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
            "enforcement_budget": str(job.get("enforcement_budget") or role_limit("repair", "ENFORCEMENT_BUDGET", ROLE_ENFORCEMENT_DEFAULTS)),
            "prompt_chars": str(len(job.get("prompt", ""))),
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

    try:
        returncode, duration = run_agent(job, worktree, log_path)
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
            "uncached_input_tokens": usage["uncached_input_tokens"],
            "effective_tokens": usage["effective_tokens"],
            "command_count": usage["command_count"],
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
        redis.hset(key, mapping={"test_status": "passed" if tests_ok else "failed"})

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
            restore_candidate(worktree, candidate_before)
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
    except Exception:
        # Never leave a failed repair's edits for the next attempt.
        try:
            restore_candidate(worktree, candidate_before)
        except Exception as restore_exc:
            print(f"[{WORKER_ID}] repair={job_id} restore failed: {restore_exc}", flush=True)
        raise

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

    # A repaired candidate must be integrated and reviewed from scratch.
    # Keep the old integration worktree/branch metadata so prepare_integration
    # can safely clean them up before creating the new integrated candidate.
    redis.hdel(
        builder_key,
        "integration_status",
        "integration_base_commit",
        "integrated_candidate_commit",
        "integration_result",
        "integration_error",
    )

    raw_sources = builder.get("source_candidate_commits", "")
    if raw_sources:
        try:
            sources = json.loads(raw_sources)
        except (TypeError, ValueError):
            sources = []
    else:
        sources = []

    if not isinstance(sources, list):
        sources = []

    if not sources:
        original_candidate = builder.get("candidate_commit", "")
        if original_candidate:
            sources = [original_candidate]

    if not sources or sources[-1] != candidate_after:
        sources.append(candidate_after)

    redis.hset(
        builder_key,
        mapping={
            "candidate_commit": candidate_after,
            "source_candidate_commits": json.dumps(sources, separators=(",", ":")),
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

    if not prepare_integration(builder_job_id):
        print(
            f"[{WORKER_ID}] repair={job_id} integration failed",
            flush=True,
        )
        return

    review_job_id, queued = queue_review_job(builder_job_id)

    print(
        f"[{WORKER_ID}] repair={job_id} "
        f"builder={builder_job_id} "
        f"candidate={candidate_after[:12]} "
        f"review={review_job_id} "
        f"{'queued' if queued else 'already queued'}",
        flush=True,
    )

def process_integrate_job(job, key):
    """Operator-requested (re)integration of an existing builder candidate.

    Used for stale candidates (main moved after integration) and for
    re-reviewing jobs that exhausted repairs. The ordered source commits are
    preserved; only derived integration/review state was cleared by the
    host-side request. Main is never modified here.
    """
    builder_job_id = str(job.get("target_builder_id", ""))
    if not builder_job_id:
        raise RuntimeError("Integrate job missing target_builder_id")
    redis.hset(key, mapping={
        "status": "integrating",
        "worker_id": WORKER_ID,
        "job_role": "integrate",
        "target_builder_id": builder_job_id,
        "updated_at": str(time.time()),
    })
    builder = redis.hgetall(f"sid:jobs:{builder_job_id}")
    if builder.get("status") != "awaiting_review":
        raise RuntimeError(
            f"Integrate target status is {builder.get('status')!r}; expected 'awaiting_review'"
        )
    if not prepare_integration(builder_job_id):
        current = redis.hgetall(f"sid:jobs:{builder_job_id}")
        redis.hset(key, mapping={
            "status": "integration_failed",
            "error": current.get("integration_error", "integration did not pass or is already running"),
            "updated_at": str(time.time()),
        })
        return
    review_job_id, queued = queue_review_job(builder_job_id)
    redis.hset(key, mapping={
        "status": "integrate_complete",
        "review_job_id": review_job_id,
        "updated_at": str(time.time()),
    })
    print(
        f"[{WORKER_ID}] reintegrated builder={builder_job_id} "
        f"review={review_job_id} {'queued' if queued else 'already queued'}",
        flush=True,
    )


def process_job(raw_job):
    global CURRENT_JOB_ID, CURRENT_JOB_ROLE
    try:
        parsed = json.loads(raw_job)
        CURRENT_JOB_ID = str(parsed.get("id", ""))
        CURRENT_JOB_ROLE = str(parsed.get("role") or "builder")
    except (ValueError, AttributeError):
        CURRENT_JOB_ID = CURRENT_JOB_ROLE = ""
    try:
        # Publish the held job before touching its state, so the
        # orchestrator never sees it claimed by nobody.
        heartbeat("working")
        with keep_alive("working"):
            _process_job(raw_job)
    finally:
        CURRENT_JOB_ID = CURRENT_JOB_ROLE = ""
        CURRENT_AGENT.clear()


def _process_job(raw_job):
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

        if job.get("role") == "integrate":
            process_integrate_job(job, key)
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
                "enforcement_budget": str(job.get("enforcement_budget") or role_limit("builder", "ENFORCEMENT_BUDGET", ROLE_ENFORCEMENT_DEFAULTS)),
                "prompt_chars": str(len(job.get("prompt", ""))),
                "timeout_seconds": str(job.get("timeout_seconds") or role_limit("builder", "TIMEOUT_SECONDS", ROLE_RUNTIME_DEFAULTS)),
                "updated_at": str(time.time()),
            },
        )
        # Marks the job as retry-eligible for the orchestrator; a retry
        # dispatch has already set the next attempt number.
        redis.hsetnx(key, "build_attempt", "1")

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

        if best_of_enabled(job):
            returncode, duration = run_best_of(job, worktree, log_path)
        else:
            returncode, duration = run_agent(job, worktree, log_path)
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
                "uncached_input_tokens": usage["uncached_input_tokens"],
                "effective_tokens": usage["effective_tokens"],
                "command_count": usage["command_count"],
                "updated_at": str(time.time()),
            },
        )

        if returncode != 0:
            raise RuntimeError(f"Codex exited with status {returncode}")

        redis.hset(key, mapping={"status": "testing"})

        tests_ok, tests_output = run_tests(worktree)
        test_log.write_text(tests_output)
        redis.hset(key, mapping={"test_status": "passed" if tests_ok else "failed"})

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
                "files_changed": str(len([x for x in diff.splitlines() if x.strip()])),
                "candidate_commit": candidate_commit,
                "source_candidate_commits": json.dumps([candidate_commit], separators=(",", ":")),
                "updated_at": str(time.time()),
            },
        )

        if not prepare_integration(job_id):
            print(
                f"[{WORKER_ID}] job={job_id} integration FAILED",
                flush=True,
            )
            return

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
        global CURRENT_JOB_ID, CURRENT_JOB_ROLE
        CURRENT_JOB_ID = CURRENT_JOB_ROLE = ""
        CURRENT_AGENT.clear()
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

            # An operator stop takes effect between jobs: the current job
            # always finishes, and no new job is claimed while disabled.
            if worker_disabled():
                time.sleep(5)
                continue

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
