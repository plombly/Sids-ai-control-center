# SID AI Command Center setup

## 1. Overview

SID is a self-hosted control plane: the FastAPI API and nginx web dashboard use
Postgres and Redis, while the orchestrator plans goals and workers run builder,
reviewer, repair, and integration jobs. The operator service runs approved host
actions. The normal flow is **goal -> jobs -> build -> integrate -> review ->
human approve**. Approval advances `main` only to the exact integrated candidate.

## 2. Prerequisites

Use a fresh Linux host with systemd, Docker and Docker Compose, git, and Python
3. The repository requires Python 3.10+; the containers use Python 3.13.

Create the durable test environment and install the repository requirements:

```sh
python3 -m venv /opt/sid-venv
/opt/sid-venv/bin/pip install -r apps/api/requirements-dev.txt
/opt/sid-venv/bin/pip install -r services/worker/requirements.txt
```

The deterministic gate also uses pytest and Node.js. Install Node.js for the
web tests; the package test command is `node app.test.js`. Install and log in to the
Codex CLI for the runtime user. Claude Code is optional:

```sh
npm install -g @anthropic-ai/claude-code
claude
```

The Claude subscription limits are shared with interactive Claude use.

## 3. Directory layout

The documented host defaults are:

```text
/opt/sids-ai-command-center/       live checkout (main)
/opt/sid-worktrees/                job worktrees and integration worktrees
/var/log/sid-ai/jobs/              job logs
/var/log/sid-ai/integration/       integration-gate reports
/opt/sid-venv/                     test Python environment
/etc/sid-ai/operator.env           root-owned operator environment
```

Relevant repository paths are:

```text
apps/api/                          FastAPI API image
apps/web/                          nginx dashboard image
services/orchestrator/             planning and dispatch
services/worker/                   build, review, repair, and integration jobs
services/operator/                 host-side operator service unit and code
scripts/                           gate, goal submission, and review commands
```

## 4. Configuration

From the checkout, copy `.env.example` to `.env`, fill in the database and
provider values, and never commit `.env`:

```sh
cp .env.example .env
```

The values below are the defaults in the repository. `REDIS_URL` is
`redis://redis:6379/0` for the Compose API configuration; host-side services
default to `redis://127.0.0.1:6379/0`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `REDIS_URL` | Redis connection for API and host services | Compose: `redis://redis:6379/0`; host services: `redis://127.0.0.1:6379/0` |
| `REPO_ROOT` | Live checkout used by host services | `/opt/sids-ai-command-center` |
| `WORKTREE_ROOT` | Job worktree directory | `/opt/sid-worktrees` |
| `DEFAULT_MODEL` | Codex/default pipeline model | `gpt-5.6-luna` |
| `SID_PYTHON` | Python used for SID test gates | unset; `/opt/sid-venv/bin/python` when that venv exists, otherwise `/tmp/sid-agent-venv/bin/python` |
| `ROLE_PROVIDERS` | Per-role provider overrides | planner `claude`, builder `codex`, reviewer `claude`, repair `claude` |
| `CLAUDE_<ROLE>_MODEL` | Claude model for a role | planner `opus`; builder/reviewer/repair `sonnet` |
| `CLAUDE_<ROLE>_BUDGET_USD` | Claude per-call budget ceiling | planner `1.00`; builder `3.00`; reviewer `1.50`; repair `2.00` |
| `CLAUDE_MAX_CONCURRENT` | Shared Claude call limit | `2` |
| `REVIEW_ASPECTS` | Review aspects to run | `spec,safety` |
| `CLAUDE_COOLDOWN_SECONDS` | Claude provider cooldown | `1800` |
| `BEST_OF_FROM_ATTEMPT` | Retry attempt at which best-of begins | `2` |
| `MAX_REPAIR_ATTEMPTS` | Repair limit | `2` |
| `MAX_BUILD_ATTEMPTS` | Build retry limit | `2` |
| `OPERATOR_ALLOWED_ACTIONS` | Actions the host operator may execute | `reject,extend,reintegrate,reopen,dequeue_approve` (supplied unit and code default; no approval actions) |
| `OPERATOR_REQUEST_TTL` | Operator request expiry in seconds | `600` |
| `SID_OPERATOR_TOKEN` | Token protecting API writes | unset (empty) |

Create the optional token file outside the repository. Use a generated value;
do not put a real value in this guide or in source control:

```sh
sudo install -d /etc/sid-ai
sudo sh -c 'umask 077; printf "SID_OPERATOR_TOKEN=%s\n" "$(openssl rand -hex 32)" > /etc/sid-ai/operator.env'
sudo chown root:root /etc/sid-ai/operator.env
sudo chmod 600 /etc/sid-ai/operator.env
```

When `SID_OPERATOR_TOKEN` is set, the API requires the `X-SID-Token` header on
every non-GET request. nginx injects the same header for dashboard API requests,
so the dashboard does not ask the browser for the token. The default operator
actions exclude `approve`; configure `SID_OPERATOR_TOKEN` first, then enable
`approve` only when the API is not reachable by people who must not approve
merges.

To enable dashboard approval after the token file exists, override the action
list with a drop-in (it survives updates to the unit file) and restart the
operator service:

```sh
sudo install -d /etc/systemd/system/sid-ai-operator.service.d
printf '[Service]\nEnvironment=OPERATOR_ALLOWED_ACTIONS=approve,queue_approve,dequeue_approve,reject,extend,reintegrate,reopen\n' \
  | sudo tee /etc/systemd/system/sid-ai-operator.service.d/approval.conf
sudo systemctl daemon-reload && sudo systemctl restart sid-ai-operator
```

`queue_approve` approves a change to merge once it is fresh on `main` (merge
queue); `approve` merges one exact candidate immediately. The API refuses both
unless `SID_OPERATOR_TOKEN` is configured.

## 5. Start the containers

From the checkout:

```sh
docker compose up -d --build
```

Compose starts Postgres, Redis, the API, and the web dashboard. The API is on
port `8000`, the dashboard is on port `8080`, and Redis is published on
`127.0.0.1:6379`; Postgres is used by the Compose network. The dashboard has no
login. Expose it only to people trusted to operate SID.

## 6. Install the systemd units

Install `services/operator/sid-ai-operator.service` for host-side actions. It
uses the checkout, Redis, worktrees, and the operator action allowlist shown
above:

```sh
cp services/operator/sid-ai-operator.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sid-ai-operator.service
```

The repository does not provide orchestrator or worker unit files. The
following are minimal examples; adapt the instance count and environment to
the host:

```ini
# sid-ai-orchestrator.service (example)
[Service]
WorkingDirectory=/opt/sids-ai-command-center
EnvironmentFile=-/etc/sid-ai/operator.env
ExecStart=/opt/sid-venv/bin/python services/orchestrator/orchestrator.py
Restart=always

# sid-ai-worker@.service (example)
[Service]
WorkingDirectory=/opt/sids-ai-command-center
EnvironmentFile=-/etc/sid-ai/operator.env
Environment=WORKER_ID=%i
ExecStart=/opt/sid-venv/bin/python services/worker/worker.py
Restart=always
```

Install the examples as `sid-ai-orchestrator.service` and
`sid-ai-worker@.service`, then enable the orchestrator and the desired worker
instances, for example `sid-ai-worker@01` through `sid-ai-worker@06`:

```sh
systemctl daemon-reload
systemctl enable --now sid-ai-orchestrator.service
systemctl enable --now sid-ai-worker@01.service sid-ai-worker@02.service
```

To force the test interpreter, add a drop-in for the orchestrator and worker
units:

```ini
[Service]
Environment=SID_PYTHON=/opt/sid-venv/bin/python
```

Changes under `services/` require restarting the affected units. Changes under
`apps/` require rebuilding the affected containers. Before any restart, verify
that no worker is busy; restarting a busy worker kills its job.

## 7. Run the integration gate

The checkout must be clean, with nothing untracked. Run the gate from the
checkout and check its exit code directly:

```sh
SID_PYTHON=/opt/sid-venv/bin/python REPO_ROOT=<checkout> python3 scripts/integration-check.py
```

The gate runs the repository's API, host, syntax, diagnostic, workflow, and web
checks and writes its report under `/var/log/sid-ai/integration/`.

## 8. Submit a goal

From the checkout, submit a normal or atomic goal:

```sh
python3 scripts/submit-goal.py "GOAL"
python3 scripts/submit-goal.py --atomic "GOAL"
```

The dashboard goal form submits the same kind of goal. For a CLI status view:

```sh
python3 scripts/goal-status.py <goal-id>
```

## 9. Review and approve

Use the dashboard only after confirming the exact candidate SHA. Dashboard
approval requires `approve` in `OPERATOR_ALLOWED_ACTIONS`. Human approval is
always required.

The host CLI supports:

```sh
python3 scripts/job-review.py approve <job-id> --candidate <sha>
python3 scripts/job-review.py queue <job-id> --candidate <sha>
python3 scripts/job-review.py reject <job-id>
python3 scripts/job-review.py extend <job-id> [N]
python3 scripts/job-review.py reintegrate <job-id>
python3 scripts/job-review.py reopen <job-id>
```

`approve` fast-forwards `main` only to the integrated candidate that was
reviewed. `queue` records approval for the merge queue; the queue
re-integrates on new `main` and requires a fresh review pass. The operator CLI
also has `dequeue <job-id>` for withdrawing a queued approval.

## 10. Troubleshooting

Use read-only checks such as:

```sh
docker exec sid-ai-redis redis-cli hgetall sid:workers:<worker-id>
systemctl status sid-ai-orchestrator.service
journalctl -u sid-ai-worker@01.service
```

Job logs are under `/var/log/sid-ai/jobs/`; integration reports are under
`/var/log/sid-ai/integration/`.
