#!/usr/bin/env bash
set -euo pipefail

SSH_TARGET="${SSH_TARGET:-root@5.223.42.109}"
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
echo "verify sleeves: SHORT=$SHORT_SLEEVE LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE"

# The risk service (shared reconcile authority for every sleeve) has NO toggle —
# always verify it enabled regardless of which entry sleeves are on. Per-sleeve
# enabled+active state is checked post-settle via verify_sleeve (below), so an off
# sleeve is required DOWN instead of being flagged as a failed deploy.
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
# The continuous sleeve's daily rmom-refresh timer is toggled with that sleeve.
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  systemctl is-enabled --quiet liquidity-migration-continuous-rmom-refresh.timer
  systemctl is-active --quiet liquidity-migration-continuous-rmom-refresh.timer
fi
# Timer parity — read-only verify must catch a deploy that forgot to enable
# (or someone manually disabled) the demo-health watchdog or daily
# combined-book report. Both fail loud if missing.
systemctl is-enabled --quiet liquidity-migration-demo-health.timer
systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer
systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer
systemctl is-active --quiet liquidity-migration-demo-health.timer
systemctl is-active --quiet liquidity-migration-demo-liveness.timer
systemctl is-active --quiet liquidity-migration-combined-book-report.timer

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

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Risk service always active (no toggle) — turning a sleeve off must never stop
# position protection.
systemctl is-active --quiet liquidity-migration-bybit-risk.service
# Per-sleeve kill-switch: an ON sleeve must be active AND enabled; an OFF sleeve must
# be DOWN (verify_sleeve fails loud if an off sleeve is somehow still running).
# Post-settle so the daemons have had SYSTEMD_SETTLE_SECONDS to come up after restart.
verify_sleeve "$SHORT_SLEEVE" $SHORT_SLEEVE_UNITS
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS

systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=STRATEGY_PROFILE=promoted'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=INTERVAL_SECONDS=60'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_RANK_END=0'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_MAX_SYMBOLS=0'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_MIN_TURNOVER_24H=0'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=MAX_ACTIVE_SYMBOLS=12'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=ORDER_SUBMIT_MODE=ws_then_rest'
# SHARED-ACCOUNT SAFETY: the single risk service must read EVERY sleeve's ledger root,
# else a sibling sleeve's live positions look untracked and get flattened. Fail loud
# if the risk unit isn't wired to the long + continuous sleeves.
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=LONG_DATA_ROOT=data/bybit-long-demo-event'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'
# Continuous sleeve live config + its disaster stop — only when the sleeve is toggled ON
# (a retired sleeve's file content must not be an unconditional verify gate).
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=1'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=STOP_LOSS_PCT=0.25'
fi

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

echo "verify-ok commit=$(git rev-parse --short HEAD)"
REMOTE_SCRIPT
