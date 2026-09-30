"""Backup: verified snapshot, root-only, rotated, failures reported."""

import json
from types import SimpleNamespace

import pytest

from sid_testing import ROOT, load_module


@pytest.fixture
def bk(tmp_path, monkeypatch):
    module = load_module(ROOT / "scripts/backup-sid.py")
    module.BACKUP_ROOT = tmp_path / "backups"
    recorded = []
    module.record_status = recorded.append
    module.recorded = recorded
    return module


def fake_steps(bk, fail=None):
    def step(name):
        def run(dest):
            if name == fail:
                raise RuntimeError(f"{name} broke")
            (dest / f"{name}.part").write_text("x")
            return {"bytes": 1}
        return run
    bk.backup_repo, bk.backup_redis = step("repo"), step("redis")
    bk.backup_postgres, bk.backup_config = step("postgres"), step("config")


def test_snapshot_is_root_only_with_manifest(bk):
    fake_steps(bk)
    assert bk.main() == 0
    [snap] = list(bk.BACKUP_ROOT.iterdir())
    assert oct(bk.BACKUP_ROOT.stat().st_mode)[-3:] == "700"
    assert oct(snap.stat().st_mode)[-3:] == "700"
    manifest = json.loads((snap / "manifest.json").read_text())
    assert set(manifest["parts"]) == {"repo", "redis", "postgres", "config"} and not manifest["errors"]
    assert bk.recorded[-1]["ok"] is True


def test_a_failing_part_is_reported_and_fails_the_run(bk):
    fake_steps(bk, fail="redis")
    assert bk.main() == 1
    assert bk.recorded[-1]["ok"] is False
    assert bk.recorded[-1]["errors"] == {"redis": "redis broke"}


def test_rotation_keeps_newest(bk, tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (root / stamp).mkdir()
    assert bk.rotate(root, 2) == ["20260101T000000Z"]
    assert sorted(p.name for p in root.iterdir()) == ["20260102T000000Z", "20260103T000000Z"]


def test_config_archive_includes_only_existing_paths(bk, tmp_path):
    present = tmp_path / "etc-sid"
    present.mkdir()
    (present / "operator.env").write_text("SECRET=1")
    bk.CONFIG_PATHS = [present, tmp_path / "missing.service"]
    dest = tmp_path / "d"
    dest.mkdir()
    assert bk.backup_config(dest)["paths"] == [str(present)]


def test_remote_copy_failure_fails_the_run(bk, monkeypatch):
    fake_steps(bk)
    bk.BACKUP_REMOTE = "backup@offsite:/sid"
    monkeypatch.setattr(bk, "run", lambda cmd, **k: SimpleNamespace(returncode=23, stderr="rsync: denied", stdout=""))
    assert bk.main() == 1
    assert bk.recorded[-1]["ok"] is False


def test_secrets_are_copied_not_read():
    source = (ROOT / "scripts/backup-sid.py").read_text()
    assert "read_text" not in source.split("def backup_config")[1].split("def rotate")[0]
