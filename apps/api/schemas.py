from typing import Any, Literal, Optional
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


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
    # ``goal`` was the original request key; accept it as an input alias while
    # exposing the newer ``prompt`` name to the handler and response schema.
    prompt: str = Field(
        min_length=1,
        max_length=100_000,
        validation_alias=AliasChoices("prompt", "goal"),
    )
    atomic: bool = False
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class GoalAccepted(BaseModel):
    id: str
    status: str
    atomic: bool = False


class WorkerAction(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class OperatorActionRequest(BaseModel):
    """An operator action for the host-side operator service to execute.

    Only the shape is checked here; the operator service re-validates
    everything and job-review.py decides whether the action is allowed.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "extend", "reintegrate", "reopen"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
    # The job status the human saw; the action is refused if it changed.
    expected_status: str = Field(pattern=r"^[a-z_]{1,64}$")
    # The exact integrated candidate the human confirmed (approve only).
    expected_candidate: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    extra: Optional[int] = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def _fields_match_action(self):
        if (self.action == "approve") != (self.expected_candidate is not None):
            raise ValueError("expected_candidate is required for approve and only for approve")
        if (self.action == "extend") != (self.extra is not None):
            raise ValueError("extra is required for extend and only for extend")
        return self


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class HandoffItem(ReadModel):
    pass
