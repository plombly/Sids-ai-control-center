#!/usr/bin/env python3

import json
import subprocess
import sys
import time
from pathlib import Path


REPO = Path("/opt/sids-ai-command-center")
PYTHON = Path("/tmp/sid-agent-venv/bin/python")
RESULT_DIR = Path("/var/log/sid-ai/integration")


CHECKS = [
    (
        "api-tests",
        [str(PYTHON), "-m", "pytest", "apps/api/tests", "-q"],
    ),
    (
        "python-compile",
        [str(PYTHON), "-m", "compileall", "-q", "apps", "services", "scripts"],
    ),
    (
        "tui-syntax",
        [str(PYTHON), "-m", "py_compile", "apps/tui/sid-tui.py"],
    ),
    (
        "worker-syntax",
        [str(PYTHON), "-m", "py_compile", "services/worker/worker.py"],
    ),
    (
        "submitter-syntax",
        [str(PYTHON), "-m", "py_compile", "scripts/submit-job.py"],
    ),
    (
        "review-syntax",
        [str(PYTHON), "-m", "py_compile", "scripts/job-review.py"],
    ),
    (
        "git-diff-check",
        ["git", "diff", "--check"],
    ),
]


def run_check(name, command):
    started = time.time()

    result = subprocess.run(
        command,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=300,
    )

    return {
        "name": name,
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "duration_seconds": round(time.time() - started, 2),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def main():
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        text=True,
    ).strip()

    branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        cwd=REPO,
        text=True,
    ).strip()

    started = time.time()
    results = []

    print(f"SID integration check: {branch} @ {commit[:12]}")
    print()

    for name, command in CHECKS:
        print(f"[ RUN  ] {name}")
        result = run_check(name, command)
        results.append(result)

        if result["passed"]:
            print(f"[ PASS ] {name} ({result['duration_seconds']}s)")
        else:
            print(f"[ FAIL ] {name} ({result['duration_seconds']}s)")
            if result["stdout"].strip():
                print(result["stdout"].rstrip())
            if result["stderr"].strip():
                print(result["stderr"].rstrip())

    passed = all(result["passed"] for result in results)

    report = {
        "commit": commit,
        "branch": branch,
        "status": "passed" if passed else "failed",
        "started_at": started,
        "duration_seconds": round(time.time() - started, 2),
        "checks": results,
    }

    report_path = RESULT_DIR / f"{commit}.json"
    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print(f"INTEGRATION: {'PASSED' if passed else 'FAILED'}")
    print(f"Report: {report_path}")

    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
