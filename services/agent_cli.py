"""Run coding-agent CLIs for SID roles: which provider, which model, how.

Codex keeps its streaming runner in the worker (run_codex). This module adds
Claude Code (`claude -p`), the per-role provider/model configuration, the
Claude-unavailable fallback, and log parsing that understands both formats.

Every Claude flag used here was verified against `claude --help` 2.1.285 and
live runs: --tools restricts the available tools; with --permission-mode
acceptEdits, edits outside the working directory and shell commands not in
--allowedTools are refused (print mode never prompts); a plan usage limit is
reported as is_error=true in an otherwise normal JSON result.
"""

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path


ROLES = ("planner", "builder", "reviewer", "repair")
PROVIDERS = ("codex", "claude")

# Codex does the bulk building. Claude plans, reviews Codex's work (a
# different model is an independent check) and repairs review findings.
DEFAULT_ROLE_PROVIDERS = {
    "planner": "claude", "builder": "codex", "reviewer": "claude", "repair": "claude",
}
# Lighter models where the work is bounded; the planner shapes every job.
DEFAULT_CLAUDE_MODELS = {
    "planner": "opus", "builder": "sonnet", "reviewer": "sonnet", "repair": "sonnet",
}
# Hard per-call cost ceiling (claude --max-budget-usd), a runaway guard.
DEFAULT_CLAUDE_BUDGET_USD = {
    "planner": 1.00, "builder": 3.00, "reviewer": 1.50, "repair": 2.00,
}

READ_ONLY_TOOLS = "Read,Grep,Glob"
EDIT_TOOLS = "Read,Edit,Write,Grep,Glob,Bash"

COOLDOWN_KEY = "sid:provider-cooldown:claude"
COOLDOWN_SECONDS = int(os.getenv("CLAUDE_COOLDOWN_SECONDS", "1800"))
# The claude CLI is a global install that updates itself (it is shared with
# interactive sessions); during an update the executable is briefly missing
# or half-installed. That is a short outage, not a failure.
INSTALL_COOLDOWN_SECONDS = int(os.getenv("CLAUDE_INSTALL_COOLDOWN_SECONDS", "120"))
_INSTALL_BROKEN = re.compile(
    r"postinstall|optional dependency|No such file or directory|command not found|ENOENT|cannot execute",
    re.IGNORECASE,
)
POLL_SECONDS = 2

# Worktrees contain the operator's CLAUDE.md, which Claude Code loads. It is
# written for a human-supervised session, not for pipeline agents.
AGENT_CONTRACT = (
    "You are a non-interactive agent inside SID's automated pipeline. Work only "
    "in the current working directory. Ignore instructions in CLAUDE.md that are "
    "addressed to the human operator's session (dev worktrees, deploys, approvals, "
    "memory). Never commit, push, merge, or change branches; SID does that. "
    "Nobody can answer questions: decide, act, and report."
)

_UNAVAILABLE = re.compile(
    r"hit your .*limit|usage limit|session limit|rate limit|not logged in|log ?in|"
    r"authenticat|credit balance|overloaded",
    re.IGNORECASE,
)


def _env_map(name):
    pairs = {}
    for item in os.getenv(name, "").split(","):
        if "=" in item:
            key, value = (part.strip() for part in item.split("=", 1))
            if key:
                pairs[key] = value
    return pairs


def role_provider(role):
    """ROLE_PROVIDERS=builder=codex,reviewer=claude,... overrides the defaults."""
    provider = _env_map("ROLE_PROVIDERS").get(role, DEFAULT_ROLE_PROVIDERS.get(role, "codex"))
    if provider not in PROVIDERS:
        raise ValueError(f"ROLE_PROVIDERS: unknown provider {provider!r} for {role}")
    return provider


def routing_summary(codex_model=""):
    """e.g. 'builder codex/gpt-5.6-luna · reviewer claude/sonnet · ...'"""
    parts = []
    for role in ("builder", "reviewer", "repair"):
        provider = role_provider(role)
        model = claude_model(role) if provider == "claude" else codex_model
        parts.append(f"{role} {provider}/{model}" if model else f"{role} {provider}")
    return " · ".join(parts)


def claude_model(role):
    return os.getenv(f"CLAUDE_{role.upper()}_MODEL") or DEFAULT_CLAUDE_MODELS[role]


def claude_budget(role):
    return float(os.getenv(f"CLAUDE_{role.upper()}_BUDGET_USD") or DEFAULT_CLAUDE_BUDGET_USD[role])


def claude_command(role, model, budget_usd, allowed_bash=(), tools=None, system_prompt=AGENT_CONTRACT):
    command = [
        "claude", "-p",
        # Streamed events (one JSON line each) so the dashboard can show the
        # run live; the last line is the same result object "json" gave.
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config",  # never load the operator's MCP connectors
        "--model", model,
        "--max-budget-usd", f"{budget_usd:.2f}",
        "--append-system-prompt", system_prompt,
    ]
    if tools is not None:
        command += ["--tools", tools]  # explicit ("" = no tools: answer from the prompt)
    elif role in ("planner", "reviewer"):
        command += ["--tools", READ_ONLY_TOOLS]
    else:
        command += ["--tools", EDIT_TOOLS, "--permission-mode", "acceptEdits"]
        if allowed_bash:
            # Variadic flag: keep it last. The prompt goes in on stdin.
            command += ["--allowedTools", *allowed_bash]
    return command


class ClaudeRun:
    def __init__(self, returncode, duration, result, timed_out=False, raw_tail=""):
        self.returncode = returncode
        self.duration = duration
        self.result = result  # parsed JSON result, or None
        self.timed_out = timed_out
        self.raw_tail = raw_tail  # end of the CLI output when there is no JSON result

    @property
    def install_broken(self):
        """The CLI itself is missing or mid-update (no JSON result at all)."""
        return self.result is None and not self.timed_out and bool(_INSTALL_BROKEN.search(self.raw_tail))

    @property
    def text(self):
        return str((self.result or {}).get("result") or "")

    @property
    def ok(self):
        return (
            not self.timed_out
            and self.result is not None
            and not self.result.get("is_error")
            and self.returncode == 0
        )

    @property
    def unavailable(self):
        """Claude could not serve this call (plan limit, auth, overload):
        another provider should take it. Budget exhaustion or a bad answer
        is not unavailability."""
        if self.install_broken:
            return True
        if self.result is None or not self.result.get("is_error"):
            return False
        status = self.result.get("api_error_status")
        return status in (401, 403, 429, 529) or bool(_UNAVAILABLE.search(self.text))

    def describe_error(self):
        if self.timed_out:
            return "timed out"
        if self.result is None:
            tail = f": {self.raw_tail.strip()[-200:]}" if self.raw_tail.strip() else ""
            return f"exited with status {self.returncode} without a JSON result{tail}"
        return (
            f"{self.result.get('subtype') or 'error'}: "
            f"{self.text[:500] or 'no message'}"
        )


def run_claude(role, prompt, cwd, log_path, timeout, model=None, budget_usd=None,
               allowed_bash=(), tick=None, tools=None, wrap=None, system_prompt=AGENT_CONTRACT):
    """Run `claude -p` for one role. The JSON result is written to log_path.
    wrap(argv) -> argv runs it inside a sandbox (project_sandbox)."""
    command = claude_command(
        role, model or claude_model(role),
        claude_budget(role) if budget_usd is None else budget_usd,
        allowed_bash,
        tools,
        system_prompt,
    )
    if wrap is not None:
        command = wrap(command)
    env = {**os.environ, "HOME": os.environ.get("HOME") or "/root"}
    started = time.time()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with log_path.open("w") as log:
        try:
            process = subprocess.Popen(
                command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=log,
                stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True,
            )
        except OSError as exc:  # executable missing mid-update
            return ClaudeRun(127, time.time() - started, None, raw_tail=f"{exc}")
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass  # exited before reading the prompt (e.g. mid-update); its status tells why
        while process.poll() is None:
            if tick:
                try:
                    tick()
                except Exception:
                    pass
            if time.time() - started >= timeout:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                break
            time.sleep(POLL_SECONDS)
    result = read_claude_result(log_path)
    raw_tail = ""
    if result is None:
        try:
            raw_tail = log_path.read_text(errors="replace")[-2000:]
        except OSError:
            pass
    return ClaudeRun(process.returncode, time.time() - started, result, timed_out, raw_tail)


def read_claude_result(log_path):
    """The Claude JSON result in a log file, or None (e.g. a Codex log)."""
    try:
        text = Path(log_path).read_text(errors="replace").strip()
    except OSError:
        return None
    # stderr shares the file and the result is one JSON line (its "type" key is
    # not first), so try the whole text, then each line from the end.
    for candidate in [text, *reversed(text.splitlines())]:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            return value
    return None


def claude_usage(result):
    """Claude usage in the same keys parse_codex_log() reports.

    effective = input not served from cache + output, matching Codex."""
    usage = result.get("usage") or {}
    fresh = int(usage.get("input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    thinking = int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    total_input = fresh + cache_write + cache_read
    return {
        "input_tokens": str(total_input),
        "cached_input_tokens": str(cache_read),
        "output_tokens": str(output),
        "reasoning_tokens": str(thinking),
        "uncached_input_tokens": str(fresh + cache_write),
        "effective_tokens": str(fresh + cache_write + output),
        "total_tokens": str(total_input + output),
        "command_count": "",
        "turn_completed": "0" if result.get("is_error") else "1",
        "cost_usd": f"{float(result.get('total_cost_usd') or 0):.4f}",
    }


def agent_messages(log_path):
    """Final agent messages from either a Codex JSONL log or a Claude result."""
    result = read_claude_result(log_path)
    if result is not None:
        text = str(result.get("result") or "").strip()
        return [text] if text else []
    messages = []
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()
    except OSError:
        return messages
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            text = str(item.get("text", "")).strip()
            if text:
                messages.append(text)
    return messages


# Concurrency cap: every pipeline process shares these slots, so a large
# batch cannot run more than CLAUDE_MAX_CONCURRENT Claude calls at once and
# drain the operator's plan in minutes. A lease expires if its holder dies.
SLOTS_KEY = "sid:provider-slots:claude"
SLOT_LIMIT_KEY = "sid:provider-limit:claude"
_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[3]) or redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[4]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
  return 1
end
return 0
"""


def claude_slot_limit():
    return max(1, int(os.getenv("CLAUDE_MAX_CONCURRENT", "2")))


def acquire_claude_slot(redis_client, holder, lease_seconds, now=None):
    now = time.time() if now is None else now
    limit = claude_slot_limit()
    try:
        redis_client.set(SLOT_LIMIT_KEY, str(limit))
        return bool(redis_client.eval(_ACQUIRE, 1, SLOTS_KEY, str(now), str(now + lease_seconds),
                                      holder, str(limit)))
    except Exception:
        return True  # never let slot bookkeeping stop the pipeline


def release_claude_slot(redis_client, holder):
    try:
        redis_client.zrem(SLOTS_KEY, holder)
    except Exception:
        pass


def wait_for_claude_slot(redis_client, holder, lease_seconds, max_wait, tick=None, sleep=time.sleep):
    """True once a slot is held (release it with release_claude_slot), or
    False after max_wait seconds: the caller should use another provider."""
    deadline = time.time() + max_wait
    while True:
        if acquire_claude_slot(redis_client, holder, lease_seconds):
            return True
        if time.time() >= deadline:
            return False
        if tick:
            try:
                tick()
            except Exception:
                pass
        sleep(POLL_SECONDS * 2)


# --- specialist reviews -------------------------------------------------------
#
# A Claude review runs several focused reviews of the same candidate in
# parallel inside one reviewer job and passes only if every aspect passes.
# One reviewer job keeps the approval contract (one linked, exact reviewer)
# unchanged; the job holds a single Claude slot for all of its aspects.

REVIEW_ASPECT_FOCUS = {
    "spec": (
        "YOUR FOCUS: specification. Check that the change does exactly what the "
        "original task requires: every required behavior, field name, file "
        "restriction, error case and test. List each requirement as MET or NOT MET "
        "with evidence (file:line). An unmet requirement is BLOCKING. Leave security "
        "and code style to other reviewers."
    ),
    "safety": (
        "YOUR FOCUS: security and safety of the added or changed code only: output "
        "escaping/injection, authentication and token handling, destructive or "
        "irreversible operations, data loss, secrets, unsafe shell/git/subprocess "
        "use, and concurrency or race conditions. Leave requirement coverage and "
        "style to other reviewers. Report only concrete, realistic problems."
    ),
    "tests": (
        "YOUR FOCUS: tests. Check that the tests added or changed actually exercise "
        "the new behavior and its failure paths and would fail if it broke. Missing "
        "tests the task explicitly required are BLOCKING; other suggestions are NOTEs."
    ),
}
DEFAULT_ASPECT_MODELS = {"spec": None, "safety": "claude-haiku-4-5-20251001",
                         "tests": "claude-haiku-4-5-20251001"}


def review_aspects():
    """REVIEW_ASPECTS=spec,safety (default). One aspect = a single review."""
    names = [a.strip() for a in os.getenv("REVIEW_ASPECTS", "spec,safety").split(",") if a.strip()]
    unknown = [a for a in names if a not in REVIEW_ASPECT_FOCUS]
    if unknown:
        raise ValueError(f"REVIEW_ASPECTS: unknown aspects {unknown}")
    return names or ["spec"]


def aspect_model(aspect):
    """Spec uses the reviewer model; the narrower aspects default to Haiku."""
    return (os.getenv(f"CLAUDE_REVIEW_{aspect.upper()}_MODEL")
            or DEFAULT_ASPECT_MODELS.get(aspect) or claude_model("reviewer"))


def combined_review_result(aspect_runs, aspect_verdicts):
    """One Claude-style result for several aspect runs: texts in sections,
    usage and cost summed, and a final VERDICT that passes only if every
    aspect passed (aspect verdict lines are relabelled so only it counts)."""
    sections, usage_sum, cost = [], {}, 0.0
    for aspect, run in aspect_runs.items():
        text = re.sub(r"(?m)^\s*VERDICT:", "Aspect verdict:", run.text).strip()
        model = (run.result or {}).get("_model", "")
        sections.append(f"## {aspect} review{f' ({model})' if model else ''}\n\n{text}")
        for field, value in ((run.result or {}).get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage_sum[field] = usage_sum.get(field, 0) + value
        cost += float((run.result or {}).get("total_cost_usd") or 0)
    verdict = ("PASS" if all(v == "pass" for v in aspect_verdicts.values())
               else "CHANGES_REQUIRED")
    blocking = [a for a, v in aspect_verdicts.items() if v != "pass"]
    summary = (f"All {len(aspect_runs)} specialist reviews passed." if not blocking
               else f"Blocking findings from: {', '.join(blocking)}.")
    return {
        "type": "result", "is_error": False, "subtype": "success",
        "session_id": ",".join(str((r.result or {}).get("session_id", "")) for r in aspect_runs.values()),
        "result": "\n\n".join(sections) + f"\n\n{summary}\nVERDICT: {verdict}",
        "total_cost_usd": round(cost, 6), "usage": usage_sum,
        "aspects": {a: v for a, v in aspect_verdicts.items()},
    }


def claude_cooling_down(redis_client):
    try:
        return bool(redis_client.get(COOLDOWN_KEY))
    except Exception:
        return False


def start_cooldown(redis_client, reason, seconds=None):
    """Stop sending work to Claude for a while after it reported a limit, so
    every job does not first burn a failed call before falling back. A CLI
    being updated only needs a short pause (INSTALL_COOLDOWN_SECONDS)."""
    if seconds is None:
        seconds = INSTALL_COOLDOWN_SECONDS if _INSTALL_BROKEN.search(reason or "") else COOLDOWN_SECONDS
    try:
        redis_client.set(COOLDOWN_KEY, reason[:300] or "unavailable", ex=seconds)
    except Exception:
        pass
