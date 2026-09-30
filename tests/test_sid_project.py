import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sid_project_test_module", ROOT / "scripts/sid-project.py")
sid_project = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sid_project
SPEC.loader.exec_module(sid_project)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}

    def hset(self, key, field=None, value=None, mapping=None):
        record = self.hashes.setdefault(key, {})
        if mapping is not None:
            record.update({str(k): str(v) for k, v in mapping.items()})
        if field is not None:
            record[str(field)] = str(value)

    def hgetall(self, key): return dict(self.hashes.get(key, {}))
    def hget(self, key, field): return self.hashes.get(key, {}).get(field)
    def sadd(self, key, value): self.sets.setdefault(key, set()).add(value)
    def sismember(self, key, value): return value in self.sets.get(key, set())
    def smembers(self, key): return set(self.sets.get(key, set()))
    def exists(self, key): return key in self.hashes


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(sid_project, "get_redis", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def git_environment(monkeypatch, tmp_path):
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def invoke(args, capsys):
    code = sid_project.main(args)
    captured = capsys.readouterr()
    return code, captured, json.loads(captured.out) if captured.out else None


def test_create_empty_and_duplicate_and_invalid(redis, tmp_path, capsys):
    code, _, data = invoke(["create", "--id", "demo", "--name", "Demo", "--empty", "--root-base", str(tmp_path)], capsys)
    assert code == 0
    root = Path(data["root"])
    assert all((root / name).is_dir() for name in ("repo", "worktrees", "logs"))
    assert subprocess.run(["git", "-C", str(root / "repo"), "log", "-1"], capture_output=True, text=True).returncode == 0
    assert data["status"] == "active" and data["default_branch"] == "main"
    assert redis.sismember("sid:projects", "demo")
    assert invoke(["create", "--id", "demo", "--name", "Again", "--empty", "--root-base", str(tmp_path)], capsys)[0] == 1
    assert invoke(["create", "--id", "Bad_ID", "--name", "Bad", "--empty", "--root-base", str(tmp_path)], capsys)[0] == 1
    assert invoke(["show", "a" * 41], capsys)[0] == 1


def test_existing_root_refused(redis, tmp_path, capsys):
    (tmp_path / "taken").mkdir()
    code, captured, _ = invoke(["create", "--id", "taken", "--name", "Taken", "--empty", "--root-base", str(tmp_path)], capsys)
    assert code == 1 and "ERROR:" in captured.err


def test_clone_detects_gate(redis, tmp_path, capsys):
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(tmp_path / "origin.git")], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(tmp_path / "origin.git"), str(source)], check=True, capture_output=True)
    (source / "tests").mkdir(); (source / "tests" / "test_x.py").write_text("")
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "tests"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "push"], cwd=source, check=True, capture_output=True)
    code, _, data = invoke(["create", "--id", "clone", "--name", "Clone", "--clone", str(tmp_path / "origin.git"), "--root-base", str(tmp_path / "projects")], capsys)
    assert code == 0 and data["gate_command"] == "python3 -m pytest -q"


def test_ssh_failure_pending_key(redis, tmp_path, capsys):
    code, _, data = invoke(["create", "--id", "ssh", "--name", "SSH", "--clone", "ssh://127.0.0.1:1/x.git", "--root-base", str(tmp_path)], capsys)
    assert code == 0 and data["status"] == "pending_key" and data["public_key"] and "retry-clone ssh" in data["retry_command"]


def test_register_mutations_push_and_list(redis, tmp_path, capsys):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    worktrees, logs = tmp_path / "worktrees", tmp_path / "logs"
    worktrees.mkdir(); logs.mkdir()
    assert invoke(["register-existing", "--id", "zeta", "--name", "Z", "--repo", str(repo), "--worktrees", str(worktrees), "--logs", str(logs)], capsys)[0] == 0
    code, _, data = invoke(["push-setup", "zeta", "git@github.com:org/repo.git"], capsys)
    assert code == 0 and data["push_remote"] == "git@github.com:org/repo.git"
    assert Path(data["deploy_key"]).stat().st_mode & 0o777 == 0o600
    assert subprocess.run(["git", "-C", str(repo), "config", "core.sshCommand"], capture_output=True, text=True).stdout.strip().startswith("ssh -i")
    assert invoke(["set-importance", "zeta", "urgent"], capsys)[0] == 1
    assert invoke(["set-importance", "zeta", "high"], capsys)[2]["importance"] == "high"
    assert invoke(["archive", "zeta"], capsys)[2]["status"] == "archived"
    assert invoke(["create", "--id", "alpha", "--name", "A", "--empty", "--root-base", str(tmp_path / "projects")], capsys)[0] == 0
    assert [item["id"] for item in invoke(["list"], capsys)[2]] == ["alpha", "zeta"]


@pytest.mark.parametrize("files, expected", [
    ({"package.json": '{"scripts":{"test":"x"}}', "tests": None}, "npm test"),
    ({"package.json": "bad", "tests": None}, "python3 -m pytest -q"),
    ({"Cargo.toml": ""}, "cargo test"), ({"go.mod": "module x"}, "go test ./..."),
    ({"Makefile": "test:; echo ok"}, "make test"), ({}, ""),
])
def test_detect_gate(tmp_path, files, expected):
    for name, content in files.items():
        path = tmp_path / name
        if content is None: path.mkdir()
        else: path.write_text(content)
    assert sid_project.detect_gate(tmp_path) == expected


def test_package_gate_beats_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text('{"scripts":{"test":"x"}}')
    assert sid_project.detect_gate(tmp_path) == "npm test"


# --- delete ------------------------------------------------------------------------

class DeleteRedis(FakeRedis):
    """FakeRedis plus the list/scan/delete calls delete() uses."""

    def __init__(self):
        super().__init__()
        self.lists, self.strings = {}, {}

    def type(self, key):
        return "hash" if key in self.hashes else "string" if key in self.strings else "none"

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return iter(sorted(k for k in [*self.hashes, *self.strings] if k.startswith(prefix)))

    def exists(self, key): return key in self.hashes or key in self.strings
    def delete(self, key): self.hashes.pop(key, None); self.strings.pop(key, None); self.lists.pop(key, None)
    def srem(self, key, value): self.sets.get(key, set()).discard(value)
    def lrange(self, key, start, end): return list(self.lists.get(key, []))

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        self.lists[key] = [item for item in items if item != value]


@pytest.fixture
def deletable(monkeypatch, tmp_path):
    fake = DeleteRedis()
    monkeypatch.setattr(sid_project, "get_redis", lambda: fake)
    base = tmp_path / "projects"
    monkeypatch.setattr(sid_project, "PROJECTS_BASE", base)
    root = base / "shop"
    for sub in ("repo", "worktrees", "logs"):
        (root / sub).mkdir(parents=True)
    (root / "deploy_key").write_text("secret")
    fake.hashes["sid:projects:shop"] = {"id": "shop", "root": str(root), "repo": str(root / "repo"),
                                        "worktrees": str(root / "worktrees"), "logs": str(root / "logs"),
                                        "status": "active", "push_remote": "git@github.com:me/shop.git"}
    fake.sets["sid:projects"] = {"shop", "other"}
    fake.hashes["sid:projects:other"] = {"id": "other", "status": "active"}
    fake.hashes["sid:jobs:b1"] = {"id": "b1", "project_id": "shop", "status": "queued"}
    fake.hashes["sid:jobs:rv1"] = {"id": "rv1", "builder_job_id": "b1", "status": "queued"}
    fake.hashes["sid:jobs:x1"] = {"id": "x1", "project_id": "other", "status": "queued"}
    fake.hashes["sid:jobs:s1"] = {"id": "s1", "status": "queued"}  # SID
    fake.hashes["sid:goals:g1"] = {"id": "g1", "project_id": "shop", "status": "running"}
    fake.hashes["sid:goals:g2"] = {"id": "g2", "project_id": "other"}
    fake.hashes["sid:project-stats:shop"] = {"remaining_effort": "3"}
    fake.lists["sid:jobs"] = [json.dumps({"id": "b1", "project_id": "shop"}), json.dumps({"id": "rv1"}),
                              json.dumps({"id": "x1", "project_id": "other"}), json.dumps({"id": "s1"})]
    fake.lists["sid:goals"] = [json.dumps({"id": "g1", "project_id": "shop"}), json.dumps({"id": "g2", "project_id": "other"})]
    return fake, root


def test_delete_wipes_the_project_and_only_the_project(deletable, capsys):
    fake, root = deletable
    code, _, out = invoke(["delete", "shop", "--confirm", "shop"], capsys)
    assert code == 0 and out["status"] == "deleted"
    assert (out["jobs_removed"], out["goals_removed"]) == (2, 1)
    assert not root.exists() and out["removed_path"] == str(root)
    assert "deploy key" in out["note"]
    assert "sid:projects:shop" not in fake.hashes and "shop" not in fake.sets["sid:projects"]
    assert not {"sid:jobs:b1", "sid:jobs:rv1", "sid:goals:g1", "sid:project-stats:shop"} & set(fake.hashes)
    assert [json.loads(x)["id"] for x in fake.lists["sid:jobs"]] == ["x1", "s1"]
    assert [json.loads(x)["id"] for x in fake.lists["sid:goals"]] == ["g2"]
    assert {"sid:jobs:x1", "sid:jobs:s1", "sid:goals:g2", "sid:projects:other"} <= set(fake.hashes)


def test_delete_refuses_sid_bad_confirmation_and_running_work(deletable, capsys):
    fake, root = deletable
    fake.hashes["sid:projects:sid"] = {"id": "sid"}
    assert invoke(["delete", "sid", "--confirm", "sid"], capsys)[0] == 1
    code, captured, _ = invoke(["delete", "shop", "--confirm", "shp"], capsys)
    assert code == 1 and "confirmation" in captured.err
    fake.hashes["sid:workers:sid-worker-03"] = {"job_id": "b1", "status": "working"}
    code, captured, _ = invoke(["delete", "shop", "--confirm", "shop"], capsys)
    assert code == 1 and "job b1" in captured.err and "Nothing was deleted" in captured.err
    assert root.exists() and fake.hashes["sid:projects:shop"]["status"] == "active"
    del fake.hashes["sid:workers:sid-worker-03"]
    fake.strings["sid:goals:g1:planning"] = "planner"
    code, captured, _ = invoke(["delete", "shop", "--confirm", "shop"], capsys)
    assert code == 1 and "goal g1" in captured.err and root.exists()


def test_delete_never_removes_directories_it_did_not_create(deletable, capsys, tmp_path):
    fake, root = deletable
    outside = tmp_path / "my-checkout"
    (outside / "repo").mkdir(parents=True)
    fake.hashes["sid:projects:shop"].update(root=str(tmp_path), repo=str(outside / "repo"))
    code, _, out = invoke(["delete", "shop", "--confirm", "shop"], capsys)
    assert code == 0 and "removed_path" not in out
    assert outside.exists() and tmp_path.exists() and root.exists()
    assert str(outside / "repo") in out["kept_paths"]


def test_delete_ignores_a_symlinked_project_root(deletable, capsys, tmp_path):
    fake, root = deletable
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = root.parent / "linked"
    link.symlink_to(target)
    fake.hashes["sid:projects:linked"] = {"id": "linked", "root": str(link), "repo": str(link / "repo"), "status": "active"}
    code, _, out = invoke(["delete", "linked", "--confirm", "linked"], capsys)
    assert code == 0 and "removed_path" not in out and (target / "keep.txt").exists()
