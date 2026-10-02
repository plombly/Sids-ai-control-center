#!/usr/bin/env python3
"""Apply Settings that change the host (run by the operator service as its
own unit, so long waits never block it).

    laika-system.py apply      timers, then restart services
    laika-system.py timers     backup schedule (BACKUP_TIME)
    laika-system.py restart    restart LAIka's services safely (laika-restart.sh)

How many workers run is not set here any more: the scaler
(services/scaler/laika_scaler.py) reads the worker settings every 15 s and
starts or drains workers itself. Settings come from Redis via
services/laika_env.py.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment)

SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")
UNIT_DIR = Path(os.environ.get("LAIKA_UNIT_DIR", "/etc/systemd/system"))
WAIT_SECONDS = int(os.environ.get("LAIKA_APPLY_WAIT", "1800"))


def run(*argv, check=False):
    return subprocess.run(list(argv), capture_output=True, text=True, check=check)


def unit(n):
    return f"laika-worker@{n:02d}.service"


def timers():
    clock = os.environ.get("BACKUP_TIME", "03:30")
    folder = UNIT_DIR / "laika-backup.timer.d"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schedule.conf").write_text(f"# Settings → Backups (laika-system.py)\n[Timer]\nOnCalendar=\nOnCalendar=*-*-* {clock}:00\n")
    run(SYSTEMCTL, "daemon-reload")
    run(SYSTEMCTL, "restart", "laika-backup.timer")
    print(f"backup timer: daily at {clock}")


def restart():
    result = run(str(ROOT / "scripts/laika-restart.sh"), "--wait", str(WAIT_SECONDS), "all")
    print(result.stdout[-2000:], result.stderr[-2000:])
    return result.returncode


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    if action == "timers":
        timers()
    elif action == "restart":
        return restart()
    elif action == "apply":
        timers()
        return restart()
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
