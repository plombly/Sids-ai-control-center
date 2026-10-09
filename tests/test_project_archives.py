"""services/project_archives.py: uploaded archives unpacked safely for the goal assistant."""

import io
import subprocess
import tarfile
import zipfile
from types import SimpleNamespace

from laika_testing import ROOT, load_module

archives = load_module(ROOT / "services/project_archives.py", "project_archives_test")


def repo_with(tmp_path, files):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for name, data in files.items():
        (repo / name).write_bytes(data)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    return SimpleNamespace(id="shop", root=tmp_path, repo=repo, default_branch="main")


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
    return buffer.getvalue()


def test_a_tarball_is_unpacked_without_escapes_and_reused(tmp_path):
    data = tar_bytes([("suite/cloudflare.py", "file", b"API = 1\n"), ("../evil.py", "file", b"x"),
                      ("/etc/evil", "file", b"x"), ("suite/link", "symlink", "/etc/passwd")])
    project = repo_with(tmp_path, {"old-suite.tar.gz": data, "README.md": b"hi"})
    copies = archives.prepare(project)
    assert [path for path, _ in copies] == ["old-suite.tar.gz"]
    folder = copies[0][1]
    assert (folder / "suite/cloudflare.py").read_text() == "API = 1\n"
    assert not (folder / "suite/link").exists() and not (tmp_path / "evil.py").exists()
    assert not any(p.name == "evil" for p in folder.rglob("*"))
    assert archives.prepare(project)[0][1] == folder                       # unpacked once per blob
    note = archives.note(copies)
    assert "old-suite.tar.gz: unpacked at" in note and "never run anything" in note


def test_zip_slip_and_links_are_refused_and_old_folders_removed(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("app/main.py", "print(1)\n")
        zf.writestr("../../escape.txt", "x")
        link = zipfile.ZipInfo("app/link")
        link.external_attr = 0o120777 << 16
        zf.writestr(link, "/etc/passwd")
    project = repo_with(tmp_path, {"code.zip": buffer.getvalue()})
    stale = archives.base_dir(project) / "0123456789abcdef"
    stale.mkdir(parents=True)
    folder = archives.prepare(project)[0][1]
    assert (folder / "app/main.py").is_file() and not (folder / "app/link").exists()
    assert not (tmp_path / "escape.txt").exists() and not stale.exists()


def test_oversized_archives_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_UNPACKED_BYTES", 10)
    project = repo_with(tmp_path, {"big.tar.gz": tar_bytes([("a.txt", "file", b"x" * 100)])})
    assert archives.prepare(project) == []
