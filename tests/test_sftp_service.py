"""services/sftp/laika_sftp.py end to end: a real SFTP client against the server."""

import asyncio
import json
import sys

import pytest

from laika_testing import ROOT, load_module

asyncssh = pytest.importorskip("asyncssh")
sys.path.insert(0, str(ROOT / "apps/api"))
import passwords  # noqa: E402

PASSWORD = "correct horse battery"


class Redis:
    def __init__(self):
        self.hashes, self.strings, self.sets, self.stream = {}, {}, {}, []

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {field: value})

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, ex=None):
        self.strings[key] = value

    def incr(self, key):
        self.strings[key] = str(int(self.strings.get(key) or 0) + 1)

    def expire(self, key, seconds):
        pass

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)

    def xadd(self, stream, fields, **kwargs):
        self.stream.append(fields)


@pytest.fixture
def world(tmp_path, monkeypatch):
    sftp = load_module(ROOT / "services/sftp/laika_sftp.py", "laika_sftp_test")
    projects, data = tmp_path / "projects", tmp_path / "data"
    for pid in ("shop", "blog"):
        (projects / pid / "repo" / "src").mkdir(parents=True)
        (projects / pid / "repo" / "src" / "app.py").write_text("print(1)\n")
    (data / "shop").mkdir(parents=True)
    for name, value in (("PROJECTS_BASE", projects), ("DATA_BASE", data), ("STATE", tmp_path / "state"),
                        ("UPLOADS", tmp_path / "uploads")):
        monkeypatch.setattr(sftp, name, value)
    (tmp_path / "uploads").mkdir()
    r = Redis()
    r.sets["laika:projects"] = {"shop", "blog"}
    for pid in ("shop", "blog"):
        r.hashes[f"laika:projects:{pid}"] = {"id": pid, "repo": str(projects / pid / "repo"), "status": "active"}
    hashed = passwords.hash_password(PASSWORD)
    r.sets["laika:users"] = {"sam", "vic"}
    r.hashes["laika:users:sam"] = {"username": "Sam", "password": hashed, "role": "member", "access": json.dumps({"shop": "build"})}
    r.hashes["laika:users:vic"] = {"username": "vic", "password": hashed, "role": "member", "access": json.dumps({"shop": "view"})}
    key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "host_key"
    key.write_private_key(str(key_path))
    committer = sftp.Committer(r, quiet=30)
    Server, Files = sftp.make_server_classes(committer)
    return sftp, r, committer, Server, Files, str(key_path), projects, data, tmp_path


def run(world, user, password, body):
    sftp, r, committer, Server, Files, key_path, *_ = world

    async def main():
        server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key_path], sftp_factory=Files,
                                              allow_scp=False)
        port = server.sockets[0].getsockname()[1]
        try:
            async with asyncssh.connect("127.0.0.1", port, username=user, password=password, known_hosts=None,
                                        client_keys=None) as conn:
                async with conn.start_sftp_client() as client:
                    return await body(client, conn)
        finally:
            server.close()
    return asyncio.run(main())


def test_a_member_sees_their_projects_and_code_changes_batch_into_one_request(world):
    sftp, r, committer, *_, projects, data, tmp = world

    async def body(client, conn):
        top = sorted(name for name in await client.listdir("/") if name not in (".", ".."))
        assert top == ["shop"]                                               # not blog
        assert sorted(n for n in await client.listdir("/shop") if n not in (".", "..")) == ["code", "data"]
        async with client.open("/shop/code/src/app.py", "w") as handle:
            await handle.write("print(2)\n")
        async with client.open("/shop/code/src/.tmp", "w") as handle:  # editor-style save
            await handle.write("new\n")
        await client.posix_rename("/shop/code/src/.tmp", "/shop/code/src/new.py")
        async with client.open("/shop/data/db.json", "w") as handle:
            await handle.write("{}")
        async with client.open("/shop/code/src/app.py") as handle:
            assert await handle.read() == "print(2)\n"                         # theirs, before the commit
        with pytest.raises(asyncssh.SFTPError):
            await client.open("/blog/code/src/app.py")
        committer.tick()
        assert r.stream == []                                                 # not quiet yet
        return True

    assert run(world, "SAM", PASSWORD, body)
    assert (projects / "shop" / "repo" / "src" / "app.py").read_text() == "print(1)\n"  # main untouched
    assert (data / "shop" / "db.json").read_text() == "{}"                   # data: immediate
    # Disconnecting sent the batch at once: one request, authored by Sam.
    assert len(r.stream) == 1
    request = r.stream[0]
    assert (request["op"], request["author"], request["project_id"]) == ("sftp", "sam", "shop")
    manifest = json.loads((tmp / "uploads" / request["upload"] / "manifest.json").read_text())
    assert {c["path"]: c["action"] for c in manifest["changes"]} == {"src/app.py": "write", "src/new.py": "write"}


def test_view_access_is_read_only_and_wrong_passwords_are_refused(world):
    sftp, r, committer, *_ = world

    async def body(client, conn):
        async with client.open("/shop/code/src/app.py") as handle:
            assert await handle.read() == "print(1)\n"
        with pytest.raises(asyncssh.SFTPError):
            await client.open("/shop/code/src/app.py", "w")
        with pytest.raises(asyncssh.SFTPError):
            await client.remove("/shop/code/src/app.py")
        return True

    assert run(world, "vic", PASSWORD, body)
    with pytest.raises(asyncssh.PermissionDenied):
        run(world, "vic", "wrong password!", lambda client, conn: None)
    assert r.strings.get("laika:sftp:fails:127.0.0.1") == "1"


def test_no_shell_or_commands(world):
    sftp, r, committer, Server, Files, key_path, *_ = world

    async def main():
        server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key_path], sftp_factory=Files,
                                              allow_scp=False)
        port = server.sockets[0].getsockname()[1]
        try:
            async with asyncssh.connect("127.0.0.1", port, username="sam", password=PASSWORD, known_hosts=None,
                                        client_keys=None) as conn:
                result = await conn.run("id", check=False)
                return result.exit_status, result.stdout
        except (asyncssh.ChannelOpenError, asyncssh.ProcessError) as exc:
            return "refused", str(exc)
        finally:
            server.close()
    status, out = asyncio.run(main())
    assert status != 0 and "uid=" not in (out or "")


def test_an_ssh_key_added_in_the_dashboard_signs_in(world):
    sftp, r, committer, Server, Files, key_path, *_ = world
    mine = asyncssh.generate_private_key("ssh-ed25519")
    other = asyncssh.generate_private_key("ssh-ed25519")
    public = mine.export_public_key("openssh").decode().strip()
    r.hashes["laika:users:sam"]["ssh_keys"] = json.dumps([{"id": "k1", "name": "laptop", "key": public}])

    async def connect(key):
        server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key_path], sftp_factory=Files)
        port = server.sockets[0].getsockname()[1]
        try:
            async with asyncssh.connect("127.0.0.1", port, username="sam", client_keys=[key], known_hosts=None,
                                        password=None, preferred_auth="publickey") as conn:
                async with conn.start_sftp_client() as client:
                    return sorted(n for n in await client.listdir("/") if n not in (".", ".."))
        finally:
            server.close()
    assert asyncio.run(connect(mine)) == ["shop"]
    with pytest.raises(asyncssh.PermissionDenied):
        asyncio.run(connect(other))
