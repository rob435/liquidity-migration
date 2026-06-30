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
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# shellcheck disable=SC2086
{
  printf 'REPO_URL=%q\n' "$REPO_URL"
  printf 'REPO_DIR=%q\n' "$REPO_DIR"
  printf 'REMOTE=%q\n' "$REMOTE"
  printf 'BRANCH=%q\n' "$BRANCH"
  printf 'EXPECTED_COMMIT=%q\n' "$EXPECTED_COMMIT"
  printf 'EXPECTED_TELEGRAM_CHAT_ID=%q\n' "$EXPECTED_TELEGRAM_CHAT_ID"
  printf 'SYSTEMD_SETTLE_SECONDS=%q\n' "$SYSTEMD_SETTLE_SECONDS"
  printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
  cat <<'REMOTE_SCRIPT'
set -euo pipefail

git_with_optional_github_token() {
  if [ -n "${GITHUB_TOKEN:-}" ] && [[ "$REPO_URL" == https://github.com/* ]]; then
    local github_basic_auth
    github_basic_auth="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
    GIT_TERMINAL_PROMPT=0 git \
      -c "http.https://github.com/.extraheader=AUTHORIZATION: Basic $github_basic_auth" \
      "$@"
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}

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
git_with_optional_github_token fetch "$REMOTE" "$BRANCH"
unset GITHUB_TOKEN
# Capture the previously-deployed commit BEFORE checkout - used below to decide
# whether the dependency manifests changed and the venv needs a pip refresh.
previous_commit="$(git rev-parse HEAD 2>/dev/null || echo "")"
if [ -n "$EXPECTED_COMMIT" ]; then
  # Deploy exactly the commit that triggered this run. A queued newer run can
  # move the box forward later; this run must not silently checkout a different
  # branch head during the trigger->fetch window.
  if ! git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH"; then
    echo "Refusing deploy: expected commit $EXPECTED_COMMIT is not on $REMOTE/$BRANCH" >&2
    exit 1
  fi
  git checkout -B "$BRANCH" "$EXPECTED_COMMIT"
else
  git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
fi

if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

# Venv refresh on dependency change: deploying code whose deps aren't installed
# fails loud at the smoke tests below but leaves recovery manual. If the diff from
# the previously-deployed commit touches a dependency manifest - or the previous
# commit can't be determined - reinstall. Fail-loud: a pip failure aborts (set -e).
if [ -x .venv/bin/pip ]; then
  if [ -z "$previous_commit" ] \
    || ! git diff --quiet "$previous_commit" HEAD -- requirements.lock pyproject.toml; then
    echo "Dependency manifests changed (or previous commit unknown) - refreshing venv ..."
    .venv/bin/pip install -q -e ".[dev]"
  fi
fi

"$PYTHON" -m pytest \
  tests/test_runtime_scripts.py \
  tests/test_promoted_profiles.py
"$PYTHON" - <<'PY'
# Pin the deployed long profile; tests/test_promoted_profiles.py also covers it.
from liquidity_migration.long_native_event_demo import _v11a_long_native_config

long_cfg = _v11a_long_native_config()
assert long_cfg.universe_size == 50
assert long_cfg.max_concurrent_positions == 10
assert long_cfg.cooldown_days == 7
assert long_cfg.weekend_size_mult == 1.5

# Continuous-fade sleeve: these assertions pin its demo/paper config so a silent drift
# can't ship. Whether the order-submitting
# sleeve actually runs is toggled per-sleeve in deploy/sleeves.env (the single source
# of truth - don't hardcode its state here). v2 is demo/paper only and intentionally
# has no server stop; do not treat it as real-money-safe.
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
  echo "Missing /etc/liquidity-migration/bybit-demo.env" >&2
  exit 1
fi

cp /etc/liquidity-migration/bybit-demo.env "/etc/liquidity-migration/bybit-demo.env.backup.$(date -u +%Y%m%dT%H%M%SZ)"
# One backup accumulates per deploy - keep only the 5 newest. (The cp above just
# wrote one, so the glob always matches and ls can't fail under pipefail.)
ls -1t /etc/liquidity-migration/bybit-demo.env.backup.* 2>/dev/null | tail -n +6 | xargs -r rm -f
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

# DEFENSE-IN-DEPTH on the highest-stakes toggle (deploy-ci-6): the order-submitting
# units (continuous-demo, long-demo, risk) run on the account this env file defines,
# and demo-only operation otherwise depends solely on the per-process runtime guard
# validate_order_submit_allowed(). Make the DEPLOY itself fail-closed: if a mis-edited
# bybit-demo.env ever set REAL_MONEY truthy, refuse the deploy rather than restart live
# order-submitting daemons against a real-money account. The strategy is NOT validated
# for real money; promotion is operator-gated, never a deploy side effect.
case "${REAL_MONEY:-}" in
  1|true|TRUE|True|yes|YES|Yes|on|ON|On)
    echo "Refusing deploy: REAL_MONEY='${REAL_MONEY}' in /etc/liquidity-migration/bybit-demo.env." \
         "This box deploys order-submitting demo units; real money is not validated and must not be" \
         "enabled by a deploy. Fix the env file to demo (unset/false REAL_MONEY) and redeploy." >&2
    exit 1
    ;;
esac

# Sync every .service / .timer in deploy/systemd/ so any unit added
# to the repo (e.g. demo-health, combined-book-report, future units)
# auto-deploys instead of needing a one-off manual cp. The long
# demo/paper omission previously caused MemoryMax=2G to sit on disk
# unused for an OOM-loop cycle - globbing prevents that whole class
# of "added a unit but forgot to wire it into deploy" misses.
for unit in deploy/systemd/liquidity-migration-*.service deploy/systemd/liquidity-migration-*.timer; do
    cp "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload

# --- per-sleeve kill-switch (deploy/sleeves.env) ----------------------------------------
# Single source of truth for which strategy sleeves run. Default (all "on") is byte-identical
# to the previous unconditional enables. Flip a sleeve to "off" in deploy/sleeves.env (or
# /etc/liquidity-migration/sleeves.env) + redeploy to RETIRE it - it stays disabled across
# deploys; "off" stops new entries; ws_risk stays up for shared-account visibility, but
# continuous_ensemble_v2 carries no server-side stop and must remain demo/paper only.
. deploy/lib_sleeves.sh
lm_load_sleeve_toggles
lm_write_resolved_sleeve_toggles
lm_verify_resolved_sleeve_toggles
echo "sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"
systemctl enable liquidity-migration-bybit-risk.service
# Forward-only data collection (P3, operator-approved 2026-06-10): liquidation
# history is unbuyable, so the collector runs always-on like the risk service.
# RESTART (not just enable --now, which is a no-op on a running unit) so deployed
# collector code changes actually take effect - append-only JSONL, no order path,
# so a bounce loses at most the in-flight websocket messages.
systemctl enable liquidity-migration-liquidation-collector.service
systemctl restart liquidity-migration-liquidation-collector.service
# The depth collector is operator-gated (deploy installs the unit but never enables it).
# If the operator HAS enabled it, restart it for the same reason as the liquidation
# collector: deployed collector code must take effect, not wait for the next reboot.
if systemctl is-enabled --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
    systemctl restart liquidity-migration-depth-collector.service
fi
# Host cleanup: stop, disable, and remove retired units so stale enabled units
# can't crash-loop on removed entrypoints.
for _retired in $RETIRED_SLEEVE_UNITS liquidity-migration-demo-health.timer liquidity-migration-demo-health.service; do
    systemctl disable --now "$_retired" 2>/dev/null || true
    rm -f "/etc/systemd/system/$_retired"
done
lm_cleanup_unknown_liqmig_units
systemctl daemon-reload
# Restart already-running repo timers (round 4): on several systemd versions an
# ACTIVE timer does not reschedule a changed OnCalendar= until restarted, so a
# cadence edit silently kept firing on the old schedule. try-restart is a no-op
# for inactive/disabled timers; the per-toggle enables below start those fresh.
for unit in deploy/systemd/liquidity-migration-*.timer; do
    systemctl try-restart "$(basename "$unit")" 2>/dev/null || true
done
apply_sleeve_enable "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
# Timers must be enabled --now: enable alone writes the symlink but does not
# start the timer, so on a fresh VPS the demo-liveness watchdog + daily combined-
# book Telegram report would sit dormant until someone ran systemctl by hand.
# --now schedules them immediately; subsequent deploys are idempotent.
systemctl enable --now liquidity-migration-demo-liveness.timer
systemctl enable --now liquidity-migration-combined-book-report.timer
# Daily refresh of the continuous-fade rmom gate + the gate seed run when either the
# continuous demo sleeve or its no-order paper evidence collector is on.
if continuous_rmom_refresh_on; then
apply_timer_enable on $CONTINUOUS_SLEEVE_TIMERS
# Seed the rmom gate NOW rather than waiting for the 00:20 UTC timer. Without this a
# fresh deploy starts the continuous daemon into an EMPTY gate -> the live decile drops
# every symbol (silent zero-signal blackout - the 2026-06-02 incident). The refresh is a
# oneshot, so this blocks until the parquet is (re)built from the sleeve's kline store.
# best-effort + fail-safe: a FIRST deploy (klines still bootstrapping) yields no rows ->
# WARN, not fail (no rmom => no entries, never WRONG entries; the daily timer + the
# rmom-staleness watchdog cover the gap; re-run the service once klines are up).
echo "Seeding continuous rmom gate (residual_momentum.parquet) ..."
systemctl start liquidity-migration-continuous-rmom-refresh.service \
  || echo "WARN: rmom seed service failed; the 00:20 timer + rmom watchdog will cover it." >&2
_check_rmom_root() {
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
    echo "WARN: continuous ${_rmom_label} rmom gate is EMPTY after seed. ALLOW_EMPTY_RMOM_GATE=1" \
         "lets deploy continue, but that sleeve emits NO entries until rmom is built." >&2
  else
    echo "ERROR: continuous ${_rmom_label} rmom gate is EMPTY after seed. Re-run" \
         "'systemctl start liquidity-migration-continuous-rmom-refresh.service' once the" \
         "daemon has bootstrapped klines, or set ALLOW_EMPTY_RMOM_GATE=1 for an explicit" \
         "first-boot/no-entry override." >&2
    return 1
  fi
else
  echo "continuous ${_rmom_label} rmom gate seeded: ${_rmom_rows} rows."
fi
}
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  _check_rmom_root "demo" "data/bybit-continuous-demo-event/residual_momentum.parquet"
fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
  _check_rmom_root "paper" "data/bybit-continuous-paper-event/residual_momentum.parquet"
fi
else
  echo "kill-switch: continuous demo+paper sleeves off -> skipping rmom timer + gate seed." >&2
  apply_timer_enable off $CONTINUOUS_SLEEVE_TIMERS
fi
# Hedge timer gating (deploy-env-timers-1): the daily BTC/ETH hedge long is booked
# with stop_price=take_profit_price=planned_exit_ts_ms=0, so the always-on risk service
# TRACKS but is contractually FORBIDDEN from force-exiting it - the daily hedge timer is
# its ONLY lifecycle manager. Unconditionally disabling the timer when CONTINUOUS_SLEEVE
# goes off (the documented retirement action) would ORPHAN any open hedge leg: never
# resized/closed (manager dead), never stopped (stopless by contract), never monitored
# (the watchdog only tracks the hedge units while continuous is on). So when continuous
# is OFF we first check the hedge addon ledger: if it holds an open hedge row, keep the
# timer ENABLED so the daily run can trim it to flat (its reduce-only legs proceed even
# when warmstart is stale), and page loudly; only disable once the leg is flat.
# _hedge_timer_state is the intended hedge-timer state; the verify block below reuses
# it so apply and verify never disagree (a kept-open timer must not fail verify_timer off).
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
    # Could not read the ledger - fail safe: do NOT auto-disable a timer that might be
    # the only manager of an open live position. Keep it enabled and page the operator.
    echo "CRITICAL: CONTINUOUS_SLEEVE=off but the hedge addon ledger" \
         "(data/bybit-continuous-hedge-event) could not be read - KEEPING the hedge timer" \
         "enabled (fail-safe) so a possibly-open, stopless hedge long is not orphaned." \
         "Inspect the addon ledger and flatten the hedge manually before retiring continuous." >&2
    _hedge_timer_state=on
  elif [ "${_hedge_open:-0}" -gt 0 ]; then
    echo "CRITICAL: CONTINUOUS_SLEEVE=off but the hedge addon ledger holds ${_hedge_open}" \
         "OPEN hedge row(s). The hedge long is stopless (risk service tracks but never exits it)" \
         "and the daily hedge timer is its ONLY manager - KEEPING the timer enabled so the daily" \
         "run can trim it to flat. It will auto-disable on the next deploy once the leg is flat." >&2
    _hedge_timer_state=on
  else
    echo "hedge leg flat (no open rows) -> disabling the hedge timer with continuous off." >&2
    _hedge_timer_state=off
  fi
fi
CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"
lm_write_resolved_sleeve_toggles
lm_verify_resolved_sleeve_toggles
apply_hedge_timer_enable "$_hedge_timer_state"

# --- restart: only the ON sleeves (off sleeves were disable --now'd above); risk always. ---
# Long/continuous share the liquidity_migration package, so any Python change
# requires restarting every running sleeve to pick up the new code.
systemctl restart liquidity-migration-bybit-risk.service
if sleeve_on "$LONG_SLEEVE"; then systemctl restart liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service; fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-demo.service; fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-paper.service; fi

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Risk always runs; each sleeve is verified per its toggle (on => active+enabled, off => NOT active).
systemctl is-active --quiet liquidity-migration-bybit-risk.service
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
# The liquidation collector is always-on (enabled+restarted above). Verify it the
# SAME way as the risk service so a deployed code change that crashes the collector
# on startup FAILS the deploy loud - otherwise a broken collector still reaches
# 'deploy-verify-ok' and the success Telegram, and the data loss (unbuyable forward
# liquidation history) is only caught out-of-band by the ~3-minute watchdog
# (deploy-ci-3). is-active catches a crash-loop reaching 'failed'; is-enabled catches
# "we never enabled it".
systemctl is-active --quiet liquidity-migration-liquidation-collector.service
systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service
# Operator-gated forward depth capture: deploy never enables it by itself, but
# if the operator has enabled it, a crash on deployed collector code must fail
# the deploy just like the always-on liquidation collector. Also reject a
# manually-started but disabled unit, because the next reboot/deploy would
# silently stop collecting Bybit depth history.
if systemctl is-enabled --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
  systemctl is-active --quiet liquidity-migration-depth-collector.service
elif systemctl is-active --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
  echo "verify failed: liquidity-migration-depth-collector.service is active but not enabled; use systemctl enable --now or stop it." >&2
  exit 1
fi
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS
fi
# Verify the hedge timer against the SAME state the apply block chose (which keeps it
# enabled when continuous is off but an open hedge leg still needs winding down -
# deploy-env-timers-1), not raw CONTINUOUS_SLEEVE, so a deliberately-kept-open timer
# does not fail verify.
verify_hedge_timer_enable "$_hedge_timer_state"
# Timer verification: is-enabled catches "we never enabled it"; is-active
# catches "we enabled it but something stopped it." Both are fail-loud here
# so deploys can't silently leave the watchdog or daily report off.
systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer
systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer
systemctl is-active --quiet liquidity-migration-demo-liveness.timer
systemctl is-active --quiet liquidity-migration-combined-book-report.timer

systemctl show liquidity-migration-bybit-risk.service \
  --property=ActiveState \
  --property=SubState \
  --property=MainPID \
  --property=ExecMainStatus \
  --no-pager

require_unit_env() {
  _unit="$1"
  _expected="$2"
  if ! systemctl show "$_unit" --property=Environment --value --no-pager | tr ' ' '\n' | grep -Fx -- "$_expected" >/dev/null; then
    echo "verify failed: $_unit missing effective env $_expected" >&2
    return 1
  fi
}

require_unit_env liquidity-migration-bybit-risk.service 'ORDER_SUBMIT_MODE=ws_then_rest'
# SHARED-ACCOUNT SAFETY: the single risk service must read EVERY sleeve's ledger
# root, else a sibling sleeve's live positions look untracked and get flattened.
# Fail the deploy loud if the risk unit isn't wired to both sibling sleeve roots.
require_unit_env liquidity-migration-bybit-risk.service 'LONG_DATA_ROOT=data/bybit-long-demo-event'
require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'
require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_ADDON_DATA_ROOT=data/bybit-continuous-hedge-event'
# Long sleeve assertions: the profile name is intentionally explicit so the live
# env cannot drift to an ambiguous label.
if sleeve_on "$LONG_SLEEVE"; then
  require_unit_env liquidity-migration-bybit-long-demo.service 'SUBMIT_ORDERS=1'
  require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
  require_unit_env liquidity-migration-bybit-long-paper.service 'SUBMIT_ORDERS=0'
  require_unit_env liquidity-migration-bybit-long-paper.service 'PAPER_MODE=1'
  require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
fi
# Order-submitting continuous sleeve assertions: submit-orders config + v2 no-stop lifecycle.
# Only when the sleeve is toggled ON; disabled unit file content must not become
# an unconditional deploy gate.
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
# MONEY-SAFETY: the continuous PAPER shadow must NEVER submit orders (kept UNCONDITIONAL -
# the paper unit must be safe regardless of toggle). Fail loud if the
# paper unit is mis-wired to submit (it must be a no-money dry-run on its own ledger root).
require_unit_env liquidity-migration-bybit-continuous-paper.service 'SUBMIT_ORDERS=0'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'PAPER_MODE=1'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'DATA_ROOT=data/bybit-continuous-paper-event'
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

python_commit="$(git rev-parse --short HEAD)"
echo "deploy-verify-ok commit=$python_commit"

# Send ONE deploy-confirmation telegram after verify passes. Daemons no
# longer fire startup telegrams (default off), so this is the operator's
# only "deploy succeeded, services back up" signal. Best-effort: a curl
# failure must not flip the deploy result - verify already passed.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  deploy_msg="[ok] liquidity-migration deploy-verify-ok commit=$python_commit (services restarted + healthy)"
  curl --silent --show-error --max-time 10 \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$deploy_msg" \
    --data-urlencode "disable_web_page_preview=true" \
    "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
    >/dev/null 2>&1 || echo "WARN: deploy-confirm telegram send failed (verify still passed)"
fi
REMOTE_SCRIPT
} | ssh $SSH_OPTS "$SSH_TARGET" "bash -s"
