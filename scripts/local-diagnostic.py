#!/usr/bin/env python3

"""Check the local SID runtime without changing any service state."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO = Path(os.getenv("REPO_ROOT", Path(__file__).resolve().parent.parent))
REDIS_HOST = "127.0.0.1"
REDIS_PORT = "6379"
API_HEALTH_URL = os.getenv("SID_API_HEALTH_URL", "http://127.0.0.1:8000/health")
ORCHESTRATOR_SERVICE = os.getenv("SID_ORCHESTRATOR_SERVICE", "sid-orchestrator.service")
WORKER_SERVICE = os.getenv("SID_WORKER_SERVICE", "sid-worker.service")


def command_check(label, command, expected_output=None):
    try:
        result = subprocess.run(
            command,
            cwd=REPO,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{label}: {exc}"

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = (result.stderr or output or "command failed").strip()
        return False, f"{label}: {detail}"
    if expected_output is not None and output != expected_output:
        return False, f"{label}: expected {expected_output!r}, got {output or 'no output'!r}"
    return True, label


def check_redis():
    return command_check(
        "Redis responded to PING",
        ["redis-cli", "-h", REDIS_HOST, "-p", REDIS_PORT, "ping"],
        "PONG",
    )


def check_service(label, service):
    return command_check(
        f"{label} is active ({service})",
        ["systemctl", "is-active", service],
    )


def check_repository():
    passed, detail = command_check(
        "repository status is clean",
        ["git", "status", "--porcelain", "--untracked-files=all"],
        "",
    )
    if not passed:
        return passed, detail
    return True, detail


def check_api():
    try:
        with urlopen(API_HEALTH_URL, timeout=10) as response:
            payload = json.load(response)
    except (OSError, ValueError, URLError) as exc:
        return False, f"API health endpoint failed ({API_HEALTH_URL}): {exc}"

    if payload.get("status") != "healthy":
        return False, f"API health status is {payload.get('status', 'missing')!r}; expected 'healthy'"
    return True, f"API health endpoint is healthy ({API_HEALTH_URL})"


def main():
    checks = [
        ("redis", check_redis),
        ("orchestrator", lambda: check_service("orchestrator", ORCHESTRATOR_SERVICE)),
        ("worker", lambda: check_service("worker", WORKER_SERVICE)),
        ("repository", check_repository),
        ("api", check_api),
    ]
    failed = False
    for _, check in checks:
        try:
            passed, detail = check()
        except Exception as exc:  # Keep every diagnostic result actionable.
            passed, detail = False, f"unexpected diagnostic error: {exc}"
        result = "PASS" if passed else "FAIL"
        print(f"[{result}] {detail}")
        failed = failed or not passed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
