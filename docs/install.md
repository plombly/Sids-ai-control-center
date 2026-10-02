# Installation

## Before you start

**Put LAIka on a machine only you (and people you trust) can reach**: a
server on your home or office network, or a VPS reachable only through your
VPN. LAIka writes and runs code; it must never be reachable from the
internet. See [Security](security.md).

You need:

| | Minimum | Recommended |
| --- | --- | --- |
| System | Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13 | Ubuntu 24.04 LTS |
| CPU | 2 cores, x86_64 or arm64 | 4+ cores |
| Memory | 4 GB | 8 GB or more |
| Disk | 20 GB free | 40 GB+ (projects, builds, backups) |
| Network | outbound internet | a VPN for remote access |

Fedora 40+ and RHEL 9 compatibles (Rocky, Alma) install too but are
experimental. Other systems are refused unless you set `LAIKA_FORCE_OS=1`.

You also need at least one AI provider: a **Claude** Pro or Max subscription
(or an Anthropic API key) and/or a **ChatGPT** plan with Codex (or an OpenAI
API key). Both work best: by default Claude plans and reviews, Codex builds.

## Install

```sh
curl -fsSL https://<release-url>/install.sh | sudo bash
```

From a local copy (a release archive or a git checkout):

```sh
sudo bash install.sh --source /path/to/laika      # a folder
sudo bash install.sh --source https://…/laika.git --ref v1.0.0
```

The installer takes 5–15 minutes and is safe to run again. It:

1. installs system packages (git, Python, bubblewrap, iptables…), Docker
   from docker.com if it is missing, Node.js 22 if yours is older than 20,
   and the `codex` and `claude` command-line tools at tested versions
   (`deploy/versions.env`);
2. creates the `laika` system user: agents, tests and your projects' apps
   run as this user, never as root;
3. puts LAIka in `/opt/laika`, data in `/var/lib/laika`, settings and
   secrets in `/etc/laika` (generated once, readable by root only), logs in
   `/var/log/laika`, backups in `/var/backups/laika`;
4. builds a Python environment from pinned versions
   (`deploy/requirements.lock`);
5. starts Redis, Postgres, the API and the dashboard in Docker, and
   LAIka's services in systemd;
6. prints the dashboard address and a **one-time setup code**.

## First run

1. Open `http://<server-address>:8080` from a computer on the same network.
2. Enter the setup code and create the administrator account. Lost the
   code? `sudo laika setup-code` prints a new one (valid 24 hours).
3. The setup guide walks you through:
   - **Name & look**: the server's name, theme and accent colour.
   - **AI providers**: sign in with your Claude and/or ChatGPT account
     (the page shows a link and a code), or paste API keys.
   - **Capacity**: automatic workers (recommended) or a fixed number.
   - **Notifications**: a Discord webhook or ntfy topic (optional).
   - **Backups**: time, how many to keep, an optional off-site copy.
   - **Safety check**: warns if the server has a public address.
4. Create your first project: empty, or cloned from a git repository.

Everything in the guide can be changed later in **Settings**.

## Check the installation

```sh
sudo laika doctor
```

checks the system, folders and permissions, secrets, tools, containers,
services, workers, the API, the sandbox, AI sign-in and network exposure,
and prints a fix for anything wrong. It changes nothing.

## Repair

```sh
sudo laika repair
```

re-runs the installer without fetching new code: missing packages, folders,
permissions, services and containers are put back. Your secrets, settings
and data are never replaced.

## Updating

```sh
sudo laika update
```

takes a backup, installs the new release and restarts LAIka safely
(running jobs finish first). The dashboard shows when an update is
available.

## Uninstall

```sh
sudo laika uninstall           # removes LAIka, KEEPS your data
sudo laika uninstall --purge   # also deletes projects, databases, settings,
                               # logs, backups and the laika user
```

Without `--purge`, your projects, databases, settings and backups stay in
`/var/lib/laika`, `/etc/laika` and `/var/backups/laika`; installing again
picks them up. Docker, Node and the AI tools are never removed.

## Where things are

| Path | What |
| --- | --- |
| `/opt/laika` | LAIka itself (replaced by updates) |
| `/var/lib/laika/projects/<id>` | each project: repository, worktrees, builds, logs |
| `/var/lib/laika/project-data/<id>` | each app's own data folder |
| `/var/lib/laika/db` | Redis and Postgres data |
| `/var/lib/laika/home` | the laika user's home (AI tool sign-ins) |
| `/etc/laika` | settings and secrets (root only) |
| `/var/log/laika` | job logs, test reports |
| `/var/backups/laika/snapshots` | daily backups |
