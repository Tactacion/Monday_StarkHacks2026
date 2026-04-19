#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

PYTHON="$(command -v python3 || echo /usr/bin/python3)"
echo "Python: $PYTHON"

cat > "$SYSTEMD_USER_DIR/sinew-receiver.service" <<EOF
[Unit]
Description=Sinew EMS Receiver
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON $SCRIPT_DIR/receiver.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable sinew-receiver.service
loginctl enable-linger "$(whoami)" 2>/dev/null || true
systemctl --user start sinew-receiver.service

echo "=== Status ==="
systemctl --user status sinew-receiver.service --no-pager || true
echo ""
echo "Logs: journalctl --user -u sinew-receiver -f"
