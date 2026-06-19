#!/usr/bin/env bash
#
# signal.sh — start / stop the Signal·Noise app
#
#   ./signal.sh start     launch the server (sets up the venv on first run)
#   ./signal.sh stop      stop the server
#   ./signal.sh restart   stop then start
#   ./signal.sh status    is it running?
#   ./signal.sh logs      tail the server log
#
# Port defaults to 5050 (macOS uses 5000 for AirPlay). Override with:
#   PORT=8000 ./signal.sh start
#
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-5050}"
VENV=".venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
PID_FILE=".signal.pid"
LOG_FILE="signal.log"
URL="http://localhost:$PORT"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[33m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }

running_pid() {
  # Echo the PID if our server is actually alive, else nothing.
  if [[ -f "$PID_FILE" ]]; then
    local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"; return
    fi
  fi
}

ensure_venv() {
  if [[ ! -x "$PY" ]]; then
    yellow "First run — creating virtual environment and installing dependencies…"
    python3 -m venv "$VENV"
    "$PIP" install --quiet --upgrade pip
    "$PIP" install --quiet -r requirements.txt
    green "Dependencies installed."
  fi
}

start() {
  local pid; pid="$(running_pid)"
  if [[ -n "$pid" ]]; then
    yellow "Already running (pid $pid) at $URL"
    return
  fi
  ensure_venv
  yellow "Starting Signal/Noise on port ${PORT}..."
  PORT="$PORT" nohup "$PY" app.py >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"

  # Wait briefly for it to come up.
  for _ in $(seq 1 20); do
    if curl -s -o /dev/null --max-time 1 "$URL/"; then
      green "Running -> $URL"
      echo "   logs: ./signal.sh logs   |   stop: ./signal.sh stop"
      return
    fi
    sleep 0.3
  done
  red "Server did not respond in time. Recent log:"
  tail -n 20 "$LOG_FILE" || true
  exit 1
}

stop() {
  local pid; pid="$(running_pid)"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
  # Sweep any stragglers from earlier runs, then clear the pid file.
  pkill -f "$PWD/app.py" 2>/dev/null || true
  rm -f "$PID_FILE"
  green "Stopped."
}

status() {
  local pid; pid="$(running_pid)"
  if [[ -n "$pid" ]]; then
    green "Running (pid $pid) -> $URL"
  else
    yellow "Not running."
  fi
}

logs() {
  [[ -f "$LOG_FILE" ]] || { yellow "No log file yet — start the server first."; return; }
  tail -n 40 -f "$LOG_FILE"
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  logs)    logs ;;
  *)
    echo "Usage: ./signal.sh {start|stop|restart|status|logs}"
    echo "  PORT=8000 ./signal.sh start   # override the port (default 5050)"
    exit 1 ;;
esac
