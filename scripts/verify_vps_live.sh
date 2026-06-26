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

require_unit_env() {
  _unit="$1"
  _expected="$2"
  if ! systemctl show "$_unit" --property=Environment --value --no-pager | tr ' ' '\n' | grep -Fx -- "$_expected" >/dev/null; then
    echo "verify failed: $_unit missing effective env $_expected" >&2
    return 1
  fi
}

"$PYTHON" - <<'PY'
# The daily-short sleeve was ERASED 2026-06-11 (operator order). Pin the surviving
# deployed configs - the LONG promoted profile and the continuous sleeve's demo/paper guards -
# mirroring the strategy-settings gate in scripts/deploy_vps_live.sh.
from liquidity_migration.long_native_event_demo import _v11a_long_native_config

long_cfg = _v11a_long_native_config()
assert long_cfg.universe_size == 50
assert long_cfg.max_concurrent_positions == 10
assert long_cfg.cooldown_days == 7
assert long_cfg.weekend_size_mult == 1.5

from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, apply_continuous_demo_profile
cont = apply_continuous_demo_profile(
    ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend")
)
assert cont.rmom_quantile == 0.25, cont.rmom_quantile
assert {c[0] for c in cont.ensemble_components} == {"p3", "p4p3", "p4p5"}
assert tuple(cont.feature_set) == ("max_ret168",), cont.feature_set
assert cont.entry_event_trigger == "none", cont.entry_event_trigger
assert cont.max_hold_hours == 24, cont.max_hold_hours
assert {c[0]: c[3] for c in cont.ensemble_components} == {"p3": 0.12, "p4p3": 0.12, "p4p5": 0.12}
assert {c[0]: c[4] for c in cont.ensemble_components} == {
    "p3": 0.3333333333333333,
    "p4p3": 0.2222222222222222,
    "p4p5": 0.4444444444444444,
}
assert cont.entry_pause_after_adverse_exits == 8, cont.entry_pause_after_adverse_exits
assert cont.entry_pause_window_minutes == 1440, cont.entry_pause_window_minutes
assert cont.stop_loss_pct == 0.0, cont.stop_loss_pct
assert cont.left_decile_exit_enabled is False, cont.left_decile_exit_enabled
assert cont.stop_approach_frac == 0.0, cont.stop_approach_frac
assert cont.failed_fade_hours == 0, cont.failed_fade_hours
assert cont.breakeven_arm_pct == 0.0, cont.breakeven_arm_pct
assert cont.sizing_mode == "inverse_vol", cont.sizing_mode
assert cont.target_vol_per_name == 0.01, cont.target_vol_per_name
assert cont.vol_weight_clamp == 2.0, cont.vol_weight_clamp
assert cont.daily_rebalance_enabled is False, cont.daily_rebalance_enabled
assert cont.daily_rebalance_realized_vol_window_days == 90, cont.daily_rebalance_realized_vol_window_days
assert cont.daily_rebalance_target_daily_vol == 0.045, cont.daily_rebalance_target_daily_vol
assert cont.daily_rebalance_max_scale == 4.0, cont.daily_rebalance_max_scale
assert cont.daily_rebalance_drawdown_half_threshold == -0.04, cont.daily_rebalance_drawdown_half_threshold
assert cont.daily_rebalance_resize_cost_bps == 10.0, cont.daily_rebalance_resize_cost_bps
assert cont.daily_rebalance_strategy_momentum_window_days == 0, cont.daily_rebalance_strategy_momentum_window_days
assert cont.daily_rebalance_strategy_momentum_min_return == 0.0, cont.daily_rebalance_strategy_momentum_min_return
assert cont.daily_rebalance_strategy_momentum_scale_when_below == 0.0, cont.daily_rebalance_strategy_momentum_scale_when_below
assert cont.btc_trend_gate == "uptrend", cont.btc_trend_gate
assert cont.entry_btc_risk_sizing_enabled is True, cont.entry_btc_risk_sizing_enabled
assert cont.entry_btc_risk_arm_id == "CTRL_BTC_RISK_70_90_35", cont.entry_btc_risk_arm_id
assert cont.entry_btc_risk_low == 0.70, cont.entry_btc_risk_low
assert cont.entry_btc_risk_high == 0.90, cont.entry_btc_risk_high
assert cont.entry_btc_risk_tail_mult == 0.35, cont.entry_btc_risk_tail_mult
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

# DEFENSE-IN-DEPTH on the highest-stakes toggle (deploy-ci-6): the VPS runs
# order-submitting demo units (continuous-demo, long-demo, risk) against the
# account this env file defines; demo-only operation otherwise depends solely on
# the per-process runtime guard validate_order_submit_allowed(). Make the VERIFY
# itself fail-closed - parity with the same guard in scripts/deploy_vps_live.sh -
# so a mis-edited bybit-demo.env that ever set REAL_MONEY truthy is caught here,
# not just at submission time. The strategy is NOT validated for real money.
case "${REAL_MONEY:-}" in
  1|true|TRUE|True|yes|YES|Yes|on|ON|On)
    echo "Verification failed: REAL_MONEY='${REAL_MONEY}' in /etc/liquidity-migration/bybit-demo.env." \
         "This box runs order-submitting demo units; real money is not validated. Fix the env file to" \
         "demo (unset/false REAL_MONEY) before relying on this host." >&2
    exit 1
    ;;
esac

# Per-sleeve kill-switch: source the toggle lib so an intentionally-off sleeve is
# verified DOWN, not flagged as a failed deploy. (Unit FILES are always synced by
# deploy regardless of toggle, so exact unit-env checks below still work for an
# off/disabled sleeve.) The risk service is intentionally NOT toggled.
. deploy/lib_sleeves.sh
lm_load_sleeve_toggles
echo "verify sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"

# The risk service (shared reconcile authority for every sleeve) has NO toggle -
# always verify it enabled regardless of which entry sleeves are on. Per-sleeve
# enabled+active state is checked post-settle via verify_sleeve (below), so an off
# sleeve is required DOWN instead of being flagged as a failed deploy.
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
# The continuous rmom-refresh timer is required if either continuous demo or
# paper evidence collection is enabled.
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS
fi
# Mirror deploy_vps_live.sh hedge fail-safe: when the continuous sleeve is off,
# an open stopless hedge leg keeps the hedge timer enabled until it is flat.
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  _hedge_timer_state=on
else
  _hedge_open="$(HEDGE_ROOT="data/bybit-continuous-hedge-event" "$PYTHON" - <<'PY' 2>/dev/null || echo unknown
import os
from liquidity_migration.storage import read_dataset
try:
    t = read_dataset(os.environ["HEDGE_ROOT"], "continuous_fade_demo_trades")
    if t.is_empty() or "status" not in t.columns:
        print(0)
    else:
        print(int(t.filter(t["status"] == "open").height))
except Exception:
    print("unknown")
PY
)"
  if [ "${_hedge_open}" = "unknown" ]; then
    _hedge_timer_state=on
  elif [ "${_hedge_open:-0}" -gt 0 ]; then
    _hedge_timer_state=on
  else
    _hedge_timer_state=off
  fi
fi
verify_timer "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS
if continuous_rmom_refresh_on; then
  _verify_rmom_root() {
    _rmom_label="$1"
    _rmom_root="$2"
    _rmom_rows="$(RMOM_ROOT="$_rmom_root" "$PYTHON" - <<'PY' 2>/dev/null || echo 0
import os
import pathlib, polars as pl
p = pathlib.Path(os.environ["RMOM_ROOT"])
print(pl.read_parquet(p).height if p.exists() else 0)
PY
)"
    if [ "${_rmom_rows:-0}" -le 0 ]; then
      if [ "${ALLOW_EMPTY_RMOM_GATE:-0}" = "1" ]; then
        echo "WARN: continuous ${_rmom_label} rmom gate is EMPTY. ALLOW_EMPTY_RMOM_GATE=1" \
             "lets verify continue, but that sleeve emits NO entries until rmom is built." >&2
      else
        echo "verify failed: continuous ${_rmom_label} rmom gate is EMPTY at ${_rmom_root}" >&2
        return 1
      fi
    else
      echo "continuous ${_rmom_label} rmom gate ok: ${_rmom_rows} rows."
    fi
  }
  if sleeve_on "$CONTINUOUS_SLEEVE"; then
    _verify_rmom_root "demo" "data/bybit-continuous-demo-event/residual_momentum.parquet"
  fi
  if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
    _verify_rmom_root "paper" "data/bybit-continuous-paper-event/residual_momentum.parquet"
  fi
fi
# Timer parity - read-only verify must catch a deploy that forgot to enable
# (or someone manually disabled) the liveness watchdog or daily combined-book
# report. Both fail loud if missing. (The demo-health watchdog was erased with
# the short sleeve 2026-06-11; deploy removes it from hosts - don't check it.)
systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer
systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer
systemctl is-active --quiet liquidity-migration-demo-liveness.timer
systemctl is-active --quiet liquidity-migration-combined-book-report.timer

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Risk service always active (no toggle) - turning a sleeve off must never stop
# position protection.
systemctl is-active --quiet liquidity-migration-bybit-risk.service
systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service
systemctl is-active --quiet liquidity-migration-liquidation-collector.service
# Per-sleeve kill-switch: an ON sleeve must be active AND enabled; an OFF sleeve must
# be DOWN (verify_sleeve fails loud if an off sleeve is somehow still running).
# Post-settle so the daemons have had SYSTEMD_SETTLE_SECONDS to come up after restart.
if systemctl is-enabled --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
  systemctl is-active --quiet liquidity-migration-depth-collector.service
elif systemctl is-active --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
  echo "Verification failed: liquidity-migration-depth-collector.service is active but not enabled; use systemctl enable --now or stop it." >&2
  exit 1
fi
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS

require_unit_env liquidity-migration-bybit-risk.service 'ORDER_SUBMIT_MODE=ws_then_rest'
# SHARED-ACCOUNT SAFETY: the single risk service must read EVERY sleeve's ledger root,
# else a sibling sleeve's live positions look untracked and get flattened. Fail loud
# if the risk unit isn't wired to the long + continuous sleeves.
require_unit_env liquidity-migration-bybit-risk.service 'LONG_DATA_ROOT=data/bybit-long-demo-event'
require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'
require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_ADDON_DATA_ROOT=data/bybit-continuous-hedge-event'
# Long sleeve assertions: the profile name is intentionally explicit so the old
# ambiguous label cannot re-enter the live env by accident.
if sleeve_on "$LONG_SLEEVE"; then
  require_unit_env liquidity-migration-bybit-long-demo.service 'SUBMIT_ORDERS=1'
  require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
  require_unit_env liquidity-migration-bybit-long-paper.service 'SUBMIT_ORDERS=0'
  require_unit_env liquidity-migration-bybit-long-paper.service 'PAPER_MODE=1'
  require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
fi
# Order-submitting continuous sleeve config - only when toggled ON
# (a retired sleeve's file content must not be an unconditional verify gate).
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'SUBMIT_ORDERS=1'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'STRATEGY_PROFILE=continuous_ensemble_v2'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_ENABLED=0'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS=90'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_MAX_SCALE=4'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD=-0.04'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_RESIZE_COST_BPS=10'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS=0'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_STRATEGY_MOMENTUM_MIN_RETURN=0'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_STRATEGY_MOMENTUM_SCALE_WHEN_BELOW=0'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'STOP_LOSS_PCT=0'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'STOP_APPROACH_FRAC=0'
fi
# MONEY-SAFETY parity with deploy_vps_live.sh (audit 2026-06-12 round 3): the
# continuous PAPER shadow must NEVER submit orders - UNCONDITIONAL regardless of
# toggle. A mis-edited paper unit previously passed this script.
require_unit_env liquidity-migration-bybit-continuous-paper.service 'SUBMIT_ORDERS=0'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'STRATEGY_PROFILE=continuous_ensemble_v2'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'FEATURE_SET=max_ret168'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'ENTRY_EVENT_TRIGGER=none'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'BTC_TREND_GATE=uptrend'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'MAX_HOLD_HOURS=24'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'SIZING_MODE=inverse_vol'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'TARGET_VOL_PER_NAME=0.01'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'VOL_WEIGHT_CLAMP=2'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_ENABLED=0'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS=90'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_MAX_SCALE=4'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD=-0.04'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_RESIZE_COST_BPS=10'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS=0'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_STRATEGY_MOMENTUM_MIN_RETURN=0'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DAILY_REBALANCE_STRATEGY_MOMENTUM_SCALE_WHEN_BELOW=0'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'PAPER_MODE=1'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DATA_ROOT=data/bybit-continuous-paper-event'

systemctl show liquidity-migration-bybit-risk.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager

echo "verify-ok commit=$(git rev-parse --short HEAD)"
REMOTE_SCRIPT
