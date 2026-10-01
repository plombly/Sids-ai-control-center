"""File operations for the project file browser, confined to one root.

Used by the API for a project's app data (apps/api/file_routes.py) and by
the host for its code (scripts/sid-project.py code-change, which commits the
result to main). Standard library only, so both can import it.

Every path is relative to the root; nothing may climb out ("..", absolute
paths, symlinks leading outside), and with forbid_git nothing may name or
create a .git entry. Existing files are never overwritten: copies, zips and
extractions get a free name ("a copy.txt", "site (2).zip").
"""

import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

OPS = ("mkdir", "rename", "move", "copy", "delete", "zip", "unzip")
MAX_UNZIP_BYTES = int(os.environ.get("SID_MAX_UNZIP_BYTES", str(2 * 1024 ** 3)))
MAX_UNZIP_FILES = 20000


class FileOpError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def parts_of(rel, forbid_git=False):
    rel = (rel or "").strip("/")
    if len(rel) > 400 or "\\" in rel or any(ord(c) < 32 for c in rel):
        raise FileOpError("Invalid path")
    parts = PurePosixPath(rel).parts if rel else ()
    if any(part in ("", ".", "..") for part in parts):
        raise FileOpError("Invalid path")
    if forbid_git and ".git" in parts:
        raise FileOpError("Not found", 404)
    return parts


def check_name(name, forbid_git=False):
    if (not name or len(name) > 255 or "/" in name or "\\" in name or name in (".", "..")
            or any(ord(c) < 32 for c in name) or (forbid_git and name == ".git")):
        raise FileOpError(f"Invalid name: {name!r}")
    return name


def resolve(root, rel, must_exist=True, forbid_git=False):
    """(path, normalized rel) inside root; refuses links that lead out."""
    root = Path(root)
    parts = parts_of(rel, forbid_git)
    target = root.joinpath(*parts)
    root_real = root.resolve()
    real = target.resolve()
    if real != root_real and root_real not in real.parents:
        raise FileOpError("Path leads outside the project", 403)
    if must_exist and not os.path.lexists(target):
        raise FileOpError("Not found", 404)
    return target, "/".join(parts)


def _rel(root, path):
    return Path(path).relative_to(root).as_posix()


def free_name(folder, name):
    """name, or "name (2)", "name (3)", ... (before the extension) if taken."""
    folder = Path(folder)
    if not os.path.lexists(folder / name):
        return name
    stem, dot, ext = name.partition(".") if not name.startswith(".") else (name, "", "")
    for n in range(2, 1000):
        candidate = f"{stem} ({n}){dot}{ext}"
        if not os.path.lexists(folder / candidate):
            return candidate
    raise FileOpError("No free name", 409)


def _not_root(rel):
    if not rel:
        raise FileOpError("Choose a file or folder, not the top level")


def _dest_folder(root, dest, forbid_git):
    folder, folder_rel = resolve(root, dest, must_exist=True, forbid_git=forbid_git)
    if folder.is_symlink() or not folder.is_dir():
        raise FileOpError("Destination is not a folder")
    return folder, folder_rel


def mkdir(root, path, forbid_git=False, keep_file=False):
    target, rel = resolve(root, path, must_exist=False, forbid_git=forbid_git)
    _not_root(rel)
    check_name(target.name, forbid_git)
    if os.path.lexists(target):
        raise FileOpError("Something with that name exists", 409)
    target.mkdir(parents=True)
    if keep_file:  # git does not track empty folders
        (target / ".gitkeep").write_text("")
    return {"path": rel}


def rename(root, path, new_name, forbid_git=False):
    source, rel = resolve(root, path, forbid_git=forbid_git)
    _not_root(rel)
    check_name(new_name, forbid_git)
    target = source.parent / new_name
    if os.path.lexists(target):
        raise FileOpError(f"{new_name} already exists", 409)
    source.rename(target)
    return {"from": rel, "path": _rel(root, target)}


def move(root, path, dest, forbid_git=False):
    source, rel = resolve(root, path, forbid_git=forbid_git)
    _not_root(rel)
    folder, folder_rel = _dest_folder(root, dest, forbid_git)
    if source.is_dir() and not source.is_symlink() and (folder == source or source.resolve() in folder.resolve().parents):
        raise FileOpError("Cannot move a folder into itself")
    target = folder / source.name
    if os.path.lexists(target):
        raise FileOpError(f"{source.name} already exists there", 409)
    shutil.move(str(source), str(target))
    return {"from": rel, "path": _rel(root, target)}


def copy(root, path, dest, forbid_git=False):
    source, rel = resolve(root, path, forbid_git=forbid_git)
    _not_root(rel)
    folder, _ = _dest_folder(root, dest, forbid_git)
    if source.is_dir() and not source.is_symlink() and (folder == source or source.resolve() in folder.resolve().parents):
        raise FileOpError("Cannot copy a folder into itself")
    target = folder / free_name(folder, source.name)
    if source.is_symlink():
        raise FileOpError("Links cannot be copied")
    if source.is_dir():
        # Links inside are copied as links (never followed out of the root).
        shutil.copytree(source, target, symlinks=True,
                        ignore=shutil.ignore_patterns(".git") if forbid_git else None)
    else:
        shutil.copy2(source, target)
    return {"from": rel, "path": _rel(root, target)}


def delete(root, path, forbid_git=False):
    target, rel = resolve(root, path, forbid_git=forbid_git)
    _not_root(rel)
    if target.is_symlink() or not target.is_dir():
        target.unlink()
    else:
        shutil.rmtree(target)
    return {"path": rel, "deleted": True}


def make_zip(root, path, forbid_git=False):
    source, rel = resolve(root, path, forbid_git=forbid_git)
    _not_root(rel)
    if source.is_symlink():
        raise FileOpError("Links cannot be zipped")
    folder = source.parent
    target = folder / free_name(folder, f"{source.name}.zip")
    tmp = folder / f".sid-zip-{os.getpid()}.tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            if source.is_dir():
                for current, dirs, files in os.walk(source, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(current, d))
                                     and not (forbid_git and d == ".git"))
                    for name in sorted(files):
                        full = os.path.join(current, name)
                        if os.path.islink(full) or not os.path.isfile(full):
                            continue
                        archive.write(full, os.path.join(source.name, os.path.relpath(full, source)))
            else:
                archive.write(source, source.name)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"from": rel, "path": _rel(root, target)}


def unzip(root, path, forbid_git=False):
    """Extract into a new folder next to the zip (named after it)."""
    source, rel = resolve(root, path, forbid_git=forbid_git)
    _not_root(rel)
    if source.is_symlink() or not source.is_file():
        raise FileOpError("Not a zip file")
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise FileOpError("Not a valid zip file")
    with archive:
        members = archive.infolist()
        if len(members) > MAX_UNZIP_FILES:
            raise FileOpError(f"Zip has more than {MAX_UNZIP_FILES} entries")
        if sum(m.file_size for m in members) > MAX_UNZIP_BYTES:
            raise FileOpError(f"Zip would unpack to more than {MAX_UNZIP_BYTES // 1024 ** 2} MB")
        base = source.name[:-4] if source.name.lower().endswith(".zip") else source.name
        folder = source.parent / free_name(source.parent, base or "unzipped")
        folder.mkdir()
        for member in members:
            name = member.filename.replace("\\", "/")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue  # never create links from an archive
            try:
                member_parts = parts_of(name, forbid_git)
            except FileOpError:
                shutil.rmtree(folder)
                raise FileOpError(f"Unsafe name in zip: {member.filename!r}")
            if not member_parts:
                continue
            target = folder.joinpath(*member_parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return {"from": rel, "path": _rel(root, folder)}


def apply(root, op, path, dest="", forbid_git=False, keep_file=False):
    if op == "mkdir":
        return mkdir(root, path, forbid_git, keep_file=keep_file)
    if op == "rename":
        return rename(root, path, dest, forbid_git)
    if op == "move":
        return move(root, path, dest, forbid_git)
    if op == "copy":
        return copy(root, path, dest, forbid_git)
    if op == "delete":
        return delete(root, path, forbid_git)
    if op == "zip":
        return make_zip(root, path, forbid_git)
    if op == "unzip":
        return unzip(root, path, forbid_git)
    raise FileOpError(f"Unknown operation: {op}")
