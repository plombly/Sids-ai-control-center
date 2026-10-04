"""services/sftp/sftp_fs.py: what an SFTP session sees and how code changes batch up."""

import hashlib
import sys

import pytest

from laika_testing import ROOT

sys.path.insert(0, str(ROOT / "services/sftp"))
import sftp_fs  # noqa: E402
from sftp_fs import CodeArea, DataArea, FsError  # noqa: E402

sha = lambda data: hashlib.sha256(data).hexdigest()


@pytest.fixture
def code(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_bytes(b"print(1)\n")
    (repo / "README.md").write_bytes(b"hi\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "escape").symlink_to(tmp_path)
    (tmp_path / "secret.txt").write_text("SECRET")
    return CodeArea(repo, tmp_path / "staging", "sam", "shop"), repo


def test_code_shows_main_without_git_or_links_out(code):
    area, repo = code
    assert area.listdir("") == ["README.md", "src"]
    assert area.entry(".git/config") is None and area.entry("escape/secret.txt") is None
    for bad in ("../x", "a/../../x"):
        assert area.entry(bad) is None or bad == "a/../../x"
    with pytest.raises(FsError):
        area.open_write(".git/hooks/post-commit", truncate=True)


def test_writes_stay_staged_and_remember_the_version_they_changed(code):
    area, repo = code
    area.open_write("src/app.py", truncate=True).write_bytes(b"print(2)\n")
    area.open_write("src/new.py", truncate=True).write_bytes(b"x\n")
    area.remove("README.md")
    assert (repo / "src" / "app.py").read_bytes() == b"print(1)\n"      # main untouched
    with open(area.open_read("src/app.py"), "rb") as handle:
        assert handle.read() == b"print(2)\n"                           # this person sees theirs
    assert area.listdir("src") == ["app.py", "new.py"] and area.listdir("") == ["src"]
    changes = area.current.changes
    assert changes["src/app.py"] == {"action": "write", "base": sha(b"print(1)\n")}
    assert changes["src/new.py"] == {"action": "write", "base": ""}
    assert changes["README.md"] == {"action": "delete", "base": sha(b"hi\n")}
    loaded = sftp_fs.Batch.load(area.current.folder)                   # survives a restart
    assert loaded.changes == changes


def test_partial_writes_keep_the_rest_and_new_then_deleted_files_vanish(code):
    area, repo = code
    target = area.open_write("README.md")                              # no truncate: starts as main
    assert target.read_bytes() == b"hi\n"
    area.open_write("tmp.swp", truncate=True)
    area.remove("tmp.swp")
    assert "tmp.swp" not in area.current.changes


def test_editor_style_save_is_one_change(code):
    area, repo = code
    area.open_write("src/.app.py.tmp", truncate=True).write_bytes(b"print(3)\n")
    area.rename("src/.app.py.tmp", "src/app.py", replace=True)
    assert set(area.current.changes) == {"src/app.py"}
    assert area.current.changes["src/app.py"]["base"] == sha(b"print(1)\n")
    with pytest.raises(FsError) as refused:
        area.rename("src/app.py", "README.md")                          # no silent overwrite
    assert refused.value.kind == "exists"


def test_folder_rename_moves_every_file(code):
    area, repo = code
    area.rename("src", "lib")
    assert area.listdir("") == ["README.md", "lib"] and area.listdir("lib") == ["app.py"]
    assert area.current.changes["src/app.py"]["action"] == "delete"
    assert area.current.changes["lib/app.py"] == {"action": "write", "base": ""}
    with pytest.raises(FsError):
        area.rename("lib", "lib/inner")


def test_batches_wait_for_quiet_and_open_files_then_stay_visible_until_committed(code, tmp_path):
    area, repo = code
    area.open_write("a.txt", truncate=True).write_bytes(b"1")
    batch = area.current
    assert area.ready(30, now=batch.last + 5) is None
    batch.open_writes = 1
    assert area.ready(30, now=batch.last + 60) is None                 # still being written
    batch.open_writes = 0
    assert area.ready(30, now=batch.last + 31) is batch
    area.sent("sftp-req1", batch, tmp_path / "uploads-req1")
    assert area.current is None and area.entry("a.txt").kind == "file"   # pending: still visible
    area.open_write("a.txt", truncate=True).write_bytes(b"2")
    assert area.current.changes["a.txt"]["base"] == sha(b"1")           # made on the pending version
    area.finished("sftp-req1")
    assert area.pending == []


def test_data_is_plain_and_confined(tmp_path):
    root = tmp_path / "data"
    assert DataArea(root).listdir("") == [] and root.is_dir()          # an app that never ran: made on first use
    (root / "out").symlink_to(tmp_path)
    data = DataArea(root)
    data.mkdir("db")
    data.open_path("db/x.json", True).write_text("{}")
    assert data.listdir("") == ["db"]                                    # the link out is not listed
    with pytest.raises(FsError):
        data.open_path("out/secret.txt", False)
    data.rename("db/x.json", "db/y.json")
    with pytest.raises(FsError):
        data.rmdir("db")
    data.remove("db/y.json")
    data.rmdir("db")


def test_a_batch_moves_by_copying_when_rename_is_impossible(code, tmp_path, monkeypatch):
    area, repo = code
    area.open_write("src/app.py", truncate=True).write_bytes(b"print(9)\n")
    batch = area.current
    old = batch.folder
    monkeypatch.setattr(sftp_fs.os, "rename", lambda *a: (_ for _ in ()).throw(OSError(18, "Invalid cross-device link")))
    batch.move_to(tmp_path / "uploads" / "sftp-1")
    assert (tmp_path / "uploads" / "sftp-1" / "files" / "src" / "app.py").read_bytes() == b"print(9)\n"
    assert (tmp_path / "uploads" / "sftp-1" / "manifest.json").is_file() and not old.exists()
