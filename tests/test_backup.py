"""Backup: verified snapshot, root-only, rotated, failures reported."""

import json
from types import SimpleNamespace

import pytest

from laika_testing import ROOT, load_module


@pytest.fixture
def bk(tmp_path, monkeypatch):
    module = load_module(ROOT / "scripts/laika-backup.py")
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
    bk.backup_projects = step("projects")


def test_snapshot_is_root_only_with_manifest(bk):
    fake_steps(bk)
    assert bk.main() == 0
    [snap] = list(bk.BACKUP_ROOT.iterdir())
    assert oct(bk.BACKUP_ROOT.stat().st_mode)[-3:] == "700"
    assert oct(snap.stat().st_mode)[-3:] == "700"
    manifest = json.loads((snap / "manifest.json").read_text())
    assert set(manifest["parts"]) == {"repo", "redis", "postgres", "config", "projects"} and not manifest["errors"]
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
    present = tmp_path / "etc-laika"
    present.mkdir()
    (present / "operator.env").write_text("SECRET=1")
    bk.CONFIG_PATHS = [present, tmp_path / "missing.service"]
    dest = tmp_path / "d"
    dest.mkdir()
    assert bk.backup_config(dest)["paths"] == [str(present)]


def test_remote_copy_failure_fails_the_run(bk, monkeypatch):
    fake_steps(bk)
    bk.BACKUP_REMOTE = "backup@offsite:/laika"
    monkeypatch.setattr(bk, "run", lambda cmd, **k: SimpleNamespace(returncode=23, stderr="rsync: denied", stdout=""))
    assert bk.main() == 1
    assert bk.recorded[-1]["ok"] is False


def test_secrets_are_copied_not_read():
    source = (ROOT / "scripts/laika-backup.py").read_text()
    assert "read_text" not in source.split("def backup_config")[1].split("def rotate")[0]


def test_rotation_never_touches_directories_it_did_not_create(bk, tmp_path):
    # Regression: the first install sorted hand-made backups in with the
    # snapshots and deleted 12 of them ("2026..." sorts before letters).
    root = tmp_path / "shared"
    root.mkdir()
    foreign = ["diagnostic-fix-20260929-180820", "efficiency-v1-20260929-125253", "upgrade-20260929-112037",
               "20260101", "20260101T000000Z-manual", "notes"]
    for name in foreign:
        (root / name).mkdir()
    (root / "link").symlink_to(root / "notes")
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (root / stamp).mkdir()
    assert bk.rotate(root, 1) == ["20260101T000000Z", "20260102T000000Z"]
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == sorted(foreign + ["link", "20260103T000000Z"])


def test_default_location_is_a_dedicated_directory(bk):
    module = load_module(ROOT / "scripts/laika-backup.py")
    assert str(module.BACKUP_ROOT).endswith("/laika/snapshots")


def test_git_remote_push_is_part_of_the_backup(bk, monkeypatch):
    fake_steps(bk)
    bk.BACKUP_GIT_REMOTE = "origin"
    calls = []
    monkeypatch.setattr(bk, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0, stderr="", stdout=""))
    assert bk.main() == 0
    assert calls[0][-3:] == ["push", "origin", "main"]
    assert "--force" not in calls[0] and "-f" not in calls[0]


def test_git_remote_push_failure_fails_the_backup(bk, monkeypatch):
    fake_steps(bk)
    bk.BACKUP_GIT_REMOTE = "origin"
    monkeypatch.setattr(bk, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stderr="rejected (non-fast-forward)", stdout=""))
    assert bk.main() == 1
    assert "non-fast-forward" in bk.recorded[-1]["errors"]["git_remote"]


def test_projects_are_bundled_with_their_app_data(bk, tmp_path):
    import subprocess
    import tarfile
    base, data = tmp_path / "projects", tmp_path / "project-data"
    repo = base / "shop" / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a"], check=True)
    subprocess.run(["git", "init", "-q", str(base / "empty" / "repo")], check=True)  # no commits: skipped
    (data / "shop").mkdir(parents=True)
    (data / "shop" / "app.db").write_text("rows")
    bk.PROJECTS_BASE, bk.PROJECT_DATA = base, data
    dest = tmp_path / "snap"
    dest.mkdir()
    result = bk.backup_projects(dest)
    assert set(result["bundles"]) == {"shop"} and result["data_bytes"] > 0
    names = tarfile.open(dest / "projects" / "project-data.tar.gz").getnames()
    assert any(n.endswith("shop/app.db") for n in names)
