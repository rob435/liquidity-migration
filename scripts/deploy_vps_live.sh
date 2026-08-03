#!/usr/bin/env bash
# Staged VPS lifecycle plus a guarded, flat-account one-command rollout.
set -euo pipefail

MODE="${1:-${DEPLOY_MODE:-verify}}"
if [ "$#" -gt 0 ]; then shift; fi
case "$MODE" in
    install|activate|verify|staged|rollout|stop-mainnet) ;;
    activate-mainnet)
        echo "activate-mainnet retired 2026-08-03: the arming switch is REAL_MONEY=true in /etc/liquidity-migration/bybit-mainnet.env; a plain activate or rollout starts the mainnet fleet when it is armed" >&2
        exit 2
        ;;
    *) echo "invalid deploy mode: $MODE" >&2; exit 2 ;;
esac

deploy_usage() {
    cat >&2 <<'USAGE'
usage: deploy_vps_live.sh {install|activate|verify|staged|rollout|stop-mainnet}
  --profile operational                   required for staged and rollout
  --stop-first / --no-stop-first          install|activate|staged: stop a running
                                          fleet instead of refusing. Default: stop
                                          unless real money is armed.
  --require-flat                          rollout: gate on a flat demo account
                                          rather than reporting residuals
  --refresh-demo-rules                    install|staged: re-probe stale demo
                                          rules (live PostOnly orders, <=200 USDT
                                          per symbol)
USAGE
    exit 2
}

DEPLOY_PROFILE=""
STOP_FIRST=auto
REQUIRE_FLAT=0
REFRESH_DEMO_RULES="${ROLLOUT_REFRESH_STALE_DEMO_RULES:-0}"

require_mode() {
    local flag="$1"
    shift
    case " $* " in
        *" $MODE "*) return 0 ;;
    esac
    echo "$flag is not a $MODE argument" >&2
    deploy_usage
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --profile)
            require_mode --profile staged rollout
            [ "$#" -ge 2 ] || { echo "--profile requires a value" >&2; exit 2; }
            DEPLOY_PROFILE="$2"
            shift 2
            ;;
        --stop-first)
            require_mode --stop-first install activate staged
            STOP_FIRST=1
            shift
            ;;
        --no-stop-first)
            require_mode --no-stop-first install activate staged
            STOP_FIRST=0
            shift
            ;;
        --require-flat)
            require_mode --require-flat rollout
            REQUIRE_FLAT=1
            shift
            ;;
        --refresh-demo-rules)
            require_mode --refresh-demo-rules install staged
            REFRESH_DEMO_RULES=1
            shift
            ;;
        *) echo "unknown $MODE argument: $1" >&2; deploy_usage ;;
    esac
done
case "$MODE" in
    staged|rollout)
        case "$DEPLOY_PROFILE" in
            operational) ;;
            demo-operational)
                echo "profile demo-operational retired with paper trading (2026-08-03); use --profile operational" >&2
                exit 2
                ;;
            *) echo "$MODE requires --profile operational" >&2; exit 2 ;;
        esac
        ;;
esac

SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3}"
REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

EXPECTED_COMMIT_EXPLICIT=0
if [ -n "$EXPECTED_COMMIT" ]; then
    EXPECTED_COMMIT_EXPLICIT=1
    if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        echo "EXPECTED_COMMIT must be a full lowercase 40-character commit" >&2
        exit 2
    fi
fi
if ! /usr/bin/git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
    echo "BRANCH is not a valid Git branch" >&2
    exit 2
fi

if [[ "$MODE" = install || "$MODE" = staged || "$MODE" = rollout ]] && [ -z "$GITHUB_TOKEN" ] \
    && [[ "$REPO_URL" == https://github.com/* ]] && command -v gh >/dev/null 2>&1; then
    GITHUB_TOKEN="$(gh auth token --hostname github.com 2>/dev/null || true)"
fi

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LOCAL_REPOSITORY="$(cd -P -- "$SCRIPT_DIRECTORY/.." && pwd)"
[[ -d "$LOCAL_REPOSITORY/.git" && ! -L "$LOCAL_REPOSITORY/.git" ]] || {
    echo "deploy must run from a checkout with a real .git directory" >&2
    exit 1
}
LOCAL_GIT=(
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent LANG=C LC_ALL=C
    GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1
    /usr/bin/git --no-pager --git-dir="$LOCAL_REPOSITORY/.git"
    --work-tree="$LOCAL_REPOSITORY" -c core.fsmonitor=false -c core.filemode=true
    -C "$LOCAL_REPOSITORY"
)
# Unset means "the tip this checkout knows about": the remote-tracking branch
# when it exists, otherwise HEAD. The host still refuses any commit that is not
# an ancestor of $REMOTE/$BRANCH, so a defaulted value cannot deploy something
# that never reached the branch.
if [ "$EXPECTED_COMMIT_EXPLICIT" -eq 0 ]; then
    EXPECTED_COMMIT_SOURCE="$REMOTE/$BRANCH"
    EXPECTED_COMMIT="$(
        "${LOCAL_GIT[@]}" rev-parse --verify --quiet "refs/remotes/$REMOTE/$BRANCH^{commit}" \
            2>/dev/null || true
    )"
    if [ -z "$EXPECTED_COMMIT" ]; then
        EXPECTED_COMMIT_SOURCE=HEAD
        EXPECTED_COMMIT="$(
            "${LOCAL_GIT[@]}" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null || true
        )"
    fi
    [ -n "$EXPECTED_COMMIT" ] || {
        echo "cannot resolve a default EXPECTED_COMMIT from $REMOTE/$BRANCH or HEAD" >&2
        exit 2
    }
    echo "EXPECTED_COMMIT defaulted to $EXPECTED_COMMIT ($EXPECTED_COMMIT_SOURCE)" >&2
fi
if [ "$("${LOCAL_GIT[@]}" cat-file -t "$EXPECTED_COMMIT" 2>/dev/null || true)" != commit ]; then
    echo "EXPECTED_COMMIT is not a local commit object: $EXPECTED_COMMIT" >&2
    exit 1
fi
if ! MAINTENANCE_LOCK_HELPER_B64="$(
    "${LOCAL_GIT[@]}" show \
        "$EXPECTED_COMMIT:liquidity_migration/ops/maintenance_lock.py" \
    | /usr/bin/python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode("ascii"))'
)"; then
    echo "expected commit does not contain the maintenance lock helper: $EXPECTED_COMMIT" >&2
    exit 1
fi
[[ -n "$MAINTENANCE_LOCK_HELPER_B64" ]] || {
    echo "expected commit returned an empty maintenance lock helper" >&2
    exit 1
}
ROLLOUT_READINESS_HELPER_B64=""
if [ "$MODE" = rollout ]; then
    if ! ROLLOUT_READINESS_HELPER_B64="$(
        "${LOCAL_GIT[@]}" show \
            "$EXPECTED_COMMIT:scripts/vps/check_deploy_rollout_readiness.py" \
        | /usr/bin/python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode("ascii"))'
    )"; then
        echo "expected commit does not contain the rollout readiness helper: $EXPECTED_COMMIT" >&2
        exit 1
    fi
    [[ -n "$ROLLOUT_READINESS_HELPER_B64" ]] || {
        echo "expected commit returned an empty rollout readiness helper" >&2
        exit 1
    }
fi
read -r -a SSH_ARGS <<< "$SSH_OPTS"
{
    printf 'MODE=%q\n' "$MODE"
    printf 'REPO_URL=%q\n' "$REPO_URL"
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'REMOTE=%q\n' "$REMOTE"
    printf 'BRANCH=%q\n' "$BRANCH"
    printf 'EXPECTED_COMMIT=%q\n' "$EXPECTED_COMMIT"
    printf 'EXPECTED_COMMIT_EXPLICIT=%q\n' "$EXPECTED_COMMIT_EXPLICIT"
    printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
    printf 'DEPLOY_PROFILE=%q\n' "$DEPLOY_PROFILE"
    printf 'STOP_FIRST=%q\n' "$STOP_FIRST"
    printf 'REQUIRE_FLAT=%q\n' "$REQUIRE_FLAT"
    printf 'ROLLOUT_REFRESH_STALE_DEMO_RULES=%q\n' "$REFRESH_DEMO_RULES"
    printf 'MAINTENANCE_LOCK_HELPER_B64=%q\n' "$MAINTENANCE_LOCK_HELPER_B64"
    printf 'ROLLOUT_READINESS_HELPER_B64=%q\n' "$ROLLOUT_READINESS_HELPER_B64"
	cat <<'REMOTE_SCRIPT'
# `-E` propagates the ERR trap into shell functions so a strict phase can still
# report which phase died; see run_strict_phase below.
set -Eeuo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

fail() { report_strict_phase_failure 1; echo "deploy failed: $*" >&2; exit 1; }

# Bash disables `set -e` (and the ERR trap) for the whole dynamic extent of a
# function invoked from a condition context (`if`, `&&`, `||`); a nested
# `set -e` does not restore it until the enclosing command finishes. So:
#   * `run_strict_phase` never puts its payload in a condition context, keeping
#     errexit lethal for the payload and everything it calls.
#   * `run_phase` (leaf commands, which may run under suppressed errexit) aborts
#     explicitly on non-zero; `exit` is honoured in any errexit context.
#     `RUN_PHASE_COLLECT_STATUS=1` opts a caller into aggregating the status
#     itself — only `run_phase_pair` does.
RUN_PHASE_COLLECT_STATUS=0
STRICT_PHASE_LABEL=""
STRICT_PHASE_STARTED=0

report_strict_phase_failure() {
    local status="${1:-1}"
    [ -n "$STRICT_PHASE_LABEL" ] || return 0
    printf 'phase-failed name=%s elapsed_seconds=%s status=%s\n' \
        "$STRICT_PHASE_LABEL" "$(( $(date +%s) - STRICT_PHASE_STARTED ))" "$status" >&2
    STRICT_PHASE_LABEL=""
    return 0
}

trap 'report_strict_phase_failure $?' ERR

run_strict_phase() {
    local label="$1"
    shift
    STRICT_PHASE_LABEL="$label"
    STRICT_PHASE_STARTED="$(date +%s)"
    printf 'phase-start name=%s utc=%s\n' "$label" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$@"
    printf 'phase-ok name=%s elapsed_seconds=%s\n' \
        "$label" "$(( $(date +%s) - STRICT_PHASE_STARTED ))"
    STRICT_PHASE_LABEL=""
}

run_phase() {
    local label="$1" started finished status
    shift
    started="$(date +%s)"
    printf 'phase-start name=%s utc=%s\n' "$label" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if "$@"; then
        status=0
    else
        status=$?
    fi
    finished="$(date +%s)"
    if [ "$status" -eq 0 ]; then
        printf 'phase-ok name=%s elapsed_seconds=%s\n' "$label" "$((finished - started))"
        return 0
    fi
    printf 'phase-failed name=%s elapsed_seconds=%s status=%s\n' \
        "$label" "$((finished - started))" "$status" >&2
    if [ "${RUN_PHASE_COLLECT_STATUS:-0}" -eq 0 ]; then
        fail "phase $label failed with status $status"
    fi
    return "$status"
}

run_phase_pair() {
    local group="$1" left_label="$2" left_function="$3"
    local right_label="$4" right_function="$5"
    local started finished left_pid right_pid left_status=0 right_status=0
    started="$(date +%s)"
    printf 'phase-group-start name=%s members=%s,%s utc=%s\n' \
        "$group" "$left_label" "$right_label" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    RUN_PHASE_COLLECT_STATUS=1 run_phase "$left_label" "$left_function" &
    left_pid=$!
    RUN_PHASE_COLLECT_STATUS=1 run_phase "$right_label" "$right_function" &
    right_pid=$!
    if wait "$left_pid"; then left_status=0; else left_status=$?; fi
    if wait "$right_pid"; then right_status=0; else right_status=$?; fi
    finished="$(date +%s)"
    if [ "$left_status" -eq 0 ] && [ "$right_status" -eq 0 ]; then
        printf 'phase-group-ok name=%s elapsed_seconds=%s\n' \
            "$group" "$((finished - started))"
        return 0
    fi
    printf 'phase-group-failed name=%s elapsed_seconds=%s left_status=%s right_status=%s\n' \
        "$group" "$((finished - started))" "$left_status" "$right_status" >&2
    [ "$left_status" -ne 0 ] && return "$left_status"
    return "$right_status"
}

GIT_ENV=(
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent LANG=C LC_ALL=C
    GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1
)
GIT_COMMAND=(
    /usr/bin/git --no-pager --no-optional-locks
    --git-dir="$REPO_DIR/.git" --work-tree="$REPO_DIR"
    -c "safe.directory=$REPO_DIR"
    -c core.fsmonitor=false -c core.filemode=true -c core.hooksPath=/dev/null
    -C "$REPO_DIR"
)

safe_git() {
    "${GIT_ENV[@]}" "${GIT_COMMAND[@]}" "$@"
}

safe_git_with_index() {
    local index_path="$1"
    shift
    "${GIT_ENV[@]}" GIT_INDEX_FILE="$index_path" "${GIT_COMMAND[@]}" "$@"
}

acquire_maintenance_locks() {
    local lock_dir=/run/liquidity-migration helper_output
    local maintenance_device maintenance_inode deploy_device deploy_inode reset_device reset_inode
    # maintenance.lock is the canonical mutex; the two retired leaves stay
    # nested so an old deploy or reset process is still excluded.
    helper_output="$(maintenance_lock_helper prepare-host)" \
        || fail "cannot prepare persistent host maintenance locks safely"
    IFS=$'\t' read -r \
        maintenance_device maintenance_inode deploy_device deploy_inode reset_device reset_inode \
        <<< "$helper_output"
    for value in \
        "$maintenance_device" "$maintenance_inode" "$deploy_device" \
        "$deploy_inode" "$reset_device" "$reset_inode"; do
        [[ "$value" =~ ^[0-9]+$ ]] \
            || fail "maintenance lock helper returned invalid identity metadata"
    done
    exec 9<"$lock_dir/maintenance.lock" \
        || fail "cannot open canonical maintenance lock without truncation"
    exec 8<"$lock_dir/deploy.lock" \
        || fail "cannot open legacy deploy lock without truncation"
    exec 7</run/lock/liquidity-migration-ledger-reset.lock \
        || fail "cannot open legacy reset lock without truncation"
    maintenance_lock_helper acquire-inherited \
        --lock 9 "$lock_dir/maintenance.lock" "$maintenance_device" "$maintenance_inode" \
        --lock 8 "$lock_dir/deploy.lock" "$deploy_device" "$deploy_inode" \
        --lock 7 /run/lock/liquidity-migration-ledger-reset.lock "$reset_device" "$reset_inode" \
        || fail "another maintenance operation is active or a lock path changed"
}

maintenance_lock_helper() {
    /usr/bin/python3 -c '
import base64
import sys

encoded = sys.argv[1]
arguments = sys.argv[2:]
source = base64.b64decode(encoded, validate=True)
sys.argv = ["maintenance_lock.py", *arguments]
namespace = {"__file__": "<transmitted-maintenance-lock-helper>", "__name__": "__main__"}
exec(compile(source, namespace["__file__"], "exec"), namespace)
' "$MAINTENANCE_LOCK_HELPER_B64" "$@"
}

rollout_readiness_helper() {
    "$PYTHON" -c '
import base64
import sys

encoded = sys.argv[1]
arguments = sys.argv[2:]
source = base64.b64decode(encoded, validate=True)
sys.argv = ["check_deploy_rollout_readiness.py", *arguments]
namespace = {"__file__": "scripts/vps/check_deploy_rollout_readiness.py", "__name__": "__main__"}
exec(compile(source, namespace["__file__"], "exec"), namespace)
' "$ROLLOUT_READINESS_HELPER_B64" "$@"
}

PROFILE_MARKER=/etc/liquidity-migration/profile
# Retired paper-fleet artifacts, removed from any host that still carries them.
# The runtime user/group stay if present (inert without units); state roots
# stay on disk as historical record.
RETIRED_PAPER_CONFIG_DIR=/etc/liquidity-migration/account-paper-execution
RETIRED_PAPER_ENVIRONMENT=/etc/liquidity-migration/account-paper-execution.env
LONG_DEMO_ROOT=/opt/liquidity-migration/data/bybit-long-demo-event
CARRY_DEMO_ROOT=/opt/liquidity-migration/data/bybit-carry-demo-event

retire_paper_host_config() {
    rm -f "$RETIRED_PAPER_ENVIRONMENT"
    rm -rf "$RETIRED_PAPER_CONFIG_DIR"
}

prepare_demo_runtime_config() {
    [ "$REPO_DIR" = /opt/liquidity-migration ] \
        || fail "systemd runtime paths require REPO_DIR=/opt/liquidity-migration"
    for path in \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env; do
        [ -f "$path" ] && [ ! -L "$path" ] || fail "missing real private config: $path"
        chown root:root "$path"
        chmod 0600 "$path"
    done
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/account-execution.env \
        ACCOUNT_SYMBOLS_FILE CANDIDATE_UNIVERSE_FILE \
        ACCOUNT_DEMO_RULES_FILE ACCOUNT_RISK_POLICY_FILE ACCOUNT_CAPTURE_ROOT \
        ACCOUNT_EXECUTION_ROOT
    demo_symbols="$ACCOUNT_SYMBOLS_FILE"
    demo_candidate="${CANDIDATE_UNIVERSE_FILE:-}"
    demo_rules="$ACCOUNT_DEMO_RULES_FILE"
    demo_risk="$ACCOUNT_RISK_POLICY_FILE"
    demo_capture="$ACCOUNT_CAPTURE_ROOT"
    demo_account_root="$ACCOUNT_EXECUTION_ROOT"
    [ "${demo_account_root#/}" != "$demo_account_root" ] \
        || fail "demo account root must be absolute: $demo_account_root"
    [ "${demo_capture#/}" != "$demo_capture" ] \
        || fail "demo account capture root must be absolute: $demo_capture"
    [ -d "$demo_capture" ] && [ ! -L "$demo_capture" ] \
        || fail "missing real demo account capture root: $demo_capture"
    for path in "$demo_symbols" "$demo_rules" "$demo_risk"; do
        [ "${path#/}" != "$path" ] || fail "demo account input must be absolute: $path"
        [ -f "$path" ] && [ ! -L "$path" ] || fail "missing real demo account input: $path"
        chown root:root "$path"
        chmod 0600 "$path"
    done
    operational_profile_source="$REPO_DIR/configs/operational.demo.json"
    [ -f "$operational_profile_source" ] && [ ! -L "$operational_profile_source" ] \
        || fail "missing tracked operational profile: $operational_profile_source"
    "$PYTHON" - "$operational_profile_source" <<'PY'
import sys
from liquidity_migration.policy.operational_profile import load_operational_profile

load_operational_profile(sys.argv[1])
PY
    # Install only while units are quiescent (install_mode enforces that). The
    # owner and all producers then read these bytes via ACCOUNT_RISK_POLICY_FILE.
    install -o root -g root -m 0600 "$operational_profile_source" "$demo_risk"
    "$PYTHON" - "$demo_risk" <<'PY'
import sys
from liquidity_migration.policy.operational_profile import load_operational_profile

load_operational_profile(sys.argv[1])
PY
    if [ -z "$demo_candidate" ]; then
        "$PYTHON" - /etc/liquidity-migration/account-execution.env "$demo_symbols" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.strategy.account_candidate_universe import load_candidate_universe
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment

path = Path(sys.argv[1])
symbols = str(Path(sys.argv[2]))
load_candidate_universe(symbols)
values = load_private_systemd_environment(path)
values["CANDIDATE_UNIVERSE_FILE"] = symbols
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(value)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
        demo_candidate="$demo_symbols"
    fi
    [ "$demo_candidate" = "$demo_symbols" ] \
        || fail "demo candidate universe is not the owner symbols file"

    "$PYTHON" - /etc/liquidity-migration/account-execution.env \
        "$demo_capture/strategy-targets.jsonl" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.policy.systemd_environment import load_private_systemd_environment

path = Path(sys.argv[1])
target_capture = Path(sys.argv[2])
if not target_capture.is_absolute() or target_capture.name != "strategy-targets.jsonl":
    raise SystemExit("demo strategy target capture path is invalid")
values = load_private_systemd_environment(path)
values["STRATEGY_TARGET_CAPTURE_PATH"] = str(target_capture)
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(value)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY

    install -d -o root -g root -m 0700 /etc/liquidity-migration
    chown root:root /etc/liquidity-migration/sleeves.resolved.env
    chmod 0600 /etc/liquidity-migration/sleeves.resolved.env
    retire_paper_host_config

    root_uid="$(id -u root)"
    root_gid="$(id -g root)"

    demo_tree_preflight_phase() {
        "$PYTHON" -m liquidity_migration.ops.reset_path_safety preflight-demo \
            --anchor "$REPO_DIR/data" \
            --root "$LONG_DEMO_ROOT" \
            --root "$CARRY_DEMO_ROOT"
    }
    demo_tree_normalize_phase() {
        "$PYTHON" -m liquidity_migration.ops.reset_path_safety normalize-demo \
            --anchor "$REPO_DIR/data" \
            --root "$LONG_DEMO_ROOT" \
            --root "$CARRY_DEMO_ROOT" \
            --uid "$root_uid" --gid "$root_gid" --create-missing
    }

    # The normalizer performs a full read-only descriptor-rooted plan first and
    # an independent final rescan after mutating.
    run_phase demo-tree-preflight demo_tree_preflight_phase \
        || fail "demo runtime descriptor/mount preflight failed"
    run_phase demo-tree-normalize demo_tree_normalize_phase \
        || fail "descriptor-rooted demo runtime normalization failed"
    chown root:root \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env
    chmod 0600 \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env
}

require_checkout() {
    [ -d "$REPO_DIR/.git" ] && [ ! -L "$REPO_DIR/.git" ] \
        || fail "missing trusted Git checkout: $REPO_DIR"
    cd "$REPO_DIR"
}

clean_checkout_status() {
    local expected_commit="$1"
    local temporary_directory temporary_index refresh_status diff_status untracked
    temporary_directory="$(
        /usr/bin/mktemp -d /run/liquidity-migration/deploy-index.XXXXXX
    )" || return 2
    /bin/chmod 0700 "$temporary_directory" || {
        /bin/rmdir "$temporary_directory" 2>/dev/null || true
        return 2
    }
    temporary_index="$temporary_directory/index"
    if ! safe_git_with_index "$temporary_index" read-tree "$expected_commit" >/dev/null; then
        /bin/rm -f -- "$temporary_index"
        /bin/rmdir "$temporary_directory" 2>/dev/null || true
        return 2
    fi
    if safe_git_with_index "$temporary_index" update-index --refresh >/dev/null; then
        refresh_status=0
    else
        refresh_status=$?
    fi
    if (( refresh_status > 1 )); then
        /bin/rm -f -- "$temporary_index"
        /bin/rmdir "$temporary_directory" 2>/dev/null || true
        return 2
    fi
    if safe_git_with_index "$temporary_index" diff-index --quiet "$expected_commit" --; then
        diff_status=0
    else
        diff_status=$?
    fi
    if (( diff_status > 1 )); then
        /bin/rm -f -- "$temporary_index"
        /bin/rmdir "$temporary_directory" 2>/dev/null || true
        return 2
    fi
    untracked="$(
        safe_git_with_index "$temporary_index" ls-files --others --exclude-standard
    )" || {
        /bin/rm -f -- "$temporary_index"
        /bin/rmdir "$temporary_directory" 2>/dev/null || true
        return 2
    }
    /bin/rm -f -- "$temporary_index"
    /bin/rmdir "$temporary_directory" 2>/dev/null || return 2
    (( refresh_status == 0 )) || echo "tracked worktree metadata differs from expected commit"
    (( diff_status == 0 )) || echo "tracked worktree differs from expected commit"
    [[ -z "$untracked" ]] || printf '%s\n' "$untracked"
}

require_clean_checkout_at() {
    local expected_commit="$1" context="$2" status
    [ "$(safe_git rev-parse HEAD)" = "$expected_commit" ] \
        || fail "checkout moved before $context"
    status="$(clean_checkout_status "$expected_commit")" \
        || fail "cannot inspect checkout independently of its index before $context"
    [ -z "$status" ] || fail "checkout is dirty before $context: $status"
    [ "$(safe_git rev-parse HEAD)" = "$expected_commit" ] \
        || fail "checkout moved while verifying $context"
}

require_clean_head() {
    require_clean_checkout_at "$EXPECTED_COMMIT" "exact-commit operation"
}

running_liqmig_units() {
    local rows
    rows="$(systemctl list-units 'liquidity-migration-*' --all --no-legend --no-pager --plain 2>/dev/null)" \
        || fail "cannot inspect liquidity-migration units"
    printf '%s\n' "$rows" \
        | awk 'NF >= 3 && $3 != "inactive" && $3 != "failed" {print $1 " (" $3 ")"}'
}

# STOP_FIRST=auto resolves to "stop" for a pure demo fleet and to the
# refusal for a funded one.
resolve_stop_first() {
    [ "$STOP_FIRST" = auto ] || return 0
    if mainnet_armed; then
        STOP_FIRST=0
    else
        STOP_FIRST=1
    fi
}

require_quiescent() {
    command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
    local running
    running="$(running_liqmig_units)"
    [ -n "$running" ] || return 0
    if [ "${STOP_FIRST:-0}" != 1 ]; then
        printf 'quiesce these units first:\n%s\n' "$running" >&2
        exit 1
    fi
    printf 'stop-first: stopping the running fleet\n%s\n' "$running"
    stop_rollout_units "${ROLLOUT_DOWNSTREAM_UNITS[@]}"
    stop_rollout_units "${ROLLOUT_OWNER_UNITS[@]}"
    running="$(running_liqmig_units)"
    [ -z "$running" ] || {
        printf 'units still running after stop-first:\n%s\n' "$running" >&2
        exit 1
    }
}

git_fetch() {
    if [ -n "$GITHUB_TOKEN" ] && [[ "$REPO_URL" == https://github.com/* ]]; then
        # Keep the credential off argv: `GIT_ENV` starts with `/usr/bin/env -i`,
        # so a `GIT_CONFIG_VALUE_0=...` prefix becomes an argv word of env and is
        # world-readable via /proc/<pid>/cmdline. A 0600 config file written by a
        # shell builtin puts only its path on argv.
        local auth config_file status=0
        auth="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
        config_file="$(mktemp)" || fail "cannot create the authenticated git config"
        chmod 0600 "$config_file"
        printf '[http "https://github.com/"]\n\textraheader = AUTHORIZATION: Basic %s\n' \
            "$auth" > "$config_file"
        "${GIT_ENV[@]}" \
        GIT_CONFIG_GLOBAL="$config_file" \
        GIT_TERMINAL_PROMPT=0 \
        "${GIT_COMMAND[@]}" "$@" || status=$?
        rm -f "$config_file"
        return "$status"
    else
        "${GIT_ENV[@]}" GIT_TERMINAL_PROMPT=0 "${GIT_COMMAND[@]}" "$@"
    fi
}

# Rollout sets this itself. A staged `install` completing a failed rollout may
# request the same maintenance explicitly; without it, a recovery that failed
# inside rule maintenance can never rebind the candidate/rules.
ROLLOUT_REFRESH_STALE_DEMO_RULES="${ROLLOUT_REFRESH_STALE_DEMO_RULES:-0}"
case "$ROLLOUT_REFRESH_STALE_DEMO_RULES" in 0|1) ;; *)
    echo "ROLLOUT_REFRESH_STALE_DEMO_RULES must be 0 or 1" >&2; exit 2 ;;
esac
ROLLOUT_DEMO_RULES_REFRESHED=0
ROLLOUT_DEMO_RULES_PROJECTED=0


refresh_stale_demo_rules_if_requested() {
    [ "$ROLLOUT_REFRESH_STALE_DEMO_RULES" -eq 1 ] || return 0
    local demo_rules receipt_dir refreshed_rules="" freshness_status
    local candidate_dir refreshed_candidate projected_rules projection_status=0
    local refresh_reason=""
    unset ACCOUNT_DEMO_RULES_FILE \
        BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
        BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY DEMO
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/account-execution.env \
        ACCOUNT_DEMO_RULES_FILE ACCOUNT_SYMBOLS_FILE
    demo_rules="$ACCOUNT_DEMO_RULES_FILE"
    if "$PYTHON" - "$demo_rules" <<'PY'
import sys
from liquidity_migration.ops.candidate_rule_coverage import (
    REGISTERED_MAX_RULE_AGE_SECONDS,
    REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS,
    classify_demo_rule_receipt_freshness,
)

status = classify_demo_rule_receipt_freshness(
    sys.argv[1],
    max_rule_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS,
)
if status == "expired":
    raise SystemExit(3)
# Proactive renewal: any rollout in the receipt's back half re-probes, so
# freshness never depends on an operator timing a dispatch against expiry.
due = classify_demo_rule_receipt_freshness(
    sys.argv[1],
    max_rule_age_seconds=REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS,
)
if due == "expired":
    raise SystemExit(4)
PY
    then
        freshness_status=0
    else
        freshness_status=$?
    fi
    [ "$freshness_status" -eq 0 ] || [ "$freshness_status" -eq 3 ] || [ "$freshness_status" -eq 4 ] \
        || fail "configured demo-rule receipt failed validation for a reason other than age"

    # A candidate-universe schema bump makes the installed artifact unreadable by
    # the code being deployed, which would otherwise fail closed at preflight with
    # the fleet already stopped. Force the freeze+projection path here instead.
    candidate_readable=1
    "$PYTHON" - "$ACCOUNT_SYMBOLS_FILE" <<'PY' || candidate_readable=0
import sys

from liquidity_migration.strategy.account_candidate_universe import load_candidate_universe

load_candidate_universe(sys.argv[1])
PY

    if [ "$freshness_status" -eq 0 ] \
        && [ "$candidate_readable" -eq 1 ]; then
        echo "demo-rule-maintenance-plan path=reuse reason=fresh"
        return 0
    fi
    if [ "$candidate_readable" -eq 0 ]; then
        echo "demo-rule-maintenance-plan path=refreeze reason=candidate-universe-unreadable-by-target-code"
    fi

    candidate_dir=/var/lib/liquidity-migration/candidate-universe-receipts
    receipt_dir=/var/lib/liquidity-migration/demo-rule-receipts
    install -d -o root -g root -m 0700 "$candidate_dir" "$receipt_dir"
    refreshed_candidate="$candidate_dir/candidate-universe-$(date -u +%Y%m%dT%H%M%SZ)-${EXPECTED_COMMIT:0:12}-$$.json"
    "$PYTHON" scripts/maintain/freeze_account_candidate_universe.py \
        --realm demo --output "$refreshed_candidate"
    printf 'candidate-universe-refresh-ok path=%s\n' "$refreshed_candidate"

    if [ "$freshness_status" -eq 0 ]; then
        # A reset changes only local journals. Retained symbols keep their exact
        # still-fresh venue evidence and timestamp; a candidate addition exits 3
        # and falls through to a complete fresh probe.
        projected_rules="$receipt_dir/demo-rules-projected-$(date -u +%Y%m%dT%H%M%SZ)-${EXPECTED_COMMIT:0:12}-$$.json"
        if "$PYTHON" scripts/maintain/project_demo_rules_to_candidate.py \
            --candidate-file "$refreshed_candidate" \
            --prior-rules-file "$demo_rules" \
            --output "$projected_rules"; then
            projection_status=0
        else
            projection_status=$?
        fi
        case "$projection_status" in
            0)
                refreshed_rules="$projected_rules"
                ROLLOUT_DEMO_RULES_PROJECTED=1
                echo "demo-rule-maintenance-plan path=projection reason=fresh-candidate-subset"
                ;;
            3)
                refresh_reason=candidate-addition-or-structural-drift
                echo "demo-rule-maintenance-plan path=probe reason=candidate-addition-or-structural-drift"
                ;;
            *) fail "fresh demo-rule candidate projection failed" ;;
        esac
    elif [ "$freshness_status" -eq 4 ]; then
        refresh_reason=refresh-due-past-half-life
        echo "demo-rule-maintenance-plan path=probe reason=refresh-due-past-half-life"
    else
        refresh_reason=expired
        echo "demo-rule-maintenance-plan path=probe reason=expired"
    fi

    if [ -z "$refreshed_rules" ]; then
        lm_load_private_systemd_environment "$PYTHON" \
            /etc/liquidity-migration/bybit-demo.env \
            BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
            BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY
        [ -z "${BYBIT_REAL_API_KEY:-}" ] && [ -z "${BYBIT_REAL_API_SECRET:-}" ] \
            || fail "demo-rule refresh refuses mainnet credentials"
        case "${REAL_MONEY:-false}" in
            0|false|FALSE|no|NO|off|OFF|'') ;;
            *) fail "demo-rule refresh refuses REAL_MONEY" ;;
        esac
        # This probe places live PostOnly orders up to 200 USDT per symbol across
        # the candidate universe, and the branch above reaches it automatically
        # once the bound receipt passes half its lifetime, so keep it demo-only.
        [ "${DEPLOY_VENUE_REALM:-demo}" = "demo" ] \
            || fail "the order-placing rule probe is demo-only; freeze rules with scripts/maintain/freeze_venue_instrument_rules.py --realm ${DEPLOY_VENUE_REALM}"
        refreshed_rules="$receipt_dir/demo-rules-$(date -u +%Y%m%dT%H%M%SZ)-${EXPECTED_COMMIT:0:12}-$$.json"
        DEMO=true "$PYTHON" scripts/maintain/probe_bybit_demo_rules.py \
            --symbols-file "$refreshed_candidate" \
            --prior-rules-file "$demo_rules" \
            --output "$refreshed_rules" \
            --confirm-demo-probe \
            || fail "demo-rule probe failed"
        ROLLOUT_DEMO_RULES_REFRESHED=1
    fi

    "$PYTHON" - /etc/liquidity-migration/account-execution.env \
        "$refreshed_rules" "$refreshed_candidate" <<'PY' \
        || fail "demo-rule rebind of the account execution environment failed"
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.policy.account_execution_config import load_demo_rules
from liquidity_migration.strategy.account_candidate_universe import load_candidate_universe
from liquidity_migration.ops.candidate_rule_coverage import build_candidate_rule_coverage
from liquidity_migration.ops.candidate_rule_coverage import REGISTERED_MAX_RULE_AGE_SECONDS
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment

path = Path(sys.argv[1])
rules = Path(sys.argv[2]).resolve(strict=True)
candidate = Path(sys.argv[3]).resolve(strict=True)
load_demo_rules(rules, max_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS)
values = load_private_systemd_environment(path)
values["ACCOUNT_DEMO_RULES_FILE"] = str(rules)
load_candidate_universe(candidate)
build_candidate_rule_coverage(candidate, rules)
values["ACCOUNT_SYMBOLS_FILE"] = str(candidate)
values["CANDIDATE_UNIVERSE_FILE"] = str(candidate)
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(value)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY DEMO
    if [ "$ROLLOUT_DEMO_RULES_PROJECTED" -eq 1 ]; then
        printf 'demo-rule-projection-ok path=%s candidate=%s\n' \
            "$refreshed_rules" "$refreshed_candidate"
    else
        printf 'demo-rule-refresh-ok path=%s candidate=%s reason=%s\n' \
            "$refreshed_rules" "$refreshed_candidate" "$refresh_reason"
    fi
}

install_mode() {
    local installed_head
    require_checkout
    # The installed checkout's toggles answer one question here: is a funded
    # sleeve in play? If it is, --stop-first stays off and a running fleet is
    # refused rather than stopped.
    . deploy/lib_sleeves.sh
    lm_load_sleeve_toggles
    resolve_stop_first
    require_quiescent
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    require_clean_checkout_at "$installed_head" "install"

    if safe_git remote get-url "$REMOTE" >/dev/null 2>&1; then
        safe_git remote set-url "$REMOTE" "$REPO_URL"
    else
        safe_git remote add "$REMOTE" "$REPO_URL"
    fi
    run_phase fetch-exact-commit \
        git_fetch fetch "$REMOTE" "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
    safe_git cat-file -e "$EXPECTED_COMMIT^{commit}" 2>/dev/null \
        || fail "expected commit is unavailable"
    safe_git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH" \
        || fail "expected commit is not on $REMOTE/$BRANCH"
    safe_git checkout -B "$BRANCH" "$EXPECTED_COMMIT"
    require_clean_head
    unset GITHUB_TOKEN

    [ -x .venv/bin/python ] || python3 -m venv .venv
    PYTHON=.venv/bin/python
    run_phase install-locked-dependencies \
        "$PYTHON" -m pip install --disable-pip-version-check --no-deps \
        --only-binary=:all: -r requirements.lock
    # No lint/type/test phase here: CI runs scripts/dev.sh lint+types+test on
    # every push to main, and the ancestor check above proves this commit is on
    # main. Re-running them with the fleet stopped only lengthens the outage.

    # Bound journald so logs cannot crowd the data roots, and keep at most the
    # newest timestamped backup of the demo credential file.
    install -d -m 0755 /etc/systemd/journald.conf.d
    printf '[Journal]\nSystemMaxUse=500M\n' \
        > /etc/systemd/journald.conf.d/liquidity-migration.conf
    systemctl restart systemd-journald 2>/dev/null || true
    # Keep hot strategy state resident; swap is overflow, not steady state.
    printf 'vm.swappiness = 20\n' > /etc/sysctl.d/90-liquidity-migration.conf
    sysctl -q -p /etc/sysctl.d/90-liquidity-migration.conf || true
    find /etc/liquidity-migration -maxdepth 1 -name 'bybit-demo.env.backup.*' -type f \
        | sort | head -n -1 | while IFS= read -r stale_backup; do
        rm -f -- "$stale_backup"
    done

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    run_phase install-systemd-manifest lm_install_current_systemd_units
    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    require_quiescent
    run_phase refresh-stale-demo-rules refresh_stale_demo_rules_if_requested

    lm_load_sleeve_toggles
    # The writer is atomic (mktemp + mv), so re-reading what this process just
    # wrote proves nothing; load_authorization validates the file a *previous*
    # process wrote, which is the read that can actually disagree.
    lm_write_resolved_sleeve_toggles
    prepare_demo_runtime_config
    require_clean_head
    echo "install-ok commit=$EXPECTED_COMMIT units_started=0"
    echo "next: run activate to start the sleeves this checkout enables"
}

load_authorization() {
    require_checkout
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    # Which profile is installed, read from the marker install wrote. An
    # explicit --profile on this invocation wins. Since the 2026-08-03 paper
    # retirement there is one profile; a demo-operational marker from an older
    # install reads as the same authorization and self-heals on the next rollout.
    AUTH_PROFILE="${DEPLOY_PROFILE:-}"
    if [ -z "$AUTH_PROFILE" ] && [ -r "$PROFILE_MARKER" ]; then
        AUTH_PROFILE="$(cat "$PROFILE_MARKER")"
    fi
    [ -n "$AUTH_PROFILE" ] || AUTH_PROFILE=operational
    case "$AUTH_PROFILE" in demo-operational|operational) ;; *) fail "unsupported profile $AUTH_PROFILE" ;; esac

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    # A resolved file written before the paper retirement is 0640 with the
    # retired runtime group; normalize so the strict private loader accepts it.
    if [ -e /etc/liquidity-migration/sleeves.resolved.env ]; then
        chown root:root /etc/liquidity-migration/sleeves.resolved.env
        chmod 0600 /etc/liquidity-migration/sleeves.resolved.env
    fi
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/sleeves.resolved.env \
        LONG_SLEEVE CARRY_SLEEVE \
        CONTINUOUS_SLEEVE CONTINUOUS_HEDGE_TIMER \
        CONTINUOUS_PAPER_SLEEVE CARRY_PAPER_SLEEVE PAPER_TARGET_MIRROR
    # The retired keys (paper trio, and since 2026-08-03 the continuous pair)
    # are loaded solely for a retirement rollout's pre-install stage: there
    # the host checkout still sources the PREVIOUS commit's lib_sleeves.sh,
    # whose resolved-toggle verifier greps those keys against these
    # variables. Nothing below reads them, and a post-retirement resolved
    # file simply leaves them unset. The mainnet sleeve keys were retired
    # 2026-08-03 (REAL_MONEY is the arming switch); a stale resolved file may
    # still carry them, and the loader simply does not ask for them.
    # A resolved file written by a pre-carry install lacks the carry keys;
    # absent means never deployed, i.e. off. Bridges only the rollout that
    # introduces them — the post-install verifier requires them present.
    if [ -z "${CARRY_SLEEVE:-}" ]; then
        echo "sleeves-resolved-transition carry-keys=absent treated-as=off reason=pre-carry-install"
        CARRY_SLEEVE="${CARRY_SLEEVE:-off}"
    fi
    for value in "$LONG_SLEEVE" "$CARRY_SLEEVE"; do
        case "$value" in on|off) ;; *) fail "invalid resolved sleeve value" ;; esac
    done
    lm_verify_resolved_sleeve_toggles
    lm_verify_no_unknown_liqmig_units
    lm_verify_guarded_unit_surfaces
}

unit_on() {
    systemctl is-active --quiet "$1" && systemctl is-enabled --quiet "$1"
}

unit_off() {
    ! systemctl is-active --quiet "$1" 2>/dev/null \
        && ! systemctl is-enabled --quiet "$1" 2>/dev/null
}

# The single arming switch: REAL_MONEY=true in the mainnet credential file,
# written by the owner's own hand next to the live API key. No file, or any
# other value, means disarmed. The value is read through the strict private
# loader and never printed. Cached: one answer per run.
MAINNET_CREDENTIAL_ENV=/etc/liquidity-migration/bybit-mainnet.env
MAINNET_ARMED_STATE=""
mainnet_armed() {
    if [ -z "$MAINNET_ARMED_STATE" ]; then
        if [ ! -f "$MAINNET_CREDENTIAL_ENV" ]; then
            MAINNET_ARMED_STATE=off
        else
            # A hand-edited file arrives 0644 from nano; the strict loader
            # would refuse it before the later provisioning could normalize
            # it, so ownership and mode are normalized here, at first read.
            chown root:root "$MAINNET_CREDENTIAL_ENV" 2>/dev/null || true
            chmod 600 "$MAINNET_CREDENTIAL_ENV" 2>/dev/null || true
            # Early install stages read the switch before PYTHON or the bash
            # env-loader helpers exist in their context, so this read stands
            # entirely on its own: the checkout's interpreter and the strict
            # parser module, nothing else.
            MAINNET_ARMED_STATE="$(
                "${PYTHON:-${REPO_DIR:-/opt/liquidity-migration}/.venv/bin/python}" -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from liquidity_migration.policy.systemd_environment import parse_systemd_environment_bytes
values = parse_systemd_environment_bytes(Path(sys.argv[1]).read_bytes(), label=sys.argv[1])
armed = str(values.get("REAL_MONEY", "")).strip().lower() in {"1", "true", "yes", "on"}
print("armed" if armed else "off")
' "$MAINNET_CREDENTIAL_ENV" "${REPO_DIR:-/opt/liquidity-migration}"
            )" || fail "cannot read the arming switch from $MAINNET_CREDENTIAL_ENV"
        fi
    fi
    [ "$MAINNET_ARMED_STATE" = armed ]
}

# verify_topology collects every mismatch instead of dying on the first one, so
# one run tells the operator the whole story.
VERIFY_UNIT_ROWS=()
VERIFY_MISMATCHES=()

verify_note() {
    VERIFY_MISMATCHES+=("$1")
}

# `verify` is the read-only status command: a live venue probe that cannot
# answer is reported there and gates nothing. Every other mode still fails.
verify_report_only() {
    [ "$MODE" = verify ]
}

verify_probe() {
    local label="$1" message="$2"
    shift 2
    if "$@"; then
        return 0
    fi
    if verify_report_only; then
        printf 'verify-warn %s: %s\n' "$label" "$message" >&2
        return 0
    fi
    verify_note "$message"
}

verify_unit() {
    local expectation="$1" unit="$2" message="$3" active enabled
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    VERIFY_UNIT_ROWS+=("$unit|$expectation|${active:-unknown}|${enabled:-unknown}")
    case "$expectation" in
        on) if unit_on "$unit"; then return 0; fi ;;
        off) if unit_off "$unit"; then return 0; fi ;;
        *) fail "invalid verify expectation: $expectation" ;;
    esac
    verify_note "$message"
}

check_demo_order_permissions() {
    local context="$1" status=0
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY DEMO
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/bybit-demo.env \
        BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    "$PYTHON" scripts/maintain/check_bybit_order_permissions.py --context "$context" || status=$?
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    return "$status"
}

rollout_flat_check() {
    local head_binding="$1" status=0
    local -a readiness_args
    case "$head_binding" in
        exact|allow_behind|none|stopped-maintenance) ;;
        *) fail "invalid rollout head binding" ;;
    esac
    require_checkout
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    . deploy/lib_systemd_environment.sh
    unset ACCOUNT_EXECUTION_ROOT BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
        BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY DEMO
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/account-execution.env ACCOUNT_EXECUTION_ROOT
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/bybit-demo.env \
        BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    [ -n "$ACCOUNT_EXECUTION_ROOT" ] || fail "demo account root is unavailable"
    readiness_args=(
        --account-root "$ACCOUNT_EXECUTION_ROOT"
        --head-binding "$head_binding"
    )
    ROLLOUT_HEAD_BINDING="$head_binding" DEMO=true \
        ACCOUNT_EXECUTION_ROOT="$ACCOUNT_EXECUTION_ROOT" \
        BYBIT_DEMO_API_KEY="$BYBIT_DEMO_API_KEY" \
        BYBIT_DEMO_API_SECRET="$BYBIT_DEMO_API_SECRET" \
        REAL_MONEY="${REAL_MONEY:-false}" \
        rollout_readiness_helper \
        "${readiness_args[@]}" || status=$?
    unset ACCOUNT_EXECUTION_ROOT BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY DEMO
    return "$status"
}

verify_topology() {
    VERIFY_UNIT_ROWS=()
    VERIFY_MISMATCHES=()

    verify_unit on liquidity-migration-account-execution.service "demo owner is not active and enabled"

    if sleeve_on "$LONG_SLEEVE"; then
        verify_unit on liquidity-migration-bybit-long-demo.service "LONG demo producer is not active"
    else
        verify_unit off liquidity-migration-bybit-long-demo.service "LONG demo producer is not off"
    fi
    if sleeve_on "$CARRY_SLEEVE"; then
        verify_unit on liquidity-migration-bybit-carry-demo.service "carry demo producer is not active"
    else
        verify_unit off liquidity-migration-bybit-carry-demo.service "carry demo producer is not off"
    fi
    # Disarmed, a running mainnet unit must not hide behind a green demo
    # verification; armed, the funded fleet is verified exactly like the
    # others.
    if mainnet_armed; then
        verify_unit on liquidity-migration-account-execution-mainnet.service \
            "mainnet owner is not active and enabled"
        verify_unit on liquidity-migration-bybit-carry-mainnet.service "carry mainnet producer is not active"
        verify_unit on liquidity-migration-bybit-long-mainnet.service "LONG mainnet producer is not active"
        verify_unit on liquidity-migration-mainnet-liveness.timer "mainnet liveness timer is not active"
        # An enabled timer says nothing about the run it fires. A watchdog that
        # fails every fire is the silence it exists to break.
        if systemctl is-failed --quiet liquidity-migration-mainnet-liveness.service; then
            verify_note "liquidity-migration-mainnet-liveness.service is failed"
        fi
    else
        for mainnet_unit in \
            liquidity-migration-account-execution-mainnet.service \
            liquidity-migration-bybit-carry-mainnet.service \
            liquidity-migration-bybit-long-mainnet.service \
            liquidity-migration-mainnet-liveness.timer; do
            verify_unit off "$mainnet_unit" "$mainnet_unit is active under demo authorization"
        done
    fi
    verify_unit on liquidity-migration-demo-liveness.timer "liveness timer is not active"
    # First-rollout tolerance: the pre-install verification runs against the
    # outgoing topology, where a unit introduced by this commit does not exist
    # yet. Once installed the manifest check requires the fragment, so this
    # cannot skip a deleted unit.
    if systemctl cat liquidity-migration-telegram-controls.service >/dev/null 2>&1; then
        verify_unit on liquidity-migration-telegram-controls.service "telegram controls daemon is not active"
    fi
    for oneshot in \
        liquidity-migration-demo-liveness.service; do
        if systemctl is-failed --quiet "$oneshot"; then
            verify_note "$oneshot is failed"
        fi
    done
    verify_probe demo-order-permissions "demo order permission verification failed" \
        check_demo_order_permissions verify

    # Report the commit the host is actually on, not the one the caller asked
    # about. Echoing EXPECTED_COMMIT made a stale host indistinguishable from a
    # current one in the only line an operator reads.
    local installed_head row
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    if [ "$installed_head" != "$EXPECTED_COMMIT" ]; then
        if [ "${EXPECTED_COMMIT_EXPLICIT:-1}" -eq 0 ] && verify_report_only; then
            printf 'verify-drift installed=%s expected=%s reason=expected-commit-defaulted\n' \
                "$installed_head" "$EXPECTED_COMMIT"
        else
            verify_note "installed checkout is $installed_head, not the requested $EXPECTED_COMMIT"
        fi
    fi

    printf 'verify-units unit|expected|active|enabled\n'
    for row in ${VERIFY_UNIT_ROWS[@]+"${VERIFY_UNIT_ROWS[@]}"}; do
        printf '  %s\n' "$row"
    done
    if [ "${#VERIFY_MISMATCHES[@]}" -ne 0 ]; then
        printf 'verify-mismatch %s\n' "${VERIFY_MISMATCHES[@]}" >&2
        fail "topology verification found ${#VERIFY_MISMATCHES[@]} mismatch(es) on $installed_head"
    fi
    local mainnet_state=off
    if mainnet_armed; then mainnet_state=armed; fi
    printf 'verify-ok commit=%s requested=%s profile=%s mainnet=%s\n' \
        "$installed_head" "$EXPECTED_COMMIT" "$AUTH_PROFILE" "$mainnet_state"
}

start_if() {
    local state="$1" unit="$2"
    if sleeve_on "$state"; then
        systemctl enable "$unit"
        systemctl start "$unit"
    else
        systemctl disable --now "$unit" 2>/dev/null || true
    fi
}

activate_mode() {
    load_authorization
    resolve_stop_first
    require_quiescent
    check_demo_order_permissions deploy \
        || fail "demo order permission deploy check failed"

    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    systemctl enable liquidity-migration-account-execution.service
    systemctl start liquidity-migration-account-execution.service

    start_if "$LONG_SLEEVE" liquidity-migration-bybit-long-demo.service
    start_if "$CARRY_SLEEVE" liquidity-migration-bybit-carry-demo.service

    systemctl enable --now liquidity-migration-demo-liveness.timer
    systemctl enable --now liquidity-migration-telegram-controls.service
    if mainnet_armed; then
        start_mainnet_fleet
    fi
    verify_topology
}

MAINNET_OWNER_UNIT=liquidity-migration-account-execution-mainnet.service
MAINNET_LIVENESS_TIMER=liquidity-migration-mainnet-liveness.timer
MAINNET_LIVENESS_SERVICE=liquidity-migration-mainnet-liveness.service

MAINNET_ROUTE_ENV=/etc/liquidity-migration/account-execution-mainnet.env
MAINNET_DEMO_TELEGRAM_ENV=/etc/liquidity-migration/bybit-demo.env

# Run one command with the live credentials in its environment and nowhere
# else. The subshell keeps them out of this script's scope and its logs.
lm_run_with_mainnet_credentials() {
    (
        # The credential resolver refuses mainnet credentials unless the
        # owner's armed switch travels with them, so the switch is loaded
        # from the same owner-written file and goes only to this subprocess.
        unset BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY
        lm_load_private_systemd_environment "$PYTHON" "$MAINNET_CREDENTIAL_ENV" \
            BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY || exit 3
        export BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY
        "$@"
    )
}

# The owner writes one file: the credential env (key, secret, REAL_MONEY,
# optional dials). Everything the old nine-step runbook demanded by hand is
# derived here at activation, and preflight still gates below.
provision_mainnet_prerequisites() {
    if [ ! -f "$MAINNET_ROUTE_ENV" ]; then
        # Fully static committed template, no secrets. Installed only when
        # absent so a hand-edited copy is never overwritten.
        install -o root -g root -m 600 \
            "$REPO_DIR/deploy/account-execution-mainnet.env.template" "$MAINNET_ROUTE_ENV" \
            || fail "cannot install the mainnet route env from the template"
        echo "provision: installed $MAINNET_ROUTE_ENV from the committed template"
    fi
    # The strict loader's ownership and mode demands are mechanical: normalize
    # them instead of failing an activation over a chmod.
    chown root:root "$MAINNET_CREDENTIAL_ENV" "$MAINNET_ROUTE_ENV" 2>/dev/null || true
    chmod 600 "$MAINNET_CREDENTIAL_ENV" "$MAINNET_ROUTE_ENV" 2>/dev/null || true
    # A funded book that cannot page is a hazard: default a missing Telegram
    # pair from the demo file (existing values are never touched).
    "$PYTHON" -m liquidity_migration.policy.real_money_arming default-telegram \
        --from-env "$MAINNET_DEMO_TELEGRAM_ENV" --execute \
        || fail "cannot default the mainnet Telegram pair"
    # Artifact paths come from the route file itself, not from copies here.
    local risk_policy_file="" universe_file="" rules_file=""
    lm_load_private_systemd_environment "$PYTHON" "$MAINNET_ROUTE_ENV" \
        ACCOUNT_RISK_POLICY_FILE ACCOUNT_SYMBOLS_FILE ACCOUNT_DEMO_RULES_FILE \
        || fail "cannot read artifact paths from $MAINNET_ROUTE_ENV"
    risk_policy_file="$ACCOUNT_RISK_POLICY_FILE"
    universe_file="$ACCOUNT_SYMBOLS_FILE"
    rules_file="$ACCOUNT_DEMO_RULES_FILE"
    mkdir -p "$(dirname "$risk_policy_file")"
    chmod 700 "$(dirname "$risk_policy_file")" 2>/dev/null || true
    # The installed profile is always the render of the current dials, so a
    # dial edit can never drift from what the kernel enforces.
    "$PYTHON" -m liquidity_migration.policy.real_money_arming render-profile \
        --execute --overwrite --output "$risk_policy_file" \
        || fail "mainnet dials do not render a loadable profile"
    # Frozen inputs only when absent: universe first, then rules bound to it.
    if [ ! -f "$universe_file" ]; then
        "$PYTHON" scripts/maintain/freeze_account_candidate_universe.py \
            --realm mainnet --output "$universe_file" \
            || fail "mainnet candidate-universe freeze failed"
    fi
    if [ ! -f "$rules_file" ]; then
        lm_run_with_mainnet_credentials "$PYTHON" scripts/maintain/freeze_venue_instrument_rules.py \
            --realm mainnet --symbols-file "$universe_file" --output "$rules_file" \
            || fail "mainnet venue-rules freeze failed"
    fi
}

ensure_mainnet_state_roots() {
    "$PYTHON" -m liquidity_migration.policy.real_money_arming create-state-roots --execute \
        || fail "mainnet state root creation failed"
}

# The single gate between a code change and a funded account: every remaining
# precondition is reported, and any one of them outstanding stops the deploy.
require_mainnet_preflight() {
    local report status=0
    report="$("$PYTHON" -m liquidity_migration.policy.real_money_arming preflight 2>&1)" || status=$?
    printf '%s\n' "$report"
    [ "$status" -eq 0 ] || fail "mainnet preflight has outstanding steps (status $status)"
}

# Which sleeves trade, and at what share, is the installed risk profile's
# decision; the preflight proves the profile is the render of the dials
# before anything starts.
start_mainnet_fleet() {
    provision_mainnet_prerequisites
    ensure_mainnet_state_roots
    require_mainnet_preflight
    systemctl enable "$MAINNET_OWNER_UNIT"
    systemctl start "$MAINNET_OWNER_UNIT"
    systemctl enable --now liquidity-migration-bybit-carry-mainnet.service
    systemctl enable --now liquidity-migration-bybit-long-mainnet.service
    systemctl enable --now "$MAINNET_LIVENESS_TIMER"
}

stop_mainnet_mode() {
    require_checkout
    # Without this, every systemctl below fails silently and the mode still
    # reports stop-mainnet-ok having stopped nothing.
    command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
    local unit
    local -a units=(
        "$MAINNET_LIVENESS_TIMER"
        "$MAINNET_LIVENESS_SERVICE"
        liquidity-migration-bybit-carry-mainnet.service
        liquidity-migration-bybit-long-mainnet.service
        "$MAINNET_OWNER_UNIT"
    )
    for unit in "${units[@]}"; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl disable --now "$unit" 2>/dev/null || true
            printf 'stopped unit=%s\n' "$unit"
        else
            printf 'stop-skipped unit=%s reason=not-installed\n' "$unit"
        fi
    done
    for unit in "${units[@]}"; do
        ! systemctl is-active --quiet "$unit" \
            || fail "unit remained active after mainnet stop: $unit"
        systemctl reset-failed "$unit" 2>/dev/null || true
    done
    echo "stop-mainnet-ok"
    echo "note: this stopped publication only; exposure is unchanged. Flatten through the account owner."
    if mainnet_armed; then
        echo "note: REAL_MONEY is still armed, so verify now fails and the next activate or rollout restarts this fleet. Set REAL_MONEY=false in /etc/liquidity-migration/bybit-mainnet.env to make the stop stick."
    fi
}

ROLLOUT_DOWNSTREAM_UNITS=(
    liquidity-migration-demo-liveness.timer
    liquidity-migration-mainnet-liveness.timer
    liquidity-migration-bybit-long-demo.service
    liquidity-migration-bybit-long-mainnet.service
    liquidity-migration-bybit-carry-demo.service
    liquidity-migration-bybit-carry-mainnet.service
    liquidity-migration-demo-liveness.service
    liquidity-migration-mainnet-liveness.service
    liquidity-migration-telegram-controls.service
    # Retired fleets stay in the stop list so the rollout that carries each
    # retirement quiesces a host still running them; the manifest install
    # then removes the unit files for good. Paper retired 2026-08-03; the
    # continuous units left the deploy set 2026-08-03 (sleeve retired
    # 2026-07-29).
    liquidity-migration-bybit-long-paper.service
    liquidity-migration-bybit-continuous-paper.service
    liquidity-migration-bybit-carry-paper.service
    liquidity-migration-paper-target-mirror.service
    liquidity-migration-continuous-hedge.timer
    liquidity-migration-continuous-rmom-refresh.timer
    liquidity-migration-bybit-continuous-demo.service
    liquidity-migration-continuous-hedge.service
    liquidity-migration-continuous-rmom-refresh.service
)
# Owners stop last and start first: every mainnet producer declares
# Requires=/After= on the mainnet owner. The retired paper owner stays here
# for the same transition reason as the retired producers above.
ROLLOUT_OWNER_UNITS=(
    liquidity-migration-account-execution.service
    liquidity-migration-account-paper-execution.service
    liquidity-migration-account-execution-mainnet.service
)
ROLLOUT_STOPPED=0
ROLLOUT_IRREVERSIBLE=0
ROLLOUT_COMPLETE=0
ROLLOUT_CURRENT_COMMIT=""
ROLLOUT_TARGET_COMMIT=""

stop_rollout_units() {
    local unit
    for unit in "$@"; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl stop "$unit"
            printf 'stopped unit=%s\n' "$unit"
        else
            # A unit introduced by the commit being deployed is not installed
            # yet and cannot be running; the manifest install adds it later.
            printf 'stop-skipped unit=%s reason=not-installed\n' "$unit"
        fi
    done
    for unit in "$@"; do
        ! systemctl is-active --quiet "$unit" \
            || fail "unit remained active after rollout stop: $unit"
        # A stop that escalated past TimeoutStopSec leaves the dead unit flagged
        # `failed` (Result=timeout), which the later quiescence gate rejects — it
        # requires exactly `inactive`. The unit is verifiably stopped here.
        systemctl reset-failed "$unit" 2>/dev/null || true
    done
}

stop_all_rollout_units_best_effort() {
    local unit failed=0
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        # A unit introduced by the commit being deployed is not installed yet;
        # counting its stop as a failure would demote a recoverable pre-install
        # abort into a forced full-fleet stop.
        systemctl cat "$unit" >/dev/null 2>&1 || continue
        if ! systemctl stop "$unit"; then
            cleanup_notice "failed-to-stop unit=$unit"
            failed=1
        fi
    done
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        if systemctl is-active --quiet "$unit"; then
            cleanup_notice "still-active unit=$unit"
            failed=1
        else
            # A verifiably stopped unit must not carry a stale `failed` flag
            # into staged recovery.
            systemctl reset-failed "$unit" 2>/dev/null || true
        fi
    done
    return "$failed"
}

# If the transport dies mid-rollout, sshd HUPs the remote process group and
# every write to the dead pipe raises SIGPIPE. Mirror each line into the host
# journal, and never let a failed write abort the stop sequence.
cleanup_notice() {
    logger -t liquidity-migration-deploy -p daemon.err -- "$*" 2>/dev/null || true
    printf '%s\n' "$*" >&2 2>/dev/null || true
}

rollout_cleanup() {
    local status="$?"
    trap - EXIT INT TERM HUP
    # A second signal, or a write to a dead stdout, must not interrupt the
    # fail-closed handoff.
    trap '' INT TERM HUP PIPE
    set +e
    if [ "$status" -ne 0 ] && [ "$ROLLOUT_STOPPED" -eq 1 ] \
        && [ "$ROLLOUT_COMPLETE" -eq 0 ]; then
        if [ "$ROLLOUT_IRREVERSIBLE" -eq 0 ]; then
            cleanup_notice \
                'rollout failed before install; restoring the verified prior topology'
            if stop_all_rollout_units_best_effort \
                && (
                    EXPECTED_COMMIT="$ROLLOUT_CURRENT_COMMIT"
                    activate_mode
                ); then
                cleanup_notice "rollout-restore-ok commit=$ROLLOUT_CURRENT_COMMIT"
            else
                cleanup_notice \
                    'CRITICAL: prior topology restore failed; forcing the managed fleet stopped'
                stop_all_rollout_units_best_effort || true
            fi
        else
            cleanup_notice \
                'rollout cannot safely restore prior authority; forcing the managed fleet stopped for explicit recovery'
            stop_all_rollout_units_best_effort || true
        fi
    fi
    exit "$status"
}

prefetch_rollout_target() {
    require_checkout
    local installed_head
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    require_clean_checkout_at "$installed_head" "rollout prefetch"
    if safe_git remote get-url "$REMOTE" >/dev/null 2>&1; then
        safe_git remote set-url "$REMOTE" "$REPO_URL"
    else
        safe_git remote add "$REMOTE" "$REPO_URL"
    fi
    git_fetch fetch "$REMOTE" "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
    safe_git cat-file -e "$EXPECTED_COMMIT^{commit}" 2>/dev/null \
        || fail "expected commit is unavailable"
    safe_git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH" \
        || fail "expected commit is not on $REMOTE/$BRANCH"
    require_clean_checkout_at "$installed_head" "rollout prefetch completion"
}

retry_exact_rollout_flat_check() {
    local attempt
    for attempt in 1 2 3; do
        if rollout_flat_check exact; then
            return 0
        fi
        [ "$attempt" -eq 3 ] || sleep 2
    done
    return 1
}

record_installed_profile() {
    printf '%s\n' "$DEPLOY_PROFILE" > "$PROFILE_MARKER"
    chmod 0644 "$PROFILE_MARKER"
}

# install + activate in one remote session, without the rollout's flat-account
# proofs and rollback machinery. The profile marker is written here too: a
# staged install that skipped it left load_authorization falling back to
# "operational" whatever the operator asked for.
staged_mode() {
    run_strict_phase staged-install install_mode
    run_strict_phase record-installed-profile record_installed_profile
    run_strict_phase staged-activate-and-verify activate_mode
    printf 'staged-ok commit=%s profile=%s\n' "$EXPECTED_COMMIT" "$DEPLOY_PROFILE"
}

# A funded fleet keeps the hard gate; a demo fleet gets the same check
# reported and continues, so rollout (and its rollback) is usable there.
rollout_flat_required() {
    if [ "${REQUIRE_FLAT:-0}" -eq 1 ]; then
        return 0
    fi
    mainnet_armed
}

rollout_flat_phase() {
    local label="$1" status=0
    shift
    if rollout_flat_required; then
        run_strict_phase "$label" "$@"
        return 0
    fi
    printf 'phase-start name=%s utc=%s\n' "$label" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$@" || status=$?
    if [ "$status" -eq 0 ]; then
        printf 'phase-ok name=%s\n' "$label"
        return 0
    fi
    printf 'rollout-flat-warn phase=%s status=%s: residual demo exposure or open orders; continuing (--require-flat gates instead)\n' \
        "$label" "$status" >&2
}

rollout_mode() {
    require_checkout
    ROLLOUT_TARGET_COMMIT="$EXPECTED_COMMIT"
    ROLLOUT_CURRENT_COMMIT="$(safe_git rev-parse HEAD)" \
        || fail "cannot read installed checkout HEAD"

    run_strict_phase rollout-target-prefetch prefetch_rollout_target

    # Prove the current receipt/topology before changing any unit, using the
    # commit it actually authorizes rather than the incoming target commit.
    EXPECTED_COMMIT="$ROLLOUT_CURRENT_COMMIT"
    load_authorization
    run_strict_phase current-topology-verification verify_topology
    rollout_flat_phase pre-stop-flat-account-proof rollout_flat_check allow_behind
    EXPECTED_COMMIT="$ROLLOUT_TARGET_COMMIT"

    ROLLOUT_STOPPED=1
    trap rollout_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # bash skips the EXIT trap on an untrapped fatal signal, so an SSH client
    # death (HUP, then SIGPIPE) would leave the fleet half-stopped uncleaned.
    trap 'exit 129' HUP
    trap 'exit 141' PIPE

    # Stop every producer/timer before either owner. A bounded exact-head
    # recheck closes the target/journal race while the owner is still alive;
    # only then are owners stopped and venue truth sampled once more.
    run_strict_phase stop-downstream-units \
        stop_rollout_units "${ROLLOUT_DOWNSTREAM_UNITS[@]}"
    rollout_flat_phase post-producer-flat-account-proof retry_exact_rollout_flat_check
    run_strict_phase stop-account-owners \
        stop_rollout_units "${ROLLOUT_OWNER_UNITS[@]}"
    rollout_flat_phase final-stopped-flat-account-proof rollout_flat_check none
    require_quiescent

    # From checkout mutation onward there is no rollback authority, so any
    # failure leaves every managed unit stopped rather than guessing.
    ROLLOUT_IRREVERSIBLE=1
    ROLLOUT_REFRESH_STALE_DEMO_RULES=1
    run_strict_phase stopped-install install_mode
    if [ "$ROLLOUT_DEMO_RULES_REFRESHED" -eq 1 ]; then
        rollout_flat_phase post-rule-refresh-flat-account-proof \
            rollout_flat_check stopped-maintenance
    fi
    run_strict_phase record-installed-profile record_installed_profile
    run_strict_phase activate-and-verify activate_mode
    ROLLOUT_COMPLETE=1
    ROLLOUT_STOPPED=0
    printf 'rollout-ok commit=%s profile=%s\n' "$EXPECTED_COMMIT" "$DEPLOY_PROFILE"
}

acquire_maintenance_locks
case "$MODE" in
    install) install_mode ;;
    activate) activate_mode ;;
    staged) staged_mode ;;
    stop-mainnet) stop_mainnet_mode ;;
    verify) load_authorization; verify_topology ;;
    rollout) rollout_mode ;;
    # Without this an unknown mode silently succeeded having done nothing.
    *) fail "unknown deploy mode: $MODE" ;;
esac
REMOTE_SCRIPT
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
