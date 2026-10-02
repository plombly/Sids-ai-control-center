#!/bin/bash
# Move an existing LAIka install that runs everything as root onto the
# dedicated `laika` user (v1.0). One time, as root, while no worker is busy:
#
#   sudo bash scripts/laika-migrate-user.sh [--keep-builtin]
#
#   1. refuses while a worker is busy; takes a backup first
#   2. stops LAIka's services (apps keep running: they are their own units)
#   3. creates the laika user; moves /opt/laika/.env to /etc/laika/compose.env
#   4. hands LAIka's data to the laika group (owners, setgid folders,
#      group-shared repositories; deploy keys stay root-only)
#   5. moves root's Codex login to the laika user (Claude: sign in again in
#      Settings -> AI; /root/.claude is left alone)
#   6. runs install.sh --repair (units, venv, containers) and starts LAIka
#   7. runs laika doctor
#
# --keep-builtin keeps the built-in LAIka project (development servers).
set -euo pipefail

REPO=${REPO:-/opt/laika}
DATA=/var/lib/laika
CONF=/etc/laika
CLI="$REPO/scripts/laika-redis-cli"
KEEP_BUILTIN=0
[ "${1:-}" = "--keep-builtin" ] && KEEP_BUILTIN=1
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# 1. Nothing in flight, and a backup.
busy=""
for key in $("$CLI" --scan --pattern 'laika:workers:laika-worker-*' 2>/dev/null); do
  [ "$("$CLI" hget "$key" status 2>/dev/null)" = working ] && busy="$busy ${key#laika:workers:}"
done
[ -z "$busy" ] || { echo "Workers are busy:$busy. Nothing changed; try again when they are idle." >&2; exit 1; }
say "Backup first"
systemctl start laika-backup.service
echo "    $(ls -1t /var/backups/laika/snapshots | head -1)"

# 2. Stop LAIka (not the apps, not the containers).
say "Stopping LAIka's services"
systemctl stop laika-scaler laika-orchestrator laika-operator laika-apps 2>/dev/null || true
mapfile -t workers < <(systemctl list-units --plain --no-legend 'laika-worker@*.service' | awk '{print $1}')
[ ${#workers[@]} -eq 0 ] || systemctl stop "${workers[@]}"

# 3. The user, and the compose settings out of the program folder.
if ! id laika >/dev/null 2>&1; then
  say "Creating the laika user"
  useradd --system --user-group --home-dir "$DATA/home" --shell /usr/sbin/nologin laika
fi
install -d -m 0750 -o root -g laika "$CONF"
if [ -f "$REPO/.env" ] && [ ! -L "$REPO/.env" ]; then
  say "Moving $REPO/.env to $CONF/compose.env"
  mv "$REPO/.env" "$CONF/compose.env"
  chmod 600 "$CONF/compose.env"
fi
if [ ! -s "$CONF/laika.env" ]; then
  if [ $KEEP_BUILTIN = 1 ]; then
    printf '# Install-wide settings.\n# Development server: keeps the built-in LAIka project.\nLAIKA_BUILTIN_PROJECT=1\n# Development checkouts hidden from project sandboxes.\nLAIKA_SANDBOX_HIDE=/opt/sid-dev\n' > "$CONF/laika.env"
  else
    printf '# Install-wide settings.\nLAIKA_BUILTIN_PROJECT=0\n' > "$CONF/laika.env"
  fi
  chmod 600 "$CONF/laika.env"
fi

# 4. Data to the laika group.
say "Handing LAIka's data to the laika user and group"
for dir in projects project-data worktrees uploads reference; do
  [ -d "$DATA/$dir" ] || continue
  chown -R laika:laika "$DATA/$dir"
  chmod -R g+rwX,o-rwx "$DATA/$dir"
  find "$DATA/$dir" -type d -exec chmod g+s {} +
done
find "$DATA/projects" -maxdepth 2 -name 'deploy_key*' -exec chown root:root {} + -exec chmod 600 {} + 2>/dev/null || true
for repo in "$DATA"/projects/*/repo "$REPO"; do
  [ -d "$repo/.git" ] || continue
  git -C "$repo" config core.sharedRepository group
done
# LAIka's own repository (development servers build it with workers).
if [ $KEEP_BUILTIN = 1 ] && [ -d "$REPO/.git" ]; then
  chgrp -R laika "$REPO/.git"
  chmod -R g+rwX "$REPO/.git"
  find "$REPO/.git" -type d -exec chmod g+s {} +
fi
install -d -m 2770 -o laika -g laika /var/log/laika
chown -R laika:laika /var/log/laika
chmod -R g+rwX,o-rwx /var/log/laika
find /var/log/laika -type d -exec chmod g+s {} +

# 5. Codex's login moves with the agents (one owner: its tokens refresh).
install -d -m 0700 -o laika -g laika "$DATA/home"
if [ -d /root/.codex ] && [ ! -e "$DATA/home/.codex" ]; then
  say "Moving the Codex login to the laika user"
  mv /root/.codex "$DATA/home/.codex"
  chown -R laika:laika "$DATA/home/.codex"
fi

# 6. Units, venv, containers, start.
say "Repairing the installation (units, Python, containers)"
bash "$REPO/install.sh" --repair --yes
systemctl restart laika-orchestrator laika-operator laika-apps laika-scaler

# 7. Check.
say "Doctor"
"$DATA/venv/bin/python" "$REPO/scripts/laika-doctor.py" || true
echo
echo "Next: sign Claude in for the laika user: dashboard -> Settings -> AI -> Sign in with your Claude account."
