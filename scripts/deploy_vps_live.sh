#!/usr/bin/env bash
# Staged VPS lifecycle plus a guarded, flat-account one-command rollout.
set -euo pipefail

MODE="${1:-${DEPLOY_MODE:-verify}}"
if [ "$#" -gt 0 ]; then shift; fi
DEPLOY_PROFILE=""
if [ "$MODE" = rollout ]; then
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --profile)
                [ "$#" -ge 2 ] || { echo "--profile requires a value" >&2; exit 2; }
                DEPLOY_PROFILE="$2"
                shift 2
                ;;
            *) echo "unknown $MODE argument: $1" >&2; exit 2 ;;
        esac
    done
    case "$DEPLOY_PROFILE" in
        demo-operational|operational) ;;
        *) echo "$MODE requires --profile demo-operational|operational" >&2; exit 2 ;;
    esac
elif [ "$#" -ne 0 ]; then
    echo "usage: deploy_vps_live.sh {install|activate|verify|rollout|activate-mainnet|stop-mainnet}" >&2
    exit 2
fi
case "$MODE" in
    install|activate|verify|rollout|activate-mainnet|stop-mainnet) ;;
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
    printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
    printf 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS=%q\n' "$RMOM_BOOTSTRAP_TIMEOUT_SECONDS"
    printf 'RMOM_BOOTSTRAP_RETRY_SECONDS=%q\n' "$RMOM_BOOTSTRAP_RETRY_SECONDS"
    printf 'DEPLOY_PROFILE=%q\n' "$DEPLOY_PROFILE"
    printf 'ROLLOUT_REFRESH_STALE_DEMO_RULES=%q\n' \
        "${ROLLOUT_REFRESH_STALE_DEMO_RULES:-0}"
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
CARRY_DEMO_ROOT=/opt/liquidity-migration/data/bybit-carry-demo-event
LONG_PAPER_ROOT=/opt/liquidity-migration/data/bybit-long-paper-event
CONTINUOUS_PAPER_ROOT=/opt/liquidity-migration/data/bybit-continuous-paper-event
CARRY_PAPER_ROOT=/opt/liquidity-migration/data/bybit-carry-paper-event

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
from liquidity_migration.operational_profile import load_operational_profile

load_operational_profile(sys.argv[1])
PY
    # Install only while units are quiescent (install_mode enforces that). The
    # owner and all producers then read these bytes via ACCOUNT_RISK_POLICY_FILE.
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

    # Rebuild the non-secret paper route as strict data: an existing file may
    # contribute tuning values only, never credentials or alternate roots.
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
        "$PAPER_SYMBOLS_FILE" "$PAPER_RULES_FILE" "$PAPER_RISK_FILE" \
        "$REPO_DIR/configs/operational.demo.json" \
        "$demo_capture" "$demo_account_root" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.operational_profile import load_operational_profile
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
    "MAX_DEMO_RULE_AGE_HOURS",
    "ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS",
}
values = {key: value for key, value in existing.items() if key in allowed_tuning}
# The paper twin's capital base tracks the committed profile's capital
# reference, keeping it comparable to the demo book with no per-host tuning.
values["PAPER_EQUITY_USDT"] = f"{load_operational_profile(sys.argv[8]).capital_reference_usdt:g}"
# The paper owner unit enables Telegram: keep operator-provided notification
# credentials or seed them from the demo channel. Transport keys only; venue
# credentials stay forbidden above.
telegram_keys = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
telegram = {key: existing.get(key, "").strip() for key in telegram_keys}
if not all(telegram.values()):
    demo_channel = Path("/etc/liquidity-migration/bybit-demo.env")
    seeded = (
        load_private_systemd_environment(demo_channel) if demo_channel.is_file() else {}
    )
    for key in telegram_keys:
        telegram[key] = telegram[key] or seeded.get(key, "").strip()
if not all(telegram.values()):
    raise SystemExit(
        "paper Telegram credentials unavailable: provide TELEGRAM_BOT_TOKEN and "
        "TELEGRAM_CHAT_ID in the paper environment or bybit-demo.env"
    )
values.update(telegram)
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
        # Read-only demo roots for the paper target mirror: the demo capture
        # tape, and the demo owner's health projection for equity-ratio scaling.
        "DEMO_ACCOUNT_CAPTURE_ROOT": sys.argv[9],
        "DEMO_ACCOUNT_EXECUTION_ROOT": sys.argv[10],
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
        "$LONG_PAPER_ROOT" "$CONTINUOUS_PAPER_ROOT" "$CARRY_PAPER_ROOT"; do
        paper_path_args+=(--root "$root")
    done
    paper_tree_preflight_phase() {
        "$PYTHON" -m liquidity_migration.reset_path_safety preflight-paper \
            --anchor "$REPO_DIR/data" "${paper_path_args[@]}"
    }
    demo_tree_preflight_phase() {
        "$PYTHON" -m liquidity_migration.reset_path_safety preflight-demo \
            --anchor "$REPO_DIR/data" \
            --root "$LONG_DEMO_ROOT" --root "$CONTINUOUS_DEMO_ROOT" \
            --root "$CARRY_DEMO_ROOT" \
            --continuous-root "$CONTINUOUS_DEMO_ROOT"
    }
    paper_tree_normalize_phase() {
        "$PYTHON" -m liquidity_migration.reset_path_safety normalize-paper \
            --anchor "$REPO_DIR/data" "${paper_path_args[@]}" \
            --uid "$paper_uid" --gid "$paper_gid" --create-missing
    }
    demo_tree_normalize_phase() {
        "$PYTHON" -m liquidity_migration.reset_path_safety normalize-demo \
            --anchor "$REPO_DIR/data" \
            --root "$LONG_DEMO_ROOT" --root "$CONTINUOUS_DEMO_ROOT" \
            --root "$CARRY_DEMO_ROOT" \
            --continuous-root "$CONTINUOUS_DEMO_ROOT" \
            --uid "$root_uid" --gid "$paper_gid" --create-missing
    }

    # The batches are disjoint. Complete both read-only plans before either
    # mutation starts, then normalize them concurrently. Each normalizer still
    # performs its own full descriptor-rooted plan and independent final rescan.
    run_phase_pair runtime-tree-preflight \
        paper-tree-preflight paper_tree_preflight_phase \
        demo-tree-preflight demo_tree_preflight_phase \
        || fail "paper/demo runtime descriptor/mount preflight failed"
    run_phase_pair runtime-tree-normalize \
        paper-tree-normalize paper_tree_normalize_phase \
        demo-tree-normalize demo_tree_normalize_phase \
        || fail "descriptor-rooted paper/demo runtime normalization failed"
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
        "$LONG_PAPER_ROOT" "$CONTINUOUS_PAPER_ROOT" "$CARRY_PAPER_ROOT"; do
        if [ ! -e "$root" ] && [ "${PAPER_BOUNDARY_PRE_INSTALL:-0}" = 1 ]; then
            # A root introduced by the commit being deployed does not exist
            # until its install phase creates it; the post-install check is strict.
            echo "paper-boundary-pending root=$root reason=created-by-this-install"
            continue
        fi
        runuser -u "$PAPER_RUNTIME_USER" -- test -w "$root" \
            || fail "paper runtime cannot write its explicit state root: $root"
        runuser -u "$PAPER_RUNTIME_USER" -- test -w "$root/.locks" \
            || fail "paper runtime cannot write its persistent lock directory: $root/.locks"
    done
    # The carry paper producer follows the carry demo market plane read-only
    # (CARRY_MARKET_FOLLOW_ROOT). Carry has no WS kline plane, but normalize-demo
    # still provisions the traversable cache tree.
    for root in "$LONG_DEMO_ROOT" "$CONTINUOUS_DEMO_ROOT" "$CARRY_DEMO_ROOT"; do
        if [ ! -e "$root" ] && [ "${PAPER_BOUNDARY_PRE_INSTALL:-0}" = 1 ]; then
            echo "paper-boundary-pending root=$root reason=created-by-this-install"
            continue
        fi
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

retire_stale_operational_receipt() {
    # Hosts upgraded from an older commit still carry the operational-ready
    # receipt; no unit consults it, so archive it rather than leave a dead gate.
    local path=/etc/liquidity-migration/account-execution-operational-ready archive stamp
    [ -e "$path" ] || [ -L "$path" ] || return 0
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    archive="/var/lib/liquidity-migration/retired-authority/$stamp"
    install -d -m 0700 "$archive"
    mv "$path" "$archive/$(basename "$path")"
    echo "retired stale operational receipt: $archive"
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
from liquidity_migration.candidate_rule_coverage import (
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

from liquidity_migration.account_candidate_universe import load_candidate_universe

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

from liquidity_migration.account_execution_config import load_demo_rules
from liquidity_migration.account_candidate_universe import load_candidate_universe
from liquidity_migration.candidate_rule_coverage import build_candidate_rule_coverage
from liquidity_migration.candidate_rule_coverage import REGISTERED_MAX_RULE_AGE_SECONDS
from liquidity_migration.systemd_environment import load_private_systemd_environment

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
        tests/test_candidate_rule_coverage.py \
        tests/test_demo_rule_probe.py \
        tests/test_deploy_rollout_readiness.py \
        tests/test_operational_profile.py \
        tests/test_strategy_planning.py \
        tests/test_runtime_scripts.py

    # Bound journald so logs cannot crowd the data roots, and keep at most the
    # newest timestamped backup of the demo credential file.
    install -d -m 0755 /etc/systemd/journald.conf.d
    printf '[Journal]\nSystemMaxUse=1G\n' \
        > /etc/systemd/journald.conf.d/liquidity-migration.conf
    systemctl restart systemd-journald 2>/dev/null || true
    find /etc/liquidity-migration -maxdepth 1 -name 'bybit-demo.env.backup.*' -type f \
        | sort | head -n -1 | while IFS= read -r stale_backup; do
        rm -f -- "$stale_backup"
    done

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    ensure_paper_runtime_identity
    run_phase install-systemd-manifest lm_install_current_systemd_units
    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    require_quiescent
    retire_stale_operational_receipt
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
    echo "next: run activate to start the sleeves this checkout enables"
}

load_authorization() {
    require_checkout
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    # Which profile is installed, read from the marker install wrote. An
    # explicit --profile on this invocation wins.
    AUTH_PROFILE="${DEPLOY_PROFILE:-}"
    if [ -z "$AUTH_PROFILE" ] && [ -r "$PROFILE_MARKER" ]; then
        AUTH_PROFILE="$(cat "$PROFILE_MARKER")"
    fi
    [ -n "$AUTH_PROFILE" ] || AUTH_PROFILE=operational
    AUTH_SHUTDOWN_EXPIRED_DEMO_RULES=0
    case "$AUTH_PROFILE" in demo-operational|operational) ;; *) fail "unsupported profile $AUTH_PROFILE" ;; esac
    if [ "$AUTH_PROFILE" = operational ]; then
        # Pre-install: state roots introduced by the commit being deployed do
        # not exist yet; the strict boundary check re-runs after install.
        PAPER_BOUNDARY_PRE_INSTALL=1 verify_paper_runtime_boundary
    fi

    . deploy/lib_sleeves.sh
    . deploy/lib_systemd_environment.sh
    lm_load_group_systemd_environment "$PYTHON" \
        /etc/liquidity-migration/sleeves.resolved.env "$PAPER_RUNTIME_GROUP" \
        LONG_SLEEVE CONTINUOUS_SLEEVE CONTINUOUS_PAPER_SLEEVE \
        CARRY_SLEEVE CARRY_PAPER_SLEEVE PAPER_TARGET_MIRROR \
        CARRY_MAINNET_SLEEVE LONG_MAINNET_SLEEVE CONTINUOUS_HEDGE_TIMER
    # A resolved file written by a pre-carry install lacks the carry keys;
    # absent means never deployed, i.e. off. Bridges only the rollout that
    # introduces them — the post-install verifier requires them present.
    if [ -z "${CARRY_SLEEVE:-}" ] || [ -z "${CARRY_PAPER_SLEEVE:-}" ]; then
        echo "sleeves-resolved-transition carry-keys=absent treated-as=off reason=pre-carry-install"
        CARRY_SLEEVE="${CARRY_SLEEVE:-off}"
        CARRY_PAPER_SLEEVE="${CARRY_PAPER_SLEEVE:-off}"
    fi
    if [ -z "${PAPER_TARGET_MIRROR:-}" ] || [ -z "${CARRY_MAINNET_SLEEVE:-}" ] \
        || [ -z "${LONG_MAINNET_SLEEVE:-}" ]; then
        echo "sleeves-resolved-transition mirror-mainnet-keys=absent treated-as=off reason=pre-mirror-install"
        PAPER_TARGET_MIRROR="${PAPER_TARGET_MIRROR:-off}"
        CARRY_MAINNET_SLEEVE="${CARRY_MAINNET_SLEEVE:-off}"
        LONG_MAINNET_SLEEVE="${LONG_MAINNET_SLEEVE:-off}"
    fi
    for value in "$LONG_SLEEVE" "$CONTINUOUS_SLEEVE" "$CONTINUOUS_PAPER_SLEEVE" \
        "$CARRY_SLEEVE" "$CARRY_PAPER_SLEEVE" "$PAPER_TARGET_MIRROR" \
        "$CARRY_MAINNET_SLEEVE" "$LONG_MAINNET_SLEEVE" "$CONTINUOUS_HEDGE_TIMER"; do
        case "$value" in on|off) ;; *) fail "invalid resolved sleeve value" ;; esac
    done
    if [ "$AUTH_PROFILE" = demo-operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
        fail "demo-operational authorization cannot run the paper continuous sleeve"
    fi
    if [ "$AUTH_PROFILE" = demo-operational ] && sleeve_on "$CARRY_PAPER_SLEEVE"; then
        fail "demo-operational authorization cannot run the paper carry sleeve"
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

any_mainnet_sleeve_on() {
    sleeve_on "${CARRY_MAINNET_SLEEVE:-off}" || sleeve_on "${LONG_MAINNET_SLEEVE:-off}"
}

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
    "$PYTHON" scripts/runtime/run_continuous_hedge.py \
        --execution-environment demo \
        --validate-model-prior-only
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
    if sleeve_on "$CARRY_SLEEVE"; then
        expected_downstream_on liquidity-migration-bybit-carry-demo.service \
            || fail "carry demo producer is not active"
    else
        unit_off liquidity-migration-bybit-carry-demo.service || fail "carry demo producer is not off"
    fi
    if [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CARRY_PAPER_SLEEVE"; then
        expected_downstream_on liquidity-migration-bybit-carry-paper.service \
            || fail "carry paper producer is not active"
    else
        unit_off liquidity-migration-bybit-carry-paper.service || fail "carry paper producer is not off"
    fi
    if [ "$AUTH_PROFILE" = operational ] && sleeve_on "$PAPER_TARGET_MIRROR"; then
        expected_downstream_on liquidity-migration-paper-target-mirror.service \
            || fail "paper target mirror is not active"
    else
        unit_off liquidity-migration-paper-target-mirror.service || fail "paper target mirror is not off"
    fi

    if sleeve_on "$CONTINUOUS_SLEEVE" \
        || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
        expected_downstream_on liquidity-migration-continuous-rmom-refresh.timer \
            || fail "RMOM timer is not active"
    else
        timer_off liquidity-migration-continuous-rmom-refresh.timer || fail "RMOM timer is not off"
    fi
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        validate_hedge_model_prior || fail "hedge model prior validation failed"
        expected_downstream_on liquidity-migration-continuous-hedge.timer \
            || fail "hedge timer is not active"
    else
        timer_off liquidity-migration-continuous-hedge.timer || fail "hedge timer is not off"
    fi
    # With no mainnet sleeve on, a running mainnet unit must not hide behind a
    # green demo/paper verification; with one on, the funded fleet is verified
    # exactly like the others.
    if any_mainnet_sleeve_on; then
        unit_on liquidity-migration-account-execution-mainnet.service \
            || fail "mainnet owner is not active and enabled"
        if sleeve_on "$CARRY_MAINNET_SLEEVE"; then
            expected_downstream_on liquidity-migration-bybit-carry-mainnet.service \
                || fail "carry mainnet producer is not active"
        else
            unit_off liquidity-migration-bybit-carry-mainnet.service \
                || fail "carry mainnet producer is not off"
        fi
        if sleeve_on "$LONG_MAINNET_SLEEVE"; then
            expected_downstream_on liquidity-migration-bybit-long-mainnet.service \
                || fail "LONG mainnet producer is not active"
        else
            unit_off liquidity-migration-bybit-long-mainnet.service \
                || fail "LONG mainnet producer is not off"
        fi
        timer_on liquidity-migration-mainnet-liveness.timer \
            || fail "mainnet liveness timer is not active"
        # An enabled timer says nothing about the run it fires. A watchdog that
        # fails every fire is the silence it exists to break.
        ! systemctl is-failed --quiet liquidity-migration-mainnet-liveness.service \
            || fail "liquidity-migration-mainnet-liveness.service is failed"
    else
        for mainnet_unit in \
            liquidity-migration-account-execution-mainnet.service \
            liquidity-migration-bybit-carry-mainnet.service \
            liquidity-migration-bybit-long-mainnet.service \
            liquidity-migration-mainnet-liveness.timer; do
            unit_off "$mainnet_unit" \
                || fail "$mainnet_unit is active under demo/paper authorization"
        done
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
    check_demo_order_permissions verify \
        || fail "demo order permission verification failed"
    printf 'verify-ok commit=%s profile=%s mainnet_carry=%s mainnet_long=%s\n' \
        "$EXPECTED_COMMIT" "$AUTH_PROFILE" "$CARRY_MAINNET_SLEEVE" "$LONG_MAINNET_SLEEVE"
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
    local deadline gate_path ok
    gate_path=data/bybit-continuous-demo-event/residual_momentum.parquet

    # Reset preserves this artifact, and the daily timer owns recomputation, so
    # a valid gate is reused rather than rebuilt (~1 min) on every deployment.
    # A failed prior refresh still forces the repair path.
    if ! systemctl is-failed --quiet liquidity-migration-continuous-rmom-refresh.service \
        && "$PYTHON" scripts/research/check_residual_momentum_gate.py --path "$gate_path"; then
        echo "rmom-bootstrap path=reuse reason=current-valid-gate"
        return 0
    fi
    echo "rmom-bootstrap path=refresh reason=missing-stale-invalid-or-failed-unit"
    deadline=$(( $(date +%s) + RMOM_BOOTSTRAP_TIMEOUT_SECONDS ))
    while true; do
        ok=1
        systemctl reset-failed liquidity-migration-continuous-rmom-refresh.service 2>/dev/null || true
        systemctl start liquidity-migration-continuous-rmom-refresh.service || ok=0
        if sleeve_on "$CONTINUOUS_SLEEVE" \
            || { [ "$AUTH_PROFILE" = operational ] && sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; }; then
            "$PYTHON" scripts/research/check_residual_momentum_gate.py \
                --path "$gate_path" || ok=0
        fi
        [ "$ok" -eq 0 ] || return 0
        [ "$(date +%s)" -lt "$deadline" ] || fail "RMOM bootstrap timed out"
        sleep "$RMOM_BOOTSTRAP_RETRY_SECONDS"
    done
}

activate_mode() {
    load_authorization
    require_quiescent
    check_demo_order_permissions deploy \
        || fail "demo order permission deploy check failed"
    if sleeve_on "$CONTINUOUS_HEDGE_TIMER"; then
        validate_hedge_model_prior || fail "hedge model prior validation failed"
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
    start_if "$CARRY_SLEEVE" liquidity-migration-bybit-carry-demo.service
    if [ "$AUTH_PROFILE" = operational ]; then
        start_if "$CARRY_PAPER_SLEEVE" liquidity-migration-bybit-carry-paper.service
        start_if "$PAPER_TARGET_MIRROR" liquidity-migration-paper-target-mirror.service
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
    if any_mainnet_sleeve_on; then
        start_mainnet_fleet
    fi
    verify_topology
}

MAINNET_OWNER_UNIT=liquidity-migration-account-execution-mainnet.service
MAINNET_LIVENESS_TIMER=liquidity-migration-mainnet-liveness.timer
MAINNET_LIVENESS_SERVICE=liquidity-migration-mainnet-liveness.service

ensure_mainnet_state_roots() {
    "$PYTHON" -m liquidity_migration.real_money_arming create-state-roots --execute \
        || fail "mainnet state root creation failed"
}

# The single gate between a code change and a funded account: every remaining
# precondition is reported, and any one of them outstanding stops the deploy.
require_mainnet_preflight() {
    local report status=0
    report="$("$PYTHON" -m liquidity_migration.real_money_arming preflight 2>&1)" || status=$?
    printf '%s\n' "$report"
    [ "$status" -eq 0 ] || fail "mainnet preflight has outstanding steps (status $status)"
}

start_mainnet_fleet() {
    ensure_mainnet_state_roots
    require_mainnet_preflight
    systemctl enable "$MAINNET_OWNER_UNIT"
    systemctl start "$MAINNET_OWNER_UNIT"
    start_if "$CARRY_MAINNET_SLEEVE" liquidity-migration-bybit-carry-mainnet.service
    start_if "$LONG_MAINNET_SLEEVE" liquidity-migration-bybit-long-mainnet.service
    systemctl enable --now "$MAINNET_LIVENESS_TIMER"
}

activate_mainnet_mode() {
    load_authorization
    any_mainnet_sleeve_on \
        || fail "no mainnet sleeve is on; turn CARRY_MAINNET_SLEEVE and/or LONG_MAINNET_SLEEVE on in deploy/sleeves.env, then install"
    start_mainnet_fleet
    verify_topology
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
    echo "note: the mainnet sleeves are still on, so verify now fails and the next activate or rollout restarts this fleet. Turn CARRY_MAINNET_SLEEVE/LONG_MAINNET_SLEEVE off and install to make the stop stick."
}

ROLLOUT_DOWNSTREAM_UNITS=(
    liquidity-migration-demo-liveness.timer
    liquidity-migration-mainnet-liveness.timer
    liquidity-migration-continuous-hedge.timer
    liquidity-migration-continuous-rmom-refresh.timer
    liquidity-migration-bybit-long-demo.service
    liquidity-migration-bybit-long-paper.service
    liquidity-migration-bybit-long-mainnet.service
    liquidity-migration-bybit-continuous-demo.service
    liquidity-migration-bybit-continuous-paper.service
    liquidity-migration-bybit-carry-demo.service
    liquidity-migration-bybit-carry-paper.service
    liquidity-migration-bybit-carry-mainnet.service
    liquidity-migration-paper-target-mirror.service
    liquidity-migration-continuous-hedge.service
    liquidity-migration-continuous-rmom-refresh.service
    liquidity-migration-demo-liveness.service
    liquidity-migration-mainnet-liveness.service
)
# Owners stop last and start first: every mainnet producer declares
# Requires=/After= on the mainnet owner.
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
    run_strict_phase pre-stop-flat-account-proof rollout_flat_check allow_behind
    if [ "${AUTH_SHUTDOWN_EXPIRED_DEMO_RULES:-0}" -eq 1 ]; then
        echo "deployment-plan class=exceptional rule_maintenance=full-probe reason=expired"
    else
        echo "deployment-plan class=routine rule_maintenance=reuse reason=fresh"
    fi
    EXPECTED_COMMIT="$ROLLOUT_TARGET_COMMIT"

    if [ "${AUTH_SHUTDOWN_EXPIRED_DEMO_RULES:-0}" -eq 1 ]; then
        # The old topology cannot restart once its demo-rule evidence expired,
        # so every failure path from here forces the fleet stopped.
        ROLLOUT_IRREVERSIBLE=1
        echo "rollout-recovery-boundary rollback=unavailable reason=expired-demo-rules"
    fi
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
    run_strict_phase post-producer-flat-account-proof retry_exact_rollout_flat_check
    run_strict_phase stop-account-owners \
        stop_rollout_units "${ROLLOUT_OWNER_UNITS[@]}"
    run_strict_phase final-stopped-flat-account-proof rollout_flat_check none
    require_quiescent

    # From checkout mutation onward there is no rollback authority, so any
    # failure leaves every managed unit stopped rather than guessing.
    ROLLOUT_IRREVERSIBLE=1
    ROLLOUT_REFRESH_STALE_DEMO_RULES=1
    run_strict_phase stopped-install install_mode
    if [ "$ROLLOUT_DEMO_RULES_REFRESHED" -eq 1 ]; then
        run_strict_phase post-rule-refresh-flat-account-proof \
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
    activate-mainnet) activate_mainnet_mode ;;
    stop-mainnet) stop_mainnet_mode ;;
    verify) load_authorization; verify_topology ;;
    rollout) rollout_mode ;;
esac
REMOTE_SCRIPT
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
