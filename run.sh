#!/usr/bin/env bash
# Control script for the trading platform: setup, start/stop the server, logs, tests.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
PID_FILE="$PROJECT_DIR/data/server.pid"
LOG_DIR="$PROJECT_DIR/logs"
SERVER_OUT="$LOG_DIR/server.out"
APP_LOG="$LOG_DIR/app.log"
SERVER_OUT_MAX_BYTES=$((10 * 1024 * 1024))
UNIT_NAME="trade-plat.service"
UNIT_FILE="$HOME/.config/systemd/user/$UNIT_NAME"
CHARTJS_URL="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"
CHARTJS_FILE="$PROJECT_DIR/frontend/vendor/chart.umd.js"

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR" "$PROJECT_DIR/data"

log() { printf '[run.sh] %s\n' "$*"; }
die() { printf '[run.sh] ERROR: %s\n' "$*" >&2; exit 1; }

load_env() {
  [[ -f "$PROJECT_DIR/.env" ]] || return 0
  # Export KEY=VALUE lines; skip comments and empty values so a template .env never clobbers real env vars.
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    [[ -z "$value" ]] && continue
    export "$key=$value"
  done <"$PROJECT_DIR/.env"
}

ensure_venv() {
  [[ -x "$PYTHON" ]] || die "venv not found. Run: ./run.sh setup"
}

is_server() {
  [[ -n "$1" && "$(readlink "/proc/$1/cwd" 2>/dev/null)" == "$PROJECT_DIR" ]] \
    && tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null | grep -q -- "-m backend.main"
}

server_pid() {
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if is_server "$pid"; then
    echo "$pid"
    return 0
  fi
  # The PID file can go stale when the server was started some other way; find the real process.
  for pid in $(pgrep -u "$(id -u)" -f -- "-m backend.main" 2>/dev/null || true); do
    if is_server "$pid"; then
      echo "$pid" >"$PID_FILE"
      echo "$pid"
      return 0
    fi
  done
  rm -f "$PID_FILE"
  return 1
}

service_installed() {
  [[ -f "$UNIT_FILE" ]] && systemctl --user is-enabled "$UNIT_NAME" >/dev/null 2>&1
}

rotate_server_out() {
  [[ -f "$SERVER_OUT" ]] || return 0
  if (( $(stat -c %s "$SERVER_OUT") > SERVER_OUT_MAX_BYTES )); then
    mv -f "$SERVER_OUT" "$SERVER_OUT.1"
    log "Rotated $SERVER_OUT to $SERVER_OUT.1"
  fi
}

cmd_setup() {
  if [[ ! -x "$PYTHON" ]]; then
    log "Creating virtualenv in $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi
  log "Installing dependencies"
  "$PIP" install --quiet --upgrade pip
  "$PIP" install --quiet -r "$PROJECT_DIR/requirements.txt"
  if [[ ! -s "$CHARTJS_FILE" ]]; then
    log "Downloading Chart.js to frontend/vendor"
    mkdir -p "$(dirname "$CHARTJS_FILE")"
    curl -fsSL --max-time 60 "$CHARTJS_URL" -o "$CHARTJS_FILE" \
      || log "WARNING: could not download Chart.js; the page will fall back to the CDN"
  fi
  if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    log "Created .env from .env.example - fill in your Longbridge credentials"
  fi
  log "Setup complete"
}

cmd_start() {
  ensure_venv
  load_env
  if pid="$(server_pid)"; then
    log "Server already running (pid $pid)"
    return 0
  fi
  if service_installed; then
    systemctl --user start "$UNIT_NAME"
    log "Started via systemd ($UNIT_NAME); logs: journalctl --user -u $UNIT_NAME and $APP_LOG"
    return 0
  fi
  rotate_server_out
  log "Starting server (output: $SERVER_OUT)"
  nohup "$PYTHON" -m backend.main >>"$SERVER_OUT" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 2
  if pid="$(server_pid)"; then
    local port="${TRADE_PLAT_PORT:-$("$PYTHON" -c 'from backend.config import settings; print(settings.port)')}"
    log "Server running (pid $pid) on http://localhost:${port}"
  else
    die "Server failed to start; see $SERVER_OUT"
  fi
}

cmd_stop() {
  if service_installed; then
    systemctl --user stop "$UNIT_NAME"
    rm -f "$PID_FILE"
    log "Stopped via systemd ($UNIT_NAME)"
    return 0
  fi
  if pid="$(server_pid)"; then
    log "Stopping server (pid $pid)"
    kill "$pid"
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "Server did not exit gracefully; killing"
      kill -9 "$pid" || true
    fi
    rm -f "$PID_FILE"
    log "Stopped"
  else
    log "Server is not running"
  fi
}

cmd_status() {
  if pid="$(server_pid)"; then
    log "Server running (pid $pid)"
    load_env
    local port="${TRADE_PLAT_PORT:-$("$PYTHON" -c 'from backend.config import settings; print(settings.port)')}"
    curl -fsS --max-time 5 "http://localhost:${port}/api/status" && echo
  else
    log "Server is not running"
    return 1
  fi
}

cmd_install_service() {
  ensure_venv
  mkdir -p "$(dirname "$UNIT_FILE")"
  cat >"$UNIT_FILE" <<UNIT
[Unit]
Description=Trade Plat server
After=network-online.target

[Service]
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON -m backend.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  if pid="$(server_pid)"; then
    log "Stopping the nohup server (pid $pid) so systemd can take over"
    kill "$pid"
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    rm -f "$PID_FILE"
  fi
  systemctl --user enable --now "$UNIT_NAME"
  log "Installed $UNIT_FILE; the server now restarts on failure and at boot"
}

cmd_logs() {
  touch "$APP_LOG"
  tail -n 100 -f "$APP_LOG"
}

cmd_dev() {
  ensure_venv
  load_env
  exec "$PYTHON" -m backend.main --reload
}

cmd_test() {
  ensure_venv
  load_env
  export TRADE_PLAT_LOG_FILE="$LOG_DIR/test.log"
  exec "$PYTHON" -m pytest -q "$PROJECT_DIR/tests" "$@"
}

cmd_check() {
  ensure_venv
  load_env
  exec "$PYTHON" -m backend.check
}

usage() {
  cat <<EOF
Usage: ./run.sh <command>

  setup     Create venv, install dependencies, vendor Chart.js, create .env
  start     Start the server in the background
  stop      Stop the background server
  restart   Stop then start
  status    Show server status and /api/status
  logs      Tail the application log
  dev       Run the server in the foreground with auto-reload
  test      Run the unit tests (extra args passed to pytest)
  check     Verify Longbridge credentials and data sources
  install-service   Run the server under systemd --user (restart on failure, start at boot)
EOF
}

case "${1:-}" in
  setup) cmd_setup ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  dev) cmd_dev ;;
  test) shift; cmd_test "$@" ;;
  check) cmd_check ;;
  install-service) cmd_install_service ;;
  *) usage; exit 1 ;;
esac
