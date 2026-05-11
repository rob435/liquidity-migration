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
RUNNER="$REPO_ROOT/scripts/run_bybit_demo_engine.sh"
SIGNAL_RUNNER="$REPO_ROOT/scripts/run_forward_signal_with_audit.sh"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found or not executable: $PYTHON_BIN" >&2
  echo "Create the repo virtualenv and install requirements first." >&2
  exit 1
fi

if [[ ! -x "$RUNNER" ]]; then
  echo "Runner not found or not executable: $RUNNER" >&2
  chmod +x "$RUNNER"
fi
if [[ ! -x "$SIGNAL_RUNNER" ]]; then
  echo "Signal runner not found or not executable: $SIGNAL_RUNNER" >&2
  chmod +x "$SIGNAL_RUNNER"
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
DEMO_ENTRY_SLEEVES=stage4_selected
DEMO_ENTRY_LEVERAGE=1
FORWARD_SIGNAL_SLEEVES=stage4_selected
FORWARD_WORKERS=
DEMO_USE_WALLET_BALANCE=1
DEMO_MAX_ORDER_NOTIONAL=0
DEMO_MAX_TOTAL_NEW_NOTIONAL=0
DEMO_MAX_ORDER_NOTIONAL_PCT_EQUITY=0.10
DEMO_MAX_TOTAL_NEW_NOTIONAL_PCT_EQUITY=1.0
EOF
  sudo chown root:"$SERVICE_GROUP" "$ENV_FILE"
  sudo chmod 0640 "$ENV_FILE"
  echo "Created env template: $ENV_FILE"
  echo "Edit it before starting the service."
fi

ensure_env_default() {
  local key="$1"
  local value="$2"
  if ! sudo grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
}

remove_env_regex() {
  local pattern="$1"
  sudo sed -i.bak -E "/^(${pattern})=/d" "$ENV_FILE"
}

ensure_env_default DEMO_ENTRY_SLEEVES stage4_selected
ensure_env_default DEMO_ENTRY_LEVERAGE 1
ensure_env_default FORWARD_SIGNAL_SLEEVES stage4_selected
ensure_env_default DEMO_USE_WALLET_BALANCE 1
ensure_env_default DEMO_MAX_ORDER_NOTIONAL 0
ensure_env_default DEMO_MAX_TOTAL_NEW_NOTIONAL 0
ensure_env_default DEMO_MAX_ORDER_NOTIONAL_PCT_EQUITY 0.10
ensure_env_default DEMO_MAX_TOTAL_NEW_NOTIONAL_PCT_EQUITY 1.0
sudo sed -i.bak -E 's/^DEMO_ENTRY_SLEEVES=rank_31_plus$/DEMO_ENTRY_SLEEVES=stage4_selected/' "$ENV_FILE"
sudo sed -i.bak -E 's/^FORWARD_SIGNAL_SLEEVES=rank_31_plus$/FORWARD_SIGNAL_SLEEVES=stage4_selected/' "$ENV_FILE"
remove_env_regex 'PROFIT_PROTECTOR_.*'
remove_env_regex 'HOURLY_FUNCTIONAL_.*'

env_value() {
  local key="$1"
  sudo awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1); exit}' "$ENV_FILE"
}

missing_env=()
for required_key in BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  if [[ -z "$(env_value "$required_key")" ]]; then
    missing_env+=("$required_key")
  fi
done
if [[ "${#missing_env[@]}" -gt 0 ]]; then
  echo "Refusing to install enabled demo runtime; missing env value(s): ${missing_env[*]}" >&2
  echo "Edit $ENV_FILE, then rerun this installer." >&2
  exit 1
fi

sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=MODEL050426 Bybit demo engine with fast protection
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$REPO_ROOT
EnvironmentFile=$ENV_FILE
Environment=PYTHON_BIN=$PYTHON_BIN
Environment=DATA_ROOT=$DATA_ROOT
Environment=CONFIG_PATH=$CONFIG_PATH
ExecStart=$RUNNER
Restart=always
RestartSec=60
KillMode=control-group
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

sudo tee "/etc/systemd/system/$SERVICE_NAME-signal.service" >/dev/null <<EOF
[Unit]
Description=MODEL050426 forward paper signal scan
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$REPO_ROOT
EnvironmentFile=$ENV_FILE
Environment=PYTHON_BIN=$PYTHON_BIN
Environment=DATA_ROOT=$DATA_ROOT
Environment=CONFIG_PATH=$CONFIG_PATH
ExecStart=$SIGNAL_RUNNER
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

sudo tee "/etc/systemd/system/$SERVICE_NAME-signal.timer" >/dev/null <<EOF
[Unit]
Description=Run MODEL050426 forward paper signal scan at 23:00 UTC

[Timer]
OnCalendar=*-*-* 23:00:00 UTC
AccuracySec=1s
Persistent=false
Unit=$SERVICE_NAME-signal.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
LEGACY_UNITS=(
  "$SERVICE_NAME.timer"
  "model050426-forward-paper.timer"
  "model050426-forward-paper.service"
  "model050426-forward-audit.timer"
  "model050426-forward-audit.service"
  "model050426-hourly-functional.timer"
  "model050426-hourly-functional.service"
  "model050426-profit-protector.timer"
  "model050426-profit-protector.service"
)
for legacy_unit in "${LEGACY_UNITS[@]}"; do
  sudo systemctl disable --now "$legacy_unit" >/dev/null 2>&1 || true
  sudo rm -f "/etc/systemd/system/$legacy_unit"
done
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME.service"
sudo systemctl enable "$SERVICE_NAME-signal.timer"

cat <<EOF
Installed:
  /etc/systemd/system/$SERVICE_NAME.service
  /etc/systemd/system/$SERVICE_NAME-signal.service
  /etc/systemd/system/$SERVICE_NAME-signal.timer
  $ENV_FILE

Next:
  sudo nano $ENV_FILE
  sudo systemctl start $SERVICE_NAME.service
  sudo systemctl start $SERVICE_NAME-signal.timer
  systemctl list-timers $SERVICE_NAME-signal.timer
  journalctl -u $SERVICE_NAME.service -f

Pause new entries:
  touch "$REPO_ROOT/$DATA_ROOT/DEMO_PAUSED"

Resume new entries:
  rm -f "$REPO_ROOT/$DATA_ROOT/DEMO_PAUSED"
EOF
