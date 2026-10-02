#!/bin/bash
# LAIka installer.
#
#   curl -fsSL <release>/install.sh | sudo bash
#   sudo bash install.sh [--source DIR|GIT_URL] [--ref TAG] [--repair] [--yes]
#
# Installs everything LAIka needs on a fresh Ubuntu 22.04 / 24.04 / 26.04 or
# Debian 12 / 13 server (Fedora 40+ and RHEL-compatible 9 are experimental):
# Docker, Python, Node and the AI command-line tools, a dedicated `laika`
# user, LAIka itself in /opt/laika, its data in /var/lib/laika, settings and
# secrets in /etc/laika, and its services. At the end it prints the address
# and a one-time setup code; the rest of the setup happens in the browser.
#
# Safe to run again: it repairs what is missing and never replaces existing
# secrets, settings or data (--repair skips fetching LAIka's code).
#
# LAIka is for your own network or VPN only. Never expose it to the internet.
set -euo pipefail

LAIKA_HOME=/opt/laika
DATA=/var/lib/laika
CONF=/etc/laika
LOGS=/var/log/laika
BACKUPS=/var/backups/laika
VENV=$DATA/venv
USER_NAME=laika
# Where LAIka's code comes from: a release repository (set when 1.0 is
# published) or a local copy (--source DIR).
SOURCE=${LAIKA_SOURCE:-}
REF=${LAIKA_REF:-}
REPAIR=0
ASSUME_YES=${LAIKA_YES:-0}

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --repair) REPAIR=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run the installer as root (sudo)."

# --- the machine --------------------------------------------------------------------------
. /etc/os-release
OS="${ID:-unknown}"; OS_VERSION="${VERSION_ID:-0}"
case "$OS:$OS_VERSION" in
  ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) FAMILY=debian ;;
  fedora:4[0-9]|rhel:9*|rocky:9*|almalinux:9*|centos:9*)
    FAMILY=fedora; warn "$PRETTY_NAME support is experimental." ;;
  *)
    if [ "${LAIKA_FORCE_OS:-0}" = 1 ]; then
      case "${ID_LIKE:-}" in *debian*) FAMILY=debian ;; *fedora*|*rhel*) FAMILY=fedora ;; *) die "Unsupported system: ${PRETTY_NAME:-$OS}" ;; esac
      warn "${PRETTY_NAME:-$OS} is not supported; continuing because LAIKA_FORCE_OS=1."
    else
      die "Unsupported system: ${PRETTY_NAME:-$OS}. Supported: Ubuntu 22.04/24.04/26.04, Debian 12/13 (Fedora 40+, RHEL 9: experimental). LAIKA_FORCE_OS=1 tries anyway."
    fi ;;
esac
case "$(uname -m)" in x86_64|aarch64) ;; *) die "Unsupported CPU: $(uname -m) (x86_64 or aarch64 needed)." ;; esac
[ -d /run/systemd/system ] || die "LAIka needs systemd."
memory_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$memory_mb" -ge 1800 ] || warn "Only ${memory_mb} MB of memory: LAIka needs 2 GB, 4 GB or more is better."
free_gb=$(df -Pk / | awk 'NR==2 {print int($4/1048576)}')
[ "$free_gb" -ge 10 ] || warn "Only ${free_gb} GB free on /: 20 GB or more is recommended."
say "Installing LAIka on ${PRETTY_NAME} ($(uname -m), ${memory_mb} MB memory)"

# --- packages -------------------------------------------------------------------------------
apt_install() { DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@" >/dev/null; }
if [ "$FAMILY" = debian ]; then
  say "System packages"
  apt-get update -q >/dev/null
  apt_install ca-certificates curl gnupg git python3 python3-venv python3-pip bubblewrap openssl \
    util-linux iproute2 iptables rsync tar xz-utils procps
  if ! command -v docker >/dev/null; then
    say "Docker (from docker.com)"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$OS/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$OS ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -q >/dev/null
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  elif ! docker compose version >/dev/null 2>&1; then
    apt_install docker-compose-plugin 2>/dev/null || apt_install docker-compose-v2
  fi
else
  say "System packages"
  dnf -y -q install ca-certificates curl git python3 python3-pip bubblewrap openssl util-linux iproute \
    iptables rsync tar xz procps-ng dnf-plugins-core >/dev/null
  if ! command -v docker >/dev/null; then
    say "Docker (from docker.com)"
    repo=fedora; [ "$OS" = fedora ] || repo=rhel
    dnf config-manager --add-repo "https://download.docker.com/linux/$repo/docker-ce.repo" >/dev/null 2>&1 \
      || dnf config-manager addrepo --from-repofile="https://download.docker.com/linux/$repo/docker-ce.repo" >/dev/null
    dnf -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  fi
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || die "Docker is installed but not running (systemctl status docker)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is needed."

# --- LAIka's code ----------------------------------------------------------------------------
if [ "$REPAIR" = 1 ] && [ -d "$LAIKA_HOME" ]; then
  say "Repairing the installation in $LAIKA_HOME"
elif [ -d "$LAIKA_HOME/.git" ] || [ -f "$LAIKA_HOME/VERSION" ]; then
  say "LAIka is already in $LAIKA_HOME (updates: laika update)"
elif [ -d "$SOURCE" ]; then
  say "Copying LAIka from $SOURCE"
  mkdir -p "$LAIKA_HOME"
  if [ -d "$SOURCE/.git" ] && git -C "$SOURCE" rev-parse --git-dir >/dev/null 2>&1; then
    git clone -q ${REF:+--branch "$REF"} "$SOURCE" "$LAIKA_HOME"
  else
    cp -a "$SOURCE/." "$LAIKA_HOME/"
  fi
else
  [ -n "$SOURCE" ] || die "Tell the installer where LAIka's code is: --source DIR or --source GIT_URL."
  say "Downloading LAIka ${REF:-(latest)}"
  git clone -q --depth 1 ${REF:+--branch "$REF"} "$SOURCE" "$LAIKA_HOME"
fi
[ -f "$LAIKA_HOME/docker-compose.yml" ] || die "$LAIKA_HOME does not contain LAIka."
# shellcheck disable=SC1091
. "$LAIKA_HOME/deploy/versions.env"
# A git checkout of LAIka is read by root services and the laika user alike.
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$LAIKA_HOME" \
  || git config --system --add safe.directory "$LAIKA_HOME"

# --- the laika user and folders --------------------------------------------------------------
if ! id "$USER_NAME" >/dev/null 2>&1; then
  say "Creating the '$USER_NAME' user (agents and tests run as it, never as root)"
  useradd --system --user-group --home-dir "$DATA/home" --shell /usr/sbin/nologin "$USER_NAME"
fi
say "Folders and permissions"
install -d -m 0755 -o root -g root "$DATA"
for dir in projects project-data worktrees uploads reference; do
  install -d -m 2770 -o "$USER_NAME" -g "$USER_NAME" "$DATA/$dir"
done
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$DATA/home"
install -d -m 0755 -o root -g root "$DATA/branding"
install -d -m 0700 -o root -g root "$DATA/trash" "$DATA/db" "$BACKUPS"
install -d -m 2770 -o "$USER_NAME" -g "$USER_NAME" "$LOGS" "$LOGS/jobs" "$LOGS/integration" "$LOGS/assist"
install -d -m 0750 -o root -g "$USER_NAME" "$CONF"
install -d -m 0700 -o root -g root "$CONF/providers" "$CONF/notify" "$CONF/project-env" "$CONF/project-keys"
# Both sides may write the same repositories (services/laika_user.py).
printf '[safe]\n\tdirectory = *\n[init]\n\tdefaultBranch = main\n' > "$CONF/gitconfig"
chmod 0644 "$CONF/gitconfig"

# --- secrets and settings (created once, never replaced) ---------------------------------------
secret() { openssl rand -hex 24; }
new_file() { # path content mode
  [ -s "$1" ] && return 0
  (umask 077; printf '%s\n' "$2" > "$1"); chmod "$3" "$1"
}
new_file "$CONF/redis.env" "REDIS_PASSWORD=$(secret)" 600
new_file "$CONF/operator.env" "LAIKA_OPERATOR_TOKEN=$(secret)" 600
new_file "$CONF/compose.env" "POSTGRES_DB=laika
POSTGRES_USER=laika
POSTGRES_PASSWORD=$(secret)
REDIS_URL=redis://redis:6379/0
LAIKA_DB_DIR=$DATA/db" 600
new_file "$CONF/laika.env" "# Install-wide settings (most settings live in the dashboard).
# Production: no built-in LAIka project.
LAIKA_BUILTIN_PROJECT=0" 600
ln -sfn "$CONF/compose.env" "$LAIKA_HOME/.env"

# --- Python ---------------------------------------------------------------------------------------
say "Python environment"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check -r "$LAIKA_HOME/deploy/requirements-host.lock"

# --- Node and the AI command-line tools ------------------------------------------------------------
node_major=$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true)
if [ "${node_major:-0}" -lt 20 ]; then
  say "Node.js $LAIKA_NODE_VERSION"
  arch=$(uname -m); [ "$arch" = x86_64 ] && arch=x64 || arch=arm64
  tarball="node-v$LAIKA_NODE_VERSION-linux-$arch.tar.xz"
  curl -fsSL "https://nodejs.org/dist/v$LAIKA_NODE_VERSION/$tarball" -o "/tmp/$tarball"
  mkdir -p /opt/laika-node && tar -xJf "/tmp/$tarball" -C /opt/laika-node --strip-components=1 && rm -f "/tmp/$tarball"
  ln -sfn /opt/laika-node/bin/node /usr/local/bin/node
  ln -sfn /opt/laika-node/bin/npm /usr/local/bin/npm
  ln -sfn /opt/laika-node/bin/npx /usr/local/bin/npx
fi
want_cli() { # command package version: install when missing or older (never downgrade)
  local have; have=$("$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
  if [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$3" "$have" | sort -V | head -1)" = "$3" ]; then return 0; fi
  say "$1 $3"
  # /usr/local: on everyone's PATH, whichever Node is installed.
  npm install -g --prefix /usr/local -s --no-fund --no-audit "$2@$3" >/dev/null
}
want_cli codex @openai/codex "$LAIKA_CODEX_VERSION"
want_cli claude @anthropic-ai/claude-code "$LAIKA_CLAUDE_VERSION"

# --- containers -------------------------------------------------------------------------------------
say "Database, API and dashboard (Docker)"
# Postgres 18 creates its data folder as its own user: the mount must be
# enterable (the data inside is 0700 postgres). Redis takes its folder over.
install -d -m 0700 "$DATA/db/redis"
install -d -m 0755 "$DATA/db/postgres"
( cd "$LAIKA_HOME" && docker compose up -d --build --wait --quiet-pull >/dev/null 2>&1 ) \
  || ( cd "$LAIKA_HOME" && docker compose up -d --build --wait ) \
  || die "The containers did not start (cd $LAIKA_HOME && docker compose logs)."

# --- services ---------------------------------------------------------------------------------------
say "Services"
for unit in "$LAIKA_HOME"/deploy/systemd/*.service "$LAIKA_HOME"/deploy/systemd/*.timer; do
  install -m 0644 "$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now laika-orchestrator laika-operator laika-apps laika-scaler >/dev/null 2>&1
for timer in backup watchdog prune notify digest restore-check; do
  systemctl enable --now "laika-$timer.timer" >/dev/null 2>&1
done
ln -sfn "$LAIKA_HOME/scripts/laika" /usr/local/bin/laika

# --- done ------------------------------------------------------------------------------------------------
for _ in $(seq 1 30); do
  curl -fsS -m 3 http://127.0.0.1:8080/health >/dev/null 2>&1 && break
  sleep 2
done
addresses=$(ip -4 -o addr show scope global 2>/dev/null | awk '$2 !~ /^(docker|br-|veth|laika-build)/ {print $4}' | cut -d/ -f1 || true)
echo
say "LAIka is installed ($(cat "$LAIKA_HOME/VERSION" 2>/dev/null || echo dev))."
echo
echo "  Open the dashboard from a computer on this network:"
for address in $addresses; do echo "      http://$address:8080"; done
code=$("$VENV/bin/python" "$LAIKA_HOME/scripts/laika-admin.py" setup-code 2>/dev/null || true)
if [ -n "$code" ]; then
  echo
  echo "  One-time setup code (valid 24 hours):  $code"
  echo "  A new one any time:                    sudo laika setup-code"
fi
echo
echo "  Check the installation:  sudo laika doctor"
echo
warn "Keep LAIka on your own network or VPN. Never forward port 8080 or expose it to the internet."
