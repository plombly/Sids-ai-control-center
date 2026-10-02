#!/usr/bin/env python3
"""laika doctor: check this LAIka installation and say how to fix problems.

    sudo laika doctor          every check, problems first explained
    sudo laika doctor --json   the same as JSON (for scripts and tests)

Read-only: it changes nothing (fixes are printed, most are
`sudo laika repair`). Exit status 1 when any check fails. Never prints
secrets: secret files are checked for their owner and mode only.
"""

import json
import os
import pwd
import grp
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

USER = os.environ.get("LAIKA_USER", "laika")
DATA = Path("/var/lib/laika")
CONF = Path("/etc/laika")
LOGS = Path("/var/log/laika")
VENV = DATA / "venv"
SUPPORTED = {("ubuntu", "22.04"), ("ubuntu", "24.04"), ("ubuntu", "26.04"), ("debian", "12"), ("debian", "13")}
SERVICES = ("laika-orchestrator", "laika-operator", "laika-apps", "laika-scaler")
TIMERS = ("laika-backup", "laika-watchdog", "laika-prune", "laika-notify", "laika-digest", "laika-restore-check")
CONTAINERS = ("laika-redis", "laika-postgres", "laika-api", "laika-web")
REPAIR = "sudo laika repair"

# (path, owner, group, mode) the installer creates (install.sh).
LAYOUT = [
    (DATA / "projects", USER, USER, 0o2770), (DATA / "project-data", USER, USER, 0o2770),
    (DATA / "worktrees", USER, USER, 0o2770), (DATA / "uploads", USER, USER, 0o2770),
    (DATA / "reference", USER, USER, 0o2770), (DATA / "home", USER, USER, 0o700),
    (DATA / "trash", "root", "root", 0o700), (DATA / "db", "root", "root", 0o700),
    (LOGS, USER, USER, 0o2770), (LOGS / "jobs", USER, USER, 0o2770),
    (CONF, "root", USER, 0o750), (CONF / "providers", "root", "root", 0o700),
    (CONF / "notify", "root", "root", 0o700), (CONF / "project-env", "root", "root", 0o700),
]
SECRETS = ("redis.env", "operator.env", "compose.env", "laika.env")


def ok(name, detail):
    return {"name": name, "level": "ok", "detail": detail, "fix": ""}


def warn(name, detail, fix=""):
    return {"name": name, "level": "warn", "detail": detail, "fix": fix}


def fail(name, detail, fix=REPAIR):
    return {"name": name, "level": "fail", "detail": detail, "fix": fix}


def run(argv, timeout=20, **kwargs):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def version_tuple(text):
    import re
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(part or 0) for part in found.groups()) if found else (0, 0, 0)


# --- checks -----------------------------------------------------------------------------------

def check_system(os_release="/etc/os-release"):
    info = {}
    try:
        for line in Path(os_release).read_text().splitlines():
            key, _, value = line.partition("=")
            info[key] = value.strip().strip('"')
    except OSError:
        pass
    name = info.get("PRETTY_NAME", "unknown")
    if (info.get("ID"), info.get("VERSION_ID")) in SUPPORTED:
        return ok("system", name)
    return warn("system", f"{name} is not a supported system (Ubuntu 22.04/24.04/26.04, Debian 12/13)")


def check_resources(meminfo="/proc/meminfo", usage=shutil.disk_usage):
    try:
        total_kb = next(int(line.split()[1]) for line in Path(meminfo).read_text().splitlines()
                        if line.startswith("MemTotal:"))
    except (OSError, StopIteration, ValueError):
        total_kb = 0
    free_gb = usage("/").free / 1024 ** 3
    memory_gb = total_kb / 1024 ** 2
    detail = f"{memory_gb:.1f} GB memory, {free_gb:.0f} GB free disk"
    if memory_gb < 1.8 or free_gb < 3:
        return fail("resources", detail + " (LAIka needs 2 GB memory and some free disk)", "Add memory or free disk space.")
    if memory_gb < 3.5 or free_gb < 10:
        return warn("resources", detail + " (4 GB memory and 20 GB disk recommended)")
    return ok("resources", detail)


def check_user():
    try:
        user = pwd.getpwnam(USER)
    except KeyError:
        return fail("user", f"no '{USER}' user: agents would run as root")
    if user.pw_uid == 0:
        return fail("user", f"'{USER}' is root", "Recreate the laika user as a normal system user.")
    return ok("user", f"'{USER}' (uid {user.pw_uid}), home {user.pw_dir}")


def check_layout(layout=None):
    problems = []
    for path, owner, group, mode in layout or LAYOUT:
        try:
            info = path.stat()
        except OSError:
            problems.append(f"{path} missing")
            continue
        try:
            have_owner = pwd.getpwuid(info.st_uid).pw_name
            have_group = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            have_owner = have_group = "?"
        have_mode = stat.S_IMODE(info.st_mode)
        if (have_owner, have_group, have_mode) != (owner, group, mode):
            problems.append(f"{path} is {have_owner}:{have_group} {have_mode:o}, expected {owner}:{group} {mode:o}")
    if problems:
        return fail("folders", "; ".join(problems[:5]) + (" …" if len(problems) > 5 else ""))
    return ok("folders", f"{len(layout or LAYOUT)} folders with the right owners and permissions")


def check_secrets(conf=CONF):
    problems = []
    for name in SECRETS:
        path = conf / name
        try:
            info = path.stat()
        except OSError:
            problems.append(f"{name} missing")
            continue
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            problems.append(f"{name} readable by others than root")
    if problems:
        return fail("secrets", "; ".join(problems))
    return ok("secrets", "present and readable by root only")


def check_tools(versions_file=ROOT / "deploy/versions.env", runner=run):
    wanted = {}
    try:
        for line in versions_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                wanted[key.strip()] = value.strip()
    except OSError:
        pass
    results, problems = [], []
    for command, key in (("codex", "LAIKA_CODEX_VERSION"), ("claude", "LAIKA_CLAUDE_VERSION")):
        found = runner([command, "--version"])
        if found.returncode:
            problems.append(f"{command} missing")
            continue
        have = version_tuple(found.stdout)
        results.append(f"{command} {'.'.join(map(str, have))}")
        if wanted.get(key) and have != version_tuple(wanted[key]):
            problems.append(f"{command} {'.'.join(map(str, have))} (tested: {wanted[key]})")
    node = runner(["node", "--version"])
    if node.returncode or version_tuple(node.stdout) < (20, 0, 0):
        problems.append("node 20 or newer missing")
    for command in ("bwrap", "git", "docker"):
        if not shutil.which(command):
            problems.append(f"{command} missing")
    if not (VENV / "bin/python").exists():
        problems.append(f"{VENV} missing")
    if any("missing" in p for p in problems):
        return fail("tools", "; ".join(problems))
    if problems:
        return warn("tools", "; ".join(problems), "sudo laika update")
    return ok("tools", ", ".join(results) + ", node, bwrap, git, docker")


def check_containers(runner=run):
    result = runner(["docker", "inspect", "--format", "{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                     *CONTAINERS])
    states = {}
    for line in result.stdout.splitlines():
        parts = line.strip().lstrip("/").split()
        if parts:
            states[parts[0]] = parts[1:]
    bad = [f"{name} {' '.join(states.get(name, ['missing']))}" for name in CONTAINERS
           if states.get(name, [""])[0] != "running" or (len(states[name]) > 1 and states[name][1] != "healthy")]
    if bad:
        return fail("containers", "; ".join(bad), "cd /opt/laika && sudo docker compose up -d")
    return ok("containers", "redis, postgres, api and web running and healthy")


def check_services(runner=run):
    result = runner(["systemctl", "is-active", *[f"{name}.service" for name in SERVICES]])
    states = dict(zip(SERVICES, result.stdout.split()))
    down = [f"{name} {states.get(name, 'unknown')}" for name in SERVICES if states.get(name) != "active"]
    timers = runner(["systemctl", "is-enabled", *[f"{name}.timer" for name in TIMERS]])
    off = [name for name, state in zip(TIMERS, timers.stdout.split()) if state != "enabled"]
    if down:
        return fail("services", "not running: " + ", ".join(down))
    if off:
        return warn("services", "timers not enabled: " + ", ".join(off), REPAIR)
    return ok("services", f"{len(SERVICES)} services running, {len(TIMERS)} timers enabled")


def redis_client():
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True,
                                    socket_connect_timeout=3, socket_timeout=3)


def check_redis_and_workers(client=None, runner=run):
    try:
        client = client or redis_client()
        client.ping()
    except Exception as exc:
        return fail("workers", f"Redis unreachable: {str(exc)[:120]}", "cd /opt/laika && sudo docker compose up -d redis")
    try:
        target = int(client.get("laika:scaler:target") or 0)
    except ValueError:
        target = 0
    live = [key for key in client.scan_iter("laika:workers:*")]
    if target and len(live) < target:
        return warn("workers", f"{len(live)} of {target} workers reporting (they may be starting)",
                    "journalctl -u 'laika-worker@*' -n 50")
    if not live:
        return fail("workers", "no worker is running", "sudo systemctl restart laika-scaler")
    return ok("workers", f"{len(live)} workers reporting" + (f" (scaler target {target})" if target else ""))


def check_http(name, url, getter=None):
    def fetch(address):
        with urllib.request.urlopen(address, timeout=5) as response:
            return response.read().decode()
    try:
        body = (getter or fetch)(url)
    except Exception as exc:
        return fail(name, f"{url} unreachable: {str(exc)[:100]}", "cd /opt/laika && sudo docker compose up -d")
    if name == "api" and '"healthy"' not in body:
        return warn(name, f"{url}: {body[:120]}")
    return ok(name, f"{url} answers")


def check_sandbox(runner=run):
    try:
        user = pwd.getpwnam(USER)
    except KeyError:
        return fail("sandbox", f"no '{USER}' user to test with")
    argv = ["setpriv", f"--reuid={user.pw_uid}", f"--regid={user.pw_gid}", "--init-groups",
            "bwrap", "--die-with-parent", "--unshare-all", "--cap-drop", "ALL", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "/bin/true"]
    result = runner(argv)
    if result.returncode:
        return fail("sandbox", f"bubblewrap cannot run as {USER}: {(result.stderr or '').strip()[:160]}",
                    "Ubuntu: the bubblewrap package must keep its AppArmor profile (bwrap-userns-restrict).")
    return ok("sandbox", f"bubblewrap works as {USER}")


def check_providers(runner=run):
    try:
        user = pwd.getpwnam(USER)
    except KeyError:
        return warn("ai", "cannot check without the laika user")
    prefix = ["setpriv", f"--reuid={user.pw_uid}", f"--regid={user.pw_gid}", "--init-groups",
              "env", f"HOME={user.pw_dir}", "DISABLE_AUTOUPDATER=1"]
    signed = []
    claude = runner(prefix + ["claude", "auth", "status", "--json"])
    try:
        if json.loads(claude.stdout or "{}").get("loggedIn"):
            signed.append("Claude")
    except ValueError:
        pass
    codex = runner(prefix + ["codex", "login", "status"])
    if codex.returncode == 0 and "logged in" in (codex.stdout + codex.stderr).lower():
        signed.append("Codex")
    keys = CONF / "providers" / "providers.env"
    has_keys = keys.exists() and keys.stat().st_size > 0
    if not signed and not has_keys:
        return warn("ai", "no AI provider signed in", "Dashboard → Settings → AI, or the setup guide (#/setup).")
    return ok("ai", ("signed in: " + ", ".join(signed)) if signed else "API keys saved")


def check_exposure(runner=run):
    import ipaddress
    result = runner(["ip", "-j", "-4", "addr"])
    try:
        interfaces = json.loads(result.stdout or "[]")
    except ValueError:
        interfaces = []
    public = []
    for interface in interfaces:
        if interface.get("ifname", "").startswith(("lo", "docker", "br-", "veth", "laika-build")):
            continue
        for info in interface.get("addr_info", []):
            try:
                if ipaddress.ip_address(info.get("local", "")).is_global:
                    public.append(info["local"])
            except ValueError:
                pass
    if public:
        return warn("exposure", f"public address {', '.join(public)}: port 8080 must not be reachable from the internet",
                    "Firewall port 8080 (and 8100-8299) to your LAN / VPN only.")
    return ok("exposure", "no public addresses")


def check_admin(client=None):
    try:
        client = client or redis_client()
        exists = bool(client.hgetall("laika:auth:admin"))
    except Exception:
        return warn("setup", "cannot check (Redis unreachable)")
    if not exists:
        return warn("setup", "no administrator account yet", "sudo laika setup-code, then open the dashboard")
    return ok("setup", "administrator account exists")


def all_checks():
    checks = [check_system, check_resources, check_user, check_layout, check_secrets, check_tools,
              check_containers, check_services, check_redis_and_workers,
              lambda: check_http("api", "http://127.0.0.1:8000/health"),
              lambda: check_http("dashboard", "http://127.0.0.1:8080/health"),
              check_sandbox, check_providers, check_exposure, check_admin]
    results = []
    for check in checks:
        try:
            results.append(check())
        except Exception as exc:  # a broken check is a finding, never a crash
            name = getattr(check, "__name__", "check").replace("check_", "")
            results.append(fail(name, f"check crashed: {exc}", "Report this."))
    return results


def render(results, color=True):
    marks = {"ok": ("✔", "32"), "warn": ("!", "33"), "fail": ("✗", "31")}
    lines = []
    for item in results:
        mark, code = marks[item["level"]]
        mark = f"\033[{code}m{mark}\033[0m" if color else mark
        lines.append(f" {mark} {item['name']:<11} {item['detail']}")
        if item["fix"] and item["level"] != "ok":
            lines.append(f"   {'':<11} fix: {item['fix']}")
    failed = sum(item["level"] == "fail" for item in results)
    warned = sum(item["level"] == "warn" for item in results)
    lines.append("")
    lines.append("All good." if not failed and not warned else f"{failed} problem(s), {warned} warning(s).")
    return "\n".join(lines)


def main(argv):
    if os.geteuid() != 0:
        print("Run it as root: sudo laika doctor", file=sys.stderr)
        return 2
    results = all_checks()
    if "--json" in argv:
        print(json.dumps(results, indent=1))
    else:
        print(render(results, color=sys.stdout.isatty()))
    return 1 if any(item["level"] == "fail" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
