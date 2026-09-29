"""Agent definitions and future execution policy boundary; no scheduler yet."""
from pydantic import BaseModel, ConfigDict, Field

from providers.base import ExecutionRequest, ExecutionResult, ProviderRegistry


class AgentPermissions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    execute: bool = False
    require_human_approval: bool = True


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=255)
    role: str = Field(default='assistant', min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    capabilities: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)


class AgentResponse(AgentCreate):
    id: int
    status: str


class AgentDefinition(AgentResponse):
    """Independent of SQLAlchemy and any particular provider."""
    pass


class AgentRunner:
    """One execution only. Scheduling, handoffs and reviews belong above this layer.

    Approval-required requests fail closed until an approval store exists.
    Workspace selection is the trusted caller's responsibility.
    """
    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def execute(self, agent: AgentDefinition, request: ExecutionRequest) -> ExecutionResult:
        if agent.status != 'ready' or not agent.permissions.execute:
            raise PermissionError('Agent is not enabled for execution')
        if agent.permissions.require_human_approval:
            raise PermissionError('Human approval gate is not implemented')
        provider = self.registry.get(agent.provider)
        if not set(agent.capabilities).issubset(provider.capabilities):
            raise ValueError('Unsupported agent capabilities')
        if request.model is not None and request.model != agent.model:
            raise ValueError('Model must be configured on the agent')
        return provider.execute(request.model_copy(update={'agent_id': agent.id, 'model': agent.model}))
