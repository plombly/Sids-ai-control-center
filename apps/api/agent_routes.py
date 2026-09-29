from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from agents import AgentCreate, AgentResponse, AgentPermissions
from database import SessionLocal
from models import Agent, AgentConfiguration
from providers import build_registry
from providers.base import ProviderInfo, ProviderRegistry

router = APIRouter(tags=['agents and providers'])
registry = build_registry()


def get_registry() -> ProviderRegistry:
    return registry


def get_agent_db():
    with SessionLocal() as session:
        yield session


def response(agent: Agent) -> AgentResponse:
    config = agent.configuration
    return AgentResponse(
        id=agent.id, name=agent.name, provider=agent.provider, model=agent.model,
        status=agent.status, role=config.role if config else 'assistant',
        capabilities=config.capabilities if config else [],
        permissions=AgentPermissions(**config.permissions) if config else AgentPermissions(),
    )


@router.get('/providers', response_model=list[ProviderInfo])
def list_providers(providers: ProviderRegistry = Depends(get_registry)):
    return providers.inspect()


@router.get('/providers/{name}', response_model=ProviderInfo)
def get_provider(name: str, providers: ProviderRegistry = Depends(get_registry)):
    try:
        return providers.get(name).inspect()
    except KeyError:
        raise HTTPException(404, 'Provider not found')


@router.post('/agents', response_model=AgentResponse, status_code=201)
def create_agent(payload: AgentCreate, db: Session = Depends(get_agent_db),
                 providers: ProviderRegistry = Depends(get_registry)):
    try:
        provider = providers.get(payload.provider)
    except KeyError:
        raise HTTPException(422, 'Provider is not registered')
    if not set(payload.capabilities).issubset(provider.capabilities):
        raise HTTPException(422, 'Unsupported capabilities for provider')
    agent = Agent(name=payload.name, provider=payload.provider, model=payload.model,
                  status='ready')
    agent.configuration = AgentConfiguration(
        role=payload.role, capabilities=payload.capabilities,
        permissions=payload.permissions.model_dump(),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return response(agent)


@router.get('/agents', response_model=list[AgentResponse])
def list_agents(db: Session = Depends(get_agent_db)):
    return [response(agent) for agent in db.query(Agent).order_by(Agent.id).all()]


@router.get('/agents/{agent_id}', response_model=AgentResponse)
def get_agent(agent_id: int, db: Session = Depends(get_agent_db)):
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, 'Agent not found')
    return response(agent)
