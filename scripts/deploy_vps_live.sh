#!/usr/bin/env bash
# Staged VPS lifecycle plus a one-command rollout.
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
USAGE
    exit 2
}

DEPLOY_PROFILE=""
STOP_FIRST=auto

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

SSH_TARGET="${SSH_TARGET:-root@208.84.103.4}"
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
	cat <<'REMOTE_SCRIPT'
# `-E` propagates the ERR trap into shell functions so a strict phase can still
# report which phase died; see run_strict_phase below.
set -Eeuo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022
readonly DISARM_MAINTENANCE_LOCK_WAIT_SECONDS=120

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

acquire_maintenance_fd() {
    local descriptor="$1" label="$2" disarm_deadline="$3" remaining_seconds
    case "$disarm_deadline" in
        ''|*[!0-9]*) fail "invalid maintenance lock deadline" ;;
    esac
    if [ "$MODE" = disarm-mainnet ]; then
        remaining_seconds=$((disarm_deadline - SECONDS))
        [ "$remaining_seconds" -gt 0 ] \
            || fail "disarm maintenance lock wait expired before $label"
        flock --exclusive --timeout "$remaining_seconds" "$descriptor" \
            || fail "disarm timed out after ${DISARM_MAINTENANCE_LOCK_WAIT_SECONDS}s waiting for canceled maintenance cleanup at $label"
        return 0
    fi
    flock --exclusive --nonblock "$descriptor" \
        || fail "another maintenance operation holds $label"
}

acquire_maintenance_locks() {
    local disarm_deadline=0
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
    if [ "$MODE" = disarm-mainnet ]; then
        disarm_deadline=$((SECONDS + DISARM_MAINTENANCE_LOCK_WAIT_SECONDS))
    fi
    acquire_maintenance_fd 9 maintenance.lock "$disarm_deadline"
    acquire_maintenance_fd 8 legacy-deploy.lock "$disarm_deadline"
    acquire_maintenance_fd 7 legacy-reset.lock "$disarm_deadline"
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
ENGINE_BUILDER_GROUP=liquidity-builder
ENGINE_BUILDER_USER=liquidity-builder
CONTROLS_GROUP=liquidity-controls
CONTROLS_USER=liquidity-controls
PRODUCER_USER=liquidity-producer
DEMO_ENGINE_USER=liquidity-engine-demo
MAINNET_ENGINE_USER=liquidity-engine-mainnet
OBSERVER_USER=liquidity-observer
LLM_USER=liquidity-llm
LLM_STATE_ROOT=/var/lib/liquidity-migration/llm-driver-ledger
CAPTURE_USER=liquidity-capture
CAPTURE_STATE_ROOT=/var/lib/liquidity-migration/forward-market
LLM_GATE_CANDIDATES_PATH="$LLM_STATE_ROOT/llm-gate-candidates.json"
LEGACY_LLM_GATE_CANDIDATES_PATH=/var/lib/liquidity-migration/targets/llm-gate-candidates.json
PRODUCER_DEMO_ENV=/etc/liquidity-migration/producer-demo.env
PRODUCER_MAINNET_ENV=/etc/liquidity-migration/producer-mainnet.env
PRODUCER_DEMO_SOURCE_ENV=/etc/liquidity-migration/producer-demo-source.env
PRODUCER_MAINNET_SOURCE_ENV=/etc/liquidity-migration/producer-mainnet-source.env
MAINNET_TELEGRAM_ENV=/etc/liquidity-migration/telegram-mainnet.env

ensure_engine_builder_identity() {
    getent group "$ENGINE_BUILDER_GROUP" >/dev/null 2>&1 \
        || groupadd --system "$ENGINE_BUILDER_GROUP"
    if ! id -u "$ENGINE_BUILDER_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --gid "$ENGINE_BUILDER_GROUP" "$ENGINE_BUILDER_USER"
    fi
    [ "$(id -gn "$ENGINE_BUILDER_USER")" = "$ENGINE_BUILDER_GROUP" ] \
        || fail "$ENGINE_BUILDER_USER does not have its isolated primary group"
    [ "$(id -nG "$ENGINE_BUILDER_USER")" = "$ENGINE_BUILDER_GROUP" ] \
        || fail "$ENGINE_BUILDER_USER has unexpected supplementary groups"
}

normalize_account_lease_access() {
    local lease links
    local -a leases=()
    while IFS= read -r -d '' lease; do
        leases+=("$lease")
    done < <(
        find /run/lock/liquidity-migration -mindepth 1 -maxdepth 1 \
            -name '*-user-*.lock' -print0
    )
    for lease in "${leases[@]}"; do
        [ -f "$lease" ] && [ ! -L "$lease" ] \
            || fail "account lease path is not a regular file: $lease"
        links="$(stat -c %h -- "$lease")" \
            || fail "cannot inspect account lease links: $lease"
        [ "$links" -eq 1 ] \
            || fail "account lease has more than one name: $lease"
        chown root:"$RUNTIME_GROUP" -- "$lease" && chmod 0660 -- "$lease" \
            || fail "cannot grant the isolated engine users access to $lease"
    done
}

normalize_runtime_state_access() {
    "$PYTHON" - "$RUNTIME_GROUP" \
        "$DEMO_ENGINE_USER" /var/lib/liquidity-migration-engine \
        "$MAINNET_ENGINE_USER" /var/lib/liquidity-migration-engine-mainnet \
        "$PRODUCER_USER" "$LONG_DEMO_ROOT" \
        "$PRODUCER_USER" "$CARRY_DEMO_ROOT" \
        "$PRODUCER_USER" "$LONG_MAINNET_ROOT" \
        "$PRODUCER_USER" "$CARRY_MAINNET_ROOT" \
        "$LLM_USER" "$LLM_STATE_ROOT" <<'PY'
import grp
import os
import pwd
import stat
import sys


def refuse(detail: str) -> None:
    raise RuntimeError(detail)


def node_kind(row: os.stat_result, display: str, device: int) -> str:
    if row.st_dev != device:
        refuse(f"runtime state crosses a filesystem at {display!r}")
    if row.st_mode & stat.S_ISUID or (
        row.st_mode & stat.S_ISGID and not stat.S_ISDIR(row.st_mode)
    ):
        refuse(f"runtime state carries file privilege bits at {display!r}")
    if stat.S_ISLNK(row.st_mode):
        refuse(f"runtime state path is linked: {display!r}")
    if stat.S_ISDIR(row.st_mode):
        return "directory"
    if stat.S_ISREG(row.st_mode):
        if row.st_nlink != 1:
            refuse(f"runtime state file has more than one name: {display!r}")
        return "file"
    refuse(f"runtime state path has an unsupported type: {display!r}")
    raise AssertionError("unreachable")


def directory_names(descriptor: int, display: str) -> list[str]:
    try:
        return sorted(os.listdir(descriptor))
    except OSError as error:
        refuse(f"cannot enumerate runtime state {display!r}: {error}")
    raise AssertionError("unreachable")


def open_directory(name: str, parent: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=parent)


def validate_directory(descriptor: int, display: str, device: int) -> None:
    names = directory_names(descriptor, display)
    for name in names:
        child_display = os.path.join(display, name)
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        kind = node_kind(before, child_display, device)
        if kind == "directory":
            child = open_directory(name, descriptor)
            try:
                opened = os.fstat(child)
                if not os.path.samestat(before, opened):
                    refuse(f"runtime state changed during validation: {child_display!r}")
                validate_directory(child, child_display, device)
            finally:
                os.close(child)
    if directory_names(descriptor, display) != names:
        refuse(f"runtime state names changed during validation: {display!r}")


def same_open_node(parent: int, name: str, opened: os.stat_result, display: str) -> None:
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not os.path.samestat(opened, current):
        refuse(f"runtime state path changed while open: {display!r}")


def migrate_directory(
    descriptor: int,
    display: str,
    device: int,
    owner: int,
    group: int,
) -> None:
    names = directory_names(descriptor, display)
    for name in names:
        child_display = os.path.join(display, name)
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        kind = node_kind(before, child_display, device)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        if kind == "directory":
            flags |= os.O_DIRECTORY
        child = os.open(name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(child)
            if not os.path.samestat(before, opened):
                refuse(f"runtime state changed before migration: {child_display!r}")
            if node_kind(opened, child_display, device) != kind:
                refuse(f"runtime state changed type: {child_display!r}")
            if kind == "directory":
                migrate_directory(child, child_display, device, owner, group)
                mode = stat.S_IMODE(os.fstat(child).st_mode) | stat.S_IRWXU
            else:
                mode = stat.S_IMODE(opened.st_mode) | stat.S_IRUSR | stat.S_IWUSR
            os.fchown(child, owner, group)
            os.fchmod(child, mode)
            same_open_node(descriptor, name, os.fstat(child), child_display)
        finally:
            os.close(child)
    if directory_names(descriptor, display) != names:
        refuse(f"runtime state names changed during migration: {display!r}")

    current = os.fstat(descriptor)
    if node_kind(current, display, device) != "directory":
        refuse(f"runtime state directory changed type: {display!r}")
    os.fchown(descriptor, owner, group)
    os.fchmod(descriptor, stat.S_IMODE(current.st_mode) | stat.S_IRWXU)


def migrate_root(owner_name: str, path: str, group: int) -> None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(before.st_mode):
        refuse(f"runtime state root is linked: {path!r}")
    if not stat.S_ISDIR(before.st_mode):
        refuse(f"runtime state root is not a directory: {path!r}")

    descriptor = open_directory(path)
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(before, opened):
            refuse(f"runtime state root changed before validation: {path!r}")
        device = opened.st_dev
        node_kind(opened, path, device)
        validate_directory(descriptor, path, device)
        owner = pwd.getpwnam(owner_name).pw_uid
        migrate_directory(descriptor, path, device, owner, group)
        current = os.lstat(path)
        if not os.path.samestat(os.fstat(descriptor), current):
            refuse(f"runtime state root changed while open: {path!r}")
    finally:
        os.close(descriptor)


try:
    group_name, *pairs = sys.argv[1:]
    if not pairs or len(pairs) % 2:
        refuse("runtime-state migration received invalid arguments")
    group_id = grp.getgrnam(group_name).gr_gid
    for offset in range(0, len(pairs), 2):
        migrate_root(pairs[offset], pairs[offset + 1], group_id)
except (KeyError, OSError, RuntimeError) as error:
    raise SystemExit(f"runtime-state migration refused: {error}") from None
PY
    [ "$?" -eq 0 ] || fail "cannot migrate runtime-state ownership"
}

migrate_legacy_llm_gate_candidates() {
    local source="$LEGACY_LLM_GATE_CANDIDATES_PATH"
    local target="$LLM_GATE_CANDIDATES_PATH"
    [ ! -L "${source%/*}" ] && [ ! -L "${target%/*}" ] \
        || fail "LLM candidate directory is linked"
    if [ -e "$source" ] || [ -L "$source" ]; then
        [ -f "$source" ] && [ ! -L "$source" ] \
            && [ "$(stat -c %h "$source")" -eq 1 ] \
            || fail "legacy LLM candidates are not a single regular file"
        [ ! -e "$target" ] && [ ! -L "$target" ] \
            || fail "both legacy and current LLM candidate files exist"
        mv -T -- "$source" "$target" \
            || fail "cannot move LLM candidates into their writer-owned state root"
    fi
}

ensure_runtime_identities() {
    [ -x /usr/bin/sudo ] && [ -x /usr/sbin/visudo ] \
        || fail "sudo and visudo are required for the isolated Telegram control boundary"
    getent group "$RUNTIME_GROUP" >/dev/null 2>&1 || groupadd --system "$RUNTIME_GROUP"
    ensure_engine_builder_identity
    getent group "$CONTROLS_GROUP" >/dev/null 2>&1 \
        || groupadd --system "$CONTROLS_GROUP"
    if ! id -u "$CONTROLS_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --gid "$CONTROLS_GROUP" "$CONTROLS_USER"
    fi
    [ "$(id -gn "$CONTROLS_USER")" = "$CONTROLS_GROUP" ] \
        && [ "$(id -nG "$CONTROLS_USER")" = "$CONTROLS_GROUP" ] \
        || fail "$CONTROLS_USER is not isolated in its dedicated primary group"
    local user
    for user in "$PRODUCER_USER" "$DEMO_ENGINE_USER" "$MAINNET_ENGINE_USER" "$OBSERVER_USER" "$LLM_USER" "$CAPTURE_USER"; do
        if ! id -u "$user" >/dev/null 2>&1; then
            useradd --system --no-create-home --home-dir /nonexistent \
                --shell /usr/sbin/nologin --gid "$RUNTIME_GROUP" "$user"
        fi
        id -nG "$user" | tr ' ' '\n' | grep -Fx "$RUNTIME_GROUP" >/dev/null \
            || fail "$user is not isolated in the $RUNTIME_GROUP runtime group"
    done
    install -d -o root -g root -m 0755 /etc/tmpfiles.d
    printf 'd /run/liquidity-migration 0755 root root -\nf /run/liquidity-migration/maintenance.lock 0600 root root -\nf /run/liquidity-migration/deploy.lock 0600 root root -\nd /run/lock/liquidity-migration 0770 root %s -\nf /run/lock/liquidity-migration-ledger-reset.lock 0600 root root -\n' "$RUNTIME_GROUP" \
        > /etc/tmpfiles.d/liquidity-migration.conf
    systemd-tmpfiles --create /etc/tmpfiles.d/liquidity-migration.conf \
        || fail "cannot create the runtime lock and engine lease boundaries"
    [ ! -L /var/lib/liquidity-migration/targets ] \
        || fail "producer target root is linked"
    install -d -o "$PRODUCER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/targets
    [ ! -L "$LLM_STATE_ROOT" ] || fail "LLM state root is linked"
    install -d -o "$LLM_USER" -g "$RUNTIME_GROUP" -m 0750 "$LLM_STATE_ROOT"
    [ ! -L "$CAPTURE_STATE_ROOT" ] || fail "forward-capture state root is linked"
    install -d -o "$CAPTURE_USER" -g "$RUNTIME_GROUP" -m 0750 "$CAPTURE_STATE_ROOT"
    migrate_legacy_llm_gate_candidates
    normalize_account_lease_access
    normalize_runtime_state_access
    normalize_producer_book_state_access
}

normalize_producer_book_state_access() {
    /usr/bin/python3 - "$PRODUCER_USER" "$RUNTIME_GROUP" \
        /var/lib/liquidity-migration/targets \
        long-demo-state.json long-mainnet-state.json <<'PY'
import grp
import os
import pwd
import stat
import sys

owner = pwd.getpwnam(sys.argv[1]).pw_uid
group = grp.getgrnam(sys.argv[2]).gr_gid
root_path = sys.argv[3]
root_before = os.lstat(root_path)
if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
    raise SystemExit(f"producer target root is not a real directory: {root_path!r}")
root = os.open(
    root_path,
    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
)
try:
    if not os.path.samestat(root_before, os.fstat(root)):
        raise SystemExit(f"producer target root changed before migration: {root_path!r}")
    for name in sys.argv[4:]:
        path = os.path.join(root_path, name)
        try:
            before = os.stat(name, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"producer book state is not a regular file: {path!r}")
        if before.st_nlink != 1:
            raise SystemExit(f"producer book state has multiple links: {path!r}")
        if before.st_size > 16 * 1024 * 1024:
            raise SystemExit(f"producer book state exceeds its reader limit: {path!r}")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=root,
        )
        try:
            opened = os.fstat(descriptor)
            if not os.path.samestat(before, opened):
                raise SystemExit(f"producer book state changed before migration: {path!r}")
            os.fchown(descriptor, owner, group)
            os.fchmod(descriptor, 0o640)
            current = os.stat(name, dir_fd=root, follow_symlinks=False)
            if not os.path.samestat(os.fstat(descriptor), current):
                raise SystemExit(f"producer book state changed during migration: {path!r}")
        finally:
            os.close(descriptor)
    if not os.path.samestat(os.fstat(root), os.lstat(root_path)):
        raise SystemExit(f"producer target root changed during migration: {root_path!r}")
finally:
    os.close(root)
PY
    [ "$?" -eq 0 ] || fail "cannot migrate producer book-state ownership"
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
    "LONG_NOTIONAL_MULTIPLIER",
    "OPERATIONAL_PROFILE_FILE", "PRODUCER_REALM",
}
values = load_private_systemd_environment(source)
forbidden = {
    "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET", "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET", "BYBIT_REAL_API_KEY_IP", "BYBIT_REAL_API_KEY_BACKUP_IP",
    "BYBIT_ATTEST_API_KEY", "BYBIT_ATTEST_API_SECRET", "BYBIT_ATTEST_API_KEY_IP",
    "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID", "REAL_MONEY", "TELEGRAM_BOT_TOKEN",
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

reconcile_demo_engine_environment() {
    local source="$REPO_DIR/deploy/engine.env.template"
    local target="$ENGINE_ENVIRONMENT"
    [ -f "$source" ] && [ ! -L "$source" ] \
        || fail "missing tracked demo engine environment: $source"
    "$PYTHON" - "$source" "$target" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.policy.systemd_environment import parse_systemd_environment_bytes

source = Path(sys.argv[1])
target = Path(sys.argv[2])
required = (
    "EXPECTED_ENGINE_ACCOUNT_USER_ID",
    "EXPECTED_ENGINE_VENUE",
    "EXPECTED_ENGINE_REALM",
)

source_snapshot = read_stable_file(
    source,
    label="tracked demo engine environment",
    reject_empty=True,
    max_bytes=1024 * 1024,
)
source_values = parse_systemd_environment_bytes(
    source_snapshot.data,
    label=str(source_snapshot.path),
)
expected = {key: source_values.get(key, "") for key in required}
invalid_template = [key for key, value in expected.items() if not value]
if invalid_template:
    raise SystemExit(
        "tracked demo engine environment lacks exact bindings: "
        + ", ".join(invalid_template)
    )

if os.path.lexists(target):
    target_snapshot = read_stable_file(
        target,
        label="installed demo engine environment",
        reject_empty=True,
        require_mode=0o600,
        require_owner=True,
        max_bytes=1024 * 1024,
    )
    installed = parse_systemd_environment_bytes(
        target_snapshot.data,
        label=str(target_snapshot.path),
    )
    conflicts = [
        key
        for key in required
        if key in installed and installed[key] != expected[key]
    ]
    if conflicts:
        raise SystemExit(
            "installed demo engine environment conflicts with tracked identity: "
            + ", ".join(conflicts)
        )
    missing = [key for key in required if key not in installed]
    if not missing:
        raise SystemExit(0)
    payload = target_snapshot.data
    if not payload.endswith(b"\n"):
        payload += b"\n"
    payload += b"\n# Deployment-reconciled exact demo engine identity.\n"
    payload += b"".join(
        f"{key}={shlex.quote(expected[key])}\n".encode("utf-8")
        for key in missing
    )
else:
    payload = source_snapshot.data

fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    with os.fdopen(fd, "wb") as handle:
        if os.name != "nt":
            os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    if os.name != "nt":
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise

installed_snapshot = read_stable_file(
    target,
    label="reconciled demo engine environment",
    reject_empty=True,
    require_mode=0o600,
    require_owner=True,
    max_bytes=1024 * 1024,
)
installed = parse_systemd_environment_bytes(
    installed_snapshot.data,
    label=str(installed_snapshot.path),
)
if any(installed.get(key) != value for key, value in expected.items()):
    raise SystemExit("reconciled demo engine environment failed identity verification")
PY
}

prepare_demo_runtime_config() {
    local demo_candidate demo_profile root
    [ "$REPO_DIR" = /opt/liquidity-migration ] \
        || fail "systemd runtime paths require REPO_DIR=/opt/liquidity-migration"
    install -d -o root -g root -m 0700 /etc/liquidity-migration
    reconcile_demo_engine_environment
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
        chmod 0750 "$root" || fail "cannot secure demo runtime root: $root"
    done
    chown root:root "$PRODUCER_DEMO_SOURCE_ENV" /etc/liquidity-migration/bybit-demo.env
    chmod 0600 "$PRODUCER_DEMO_SOURCE_ENV" /etc/liquidity-migration/bybit-demo.env
}

trusted_checkout_directory() {
    local directory="$1" mode
    [ -d "$directory" ] && [ ! -L "$directory" ] \
        && [ "$(readlink -f "$directory")" = "$directory" ] \
        && [ "$(stat -c %u "$directory")" -eq 0 ] \
        || return 1
    mode="$(stat -c %a "$directory")" || return 1
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] \
        && (( (8#$mode & 0022) == 0 ))
}

require_checkout() {
    [ -d "$REPO_DIR/.git" ] && [ ! -L "$REPO_DIR/.git" ] \
        || fail "missing trusted Git checkout: $REPO_DIR"
    cd "$REPO_DIR"
}

require_trusted_checkout() {
    local directory unsafe_git_metadata
    require_checkout
    for directory in "${REPO_DIR%/*}" "$REPO_DIR" "$REPO_DIR/scripts" \
        "$REPO_DIR/liquidity_migration" "$REPO_DIR/liquidity_migration/ops" \
        "$REPO_DIR/.git"; do
        trusted_checkout_directory "$directory" \
            || fail "trusted checkout ancestry is missing, linked, non-root-owned, or group/world-writable: $directory"
    done
    unsafe_git_metadata="$(
        /usr/bin/find "$REPO_DIR/.git" -xdev -mindepth 1 \
            \( ! -uid 0 -o -perm /022 -o -type l \
                -o \( ! -type f -a ! -type d \) \) \
            -print -quit
    )" || fail "cannot inspect trusted checkout metadata permissions"
    [ -z "$unsafe_git_metadata" ] \
        || fail "trusted checkout metadata contains a non-root-owned, writable, linked, or special entry: $unsafe_git_metadata"
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
        rm -f "$config_file" \
            || fail "cannot remove the authenticated Git configuration"
        return "$status"
    else
        "${GIT_ENV[@]}" GIT_TERMINAL_PROMPT=0 "${GIT_COMMAND[@]}" "$@"
    fi
}

install_mode() {
    local installed_head
    require_rollout_for_funded_generation_change install
    require_trusted_checkout
    # The installed checkout's toggles answer one question here: is a funded
    # sleeve in play? If it is, --stop-first stays off and a running fleet is
    # refused rather than stopped.
    . deploy/lib_sleeves.sh
    lm_load_sleeve_toggles
    resolve_stop_first
    verify_prefetched_deploy_inputs "$EXPECTED_COMMIT" source
    require_quiescent
    run_phase persist-install-boot-fence disable_rollout_units_for_boot_fence
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    require_clean_checkout_at "$installed_head" "install"

    safe_git checkout -B "$BRANCH" "$EXPECTED_COMMIT" \
        || fail "cannot select the prefetched exact commit"
    require_clean_head
    verify_prefetched_deploy_inputs "$EXPECTED_COMMIT" checkout
    unset GITHUB_TOKEN

    run_phase install-locked-dependencies install_python_environment
    PYTHON=.venv/bin/python
    # No lint/type/test phase here: CI runs scripts/dev.sh lint+types+test on
    # every push to main, and the ancestor check above proves this commit is on
    # main. Re-running them with the fleet stopped only lengthens the outage.

    run_phase install-runtime-identities ensure_runtime_identities
    run_phase verify-python-runtime verify_python_runtime_environment

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
    run_phase install-mainnet-engine-config install_mainnet_engine_config
    require_clean_head
    run_phase engine-build build_engine
    echo "install-ok commit=$EXPECTED_COMMIT units_started=0"
    echo "next: run activate to start the sleeves this checkout enables"
}

load_authorization() {
    require_trusted_checkout
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
ENGINE_RUST_TOOLCHAIN=1.90.0
ENGINE_BUILDER_STATE=/var/lib/liquidity-migration-builder
ENGINE_BUILDER_CARGO_HOME="$ENGINE_BUILDER_STATE/cargo-home"
ENGINE_BUILDER_TARGET_DIR="$ENGINE_BUILDER_STATE/target"
PYTHON_PREFETCH_VENV="$ENGINE_BUILDER_STATE/python-prefetch-venv"
PYTHON_WHEEL_CACHE="$ENGINE_BUILDER_STATE/python-wheels"
DEPLOY_VENV_STAGING_ROOT="$REPO_DIR/venv"
DEPLOY_VENV_STAGING=""
ENGINE_BINARY=/opt/liquidity-migration-engine/bin/engine
ENGINE_LAUNCHER=/opt/liquidity-migration-engine/bin/run-authorized-runtime
ENGINE_CONTROL_HELPER=/opt/liquidity-migration-engine/bin/telegram-control-helper
CONTROLS_SUDOERS=/etc/sudoers.d/liquidity-migration-controls
TELEGRAM_CONTROLS_BOT=/opt/liquidity-migration/liquidity_migration/ops/telegram_controls.py
ACTIVATION_RECEIPT=/opt/liquidity-migration-engine/bin/activation.complete
ACTIVATION_PERMIT=/run/liquidity-migration/activation.permit
ACTIVATION_WATCHDOG_UNIT=liquidity-migration-activation-watchdog.service
ACTIVATION_LEASE_SECONDS=6
ENGINE_ENVIRONMENT=/etc/liquidity-migration/engine.env
ENGINE_MAINNET_ENVIRONMENT=/etc/liquidity-migration/engine-mainnet.env
ENGINE_MAINNET_CONFIG=/etc/liquidity-migration/engine-mainnet.toml
ENGINE_CANDIDATE_BINARY="$ENGINE_BUILDER_TARGET_DIR/release/engine"
ENGINE_PREFETCHED_COMMIT=""
ENGINE_PREFETCHED_DIGEST=""
PYTHON_PREFETCHED_LOCK_DIGEST=""
PYTHON_PREFETCHED_WHEEL_DIGEST=""
DEPLOY_PREFETCHED_COMMIT=""
DEPLOY_PREFETCHED_REMOTE_TIP=""
ENGINE_ACTIVE_BUILDER_UNIT=""

# The single arming switch: REAL_MONEY=true in the mainnet credential file,
# written by the owner's own hand next to the live API key. No file, or any
# other value, means disarmed. The value is read through the strict private
# loader and never printed. Cached: one answer per run.
MAINNET_CREDENTIAL_ENV=/etc/liquidity-migration/bybit-mainnet.env
MAINNET_ATTESTOR_ENV=/etc/liquidity-migration/bybit-mainnet-attestor.env
MAINNET_ARMED_STATE=""
ROLLOUT_FUNDED_AUTHORITY=0

funded_configuration_present() {
    local path
    for path in \
        "$MAINNET_CREDENTIAL_ENV" \
        "$MAINNET_ATTESTOR_ENV" \
        "$ENGINE_MAINNET_ENVIRONMENT" \
        /etc/liquidity-migration/engine-mainnet.toml \
        "$PRODUCER_MAINNET_ENV" \
        "$PRODUCER_MAINNET_SOURCE_ENV" \
        "$MAINNET_TELEGRAM_ENV"; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            return 0
        fi
    done
    return 1
}

install_mainnet_engine_config() {
    funded_configuration_present || return 0
    install -o root -g "$RUNTIME_GROUP" -m 0640 \
        deploy/engine.mainnet.toml.template "${ENGINE_MAINNET_CONFIG}.new" \
        || fail "cannot stage the committed mainnet engine config"
    mv -f "${ENGINE_MAINNET_CONFIG}.new" "$ENGINE_MAINNET_CONFIG" \
        || fail "cannot install the committed mainnet engine config"
}

require_rollout_for_funded_generation_change() {
    local operation="$1"
    if [ "${ROLLOUT_FUNDED_AUTHORITY:-0}" -ne 1 ] \
        && funded_configuration_present; then
        fail "$operation refused: this host has persisted funded configuration; only rollout may change or activate its generation"
    fi
}

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
                "${PYTHON:-/usr/bin/python3}" -c '
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

verify_topology() {
    local activation_policy="${1:-complete}"
    case "$activation_policy" in
        complete|activation-in-progress) ;;
        *) fail "invalid topology activation policy: $activation_policy" ;;
    esac
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
    if systemctl cat liquidity-migration-backup.timer >/dev/null 2>&1; then
        verify_unit on liquidity-migration-backup.timer "nightly backup timer is not active"
    fi
    if systemctl cat liquidity-migration-chaos-drill.timer >/dev/null 2>&1; then
        verify_unit on liquidity-migration-chaos-drill.timer "weekly chaos drill timer is not active"
    fi
    if systemctl cat liquidity-migration-forward-capture.service >/dev/null 2>&1; then
        verify_unit on liquidity-migration-forward-capture.service "forward market capture is not active"
    fi
    if systemctl cat liquidity-migration-forward-upload.timer >/dev/null 2>&1; then
        verify_unit on liquidity-migration-forward-upload.timer "forward market upload timer is not active"
    fi
    verify_unit on "$ENGINE_UNIT" "required demo Rust engine is not active"
    if [ ! -x "$ENGINE_BINARY" ] || [ ! -r "${ENGINE_BINARY}.release" ]; then
        verify_note "required commit-bound Rust engine artifact is missing"
    else
        local marker_commit marker_digest marker_launcher_digest
        local marker_helper_digest marker_sudoers_digest marker_bot_digest
        local actual_digest actual_launcher_digest actual_helper_digest
        local actual_sudoers_digest actual_bot_digest
        marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
        marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
        marker_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "${ENGINE_BINARY}.release")"
        marker_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "${ENGINE_BINARY}.release")"
        marker_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "${ENGINE_BINARY}.release")"
        marker_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "${ENGINE_BINARY}.release")"
        actual_digest="$(sha256sum "$ENGINE_BINARY" | awk '{print $1}' || true)"
        [ "$marker_commit" = "$EXPECTED_COMMIT" ] \
            || verify_note "engine artifact is not bound to requested commit $EXPECTED_COMMIT"
        [ -n "$marker_digest" ] && [ "$marker_digest" = "$actual_digest" ] \
            || verify_note "engine artifact digest does not match its release marker"
        if [ -n "$marker_launcher_digest" ]; then
            actual_launcher_digest="$(sha256sum "$ENGINE_LAUNCHER" 2>/dev/null | awk '{print $1}' || true)"
            [ "$marker_launcher_digest" = "$actual_launcher_digest" ] \
                || verify_note "trusted launcher digest does not match its release marker"
            if [ -n "$marker_helper_digest$marker_sudoers_digest$marker_bot_digest" ]; then
                actual_helper_digest="$(sha256sum "$ENGINE_CONTROL_HELPER" 2>/dev/null | awk '{print $1}' || true)"
                actual_sudoers_digest="$(sha256sum "$CONTROLS_SUDOERS" 2>/dev/null | awk '{print $1}' || true)"
                actual_bot_digest="$(sha256sum "$TELEGRAM_CONTROLS_BOT" 2>/dev/null | awk '{print $1}' || true)"
                [[ "$marker_helper_digest" =~ ^[0-9a-f]{64}$ ]] \
                    && [ "$marker_helper_digest" = "$actual_helper_digest" ] \
                    || verify_note "Telegram control helper digest does not match its release marker"
                [[ "$marker_sudoers_digest" =~ ^[0-9a-f]{64}$ ]] \
                    && [ "$marker_sudoers_digest" = "$actual_sudoers_digest" ] \
                    || verify_note "controls sudoers digest does not match its release marker"
                [[ "$marker_bot_digest" =~ ^[0-9a-f]{64}$ ]] \
                    && [ "$marker_bot_digest" = "$actual_bot_digest" ] \
                    || verify_note "Telegram controls bot digest does not match its release marker"
                verify_controls_sudo_policy
            fi
            if [ "$activation_policy" = activation-in-progress ]; then
                activation_authority_matches "$ACTIVATION_PERMIT" permit \
                    || verify_note "generation has no valid in-progress activation permit"
            else
                activation_authority_matches "$ACTIVATION_RECEIPT" complete \
                    || verify_note "generation has no valid activation completion receipt"
            fi
        fi
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

require_pinned_engine_toolchain() {
    local cargo="$ENGINE_TOOLCHAIN_DIR/cargo/bin/cargo"
    local rustc="$ENGINE_TOOLCHAIN_DIR/cargo/bin/rustc"
    [ -x "$cargo" ] || fail "pinned Rust cargo proxy is missing: $cargo"
    [ -x "$rustc" ] || fail "pinned Rust compiler proxy is missing: $rustc"
    RUSTUP_HOME="$ENGINE_TOOLCHAIN_DIR/rustup" \
        RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN" \
        "$rustc" --version | grep -F "rustc $ENGINE_RUST_TOOLCHAIN " >/dev/null \
        || fail "host Rust compiler does not match rust-toolchain.toml (required $ENGINE_RUST_TOOLCHAIN)"
    RUSTUP_HOME="$ENGINE_TOOLCHAIN_DIR/rustup" \
        RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN" \
        "$cargo" --version >/dev/null \
        || fail "pinned Rust cargo cannot run for $ENGINE_RUST_TOOLCHAIN"
}

stop_active_engine_builder_unit() {
    local unit="$ENGINE_ACTIVE_BUILDER_UNIT"
    [ -n "$unit" ] || return 0
    systemctl stop "$unit" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        return 1
    fi
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    ENGINE_ACTIVE_BUILDER_UNIT=""
}

stop_stale_engine_builder_units() {
    local rows unit
    rows="$(
        systemctl list-units 'liquidity-migration-engine-*.service' \
            --all --no-legend --no-pager --plain 2>/dev/null
    )" || fail "cannot enumerate transient builder units"
    while IFS= read -r unit; do
        unit="${unit%% *}"
        case "$unit" in
            liquidity-migration-engine-fetch-[0-9]*-[0-9]*.service|\
            liquidity-migration-engine-build-[0-9]*-[0-9]*.service|\
            liquidity-migration-engine-python-fetch-[0-9]*-[0-9]*.service) ;;
            *) continue ;;
        esac
        if ! systemctl stop "$unit" 2>/dev/null \
            && systemctl is-active --quiet "$unit" 2>/dev/null; then
            fail "cannot stop stale transient builder unit $unit"
        fi
        ! systemctl is-active --quiet "$unit" 2>/dev/null \
            || fail "stale transient builder unit remained active: $unit"
        systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    done <<< "$rows"
}

run_engine_builder_step() {
    local step="$1" command status=0 unit
    local -a network_boundary=()
    case "$step" in
        fetch)
            command='cd /opt/engine-build/engine && exec /opt/rust/cargo/bin/cargo fetch --locked'
            ;;
        build)
            network_boundary+=(
                --property="PrivateNetwork=true"
                --property="RestrictAddressFamilies=AF_UNIX"
            )
            command='cd /opt/engine-build/engine && exec /usr/bin/nice -n 19 /opt/rust/cargo/bin/cargo build --release --locked --offline -j 1'
            ;;
        python-fetch)
            command='/usr/bin/python3 -m venv /var/lib/liquidity-migration-builder/python-prefetch-venv && exec /var/lib/liquidity-migration-builder/python-prefetch-venv/bin/python -m pip download --disable-pip-version-check --no-deps --no-cache-dir --only-binary=:all: --dest /var/lib/liquidity-migration-builder/python-wheels -r /opt/engine-build/requirements.lock'
            ;;
        *) fail "invalid engine builder step: $step" ;;
    esac
    [ -z "$ENGINE_ACTIVE_BUILDER_UNIT" ] \
        || fail "another transient builder unit is still tracked: $ENGINE_ACTIVE_BUILDER_UNIT"
    unit="liquidity-migration-engine-$step-$$-$RANDOM"
    ENGINE_ACTIVE_BUILDER_UNIT="$unit"
    systemd-run --quiet --wait --pipe --collect --service-type=exec \
        --unit="$unit" \
        --property="Restart=no" \
        --property="KillMode=control-group" \
        --property="RuntimeMaxSec=45m" \
        --property="TimeoutStopSec=30s" \
        --property="NoNewPrivileges=true" \
        --property="PrivateTmp=true" \
        --property="ProtectProc=invisible" \
        --property="ProcSubset=pid" \
        --property="ProtectSystem=strict" \
        --property="ProtectHome=true" \
        --property="ReadOnlyPaths=$ENGINE_BUILD_DIR $ENGINE_TOOLCHAIN_DIR" \
        --property="ReadWritePaths=$ENGINE_BUILDER_STATE" \
        --property="UMask=0077" \
        "${network_boundary[@]}" \
        /usr/sbin/runuser -u "$ENGINE_BUILDER_USER" -- \
            /usr/bin/env -i \
                HOME=/nonexistent \
                PATH="$ENGINE_TOOLCHAIN_DIR/cargo/bin:/usr/bin:/bin" \
                CARGO_HOME="$ENGINE_BUILDER_CARGO_HOME" \
                CARGO_TARGET_DIR="$ENGINE_BUILDER_TARGET_DIR" \
                RUSTUP_HOME="$ENGINE_TOOLCHAIN_DIR/rustup" \
                RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN" \
                RUST_BACKTRACE=1 \
                /bin/sh -c "$command" \
        || status=$?
    stop_active_engine_builder_unit \
        || fail "cannot stop transient builder unit $unit"
    return "$status"
}

# Compile an exact commit in the isolated build clone while the current fleet
# remains live. Stopped installation only consumes the bound candidate.
prepare_disposable_engine_build_root() {
    local mode mount_boundary
    [ "$ENGINE_BUILD_DIR" = /opt/engine-build ] \
        || fail "engine build root escaped its fixed path"
    [ -d /opt ] && [ ! -L /opt ] && [ "$(readlink -f /opt)" = /opt ] \
        && [ "$(stat -c %u /opt)" -eq 0 ] && [ "$(stat -c %g /opt)" -eq 0 ] \
        || fail "engine build parent is not a canonical root-owned directory"
    mode="$(stat -c %a /opt)" || fail "cannot inspect engine build parent mode"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 0022) == 0 )) \
        || fail "engine build parent is group/other writable"
    if [ ! -e "$ENGINE_BUILD_DIR" ] && [ ! -L "$ENGINE_BUILD_DIR" ]; then
        install -d -o root -g root -m 0755 "$ENGINE_BUILD_DIR" \
            || fail "cannot create the disposable engine build root"
    fi
    [ -d "$ENGINE_BUILD_DIR" ] && [ ! -L "$ENGINE_BUILD_DIR" ] \
        && [ "$(readlink -f "$ENGINE_BUILD_DIR")" = "$ENGINE_BUILD_DIR" ] \
        && [ "$(stat -c %u "$ENGINE_BUILD_DIR")" -eq 0 ] \
        && [ "$(stat -c %g "$ENGINE_BUILD_DIR")" -eq 0 ] \
        || fail "engine build root is linked, redirected, or not root-owned"
    # Cargo runs as the isolated builder and must be able to traverse this source-only root.
    chmod 0755 "$ENGINE_BUILD_DIR" \
        || fail "cannot make the engine build root readable by the isolated builder"
    mode="$(stat -c %a "$ENGINE_BUILD_DIR")" \
        || fail "cannot inspect engine build root mode"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 0022) == 0 )) \
        || fail "engine build root is group/other writable"
    mount_boundary="$(
        awk -v root="$ENGINE_BUILD_DIR" '
            $5 == root || index($5, root "/") == 1 { print $5; exit }
        ' /proc/self/mountinfo
    )" || fail "cannot inspect engine build mount boundaries"
    [ -z "$mount_boundary" ] \
        || fail "engine build root contains a mount boundary: $mount_boundary"
    if [ ! -e "$ENGINE_BUILD_DIR/.git" ] && [ ! -L "$ENGINE_BUILD_DIR/.git" ]; then
        "${GIT_ENV[@]}" /usr/bin/git init --quiet "$ENGINE_BUILD_DIR" \
            || fail "cannot prepare engine build clone"
    fi
    [ -d "$ENGINE_BUILD_DIR/.git" ] && [ ! -L "$ENGINE_BUILD_DIR/.git" ] \
        && [ "$(readlink -f "$ENGINE_BUILD_DIR/.git")" = "$ENGINE_BUILD_DIR/.git" ] \
        && [ "$(stat -c %u "$ENGINE_BUILD_DIR/.git")" -eq 0 ] \
        && [ "$(stat -c %g "$ENGINE_BUILD_DIR/.git")" -eq 0 ] \
        || fail "engine build Git directory is linked, redirected, or not root-owned"
    mode="$(stat -c %a "$ENGINE_BUILD_DIR/.git")" \
        || fail "cannot inspect engine build Git directory mode"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 0022) == 0 )) \
        || fail "engine build Git directory is group/other writable"
}

materialize_single_link_engine_candidate() {
    local candidate="$ENGINE_CANDIDATE_BINARY" candidate_dir hardlink_count
    local internal_links source_digest temporary temporary_digest
    candidate_dir="${candidate%/*}"
    [ "$candidate_dir" = "$ENGINE_BUILDER_TARGET_DIR/release" ] \
        && [ -d "$ENGINE_BUILDER_TARGET_DIR" ] \
        && [ ! -L "$ENGINE_BUILDER_TARGET_DIR" ] \
        && [ "$(readlink -f "$ENGINE_BUILDER_TARGET_DIR")" = "$ENGINE_BUILDER_TARGET_DIR" ] \
        && [ "$(stat -c %U "$ENGINE_BUILDER_TARGET_DIR")" = "$ENGINE_BUILDER_USER" ] \
        && [ "$(stat -c %G "$ENGINE_BUILDER_TARGET_DIR")" = "$ENGINE_BUILDER_GROUP" ] \
        || fail "engine target root or candidate path is unsafe"
    [ -f "$candidate" ] && [ ! -L "$candidate" ] && [ -x "$candidate" ] \
        && [ "$(readlink -f "$candidate")" = "$candidate" ] \
        && [ "$(stat -c %U "$candidate")" = "$ENGINE_BUILDER_USER" ] \
        && [ "$(stat -c %G "$candidate")" = "$ENGINE_BUILDER_GROUP" ] \
        || fail "Cargo produced no safe regular engine binary"
    hardlink_count="$(stat -c %h "$candidate")" \
        || fail "cannot inspect Cargo engine hard links"
    internal_links="$(
        find "$ENGINE_BUILDER_TARGET_DIR" -xdev -type f -samefile "$candidate" \
            -printf . | wc -c | tr -d '[:space:]'
    )" || fail "cannot enumerate Cargo engine hard links"
    [[ "$hardlink_count" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$internal_links" =~ ^[1-9][0-9]*$ ]] \
        && [ "$internal_links" -eq "$hardlink_count" ] \
        || fail "Cargo engine binary has a hard-link alias outside its target root"
    source_digest="$(sha256sum "$candidate" | awk '{print $1}')" \
        || fail "cannot digest Cargo engine binary"
    temporary="$(mktemp "$candidate_dir/.engine-candidate.XXXXXX")" \
        || fail "cannot create single-link engine candidate staging file"
    [ "${temporary%/*}" = "$candidate_dir" ] && [ ! -L "$temporary" ] \
        && [ "$(stat -c %h "$temporary")" -eq 1 ] \
        || fail "engine candidate staging path escaped or is linked"
    install -o "$ENGINE_BUILDER_USER" -g "$ENGINE_BUILDER_GROUP" -m 0700 \
        "$candidate" "$temporary" \
        || fail "cannot materialize the single-link engine candidate"
    temporary_digest="$(sha256sum "$temporary" | awk '{print $1}')" \
        || fail "cannot digest staged single-link engine candidate"
    [ "$temporary_digest" = "$source_digest" ] \
        && [ -f "$temporary" ] && [ ! -L "$temporary" ] \
        && [ "$(stat -c %h "$temporary")" -eq 1 ] \
        && [ "$(stat -c %U "$temporary")" = "$ENGINE_BUILDER_USER" ] \
        && [ "$(stat -c %G "$temporary")" = "$ENGINE_BUILDER_GROUP" ] \
        || fail "single-link engine candidate differs from Cargo output"
    mv -fT -- "$temporary" "$candidate" \
        || fail "cannot atomically select the single-link engine candidate"
}

compile_engine_commit() {
    local commit="$1"
    local built dirty candidate_digest candidate_real expected_candidate_real status=0
    ENGINE_PREFETCHED_COMMIT=""
    ENGINE_PREFETCHED_DIGEST=""
    ensure_engine_builder_identity
    require_pinned_engine_toolchain
    prepare_disposable_engine_build_root
    chmod -R u+rwX "$ENGINE_BUILD_DIR" \
        || fail "cannot reopen the root-owned engine source for exact reset"
    engine_git fetch --no-tags --quiet "$REPO_DIR" "$commit" \
        || fail "cannot copy engine commit $commit into the build clone"
    engine_git reset --hard --quiet FETCH_HEAD \
        || fail "cannot reset the engine build clone to $commit"
    # This checkout is a disposable compiler input, never a runtime state
    # root. Remove ignored and untracked residue from prior benchmarks or
    # cross-platform copies before proving the exact commit is clean.
    engine_git clean -ffdx --quiet \
        || fail "cannot scrub the disposable engine build clone"
    built="$(engine_git rev-parse HEAD)" || fail "cannot read engine build clone HEAD"
    [ "$built" = "$commit" ] || fail "engine build clone is $built, not $commit"
    dirty="$(engine_git status --porcelain=v1 --untracked-files=all)" \
        || fail "cannot inspect exact engine source checkout"
    [ -z "$dirty" ] || fail "engine build source is dirty before compilation"

    # Root prepares an immutable source view. Cargo, proc macros, and build.rs
    # run as a credential-isolated builder and can write only their disposable
    # CARGO_HOME/target roots outside the checkout.
    chown -R root:root "$ENGINE_BUILD_DIR" \
        && chmod -R a-w "$ENGINE_BUILD_DIR" \
        || fail "cannot make exact engine source root-owned and non-writable"
    [ -z "$(find "$ENGINE_BUILD_DIR" ! -user root -print -quit)" ] \
        && [ -z "$(find "$ENGINE_BUILD_DIR" ! -type l -perm /222 -print -quit)" ] \
        || fail "engine source ownership or write permissions are unsafe"
    install -d -o "$ENGINE_BUILDER_USER" -g "$ENGINE_BUILDER_GROUP" -m 0700 \
        "$ENGINE_BUILDER_STATE" \
        || fail "cannot prepare isolated engine builder state"
    [ "$(readlink -f "$ENGINE_BUILDER_STATE")" = "$ENGINE_BUILDER_STATE" ] \
        && [ ! -L "$ENGINE_BUILDER_STATE" ] \
        || fail "engine builder state path is linked or escapes its fixed root"
    rm -rf -- "$ENGINE_BUILDER_CARGO_HOME" "$ENGINE_BUILDER_TARGET_DIR" \
        || fail "cannot clear disposable Rust builder roots"
    install -d -o "$ENGINE_BUILDER_USER" -g "$ENGINE_BUILDER_GROUP" -m 0700 \
        "$ENGINE_BUILDER_CARGO_HOME" "$ENGINE_BUILDER_TARGET_DIR" \
        || fail "cannot prepare disposable builder output roots"
    # Dependency archives are fetched while network access is available, then
    # proc macros and build.rs execute offline inside a private network. The
    # transient cgroup prevents a child from surviving artifact staging.
    if run_engine_builder_step fetch; then
        status=0
    else
        status=$?
    fi
    [ "$status" -eq 0 ] || fail "locked engine dependency fetch failed (status $status)"
    if run_engine_builder_step build; then
        status=0
    else
        status=$?
    fi
    [ "$status" -eq 0 ] || fail "locked offline release engine build failed (status $status)"
    built="$(engine_git rev-parse HEAD)" || fail "cannot re-read engine source HEAD"
    dirty="$(engine_git status --porcelain=v1 --untracked-files=all)" \
        || fail "cannot re-inspect engine source after compilation"
    [ "$built" = "$commit" ] && [ -z "$dirty" ] \
        || fail "engine source changed during unprivileged compilation"
    [ -z "$(find "$ENGINE_BUILD_DIR" ! -user root -print -quit)" ] \
        && [ -z "$(find "$ENGINE_BUILD_DIR" ! -type l -perm /222 -print -quit)" ] \
        || fail "engine source permissions changed during compilation"
    # Cargo normally hard-links the promoted binary to its hashed deps entry.
    # Prove every alias is inside the disposable target, then atomically copy
    # the exact bytes onto a one-link handoff path for stopped installation.
    materialize_single_link_engine_candidate
    [ -f "$ENGINE_CANDIDATE_BINARY" ] && [ ! -L "$ENGINE_CANDIDATE_BINARY" ] \
        && [ -x "$ENGINE_CANDIDATE_BINARY" ] \
        || fail "locked release build produced no regular engine binary"
    candidate_real="$(readlink -f "$ENGINE_CANDIDATE_BINARY")" \
        || fail "cannot resolve engine candidate"
    expected_candidate_real="$ENGINE_BUILDER_TARGET_DIR/release/engine"
    [ "$candidate_real" = "$expected_candidate_real" ] \
        && [ "$(stat -c %U "$ENGINE_CANDIDATE_BINARY")" = "$ENGINE_BUILDER_USER" ] \
        && [ "$(stat -c %G "$ENGINE_CANDIDATE_BINARY")" = "$ENGINE_BUILDER_GROUP" ] \
        && [ "$(stat -c %h "$ENGINE_CANDIDATE_BINARY")" -eq 1 ] \
        || fail "engine candidate is linked, outside its target root, or not builder-owned"
    candidate_digest="$(sha256sum "$ENGINE_CANDIDATE_BINARY" | awk '{print $1}')" \
        || fail "cannot digest the prefetched engine candidate"
    [[ "$candidate_digest" =~ ^[0-9a-f]{64}$ ]] \
        || fail "prefetched engine candidate digest is invalid"
    ENGINE_PREFETCHED_COMMIT="$commit"
    ENGINE_PREFETCHED_DIGEST="$candidate_digest"
    printf 'engine-prefetch-ok commit=%s sha256=%s binary=%s\n' \
        "$commit" "$candidate_digest" "$ENGINE_CANDIDATE_BINARY"
}

verify_prefetched_engine_candidate() {
    local commit="$1"
    local actual_digest built candidate_real dirty expected_candidate_real
    [ "$ENGINE_PREFETCHED_COMMIT" = "$commit" ] \
        || fail "engine candidate was not prefetched for commit $commit"
    [[ "$ENGINE_PREFETCHED_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        || fail "prefetched engine candidate has no valid digest binding"
    built="$(engine_git rev-parse HEAD)" \
        || fail "cannot read prefetched engine source HEAD"
    [ "$built" = "$commit" ] \
        || fail "prefetched engine source is $built, not $commit"
    dirty="$(engine_git status --porcelain=v1 --untracked-files=all)" \
        || fail "cannot inspect prefetched engine source"
    [ -z "$dirty" ] || fail "prefetched engine source is dirty"
    [ -z "$(find "$ENGINE_BUILD_DIR" ! -user root -print -quit)" ] \
        && [ -z "$(find "$ENGINE_BUILD_DIR" ! -type l -perm /222 -print -quit)" ] \
        || fail "prefetched engine source ownership or write permissions changed"
    [ -f "$ENGINE_CANDIDATE_BINARY" ] && [ ! -L "$ENGINE_CANDIDATE_BINARY" ] \
        && [ -x "$ENGINE_CANDIDATE_BINARY" ] \
        || fail "prefetched engine candidate is not a regular executable"
    candidate_real="$(readlink -f "$ENGINE_CANDIDATE_BINARY")" \
        || fail "cannot resolve prefetched engine candidate"
    expected_candidate_real="$ENGINE_BUILDER_TARGET_DIR/release/engine"
    [ "$candidate_real" = "$expected_candidate_real" ] \
        && [ "$(stat -c %U "$ENGINE_CANDIDATE_BINARY")" = "$ENGINE_BUILDER_USER" ] \
        && [ "$(stat -c %G "$ENGINE_CANDIDATE_BINARY")" = "$ENGINE_BUILDER_GROUP" ] \
        && [ "$(stat -c %h "$ENGINE_CANDIDATE_BINARY")" -eq 1 ] \
        || fail "prefetched engine candidate moved, linked, or changed owner"
    actual_digest="$(sha256sum "$ENGINE_CANDIDATE_BINARY" | awk '{print $1}')" \
        || fail "cannot digest the prefetched engine candidate"
    [ "$actual_digest" = "$ENGINE_PREFETCHED_DIGEST" ] \
        || fail "prefetched engine candidate changed before stopped installation"
}

python_wheel_cache_digest() {
    local lock_file="$1" builder_uid builder_gid
    [ -d "$PYTHON_WHEEL_CACHE" ] && [ ! -L "$PYTHON_WHEEL_CACHE" ] \
        && [ "$(readlink -f "$PYTHON_WHEEL_CACHE")" = "$PYTHON_WHEEL_CACHE" ] \
        && [ "$(stat -c %U "$PYTHON_WHEEL_CACHE")" = "$ENGINE_BUILDER_USER" ] \
        && [ "$(stat -c %G "$PYTHON_WHEEL_CACHE")" = "$ENGINE_BUILDER_GROUP" ] \
        && [ "$(stat -c %a "$PYTHON_WHEEL_CACHE")" = 700 ] \
        || fail "Python wheel cache path, owner, or mode changed"
    builder_uid="$(id -u "$ENGINE_BUILDER_USER")" \
        || fail "cannot resolve engine builder uid"
    builder_gid="$(id -g "$ENGINE_BUILDER_USER")" \
        || fail "cannot resolve engine builder gid"
    /usr/bin/python3 - "$PYTHON_WHEEL_CACHE" "$lock_file" \
        "$builder_uid" "$builder_gid" <<'PY'
import hashlib
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
lock = Path(sys.argv[2])
expected_uid = int(sys.argv[3])
expected_gid = int(sys.argv[4])
requirements = [
    line.strip()
    for line in lock.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
wheels = sorted(root.iterdir(), key=lambda path: path.name.encode("utf-8"))
if not requirements or len(wheels) != len(requirements):
    raise SystemExit(
        f"wheel cache count {len(wheels)} does not match locked requirement count {len(requirements)}"
    )

manifest = hashlib.sha256()
for wheel in wheels:
    metadata = os.lstat(wheel)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or wheel.suffix.lower() != ".whl"
    ):
        raise SystemExit(f"unsafe wheel cache entry: {wheel.name}")
    content = hashlib.sha256()
    with wheel.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            content.update(chunk)
    manifest.update(wheel.name.encode("utf-8"))
    manifest.update(b"\0")
    manifest.update(content.digest())
print(manifest.hexdigest())
PY
}

prefetch_python_dependencies() {
    local lock_digest status=0 wheel_digest
    PYTHON_PREFETCHED_LOCK_DIGEST=""
    PYTHON_PREFETCHED_WHEEL_DIGEST=""
    rm -rf -- "$PYTHON_PREFETCH_VENV" "$PYTHON_WHEEL_CACHE" \
        || fail "cannot clear disposable Python prefetch roots"
    install -d -o "$ENGINE_BUILDER_USER" -g "$ENGINE_BUILDER_GROUP" -m 0700 \
        "$PYTHON_WHEEL_CACHE" \
        || fail "cannot prepare isolated Python wheel cache"
    if run_engine_builder_step python-fetch; then
        status=0
    else
        status=$?
    fi
    [ "$status" -eq 0 ] \
        || fail "locked Python dependency fetch failed (status $status)"
    lock_digest="$(sha256sum "$ENGINE_BUILD_DIR/requirements.lock" | awk '{print $1}')" \
        || fail "cannot digest prefetched Python requirement lock"
    wheel_digest="$(python_wheel_cache_digest "$ENGINE_BUILD_DIR/requirements.lock")" \
        || fail "cannot validate prefetched Python wheel cache"
    [[ "$lock_digest" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$wheel_digest" =~ ^[0-9a-f]{64}$ ]] \
        || fail "prefetched Python dependency digest is invalid"
    PYTHON_PREFETCHED_LOCK_DIGEST="$lock_digest"
    PYTHON_PREFETCHED_WHEEL_DIGEST="$wheel_digest"
    printf 'python-prefetch-ok lock_sha256=%s wheels_sha256=%s directory=%s\n' \
        "$lock_digest" "$wheel_digest" "$PYTHON_WHEEL_CACHE"
}

verify_prefetched_python_dependencies() {
    local lock_file="$1" lock_digest wheel_digest
    [[ "$PYTHON_PREFETCHED_LOCK_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$PYTHON_PREFETCHED_WHEEL_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        || fail "Python dependencies were not bound during prefetch"
    [ -f "$lock_file" ] && [ ! -L "$lock_file" ] \
        || fail "prefetched Python requirement lock is missing or linked: $lock_file"
    lock_digest="$(sha256sum "$lock_file" | awk '{print $1}')" \
        || fail "cannot digest Python requirement lock: $lock_file"
    [ "$lock_digest" = "$PYTHON_PREFETCHED_LOCK_DIGEST" ] \
        || fail "Python requirement lock changed after prefetch"
    wheel_digest="$(python_wheel_cache_digest "$lock_file")" \
        || fail "cannot revalidate prefetched Python wheel cache"
    [ "$wheel_digest" = "$PYTHON_PREFETCHED_WHEEL_DIGEST" ] \
        || fail "prefetched Python wheel cache changed before offline installation"
}

verify_prefetched_deploy_inputs() {
    local commit="$1" view="$2" current_remote_tip lock_file
    [ "$DEPLOY_PREFETCHED_COMMIT" = "$commit" ] \
        || fail "deployment inputs were not prefetched for commit $commit"
    [[ "$DEPLOY_PREFETCHED_REMOTE_TIP" =~ ^[0-9a-f]{40}$ ]] \
        || fail "prefetched remote branch has no valid commit binding"
    current_remote_tip="$(safe_git rev-parse "$REMOTE/$BRANCH")" \
        || fail "cannot read the cached remote branch"
    [ "$current_remote_tip" = "$DEPLOY_PREFETCHED_REMOTE_TIP" ] \
        || fail "cached remote branch changed after prefetch"
    [ "$(safe_git cat-file -t "$commit" 2>/dev/null || true)" = commit ] \
        || fail "prefetched deploy commit is unavailable"
    verify_prefetched_engine_candidate "$commit"
    case "$view" in
        source) lock_file="$ENGINE_BUILD_DIR/requirements.lock" ;;
        checkout)
            [ "$(safe_git rev-parse HEAD)" = "$commit" ] \
                || fail "checkout does not match prefetched deploy commit"
            lock_file="$REPO_DIR/requirements.lock"
            ;;
        *) fail "invalid prefetched deploy input view: $view" ;;
    esac
    verify_prefetched_python_dependencies "$lock_file"
}

secure_venv_directory() {
    local path="$1" mode
    [ -d "$path" ] && [ ! -L "$path" ] \
        && [ "$(readlink -f "$path")" = "$path" ] \
        && [ "$(stat -c %u "$path")" -eq 0 ] \
        && [ "$(stat -c %g "$path")" -eq 0 ] \
        || return 1
    mode="$(stat -c %a "$path")" || return 1
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 0022) == 0 ))
}

verify_python_runtime_environment() {
    local deployed="$REPO_DIR/.venv" runtime_user state_file
    [ -x /usr/bin/zstd ] || fail "zstd is required for forward market capture"
    [ "$(stat -c %a "$deployed")" = 755 ] \
        || fail "deployed Python environment is not traversable by runtime identities"
    for runtime_user in \
        "$PRODUCER_USER" "$CONTROLS_USER" "$OBSERVER_USER" "$LLM_USER" "$CAPTURE_USER"; do
        /usr/bin/sudo -u "$runtime_user" -- \
            /usr/bin/env PYTHONDONTWRITEBYTECODE=1 \
            "$deployed/bin/python" - "$deployed" <<'PY'
from pathlib import Path
import sys

expected = Path(sys.argv[1]).resolve()
if Path(sys.prefix).resolve() != expected:
    raise SystemExit(
        f"runtime interpreter escaped deployed environment: {sys.prefix!r} != {str(expected)!r}"
    )

import polars  # noqa: F401, E402
import websocket  # noqa: F401, E402
from liquidity_migration.cli.commands import main  # noqa: F401, E402
PY
        [ "$?" -eq 0 ] \
            || fail "deployed Python environment is unusable by $runtime_user"
    done
    for state_file in \
        /var/lib/liquidity-migration/targets/long-demo-state.json \
        /var/lib/liquidity-migration/targets/long-mainnet-state.json; do
        [ -e "$state_file" ] || continue
        /usr/bin/sudo -u "$PRODUCER_USER" -- \
            /usr/bin/env PYTHONDONTWRITEBYTECODE=1 \
            "$deployed/bin/python" - "$state_file" <<'PY'
import sys
from liquidity_migration.strategy.long_book_state import (
    migrate_empty_v1_book_state,
    read_book_state,
)

migrate_empty_v1_book_state(sys.argv[1])
read_book_state(sys.argv[1])
PY
        [ "$?" -eq 0 ] \
            || fail "producer cannot restore durable book state: $state_file"
    done
}

remove_deploy_venv_staging() {
    local path="$DEPLOY_VENV_STAGING"
    [ -n "$path" ] || return 0
    [ "${path%/*}" = "$DEPLOY_VENV_STAGING_ROOT" ] \
        && [[ "${path##*/}" == deploy.* ]] \
        && [ ! -L "$path" ] \
        || return 1
    if [ -e "$path" ]; then
        secure_venv_directory "$path" || return 1
        rm -rf -- "$path" || return 1
    fi
    DEPLOY_VENV_STAGING=""
}

install_python_environment() {
    local deployed="$REPO_DIR/.venv" staging unsafe
    [ -z "$DEPLOY_VENV_STAGING" ] \
        || fail "a Python environment staging generation is already tracked"
    if [ -e "$deployed" ] || [ -L "$deployed" ]; then
        secure_venv_directory "$deployed" \
            || fail "deployed Python environment is linked, unowned, or writable"
    fi
    [ ! -L "$DEPLOY_VENV_STAGING_ROOT" ] \
        || fail "Python environment staging root is linked"
    install -d -o root -g root -m 0700 "$DEPLOY_VENV_STAGING_ROOT" \
        || fail "cannot prepare Python environment staging root"
    secure_venv_directory "$DEPLOY_VENV_STAGING_ROOT" \
        || fail "Python environment staging root is unsafe"
    staging="$(mktemp -d "$DEPLOY_VENV_STAGING_ROOT/deploy.XXXXXX")" \
        || fail "cannot create Python environment staging generation"
    DEPLOY_VENV_STAGING="$staging"
    chmod 0755 "$staging" \
        || fail "cannot make the Python environment traversable by runtime identities"
    secure_venv_directory "$staging" \
        || fail "new Python environment staging generation is unsafe"
    /usr/bin/python3 -m venv "$staging" \
        || fail "cannot create a fresh Python environment"
    "$staging/bin/python" -m pip install --disable-pip-version-check --no-deps \
        --no-cache-dir --no-index --find-links "$PYTHON_WHEEL_CACHE" \
        --only-binary=:all: -r "$REPO_DIR/requirements.lock" \
        || fail "cannot install locked dependencies from the offline wheel cache"
    "$staging/bin/python" - "$REPO_DIR/requirements.lock" <<'PY'
from importlib.metadata import distributions
from pathlib import Path
import re
import sys
import sysconfig

normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
expected = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    parts = line.split("==")
    if len(parts) != 2 or not all(parts):
        raise SystemExit(f"non-exact requirement in deployment lock: {line}")
    name = normalize(parts[0])
    if name in expected:
        raise SystemExit(f"duplicate requirement in deployment lock: {name}")
    expected[name] = parts[1]

# `distributions()` without an explicit path also searches the current working
# directory.  The deployed checkout can contain historical source-tree
# metadata, but only distributions physically installed in this new venv are
# part of its dependency generation.
environment_root = Path(sys.prefix).resolve()
site_packages = sorted(
    {
        str(Path(sysconfig.get_path(kind)).resolve())
        for kind in ("purelib", "platlib")
    }
)
if any(not Path(path).is_relative_to(environment_root) for path in site_packages):
    raise SystemExit(
        f"fresh environment site-packages escaped its root: {site_packages}"
    )

actual = {}
for distribution in distributions(path=site_packages):
    name = normalize(distribution.metadata["Name"])
    if name in {"pip", "setuptools"}:
        continue
    if name in actual:
        raise SystemExit(f"duplicate installed distribution: {name}")
    actual[name] = distribution.version
if actual != expected:
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    wrong = sorted(
        name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
    )
    raise SystemExit(
        f"fresh environment differs from lock: missing={missing} extra={extra} wrong={wrong}"
    )
PY
    [ "$?" -eq 0 ] \
        || fail "fresh Python environment does not exactly match the deployment lock"
    unsafe="$(
        find "$staging" -xdev -mindepth 1 \
            \( ! -type l -a \( ! -uid 0 -o -perm /022 \
                -o \( ! -type f -a ! -type d \) \) \) \
            -print -quit
    )" || fail "cannot inspect the fresh Python environment"
    [ -z "$unsafe" ] \
        || fail "fresh Python environment contains an unsafe entry: $unsafe"
    /usr/bin/python3 - "$staging" "$deployed" <<'PY'
import ctypes
import os
import sys

source = os.fsencode(sys.argv[1])
target = os.fsencode(sys.argv[2])
AT_FDCWD = -100
RENAME_EXCHANGE = 2
if os.path.lexists(target):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, source, AT_FDCWD, target, RENAME_EXCHANGE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), sys.argv[2])
else:
    os.rename(source, target)
directory = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    [ "$?" -eq 0 ] \
        || fail "cannot atomically install the fresh Python environment"
    secure_venv_directory "$deployed" && [ -x "$deployed/bin/python" ] \
        || fail "atomically installed Python environment is unsafe or incomplete"
    remove_deploy_venv_staging \
        || fail "cannot remove the replaced Python environment generation"
}

# Prove the dedicated bot identity has exactly the four reviewed commands and
# no stale/broader sudo grant. Whitespace is presentation-only in `sudo -l`;
# command paths and argv remain exact after normalization.
verify_controls_sudo_policy() {
    local actual expected
    actual="$(
        LC_ALL=C COLUMNS=4096 /usr/bin/sudo -l -U "$CONTROLS_USER" 2>/dev/null \
            | awk '/^[[:space:]]*\(/ { print }' \
            | LC_ALL=C sed 's/[[:space:]]//g' \
            | LC_ALL=C sort
    )" || fail "cannot enumerate the effective Telegram controls sudo policy"
    expected="$(
        printf '%s\n' \
            '(root:root)NOPASSWD:/opt/liquidity-migration-engine/bin/telegram-control-helper pause-demo' \
            '(root:root)NOPASSWD:/opt/liquidity-migration-engine/bin/telegram-control-helper pause-mainnet' \
            '(root:root)NOPASSWD:/opt/liquidity-migration-engine/bin/telegram-control-helper resume-demo' \
            '(root:root)NOPASSWD:/opt/liquidity-migration-engine/bin/telegram-control-helper resume-mainnet' \
            '(root:root)NOPASSWD:/opt/liquidity-migration-engine/bin/telegram-control-helper status-demo' \
            | LC_ALL=C sed 's/[[:space:]]//g' \
            | LC_ALL=C sort
    )"
    [ "$actual" = "$expected" ] \
        || fail "effective Telegram controls sudo policy is not the exact five-command boundary"
}

# Atomically install the exact candidate compiled during prefetch. The stopped
# window performs no Rust compilation or dependency fetch.
build_engine() {
    local commit candidate_digest candidate_after digest launcher_digest
    local helper_digest sudoers_digest bot_digest helper_source sudoers_source
    local helper_source_before helper_source_after sudoers_source_before
    local sudoers_source_after launcher_source marker_tmp
    commit="$(safe_git rev-parse HEAD)" || fail "cannot read installed commit for engine build"
    [ "$commit" = "$EXPECTED_COMMIT" ] || fail "engine build commit is not the requested commit"
    verify_prefetched_engine_candidate "$commit"
    launcher_source="$REPO_DIR/deploy/run_authorized_runtime_trusted.sh"
    helper_source="$REPO_DIR/deploy/telegram_control_helper.sh"
    sudoers_source="$REPO_DIR/deploy/liquidity-controls.sudoers"
    [ "$TELEGRAM_CONTROLS_BOT" = "$REPO_DIR/liquidity_migration/ops/telegram_controls.py" ] \
        || fail "Telegram controls bot path escaped the fixed checkout location"
    local source
    for source in "$launcher_source" "$helper_source" "$sudoers_source" \
        "$TELEGRAM_CONTROLS_BOT"; do
        [ -f "$source" ] && [ ! -L "$source" ] \
            && [ "$(stat -c %u "$source")" -eq 0 ] \
            && [ "$(stat -c %g "$source")" -eq 0 ] \
            || fail "release control source must be a root-owned regular file: $source"
        case "$(stat -c %a "$source")" in
            440|600|640|644|700|740|744|755) ;;
            *) fail "release control source must not be group/other writable: $source" ;;
        esac
    done
    /bin/bash -n "$launcher_source" \
        || fail "trusted runtime launcher source has invalid shell syntax"
    /bin/bash -n "$helper_source" \
        || fail "Telegram control helper source has invalid shell syntax"
    require_clean_head
    install -d -o root -g root -m 0755 "${ENGINE_BINARY%/*}" \
        || fail "cannot create the engine binary directory"
    install -d -o root -g root -m 0755 "${CONTROLS_SUDOERS%/*}" \
        || fail "cannot create the sudoers fragment directory"
    for source in "$ENGINE_BINARY.new" "$ENGINE_LAUNCHER.new" \
        "$ENGINE_CONTROL_HELPER.new" "$CONTROLS_SUDOERS.new"; do
        [ ! -L "$source" ] || fail "release staging path is linked: $source"
        rm -f -- "$source" || fail "cannot clear release staging path: $source"
    done
    for source in "$ENGINE_CONTROL_HELPER" "$CONTROLS_SUDOERS"; do
        [ ! -L "$source" ] || fail "installed control boundary path is linked: $source"
    done
    candidate_digest="$ENGINE_PREFETCHED_DIGEST"
    helper_source_before="$(sha256sum "$helper_source" | awk '{print $1}')" \
        || fail "cannot digest the Telegram control helper source"
    sudoers_source_before="$(sha256sum "$sudoers_source" | awk '{print $1}')" \
        || fail "cannot digest the controls sudoers source"
    install -o root -g liquidity-migration -m 0755 \
        "$ENGINE_CANDIDATE_BINARY" "$ENGINE_BINARY.new" \
        || fail "cannot stage the release engine binary"
    install -o root -g root -m 0755 \
        "$launcher_source" "$ENGINE_LAUNCHER.new" \
        || fail "cannot stage the trusted runtime launcher"
    install -o root -g root -m 0755 \
        "$helper_source" "$ENGINE_CONTROL_HELPER.new" \
        || fail "cannot stage the Telegram control helper"
    install -o root -g root -m 0440 \
        "$sudoers_source" "$CONTROLS_SUDOERS.new" \
        || fail "cannot stage the controls sudoers fragment"
    /usr/sbin/visudo -cf "$CONTROLS_SUDOERS.new" >/dev/null \
        || fail "staged controls sudoers fragment is invalid"
    digest="$(sha256sum "$ENGINE_BINARY.new" | awk '{print $1}')" \
        || fail "cannot digest the staged engine binary"
    candidate_after="$(sha256sum "$ENGINE_CANDIDATE_BINARY" | awk '{print $1}')" \
        || fail "cannot redigest the engine candidate after staging"
    [ "$digest" = "$candidate_digest" ] && [ "$candidate_after" = "$candidate_digest" ] \
        || fail "engine candidate changed while it was staged"
    launcher_digest="$(sha256sum "$ENGINE_LAUNCHER.new" | awk '{print $1}')" \
        || fail "cannot digest the staged trusted runtime launcher"
    helper_digest="$(sha256sum "$ENGINE_CONTROL_HELPER.new" | awk '{print $1}')" \
        || fail "cannot digest the staged Telegram control helper"
    sudoers_digest="$(sha256sum "$CONTROLS_SUDOERS.new" | awk '{print $1}')" \
        || fail "cannot digest the staged controls sudoers fragment"
    bot_digest="$(sha256sum "$TELEGRAM_CONTROLS_BOT" | awk '{print $1}')" \
        || fail "cannot digest the Telegram controls bot"
    helper_source_after="$(sha256sum "$helper_source" | awk '{print $1}')" \
        || fail "cannot redigest the Telegram control helper source"
    sudoers_source_after="$(sha256sum "$sudoers_source" | awk '{print $1}')" \
        || fail "cannot redigest the controls sudoers source"
    [ "$helper_digest" = "$helper_source_before" ] \
        && [ "$helper_source_after" = "$helper_source_before" ] \
        && [ "$sudoers_digest" = "$sudoers_source_before" ] \
        && [ "$sudoers_source_after" = "$sudoers_source_before" ] \
        || fail "control boundary source changed while it was staged"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$launcher_digest" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$helper_digest" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$sudoers_digest" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$bot_digest" =~ ^[0-9a-f]{64}$ ]] \
        || fail "invalid staged release digest"
    marker_tmp="${ENGINE_BINARY}.release.tmp.$$"
    printf 'commit=%s\nsha256=%s\nlauncher_sha256=%s\ncontrol_helper_sha256=%s\ncontrols_sudoers_sha256=%s\ntelegram_bot_sha256=%s\nrustc=1.90.0\n' \
        "$commit" "$digest" "$launcher_digest" "$helper_digest" \
        "$sudoers_digest" "$bot_digest" > "$marker_tmp" \
        || fail "cannot write engine release marker"
    chown root:root "$marker_tmp" && chmod 0644 "$marker_tmp" \
        || fail "cannot secure engine release marker"
    # The receipt is the commit point. A crash after either artifact rename but
    # before the receipt rename leaves the old receipt in place, so startup
    # rejects the mixed generation.
    mv -f "$ENGINE_BINARY.new" "$ENGINE_BINARY" \
        || fail "cannot atomically install the engine binary"
    mv -f "$ENGINE_LAUNCHER.new" "$ENGINE_LAUNCHER" \
        || fail "cannot atomically install the trusted runtime launcher"
    mv -f "$ENGINE_CONTROL_HELPER.new" "$ENGINE_CONTROL_HELPER" \
        || fail "cannot atomically install the Telegram control helper"
    mv -f "$CONTROLS_SUDOERS.new" "$CONTROLS_SUDOERS" \
        || fail "cannot atomically install the controls sudoers fragment"
    /usr/sbin/visudo -c >/dev/null \
        || fail "installed global sudo policy is invalid"
    verify_controls_sudo_policy
    mv -f "$marker_tmp" "${ENGINE_BINARY}.release" \
        || fail "cannot atomically install engine release marker"
    sync
    verify_engine_release launcher-required
    printf 'engine-build-ok commit=%s sha256=%s launcher_sha256=%s control_helper_sha256=%s controls_sudoers_sha256=%s telegram_bot_sha256=%s binary=%s\n' \
        "$commit" "$digest" "$launcher_digest" "$helper_digest" \
        "$sudoers_digest" "$bot_digest" "$ENGINE_BINARY"
}

verify_engine_release() {
    local launcher_policy="${1:-launcher-optional}"
    local installed_head marker_commit marker_digest marker_launcher_digest
    local marker_helper_digest marker_sudoers_digest marker_bot_digest
    local actual_digest actual_launcher_digest actual_helper_digest
    local actual_sudoers_digest actual_bot_digest marker_has_controls=0
    case "$launcher_policy" in
        launcher-optional|launcher-required) ;;
        *) fail "invalid engine release launcher policy: $launcher_policy" ;;
    esac
    [ -d "${ENGINE_BINARY%/*}" ] && [ ! -L "${ENGINE_BINARY%/*}" ] \
        && [ "$(readlink -f "${ENGINE_BINARY%/*}")" = "${ENGINE_BINARY%/*}" ] \
        && [ "$(stat -c %u "${ENGINE_BINARY%/*}")" -eq 0 ] \
        && [ "$(stat -c %g "${ENGINE_BINARY%/*}")" -eq 0 ] \
        && [ "$(stat -c %a "${ENGINE_BINARY%/*}")" = 755 ] \
        || fail "engine release directory is not the root:root mode 0755 fixed boundary"
    [ -f "$ENGINE_BINARY" ] && [ ! -L "$ENGINE_BINARY" ] \
        && [ -x "$ENGINE_BINARY" ] \
        && [ "$(stat -c %u "$ENGINE_BINARY")" -eq 0 ] \
        && [ "$(stat -c %a "$ENGINE_BINARY")" = 755 ] \
        || fail "required engine binary is not a trusted root-owned regular executable: $ENGINE_BINARY"
    [ -f "${ENGINE_BINARY}.release" ] && [ ! -L "${ENGINE_BINARY}.release" ] \
        && [ "$(stat -c %u "${ENGINE_BINARY}.release")" -eq 0 ] \
        && [ "$(stat -c %g "${ENGINE_BINARY}.release")" -eq 0 ] \
        && [ "$(stat -c %a "${ENGINE_BINARY}.release")" = 644 ] \
        || fail "engine release marker is missing or untrusted"
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
    marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
    marker_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "${ENGINE_BINARY}.release")"
    [[ "$marker_commit" =~ ^[0-9a-f]{40}$ ]] \
        && [ "$marker_commit" = "$installed_head" ] \
        || fail "engine release marker is not bound to installed commit $installed_head"
    [[ "$marker_digest" =~ ^[0-9a-f]{64}$ ]] \
        || fail "engine release marker has an invalid engine digest"
    actual_digest="$(sha256sum "$ENGINE_BINARY" | awk '{print $1}')" \
        || fail "cannot digest installed engine binary"
    [ "$marker_digest" = "$actual_digest" ] \
        || fail "installed engine digest does not match its release marker"
    if [ -n "$marker_launcher_digest" ]; then
        [[ "$marker_launcher_digest" =~ ^[0-9a-f]{64}$ ]] \
            || fail "engine release marker has an invalid launcher digest"
        [ -f "$ENGINE_LAUNCHER" ] && [ ! -L "$ENGINE_LAUNCHER" ] \
            && [ "$(stat -c %u "$ENGINE_LAUNCHER")" -eq 0 ] \
            && [ "$(stat -c %g "$ENGINE_LAUNCHER")" -eq 0 ] \
            && [ "$(stat -c %a "$ENGINE_LAUNCHER")" = 755 ] \
            || fail "trusted runtime launcher is missing, linked, or not root:root mode 0755"
        actual_launcher_digest="$(sha256sum "$ENGINE_LAUNCHER" | awk '{print $1}')" \
            || fail "cannot digest the installed trusted runtime launcher"
        [ "$actual_launcher_digest" = "$marker_launcher_digest" ] \
            || fail "installed launcher digest does not match its release marker"
        if [ -n "$marker_helper_digest$marker_sudoers_digest$marker_bot_digest" ]; then
            [[ "$marker_helper_digest" =~ ^[0-9a-f]{64}$ ]] \
                && [[ "$marker_sudoers_digest" =~ ^[0-9a-f]{64}$ ]] \
                && [[ "$marker_bot_digest" =~ ^[0-9a-f]{64}$ ]] \
                || fail "engine release marker has a partial or invalid control boundary"
            awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
NR == 7 && $0 == "rustc=1.90.0" { next }
{ exit 1 }
END { if (NR != 7) exit 1 }
' "${ENGINE_BINARY}.release" \
                || fail "engine release marker has an invalid control-bound schema"
            [ -f "$ENGINE_CONTROL_HELPER" ] && [ ! -L "$ENGINE_CONTROL_HELPER" ] \
                && [ "$(stat -c %u "$ENGINE_CONTROL_HELPER")" -eq 0 ] \
                && [ "$(stat -c %g "$ENGINE_CONTROL_HELPER")" -eq 0 ] \
                && [ "$(stat -c %a "$ENGINE_CONTROL_HELPER")" = 755 ] \
                || fail "Telegram control helper is missing, linked, or not root:root mode 0755"
            [ -f "$CONTROLS_SUDOERS" ] && [ ! -L "$CONTROLS_SUDOERS" ] \
                && [ "$(stat -c %u "$CONTROLS_SUDOERS")" -eq 0 ] \
                && [ "$(stat -c %g "$CONTROLS_SUDOERS")" -eq 0 ] \
                && [ "$(stat -c %a "$CONTROLS_SUDOERS")" = 440 ] \
                || fail "controls sudoers fragment is missing, linked, or not root:root mode 0440"
            [ -f "$TELEGRAM_CONTROLS_BOT" ] && [ ! -L "$TELEGRAM_CONTROLS_BOT" ] \
                && [ "$(stat -c %u "$TELEGRAM_CONTROLS_BOT")" -eq 0 ] \
                && [ "$(stat -c %g "$TELEGRAM_CONTROLS_BOT")" -eq 0 ] \
                && [ "$(stat -c %a "$TELEGRAM_CONTROLS_BOT")" = 644 ] \
                || fail "Telegram controls bot is missing, linked, or not root:root mode 0644"
            /usr/sbin/visudo -cf "$CONTROLS_SUDOERS" >/dev/null \
                || fail "installed controls sudoers fragment is invalid"
            verify_controls_sudo_policy
            actual_helper_digest="$(sha256sum "$ENGINE_CONTROL_HELPER" | awk '{print $1}')" \
                || fail "cannot digest the installed Telegram control helper"
            actual_sudoers_digest="$(sha256sum "$CONTROLS_SUDOERS" | awk '{print $1}')" \
                || fail "cannot digest the installed controls sudoers fragment"
            actual_bot_digest="$(sha256sum "$TELEGRAM_CONTROLS_BOT" | awk '{print $1}')" \
                || fail "cannot digest the installed Telegram controls bot"
            [ "$actual_helper_digest" = "$marker_helper_digest" ] \
                && [ "$actual_sudoers_digest" = "$marker_sudoers_digest" ] \
                && [ "$actual_bot_digest" = "$marker_bot_digest" ] \
                || fail "installed control boundary differs from its release marker"
            marker_has_controls=1
        else
            awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && $0 == "rustc=1.90.0" { next }
{ exit 1 }
END { if (NR != 4) exit 1 }
' "${ENGINE_BINARY}.release" \
                || fail "legacy launcher release marker schema is invalid"
        fi
    else
        [ -z "$marker_helper_digest$marker_sudoers_digest$marker_bot_digest" ] \
            || fail "engine release marker has controls without a trusted launcher"
        awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && $0 == "rustc=1.90.0" { next }
{ exit 1 }
END { if (NR != 3) exit 1 }
' "${ENGINE_BINARY}.release" \
            || fail "legacy engine release marker schema is invalid"
    fi
    if [ "$launcher_policy" = launcher-required ] \
        && [ "$marker_has_controls" -ne 1 ]; then
        fail "engine release marker predates the complete trusted runtime/control boundary"
    fi
}

# Persistent activation is a commit protocol separate from artifact install.
# A generation may start under a root-watchdog freshness lease while it is
# being verified, but it may survive a reboot only after the root-owned
# completion receipt is atomically installed and synced.
activation_watchdog_running() {
    [ "$(systemctl show -p ActiveState --value "$ACTIVATION_WATCHDOG_UNIT" 2>/dev/null || true)" = active ] \
        && [ "$(systemctl show -p SubState --value "$ACTIVATION_WATCHDOG_UNIT" 2>/dev/null || true)" = running ]
}

stop_activation_watchdog() {
    systemctl stop "$ACTIVATION_WATCHDOG_UNIT" 2>/dev/null || true
    if activation_watchdog_running; then
        return 1
    fi
    systemctl reset-failed "$ACTIVATION_WATCHDOG_UNIT" 2>/dev/null || true
}

start_activation_watchdog() {
    local owner_pid="$1" owner_start_ticks="$2"
    stop_activation_watchdog \
        || fail "cannot stop the prior activation lease watchdog"
    /usr/bin/systemd-run --quiet --collect --service-type=exec \
        --unit="$ACTIVATION_WATCHDOG_UNIT" \
        --property=User=root \
        --property=Group=root \
        --property=WorkingDirectory=/ \
        --property=Restart=no \
        --property=KillMode=control-group \
        --property=TimeoutStopSec=10s \
        --property=RuntimeMaxSec=2h \
        --property=Nice=19 \
        --property=NoNewPrivileges=true \
        --property=PrivateDevices=true \
        --property=PrivateNetwork=true \
        --property=PrivateTmp=true \
        --property=ProtectClock=true \
        --property=ProtectControlGroups=true \
        --property=ProtectHome=true \
        --property=ProtectKernelLogs=true \
        --property=ProtectKernelModules=true \
        --property=ProtectKernelTunables=true \
        --property=ProtectSystem=strict \
        --property=RestrictAddressFamilies=AF_UNIX \
        --property=RestrictRealtime=true \
        --property=RestrictSUIDSGID=true \
        --property=LockPersonality=true \
        --property=MemoryDenyWriteExecute=true \
        --property=UMask=0022 \
        --property="ReadWritePaths=${ACTIVATION_PERMIT%/*}" \
        --property="InaccessiblePaths=-/etc/liquidity-migration" \
        /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
            "$ENGINE_LAUNCHER" --activation-watchdog \
            "$owner_pid" "$owner_start_ticks" \
        || fail "cannot start the root activation lease watchdog"
    activation_watchdog_running \
        || fail "activation lease watchdog did not remain active"
}

activation_authority_matches_unlocked() {
    local path="$1" kind="$2" marker_commit marker_digest marker_launcher_digest
    local marker_helper_digest marker_sudoers_digest marker_bot_digest
    local file_commit file_digest file_launcher_digest file_helper_digest
    local file_sudoers_digest file_bot_digest file_boot_id file_owner_pid
    local file_owner_start_ticks file_owner_stat file_owner_tail file_not_after
    local current_epoch
    local -a file_owner_fields=()
    local control_bound=0
    [ -f "$path" ] && [ ! -L "$path" ] \
        && [ "$(stat -c %u "$path")" -eq 0 ] \
        && [ "$(stat -c %g "$path")" -eq 0 ] \
        && [ "$(stat -c %a "$path")" = 644 ] \
        || return 1
    marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
    marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
    marker_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "${ENGINE_BINARY}.release")"
    [[ "$marker_commit" =~ ^[0-9a-f]{40}$ ]] \
        && [[ "$marker_digest" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$marker_launcher_digest" =~ ^[0-9a-f]{64}$ ]] \
        || return 1
    if [ -n "$marker_helper_digest$marker_sudoers_digest$marker_bot_digest" ]; then
        [[ "$marker_helper_digest" =~ ^[0-9a-f]{64}$ ]] \
            && [[ "$marker_sudoers_digest" =~ ^[0-9a-f]{64}$ ]] \
            && [[ "$marker_bot_digest" =~ ^[0-9a-f]{64}$ ]] \
            || return 1
        control_bound=1
    fi
    file_commit="$(sed -n 's/^commit=//p' "$path")"
    file_digest="$(sed -n 's/^sha256=//p' "$path")"
    file_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "$path")"
    [ "$file_commit" = "$marker_commit" ] \
        && [ "$file_digest" = "$marker_digest" ] \
        && [ "$file_launcher_digest" = "$marker_launcher_digest" ] \
        || return 1
    if [ "$control_bound" -eq 1 ]; then
        file_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "$path")"
        file_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "$path")"
        file_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "$path")"
        [ "$file_helper_digest" = "$marker_helper_digest" ] \
            && [ "$file_sudoers_digest" = "$marker_sudoers_digest" ] \
            && [ "$file_bot_digest" = "$marker_bot_digest" ] \
            || return 1
    fi
    case "$kind" in
        complete)
            if [ "$control_bound" -eq 1 ]; then
                awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
{ exit 1 }
END { if (NR != 6) exit 1 }
' "$path" >/dev/null
            else
                awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
{ exit 1 }
END { if (NR != 3) exit 1 }
' "$path" >/dev/null
            fi
            ;;
        permit)
            [ -d "${ACTIVATION_PERMIT%/*}" ] \
                && [ ! -L "${ACTIVATION_PERMIT%/*}" ] \
                && [ "$(stat -c %u "${ACTIVATION_PERMIT%/*}")" -eq 0 ] \
                && [ "$(stat -c %g "${ACTIVATION_PERMIT%/*}")" -eq 0 ] \
                && [ "$(stat -c %a "${ACTIVATION_PERMIT%/*}")" = 755 ] \
                || return 1
            [ "$control_bound" -eq 1 ] || return 1
            awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
NR == 7 && /^boot_id=/ { next }
NR == 8 && /^owner_pid=/ { next }
NR == 9 && /^owner_start_ticks=/ { next }
NR == 10 && /^not_after_epoch=/ { next }
{ exit 1 }
END { if (NR != 10) exit 1 }
' "$path" >/dev/null || return 1
            file_boot_id="$(sed -n 's/^boot_id=//p' "$path")"
            file_owner_pid="$(sed -n 's/^owner_pid=//p' "$path")"
            file_owner_start_ticks="$(sed -n 's/^owner_start_ticks=//p' "$path")"
            file_not_after="$(sed -n 's/^not_after_epoch=//p' "$path")"
            [[ "$file_boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
                && [ "$file_boot_id" = "$(cat /proc/sys/kernel/random/boot_id)" ] \
                && [[ "$file_owner_pid" =~ ^[1-9][0-9]*$ ]] \
                && [[ "$file_owner_start_ticks" =~ ^[1-9][0-9]*$ ]] \
                && [[ "$file_not_after" =~ ^[0-9]{10,}$ ]] \
                || return 1
            current_epoch="$(date -u +%s)" || return 1
            [ "$current_epoch" -lt "$file_not_after" ] \
                && [ "$file_not_after" -le "$((current_epoch + ACTIVATION_LEASE_SECONDS + 2))" ] \
                && activation_watchdog_running \
                || return 1
            [ -r "/proc/$file_owner_pid/stat" ] \
                || return 1
            file_owner_stat="$(<"/proc/$file_owner_pid/stat")" \
                || return 1
            file_owner_tail="${file_owner_stat##*) }"
            read -r -a file_owner_fields <<< "$file_owner_tail" \
                || return 1
            [ "${#file_owner_fields[@]}" -ge 20 ] \
                && [ "${file_owner_fields[0]}" != Z ] \
                && [ "${file_owner_fields[0]}" != X ] \
                && [ "${file_owner_fields[19]}" = "$file_owner_start_ticks" ]
            ;;
        *) return 1 ;;
    esac
}

activation_authority_matches() {
    local path="$1" kind="$2" authority_fd descriptor_path status=1
    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    exec {authority_fd}<"$path" || return 1
    descriptor_path="/proc/self/fd/$authority_fd"
    if /usr/bin/flock -s "$authority_fd" \
        && [ "$path" -ef "$descriptor_path" ] \
        && [ "$(stat -Lc %h "$descriptor_path")" -eq 1 ] \
        && activation_authority_matches_unlocked "$path" "$kind" \
        && [ "$path" -ef "$descriptor_path" ]; then
        status=0
    fi
    /usr/bin/flock -u "$authority_fd" || status=1
    exec {authority_fd}<&- || status=1
    return "$status"
}

invalidate_activation_authority() {
    local path
    stop_activation_watchdog \
        || fail "cannot stop the activation lease watchdog"
    for path in "$ACTIVATION_PERMIT" "$ACTIVATION_RECEIPT"; do
        [ ! -L "$path" ] || fail "activation authority path is linked: $path"
        rm -f -- "$path" || fail "cannot invalidate activation authority: $path"
    done
    sync
}

begin_activation_generation() {
    local marker_commit marker_digest marker_launcher_digest marker_helper_digest
    local marker_sudoers_digest marker_bot_digest boot_id owner_pid owner_stat
    local owner_tail owner_start_ticks not_after temporary
    local -a owner_fields=()
    verify_engine_release launcher-required
    invalidate_activation_authority
    marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
    marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
    marker_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "${ENGINE_BINARY}.release")"
    boot_id="$(cat /proc/sys/kernel/random/boot_id)" \
        || fail "cannot bind the activation permit to this boot"
    [[ "$boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
        || fail "kernel boot id is not canonical"
    owner_pid="$$"
    owner_stat="$(<"/proc/$owner_pid/stat")" \
        || fail "cannot bind the activation permit to its deployment owner"
    owner_tail="${owner_stat##*) }"
    read -r -a owner_fields <<< "$owner_tail" \
        || fail "cannot parse the activation owner process identity"
    [ "${#owner_fields[@]}" -ge 20 ] \
        || fail "activation owner process identity is incomplete"
    owner_start_ticks="${owner_fields[19]}"
    [[ "$owner_pid" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$owner_start_ticks" =~ ^[1-9][0-9]*$ ]] \
        || fail "activation owner process identity is invalid"
    # This is only an initial freshness lease. The root watchdog validates the
    # owner PID/start-ticks pair and refreshes it once per second; if either the
    # rollout or watchdog dies, launchers revoke within a few seconds.
    not_after="$(( $(date -u +%s) + ACTIVATION_LEASE_SECONDS ))"
    temporary="$(mktemp "${ACTIVATION_PERMIT}.tmp.XXXXXX")" \
        || fail "cannot stage the transient activation permit"
    printf 'commit=%s\nsha256=%s\nlauncher_sha256=%s\ncontrol_helper_sha256=%s\ncontrols_sudoers_sha256=%s\ntelegram_bot_sha256=%s\nboot_id=%s\nowner_pid=%s\nowner_start_ticks=%s\nnot_after_epoch=%s\n' \
        "$marker_commit" "$marker_digest" "$marker_launcher_digest" \
        "$marker_helper_digest" "$marker_sudoers_digest" "$marker_bot_digest" \
        "$boot_id" "$owner_pid" "$owner_start_ticks" "$not_after" > "$temporary" \
        || fail "cannot write the transient activation permit"
    chown root:root "$temporary" && chmod 0644 "$temporary" \
        || fail "cannot secure the transient activation permit"
    mv -f "$temporary" "$ACTIVATION_PERMIT" \
        || fail "cannot atomically install the transient activation permit"
    start_activation_watchdog "$owner_pid" "$owner_start_ticks"
    sync
    activation_authority_matches "$ACTIVATION_PERMIT" permit \
        || fail "transient activation permit failed its installed validation"
}

complete_activation_generation() {
    local marker_commit marker_digest marker_launcher_digest marker_helper_digest
    local marker_sudoers_digest marker_bot_digest temporary
    verify_engine_release launcher-required
    activation_authority_matches "$ACTIVATION_PERMIT" permit \
        || fail "activation permit expired or changed before completion"
    marker_commit="$(sed -n 's/^commit=//p' "${ENGINE_BINARY}.release")"
    marker_digest="$(sed -n 's/^sha256=//p' "${ENGINE_BINARY}.release")"
    marker_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "${ENGINE_BINARY}.release")"
    marker_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "${ENGINE_BINARY}.release")"
    # Flush every enable symlink and service-side state verified immediately
    # before this call. The receipt rename is the persistent activation commit.
    sync
    temporary="$(mktemp "${ACTIVATION_RECEIPT}.tmp.XXXXXX")" \
        || fail "cannot stage the activation completion receipt"
    printf 'commit=%s\nsha256=%s\nlauncher_sha256=%s\ncontrol_helper_sha256=%s\ncontrols_sudoers_sha256=%s\ntelegram_bot_sha256=%s\n' \
        "$marker_commit" "$marker_digest" "$marker_launcher_digest" \
        "$marker_helper_digest" "$marker_sudoers_digest" "$marker_bot_digest" \
        > "$temporary" \
        || fail "cannot write the activation completion receipt"
    chown root:root "$temporary" && chmod 0644 "$temporary" \
        || fail "cannot secure the activation completion receipt"
    mv -f "$temporary" "$ACTIVATION_RECEIPT" \
        || fail "cannot atomically install the activation completion receipt"
    sync
    activation_authority_matches "$ACTIVATION_RECEIPT" complete \
        || fail "activation completion receipt failed its installed validation"
    # The durable receipt is visible before terminating the temporary lease.
    # Launchers accept either authority, so this handoff has no rejection gap.
    stop_activation_watchdog \
        || fail "cannot stop the completed activation lease watchdog"
    rm -f -- "$ACTIVATION_PERMIT" \
        || fail "cannot retire the transient activation permit"
    sync
    printf 'activation-complete commit=%s sha256=%s launcher_sha256=%s control_helper_sha256=%s controls_sudoers_sha256=%s telegram_bot_sha256=%s\n' \
        "$marker_commit" "$marker_digest" "$marker_launcher_digest" \
        "$marker_helper_digest" "$marker_sudoers_digest" "$marker_bot_digest"
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
        "exodus": Path("/var/lib/liquidity-migration/targets/exodus-mainnet.json"),
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
        mainnet) set -- long-mainnet.json carry-mainnet.json exodus-mainnet.json ;;
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
    verify_engine_release launcher-required
    quarantine_engine_inputs "$env_file" "$realm"
    systemctl enable "$unit" || fail "cannot enable required Rust engine $unit"
    systemctl start "$unit" || fail "cannot start required Rust engine $unit"
    wait_engine_heartbeat "$env_file" "$realm"
}
activate_mode() {
    local producer_started_ns
    require_rollout_for_funded_generation_change activate
    load_authorization
    resolve_stop_first
    require_quiescent
    begin_activation_generation

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
    if systemctl is-failed --quiet liquidity-migration-demo-liveness.service; then
        fail "the immediate demo liveness pass failed"
    fi
    systemctl enable --now liquidity-migration-telegram-controls.service \
        || fail "cannot start Telegram controls"
    systemctl enable --now liquidity-migration-llm-ledger.timer \
        || fail "cannot start the LLM ledger timer"
    systemctl enable --now liquidity-migration-trade-notify.timer \
        || fail "cannot start the trade notification timer"
    systemctl enable --now liquidity-migration-forward-capture.service \
        || fail "cannot start forward market capture"
    systemctl enable --now liquidity-migration-forward-upload.timer \
        || fail "cannot enable the forward market upload timer"
    systemctl enable --now liquidity-migration-backup.timer \
        || fail "cannot enable the nightly backup timer"
    systemctl enable --now liquidity-migration-chaos-drill.timer \
        || fail "cannot enable the weekly chaos drill timer"
    if mainnet_armed; then
        start_mainnet_fleet
    fi
    verify_topology activation-in-progress
    complete_activation_generation
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

    install -d -o root -g "$RUNTIME_GROUP" -m 0750 \
        "$(dirname "$risk_policy_file")" \
        || fail "cannot create the mainnet producer input directory"
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
    local producer_started_ns
    provision_mainnet_prerequisites
    require_mainnet_preflight
    install -d -o "$PRODUCER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        "$LONG_MAINNET_ROOT" "$CARRY_MAINNET_ROOT" /var/lib/liquidity-migration/targets
    start_required_engine "$MAINNET_OWNER_UNIT" /etc/liquidity-migration/engine-mainnet.env mainnet
    producer_started_ns="$(date +%s%N)"
    systemctl enable --now liquidity-migration-bybit-carry-mainnet.service \
        || fail "cannot start the funded carry target producer"
    systemctl enable --now liquidity-migration-bybit-long-mainnet.service \
        || fail "cannot start the funded LONG target producer"
    wait_fresh_producer_book liquidity-migration-bybit-carry-mainnet.service \
        "$CARRY_MAINNET_ROOT" /var/lib/liquidity-migration/targets/carry-mainnet.json \
        mainnet "$producer_started_ns"
    wait_fresh_producer_book liquidity-migration-bybit-carry-mainnet.service \
        "$CARRY_MAINNET_ROOT" /var/lib/liquidity-migration/targets/exodus-mainnet.json \
        mainnet "$producer_started_ns"
    wait_fresh_producer_book liquidity-migration-bybit-long-mainnet.service \
        "$LONG_MAINNET_ROOT" /var/lib/liquidity-migration/targets/long-mainnet.json \
        mainnet "$producer_started_ns"
    systemctl enable --now "$MAINNET_LIVENESS_TIMER" \
        || fail "cannot enable the funded liveness timer"
    systemctl start "$MAINNET_LIVENESS_SERVICE" \
        || fail "the immediate funded liveness pass failed to start"
    if systemctl is-failed --quiet "$MAINNET_LIVENESS_SERVICE"; then
        fail "the immediate funded liveness pass failed"
    fi
}
resolve_fail_safe_python() {
    local interpreter mode directory
    interpreter="$(/usr/bin/readlink -f /usr/bin/python3)" || return 1
    [[ "$interpreter" =~ ^/usr/bin/python3(\.[0-9]+)?$ ]] || return 1
    [ -f "$interpreter" ] && [ ! -L "$interpreter" ] \
        && [ -x "$interpreter" ] \
        && [ "$(/usr/bin/stat -c %u "$interpreter")" -eq 0 ] \
        && [ "$(/usr/bin/stat -c %g "$interpreter")" -eq 0 ] \
        || return 1
    mode="$(/usr/bin/stat -c %a "$interpreter")" || return 1
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 0022) == 0 )) \
        || return 1
    for directory in /usr /usr/bin; do
        [ -d "$directory" ] && [ ! -L "$directory" ] \
            && [ "$(/usr/bin/stat -c %u "$directory")" -eq 0 ] \
            && [ "$(/usr/bin/stat -c %g "$directory")" -eq 0 ] \
            || return 1
        mode="$(/usr/bin/stat -c %a "$directory")" || return 1
        [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 0022) == 0 )) \
            || return 1
    done
    printf '%s\n' "$interpreter"
}

# Emergency containment must not trust the checkout: it operates only on this
# fixed unit allowlist through PID 1, then proves every installed unit inactive
# and persistently disabled. The caller may subsequently change credentials,
# but a failed credential rewrite still leaves the funded fleet quarantined.
quarantine_mainnet_units() {
    local unit load_state failures=0
    local -a units=(
        "$MAINNET_LIVENESS_TIMER"
        "$MAINNET_LIVENESS_SERVICE"
        liquidity-migration-bybit-carry-mainnet.service
        liquidity-migration-bybit-long-mainnet.service
        "$MAINNET_OWNER_UNIT"
    )
    [ -x /usr/bin/systemctl ] || return 1
    for unit in "${units[@]}"; do
        load_state="$(/usr/bin/systemctl show --property=LoadState --value "$unit" 2>/dev/null)" \
            || { printf 'stop-failed unit=%s reason=load-state-unavailable\n' "$unit" >&2; failures=1; continue; }
        if [ "$load_state" = not-found ]; then
            printf 'stop-skipped unit=%s reason=not-installed\n' "$unit"
            continue
        fi
        /usr/bin/systemctl disable --now "$unit" 2>/dev/null || true
    done
    /bin/sync || failures=1
    for unit in "${units[@]}"; do
        load_state="$(/usr/bin/systemctl show --property=LoadState --value "$unit" 2>/dev/null)" \
            || { printf 'stop-failed unit=%s reason=verification-unavailable\n' "$unit" >&2; failures=1; continue; }
        [ "$load_state" = not-found ] && continue
        if /usr/bin/systemctl is-active --quiet "$unit"; then
            printf 'stop-failed unit=%s reason=still-active\n' "$unit" >&2
            failures=1
        fi
        if /usr/bin/systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            printf 'stop-failed unit=%s reason=still-enabled\n' "$unit" >&2
            failures=1
        fi
        /usr/bin/systemctl reset-failed "$unit" 2>/dev/null || true
        printf 'stopped unit=%s\n' "$unit"
    done
    [ "$failures" -eq 0 ]
}

disarm_mainnet_mode() {
    local fail_safe_python
    quarantine_mainnet_units \
        || fail "cannot prove the funded units inactive and disabled"
    fail_safe_python="$(resolve_fail_safe_python)" \
        || fail "the fixed root-owned system Python boundary is unavailable"
    # This parser is intentionally embedded and standard-library-only. A
    # compromised checkout or virtualenv must not gain execution as root while
    # the disarm path reads the funded credential file.
    /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
        "$fail_safe_python" -I -S - "$MAINNET_CREDENTIAL_ENV" <<'PY'
import os
import re
import shlex
import stat
import sys
import tempfile

MAX_FILE_BYTES = 1024 * 1024
KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")


class DisarmError(Exception):
    pass


def checked_snapshot(path: str) -> tuple[bytes, os.stat_result]:
    parent = os.path.dirname(path)
    parent_stat = os.lstat(parent)
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != 0
        or parent_stat.st_gid != 0
        or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise DisarmError("unsafe credential directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_FILE_BYTES
        ):
            raise DisarmError("unsafe credential file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                raise DisarmError("short credential read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise DisarmError("credential grew while reading")
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise DisarmError("credential changed while reading")
        return b"".join(chunks), before
    finally:
        os.close(descriptor)


def parse_environment(data: bytes) -> dict[str, str]:
    if len(data) > MAX_FILE_BYTES or b"\0" in data or b"\r" in data:
        raise DisarmError("credential contains invalid bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DisarmError("credential is not UTF-8") from error
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, raw_value = line.partition("=")
        if separator != "=" or not KEY_PATTERN.fullmatch(key) or key in values:
            raise DisarmError("credential assignment is invalid or repeated")
        if "\\" in raw_value:
            raise DisarmError("credential uses unsupported escape syntax")
        try:
            parsed = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as error:
            raise DisarmError("credential quoting is invalid") from error
        if len(parsed) > 1:
            raise DisarmError("credential value is ambiguous")
        values[key] = "" if not parsed else parsed[0]
    return values


def replace_disarmed(path: str, values: dict[str, str], original: os.stat_result) -> None:
    parent = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), 0, 0)
            for key, value in sorted(values.items()):
                handle.write(f"{key}={shlex.quote(value)}\n")
            handle.flush()
            os.fsync(handle.fileno())
        current = os.lstat(path)
        if (
            current.st_dev != original.st_dev
            or current.st_ino != original.st_ino
            or current.st_size != original.st_size
            or current.st_mtime_ns != original.st_mtime_ns
            or current.st_ctime_ns != original.st_ctime_ns
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != 0
            or current.st_gid != 0
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
        ):
            raise DisarmError("credential changed before replacement")
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


try:
    credential_path = sys.argv[1]
    payload, source_stat = checked_snapshot(credential_path)
    environment = parse_environment(payload)
    environment["REAL_MONEY"] = "false"
    replace_disarmed(credential_path, environment, source_stat)
except (DisarmError, OSError) as error:
    print(f"disarm refused: {type(error).__name__}", file=sys.stderr)
    raise SystemExit(2)
PY
    MAINNET_ARMED_STATE=off
    echo "disarm-mainnet-ok real_money=false units=inactive-and-disabled"
    echo "note: disarm does not flatten existing exposure; reconcile/flatten separately"
}

stop_mainnet_mode() {
    quarantine_mainnet_units \
        || fail "cannot prove the funded units inactive and disabled"
    echo "stop-mainnet-ok"
    echo "note: this stopped publication only; exposure is unchanged. Flatten through the account owner."
    echo "note: REAL_MONEY was not read; run disarm-mainnet to remove arming at the credential boundary."
}

ROLLOUT_DOWNSTREAM_UNITS=(
    liquidity-migration-forward-upload.timer
    liquidity-migration-forward-upload.service
    liquidity-migration-forward-capture.service
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
ROLLOUT_CANCELLATION_SIGNAL=""
ROLLOUT_PRIOR_ACTIVE_UNITS=()
ROLLOUT_PRIOR_ENABLED_UNITS=()
ROLLOUT_PRIOR_RUNTIME_ENABLED_UNITS=()

snapshot_prior_topology() {
    local unit enabled
    ROLLOUT_PRIOR_ACTIVE_UNITS=()
    ROLLOUT_PRIOR_ENABLED_UNITS=()
    ROLLOUT_PRIOR_RUNTIME_ENABLED_UNITS=()
    for unit in "${ROLLOUT_OWNER_UNITS[@]}" "${ROLLOUT_DOWNSTREAM_UNITS[@]}"; do
        systemctl cat "$unit" >/dev/null 2>&1 || continue
        if systemctl is-active --quiet "$unit"; then
            ROLLOUT_PRIOR_ACTIVE_UNITS+=("$unit")
        fi
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
        case "$enabled" in
            enabled)
                ROLLOUT_PRIOR_ENABLED_UNITS+=("$unit")
                ;;
            enabled-runtime)
                ROLLOUT_PRIOR_RUNTIME_ENABLED_UNITS+=("$unit")
                ;;
            disabled|static|indirect|masked|masked-runtime) ;;
            linked|linked-runtime|alias|generated|transient)
                fail "rollout cannot preserve unsupported enablement state for $unit: $enabled"
                ;;
            *) fail "rollout cannot classify enablement state for $unit: ${enabled:-unknown}" ;;
        esac
    done
    printf 'prior-topology-ok active=%s enabled=%s enabled_runtime=%s\n' \
        "${#ROLLOUT_PRIOR_ACTIVE_UNITS[@]}" "${#ROLLOUT_PRIOR_ENABLED_UNITS[@]}" \
        "${#ROLLOUT_PRIOR_RUNTIME_ENABLED_UNITS[@]}"
}

prior_topology_contains() {
    local needle="$1" candidate
    shift
    for candidate in "$@"; do
        [ "$candidate" = "$needle" ] && return 0
    done
    return 1
}

restore_prior_topology_snapshot() {
    local unit index
    systemctl daemon-reload || return 1
    for unit in "${ROLLOUT_PRIOR_ENABLED_UNITS[@]}"; do
        systemctl enable "$unit" || return 1
    done
    for unit in "${ROLLOUT_PRIOR_RUNTIME_ENABLED_UNITS[@]}"; do
        systemctl enable --runtime "$unit" || return 1
    done
    for unit in "${ROLLOUT_OWNER_UNITS[@]}"; do
        prior_topology_contains "$unit" "${ROLLOUT_PRIOR_ACTIVE_UNITS[@]}" || continue
        systemctl start "$unit" || return 1
    done
    for ((index=${#ROLLOUT_DOWNSTREAM_UNITS[@]} - 1; index >= 0; index--)); do
        unit="${ROLLOUT_DOWNSTREAM_UNITS[$index]}"
        prior_topology_contains "$unit" "${ROLLOUT_PRIOR_ACTIVE_UNITS[@]}" || continue
        systemctl start "$unit" || return 1
    done
    for unit in "${ROLLOUT_PRIOR_ACTIVE_UNITS[@]}"; do
        systemctl is-active --quiet "$unit" || return 1
    done
    for unit in "${ROLLOUT_PRIOR_ENABLED_UNITS[@]}"; do
        [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" = enabled ] || return 1
    done
    for unit in "${ROLLOUT_PRIOR_RUNTIME_ENABLED_UNITS[@]}"; do
        [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" = enabled-runtime ] || return 1
    done
}

restore_prior_topology() {
    stop_all_rollout_units_best_effort || return 1
    if [ -f "${ENGINE_BINARY}.release" ]; then
        (
            EXPECTED_COMMIT="$ROLLOUT_CURRENT_COMMIT"
            # The boot fence deliberately revoked the old receipt. Rebind the
            # unchanged incumbent release, start only the units observed before
            # the rollout, then commit that exact restored generation.
            begin_activation_generation
            restore_prior_topology_snapshot
            complete_activation_generation
        )
    else
        restore_prior_topology_snapshot
    fi
}

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

disable_rollout_units_for_boot_fence() {
    local unit enabled
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
            case "$enabled" in
                static|indirect|masked|masked-runtime) ;;
                *)
                    systemctl disable "$unit" 2>/dev/null \
                        || fail "cannot persistently disable rollout unit before mutation: $unit"
                    ;;
            esac
        fi
    done
    systemctl daemon-reload \
        || fail "cannot reload the persistent rollout boot fence"
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        ! systemctl is-active --quiet "$unit" \
            || fail "boot-fenced rollout unit is still active: $unit"
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
        case "$enabled" in
            disabled|static|indirect|masked|masked-runtime|not-found) ;;
            *) fail "rollout unit remains boot-enabled after persistent fence: $unit ($enabled)" ;;
        esac
    done
    invalidate_activation_authority
    sync
    echo "rollout-boot-fence-ok units=inactive-and-disabled"
}

stop_all_rollout_units_best_effort() {
    local unit enabled path failed=0
    # Remove both the transient and persistent startup authorities before
    # stopping anything. If cleanup itself is interrupted, an enabled unit can
    # no longer cross the trusted launcher on the next boot.
    if ! stop_activation_watchdog; then
        cleanup_notice "failed-to-stop-activation-watchdog unit=$ACTIVATION_WATCHDOG_UNIT"
        failed=1
    fi
    for path in "$ACTIVATION_PERMIT" "$ACTIVATION_RECEIPT"; do
        if [ -L "$path" ]; then
            cleanup_notice "linked-activation-authority path=$path"
            failed=1
        elif ! rm -f -- "$path"; then
            cleanup_notice "failed-to-remove-activation-authority path=$path"
            failed=1
        fi
    done
    sync
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        # A unit introduced by the commit being deployed is not installed yet;
        # counting its stop as a failure would demote a recoverable pre-install
        # abort into a forced full-fleet stop.
        systemctl cat "$unit" >/dev/null 2>&1 || continue
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
        case "$enabled" in
            static|indirect)
                if ! systemctl stop "$unit"; then
                    cleanup_notice "failed-to-stop static-unit=$unit"
                    failed=1
                fi
                ;;
            *)
                if ! systemctl disable --now "$unit"; then
                    cleanup_notice "failed-to-disable-and-stop unit=$unit"
                    systemctl stop "$unit" 2>/dev/null || true
                    failed=1
                fi
                ;;
        esac
    done
    if ! systemctl daemon-reload; then
        cleanup_notice "failed-to-reload-systemd-after-rollout-quarantine"
        failed=1
    fi
    for unit in "${ROLLOUT_DOWNSTREAM_UNITS[@]}" "${ROLLOUT_OWNER_UNITS[@]}"; do
        if systemctl is-active --quiet "$unit"; then
            cleanup_notice "still-active unit=$unit"
            failed=1
        else
            # A verifiably stopped unit must not carry a stale `failed` flag
            # into staged recovery.
            systemctl reset-failed "$unit" 2>/dev/null || true
        fi
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
        case "$enabled" in
            disabled|static|indirect|masked|masked-runtime|not-found) ;;
            *) cleanup_notice "still-boot-enabled unit=$unit state=$enabled"; failed=1 ;;
        esac
    done
    sync
    return "$failed"
}

# If the transport dies mid-rollout, sshd HUPs the remote process group and
# every write to the dead pipe raises SIGPIPE. Mirror each line into the host
# journal, and never let a failed write abort the stop sequence.
cleanup_notice() {
    logger -t liquidity-migration-deploy -p daemon.err -- "$*" 2>/dev/null || true
    printf '%s\n' "$*" >&2 2>/dev/null || true
}

deploy_cancel() {
    local signal="$1" status="$2"
    : "$signal"
    exit "$status"
}

deploy_cleanup() {
    local status="$?"
    trap - EXIT INT TERM HUP PIPE
    trap '' INT TERM HUP PIPE
    set +e
    if ! stop_active_engine_builder_unit; then
        cleanup_notice "cannot stop tracked transient builder unit=$ENGINE_ACTIVE_BUILDER_UNIT"
        [ "$status" -ne 0 ] || status=1
    fi
    if ! remove_deploy_venv_staging; then
        cleanup_notice "cannot remove Python environment staging generation=$DEPLOY_VENV_STAGING"
        [ "$status" -ne 0 ] || status=1
    fi
    exit "$status"
}

rollout_cancel() {
    local signal="$1" status="$2"
    ROLLOUT_CANCELLATION_SIGNAL="$signal"
    exit "$status"
}

rollout_cleanup() {
    local status="$?"
    local cancellation_signal="$ROLLOUT_CANCELLATION_SIGNAL"
    trap - EXIT INT TERM HUP PIPE
    # A second signal, or a write to a dead stdout, must not interrupt the
    # fail-closed handoff.
    trap '' INT TERM HUP PIPE
    set +e
    if ! stop_active_engine_builder_unit; then
        cleanup_notice "cannot stop tracked transient builder unit=$ENGINE_ACTIVE_BUILDER_UNIT"
        [ "$status" -ne 0 ] || status=1
    fi
    if ! remove_deploy_venv_staging; then
        cleanup_notice "cannot remove Python environment staging generation=$DEPLOY_VENV_STAGING"
        [ "$status" -ne 0 ] || status=1
    fi
    if [ "$status" -ne 0 ] && [ -n "$cancellation_signal" ] \
        && [ "$ROLLOUT_STOPPED" -eq 1 ]; then
        cleanup_notice \
            "rollout canceled signal=$cancellation_signal; forcing the managed fleet stopped"
        stop_all_rollout_units_best_effort || true
    elif [ "$status" -ne 0 ] && [ -n "$cancellation_signal" ]; then
        cleanup_notice \
            "rollout canceled signal=$cancellation_signal before fleet stop; incumbent topology untouched"
    elif [ "$status" -ne 0 ] && [ "$ROLLOUT_STOPPED" -eq 1 ] \
        && [ "$ROLLOUT_COMPLETE" -eq 0 ]; then
        if [ "$ROLLOUT_IRREVERSIBLE" -eq 0 ]; then
            cleanup_notice \
                'rollout failed before install; restoring the prior topology'
            if restore_prior_topology; then
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
    require_trusted_checkout
    local installed_head remote_tip
    DEPLOY_PREFETCHED_COMMIT=""
    DEPLOY_PREFETCHED_REMOTE_TIP=""
    stop_stale_engine_builder_units
    installed_head="$(safe_git rev-parse HEAD)" || fail "cannot read installed checkout HEAD"
    require_clean_checkout_at "$installed_head" "rollout prefetch"
    if safe_git remote get-url "$REMOTE" >/dev/null 2>&1; then
        safe_git remote set-url "$REMOTE" "$REPO_URL" \
            || fail "cannot set the rollout prefetch remote"
    else
        safe_git remote add "$REMOTE" "$REPO_URL" \
            || fail "cannot add the rollout prefetch remote"
    fi
    git_fetch fetch "$REMOTE" "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH" \
        || fail "cannot fetch the rollout target branch"
    safe_git cat-file -e "$EXPECTED_COMMIT^{commit}" 2>/dev/null \
        || fail "expected commit is unavailable"
    safe_git merge-base --is-ancestor "$EXPECTED_COMMIT" "$REMOTE/$BRANCH" \
        || fail "expected commit is not on $REMOTE/$BRANCH"
    remote_tip="$(safe_git rev-parse "$REMOTE/$BRANCH")" \
        || fail "cannot bind the fetched target branch"
    [[ "$remote_tip" =~ ^[0-9a-f]{40}$ ]] \
        || fail "fetched target branch binding is invalid"
    require_pinned_engine_toolchain
    compile_engine_commit "$EXPECTED_COMMIT"
    prefetch_python_dependencies
    [ "$(safe_git rev-parse "$REMOTE/$BRANCH")" = "$remote_tip" ] \
        || fail "cached target branch changed during prefetch"
    require_clean_checkout_at "$installed_head" "rollout prefetch completion"
    DEPLOY_PREFETCHED_COMMIT="$EXPECTED_COMMIT"
    DEPLOY_PREFETCHED_REMOTE_TIP="$remote_tip"
    verify_prefetched_deploy_inputs "$EXPECTED_COMMIT" source
}

record_installed_profile() {
    printf '%s\n' "$DEPLOY_PROFILE" > "$PROFILE_MARKER"
    chmod 0644 "$PROFILE_MARKER"
}

# install + activate in one remote session, without the rollout's rollback
# machinery. The profile marker is written here too: a
# staged install that skipped it left load_authorization falling back to
# "operational" whatever the operator asked for.
staged_mode() {
    require_rollout_for_funded_generation_change staged
    run_strict_phase staged-target-prefetch prefetch_rollout_target
    run_strict_phase staged-install install_mode
    run_strict_phase record-installed-profile record_installed_profile
    run_strict_phase staged-activate-and-verify activate_mode

    printf 'staged-ok commit=%s profile=%s\n' "$EXPECTED_COMMIT" "$DEPLOY_PROFILE"
}

rollout_mode() {
    require_trusted_checkout
    ROLLOUT_FUNDED_AUTHORITY=1
    ROLLOUT_CURRENT_COMMIT="$(safe_git rev-parse HEAD)" \
        || fail "cannot read installed checkout HEAD"

    # Install cleanup before any work that can stop or mutate the fleet.
    trap rollout_cleanup EXIT
    trap 'rollout_cancel INT 130' INT
    trap 'rollout_cancel TERM 143' TERM
    trap 'rollout_cancel HUP 129' HUP
    trap 'rollout_cancel PIPE 141' PIPE
    run_strict_phase rollout-target-prefetch prefetch_rollout_target
    run_strict_phase snapshot-prior-topology snapshot_prior_topology

    ROLLOUT_STOPPED=1
    # bash skips the EXIT trap on an untrapped fatal signal, so an SSH client
    # death (HUP, then SIGPIPE) would leave the fleet half-stopped uncleaned.

    # Stop every producer/timer before either owner. Activation quarantines
    # every old book before admitting work from the new service generation.
    run_strict_phase stop-downstream-units \
        stop_rollout_units "${ROLLOUT_DOWNSTREAM_UNITS[@]}"
    run_strict_phase stop-account-owners \
        stop_rollout_units "${ROLLOUT_OWNER_UNITS[@]}"
    run_strict_phase persist-rollout-boot-fence disable_rollout_units_for_boot_fence
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

trap deploy_cleanup EXIT
trap 'deploy_cancel INT 130' INT
trap 'deploy_cancel TERM 143' TERM
trap 'deploy_cancel HUP 129' HUP
trap 'deploy_cancel PIPE 141' PIPE
acquire_maintenance_locks
case "$MODE" in
    install)
        require_rollout_for_funded_generation_change install
        run_strict_phase install-target-prefetch prefetch_rollout_target
        install_mode
        ;;
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
