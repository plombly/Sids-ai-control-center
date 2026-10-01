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
def keys_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(sid_project, "KEYS_BASE", tmp_path / "keys")


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
    monkeypatch.setattr(sid_project, "SYSTEMCTL", "true")
    monkeypatch.setattr(sid_project, "KEYS_BASE", tmp_path / "keys")
    monkeypatch.setattr(sid_project.sid_projects, "DATA_BASE", tmp_path / "project-data")
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


def test_delete_also_removes_app_data_and_the_key_dir(deletable, capsys, tmp_path):
    fake, root = deletable
    data = tmp_path / "project-data" / "shop"
    data.mkdir(parents=True)
    (data / "app.db").write_text("x")
    keys = tmp_path / "keys" / "shop"
    keys.mkdir(parents=True)
    code, _, out = invoke(["delete", "shop", "--confirm", "shop"], capsys)
    assert code == 0 and not data.exists() and not keys.exists()
    assert (tmp_path / "project-data").exists()


def test_new_deploy_keys_live_outside_the_project_directory(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    key = sid_project.make_key(root, "shop")
    assert key == tmp_path / "keys" / "shop" / "deploy_key" and key.exists()
    assert not (root / "deploy_key").exists()


# --- commit-upload: a file from the dashboard onto main ---------------------------------

class UploadRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.strings = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    def eval(self, script, numkeys, key, token):
        if self.strings.get(key) == token:
            del self.strings[key]


@pytest.fixture
def uploadable(monkeypatch, tmp_path):
    fake = UploadRedis()
    monkeypatch.setattr(sid_project, "get_redis", lambda: fake)
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(sid_project, "UPLOADS_BASE", uploads)
    repo = tmp_path / "shop" / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    fake.hashes["sid:projects:shop"] = {"id": "shop", "repo": str(repo), "default_branch": "main", "status": "active"}

    def stage(upload_id, content=b"data"):
        (uploads / upload_id).mkdir(parents=True)
        (uploads / upload_id / "file").write_bytes(content)
    return fake, repo, uploads, stage


def test_upload_is_committed_to_main_and_the_staging_removed(uploadable, capsys):
    fake, repo, uploads, stage = uploadable
    stage("upload-0001", b"\x89PNG")
    code, captured, out = invoke(["commit-upload", "shop", "--path", "static/img/logo.png", "--upload", "upload-0001"], capsys)
    assert code == 0, captured.err
    assert out["status"] == "committed" and (repo / "static/img/logo.png").read_bytes() == b"\x89PNG"
    log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an|%s"], capture_output=True, text=True).stdout
    assert log.strip() == "SID operator|Upload static/img/logo.png from the dashboard"
    assert not (uploads / "upload-0001").exists() and not fake.strings


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", ".git/hooks/post-commit", "a/../../x", "a/.git/config", "", "a\nb"])
def test_upload_paths_are_confined_to_the_repository(uploadable, capsys, path):
    fake, repo, uploads, stage = uploadable
    stage("upload-0002")
    code, captured, _ = invoke(["commit-upload", "shop", "--path", path, "--upload", "upload-0002"], capsys)
    assert code == 1 and "invalid path" in captured.err
    assert not (uploads / "upload-0002").exists()  # staging is always cleaned up


def test_upload_refuses_symlinked_parents_dirty_main_and_a_held_lock(uploadable, capsys, tmp_path):
    fake, repo, uploads, stage = uploadable
    (repo / "link").symlink_to(tmp_path)
    subprocess.run(["git", "-C", str(repo), "add", "link"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "link"], check=True)
    stage("upload-0003")
    code, captured, _ = invoke(["commit-upload", "shop", "--path", "link/x.txt", "--upload", "upload-0003"], capsys)
    assert code == 1 and "link" in captured.err and not (tmp_path / "x.txt").exists()
    (repo / "dirty.txt").write_text("x")
    stage("upload-0004")
    code, captured, _ = invoke(["commit-upload", "shop", "--path", "a.txt", "--upload", "upload-0004"], capsys)
    assert code == 1 and "uncommitted" in captured.err
    (repo / "dirty.txt").unlink()
    fake.strings["sid:approval-lock:shop"] = "someone"
    stage("upload-0005")
    code, captured, _ = invoke(["commit-upload", "shop", "--path", "a.txt", "--upload", "upload-0005"], capsys)
    assert code == 1 and "approval" in captured.err and fake.strings["sid:approval-lock:shop"] == "someone"
    assert invoke(["commit-upload", "sid", "--path", "a.txt", "--upload", "upload-0006"], capsys)[0] == 1


def test_code_change_commits_each_operation(uploadable, capsys):
    fake, repo, uploads, stage = uploadable
    steps = [(["--op", "mkdir", "--path", "docs"], "Create folder docs"),
             (["--op", "rename", "--path", "README.md", "--dest", "INTRO.md"], "Rename README.md to INTRO.md"),
             (["--op", "move", "--path", "INTRO.md", "--dest", "docs"], "Move INTRO.md to docs/INTRO.md"),
             (["--op", "copy", "--path", "docs/INTRO.md", "--dest", ""], "Copy docs/INTRO.md to INTRO.md"),
             (["--op", "zip", "--path", "docs"], "Zip docs into docs.zip"),
             (["--op", "unzip", "--path", "docs.zip"], "Unzip docs.zip into docs (2)"),
             (["--op", "delete", "--path", "docs.zip"], "Delete docs.zip")]
    for extra, message in steps:
        code, captured, out = invoke(["code-change", "shop", *extra], capsys)
        assert code == 0 and out["status"] == "committed", captured.err
        subject = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
        assert subject.strip() == f"{message} from the dashboard"
    assert (repo / "docs (2)" / "docs" / "INTRO.md").read_text() == "hi\n"
    assert not fake.strings  # lock released
    code, captured, _ = invoke(["code-change", "shop", "--op", "delete", "--path", ".git"], capsys)
    assert code == 1


def test_push_setup_refuses_sid(capsys):
    code, captured, _ = invoke(["push-setup", "sid", "git@github.com:me/x.git"], capsys)
    assert code == 1 and "by hand" in captured.err


# --- code-batch: several items, between code and app data, one commit ------------------

@pytest.fixture
def batchable(uploadable, monkeypatch, tmp_path):
    fake, repo, uploads, stage = uploadable
    monkeypatch.setattr(sid_project.sid_projects, "DATA_BASE", tmp_path / "project-data")
    data = tmp_path / "project-data" / "shop"
    (data / "img").mkdir(parents=True)
    (data / "img" / "logo.png").write_bytes(b"png")
    (data / "notes.txt").write_text("notes")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "docs"], check=True)
    return fake, repo, data


def batch(capsys, **spec):
    return invoke(["code-batch", "shop", f"--spec={json.dumps(spec)}"], capsys)


def subject(repo):
    return subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()


def test_data_to_code_move_removes_originals_only_after_the_commit(batchable, capsys):
    fake, repo, data = batchable
    code, captured, out = batch(capsys, op="move", from_area="data", to_area="code", paths=["img", "notes.txt"], dest="docs")
    assert code == 0, captured.err
    assert (repo / "docs" / "img" / "logo.png").read_bytes() == b"png" and (repo / "docs" / "notes.txt").exists()
    assert not (data / "img").exists() and not (data / "notes.txt").exists()
    assert subject(repo) == "Move 2 items from app data into docs/ from the dashboard"


def test_code_to_data_move_and_multi_delete_and_zip(batchable, capsys):
    fake, repo, data = batchable
    assert batch(capsys, op="move", from_area="code", to_area="data", paths=["README.md"], dest="")[0] == 0
    assert (data / "README.md").read_text() == "hi\n" and not (repo / "README.md").exists()
    assert subject(repo) == "Move 1 item to app data from the dashboard"
    assert batch(capsys, op="zip", from_area="code", paths=["docs"], dest="", name="bundle")[0] == 0
    assert (repo / "bundle.zip").exists() and subject(repo) == "Zip 1 item into bundle.zip from the dashboard"
    assert batch(capsys, op="delete", from_area="code", paths=["docs", "bundle.zip"])[0] == 0
    assert subject(repo) == "Delete 2 items from the dashboard"


def test_conflicts_are_reported_and_answers_are_applied(batchable, capsys):
    fake, repo, data = batchable
    (data / "README.md").write_text("from data")
    code, captured, _ = batch(capsys, op="copy", from_area="data", to_area="code", paths=["README.md"], dest="")
    assert code == 1 and 'conflict: ["README.md"]' in captured.err
    code, _, out = batch(capsys, op="copy", from_area="data", to_area="code", paths=["README.md"], dest="",
                         resolutions={"README.md": "keep"})
    assert code == 0 and (repo / "README (2).md").read_text() == "from data"


def test_a_failure_part_way_leaves_main_exactly_as_it_was(batchable, capsys, monkeypatch):
    fake, repo, data = batchable
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout

    def broken(*args, **kwargs):
        raise sid_project.file_ops.FileOpError("disk full")
    monkeypatch.setattr(sid_project.file_ops, "delete_many", broken)
    code, captured, _ = batch(capsys, op="move", from_area="code", to_area="data", paths=["docs"], dest="")
    assert code == 1 and "disk full" in captured.err
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
    assert (repo / "docs" / "guide.md").exists() and not fake.strings


def test_batches_that_do_not_touch_code_are_refused(batchable, capsys):
    code, captured, _ = batch(capsys, op="copy", from_area="code", to_area="data", paths=["README.md"], dest="")
    assert code == 1 and "directly" in captured.err


def test_upload_conflict_choices(uploadable, capsys):
    fake, repo, uploads, stage = uploadable
    stage("upload-c001", b"new")
    code, captured, _ = invoke(["commit-upload", "shop", "--path=README.md", "--upload=upload-c001"], capsys)
    assert code == 1 and "conflict" in captured.err and (repo / "README.md").read_text() == "hi\n"
    stage("upload-c002", b"new")
    assert invoke(["commit-upload", "shop", "--path=README.md", "--upload=upload-c002", "--on-conflict=keep"], capsys)[0] == 0
    assert (repo / "README (2).md").read_bytes() == b"new"
    stage("upload-c003", b"newer")
    assert invoke(["commit-upload", "shop", "--path=README.md", "--upload=upload-c003", "--on-conflict=overwrite"], capsys)[0] == 0
    assert (repo / "README.md").read_bytes() == b"newer"
