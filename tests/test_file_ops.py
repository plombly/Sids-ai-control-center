"""apps/api/file_ops.py: file-browser operations confined to one root."""

import io
import sys
import zipfile

import pytest

from laika_testing import ROOT

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


# --- batches -----------------------------------------------------------------------------

@pytest.fixture
def two(tmp_path):
    code, data = tmp_path / "code", tmp_path / "data"
    for base in (code, data):
        (base / "docs").mkdir(parents=True)
    (code / "a.txt").write_text("code a")
    (code / "b.txt").write_text("code b")
    (code / "lib").mkdir()
    (code / "lib" / "x.js").write_text("x")
    (data / "a.txt").write_text("data a")
    (data / "docs" / "a.txt").write_text("docs a")
    return code, data


def test_unanswered_conflicts_change_nothing_and_list_every_name(two):
    code, data = two
    with pytest.raises(file_ops.Conflict) as caught:
        file_ops.transfer(code, ["a.txt", "b.txt", "lib"], data, "", "copy")
    assert caught.value.conflicts == ["a.txt"] and caught.value.status == 409
    assert not (data / "b.txt").exists() and (data / "a.txt").read_text() == "data a"


@pytest.mark.parametrize("choice,expect", [("overwrite", ("code a", None)), ("skip", ("data a", None)),
                                           ("keep", ("data a", "code a"))])
def test_each_conflict_choice(two, choice, expect):
    code, data = two
    result = file_ops.transfer(code, ["a.txt", "b.txt"], data, "", "copy", resolutions={"a.txt": choice})
    assert (data / "a.txt").read_text() == expect[0]
    assert ((data / "a (2).txt").read_text() if (data / "a (2).txt").exists() else None) == expect[1]
    assert (data / "b.txt").read_text() == "code b"
    assert ("a.txt" in result["skipped"]) == (choice == "skip")


def test_apply_to_all_default_and_keep_numbering(two):
    code, data = two
    (data / "a (2).txt").write_text("taken")
    file_ops.transfer(code, ["a.txt"], data, "", "copy", default="keep")
    assert (data / "a (3).txt").read_text() == "code a"


def test_move_between_roots_and_into_a_folder(two):
    code, data = two
    result = file_ops.transfer(data, ["a.txt"], code, "docs", "move")
    assert result["done"] == [{"from": "a.txt", "path": "docs/a.txt"}]
    assert not (data / "a.txt").exists() and (code / "docs" / "a.txt").read_text() == "data a"


def test_batch_traps(two):
    code, data = two
    with pytest.raises(FileOpError, match="inside itself"):
        file_ops.transfer(code, ["lib"], code, "lib", "move")
    (code / "docs" / "lib").mkdir()
    with pytest.raises(FileOpError, match="both named"):
        file_ops.transfer(code, ["lib", "docs/lib"], data, "", "copy")
    # moving onto itself is a no-op, copying onto itself with "keep" makes a copy
    assert file_ops.transfer(code, ["a.txt"], code, "", "move")["skipped"] == ["a.txt"]
    assert file_ops.transfer(code, ["a.txt"], code, "", "copy", default="keep")["done"][0]["path"] == "a (2).txt"
    with pytest.raises(FileOpError):
        file_ops.transfer(code, [".git"], data, "", "copy", src_git=True)


def test_overwriting_a_folder_that_holds_the_source_is_refused(two):
    code, data = two
    (code / "docs" / "docs").mkdir()
    with pytest.raises(FileOpError, match="contains"):
        file_ops.transfer(code, ["docs/docs"], code, "", "move", default="overwrite")
    assert (code / "docs" / "docs").is_dir()


def test_zip_many_and_delete_many(two):
    code, _ = two
    result = file_ops.zip_many(code, ["a.txt", "lib"], "", "Archive")
    assert result["path"] == "Archive.zip"
    assert sorted(zipfile.ZipFile(code / "Archive.zip").namelist()) == ["a.txt", "lib/x.js"]
    with pytest.raises(file_ops.Conflict):
        file_ops.zip_many(code, ["b.txt"], "", "Archive.zip")
    assert file_ops.zip_many(code, ["b.txt"], "", "Archive.zip", resolution="keep")["path"] == "Archive (2).zip"
    file_ops.delete_many(code, ["a.txt", "lib"])
    assert not (code / "a.txt").exists() and not (code / "lib").exists()
    with pytest.raises(FileOpError):
        file_ops.delete_many(code, ["b.txt", "../x"])
    assert (code / "b.txt").exists()  # nothing deleted when any item is invalid


def test_rename_conflict_choices(two):
    code, _ = two
    with pytest.raises(file_ops.Conflict):
        file_ops.rename(code, "a.txt", "b.txt")
    assert file_ops.rename(code, "a.txt", "b.txt", resolution="keep")["path"] == "b (2).txt"
    file_ops.rename(code, "b (2).txt", "b.txt", resolution="overwrite")
    assert (code / "b.txt").read_text() == "code a" and not (code / "b (2).txt").exists()
