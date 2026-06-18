#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
EXPECTED_TELEGRAM_CHAT_ID="${EXPECTED_TELEGRAM_CHAT_ID:-8388367561}"
LOCAL_SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner}"
GITHUB_ACTIONS_SSH_PUBLIC_KEY="${GITHUB_ACTIONS_SSH_PUBLIC_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv liquidity-migration-github-actions-20260609}"
CLEAN_DIRTY_CHECKOUT="${CLEAN_DIRTY_CHECKOUT:-0}"
SYSTEMD_SETTLE_SECONDS="${SYSTEMD_SETTLE_SECONDS:-15}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this from the VPS provider console as root." >&2
  exit 1
fi

missing_prereqs=()
for binary in git python3 sshd; do
  if ! command -v "$binary" >/dev/null 2>&1; then
    missing_prereqs+=("$binary")
  fi
done

if [ "${#missing_prereqs[@]}" -gt 0 ] || ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates git openssh-server python3 python3-venv python3-pip
  else
    echo "Missing deploy prerequisites and apt-get is unavailable: ${missing_prereqs[*]:-python3-venv}" >&2
    exit 1
  fi
fi

chown root:root /root
chmod 700 /root
usermod -U root 2>/dev/null || true
mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
  if ! grep -Fxq "$public_key" /root/.ssh/authorized_keys; then
    printf '%s\n' "$public_key" >> /root/.ssh/authorized_keys
  fi
done
chown -R root:root /root/.ssh
if command -v ssh-keygen >/dev/null 2>&1; then
  echo "Restored authorized key fingerprints:"
  for public_key in "$LOCAL_SSH_PUBLIC_KEY" "$GITHUB_ACTIONS_SSH_PUBLIC_KEY"; do
    tmp_public_key="$(mktemp)"
    printf '%s\n' "$public_key" > "$tmp_public_key"
    ssh-keygen -lf "$tmp_public_key" -E sha256
    rm -f "$tmp_public_key"
  done
fi

if [ -d /etc/ssh ]; then
  mkdir -p /etc/ssh/sshd_config.d
  cat >/etc/ssh/sshd_config.d/99-liquidity-migration-recovery.conf <<'SSH_CONFIG'
PubkeyAuthentication yes
PermitRootLogin prohibit-password
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2
AuthenticationMethods publickey
SSH_CONFIG
  if [ -f /etc/ssh/sshd_config ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
    cp /etc/ssh/sshd_config "/etc/ssh/sshd_config.liquidity-migration-backup.$(date -u +%Y%m%dT%H%M%SZ)"
    tmp_sshd_config="$(mktemp)"
    printf '%s\n' 'Include /etc/ssh/sshd_config.d/*.conf' > "$tmp_sshd_config"
    cat /etc/ssh/sshd_config >> "$tmp_sshd_config"
    cat "$tmp_sshd_config" > /etc/ssh/sshd_config
    rm -f "$tmp_sshd_config"
  fi
fi
if command -v sshd >/dev/null 2>&1; then
  mkdir -p /run/sshd
  sshd -t
  sshd_root_context="user=root,host=localhost,addr=127.0.0.1"
  effective_sshd_config="$(sshd -T -C "$sshd_root_context")"
  printf '%s\n' "$effective_sshd_config" | grep -E '^(pubkeyauthentication|permitrootlogin|authorizedkeysfile|authenticationmethods) '
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^pubkeyauthentication yes$'
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^permitrootlogin (yes|without-password|prohibit-password)$'
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^authorizedkeysfile .*[.]ssh/authorized_keys'
  printf '%s\n' "$effective_sshd_config" | grep -Eq '^authenticationmethods publickey$'
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart ssh.service || systemctl restart sshd.service || true
else
  service ssh restart || service sshd restart || true
fi

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

if [ -e "$REPO_DIR" ] && [ ! -d "$REPO_DIR/.git" ]; then
  backup_dir="/root/liquidity-migration-deploy-backups"
  mkdir -p "$backup_dir"
  backup_path="$backup_dir/non-git-checkout-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$REPO_DIR" "$backup_path"
  echo "Moved non-git checkout to $backup_path"
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  mkdir -p "$(dirname "$REPO_DIR")"
  git_with_optional_github_token clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"

if [ -n "$(git status --short)" ]; then
  if [ "$CLEAN_DIRTY_CHECKOUT" != "1" ]; then
    echo "Refusing deploy: VPS git checkout is dirty." >&2
    echo "Rerun with CLEAN_DIRTY_CHECKOUT=1 to save a patch, reset tracked files, and clean untracked non-ignored files." >&2
    git status --short >&2
    exit 1
  fi
  backup_dir="/root/liquidity-migration-deploy-backups"
  mkdir -p "$backup_dir"
  backup_patch="$backup_dir/dirty-checkout-$(date -u +%Y%m%dT%H%M%SZ).patch"
  git diff > "$backup_patch"
  git status --short > "$backup_patch.status"
  untracked_nul="$backup_patch.untracked-files.nul"
  untracked_list="$backup_patch.untracked-files.txt"
  untracked_archive="$backup_patch.untracked-files.tgz"
  git ls-files --others --exclude-standard -z > "$untracked_nul"
  if [ -s "$untracked_nul" ]; then
    tr '\0' '\n' < "$untracked_nul" > "$untracked_list"
    tar --null -czf "$untracked_archive" --files-from "$untracked_nul"
  else
    rm -f "$untracked_nul"
  fi
  git reset --hard
  git clean -fd
  echo "Cleaned dirty checkout; saved diff/status under $backup_dir"
fi

if git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote set-url "$REMOTE" "$REPO_URL"
else
  git remote add "$REMOTE" "$REPO_URL"
fi
git_with_optional_github_token fetch "$REMOTE" "$BRANCH"
unset GITHUB_TOKEN
if [ -n "$EXPECTED_COMMIT" ]; then
  # Deploy EXACTLY the requested commit (round 4) — see deploy_vps_live.sh for
  # the trigger->fetch race this closes.
  if ! git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH"; then
    echo "Refusing deploy: expected commit $EXPECTED_COMMIT is not on $REMOTE/$BRANCH" >&2
    exit 1
  fi
  git checkout -B "$BRANCH" "$EXPECTED_COMMIT"
else
  git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
PYTHON=.venv/bin/python

"$PYTHON" -m pytest \
  tests/test_runtime_scripts.py \
  tests/test_promoted_profiles.py

"$PYTHON" - <<'PY'
# The daily-short sleeve was ERASED 2026-06-11 (operator order). Pin the surviving
# deployed configs — identical to the strategy-settings gate in deploy_vps_live.sh.
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
assert cont.daily_rebalance_enabled is True, cont.daily_rebalance_enabled
assert cont.daily_rebalance_realized_vol_window_days == 90, cont.daily_rebalance_realized_vol_window_days
assert cont.daily_rebalance_target_daily_vol == 0.045, cont.daily_rebalance_target_daily_vol
assert cont.daily_rebalance_max_scale == 4.0, cont.daily_rebalance_max_scale
assert cont.daily_rebalance_drawdown_half_threshold == -0.04, cont.daily_rebalance_drawdown_half_threshold
assert cont.daily_rebalance_strategy_momentum_window_days == 0, cont.daily_rebalance_strategy_momentum_window_days
assert cont.daily_rebalance_strategy_momentum_min_return == 0.0, cont.daily_rebalance_strategy_momentum_min_return
assert cont.daily_rebalance_strategy_momentum_scale_when_below == 0.0, cont.daily_rebalance_strategy_momentum_scale_when_below
print("strategy-settings-ok")
PY

if [ ! -f /etc/liquidity-migration/bybit-demo.env ]; then
  echo "Missing /etc/liquidity-migration/bybit-demo.env; restore secrets before starting services." >&2
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

# DEFENSE-IN-DEPTH on the highest-stakes toggle (deploy-ci-6): this recovery path
# enables+restarts order-submitting demo units (continuous-demo, long-demo, risk)
# against the account this env file defines; demo-only operation otherwise depends
# solely on the per-process runtime guard validate_order_submit_allowed(). Make the
# RECOVERY itself fail-closed — parity with scripts/deploy_vps_live.sh — so a
# mis-edited bybit-demo.env that ever set REAL_MONEY truthy refuses the deploy
# rather than restarting live order-submitting daemons against a real-money
# account. The strategy is NOT validated for real money; promotion is
# operator-gated, never a deploy side effect.
case "${REAL_MONEY:-}" in
  1|true|TRUE|True|yes|YES|Yes|on|ON|On)
    echo "Refusing deploy: REAL_MONEY='${REAL_MONEY}' in /etc/liquidity-migration/bybit-demo.env." \
         "This box deploys order-submitting demo units; real money is not validated and must not be" \
         "enabled by a deploy. Fix the env file to demo (unset/false REAL_MONEY) and redeploy." >&2
    exit 1
    ;;
esac

# Sync every .service / .timer in deploy/systemd/ so disaster recovery brings
# up the same unit set scripts/deploy_vps_live.sh does — paper, both long
# sleeves, and the demo-health + combined-book-report timers. Hand-listing
# fell behind every time a new unit landed and left recovered VPSes
# partially configured.
for unit in deploy/systemd/liquidity-migration-*.service deploy/systemd/liquidity-migration-*.timer; do
    cp "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
# --- per-sleeve kill-switch (deploy/sleeves.env) — the SAME single source of truth as
# scripts/deploy_vps_live.sh, so disaster recovery can NEVER resurrect an OFF sleeve.
# Previously this path hardcoded every sleeve ON, which would re-enable the look-ahead-
# disabled continuous sleeve (it ships SUBMIT_ORDERS=1) regardless of the toggle. ------
. deploy/lib_sleeves.sh
lm_load_sleeve_toggles
echo "sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"
# One-time cleanup for the ERASED daily-short sleeve (2026-06-11): a recovered older
# host may still have its retired units installed/enabled.
for _retired in $RETIRED_SLEEVE_UNITS liquidity-migration-demo-health.timer liquidity-migration-demo-health.service; do
    systemctl disable --now "$_retired" 2>/dev/null || true
    rm -f "/etc/systemd/system/$_retired"
done
# Reload AFTER removing retired unit files (deploy_vps_live.sh parity) so stale
# erased-unit definitions don't linger in systemd memory on a recovered host.
systemctl daemon-reload
systemctl enable liquidity-migration-bybit-risk.service
# Forward-only data collection — parity with scripts/deploy_vps_live.sh: liquidation
# history is unbuyable, so the collector runs always-on like the risk service.
# RESTART (not just enable --now, which is a no-op on a running unit) so recovered
# collector code actually takes effect — append-only JSONL, no order path.
systemctl enable liquidity-migration-liquidation-collector.service
systemctl restart liquidity-migration-liquidation-collector.service
# The depth collector is operator-gated (recovery installs the unit but must NEVER
# enable it). If the operator HAS enabled it, restart it for the same reason as the
# liquidation collector: recovered collector code must take effect now.
if systemctl is-enabled --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
    systemctl restart liquidity-migration-depth-collector.service
fi
apply_sleeve_enable "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
# Timers must be enable --now: enable alone writes the symlink but doesn't
# start the timer, so the liveness watchdog + daily combined-book report would
# sit dormant on a freshly-recovered VPS. (The demo-health watchdog was erased
# with the short sleeve 2026-06-11 and is removed from the host above.)
systemctl enable --now liquidity-migration-demo-liveness.timer
systemctl enable --now liquidity-migration-combined-book-report.timer
# The continuous rmom-refresh timer is required if either continuous demo or
# paper evidence collection is enabled.
if continuous_rmom_refresh_on; then
  apply_timer_enable on $CONTINUOUS_SLEEVE_TIMERS
else
  apply_timer_enable off $CONTINUOUS_SLEEVE_TIMERS
fi
apply_timer_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS
# Seed the continuous rmom gate BEFORE restarting the continuous daemon — same fix as
# deploy_vps_live.sh (the 2026-06-02 empty-gate blackout: a daemon started into an
# empty gate emits zero entries silently). Best-effort: a first boot with the kline
# store still bootstrapping yields no rows — the 00:20 timer + rmom watchdog cover it.
if continuous_rmom_refresh_on; then
  systemctl start liquidity-migration-continuous-rmom-refresh.service \
    || echo "WARN: rmom seed failed; the daily timer + rmom watchdog will cover it." >&2
fi
# Restart: risk ALWAYS + FIRST (the shared multi-sleeve tracker reading every *_DATA_ROOT
# must be up before any sleeve restarts); then only the ON sleeves (off ones were
# disable --now'd above).
systemctl restart liquidity-migration-bybit-risk.service
if sleeve_on "$LONG_SLEEVE"; then systemctl restart liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service; fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-demo.service; fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-paper.service; fi

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Risk always runs; each sleeve is verified per its toggle (on => active+enabled,
# off => NOT active) — identical to deploy_vps_live.sh.
systemctl is-active --quiet liquidity-migration-bybit-risk.service
systemctl is-enabled --quiet liquidity-migration-bybit-risk.service
# The liquidation collector is always-on (enabled+restarted above). Verify it the
# SAME way as the risk service so a recovered code change that crashes the collector
# on startup FAILS the recovery loud — otherwise a broken collector still reaches
# 'deploy-verify-ok' and the data loss (unbuyable forward liquidation history) is
# only caught out-of-band by the ~3-minute watchdog (deploy-ci-3). is-active catches
# a crash-loop reaching 'failed'; is-enabled catches "we never enabled it".
systemctl is-active --quiet liquidity-migration-liquidation-collector.service
systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS
fi
verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS
# Timer parity — recovery must catch a missed enable just like deploy does.
# (demo-health was erased with the short sleeve 2026-06-11 — don't check it.)
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
# SHARED-ACCOUNT SAFETY: a recovered VPS must keep the single risk service wired to
# read EVERY sleeve's ledger root, else a sibling sleeve's live positions get flattened.
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=LONG_DATA_ROOT=data/bybit-long-demo-event'
systemctl cat liquidity-migration-bybit-risk.service --no-pager | grep -E 'Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'
# Order-submitting continuous config asserts only when the sleeve is toggled ON — a
# retired sleeve's file content must not be an unconditional recovery gate.
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=1'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=STRATEGY_PROFILE=continuous_ensemble_v2'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=SIZING_MODE=inverse_vol'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=TARGET_VOL_PER_NAME=0.01'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=VOL_WEIGHT_CLAMP=2'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=DAILY_REBALANCE_ENABLED=1'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=DAILY_REBALANCE_MAX_SCALE=4'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=STOP_LOSS_PCT=0'
  systemctl cat liquidity-migration-bybit-continuous-demo.service --no-pager | grep -E 'Environment=STOP_APPROACH_FRAC=0'
fi
# MONEY-SAFETY parity with deploy_vps_live.sh (audit 2026-06-12 round 3): the
# continuous PAPER shadow must NEVER submit orders — UNCONDITIONAL regardless of
# toggle. A mis-edited paper unit previously passed this script.
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=SUBMIT_ORDERS=0'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=STRATEGY_PROFILE=continuous_ensemble_v2'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=SIZING_MODE=inverse_vol'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=TARGET_VOL_PER_NAME=0.01'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=VOL_WEIGHT_CLAMP=2'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=DAILY_REBALANCE_ENABLED=1'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=DAILY_REBALANCE_MAX_SCALE=4'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=PAPER_MODE=1'
systemctl cat liquidity-migration-bybit-continuous-paper.service --no-pager | grep -E 'Environment=DATA_ROOT=data/bybit-continuous-paper-event'

echo "deploy-verify-ok commit=$(git rev-parse --short HEAD)"

# --- HOST-KEY PIN REMINDER (2026-06-09 incident) -----------------------------------
# A rescue/re-image regenerates the box's SSH host keys, which silently breaks BOTH
# the CI deploy (.github/workflows/vps-deploy.yml pins the ED25519 host key — by
# design; do NOT re-add keyscan) and the operator's local known_hosts. The 2026-06-08
# recovery left the 2026-06-04 pin stale, so every subsequent auto-deploy fails host
# verification. Print the new identity loudly so updating the pin is part of every
# recovery, not an afterthought.
echo ""
echo "=== ACTION REQUIRED: update the pinned host key after this recovery ==="
echo "new known_hosts line for the vps-deploy.yml printf (and your local known_hosts):"
echo "$(hostname -I 2>/dev/null | awk '{print $1}') $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 || true
echo "ALSO update the VPS_ED25519_FINGERPRINT repo secret/var to the SHA256 above."
echo "Until the pin matches, pushes to main will NOT deploy (host verification fails — by design)."
