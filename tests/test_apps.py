"""services/apps/sid_apps.py: keep each project's app running from main."""

import subprocess
import sys

import pytest

from sid_testing import ROOT, MemoryRedis, load_module

sys.path.insert(0, str(ROOT / "services"))


class AppsRedis(MemoryRedis):
    def smembers(self, key):
        return set(self.values.get(key, set()))

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return iter([k for k in list(self.records) if k.startswith(prefix)])

    def delete(self, key):
        self.records.pop(key, None)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def apps(tmp_path, monkeypatch):
    module = load_module(ROOT / "services/apps/sid_apps.py")
    module.redis = AppsRedis()
    root = tmp_path / "shop"
    repo = root / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "server.js").write_text("// v1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "v1", cwd=repo)
    module.redis.records["sid:projects:shop"] = {
        "id": "shop", "name": "Shop", "status": "active", "root": str(root), "repo": str(repo),
        "worktrees": str(root / "worktrees"), "logs": str(root / "logs"), "run_command": "node server.js",
        "setup_command": "echo setup-ok"}
    module.redis.values["sid:projects"] = {"shop"}
    calls, units = [], {}

    def fake_run(args, **kwargs):
        calls.append(list(args))
        name = args[0]
        if name in ("systemctl",):
            if args[1] == "show":
                return subprocess.CompletedProcess(args, 0, units.get(args[2], "inactive") + "\n", "")
            if args[1] == "stop":
                units[args[2]] = "inactive"
            return subprocess.CompletedProcess(args, 0, "", "")
        if name == "systemd-run":
            unit = next(a for a in args if a.startswith("--unit=")).split("=", 1)[1]
            units[unit] = "active"
            return subprocess.CompletedProcess(args, 0, "", "")
        if name == "journalctl":
            return subprocess.CompletedProcess(args, 0, "boom\n", "")
        if name == "docker":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, text=True, capture_output=True, **kwargs)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "NGINX_DIR", tmp_path / "nginx")
    # Setup runs for real, but without bubblewrap (the sandbox has its own tests).
    monkeypatch.setattr(module.project_sandbox, "command", lambda argv, *a, **k: list(argv))
    return module, repo, root, calls, units


def status(module):
    return module.redis.records.get("sid:app-status:shop", {})


def started(calls):
    return [c for c in calls if c[0] == "systemd-run"]


def test_first_loop_deploys_main_with_a_port(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    st = status(module)
    assert st["state"] == "running" and st["port"] == "8100"
    assert module.redis.records["sid:projects:shop"]["run_port"] == "8100"
    assert (root / "live" / "server.js").read_text() == "// v1\n"
    [run] = started(calls)
    assert "--setenv=PORT=8100" in run and "--setenv=HOST=0.0.0.0" in run and "--unit=sid-app-shop" in run
    assert run[-3:] == ["/bin/sh", "-c", "node server.js"]
    module.loop_once()
    assert len(started(calls)) == 1  # nothing changed: no redeploy


def test_a_new_main_commit_redeploys(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    (repo / "server.js").write_text("// v2\n")
    git("commit", "-qam", "v2", cwd=repo)
    module.loop_once()
    assert len(started(calls)) == 2 and (root / "live" / "server.js").read_text() == "// v2\n"


def test_restart_request_and_crash_reporting(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    units["sid-app-shop"] = "failed"
    module.loop_once()
    assert status(module)["state"] == "crashed" and "boom" in status(module)["log"]
    module.redis.records["sid:projects:shop"]["restart_at"] = str(float(status(module)["deployed_at"]) + 1)
    module.loop_once()
    assert status(module)["state"] == "running" and len(started(calls)) == 2


def test_removing_the_run_command_stops_the_app(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    module.redis.records["sid:projects:shop"]["run_command"] = ""
    module.loop_once()
    assert units["sid-app-shop"] == "inactive" and status(module)["state"] == "stopped"


def test_failed_setup_waits_for_a_change(apps):
    module, repo, root, calls, units = apps
    module.redis.records["sid:projects:shop"]["setup_command"] = "echo no-network; exit 3"
    module.loop_once()
    assert status(module)["state"] == "setup_failed" and "no-network" in status(module)["log"]
    module.loop_once()
    assert not started(calls)


def test_ports_are_unique_across_projects(apps):
    module, *_ = apps
    module.redis.records["sid:projects:other"] = {"id": "other", "run_port": "8100"}
    module.redis.values["sid:projects"].add("other")
    module.loop_once()
    assert status(module)["port"] == "8101"


def test_deleted_projects_apps_are_stopped(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    module.redis.values["sid:projects"] = set()
    del module.redis.records["sid:projects:shop"]
    module.loop_once()
    assert units["sid-app-shop"] == "inactive" and "sid:app-status:shop" not in module.redis.records


def test_tampered_live_checkout_is_refused(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    (root / "live" / ".git").write_text("gitdir: /tmp/evil\n")
    (repo / "server.js").write_text("// v2\n")
    git("commit", "-qam", "v2", cwd=repo)
    module.loop_once()
    assert status(module)["state"] == "error" and "tampered" in status(module)["error"]


def test_apps_run_with_resource_limits_and_changing_them_redeploys(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    [first] = started(calls)
    assert {"--property=MemoryMax=1024M", "--property=CPUQuota=100%", "--property=TasksMax=512",
            "--property=MemorySwapMax=0"} <= set(first)
    module.redis.records["sid:projects:shop"].update(run_memory_mb="256", run_cpus="0.5")
    module.loop_once()
    assert "--property=MemoryMax=256M" in started(calls)[-1] and "--property=CPUQuota=50%" in started(calls)[-1]


def test_out_of_memory_is_explained(apps, monkeypatch):
    module, repo, root, calls, units = apps
    module.loop_once()
    units["sid-app-shop"] = "failed"
    real = module.run
    monkeypatch.setattr(module, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "oom-kill\n", "")
                        if args[:2] == ["systemctl", "show"] and "Result" in args else real(args, **kw))
    module.loop_once()
    assert "1024 MB memory limit" in status(module)["error"]


def test_failed_units_are_kept_for_the_crash_report():
    module = load_module(ROOT / "services/apps/sid_apps.py")
    assert '"--collect"' not in (ROOT / "services/apps/sid_apps.py").read_text().split("def start_unit")[1].split("def deploy")[0]


def test_apps_load_secrets_from_their_env_file(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    [run] = started(calls)
    assert f"--property=EnvironmentFile=-{module.ENV_DIR}/shop.env" in run
    assert not any("API_KEY" in arg for arg in run)  # values never on the command line


def test_friendly_addresses_follow_running_apps(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    conf = (module.NGINX_DIR / "apps.conf").read_text()
    assert "server_name shop.sid.lan;" in conf and "proxy_pass http://127.0.0.1:8100;" in conf
    reloads = [c for c in calls if c[:2] == ["docker", "exec"] and c[-2:] == ["-s", "reload"]]
    assert len(reloads) == 1
    module.loop_once()
    assert len([c for c in calls if c[-2:] == ["-s", "reload"]]) == 1  # unchanged: no reload
    module.redis.records["sid:projects:shop"]["run_command"] = ""
    module.loop_once()
    assert "shop.sid.lan" not in (module.NGINX_DIR / "apps.conf").read_text()


def test_a_config_nginx_rejects_is_rolled_back(apps, monkeypatch):
    module, repo, root, calls, units = apps
    real = module.run
    monkeypatch.setattr(module, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "bad")
                        if args[-1] == "-t" else real(args, **kw))
    module.publish_routes({"shop": "8100"})
    assert not (module.NGINX_DIR / "apps.conf").exists()
