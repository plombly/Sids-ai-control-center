import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents import AgentDefinition, AgentRunner
from agent_routes import get_agent_db
from main import app, get_db
from models import Base, Agent
from providers.base import ExecutionRequest, ProviderRegistry
from providers.codex import CodexProvider


@pytest.fixture
def cli(tmp_path):
    def make(body):
        path = tmp_path / 'fake-codex'
        path.write_text(f'#!{sys.executable}\n' + body)
        path.chmod(0o755)
        return CodexProvider(binary=str(path))
    return make


def test_cli_success_and_argument_safety(cli, tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'must-not-reach-child')
    provider = cli('import sys, os, json\n'
                   'assert "DATABASE_URL" not in os.environ\n'
                   'assert sys.argv[1:4] == ["--ask-for-approval", "never", "exec"]\n'
                   'assert "read-only" in sys.argv and "--json" in sys.argv\n'
                   'assert sys.argv[-1] == "-"\n'
                   'print(sys.stdin.read())\n'
                   'print("diagnostic", file=sys.stderr)\n')
    prompt = '$(touch SHOULD_NOT_EXIST); --help'
    result = provider.execute(ExecutionRequest(prompt=prompt, workspace=str(tmp_path), model='test-model', task_id=42))
    assert result.status == 'succeeded'
    assert result.exit_code == 0 and result.stdout.strip() == prompt
    assert result.stderr.strip() == 'diagnostic'
    assert result.model == 'test-model' and result.metadata['task_id'] == 42
    assert result.duration_seconds > 0
    assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()


def test_failure_timeout_and_output_limit(cli, tmp_path):
    provider = cli('import sys\nprint("x" * 100)\nsys.exit(7)\n')
    provider.output_limit_bytes = 10
    result = provider.execute(ExecutionRequest(prompt='test', workspace=str(tmp_path)))
    assert result.status == 'failed' and result.exit_code == 7
    assert len(result.stdout) == 10 and result.metadata['stdout_truncated']
    provider = cli('import time\nprint("partial", flush=True)\ntime.sleep(10)\n')
    result = provider.execute(ExecutionRequest(prompt='test', workspace=str(tmp_path), timeout_seconds=0.2))
    assert result.status == 'timed_out' and result.exit_code < 0
    assert 'partial' in result.stdout
    assert result.duration_seconds < 3


def test_missing_and_invalid_workspace(tmp_path):
    provider = CodexProvider(binary=str(tmp_path / 'missing'))
    assert not provider.inspect().available
    request = ExecutionRequest(prompt='test', workspace=str(tmp_path))
    assert provider.execute(request).status == 'unavailable'
    provider = CodexProvider(binary=sys.executable)
    request.workspace = str(tmp_path / 'missing')
    assert provider.execute(request).metadata['error'] == 'invalid_workspace'


def test_registry_and_policy(cli, tmp_path):
    registry = ProviderRegistry()
    provider = cli('print("done")\n')
    provider.name = 'future-provider'
    registry.register(provider)
    with pytest.raises(ValueError):
        registry.register(provider)
    agent = AgentDefinition(id=1, name='reviewer', role='reviewer', provider=provider.name,
                            status='ready', capabilities=['code_analysis'])
    runner = AgentRunner(registry)
    request = ExecutionRequest(prompt='review', workspace=str(tmp_path))
    with pytest.raises(PermissionError):
        runner.execute(agent, request)
    agent.permissions.execute = True
    with pytest.raises(PermissionError, match='approval'):
        runner.execute(agent, request)
    agent.permissions.require_human_approval = False
    assert runner.execute(agent, request).metadata['agent_id'] == 1


@pytest.fixture
def client():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    def database():
        with sessions() as session:
            yield session
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_agent_db] = database
    # No lifespan: init_database belongs to production; use only the fixture DB.
    with sessions() as session:
        session.add(Agent(name='legacy', provider='old-provider'))
        session.commit()
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def test_agent_provider_routes_and_legacy(client):
    assert client.get('/providers').json()[0]['name'] == 'codex'
    assert client.get('/providers/missing').status_code == 404
    legacy = client.get('/agents/1').json()
    assert legacy['role'] == 'assistant' and not legacy['permissions']['execute']
    payload = {'name': 'reviewer', 'provider': 'codex', 'role': 'reviewer', 'capabilities': ['code_analysis']}
    created = client.post('/agents', json=payload)
    assert created.status_code == 201
    assert client.get(f"/agents/{created.json()['id']}").json() == created.json()
    assert len(client.get('/agents').json()) == 2
    assert client.get('/agents/999').status_code == 404
    assert client.post('/agents', json={**payload, 'provider': 'missing'}).status_code == 422
    assert client.post('/agents', json={**payload, 'capabilities': ['unknown']}).status_code == 422


def test_existing_projects_tasks(client):
    project = client.post('/projects', json={'name': 'regression'}).json()
    assert client.get(f"/projects/{project['id']}").json() == project
    assert client.get('/projects').json() == [project]
    task = client.post('/tasks', json={'project_id': project['id'], 'title': 'keep working'}).json()
    assert task['status'] == 'pending' and task['agent_id'] is None
    assert client.get('/tasks').json() == [task]
    assert client.get(f"/projects/{project['id']}/tasks").json() == [task]
    assert client.post('/tasks', json={'project_id': 999, 'title': 'bad'}).status_code == 404
    assert client.get('/').json()['status'] == 'online'


def test_additive_schema_preserves_existing_rows():
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE agents (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, provider VARCHAR(100) NOT NULL, model VARCHAR(255), status VARCHAR(50) NOT NULL, created_at DATETIME)'))
        connection.execute(text("INSERT INTO agents (id, name, provider, status) VALUES (1, 'old', 'codex', 'offline')"))
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        agent = session.get(Agent, 1)
        assert agent.name == 'old' and agent.configuration is None
    engine.dispose()


def test_process_start_failure(cli, tmp_path, monkeypatch):
    provider = cli('print("unused")\n')
    def fail(*args, **kwargs):
        raise OSError('sensitive detail must not be returned')
    monkeypatch.setattr('providers.codex.subprocess.Popen', fail)
    result = provider.execute(ExecutionRequest(prompt='test', workspace=str(tmp_path)))
    assert result.status == 'failed' and result.exit_code is None
    assert result.metadata['error'] == 'process_start_failed'
    assert result.stderr == ''


def test_health_route(client, monkeypatch):
    from unittest.mock import MagicMock
    import main
    engine = MagicMock()
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main.redis, 'ping', lambda: True)
    monkeypatch.setattr(main, '_pipeline_health', lambda: None)  # never touch a live Redis
    assert client.get('/health').json() == {
        'status': 'healthy', 'services': {'postgres': 'healthy', 'redis': 'healthy'}, 'pipeline': None}


class _HeartbeatRedis:
    def __init__(self, keys, statuses=None, fail=False):
        self.keys, self.statuses, self.fail = keys, statuses or {}, fail

    def scan_iter(self, pattern):
        if self.fail:
            raise ConnectionError('down')
        prefix = pattern[:-1]
        return iter([k for k in self.keys if k.startswith(prefix)])

    def hget(self, key, field):
        return self.statuses.get(key)


def test_health_reports_pipeline_heartbeats_without_changing_status(client, monkeypatch):
    from unittest.mock import MagicMock
    import main
    monkeypatch.setattr(main, 'engine', MagicMock())
    fake = _HeartbeatRedis(['sid:orchestrators:o1', 'sid:operator-service:op', 'sid:workers:w1', 'sid:workers:w2'],
                           {'sid:workers:w1': 'working', 'sid:workers:w2': 'idle'})
    fake.ping = lambda: True
    monkeypatch.setattr(main, 'redis', fake)
    body = client.get('/health').json()
    assert body['pipeline'] == {'orchestrators': 1, 'operator_service': True, 'workers': 2, 'workers_busy': 1}
    assert body['status'] == 'healthy'
    empty = _HeartbeatRedis([])
    empty.ping = lambda: True
    monkeypatch.setattr(main, 'redis', empty)
    body = client.get('/health').json()
    assert body['pipeline'] == {'orchestrators': 0, 'operator_service': False, 'workers': 0, 'workers_busy': 0}
    assert body['status'] == 'healthy', 'pipeline state never changes status'
    down = _HeartbeatRedis([], fail=True)
    down.ping = lambda: True
    monkeypatch.setattr(main, 'redis', down)
    assert client.get('/health').json()['pipeline'] is None
