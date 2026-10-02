# Architecture

## Components

```
browser / phone ──► nginx (laika-web, :8080) ──► API (laika-api, 127.0.0.1:8000)
                                                    │  reads and requests only
                                                    ▼
                                                 Redis ◄──────────────┐
                                                    ▲                 │
   orchestrator (laika)   workers 1..N (laika)   operator (root)   apps, scaler (root)
   plans goals,           build, review,         executes approved   runs apps and previews,
   dispatches repairs,    repair, integrate      actions: merge,     builds, starts and stops
   releases dependents    in sandboxes           reject, projects    workers
```

- **API** (FastAPI, Docker): the dashboard's backend. It has no access to
  repositories, systemd or a shell; anything that changes a repository is a
  request the host-side operator service validates and executes.
- **Orchestrator**: plans goals into jobs (Claude by default), dispatches
  repairs after reviews, recovers lost or failed work within limits, and
  re-integrates queued approvals when main moves.
- **Workers**: claim the best ready job (by project importance, work in
  progress, remaining effort and age) and run it in the job's own git
  worktree, inside a sandbox, as the `laika` user.
- **Operator service**: the only place where main changes, through
  `scripts/job-review.py`.
- **Apps service**: runs project apps and previews as systemd units, starts
  builds, the goal assistant and housekeeping.
- **Scaler**: starts and drains workers ([Workers](workers.md)).

## The pipeline

1. **Plan**: a goal becomes jobs, each with the files it may change.
2. **Build**: a builder agent works in an isolated worktree; the project's
   tests (the *gate*) run offline in the sandbox; the result is committed.
3. **Integrate**: the exact commits are applied onto the latest main in a
   separate worktree, and the gate runs again on that result.
4. **Review**: independent reviewers (a specification check and a safety
   check) read the full diff of the exact integrated result, with the test
   results, and either pass it or list blocking findings.
5. **Repair**: findings go to a repair agent, then back to step 3; after a
   few rounds the job is handed to you instead of looping.
6. **Approve**: you approve the exact integrated, tested and reviewed
   result; main moves forward to it (fast-forward only).

## The safety contract

These hold for every change, and the test suite checks them:

1. Changes are integrated onto the latest clean main in isolation before
   review; a failed integration leaves main untouched.
2. The source commits, integration base, exact integrated result and the
   integration outcome are recorded.
3. Review always covers the exact integrated result, in full.
4. Approval requires a passed integration and a passed independent review
   of that exact result, and refuses if main moved since: the change is
   integrated and reviewed again.
5. Approval only fast-forwards main to the reviewed result.
6. Integrations, reviews and approvals cannot run twice (atomic
   reservations) and their locks are ownership-safe.
7. Repairs and retries are bounded.
8. **A human approves every change.** There is no automatic approval and
   no way to override a review's verdict. "Approve all" queues approvals
   bound to the exact changes you saw; each still merges only after its
   tests and a fresh review on the latest main.

## Data

| Store | What |
| --- | --- |
| Redis | goals, jobs, queues, workers, settings, sessions, events |
| Postgres | the API's project/task/agent records |
| git | every project's repository and per-job worktrees |
| files | job logs, builds, app data, backups |

## Development

LAIka itself is developed outside of LAIka, by its maintainers, with the
same contract: tests (`scripts/integration-check.py` runs every check),
independent review and human approval before anything is merged.
