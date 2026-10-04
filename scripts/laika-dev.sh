#!/bin/bash
# A development LAIka beside the real one: a whole separate installation in
# its own systemd container (its own Docker, Redis, database, workers, users
# and AI sign-ins), so changes can be tried before they reach the server
# people use. Nothing in it touches the host's LAIka.
#
#   scripts/laika-dev.sh create [REF]   build the machine and install REF (default: this checkout's HEAD)
#   scripts/laika-dev.sh deploy [REF]   install REF over it (data, users and sign-ins are kept)
#   scripts/laika-dev.sh status         container, version, health
#   scripts/laika-dev.sh setup-code     a one-time setup code for its first administrator
#   scripts/laika-dev.sh shell          a root shell inside (laika doctor, logs, ...)
#   scripts/laika-dev.sh stop | start   pause it (frees memory) / resume it
#   scripts/laika-dev.sh destroy        delete the machine AND all of its data
#
# Dashboard: http://<this server>:DEV_PORT (default 9080). Project apps,
# which run on 8100-8139 inside, are published on 9100-9139 (8105 -> 9105),
# previews 8200-8209 on 9200-9209. Limits: DEV_MEMORY (3g), DEV_CPUS (2), so
# it cannot starve the real LAIka. Needs Docker and root.
set -euo pipefail

NAME=${DEV_NAME:-laika-dev}
PORT=${DEV_PORT:-9080}
MEMORY=${DEV_MEMORY:-3g}
CPUS=${DEV_CPUS:-2}
IMAGE=laika-dev-base:ubuntu-24.04
REPO=$(cd "$(dirname "$0")/.." && pwd)

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
inside() { docker exec "$NAME" bash -lc "$*"; }
exists() { docker container inspect "$NAME" >/dev/null 2>&1; }
running() { [ "$(docker container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = true ]; }

[ "$(id -u)" = 0 ] || die "Run it as root (sudo)."

copy_source() { # REF -> /root/laika-src inside, a plain copy of that commit
  local ref=${1:-HEAD} work
  git -C "$REPO" rev-parse --verify -q "$ref^{commit}" >/dev/null || die "Unknown commit: $ref"
  work=$(mktemp -d)
  git -C "$REPO" archive --format=tar --prefix=laika-src/ "$ref" > "$work/src.tar"
  inside "rm -rf /root/laika-src"
  docker cp "$work/src.tar" "$NAME:/root/src.tar" >/dev/null
  inside "tar -xf /root/src.tar -C /root && rm /root/src.tar"
  rm -rf "$work"
  say "Source: $(git -C "$REPO" log -1 --format='%h %s' "$ref")"
}

wait_for_systemd() {
  for _ in $(seq 1 60); do
    case "$(inside 'systemctl is-system-running' 2>/dev/null || true)" in running|degraded) return 0 ;; esac
    sleep 2
  done
  die "systemd did not start in $NAME"
}

create() {
  exists && die "$NAME already exists (deploy updates it; destroy removes it)."
  say "Base machine (Ubuntu 24.04 with systemd)"
  local work; work=$(mktemp -d)
  cat > "$work/Dockerfile" <<'EOF'
FROM ubuntu:24.04
RUN apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q systemd systemd-sysv dbus iproute2 sudo ca-certificates curl git rsync && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
  docker build -q -t "$IMAGE" "$work" >/dev/null
  rm -rf "$work"
  say "Starting $NAME (memory $MEMORY, $CPUS CPUs, dashboard on port $PORT)"
  # Data lives in named volumes, so stop/start and deploys keep it.
  docker run -d --name "$NAME" --hostname "$NAME" --privileged --cgroupns=private --restart unless-stopped \
    --tmpfs /run --tmpfs /run/lock --memory="$MEMORY" --cpus="$CPUS" \
    -v "$NAME-docker:/var/lib/docker" -v "$NAME-containerd:/var/lib/containerd" \
    -v "$NAME-data:/var/lib/laika" -v "$NAME-etc:/etc/laika" -v "$NAME-logs:/var/log/laika" \
    -v "$NAME-backups:/var/backups/laika" -v "$NAME-opt:/opt" \
    -p "$PORT:8080" -p 9100-9139:8100-8139 -p 9200-9209:8200-8209 "$IMAGE" >/dev/null
  wait_for_systemd
  copy_source "${1:-HEAD}"
  say "Installing LAIka inside (a few minutes)"
  inside "bash /root/laika-src/install.sh --source /root/laika-src --yes" | grep -E '==>|!!|xx' || true
  inside "test -f /opt/laika/VERSION" || die "The installation failed: scripts/laika-dev.sh shell, then look at the output of install.sh"
  status
  echo
  say "Open http://$(hostname -I | awk '{print $1}'):$PORT and use this setup code:"
  setup_code
}

deploy() {
  exists || die "No $NAME yet: scripts/laika-dev.sh create"
  running || docker start "$NAME" >/dev/null
  wait_for_systemd
  copy_source "${1:-HEAD}"
  say "Installing the new code (data, users and sign-ins are kept)"
  # Like an update: replace the program files, keep the .env link, repair, restart.
  inside "rsync -a --delete --exclude=.env /root/laika-src/ /opt/laika/ && bash /opt/laika/install.sh --repair --yes" \
    | grep -E '==>|!!|xx' || true
  inside "/opt/laika/scripts/laika-restart.sh --wait 60 all" >/dev/null 2>&1 || true
  status
}

status() {
  exists || { echo "$NAME: not created"; return 0; }
  running || { echo "$NAME: stopped (scripts/laika-dev.sh start)"; return 0; }
  echo "$NAME: running · LAIka $(inside 'cat /opt/laika/VERSION 2>/dev/null' || echo '?') · http://$(hostname -I | awk '{print $1}'):$PORT"
  inside "laika doctor --json" 2>/dev/null | python3 -c '
import json, sys
try:
    checks = json.load(sys.stdin)
except ValueError:
    sys.exit("  doctor did not run")
bad = [c for c in checks if c["level"] != "ok"]
print("  doctor: all good" if not bad else "  doctor: " + "; ".join(f"{c[\"name\"]} ({c[\"level\"]}): {c[\"detail\"]}" for c in bad))'
}

setup_code() {
  inside "laika setup-code" 2>/dev/null || echo "  (an administrator exists already: sign in, or: scripts/laika-dev.sh shell, then laika reset-password)"
}

case "${1:-}" in
  create) create "${2:-}" ;;
  deploy) deploy "${2:-}" ;;
  status) status ;;
  setup-code) setup_code ;;
  shell) docker exec -it "$NAME" bash -l ;;
  stop) docker stop "$NAME" >/dev/null && echo "$NAME stopped (its data is kept)" ;;
  start) docker start "$NAME" >/dev/null && wait_for_systemd && status ;;
  destroy)
    exists || die "No $NAME"
    if [ "${2:-}" != "--yes" ]; then
      read -r -p "Delete $NAME and ALL its data (projects, users, sign-ins)? Type its name: " answer
      [ "$answer" = "$NAME" ] || { echo "Nothing changed."; exit 1; }
    fi
    docker rm -f "$NAME" >/dev/null
    docker volume rm "$NAME-docker" "$NAME-containerd" "$NAME-data" "$NAME-etc" "$NAME-logs" "$NAME-backups" "$NAME-opt" >/dev/null
    echo "$NAME and its data are deleted" ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
