#!/usr/bin/env bash
set -euo pipefail

SSH_TARGET="${SSH_TARGET:-root@5.223.42.109}"
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
systemctl disable --now \
  model050426.service \
  model050426-bybit-demo-signal.timer \
  model050426-bybit-demo-signal.service \
  2>/dev/null || true
systemctl enable liquidity-migration-bybit-demo.service
systemctl enable liquidity-migration-bybit-risk.service
systemctl enable liquidity-migration-bybit-paper.service
systemctl enable liquidity-migration-bybit-long-demo.service
systemctl enable liquidity-migration-bybit-long-paper.service
# Timers must be enabled --now: enable alone writes the symlink but does not
# start the timer, so on a fresh VPS the demo-health watchdog + daily combined-
# book Telegram report would sit dormant until someone ran systemctl by hand.
# --now schedules them immediately; subsequent deploys are idempotent.
systemctl enable --now liquidity-migration-demo-health.timer
systemctl enable --now liquidity-migration-combined-book-report.timer
systemctl restart liquidity-migration-bybit-demo.service
systemctl restart liquidity-migration-bybit-risk.service
systemctl restart liquidity-migration-bybit-paper.service
# Long-sleeve services also need restart after a deploy — they share the
# liquidity_migration package with the short side, so any Python change
# requires restarting them too. Previously missed; the long daemon would
# stay on the old code until the next manual restart.
systemctl restart liquidity-migration-bybit-long-demo.service
systemctl restart liquidity-migration-bybit-long-paper.service

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

systemctl is-active --quiet liquidity-migration-bybit-demo.service
systemctl is-active --quiet liquidity-migration-bybit-risk.service
systemctl is-active --quiet liquidity-migration-bybit-paper.service
systemctl is-active --quiet liquidity-migration-bybit-long-demo.service
systemctl is-active --quiet liquidity-migration-bybit-long-paper.service
systemctl is-enabled --quiet liquidity-migration-bybit-demo.service
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
systemctl is-enabled --quiet liquidity-migration-bybit-paper.service
systemctl is-enabled --quiet liquidity-migration-bybit-long-demo.service
systemctl is-enabled --quiet liquidity-migration-bybit-long-paper.service
# Timer verification: is-enabled catches "we never enabled it"; is-active
# catches "we enabled it but something stopped it." Both are fail-loud here
# so deploys can't silently leave the watchdog or daily report off.
systemctl is-enabled --quiet liquidity-migration-demo-health.timer
systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer
systemctl is-active --quiet liquidity-migration-demo-health.timer
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
systemctl cat liquidity-migration-bybit-demo.service --no-pager | grep -E 'Environment=MAX_ACTIVE_SYMBOLS=3'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=ORDER_SUBMIT_MODE=ws_then_rest'

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
