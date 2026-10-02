#!/usr/bin/env python3
"""Build one project and keep the result as a downloadable zip.

Started by the apps service as its own unit (laika-build-<project>-<id>):
    laika-build.py PROJECT BUILD_ID
1. exports the project's main (git archive: tracked files, no .git) into
   <project>/builds/work-<id>; a git worktree's .git pointer names a host
   path that does not exist in the container, which broke every git (and
   Flutter) command inside builds
2. runs the build recipe (apps/api/project_catalog.resolve_recipe) in the
   stack's Docker image: only that checkout is mounted (at /src), plus a
   per-project package cache volume; memory, CPU and process limits; no
   secrets, no other files of this server. Network (project build_network):
   "internet" (default) = the laika-build-net Docker network, whose firewall
   (ensure_build_network) lets builds reach the internet but nothing on
   this server or the local network; "none" = no network at all
3. zips the recipe's output into <project>/builds/<id>.zip, keeps the log in
   <project>/builds/<id>.log, keeps the newest KEEP builds, removes the
   checkout. Status in Redis laika:build:<project>:<id>.
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
import laika_env  # noqa: E402,F401  (Settings → environment, before any configuration is read)
sys.path.insert(0, str(ROOT / "apps/api"))
import project_catalog  # noqa: E402
import laika_projects  # noqa: E402
import laika_redis  # noqa: E402

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


BUILD_NET = "laika-build-net"
BRIDGE = "laika-build0"
IPTABLES = os.getenv("LAIKA_IPTABLES", "iptables")
# Never reachable from a build: private networks (your LAN, VPN, Docker's
# own networks with LAIka's containers), link-local and multicast.
PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "224.0.0.0/4")


def firewall_rules():
    """(chain, rule) pairs, checked before every build. Forwarded traffic
    from the build bridge may not go to private addresses (DNS excepted, in
    case the resolver is on the LAN); traffic to this server itself (INPUT)
    is dropped entirely: no dashboard, API, Redis or apps."""
    forward = [["-p", "udp", "--dport", "53", "-j", "RETURN"], ["-p", "tcp", "--dport", "53", "-j", "RETURN"]]
    forward += [["-d", net, "-j", "DROP"] for net in PRIVATE_NETS]
    return [("LAIKA-BUILD-FWD", rule) for rule in forward] + [("LAIKA-BUILD-IN", ["-j", "DROP"])]


def ensure_build_network(run=subprocess.run):
    """Create the build network and its firewall if missing (both vanish
    with a reboot or a Docker reset). Raises if either cannot be set up:
    a build never runs with open access to the local network."""
    def call(*argv):
        return run(list(argv), capture_output=True, text=True)

    if call("docker", "network", "inspect", BUILD_NET).returncode:
        made = call("docker", "network", "create", "--driver", "bridge", "--label", "laika=build",
                    "-o", f"com.docker.network.bridge.name={BRIDGE}", BUILD_NET)
        if made.returncode:
            raise RuntimeError(f"could not create the build network: {(made.stderr or '').strip()[:200]}")
    # Rules are only ever added (never flushed), so a build starting while
    # another one runs never sees the firewall half-built.
    for chain in ("LAIKA-BUILD-FWD", "LAIKA-BUILD-IN"):
        call(IPTABLES, "-N", chain)  # fails harmlessly when it exists
    for chain, rule in firewall_rules():
        if call(IPTABLES, "-C", chain, *rule).returncode and call(IPTABLES, "-A", chain, *rule).returncode:
            raise RuntimeError(f"could not set up the build firewall ({chain} {' '.join(rule)})")
    for parent, chain in (("DOCKER-USER", "LAIKA-BUILD-FWD"), ("INPUT", "LAIKA-BUILD-IN")):
        if call(IPTABLES, "-C", parent, "-i", BRIDGE, "-j", chain).returncode:
            if call(IPTABLES, "-I", parent, "1", "-i", BRIDGE, "-j", chain).returncode:
                raise RuntimeError(f"could not set up the build firewall ({parent})")


def network_args(mode):
    return ["--network", "none"] if mode == "none" else ["--network", BUILD_NET]


def cache_volume(project_id):
    return f"laika-build-cache-{project_id}"


def env_args():
    return [arg for key, value in CACHE_ENV.items() for arg in ("-e", f"{key}={value}")]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def export_tree(repo, commit, dest):
    """Tracked files of commit, without .git, into dest (a new directory)."""
    dest.mkdir()
    archive = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", commit], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "--no-same-owner", "-C", str(dest)], stdin=archive.stdout, capture_output=True)
    archive.stdout.close()
    if archive.wait() != 0 or extract.returncode != 0:
        raise RuntimeError(f"checkout failed: {extract.stderr.decode(errors='replace').strip()[:200]}")


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
    ids = r.lrange(f"laika:builds:{project.id}", 0, -1) or []
    for old in ids[KEEP:]:
        for suffix in (".zip", ".log"):
            target = folder / f"{old}{suffix}"
            if target.is_file() and target.parent == folder:
                target.unlink()
        r.delete(f"laika:build:{project.id}:{old}")
    r.ltrim(f"laika:builds:{project.id}", 0, KEEP - 1)


def connect():
    return redis_lib.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def main(project_id, build_id, r=None, runner=subprocess.run, network_setup=None):
    r = r or connect()
    key = f"laika:build:{project_id}:{build_id}"
    status = lambda **fields: r.hset(key, mapping={k: str(v) for k, v in fields.items()})
    try:
        project = laika_projects.load(r, project_id)
    except LookupError:
        status(status="failed", error="project not found", finished_at=time.time())
        return 1
    fields = r.hgetall(f"laika:projects:{project_id}")
    detection = json.loads(r.get(f"laika:project-type:{project_id}") or "{}")
    recipe = project_catalog.resolve_recipe(fields, detection, project_id)
    if recipe.get("unsupported"):
        status(status="failed", error=recipe["unsupported"], finished_at=time.time())
        return 1
    folder = laika_projects.builds_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    work = folder / f"work-{build_id}"
    log_path = folder / f"{build_id}.log"
    head = git(project.repo, "rev-parse", f"refs/heads/{project.default_branch}").stdout.strip()
    status(status="running", started_at=time.time(), commit=head, recipe=recipe.get("label", ""),
           image=recipe["image"], command=recipe["command"])
    try:
        if not head:
            raise RuntimeError(f"the project has no {project.default_branch} branch yet; nothing to build")
        export_tree(project.repo, head, work)
        mode = "none" if fields.get("build_network") == "none" else "internet"
        if mode == "internet":
            (network_setup or ensure_build_network)()
        container = f"laika-build-{project_id}-{build_id}"
        command = ["docker", "run", "--rm", "--name", container, "-v", f"{work}:/src", "-w", "/src",
                   "-v", f"{cache_volume(project_id)}:/root/.cache", *env_args(),
                   "--memory", MEMORY, "--cpus", CPUS, "--pids-limit", "2048", *network_args(mode),
                   recipe["image"], "sh", "-c", recipe["command"]]
        with log_path.open("w") as log:
            access = "internet, not this server or your network" if mode == "internet" else "no network"
            log.write(f"$ {recipe['command']}\n(in {recipe['image']}, main {head[:8]}, {access})\n\n")
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
        laika_projects.record_event(r, project_id, "build", f"Build {build_id} succeeded ({files} files)", ref=build_id)
        return 0
    except Exception as exc:
        status(status="failed", finished_at=time.time(), error=str(exc)[:400])
        laika_projects.record_event(r, project_id, "build_failed", f"Build {build_id} failed: {exc}"[:200], ref=build_id)
        return 1
    finally:
        # The build could have rewritten anything in its copy, so it is
        # deleted as plain files; no git command ever runs inside it.
        if work.parent == folder and (work.exists() or work.is_symlink()):
            if work.is_symlink():
                work.unlink()
            else:
                shutil.rmtree(work, ignore_errors=True)
        git(project.repo, "worktree", "prune")
        prune(r, project, folder)


if __name__ == "__main__":
    if len(sys.argv) != 3 or not laika_projects.PROJECT_ID.fullmatch(sys.argv[1]) or not sys.argv[2].isalnum():
        print("usage: laika-build.py PROJECT BUILD_ID", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
