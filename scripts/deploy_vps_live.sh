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
# Capture the previously-deployed commit BEFORE checkout — used below to decide
# whether the dependency manifests changed and the venv needs a pip refresh.
previous_commit="$(git rev-parse HEAD 2>/dev/null || echo "")"
if [ -n "$EXPECTED_COMMIT" ]; then
  # Deploy EXACTLY the commit that triggered this run, not whatever the remote
  # branch head is by fetch time (round 4): with the old check-after-checkout, a
  # second push landing in the trigger->fetch window failed the run AFTER the
  # checkout — box left disk=newer / processes=old, and if the second push was
  # docs-only (outside the workflow paths filter) no later run ever redeployed
  # the first commit. Each run now deploys its own SHA; a queued newer run then
  # moves the box forward.
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
# the previously-deployed commit touches a dependency manifest — or the previous
# commit can't be determined — reinstall. Fail-loud: a pip failure aborts (set -e).
if [ -x .venv/bin/pip ]; then
  if [ -z "$previous_commit" ] \
    || ! git diff --quiet "$previous_commit" HEAD -- requirements.lock pyproject.toml; then
    echo "Dependency manifests changed (or previous commit unknown) — refreshing venv ..."
    .venv/bin/pip install -q -e ".[dev]"
  fi
fi

"$PYTHON" -m pytest \
  tests/test_runtime_scripts.py \
  tests/test_promoted_profiles.py
"$PYTHON" - <<'PY'
# The daily-short sleeve was ERASED 2026-06-11 (operator order); the long profile
# is the one promoted sleeve and is pinned by tests/test_promoted_profiles.py above.
from liquidity_migration.long_native_event_demo import _v11a_long_native_config

long_cfg = _v11a_long_native_config()
assert long_cfg.universe_size == 50
assert long_cfg.max_concurrent_positions == 10
assert long_cfg.cooldown_days == 7
assert long_cfg.weekend_size_mult == 1.5

# Continuous-fade sleeve (de-promoted 2026-06-05, look-ahead invalidated): these
# assertions pin its config so a silent drift can't ship. Whether the order-submitting
# sleeve actually runs is toggled per-sleeve in deploy/sleeves.env (the single source
# of truth — don't hardcode its state here). rmom 0.33; breaker w24/n8; 25% stop.
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
# One backup accumulates per deploy — keep only the 5 newest. (The cp above just
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
echo "sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"
systemctl enable liquidity-migration-bybit-risk.service
# Forward-only data collection (P3, operator-approved 2026-06-10): liquidation
# history is unbuyable, so the collector runs always-on like the risk service.
# RESTART (not just enable --now, which is a no-op on a running unit) so deployed
# collector code changes actually take effect — append-only JSONL, no order path,
# so a bounce loses at most the in-flight websocket messages.
systemctl enable liquidity-migration-liquidation-collector.service
systemctl restart liquidity-migration-liquidation-collector.service
# The depth collector is operator-gated (deploy installs the unit but never enables it).
# If the operator HAS enabled it, restart it for the same reason as the liquidation
# collector: deployed collector code must take effect, not wait for the next reboot.
if systemctl is-enabled --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
    systemctl restart liquidity-migration-depth-collector.service
fi
# One-time cleanup for the ERASED daily-short sleeve (2026-06-11): stop, disable,
# and remove its units from the host so stale enabled units can't crash-loop on a
# deleted entrypoint.
for _retired in $RETIRED_SLEEVE_UNITS liquidity-migration-demo-health.timer liquidity-migration-demo-health.service; do
    systemctl disable --now "$_retired" 2>/dev/null || true
    rm -f "/etc/systemd/system/$_retired"
done
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
# deploy-env-timers-3: the paper unit hard-codes KLINES_FOLLOW_ROOT to the DEMO root,
# so with CONTINUOUS_SLEEVE=off + CONTINUOUS_PAPER_SLEEVE=on the demo daemon stops, its
# kline store stops advancing, and the paper shadow follows a FROZEN snapshot while the
# rmom gate is rebuilt only from that frozen demo root. Documented-valid but degrades for
# up to ~2 days while appearing healthy. Warn loudly at deploy time so the operator drops
# the KLINES_FOLLOW_ROOT override (run the paper shadow on its own pool) rather than
# discovering it only via the slower kline-staleness / rmom-staleness watchdogs.
if ! sleeve_on "$CONTINUOUS_SLEEVE" && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
  echo "WARN: CONTINUOUS_SLEEVE=off but CONTINUOUS_PAPER_SLEEVE=on — the paper shadow's" \
       "KLINES_FOLLOW_ROOT still points at the now-FROZEN demo kline store" \
       "(data/bybit-continuous-demo-event), so paper evidence runs on stale market data" \
       "for up to ~2 days while looking healthy. Drop the KLINES_FOLLOW_ROOT line from" \
       "deploy/systemd/liquidity-migration-bybit-continuous-paper.service so the shadow" \
       "streams its own klines, then redeploy." >&2
fi
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
# The refresher always (re)builds the DEMO root's gate — the paper shadow FOLLOWS
# that root (KLINES_FOLLOW_ROOT), so the demo parquet is the one to check in both
# demo and paper-only modes (run_continuous_rmom_refresh.sh never writes the paper root).
_rmom_root="data/bybit-continuous-demo-event/residual_momentum.parquet"
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
# Hedge timer gating (deploy-env-timers-1): the daily BTC/ETH hedge long is booked
# with stop_price=take_profit_price=planned_exit_ts_ms=0, so the always-on risk service
# TRACKS but is contractually FORBIDDEN from force-exiting it — the daily hedge timer is
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
    # Could not read the ledger — fail safe: do NOT auto-disable a timer that might be
    # the only manager of an open live position. Keep it enabled and page the operator.
    echo "CRITICAL: CONTINUOUS_SLEEVE=off but the hedge addon ledger" \
         "(data/bybit-continuous-hedge-event) could not be read — KEEPING the hedge timer" \
         "enabled (fail-safe) so a possibly-open, stopless hedge long is not orphaned." \
         "Inspect the addon ledger and flatten the hedge manually before retiring continuous." >&2
    _hedge_timer_state=on
  elif [ "${_hedge_open:-0}" -gt 0 ]; then
    echo "CRITICAL: CONTINUOUS_SLEEVE=off but the hedge addon ledger holds ${_hedge_open}" \
         "OPEN hedge row(s). The hedge long is stopless (risk service tracks but never exits it)" \
         "and the daily hedge timer is its ONLY manager — KEEPING the timer enabled so the daily" \
         "run can trim it to flat. It will auto-disable on the next deploy once the leg is flat." >&2
    _hedge_timer_state=on
  else
    echo "hedge leg flat (no open rows) -> disabling the hedge timer with continuous off." >&2
    _hedge_timer_state=off
  fi
fi
apply_timer_enable "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS

# --- restart: only the ON sleeves (off sleeves were disable --now'd above); risk always. ---
# Long/continuous share the liquidity_migration package with the short side, so any Python
# change requires restarting every running sleeve to pick up the new code.
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
# on startup FAILS the deploy loud — otherwise a broken collector still reaches
# 'deploy-verify-ok' and the success Telegram, and the data loss (unbuyable forward
# liquidation history) is only caught out-of-band by the ~3-minute watchdog
# (deploy-ci-3). is-active catches a crash-loop reaching 'failed'; is-enabled catches
# "we never enabled it".
systemctl is-active --quiet liquidity-migration-liquidation-collector.service
systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS $CONTINUOUS_FORWARD_REPORT_TIMERS
fi
# Verify the hedge timer against the SAME state the apply block chose (which keeps it
# enabled when continuous is off but an open hedge leg still needs winding down —
# deploy-env-timers-1), not raw CONTINUOUS_SLEEVE, so a deliberately-kept-open timer
# does not fail verify.
verify_timer "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS
# Timer verification: is-enabled catches "we never enabled it"; is-active
# catches "we enabled it but something stopped it." Both are fail-loud here
# so deploys can't silently leave the watchdog or daily report off. (The
# demo-health watchdog was erased with the short sleeve 2026-06-11 and is
# actively removed from the host above — do not re-add checks for it.)
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
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=ORDER_SUBMIT_MODE=ws_then_rest'
# SHARED-ACCOUNT SAFETY: the single risk service must read EVERY sleeve's ledger
# root, else a sibling sleeve's live positions look untracked and get flattened.
# Fail the deploy loud if the risk unit isn't wired to both sibling sleeve roots.
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
