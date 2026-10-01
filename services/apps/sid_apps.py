#!/usr/bin/env python3
"""Keep each project's app running from its latest main.

A project with a run command (set on its web page) gets:
  - a live checkout of its main branch at <project>/live (a git worktree),
  - its dependency setup run there after every change of main,
  - the app itself as the systemd unit sid-app-<id>, started through
    `systemd-run` inside the project sandbox (kind "app": host network so
    your PC can reach it, only the live checkout and <project>/data
    writable), with PORT set (8100-8199, assigned once per project) and
    HOST=0.0.0.0, restarted by systemd if it crashes,
  - a status record sid:app-status:<id> the dashboard shows (state, commit,
    port, last log lines).

Every loop the service reconciles: a new main commit, a changed command or
port, or a restart request (restart_at on the project) redeploys; a project
without a run command, archived or deleting has its app stopped. The SID
project itself is never run here.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from redis import Redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_sandbox  # noqa: E402
import sid_projects  # noqa: E402
import sid_redis  # noqa: E402

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
LOOP_SECONDS = float(os.getenv("APPS_LOOP_SECONDS", "10"))
SETUP_TIMEOUT = int(os.getenv("APPS_SETUP_TIMEOUT", "900"))
PORT_RANGE = range(int(os.getenv("APPS_PORT_MIN", "8100")), int(os.getenv("APPS_PORT_MAX", "8199")) + 1)
STATUS_PREFIX = "sid:app-status:"
# Project secrets written by the API (apps/api/env_routes.py), loaded by the
# app's unit with EnvironmentFile= (never on a command line).
ENV_DIR = Path(os.getenv("SID_PROJECT_ENV_DIR", "/etc/sid-ai/project-env"))
# Friendly addresses: <project>.<APP_DOMAIN> -> the app's port, served by the
# dashboard's nginx (host network) from NGINX_DIR/apps.conf.
APP_DOMAIN = os.getenv("SID_APP_DOMAIN", "sid.lan")
NGINX_DIR = Path(os.getenv("SID_NGINX_DIR", "/opt/sid-nginx"))
WEB_CONTAINER = os.getenv("SID_WEB_CONTAINER", "sid-ai-web")
DOCKER = os.getenv("DOCKER", "docker")
SYSTEMCTL = os.getenv("SYSTEMCTL", "systemctl")
SYSTEMD_RUN = os.getenv("SYSTEMD_RUN", "systemd-run")
JOURNALCTL = os.getenv("JOURNALCTL", "journalctl")

redis = None
stopping = False
PROJECT_CLI = Path(__file__).resolve().parents[2] / "scripts/sid-project.py"
PURGE_EVERY = float(os.getenv("APPS_PURGE_SECONDS", "600"))
last_purge = 0.0


def purge_trash(now=None):
    """Empty expired project trash (sid-project.py purge-trash) every
    PURGE_EVERY seconds; this service is the host's always-on loop."""
    global last_purge
    now = time.time() if now is None else now
    if now - last_purge < PURGE_EVERY:
        return
    last_purge = now
    result = run(["/usr/bin/python3", str(PROJECT_CLI), "purge-trash"], timeout=600)
    if result.returncode:
        print(f"[sid-apps] purge-trash failed: {result.stderr.strip()[-300:]}", flush=True)
    elif '"removed": []' not in result.stdout:
        print(f"[sid-apps] {result.stdout.strip()}", flush=True)


def unit_name(project_id):
    return f"sid-app-{project_id}"


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, **kwargs)


def git(args, cwd):
    result = run(["git", *args], cwd=str(cwd))
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def unit_state(project_id):
    """systemd's ActiveState for the app unit ("inactive" when absent)."""
    out = run([SYSTEMCTL, "show", unit_name(project_id), "-p", "ActiveState", "--value"]).stdout.strip()
    return out or "inactive"


def stop_unit(project_id):
    run([SYSTEMCTL, "stop", unit_name(project_id)])
    run([SYSTEMCTL, "reset-failed", unit_name(project_id)])


def log_tail(project_id, lines=30):
    out = run([JOURNALCTL, "-u", unit_name(project_id), "-n", str(lines), "--no-pager", "-o", "cat"]).stdout
    return out[-4000:]


def set_status(project_id, **fields):
    fields["updated_at"] = str(time.time())
    redis.hset(STATUS_PREFIX + project_id, mapping={k: str(v) for k, v in fields.items()})


def assign_port(project):
    """The project's port, assigning the lowest free one on first use."""
    if project.run_port in PORT_RANGE:
        return project.run_port
    taken = set()
    for other in redis.smembers("sid:projects") or []:
        try:
            taken.add(int(redis.hget(f"sid:projects:{other}", "run_port") or 0))
        except ValueError:
            pass
    for port in PORT_RANGE:
        if port not in taken:
            redis.hset(f"sid:projects:{project.id}", "run_port", str(port))
            project.run_port = port
            return port
    raise RuntimeError("no free app port left in 8100-8199")


def live_checkout(project, commit):
    """<project>/live at commit (a detached worktree of the project repo)."""
    live = Path(project.root) / "live"
    if not os.path.lexists(live / ".git"):
        if live.exists():
            raise RuntimeError(f"{live} exists but is not a worktree; remove it by hand")
        git(["worktree", "prune"], project.repo)
        git(["worktree", "add", "--detach", str(live), commit], project.repo)
    sid_projects.verify_worktree_pointer(live, project.repo)
    git(["checkout", "--detach", "--force", commit], live)
    return live


def app_env(live, port):
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "LANG": os.environ.get("LANG", "C.UTF-8"), "PORT": str(port), "HOST": "0.0.0.0",
           "NODE_ENV": "production"}
    local = [str(live / "node_modules/.bin"), str(live / ".venv/bin")]
    env["PATH"] = os.pathsep.join([p for p in local if Path(p).is_dir()] + [env["PATH"]])
    return env


def setup(project, live):
    command = project.setup_command or sid_projects.detect_setup(live)
    if not command:
        return True, ""
    argv = project_sandbox.command(["/bin/sh", "-c", command], project, live, kind="setup")
    try:
        result = run(argv, cwd=str(live), timeout=SETUP_TIMEOUT, env={**os.environ, **app_env(live, 0)})
    except subprocess.TimeoutExpired:
        return False, f"setup timed out after {SETUP_TIMEOUT}s"
    return result.returncode == 0, (result.stdout + result.stderr)[-3000:]


def start_unit(project, live, port):
    env = app_env(live, port)
    inner = project_sandbox.command(["/bin/sh", "-c", project.run_command], project, live, kind="app")
    # No --collect: a unit that gave up must stay "failed" (with its Result)
    # until stop_unit's reset-failed, or the crash would vanish and the app
    # would be redeployed in a loop instead of being reported.
    args = [SYSTEMD_RUN, f"--unit={unit_name(project.id)}", "--quiet",
            # A web app should keep running: restart it whenever it exits,
            # but give up (state "failed" -> dashboard "crashed") after 5
            # quick exits instead of looping forever.
            "--property=Restart=always", "--property=RestartSec=5",
            "--property=StartLimitIntervalSec=120", "--property=StartLimitBurst=5",
            # Resource limits, so one app cannot starve SID or the others.
            f"--property=MemoryMax={project.run_memory_mb}M", "--property=MemorySwapMax=0",
            f"--property=CPUQuota={int(project.run_cpus * 100)}%", f"--property=TasksMax={project.run_tasks}",
            f"--property=WorkingDirectory={live}",
            f"--property=EnvironmentFile=-{ENV_DIR / (project.id + '.env')}",
            f"--description=SID app {project.id}"]
    args += [f"--setenv={k}={v}" for k, v in env.items()]
    result = run([*args, "--", *inner])
    if result.returncode:
        raise RuntimeError(f"systemd-run failed: {(result.stderr or result.stdout).strip()}")


def limits_of(project):
    return f"{project.run_memory_mb}M/{project.run_cpus:g}cpu/{project.run_tasks}tasks"


def deploy(project, head, port):
    set_status(project.id, state="deploying", commit=head, port=port, command=project.run_command, error="",
               limits=limits_of(project))
    stop_unit(project.id)
    live = live_checkout(project, head)
    ok, output = setup(project, live)
    if not ok:
        set_status(project.id, state="setup_failed", error="dependency setup failed", log=output)
        return
    start_unit(project, live, port)
    set_status(project.id, state="running", deployed_at=time.time(), log="")


def reconcile(project):
    status = redis.hgetall(STATUS_PREFIX + project.id) or {}
    wanted = bool(project.run_command) and project.status == "active"
    if not wanted:
        if unit_state(project.id) in ("active", "activating", "reloading", "failed"):
            stop_unit(project.id)
        if status.get("state") not in (None, "stopped"):
            set_status(project.id, state="stopped", error="")
        return
    port = assign_port(project)
    head = git(["rev-parse", f"refs/heads/{project.default_branch}"], project.repo)
    try:
        restart_at = float(redis.hget(f"sid:projects:{project.id}", "restart_at") or 0)
        deployed_at = float(status.get("deployed_at") or 0)
    except ValueError:
        restart_at = deployed_at = 0
    changed = (status.get("commit") != head or status.get("command") != project.run_command
               or status.get("port") != str(port) or restart_at > deployed_at
               or status.get("limits") != limits_of(project))  # also gives older apps their limits
    if status.get("state") == "setup_failed" and not changed:
        return  # wait for a new commit, a new command or a restart request
    if changed or status.get("state") in (None, "", "stopped", "deploying"):
        deploy(project, head, port)
        return
    state = unit_state(project.id)
    if state == "active":
        if status.get("state") != "running":
            set_status(project.id, state="running", error="")
    elif state == "failed":
        result = run([SYSTEMCTL, "show", unit_name(project.id), "-p", "Result", "--value"]).stdout.strip()
        reason = (f"it used more than its {project.run_memory_mb} MB memory limit"
                  if result == "oom-kill" else "the app keeps exiting; see the log")
        set_status(project.id, state="crashed", error=reason, log=log_tail(project.id))
    elif state == "inactive" and status.get("state") in ("running", "crashed"):
        # Gone (host reboot, manual stop): start it again from the same commit.
        deploy(project, head, port)


def nginx_config(routes, domain=APP_DOMAIN):
    """Server blocks for {project_id: port}, sorted, deterministic."""
    blocks = []
    for project_id, port in sorted(routes.items()):
        blocks.append(f"""server {{
    listen 80;
    listen 8080;
    server_name {project_id}.{domain};
    client_max_body_size 100m;
    location / {{
        proxy_pass http://127.0.0.1:{int(port)};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }}
}}
""")
    return "# Written by services/apps/sid_apps.py: friendly app addresses.\n" + "".join(blocks)


def publish_routes(routes):
    """Write the nginx config when it changed and reload nginx; a config
    nginx rejects is rolled back (the dashboard keeps working)."""
    target = NGINX_DIR / "apps.conf"
    text = nginx_config(routes)
    try:
        current = target.read_text()
    except OSError:
        current = None
    if current == text:
        return False
    NGINX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, target)
    check = run([DOCKER, "exec", WEB_CONTAINER, "nginx", "-t"])
    if check.returncode != 0:
        if current is None:
            target.unlink()
        else:
            target.write_text(current)
        print(f"[sid-apps] nginx rejected the app addresses, kept the old ones: {check.stderr.strip()[-300:]}", flush=True)
        return False
    run([DOCKER, "exec", WEB_CONTAINER, "nginx", "-s", "reload"])
    return True


def cleanup_removed(known_ids):
    """Stop apps whose project no longer exists (deleted)."""
    for key in redis.scan_iter(STATUS_PREFIX + "*"):
        project_id = key[len(STATUS_PREFIX):]
        if project_id not in known_ids:
            stop_unit(project_id)
            redis.delete(key)


def loop_once():
    projects = [p for p in sid_projects.all_projects(redis) if not p.is_sid]
    known = set(redis.smembers("sid:projects") or [])
    for project in projects:
        try:
            reconcile(project)
        except Exception as exc:
            set_status(project.id, state="error", error=str(exc)[:500])
    routes = {}
    for project in projects:
        status = redis.hgetall(STATUS_PREFIX + project.id) or {}
        if project.run_command and status.get("port", "").isdigit():
            routes[project.id] = status["port"]
    try:
        publish_routes(routes)
    except Exception as exc:
        print(f"[sid-apps] could not publish app addresses: {exc}", flush=True)
    # Registered but not active (archived, deleting): stop their apps too.
    for project_id in known - {p.id for p in projects} - {sid_projects.SID_PROJECT}:
        if unit_state(project_id) != "inactive":
            stop_unit(project_id)
            set_status(project_id, state="stopped", error="")
    cleanup_removed(known)


def main():
    global redis
    redis = Redis.from_url(REDIS_URL, password=sid_redis.password(), decode_responses=True)

    def stop(*_):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"[sid-apps] running; ports {PORT_RANGE.start}-{PORT_RANGE.stop - 1}", flush=True)
    while not stopping:
        try:
            loop_once()
            purge_trash()
            redis.set("sid:apps-service", json.dumps({"updated_at": time.time()}), ex=60)
        except Exception as exc:
            print(f"[sid-apps] loop error: {exc}", flush=True)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
