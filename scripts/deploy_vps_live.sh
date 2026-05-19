#!/usr/bin/env bash
set -euo pipefail

SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10}"
REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
EXPECTED_TELEGRAM_CHAT_ID="${EXPECTED_TELEGRAM_CHAT_ID:-8388367561}"
SYSTEMD_SETTLE_SECONDS="${SYSTEMD_SETTLE_SECONDS:-15}"

# shellcheck disable=SC2086
ssh $SSH_OPTS "$SSH_TARGET" \
  "REPO_URL='$REPO_URL' REPO_DIR='$REPO_DIR' REMOTE='$REMOTE' BRANCH='$BRANCH' EXPECTED_COMMIT='$EXPECTED_COMMIT' EXPECTED_TELEGRAM_CHAT_ID='$EXPECTED_TELEGRAM_CHAT_ID' SYSTEMD_SETTLE_SECONDS='$SYSTEMD_SETTLE_SECONDS' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

cd "$REPO_DIR"

if [ -n "$(git status --short)" ]; then
  echo "Refusing deploy: VPS git checkout is dirty." >&2
  git status --short >&2
  exit 1
fi

if git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote set-url "$REMOTE" "$REPO_URL"
else
  git remote add "$REMOTE" "$REPO_URL"
fi
git fetch "$REMOTE" "$BRANCH"
git checkout -B "$BRANCH" "$REMOTE/$BRANCH"

if [ -n "$EXPECTED_COMMIT" ]; then
  actual_commit="$(git rev-parse HEAD)"
  if [ "$actual_commit" != "$EXPECTED_COMMIT" ]; then
    echo "Refusing deploy: expected commit $EXPECTED_COMMIT but VPS has $actual_commit" >&2
    exit 1
  fi
fi

if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

"$PYTHON" -m pytest \
  tests/test_runtime_scripts.py \
  tests/test_liquidity_migration_champion_challenger.py \
  tests/test_liquidity_migration_cli.py::test_cli_volume_events_defaults_to_selected_liquidity_migration \
  tests/test_liquidity_migration_event_demo.py::test_demo_relaxed_profile_lowers_gates_for_more_demo_trades

"$PYTHON" - <<'PY'
from liquidity_migration.event_demo import _demo_event_config, _demo_strategy_id
from liquidity_migration.volume_events import VolumeEventResearchConfig

promoted = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
demo = _demo_event_config(VolumeEventResearchConfig(), profile="demo_relaxed")

assert _demo_strategy_id("promoted") == "liqmig_union_q40_h3_tp26_g100_qsqueeze"
assert _demo_strategy_id("demo_relaxed") == "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6"
assert promoted.take_profit_pcts == (0.26,)
assert demo.take_profit_pcts == (0.21,)
assert demo.failed_fade_exit_hours == 6
assert demo.failed_fade_min_mfe_pct == 0.01
assert demo.failed_fade_loss_pct == 0.04
assert demo.failed_fade_close_location_min == 0.0
print("strategy-settings-ok")
PY

if [ ! -f /etc/liquidity-migration/bybit-demo.env ]; then
  echo "Missing /etc/liquidity-migration/bybit-demo.env" >&2
  exit 1
fi

cp /etc/liquidity-migration/bybit-demo.env "/etc/liquidity-migration/bybit-demo.env.backup.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -Eq '^TELEGRAM_CHAT_ID=' /etc/liquidity-migration/bybit-demo.env; then
  sed -i "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=$EXPECTED_TELEGRAM_CHAT_ID/" /etc/liquidity-migration/bybit-demo.env
else
  printf '\nTELEGRAM_CHAT_ID=%s\n' "$EXPECTED_TELEGRAM_CHAT_ID" >> /etc/liquidity-migration/bybit-demo.env
fi

set -a
. /etc/liquidity-migration/bybit-demo.env
set +a

if [ "${TELEGRAM_CHAT_ID:-}" != "$EXPECTED_TELEGRAM_CHAT_ID" ]; then
  echo "Refusing deploy: TELEGRAM_CHAT_ID is '${TELEGRAM_CHAT_ID:-unset}', expected '$EXPECTED_TELEGRAM_CHAT_ID'" >&2
  exit 1
fi

cp deploy/systemd/liquidity-migration-bybit-demo.service /etc/systemd/system/liquidity-migration-bybit-demo.service
cp deploy/systemd/liquidity-migration-bybit-risk.service /etc/systemd/system/liquidity-migration-bybit-risk.service
systemctl daemon-reload
systemctl disable --now \
  model050426.service \
  model050426-bybit-demo-signal.timer \
  model050426-bybit-demo-signal.service \
  2>/dev/null || true
systemctl enable liquidity-migration-bybit-demo.service
systemctl enable liquidity-migration-bybit-risk.service
systemctl restart liquidity-migration-bybit-demo.service
systemctl restart liquidity-migration-bybit-risk.service

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

systemctl is-active --quiet liquidity-migration-bybit-demo.service
systemctl is-active --quiet liquidity-migration-bybit-risk.service
systemctl is-enabled --quiet liquidity-migration-bybit-demo.service
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service

for legacy_unit in \
  model050426.service \
  model050426-bybit-demo-signal.timer \
  model050426-bybit-demo-signal.service; do
  if systemctl is-active --quiet "$legacy_unit" 2>/dev/null; then
    echo "Verification failed: retired unit $legacy_unit is still active." >&2
    exit 1
  fi
  if systemctl is-enabled --quiet "$legacy_unit" 2>/dev/null; then
    echo "Verification failed: retired unit $legacy_unit is still enabled." >&2
    exit 1
  fi
done

systemctl show liquidity-migration-bybit-demo.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager
systemctl show liquidity-migration-bybit-risk.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=STRATEGY_PROFILE=demo_relaxed'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=INTERVAL_SECONDS=60'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_RANK_END=300'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_MAX_SYMBOLS=300'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=ORDER_SUBMIT_MODE=ws_then_rest'

python_commit="$(git rev-parse --short HEAD)"
echo "deploy-verify-ok commit=$python_commit"
REMOTE_SCRIPT
