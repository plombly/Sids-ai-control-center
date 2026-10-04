"""The files an SFTP session sees (services/sftp/laika_sftp.py), without SSH.

    /                       the projects this person may see
    /<project>/code/...     main, plus this person's changes not committed yet
    /<project>/data/...     the app's data folder (changes are immediate)

Code is never written in place. Each person's changes to a project collect
in a Batch (a staging folder: files/<path> + manifest.json) on top of main;
the service commits a batch as one commit when it has been quiet for a
while (scripts/laika-project.py sftp-commit, through the operator service).
Until a batch is committed it stays visible to its owner as a "pending"
layer, so their files never appear to jump back. Each change remembers the
SHA-256 of the version it was made on ("base"): the host refuses to apply a
change whose file moved on main in the meantime and keeps that version as a
conflict copy instead.

Paths are POSIX strings relative to an area root ("" is the root). Nothing
here follows a link out of its root, and .git is invisible.
"""

import hashlib
import json
import os
import posixpath
import shutil
import stat
import time
import uuid
from pathlib import Path

MAX_RENAME_FILES = 2000


class FsError(Exception):
    """kind: missing | denied | exists | notempty | notdir | isdir | failure | unsupported"""

    def __init__(self, kind, message=""):
        super().__init__(message or kind)
        self.kind = kind


def clean(rel):
    """A relative path inside an area, or FsError."""
    rel = posixpath.normpath("/" + (rel or "")).lstrip("/")
    rel = "" if rel == "." else rel
    parts = rel.split("/") if rel else []
    if any(part in ("", ".", "..") or "\0" in part for part in parts) or len(rel) > 1024:
        raise FsError("missing", "invalid path")
    return rel


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(root, path):
    """path (possibly through links) stays inside root."""
    root_real = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real + os.sep)


class Entry:
    def __init__(self, kind, size=0, mtime=0.0, path=None):
        self.kind, self.size, self.mtime, self.path = kind, size, mtime, path  # kind: file | dir

    @classmethod
    def of(cls, path):
        info = os.stat(path)
        return cls("dir" if stat.S_ISDIR(info.st_mode) else "file", info.st_size, info.st_mtime, path)


# --- the app's data folder: plain files ----------------------------------------------------------

class DataArea:
    def __init__(self, root):
        self.root = Path(root)

    def real(self, rel, must_exist=True):
        rel = clean(rel)
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)  # an app that never ran has no data folder yet
        path = self.root / rel if rel else self.root
        if rel and not _inside(self.root, path.parent):
            raise FsError("denied", "path leads outside the project")
        if os.path.lexists(path) and not _inside(self.root, path):
            raise FsError("denied", "link leads outside the project")
        if must_exist and not os.path.lexists(path):
            raise FsError("missing")
        return path

    def entry(self, rel):
        path = self.real(rel)
        return Entry.of(path)

    def listdir(self, rel):
        path = self.real(rel)
        if not path.is_dir():
            raise FsError("notdir")
        return sorted(name for name in os.listdir(path) if _inside(self.root, path / name))

    def open_path(self, rel, write):
        path = self.real(rel, must_exist=not write)
        if path.is_dir():
            raise FsError("isdir")
        if write and not path.parent.is_dir():
            raise FsError("missing", "no such folder")
        return path

    def remove(self, rel):
        path = self.real(rel)
        if path.is_dir() and not path.is_symlink():
            raise FsError("isdir")
        path.unlink()

    def mkdir(self, rel):
        path = self.real(rel, must_exist=False)
        if os.path.lexists(path):
            raise FsError("exists")
        path.mkdir()

    def rmdir(self, rel):
        path = self.real(rel)
        if not clean(rel):
            raise FsError("denied", "the data folder itself stays")
        if any(path.iterdir()):
            raise FsError("notempty")
        path.rmdir()

    def rename(self, old, new, replace=False):
        source, dest = self.real(old), self.real(new, must_exist=False)
        if os.path.lexists(dest) and not replace:
            raise FsError("exists")
        os.replace(source, dest) if replace else os.rename(source, dest)


# --- code: main + this person's batches -----------------------------------------------------------

class Batch:
    """One person's uncommitted code changes to one project."""

    def __init__(self, folder, user="", project=""):
        self.folder = Path(folder)
        self.user, self.project = user, project
        self.changes = {}  # path -> {"action": "write" | "delete", "base": sha256 or ""}
        self.dirs = set()  # folders made here (git keeps no empty folders)
        self.last = time.time()
        self.open_writes = 0

    def file(self, rel):
        return self.folder / "files" / rel

    def save(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        body = {"user": self.user, "project": self.project, "dirs": sorted(self.dirs),
                "changes": [{"path": path, **change} for path, change in sorted(self.changes.items())]}
        temp = self.folder / ".manifest.tmp"
        temp.write_text(json.dumps(body, indent=1))
        os.replace(temp, self.folder / "manifest.json")

    @classmethod
    def load(cls, folder):
        body = json.loads((Path(folder) / "manifest.json").read_text())
        batch = cls(folder, body.get("user", ""), body.get("project", ""))
        batch.changes = {item["path"]: {"action": item["action"], "base": item.get("base", "")}
                         for item in body.get("changes", [])}
        batch.dirs = set(body.get("dirs", []))
        return batch

    def has_under(self, rel):
        prefix = rel + "/" if rel else ""
        return any(path.startswith(prefix) and change["action"] == "write" for path, change in self.changes.items()) \
            or any(folder == rel or folder.startswith(prefix) for folder in self.dirs)

    def move_to(self, folder):
        folder = Path(folder)
        try:
            os.rename(self.folder, folder)
        except OSError:
            # Different mounts (the service's ReadWritePaths): copy the files
            # only. Copying modes would try to set the setgid bit LAIka's
            # folders carry, which the locked-down service may not do.
            try:
                for path in sorted(self.folder.rglob("*")):
                    target = folder / path.relative_to(self.folder)
                    if path.is_dir() and not path.is_symlink():
                        target.mkdir(parents=True, exist_ok=True)
                    elif path.is_file() and not path.is_symlink():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                folder.mkdir(parents=True, exist_ok=True)
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            shutil.rmtree(self.folder, ignore_errors=True)
        self.folder = folder


class CodeArea:
    """main's checkout (never written here) seen through one person's batches."""

    def __init__(self, repo, staging, user, project):
        self.repo, self.staging = Path(repo), Path(staging)
        self.user, self.project = user, project
        self.pending = []  # [(request_id, Batch)], oldest first: sent, not committed yet
        self.current = None

    # -- layers --
    def layers(self):
        """Newest first."""
        found = [self.current] if self.current else []
        return found + [batch for _, batch in reversed(self.pending)]

    def batch(self):
        if self.current is None:
            self.current = Batch(self.staging / uuid.uuid4().hex, self.user, self.project)
            self.current.save()
        return self.current

    def _main(self, rel):
        if rel.split("/")[0] == ".git" or "/.git/" in f"/{rel}/":
            return None
        path = self.repo / rel if rel else self.repo
        if not os.path.lexists(path) or not _inside(self.repo, path):
            return None
        return path

    def entry(self, rel):
        """Entry or None, as this person sees it."""
        rel = clean(rel)
        if not rel:
            return Entry.of(self.repo)
        for layer in self.layers():
            change = layer.changes.get(rel)
            if change:
                return Entry.of(layer.file(rel)) if change["action"] == "write" else None
            if layer.has_under(rel):
                return Entry("dir", 0, layer.last)
        path = self._main(rel)
        if path is None:
            return None
        found = Entry.of(path)
        if found.kind == "dir" and self._emptied(rel):
            return None  # every file in it was deleted or moved away here: git drops it too
        return found

    def _emptied(self, rel):
        prefix = rel + "/"
        if not any(p.startswith(prefix) and c["action"] == "delete" for layer in self.layers() for p, c in layer.changes.items()):
            return False
        return not self._children(rel)

    def need(self, rel):
        found = self.entry(rel)
        if found is None:
            raise FsError("missing")
        return found

    def listdir(self, rel):
        rel = clean(rel)
        if self.need(rel).kind != "dir":
            raise FsError("notdir")
        return self._children(rel)

    def _children(self, rel):
        names = set()
        path = self._main(rel) if rel else self.repo
        if path is not None and path.is_dir():
            names.update(name for name in os.listdir(path) if not (name == ".git"))
        prefix = rel + "/" if rel else ""
        for layer in self.layers():
            for item in list(layer.changes) + list(layer.dirs):
                if item.startswith(prefix):
                    names.add(item[len(prefix):].split("/")[0])
        return sorted(name for name in names if name and self.entry(prefix + name) is not None)

    def base_for(self, rel):
        """The version a change to rel is made on: the newest pending one, else main."""
        for _, batch in reversed(self.pending):
            change = batch.changes.get(rel)
            if change:
                return sha256_of(batch.file(rel)) if change["action"] == "write" else ""
        path = self._main(rel)
        return sha256_of(path) if path is not None and path.is_file() else ""

    def _change(self, rel, action):
        batch = self.batch()
        if rel not in batch.changes:
            base = self.base_for(rel)
            batch.changes[rel] = {"action": action, "base": base}
        else:
            batch.changes[rel]["action"] = action
        batch.last = time.time()
        return batch

    def _parent_ok(self, rel):
        parent = posixpath.dirname(rel)
        found = self.entry(parent)
        if found is None:
            raise FsError("missing", "no such folder")
        if found.kind != "dir":
            raise FsError("notdir")

    def open_read(self, rel):
        found = self.need(rel)
        if found.kind == "dir":
            raise FsError("isdir")
        return found.path

    def open_write(self, rel, truncate=False, exclusive=False):
        """The staging file to write; the change is recorded at once."""
        rel = clean(rel)
        if not rel or rel.split("/")[0] == ".git" or "/.git/" in f"/{rel}/":
            raise FsError("denied", ".git cannot be changed")
        current = self.entry(rel)
        if current is not None and current.kind == "dir":
            raise FsError("isdir")
        if current is not None and exclusive:
            raise FsError("exists")
        self._parent_ok(rel)
        batch = self._change(rel, "write")
        target = batch.file(rel)
        if target.parent.is_file():
            raise FsError("failure", "a staged file is in the way")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            if current is not None and not truncate:
                shutil.copyfile(current.path, target)  # partial writes and appends keep the rest
            else:
                target.touch()
        elif truncate:
            target.write_bytes(b"")
        batch.save()
        return target

    def remove(self, rel):
        rel = clean(rel)
        found = self.need(rel)
        if found.kind == "dir":
            raise FsError("isdir")
        batch = self._change(rel, "delete")
        if batch.changes[rel]["base"] == "" and rel not in [p for _, b in self.pending for p in b.changes]:
            if self._main(rel) is None:
                del batch.changes[rel]  # made and removed before any commit: nothing to do
        staged = batch.file(rel)
        if staged.exists():
            staged.unlink()
        batch.save()

    def mkdir(self, rel):
        rel = clean(rel)
        if self.entry(rel) is not None:
            raise FsError("exists")
        if rel.split("/")[0] == ".git":
            raise FsError("denied")
        self._parent_ok(rel)
        batch = self.batch()
        batch.dirs.add(rel)
        batch.last = time.time()
        batch.save()

    def rmdir(self, rel):
        rel = clean(rel)
        if not rel:
            raise FsError("denied")
        if self.need(rel).kind != "dir":
            raise FsError("notdir")
        if self.listdir(rel):
            raise FsError("notempty")
        if self.current:
            self.current.dirs.discard(rel)
            self.current.save()
        # An empty folder from main or a pending batch simply goes once its
        # files are gone; git keeps no empty folders.

    def files_under(self, rel):
        found = []
        for name in self.listdir(rel):
            child = f"{rel}/{name}" if rel else name
            entry = self.entry(child)
            if entry.kind == "dir":
                found += self.files_under(child)
            else:
                found.append(child)
            if len(found) > MAX_RENAME_FILES:
                raise FsError("failure", f"too many files to move at once (over {MAX_RENAME_FILES})")
        return found

    def rename(self, old, new, replace=False):
        old, new = clean(old), clean(new)
        source = self.need(old)
        if not new or new.split("/")[0] == ".git":
            raise FsError("denied")
        if new == old or new.startswith(old + "/"):
            raise FsError("failure", "cannot move a folder into itself")
        existing = self.entry(new)
        if existing is not None and (not replace or existing.kind == "dir"):
            raise FsError("exists")
        self._parent_ok(new)
        moves = [(old, new)] if source.kind == "file" else [(path, new + path[len(old):]) for path in self.files_under(old)]
        if source.kind == "dir":
            batch = self.batch()
            batch.dirs.add(new)
            for _, dst in moves:
                parent = posixpath.dirname(dst)
                while parent and parent != new:
                    batch.dirs.add(parent)
                    parent = posixpath.dirname(parent)
        for src, dst in moves:
            with open(self.need(src).path, "rb") as handle:
                data = handle.read()
            target = self.open_write(dst, truncate=True)
            target.write_bytes(data)
        for src, _ in moves:
            self.remove(src)
        if source.kind == "dir":
            batch = self.batch()
            batch.dirs = {d for d in batch.dirs if d != old and not d.startswith(old + "/")}
            batch.save()

    # -- committing --
    def ready(self, quiet_seconds, now=None):
        """The current batch, if it has changes, nothing is being written,
        and it was quiet long enough."""
        batch = self.current
        now = time.time() if now is None else now
        if batch is None or batch.open_writes:
            return None
        if not batch.changes:
            return None
        return batch if now - batch.last >= quiet_seconds else None

    def sent(self, request_id, batch, folder):
        """The current batch went to the host as request_id (moved to folder)."""
        batch.move_to(folder)
        self.pending.append((request_id, batch))
        if self.current is batch:
            self.current = None

    def finished(self, request_id):
        self.pending = [(rid, batch) for rid, batch in self.pending if rid != request_id]
