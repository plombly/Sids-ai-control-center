# CLAUDE.md — SID's AI Command Center

Context for Claude Code working on this repository. Read this fully before
changing anything. The owner is SID (the operator). Ask before anything
destructive or anything that affects the running system.

## What this is

A self-hosted AI software-engineering control plane on host `ai-server`:
goals are planned into jobs, builder agents implement them in isolated Git
worktrees, SID integrates each candidate onto the latest main in isolation,
an independent reviewer agent reviews that exact integrated candidate, and a
human approves before main advances.

```
apps/api/            FastAPI (Docker: sid-ai-api, :8000). Reads Redis, NO repo authority
apps/web/            Dependency-free UI served by nginx (Docker: sid-ai-web, :8080, /api -> api)
apps/tui/sid-tui.py  Terminal dashboard
services/orchestrator/orchestrator.py   planning, repair dispatch, dependency release, goal state
services/worker/worker.py               builder / reviewer / repair / integrate jobs
services/operator/sid_operator.py       executes Web operator actions via job-review.py (host, not Docker)
scripts/job-review.py        HOST-SIDE authority: approve / reject / extend / reintegrate / reopen
scripts/integration-check.py deterministic gate (tests, self-tests, diagnostics)
scripts/workflow-self-test.py, efficiency-self-test.py   fast no-network contract tests
tests/                       pytest for host-side code (job-review, operator service); gate check `host-tests`
```

Runtime (all live, all as root):

| Thing | Where |
| --- | --- |
| Live checkout (main) | `/opt/sids-ai-command-center` |
| Job worktrees | `/opt/sid-worktrees/job-<id>` and `job-<id>-integration` |
| Job logs | `/var/log/sid-ai/jobs/<id>.jsonl`, `<id>-tests.log`; gate reports in `/var/log/sid-ai/integration/` |
| Test Python | `/opt/sid-venv/bin/python` (via `SID_PYTHON`; `/tmp` venv is legacy — /tmp is wiped on reboot) |
| Orchestrator unit | `sid-ai-orchestrator.service` (`sid-orchestrator-01` is only its ORCHESTRATOR_ID, NOT a unit) |
| Worker units | `sid-ai-worker@01..06.service` (+ drop-in `sid-python.conf` setting SID_PYTHON) |
| Operator unit | `sid-ai-operator.service`; template in `services/operator/`, installing it is SID's call |
| Redis | Docker `sid-ai-redis`, host `127.0.0.1:6379`; inspect with `docker exec sid-ai-redis redis-cli ...` |
| Postgres | Docker `sid-ai-postgres` (only used by projects/tasks/agents API) |
| Agent CLI | `codex-cli 0.159.0`, model `gpt-5.6-luna`, auth in `/root/.codex` |
| Claude CLI | `claude` 2.1.285, claude.ai subscription login (shares plan limits with interactive sessions) |
| Role routing | `services/agent_cli.py`: planner=claude/opus, builder=codex, reviewer=claude/sonnet, repair=claude/sonnet; `ROLE_PROVIDERS`, `CLAUDE_<ROLE>_MODEL`, `CLAUDE_<ROLE>_BUDGET_USD`. Claude limit/auth errors fall back to Codex and set `sid:provider-cooldown:claude` (30 min) |
| Write access | `SID_OPERATOR_TOKEN` in `/etc/sid-ai/operator.env` (root 600, never print it); API (:8000) needs `X-SID-Token` on every non-GET; nginx injects it for the dashboard (:8080), so the browser needs nothing (single-operator LAN, by SID's choice). Web approve is enabled in the installed operator unit and refused by the API when no token is configured |

Redis keys: `sid:goals:<id>` (hash; `sid:goals:<id>:planning` is a STRING lock
— never hash-command it), `sid:jobs:<id>` (hash), queues `sid:goals` / `sid:jobs`
(lists), `sid:workers:<id>` (heartbeat hash, 30s TTL),
`sid:worker-control:<id>` (`disabled` = stop claiming jobs),
`sid:integration-lock:<id>`, `sid:approval-lock:main`,
`sid:orchestrators:<id>` (orchestrator heartbeat), `sid:goal-requests:<request_id>`
(goal submit idempotency, 24h).
Operator actions: `sid:operator-requests` (STREAM, consumer group `sid-operator`),
`sid:operator-results:<request_id>` (hash, kept as audit; status `pending` ->
`running` -> `succeeded|refused|error|expired|interrupted`, or `queue_failed`),
`sid:operator-service:<id>` (heartbeat hash, 30s TTL, lists `allowed_actions`).

## Working rules (non-negotiable)

1. **Never edit `/opt/sids-ai-command-center` directly.** Work in the dev
   worktree `/opt/sid-dev` on branch `dev/claude`. The live tree must stay
   clean: workers refuse to integrate when main is dirty, and the gate's
   `local-diagnostic` check fails on ANY untracked file there.
2. **Commit before running the gate.** `local-diagnostic` also requires the
   tree it runs in to be clean, including untracked files.
3. **Gate command** (from `/opt/sid-dev`, after committing):
   `SID_PYTHON=/opt/sid-venv/bin/python REPO_ROOT=/opt/sid-dev python3 scripts/integration-check.py`
   Check its exit code directly. Do not pipe it into `tail`, because the pipe
   hides failure.
4. **Deploying is SID's call.** Propose, don't do: merging to main
   (`git merge --ff-only dev/claude` in the live tree), restarting units, or
   rebuilding containers. Before any restart, confirm no worker is busy
   (`docker exec sid-ai-redis redis-cli hget sid:workers:sid-worker-0N status`
   is not `working`). Restarting a worker mid-job kills that job.
   Workers/orchestrator need a restart for `services/` changes; api/web need
   `docker compose up -d --build api web` for `apps/` changes;
   `scripts/job-review.py` takes effect immediately for the CLI, but the
   operator service loads it at start and needs a restart
   (`sid-ai-operator.service`) for job-review or operator changes.
5. **Do not read or print secrets**: `.env`, `/root/.codex/auth.json`,
   `/root/.claude*`, container env. Nothing here requires them.
6. **Do not mutate Redis job/goal state by hand** except through
   `scripts/job-review.py` or code paths under test. Failed goals are audit
   history. Leave them.
7. Do not give the API container repository, systemd, or shell authority.

## v1.2 safety contract (preserve all of it)

1. Integrate exact source candidates into an isolated worktree based on the latest clean main before review.
2. Integration failure leaves main unchanged.
3. Persist source candidates, integration base, exact integrated candidate, and integration result.
4. Independent review inspects the exact integrated candidate (full `base..candidate` range).
5. Approval requires integration PASS and independent review PASS on that exact candidate.
6. If main changed after integration, approval refuses; recovery is reintegration + fresh review.
7. Approval may advance main only (ff-only) to the immutable integrated candidate.
8. No duplicate integration/review (atomic reservations: `hsetnx`, `SET NX`).
9. Integration and approval locks are ownership-safe (compare-and-delete Lua).
10. Bounded repair behavior and dependency handling.
11. Human approval stays mandatory. No auto-approve, no verdict override.
12. Host-side `scripts/job-review.py` is authoritative for final Git validation and merge.
13. Merge queue (amendment, 2026-09-30): a queued approval (`queue_approve` /
    `job-review.py queue JOB --candidate SHA`) binds to the exact ordered
    SOURCE commits the human approved (the candidate they saw must match at
    queue time). When main moves, the orchestrator re-integrates those same
    sources on the new main; the queue merges only through unchanged
    `approve()` after the gate AND a fresh independent review pass on the new
    integrated candidate. Changed sources (repair/rebuild) or a failing fresh
    review void the approval. `approve --candidate` stays exact-SHA.

`workflow-self-test.py` and `efficiency-self-test.py` assert several of these
by inspecting source text. If you legitimately change such code, update
the assertion to check the NEW correct behavior. Never delete a check to
get green.

## Job lifecycle

Builder: `queued|blocked` -> `claimed` -> `running` -> `testing` ->
`awaiting_review` (candidate committed) -> integration (`integration_status`
running/passed, or job `integration_failed`) -> review queued -> reviewer job
`reviewing` -> `review_complete` (verdict on builder: `pass` |
`changes_required`) -> orchestrator dispatches a repair (up to
`max_repair_attempts`, default `MAX_REPAIR_ATTEMPTS=2`) -> repair commits onto
the builder worktree, appends to `source_candidate_commits`, clears derived
review/integration fields -> re-integrate -> re-review ... -> human
`approve` -> `merged`.

Terminal/failure: `failed`, `test_failed`, `integration_failed`, `rejected`,
`blocked_failed_dependency` (cascades), legacy `repair_exhausted`.
Terminal/success: `merged`, `completed_no_changes` (builder produced no diff).
Goal statuses also include `planning_failed`. Legacy records (pre-integration)
may have no `role`, `integration_status=legacy_not_run`, or a hand-set
`archived` status, and one id (`salvage-api-22178db`) is not alphanumeric, so
neither the CLI nor the operator service can act on it. Leave them.
Non-terminal hand-off: `needs_human` (repairs exhausted; dependents wait).
Other roles: reviewer (`review_complete`), repair (`repair_complete`),
integrate (`integrate_complete` / `integration_failed`).

Self-healing (orchestrator loop; approval is never automatic):
- Workers publish the job they hold (`job_id` in `sid:workers:<id>`). A job in
  an in-flight status that no live worker holds and the queue lacks, for
  `LOST_JOB_CONFIRM_SECONDS` (60), is failed "worker lost". Skipped while any
  live worker is too old to report `job_id`.
- A failed/test_failed/integration_failed builder with `build_attempt` is
  rebuilt from current main with the failure in its prompt, up to
  `MAX_BUILD_ATTEMPTS` (2), then `needs_human` (`needs_human_kind=build`).
  While a retry is pending the failure is not terminal (no cascade).
  Records without `build_attempt` (pre-self-healing) are never retried.
- An `awaiting_review` builder whose review is not complete and has nothing
  in flight is reintegrated + re-reviewed, up to `MAX_REVIEW_RECOVERIES` (2),
  then `needs_human` (`needs_human_kind=review`).
- A failed repair resets the builder worktree to the committed candidate.

Projects (2026-09-30): fully separated repositories sharing only the workers.
- Registry: Redis hash sid:projects:<id> + set sid:projects, managed by
  scripts/sid-project.py (create --empty|--clone URL, register-existing,
  retry-clone, push-setup, set-importance, archive, list). Roots under
  /opt/sid-projects/<id>/{repo,worktrees,logs}; per-project deploy key.
  SID itself is project "sid" (its paths, gate and Redis key names never
  come from the registry). services/sid_projects.py resolves a project;
  an unknown project is an error, never a fallback to SID.
- Every job carries project_id (reviews/repairs/integrations inherit it
  from their builder). Worker switches repo/worktree/log roots per job;
  job-review.py actions run inside the job's project; approval lock, merge
  queue and main-head are per project (SID keeps sid:merge-queue etc.).
- Non-SID gates: the project's gate_command, run with SID's venv first on
  PATH, bytecode/pytest caches off, and every untracked file the gate
  created removed afterwards (by exact path) so nothing leaks into
  candidates or makes integrated worktrees look modified.
- Web: #/ dashboard (health, per-project progress, approvals, workers),
  #/projects list + create, #/projects/<id> with its own prompt input,
  importance, GitHub push setup. Creation/clone/push-setup go through the
  operator service (actions create_project, project_retry_clone,
  project_push_setup), which runs sid-project.py on the host.
- Scheduler: workers rank ready jobs by project importance (6 general
  workers: high>medium>low; workers 07-08 WORKER_CLASS=support:
  medium>low>high), then in-flight work, then least remaining effort
  (planner sizes S/M/L = 1/3/8, sid:project-stats:<id>) minus aging, then
  age; claim by LREM. Re-ranked every pick; never preempts.
- The claude CLI self-updates (shared with interactive sessions); a missing
  or half-installed executable is a 2-minute outage with Codex fallback.

Parallelism (2026-09-30):
- Scope scheduling: a builder holds its planner `scope` files from dispatch
  until final; overlapping jobs wait as `blocked` with `blocked_reason`,
  released oldest-first. Planner scopes must list every changed file.
- Merge queue: `queue_approve` / `job-review.py queue`; see contract item 13.
  Operator service merges one ready job per loop; orchestrator re-integrates
  stale queued jobs once per main commit.
- Claude cap: `CLAUDE_MAX_CONCURRENT` (2) lease slots shared by workers and
  planner; waiting jobs fall back to Codex after 15 min.
- Specialist reviews: `REVIEW_ASPECTS` (spec on sonnet, safety on haiku) run
  in parallel inside one reviewer job; all must pass.
- Best-of: from `BEST_OF_FROM_ATTEMPT` (2) a retry builds twice in parallel
  (job-<id>-alt worktree), both on Codex by default (`BEST_OF_ALT_PROVIDER`);
  smaller passing change wins. SID's intent: Claude never builds by default.
- Cheap re-reviews: a passing review records the change's `git patch-id`; a
  merge-queue re-integration with the identical patch gets one Haiku
  "rebase check" instead of the full specialist review.
- Planner: Opus for multi-job goals, Sonnet for atomic goals.
- Web: apps/web/app.js is split into lib/ modules; new features are a module
  registering via lib/registry.js plus one import in lib/features.js, with
  their own apps/web/<name>.test.js (the gate runs all of them).

Operations (host timers, units in deploy/systemd/):
- `sid-ai-backup.timer` daily 03:30: scripts/backup-sid.py ->
  /var/backups/sid-ai/snapshots/<UTC stamp>/ (repo bundle, Redis RDB, pg
  dump, config tar incl. secrets, root-only), newest 14 kept; set
  BACKUP_REMOTE for an off-host copy (none configured yet). Hand-made
  backups in /var/backups/sid-ai are never touched (12 were lost once).
- `sid-ai-watchdog.timer` every 2 min: scripts/sid-watchdog.py -> sid:health.
- `sid-ai-prune.timer` weekly: scripts/prune-sid-data.py --days 30 --apply.
- Claude runs on the operator's claude.ai plan, shared with interactive
  sessions; a limit makes the pipeline fall back to Codex for 30 min.

Operator commands (host):

```
python3 scripts/job-review.py approve JOB [--candidate SHA]   # refuses unless SHA is the integrated candidate
python3 scripts/job-review.py reject JOB        # awaiting_review, needs_human, repair_exhausted, integration_failed
python3 scripts/job-review.py extend JOB [N]    # needs_human: N more repairs, or N more rebuilds if kind=build
python3 scripts/job-review.py reintegrate JOB   # fresh integration on current main + fresh review (stale recovery)
python3 scripts/job-review.py reopen JOB        # un-block blocked_failed_dependency once deps recovered
python3 scripts/submit-goal.py [--atomic] "GOAL"
```

Web operator actions (Plan A): `POST /api/jobs/{id}/actions` with
`{action, request_id, expected_status, expected_candidate (approve only, full SHA),
extra (extend only)}` records a pending result and appends to the stream; poll
`GET /api/operator-requests/{request_id}`; `GET /api/operator/status` shows the
service. The API only checks shape (plus early 404/409/403/503 feedback). The
operator service re-validates, runs each request id at most once, expires
requests older than `OPERATOR_REQUEST_TTL` (600s, measured on the Redis clock),
refuses if the job status changed, and marks a request that was running at a
crash `interrupted` (never re-run; check the job and submit a new request).
`OPERATOR_ALLOWED_ACTIONS` defaults to everything except `approve`: enable
approve only once the API is not reachable by people who must not merge.

## History you must know

The repo was built by an automated Codex loop and "broke constantly".
Root causes found in the audit and fixed in `b1d8a6f` (+ `50f0375`):

- **Reviewers could not run tests.** Codex `read-only` sandbox has no writable
  temp dir; pytest crashed in 30/57 reviews. Reviewers now get the gate
  result in the prompt and are told not to run tests.
- **Reviewers saw the wrong diff.** It was truncated at 12k chars and, on re-reviews, only
  showed the last repair commit (`candidate^..candidate`). Now the full
  `integration_base_commit..candidate` range is sent, per file, and omitted files are named.
- **Goalposts moved every round.** Each fresh reviewer found new nits with no
  memory of prior findings, so `repair_exhausted` was the expected outcome.
  Now: prior findings (`review_findings_history`), a blocking-severity bar,
  and `VERDICT: PASS_WITH_NOTES` (recorded as `pass`; approval contract unchanged).
- **Exhaustion was terminal.** It is now `needs_human`, with `extend` / `reintegrate` / `reject`.
- **Approval could strand jobs.** Merge is now recorded in Redis BEFORE best-effort cleanup.
- **Dependency failures didn't cascade.** `blocked_failed_dependency` now counts as failed.
- **Web Stop/Start were cosmetic.** The heartbeat overwrote them. Now there's a separate control key.
- **Busy workers vanished.** No heartbeat ran during tests or integration. A background keep-alive fixes it.
- New telemetry: `test_status`, `files_changed`, `review_findings`.

Verified after deploy: smoke goal `dc04a711` / job `7a96c19c` built in 20s,
reviewer passed on the first review in 14s with one command. Three legacy
exhausted jobs were reintegrated, hit genuine cherry-pick conflicts, and were rejected.

Merged merges reviewed under the old partial-diff reviewer (worth a skeptical
look if touching their code): `4b87827f`, `358966df`, `17b11696`.

## Current gaps (the work ahead)

Web v2 was supposed to be a 4-job goal (`a9bb98ee`). Only its backend job
merged. The UI, stale-recovery, and tests jobs never ran: `a70c8fdc`,
`1fa04294`, `4a616f01` sit in `blocked_failed_dependency`. `a70c8fdc` depends
only on merged `358966df`, so `reopen` would accept it, but that revives the old
plan that Plan B replaces; SID decides. The later single
job `17b11696` was hand-salvaged. What exists vs missing:

- Working: goal submit (normal/atomic, request_id idempotency), approval-ready
  read model, worker idle/busy, Copy for ChatGPT, handoff zip, Stop/Start.
- Broken or incomplete:
  - List endpoints default to 8 items, so history silently truncates.
  - Repository panel is always "unknown" (API container has no git).
- Missing: Web approve/reject/extend/reintegrate/reopen buttons (the backend
  for them is Plan A), add worker,
  queue management, goal/job drill-down UI, persisted command/tool events,
  prompts in UI, reviewer findings in UI, provider controls, auth.
- `apps/web/app.js` / `styles.css` were reformatted with prettier (print width
  120, single quotes in JS). Keep them readable and dependency-free, and keep
  `apps/web/app.test.js` passing (the gate runs it). Mechanical rewrites belong
  to a deterministic tool, not a builder agent: an agent reformat (job d14f4ccc)
  changed string literals twice and was rejected.

## Plan (in order)

**A. Host-side action service. IMPLEMENTED on `dev/claude`, pending SID's
merge + unit install (see "Web operator actions" above).** A small service
on the host (NOT in Docker) that executes operator actions requested from the
Web by calling the same functions as `scripts/job-review.py`. Suggested shape:
the API writes validated requests to a Redis stream/list
(`sid:operator-requests`) with job id, action, the exact candidate commit the
human saw, and a request id. The host service validates and executes each
request, records the result (`sid:operator-results:<request_id>`), and is
idempotent per request id. Approval MUST re-check everything
`_approve_unlocked` checks and refuse if the candidate the human confirmed
≠ `integrated_candidate_commit`. New systemd unit (e.g.
`sid-ai-operator.service`). Full tests for every action and every refusal
path. Don't break the CLI.

**B. Web v2 UI features.** Approval UI with exact-candidate confirmation,
needs_human actions, drill-down views (job lineage, candidates, integration +
review state, findings, tests, tokens, runtime), persisted dismiss/archive
(audit-preserving), un-truncated history with paging, redaction fix. Prefer
submitting these to SID itself as small `--atomic` goals once A is merged;
that exercises the pipeline. Write the goals narrowly.

**C. Claude CLI provider.** Install `@anthropic-ai/claude-code` if missing.
Implement an adapter beside `apps/api/providers/codex.py` behind the existing
`Provider` interface. Worker execution currently hard-codes `codex exec`
(`run_codex`, planner in the orchestrator), so factor that behind a provider
selection per job/worker. Use non-interactive print mode with JSON output.
Verify flags with `claude --help`; do not guess. Include model selection,
auth detection, and Codex<->Claude cross-review (a reviewer of the opposite
provider). Keep the UI provider-agnostic.

**D. Public repo prep.** Git history is already clean of secrets. Still needed:
- Add a LICENSE.
- Remove hard-coded `HOME=/root` (worker `run_codex`, orchestrator planner).
- Replace the `gpt-5.6-luna` defaults in scripts with env config.
- Write a generic setup guide.

**Deferred by the owner until fully operational (do not do unasked):** secret
rotation, binding :8000/:8080 to localhost, API auth. Mention it when
relevant, but it's SID's decision.

## Style

Small, reviewable commits with clear messages. Tests with every behavior
change. Explain the root cause, not just the fix. When unsure about live
state, inspect it read-only (Redis, logs, `systemctl status`) rather than guessing.
