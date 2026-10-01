"""Project file browser: confined paths, data uploads, code uploads via the host."""

import io
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
