#!/usr/bin/env bash
# Install (or remove) Unified Sync Tool as a Linux systemd --user service
# Usage:
#   ./scripts/install_systemd_service.sh install   # Install, enable & start on login
#   ./scripts/install_systemd_service.sh status    # Check running service status
#   ./scripts/install_systemd_service.sh uninstall # Stop, disable & remove unit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_NAME="sharepoint-sync-tool.service"
UNIT_PATH="${UNIT_DIR}/${UNIT_NAME}"
ACTION="${1:-install}"

case "$ACTION" in
  install)
    if [[ ! -x "$PYTHON_BIN" ]]; then
      echo "Error: virtualenv python not found at $PYTHON_BIN" >&2
      exit 1
    fi
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Unified SharePoint & Google Drive Sync Tool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} -u ${PROJECT_DIR}/app.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=SP_SYNC_HOST=127.0.0.1
Environment=SP_SYNC_PORT=5000

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    echo "✅ Installed and started ${UNIT_NAME} -> http://127.0.0.1:5000"
    echo "   Logs:   journalctl --user -u ${UNIT_NAME} -f"
    echo "   Status: systemctl --user status ${UNIT_NAME}"
    ;;
  status)
    systemctl --user status "$UNIT_NAME" --no-pager || true
    ;;
  uninstall)
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload
    echo "✅ Uninstalled ${UNIT_NAME}"
    ;;
  *)
    echo "Usage: $0 {install|status|uninstall}" >&2
    exit 2
    ;;
esac
