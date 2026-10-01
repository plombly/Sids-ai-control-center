"""apps/api/file_ops.py: file-browser operations confined to one root."""

import io
import sys
import zipfile

import pytest

from sid_testing import ROOT

sys.path.insert(0, str(ROOT / "apps/api"))
import file_ops  # noqa: E402
from file_ops import FileOpError  # noqa: E402


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "root"
    (base / "site" / "img").mkdir(parents=True)
    (base / "site" / "index.html").write_text("<h1>hi</h1>")
    (base / "site" / "img" / "a.png").write_bytes(b"png")
    (base / "notes.txt").write_text("n")
    (tmp_path / "outside.txt").write_text("SECRET")
    (base / "out").symlink_to(tmp_path)
    return base


def test_rename_move_copy_delete(root):
    assert file_ops.apply(root, "rename", "notes.txt", "todo.txt")["path"] == "todo.txt"
    assert file_ops.apply(root, "move", "todo.txt", "site")["path"] == "site/todo.txt"
    assert file_ops.apply(root, "copy", "site/todo.txt", "site")["path"] == "site/todo (2).txt"
    assert file_ops.apply(root, "copy", "site", "")["path"] == "site (2)"
    assert (root / "site (2)" / "img" / "a.png").read_bytes() == b"png"
    file_ops.apply(root, "delete", "site (2)")
    assert not (root / "site (2)").exists() and root.exists()
    assert file_ops.apply(root, "mkdir", "site/new")["path"] == "site/new"


def test_refusals(root):
    cases = [("rename", "site", "../x"), ("rename", "site", "img/x"), ("move", "site", "site/img"),
             ("copy", "site", "site"), ("delete", "", ""), ("move", "notes.txt", "out"),
             ("delete", "out/outside.txt", ""), ("rename", "notes.txt", "site"), ("move", "../x", "")]
    for op, path, dest in cases:
        with pytest.raises(FileOpError):
            file_ops.apply(root, op, path, dest)
    assert (root.parent / "outside.txt").read_text() == "SECRET"


def test_git_is_off_limits_in_code(root):
    (root / ".git").mkdir()
    for op, path, dest in [("delete", ".git", ""), ("rename", "notes.txt", ".git"), ("mkdir", ".git/hooks", "")]:
        with pytest.raises(FileOpError):
            file_ops.apply(root, op, path, dest, forbid_git=True)


def test_zip_and_unzip_round_trip(root):
    assert file_ops.apply(root, "zip", "site")["path"] == "site.zip"
    assert file_ops.apply(root, "zip", "site")["path"] == "site (2).zip"
    names = zipfile.ZipFile(root / "site.zip").namelist()
    assert "site/index.html" in names and "site/img/a.png" in names
    assert file_ops.apply(root, "unzip", "site.zip")["path"] == "site (2)"
    assert (root / "site (2)" / "site" / "img" / "a.png").read_bytes() == b"png"


def _zip_with(root, entries, symlink=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink[0])
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, symlink[1])
    (root / "evil.zip").write_bytes(buffer.getvalue())


@pytest.mark.parametrize("name", ["../escape.txt", "a/../../escape.txt"])
def test_unzip_refuses_zip_slip(root, name):
    _zip_with(root, [("ok.txt", "ok"), (name, "x")])
    with pytest.raises(FileOpError, match="Unsafe"):
        file_ops.apply(root, "unzip", "evil.zip")
    assert not (root.parent / "escape.txt").exists() and not (root / "evil").exists()


def test_unzip_skips_links_and_git_in_code(root):
    _zip_with(root, [("ok.txt", "ok")], symlink=("link", "/etc/passwd"))
    out = file_ops.apply(root, "unzip", "evil.zip")["path"]
    assert (root / out / "ok.txt").exists() and not (root / out / "link").exists()
    _zip_with(root, [(".git/hooks/post-commit", "x")])
    with pytest.raises(FileOpError):
        file_ops.apply(root, "unzip", "evil.zip", forbid_git=True)


def test_unzip_size_limit(root, monkeypatch):
    monkeypatch.setattr(file_ops, "MAX_UNZIP_BYTES", 10)
    _zip_with(root, [("big.txt", "x" * 100)])
    with pytest.raises(FileOpError, match="unpack"):
        file_ops.apply(root, "unzip", "evil.zip")


def test_unzip_absolute_names_stay_inside(root):
    _zip_with(root, [("/abs.txt", "x")])
    out = file_ops.apply(root, "unzip", "evil.zip")["path"]
    assert (root / out / "abs.txt").read_text() == "x"
