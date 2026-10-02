# LAIka

FastAPI service backed by PostgreSQL and Redis. Projects and Tasks retain their
existing APIs and relationships. This foundation adds provider-neutral execution
contracts and persistent agent definitions; it does not schedule or automatically
execute tasks.

## Architecture

- `apps/api/providers/base.py`: `Provider` interface (`execute`, `inspect`, name,
  default model, supported capabilities), validated request/result schemas, and an
  explicit registry. Add a Claude, Gemini, or other adapter by implementing this
  interface and registering it in `providers.build_registry()`; no OpenAI SDK is
  required by the core.
- `apps/api/providers/codex.py`: first adapter, invoking a local Codex executable.
- `apps/api/agents.py`: provider-independent agent definitions and a single-run
  policy boundary. Agents have their own ID, role, provider reference, optional
  model, capabilities, permissions, and status. Multiple agents can share a
  provider. `ready` means configured, not authenticated or available.
- `models.Agent` remains the persistent identity referenced by `Task.agent_id`.
  The additive `agent_configurations` table stores role, capabilities, and
  permissions. Existing agents without configuration receive conservative
  defaults. Existing tables and data are not altered. The current startup
  `create_all` creates this new table on the next service start; the database user
  needs CREATE permission. No manual backfill is required.
- `AgentRunner` resolves a provider through the registry, validates capabilities
  and agent model, and enforces execution permissions. Execution is denied by
  default. Requests requiring human approval fail closed because an approval
  store is not yet implemented. There is no public execution endpoint.

Future orchestration belongs above `AgentRunner`: use task/agent IDs and
`parent_execution_id` for correlation, persist execution records, and introduce
queues, handoff records, review-agent decisions, and durable human approvals there.
Roles such as `reviewer` are definitions today, not scheduling behavior. Neither
provider adapters nor task CRUD implement an orchestrator. Redis remains available
for a future queue; this change adds no queue consumers or background jobs.

## APIs

Existing routes: `GET /`, `GET /health`, `POST/GET /projects`,
`GET /projects/{id}`, `POST/GET /tasks`, `GET /projects/{id}/tasks`.

New routes:

| Route | Purpose |
| --- | --- |
| `GET /providers` | Registered providers, models, capabilities, installation status |
| `GET /providers/{name}` | Inspect one provider (404 if unknown) |
| `POST /agents` | Persist an agent and its configuration (201) |
| `GET /agents` | Inspect persisted agents, including legacy entries |
| `GET /agents/{id}` | Inspect one agent (404 if unknown) |

Example registration:

```sh
curl -X POST http://localhost:8000/agents \
  -H 'Content-Type: application/json' \
  -d '{"name":"Code reviewer","role":"reviewer","provider":"codex","capabilities":["code_analysis"]}'
```

Permissions default to `{"execute":false,"require_human_approval":true}`.
Unknown providers and unsupported capabilities return 422. A provider can be
registered even when its CLI is unavailable. Models are optional and are validated
by the provider at execution time, not against a hardcoded model catalog.
Interactive API documentation is at `/docs`. These routes inherit the existing
service's lack of authentication; keep deployment behind trusted access controls.

## Local Codex adapter

The adapter was checked against installed `codex-cli 0.159.0` help and the
[official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
It invokes the CLI with an argv list (no shell):

```text
codex --ask-for-approval never exec --sandbox read-only --color never --ephemeral --json --cd WORKSPACE [--model MODEL] -
```

The prompt is sent through stdin. Results include raw stdout JSONL, stderr,
exit code, UTC start time, monotonic duration, execution UUID, requested model,
correlation IDs, sandbox policy, and output size/truncation metadata. Model `null`
means the CLI selects its configured default; it is not a claim about the resolved
model. Nonzero exits, missing executables, invalid workspaces, launch failures,
and timeouts have explicit outcomes. POSIX process groups are killed and reaped
on timeout or interruption. This adapter targets Linux/POSIX workers.

Output is temporarily spooled to disk and returned up to 1 MiB per stream by
default. Returned output is memory bounded; temporary disk usage is not quota
limited. Use worker/container disk quotas for untrusted or large workloads.
Raw output may contain sensitive repository content: it is not logged, persisted,
or returned by the inspection routes. Trusted internal callers must protect it.

Runtime configuration (no credentials are stored in code):

| Setting | Default | Meaning |
| --- | --- | --- |
| `CODEX_BINARY` | `codex` | Executable name or absolute path |
| `CODEX_MODEL` | unset | Provider default; agent model takes precedence |
| `CODEX_HOME` | CLI default | Existing CLI configuration/authentication location |

The CLI must already be authenticated for the runtime user, or receive credentials
through its supported runtime environment. Only an explicit environment allowlist
is passed to the child; application DATABASE_URL/REDIS_URL are not forwarded.
Availability checks only locate the executable: they do not contact a model,
validate credentials, or guarantee execution will succeed. No inference is run at
API startup or during inspection. Request timeouts default to 300 seconds (maximum
3600). There is no retry or concurrent-run manager yet.

Read-only sandboxing prevents workspace writes but is not a secret isolation
boundary. Run against dedicated workspaces without secrets using a restricted OS
identity. Local CLI configuration and extensions remain operator-managed. The
internal caller must select an authorized workspace. Do not pass arbitrary public
requests directly to the adapter.

The current API Docker image contains Python dependencies only; the host's Codex
installation and login are **not** automatically available inside it. Provider
inspection will correctly report unavailable there. For execution, provision Codex
and authentication in the same trusted runtime as the calling worker. This change
does not mount host credentials or change the Compose services.

## Run and test

With the existing deployment configuration already provisioned:

```sh
docker compose up -d --build
```

For local development, supply `DATABASE_URL` and `REDIS_URL` through your runtime,
then run `uvicorn main:app --app-dir apps/api`. Do not commit credentials.

Tests use isolated in-memory SQLite and fake CLI executables. They do not read
`.env`, call a paid model, or connect to PostgreSQL/Redis:

```sh
python3 -m venv /var/lib/laika/venv
/var/lib/laika/venv/bin/pip install -r apps/api/requirements-dev.txt
/var/lib/laika/venv/bin/python -m pytest apps/api/tests -q
python3 -m compileall -q apps/api
```

Python 3.10+ is required; Docker uses 3.13. Tests cover subprocess arguments/stdin,
failures/timeouts/output limits, unavailable providers, registration, execution
gates, legacy-agent schema compatibility, new routes, and Project/Task regression.
An authenticated live Codex run and PostgreSQL deployment smoke test remain
separate integration checks.

## Review, repair and operator workflow

Reviewers are shown the complete integrated change
(`integration_base_commit..integrated_candidate_commit`), the deterministic
gate result, and any previous findings. They do not run tests (their sandbox
is read-only) and block only on correctness, requirement, regression, or
security defects in changed code. `VERDICT: PASS_WITH_NOTES` counts as a pass;
notes are stored in `review_findings`.

When a job still requires changes after its repair allowance
(`MAX_REPAIR_ATTEMPTS`, default 2), it becomes `needs_human` instead of
failing. Dependents keep waiting and the goal stays open. On the host:

```sh
python3 scripts/job-review.py approve JOB_ID
python3 scripts/job-review.py reject JOB_ID        # also works for needs_human
python3 scripts/job-review.py extend JOB_ID [N]    # grant N more repairs (default 1)
python3 scripts/job-review.py reintegrate JOB_ID   # fresh integration on current main + fresh review
python3 scripts/job-review.py reopen JOB_ID        # un-block a job whose failed dependency recovered
```

`reintegrate` is also the recovery path for stale candidates (main moved after
integration). It preserves the ordered source commits and never touches main.

Workers honor `laika:worker-control:<WORKER_ID> = disabled` (set by the Web/API
Stop button): the current job finishes, then no new jobs are claimed until
Start clears it. Test gates use `LAIKA_PYTHON`, defaulting to `/var/lib/laika/venv`.
