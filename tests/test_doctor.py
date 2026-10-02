"""laika doctor: its checks read the machine and explain problems (never secrets)."""

import json
import os
import subprocess

import pytest

from laika_testing import ROOT, MemoryRedis, load_module


@pytest.fixture
def doc():
    return load_module(ROOT / "scripts/laika-doctor.py", "laika_doctor_test")


def done(stdout="", code=0, stderr=""):
    return lambda argv, **k: subprocess.CompletedProcess(argv, code, stdout, stderr)


def test_system_and_resources(doc, tmp_path):
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\n')
    assert doc.check_system(release)["level"] == "ok"
    release.write_text('ID=arch\nVERSION_ID=rolling\nPRETTY_NAME="Arch Linux"\n')
    assert doc.check_system(release)["level"] == "warn"
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8000000 kB\n")
    usage = lambda path: type("U", (), {"free": 50 * 1024 ** 3})()
    assert doc.check_resources(meminfo, usage)["level"] == "ok"
    meminfo.write_text("MemTotal: 1000000 kB\n")
    assert doc.check_resources(meminfo, usage)["level"] == "fail"


def test_layout_and_secrets_report_owner_and_mode_only(doc, tmp_path):
    good = tmp_path / "good"
    good.mkdir(mode=0o700)
    os.chmod(good, 0o700)
    assert doc.check_layout([(good, "root", "root", 0o700)])["level"] == "ok"
    result = doc.check_layout([(good, "root", "root", 0o2770), (tmp_path / "gone", "root", "root", 0o700)])
    assert result["level"] == "fail" and "expected root:root 2770" in result["detail"] and "gone missing" in result["detail"]
    for name in doc.SECRETS:
        (tmp_path / name).write_text("REDIS_PASSWORD=very-secret\n")
        os.chmod(tmp_path / name, 0o600)
    assert doc.check_secrets(tmp_path)["level"] == "ok"
    os.chmod(tmp_path / "redis.env", 0o644)
    result = doc.check_secrets(tmp_path)
    assert result["level"] == "fail" and "very-secret" not in json.dumps(result)


def test_tools_compare_with_the_tested_versions(doc, tmp_path, monkeypatch):
    versions = tmp_path / "versions.env"
    versions.write_text("LAIKA_CODEX_VERSION=0.159.0\nLAIKA_CLAUDE_VERSION=2.1.287\n")
    monkeypatch.setattr(doc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doc, "VENV", tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/python").write_text("")
    answers = {"codex": "codex-cli 0.159.0", "claude": "2.1.287 (Claude Code)", "node": "v22.22.1"}
    runner = lambda argv, **k: subprocess.CompletedProcess(argv, 0, answers[argv[0]], "")
    assert doc.check_tools(versions, runner)["level"] == "ok"
    answers["claude"] = "2.1.300 (Claude Code)"
    newer = doc.check_tools(versions, runner)
    assert newer["level"] == "ok" and "newer than tested" in newer["detail"]
    answers["claude"] = "2.1.200 (Claude Code)"
    assert doc.check_tools(versions, runner)["level"] == "warn"
    answers["claude"] = "2.1.287 (Claude Code)"
    answers["node"] = "v18.0.0"
    assert doc.check_tools(versions, runner)["level"] == "fail"


def test_containers_services_and_workers(doc):
    healthy = "\n".join(f"/{name} running healthy" for name in doc.CONTAINERS)
    assert doc.check_containers(done(healthy))["level"] == "ok"
    assert doc.check_containers(done(healthy.replace("laika-api running healthy", "laika-api running unhealthy")))["level"] == "fail"
    runner = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "\n".join(
        ("active" if argv[1] == "is-active" else "enabled") for _ in argv[2:]), "")
    assert doc.check_services(runner)["level"] == "ok"
    r = MemoryRedis()
    r.ping = lambda: True
    assert doc.check_redis_and_workers(r)["level"] == "fail"       # no worker at all
    r.values["laika:scaler:target"] = "2"
    r.records["laika:workers:laika-worker-01"] = {"status": "idle"}
    assert doc.check_redis_and_workers(r)["level"] == "warn"
    r.records["laika:workers:laika-worker-02"] = {"status": "idle"}
    assert doc.check_redis_and_workers(r)["level"] == "ok"


def test_exposure_and_render(doc):
    out = json.dumps([{"ifname": "eth0", "addr_info": [{"local": "93.184.216.34"}]},
                      {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]}])
    assert doc.check_exposure(done(out))["level"] == "warn"
    text = doc.render([doc.ok("a", "fine"), doc.fail("b", "broken", "do this")], color=False)
    assert " ✔ a" in text and " ✗ b" in text and "fix: do this" in text and "1 problem(s)" in text


def test_backups(doc):
    r = MemoryRedis()
    assert doc.check_backups(r)["level"] == "warn"                       # none yet
    r.values["laika:backup:last"] = json.dumps({"at": "20261002T033000Z", "ok": True})
    assert doc.check_backups(r, now=1790919000)["level"] == "ok"
    assert doc.check_backups(r, now=1790919000 + 3 * 86400)["level"] == "warn"
    r.values["laika:backup:last"] = json.dumps({"at": "20261002T033000Z", "ok": False, "errors": {"repo": "x"}})
    result = doc.check_backups(r, now=1790919000)
    assert result["level"] == "fail" and "repo" in result["detail"]
