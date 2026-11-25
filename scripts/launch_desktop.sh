#!/usr/bin/env bash
# Smart Desktop Launcher for Unified Sync Tool
# - If already running on HOST:PORT -> opens browser immediately
# - Otherwise starts Waitress server in background, waits until ready, then opens browser
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
HOST="${SP_SYNC_HOST:-127.0.0.1}"
PORT="${SP_SYNC_PORT:-5000}"
URL="http://${HOST}:${PORT}"
LOG_FILE="${PROJECT_DIR}/data/launcher.log"

mkdir -p "${PROJECT_DIR}/data"

port_is_open() {
  "$PYTHON_BIN" -c "
import socket, sys
s = socket.socket()
s.settimeout(0.4)
try:
    s.connect(('${HOST}', int('${PORT}')))
    s.close()
    sys.exit(0)
except OSError:
    sys.exit(1)
" >/dev/null 2>&1
}

open_browser() {
  if command -v xdg-open >/dev/null 2>&1; then
    nohup xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v gio >/dev/null 2>&1; then
    nohup gio open "$URL" >/dev/null 2>&1 &
  fi
}

if port_is_open; then
  open_browser
  exit 0
fi

cd "$PROJECT_DIR"
nohup "$PYTHON_BIN" -u "${PROJECT_DIR}/app.py" >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!

# Wait up to 10 seconds for Waitress to bind and accept connections
for _ in $(seq 1 25); do
  if port_is_open; then
    open_browser
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  sleep 0.4
done

# Fallback notification if startup failed
if command -v notify-send >/dev/null 2>&1; then
  notify-send -u critical "Unified Sync Tool" "Failed to start server. Check ${LOG_FILE}"
fi
exit 1
