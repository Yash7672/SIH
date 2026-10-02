#!/usr/bin/env bash
# RAKSHAK - stop (Linux / macOS). Mirrors scripts/stop.ps1.
#   ./scripts/stop.sh [--mode docker|local|auto] [--ports 8000,5173,5174,8081] [--volumes]
set -uo pipefail

MODE="auto"
PORTS="8000,5173,5174,8081"
REMOVE_VOLUMES=0

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--mode)     MODE="${2:-auto}"; shift 2 ;;
    --mode=*)      MODE="${1#*=}"; shift ;;
    --ports)       PORTS="${2:-}"; shift 2 ;;
    --ports=*)     PORTS="${1#*=}"; shift ;;
    --volumes|-v)  REMOVE_VOLUMES=1; shift ;;
    -h|--help)     sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$REPO_ROOT/.rakshak"
PIDS_TSV="$STATE_DIR/pids.tsv"

step() { printf '  -> %s\n' "$1"; }
ok()   { printf '  [ OK ] %s\n' "$1"; }
info() { printf '  [info] %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1" >&2; }
err()  { printf '  [FAIL] %s\n' "$1" >&2; }

port_pid() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$port" 2>/dev/null | tr -d ' ' | head -1
    return
  fi
  netstat -an 2>/dev/null | awk -v p="$port" '$2 ~ p && $4 == "LISTEN" {print $7}' | head -1
}

kill_tree() {
  local pid="$1"
  [ "$pid" -gt 0 ] 2>/dev/null || return 0
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  sleep 0.5
  pkill -KILL -P "$pid" 2>/dev/null || true
  kill -KILL "$pid" 2>/dev/null || true
}

echo
echo "---------------------------------------------------------------------------"
echo "  RAKSHAK - stop"
echo "---------------------------------------------------------------------------"

docker_ready=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then docker_ready=1; fi

recorded_mode=""
[ -f "$STATE_DIR/mode" ] && recorded_mode="$(cat "$STATE_DIR/mode")"

if [ "$MODE" = "auto" ]; then
  if [ -n "$recorded_mode" ]; then EFFECTIVE="$recorded_mode"
  elif [ "$docker_ready" -eq 1 ]; then EFFECTIVE="docker"
  else EFFECTIVE="local"; fi
else
  EFFECTIVE="$MODE"
fi

info "mode: $EFFECTIVE"

stopped_list=""
add_stopped() { stopped_list="${stopped_list}    - $1\n"; }

if [ "$EFFECTIVE" = "docker" ]; then
  if [ "$docker_ready" -eq 0 ]; then
    warn "Docker is not running, so there is nothing to stop for the container stack"
    EFFECTIVE="local"
  else
    step "docker compose down"
    if [ "$REMOVE_VOLUMES" -eq 1 ]; then
      ( cd "$REPO_ROOT" && docker compose down -v >/dev/null 2>&1 )
      ok "containers, network and volumes removed"
      add_stopped "docker containers + volumes (postgres data wiped)"
    else
      ( cd "$REPO_ROOT" && docker compose down >/dev/null 2>&1 )
      ok "containers and network removed (volumes kept - use --volumes to drop the database)"
      add_stopped "docker containers (postgres, redis, backend, citizen_web, police_dashboard)"
    fi
  fi
fi

if [ "$EFFECTIVE" = "local" ]; then
  if [ -f "$PIDS_TSV" ]; then
    while IFS=$'\t' read -r name pid label; do
      [ -z "$name" ] && continue
      if kill -0 "$pid" 2>/dev/null; then
        step "stopping ${label:-$name} (pid $pid)"
        kill_tree "$pid"
        add_stopped "${label:-$name} (pid $pid)"
      else
        info "${label:-$name}: already gone (pid $pid)"
        add_stopped "${label:-$name} (was not running)"
      fi
    done < "$PIDS_TSV"
    rm -f "$PIDS_TSV"
  fi

  IFS=',' read -r -a port_list <<< "$PORTS"
  for port in "${port_list[@]}"; do
    port="$(echo "$port" | tr -d '[:space:]')"
    [ -z "$port" ] && continue
    pid="$(port_pid "$port" | tr -d '[:space:]')"
    if [ -z "$pid" ]; then
      ok "TCP $port is free"
      continue
    fi
    proc="$(ps -p "$pid" -o comm= 2>/dev/null | head -1)"
    step "TCP $port still held by ${proc:-unknown} (pid $pid) - stopping it"
    kill_tree "$pid"
    sleep 0.5
    if [ -z "$(port_pid "$port")" ]; then
      ok "TCP $port is free"
      add_stopped "process on TCP $port (${proc:-unknown}, pid $pid)"
    else
      warn "TCP $port is STILL held - close the app using it manually"
    fi
  done

  rm -f "$STATE_DIR"/backend.pid "$STATE_DIR"/citizen.pid "$STATE_DIR"/police.pid "$STATE_DIR"/expo.pid
fi

echo
echo "  Stopped:"
if [ -z "$stopped_list" ]; then echo "    (nothing was running)"; else printf "$stopped_list"; fi
echo
info "PostgreSQL and Redis are system services and were left running."
echo
echo "  Start again with ./start.sh"
echo
exit 0