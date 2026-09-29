from typing import Optional
from pydantic import BaseModel, ConfigDict


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    repository: Optional[str] = None


class ProjectResponse(ProjectCreate):
    id: int
    status: str

    model_config = ConfigDict(from_attributes=True)


class TaskCreate(BaseModel):
    project_id: int
    title: str
    description: Optional[str] = None
    priority: int = 50


class TaskResponse(TaskCreate):
    id: int
    status: str
    agent_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)
