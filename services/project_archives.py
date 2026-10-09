"""Archives uploaded to a project, unpacked read-only for the goal assistant.

The assistant only has Read / Grep / Glob, so an uploaded .tar.gz or .zip
was opaque to it: in the NGINX-Manager trial it planned around the owner's
old suite without ever seeing it. Each archive committed on main (up to
MAX_ARCHIVES, each at most MAX_ARCHIVE_BYTES) is unpacked once into
<project root>/unpacked/<blob sha>/, which the project sandbox shows, and
handed to Claude with --add-dir.

Unpacking treats the archive as hostile: tar uses the "data" filter (no
absolute paths, no "..", no links leaving the folder, no devices, no
setuid bits), links are skipped altogether, zip entries are checked the
same way, and the total size and number of files are capped. Nothing in an
archive is ever run. Folders of archives no longer on main are removed.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

SUFFIXES = (".tar.gz", ".tgz", ".tar", ".tar.xz", ".tar.bz2", ".zip")
MAX_ARCHIVES = 5
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
MARKER = ".laika-unpacked"


class ArchiveError(Exception):
    pass


def base_dir(project):
    return Path(project.root) / "unpacked"


def on_main(project, branch=None, runner=subprocess.run):
    """[(path, blob sha, size)] of archives committed on main."""
    branch = branch or getattr(project, "default_branch", "main") or "main"
    listing = runner(["git", "ls-tree", "-r", "-l", "-z", f"refs/heads/{branch}"], cwd=str(project.repo),
                     capture_output=True, text=True)
    if listing.returncode:
        return []
    found = []
    for entry in listing.stdout.split("\0"):
        if not entry.strip():
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob" or not path.lower().endswith(SUFFIXES):
            continue
        size = int(parts[3]) if parts[3].isdigit() else 0
        if 0 < size <= MAX_ARCHIVE_BYTES:
            found.append((path, parts[2], size))
    return found[:MAX_ARCHIVES]


def _safe_member(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts and str(path) not in ("", ".")


def _unpack_tar(archive, dest):
    total = files = 0
    with tarfile.open(archive) as tar:
        members = []
        for member in tar:
            if not (member.isfile() or member.isdir()) or not _safe_member(member.name):
                continue  # links, devices, fifos and odd paths are skipped
            files += 1
            total += member.size
            if files > MAX_FILES or total > MAX_UNPACKED_BYTES:
                raise ArchiveError("archive too large to unpack")
            members.append(member)
        tar.extractall(dest, members=members, filter="data")


def _unpack_zip(archive, dest):
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = [info for info in zf.infolist() if _safe_member(info.filename)]
        if len(infos) > MAX_FILES:
            raise ArchiveError("archive too large to unpack")
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # a symlink stored in the zip
                continue
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise ArchiveError("archive too large to unpack")
            target = dest / PurePosixPath(info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def unpack(project, path, blob, runner=subprocess.run):
    """The folder holding this archive's contents (unpacked once per blob)."""
    dest = base_dir(project) / blob[:16]
    if (dest / MARKER).is_file():
        return dest
    base_dir(project).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".unpack-", dir=base_dir(project)))
    try:
        archive = work / "archive"
        with open(archive, "wb") as out:
            result = runner(["git", "cat-file", "blob", blob], cwd=str(project.repo), stdout=out)
        if result.returncode:
            raise ArchiveError("could not read the archive from the repository")
        content = work / "content"
        content.mkdir()
        if path.lower().endswith(".zip"):
            _unpack_zip(archive, content)
        else:
            _unpack_tar(archive, content)
        archive.unlink()
        (content / MARKER).write_text(f"{path}\n{blob}\n")
        if dest.exists():
            shutil.rmtree(dest)
        content.rename(dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prepare(project, runner=subprocess.run):
    """[(archive path on main, unpacked folder)]; problems are skipped."""
    copies, keep = [], set()
    for path, blob, _size in on_main(project, runner=runner):
        try:
            folder = unpack(project, path, blob, runner=runner)
        except (ArchiveError, tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            print(f"[archives] {project.id}: {path}: {exc}", flush=True)
            continue
        copies.append((path, folder))
        keep.add(folder.name)
    base = base_dir(project)
    if base.is_dir():
        for old in base.iterdir():
            if old.is_dir() and not old.is_symlink() and old.name not in keep and not old.name.startswith(".unpack-"):
                shutil.rmtree(old, ignore_errors=True)
    return copies


def note(copies):
    if not copies:
        return ""
    lines = "\n".join(f"- {path}: unpacked at {folder}" for path, folder in copies)
    return ("Uploaded archives in this project are unpacked for you, read-only (read them with Read, Grep and "
            f"Glob; never run anything from them):\n{lines}\n")
