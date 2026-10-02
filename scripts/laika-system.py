#!/usr/bin/env python3
"""Apply Settings that change the host (run by the operator service as its
own unit, so long waits never block it).

    laika-system.py apply      worker count, timers, then restart services
    laika-system.py workers    start workers 1..WORKER_COUNT, stop the rest
    laika-system.py timers     backup schedule (BACKUP_TIME)
    laika-system.py restart    restart LAIka's services safely (laika-restart.sh)

Workers above the new count are paused, allowed to finish their job, then
stopped and disabled. Settings come from Redis via services/laika_env.py.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment)

SYSTEMCTL = os.environ.get("SYSTEMCTL", "systemctl")
UNIT_DIR = Path(os.environ.get("LAIKA_UNIT_DIR", "/etc/systemd/system"))
MAX_WORKERS = 32
WAIT_SECONDS = int(os.environ.get("LAIKA_APPLY_WAIT", "1800"))


def run(*argv, check=False):
    return subprocess.run(list(argv), capture_output=True, text=True, check=check)


def unit(n):
    return f"laika-worker@{n:02d}.service"


def redis_client():
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def workers(r=None, sleep=time.sleep, clock=time.time):
    count = int(os.environ.get("WORKER_COUNT", "8"))
    r = r or redis_client()
    for n in range(1, MAX_WORKERS + 1):
        name = unit(n)
        active = run(SYSTEMCTL, "is-active", name).stdout.strip() in ("active", "activating")
        if n <= count:
            if not active:
                run(SYSTEMCTL, "enable", "--now", name)
                print(f"started {name}")
            continue
        if not active:
            run(SYSTEMCTL, "disable", name)
            continue
        worker_id = f"laika-worker-{n:02d}"
        r.set(f"laika:worker-control:{worker_id}", "disabled")  # claims nothing new
        deadline = clock() + WAIT_SECONDS
        while r.hget(f"laika:workers:{worker_id}", "status") == "working" and clock() < deadline:
            sleep(5)
        run(SYSTEMCTL, "disable", "--now", name)
        r.delete(f"laika:worker-control:{worker_id}")
        print(f"stopped {name}")


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
    if action == "workers":
        workers()
    elif action == "timers":
        timers()
    elif action == "restart":
        return restart()
    elif action == "apply":
        timers()
        workers()
        return restart()
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
