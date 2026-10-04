#!/usr/bin/env python3
"""LAIka's SFTP server: edit project files with any SFTP app (WinSCP,
FileZilla, Cyberduck, VS Code's SFTP extensions, `sftp`).

Runs as the laika user (unit deploy/systemd/laika-sftp.service) on
SFTP_PORT (2222). Sign in with a LAIka username and password, or an SSH key
added under Settings -> Access. SFTP only: no shell, commands or
forwarding. Repeated failed sign-ins from one address are refused for 15
minutes, like the dashboard's.

What a person sees and may do (services/sftp/sftp_fs.py): a folder per
project they can access (apps/api/access.py), each with code/ and data/.
View: read-only. Build and up: may change files. data/ changes at once.
code/ changes collect per person and project and become ONE commit on main
SFTP_COMMIT_SECONDS (30) after their last write, or when they disconnect
(scripts/laika-project.py sftp-commit through the operator service, as the
person; files that changed on main meanwhile are kept as conflict copies).

Redis: laika:sftp:info (port, host key fingerprint, enabled; for the
dashboard), laika:sftp:fails:<ip>, laika:sftp:heartbeat.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import posixpath
import stat
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services"))
import laika_env  # noqa: E402,F401  (Settings -> environment, first)
import laika_redis  # noqa: E402
sys.path.insert(0, str(ROOT / "apps/api"))
import access  # noqa: E402
import passwords  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sftp_fs import CodeArea, DataArea, FsError  # noqa: E402

PORT = int(os.environ.get("SFTP_PORT", "2222"))
ENABLED = os.environ.get("SFTP_ENABLED", "true").lower() not in ("0", "false", "no", "off")
QUIET = max(5, int(os.environ.get("SFTP_COMMIT_SECONDS", "30")))
HOST_KEY = Path(os.environ.get("LAIKA_SFTP_HOST_KEY", "/etc/laika/sftp/ssh_host_ed25519_key"))
STATE = Path(os.environ.get("LAIKA_SFTP_STATE", "/var/lib/laika/sftp"))
UPLOADS = Path(os.environ.get("LAIKA_UPLOADS", "/var/lib/laika/uploads"))
DATA_BASE = Path(os.environ.get("LAIKA_PROJECT_DATA", "/var/lib/laika/project-data"))
PROJECTS_BASE = Path(os.environ.get("LAIKA_PROJECTS_BASE", "/var/lib/laika/projects"))
FAILS_ALLOWED, FAILS_WINDOW = 10, 900
OPERATOR_STREAM = "laika:operator-requests"
LEVEL_CACHE_SECONDS = 10
log = logging.getLogger("laika-sftp")


def redis_client():
    import redis as redis_lib
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


# --- who may see what --------------------------------------------------------------------------

def project_record(r, project_id):
    record = r.hgetall(f"laika:projects:{project_id}") or {}
    if not record or project_id == "laika" or record.get("view_only") == "1":
        return None
    if (record.get("status") or "active") not in ("active",):
        return None
    repo = Path(record.get("repo") or "")
    try:
        repo.resolve().relative_to(PROJECTS_BASE.resolve())
    except ValueError:
        return None  # only projects LAIka keeps, like the file browser
    return record


def visible_projects(r, user):
    """{project: level number} this person may use over SFTP."""
    ctx = access.context_for(user)
    levels = access.levels(r, ctx)
    ids = r.smembers("laika:projects") or set()
    if levels is None:  # administrators: every project LAIka keeps
        levels = {pid: access.LEVELS["approve"] for pid in ids}
    return {pid: level for pid, level in levels.items() if pid in ids and project_record(r, pid)}


# --- commits: one batch per person and project ---------------------------------------------------

class Committer:
    """Owns every person's code areas, sends quiet batches to the host and
    drops them once the host has answered."""

    def __init__(self, r, quiet=QUIET):
        self.r, self.quiet = r, quiet
        self.areas = {}  # (user, project) -> CodeArea
        self.sessions = {}  # user -> open sessions

    def area(self, user, project_id, repo):
        key = (user, project_id)
        if key not in self.areas:
            self.areas[key] = CodeArea(repo, STATE / "staging" / user / project_id, user, project_id)
        return self.areas[key]

    def send(self, area, batch):
        request_id = f"sftp-{uuid.uuid4().hex[:24]}"
        area.sent(request_id, batch, UPLOADS / request_id)
        fields = {"request_id": request_id, "action": "project_commit_upload", "job_id": "", "project_id": area.project,
                  "op": "sftp", "path": "sftp", "upload": request_id, "author": area.user}
        key = f"laika:operator-results:{request_id}"
        self.r.hset(key, mapping={**fields, "status": "pending", "created_at": str(time.time()),
                                  "requested_by": area.user})
        self.r.xadd(OPERATOR_STREAM, fields, maxlen=10000, approximate=True)
        log.info("sent %s's changes to %s as %s (%d files)", area.user, area.project, request_id, len(batch.changes))
        return request_id

    def tick(self, now=None, flush_user=None):
        """Send what is ready; forget what the host finished. flush_user:
        that person disconnected, send theirs without waiting."""
        for (user, _), area in list(self.areas.items()):
            batch = area.ready(0 if user == flush_user else self.quiet, now)
            if batch is not None:
                try:
                    self.send(area, batch)
                except Exception as exc:  # Redis down: it stays staged and is sent next time
                    log.warning("could not send %s's changes: %s", user, exc)
            for request_id, _ in list(area.pending):
                status = self.r.hget(f"laika:operator-results:{request_id}", "status")
                if status not in (None, "pending", "running"):
                    area.finished(request_id)
                    log.info("%s: %s", request_id, status)
                elif status is None:
                    area.finished(request_id)  # expired from Redis: nothing more to wait for

    def recover(self):
        """Batches staged before a restart are sent at once."""
        from sftp_fs import Batch
        for manifest in sorted((STATE / "staging").glob("*/*/*/manifest.json")):
            batch = Batch.load(manifest.parent)
            if not batch.changes:
                continue
            record = project_record(self.r, batch.project)
            if record is None:
                continue
            area = self.area(batch.user, batch.project, record["repo"])
            area.current = batch
            batch.last = 0
        self.tick()


# --- the SFTP server ---------------------------------------------------------------------------------

def make_server_classes(committer):
    import asyncssh

    errors = {"missing": asyncssh.SFTPNoSuchFile, "denied": asyncssh.SFTPPermissionDenied,
              "exists": asyncssh.SFTPFileAlreadyExists, "notempty": asyncssh.SFTPDirNotEmpty,
              "notdir": asyncssh.SFTPNotADirectory, "isdir": asyncssh.SFTPFileIsADirectory,
              "unsupported": asyncssh.SFTPOpUnsupported}

    def fail(exc):
        if isinstance(exc, FsError):
            return errors.get(exc.kind, asyncssh.SFTPFailure)(str(exc))
        if isinstance(exc, FileNotFoundError):
            return asyncssh.SFTPNoSuchFile(str(exc))
        if isinstance(exc, PermissionError):
            return asyncssh.SFTPPermissionDenied(str(exc))
        return asyncssh.SFTPFailure(str(exc))

    class Handle:
        def __init__(self, path, write, batch=None, mode="rb"):
            self.file = open(path, mode)
            self.write, self.batch = write, batch
            if batch is not None:
                batch.open_writes += 1

        def close(self):
            self.file.close()
            if self.batch is not None:
                self.batch.open_writes -= 1
                self.batch.last = time.time()
                self.batch = None

    class Server(asyncssh.SSHServer):
        def connection_made(self, conn):
            self.conn = conn
            peer = conn.get_extra_info("peername") or ("?",)
            self.ip = str(peer[0])

        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def public_key_auth_supported(self):
            return True

        def _blocked(self):
            return int(committer.r.get(f"laika:sftp:fails:{self.ip}") or 0) >= FAILS_ALLOWED

        def _failed(self, username):
            key = f"laika:sftp:fails:{self.ip}"
            committer.r.incr(key)
            committer.r.expire(key, FAILS_WINDOW)
            log.warning("failed sign-in for %r from %s", username[:40], self.ip)

        def _user(self, username):
            user = access.get_user(committer.r, (username or "").strip().lower())
            if not user or user["disabled"] or not user.get("password"):
                return None
            return user

        def validate_password(self, username, password):
            if self._blocked():
                return False
            user = self._user(username)
            if user and passwords.verify_password(password, user.get("password", "")):
                committer.r.delete(f"laika:sftp:fails:{self.ip}")
                self.conn.set_extra_info(laika_user=user["name"])
                return True
            self._failed(username)
            return False

        def validate_public_key(self, username, key):
            if self._blocked():
                return False
            user = self._user(username)
            offered = key.export_public_key("openssh").split()[:2]
            for item in json.loads((user or {}).get("ssh_keys") or "[]"):
                if item.get("key", "").split()[:2] == [part.decode() if isinstance(part, bytes) else part for part in offered]:
                    self.conn.set_extra_info(laika_user=user["name"])
                    return True
            return False  # not counted as a failure: clients try every key they have

        def auth_completed(self):
            name = self.conn.get_extra_info("laika_user")
            committer.sessions[name] = committer.sessions.get(name, 0) + 1

        def connection_lost(self, exc):
            name = self.conn.get_extra_info("laika_user")
            if name:
                committer.sessions[name] = max(0, committer.sessions.get(name, 1) - 1)
                if not committer.sessions[name]:
                    committer.tick(flush_user=name)  # disconnected: commit now

    class Files(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan)
            self.name = chan.get_connection().get_extra_info("laika_user")
            self._levels, self._levels_at = {}, 0

        # -- paths --
        def levels(self):
            if time.time() - self._levels_at > LEVEL_CACHE_SECONDS:
                user = access.get_user(committer.r, self.name)
                self._levels = visible_projects(committer.r, user) if user and not user["disabled"] else {}
                self._levels_at = time.time()
            return self._levels

        def where(self, path):
            """(project, area, rel) for a virtual path; project/area may be ""."""
            text = posixpath.normpath("/" + path.decode("utf-8", "surrogateescape")).lstrip("/")
            parts = [part for part in text.split("/") if part and part != "."]
            if not parts:
                return "", "", ""
            project = parts[0]
            if project not in self.levels():
                raise FsError("missing")
            if len(parts) == 1:
                return project, "", ""
            if parts[1] not in ("code", "data"):
                raise FsError("missing")
            return project, parts[1], "/".join(parts[2:])

        def area(self, project, kind):
            record = project_record(committer.r, project)
            if record is None:
                raise FsError("missing")
            if kind == "data":
                return DataArea(DATA_BASE / project)
            return committer.area(self.name, project, record["repo"])

        def writable(self, project):
            if self.levels().get(project, 0) < access.LEVELS["build"]:
                raise FsError("denied", "you have View access to this project")

        def attrs(self, kind, size=0, mtime=None, writable=False):
            mode = (stat.S_IFDIR | (0o755 if writable else 0o555)) if kind == "dir" else \
                (stat.S_IFREG | (0o644 if writable else 0o444))
            when = int(mtime or time.time())
            return asyncssh.SFTPAttrs(size=size, permissions=mode, uid=0, gid=0, atime=when, mtime=when)

        # -- reading --
        def realpath(self, path):
            text = posixpath.normpath("/" + path.decode("utf-8", "surrogateescape"))
            return ("/" if text in ("/", "//") else text).encode("utf-8", "surrogateescape")

        def lstat(self, path):
            try:
                project, kind, rel = self.where(path)
                if not kind:
                    return self.attrs("dir")
                entry = self.area(project, kind).entry(rel)
                if entry is None:
                    raise FsError("missing")
                writable = self.levels().get(project, 0) >= access.LEVELS["build"]
                return self.attrs(entry.kind, entry.size, entry.mtime, writable)
            except Exception as exc:
                raise fail(exc)

        stat = lstat

        def listdir(self, path):
            try:
                project, kind, rel = self.where(path)
                if not project:
                    names = sorted(self.levels())
                elif not kind:
                    names = ["code", "data"]
                else:
                    names = self.area(project, kind).listdir(rel)
                return [b".", b".."] + [name.encode("utf-8", "surrogateescape") for name in names]
            except Exception as exc:
                raise fail(exc)

        def open(self, path, pflags, attrs):
            try:
                project, kind, rel = self.where(path)
                write = bool(pflags & (asyncssh.FXF_WRITE | asyncssh.FXF_APPEND | asyncssh.FXF_CREAT | asyncssh.FXF_TRUNC))
                if not kind or not rel:
                    raise FsError("isdir")
                area = self.area(project, kind)
                if not write:
                    target = area.open_read(rel) if kind == "code" else area.open_path(rel, False)
                    return Handle(target, False)
                self.writable(project)
                truncate = bool(pflags & asyncssh.FXF_TRUNC)
                exclusive = bool(pflags & asyncssh.FXF_EXCL)
                if kind == "code":
                    target = area.open_write(rel, truncate=truncate, exclusive=exclusive)
                    return Handle(target, True, area.current, "r+b")
                target = area.open_path(rel, True)
                if exclusive and target.exists():
                    raise FsError("exists")
                if not target.exists() or truncate:
                    target.write_bytes(b"")
                return Handle(target, True, None, "r+b")
            except Exception as exc:
                raise fail(exc)

        def read(self, handle, offset, size):
            handle.file.seek(offset)
            return handle.file.read(size)

        def write(self, handle, offset, data):
            if not handle.write:
                raise asyncssh.SFTPPermissionDenied("opened read-only")
            handle.file.seek(offset)
            handle.file.write(data)
            if handle.batch is not None:
                handle.batch.last = time.time()
            return len(data)

        def close(self, handle):
            handle.close()

        def fstat(self, handle):
            info = os.fstat(handle.file.fileno())
            return self.attrs("file", info.st_size, info.st_mtime, handle.write)

        # -- changing --
        def _change(self, path, action):
            try:
                project, kind, rel = self.where(path)
                if not kind or not rel:
                    raise FsError("denied", "projects and their code/ and data/ folders cannot be changed")
                self.writable(project)
                return action(self.area(project, kind), rel)
            except Exception as exc:
                raise fail(exc)

        def remove(self, path):
            self._change(path, lambda area, rel: area.remove(rel))

        def mkdir(self, path, attrs):
            self._change(path, lambda area, rel: area.mkdir(rel))

        def rmdir(self, path):
            self._change(path, lambda area, rel: area.rmdir(rel))

        def _rename(self, old, new, replace):
            try:
                p1, k1, r1 = self.where(old)
                p2, k2, r2 = self.where(new)
                if (p1, k1) != (p2, k2) or not k1 or not r1 or not r2:
                    raise FsError("unsupported", "move between projects or code/data by copying")
                self.writable(p1)
                self.area(p1, k1).rename(r1, r2, replace=replace)
            except Exception as exc:
                raise fail(exc)

        def rename(self, oldpath, newpath):
            self._rename(oldpath, newpath, False)

        def posix_rename(self, oldpath, newpath):
            self._rename(oldpath, newpath, True)  # editors save with temp file + rename over

        def setstat(self, path, attrs):
            pass  # modes, owners and times are LAIka's to decide; accept and ignore

        def fsetstat(self, handle, attrs):
            if attrs.size is not None and handle.write:
                handle.file.truncate(attrs.size)

        def readlink(self, path):
            raise asyncssh.SFTPOpUnsupported("no links here")

        def symlink(self, oldpath, newpath):
            raise asyncssh.SFTPOpUnsupported("no links here")

        def link(self, oldpath, newpath):
            raise asyncssh.SFTPOpUnsupported("no links here")

        def statvfs(self, path):
            info = os.statvfs(STATE if STATE.exists() else "/")
            return asyncssh.SFTPVFSAttrs.from_local(info)

    return Server, Files


def fingerprint(key_path):
    """SHA256:... of the host key, as ssh shows it."""
    blob = base64.b64decode(Path(str(key_path) + ".pub").read_text().split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


async def serve():
    import asyncssh
    r = redis_client()
    committer = Committer(r)
    committer.recover()
    Server, Files = make_server_classes(committer)
    info = {"enabled": ENABLED, "port": PORT, "commit_seconds": QUIET,
            "fingerprint": fingerprint(HOST_KEY) if HOST_KEY.exists() else ""}
    if ENABLED:
        await asyncssh.create_server(Server, "", PORT, server_host_keys=[str(HOST_KEY)], sftp_factory=Files,
                                     allow_scp=False, agent_forwarding=False, x11_forwarding=False,
                                     login_timeout=60, keepalive_interval=60,
                                     server_version="LAIka")
        log.info("SFTP on port %d (commits %ds after the last change)", PORT, QUIET)
    while True:
        r.set("laika:sftp:info", json.dumps({**info, "at": time.time()}), ex=120)
        committer.tick()
        await asyncio.sleep(2)


def main():
    logging.basicConfig(level=logging.INFO, format="[laika-sftp] %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
