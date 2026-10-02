#!/usr/bin/env python3
"""LAIka updates: check for a new release and install it (root).

    laika-update.py check          is a newer, correctly signed release out?
    laika-update.py apply [--yes]  install it (the dashboard's "Update now"
                                   and `sudo laika update` run this)

Releases come from UPDATE_URL (Settings, else deploy/release.env): a
folder with manifest.json, manifest.json.sig and the tarball it names. The
manifest must be signed with the release key (deploy/release-key.pub,
verified with `ssh-keygen -Y verify`) and the tarball must match its
SHA-256; anything else is refused before anything changes.

Installing:
  1. backup (scripts/laika-backup.py); refuses on failure
  2. pauses the scaler and every worker, waits until no job runs
  3. unpacks the release next to /opt/laika, swaps it in (the old version
     stays as /opt/laika.previous), runs its install.sh --repair
  4. restarts LAIka's services, resumes the workers
  5. runs laika doctor; if the containers, services or API are broken it
     swaps the previous version back, repairs and restarts again
Progress: laika:update:status (JSON). A server running LAIka from a git
checkout is updated with git instead (refused here).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401  (Settings → environment)

HOME = Path(os.environ.get("LAIKA_HOME", "/opt/laika"))
PREVIOUS = HOME.with_name(HOME.name + ".previous")
STATUS_KEY = "laika:update:status"
AVAILABLE_KEY = "laika:update:available"
NAMESPACE = "laika-release"
WAIT_SECONDS = int(os.environ.get("LAIKA_UPDATE_WAIT", "1800"))
CRITICAL = ("containers", "services", "api", "dashboard")


def read_env_file(path):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def update_url():
    url = os.environ.get("UPDATE_URL") or read_env_file(ROOT / "deploy/release.env").get("LAIKA_UPDATE_URL", "")
    return url.rstrip("/") + "/" if url else ""


def version_key(text):
    """Comparable version: 1.0.0 > 1.0.0-rc1 > 1.0.0-dev; unparsable is lowest."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?", (text or "").strip())
    if not match:
        return (-1,)
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), 1 if pre is None else 0, pre or "")


def current_version(home=HOME):
    try:
        return (home / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def fetch(url, limit=200 * 1024 * 1024):
    request = urllib.request.Request(url, headers={"User-Agent": "laika-update"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than {limit} bytes")
    return data


def verify(manifest_bytes, signature_bytes, key_file=ROOT / "deploy/release-key.pub", runner=subprocess.run):
    """True if the manifest is signed by the release key."""
    with tempfile.TemporaryDirectory() as folder:
        sig = Path(folder) / "manifest.json.sig"
        sig.write_bytes(signature_bytes)
        result = runner(["ssh-keygen", "-Y", "verify", "-f", str(key_file), "-I", NAMESPACE, "-n", NAMESPACE,
                         "-s", str(sig)], input=manifest_bytes, capture_output=True)
    return result.returncode == 0


def latest(base, getter=fetch, verifier=verify):
    """The signed manifest at base, or raise."""
    manifest_bytes = getter(base + "manifest.json")
    if not verifier(manifest_bytes, getter(base + "manifest.json.sig")):
        raise ValueError("the release manifest is not signed by the LAIka release key")
    manifest = json.loads(manifest_bytes)
    if not re.fullmatch(r"laika-[0-9A-Za-z.-]+\.tar\.gz", manifest.get("tarball", "")):
        raise ValueError("the manifest names an unexpected file")
    return manifest


def redis_client():
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def status(r, state, message="", **extra):
    print(f"[laika-update] {state}: {message}", flush=True)
    try:
        r.set(STATUS_KEY, json.dumps({"state": state, "message": message, "at": time.time(), **extra}), ex=7 * 86400)
    except Exception:
        pass


def check(r=None, getter=fetch, verifier=verify):
    base = update_url()
    if not base:
        return {"configured": False}
    manifest = latest(base, getter, verifier)
    current = current_version()
    newer = version_key(manifest["version"]) > version_key(current)
    info = {"configured": True, "current": current, "latest": manifest["version"], "newer": newer,
            "notes": manifest.get("notes", "")[:4000], "checked_at": time.time()}
    if r is not None:
        r.set(AVAILABLE_KEY, json.dumps(info), ex=7 * 86400)
    return info


def safe_extract(data, target):
    with tarfile.open(fileobj=__import__("io").BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"unsafe path in the release: {member.name}")
        tar.extractall(target)
    entries = [p for p in Path(target).iterdir()]
    if len(entries) != 1 or not (entries[0] / "install.sh").is_file():
        raise ValueError("the release does not look like LAIka")
    return entries[0]


def pause_workers(r, clock=time.time, sleep=time.sleep):
    subprocess.run(["systemctl", "stop", "laika-scaler"], capture_output=True)
    paused = []
    for key in r.scan_iter("laika:workers:laika-worker-*"):
        worker = key.split(":", 2)[2]
        control = f"laika:worker-control:{worker}"
        if r.get(control) != "disabled":
            r.set(control, "disabled")
            paused.append(control)
    deadline = clock() + WAIT_SECONDS
    while clock() < deadline:
        busy = [k for k in r.scan_iter("laika:workers:laika-worker-*") if r.hget(k, "status") == "working"]
        if not busy:
            return paused
        sleep(5)
    resume_workers(r, paused)
    raise TimeoutError(f"jobs still running after {WAIT_SECONDS}s; nothing changed")


def resume_workers(r, paused):
    for control in paused:
        r.delete(control)
    subprocess.run(["systemctl", "start", "laika-scaler"], capture_output=True)


def run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{' '.join(argv[:3])}… failed: {(result.stderr or result.stdout)[-600:]}")
    return result


def doctor_problems(home=HOME):
    result = subprocess.run([sys.executable, str(home / "scripts/laika-doctor.py"), "--json"], capture_output=True, text=True)
    try:
        checks = json.loads(result.stdout)
    except ValueError:
        return ["doctor did not run"]
    return [f"{c['name']}: {c['detail']}" for c in checks if c["level"] == "fail" and c["name"] in CRITICAL]


def restart_all(home):
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    run([str(home / "scripts/laika-restart.sh"), "--wait", "60", "all"])


def apply(r, getter=fetch, verifier=verify):
    if (HOME / ".git").exists():
        raise SystemExit("This server runs LAIka from a git checkout: update with git, then `sudo laika repair`.")
    base = update_url()
    if not base:
        raise SystemExit("Updates are not configured (Settings → System → Update address).")
    status(r, "checking", "Looking for a new release")
    manifest = latest(base, getter, verifier)
    current = current_version()
    if version_key(manifest["version"]) <= version_key(current):
        status(r, "done", f"Already up to date ({current})")
        return 0
    status(r, "downloading", f"Downloading LAIka {manifest['version']}", version=manifest["version"])
    data = getter(base + manifest["tarball"])
    if hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
        raise ValueError("the download does not match the signed manifest")
    staging = Path(tempfile.mkdtemp(prefix="laika-update-", dir=HOME.parent))
    try:
        new_tree = safe_extract(data, staging)
        status(r, "backing_up", "Backing up first")
        run([sys.executable, str(HOME / "scripts/laika-backup.py")], timeout=3600)
        status(r, "waiting", "Waiting for running jobs to finish")
        paused = pause_workers(r)
        try:
            status(r, "installing", f"Installing LAIka {manifest['version']}")
            if PREVIOUS.exists():
                shutil.rmtree(PREVIOUS)
            HOME.rename(PREVIOUS)
            new_tree.rename(HOME)
            env_link = HOME / ".env"
            if not env_link.exists():
                env_link.symlink_to("/etc/laika/compose.env")
            try:
                run(["bash", str(HOME / "install.sh"), "--repair", "--yes"], timeout=3600)
                status(r, "restarting", "Restarting LAIka")
                restart_all(HOME)
                problems = doctor_problems(HOME)
                if problems:
                    raise RuntimeError("after the update: " + "; ".join(problems))
            except Exception as exc:
                status(r, "rolling_back", f"Going back to {current}: {exc}")
                broken = HOME.with_name(HOME.name + ".failed")
                if broken.exists():
                    shutil.rmtree(broken)
                HOME.rename(broken)
                PREVIOUS.rename(HOME)
                subprocess.run(["bash", str(HOME / "install.sh"), "--repair", "--yes"], capture_output=True)
                restart_all(HOME)
                status(r, "failed", f"The update to {manifest['version']} failed and {current} was restored: {exc}")
                return 1
        finally:
            resume_workers(r, paused)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    r.delete(AVAILABLE_KEY)
    status(r, "done", f"Updated from {current} to {manifest['version']}", version=manifest["version"])
    return 0


def main(argv):
    if os.geteuid() != 0:
        print("Run it as root: sudo laika update", file=sys.stderr)
        return 2
    r = redis_client()
    if len(argv) > 1 and argv[1] == "check":
        print(json.dumps(check(r), indent=1))
        return 0
    if len(argv) > 1 and argv[1] == "apply":
        if "--yes" not in argv and sys.stdin.isatty():
            info = check(r)
            if not info.get("newer"):
                print(f"LAIka {info.get('current')} is up to date.")
                return 0
            if input(f"Update LAIka {info['current']} to {info['latest']}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        try:
            return apply(r)
        except Exception as exc:
            status(r, "failed", str(exc))
            print(f"update failed: {exc}", file=sys.stderr)
            return 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
