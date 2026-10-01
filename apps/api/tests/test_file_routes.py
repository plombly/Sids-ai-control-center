"""Project file browser: confined paths, data uploads, code uploads via the host."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import file_routes
import main
from test_project_routes import FakeRedis, _operator_ready


@pytest.fixture
def files(monkeypatch, tmp_path):
    projects_mount, data_mount, uploads = tmp_path / "projects", tmp_path / "data", tmp_path / "uploads"
    repo = projects_mount / "shop" / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.js").write_text("console.log(1)\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "escape").symlink_to(tmp_path)
    (tmp_path / "secret.txt").write_text("SECRET")
    uploads.mkdir()
    monkeypatch.setattr(file_routes, "PROJECTS_MOUNT", projects_mount)
    monkeypatch.setattr(file_routes, "DATA_MOUNT", data_mount)
    monkeypatch.setattr(file_routes, "UPLOADS_MOUNT", uploads)
    fake = FakeRedis({"sid:projects:shop": {"id": "shop", "name": "Shop", "status": "active",
                                             "repo": "/opt/sid-projects/shop/repo"}}, members={"shop"})
    monkeypatch.setattr(main, "redis", fake)
    with TestClient(main.app) as client:
        yield client, fake, repo, data_mount / "shop", uploads


def test_browse_code_hides_git_and_downloads(files):
    client, _, repo, _, _ = files
    listing = client.get("/api/projects/shop/files?area=code").json()
    assert [e["name"] for e in listing["entries"]] == ["src", "escape"]
    assert client.get("/api/projects/shop/files?area=code&path=src").json()["entries"][0]["name"] == "app.js"
    assert client.get("/api/projects/shop/files/download?area=code&path=src/app.js").text == "console.log(1)\n"
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/projects/shop/files/download?area=code").content))
    assert "src/app.js" in archive.namelist() and not any(n.startswith(".git") for n in archive.namelist())
    assert not any("secret" in n for n in archive.namelist())


@pytest.mark.parametrize("path,code", [("../../secret.txt", 422), (".git/config", 404), ("escape/secret.txt", 403),
                                       ("src/../../x", 422), ("a\nb", 422)])
def test_paths_cannot_leave_the_project(files, path, code):
    client, *_ = files
    assert client.get("/api/projects/shop/files/download", params={"area": "code", "path": path}).status_code == code


def test_sid_and_unknown_projects_are_not_browsable(files):
    client, *_ = files
    assert client.get("/api/projects/sid/files").status_code == 404
    assert client.get("/api/projects/ghost/files").status_code == 404


def test_data_upload_folder_download_delete(files):
    client, _, _, data, _ = files
    assert client.get("/api/projects/shop/files?area=data").json()["entries"] == []
    assert client.post("/api/projects/shop/files/data/folder", json={"path": "img"}).status_code == 201
    put = client.put("/api/projects/shop/files/data?path=img/logo.png", content=b"\x89PNG")
    assert put.status_code == 200 and put.json()["size"] == 4
    assert (data / "img" / "logo.png").read_bytes() == b"\x89PNG"
    assert client.get("/api/projects/shop/files/download?area=data&path=img/logo.png").content == b"\x89PNG"
    assert client.put("/api/projects/shop/files/data?path=../x", content=b"x").status_code == 422
    assert client.delete("/api/projects/shop/files/data?path=img").json()["deleted"]
    assert not (data / "img").exists() and data.exists()
    assert client.delete("/api/projects/shop/files/data", params={"path": "/"}).status_code == 422


def test_data_upload_size_cap(files, monkeypatch):
    client, _, _, data, _ = files
    monkeypatch.setattr(file_routes, "MAX_UPLOAD", 3)
    assert client.put("/api/projects/shop/files/data?path=big.bin", content=b"12345").status_code == 413
    assert not any(data.iterdir())


def test_code_upload_is_staged_for_the_host(files):
    client, fake, repo, _, uploads = files
    _operator_ready(fake, allowed="project_commit_upload")
    response = client.put("/api/projects/shop/files/code",
                          params={"path": "static/logo.png", "request_id": "upload-0001"}, content=b"\x89PNG")
    assert response.status_code == 202 and response.json()["status"] == "pending"
    assert (uploads / "upload-0001" / "file").read_bytes() == b"\x89PNG"
    fields = fake.stream[-1][1]
    assert (fields["action"], fields["path"], fields["upload"]) == ("project_commit_upload", "static/logo.png", "upload-0001")
    assert not (repo / "static").exists()  # the API never writes the repository
    bad = client.put("/api/projects/shop/files/code", params={"path": ".git/hooks/x", "request_id": "upload-0002"},
                     content=b"x")
    assert bad.status_code == 404 and not (uploads / "upload-0002").exists()


def test_code_upload_cleans_staging_when_the_host_refuses(files):
    client, fake, _, _, uploads = files
    _operator_ready(fake, allowed="reject")
    response = client.put("/api/projects/shop/files/code", params={"path": "a.txt", "request_id": "upload-0003"},
                          content=b"x")
    assert response.status_code == 403 and not (uploads / "upload-0003").exists()


def test_data_operations_happen_now(files):
    client, _, _, data, _ = files
    client.put("/api/projects/shop/files/data?path=a.txt", content=b"a")
    post = lambda **body: client.post("/api/projects/shop/files/data/op", json=body)
    assert post(op="rename", path="a.txt", dest="b.txt").json()["path"] == "b.txt"
    assert post(op="mkdir", path="dir").json()["path"] == "dir"
    assert post(op="move", path="b.txt", dest="dir").json()["path"] == "dir/b.txt"
    assert post(op="zip", path="dir").json()["path"] == "dir.zip"
    assert post(op="unzip", path="dir.zip").json()["path"] == "dir (2)"
    assert (data / "dir (2)" / "dir" / "b.txt").read_bytes() == b"a"
    assert post(op="rename", path="dir", dest="../x").status_code == 422
    assert post(op="delete", path="").status_code == 422


def test_code_operations_go_to_the_host(files):
    client, fake, repo, _, _ = files
    _operator_ready(fake, allowed="project_commit_upload")
    post = lambda **body: client.post("/api/projects/shop/files/code/op", json=body)
    response = post(op="rename", path="src/app.js", dest="main.js", request_id="code-op-0001")
    assert response.status_code == 202 and response.json()["status"] == "pending"
    fields = fake.stream[-1][1]
    assert (fields["action"], fields["op"], fields["path"], fields["dest"]) == ("project_commit_upload", "rename", "src/app.js", "main.js")
    assert (repo / "src" / "app.js").exists()  # nothing changed by the API
    assert post(op="delete", path=".git", request_id="code-op-0002").status_code == 404
    assert post(op="rename", path="src/app.js", dest=".git", request_id="code-op-0003").status_code == 422
    assert post(op="delete", path="missing.txt", request_id="code-op-0004").status_code == 404
    assert post(op="delete", path="src/app.js").status_code == 422  # needs a request id


# --- batches, conflicts, multi-download ------------------------------------------------

def _batch(client, **body):
    return client.post("/api/projects/shop/files/batch", json=body)


def test_data_batches_run_now_with_conflict_answers(files):
    client, _, _, data, _ = files
    for name in ("a.txt", "b.txt"):
        client.put(f"/api/projects/shop/files/data?path={name}", content=name.encode())
    client.post("/api/projects/shop/files/data/folder", json={"path": "box"})
    client.put("/api/projects/shop/files/data?path=box/a.txt", content=b"old")
    clash = _batch(client, op="move", from_area="data", to_area="data", paths=["a.txt", "b.txt"], dest="box")
    assert clash.status_code == 409 and clash.json()["detail"]["conflicts"] == ["a.txt"]
    assert (data / "b.txt").exists()  # nothing moved yet
    done = _batch(client, op="move", from_area="data", to_area="data", paths=["a.txt", "b.txt"], dest="box",
                  resolutions={"a.txt": "overwrite"})
    assert done.status_code == 200 and (data / "box" / "a.txt").read_bytes() == b"a.txt"
    zipped = _batch(client, op="zip", from_area="data", paths=["box"], dest="", name="Archive")
    assert zipped.json()["path"] == "Archive.zip"
    assert _batch(client, op="rename", from_area="data", paths=["Archive.zip"], dest="box").status_code == 409
    assert _batch(client, op="delete", from_area="data", paths=["box", "Archive.zip"]).status_code == 200
    assert list(data.iterdir()) == []


def test_code_to_data_copy_runs_now_but_changes_to_code_go_to_the_host(files):
    client, fake, repo, data, _ = files
    copied = _batch(client, op="copy", from_area="code", to_area="data", paths=["src"], dest="")
    assert copied.status_code == 200 and (data / "src" / "app.js").exists()
    _operator_ready(fake, allowed="project_commit_upload")
    queued = _batch(client, op="move", from_area="data", to_area="code", paths=["src"], dest="", request_id="batch-0001",
                    default="keep")
    assert queued.status_code == 202
    fields = fake.stream[-1][1]
    spec = json.loads(fields["batch"])
    assert fields["op"] == "batch" and spec["op"] == "move" and spec["to_area"] == "code" and spec["default"] == "keep"
    assert (data / "src").exists()  # the host does it
    clash = _batch(client, op="copy", from_area="data", to_area="code", paths=["src"], dest="", request_id="batch-0002")
    assert clash.status_code == 409 and clash.json()["detail"]["conflicts"] == ["src"]
    assert _batch(client, op="delete", from_area="code", paths=["src"]).status_code == 422  # needs a request id


def test_multi_download_and_exists(files):
    client, _, _, data, _ = files
    client.put("/api/projects/shop/files/data?path=n.txt", content=b"n")
    selection = client.get("/api/projects/shop/files/download", params=[("area", "code"), ("path", "src"), ("path", "escape")])
    assert selection.status_code == 403  # every item is checked
    selection = client.get("/api/projects/shop/files/download", params=[("area", "code"), ("path", "src"), ("path", "src/app.js")])
    names = zipfile.ZipFile(io.BytesIO(selection.content)).namelist()
    assert sorted(names) == ["app.js", "src/app.js"]
    found = client.get("/api/projects/shop/files/exists", params=[("area", "data"), ("path", "n.txt"), ("path", "z.txt")])
    assert found.json() == {"exists": ["n.txt"]}


def test_upload_conflict_choices(files):
    client, fake, _, data, uploads = files
    client.put("/api/projects/shop/files/data?path=n.txt", content=b"one")
    clash = client.put("/api/projects/shop/files/data?path=n.txt", content=b"two")
    assert clash.status_code == 409 and clash.json()["detail"]["conflicts"] == ["n.txt"]
    assert (data / "n.txt").read_bytes() == b"one" and not [p for p in data.iterdir() if p.name.startswith(".sid")]
    assert client.put("/api/projects/shop/files/data?path=n.txt&on_conflict=keep", content=b"two").json()["path"] == "n (2).txt"
    client.put("/api/projects/shop/files/data?path=n.txt&on_conflict=overwrite", content=b"three")
    assert (data / "n.txt").read_bytes() == b"three"
    _operator_ready(fake, allowed="project_commit_upload")
    client.put("/api/projects/shop/files/code", params={"path": "a.txt", "request_id": "upload-k001", "on_conflict": "keep"},
               content=b"x")
    assert fake.stream[-1][1]["on_conflict"] == "keep"
