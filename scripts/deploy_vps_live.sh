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
  tests/test_liquidity_migration_cli.py::test_cli_volume_events_defaults_to_selected_liquidity_migration \
  tests/test_liquidity_migration_event_demo_cycle.py::test_demo_relaxed_profile_lowers_gates_for_more_demo_trades

"$PYTHON" - <<'PY'
from liquidity_migration.event_demo import _demo_event_config, _demo_strategy_id
from liquidity_migration.volume_events import VolumeEventResearchConfig

promoted = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
demo = _demo_event_config(VolumeEventResearchConfig(), profile="demo_relaxed")

assert _demo_strategy_id("promoted") == "liqmig_union_q40_h3_tp26_g100_qsqueeze"
assert _demo_strategy_id("demo_relaxed") == "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6"
assert promoted.take_profit_pcts == (0.26,)
# promoted = drop_all_4 (2026-05-30) + age300 + ff6_4pct (2026-05-31).
assert promoted.max_active_symbols == 12
assert promoted.universe_rank_max == 99999
assert promoted.liquidity_migration_pit_age_days_min == 300
assert promoted.failed_fade_exit_hours == 6
assert promoted.failed_fade_min_mfe_pct == 0.01
assert promoted.failed_fade_loss_pct == 0.04
assert promoted.failed_fade_close_location_min == 0.0
assert demo.take_profit_pcts == (0.21,)
assert demo.failed_fade_exit_hours == 6
assert demo.failed_fade_min_mfe_pct == 0.01
assert demo.failed_fade_loss_pct == 0.04
assert demo.failed_fade_close_location_min == 0.0

# Continuous-fade sleeve (OFF / de-promoted 2026-06-05, look-ahead invalidated): these
# assertions still pin its config so a silent drift can't ship IF it is ever re-enabled —
# the order-submitting sleeve is disabled by default (CONTINUOUS_SLEEVE=off). rmom 0.33;
# breaker w24/n8; 25% stop.
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
cont = ContinuousDemoCycleConfig()
assert cont.rmom_quantile == 0.33, cont.rmom_quantile
assert cont.entry_pause_after_adverse_exits == 8, cont.entry_pause_after_adverse_exits
assert cont.entry_pause_window_minutes == 1440, cont.entry_pause_window_minutes
assert cont.stop_loss_pct == 0.25, cont.stop_loss_pct
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

# Sync every .service / .timer in deploy/systemd/ so any unit added
# to the repo (e.g. demo-health, combined-book-report, future units)
# auto-deploys instead of needing a one-off manual cp. The long
# demo/paper omission previously caused MemoryMax=2G to sit on disk
# unused for an OOM-loop cycle — globbing prevents that whole class
# of "added a unit but forgot to wire it into deploy" misses.
for unit in deploy/systemd/liquidity-migration-*.service deploy/systemd/liquidity-migration-*.timer; do
    cp "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload

# --- per-sleeve kill-switch (deploy/sleeves.env) ----------------------------------------
# Single source of truth for which strategy sleeves run. Default (all "on") is byte-identical
# to the previous unconditional enables. Flip a sleeve to "off" in deploy/sleeves.env (or
# /etc/liquidity-migration/sleeves.env) + redeploy to RETIRE it — it stays disabled across
# deploys; "off" stops new entries but ws_risk + the server-side disaster stops keep open
# positions protected until they exit (no flatten). The risk service always runs.
. deploy/lib_sleeves.sh
lm_load_sleeve_toggles
echo "sleeves: SHORT=$SHORT_SLEEVE SHORT_PAPER=$SHORT_PAPER_SLEEVE LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"
systemctl enable liquidity-migration-bybit-risk.service
apply_sleeve_enable "$SHORT_SLEEVE" $SHORT_SLEEVE_UNITS
apply_sleeve_enable "$SHORT_PAPER_SLEEVE" $SHORT_PAPER_SLEEVE_UNITS
apply_sleeve_enable "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
# Timers must be enabled --now: enable alone writes the symlink but does not
# start the timer, so on a fresh VPS the demo-health watchdog + daily combined-
# book Telegram report would sit dormant until someone ran systemctl by hand.
# --now schedules them immediately; subsequent deploys are idempotent.
systemctl enable --now liquidity-migration-demo-health.timer
systemctl enable --now liquidity-migration-demo-liveness.timer
systemctl enable --now liquidity-migration-combined-book-report.timer
# Daily refresh of the continuous-fade rmom gate + the gate seed run when either the
# continuous demo sleeve or its no-order paper evidence collector is on.
if continuous_rmom_refresh_on; then
apply_timer_enable on $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
# Seed the rmom gate NOW rather than waiting for the 00:20 UTC timer. Without this a
# fresh deploy starts the continuous daemon into an EMPTY gate -> the live decile drops
# every symbol (silent zero-signal blackout — the 2026-06-02 incident). The refresh is a
# oneshot, so this blocks until the parquet is (re)built from the sleeve's kline store.
# best-effort + fail-safe: a FIRST deploy (klines still bootstrapping) yields no rows ->
# WARN, not fail (no rmom => no entries, never WRONG entries; the daily timer + the
# rmom-staleness watchdog cover the gap; re-run the service once klines are up).
echo "Seeding continuous rmom gate (residual_momentum.parquet) ..."
systemctl start liquidity-migration-continuous-rmom-refresh.service \
  || echo "WARN: rmom seed service failed; the 00:20 timer + rmom watchdog will cover it." >&2
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  _rmom_root="data/bybit-continuous-demo-event/residual_momentum.parquet"
else
  _rmom_root="data/bybit-continuous-paper-event/residual_momentum.parquet"
fi
_rmom_rows="$(RMOM_ROOT="$_rmom_root" "$PYTHON" - <<'PY' 2>/dev/null || echo 0
import os
import pathlib, polars as pl
p = pathlib.Path(os.environ["RMOM_ROOT"])
print(pl.read_parquet(p).height if p.exists() else 0)
PY
)"
if [ "${_rmom_rows:-0}" -le 0 ]; then
  echo "WARN: continuous rmom gate is EMPTY after seed (likely a first deploy with the kline" \
       "store still bootstrapping). The continuous sleeve emits NO entries until rmom is built —" \
       "re-run 'systemctl start liquidity-migration-continuous-rmom-refresh.service' once the" \
       "daemon has bootstrapped klines. Fail-safe: no entries, never wrong entries." >&2
else
  echo "continuous rmom gate seeded: ${_rmom_rows} rows."
fi
else
  echo "kill-switch: continuous demo+paper sleeves off -> skipping rmom timer + gate seed." >&2
  apply_timer_enable off $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
fi
apply_timer_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS

# --- restart: only the ON sleeves (off sleeves were disable --now'd above); risk always. ---
# Long/continuous share the liquidity_migration package with the short side, so any Python
# change requires restarting every running sleeve to pick up the new code.
systemctl restart liquidity-migration-bybit-risk.service
if sleeve_on "$SHORT_SLEEVE"; then systemctl restart liquidity-migration-bybit-demo.service; fi
if sleeve_on "$SHORT_SLEEVE" && sleeve_on "$SHORT_PAPER_SLEEVE"; then systemctl restart liquidity-migration-bybit-paper.service; fi
if sleeve_on "$LONG_SLEEVE"; then systemctl restart liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service; fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-demo.service; fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-paper.service; fi

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Risk always runs; each sleeve is verified per its toggle (on => active+enabled, off => NOT active).
systemctl is-active --quiet liquidity-migration-bybit-risk.service
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
verify_sleeve "$SHORT_SLEEVE" $SHORT_SLEEVE_UNITS
verify_sleeve "$SHORT_PAPER_SLEEVE" $SHORT_PAPER_SLEEVE_UNITS
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
fi
verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS
# Timer verification: is-enabled catches "we never enabled it"; is-active
# catches "we enabled it but something stopped it." Both are fail-loud here
# so deploys can't silently leave the watchdog or daily report off.
systemctl is-enabled --quiet liquidity-migration-demo-health.timer
systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer
systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer
systemctl is-active --quiet liquidity-migration-demo-health.timer
systemctl is-active --quiet liquidity-migration-demo-liveness.timer
systemctl is-active --quiet liquidity-migration-combined-book-report.timer

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
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=STRATEGY_PROFILE=promoted'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=INTERVAL_SECONDS=60'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_RANK_END=0'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_MAX_SYMBOLS=0'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=UNIVERSE_MIN_TURNOVER_24H=0'
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=MAX_ACTIVE_SYMBOLS=12'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=ORDER_SUBMIT_MODE=ws_then_rest'
# SHARED-ACCOUNT SAFETY: the single risk service must read EVERY sleeve's ledger
# root, else a sibling sleeve's live positions look untracked and get flattened.
# Fail the deploy loud if the risk unit isn't wired to track the long sleeve. (Continuous
# is OFF/de-promoted; the risk unit still reads its root so any legacy open positions stay
# tracked rather than flattened.)
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=LONG_DATA_ROOT=data/bybit-long-demo-event'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'
# Order-submitting continuous sleeve assertions: submit-orders config + disaster stop present.
# Only when the sleeve is toggled ON; a retired sleeve's file content must not be an
# unconditional deploy gate.
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=1'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=STOP_LOSS_PCT=0.25'
fi
# MONEY-SAFETY: the continuous PAPER shadow must NEVER submit orders (kept UNCONDITIONAL —
# the paper unit must be safe regardless of toggle). Fail loud if the
# paper unit is mis-wired to submit (it must be a no-money dry-run on its own ledger root).
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=0'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=PAPER_MODE=1'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=DATA_ROOT=data/bybit-continuous-paper-event'

python_commit="$(git rev-parse --short HEAD)"
echo "deploy-verify-ok commit=$python_commit"

# Send ONE deploy-confirmation telegram after verify passes. Daemons no
# longer fire startup telegrams (default off), so this is the operator's
# only "deploy succeeded, services back up" signal. Best-effort: a curl
# failure must not flip the deploy result — verify already passed.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  deploy_msg="✅ liquidity-migration deploy-verify-ok commit=$python_commit (services restarted + healthy)"
  curl --silent --show-error --max-time 10 \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$deploy_msg" \
    --data-urlencode "disable_web_page_preview=true" \
    "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
    >/dev/null 2>&1 || echo "WARN: deploy-confirm telegram send failed (verify still passed)"
fi
REMOTE_SCRIPT
} | ssh $SSH_OPTS "$SSH_TARGET" "bash -s"
