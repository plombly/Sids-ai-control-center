"""services/apps/laika_apps.py: keep each project's app running from main."""

import subprocess
import sys

import pytest

from laika_testing import ROOT, MemoryRedis, load_module

sys.path.insert(0, str(ROOT / "services"))


class AppsRedis(MemoryRedis):
    def smembers(self, key):
        return set(self.values.get(key, set()))

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return iter([k for k in list(self.records) if k.startswith(prefix)])

    def delete(self, *keys):
        return super().delete(*keys)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def apps(tmp_path, monkeypatch):
    module = load_module(ROOT / "services/apps/laika_apps.py")
    module.redis = AppsRedis()
    root = tmp_path / "shop"
    repo = root / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "server.js").write_text("// v1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "v1", cwd=repo)
    module.redis.records["laika:projects:shop"] = {
        "id": "shop", "name": "Shop", "status": "active", "root": str(root), "repo": str(repo),
        "worktrees": str(root / "worktrees"), "logs": str(root / "logs"), "run_command": "node server.js",
        "setup_command": "echo setup-ok"}
    module.redis.values["laika:projects"] = {"shop"}
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
        return subprocess.run(args, text=True, capture_output=True, **kwargs)

    monkeypatch.setattr(module, "run", fake_run)
    # Setup runs for real, but without bubblewrap (the sandbox has its own tests).
    monkeypatch.setattr(module.project_sandbox, "command", lambda argv, *a, **k: list(argv))
    return module, repo, root, calls, units


def status(module):
    return module.redis.records.get("laika:app-status:shop", {})


def started(calls):
    return [c for c in calls if c[0] == "systemd-run"]


def test_first_loop_deploys_main_with_a_port(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    st = status(module)
    assert st["state"] == "running" and st["port"] == "8100"
    assert module.redis.records["laika:projects:shop"]["run_port"] == "8100"
    assert (root / "live" / "server.js").read_text() == "// v1\n"
    [run] = started(calls)
    assert "--setenv=PORT=8100" in run and "--setenv=HOST=0.0.0.0" in run and "--unit=laika-app-shop" in run
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
    units["laika-app-shop"] = "failed"
    module.loop_once()
    assert status(module)["state"] == "crashed" and "boom" in status(module)["log"]
    module.redis.records["laika:projects:shop"]["restart_at"] = str(float(status(module)["deployed_at"]) + 1)
    module.loop_once()
    assert status(module)["state"] == "running" and len(started(calls)) == 2


def test_removing_the_run_command_stops_the_app(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    module.redis.records["laika:projects:shop"]["run_command"] = ""
    module.loop_once()
    assert units["laika-app-shop"] == "inactive" and status(module)["state"] == "stopped"


def test_failed_setup_waits_for_a_change(apps):
    module, repo, root, calls, units = apps
    module.redis.records["laika:projects:shop"]["setup_command"] = "echo no-network; exit 3"
    module.loop_once()
    assert status(module)["state"] == "setup_failed" and "no-network" in status(module)["log"]
    module.loop_once()
    assert not started(calls)


def test_ports_are_unique_across_projects(apps):
    module, *_ = apps
    module.redis.records["laika:projects:other"] = {"id": "other", "run_port": "8100"}
    module.redis.values["laika:projects"].add("other")
    module.loop_once()
    assert status(module)["port"] == "8101"


def test_deleted_projects_apps_are_stopped(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    module.redis.values["laika:projects"] = set()
    del module.redis.records["laika:projects:shop"]
    module.loop_once()
    assert units["laika-app-shop"] == "inactive" and "laika:app-status:shop" not in module.redis.records


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
    module.redis.records["laika:projects:shop"].update(run_memory_mb="256", run_cpus="0.5")
    module.loop_once()
    assert "--property=MemoryMax=256M" in started(calls)[-1] and "--property=CPUQuota=50%" in started(calls)[-1]


def test_out_of_memory_is_explained(apps, monkeypatch):
    module, repo, root, calls, units = apps
    module.loop_once()
    units["laika-app-shop"] = "failed"
    real = module.run
    monkeypatch.setattr(module, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "oom-kill\n", "")
                        if args[:2] == ["systemctl", "show"] and "Result" in args else real(args, **kw))
    module.loop_once()
    assert "1024 MB memory limit" in status(module)["error"]


def test_failed_units_are_kept_for_the_crash_report():
    module = load_module(ROOT / "services/apps/laika_apps.py")
    assert '"--collect"' not in (ROOT / "services/apps/laika_apps.py").read_text().split("def start_unit")[1].split("def deploy")[0]


def test_apps_load_secrets_from_their_env_file(apps):
    module, repo, root, calls, units = apps
    module.loop_once()
    [run] = started(calls)
    assert f"--property=EnvironmentFile=-{module.ENV_DIR}/shop.env" in run
    assert not any("API_KEY" in arg for arg in run)  # values never on the command line


# --- previews -------------------------------------------------------------------------

def head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def request_preview(module, repo, job_id="j1", status="awaiting_review"):
    module.redis.records[f"laika:jobs:{job_id}"] = {"id": job_id, "project_id": "shop", "status": status,
                                                  "integrated_candidate_commit": head(repo)}
    module.redis.records[f"laika:preview:{job_id}"] = {"state": "requested", "project_id": "shop",
                                                     "candidate": head(repo), "requested_at": "1000"}


def preview_runs(calls):
    return [c for c in calls if c[0] == "systemd-run" and any(a.startswith("--unit=laika-preview-") for a in c)]


def test_a_requested_preview_runs_the_candidate_with_its_own_data(apps):
    module, repo, root, calls, units = apps
    request_preview(module, repo)
    module.reconcile_previews({"shop": module.laika_projects.load(module.redis, "shop")}, now=1100)
    info = module.redis.records["laika:preview:j1"]
    assert info["state"] == "running" and info["port"] == "8200"
    workdir, data = module.preview_paths(module.laika_projects.load(module.redis, "shop"), "j1")
    assert (workdir / "server.js").exists() and data.is_dir()
    [run] = preview_runs(calls)
    assert "--setenv=PORT=8200" in run and "--unit=laika-preview-j1" in run
    assert any("EnvironmentFile" in a for a in run) and "--property=MemoryMax=1024M" in run


def test_previews_go_away_when_the_change_is_decided(apps):
    module, repo, root, calls, units = apps
    project = lambda: {"shop": module.laika_projects.load(module.redis, "shop")}
    request_preview(module, repo)
    module.reconcile_previews(project(), now=1100)
    workdir, data = module.preview_paths(project()["shop"], "j1")
    module.redis.records["laika:jobs:j1"]["status"] = "merged"
    module.reconcile_previews(project(), now=1200)
    assert "laika:preview:j1" not in module.redis.records
    assert not workdir.exists() and not data.exists()
    assert ["systemctl", "stop", "laika-preview-j1"] in calls


def test_stop_request_and_expiry(apps):
    module, repo, root, calls, units = apps
    project = lambda: {"shop": module.laika_projects.load(module.redis, "shop")}
    request_preview(module, repo, "j2")
    module.redis.records["laika:preview:j2"]["state"] = "stop"
    module.reconcile_previews(project(), now=1100)
    assert "laika:preview:j2" not in module.redis.records
    request_preview(module, repo, "j3")
    module.reconcile_previews(project(), now=1000 + 5 * 3600)  # older than PREVIEW_HOURS
    assert "laika:preview:j3" not in module.redis.records and not preview_runs(calls)


# --- project types and builds ------------------------------------------------------------

def test_type_is_detected_once_per_main_commit_and_on_recheck(apps, monkeypatch):
    module, repo, root, calls, units = apps
    seen = []
    real = module.project_detect.detect_repo
    monkeypatch.setattr(module.project_detect, "detect_repo", lambda *a: seen.append(a) or real(*a))
    projects = [p for p in module.laika_projects.all_projects(module.redis) if not p.is_builtin]
    module.publish_types(projects)
    stored = module.json.loads(module.redis.get("laika:project-type:shop"))
    assert stored["head"] == head(repo) and "type" in stored and stored["checked_at"]
    module.publish_types(projects)
    assert len(seen) == 1  # unchanged main: no new detection
    (repo / "index.html").write_text("<html></html>\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "page", cwd=repo)
    module.publish_types(projects)
    assert len(seen) == 2
    # "Recheck now" removes the stored result.
    module.redis.delete("laika:project-type:shop")
    module.publish_types(projects)
    assert len(seen) == 3
    # Unrecognised projects are looked at again every hour.
    stored = module.json.loads(module.redis.get("laika:project-type:shop"))
    module.redis.set("laika:project-type:shop", module.json.dumps({**stored, "type": "unknown"}))
    module.publish_types(projects)
    assert len(seen) == 3
    module.redis.set("laika:project-type:shop", module.json.dumps({**stored, "type": "unknown", "checked_at": 1}))
    module.publish_types(projects)
    assert len(seen) == 4


def build_runs(calls):
    return [c for c in started(calls) if any(a.startswith("--unit=laika-build-") for a in c)]


def test_a_build_request_starts_one_build_unit(apps):
    module, repo, root, calls, units = apps
    module.redis.set("laika:build-request:shop", module.json.dumps({"build_id": "b1"}))
    projects = [p for p in module.laika_projects.all_projects(module.redis) if not p.is_builtin]
    module.launch_builds(projects)
    runs = build_runs(calls)
    assert len(runs) == 1 and runs[0][-3:] == [str(module.BUILD_SCRIPT), "shop", "b1"]
    assert any(a.startswith("--property=RuntimeMaxSec=") for a in runs[0])
    assert module.redis.get("laika:build-request:shop") is None
    assert module.redis.records["laika:build:shop:b1"]["status"] == "queued"
    assert module.redis.lrange("laika:builds:shop", 0, -1) == ["b1"]
    # A second request waits while the first build runs.
    module.redis.set("laika:build-request:shop", module.json.dumps({"build_id": "b2"}))
    module.launch_builds(projects)
    assert len(build_runs(calls)) == 1 and module.redis.get("laika:build-request:shop")
    units["laika-build-shop-b1"] = "inactive"
    module.redis.records["laika:build:shop:b1"]["status"] = "succeeded"
    module.launch_builds(projects)
    assert len(build_runs(calls)) == 2


def test_a_build_that_cannot_start_is_reported(apps, monkeypatch):
    module, repo, root, calls, units = apps
    monkeypatch.setattr(module, "run", lambda args, **k: subprocess.CompletedProcess(args, 1, "", "no systemd"))
    module.redis.set("laika:build-request:shop", module.json.dumps({"build_id": "b1"}))
    module.launch_builds([p for p in module.laika_projects.all_projects(module.redis) if not p.is_builtin])
    record = module.redis.records["laika:build:shop:b1"]
    assert record["status"] == "failed" and "no systemd" in record["error"]


def test_bad_build_ids_are_ignored_and_vanished_builds_fail(apps):
    module, repo, root, calls, units = apps
    projects = [p for p in module.laika_projects.all_projects(module.redis) if not p.is_builtin]
    module.redis.set("laika:build-request:shop", module.json.dumps({"build_id": "../x-y"}))
    module.launch_builds(projects)
    assert not build_runs(calls) and module.redis.get("laika:build-request:shop") is None
    # A queued build whose unit never ran (or died) is failed after the grace time.
    module.redis.lpush("laika:builds:shop", "b9")
    module.redis.records["laika:build:shop:b9"] = {"status": "queued", "requested_at": str(module.time.time())}
    module.launch_builds(projects)
    assert module.redis.records["laika:build:shop:b9"]["status"] == "queued"
    module.redis.records["laika:build:shop:b9"]["requested_at"] = "1"
    module.launch_builds(projects)
    assert module.redis.records["laika:build:shop:b9"]["status"] == "failed"
    # A running unit is left alone.
    module.redis.records["laika:build:shop:b8"] = {"status": "running", "requested_at": "1"}
    module.redis.lpush("laika:builds:shop", "b8")
    units["laika-build-shop-b8"] = "active"
    module.launch_builds(projects)
    assert module.redis.records["laika:build:shop:b8"]["status"] == "running"
