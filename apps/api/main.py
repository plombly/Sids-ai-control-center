from fastapi import FastAPI
from sqlalchemy import create_engine, text
from redis import Redis
import os

from database import init_database

app = FastAPI(
    title="SID's AI Command Center",
    version="0.1.0"
)

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

engine = create_engine(DATABASE_URL)
redis = Redis.from_url(REDIS_URL, decode_responses=True)


@app.get("/")
def root():
    return {
        "name": "SID's AI Command Center",
        "version": "0.1.0",
        "status": "online"
    }


@app.get("/health")
def health():
    services = {}

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        services["postgres"] = "healthy"
    except Exception as exc:
        services["postgres"] = f"error: {exc}"

    try:
        redis.ping()
        services["redis"] = "healthy"
    except Exception as exc:
        services["redis"] = f"error: {exc}"

    healthy = all(value == "healthy" for value in services.values())

    return {
        "status": "healthy" if healthy else "degraded",
        "services": services
    }


@app.on_event("startup")
def startup():
    init_database()


# ---------------------------------------------------------------------------
# Projects / Tasks API
# ---------------------------------------------------------------------------

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Project, Task
from schemas import (
    ProjectCreate,
    ProjectResponse,
    TaskCreate,
    TaskResponse,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/projects", response_model=ProjectResponse)
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    record = Project(**project.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.get("/projects", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.id.desc()).all()


@app.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db)):
    record = db.get(Project, project_id)

    if not record:
        raise HTTPException(status_code=404, detail="Project not found")

    return record


@app.post("/tasks", response_model=TaskResponse)
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    project = db.get(Project, task.project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    record = Task(**task.model_dump())

    db.add(record)
    db.commit()
    db.refresh(record)

    return record


@app.get("/tasks", response_model=list[TaskResponse])
def list_tasks(db: Session = Depends(get_db)):
    return db.query(Task).order_by(Task.priority.desc(), Task.id.asc()).all()


@app.get("/projects/{project_id}/tasks", response_model=list[TaskResponse])
def project_tasks(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.priority.desc(), Task.id.asc())
        .all()
    )


# Provider inspection and persistent agent definitions (execution is internal only).
from agent_routes import router as agent_router

app.include_router(agent_router)
