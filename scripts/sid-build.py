#!/usr/bin/env python3
"""Build one project and keep the result as a downloadable zip.

Started by the apps service as its own unit (sid-build-<project>-<id>):
    sid-build.py PROJECT BUILD_ID
1. checks out the project's main into <project>/builds/work-<id>
2. runs the build recipe (apps/api/project_catalog.resolve_recipe) in the
   stack's Docker image: only that checkout is mounted (at /src), plus a
   per-project package cache volume; memory, CPU and process limits; network
   for dependencies; no secrets, no other files of this server
3. zips the recipe's output into <project>/builds/<id>.zip, keeps the log in
   <project>/builds/<id>.log, keeps the newest KEEP builds, removes the
   checkout. Status in Redis sid:build:<project>:<id>.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import redis as redis_lib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "apps/api"))
import project_catalog  # noqa: E402
import sid_projects  # noqa: E402
import sid_redis  # noqa: E402

KEEP = int(os.getenv("BUILD_KEEP", "5"))
MEMORY = os.getenv("BUILD_MEMORY", "4g")
CPUS = os.getenv("BUILD_CPUS", "2")
SKIP_IN_ZIP = {".git", "node_modules", "__pycache__", ".venv"}


# Package downloads land in the project's cache volume so rebuilds are fast.
CACHE_ENV = {"HOME": "/root", "CI": "1", "npm_config_cache": "/root/.cache/npm", "PIP_CACHE_DIR": "/root/.cache/pip",
             "GRADLE_USER_HOME": "/root/.cache/gradle", "GOMODCACHE": "/root/.cache/go-mod",
             "GOCACHE": "/root/.cache/go-build", "CARGO_HOME": "/root/.cache/cargo",
             "PUB_CACHE": "/root/.cache/pub", "NUGET_PACKAGES": "/root/.cache/nuget",
             "PLATFORMIO_CORE_DIR": "/root/.cache/platformio"}


def cache_volume(project_id):
    return f"sid-build-cache-{project_id}"


def env_args():
    return [arg for key, value in CACHE_ENV.items() for arg in ("-e", f"{key}={value}")]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def zip_output(source, archive):
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        if source.is_file():
            out.write(source, source.name)
            return 1
        for current, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_IN_ZIP and not os.path.islink(os.path.join(current, d))]
            for name in files:
                if name == ".git":  # a checkout's pointer file, never part of a build
                    continue
                full = os.path.join(current, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                out.write(full, os.path.relpath(full, source))
                count += 1
    return count


def prune(r, project, folder):
    ids = r.lrange(f"sid:builds:{project.id}", 0, -1) or []
    for old in ids[KEEP:]:
        for suffix in (".zip", ".log"):
            target = folder / f"{old}{suffix}"
            if target.is_file() and target.parent == folder:
                target.unlink()
        r.delete(f"sid:build:{project.id}:{old}")
    r.ltrim(f"sid:builds:{project.id}", 0, KEEP - 1)


def connect():
    return redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=sid_redis.password(), decode_responses=True)


def main(project_id, build_id, r=None, runner=subprocess.run):
    r = r or connect()
    key = f"sid:build:{project_id}:{build_id}"
    status = lambda **fields: r.hset(key, mapping={k: str(v) for k, v in fields.items()})
    try:
        project = sid_projects.load(r, project_id)
    except LookupError:
        status(status="failed", error="project not found", finished_at=time.time())
        return 1
    fields = r.hgetall(f"sid:projects:{project_id}")
    detection = json.loads(r.get(f"sid:project-type:{project_id}") or "{}")
    recipe = project_catalog.resolve_recipe(fields, detection, project_id)
    if recipe.get("unsupported"):
        status(status="failed", error=recipe["unsupported"], finished_at=time.time())
        return 1
    folder = sid_projects.builds_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    work = folder / f"work-{build_id}"
    log_path = folder / f"{build_id}.log"
    head = git(project.repo, "rev-parse", f"refs/heads/{project.default_branch}").stdout.strip()
    status(status="running", started_at=time.time(), commit=head, recipe=recipe.get("label", ""),
           image=recipe["image"], command=recipe["command"])
    try:
        if not head:
            raise RuntimeError(f"the project has no {project.default_branch} branch yet; nothing to build")
        added = git(project.repo, "worktree", "add", "--detach", str(work), head)
        if added.returncode:
            raise RuntimeError(f"checkout failed: {added.stderr.strip()[:200]}")
        sid_projects.verify_worktree_pointer(work, project.repo)
        container = f"sid-build-{project_id}-{build_id}"
        command = ["docker", "run", "--rm", "--name", container, "-v", f"{work}:/src", "-w", "/src",
                   "-v", f"{cache_volume(project_id)}:/root/.cache", *env_args(),
                   "--memory", MEMORY, "--cpus", CPUS, "--pids-limit", "2048", recipe["image"], "sh", "-c", recipe["command"]]
        with log_path.open("w") as log:
            log.write(f"$ {recipe['command']}\n(in {recipe['image']}, main {head[:8]})\n\n")
            log.flush()
            result = runner(command, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise RuntimeError(f"the build command failed (exit {result.returncode}); see the log")
        output = (work / recipe.get("output", ".")).resolve()
        if work.resolve() not in (output, *output.parents) or not output.exists():
            raise RuntimeError(f"the build finished but {recipe.get('output')} was not created")
        archive = folder / f"{build_id}.zip"
        files = zip_output(output, archive)
        if not files:
            raise RuntimeError(f"{recipe.get('output')} is empty")
        status(status="succeeded", finished_at=time.time(), size=archive.stat().st_size, files=files, error="")
        sid_projects.record_event(r, project_id, "build", f"Build {build_id} succeeded ({files} files)", ref=build_id)
        return 0
    except Exception as exc:
        status(status="failed", finished_at=time.time(), error=str(exc)[:400])
        sid_projects.record_event(r, project_id, "build_failed", f"Build {build_id} failed: {exc}"[:200], ref=build_id)
        return 1
    finally:
        # The build could have rewritten anything in the checkout (even its
        # .git pointer), so it is deleted as plain files and git only prunes
        # its own record; no git command ever runs inside it.
        if work.parent == folder and (work.exists() or work.is_symlink()):
            if work.is_symlink():
                work.unlink()
            else:
                shutil.rmtree(work, ignore_errors=True)
        git(project.repo, "worktree", "prune")
        prune(r, project, folder)


if __name__ == "__main__":
    if len(sys.argv) != 3 or not sid_projects.PROJECT_ID.fullmatch(sys.argv[1]) or not sys.argv[2].isalnum():
        print("usage: sid-build.py PROJECT BUILD_ID", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
