#!/usr/bin/env bash
# Staged VPS lifecycle plus a guarded, flat-account one-command rollout.
set -euo pipefail

MODE="${1:-${DEPLOY_MODE:-verify}}"
if [ "$#" -gt 0 ]; then shift; fi
DEPLOY_PROFILE=""
DEPLOY_AUTHORIZATION_REFERENCE=""
DEPLOY_OWNER_ACKNOWLEDGEMENT=""
if [ "$MODE" = rollout ]; then
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --profile)
                [ "$#" -ge 2 ] || { echo "--profile requires a value" >&2; exit 2; }
                DEPLOY_PROFILE="$2"
                shift 2
                ;;
            --authorization-reference)
                [ "$#" -ge 2 ] || { echo "--authorization-reference requires a value" >&2; exit 2; }
                DEPLOY_AUTHORIZATION_REFERENCE="$2"
                shift 2
                ;;
            --owner-acknowledgement)
                [ "$#" -ge 2 ] || { echo "--owner-acknowledgement requires a value" >&2; exit 2; }
                DEPLOY_OWNER_ACKNOWLEDGEMENT="$2"
                shift 2
                ;;
            *) echo "unknown rollout argument: $1" >&2; exit 2 ;;
        esac
    done
    case "$DEPLOY_PROFILE" in
        demo-operational|operational) ;;
        *) echo "rollout requires --profile demo-operational|operational" >&2; exit 2 ;;
    esac
    [ -n "$DEPLOY_AUTHORIZATION_REFERENCE" ] \
        && [ "${#DEPLOY_AUTHORIZATION_REFERENCE}" -le 500 ] \
        || { echo "rollout requires a 1..500 character --authorization-reference" >&2; exit 2; }
    [ "$DEPLOY_OWNER_ACKNOWLEDGEMENT" = \
        AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION ] \
        || { echo "rollout requires the exact demo/paper-only owner acknowledgement" >&2; exit 2; }
elif [ "$#" -ne 0 ]; then
    echo "usage: deploy_vps_live.sh {install|activate|verify|rollout}" >&2
    exit 2
fi
case "$MODE" in
    install|activate|verify|rollout) ;;
    *) echo "invalid deploy mode: $MODE" >&2; exit 2 ;;
esac

SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3}"
REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
RMOM_BOOTSTRAP_TIMEOUT_SECONDS="${RMOM_BOOTSTRAP_TIMEOUT_SECONDS:-300}"
RMOM_BOOTSTRAP_RETRY_SECONDS="${RMOM_BOOTSTRAP_RETRY_SECONDS:-10}"

if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "EXPECTED_COMMIT must be a full lowercase 40-character commit" >&2
    exit 2
fi
if ! /usr/bin/git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
    echo "BRANCH is not a valid Git branch" >&2
    exit 2
fi
for value in "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS" "$RMOM_BOOTSTRAP_RETRY_SECONDS"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "RMOM durations must be positive integers" >&2; exit 2; }
done

if [[ "$MODE" = install || "$MODE" = rollout ]] && [ -z "$GITHUB_TOKEN" ] \
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
if [ "$("${LOCAL_GIT[@]}" cat-file -t "$EXPECTED_COMMIT" 2>/dev/null || true)" != commit ]; then
    echo "EXPECTED_COMMIT is not a local commit object: $EXPECTED_COMMIT" >&2
    exit 1
fi
if ! MAINTENANCE_LOCK_HELPER_B64="$(
    "${LOCAL_GIT[@]}" show \
        "$EXPECTED_COMMIT:liquidity_migration/maintenance_lock.py" \
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
ROLLOUT_SHUTDOWN_AUTHORITY_HELPER_B64=""
if [ "$MODE" = rollout ]; then
    if ! ROLLOUT_READINESS_HELPER_B64="$(
        "${LOCAL_GIT[@]}" show \
            "$EXPECTED_COMMIT:scripts/check_deploy_rollout_readiness.py" \
        | /usr/bin/python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode("ascii"))'
    )"; then
        echo "expected commit does not contain the rollout readiness helper: $EXPECTED_COMMIT" >&2
        exit 1
    fi
    [[ -n "$ROLLOUT_READINESS_HELPER_B64" ]] || {
        echo "expected commit returned an empty rollout readiness helper" >&2
        exit 1
    }
    if ! ROLLOUT_SHUTDOWN_AUTHORITY_HELPER_B64="$(
        "${LOCAL_GIT[@]}" show \
            "$EXPECTED_COMMIT:scripts/verify_rollout_shutdown_authority.py" \
        | /usr/bin/python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode("ascii"))'
    )"; then
        echo "expected commit does not contain the rollout shutdown authority helper: $EXPECTED_COMMIT" >&2
        exit 1
    fi
    [[ -n "$ROLLOUT_SHUTDOWN_AUTHORITY_HELPER_B64" ]] || {
        echo "expected commit returned an empty rollout shutdown authority helper" >&2
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
    printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
    printf 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS=%q\n' "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS"
    printf 'RMOM_BOOTSTRAP_RETRY_SECONDS=%q\n' "$RMOM_BOOTSTRAP_RETRY_SECONDS"
    printf 'DEPLOY_PROFILE=%q\n' "$DEPLOY_PROFILE"
    printf 'DEPLOY_AUTHORIZATION_REFERENCE=%q\n' "$DEPLOY_AUTHORIZATION_REFERENCE"
    printf 'DEPLOY_OWNER_ACKNOWLEDGEMENT=%q\n' "$DEPLOY_OWNER_ACKNOWLEDGEMENT"
    printf 'MAINTENANCE_LOCK_HELPER_B64=%q\n' "$MAINTENANCE_LOCK_HELPER_B64"
    printf 'ROLLOUT_READINESS_HELPER_B64=%q\n' "$ROLLOUT_READINESS_HELPER_B64"
    printf 'ROLLOUT_SHUTDOWN_AUTHORITY_HELPER_B64=%q\n' \
        "$ROLLOUT_SHUTDOWN_AUTHORITY_HELPER_B64"
	cat <<'REMOTE_SCRIPT'
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

fail() { echo "deploy failed: $*" >&2; exit 1; }

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
    else
        printf 'phase-failed name=%s elapsed_seconds=%s status=%s\n' \
            "$label" "$((finished - started))" "$status" >&2
    fi
    return "$status"
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
    # maintenance.lock is the canonical cross-operation mutex. The two retired
    # leaves stay nested during migration so this version also excludes an old
    # deploy or reset process that started from the previously installed code.
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
namespace = {"__file__": "scripts/check_deploy_rollout_readiness.py", "__name__": "__main__"}
exec(compile(source, namespace["__file__"], "exec"), namespace)
' "$ROLLOUT_READINESS_HELPER_B64" "$@"
}

rollout_shutdown_authority_helper() {
    "$PYTHON" -c '
import base64
import sys

encoded = sys.argv[1]
arguments = sys.argv[2:]
source = base64.b64decode(encoded, validate=True)
sys.argv = ["verify_rollout_shutdown_authority.py", *arguments]
namespace = {
    "__file__": "scripts/verify_rollout_shutdown_authority.py",
    "__name__": "__main__",
}
exec(compile(source, namespace["__file__"], "exec"), namespace)
' "$ROLLOUT_SHUTDOWN_AUTHORITY_HELPER_B64" "$@"
}

PAPER_RUNTIME_USER=liquidity-migration-paper
PAPER_RUNTIME_GROUP=liquidity-migration-paper
PAPER_ACCOUNT_ROOT=/opt/liquidity-migration/data/bybit-account-paper
PAPER_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-paper-intents
PAPER_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-paper-market-capture
PAPER_CONFIG_DIR=/etc/liquidity-migration/account-paper-execution
PAPER_ENVIRONMENT=/etc/liquidity-migration/account-paper-execution.env
PAPER_SYMBOLS_FILE=$PAPER_CONFIG_DIR/symbols.txt
PAPER_RULES_FILE=$PAPER_CONFIG_DIR/demo-rules.json
PAPER_RISK_FILE=$PAPER_CONFIG_DIR/risk-policy.json
LONG_DEMO_ROOT=/opt/liquidity-migration/data/bybit-long-demo-event
CONTINUOUS_DEMO_ROOT=/opt/liquidity-migration/data/bybit-continuous-demo-event
LONG_PAPER_ROOT=/opt/liquidity-migration/data/bybit-long-paper-event
CONTINUOUS_PAPER_ROOT=/opt/liquidity-migration/data/bybit-continuous-paper-event

ensure_paper_runtime_identity() {
    command -v getent >/dev/null 2>&1 || fail "getent is unavailable"
    command -v runuser >/dev/null 2>&1 || fail "runuser is unavailable"
    getent group "$PAPER_RUNTIME_GROUP" >/dev/null \
        || groupadd --system "$PAPER_RUNTIME_GROUP"
    if ! id -u "$PAPER_RUNTIME_USER" >/dev/null 2>&1; then
        useradd --system --gid "$PAPER_RUNTIME_GROUP" --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin "$PAPER_RUNTIME_USER"
    fi
    [ "$(id -gn "$PAPER_RUNTIME_USER")" = "$PAPER_RUNTIME_GROUP" ] \
        || fail "$PAPER_RUNTIME_USER does not use $PAPER_RUNTIME_GROUP as its primary group"
    paper_shell="$(getent passwd "$PAPER_RUNTIME_USER" | awk -F: '{print $7}')"
    case "$paper_shell" in
        /usr/sbin/nologin|/sbin/nologin|/bin/false) ;;
        *) fail "$PAPER_RUNTIME_USER must be a non-login account" ;;
    esac
}

prepare_paper_runtime_boundary() {
    [ "$REPO_DIR" = /opt/liquidity-migration ] \
        || fail "systemd paper paths require REPO_DIR=/opt/liquidity-migration"
    ensure_paper_runtime_identity
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
        ACCOUNT_DEMO_RULES_FILE ACCOUNT_RISK_POLICY_FILE ACCOUNT_CAPTURE_ROOT
    demo_symbols="$ACCOUNT_SYMBOLS_FILE"
    demo_candidate="${CANDIDATE_UNIVERSE_FILE:-}"
    demo_rules="$ACCOUNT_DEMO_RULES_FILE"
    demo_risk="$ACCOUNT_RISK_POLICY_FILE"
    demo_capture="$ACCOUNT_CAPTURE_ROOT"
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
from liquidity_migration.operational_profile import load_operational_profile

load_operational_profile(sys.argv[1])
PY
    # Install only while every managed unit is quiescent (install_mode enforces
    # that before this boundary). The account owner and all target producers
    # subsequently consume these exact bytes through ACCOUNT_RISK_POLICY_FILE.
    install -o root -g root -m 0600 "$operational_profile_source" "$demo_risk"
    "$PYTHON" - "$demo_risk" <<'PY'
import sys
from liquidity_migration.operational_profile import load_operational_profile

load_operational_profile(sys.argv[1])
PY
    if [ -z "$demo_candidate" ]; then
        "$PYTHON" - /etc/liquidity-migration/account-execution.env "$demo_symbols" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.account_candidate_universe import load_candidate_universe
from liquidity_migration.systemd_environment import load_private_systemd_environment

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

from liquidity_migration.systemd_environment import load_private_systemd_environment

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

    install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 0710 /etc/liquidity-migration
    install -d -o root -g "$PAPER_RUNTIME_GROUP" -m 0750 "$PAPER_CONFIG_DIR"
    install -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0600 \
        "$demo_symbols" "$PAPER_SYMBOLS_FILE"
    install -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0600 \
        "$demo_rules" "$PAPER_RULES_FILE"
    install -o "$PAPER_RUNTIME_USER" -g "$PAPER_RUNTIME_GROUP" -m 0600 \
        "$demo_risk" "$PAPER_RISK_FILE"

    # Rebuild the non-secret paper route as strict data. An existing file may
    # contribute benign tuning values, but credentials and alternate roots are
    # never migrated across this boundary.
    if [ -e "$PAPER_ENVIRONMENT" ] || [ -L "$PAPER_ENVIRONMENT" ]; then
        [ -f "$PAPER_ENVIRONMENT" ] && [ ! -L "$PAPER_ENVIRONMENT" ] \
            || fail "paper environment must be a real regular file"
        chown root:root "$PAPER_ENVIRONMENT"
        chmod 0600 "$PAPER_ENVIRONMENT"
    else
        install -o root -g root -m 0600 /dev/null "$PAPER_ENVIRONMENT"
    fi
    "$PYTHON" - "$PAPER_ENVIRONMENT" \
        "$PAPER_ACCOUNT_ROOT" "$PAPER_INBOX_ROOT" "$PAPER_CAPTURE_ROOT" \
        "$PAPER_SYMBOLS_FILE" "$PAPER_RULES_FILE" "$PAPER_RISK_FILE" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.systemd_environment import (
    load_private_systemd_environment,
)

path = Path(sys.argv[1])
existing = load_private_systemd_environment(path) if path.stat().st_size else {}
for key in (
    "BYBIT_DEMO_API_KEY",
    "BYBIT_DEMO_API_SECRET",
    "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET",
):
    if existing.get(key):
        raise SystemExit(f"paper environment contains forbidden credential {key}")
if existing.get("REAL_MONEY", "").strip().lower() not in {"", "0", "false", "no", "off"}:
    raise SystemExit("paper environment does not explicitly disable REAL_MONEY")
allowed_tuning = {
    "PAPER_EQUITY_USDT",
    "MAX_DEMO_RULE_AGE_HOURS",
    "ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS",
}
values = {key: value for key, value in existing.items() if key in allowed_tuning}
values.update(
    {
        "ACCOUNT_PAPER_KERNEL_REQUIRED": "1",
        "ACCOUNT_RAW_MARKET_PERSISTENCE": "0",
        "ACCOUNT_EXECUTION_ROOT": sys.argv[2],
        "ACCOUNT_INTENT_INBOX_ROOT": sys.argv[3],
        "ACCOUNT_PAPER_CAPTURE_ROOT": sys.argv[4],
        "STRATEGY_TARGET_CAPTURE_PATH": str(Path(sys.argv[4]) / "strategy-targets.jsonl"),
        "ACCOUNT_SYMBOLS_FILE": sys.argv[5],
        "CANDIDATE_UNIVERSE_FILE": sys.argv[5],
        "ACCOUNT_DEMO_RULES_FILE": sys.argv[6],
        "ACCOUNT_RISK_POLICY_FILE": sys.argv[7],
    }
)
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
    chown root:"$PAPER_RUNTIME_GROUP" "$PAPER_ENVIRONMENT"
    chmod 0640 "$PAPER_ENVIRONMENT"
    chown root:"$PAPER_RUNTIME_GROUP" /etc/liquidity-migration/sleeves.resolved.env
    chmod 0640 /etc/liquidity-migration/sleeves.resolved.env

    paper_uid="$(id -u "$PAPER_RUNTIME_USER")"
    paper_gid="$(id -g "$PAPER_RUNTIME_USER")"
    root_uid="$(id -u root)"

    paper_path_args=()
    for root in \
        "$PAPER_ACCOUNT_ROOT" "$PAPER_INBOX_ROOT" "$PAPER_CAPTURE_ROOT" \
        "$LONG_PAPER_ROOT" "$CONTINUOUS_PAPER_ROOT"; do
        paper_path_args+=(--root "$root")
    done
    run_phase paper-tree-preflight \
        "$PYTHON" -m liquidity_migration.reset_path_safety preflight-paper \
        --anchor "$REPO_DIR/data" "${paper_path_args[@]}" \
        || fail "paper runtime descriptor/mount preflight failed"
    run_phase demo-tree-preflight \
        "$PYTHON" -m liquidity_migration.reset_path_safety preflight-demo \
        --anchor "$REPO_DIR/data" \
        --root "$LONG_DEMO_ROOT" --root "$CONTINUOUS_DEMO_ROOT" \
        --continuous-root "$CONTINUOUS_DEMO_ROOT" \
        || fail "demo runtime descriptor/mount preflight failed"

    run_phase paper-tree-normalize \
        "$PYTHON" -m liquidity_migration.reset_path_safety normalize-paper \
        --anchor "$REPO_DIR/data" "${paper_path_args[@]}" \
        --uid "$paper_uid" --gid "$paper_gid" --create-missing \
        || fail "descriptor-rooted paper runtime normalization failed"
    run_phase demo-tree-normalize \
        "$PYTHON" -m liquidity_migration.reset_path_safety normalize-demo \
        --anchor "$REPO_DIR/data" \
        --root "$LONG_DEMO_ROOT" --root "$CONTINUOUS_DEMO_ROOT" \
        --continuous-root "$CONTINUOUS_DEMO_ROOT" \
        --uid "$root_uid" --gid "$paper_gid" --create-missing \
        || fail "descriptor-rooted shared demo cache normalization failed"
    chown root:root \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env
    chmod 0600 \
        /etc/liquidity-migration/account-execution.env \
        /etc/liquidity-migration/bybit-demo.env
}

verify_paper_runtime_boundary() {
    ensure_paper_runtime_identity
    for path in \
        "$PAPER_ENVIRONMENT" /etc/liquidity-migration/sleeves.resolved.env \
        "$PAPER_SYMBOLS_FILE" "$PAPER_RULES_FILE" "$PAPER_RISK_FILE"; do
        runuser -u "$PAPER_RUNTIME_USER" -- test -r "$path" \
            || fail "paper runtime cannot read required non-secret input: $path"
    done
    for path in \
        /etc/liquidity-migration/bybit-demo.env \
        /etc/liquidity-migration/account-execution.env; do
        runuser -u "$PAPER_RUNTIME_USER" -- test ! -r "$path" \
            || fail "paper runtime can read forbidden demo config: $path"
    done
    for root in \
        "$PAPER_ACCOUNT_ROOT" "$PAPER_INBOX_ROOT" "$PAPER_CAPTURE_ROOT" \
        "$LONG_PAPER_ROOT" "$CONTINUOUS_PAPER_ROOT"; do
        runuser -u "$PAPER_RUNTIME_USER" -- test -w "$root" \
            || fail "paper runtime cannot write its explicit state root: $root"
        runuser -u "$PAPER_RUNTIME_USER" -- test -w "$root/.locks" \
            || fail "paper runtime cannot write its persistent lock directory: $root/.locks"
    done
    for root in "$LONG_DEMO_ROOT" "$CONTINUOUS_DEMO_ROOT"; do
        runuser -u "$PAPER_RUNTIME_USER" -- \
            test -x "$root/.cache/ws_klines" \
            || fail "paper runtime cannot traverse demo kline cache: $root"
        snapshot="$root/.cache/ws_klines/store.parquet"
        if [ -e "$snapshot" ]; then
            runuser -u "$PAPER_RUNTIME_USER" -- test -r "$snapshot" \
                || fail "paper runtime cannot read demo kline snapshot: $snapshot"
        fi
        runuser -u "$PAPER_RUNTIME_USER" -- test ! -w "$root" \
            || fail "paper runtime can write demo market root: $root"
    done
    if [ -e "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" ]; then
        runuser -u "$PAPER_RUNTIME_USER" -- \
            test -r "$CONTINUOUS_DEMO_ROOT/residual_momentum.parquet" \
            || fail "paper runtime cannot read the shared RMOM input"
    fi
    runuser -u "$PAPER_RUNTIME_USER" -- test ! -w "$REPO_DIR/liquidity_migration" \
        || fail "paper runtime can write repository code"
    if [ -e /etc/liquidity-migration/account-execution-operational-ready ]; then
        runuser -u "$PAPER_RUNTIME_USER" -- \
            test -r /etc/liquidity-migration/account-execution-operational-ready \
            || fail "paper runtime cannot read the operational receipt"
    fi
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

require_quiescent() {
    command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
    local rows running
    rows="$(systemctl list-units 'liquidity-migration-*' --all --no-legend --no-pager --plain 2>/dev/null)" \
        || fail "cannot inspect liquidity-migration units"
    running="$(printf '%s\n' "$rows" | awk 'NF >= 3 && $3 != "inactive" && $3 != "failed" {print $1 " (" $3 ")"}')"
    [ -z "$running" ] || { printf 'quiesce these units first:\n%s\n' "$running" >&2; exit 1; }
}

git_fetch() {
    if [ -n "$GITHUB_TOKEN" ] && [[ "$REPO_URL" == https://github.com/* ]]; then
        local auth
        auth="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
        "${GIT_ENV[@]}" \
        GIT_CONFIG_COUNT=1 \
        GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
        GIT_CONFIG_VALUE_0="AUTHORIZATION: Basic $auth" \
        GIT_TERMINAL_PROMPT=0 \
        "${GIT_COMMAND[@]}" "$@"
    else
        "${GIT_ENV[@]}" GIT_TERMINAL_PROMPT=0 "${GIT_COMMAND[@]}" "$@"
    fi
}

invalidate_operational_authorization() {
    local path=/etc/liquidity-migration/account-execution-operational-ready archive stamp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    [ -e "$path" ] || [ -L "$path" ] || return 0
    archive="/var/lib/liquidity-migration/retired-authority/$stamp"
    install -d -m 0700 "$archive"
    mv "$path" "$archive/$(basename "$path")"
    echo "invalidated prior operational authorization: $archive"
}

ROLLOUT_REFRESH_STALE_DEMO_RULES=0
ROLLOUT_DEMO_RULES_REFRESHED=0

refresh_stale_demo_rules_if_requested() {
    [ "$ROLLOUT_REFRESH_STALE_DEMO_RULES" -eq 1 ] || return 0
    local demo_symbols demo_rules receipt_dir refreshed_rules freshness_status
    unset ACCOUNT_SYMBOLS_FILE ACCOUNT_DEMO_RULES_FILE \
        BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
        BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY DEMO
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/account-execution.env \
        ACCOUNT_SYMBOLS_FILE ACCOUNT_DEMO_RULES_FILE
    demo_symbols="$ACCOUNT_SYMBOLS_FILE"
    demo_rules="$ACCOUNT_DEMO_RULES_FILE"
    if "$PYTHON" - "$demo_rules" <<'PY'
import sys
from liquidity_migration.account_execution_config import load_demo_rules
from liquidity_migration.candidate_rule_coverage import REGISTERED_MAX_RULE_AGE_SECONDS

try:
    load_demo_rules(
        sys.argv[1],
        max_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS,
    )
except ValueError as exc:
    if str(exc) == "demo rules receipt is stale or future-dated":
        raise SystemExit(3) from exc
    raise
PY
    then
        echo "demo-rule-refresh-skipped reason=fresh"
        return 0
    else
        freshness_status=$?
    fi
    [ "$freshness_status" -eq 3 ] \
        || fail "configured demo-rule receipt failed validation for a reason other than age"

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
    receipt_dir=/var/lib/liquidity-migration/demo-rule-receipts
    install -d -o root -g root -m 0700 "$receipt_dir"
    refreshed_rules="$receipt_dir/demo-rules-$(date -u +%Y%m%dT%H%M%SZ)-${EXPECTED_COMMIT:0:12}-$$.json"
    DEMO=true "$PYTHON" scripts/probe_bybit_demo_rules.py \
        --symbols-file "$demo_symbols" \
        --prior-rules-file "$demo_rules" \
        --output "$refreshed_rules" \
        --confirm-demo-probe

    "$PYTHON" - /etc/liquidity-migration/account-execution.env \
        "$refreshed_rules" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.account_execution_config import load_demo_rules
from liquidity_migration.candidate_rule_coverage import REGISTERED_MAX_RULE_AGE_SECONDS
from liquidity_migration.systemd_environment import load_private_systemd_environment

path = Path(sys.argv[1])
rules = Path(sys.argv[2]).resolve(strict=True)
load_demo_rules(rules, max_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS)
values = load_private_systemd_environment(path)
values["ACCOUNT_DEMO_RULES_FILE"] = str(rules)
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
    ROLLOUT_DEMO_RULES_REFRESHED=1
    printf 'demo-rule-refresh-ok path=%s\n' "$refreshed_rules"
}

install_mode() {
    local installed_head
    require_checkout
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
    run_phase ruff "$PYTHON" -m ruff check liquidity_migration scripts tests
    run_phase mypy "$PYTHON" -m mypy liquidity_migration
    run_phase focused-runtime-tests "$PYTHON" -m pytest -q \
        tests/test_forward_epoch_start.py \
        tests/test_deploy_rollout_readiness.py \
        tests/test_operational_profile.py \
        tests/test_operational_runtime_authority.py \
        tests/test_strategy_planning.py \
        tests/test_runtime_scripts.py

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    ensure_paper_runtime_identity
    run_phase install-systemd-manifest lm_install_current_systemd_units
    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    require_quiescent
    invalidate_operational_authorization
    run_phase refresh-stale-demo-rules refresh_stale_demo_rules_if_requested

    lm_load_sleeve_toggles
    if sleeve_on "$CONTINUOUS_SLEEVE"; then
        CONTINUOUS_HEDGE_TIMER=on
    else
        lm_load_private_systemd_environment "$PYTHON" \
            /etc/liquidity-migration/account-execution.env ACCOUNT_EXECUTION_ROOT
        hedge_open="$(ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT" "$PYTHON" - <<'PY' 2>/dev/null || echo unknown
import os
from pathlib import Path
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.account_strategy_state import canonical_strategy_trade_rows

try:
    rows = canonical_strategy_trade_rows(
        Path(os.environ["ACCOUNT_ROOT"]), sleeve=SleeveAdapterKind.HEDGE.value
    )
    print(int((rows["status"] == "open").sum()) if not rows.is_empty() else 0)
except Exception:
    print("unknown")
PY
)"
        if [ "$hedge_open" = 0 ]; then CONTINUOUS_HEDGE_TIMER=off; else CONTINUOUS_HEDGE_TIMER=on; fi
    fi
    export CONTINUOUS_HEDGE_TIMER
    lm_write_resolved_sleeve_toggles
    prepare_paper_runtime_boundary
    lm_verify_resolved_sleeve_toggles
    run_phase verify-paper-runtime-boundary verify_paper_runtime_boundary
    require_clean_head
    echo "install-ok commit=$EXPECTED_COMMIT units_started=0"
    echo "next: issue a new operational authorization for this stopped exact checkout, then run activate"
}

load_authorization() {
    local verification_mode="${1:-strict}"
    case "$verification_mode" in
        strict|rollout-shutdown) ;;
        *) fail "invalid authorization verification mode" ;;
    esac
    require_checkout
    require_clean_head
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    if [ "$verification_mode" = rollout-shutdown ]; then
        AUTH_JSON="$(rollout_shutdown_authority_helper \
            --receipt /etc/liquidity-migration/account-execution-operational-ready \
            --repo-root "$REPO_DIR")" \
            || fail "rollout shutdown authorization verification failed"
        AUTH_SHUTDOWN_EXPIRED_DEMO_RULES="$(
            printf '%s' "$AUTH_JSON" | "$PYTHON" -c \
                'import json,sys; print(int(json.load(sys.stdin).get("_rollout_shutdown_expired_demo_rules") is True))'
        )"
    else
        AUTH_JSON="$("$PYTHON" -m liquidity_migration.operational_runtime_authority verify \
            --receipt /etc/liquidity-migration/account-execution-operational-ready \
            --repo-root "$REPO_DIR")" \
            || fail "operational authorization verification failed"
        AUTH_SHUTDOWN_EXPIRED_DEMO_RULES=0
    fi
    AUTH_PROFILE="$(printf '%s' "$AUTH_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["profile"])')"
    AUTH_COMMIT="$(printf '%s' "$AUTH_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["authorized_commit"])')"
    [ "$AUTH_COMMIT" = "$EXPECTED_COMMIT" ] || fail "authorization is for another commit"
    case "$AUTH_PROFILE" in demo-operational|operational) ;; *) fail "unsupported profile $AUTH_PROFILE" ;; esac
    if [ "$AUTH_PROFILE" = operational ]; then
        verify_paper_runtime_boundary
    fi

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    lm_load_group_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/sleeves.resolved.env "$PAPER_RUNTIME_GROUP" \
        LONG_SLEEVE CONTINUOUS_SLEEVE CONTINUOUS_PAPER_SLEEVE CONTINUOUS_HEDGE_TIMER
    for value in "$LONG_SLEEVE" "$CONTINUOUS_SLEEVE" "$CONTINUOUS_PAPER_SLEEVE" "$CONTINUOUS_HEDGE_TIMER"; do
        case "$value" in on|off) ;; *) fail "invalid resolved sleeve value" ;; esac
    done
    if [ "$AUTH_PROFILE" = demo-operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
        fail "demo-operational authorization cannot run the paper continuous sleeve"
    fi
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

timer_on() { unit_on "$1"; }
timer_off() { unit_off "$1"; }

expected_downstream_on() {
    local unit="$1"
    if unit_on "$unit"; then
        return 0
    fi
    if [ "${AUTH_SHUTDOWN_EXPIRED_DEMO_RULES:-0}" -eq 1 ] \
        && systemctl is-enabled --quiet "$unit" \
        && ! systemctl is-active --quiet "$unit"; then
        printf 'topology-warning unit=%s state=enabled-not-active cause=expired-authority-recovery\n' \
            "$unit"
        return 0
    fi
    return 1
}

validate_hedge_model_prior() {
    "$PYTHON" scripts/run_continuous_hedge.py \
        --execution-environment demo \
        --validate-model-prior-only
}

check_demo_order_permissions() {
    local context="$1" status=0
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY DEMO
    lm_load_private_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/bybit-demo.env \
        BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    "$PYTHON" scripts/check_bybit_order_permissions.py --context "$context" || status=$?
    unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY
    return "$status"
}

rollout_flat_check() {
    local head_binding="$1" status=0
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
    ROLLOUT_HEAD_BINDING="$head_binding" DEMO=true \
        ACCOUNT_EXECUTION_ROOT="$ACCOUNT_EXECUTION_ROOT" \
        BYBIT_DEMO_API_KEY="$BYBIT_DEMO_API_KEY" \
        BYBIT_DEMO_API_SECRET="$BYBIT_DEMO_API_SECRET" \
        REAL_MONEY="${REAL_MONEY:-false}" \
        rollout_readiness_helper \
        --account-root "$ACCOUNT_EXECUTION_ROOT" \
        --head-binding "$head_binding" || status=$?
    unset ACCOUNT_EXECUTION_ROOT BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY DEMO
    return "$status"
}

verify_topology() {
    unit_on liquidity-migration-account-execution.service || fail "demo owner is not active and enabled"
    if [ "$AUTH_PROFILE" = operational ]; then
        unit_on liquidity-migration-account-paper-execution.service || fail "paper owner is not active and enabled"
    else
        unit_off liquidity-migration-account-paper-execution.service || fail "paper owner is active under demo-only authorization"
    fi

    if sleeve_on "$LONG_SLEEVE"; then
        expected_downstream_on liquidity-migration-bybit-long-demo.service \
            || fail "LONG demo producer is not active"
    else
        unit_off liquidity-migration-bybit-long-demo.service || fail "LONG demo producer is not off"
    fi
    if [ "$AUTH_PROFILE" = operational ] && sleeve_on "$LONG_SLEEVE"; then
        expected_downstream_on liquidity-migration-bybit-long-paper.service \
            || fail "LONG paper producer is not active"
    else
        unit_off liquidity-migration-bybit-long-paper.service || fail "LONG paper producer is not off"
    fi
    if sleeve_on "$CONTINUOUS_SLEEVE"; then
        expected_downstream_on liquidity-migration-bybit-continuous-demo.service \
            || fail "continuous demo producer is not active"
    else
        unit_off liquidity-migration-bybit-continuous-demo.service || fail "continuous demo producer is not off"
    fi
    if [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
        expected_downstream_on liquidity-migration-bybit-continuous-paper.service \
            || fail "continuous paper producer is not active"
    else
        unit_off liquidity-migration-bybit-continuous-paper.service || fail "continuous paper producer is not off"
    fi

    if sleeve_on "$CONTINUOUS_SLEEVE" \
        || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
        expected_downstream_on liquidity-migration-continuous-rmom-refresh.timer \
            || fail "RMOM timer is not active"
    else
        timer_off liquidity-migration-continuous-rmom-refresh.timer || fail "RMOM timer is not off"
    fi
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        validate_hedge_model_prior
        expected_downstream_on liquidity-migration-continuous-hedge.timer \
            || fail "hedge timer is not active"
    else
        timer_off liquidity-migration-continuous-hedge.timer || fail "hedge timer is not off"
    fi
    expected_downstream_on liquidity-migration-demo-liveness.timer \
        || fail "liveness timer is not active"
    for oneshot in \
        liquidity-migration-continuous-rmom-refresh.service \
        liquidity-migration-continuous-hedge.service \
        liquidity-migration-demo-liveness.service; do
        if systemctl is-failed --quiet "$oneshot"; then
            if [ "${AUTH_SHUTDOWN_EXPIRED_DEMO_RULES:-0}" -eq 1 ] \
                && [ "$(systemctl show "$oneshot" --property=Result --value)" = exit-code ] \
                && [ "$(systemctl show "$oneshot" --property=ExecMainCode --value)" = 1 ] \
                && [ "$(systemctl show "$oneshot" --property=ExecMainStatus --value)" = 2 ]; then
                printf 'topology-warning unit=%s state=failed cause=expired-authority-pre-exec\n' \
                    "$oneshot"
            else
                fail "$oneshot is failed"
            fi
        fi
    done
    check_demo_order_permissions verify
    echo "verify-ok commit=$EXPECTED_COMMIT profile=$AUTH_PROFILE"
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

seed_rmom() {
    local deadline ok
    deadline=$(( $(date +%s) + RMOM_BOOTSTRAP_TIMEOUT_SECONDS ))
    while true; do
        ok=1
        systemctl reset-failed liquidity-migration-continuous-rmom-refresh.service 2>/dev/null || true
        systemctl start liquidity-migration-continuous-rmom-refresh.service || ok=0
        if sleeve_on "$CONTINUOUS_SLEEVE" \
            || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
            "$PYTHON" scripts/check_residual_momentum_gate.py \
                --path data/bybit-continuous-demo-event/residual_momentum.parquet || ok=0
        fi
        [ "$ok" -eq 0 ] || return 0
        [ "$(date +%s)" -lt "$deadline" ] || fail "RMOM bootstrap timed out"
        sleep "$RMOM_BOOTSTRAP_RETRY_SECONDS"
    done
}

activate_mode() {
    load_authorization
    require_quiescent
    check_demo_order_permissions deploy
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        validate_hedge_model_prior
    fi

    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    systemctl enable liquidity-migration-account-execution.service
    systemctl start liquidity-migration-account-execution.service
    if [ "$AUTH_PROFILE" = operational ]; then
        systemctl enable liquidity-migration-account-paper-execution.service
        systemctl start liquidity-migration-account-paper-execution.service
    fi

    start_if "$LONG_SLEEVE" liquidity-migration-bybit-long-demo.service
    if [ "$AUTH_PROFILE" = operational ]; then
        start_if "$LONG_SLEEVE" liquidity-migration-bybit-long-paper.service
    fi
    start_if "$CONTINUOUS_SLEEVE" liquidity-migration-bybit-continuous-demo.service
    if [ "$AUTH_PROFILE" = operational ]; then
        start_if "$CONTINUOUS_PAPER_SLEEVE" liquidity-migration-bybit-continuous-paper.service
    fi

    if sleeve_on "$CONTINUOUS_SLEEVE" \
        || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
        run_phase seed-residual-momentum seed_rmom
        systemctl enable --now liquidity-migration-continuous-rmom-refresh.timer
    fi
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        systemctl enable --now liquidity-migration-continuous-hedge.timer
    fi
    systemctl enable --now liquidity-migration-demo-liveness.timer
    verify_topology
}

ROLLOUT_DOWNSTREAM_UNITS=(
    liquidity-migration-demo-liveness.timer
    liquidity-migration-continuous-hedge.timer
    liquidity-migration-continuous-rmom-refresh.timer
    liquidity-migration-bybit-long-demo.service
    liquidity-migration-bybit-long-paper.service
    liquidity-migration-bybit-continuous-demo.service
    liquidity-migration-bybit-continuous-paper.service
    liquidity-migration-continuous-hedge.service
    liquidity-migration-continuous-rmom-refresh.service
    liquidity-migration-demo-liveness.service
)
ROLLOUT_OWNER_UNITS=(
    liquidity-migration-account-execution.service
    liquidity-migration-account-paper-execution.service
)
ROLLOUT_STOPPED=0
ROLLOUT_IRREVERSIBLE=0
ROLLOUT_COMPLETE=0
ROLLOUT_CURRENT_COMMIT=""
ROLLOUT_TARGET_COMMIT=""

stop_rollout_units() {
    local unit
    for unit in "$@"; do
        systemctl stop "$unit"
        printf 'stopped unit=%s\n' "$unit"
    done
    for unit in "$@"; do
        ! systemctl is-active --quiet "$unit" \
            || fail "unit remained active after rollout stop: $unit"
    done
}

stop_all_rollout_units_best_effort() {
    local unit failed=0
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        if ! systemctl stop "$unit"; then
            printf 'failed-to-stop unit=%s\n' "$unit" >&2
            failed=1
        fi
    done
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        if systemctl is-active --quiet "$unit"; then
            printf 'still-active unit=%s\n' "$unit" >&2
            failed=1
        fi
    done
    return "$failed"
}

rollout_cleanup() {
    local status="$?"
    trap - EXIT INT TERM
    if [ "$status" -ne 0 ] && [ "$ROLLOUT_STOPPED" -eq 1 ] \
        && [ "$ROLLOUT_COMPLETE" -eq 0 ]; then
        if [ "$ROLLOUT_IRREVERSIBLE" -eq 0 ]; then
            printf '%s\n' \
                'rollout failed before install; restoring the verified prior topology' >&2
            if stop_all_rollout_units_best_effort \
                && (
                    EXPECTED_COMMIT="$ROLLOUT_CURRENT_COMMIT"
                    activate_mode
                ); then
                printf 'rollout-restore-ok commit=%s\n' "$ROLLOUT_CURRENT_COMMIT" >&2
            else
                printf '%s\n' \
                    'CRITICAL: prior topology restore failed; forcing the managed fleet stopped' >&2
                stop_all_rollout_units_best_effort || true
            fi
        else
            printf '%s\n' \
                'rollout cannot safely restore prior authority; forcing the managed fleet stopped for explicit recovery' >&2
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

issue_rollout_authorization() {
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    export LIQUIDITY_MIGRATION_MAINTENANCE_LOCK_FDS=9,8,7
    "$PYTHON" -m liquidity_migration.operational_runtime_authority issue \
        --expected-commit "$EXPECTED_COMMIT" \
        --repo-root "$REPO_DIR" \
        --profile "$DEPLOY_PROFILE" \
        --authorization-reference "$DEPLOY_AUTHORIZATION_REFERENCE" \
        --owner-acknowledgement "$DEPLOY_OWNER_ACKNOWLEDGEMENT"
}

rollout_mode() {
    require_checkout
    ROLLOUT_TARGET_COMMIT="$EXPECTED_COMMIT"
    ROLLOUT_CURRENT_COMMIT="$(safe_git rev-parse HEAD)" \
        || fail "cannot read installed checkout HEAD"

    run_phase rollout-target-prefetch prefetch_rollout_target

    # Prove the current receipt/topology before changing any unit, using the
    # commit it actually authorizes rather than the incoming target commit.
    EXPECTED_COMMIT="$ROLLOUT_CURRENT_COMMIT"
    load_authorization rollout-shutdown
    run_phase current-topology-verification verify_topology
    run_phase pre-stop-flat-account-proof rollout_flat_check allow_behind
    EXPECTED_COMMIT="$ROLLOUT_TARGET_COMMIT"

    if [ "${AUTH_SHUTDOWN_EXPIRED_DEMO_RULES:-0}" -eq 1 ]; then
        # Strict runtime verification cannot restart the old topology once its
        # demo-rule evidence has expired.  Make that loss of rollback explicit
        # before stopping anything; all failure paths from here force stopped.
        ROLLOUT_IRREVERSIBLE=1
        echo "rollout-recovery-boundary rollback=unavailable reason=expired-demo-rules"
    fi
    ROLLOUT_STOPPED=1
    trap rollout_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    # Stop every producer/timer before either owner. A bounded exact-head
    # recheck closes the target/journal race while the owner is still alive;
    # only then are owners stopped and venue truth sampled once more.
    run_phase stop-downstream-units \
        stop_rollout_units "${ROLLOUT_DOWNSTREAM_UNITS[@]}"
    run_phase post-producer-flat-account-proof retry_exact_rollout_flat_check
    run_phase stop-account-owners \
        stop_rollout_units "${ROLLOUT_OWNER_UNITS[@]}"
    run_phase final-stopped-flat-account-proof rollout_flat_check none
    require_quiescent

    # From checkout mutation onward the old create-only receipt cannot be used
    # as rollback authority. Any failure therefore leaves every managed unit
    # stopped instead of guessing across commits.
    ROLLOUT_IRREVERSIBLE=1
    ROLLOUT_REFRESH_STALE_DEMO_RULES=1
    run_phase stopped-install install_mode
    if [ "$ROLLOUT_DEMO_RULES_REFRESHED" -eq 1 ]; then
        run_phase post-rule-refresh-flat-account-proof \
            rollout_flat_check stopped-maintenance
    fi
    run_phase create-operational-authority issue_rollout_authorization
    run_phase activate-and-verify activate_mode
    ROLLOUT_COMPLETE=1
    ROLLOUT_STOPPED=0
    printf 'rollout-ok commit=%s profile=%s\n' "$EXPECTED_COMMIT" "$DEPLOY_PROFILE"
}

acquire_maintenance_locks
case "$MODE" in
    install) install_mode ;;
    activate) activate_mode ;;
    verify) load_authorization; verify_topology ;;
    rollout) rollout_mode ;;
esac
REMOTE_SCRIPT
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
