"""Previews: run a change's app before approving it.

POST asks the host's apps service (services/apps/laika_apps.py) to start the
job's exact integrated candidate as a preview; DELETE stops it. The apps
service does the work and reports state, port and errors in
laika:preview:<job>, which /api/approvals attaches to each card.
"""

import time

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _main():
    import main
    return main


def preview_status(job_id):
    info = _main()._hash(f"laika:preview:{job_id}")
    if not info:
        return None
    port = info.get("port")
    return {"state": info.get("state") or "requested", "port": int(port) if port and port.isdigit() else None,
            "error": info.get("error") or "", "log": (info.get("log") or "")[-2000:],
            "started_at": info.get("started_at") or None}


def previewable(job):
    """A ready-to-approve job of a project that has a run command."""
    project_id = job.get("project_id") or "laika"
    if project_id == "laika":
        return False
    return bool(_main()._hash(f"laika:projects:{project_id}").get("run_command"))


def _ready_job(job_id):
    main = _main()
    if not job_id.isalnum() or len(job_id) > 64:
        raise HTTPException(status_code=422, detail="Invalid job id")
    data = main._hash(f"laika:jobs:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = main._job(f"laika:jobs:{job_id}", data)
    return job, data


@router.post("/api/jobs/{job_id}/preview", status_code=202)
def start_preview(job_id: str):
    job, data = _ready_job(job_id)
    if not _main()._approval_ready(job):
        raise HTTPException(status_code=409, detail="Only a change that is ready for approval can be previewed")
    if not previewable(job):
        raise HTTPException(status_code=409, detail="Give the project a run command first (Build & run)")
    current = preview_status(job_id)
    if current and current["state"] in ("requested", "starting", "running"):
        return current
    _main().redis.hset(f"laika:preview:{job_id}", mapping={
        "state": "requested", "project_id": job["project_id"], "candidate": data["integrated_candidate_commit"],
        "requested_at": str(time.time()), "error": "", "log": ""})
    return preview_status(job_id)


@router.delete("/api/jobs/{job_id}/preview")
def stop_preview(job_id: str):
    _ready_job(job_id)
    if not preview_status(job_id):
        raise HTTPException(status_code=404, detail="No preview")
    _main().redis.hset(f"laika:preview:{job_id}", "state", "stop")
    return {"job_id": job_id, "state": "stop"}
