#!/usr/bin/env bash
set -euo pipefail

SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
EXPECTED_TELEGRAM_CHAT_ID="${EXPECTED_TELEGRAM_CHAT_ID:-8388367561}"
SYSTEMD_SETTLE_SECONDS="${SYSTEMD_SETTLE_SECONDS:-5}"

# shellcheck disable=SC2086
ssh $SSH_OPTS "$SSH_TARGET" \
  "REPO_DIR='$REPO_DIR' EXPECTED_COMMIT='$EXPECTED_COMMIT' EXPECTED_TELEGRAM_CHAT_ID='$EXPECTED_TELEGRAM_CHAT_ID' SYSTEMD_SETTLE_SECONDS='$SYSTEMD_SETTLE_SECONDS' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

cd "$REPO_DIR"

if [ -n "$(git status --short)" ]; then
  echo "Verification failed: VPS git checkout is dirty." >&2
  git status --short >&2
  exit 1
fi

actual_commit="$(git rev-parse HEAD)"
if [ -n "$EXPECTED_COMMIT" ] && [ "$actual_commit" != "$EXPECTED_COMMIT" ]; then
  echo "Verification failed: expected commit $EXPECTED_COMMIT but VPS has $actual_commit" >&2
  exit 1
fi

if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

"$PYTHON" - <<'PY'
# The daily-short sleeve was ERASED 2026-06-11 (operator order). Pin the surviving
# deployed configs — the LONG promoted profile and the continuous sleeve's guards —
# mirroring the strategy-settings gate in scripts/deploy_vps_live.sh.
from liquidity_migration.long_native_event_demo import _v11a_long_native_config

long_cfg = _v11a_long_native_config()
assert long_cfg.universe_size == 50
assert long_cfg.max_concurrent_positions == 10
assert long_cfg.cooldown_days == 7
assert long_cfg.weekend_size_mult == 1.5

from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
cont = ContinuousDemoCycleConfig()
assert cont.rmom_quantile == 0.33, cont.rmom_quantile
assert cont.entry_pause_after_adverse_exits == 8, cont.entry_pause_after_adverse_exits
assert cont.entry_pause_window_minutes == 1440, cont.entry_pause_window_minutes
assert cont.stop_loss_pct == 0.25, cont.stop_loss_pct
print("strategy-settings-ok")
PY

if [ ! -f /etc/liquidity-migration/bybit-demo.env ]; then
  echo "Verification failed: missing /etc/liquidity-migration/bybit-demo.env" >&2
  exit 1
fi

set -a
. /etc/liquidity-migration/bybit-demo.env
set +a

if [ "${TELEGRAM_CHAT_ID:-}" != "$EXPECTED_TELEGRAM_CHAT_ID" ]; then
  echo "Verification failed: TELEGRAM_CHAT_ID is '${TELEGRAM_CHAT_ID:-unset}', expected '$EXPECTED_TELEGRAM_CHAT_ID'" >&2
  exit 1
fi

# Per-sleeve kill-switch: source the toggle lib so an intentionally-off sleeve is
# verified DOWN, not flagged as a failed deploy. (Unit FILES are always synced by
# deploy regardless of toggle, so the `systemctl cat` env checks below still work for
# an off/disabled sleeve.) The risk service is intentionally NOT toggled.
. deploy/lib_sleeves.sh
lm_load_sleeve_toggles
echo "verify sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"

# The risk service (shared reconcile authority for every sleeve) has NO toggle —
# always verify it enabled regardless of which entry sleeves are on. Per-sleeve
# enabled+active state is checked post-settle via verify_sleeve (below), so an off
# sleeve is required DOWN instead of being flagged as a failed deploy.
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
# The continuous rmom-refresh timer is required if either continuous demo or
# paper evidence collection is enabled.
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
fi
verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS
# Timer parity — read-only verify must catch a deploy that forgot to enable
# (or someone manually disabled) the liveness watchdog or daily combined-book
# report. Both fail loud if missing. (The demo-health watchdog was erased with
# the short sleeve 2026-06-11; deploy removes it from hosts — don't check it.)
systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer
systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer
systemctl is-active --quiet liquidity-migration-demo-liveness.timer
systemctl is-active --quiet liquidity-migration-combined-book-report.timer

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Risk service always active (no toggle) — turning a sleeve off must never stop
# position protection.
systemctl is-active --quiet liquidity-migration-bybit-risk.service
# Per-sleeve kill-switch: an ON sleeve must be active AND enabled; an OFF sleeve must
# be DOWN (verify_sleeve fails loud if an off sleeve is somehow still running).
# Post-settle so the daemons have had SYSTEMD_SETTLE_SECONDS to come up after restart.
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS

systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=ORDER_SUBMIT_MODE=ws_then_rest'
# SHARED-ACCOUNT SAFETY: the single risk service must read EVERY sleeve's ledger root,
# else a sibling sleeve's live positions look untracked and get flattened. Fail loud
# if the risk unit isn't wired to the long + continuous sleeves.
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=LONG_DATA_ROOT=data/bybit-long-demo-event'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'
# Order-submitting continuous sleeve config + its disaster stop — only when toggled ON
# (a retired sleeve's file content must not be an unconditional verify gate).
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=1'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=STOP_LOSS_PCT=0.25'
fi
# MONEY-SAFETY parity with deploy_vps_live.sh (audit 2026-06-12 round 3): the
# continuous PAPER shadow must NEVER submit orders — UNCONDITIONAL regardless of
# toggle. A mis-edited paper unit previously passed this script.
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=0'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=PAPER_MODE=1'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=DATA_ROOT=data/bybit-continuous-paper-event'

systemctl show liquidity-migration-bybit-risk.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager

echo "verify-ok commit=$(git rev-parse --short HEAD)"
REMOTE_SCRIPT
