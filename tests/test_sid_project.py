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
