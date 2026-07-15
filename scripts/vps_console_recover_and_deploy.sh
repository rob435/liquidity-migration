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
INSTALL_PREFLIGHT_ONLY="${INSTALL_PREFLIGHT_ONLY:-0}"
CUTOVER_PHASE="install-preflight"

case "$INSTALL_PREFLIGHT_ONLY" in
  0|1) ;;
  *)
    echo "Refusing recovery deploy: INSTALL_PREFLIGHT_ONLY must be 0 or 1." >&2
    exit 1
    ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this from the VPS provider console as root." >&2
  exit 1
fi

if [ "$INSTALL_PREFLIGHT_ONLY" = "0" ]; then
  if [ -e /etc/liquidity-migration/account-execution-ready ]; then
    echo "Refusing recovery deploy: retired ambiguous account-execution-ready marker exists." >&2
    exit 1
  fi
  if [ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]; then
    echo "Refusing recovery deploy: missing account-execution-capture-enabled marker." >&2
    exit 1
  fi
  if [ -z "$EXPECTED_COMMIT" ]; then
    echo "Refusing recovery deploy before checkout: full recovery requires EXPECTED_COMMIT bound to the cutover evidence." >&2
    exit 1
  fi
  if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Refusing recovery deploy: full recovery requires the full lowercase latch-bound commit." >&2
    exit 1
  fi
  if ! command -v git >/dev/null 2>&1 || [ ! -d "$REPO_DIR/.git" ]; then
    echo "Refusing recovery deploy before checkout: activated recovery requires the existing latch-bound git checkout; run install-preflight first." >&2
    exit 1
  fi
  if [ "$(git -C "$REPO_DIR" rev-parse --verify HEAD)" != "$EXPECTED_COMMIT" ]; then
    echo "Refusing recovery deploy: activated checkout moved away from latch-bound commit $EXPECTED_COMMIT." >&2
    exit 1
  fi

  # Do not source or execute anything from the checkout until dirty bytes have
  # been archived and replaced by the already-proved HEAD. A modified phase
  # helper or Python verifier must not be able to call itself "activated".
  if [ -n "$(git -C "$REPO_DIR" status --short)" ]; then
    if [ "$CLEAN_DIRTY_CHECKOUT" != "1" ]; then
      echo "Refusing deploy: VPS git checkout is dirty." >&2
      echo "Rerun with CLEAN_DIRTY_CHECKOUT=1 to archive it and restore the exact latch-bound commit." >&2
      git -C "$REPO_DIR" status --short >&2
      exit 1
    fi
    backup_dir="/root/liquidity-migration-deploy-backups"
    mkdir -p "$backup_dir"
    backup_patch="$backup_dir/dirty-checkout-$(date -u +%Y%m%dT%H%M%SZ).patch"
    (
      cd "$REPO_DIR"
      git diff --no-ext-diff --binary > "$backup_patch"
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
      git reset --hard "$EXPECTED_COMMIT"
      git clean -fd
    )
    echo "Cleaned dirty checkout; saved diff/status under $backup_dir"
  fi
  if [ "$(git -C "$REPO_DIR" rev-parse --verify HEAD)" != "$EXPECTED_COMMIT" ] \
    || [ -n "$(git -C "$REPO_DIR" status --short)" ]; then
    echo "Refusing recovery deploy: cleanup did not produce the exact clean latch-bound checkout." >&2
    exit 1
  fi

  phase_python="/usr/bin/python3"
  phase_library="$REPO_DIR/deploy/lib_fresh_epoch.sh"
  if [ ! -x "$phase_python" ] || [ ! -f "$phase_library" ]; then
    echo "Refusing recovery deploy before checkout: clean system Python or the staged fresh-epoch verifier is unavailable; run install-preflight first." >&2
    exit 1
  fi
  # Do not let an ignored checkout venv or local __pycache__ participate in the
  # first trust decision. The verifier has only stdlib/project dependencies.
  phase_pycache="$(mktemp -d /root/liquidity-migration-phase-pycache.XXXXXX)"
  chmod 0700 "$phase_pycache"
  trap 'rm -rf "$phase_pycache"' EXIT
  export PYTHONNOUSERSITE=1
  export PYTHONPYCACHEPREFIX="$phase_pycache"
  cd "$REPO_DIR"
  . "$phase_library"
  CUTOVER_PHASE="$(lm_fresh_epoch_phase "$phase_python")"
  case "$CUTOVER_PHASE" in
    activated)
      # Recovery reuses the exact clean, latch-bound checkout. It does not need
      # a live private-repository lookup after the one-time activation.
      lm_verify_authorized_deploy_epoch "$phase_python" "$REPO_DIR" "$EXPECTED_COMMIT"
      unset GITHUB_TOKEN
      ;;
    preactivation)
      echo "Refusing recovery deploy: fresh-epoch activation has not occurred; use the checked initial deploy while its short-lived authorization is valid." >&2
      exit 1
      ;;
    partial)
      echo "Refusing recovery deploy: fresh-epoch state is partial; preserve it for incident review and do not start services." >&2
      exit 1
      ;;
    *)
      echo "Refusing recovery deploy: unknown fresh-epoch phase '$CUTOVER_PHASE'." >&2
      exit 1
      ;;
  esac
  rm -rf "$phase_pycache"
  trap - EXIT
  unset PYTHONNOUSERSITE PYTHONPYCACHEPREFIX
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
    (
      export GIT_CONFIG_COUNT=1
      export GIT_CONFIG_KEY_0=http.https://github.com/.extraheader
      export GIT_CONFIG_VALUE_0="AUTHORIZATION: Basic $github_basic_auth"
      export GIT_TERMINAL_PROMPT=0
      unset GITHUB_TOKEN
      git "$@"
    )
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}

require_install_preflight_quiescence() {
  [ "$INSTALL_PREFLIGHT_ONLY" = "1" ] || return 0
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "Refusing recovery install preflight: systemctl is unavailable, so fleet quiescence cannot be proved." >&2
    exit 1
  fi
  if ! _preflight_units="$(systemctl list-units 'liquidity-migration-*' --all --no-legend --no-pager --plain 2>/dev/null)"; then
    echo "Refusing recovery install preflight: failed to inspect liquidity-migration unit state." >&2
    exit 1
  fi
  _preflight_running="$(printf '%s\n' "$_preflight_units" | awk 'NF >= 3 && $3 != "inactive" && $3 != "failed" {print $1 " (" $3 ")"}')"
  if [ -n "$_preflight_running" ]; then
    echo "Refusing recovery install preflight: quiesce every liquidity-migration unit before checkout:" >&2
    printf '%s\n' "$_preflight_running" >&2
    exit 1
  fi
}

require_install_preflight_quiescence

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

if [ "$CUTOVER_PHASE" = "activated" ]; then
  if [ "$(git rev-parse --verify HEAD)" != "$EXPECTED_COMMIT" ] \
    || [ -n "$(git status --short)" ]; then
    echo "Refusing recovery deploy: latch-bound checkout changed after clean phase verification." >&2
    exit 1
  fi
  echo "Reusing activated latch-bound checkout without a private-repository fetch."
else
  if git remote get-url "$REMOTE" >/dev/null 2>&1; then
    git remote set-url "$REMOTE" "$REPO_URL"
  else
    git remote add "$REMOTE" "$REPO_URL"
  fi
  git_with_optional_github_token fetch "$REMOTE" "$BRANCH"
  unset GITHUB_TOKEN
  if [ -n "$EXPECTED_COMMIT" ]; then
    # Deploy EXACTLY the requested commit (round 4) - see deploy_vps_live.sh for
    # the trigger->fetch race this closes.
    if ! git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH"; then
      echo "Refusing deploy: expected commit $EXPECTED_COMMIT is not on $REMOTE/$BRANCH" >&2
      exit 1
    fi
    git checkout -B "$BRANCH" "$EXPECTED_COMMIT"
  else
    git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
  fi
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
PYTHON=.venv/bin/python
"$PYTHON" -m pip install --disable-pip-version-check --no-deps \
  --only-binary=:all: -r requirements.lock

validate_account_execution_roots() {
  _root_context="$1"
  shift
  "$PYTHON" - "$_root_context" "$@" <<'PY'
import sys
from itertools import combinations
from pathlib import Path

context, *fields = sys.argv[1:]
if len(fields) != 12 or len(fields) % 2:
    print(f"{context}: internal error while validating account execution roots.", file=sys.stderr)
    raise SystemExit(1)

roots: list[tuple[str, str, Path]] = []
invalid = False
for label, value in zip(fields[::2], fields[1::2], strict=True):
    if not value:
        print(f"{context}: {label} is required.", file=sys.stderr)
        invalid = True
        continue
    path = Path(value)
    if not path.is_absolute():
        print(f"{context}: {label} must be absolute, got {value!r}.", file=sys.stderr)
        invalid = True
        continue
    roots.append((label, value, path.resolve(strict=False)))

for (left_label, left_value, left), (right_label, right_value, right) in combinations(roots, 2):
    if left == right or left in right.parents or right in left.parents:
        print(
            f"{context}: account execution roots must be pairwise disjoint; "
            f"{left_label}={left_value!r} overlaps {right_label}={right_value!r}.",
            file=sys.stderr,
        )
        invalid = True

raise SystemExit(1 if invalid else 0)
PY
}

"$PYTHON" -m pytest \
  tests/test_runtime_scripts.py \
  tests/test_promoted_profiles.py

"$PYTHON" - <<'PY'
# Pin the deployed configs - identical to the strategy-settings gate in deploy_vps_live.sh.
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
assert cont.sizing_mode == "inverse_vol", cont.sizing_mode
assert cont.target_vol_per_name == 0.01, cont.target_vol_per_name
assert cont.vol_weight_clamp == 2.0, cont.vol_weight_clamp
assert cont.btc_trend_gate == "uptrend", cont.btc_trend_gate
assert cont.entry_btc_risk_sizing_enabled is True, cont.entry_btc_risk_sizing_enabled
assert cont.entry_btc_risk_arm_id == "CTRL_BTC_RISK_70_90_35", cont.entry_btc_risk_arm_id
assert cont.entry_btc_risk_low == 0.70, cont.entry_btc_risk_low
assert cont.entry_btc_risk_high == 0.90, cont.entry_btc_risk_high
assert cont.entry_btc_risk_tail_mult == 0.35, cont.entry_btc_risk_tail_mult
print("strategy-settings-ok")
PY

# The install-only recovery phase deliberately ends before secrets, account
# roots, or either cutover marker are inspected. It installs the exact checked-
# out scripts/config and current unit manifest, and removes unknown historical
# units, but never enables, starts, or restarts a current owner/producer.
. deploy/lib_sleeves.sh
if [ "$INSTALL_PREFLIGHT_ONLY" = "1" ]; then
  lm_install_current_systemd_units
  echo "install-preflight-ok commit=$(git rev-parse --short HEAD) current_units_installed=1 current_units_started=0 cutover_markers_untouched=1"
  exit 0
fi
. deploy/lib_systemd_environment.sh

if [ ! -f /etc/liquidity-migration/bybit-demo.env ]; then
  echo "Missing /etc/liquidity-migration/bybit-demo.env; restore secrets before starting services." >&2
  exit 1
fi
if [[ ! "$EXPECTED_TELEGRAM_CHAT_ID" =~ ^-?[0-9]+$ ]]; then
  echo "Refusing recovery deploy: EXPECTED_TELEGRAM_CHAT_ID must be a signed decimal integer." >&2
  exit 1
fi
lm_load_private_systemd_environment "$PYTHON" \
  /etc/liquidity-migration/bybit-demo.env \
  BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY \
  TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID

cp /etc/liquidity-migration/bybit-demo.env "/etc/liquidity-migration/bybit-demo.env.backup.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -Eq '^TELEGRAM_CHAT_ID=' /etc/liquidity-migration/bybit-demo.env; then
  sed -i "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=$EXPECTED_TELEGRAM_CHAT_ID/" /etc/liquidity-migration/bybit-demo.env
else
  printf '\nTELEGRAM_CHAT_ID=%s\n' "$EXPECTED_TELEGRAM_CHAT_ID" >> /etc/liquidity-migration/bybit-demo.env
fi

lm_load_private_systemd_environment "$PYTHON" \
  /etc/liquidity-migration/bybit-demo.env \
  BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY \
  TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID

if [ "${TELEGRAM_CHAT_ID:-}" != "$EXPECTED_TELEGRAM_CHAT_ID" ]; then
  echo "Refusing deploy: TELEGRAM_CHAT_ID is '${TELEGRAM_CHAT_ID:-unset}', expected '$EXPECTED_TELEGRAM_CHAT_ID'" >&2
  exit 1
fi

# DEFENSE-IN-DEPTH on the highest-stakes toggle (deploy-ci-6): this recovery path
# enables+restarts the demo account owner and target producers
# against the account this env file defines. Make recovery fail closed, in parity
# with scripts/deploy_vps_live.sh, so a
# mis-edited bybit-demo.env with a non-false REAL_MONEY value refuses the deploy
# rather than restarting the demo account owner against a real-money
# account. The strategy is NOT validated for real money; promotion is
# operator-gated, never a deploy side effect.
case "${REAL_MONEY:-}" in
  1|true|TRUE|True|yes|YES|Yes|on|ON|On)
    echo "Refusing deploy: REAL_MONEY='${REAL_MONEY}' in /etc/liquidity-migration/bybit-demo.env." \
         "This box deploys a demo account owner; real money is not validated and must not be" \
         "enabled by a deploy. Fix the env file to demo (unset/false REAL_MONEY) and redeploy." >&2
    exit 1
    ;;
  ""|0|false|FALSE|False|no|NO|No|off|OFF|Off)
    ;;
  *)
    echo "Refusing deploy: REAL_MONEY='${REAL_MONEY}' in /etc/liquidity-migration/bybit-demo.env is ambiguous; unset it or use an explicit false value." >&2
    exit 1
    ;;
esac
"$PYTHON" scripts/check_bybit_order_permissions.py --context recovery-deploy

if [ -e /etc/liquidity-migration/account-execution-ready ]; then
  echo "Refusing recovery deploy: retired ambiguous account-execution-ready marker exists." >&2
  exit 1
fi
if [ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]; then
  echo "Refusing recovery deploy: missing account-execution-capture-enabled marker." >&2
  exit 1
fi
. deploy/lib_fresh_epoch.sh
# Recovery never creates or replaces the bound epoch. It source-reopens the
# activated latch and all bound artifacts without renewing the spent receipt.
lm_verify_authorized_deploy_epoch "$PYTHON" "$REPO_DIR" "$EXPECTED_COMMIT"
if [ ! -s /etc/liquidity-migration/account-execution.env ]; then
  echo "Refusing recovery deploy: account-execution.env is missing or empty." >&2
  exit 1
fi
lm_load_private_systemd_environment "$PYTHON" \
  /etc/liquidity-migration/account-execution.env \
  ACCOUNT_EXECUTION_KERNEL_REQUIRED
if [ "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-}" != "1" ]; then
  echo "Refusing recovery deploy: ACCOUNT_EXECUTION_KERNEL_REQUIRED=1 is required." >&2
  exit 1
fi
DEMO_ACCOUNT_EXECUTION_KERNEL_REQUIRED="$ACCOUNT_EXECUTION_KERNEL_REQUIRED"
if [ ! -s /etc/liquidity-migration/account-paper-execution.env ]; then
  echo "Refusing recovery deploy: account-paper-execution.env is missing or empty." >&2
  exit 1
fi
lm_load_private_systemd_environment "$PYTHON" \
  /etc/liquidity-migration/account-paper-execution.env \
  ACCOUNT_PAPER_KERNEL_REQUIRED ACCOUNT_TWIN_CALIBRATION_FILE
PAPER_ACCOUNT_KERNEL_REQUIRED="${ACCOUNT_PAPER_KERNEL_REQUIRED:-}"
PAPER_ACCOUNT_TWIN_CALIBRATION_FILE="${ACCOUNT_TWIN_CALIBRATION_FILE:-}"
unset ACCOUNT_PAPER_KERNEL_REQUIRED ACCOUNT_TWIN_CALIBRATION_FILE
export ACCOUNT_EXECUTION_KERNEL_REQUIRED="$DEMO_ACCOUNT_EXECUTION_KERNEL_REQUIRED"
lm_load_fresh_epoch_roots "$PYTHON" "$REPO_DIR" "$EXPECTED_COMMIT"
if [ "$PAPER_ACCOUNT_KERNEL_REQUIRED" != "1" ]; then
  echo "Refusing recovery deploy: ACCOUNT_PAPER_KERNEL_REQUIRED=1 is required." >&2
  exit 1
fi
if [ ! -s "$PAPER_ACCOUNT_TWIN_CALIBRATION_FILE" ]; then
  echo "Refusing recovery deploy: ACCOUNT_TWIN_CALIBRATION_FILE is missing or empty." >&2
  exit 1
fi
"$PYTHON" -c 'from liquidity_migration.execution_twin_calibration import load_calibration_receipt; import sys; receipt = load_calibration_receipt(sys.argv[1]); raise SystemExit(0 if receipt["execution_twin_gate_passed"] is True else 1)' "$PAPER_ACCOUNT_TWIN_CALIBRATION_FILE" || {
  echo "Refusing recovery deploy: execution-twin calibration gate has not passed." >&2
  exit 1
}
if ! validate_account_execution_roots \
  "Refusing recovery deploy" \
  "demo account root" "$DEMO_ACCOUNT_EXECUTION_ROOT" \
  "demo intent inbox root" "$DEMO_ACCOUNT_INTENT_INBOX_ROOT" \
  "demo capture root" "$DEMO_ACCOUNT_CAPTURE_ROOT" \
  "paper account root" "$PAPER_ACCOUNT_EXECUTION_ROOT" \
  "paper intent inbox root" "$PAPER_ACCOUNT_INTENT_INBOX_ROOT" \
  "paper capture root" "$PAPER_ACCOUNT_CAPTURE_ROOT"; then
  exit 1
fi

# --- per-sleeve kill-switch (deploy/sleeves.env) - the SAME single source of truth as
# scripts/deploy_vps_live.sh, so disaster recovery can NEVER resurrect an OFF sleeve.
lm_install_current_systemd_units
lm_load_sleeve_toggles
lm_write_resolved_sleeve_toggles
lm_verify_resolved_sleeve_toggles
echo "sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"
systemctl enable liquidity-migration-account-execution.service
systemctl enable liquidity-migration-account-paper-execution.service
systemctl restart liquidity-migration-account-execution.service
systemctl restart liquidity-migration-account-paper-execution.service
# Forward-only data collection - parity with scripts/deploy_vps_live.sh: liquidation
# history is unbuyable, so the collector runs always-on like the account owner.
# RESTART (not just enable --now, which is a no-op on a running unit) so recovered
# collector code actually takes effect - append-only JSONL, no order path.
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
# Owners are ready; restart only enabled target producers before any auxiliary
# timer. Continuous daemons own the kline stores from which RMOM is rebuilt.
if sleeve_on "$LONG_SLEEVE"; then systemctl restart liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service; fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-demo.service; fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-paper.service; fi

# Rebuild and verify each enabled continuous root before arming its timer.
if continuous_rmom_refresh_on; then
  systemctl start liquidity-migration-continuous-rmom-refresh.service
  _check_rmom_root() {
    _rmom_label="$1"
    _rmom_root="$2"
    if _rmom_status="$("$PYTHON" scripts/check_residual_momentum_gate.py --path "$_rmom_root" 2>&1)"; then
      echo "continuous ${_rmom_label} rmom gate seeded: ${_rmom_status}"
    else
      echo "ERROR: continuous ${_rmom_label} rmom gate is unusable after recovery refresh: ${_rmom_status}." >&2
      return 1
    fi
  }
  if sleeve_on "$CONTINUOUS_SLEEVE"; then
    _check_rmom_root "demo" "$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet"
  fi
  if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
    _check_rmom_root "paper" "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet"
  fi
  apply_timer_enable on $CONTINUOUS_SLEEVE_TIMERS
  for _rmom_timer in $CONTINUOUS_SLEEVE_TIMERS; do systemctl restart "$_rmom_timer"; done
else
  apply_timer_enable off $CONTINUOUS_SLEEVE_TIMERS
fi

# Mirror deploy_vps_live.sh: keep the publisher alive until canonical hedge
# targets are flat when the continuous sleeve is switched off.
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  _hedge_timer_state=on
else
  _hedge_open="$(ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT" "$PYTHON" - <<'PY' 2>/dev/null || echo unknown
import os
from pathlib import Path

from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.account_strategy_state import canonical_strategy_trade_rows

try:
    root = Path(os.environ["ACCOUNT_ROOT"]).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    rows = canonical_strategy_trade_rows(root, sleeve=SleeveAdapterKind.HEDGE.value)
    print(0 if rows.is_empty() else int(rows.filter(rows["status"] == "open").height))
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
CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"
lm_write_resolved_sleeve_toggles
lm_verify_resolved_sleeve_toggles
apply_hedge_timer_enable "$_hedge_timer_state"

# Recovery watchdog starts last; no timer may race root validation.
systemctl enable --now liquidity-migration-demo-liveness.timer
systemctl restart liquidity-migration-demo-liveness.timer

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Both account owners are mandatory independently of sleeve toggles.
systemctl is-active --quiet liquidity-migration-account-execution.service
systemctl is-enabled --quiet liquidity-migration-account-execution.service
systemctl is-active --quiet liquidity-migration-account-paper-execution.service
systemctl is-enabled --quiet liquidity-migration-account-paper-execution.service
# The liquidation collector is always-on (enabled+restarted above). Verify it the
# SAME way as the account owners so a recovered code change that crashes the collector
# on startup FAILS the recovery loud - otherwise a broken collector still reaches
# 'deploy-verify-ok' and the data loss (unbuyable forward liquidation history) is
# only caught out-of-band by the ~3-minute watchdog (deploy-ci-3). is-active catches
# a crash-loop reaching 'failed'; is-enabled catches "we never enabled it".
systemctl is-active --quiet liquidity-migration-liquidation-collector.service
systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service
if systemctl is-enabled --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
  systemctl is-active --quiet liquidity-migration-depth-collector.service
elif systemctl is-active --quiet liquidity-migration-depth-collector.service 2>/dev/null; then
  echo "verify failed: liquidity-migration-depth-collector.service is active but not enabled; use systemctl enable --now or stop it." >&2
  exit 1
fi
verify_sleeve "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
verify_sleeve "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
lm_verify_no_unknown_liqmig_units
_fresh_active_units=(
  liquidity-migration-account-execution.service
  liquidity-migration-account-paper-execution.service
)
if sleeve_on "$LONG_SLEEVE"; then
  _fresh_active_units+=(
    liquidity-migration-bybit-long-demo.service
    liquidity-migration-bybit-long-paper.service
  )
fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  _fresh_active_units+=(liquidity-migration-bybit-continuous-demo.service)
fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
  _fresh_active_units+=(liquidity-migration-bybit-continuous-paper.service)
fi
lm_verify_active_fresh_processes "$PYTHON" "$REPO_DIR" "$EXPECTED_COMMIT" "${_fresh_active_units[@]}"
if continuous_rmom_refresh_on; then
  verify_timer on $CONTINUOUS_SLEEVE_TIMERS
else
  verify_timer off $CONTINUOUS_SLEEVE_TIMERS
fi
verify_hedge_timer_enable "$_hedge_timer_state"
# Timer parity - recovery must catch a missed enable just like deploy does.
systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer
systemctl is-active --quiet liquidity-migration-demo-liveness.timer

systemctl show liquidity-migration-account-execution.service \
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

require_unit_env liquidity-migration-account-execution.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'
require_unit_env liquidity-migration-account-execution.service 'CONFIRM_DEMO_ORDERS=1'
require_unit_env liquidity-migration-account-execution.service 'TELEGRAM_ENABLED=1'
require_unit_env liquidity-migration-account-paper-execution.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'
# Long sleeve assertions: recovery restarts the long demo/paper units, so it
# must prove the same profile and paper/no-order boundaries as deploy/verify.
if sleeve_on "$LONG_SLEEVE"; then
  require_unit_env liquidity-migration-bybit-long-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'
  require_unit_env liquidity-migration-bybit-long-demo.service 'EXECUTION_ENVIRONMENT=demo'
  require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
  require_unit_env liquidity-migration-bybit-long-paper.service 'EXECUTION_ENVIRONMENT=paper'
  require_unit_env liquidity-migration-bybit-long-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'
  require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
fi
# Order-submitting continuous config asserts only when the sleeve is toggled ON.
# Disabled unit file content must not be an unconditional recovery gate.
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'EXECUTION_ENVIRONMENT=demo'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'STRATEGY_PROFILE=continuous_ensemble_v2'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_LEVERAGE=10'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'NOTIONAL_MULTIPLIER=10'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'PER_POSITION_NOTIONAL_PCT_EQUITY=2'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'
  require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'
fi
# MONEY-SAFETY parity with deploy_vps_live.sh (audit 2026-06-12 round 3): the
# continuous PAPER shadow must NEVER submit orders - UNCONDITIONAL regardless of
# toggle. A mis-edited paper unit previously passed this script.
require_unit_env liquidity-migration-bybit-continuous-paper.service 'EXECUTION_ENVIRONMENT=paper'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'STRATEGY_PROFILE=continuous_ensemble_v2'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'FEATURE_SET=max_ret168'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'ENTRY_EVENT_TRIGGER=none'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'BTC_TREND_GATE=uptrend'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'MAX_HOLD_HOURS=24'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'ENTRY_LEVERAGE=10'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'NOTIONAL_MULTIPLIER=10'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'PER_POSITION_NOTIONAL_PCT_EQUITY=2'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'SIZING_MODE=inverse_vol'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'TARGET_VOL_PER_NAME=0.01'
require_unit_env liquidity-migration-bybit-continuous-paper.service 'VOL_WEIGHT_CLAMP=2'

echo "deploy-verify-ok commit=$(git rev-parse --short HEAD)"

# --- HOST-KEY PIN REMINDER (2026-06-09 incident) -----------------------------------
# A rescue/re-image regenerates the box's SSH host keys, which silently breaks BOTH
# the CI deploy (.github/workflows/vps-deploy.yml pins the ED25519 host key - by
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
echo "Until the pin matches, pushes to main will NOT deploy (host verification fails - by design)."
