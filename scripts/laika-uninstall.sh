#!/bin/bash
# Remove LAIka from this server.
#
#   sudo laika uninstall            stop and remove LAIka; KEEP your data
#   sudo laika uninstall --purge    also delete projects, databases, settings,
#                                   secrets, logs, backups and the laika user
#   --yes                           do not ask
#
# Kept by default (an install later picks them up again):
#   /var/lib/laika (projects, app data, databases), /etc/laika (settings,
#   secrets), /var/log/laika, /var/backups/laika and the laika user.
# Never removed: Docker, Node, the codex and claude tools, system packages.
set -uo pipefail

PURGE=0
YES=${LAIKA_YES:-0}
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 2; }

LAIKA_HOME=/opt/laika
# A development checkout (other worktrees share its repository) is never
# deleted by an uninstall.
if [ -d "$LAIKA_HOME/.git" ] && [ "$(git -C "$LAIKA_HOME" worktree list 2>/dev/null | wc -l)" -gt 1 ]; then
  echo "$LAIKA_HOME is a development repository with other worktrees; refusing to uninstall." >&2
  exit 1
fi
if [ "$YES" != 1 ]; then
  if [ "$PURGE" = 1 ]; then
    echo "This removes LAIka AND DELETES every project, database, setting, log and backup."
    read -r -p "Type 'delete everything' to continue: " answer < /dev/tty
    [ "$answer" = "delete everything" ] || { echo "Nothing changed."; exit 1; }
  else
    read -r -p "Remove LAIka from this server (your data is kept)? [y/N] " answer < /dev/tty
    [[ "$answer" =~ ^[Yy] ]] || { echo "Nothing changed."; exit 1; }
  fi
fi

echo "==> Stopping LAIka's services, apps and previews"
mapfile -t units < <(systemctl list-units --all --plain --no-legend 'laika-*' | awk '{print $1}')
mapfile -t files < <(systemctl list-unit-files --plain --no-legend 'laika-*' | awk '{print $1}')
systemctl disable --now "${files[@]}" >/dev/null 2>&1 || true
systemctl stop "${units[@]}" >/dev/null 2>&1 || true
systemctl reset-failed 'laika-*' >/dev/null 2>&1 || true
for file in "${files[@]}"; do
  rm -f "/etc/systemd/system/$file"
done
rm -rf /etc/systemd/system/laika-*.d
systemctl daemon-reload

echo "==> Removing the containers"
if [ -f "$LAIKA_HOME/docker-compose.yml" ]; then
  (cd "$LAIKA_HOME" && docker compose down --remove-orphans >/dev/null 2>&1) || true
fi
docker rm -f laika-api laika-web laika-redis laika-postgres >/dev/null 2>&1 || true
# Build containers and their network (scripts/laika-build.py).
docker network rm laika-build-net >/dev/null 2>&1 || true
iptables -D DOCKER-USER -i laika-build0 -j LAIKA-BUILD-FWD >/dev/null 2>&1 || true
iptables -D INPUT -i laika-build0 -j LAIKA-BUILD-IN >/dev/null 2>&1 || true
for chain in LAIKA-BUILD-FWD LAIKA-BUILD-IN; do
  iptables -F "$chain" >/dev/null 2>&1 && iptables -X "$chain" >/dev/null 2>&1 || true
done

echo "==> Removing LAIka's program files"
rm -f /usr/local/bin/laika
rm -rf "$LAIKA_HOME"

if [ "$PURGE" = 1 ]; then
  echo "==> Deleting data, settings, logs, backups and the laika user"
  docker volume ls -q | grep '^laika-build-cache-' | xargs -r docker volume rm >/dev/null 2>&1 || true
  docker image ls --format '{{.Repository}}' | grep -E '^laika-(api|web)$' | xargs -r docker image rm >/dev/null 2>&1 || true
  rm -rf /var/lib/laika /etc/laika /var/log/laika /var/backups/laika
  userdel laika >/dev/null 2>&1 || true
  groupdel laika >/dev/null 2>&1 || true
  echo "LAIka is removed, with all of its data."
else
  echo "LAIka is removed. Your data is still in /var/lib/laika, /etc/laika and /var/backups/laika;"
  echo "installing LAIka again picks it up. To delete it as well, remove those folders and run: userdel laika"
fi
