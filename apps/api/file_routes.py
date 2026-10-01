"""Project file browser: browse and download a project's code (its main
checkout) and app data; upload to data directly, and to code as a commit
on main made on the host by the operator service.

The API container sees (docker-compose volumes):
  /projects      read-only   /opt/sid-projects (each project's repo, ...)
  /project-data  read-write  /opt/sid-project-data (each project's app data)
  /uploads       read-write  /opt/sid-uploads (code uploads waiting to be
                             committed by the host)
It never writes a repository: code uploads are staged and committed by
scripts/sid-project.py commit-upload (operator action project_commit_upload).
Every path is confined to its area (no "..", no .git in code, symlinks may
not lead outside). SID's own repository is not exposed here.
"""

import os
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
import json
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

import file_ops
import project_routes as projects

router = APIRouter()

HOST_PROJECTS = Path("/opt/sid-projects")
PROJECTS_MOUNT = Path(os.environ.get("SID_PROJECTS_MOUNT", "/projects"))
DATA_MOUNT = Path(os.environ.get("SID_DATA_MOUNT", "/project-data"))
UPLOADS_MOUNT = Path(os.environ.get("SID_UPLOADS_MOUNT", "/uploads"))
MAX_UPLOAD = int(os.environ.get("SID_MAX_UPLOAD_BYTES", str(1024 ** 3)))  # 1 GiB
STAGED_MAX_AGE = 24 * 3600
LIST_LIMIT = 2000


def _project(project_id):
    project_id = projects._id(project_id)
    if project_id == "sid":
        raise HTTPException(status_code=404, detail="SID's own files are not browsable here")
    if not projects._known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return project_id, projects._project_data(project_id)


def _area_root(project_id, data, area, create=False):
    if area == "data":
        root = DATA_MOUNT / project_id
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root
    try:
        rel = Path(data.get("repo") or "").relative_to(HOST_PROJECTS)
    except ValueError:
        raise HTTPException(status_code=404, detail="This project's code lives outside /opt/sid-projects")
    return PROJECTS_MOUNT / rel


def _parts(rel, area):
    rel = (rel or "").strip("/")
    if len(rel) > 400 or "\\" in rel or any(ord(c) < 32 for c in rel):
        raise HTTPException(status_code=422, detail="Invalid path")
    parts = PurePosixPath(rel).parts if rel else ()
    if any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=422, detail="Invalid path")
    if area == "code" and ".git" in parts:
        raise HTTPException(status_code=404, detail="Not found")
    return parts


def _resolve(root, rel, area, must_exist=True):
    """root/rel, refusing anything that is (or leads through a link to)
    outside root."""
    parts = _parts(rel, area)
    target = root.joinpath(*parts)
    root_real = root.resolve()
    real = target.resolve()
    if real != root_real and root_real not in real.parents:
        raise HTTPException(status_code=403, detail="Path leads outside the project")
    if must_exist and not os.path.lexists(target):
        raise HTTPException(status_code=404, detail="Not found")
    return target, "/".join(parts)


def _entry(path):
    info = path.lstat()
    kind = "link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode) else "file"
    return {"name": path.name, "type": kind, "size": info.st_size if kind == "file" else None,
            "modified": info.st_mtime}


@router.get("/api/projects/{project_id}/files")
def list_files(project_id: str, area: Literal["code", "data"] = "code", path: str = ""):
    project_id, data = _project(project_id)
    root = _area_root(project_id, data, area, create=(area == "data"))
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Nothing here yet")
    target, rel = _resolve(root, path, area)
    if not target.is_dir():
        raise HTTPException(status_code=422, detail="Not a folder")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if area == "code" and child.name == ".git":
            continue
        entries.append(_entry(child))
        if len(entries) >= LIST_LIMIT:
            break
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"project_id": project_id, "area": area, "path": rel, "entries": entries,
            "truncated": len(entries) >= LIST_LIMIT}


def _zip_folder(folder, area):
    handle = tempfile.NamedTemporaryFile(prefix="sid-zip-", suffix=".zip", delete=False)
    handle.close()
    with zipfile.ZipFile(handle.name, "w", zipfile.ZIP_DEFLATED) as archive:
        for current, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = [d for d in dirs if not (area == "code" and d == ".git")
                       and not os.path.islink(os.path.join(current, d))]
            for name in files:
                full = os.path.join(current, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                archive.write(full, os.path.relpath(full, folder))
    return handle.name


@router.get("/api/projects/{project_id}/files/download")
def download(project_id: str, area: Literal["code", "data"] = "code", path: List[str] = Query(default=[""])):
    """One file: the file. One folder: a zip of it. Several paths: one zip
    with each of them at its top level."""
    project_id, data = _project(project_id)
    root = _area_root(project_id, data, area)
    if len(path) > 1:
        if len(path) > file_ops.MAX_BATCH:
            raise HTTPException(status_code=422, detail=f"At most {file_ops.MAX_BATCH} items at once")
        items = []
        for one in path:
            target, rel = _resolve(root, one, area)
            if not rel:
                raise HTTPException(status_code=422, detail="Choose items, not the top level")
            items.append((target, target.name))
        handle = tempfile.NamedTemporaryFile(prefix="sid-zip-", suffix=".zip", delete=False)
        handle.close()
        file_ops.write_zip(handle.name, items, forbid_git=(area == "code"))
        return FileResponse(handle.name, media_type="application/zip", filename=f"{project_id}-{area}-selection.zip",
                            background=BackgroundTask(os.unlink, handle.name))
    target, rel = _resolve(root, path[0], area)
    real = target.resolve()
    if real.is_dir():
        name = (rel.rsplit("/", 1)[-1] if rel else f"{project_id}-{area}") + ".zip"
        archive = _zip_folder(real, area)
        return FileResponse(archive, media_type="application/zip", filename=name,
                            background=BackgroundTask(os.unlink, archive))
    if not real.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real, filename=target.name)


async def _receive(request, dest_dir):
    """Stream the request body into a temp file in dest_dir (size-capped)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=dest_dir, prefix=".sid-upload-", delete=False)
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(status_code=413, detail=f"File is larger than {MAX_UPLOAD // 1024 ** 2} MB")
            handle.write(chunk)
        handle.close()
        return Path(handle.name), size
    except BaseException:
        handle.close()
        os.unlink(handle.name)
        raise


@router.put("/api/projects/{project_id}/files/data")
async def upload_data(project_id: str, request: Request, path: str = Query(min_length=1),
                      on_conflict: Literal["ask", "overwrite", "skip", "keep"] = "ask"):
    project_id, data = _project(project_id)
    root = _area_root(project_id, data, "data", create=True)
    target, rel = _resolve(root, path, "data", must_exist=False)
    if not rel:
        raise HTTPException(status_code=422, detail="Choose a file name")
    parent = target.parent
    _resolve(root, "/".join(PurePosixPath(rel).parts[:-1]), "data", must_exist=False)
    temp, size = await _receive(request, parent)  # always read the body, then decide
    try:
        if os.path.lexists(target):
            if on_conflict == "ask":
                raise _conflict([target.name])
            if on_conflict == "skip":
                return {"area": "data", "path": rel, "size": size, "skipped": True}
            if on_conflict == "keep":
                target = parent / file_ops.free_name(parent, target.name)
            elif target.is_symlink() or target.is_dir():
                raise HTTPException(status_code=409, detail="A folder or link with that name exists")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return {"area": "data", "path": target.relative_to(root).as_posix(), "size": size}


class Folder(BaseModel):
    path: str = Field(min_length=1, max_length=400)


@router.post("/api/projects/{project_id}/files/data/folder", status_code=201)
def make_folder(project_id: str, payload: Folder):
    project_id, data = _project(project_id)
    root = _area_root(project_id, data, "data", create=True)
    target, rel = _resolve(root, payload.path, "data", must_exist=False)
    if os.path.lexists(target):
        raise HTTPException(status_code=409, detail="Something with that name exists")
    target.mkdir(parents=True)
    return {"area": "data", "path": rel}


@router.delete("/api/projects/{project_id}/files/data")
def delete_data(project_id: str, path: str = Query(min_length=1)):
    project_id, data = _project(project_id)
    root = _area_root(project_id, data, "data")
    target, rel = _resolve(root, path, "data")
    if not rel:
        raise HTTPException(status_code=422, detail="The data folder itself cannot be deleted")
    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        shutil.rmtree(target)
    return {"area": "data", "path": rel, "deleted": True}


def _prune_staged():
    cutoff = time.time() - STAGED_MAX_AGE
    for entry in UPLOADS_MOUNT.iterdir() if UPLOADS_MOUNT.is_dir() else ():
        try:
            if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


@router.put("/api/projects/{project_id}/files/code", status_code=202)
async def upload_code(project_id: str, request: Request, path: str = Query(min_length=1),
                      request_id: str = Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"),
                      on_conflict: Literal["ask", "overwrite", "skip", "keep"] = "ask"):
    """Stage a file and ask the host to commit it to main (no review: the
    operator is the authority). Poll /api/operator-requests/<request_id>."""
    project_id, data = _project(project_id)
    _area_root(project_id, data, "code")
    rel = "/".join(_parts(path, "code"))
    if not rel:
        raise HTTPException(status_code=422, detail="Choose a file name")
    _prune_staged()
    staging = UPLOADS_MOUNT / request_id
    if staging.exists():
        raise HTTPException(status_code=409, detail="Request id already used")
    temp, size = await _receive(request, staging)
    os.replace(temp, staging / "file")
    try:
        result = projects._operator_request("project_commit_upload", request_id,
                                            {"project_id": project_id, "path": rel, "upload": request_id,
                                             "on_conflict": on_conflict})
    except HTTPException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**result, "path": rel, "size": size}


class FileOp(BaseModel):
    op: Literal["mkdir", "rename", "move", "copy", "delete", "zip", "unzip"]
    path: str = Field(default="", max_length=400)
    dest: str = Field(default="", max_length=400)
    request_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


@router.post("/api/projects/{project_id}/files/{area}/op")
def file_op(project_id: str, area: Literal["code", "data"], payload: FileOp):
    """mkdir / rename (dest = new name) / move, copy (dest = folder) /
    delete / zip / unzip. Data: done now. Code: checked here against the
    read-only checkout, then committed to main by the host (poll the
    operator request)."""
    project_id, data = _project(project_id)
    if area == "data":
        root = _area_root(project_id, data, "data", create=True)
        try:
            return {"area": "data", **file_ops.apply(root, payload.op, payload.path, payload.dest)}
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
    root = _area_root(project_id, data, "code")
    try:
        _, rel = file_ops.resolve(root, payload.path, must_exist=payload.op != "mkdir", forbid_git=True)
        if payload.op == "rename":
            file_ops.check_name(payload.dest, forbid_git=True)
        if payload.op in ("move", "copy"):
            file_ops.resolve(root, payload.dest, forbid_git=True)
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    if not rel:
        raise HTTPException(status_code=422, detail="Choose a file or folder, not the top level")
    if not payload.request_id:
        raise HTTPException(status_code=422, detail="request_id is required for code changes")
    result = projects._operator_request("project_commit_upload", payload.request_id, {
        "project_id": project_id, "op": payload.op, "path": rel, "dest": payload.dest})
    return JSONResponse(status_code=202, content={**result, "area": "code", "op": payload.op, "path": rel})


def _conflict(names):
    return HTTPException(status_code=409, detail={
        "message": f"{len(names)} name(s) already exist", "conflicts": list(names)})


@router.get("/api/projects/{project_id}/files/exists")
def exists(project_id: str, area: Literal["code", "data"] = "code", path: List[str] = Query(default=[])):
    """Which of these paths exist already (asked before uploading)."""
    project_id, data = _project(project_id)
    root = _area_root(project_id, data, area, create=(area == "data"))
    found = []
    for one in path[:file_ops.MAX_BATCH]:
        target, rel = _resolve(root, one, area, must_exist=False)
        if rel and os.path.lexists(target):
            found.append(rel)
    return {"exists": found}


class Batch(BaseModel):
    op: Literal["copy", "move", "delete", "zip", "rename"]
    from_area: Literal["code", "data"]
    to_area: Optional[Literal["code", "data"]] = None
    paths: List[str] = Field(min_length=1, max_length=500)
    dest: str = Field(default="", max_length=400)
    name: str = Field(default="", max_length=255)
    resolutions: Dict[str, Literal["overwrite", "skip", "keep"]] = Field(default_factory=dict)
    default: Optional[Literal["overwrite", "skip", "keep"]] = None
    request_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _run_batch(op, roots, payload, dry_run=False):
    """file_ops for a batch. roots: {"code": path, "data": path}. dry_run
    only checks (conflicts, paths) without changing anything."""
    src_area = payload.from_area
    dst_area = payload.to_area or src_area
    src, dst = roots[src_area], roots[dst_area]
    git = {"code": True, "data": False}
    if op == "delete":
        if dry_run:
            return file_ops._sources(src, payload.paths, git[src_area])
        return file_ops.delete_many(src, payload.paths, git[src_area])
    if op == "zip":
        name = payload.name or "Archive.zip"
        if dry_run:
            file_ops._sources(src, payload.paths, git[src_area])
            name = name if name.lower().endswith(".zip") else f"{name}.zip"
            folder, _ = file_ops._dest_folder(src, payload.dest, git[src_area])
            file_ops.check_name(name, git[src_area])
            if os.path.lexists(folder / name) and payload.default is None:
                raise file_ops.Conflict([name])
            return None
        return file_ops.zip_many(src, payload.paths, payload.dest, name, git[src_area], payload.default)
    if op == "rename":
        if len(payload.paths) != 1:
            raise file_ops.FileOpError("Rename one item at a time")
        if dry_run:
            source, _ = file_ops.resolve(src, payload.paths[0], forbid_git=git[src_area])
            file_ops.check_name(payload.dest, git[src_area])
            target = source.parent / payload.dest
            if os.path.lexists(target) and target != source and payload.default is None:
                raise file_ops.Conflict([payload.dest])
            return None
        return {"done": [file_ops.rename(src, payload.paths[0], payload.dest, git[src_area], payload.default)],
                "skipped": []}
    mode = op
    if dry_run:
        return file_ops.plan_transfer(src, payload.paths, dst, payload.dest, mode, git[src_area], git[dst_area],
                                      payload.resolutions, payload.default)
    return file_ops.transfer(src, payload.paths, dst, payload.dest, mode, git[src_area], git[dst_area],
                             payload.resolutions, payload.default)


@router.post("/api/projects/{project_id}/files/batch")
def batch(project_id: str, payload: Batch):
    """Several items at once, within a tab or between Code and App data.
    Conflicts come back as 409 {"conflicts": [...]} until answered
    (resolutions per name, or default for all). Anything that changes Code
    is committed to main by the host as one commit (poll the operator
    request); everything else happens here and now."""
    project_id, data = _project(project_id)
    roots = {"code": _area_root(project_id, data, "code"),
             "data": _area_root(project_id, data, "data", create=True)}
    dst_area = payload.to_area or payload.from_area
    if payload.op in ("copy", "move") and not payload.to_area:
        raise HTTPException(status_code=422, detail="Choose where to paste")
    changes_code = dst_area == "code" if payload.op in ("copy", "move") else payload.from_area == "code"
    if payload.op == "move" and payload.from_area == "code":
        changes_code = True
    try:
        if not changes_code:
            return {"status": "succeeded", **_run_batch(payload.op, roots, payload)}
        _run_batch(payload.op, roots, payload, dry_run=True)  # early answers; the host checks again
    except file_ops.Conflict as exc:
        raise _conflict(exc.conflicts)
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    if not payload.request_id:
        raise HTTPException(status_code=422, detail="request_id is required for changes to code")
    spec = payload.model_dump(exclude={"request_id"}, exclude_none=True)
    result = projects._operator_request("project_commit_upload", payload.request_id, {
        "project_id": project_id, "op": "batch", "path": payload.paths[0],
        "batch": json.dumps(spec, separators=(",", ":"))})
    return JSONResponse(status_code=202, content={**result, "op": payload.op})
