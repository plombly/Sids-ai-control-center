from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


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


# These models are intentionally additive and permissive: Redis records are
# written by older workers as well as current ones, so missing telemetry must
# remain a valid response.
class GoalSubmit(BaseModel):
    goal: str = Field(min_length=1, max_length=100_000)
    atomic: bool = False
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class PromptSubmit(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    atomic: bool = False
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class GoalAccepted(BaseModel):
    id: str
    status: str
    atomic: bool = False


class WorkerAction(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class HandoffItem(ReadModel):
    pass
