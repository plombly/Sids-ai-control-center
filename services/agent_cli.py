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


def claude_model(role):
    return os.getenv(f"CLAUDE_{role.upper()}_MODEL") or DEFAULT_CLAUDE_MODELS[role]


def claude_budget(role):
    return float(os.getenv(f"CLAUDE_{role.upper()}_BUDGET_USD") or DEFAULT_CLAUDE_BUDGET_USD[role])


def claude_command(role, model, budget_usd, allowed_bash=()):
    command = [
        "claude", "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--strict-mcp-config",  # never load the operator's MCP connectors
        "--model", model,
        "--max-budget-usd", f"{budget_usd:.2f}",
        "--append-system-prompt", AGENT_CONTRACT,
    ]
    if role in ("planner", "reviewer"):
        command += ["--tools", READ_ONLY_TOOLS]
    else:
        command += ["--tools", EDIT_TOOLS, "--permission-mode", "acceptEdits"]
        if allowed_bash:
            # Variadic flag: keep it last. The prompt goes in on stdin.
            command += ["--allowedTools", *allowed_bash]
    return command


class ClaudeRun:
    def __init__(self, returncode, duration, result, timed_out=False):
        self.returncode = returncode
        self.duration = duration
        self.result = result  # parsed JSON result, or None
        self.timed_out = timed_out

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
        if self.result is None or not self.result.get("is_error"):
            return False
        status = self.result.get("api_error_status")
        return status in (401, 403, 429, 529) or bool(_UNAVAILABLE.search(self.text))

    def describe_error(self):
        if self.timed_out:
            return "timed out"
        if self.result is None:
            return f"exited with status {self.returncode} without a JSON result"
        return (
            f"{self.result.get('subtype') or 'error'}: "
            f"{self.text[:500] or 'no message'}"
        )


def run_claude(role, prompt, cwd, log_path, timeout, model=None, budget_usd=None,
               allowed_bash=(), tick=None):
    """Run `claude -p` for one role. The JSON result is written to log_path."""
    command = claude_command(
        role, model or claude_model(role),
        claude_budget(role) if budget_usd is None else budget_usd,
        allowed_bash,
    )
    env = {**os.environ, "HOME": os.environ.get("HOME") or "/root"}
    started = time.time()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=log,
            stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True,
        )
        process.stdin.write(prompt)
        process.stdin.close()
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
    return ClaudeRun(process.returncode, time.time() - started,
                     read_claude_result(log_path), timed_out)


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


def claude_cooling_down(redis_client):
    try:
        return bool(redis_client.get(COOLDOWN_KEY))
    except Exception:
        return False


def start_cooldown(redis_client, reason):
    """Stop sending work to Claude for a while after it reported a limit, so
    every job does not first burn a failed call before falling back."""
    try:
        redis_client.set(COOLDOWN_KEY, reason[:300] or "unavailable", ex=COOLDOWN_SECONDS)
    except Exception:
        pass
