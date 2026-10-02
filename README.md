# LAIka

**A self-hosted AI software builder.** Describe what you want; LAIka plans it
into small jobs, AI agents build each one in an isolated copy of your
repository, an independent AI reviewer checks the exact result, and nothing
reaches your code until **you** approve it.

LAIka runs on your own Linux server, uses your own AI subscriptions
(Claude and/or ChatGPT/Codex) or API keys, and is managed from a web
dashboard that works on desktop, tablet and phone.

> ⚠️ **LAIka is for your own network or VPN only. Never expose it to the
> internet.** It writes and runs code on your server. Running it on a
> public address is unsupported and entirely at your own risk; see
> [Security](docs/security.md).

## What it does

- **Goals in plain words.** Type what you want, or let the goal assistant
  ask a few questions and write a precise brief for you.
- **Planned and parallel.** Goals are split into jobs that run side by side
  when they don't touch the same files; workers are added and removed
  automatically as work comes and goes.
- **Built safely.** Every job works in its own git worktree inside a
  sandbox as an unprivileged user: no access to LAIka's secrets, other
  projects or the rest of the server; tests run offline unless you allow
  internet access.
- **Reviewed for real.** Each change is integrated onto the latest main and
  reviewed by a separate AI against the exact integrated result, with the
  test results in hand. Findings are repaired automatically, within limits.
- **You approve.** One click merges a change, or approve everything a goal
  produced through the merge queue. Nothing ever merges on its own. Undo
  is one click too.
- **Projects of any kind.** Web apps, APIs, bots, CLIs, libraries, mobile
  and desktop apps, games and more: LAIka detects the stack, runs web apps
  for you, shows previews of pending changes, and builds downloadable
  artifacts.
- **Groups.** Related projects (an app and its API) can be planned together,
  see each other's code read-only, and be approved and built as a group.
- **Everything at a glance.** Live job logs, history, activity, usage and
  costs, files, notifications (Discord, ntfy) and a weekly digest.
- **Phones.** Pair the LAIka app with a revocable per-device key.

## Requirements

- A Linux server or VM: **Ubuntu 22.04, 24.04 or 26.04, or Debian 12 or 13**
  (x86_64 or arm64). Fedora 40+ and RHEL 9 compatibles are experimental.
- 2 CPUs and 4 GB memory minimum; 4+ CPUs, 8 GB and 40 GB disk recommended.
- Internet access for the server (to install, and for the AI providers).
- A Claude Pro/Max subscription or Anthropic API key, and/or a ChatGPT plan
  or OpenAI API key.

## Install

On the server, as root:

```sh
curl -fsSL https://<release-url>/install.sh | sudo bash
```

or from a copy of this repository:

```sh
sudo bash install.sh --source /path/to/laika
```

The installer sets up Docker, Python, Node and the AI command-line tools,
creates a dedicated `laika` user, installs LAIka to `/opt/laika` with its
data in `/var/lib/laika`, starts everything and prints the dashboard
address with a **one-time setup code**. Open the address from a computer on
the same network, enter the code, create your account, and follow the
setup guide (AI sign-in, capacity, notifications, backups).

Details: [Installation](docs/install.md).

## Everyday commands

```sh
sudo laika doctor          # check the installation, with fixes
sudo laika status          # services at a glance
sudo laika setup-code      # a new first-run setup code
sudo laika reset-password  # forgot the dashboard password
sudo laika repair          # put back missing pieces
sudo laika uninstall       # remove LAIka (keeps your data unless --purge)
```

## Documentation

| | |
| --- | --- |
| [Installation](docs/install.md) | requirements, install, first run, repair, uninstall |
| [Using LAIka](docs/using.md) | projects, goals, approvals, groups, previews, builds |
| [Configuration](docs/configuration.md) | every setting, branding, files in `/etc/laika` |
| [Workers](docs/workers.md) | how many jobs run at once and automatic scaling |
| [Security](docs/security.md) | the network rule, sign-in, sandboxing, secrets |
| [Operations](docs/operations.md) | backups, restore checks, health, logs, commands |
| [Phones](docs/phones.md) | pairing the LAIka app, VPN access |
| [Troubleshooting](docs/troubleshooting.md) | common problems and fixes |
| [Architecture](docs/architecture.md) | how the pipeline works and what it guarantees |

## Privacy

LAIka has **no telemetry**. It talks only to the AI providers you sign in to,
the package registries your projects use, and the notification services you
configure.

## License

MIT: see [LICENSE](LICENSE). Security reports: [SECURITY.md](SECURITY.md).

## Contributing

Every change goes through tests, an independent review and human approval;
see [Architecture](docs/architecture.md) for the contract that keeps it safe.
Never commit keys, tokens or credentials: `git config core.hooksPath
scripts/hooks` installs a pre-push check that refuses anything that looks
like one.
