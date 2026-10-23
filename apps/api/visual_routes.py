"""Screenshots of a candidate rendered on LNMT (services/visual_preview.py),
for the approval card: GET /api/jobs/<id>/visual/<file>.png. The files are in
<project root>/visual/<job>/ on the host, read here through the read-only
/projects mount. Access: the job's project, view level (access.py)."""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import file_routes

router = APIRouter()
FILE = re.compile(r"^[a-z0-9-]{1,80}-(?:desktop|phone)\.png$")
JOB = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _main():
    import main
    return main


@router.get("/api/jobs/{job_id}/visual/{name}")
def visual_file(job_id: str, name: str):
    if not JOB.fullmatch(job_id) or not FILE.fullmatch(name):
        raise HTTPException(status_code=404, detail="Not found")
    project = _main().redis.hget(f"laika:jobs:{job_id}", "project_id") or ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", project) or project == "laika":
        raise HTTPException(status_code=404, detail="Not found")
    path = file_routes.PROJECTS_MOUNT / project / "visual" / job_id / name
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="No screenshot")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
