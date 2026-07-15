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
RMOM_BOOTSTRAP_TIMEOUT_SECONDS="${RMOM_BOOTSTRAP_TIMEOUT_SECONDS:-1800}"
RMOM_BOOTSTRAP_RETRY_SECONDS="${RMOM_BOOTSTRAP_RETRY_SECONDS:-30}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
INSTALL_PREFLIGHT_ONLY="${INSTALL_PREFLIGHT_ONLY:-0}"

case "$INSTALL_PREFLIGHT_ONLY" in
  0|1) ;;
  *)
    echo "Refusing deploy: INSTALL_PREFLIGHT_ONLY must be 0 or 1." >&2
    exit 1
    ;;
esac

for _duration_name in SYSTEMD_SETTLE_SECONDS RMOM_BOOTSTRAP_TIMEOUT_SECONDS RMOM_BOOTSTRAP_RETRY_SECONDS; do
  _duration_value="${!_duration_name}"
  if [[ ! "$_duration_value" =~ ^[0-9]+$ ]]; then
    echo "Refusing deploy: $_duration_name must be a non-negative integer." >&2
    exit 1
  fi
done
if [ "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS" -le 0 ] || [ "$RMOM_BOOTSTRAP_RETRY_SECONDS" -le 0 ]; then
  echo "Refusing deploy: RMOM bootstrap timeout/retry durations must be positive." >&2
  exit 1
fi

if ! git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
  echo "Refusing deploy: BRANCH must be a valid Git branch name." >&2
  exit 1
fi

# Local deploys of the private GitHub repository should work through the same
# checked path as Actions.  Reuse an authenticated gh keyring token when the
# caller did not explicitly provide one; never print it, persist it, or place it
# in argv. Public/non-GitHub remotes continue to work without gh.
if [ -z "$GITHUB_TOKEN" ] && [[ "$REPO_URL" == https://github.com/* ]] \
  && command -v gh >/dev/null 2>&1; then
  _local_github_token="$(gh auth token --hostname github.com 2>/dev/null || true)"
  if [ -n "$_local_github_token" ]; then
    GITHUB_TOKEN="$_local_github_token"
    echo "deploy auth: using the authenticated local gh credential for the remote fetch"
  fi
  unset _local_github_token
fi

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
  printf 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS=%q\n' "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS"
  printf 'RMOM_BOOTSTRAP_RETRY_SECONDS=%q\n' "$RMOM_BOOTSTRAP_RETRY_SECONDS"
  printf 'INSTALL_PREFLIGHT_ONLY=%q\n' "$INSTALL_PREFLIGHT_ONLY"
  cat <<'REMOTE_SCRIPT'
set -euo pipefail

git_with_optional_github_token() {
  if [ -n "${GITHUB_TOKEN:-}" ] && [[ "$REPO_URL" == https://github.com/* ]]; then
    local github_basic_auth
    github_basic_auth="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
    (
      # Dynamic git configuration is inherited as environment, not exposed in
      # git's argv. Do not preserve caller-supplied config slots across this
      # security boundary.
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
    echo "Refusing install preflight: systemctl is unavailable, so fleet quiescence cannot be proved." >&2
    exit 1
  fi
  if ! _preflight_units="$(systemctl list-units 'liquidity-migration-*' --all --no-legend --no-pager --plain 2>/dev/null)"; then
    echo "Refusing install preflight: failed to inspect liquidity-migration unit state." >&2
    exit 1
  fi
  _preflight_running="$(printf '%s\n' "$_preflight_units" | awk '$3 != "inactive" && $3 != "failed" {print $1 " (" $3 ")"}')"
  if [ -n "$_preflight_running" ]; then
    echo "Refusing install preflight: quiesce every liquidity-migration unit before checkout:" >&2
    printf '%s\n' "$_preflight_running" >&2
    exit 1
  fi
}

require_full_deploy_authority() {
  [ "$INSTALL_PREFLIGHT_ONLY" = "0" ] || return 0
  if [ -e /etc/liquidity-migration/account-execution-ready ]; then
    echo "Refusing deploy: retired ambiguous account-execution-ready marker exists." >&2
    exit 1
  fi
  if [ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]; then
    echo "Refusing deploy: missing account-execution-capture-enabled marker." >&2
    exit 1
  fi
  if [ -z "$EXPECTED_COMMIT" ]; then
    echo "Refusing deploy before checkout: full deploy requires EXPECTED_COMMIT bound to the cutover evidence." >&2
    exit 1
  fi
  _authority_python="$REPO_DIR/.venv/bin/python"
  _authority_script="$REPO_DIR/scripts/account_execution_cutover_authority.py"
  if [ ! -x "$_authority_python" ] || [ ! -f "$_authority_script" ]; then
    echo "Refusing deploy before checkout: staged cutover authority verifier is unavailable; run install-preflight first." >&2
    exit 1
  fi
  if ! GITHUB_TOKEN="$GITHUB_TOKEN" \
    "$_authority_python" "$_authority_script" verify \
    --receipt /etc/liquidity-migration/account-execution-deploy-ready \
    --expected-commit "$EXPECTED_COMMIT" \
    --repo-root "$REPO_DIR"; then
    echo "Refusing deploy before checkout: deploy-ready authorization is missing, stale, altered, or not bound to this host/commit." >&2
    exit 1
  fi
}

require_install_preflight_quiescence
require_full_deploy_authority

cd "$REPO_DIR"

if [ -n "$(git status --short)" ]; then
  echo "Refusing deploy: VPS git checkout is dirty." >&2
  git status --short >&2
  exit 1
fi

checkout_expected_branch_commit() {
  if git remote get-url "$REMOTE" >/dev/null 2>&1; then
    git remote set-url "$REMOTE" "$REPO_URL"
  else
    git remote add "$REMOTE" "$REPO_URL"
  fi
  git_with_optional_github_token fetch "$REMOTE" \
    "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
  # Capture the previously-deployed commit BEFORE checkout - used below to decide
  # whether the dependency manifests changed and the venv needs a pip refresh.
  previous_commit="$(git rev-parse HEAD 2>/dev/null || echo "")"
  if [ -n "$EXPECTED_COMMIT" ]; then
    if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
      echo "Refusing deploy: EXPECTED_COMMIT must be a 7-40 character hexadecimal commit id" >&2
      exit 1
    fi
    if ! expected_commit_full="$(git rev-parse --verify "${EXPECTED_COMMIT}^{commit}" 2>/dev/null)"; then
      echo "Refusing deploy: expected commit prefix $EXPECTED_COMMIT is missing or ambiguous" >&2
      exit 1
    fi
    # Deploy exactly the commit that triggered this run. A queued newer run can
    # move the box forward later; this run must not silently checkout a different
    # branch head during the trigger->fetch window.
    if ! git merge-base --is-ancestor "$expected_commit_full" "$REMOTE/$BRANCH"; then
      echo "Refusing deploy: expected commit $expected_commit_full is not on $REMOTE/$BRANCH" >&2
      exit 1
    fi
    git checkout -B "$BRANCH" "$expected_commit_full"
  else
    git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
  fi
}

checkout_expected_branch_commit

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
PYTHON=.venv/bin/python

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

# Install only the exact versions reviewed in this commit. The checkout itself
# supplies importable source from WorkingDirectory, so an editable install and
# its unpinned build-isolation environment are neither needed nor allowed.
"$PYTHON" -m pip install --disable-pip-version-check --no-deps \
  --only-binary=:all: -r requirements.lock

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
# can't ship. Whether the demo target producer actually runs is toggled per-sleeve
# in deploy/sleeves.env (the single source
# of truth - don't hardcode its state here). v2 is demo/paper only; do not treat
# this target profile or its execution-stress scale as real-money authorization.
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

# Break the account-owner cutover bootstrap cycle without weakening its
# deployment gate. This phase installs the checked-out scripts/config by
# virtue of the exact checkout above, installs the current systemd manifest,
# and stops/removes unknown historical units. It never reads or creates either
# cutover marker and never enables, starts, or restarts a current unit.
. deploy/lib_sleeves.sh
if [ "$INSTALL_PREFLIGHT_ONLY" = "1" ]; then
  lm_install_current_systemd_units
  echo "install-preflight-ok commit=$(git rev-parse --short HEAD) current_units_installed=1 current_units_started=0 cutover_markers_untouched=1"
  exit 0
fi
. deploy/lib_systemd_environment.sh

if [ ! -f /etc/liquidity-migration/bybit-demo.env ]; then
  echo "Missing /etc/liquidity-migration/bybit-demo.env" >&2
  exit 1
fi
if [[ ! "$EXPECTED_TELEGRAM_CHAT_ID" =~ ^-?[0-9]+$ ]]; then
  echo "Refusing deploy: EXPECTED_TELEGRAM_CHAT_ID must be a signed decimal integer." >&2
  exit 1
fi
lm_load_private_systemd_environment "$PYTHON" \
  /etc/liquidity-migration/bybit-demo.env \
  BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY \
  TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID

cp /etc/liquidity-migration/bybit-demo.env "/etc/liquidity-migration/bybit-demo.env.backup.$(date -u +%Y%m%dT%H%M%SZ)"
# One backup accumulates per deploy - keep only the 5 newest. (The cp above just
# wrote one, so the glob always matches and ls can't fail under pipefail.)
ls -1t /etc/liquidity-migration/bybit-demo.env.backup.* 2>/dev/null | tail -n +6 | xargs -r rm -f
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

# DEFENSE-IN-DEPTH on the highest-stakes toggle: the sole demo account owner
# runs on the account this env file defines. Make the deploy itself fail closed:
# if a mis-edited bybit-demo.env sets REAL_MONEY to anything but an explicit
# false form, refuse before restarting
# the owner. The strategy is NOT validated
# for real money; promotion is operator-gated, never a deploy side effect.
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
"$PYTHON" scripts/check_bybit_order_permissions.py --context deploy

# There is one execution topology: target-only sleeves plus the demo and paper
# account owners. Missing capture/deploy authorization or an owner environment stops the
# deploy; it never resurrects direct sleeve/risk order mutation.
if [ -e /etc/liquidity-migration/account-execution-ready ]; then
  echo "Refusing deploy: retired ambiguous account-execution-ready marker exists." >&2
  exit 1
fi
if [ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]; then
  echo "Refusing deploy: missing account-execution-capture-enabled marker." >&2
  exit 1
fi
if ! GITHUB_TOKEN="$GITHUB_TOKEN" \
  "$PYTHON" scripts/account_execution_cutover_authority.py verify \
  --receipt /etc/liquidity-migration/account-execution-deploy-ready \
  --expected-commit "$EXPECTED_COMMIT" \
  --repo-root "$REPO_DIR"; then
  echo "Refusing deploy: deploy-ready authorization failed after checkout." >&2
  exit 1
fi
. deploy/lib_fresh_epoch.sh
# One-time flat cutover transition. This source-reopens the authorization,
# proves the registered natural units are still stopped, proves all ten new
# roots are still empty, and atomically materializes their exact late env files.
# A partially activated/populated epoch must use the explicit recovery path;
# this deploy path never guesses that populated roots are safe to reuse.
lm_prepare_authorized_deploy_epoch "$PYTHON" "$REPO_DIR" "$EXPECTED_COMMIT"
unset GITHUB_TOKEN
if [ ! -s /etc/liquidity-migration/account-execution.env ]; then
  echo "Refusing deploy: account-execution.env is missing or empty." >&2
  exit 1
fi
lm_load_private_systemd_environment "$PYTHON" \
  /etc/liquidity-migration/account-execution.env \
  ACCOUNT_EXECUTION_KERNEL_REQUIRED
if [ "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-}" != "1" ]; then
  echo "Refusing deploy: ACCOUNT_EXECUTION_KERNEL_REQUIRED=1 is required." >&2
  exit 1
fi
DEMO_ACCOUNT_EXECUTION_KERNEL_REQUIRED="$ACCOUNT_EXECUTION_KERNEL_REQUIRED"
if [ ! -s /etc/liquidity-migration/account-paper-execution.env ]; then
  echo "Refusing deploy: account-paper-execution.env is missing or empty." >&2
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
  echo "Refusing deploy: ACCOUNT_PAPER_KERNEL_REQUIRED=1 is required." >&2
  exit 1
fi
if [ ! -s "$PAPER_ACCOUNT_TWIN_CALIBRATION_FILE" ]; then
  echo "Refusing deploy: ACCOUNT_TWIN_CALIBRATION_FILE is missing or empty." >&2
  exit 1
fi
"$PYTHON" -c 'from liquidity_migration.execution_twin_calibration import load_calibration_receipt; import sys; receipt = load_calibration_receipt(sys.argv[1]); raise SystemExit(0 if receipt["execution_twin_gate_passed"] is True else 1)' "$PAPER_ACCOUNT_TWIN_CALIBRATION_FILE" || {
  echo "Refusing deploy: execution-twin calibration gate has not passed." >&2
  exit 1
}
if ! validate_account_execution_roots \
  "Refusing deploy" \
  "demo account root" "$DEMO_ACCOUNT_EXECUTION_ROOT" \
  "demo intent inbox root" "$DEMO_ACCOUNT_INTENT_INBOX_ROOT" \
  "demo capture root" "$DEMO_ACCOUNT_CAPTURE_ROOT" \
  "paper account root" "$PAPER_ACCOUNT_EXECUTION_ROOT" \
  "paper intent inbox root" "$PAPER_ACCOUNT_INTENT_INBOX_ROOT" \
  "paper capture root" "$PAPER_ACCOUNT_CAPTURE_ROOT"; then
  exit 1
fi

# --- per-sleeve kill-switch (deploy/sleeves.env) ----------------------------------------
# Single source of truth for which strategy sleeves run. Flip a sleeve to "off" in deploy/sleeves.env (or
# /etc/liquidity-migration/sleeves.env) + redeploy to RETIRE it - it stays disabled across
# deploys; "off" stops new targets while the account owner retains shared-account visibility.
lm_install_current_systemd_units
lm_load_sleeve_toggles
lm_write_resolved_sleeve_toggles
lm_verify_resolved_sleeve_toggles
echo "sleeves: LONG=$LONG_SLEEVE CONTINUOUS=$CONTINUOUS_SLEEVE CONTINUOUS_PAPER=$CONTINUOUS_PAPER_SLEEVE"
# Forward-only data collection (P3, operator-approved 2026-06-10): liquidation
# history is unbuyable, so the collector runs always-on like the account owner.
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
# Start both execution owners before any target producer. There is no legacy
# fallback; failure here aborts the deploy before sleeve restarts.
systemctl enable liquidity-migration-account-execution.service
systemctl enable liquidity-migration-account-paper-execution.service
systemctl restart liquidity-migration-account-execution.service
systemctl restart liquidity-migration-account-paper-execution.service
apply_sleeve_enable "$LONG_SLEEVE" $LONG_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_SLEEVE_UNITS
apply_sleeve_enable "$CONTINUOUS_PAPER_SLEEVE" $CONTINUOUS_PAPER_SLEEVE_UNITS
# Start only owner-dependent target producers now. The owner units' ExecStartPost
# readiness checks make each restart block until its exact route and market tape
# are healthy. Continuous producers must bootstrap their new kline roots before
# RMOM can be built, so starting RMOM against an empty pre-bootstrap root is an
# ordering bug, not a warning to suppress.
if sleeve_on "$LONG_SLEEVE"; then systemctl restart liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service; fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-demo.service; fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then systemctl restart liquidity-migration-bybit-continuous-paper.service; fi

# Daily refresh of the continuous-fade RMOM gate. No timer or hedge/liveness
# auxiliary starts until each enabled continuous producer has bootstrapped and
# its own fresh-root gate validates.
if continuous_rmom_refresh_on; then
_check_rmom_root() {
  _rmom_label="$1"
  _rmom_root="$2"
  if _rmom_status="$("$PYTHON" scripts/check_residual_momentum_gate.py --path "$_rmom_root" 2>&1)"; then
    echo "continuous ${_rmom_label} rmom gate ok: ${_rmom_status}"
    return 0
  fi
  echo "ERROR: continuous ${_rmom_label} rmom gate is EMPTY, provisional-only, or stale after deploy gate check: ${_rmom_status}." >&2
  return 1
}
_rmom_needs_seed=0
_rmom_seed_reason=""
_add_rmom_seed_reason() {
  if [ -n "$_rmom_seed_reason" ]; then
    _rmom_seed_reason="${_rmom_seed_reason}, $1"
  else
    _rmom_seed_reason="$1"
  fi
  _rmom_needs_seed=1
}
if [ -z "$previous_commit" ] || ! git diff --quiet "$previous_commit" HEAD -- \
  pyproject.toml \
  requirements.lock \
  deploy/systemd/liquidity-migration-continuous-rmom-refresh.service \
  scripts/run_continuous_rmom_refresh.sh \
  scripts/precompute_residual_momentum.py \
  scripts/check_residual_momentum_gate.py \
  liquidity_migration/_common.py \
  liquidity_migration/risk_model.py \
  liquidity_migration/daily_feature_panel.py \
  liquidity_migration/storage.py; then
  _add_rmom_seed_reason "rmom build/validation code changed"
fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  if _rmom_status="$("$PYTHON" scripts/check_residual_momentum_gate.py \
      --path "$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet" 2>&1)"; then
    echo "continuous demo rmom gate already current: ${_rmom_status}"
  else
    _add_rmom_seed_reason "demo gate missing/stale"
  fi
fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
  if _rmom_status="$("$PYTHON" scripts/check_residual_momentum_gate.py \
      --path "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet" 2>&1)"; then
    echo "continuous paper rmom gate already current: ${_rmom_status}"
  else
    _add_rmom_seed_reason "paper gate missing/stale"
  fi
fi
if [ "$_rmom_needs_seed" -eq 1 ]; then
  # Seed only after the continuous daemons have started their fresh-root kline
  # bootstrap. A failure is fatal: the flat cutover cannot silently ship a
  # zero-signal sleeve and hope a later timer repairs it.
  echo "Seeding continuous rmom gate (${_rmom_seed_reason}) ..."
  _rmom_deadline=$(( $(date +%s) + RMOM_BOOTSTRAP_TIMEOUT_SECONDS ))
  while true; do
    _rmom_attempt_ok=1
    systemctl reset-failed liquidity-migration-continuous-rmom-refresh.service 2>/dev/null || true
    if ! systemctl start liquidity-migration-continuous-rmom-refresh.service; then
      _rmom_attempt_ok=0
    fi
    if sleeve_on "$CONTINUOUS_SLEEVE" \
      && ! _check_rmom_root "demo" "$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet"; then
      _rmom_attempt_ok=0
    fi
    if sleeve_on "$CONTINUOUS_PAPER_SLEEVE" \
      && ! _check_rmom_root "paper" "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet"; then
      _rmom_attempt_ok=0
    fi
    if [ "$_rmom_attempt_ok" -eq 1 ]; then
      break
    fi
    if [ "$(date +%s)" -ge "$_rmom_deadline" ]; then
      echo "ERROR: fresh continuous roots did not produce valid RMOM gates within ${RMOM_BOOTSTRAP_TIMEOUT_SECONDS}s." >&2
      exit 1
    fi
    echo "RMOM not ready; waiting ${RMOM_BOOTSTRAP_RETRY_SECONDS}s for fresh-root kline bootstrap ..." >&2
    sleep "$RMOM_BOOTSTRAP_RETRY_SECONDS"
  done
else
  echo "continuous rmom gates are current and build code is unchanged; skipping seed"
fi
if sleeve_on "$CONTINUOUS_SLEEVE"; then
  _check_rmom_root "demo" "$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet"
fi
if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
  _check_rmom_root "paper" "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet"
fi
apply_timer_enable on $CONTINUOUS_SLEEVE_TIMERS
for _rmom_timer in $CONTINUOUS_SLEEVE_TIMERS; do systemctl restart "$_rmom_timer"; done
else
  echo "kill-switch: continuous demo+paper sleeves off -> skipping rmom timer + gate seed." >&2
  apply_timer_enable off $CONTINUOUS_SLEEVE_TIMERS
fi
# The hedge publisher is the only producer that can replace an open hedge target
# with zero. When CONTINUOUS is disabled, keep its timer alive until the canonical
# account projection is flat; never infer exposure from a retired sleeve ledger.
# _hedge_timer_state is the intended hedge-timer state; the verify block below reuses
# it so apply and verify never disagree (a kept-open timer must not fail verify_timer off).
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
    echo "CRITICAL: CONTINUOUS_SLEEVE=off but canonical hedge targets could not be read from" \
         "$ACCOUNT_EXECUTION_ROOT; KEEPING the hedge target timer enabled." >&2
    _hedge_timer_state=on
  elif [ "${_hedge_open:-0}" -gt 0 ]; then
    echo "CRITICAL: CONTINUOUS_SLEEVE=off but the account kernel holds ${_hedge_open}" \
         "open hedge target(s); KEEPING the publisher timer enabled until it queues zero." >&2
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

# The liveness watchdog starts last, after owners, target producers, fresh RMOM,
# and the hedge timer all have their final state.
systemctl enable --now liquidity-migration-demo-liveness.timer
systemctl restart liquidity-migration-demo-liveness.timer

if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then
  sleep "$SYSTEMD_SETTLE_SECONDS"
fi

# Both account owners are mandatory; each sleeve is verified per its toggle.
systemctl is-active --quiet liquidity-migration-account-execution.service
systemctl is-enabled --quiet liquidity-migration-account-execution.service
systemctl is-active --quiet liquidity-migration-account-paper-execution.service
systemctl is-enabled --quiet liquidity-migration-account-paper-execution.service
# The liquidation collector is always-on (enabled+restarted above). Verify it the
# SAME way as the account owners so a deployed code change that crashes the collector
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
# Verify the hedge timer against the SAME state the apply block chose (which keeps it
# enabled when continuous is off but an open hedge leg still needs winding down -
# deploy-env-timers-1), not raw CONTINUOUS_SLEEVE, so a deliberately-kept-open timer
# does not fail verify.
verify_hedge_timer_enable "$_hedge_timer_state"
# Timer verification: is-enabled catches "we never enabled it"; is-active
# catches "we enabled it but something stopped it." Both are fail-loud here
# so deploys can't silently leave the watchdog off.
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
# Long sleeve assertions: the profile name is intentionally explicit so the live
# env cannot drift to an ambiguous label.
if sleeve_on "$LONG_SLEEVE"; then
  require_unit_env liquidity-migration-bybit-long-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'
  require_unit_env liquidity-migration-bybit-long-demo.service 'EXECUTION_ENVIRONMENT=demo'
  require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
  require_unit_env liquidity-migration-bybit-long-paper.service 'EXECUTION_ENVIRONMENT=paper'
  require_unit_env liquidity-migration-bybit-long-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'
  require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'
fi
# Order-submitting continuous sleeve assertions: account route plus active target profile.
# Only when the sleeve is toggled ON; disabled unit file content must not become
# an unconditional deploy gate.
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
# MONEY-SAFETY: the continuous PAPER shadow must NEVER submit orders (kept UNCONDITIONAL -
# the paper unit must be safe regardless of toggle). Fail loud if the
# paper unit is mis-wired to submit (it must publish only to the deterministic
# paper account owner on its own ledger root).
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
