# LAIka: features and fixes to add

The persistent work list (not shipped: `*-todo.md` is excluded from
releases). Claude keeps it current: read it at the start of every session
and after every context compaction, tick items off as they ship, and add
new ideas the owner mentions. Hands-on checks for the owner live in
`dylan-todo.md`.

Working rules for this list: build on `dev/claude`, deploy to the
development LAIka (`scripts/laika-dev.sh deploy`, http://10.0.0.59:9080) for
the owner to try, then release and deploy to production when they say so.

## 1.4: released 2026-10-04 (checkpoints 1, 2, 4 and the server side of 5)

Order and checkpoints. Report to the owner at each checkpoint.

### Checkpoint 1: polish from the week's testing
- [x] **Diagnose and fix in one go.** The goal-box Conversation can propose a
      ready goal inside its reply (when it has found a cause or the idea is
      clear); the chat shows it with "Start this goal" / "Edit first", so a
      found cause becomes a goal without re-explaining.
- [x] **App logs on the project page.** The apps service publishes the app
      unit's recent log (journal of laika-app-<id>, last ~200 lines) to Redis;
      the project page shows it live (like job logs), with why it last
      crashed / restarted.
- [x] **Play-test step for web apps.** After a deploy of a project with a web
      app, load the page in a headless browser (Docker Playwright image,
      host network, the app's port), record console errors, failed requests
      and a screenshot; show them on the project page and flag a blank page
      or JS errors (notification "app looks broken").
- [x] **Smarter repair rounds.** A repair gets every open finding as a
      checklist and must address all of them; the re-review states for each
      earlier finding whether it is fixed, and only blocks on new findings
      that are real regressions or blocking-severity. The safety contract
      stays: the review still inspects the whole integrated candidate
      (base..candidate).

### Checkpoint 2: project templates
- [x] "New project" offers templates (web game, website, API + website,
      Discord bot, CLI tool, Python script/service, …) that start with a
      working skeleton: tests, a run command that listens on $PORT /
      0.0.0.0, README. The first goal can then be about features.

### Checkpoint 4: remote access (Tailscale)
- [x] Settings → Remote access: install / sign in Tailscale from the
      dashboard (auth key), show the tailnet address for the dashboard and
      apps; doctor checks it; nothing public (VPN-only stays the rule).

### Checkpoint 5: the LAIka phone app (exists: laika-app project, Flutter)
- [x] Server: pairing codes and /api/app/info carry the Tailscale addresses
      (alt_urls / tailnet_urls).
- [x] App: try the other addresses when the first does not answer (merged
      2026-10-04, job b7aa7c2a, reviewed and approved by Claude).
- [x] Android APK rebuilt: build apk9 on the real LAIka (LAIka App -> Builds).
      Owner's device tests: dylan-todo.md "LAIka App".
- [ ] Needs the owner's decision/accounts: real push notifications (FCM/
      APNs need a Firebase project / Apple account; alternative without
      accounts: the per-person ntfy notifications of 1.2 + the ntfy app),
      Android release signing key (kept like the release key), iOS VPN
      (paid Apple account).

## 1.5: released 2026-10-05

- [x] **Spending limits**: monthly $ per project (project Settings, admin or
      Approve) and per person (Settings -> Users, admin); 80% warning
      notification; at 100% new goals and Conversations are refused
      (default; "warn only" switch per limit); running work finishes.
      Spend = job/planner/assistant cost_usd this calendar month (Claude on a
      subscription reports API-equivalent cost; Codex on ChatGPT often $0).
- [x] **Approve-all for project groups**: on a parent project's page,
      "Approve all N" across the parent and its children (all goals), through
      the merge queue; only projects the person may approve; never LAIka.
- [x] Small: doctor recognises the long-lived Claude token.
- [x] Small: dashboard Repository panel shows the version on release installs.
- [x] Small: dev LAIka tidy-up (Play-test demo, Starter Game, Tailscale).
- [x] Tested 2026-10-04 on laika-dev (API, browser desktop+phone, real
      Conversation turn, two group approve-alls merged). Fixed: approve-all gave
      no feedback on project pages, action results vanished from the banner on
      refresh, months now UTC (API container vs host), read-only limit for
      builders, notifications use project names.
- [x] Owner's follow-ups: SPENDING_LIMITS setting (approvers/admins/off,
      first-run setup + Settings), paused goal box with the reason.
- [x] Shipped 1.5.0 2026-10-05: production deployed, signed, published, CI green.

## Found in the NGINX-Manager trial (2026-10-07/08)

Fixed during the trial (on main, unreleased): split plan over the 10-job
limit failed the goal; project builders tested with LAIka's venv; dashboard
Approve refused on stale main (now queue_approve); commit identity for moved
or imported projects; dashboard caching.

- [x] Run detection: Python packages started with `python -m <pkg>` (a
      `<pkg>/__main__.py` or pyproject [project.scripts]) get no run command,
      so no "Open app" (NGINX-Manager).
- [x] Goal-box Conversation cannot look inside uploaded archives (.tar.gz /
      .zip): it planned around the owner's old suite without reading it.
- [x] Released 1.5.1 (CI failed) and 1.5.2 (2026-10-11, CI green).

## Released in 1.5.3 (2026-10-15)

- Dashboard API reads trimmed job hashes (Lua, one round trip); goal
  status map once per snapshot.
- Orchestrator: idle passes every 3 s; one light read of all jobs per pass,
  candidates re-read in full before acting.
- Release scan: scripts/release-scan-reviewed.txt for findings in history
  (nightly backup push works again).

## 2.0: improvements from Phoenix + more (agreed 2026-10-18; 2.0 per owner 2026-10-19; dev LAIka only for testing)

Reimplemented ideas only (memory phoenix-integration). Order:
- [x] CP1 providers: classify Claude/Codex failures (quota/rate/auth/other)
      with reset-time parsing; per-provider holds until reset (+2 min, caps);
      provider waits and worker-lost interruptions requeue without using a
      build attempt (capped); Claude rate_limit_event -> usage meter + early
      switch; dashboard holds with reset time; run_codex timeout SIGKILL +
      group cleanup after exit.
- [x] CP2 attacker model: gate from the base (fail closed; LAIka's gate
      script from base); candidate safety (refuse escaping symlinks,
      gitlinks, unsafe/.git paths; flag sensitive files to reviewer and
      approval card), host re-check at approve; hardened git; merge exact
      SHA + assert; env allowlist + redaction; Codex/Claude isolation flags.
- [x] CP3 state: execution_id fencing of worker writes; atomic plan
      materialization; transition helper + job event stream (warn-only
      first); job timeline in detail.
- [x] CP4 visibility: acceptance criteria per job (plan schema, unknown
      keys rejected, builder + per-criterion review, approval checklist);
      gate evidence view; "why is this waiting"; routing page; pause all new
      work; while-you-were-away; diagnostics hints; backup catalogue.

## Next after the Phoenix work (owner, 2026-10-19; discuss first)

- [x] 2.0 CP5 accounts (decided 2026-10-19; built 2026-10-19): several Claude and Codex
      accounts per provider, each a subscription login or an API key;
      LAIka spreads runs over them (most room left: fewest running, lowest
      plan usage); concurrency per account (default 2); pauses per account
      (a provider is paused only when all its accounts are); admins only
      (Settings -> AI). Existing login = account "default". Per-account
      homes: CLAUDE_CONFIG_DIR / CODEX_HOME under the laika home; keys
      root:laika 0640 in /etc/laika/providers/accounts/. Record the
      account per job; routing and usage per account.

## Backlog (not scheduled)

- **Off-site backups** (moved from 1.4 on 2026-10-04: the owner first spins
  up another VM to back up to). Guided setup in Settings → Backups: another
  machine over SSH/rsync, a NAS, or S3-compatible storage; test connection;
  encrypted at rest for S3; the monthly restore check also tests the
  off-site copy.

- **Share an app with friends:** a public link to ONE app (e.g. Tank Tumble)
  through a tunnel, on/off switch, optional password; LAIka itself stays
  private. (Asked 2026-10-04, group 3.)
- Planner: splits big jobs (done 1.3.0); watch whether 6 files is right.
- Fedora/RHEL: test RHEL 10 on an x86-64-v3 machine (needs hardware);
  SELinux enforcing on a real install.

## Done (recent)
- 1.4.0 (2026-10-04): see CHANGELOG.md; production runs it.
- 1.3.1 (2026-10-04): project rename.
- 1.3.0 (2026-10-04): SFTP, goal-box Conversation, diagnose-first planner,
  smaller jobs, Needs you on project pages, release-install fixes.
