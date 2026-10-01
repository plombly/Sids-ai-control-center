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
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from redis import Redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_detect  # noqa: E402
import project_history  # noqa: E402
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


def start_unit(project, live, port, unit=None, data_dir=None, description=None):
    env = app_env(live, port)
    inner = project_sandbox.command(["/bin/sh", "-c", project.run_command], project, live, kind="app",
                                    data_dir=data_dir)
    # No --collect: a unit that gave up must stay "failed" (with its Result)
    # until stop_unit's reset-failed, or the crash would vanish and the app
    # would be redeployed in a loop instead of being reported.
    args = [SYSTEMD_RUN, f"--unit={unit or unit_name(project.id)}", "--quiet",
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
            f"--description={description or f'SID app {project.id}'}"]
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
        sid_projects.record_event(redis, project.id, "app_problem", "App could not install its dependencies", ref=head[:12])
        return
    start_unit(project, live, port)
    set_status(project.id, state="running", deployed_at=time.time(), log="")
    sid_projects.record_event(redis, project.id, "deployed", f"App deployed from main {head[:8]}", ref=head[:12])


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
        if status.get("state") != "crashed":
            sid_projects.record_event(redis, project.id, "app_problem", f"App crashed: {reason}")
        set_status(project.id, state="crashed", error=reason, log=log_tail(project.id))
    elif state == "inactive" and status.get("state") in ("running", "crashed"):
        # Gone (host reboot, manual stop): start it again from the same commit.
        deploy(project, head, port)


# --- previews: a change's app, before it is approved -----------------------------------
#
# The dashboard asks for one (sid:preview:<job> state "requested"); this runs
# the job's exact integrated candidate from <project>/previews/job-<id> as
# unit sid-preview-<job>, with its own empty data folder, the app's limits and
# secrets, on a port from PREVIEW_PORTS. It is torn down when the job is
# approved, rejected or re-integrated, when asked, or after PREVIEW_HOURS.

PREVIEW_PREFIX = "sid:preview:"
PREVIEW_PORTS = range(int(os.getenv("PREVIEW_PORT_MIN", "8200")), int(os.getenv("PREVIEW_PORT_MAX", "8299")) + 1)
PREVIEW_HOURS = float(os.getenv("PREVIEW_HOURS", "4"))


def preview_unit(job_id):
    return f"sid-preview-{job_id}"


def preview_paths(project, job_id):
    base = Path(project.root) / "previews"
    return base / f"job-{job_id}", base / f"job-{job_id}-data"


def unit_state_of(unit):
    return run([SYSTEMCTL, "show", unit, "-p", "ActiveState", "--value"]).stdout.strip() or "inactive"


def teardown_preview(job_id, project):
    for verb in ("stop", "reset-failed"):
        run([SYSTEMCTL, verb, preview_unit(job_id)])
    if project is None:
        return
    workdir, data = preview_paths(project, job_id)
    if os.path.lexists(workdir / ".git"):
        try:
            sid_projects.verify_worktree_pointer(workdir, project.repo)
            git(["worktree", "remove", "--force", str(workdir)], project.repo)
        except RuntimeError as exc:
            print(f"[sid-apps] preview {job_id}: {exc}", flush=True)
    if workdir.is_dir() and not workdir.is_symlink():
        shutil.rmtree(workdir, ignore_errors=True)
    if data.is_dir() and not data.is_symlink():
        shutil.rmtree(data, ignore_errors=True)
    run(["git", "-C", str(project.repo), "worktree", "prune"])


def set_preview(job_id, **fields):
    fields["updated_at"] = str(time.time())
    redis.hset(PREVIEW_PREFIX + job_id, mapping={k: str(v) for k, v in fields.items()})


def preview_port(job_id):
    taken = set()
    for key in redis.scan_iter(PREVIEW_PREFIX + "*"):
        if key != PREVIEW_PREFIX + job_id:
            port = redis.hget(key, "port")
            if port and port.isdigit():
                taken.add(int(port))
    for port in PREVIEW_PORTS:
        if port not in taken:
            return port
    raise RuntimeError("no free preview port left")


def start_preview(job_id, project, candidate):
    set_preview(job_id, state="starting", error="")
    workdir, data = preview_paths(project, job_id)
    teardown_preview(job_id, project)  # a leftover from an earlier try
    workdir.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", str(workdir), candidate], project.repo)
    sid_projects.verify_worktree_pointer(workdir, project.repo)
    ok, output = setup(project, workdir)
    if not ok:
        set_preview(job_id, state="setup_failed", error="dependency setup failed", log=output)
        return
    port = preview_port(job_id)
    data.mkdir(parents=True, exist_ok=True)  # empty: never the app's real data
    start_unit(project, workdir, port, unit=preview_unit(job_id), data_dir=data,
               description=f"SID preview {project.id} job {job_id}")
    set_preview(job_id, state="running", port=port, started_at=time.time())
    sid_projects.record_event(redis, project.id, "preview", f"Preview started for job {job_id}", ref=job_id)


def reconcile_previews(projects_by_id, now=None):
    now = time.time() if now is None else now
    for key in list(redis.scan_iter(PREVIEW_PREFIX + "*")):
        job_id = key[len(PREVIEW_PREFIX):]
        info = redis.hgetall(key) or {}
        job = redis.hgetall(f"sid:jobs:{job_id}") or {}
        project = projects_by_id.get(info.get("project_id"))
        try:
            requested = float(info.get("requested_at") or 0)
        except ValueError:
            requested = 0
        stale = (info.get("state") == "stop" or project is None or not project.run_command
                 or job.get("status") != "awaiting_review"
                 or job.get("integrated_candidate_commit") != info.get("candidate")
                 or now - requested > PREVIEW_HOURS * 3600)
        try:
            if stale:
                teardown_preview(job_id, project)
                redis.delete(key)
            elif info.get("state") == "requested":
                start_preview(job_id, project, info["candidate"])
            elif info.get("state") == "running":
                state = unit_state_of(preview_unit(job_id))
                if state == "failed":
                    result = run([SYSTEMCTL, "show", preview_unit(job_id), "-p", "Result", "--value"]).stdout.strip()
                    reason = (f"it used more than its {project.run_memory_mb} MB memory limit"
                              if result == "oom-kill" else "the preview keeps exiting; see the log")
                    set_preview(job_id, state="crashed", error=reason,
                                log=run([JOURNALCTL, "-u", preview_unit(job_id), "-n", "30", "--no-pager", "-o", "cat"]).stdout[-4000:])
        except Exception as exc:
            set_preview(job_id, state="error", error=str(exc)[:500])


def cleanup_removed(known_ids):
    """Stop apps whose project no longer exists (deleted)."""
    for key in redis.scan_iter(STATUS_PREFIX + "*"):
        project_id = key[len(STATUS_PREFIX):]
        if project_id not in known_ids:
            stop_unit(project_id)
            redis.delete(key)


def merged_jobs(project_id):
    """Merged builder jobs of a project (for its history)."""
    found = []
    for key in redis.scan_iter("sid:jobs:*"):
        if key.count(":") != 2 or redis.type(key) != "hash":
            continue
        job = redis.hgetall(key)
        if job.get("status") == "merged" and job.get("merge_commit") and (job.get("project_id") or "sid") == project_id:
            found.append(job)
    return found


def publish_histories(projects):
    for project in projects:
        try:
            project_history.publish(redis, project, lambda: merged_jobs(project.id), project.default_branch)
        except Exception as exc:
            print(f"[sid-apps] history of {project.id}: {exc}", flush=True)


TYPE_PREFIX = "sid:project-type:"
BUILD_REQUEST_PREFIX = "sid:build-request:"
BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/sid-build.py"
BUILD_MAX_SECONDS = int(os.getenv("BUILD_MAX_SECONDS", "2700"))
UNKNOWN_RECHECK = 3600  # an undetected project is looked at again hourly


def publish_types(projects):
    """Re-detect each project's type and stack when main moved, when the
    dashboard asked for a recheck (stored result removed), and hourly while
    nothing was recognised."""
    for project in projects:
        key = TYPE_PREFIX + project.id
        try:
            current = project_history.head(project.repo, project.default_branch)
        except RuntimeError:
            current = ""
        try:
            stored = json.loads(redis.get(key) or "{}")
        except ValueError:
            stored = {}
        fresh = stored.get("type") != "unknown" or time.time() - float(stored.get("checked_at") or 0) < UNKNOWN_RECHECK
        if stored.get("head") == current and current and fresh:
            continue
        try:
            result = project_detect.detect_repo(project.repo, f"refs/heads/{project.default_branch}")
        except Exception as exc:
            print(f"[sid-apps] detect {project.id}: {exc}", flush=True)
            continue
        result.update(head=current, checked_at=time.time())
        redis.set(key, json.dumps(result))


def unit_is_running(unit):
    return unit_state_of(unit) in ("active", "activating")


def launch_builds(projects):
    """Start requested builds (one at a time per project) as their own units:
    scripts/sid-build.py does the work and reports in sid:build:<id>:<build>."""
    for project in projects:
        raw = redis.get(BUILD_REQUEST_PREFIX + project.id)
        if not raw:
            continue
        running = [b for b in (redis.lrange(f"sid:builds:{project.id}", 0, 4) or [])
                   if redis.hget(f"sid:build:{project.id}:{b}", "status") in ("queued", "running")]
        if any(unit_is_running(f"sid-build-{project.id}-{b}") for b in running):
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            redis.delete(BUILD_REQUEST_PREFIX + project.id)
            continue
        build_id = request.get("build_id") or time.strftime("%Y%m%d%H%M%S")
        redis.delete(BUILD_REQUEST_PREFIX + project.id)
        redis.hset(f"sid:build:{project.id}:{build_id}", mapping={"id": build_id, "status": "queued",
                                                                   "requested_at": str(time.time())})
        redis.lpush(f"sid:builds:{project.id}", build_id)
        result = run([SYSTEMD_RUN, f"--unit=sid-build-{project.id}-{build_id}", "--quiet", "--collect",
                      f"--property=RuntimeMaxSec={BUILD_MAX_SECONDS}", f"--description=SID build {project.id} {build_id}",
                      "/opt/sid-venv/bin/python", str(BUILD_SCRIPT), project.id, build_id])
        if result.returncode:
            redis.hset(f"sid:build:{project.id}:{build_id}", mapping={"status": "failed",
                       "error": f"could not start the build: {(result.stderr or result.stdout).strip()[:300]}"})


def loop_once():
    publish_histories(sid_projects.all_projects(redis))  # SID too (read-only history)
    publish_types(sid_projects.all_projects(redis))
    launch_builds([p for p in sid_projects.all_projects(redis) if not p.is_sid])
    projects = [p for p in sid_projects.all_projects(redis) if not p.is_sid]
    known = set(redis.smembers("sid:projects") or [])
    for project in projects:
        try:
            reconcile(project)
        except Exception as exc:
            set_status(project.id, state="error", error=str(exc)[:500])
    reconcile_previews({p.id: p for p in projects})
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
