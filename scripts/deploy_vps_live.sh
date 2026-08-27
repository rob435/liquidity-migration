#!/usr/bin/env bash
# Staged VPS lifecycle plus a guarded, flat-account one-command rollout.
set -euo pipefail

MODE="${1:-${DEPLOY_MODE:-verify}}"
if [ "$#" -gt 0 ]; then shift; fi
case "$MODE" in
    install|activate|verify|staged|rollout|stop-mainnet|disarm-mainnet) ;;
    activate-mainnet)
        echo "activate-mainnet retired 2026-08-03: the arming switch is REAL_MONEY=true in /etc/liquidity-migration/bybit-mainnet.env; a plain activate or rollout starts the mainnet fleet when it is armed" >&2
        exit 2
        ;;
    *) echo "invalid deploy mode: $MODE" >&2; exit 2 ;;
esac

deploy_usage() {
    cat >&2 <<'USAGE'
usage: deploy_vps_live.sh {install|activate|verify|staged|rollout|stop-mainnet|disarm-mainnet}
  --profile operational                   required for staged and rollout
  --stop-first / --no-stop-first          install|activate|staged: stop a running
                                          fleet instead of refusing. Default: stop
                                          unless real money is armed.
  --require-flat                          rollout: gate on a flat demo account
                                          rather than reporting residuals
USAGE
    exit 2
}

DEPLOY_PROFILE=""
STOP_FIRST=auto
REQUIRE_FLAT=0

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
    local lock_dir=/run/liquidity-migration lock_path
    # maintenance.lock is the canonical mutex; the two retired leaves stay
    # nested so an old deploy or reset process is still excluded.
    command -v flock >/dev/null 2>&1 || fail "flock is required for deploy serialization"
    install -d -o root -g root -m 0755 "$lock_dir" \
        || fail "cannot prepare the maintenance lock directory"
    for lock_path in \
        "$lock_dir/maintenance.lock" \
        "$lock_dir/deploy.lock" \
        /run/lock/liquidity-migration-ledger-reset.lock; do
        [ ! -L "$lock_path" ] || fail "maintenance lock path is a symlink: $lock_path"
        if [ ! -e "$lock_path" ]; then
            (umask 077; : > "$lock_path") \
                || fail "cannot create maintenance lock: $lock_path"
        fi
        [ -f "$lock_path" ] && [ ! -L "$lock_path" ] \
            || fail "maintenance lock is not a regular file: $lock_path"
        [ "$(stat -c %u "$lock_path")" -eq 0 ] \
            || fail "maintenance lock is not root-owned: $lock_path"
        chmod 0600 "$lock_path" || fail "cannot secure maintenance lock: $lock_path"
    done
    exec 9<>"$lock_dir/maintenance.lock" \
        || fail "cannot open canonical maintenance lock without truncation"
    exec 8<>"$lock_dir/deploy.lock" \
        || fail "cannot open legacy deploy lock without truncation"
    exec 7<>/run/lock/liquidity-migration-ledger-reset.lock \
        || fail "cannot open legacy reset lock without truncation"
    flock --exclusive --nonblock 9 \
        || fail "another maintenance operation holds maintenance.lock"
    flock --exclusive --nonblock 8 \
        || fail "another maintenance operation holds the legacy deploy lock"
    flock --exclusive --nonblock 7 \
        || fail "another maintenance operation holds the legacy reset lock"
}

PROFILE_MARKER=/etc/liquidity-migration/profile
# Retired paper-fleet artifacts, removed from any host that still carries them.
# The runtime user/group stay if present (inert without units); state roots
# stay on disk as historical record.
RETIRED_PAPER_CONFIG_DIR=/etc/liquidity-migration/account-paper-execution
RETIRED_PAPER_ENVIRONMENT=/etc/liquidity-migration/account-paper-execution.env
LONG_DEMO_ROOT=/opt/liquidity-migration/data/bybit-long-demo-event
CARRY_DEMO_ROOT=/opt/liquidity-migration/data/bybit-carry-demo-event
LONG_MAINNET_ROOT=/opt/liquidity-migration/data/bybit-long-mainnet-event
CARRY_MAINNET_ROOT=/opt/liquidity-migration/data/bybit-carry-mainnet-event

RUNTIME_GROUP=liquidity-migration
PRODUCER_USER=liquidity-producer
DEMO_ENGINE_USER=liquidity-engine-demo
MAINNET_ENGINE_USER=liquidity-engine-mainnet
OBSERVER_USER=liquidity-observer
LLM_USER=liquidity-llm
PRODUCER_DEMO_ENV=/etc/liquidity-migration/producer-demo.env
PRODUCER_MAINNET_ENV=/etc/liquidity-migration/producer-mainnet.env
PRODUCER_DEMO_SOURCE_ENV=/etc/liquidity-migration/producer-demo-source.env
PRODUCER_MAINNET_SOURCE_ENV=/etc/liquidity-migration/producer-mainnet-source.env
MAINNET_TELEGRAM_ENV=/etc/liquidity-migration/telegram-mainnet.env

ensure_runtime_identities() {
    getent group "$RUNTIME_GROUP" >/dev/null 2>&1 || groupadd --system "$RUNTIME_GROUP"
    local user
    for user in "$PRODUCER_USER" "$DEMO_ENGINE_USER" "$MAINNET_ENGINE_USER" "$OBSERVER_USER" "$LLM_USER"; do
        if ! id -u "$user" >/dev/null 2>&1; then
            useradd --system --no-create-home --home-dir /nonexistent \
                --shell /usr/sbin/nologin --gid "$RUNTIME_GROUP" "$user"
        fi
        id -nG "$user" | tr ' ' '\n' | grep -Fx "$RUNTIME_GROUP" >/dev/null \
            || fail "$user is not isolated in the $RUNTIME_GROUP runtime group"
    done
    install -d -o root -g root -m 0755 /etc/tmpfiles.d
    printf 'd /run/lock/liquidity-migration 0770 root %s -\n' "$RUNTIME_GROUP" \
        > /etc/tmpfiles.d/liquidity-migration.conf
    systemd-tmpfiles --create /etc/tmpfiles.d/liquidity-migration.conf \
        || fail "cannot create the engine lease directory"
    install -d -o "$PRODUCER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/targets
}

write_producer_environment() {
    local source="$1" target="$2"
    "$PYTHON" - "$source" "$target" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment
source = Path(sys.argv[1])
target = Path(sys.argv[2])
allowed = {
    "CANDIDATE_UNIVERSE_FILE", "CARRY_NOTIONAL_MULTIPLIER",
    "EXODUS_NOTIONAL_MULTIPLIER", "LONG_NOTIONAL_MULTIPLIER",
    "OPERATIONAL_PROFILE_FILE", "PRODUCER_REALM",
}
values = load_private_systemd_environment(source)
forbidden = {
    "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET", "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET", "REAL_MONEY", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID", "TELEGRAM_ALERT_CHAT_ID",
}
leaked = sorted(key for key in forbidden if str(values.get(key) or "").strip())
if leaked:
    raise SystemExit(f"{source}: producer source contains forbidden secret/control keys: {', '.join(leaked)}")
filtered = {key: value for key, value in values.items() if key in allowed}
if filtered.get("PRODUCER_REALM") not in {"demo", "mainnet"}:
    raise SystemExit(f"{source}: PRODUCER_REALM must be demo or mainnet")
for required in ("CANDIDATE_UNIVERSE_FILE", "OPERATIONAL_PROFILE_FILE"):
    value = str(filtered.get(required) or "")
    if not value or not Path(value).is_absolute():
        raise SystemExit(f"{source}: {required} must be an absolute path")
target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in sorted(filtered.items()):
            handle.write(f"{key}={shlex.quote(str(value))}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
    chown root:"$RUNTIME_GROUP" "$target" && chmod 0640 "$target" \
        || fail "cannot secure producer environment $target"
    unset OPERATIONAL_PROFILE_FILE CANDIDATE_UNIVERSE_FILE
    lm_load_private_systemd_environment "$PYTHON" "$source" \
        OPERATIONAL_PROFILE_FILE CANDIDATE_UNIVERSE_FILE
    local input
    for input in "$OPERATIONAL_PROFILE_FILE" "$CANDIDATE_UNIVERSE_FILE"; do
        [ -f "$input" ] && [ ! -L "$input" ] \
            || fail "producer input is missing or linked: $input"
        chown root:"$RUNTIME_GROUP" "$input" && chmod 0640 "$input" \
            || fail "cannot secure producer input: $input"
    done
    # Grant producer traversal only along the declared inputs, never across every
    # credential/config directory under /etc/liquidity-migration.
    local directory
    for input in "$target" "$OPERATIONAL_PROFILE_FILE" "$CANDIDATE_UNIVERSE_FILE"; do
        directory="$(dirname "$input")"
        case "$directory" in
            /etc/liquidity-migration|/etc/liquidity-migration/*) ;;
            *) fail "producer input must stay below /etc/liquidity-migration: $input" ;;
        esac
        while :; do
            chown root:"$RUNTIME_GROUP" "$directory" && chmod 0750 "$directory" \
                || fail "cannot grant producer traversal: $directory"
            [ "$directory" = /etc/liquidity-migration ] && break
            directory="$(dirname "$directory")"
        done
    done
}

project_mainnet_telegram_environment() {
    "$PYTHON" - "$MAINNET_CREDENTIAL_ENV" "$MAINNET_TELEGRAM_ENV" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment
source = Path(sys.argv[1])
target = Path(sys.argv[2])
values = load_private_systemd_environment(source)
allowed = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERT_CHAT_ID")
filtered = {key: values[key] for key in allowed if str(values.get(key) or "")}
if not filtered.get("TELEGRAM_BOT_TOKEN") or not (filtered.get("TELEGRAM_CHAT_ID") or filtered.get("TELEGRAM_ALERT_CHAT_ID")):
    raise SystemExit("funded watchdog requires a Telegram token and chat id")
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in sorted(filtered.items()):
            handle.write(f"{key}={shlex.quote(str(value))}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
    chown root:root "$MAINNET_TELEGRAM_ENV" && chmod 0600 "$MAINNET_TELEGRAM_ENV" \
        || fail "cannot secure funded notification environment"
}
retire_paper_host_config() {
    rm -f "$RETIRED_PAPER_ENVIRONMENT"
    rm -rf "$RETIRED_PAPER_CONFIG_DIR"
}

prepare_demo_runtime_config() {
    local demo_candidate demo_profile root
    [ "$REPO_DIR" = /opt/liquidity-migration ] \
        || fail "systemd runtime paths require REPO_DIR=/opt/liquidity-migration"
    install -d -o "$PRODUCER_USER" -g "$RUNTIME_GROUP" -m 0750 /var/lib/liquidity-migration/targets
    if [ ! -f "$PRODUCER_DEMO_SOURCE_ENV" ]; then
        install -o root -g root -m 0600 \
            "$REPO_DIR/deploy/producer-demo-source.env.template" "$PRODUCER_DEMO_SOURCE_ENV" \
            || fail "cannot install the demo producer source env"
    fi
    for path in "$PRODUCER_DEMO_SOURCE_ENV" /etc/liquidity-migration/bybit-demo.env; do
        [ -f "$path" ] && [ ! -L "$path" ] || fail "missing real private config: $path"
        chown root:root "$path"
        chmod 0600 "$path"
    done
    lm_load_private_systemd_environment "$PYTHON" \
        "$PRODUCER_DEMO_SOURCE_ENV" \
        PRODUCER_REALM CANDIDATE_UNIVERSE_FILE OPERATIONAL_PROFILE_FILE
    [ "$PRODUCER_REALM" = demo ] || fail "demo producer source must declare PRODUCER_REALM=demo"
    demo_candidate="$CANDIDATE_UNIVERSE_FILE"
    demo_profile="$OPERATIONAL_PROFILE_FILE"
    for path in "$demo_candidate" "$demo_profile"; do
        [ "${path#/}" != "$path" ] || fail "demo producer input must be absolute: $path"
    done
    operational_profile_source="$REPO_DIR/configs/operational.demo.json"
    [ -f "$operational_profile_source" ] && [ ! -L "$operational_profile_source" ] \
        || fail "missing tracked operational profile: $operational_profile_source"
    "$PYTHON" - "$operational_profile_source" <<'PY'
import sys
from liquidity_migration.policy.operational_profile import load_operational_profile

load_operational_profile(sys.argv[1])
PY
    install -d -o root -g root -m 0700 "$(dirname "$demo_profile")"
    install -o root -g root -m 0600 "$operational_profile_source" "$demo_profile"
    [ -f "$demo_candidate" ] && [ ! -L "$demo_candidate" ] \
        || fail "install a reviewed demo candidate universe: $demo_candidate"
    install -d -o root -g root -m 0700 /etc/liquidity-migration
    chown root:root /etc/liquidity-migration/sleeves.resolved.env
    chmod 0600 /etc/liquidity-migration/sleeves.resolved.env
    write_producer_environment "$PRODUCER_DEMO_SOURCE_ENV" "$PRODUCER_DEMO_ENV"
    retire_paper_host_config

    [ ! -L "$REPO_DIR/data" ] || fail "demo runtime data directory must not be a symlink"
    for root in "$LONG_DEMO_ROOT" "$CARRY_DEMO_ROOT"; do
        case "$root" in
            "$REPO_DIR"/data/*) ;;
            *) fail "demo runtime root escapes the checkout data directory: $root" ;;
        esac
        [ ! -L "$root" ] || fail "demo runtime root must not be a symlink: $root"
        install -d -o "$PRODUCER_USER" -g "$RUNTIME_GROUP" -m 0750 "$root" \
            || fail "cannot create demo runtime root: $root"
        chown -R --no-dereference "$PRODUCER_USER:$RUNTIME_GROUP" "$root" \
            || fail "cannot assign demo runtime root: $root"
        chmod 0750 "$root" || fail "cannot secure demo runtime root: $root"
    done
    chown root:root "$PRODUCER_DEMO_SOURCE_ENV" /etc/liquidity-migration/bybit-demo.env
    chmod 0600 "$PRODUCER_DEMO_SOURCE_ENV" /etc/liquidity-migration/bybit-demo.env
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

# STOP_FIRST=auto resolves to "stop" unless a funded unit is actually
# running: bouncing a live mainnet owner mid-order is the one stop that is
# never automatic. An armed switch with nothing funded running keeps the
# demo fleet's normal auto-cycle.
resolve_stop_first() {
    [ "$STOP_FIRST" = auto ] || return 0
    if mainnet_armed && running_liqmig_units | grep -q -- '-mainnet'; then
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

    run_phase install-runtime-identities ensure_runtime_identities

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
    lm_load_sleeve_toggles
    # The writer is atomic (mktemp + mv), so re-reading what this process just
    # wrote proves nothing; load_authorization validates the file a *previous*
    # process wrote, which is the read that can actually disagree.
    lm_write_resolved_sleeve_toggles
    prepare_demo_runtime_config
    require_clean_head
    run_phase engine-build build_engine
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

# Rust is the only account owner. Every deployed topology requires the demo
# engine, and an armed topology also requires the separately credentialed
# funded engine. Missing binaries/configuration are fatal rather than an
# implicit opt-out.
ENGINE_UNIT=liquidity-migration-engine.service
# Built in a clone of its own, never the deployed checkout: cargo writes a
# target/ tree beside the source, and the deployed checkout is proved clean
# against the exact commit at several points in this script.
ENGINE_BUILD_DIR=/opt/engine-build
ENGINE_TOOLCHAIN_DIR=/opt/rust
ENGINE_BINARY=/opt/liquidity-migration-engine/bin/engine
ENGINE_ENVIRONMENT=/etc/liquidity-migration/engine.env

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
            # This status read is deliberately non-mutating. Provisioning and
            # disarm own permission changes; verify never repairs a credential.
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

rollout_flat_check() {
    local head_binding="$1" realm
    case "$head_binding" in
        exact|allow_behind|none|stopped-maintenance) ;;
        *) fail "invalid rollout head binding" ;;
    esac
    if mainnet_armed; then realm=mainnet; else realm=demo; fi
    # There is intentionally no configured-symbol fallback here. The current
    # engine AccountView can omit unknown or delisted residual positions, so it
    # cannot establish venue-global flatness. Until an independently reviewed,
    # credential-isolated venue-global attestation verifier is shipped, every
    # funded (and every --require-flat) rollout stops before producers, owners,
    # target books, checkout, or LONG schema-v2 state are changed.
    printf '%s\n' \
        "venue-global-flat-attestation-unavailable realm=$realm binding=$head_binding; refusing generation-changing rollout" >&2
    return 3
}
verify_topology() {
    VERIFY_UNIT_ROWS=()
    VERIFY_MISMATCHES=()

    # Rust is the only account owner. Every deployed topology requires the demo
    # engine, and an armed topology additionally requires the funded engine.
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
        verify_unit on "$MAINNET_OWNER_UNIT" "funded Rust engine is not active"
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
            liquidity-migration-engine-mainnet.service \
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
    if systemctl cat liquidity-migration-llm-ledger.timer >/dev/null 2>&1; then
        verify_unit on liquidity-migration-llm-ledger.timer "LLM ledger timer is not active"
    fi
    if systemctl cat liquidity-migration-trade-notify.timer >/dev/null 2>&1; then
        verify_unit on liquidity-migration-trade-notify.timer "trade notify timer is not active"
    fi
    verify_unit on "$ENGINE_UNIT" "required demo Rust engine is not active"
    if [ ! -x "$ENGINE_BINARY" ] || [ ! -r "${ENGINE_BINARY}.release" ]; then
        verify_note "required commit-bound Rust engine artifact is missing"
    else
        local marker_commit marker_digest actual_digest
        marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
        marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
        actual_digest="$(sha256sum "$ENGINE_BINARY" | awk '{print $1}' || true)"
        [ "$marker_commit" = "$EXPECTED_COMMIT" ] \
            || verify_note "engine artifact is not bound to requested commit $EXPECTED_COMMIT"
        [ -n "$marker_digest" ] && [ "$marker_digest" = "$actual_digest" ] \
            || verify_note "engine artifact digest does not match its release marker"
    fi
    for oneshot in \
        liquidity-migration-demo-liveness.service; do
        if systemctl is-failed --quiet "$oneshot"; then
            verify_note "$oneshot is failed"
        fi
    done

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

# Read-only Git against the engine's own build clone. Same hardened invocation
# as the deployed checkout's, pointed somewhere else.
engine_git() {
    "${GIT_ENV[@]}" /usr/bin/git --no-pager --no-optional-locks \
        --git-dir="$ENGINE_BUILD_DIR/.git" --work-tree="$ENGINE_BUILD_DIR" \
        -c "safe.directory=$ENGINE_BUILD_DIR" \
        -c core.fsmonitor=false -c core.hooksPath=/dev/null \
        -C "$ENGINE_BUILD_DIR" "$@"
}

# Build the exact locked release while the managed units are stopped. Any
# compiler, fetch, build, install, or digest failure aborts the deployment.
build_engine() {
    local cargo="$ENGINE_TOOLCHAIN_DIR/cargo/bin/cargo"
    local rustc="$ENGINE_TOOLCHAIN_DIR/cargo/bin/rustc"
    local commit built digest marker_tmp status=0
    [ -x "$cargo" ] || fail "pinned Rust toolchain is missing: $cargo"
    [ -x "$rustc" ] || fail "pinned rustc is missing: $rustc"
    "$rustc" --version | grep -F 'rustc 1.90.0 ' >/dev/null \
        || fail "host Rust compiler does not match rust-toolchain.toml (required 1.90.0)"
    commit="$(safe_git rev-parse HEAD)" || fail "cannot read installed commit for engine build"
    [ "$commit" = "$EXPECTED_COMMIT" ] || fail "engine build commit is not the requested commit"
    if [ ! -d "$ENGINE_BUILD_DIR/.git" ]; then
        "${GIT_ENV[@]}" /usr/bin/git init --quiet "$ENGINE_BUILD_DIR" \
            || fail "cannot prepare engine build clone"
    fi
    engine_git fetch --no-tags --quiet "$REPO_DIR" HEAD \
        || fail "cannot copy the deployed commit into the engine build clone"
    engine_git reset --hard --quiet FETCH_HEAD \
        || fail "cannot reset the engine build clone to the deployed commit"
    built="$(engine_git rev-parse HEAD)" || fail "cannot read engine build clone HEAD"
    [ "$built" = "$commit" ] || fail "engine build clone is $built, not $commit"
    (
        CARGO_HOME="$ENGINE_TOOLCHAIN_DIR/cargo"
        RUSTUP_HOME="$ENGINE_TOOLCHAIN_DIR/rustup"
        PATH="$ENGINE_TOOLCHAIN_DIR/cargo/bin:$PATH"
        export CARGO_HOME RUSTUP_HOME PATH
        cd "$ENGINE_BUILD_DIR/engine"
        nice -n 19 "$cargo" build --release --locked -j 1
    ) || status=$?
    [ "$status" -eq 0 ] || fail "locked release engine build failed (status $status)"
    install -d -o root -g liquidity-migration -m 0755 "${ENGINE_BINARY%/*}" \
        || fail "cannot create the engine binary directory"
    install -o root -g liquidity-migration -m 0755 \
        "$ENGINE_BUILD_DIR/engine/target/release/engine" "$ENGINE_BINARY.new" \
        || fail "cannot stage the release engine binary"
    digest="$(sha256sum "$ENGINE_BINARY.new" | awk '{print $1}')" \
        || fail "cannot digest the staged engine binary"
    [ "${#digest}" -eq 64 ] || fail "invalid staged engine digest"
    mv -f "$ENGINE_BINARY.new" "$ENGINE_BINARY" \
        || fail "cannot atomically install the engine binary"
    marker_tmp="${ENGINE_BINARY}.release.tmp.$$"
    printf 'commit=%s\nsha256=%s\nrustc=1.90.0\n' "$commit" "$digest" > "$marker_tmp" \
        || fail "cannot write engine release marker"
    chown root:root "$marker_tmp" && chmod 0644 "$marker_tmp" \
        || fail "cannot secure engine release marker"
    mv -f "$marker_tmp" "${ENGINE_BINARY}.release" \
        || fail "cannot atomically install engine release marker"
    verify_engine_release
    printf 'engine-build-ok commit=%s sha256=%s binary=%s\n' "$commit" "$digest" "$ENGINE_BINARY"
}

verify_engine_release() {
    local installed_head marker_commit marker_digest actual_digest
    [ -x "$ENGINE_BINARY" ] || fail "required engine binary is missing: $ENGINE_BINARY"
    [ -r "${ENGINE_BINARY}.release" ] || fail "engine release marker is missing"
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
    marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
    [ -n "$marker_commit" ] && [ "$marker_commit" = "$installed_head" ] \
        || fail "engine release marker is not bound to installed commit $installed_head"
    actual_digest="$(sha256sum "$ENGINE_BINARY" | awk '{print $1}')" \
        || fail "cannot digest installed engine binary"
    [ -n "$marker_digest" ] && [ "$marker_digest" = "$actual_digest" ] \
        || fail "installed engine digest does not match its release marker"
}
validate_engine_environment() {
    local env_file="$1" expected_realm="$2" expected_config_venue
    [ -f "$env_file" ] && [ ! -L "$env_file" ] \
        || fail "required Rust engine environment is missing or linked: $env_file"
    chown root:root "$env_file" && chmod 0600 "$env_file" \
        || fail "cannot secure engine environment $env_file"
    unset ENGINE_CONFIG_FILE LIVENESS_ENGINE_HEARTBEAT_FILE \
        EXPECTED_ENGINE_ACCOUNT_USER_ID EXPECTED_ENGINE_VENUE EXPECTED_ENGINE_REALM \
        EXPECTED_ENGINE_VERSION
    lm_load_private_systemd_environment "$PYTHON" "$env_file" \
        ENGINE_CONFIG_FILE LIVENESS_ENGINE_HEARTBEAT_FILE \
        EXPECTED_ENGINE_ACCOUNT_USER_ID EXPECTED_ENGINE_VENUE EXPECTED_ENGINE_REALM \
        EXPECTED_ENGINE_VERSION
    [ -n "${EXPECTED_ENGINE_ACCOUNT_USER_ID:-}" ] \
        || fail "$env_file must set EXPECTED_ENGINE_ACCOUNT_USER_ID to the exact venue id"
    [[ "$EXPECTED_ENGINE_ACCOUNT_USER_ID" =~ ^[A-Za-z0-9._:-]{1,128}$ ]] \
        || fail "$env_file contains an invalid EXPECTED_ENGINE_ACCOUNT_USER_ID"
    [ "${EXPECTED_ENGINE_VENUE:-}" = bybit ] \
        || fail "$env_file must bind EXPECTED_ENGINE_VENUE=bybit"
    [ "${EXPECTED_ENGINE_REALM:-}" = "$expected_realm" ] \
        || fail "$env_file does not bind the expected $expected_realm realm"
    case "$expected_realm" in
        demo) expected_config_venue=bybit_demo ;;
        mainnet) expected_config_venue=bybit_mainnet ;;
        *) fail "unsupported engine realm $expected_realm" ;;
    esac
    [ -f "$ENGINE_CONFIG_FILE" ] && [ ! -L "$ENGINE_CONFIG_FILE" ] \
        || fail "engine config is missing or linked: $ENGINE_CONFIG_FILE"
    "$PYTHON" - "$ENGINE_CONFIG_FILE" "$LIVENESS_ENGINE_HEARTBEAT_FILE" \
        "$expected_config_venue" "$expected_realm" <<'PY'
import sys
import tomllib
from pathlib import Path
config_path = Path(sys.argv[1])
expected_heartbeat = Path(sys.argv[2])
expected_venue = sys.argv[3]
realm = sys.argv[4]
if not expected_heartbeat.is_absolute():
    raise SystemExit("engine heartbeat path must be absolute")
with config_path.open("rb") as handle:
    config = tomllib.load(handle)
engine = config.get("engine") or {}
if engine.get("venue") != expected_venue:
    raise SystemExit(f"engine config venue is {engine.get('venue')!r}, expected {expected_venue!r}")
if Path(str(engine.get("heartbeat_path") or "")) != expected_heartbeat:
    raise SystemExit("engine config heartbeat_path disagrees with LIVENESS_ENGINE_HEARTBEAT_FILE")
expected_books = {
    "demo": {
        "carry": Path("/var/lib/liquidity-migration/targets/carry-demo.json"),
        "long": Path("/var/lib/liquidity-migration/targets/long-demo.json"),
        "exodus": Path("/var/lib/liquidity-migration/targets/exodus-demo.json"),
    },
    "mainnet": {
        "carry": Path("/var/lib/liquidity-migration/targets/carry-mainnet.json"),
        "long": Path("/var/lib/liquidity-migration/targets/long-mainnet.json"),
    },
}[realm]
observed = {
    str(row.get("sleeve")): Path(str(row.get("book_path") or ""))
    for row in config.get("strategy", [])
    if row.get("name") == "target_book"
}
for sleeve, expected in expected_books.items():
    if observed.get(sleeve) != expected:
        raise SystemExit(f"{sleeve} book_path is {observed.get(sleeve)!s}, expected {expected}")
PY
    chown root:"$RUNTIME_GROUP" "$ENGINE_CONFIG_FILE" && chmod 0640 "$ENGINE_CONFIG_FILE" \
        || fail "cannot make engine config readable only to its runtime group"
}

quarantine_engine_inputs() {
    local env_file="$1" realm="$2" archive stamp path
    validate_engine_environment "$env_file" "$realm"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    archive="/var/lib/liquidity-migration/targets/archive/$stamp-$realm"
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$archive"
    case "$realm" in
        demo) set -- long-demo.json carry-demo.json exodus-demo.json ;;
        mainnet) set -- long-mainnet.json carry-mainnet.json ;;
    esac
    for path in "$@"; do
        if [ -e "/var/lib/liquidity-migration/targets/$path" ]; then
            [ ! -L "/var/lib/liquidity-migration/targets/$path" ] \
                || fail "refusing linked target book: $path"
            mv "/var/lib/liquidity-migration/targets/$path" "$archive/$path"
        fi
    done
    if [ -e "$LIVENESS_ENGINE_HEARTBEAT_FILE" ]; then
        [ ! -L "$LIVENESS_ENGINE_HEARTBEAT_FILE" ] \
            || fail "refusing linked engine heartbeat"
        mv "$LIVENESS_ENGINE_HEARTBEAT_FILE" "$LIVENESS_ENGINE_HEARTBEAT_FILE.pre-activation-$stamp"
    fi
}

wait_engine_heartbeat() {
    local env_file="$1" realm="$2" attempt unit
    validate_engine_environment "$env_file" "$realm"
    if [ "$realm" = mainnet ]; then unit="$MAINNET_OWNER_UNIT"; else unit="$ENGINE_UNIT"; fi
    for attempt in $(seq 1 60); do
        if systemctl is-active --quiet "$unit" 2>/dev/null \
            && "$PYTHON" - "$LIVENESS_ENGINE_HEARTBEAT_FILE" \
                "$EXPECTED_ENGINE_ACCOUNT_USER_ID" "$EXPECTED_ENGINE_VENUE" \
                "$EXPECTED_ENGINE_REALM" "${EXPECTED_ENGINE_VERSION:-}" <<'PY'
import json
import sys
import time
from pathlib import Path
path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_bytes())
except (OSError, ValueError):
    raise SystemExit(1)
expected_account, expected_venue, expected_realm, expected_version = sys.argv[2:]
for key, expected in (
    ("account_user_id", expected_account),
    ("venue", expected_venue),
    ("realm", expected_realm),
):
    if payload.get(key) != expected:
        raise SystemExit(1)
if expected_version and payload.get("engine_version") != expected_version:
    raise SystemExit(1)
wall = payload.get("wall_ts_ms")
observed = payload.get("account_observed_wall_ts_ms")
if type(wall) is not int or not (0 <= time.time() * 1000 - wall <= 15_000):
    raise SystemExit(1)
if type(observed) is not int or not (0 <= wall - observed <= 60_000):
    raise SystemExit(1)
if payload.get("mode") != "live" or payload.get("may_open") is not True:
    raise SystemExit(1)
PY
        then
            printf 'engine-heartbeat-ok realm=%s account=%s path=%s\n' \
                "$realm" "$EXPECTED_ENGINE_ACCOUNT_USER_ID" "$LIVENESS_ENGINE_HEARTBEAT_FILE"
            return 0
        fi
        [ "$attempt" -eq 60 ] || sleep 2
    done
    fail "$realm engine did not publish a fresh, may-open, exact-account heartbeat"
}

wait_fresh_producer_book() {
    local unit="$1" root="$2" book="$3" realm="$4" started_ns="$5"
    local invocation attempt
    invocation="$(systemctl show "$unit" --property=InvocationID --value --no-pager)" \
        || fail "cannot read $unit invocation id"
    [ -n "$invocation" ] || fail "$unit has no systemd invocation id"
    for attempt in $(seq 1 450); do
        systemctl is-active --quiet "$unit" \
            || fail "$unit stopped before publishing a fresh target book"
        if "$PYTHON" - "$root" "$book" "$realm" "$started_ns" "$invocation" <<'PY'
import json
import sys
import time
from pathlib import Path
root, book = Path(sys.argv[1]), Path(sys.argv[2])
realm, started_ns, invocation = sys.argv[3], int(sys.argv[4]), sys.argv[5]
try:
    health_path = root / ".cache" / "strategy_cycle_health.json"
    health = json.loads(health_path.read_bytes())
    payload = json.loads(book.read_bytes())
    stat = book.stat()
except (OSError, ValueError):
    raise SystemExit(1)
if health.get("invocation_id") != invocation or health.get("environment") != realm:
    raise SystemExit(1)
if type(health.get("completed_ts_ns")) is not int or health["completed_ts_ns"] < started_ns:
    raise SystemExit(1)
if stat.st_mtime_ns < started_ns:
    raise SystemExit(1)
now_ms = int(time.time() * 1000)
decision = payload.get("decision_ts_ms")
valid_until = payload.get("valid_until_ms")
if type(decision) is not int or type(valid_until) is not int:
    raise SystemExit(1)
if decision > now_ms + 5_000 or valid_until <= now_ms:
    raise SystemExit(1)
if not isinstance(payload.get("targets"), list):
    raise SystemExit(1)
PY
        then
            printf 'producer-book-ok unit=%s invocation=%s book=%s\n' "$unit" "$invocation" "$book"
            return 0
        fi
        [ "$attempt" -eq 450 ] || sleep 2
    done
    fail "$unit did not publish a valid book from its current service generation"
}

start_required_engine() {
    local unit="$1" env_file="$2" realm="$3"
    verify_engine_release
    quarantine_engine_inputs "$env_file" "$realm"
    systemctl enable "$unit" || fail "cannot enable required Rust engine $unit"
    systemctl start "$unit" || fail "cannot start required Rust engine $unit"
    wait_engine_heartbeat "$env_file" "$realm"
}
activate_mode() {
    local producer_started_ns
    load_authorization
    resolve_stop_first
    require_quiescent

    for unit in $(lm_expected_systemd_units); do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    # Remove every previously valid book before the engine starts. It therefore
    # boots holding/no-op, proves the exact account in a new heartbeat, and only
    # then admits books from the service generations started below.
    start_required_engine "$ENGINE_UNIT" "$ENGINE_ENVIRONMENT" demo
    producer_started_ns="$(date +%s%N)"
    start_if "$LONG_SLEEVE" liquidity-migration-bybit-long-demo.service
    start_if "$CARRY_SLEEVE" liquidity-migration-bybit-carry-demo.service
    if sleeve_on "$LONG_SLEEVE"; then
        wait_fresh_producer_book liquidity-migration-bybit-long-demo.service \
            "$LONG_DEMO_ROOT" /var/lib/liquidity-migration/targets/long-demo.json \
            demo "$producer_started_ns"
    fi
    if sleeve_on "$CARRY_SLEEVE"; then
        wait_fresh_producer_book liquidity-migration-bybit-carry-demo.service \
            "$CARRY_DEMO_ROOT" /var/lib/liquidity-migration/targets/carry-demo.json \
            demo "$producer_started_ns"
    fi

    systemctl enable --now liquidity-migration-demo-liveness.timer \
        || fail "cannot enable the demo watchdog timer"
    systemctl start liquidity-migration-demo-liveness.service \
        || fail "the immediate demo liveness pass failed to start"
    systemctl is-failed --quiet liquidity-migration-demo-liveness.service \
        && fail "the immediate demo liveness pass failed"
    systemctl enable --now liquidity-migration-telegram-controls.service \
        || fail "cannot start Telegram controls"
    systemctl enable --now liquidity-migration-llm-ledger.timer \
        || fail "cannot start the LLM ledger timer"
    systemctl enable --now liquidity-migration-trade-notify.timer \
        || fail "cannot start the trade notification timer"
    if mainnet_armed; then
        start_mainnet_fleet
    fi
    verify_topology
}
# The engine owns the funded account.
#
# Naming the engine is not arming it. The mainnet gateway refuses to build
# unless REAL_MONEY is set in the host credential file by the account owner,
# so the worst this can do is start a process that reads and reports.
MAINNET_OWNER_UNIT=liquidity-migration-engine-mainnet.service
MAINNET_LIVENESS_TIMER=liquidity-migration-mainnet-liveness.timer
MAINNET_LIVENESS_SERVICE=liquidity-migration-mainnet-liveness.service

MAINNET_DEMO_TELEGRAM_ENV=/etc/liquidity-migration/bybit-demo.env

# The owner writes one file: the credential env (key, secret, REAL_MONEY,
# optional dials). Everything else is derived here at activation, and
# preflight still gates below.
provision_mainnet_prerequisites() {
    if [ ! -f "$PRODUCER_MAINNET_SOURCE_ENV" ]; then
        install -o root -g root -m 600 \
            "$REPO_DIR/deploy/producer-mainnet-source.env.template" "$PRODUCER_MAINNET_SOURCE_ENV" \
            || fail "cannot install the mainnet producer source env"
        echo "provision: installed $PRODUCER_MAINNET_SOURCE_ENV from the committed template"
    fi
    chown root:root "$MAINNET_CREDENTIAL_ENV" "$PRODUCER_MAINNET_SOURCE_ENV" 2>/dev/null || true
    chmod 600 "$MAINNET_CREDENTIAL_ENV" "$PRODUCER_MAINNET_SOURCE_ENV" 2>/dev/null || true
    # A funded book that cannot page is a hazard: default a missing Telegram
    # pair from the demo file (existing values are never touched).
    "$PYTHON" -m liquidity_migration.policy.real_money_arming default-telegram \
        --from-env "$MAINNET_DEMO_TELEGRAM_ENV" --execute \
        || fail "cannot default the mainnet Telegram pair"
    local risk_policy_file="" universe_file=""

    lm_load_private_systemd_environment "$PYTHON" "$PRODUCER_MAINNET_SOURCE_ENV" \
        PRODUCER_REALM OPERATIONAL_PROFILE_FILE CANDIDATE_UNIVERSE_FILE \
        || fail "cannot read producer inputs from $PRODUCER_MAINNET_SOURCE_ENV"
    [ "$PRODUCER_REALM" = mainnet ] \
        || fail "funded producer source must declare PRODUCER_REALM=mainnet"
    risk_policy_file="$OPERATIONAL_PROFILE_FILE"
    universe_file="$CANDIDATE_UNIVERSE_FILE"

    mkdir -p "$(dirname "$risk_policy_file")"
    chmod 700 "$(dirname "$risk_policy_file")" 2>/dev/null || true
    # The installed profile is always the render of the current dials, so a
    # dial edit can never drift from what the kernel enforces.
    "$PYTHON" -m liquidity_migration.policy.real_money_arming render-profile \
        --execute --overwrite --output "$risk_policy_file" \
        || fail "mainnet dials do not render a loadable profile"
    [ -f "$universe_file" ] && [ ! -L "$universe_file" ] \
        || fail "install a reviewed mainnet candidate-universe artifact before activation: $universe_file"
    write_producer_environment "$PRODUCER_MAINNET_SOURCE_ENV" "$PRODUCER_MAINNET_ENV"
    project_mainnet_telegram_environment
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
    local producer_started_ns root
    provision_mainnet_prerequisites
    require_mainnet_preflight
    install -d -o "$PRODUCER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        "$LONG_MAINNET_ROOT" "$CARRY_MAINNET_ROOT" /var/lib/liquidity-migration/targets
    for root in "$LONG_MAINNET_ROOT" "$CARRY_MAINNET_ROOT"; do
        chown -R --no-dereference "$PRODUCER_USER:$RUNTIME_GROUP" "$root" \
            || fail "cannot assign mainnet producer state to $PRODUCER_USER: $root"
    done
    start_required_engine "$MAINNET_OWNER_UNIT" /etc/liquidity-migration/engine-mainnet.env mainnet
    producer_started_ns="$(date +%s%N)"
    systemctl enable --now liquidity-migration-bybit-carry-mainnet.service \
        || fail "cannot start the funded carry target producer"
    systemctl enable --now liquidity-migration-bybit-long-mainnet.service \
        || fail "cannot start the funded LONG target producer"
    wait_fresh_producer_book liquidity-migration-bybit-carry-mainnet.service \
        "$CARRY_MAINNET_ROOT" /var/lib/liquidity-migration/targets/carry-mainnet.json \
        mainnet "$producer_started_ns"
    wait_fresh_producer_book liquidity-migration-bybit-long-mainnet.service \
        "$LONG_MAINNET_ROOT" /var/lib/liquidity-migration/targets/long-mainnet.json \
        mainnet "$producer_started_ns"
    systemctl enable --now "$MAINNET_LIVENESS_TIMER" \
        || fail "cannot enable the funded liveness timer"
    systemctl start "$MAINNET_LIVENESS_SERVICE" \
        || fail "the immediate funded liveness pass failed to start"
    systemctl is-failed --quiet "$MAINNET_LIVENESS_SERVICE" \
        && fail "the immediate funded liveness pass failed"
}
disarm_mainnet_mode() {
    require_checkout
    PYTHON=.venv/bin/python
    [ -x "$PYTHON" ] || fail "missing deployed Python environment"
    [ -f "$MAINNET_CREDENTIAL_ENV" ] && [ ! -L "$MAINNET_CREDENTIAL_ENV" ] \
        || fail "cannot disarm without the real private credential file: $MAINNET_CREDENTIAL_ENV"
    # Replace the owner file before stopping processes. A concurrent restart
    # therefore reads false and refuses mainnet even if this command is
    # interrupted between the atomic replace and systemctl stop.
    "$PYTHON" - "$MAINNET_CREDENTIAL_ENV" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment
path = Path(sys.argv[1])
values = load_private_systemd_environment(path)
values["REAL_MONEY"] = "false"
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(str(value))}\n")
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
    chown root:root "$MAINNET_CREDENTIAL_ENV" && chmod 0600 "$MAINNET_CREDENTIAL_ENV" \
        || fail "cannot secure the atomically disarmed credential file"
    MAINNET_ARMED_STATE=off
    local unit
    for unit in \
        "$MAINNET_LIVENESS_TIMER" "$MAINNET_LIVENESS_SERVICE" \
        liquidity-migration-bybit-carry-mainnet.service \
        liquidity-migration-bybit-long-mainnet.service \
        "$MAINNET_OWNER_UNIT"; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl disable --now "$unit" \
                || fail "failed to stop or disable disarmed unit: $unit"
            ! systemctl is-active --quiet "$unit" \
                || fail "disarmed unit remained active: $unit"
            ! systemctl is-enabled --quiet "$unit" 2>/dev/null \
                || fail "disarmed unit remained enabled: $unit"
        fi
    done
    echo "disarm-mainnet-ok real_money=false units=inactive-and-disabled"
    echo "note: disarm does not flatten existing exposure; reconcile/flatten separately"
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
    liquidity-migration-llm-ledger.timer
    liquidity-migration-llm-ledger.service
    liquidity-migration-trade-notify.timer
    liquidity-migration-trade-notify.service
    liquidity-migration-bybit-long-demo.service
    liquidity-migration-bybit-long-mainnet.service
    liquidity-migration-bybit-carry-demo.service
    liquidity-migration-bybit-carry-mainnet.service
    liquidity-migration-demo-liveness.service
    liquidity-migration-mainnet-liveness.service
    liquidity-migration-telegram-controls.service
    # Retired fleets stay in the stop list so the rollout that carries each
    # retirement quiesces a host still running them; the manifest install
    # then removes the unit files for good.
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
# Owners stop last and start first. The engines are the owners now: they hold
# the account lease and carry the orders, and a producer publishing a book
# nobody owns is the state this ordering exists to keep short. They are also
# what `require_quiescent` needs named, since it refuses to install while any
# liquidity-migration-* unit is running and stop-first only stops what these
# lists name.
ROLLOUT_OWNER_UNITS=(
    liquidity-migration-engine.service
    liquidity-migration-engine-mainnet.service
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
    run_strict_phase stopped-install install_mode
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
    disarm-mainnet) disarm_mainnet_mode ;;
    verify) load_authorization; verify_topology ;;
    rollout) rollout_mode ;;
    # Without this an unknown mode silently succeeded having done nothing.
    *) fail "unknown deploy mode: $MODE" ;;
esac
REMOTE_SCRIPT
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
