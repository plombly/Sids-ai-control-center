"""Provider-neutral contracts. No SDK or orchestration dependencies."""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000, repr=False)
    workspace: str
    model: str | None = None
    timeout_seconds: float = Field(default=300, gt=0, le=3600)
    # Correlation only; future orchestrators own scheduling and persistence.
    task_id: int | None = None
    agent_id: int | None = None
    parent_execution_id: str | None = None


class ExecutionResult(BaseModel):
    execution_id: str
    provider: str
    model: str | None
    status: Literal['succeeded', 'failed', 'timed_out', 'unavailable']
    stdout: str = Field(default='', repr=False)
    stderr: str = Field(default='', repr=False)
    exit_code: int | None = None
    started_at: datetime
    duration_seconds: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderInfo(BaseModel):
    name: str
    model: str | None
    capabilities: list[str]
    available: bool
    status: Literal['available', 'unavailable']
    detail: str


class Provider(ABC):
    name: str
    model: str | None
    capabilities: tuple[str, ...]

    @abstractmethod
    def inspect(self) -> ProviderInfo: ...

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        if provider.name in self._providers:
            raise ValueError(f'Provider already registered: {provider.name}')
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        return self._providers[name]

    def inspect(self) -> list[ProviderInfo]:
        return [provider.inspect() for provider in self._providers.values()]
