#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-model050426-bybit-demo}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
ENV_DIR="${ENV_DIR:-/etc/model050426}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/bybit-demo.env}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
DATA_ROOT="${DATA_ROOT:-data/forward-paper}"
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  echo "Create the repo virtualenv and install requirements first." >&2
  exit 1
fi

sudo install -d -m 0750 -o root -g "$SERVICE_GROUP" "$ENV_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<'EOF'
# Bybit demo-only shadow trader environment.
# Fill these values on the VPS. Do not commit real files with secrets.
BYBIT_DEMO_API_KEY=
BYBIT_DEMO_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  sudo chown root:"$SERVICE_GROUP" "$ENV_FILE"
  sudo chmod 0640 "$ENV_FILE"
  echo "Created env template: $ENV_FILE"
  echo "Edit it before starting the timer."
fi

sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=MODEL050426 Bybit demo-only shadow cycle
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$REPO_ROOT
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_BIN -m aggression_carry --data-root $DATA_ROOT --config $CONFIG_PATH bybit-demo-cycle --submit-orders --i-understand-demo-sync --telegram
ExecStart=$PYTHON_BIN -m aggression_carry --data-root $DATA_ROOT --config $CONFIG_PATH forward-audit --telegram
Nice=5
IOSchedulingClass=best-effort
IOSchedulingPriority=7
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=false
ReadWritePaths=$REPO_ROOT

[Install]
WantedBy=multi-user.target
EOF

sudo tee "/etc/systemd/system/$SERVICE_NAME.timer" >/dev/null <<EOF
[Unit]
Description=Run MODEL050426 Bybit demo-only shadow cycle every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=false
Unit=$SERVICE_NAME.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME.timer"

cat <<EOF
Installed:
  /etc/systemd/system/$SERVICE_NAME.service
  /etc/systemd/system/$SERVICE_NAME.timer
  $ENV_FILE

Next:
  sudo nano $ENV_FILE
  sudo systemctl start $SERVICE_NAME.timer
  systemctl list-timers $SERVICE_NAME.timer
  journalctl -u $SERVICE_NAME.service -f

Pause new entries:
  touch "$REPO_ROOT/$DATA_ROOT/DEMO_PAUSED"

Resume new entries:
  rm -f "$REPO_ROOT/$DATA_ROOT/DEMO_PAUSED"
EOF
