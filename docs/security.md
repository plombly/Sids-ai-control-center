# Security

## The rule: your network or VPN only

**LAIka must never be reachable from the internet.** It plans and writes
code, runs that code's tests and apps on your server, and holds your AI
accounts. It is designed for one owner on a private network.

- Reach the dashboard on your LAN, or from outside **through a VPN**
  (WireGuard, OpenVPN, Tailscale…). The LAIka phone app connects through
  your VPN too.
- Do not forward port 8080 (or the app ports 8100–8299) on your router,
  and do not put LAIka behind a public reverse proxy or tunnel.
- On a cloud server, firewall every LAIka port to your VPN only.
  `sudo laika doctor`, the setup guide and the health page warn when the
  server has a public address.

Running LAIka on a public address is **unsupported**. If you choose to do
it anyway, you take full responsibility for the consequences.

## Signing in

- One administrator account, created with a one-time setup code that only
  someone with root on the server can print (`sudo laika setup-code`).
  Until it exists, LAIka answers nothing but the setup page.
- Passwords are stored as scrypt hashes. Sessions are HttpOnly,
  SameSite=Strict cookies; writes from other websites are refused.
- Failed sign-ins and setup codes are rate-limited per address.
- **Settings → Access** lists signed-in browsers (end any of them),
  changes the password (signing out every other browser) and shows the
  audit log of every change. Forgot the password:
  `sudo laika reset-password` on the server.
- Phones use their own key per device, revocable in **Settings → Phones**.
  Phones can submit goals and answer questions, but never approve changes
  or change settings.

## What runs as what

| Runs as | What |
| --- | --- |
| `laika` (unprivileged) | workers, AI agents, tests, your projects' apps and previews, the goal assistant, AI sign-ins |
| root | the control services that start units, run Docker and set firewall rules (operator, apps, scaler, backups, health) |
| Docker | Redis, Postgres, the API, the dashboard, project builds |

Agents and tests also run in a **sandbox** (bubblewrap): the server is
read-only, LAIka's own files, settings, secrets, logs, backups and every
other project are hidden, the Docker socket and deploy keys are masked,
and only the job's own worktree is writable. Tests have **no internet**
unless you allow it for a project (the dashboard asks when tests seem to
need it). Builds run in containers on a network that can reach the
internet but not your LAN or the server. Apps run with memory, CPU and
process limits.

## Secrets

- `/etc/laika/*.env` (database and Redis passwords, the operator token,
  AI API keys, notification webhooks, project secrets) are readable by
  root only. Services receive them through systemd, and every sandbox
  strips them from its environment. AI keys reach only the AI tools.
- Project secrets (Settings → Secrets on a project) are write-only in the
  dashboard and reach only that project's app.
- Backups contain secrets and are readable by root only. If you copy them
  off the server, keep them encrypted.

## Human approval is mandatory

No change reaches a project's main branch without you approving the exact
integrated, tested and independently reviewed result. "Approve all" queues
each change; each is merged only after its tests and a fresh review pass on
the latest main. See [Architecture](architecture.md).

## No telemetry

LAIka sends nothing anywhere except to the AI providers you sign in to,
package registries your projects use, git remotes you configure, and the
notification services you set up.

## Reporting a problem

Report security problems privately to the maintainers (see the repository's
security policy) rather than in a public issue.
