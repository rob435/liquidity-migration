#!/usr/bin/env bash
# Archive and rebuild demo/paper account state and strategy projections on the VPS.
#
# Safe defaults:
#   * no flag means DRY RUN (no service or file mutation)
#   * --execute is required to stop services, archive, and rebuild projections
#   * concurrent execute attempts are refused by a nonblocking process lock
#   * execute holds both account-owner leases from quiescence through
#     archive/reset, then releases them only for the owner-first restart handoff
#   * REAL_MONEY/mainnet configuration is refused
#   * the demo owner must load the same resolved demo credential env file
#   * canonical demo/paper account roots, inboxes, and captures are archived
#     and recreated empty in the same maintenance transaction
#   * the Bybit demo account must have no positions and no open orders
#   * only initially-active daemons/timers are restarted and verified, unless
#     --leave-stopped is selected for a controlled evidence cutover
#   * configs, locks, reports, signal files, market-data caches, and the
#     continuous account-equity high-water risk state are preserved
#
# Examples (from /opt/liquidity-migration):
#   scripts/maintain/reset_demo_paper_ledgers.sh
#   scripts/maintain/reset_demo_paper_ledgers.sh --sleeves continuous
#   scripts/maintain/reset_demo_paper_ledgers.sh --execute --sleeves all --label exit-overhaul
#   scripts/maintain/reset_demo_paper_ledgers.sh --execute --sleeves long --include-reports
#
# --include-caches is deliberately separate: it removes selected roots' .cache
# directories and can force a slow market-data bootstrap. residual_momentum.parquet
# and root-level kline datasets are still preserved.
set -euo pipefail

# Maintenance executes as root on the VPS.  Do not let an inherited interactive
# PATH replace systemctl, Git, archive, or filesystem utilities with lookalikes.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

RESET_SCRIPT_DIRECTORY="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESET_REPOSITORY_DIRECTORY="$(cd -P -- "$RESET_SCRIPT_DIRECTORY/../.." && pwd)"
MAINTENANCE_LOCK_HELPER="$RESET_REPOSITORY_DIRECTORY/liquidity_migration/maintenance_lock.py"

usage() {
  cat <<'EOF'
Usage: scripts/maintain/reset_demo_paper_ledgers.sh [options]

Archive and rebuild demo + paper account state and strategy telemetry.
Canonical account journals begin a new epoch only after the venue
flatness guard; the prior journals remain in the verified archive.
Default mode is a read-only preview. Mutation requires --execute.

Options:
  --execute                 stop writers, verify demo account flat, archive, rebuild,
                            restart previously-active units, and verify them
  --leave-stopped           execute the reset but leave every managed unit stopped;
                            required before explicit paper-owner/demo-owner staging
  --dry-run                 explicit preview (the default)
  --sleeves LIST            all (default), long, continuous, carry, or comma-separated list
  --archive-dir DIR         archive destination (default: data/_archive)
  --label LABEL             optional safe suffix added after the UTC timestamp
  --include-reports         also archive/reset reports/ in selected roots
  --include-caches          also archive/reset .cache/ in selected roots (slow rebuild)
  --env-file FILE           demo credential env (default: /etc/liquidity-migration/bybit-demo.env)
  --account-env-file FILE   demo owner route env (default: /etc/liquidity-migration/account-execution.env)
  --paper-account-env-file FILE
                            paper owner route env (default: /etc/liquidity-migration/account-paper-execution.env)
  --settle-seconds N        wait before restart verification (default: 3; max: 60)
  -h, --help                show this help

After a continuous reset it writes fresh demo + paper cycle heartbeats. The demo
heartbeat records the venue-verified flat boundary; the paper heartbeat records
that the old deterministic epoch was archived and not carried forward.

The account-owner roots, intent inboxes, raw captures, and strategy telemetry
are archived in full before a fresh epoch is created. Demo reset additionally
requires venue-flat proof; paper reset explicitly retires the archived
simulated epoch without pretending the demo proof applies to it.

The command never removes configs, persistent lock inodes, residual_momentum.parquet,
or root-level market-data datasets. It never cancels orders or closes positions: execute
refuses until the configured Bybit demo account is already flat with no open orders.
Execute also refuses if another deploy/reset maintenance operation holds the shared
host lock or if any submit-armed systemd unit does not load the same resolved
credential env file. The authenticated demo-account lock path has no operator
override; it is derived from Bybit `userID` through the same repository helper as
the owner, rule probe, and venue-accounting command. The canonical paper-owner
lease is likewise held while its local epoch is cleared in place.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

append_unique() {
  # append_unique ARRAY_ITEM... is implemented against the global OUT array to
  # stay compatible with the Bash 3 shipped by older operator laptops.
  local candidate existing
  for candidate in "$@"; do
    for existing in "${OUT[@]:-}"; do
      [[ "$existing" == "$candidate" ]] && continue 2
    done
    OUT+=("$candidate")
  done
}

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

validate_real_money_value() {
  local source="$1" raw="$2" value
  value="$(lower "$raw")"
  case "$value" in
    ""|0|false|no|off|__unset__)
      return 0
      ;;
    1|true|yes|on)
      die "refusing ledger reset: REAL_MONEY='$raw' from $source selects mainnet. This workflow is demo/paper only."
      ;;
    *)
      die "refusing ledger reset: ambiguous REAL_MONEY='$raw' from $source. Use false/off/0 for demo."
      ;;
  esac
}

MODE="dry-run"
ARCHIVE_DIR="data/_archive"
LABEL=""
SLEEVES_RAW="all"
INCLUDE_REPORTS=0
INCLUDE_CACHES=0
LEAVE_STOPPED=0
EXECUTED_CANDIDATE_COMMIT=""
ENV_FILE="/etc/liquidity-migration/bybit-demo.env"
ACCOUNT_ENV_FILE="${ACCOUNT_EXECUTION_ENV_FILE:-/etc/liquidity-migration/account-execution.env}"
PAPER_ACCOUNT_ENV_FILE="${ACCOUNT_PAPER_EXECUTION_ENV_FILE:-/etc/liquidity-migration/account-paper-execution.env}"
SETTLE_SECONDS="${LEDGER_RESET_SETTLE_SECONDS:-3}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"
MAINTENANCE_LOCK_DIR=/run/liquidity-migration
MAINTENANCE_LOCK_FILE="$MAINTENANCE_LOCK_DIR/maintenance.lock"
LEGACY_DEPLOY_LOCK_FILE="$MAINTENANCE_LOCK_DIR/deploy.lock"
LEGACY_RESET_LOCK_FILE=/run/lock/liquidity-migration-ledger-reset.lock
PAPER_RUNTIME_USER=liquidity-migration-paper
PAPER_RUNTIME_GROUP=liquidity-migration-paper

acquire_host_maintenance_locks() {
  # fd 9 is the canonical cross-operation mutex. fds 6 and 5 bridge old
  # deployed scripts so a rolling migration cannot overlap this reset with a
  # legacy deploy/reset. Acquire before reading the checkout, deployed env,
  # route, Git, service, credential, or root state; retain all fds to EXIT.
  local lock_python helper_output
  local maintenance_device maintenance_inode deploy_device deploy_inode reset_device reset_inode
  lock_python=/usr/bin/python3
  [[ -x "$lock_python" ]] \
    || die "/usr/bin/python3 is required for descriptor-safe maintenance locking"
  [[ -f "$MAINTENANCE_LOCK_HELPER" && ! -L "$MAINTENANCE_LOCK_HELPER" ]] \
    || die "maintenance lock helper must be a real repository file: $MAINTENANCE_LOCK_HELPER"
  helper_output="$(
    "$lock_python" "$MAINTENANCE_LOCK_HELPER" prepare-host
  )" || die "cannot prepare persistent host maintenance locks safely"
  IFS=$'\t' read -r \
    maintenance_device maintenance_inode deploy_device deploy_inode reset_device reset_inode \
    <<< "$helper_output"
  for value in \
    "$maintenance_device" "$maintenance_inode" "$deploy_device" \
    "$deploy_inode" "$reset_device" "$reset_inode"; do
    [[ "$value" =~ ^[0-9]+$ ]] || die "maintenance lock helper returned invalid identity metadata"
  done

  # Read-only shell opens cannot truncate a planted target. The helper has
  # already descriptor-created each leaf; the inherited-fd command below
  # rejects any replacement between preparation and these opens.
  if ! { exec 9<"$MAINTENANCE_LOCK_FILE"; }; then
    die "cannot open shared maintenance lock without truncation: $MAINTENANCE_LOCK_FILE"
  fi
  if ! { exec 6<"$LEGACY_DEPLOY_LOCK_FILE"; }; then
    die "cannot open legacy deploy lock without truncation: $LEGACY_DEPLOY_LOCK_FILE"
  fi
  if ! { exec 5<"$LEGACY_RESET_LOCK_FILE"; }; then
    die "cannot open legacy reset lock without truncation: $LEGACY_RESET_LOCK_FILE"
  fi
  "$lock_python" "$MAINTENANCE_LOCK_HELPER" acquire-inherited \
    --lock 9 "$MAINTENANCE_LOCK_FILE" "$maintenance_device" "$maintenance_inode" \
    --lock 6 "$LEGACY_DEPLOY_LOCK_FILE" "$deploy_device" "$deploy_inode" \
    --lock 5 "$LEGACY_RESET_LOCK_FILE" "$reset_device" "$reset_inode" \
    || die "another maintenance operation is active or a lock path changed"
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --execute)
      MODE="execute"
      shift
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --leave-stopped)
      LEAVE_STOPPED=1
      shift
      ;;
    --sleeves)
      [[ "$#" -ge 2 ]] || die "--sleeves requires a value"
      SLEEVES_RAW="$2"
      shift 2
      ;;
    --archive-dir)
      [[ "$#" -ge 2 ]] || die "--archive-dir requires a value"
      ARCHIVE_DIR="$2"
      shift 2
      ;;
    --label)
      [[ "$#" -ge 2 ]] || die "--label requires a value"
      LABEL="$2"
      shift 2
      ;;
    --include-reports)
      INCLUDE_REPORTS=1
      shift
      ;;
    --include-caches)
      INCLUDE_CACHES=1
      shift
      ;;
    --env-file)
      [[ "$#" -ge 2 ]] || die "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --account-env-file)
      [[ "$#" -ge 2 ]] || die "--account-env-file requires a value"
      ACCOUNT_ENV_FILE="$2"
      shift 2
      ;;
    --paper-account-env-file)
      [[ "$#" -ge 2 ]] || die "--paper-account-env-file requires a value"
      PAPER_ACCOUNT_ENV_FILE="$2"
      shift 2
      ;;
    --settle-seconds)
      [[ "$#" -ge 2 ]] || die "--settle-seconds requires a value"
      SETTLE_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$MODE" == "execute" ]]; then
  acquire_host_maintenance_locks
fi

[[ -d liquidity_migration && -d data ]] \
  || die "run from the repo root (expected liquidity_migration/ and data/)"
[[ "$(pwd -P)" == "$RESET_REPOSITORY_DIRECTORY" ]] \
  || die "run from the repository containing this reset script: $RESET_REPOSITORY_DIRECTORY"

clean_candidate_git() {
  local index_path="$1"
  shift
  local clean_environment=(
    "PATH=/usr/bin:/bin"
    "HOME=/nonexistent"
    "LANG=C"
    "LC_ALL=C"
    "GIT_CONFIG_NOSYSTEM=1"
    "GIT_NO_REPLACE_OBJECTS=1"
  )
  [[ -z "$index_path" ]] \
    || clean_environment+=("GIT_INDEX_FILE=$index_path")
  [[ -d "$PWD/.git" && ! -L "$PWD/.git" ]] \
    || die "execute requires a real checkout .git directory"
  /usr/bin/env -i \
    "${clean_environment[@]}" \
    /usr/bin/git --no-optional-locks \
      --git-dir="$PWD/.git" \
      --work-tree="$PWD" \
      -c "safe.directory=$PWD" \
      -c core.fsmonitor=false \
      -c core.filemode=true \
      -C "$PWD" \
      "$@"
}

clean_candidate_checkout_status() {
  local expected_commit="$1"
  local temporary_directory temporary_index refresh_status diff_status untracked
  temporary_directory="$(
    mktemp -d "${TMPDIR:-/tmp}/liqmig-reset-index.XXXXXX"
  )" || return 2
  chmod 0700 "$temporary_directory" || {
    rmdir "$temporary_directory" 2>/dev/null || true
    return 2
  }
  temporary_index="$temporary_directory/index"
  if ! clean_candidate_git "$temporary_index" read-tree "$expected_commit" >/dev/null; then
    rm -f -- "$temporary_index"
    rmdir "$temporary_directory" 2>/dev/null || true
    return 2
  fi
  if clean_candidate_git "$temporary_index" update-index --refresh >/dev/null; then
    refresh_status=0
  else
    refresh_status=$?
  fi
  if (( refresh_status > 1 )); then
    rm -f -- "$temporary_index"
    rmdir "$temporary_directory" 2>/dev/null || true
    return 2
  fi
  if clean_candidate_git "$temporary_index" diff-index --quiet "$expected_commit" --; then
    diff_status=0
  else
    diff_status=$?
  fi
  if (( diff_status > 1 )); then
    rm -f -- "$temporary_index"
    rmdir "$temporary_directory" 2>/dev/null || true
    return 2
  fi
  untracked="$(
    clean_candidate_git "$temporary_index" \
      ls-files --others --exclude-standard
  )" || {
    rm -f -- "$temporary_index"
    rmdir "$temporary_directory" 2>/dev/null || true
    return 2
  }
  rm -f -- "$temporary_index"
  rmdir "$temporary_directory" 2>/dev/null || return 2
  (( refresh_status == 0 )) || echo "tracked worktree metadata differs from HEAD"
  (( diff_status == 0 )) || echo "tracked worktree differs from HEAD"
  [[ -z "$untracked" ]] || printf '%s\n' "$untracked"
}

bind_clean_candidate_checkout() {
  local status
  EXECUTED_CANDIDATE_COMMIT="$(
    clean_candidate_git "" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true
  )"
  [[ "$EXECUTED_CANDIDATE_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    || die "execute requires a repository checkout with one full Git HEAD"
  status="$(
    clean_candidate_checkout_status "$EXECUTED_CANDIDATE_COMMIT" 2>/dev/null
  )" || die "cannot verify execute checkout cleanliness"
  [[ -z "$status" ]] || die "execute requires an exact clean candidate checkout"
  [[ "$(clean_candidate_git "" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)" \
      == "$EXECUTED_CANDIDATE_COMMIT" ]] \
    || die "repository HEAD changed while candidate cleanliness was bound"
}

verify_clean_candidate_checkout() {
  local current status
  current="$(
    clean_candidate_git "" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true
  )"
  [[ "$current" == "$EXECUTED_CANDIDATE_COMMIT" ]] \
    || die "repository HEAD changed during the reset"
  status="$(
    clean_candidate_checkout_status "$EXECUTED_CANDIDATE_COMMIT" 2>/dev/null
  )" || die "cannot recheck execute checkout cleanliness"
  [[ -z "$status" ]] || die "execute checkout changed during the reset"
  [[ "$(clean_candidate_git "" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)" \
      == "$EXECUTED_CANDIDATE_COMMIT" ]] \
    || die "repository HEAD changed during candidate cleanliness recheck"
}

if [[ "$MODE" == "execute" ]]; then
  bind_clean_candidate_checkout
fi

[[ -n "$ARCHIVE_DIR" ]] || die "--archive-dir must not be empty"
ARCHIVE_DIR="${ARCHIVE_DIR%/}"
[[ -n "$ARCHIVE_DIR" ]] || ARCHIVE_DIR="/"
[[ "$SETTLE_SECONDS" =~ ^[0-9]+$ ]] || die "--settle-seconds must be an integer from 0 to 60"
(( SETTLE_SECONDS <= 60 )) || die "--settle-seconds must not exceed 60"
if [[ -n "$LABEL" && ! "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  die "--label must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
fi

if [[ "${REAL_MONEY+x}" == "x" ]]; then
  validate_real_money_value "the caller environment" "$REAL_MONEY"
fi

CANONICAL_PYTHON="$PWD/.venv/bin/python"
if [[ ! -x "$CANONICAL_PYTHON" ]]; then
  CANONICAL_PYTHON="$(command -v python3 || true)"
fi
[[ -n "$CANONICAL_PYTHON" && -x "$CANONICAL_PYTHON" ]] \
  || die "Python runtime is required to validate reset paths"

# Read systemd EnvironmentFile values as data. Do not source these files into
# the maintenance shell: route files must not be able to overwrite reset control
# variables, and credential/Telegram values must not leak into systemctl, tar,
# or projection-rebuild children.
systemd_env_value() {
  "$CANONICAL_PYTHON" - "$1" "$2" <<'PY'
import pathlib
import re
import shlex
import sys


path = pathlib.Path(sys.argv[1])
target = sys.argv[2]
matches: list[str] = []
for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    if "=" not in stripped:
        continue
    key, raw = stripped.split("=", 1)
    key = key.strip()
    if key != target:
        continue
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        print(f"invalid environment key at {path}:{number}", file=sys.stderr)
        raise SystemExit(2)
    try:
        parts = shlex.split(raw.strip(), comments=False, posix=True)
    except ValueError as exc:
        print(f"invalid {target} value at {path}:{number}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if len(parts) > 1:
        print(f"ambiguous whitespace in {target} at {path}:{number}", file=sys.stderr)
        raise SystemExit(2)
    matches.append(parts[0] if parts else "")
if len(matches) > 1:
    print(f"duplicate {target} definitions in {path}", file=sys.stderr)
    raise SystemExit(2)
print(matches[0] if matches else "")
PY
}

[[ -r "$ACCOUNT_ENV_FILE" ]] || die "demo account env is missing or unreadable: $ACCOUNT_ENV_FILE"
[[ -r "$PAPER_ACCOUNT_ENV_FILE" ]] || die "paper account env is missing or unreadable: $PAPER_ACCOUNT_ENV_FILE"

# Resolve each owner route independently; paper and demo intentionally reuse
# variable names in separate env files.
DEMO_KERNEL_REQUIRED="$(systemd_env_value "$ACCOUNT_ENV_FILE" ACCOUNT_EXECUTION_KERNEL_REQUIRED)"
DEMO_ACCOUNT_ROOT="$(systemd_env_value "$ACCOUNT_ENV_FILE" ACCOUNT_EXECUTION_ROOT)"
DEMO_ACCOUNT_INBOX_ROOT="$(systemd_env_value "$ACCOUNT_ENV_FILE" ACCOUNT_INTENT_INBOX_ROOT)"
DEMO_ACCOUNT_CAPTURE_ROOT="$(systemd_env_value "$ACCOUNT_ENV_FILE" ACCOUNT_CAPTURE_ROOT)"
[[ "$DEMO_KERNEL_REQUIRED" == "1" ]] \
  || die "demo account env must set ACCOUNT_EXECUTION_KERNEL_REQUIRED=1"
[[ -n "$DEMO_ACCOUNT_ROOT" && -n "$DEMO_ACCOUNT_INBOX_ROOT" && -n "$DEMO_ACCOUNT_CAPTURE_ROOT" ]] \
  || die "demo account env must set ACCOUNT_EXECUTION_ROOT, ACCOUNT_INTENT_INBOX_ROOT, and ACCOUNT_CAPTURE_ROOT"

PAPER_KERNEL_REQUIRED="$(systemd_env_value "$PAPER_ACCOUNT_ENV_FILE" ACCOUNT_PAPER_KERNEL_REQUIRED)"
PAPER_ACCOUNT_ROOT="$(systemd_env_value "$PAPER_ACCOUNT_ENV_FILE" ACCOUNT_EXECUTION_ROOT)"
PAPER_ACCOUNT_INBOX_ROOT="$(systemd_env_value "$PAPER_ACCOUNT_ENV_FILE" ACCOUNT_INTENT_INBOX_ROOT)"
PAPER_ACCOUNT_CAPTURE_ROOT="$(systemd_env_value "$PAPER_ACCOUNT_ENV_FILE" ACCOUNT_PAPER_CAPTURE_ROOT)"
[[ "$PAPER_KERNEL_REQUIRED" == "1" ]] \
  || die "paper account env must set ACCOUNT_PAPER_KERNEL_REQUIRED=1"
[[ -n "$PAPER_ACCOUNT_ROOT" && -n "$PAPER_ACCOUNT_INBOX_ROOT" && -n "$PAPER_ACCOUNT_CAPTURE_ROOT" ]] \
  || die "paper account env must set ACCOUNT_EXECUTION_ROOT, ACCOUNT_INTENT_INBOX_ROOT, and ACCOUNT_PAPER_CAPTURE_ROOT"

repo_data_path() {
  "$CANONICAL_PYTHON" - "$1" "$PWD" <<'PY'
import pathlib
import sys

raw = pathlib.Path(sys.argv[1]).expanduser()
repo = pathlib.Path(sys.argv[2]).resolve()
resolved = (raw if raw.is_absolute() else repo / raw).resolve(strict=False)
try:
    relative = resolved.relative_to(repo)
    relative.relative_to("data")
except ValueError:
    print(f"account state path must stay below {repo / 'data'}: {resolved}", file=sys.stderr)
    raise SystemExit(1)
print(relative)
PY
}

DEMO_ACCOUNT_ROOT="$(repo_data_path "$DEMO_ACCOUNT_ROOT")" || die "invalid demo account root"
DEMO_ACCOUNT_INBOX_ROOT="$(repo_data_path "$DEMO_ACCOUNT_INBOX_ROOT")" || die "invalid demo account inbox root"
DEMO_ACCOUNT_CAPTURE_ROOT="$(repo_data_path "$DEMO_ACCOUNT_CAPTURE_ROOT")" || die "invalid demo account capture root"
PAPER_ACCOUNT_ROOT="$(repo_data_path "$PAPER_ACCOUNT_ROOT")" || die "invalid paper account root"
PAPER_ACCOUNT_INBOX_ROOT="$(repo_data_path "$PAPER_ACCOUNT_INBOX_ROOT")" || die "invalid paper account inbox root"
PAPER_ACCOUNT_CAPTURE_ROOT="$(repo_data_path "$PAPER_ACCOUNT_CAPTURE_ROOT")" || die "invalid paper account capture root"
PAPER_ACCOUNT_LEASE_PATH="$PWD/$PAPER_ACCOUNT_ROOT/account_execution_owner.lock"
[[ "$PAPER_ACCOUNT_LEASE_PATH" == /* && "$PAPER_ACCOUNT_LEASE_PATH" != *$'\n'* ]] \
  || die "canonical paper-account lease path is invalid"

ACCOUNT_STATE_TARGETS=(
  "$DEMO_ACCOUNT_ROOT"
  "$DEMO_ACCOUNT_INBOX_ROOT"
  "$DEMO_ACCOUNT_CAPTURE_ROOT"
  "$PAPER_ACCOUNT_ROOT"
  "$PAPER_ACCOUNT_INBOX_ROOT"
  "$PAPER_ACCOUNT_CAPTURE_ROOT"
)

# Refuse route layouts that overlap each other or any strategy root: a narrow
# account reset could otherwise recursively erase a preserved journal, cache,
# config, or unselected sleeve. The deployment uses six sibling roots, so
# overlap means a bad env file.
"$CANONICAL_PYTHON" - "$PWD" "${ACCOUNT_STATE_TARGETS[@]}" <<'PY' \
  || die "account execution roots must be pairwise disjoint and separate from strategy roots"
import pathlib
import sys


repo = pathlib.Path(sys.argv[1]).resolve()
account_paths = [(repo / raw).resolve(strict=False) for raw in sys.argv[2:]]
reserved = [
    (repo / raw).resolve(strict=False)
    for raw in (
        "data/bybit-long-demo-event",
        "data/bybit-long-paper-event",
        "data/bybit-continuous-demo-event",
        "data/bybit-continuous-paper-event",
        "data/bybit-carry-demo-event",
        "data/bybit-carry-paper-event",
    )
]


def overlaps(left: pathlib.Path, right: pathlib.Path) -> bool:
    return left == right or left in right.parents or right in left.parents


for index, left in enumerate(account_paths):
    for right in account_paths[index + 1 :]:
        if overlaps(left, right):
            print(f"overlapping account roots: {left} and {right}", file=sys.stderr)
            raise SystemExit(1)
    for right in reserved:
        if overlaps(left, right):
            print(f"account root overlaps strategy root: {left} and {right}", file=sys.stderr)
            raise SystemExit(1)
PY

# Parse and canonicalise sleeve selection.
SELECT_LONG=0
SELECT_CONTINUOUS=0
SELECT_CARRY=0
normalised_sleeves="$(printf '%s' "$SLEEVES_RAW" | tr ',' ' ')"
[[ -n "${normalised_sleeves//[[:space:]]/}" ]] || die "--sleeves must not be empty"
for sleeve in $normalised_sleeves; do
  case "$(lower "$sleeve")" in
    all)
      SELECT_LONG=1
      SELECT_CONTINUOUS=1
      SELECT_CARRY=1
      ;;
    long)
      SELECT_LONG=1
      ;;
    continuous)
      SELECT_CONTINUOUS=1
      ;;
    carry)
      SELECT_CARRY=1
      ;;
    *)
      echo "unknown sleeve: $sleeve" >&2
      echo "expected: all, long, continuous, carry, or a comma-separated combination" >&2
      exit 2
      ;;
  esac
done

SELECTED_SLEEVES=()
(( SELECT_LONG )) && SELECTED_SLEEVES+=("long")
(( SELECT_CONTINUOUS )) && SELECTED_SLEEVES+=("continuous")
(( SELECT_CARRY )) && SELECTED_SLEEVES+=("carry")

# Static allowlist of generated projections/epoch telemetry. The canonical
# journal is intentionally absent: no caller-supplied path can delete it.
LONG_LEDGER_TARGETS=(
  data/bybit-long-demo-event/long_native_demo_cycles
  data/bybit-long-demo-event/strategy_event_tape.jsonl
  data/bybit-long-demo-event/strategy_event_decision_tape.jsonl
  data/bybit-long-demo-event/strategy_target_scheduling_capture.jsonl
  data/bybit-long-paper-event/long_native_paper_cycles
  data/bybit-long-paper-event/strategy_event_tape.jsonl
  data/bybit-long-paper-event/strategy_event_decision_tape.jsonl
  data/bybit-long-paper-event/strategy_target_scheduling_capture.jsonl
)
CONTINUOUS_LEDGER_TARGETS=(
  data/bybit-continuous-demo-event/continuous_fade_demo_cycles
  data/bybit-continuous-demo-event/strategy_event_tape.jsonl
  data/bybit-continuous-demo-event/strategy_event_decision_tape.jsonl
  data/bybit-continuous-demo-event/strategy_target_scheduling_capture.jsonl
  data/bybit-continuous-paper-event/continuous_fade_paper_cycles
  data/bybit-continuous-paper-event/strategy_event_tape.jsonl
  data/bybit-continuous-paper-event/strategy_event_decision_tape.jsonl
  data/bybit-continuous-paper-event/strategy_target_scheduling_capture.jsonl
)
CARRY_LEDGER_TARGETS=(
  data/bybit-carry-demo-event/carry_hold_demo_cycles
  data/bybit-carry-demo-event/strategy_event_tape.jsonl
  data/bybit-carry-demo-event/strategy_event_decision_tape.jsonl
  data/bybit-carry-demo-event/strategy_target_scheduling_capture.jsonl
  data/bybit-carry-paper-event/carry_hold_paper_cycles
  data/bybit-carry-paper-event/strategy_event_tape.jsonl
  data/bybit-carry-paper-event/strategy_event_decision_tape.jsonl
  data/bybit-carry-paper-event/strategy_target_scheduling_capture.jsonl
)
LONG_ROOTS=(data/bybit-long-demo-event data/bybit-long-paper-event)
CONTINUOUS_ROOTS=(
  data/bybit-continuous-demo-event
  data/bybit-continuous-paper-event
)
CARRY_ROOTS=(
  data/bybit-carry-demo-event
  data/bybit-carry-paper-event
)

OUT=()
(( SELECT_LONG )) && append_unique "${LONG_LEDGER_TARGETS[@]}"
if (( SELECT_CONTINUOUS )); then
  append_unique "${CONTINUOUS_LEDGER_TARGETS[@]}"
fi
if (( SELECT_CARRY )); then
  append_unique "${CARRY_LEDGER_TARGETS[@]}"
fi
append_unique "${ACCOUNT_STATE_TARGETS[@]}"
TARGETS=("${OUT[@]}")

OUT=()
(( SELECT_LONG )) && append_unique "${LONG_ROOTS[@]}"
(( SELECT_CONTINUOUS )) && append_unique "${CONTINUOUS_ROOTS[@]}"
(( SELECT_CARRY )) && append_unique "${CARRY_ROOTS[@]}"
SELECTED_ROOTS=("${OUT[@]}")

if (( INCLUDE_REPORTS )); then
  for root in "${SELECTED_ROOTS[@]}"; do
    OUT=("${TARGETS[@]}")
    append_unique "$root/reports"
    TARGETS=("${OUT[@]}")
  done
fi
if (( INCLUDE_CACHES )); then
  for root in "${SELECTED_ROOTS[@]}"; do
    OUT=("${TARGETS[@]}")
    append_unique "$root/.cache"
    TARGETS=("${OUT[@]}")
  done
fi

refresh_existing_targets() {
  local target
  EXISTING_TARGETS=()
  for target in "${TARGETS[@]}"; do
    case "$target" in
      data/bybit-*) ;;
      *) die "internal safety error: non-data target '$target'" ;;
    esac
    [[ "$target" != *".."* ]] || die "internal safety error: traversal target '$target'"
    [[ -e "$target" || -L "$target" ]] && EXISTING_TARGETS+=("$target")
  done
  return 0
}
refresh_existing_targets

# The archive must never sit inside anything being archived, or tar consumes its
# own growing output. Guard journal paths the pre-archive bootstrap may still
# create. Canonicalise through symlinks and collapse '.', '..', and
# relative/absolute aliases; a lexical prefix check misses ``target/./_archive``.
canonical_path() {
  "$CANONICAL_PYTHON" -c '
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve(strict=False))
' "$1"
}
archive_compare="$(canonical_path "$ARCHIVE_DIR")"
for target in "${TARGETS[@]}"; do
  target_compare="$(canonical_path "$target")"
  case "$archive_compare/" in
    "$target_compare/"*)
      die "--archive-dir must be outside reset targets (archive '$ARCHIVE_DIR', target '$target')"
      ;;
  esac
  case "$target_compare/" in
    "$archive_compare/"*)
      die "--archive-dir must not contain reset targets (archive '$ARCHIVE_DIR', target '$target')"
      ;;
  esac
done

# The account is shared, so every target producer and both account owners must
# be quiesced even for a one-sleeve reset. Producers stop before owners; this
# prevents new targets from being queued while their sole consumer is down.
STOP_UNITS=(
  liquidity-migration-demo-liveness.timer
  liquidity-migration-continuous-hedge.timer
  liquidity-migration-continuous-rmom-refresh.timer
  liquidity-migration-bybit-long-demo.service
  liquidity-migration-bybit-long-paper.service
  liquidity-migration-bybit-continuous-demo.service
  liquidity-migration-bybit-continuous-paper.service
  liquidity-migration-bybit-carry-demo.service
  liquidity-migration-bybit-carry-paper.service
  liquidity-migration-continuous-hedge.service
  liquidity-migration-continuous-rmom-refresh.service
  liquidity-migration-demo-liveness.service
  liquidity-migration-account-execution.service
  liquidity-migration-account-paper-execution.service
)
# The account owner is the only venue-mutating process and must use the exact
# credential file selected by --env-file (after symlink/path resolution).
ACCOUNT_BOUND_UNITS=(
  liquidity-migration-account-execution.service
)
NON_RESTARTABLE_ONESHOTS=(
  liquidity-migration-continuous-hedge.service
  liquidity-migration-continuous-rmom-refresh.service
  liquidity-migration-demo-liveness.service
)
OWNER_RESTART_UNITS=(
  liquidity-migration-account-execution.service
  liquidity-migration-account-paper-execution.service
)
DOWNSTREAM_RESTART_UNITS=(
  liquidity-migration-bybit-long-demo.service
  liquidity-migration-bybit-long-paper.service
  liquidity-migration-bybit-continuous-demo.service
  liquidity-migration-bybit-continuous-paper.service
  liquidity-migration-bybit-carry-demo.service
  liquidity-migration-bybit-carry-paper.service
  liquidity-migration-continuous-rmom-refresh.timer
  liquidity-migration-continuous-hedge.timer
  liquidity-migration-demo-liveness.timer
)
RESTART_UNITS=("${OWNER_RESTART_UNITS[@]}" "${DOWNSTREAM_RESTART_UNITS[@]}")

echo "Ledger reset plan"
echo "  mode: $MODE"
echo "  sleeves: ${SELECTED_SLEEVES[*]}"
echo "  archive dir: $ARCHIVE_DIR"
echo "  include reports: $INCLUDE_REPORTS"
echo "  include caches: $INCLUDE_CACHES"
echo "  leave managed units stopped: $LEAVE_STOPPED"
echo "  canonical account state: ${ACCOUNT_STATE_TARGETS[*]}"
echo "  existing targets: ${#EXISTING_TARGETS[@]}"
for target in "${EXISTING_TARGETS[@]}"; do
  # Do not recursively inspect an unvalidated target during preview. Execute
  # performs descriptor/mount preflight after every writer is quiescent.
  echo "    - $target (present; size not traversed during preview)"
done

echo "  preserved by default: configs/, .locks/, reports/, .cache/, residual_momentum.parquet, root-level market data"
(( INCLUDE_REPORTS )) && echo "  selected exception: reports/ will be archived and reset"
(( INCLUDE_CACHES )) && echo "  selected exception: .cache/ will be archived and reset; expect market-data bootstrap"
echo "  quiesced units: ${#STOP_UNITS[@]} (all shared-account writers plus maintenance readers/timers)"

if [[ "$MODE" == "dry-run" ]]; then
  echo
  echo "DRY RUN: no services or files were changed."
  echo "Execute only after reviewing the plan:"
  execute_hint="scripts/maintain/reset_demo_paper_ledgers.sh --execute --sleeves $SLEEVES_RAW"
  [[ -n "$LABEL" ]] && execute_hint="$execute_hint --label $LABEL"
  (( INCLUDE_REPORTS )) && execute_hint="$execute_hint --include-reports"
  (( INCLUDE_CACHES )) && execute_hint="$execute_hint --include-caches"
  (( LEAVE_STOPPED )) && execute_hint="$execute_hint --leave-stopped"
  echo "  $execute_hint"
  echo "Execute will refuse REAL_MONEY, missing demo credentials, any open demo position/order, missing units, or restart verification failure."
  exit 0
fi

# Even a fully-absent layout passes through the flatness guard, creates all six
# roots, and writes an archive+manifest, so a missing epoch is never mistaken
# for a completed reset.
[[ -r "$ENV_FILE" ]] || die "demo env file is missing or unreadable: $ENV_FILE"
[[ "$SYSTEMCTL_BIN" == /* && -x "$SYSTEMCTL_BIN" && ! -L "$SYSTEMCTL_BIN" ]] \
  || die "systemctl must be one absolute non-symlink executable: $SYSTEMCTL_BIN"

PYTHON="$PWD/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || die "Python runtime not found (.venv/bin/python or python3)"

# Freeze the selected demo credential for the identity and flatness checks. Do
# not export it into systemctl, tar, or projection-rebuild children.
credential_demo="$(systemd_env_value "$ENV_FILE" DEMO)"
credential_real_money="$(systemd_env_value "$ENV_FILE" REAL_MONEY)"
credential_api_key="$(systemd_env_value "$ENV_FILE" BYBIT_DEMO_API_KEY)"
credential_api_secret="$(systemd_env_value "$ENV_FILE" BYBIT_DEMO_API_SECRET)"
validate_real_money_value "$ENV_FILE" "${credential_real_money:-__unset__}"
[[ -n "$credential_api_key" && -n "$credential_api_secret" ]] \
  || die "missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET in $ENV_FILE"

# Resolve the venue account before any service mutation. The returned lease path
# is keyed by Bybit userID, so it is account-wide across API keys and cannot be
# steered by a caller-selected ledger root.
if ! DEMO_ACCOUNT_LEASE_PATH="$(
  unset DEMO REAL_MONEY BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
    BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
  DEMO="$credential_demo"
  REAL_MONEY="$credential_real_money"
  BYBIT_DEMO_API_KEY="$credential_api_key"
  BYBIT_DEMO_API_SECRET="$credential_api_secret"
  export DEMO REAL_MONEY BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET
  "$PYTHON" - --resolve-demo-account-lease <<'PY'
import sys

from liquidity_migration.account_owner_lease import (
    DemoAccountIdentity,
    canonical_demo_account_lease_path,
)
from liquidity_migration.bybit import BybitPrivateClient, resolve_demo_credentials


api_key, api_secret = resolve_demo_credentials()
if not api_key or not api_secret:
    print("missing demo API credentials", file=sys.stderr)
    raise SystemExit(1)
client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=True)
identity = DemoAccountIdentity.from_api_key_info(
    api_key=api_key,
    api_key_info=client.get_api_key_information(),
)
print(canonical_demo_account_lease_path(identity))
PY
)"; then
  die "cannot derive the authenticated canonical demo-account lease"
fi
[[ "$DEMO_ACCOUNT_LEASE_PATH" == /* && "$DEMO_ACCOUNT_LEASE_PATH" != *$'\n'* ]] \
  || die "canonical demo-account lease path is not one absolute path"
echo "  authenticated demo-account lease: $DEMO_ACCOUNT_LEASE_PATH"

ACTIVE_BEFORE=()
for unit in "${STOP_UNITS[@]}"; do
  load_state="$("$SYSTEMCTL_BIN" show "$unit" --property=LoadState --value 2>/dev/null || true)"
  [[ -n "$load_state" && "$load_state" != "not-found" ]] \
    || die "required systemd unit is not installed: $unit"
  if "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
    ACTIVE_BEFORE+=("$unit")
  fi
done

# systemctl show reflects unit drop-ins and daemon-reloaded state, unlike
# grepping the checked-in unit files. Resolve both sides so an intentional
# symlink alias passes but a different credential/account file cannot.
for unit in "${ACCOUNT_BOUND_UNITS[@]}"; do
  if ! unit_environment_files="$(
    "$SYSTEMCTL_BIN" show "$unit" --property=EnvironmentFiles --value
  )"; then
    die "failed to resolve EnvironmentFiles for submit-armed unit: $unit"
  fi
  if ! unit_environment="$(
    "$SYSTEMCTL_BIN" show "$unit" --property=Environment --value
  )"; then
    die "failed to resolve direct Environment assignments for submit-armed unit: $unit"
  fi
  if ! "$PYTHON" -c '
import pathlib
import re
import shlex
import sys

expected = pathlib.Path(sys.argv[1]).resolve(strict=True)
raw_files = sys.argv[2]
raw_environment = sys.argv[3]
paths = re.findall(
    r"(?:^|[\n ])(.+?) \(ignore_errors=(?:yes|no)\)(?=[\n ]|$)",
    raw_files,
)
found_expected = False

def protected(key: str) -> bool:
    return key.startswith("BYBIT_") or key in {"REAL_MONEY", "DEMO"}

for value in paths:
    try:
        candidate = pathlib.Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        continue
    if candidate == expected:
        found_expected = True
        continue
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if protected(key):
            print(
                f"conflicting protected environment key {key} in {candidate}",
                file=sys.stderr,
            )
            raise SystemExit(2)

try:
    direct_assignments = shlex.split(raw_environment)
except ValueError:
    print("unparseable direct Environment assignments", file=sys.stderr)
    raise SystemExit(3)
for assignment in direct_assignments:
    key = assignment.split("=", 1)[0]
    if "=" in assignment and protected(key):
        print(f"conflicting protected direct Environment key {key}", file=sys.stderr)
        raise SystemExit(4)

raise SystemExit(0 if found_expected else 1)
' "$ENV_FILE" "$unit_environment_files" "$unit_environment"; then
    die "submit-armed unit $unit has an ambiguous credential environment or does not exclusively load the selected demo env file (resolved from $ENV_FILE); refusing before stopping services"
  fi
done

# The reset must archive the same canonical roots that the two running owners
# will reopen. Reject a drop-in or extra env file that redirects an owner after
# the reset has already removed a different route.
verify_owner_route_env() {
  local unit="$1" expected_file="$2" protected_csv="$3"
  local unit_environment_files unit_environment
  unit_environment_files="$(
    "$SYSTEMCTL_BIN" show "$unit" --property=EnvironmentFiles --value
  )" || die "failed to resolve route EnvironmentFiles for owner: $unit"
  unit_environment="$(
    "$SYSTEMCTL_BIN" show "$unit" --property=Environment --value
  )" || die "failed to resolve direct route Environment assignments for owner: $unit"
  "$PYTHON" -c '
import pathlib
import re
import shlex
import sys

expected = pathlib.Path(sys.argv[1]).resolve(strict=True)
raw_files = sys.argv[2]
raw_environment = sys.argv[3]
protected = set(sys.argv[4].split(","))
paths = re.findall(
    r"(?:^|[\n ])(.+?) \(ignore_errors=(?:yes|no)\)(?=[\n ]|$)",
    raw_files,
)
found_expected = False
for value in paths:
    try:
        candidate = pathlib.Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        continue
    if candidate == expected:
        found_expected = True
        continue
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in protected:
            print(f"conflicting owner route key {key} in {candidate}", file=sys.stderr)
            raise SystemExit(2)
try:
    direct_assignments = shlex.split(raw_environment)
except ValueError:
    print("unparseable direct owner Environment assignments", file=sys.stderr)
    raise SystemExit(3)
for assignment in direct_assignments:
    key = assignment.split("=", 1)[0]
    if "=" in assignment and key in protected:
        print(f"conflicting direct owner route key {key}", file=sys.stderr)
        raise SystemExit(4)
raise SystemExit(0 if found_expected else 1)
' "$expected_file" "$unit_environment_files" "$unit_environment" "$protected_csv" \
    || die "owner $unit has an ambiguous route environment or does not exclusively load $expected_file; refusing before stopping services"
}

verify_owner_route_env \
  liquidity-migration-account-execution.service \
  "$ACCOUNT_ENV_FILE" \
  ACCOUNT_EXECUTION_ROOT,ACCOUNT_INTENT_INBOX_ROOT,ACCOUNT_CAPTURE_ROOT
verify_owner_route_env \
  liquidity-migration-account-paper-execution.service \
  "$PAPER_ACCOUNT_ENV_FILE" \
  ACCOUNT_EXECUTION_ROOT,ACCOUNT_INTENT_INBOX_ROOT,ACCOUNT_CAPTURE_ROOT,ACCOUNT_PAPER_CAPTURE_ROOT

was_active() {
  local needle="$1" unit
  for unit in "${ACTIVE_BEFORE[@]:-}"; do
    [[ "$unit" == "$needle" ]] && return 0
  done
  return 1
}

for unit in "${NON_RESTARTABLE_ONESHOTS[@]}"; do
  if was_active "$unit"; then
    die "transient oneshot is active: $unit. Retry after it finishes; reset will not interrupt and re-run it."
  fi
done

SERVICES_STOPPED=0
RESTART_COMPLETE=0
FAILURE_RECOVERY_ALLOWED=1
MANIFEST_DIR=""
DEMO_ACCOUNT_LEASE_HELD=0
PAPER_ACCOUNT_LEASE_HELD=0
DEMO_ACCOUNT_LEASE_RECEIPT=()
PAPER_ACCOUNT_LEASE_RECEIPT=()

acquire_demo_account_lease() {
  local helper_output lease_device lease_inode lease_uid lease_gid lease_mount_id
  local parent_device parent_inode parent_uid parent_gid parent_mount_id extra rc value

  helper_output="$(
    "$PYTHON" -m liquidity_migration.account_owner_lease \
      prepare "$DEMO_ACCOUNT_LEASE_PATH"
  )" || die "cannot prepare canonical demo-account lease safely"
  [[ "$helper_output" != *$'\n'* ]] \
    || die "demo-account lease helper returned multiline identity metadata"
  IFS=$'\t' read -r \
    lease_device lease_inode lease_uid lease_gid lease_mount_id \
    parent_device parent_inode parent_uid parent_gid parent_mount_id extra \
    <<< "$helper_output"
  [[ -z "$extra" ]] || die "demo-account lease helper returned extra identity metadata"
  DEMO_ACCOUNT_LEASE_RECEIPT=(
    "$lease_device" "$lease_inode" "$lease_uid" "$lease_gid" "$lease_mount_id"
    "$parent_device" "$parent_inode" "$parent_uid" "$parent_gid" "$parent_mount_id"
  )
  for value in "${DEMO_ACCOUNT_LEASE_RECEIPT[@]}"; do
    [[ "$value" =~ ^[0-9]+$ ]] \
      || die "demo-account lease helper returned invalid identity metadata"
  done
  if ! { exec 8<>"$DEMO_ACCOUNT_LEASE_PATH"; }; then
    die "cannot open canonical demo-account lease without truncation: $DEMO_ACCOUNT_LEASE_PATH"
  fi

  if "$PYTHON" -m liquidity_migration.account_owner_lease acquire-inherited \
    8 "$DEMO_ACCOUNT_LEASE_PATH" "${DEMO_ACCOUNT_LEASE_RECEIPT[@]}" \
    demo ledger_reset; then
    DEMO_ACCOUNT_LEASE_HELD=1
    echo "  canonical demo-account lease acquired for flatness/archive/reset: $DEMO_ACCOUNT_LEASE_PATH"
    return 0
  else
    rc="$?"
  fi

  exec 8>&-
  if [[ "$rc" == "73" ]]; then
    die "canonical demo-account lease is already held: $DEMO_ACCOUNT_LEASE_PATH"
  fi
  die "cannot acquire canonical demo-account lease: $DEMO_ACCOUNT_LEASE_PATH (no alternate path is allowed)"
}

release_demo_account_lease() {
  local context="$1"
  if (( DEMO_ACCOUNT_LEASE_HELD )); then
    exec 8>&-
    DEMO_ACCOUNT_LEASE_HELD=0
    echo "  canonical demo-account lease released for owner-first restart handoff ($context)"
  fi
}

acquire_paper_account_lease() {
  local helper_output lease_device lease_inode lease_uid lease_gid lease_mount_id
  local parent_device parent_inode parent_uid parent_gid parent_mount_id extra rc value

  helper_output="$(
    "$PYTHON" -m liquidity_migration.account_owner_lease \
      prepare "$PAPER_ACCOUNT_LEASE_PATH"
  )" || die "cannot prepare canonical paper-account lease safely"
  [[ "$helper_output" != *$'\n'* ]] \
    || die "paper-account lease helper returned multiline identity metadata"
  IFS=$'\t' read -r \
    lease_device lease_inode lease_uid lease_gid lease_mount_id \
    parent_device parent_inode parent_uid parent_gid parent_mount_id extra \
    <<< "$helper_output"
  [[ -z "$extra" ]] || die "paper-account lease helper returned extra identity metadata"
  PAPER_ACCOUNT_LEASE_RECEIPT=(
    "$lease_device" "$lease_inode" "$lease_uid" "$lease_gid" "$lease_mount_id"
    "$parent_device" "$parent_inode" "$parent_uid" "$parent_gid" "$parent_mount_id"
  )
  for value in "${PAPER_ACCOUNT_LEASE_RECEIPT[@]}"; do
    [[ "$value" =~ ^[0-9]+$ ]] \
      || die "paper-account lease helper returned invalid identity metadata"
  done
  if ! { exec 7<>"$PAPER_ACCOUNT_LEASE_PATH"; }; then
    die "cannot open canonical paper-account lease without truncation: $PAPER_ACCOUNT_LEASE_PATH"
  fi

  if "$PYTHON" -m liquidity_migration.account_owner_lease acquire-inherited \
    7 "$PAPER_ACCOUNT_LEASE_PATH" "${PAPER_ACCOUNT_LEASE_RECEIPT[@]}" \
    paper ledger_reset; then
    PAPER_ACCOUNT_LEASE_HELD=1
    echo "  canonical paper-account lease acquired for archive/reset: $PAPER_ACCOUNT_LEASE_PATH"
    return 0
  else
    rc="$?"
  fi

  exec 7>&-
  if [[ "$rc" == "73" ]]; then
    die "canonical paper-account lease is already held: $PAPER_ACCOUNT_LEASE_PATH"
  fi
  die "cannot acquire canonical paper-account lease: $PAPER_ACCOUNT_LEASE_PATH"
}

release_paper_account_lease() {
  local context="$1"
  if (( PAPER_ACCOUNT_LEASE_HELD )); then
    exec 7>&-
    PAPER_ACCOUNT_LEASE_HELD=0
    echo "  canonical paper-account lease released for owner-first restart handoff ($context)"
  fi
}

restart_previously_active() {
  local context="$1" unit failed=0
  echo
  echo "Restarting previously-active account owners first ($context) ..."
  for unit in "${OWNER_RESTART_UNITS[@]}"; do
    if was_active "$unit"; then
      if "$SYSTEMCTL_BIN" start "$unit"; then
        echo "  started $unit"
      else
        echo "  FAILED to start $unit" >&2
        failed=1
      fi
    else
      echo "  left inactive $unit (it was inactive before reset)"
    fi
  done
  if (( failed )); then
    echo "  owner start failed; downstream producers remain stopped" >&2
    return "$failed"
  fi
  if (( SETTLE_SECONDS > 0 )); then
    echo "Waiting ${SETTLE_SECONDS}s for account owners before downstream startup ..."
    sleep "$SETTLE_SECONDS"
  fi
  for unit in "${OWNER_RESTART_UNITS[@]}"; do
    if was_active "$unit" && ! "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
      echo "  owner did not remain active: $unit; downstream producers remain stopped" >&2
      failed=1
    fi
  done
  (( failed == 0 )) || return "$failed"

  echo "Restarting previously-active downstream producers/timers ($context) ..."
  for unit in "${DOWNSTREAM_RESTART_UNITS[@]}"; do
    if was_active "$unit"; then
      if "$SYSTEMCTL_BIN" start "$unit"; then
        echo "  started $unit"
      else
        echo "  FAILED to start $unit" >&2
        failed=1
      fi
    else
      echo "  left inactive $unit (it was inactive before reset)"
    fi
  done
  return "$failed"
}

stop_all_managed_units_after_failed_handoff() {
  local unit failed=0
  echo "Stopping every managed unit after the failed reset handoff ..." >&2
  # STOP_UNITS is ordered downstream-before-owner. Repeating the full stop is
  # safe for units that never restarted and closes the partial-topology case.
  for unit in "${STOP_UNITS[@]}"; do
    if "$SYSTEMCTL_BIN" stop "$unit"; then
      echo "  stopped $unit" >&2
    else
      echo "  FAILED to stop $unit" >&2
      failed=1
    fi
  done
  for unit in "${STOP_UNITS[@]}"; do
    if "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
      echo "  STILL ACTIVE after failed handoff: $unit" >&2
      failed=1
    fi
  done
  return "$failed"
}

# If the transport dies mid-reset, sshd HUPs the remote process group and every
# later write raises SIGPIPE. Mirror each line into the host journal, and never
# let a failed write abort the fail-closed handoff.
cleanup_notice() {
  logger -t liquidity-migration-reset -p daemon.err -- "$*" 2>/dev/null || true
  printf '%s\n' "$*" >&2 2>/dev/null || true
}

cleanup() {
  local rc="$?"
  trap - EXIT
  # Once cleanup owns the handoff, a second signal or a dead stdout must not
  # interrupt the fail-closed stop sequence.
  trap '' INT TERM HUP PIPE
  set +e
  [[ -z "$MANIFEST_DIR" ]] || rm -rf -- "$MANIFEST_DIR"
  if (( SERVICES_STOPPED )) && (( ! RESTART_COMPLETE )); then
    if (( FAILURE_RECOVERY_ALLOWED )) && (( ! LEAVE_STOPPED )); then
      release_paper_account_lease "failure recovery"
      release_demo_account_lease "failure recovery"
      cleanup_notice "Reset did not complete; attempting to restore pre-reset service state."
      if ! restart_previously_active "failure recovery"; then
        cleanup_notice "Failure recovery produced a partial topology; failing closed."
        if ! stop_all_managed_units_after_failed_handoff; then
          cleanup_notice "CRITICAL: at least one managed unit could not be stopped."
        fi
      fi
    else
      if stop_all_managed_units_after_failed_handoff; then
        cleanup_notice "Reset handoff did not complete; managed units are stopped and verified."
      else
        cleanup_notice "CRITICAL: reset handoff failed and at least one managed unit remains active."
      fi
      release_paper_account_lease "failed stopped handoff"
      release_demo_account_lease "failed stopped handoff"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# bash skips the EXIT trap on an untrapped fatal signal, so an SSH client death
# (HUP, then SIGPIPE) would skip the fail-closed handoff entirely.
trap 'exit 129' HUP
trap 'exit 141' PIPE

echo
echo "Stopping shared-account writers and maintenance units ..."
SERVICES_STOPPED=1
for unit in "${STOP_UNITS[@]}"; do
  "$SYSTEMCTL_BIN" stop "$unit"
  echo "  stopped $unit"
done
for unit in "${STOP_UNITS[@]}"; do
  if "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
    die "unit remained active after stop: $unit"
  fi
done
# A stopped service may retain ActiveState=failed from an earlier timeout, and
# downstream checks require literal `inactive`. Reset only units that really are
# failed: systemd may garbage-collect an inactive unit object between stop and
# reset-failed, so an unconditional reset-failed races a healthy unit.
for unit in "${STOP_UNITS[@]}"; do
  unit_load_state="$(
    "$SYSTEMCTL_BIN" show "$unit" --property=LoadState --value
  )" || die "could not read stopped unit load state: $unit"
  unit_active_state="$(
    "$SYSTEMCTL_BIN" show "$unit" --property=ActiveState --value
  )" || die "could not read stopped unit state: $unit"
  [[ "$unit_load_state" == loaded ]] \
    || die "managed unit is not loaded after stop: $unit ($unit_load_state)"
  if [[ "$unit_active_state" == failed ]]; then
    "$SYSTEMCTL_BIN" reset-failed "$unit" \
      || die "could not clear stopped failure state: $unit"
    unit_load_state="$(
      "$SYSTEMCTL_BIN" show "$unit" --property=LoadState --value
    )" || die "could not re-read stopped unit load state: $unit"
    unit_active_state="$(
      "$SYSTEMCTL_BIN" show "$unit" --property=ActiveState --value
    )" || die "could not re-read stopped unit state: $unit"
  fi
  [[ "$unit_load_state" == loaded ]] \
    || die "managed unit is not loaded after failure-state normalization: $unit ($unit_load_state)"
  [[ "$unit_active_state" == inactive ]] \
    || die "unit is not literally inactive after stop/reset-failed: $unit ($unit_active_state)"
done
echo "  quiescence verified (all managed units loaded and inactive)"

# Bind every filesystem tree before either account lease can create, chown,
# truncate, or write a lock leaf. Paper/demo-specific policies additionally
# reject unsafe cache/lock components, while the strict account pass rejects a
# final root symlink as well as symlinks anywhere below an account root.
id -u "$PAPER_RUNTIME_USER" >/dev/null 2>&1 \
  || die "paper runtime user is not provisioned: $PAPER_RUNTIME_USER"
[[ "$(id -gn "$PAPER_RUNTIME_USER")" == "$PAPER_RUNTIME_GROUP" ]] \
  || die "paper runtime user has the wrong primary group"
PAPER_RUNTIME_UID="$(id -u "$PAPER_RUNTIME_USER")"
PAPER_RUNTIME_GID="$(id -g "$PAPER_RUNTIME_USER")"

account_preflight_args=(preflight --anchor "$PWD/data" --reject-symlinks)
for target in "${ACCOUNT_STATE_TARGETS[@]}"; do
  account_preflight_args+=(--target "$PWD/$target")
done
"$PYTHON" -m liquidity_migration.reset_path_safety \
  "${account_preflight_args[@]}" \
  || die "account root descriptor/mount preflight failed before owner leases"

paper_preflight_args=(preflight-paper --anchor "$PWD/data")
for root in \
  "$PAPER_ACCOUNT_ROOT" "$PAPER_ACCOUNT_INBOX_ROOT" "$PAPER_ACCOUNT_CAPTURE_ROOT" \
  data/bybit-long-paper-event data/bybit-continuous-paper-event \
  data/bybit-carry-paper-event; do
  paper_preflight_args+=(--root "$PWD/$root")
done
"$PYTHON" -m liquidity_migration.reset_path_safety \
  "${paper_preflight_args[@]}" \
  || die "paper runtime descriptor/mount preflight failed before owner leases"

"$PYTHON" -m liquidity_migration.reset_path_safety preflight-demo \
  --anchor "$PWD/data" \
  --root "$PWD/data/bybit-long-demo-event" \
  --root "$PWD/data/bybit-continuous-demo-event" \
  --root "$PWD/data/bybit-carry-demo-event" \
  --continuous-root "$PWD/data/bybit-continuous-demo-event" \
  || die "demo runtime descriptor/mount preflight failed before owner leases"

reset_tree_preflight_args=(preflight --anchor "$PWD/data")
for target in "${SELECTED_ROOTS[@]}" "${EXISTING_TARGETS[@]}"; do
  reset_tree_preflight_args+=(--target "$PWD/$target")
done
"$PYTHON" -m liquidity_migration.reset_path_safety \
  "${reset_tree_preflight_args[@]}" \
  || die "reset target descriptor/mount preflight failed before owner leases"

acquire_demo_account_lease
acquire_paper_account_lease

echo
echo "Checking demo/mainnet boundary and flat-account precondition ..."
(
  unset DEMO REAL_MONEY BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
    BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
  DEMO="$credential_demo"
  REAL_MONEY="$credential_real_money"
  BYBIT_DEMO_API_KEY="$credential_api_key"
  BYBIT_DEMO_API_SECRET="$credential_api_secret"
  export DEMO REAL_MONEY BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET
  validate_real_money_value "$ENV_FILE" "${REAL_MONEY:-__unset__}"
  "$PYTHON" - <<'PY'
import sys

from liquidity_migration.bybit import BybitPrivateClient, resolve_demo_credentials


def amount(row: dict) -> float:
    for key in ("size", "qty", "positionAmt"):
        try:
            value = abs(float(row.get(key) or 0.0))
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            return value
    return 0.0


api_key, api_secret = resolve_demo_credentials()
if not api_key or not api_secret:
    print("ERROR: missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET; cannot prove account is flat", file=sys.stderr)
    raise SystemExit(1)

client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=True)
positions = [row for row in client.get_positions(settle_coin="USDT") if amount(row) > 0.0]
# Bybit documents the omitted orderFilter as all linear order kinds, but query
# StopOrder explicitly too rather than trust a default to expose conditionals.
all_orders = list(client.get_open_orders(settle_coin="USDT"))
conditional_orders = list(
    client.get_open_orders(settle_coin="USDT", order_filter="StopOrder")
)
orders_by_identity: dict[str, dict] = {}
for source, rows in (("all-kinds", all_orders), ("conditional", conditional_orders)):
    for index, row in enumerate(rows):
        identity = str(row.get("orderId") or row.get("orderLinkId") or f"{source}:{index}")
        orders_by_identity[identity] = row
orders = list(orders_by_identity.values())
if positions or orders:
    print(
        f"ERROR: demo account is not flat: open_positions={len(positions)} open_orders={len(orders)}. "
        "Close/cancel them through the normal demo workflow, then retry; this reset never does that for you.",
        file=sys.stderr,
    )
    for row in positions[:20]:
        print(
            f"  position symbol={row.get('symbol', '?')} side={row.get('side', '?')} size={amount(row):g}",
            file=sys.stderr,
        )
    for row in orders[:20]:
        print(
            f"  order symbol={row.get('symbol', '?')} side={row.get('side', '?')} "
            f"status={row.get('orderStatus', '?')} link={row.get('orderLinkId', '?')}",
            file=sys.stderr,
        )
    raise SystemExit(1)
print("  demo-account-flat-ok positions=0 open_orders=0")
PY
)

# The preview inventory was collected while producers were still running.
# Refresh after quiescence so an inbox, capture, or projection created
# during that interval cannot survive into the fresh epoch merely because its
# path did not exist during planning.
refresh_existing_targets

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
suffix=""
[[ -z "$LABEL" ]] || suffix="-$LABEL"
archive_stem="ledger-reset-$STAMP$suffix"
umask 077

MANIFEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ledger-reset-manifest.XXXXXX")"
MANIFEST_PATH="$MANIFEST_DIR/ledger-reset-manifest.txt"
verify_clean_candidate_checkout
git_head="$EXECUTED_CANDIDATE_COMMIT"
{
  echo "ledger_reset_utc=$STAMP"
  echo "git_head=$git_head"
  echo "sleeves=${SELECTED_SLEEVES[*]}"
  echo "include_reports=$INCLUDE_REPORTS"
  echo "include_caches=$INCLUDE_CACHES"
  echo "leave_stopped=$LEAVE_STOPPED"
  echo "env_file=$ENV_FILE"
  echo "account_env_file=$ACCOUNT_ENV_FILE"
  echo "paper_account_env_file=$PAPER_ACCOUNT_ENV_FILE"
  echo "demo_account_lease_path=$DEMO_ACCOUNT_LEASE_PATH"
  echo "demo_boundary=venue_verified_flat_positions_0_open_orders_0"
  echo "paper_boundary=archived_deterministic_epoch_not_carried_forward"
  echo "active_before=${ACTIVE_BEFORE[*]}"
  for target in "${ACCOUNT_STATE_TARGETS[@]}"; do
    echo "account_epoch_target=$target"
  done
  for target in "${EXISTING_TARGETS[@]}"; do
    echo "target=$target"
  done
} > "$MANIFEST_PATH"

echo
echo "Archiving ${#EXISTING_TARGETS[@]} reset target(s) ..."
archive_create_args=(
  create
  --repository "$PWD"
  --archive-directory "$ARCHIVE_DIR"
  --stem "$archive_stem"
  --manifest "$MANIFEST_PATH"
)
for target in "${EXISTING_TARGETS[@]}"; do
  archive_create_args+=(--target "$target")
done
ARCHIVE_PATH=""
SHA_PATH=""
archive_sha=""
archive_size=""
archive_device=""
archive_inode=""
if ! IFS=$'\t' read -r \
  ARCHIVE_PATH SHA_PATH archive_sha archive_size archive_device archive_inode \
  < <(
    "$PYTHON" -m liquidity_migration.account_reset_archive \
      "${archive_create_args[@]}"
  ); then
  die "descriptor-bound reset archive publication failed"
fi
[[ "$ARCHIVE_PATH" == /* && "$SHA_PATH" == /* ]] \
  || die "reset archive publisher returned invalid paths"
[[ "$archive_sha" =~ ^[0-9a-f]{64}$ ]] \
  || die "reset archive publisher returned an invalid digest"
for value in "$archive_size" "$archive_device" "$archive_inode"; do
  [[ "$value" =~ ^[0-9]+$ ]] \
    || die "reset archive publisher returned invalid identity metadata"
done
echo "  archive verified: $ARCHIVE_PATH ($archive_size bytes)"
echo "  sha256: $archive_sha"
echo "  digest sidecar: $SHA_PATH"

verify_clean_candidate_checkout
"$PYTHON" -m liquidity_migration.account_reset_archive verify \
  --repository "$PWD" \
  --archive "$ARCHIVE_PATH" \
  --sidecar "$SHA_PATH" \
  --sha256 "$archive_sha" \
  --device "$archive_device" \
  --inode "$archive_inode" \
  --size "$archive_size" \
  || die "reset archive changed before live-state removal"

# From the first live-state removal onward a failure leaves every managed unit
# stopped: restarting owners into a partially cleared epoch is worse than an outage.
FAILURE_RECOVERY_ALLOWED=0

echo
echo "Removing only archived generated projections and epoch telemetry ..."
"$PYTHON" - \
  "$DEMO_ACCOUNT_LEASE_PATH" "${DEMO_ACCOUNT_LEASE_RECEIPT[@]}" \
  "$PAPER_ACCOUNT_LEASE_PATH" "${PAPER_ACCOUNT_LEASE_RECEIPT[@]}" \
  -- "${ACCOUNT_STATE_TARGETS[@]}" <<'PY'
from pathlib import Path
import sys

from liquidity_migration.account_epoch_reset import (
    clear_account_epoch_roots_preserving_locks,
)
from liquidity_migration.account_owner_lease import (
    PreparedAccountOwnerLease,
    revalidate_inherited_account_owner_lease,
)


def parse_receipt(arguments: list[str], offset: int) -> tuple[PreparedAccountOwnerLease, int]:
    path = Path(arguments[offset])
    values = [int(value, 10) for value in arguments[offset + 1 : offset + 11]]
    if len(values) != 10:
        raise SystemExit("account-owner lease receipt is incomplete before epoch clear")
    return (
        PreparedAccountOwnerLease(
            path=path,
            device=values[0],
            inode=values[1],
            uid=values[2],
            gid=values[3],
            mount_id=values[4],
            parent_device=values[5],
            parent_inode=values[6],
            parent_uid=values[7],
            parent_gid=values[8],
            parent_mount_id=values[9],
        ),
        offset + 11,
    )


demo_receipt, offset = parse_receipt(sys.argv, 1)
paper_receipt, offset = parse_receipt(sys.argv, offset)
if offset >= len(sys.argv) or sys.argv[offset] != "--":
    raise SystemExit("account-owner lease receipt boundary is invalid before epoch clear")
revalidate_inherited_account_owner_lease(8, demo_receipt)
revalidate_inherited_account_owner_lease(7, paper_receipt)

results = clear_account_epoch_roots_preserving_locks(sys.argv[offset + 1 :])
for result in results:
    print(
        f"  cleared {result.root} removed_entries={result.removed_entries} "
        f"preserved_lock_files={len(result.preserved_lock_files)}"
    )
PY
generic_remove_args=(remove --anchor "$PWD/data")
for target in "${EXISTING_TARGETS[@]}"; do
  account_state_target=0
  for account_root in "${ACCOUNT_STATE_TARGETS[@]}"; do
    if [[ "$target" == "$account_root" ]]; then
      account_state_target=1
      break
    fi
  done
  if (( account_state_target )); then
    echo "  preserving persistent locks while clearing $target in place"
    continue
  fi
  generic_remove_args+=(--target "$PWD/$target")
done
"$PYTHON" -m liquidity_migration.reset_path_safety \
  "${generic_remove_args[@]}" \
  || die "descriptor-rooted generated-target removal failed"

# Seed the continuous cycle stream with the verified reset boundary so liveness
# monitoring has an explicit new-epoch fact before the producer restarts.
if (( SELECT_CONTINUOUS )); then
  echo
  echo "Writing post-reset demo-flat and paper-archive boundary heartbeats ..."
  "$PYTHON" - --write-reset-boundary "$STAMP" "$ARCHIVE_PATH" <<'PY'
import datetime as dt
import sys
from pathlib import Path

import polars as pl

from liquidity_migration.storage import write_dataset


stamp = sys.argv[2]
archive_path = sys.argv[3]
now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
for root, dataset, mode in (
    (
        Path("data/bybit-continuous-demo-event"),
        "continuous_fade_demo_cycles",
        "demo",
    ),
    (
        Path("data/bybit-continuous-paper-event"),
        "continuous_fade_paper_cycles",
        "paper",
    ),
):
    row = {
        "cycle_id": f"ledger-reset-{stamp}-{mode}",
        "ts_ms": now_ms,
        "mode": "ledger_reset_boundary",
        "environment": mode,
        "entries_executed": 0,
        "exits_executed": 0,
        "open_trades_before": 0,
        "open_trades_after": 0,
        "reset_archive": archive_path,
    }
    if mode == "demo":
        row.update(
            reason="verified_flat_ledger_reset",
            account_flat_verified=True,
            paper_epoch_archived=False,
            flatness_basis="bybit_demo_positions_and_open_orders",
        )
    else:
        row.update(
            reason="archived_paper_epoch_reset",
            account_flat_verified=False,
            paper_epoch_archived=True,
            flatness_basis="fresh_empty_deterministic_epoch",
        )
    write_dataset(pl.DataFrame([row]), root, dataset, append=True)
print("  reset-boundary-heartbeats-ok demo_venue_flat=1 paper_epoch_archived=1")
PY
fi

# Reset runs as root while every writer is stopped. Restore both private
# account trees and the deliberately shared demo-cache boundary through held
# descriptors; no recursive pathname chown/find traversal is allowed here.
ROOT_RUNTIME_UID="$(id -u root)"
ROOT_RUNTIME_GID="$(id -g root)"
demo_account_normalize_args=(
  normalize-paper --anchor "$PWD/data"
  --uid "$ROOT_RUNTIME_UID" --gid "$ROOT_RUNTIME_GID"
)
for root in "$DEMO_ACCOUNT_ROOT" "$DEMO_ACCOUNT_INBOX_ROOT" "$DEMO_ACCOUNT_CAPTURE_ROOT"; do
  demo_account_normalize_args+=(--root "$PWD/$root")
done
"$PYTHON" -m liquidity_migration.reset_path_safety \
  "${demo_account_normalize_args[@]}" \
  || die "descriptor-rooted demo account permission normalization failed"

paper_normalize_args=(
  normalize-paper --anchor "$PWD/data"
  --uid "$PAPER_RUNTIME_UID" --gid "$PAPER_RUNTIME_GID"
)
for root in \
  "$PAPER_ACCOUNT_ROOT" "$PAPER_ACCOUNT_INBOX_ROOT" "$PAPER_ACCOUNT_CAPTURE_ROOT" \
  data/bybit-long-paper-event data/bybit-continuous-paper-event \
  data/bybit-carry-paper-event; do
  paper_normalize_args+=(--root "$PWD/$root")
done
"$PYTHON" -m liquidity_migration.reset_path_safety \
  "${paper_normalize_args[@]}" \
  || die "descriptor-rooted paper runtime permission normalization failed"

"$PYTHON" -m liquidity_migration.reset_path_safety normalize-demo \
  --anchor "$PWD/data" \
  --root "$PWD/data/bybit-long-demo-event" \
  --root "$PWD/data/bybit-continuous-demo-event" \
  --root "$PWD/data/bybit-carry-demo-event" \
  --continuous-root "$PWD/data/bybit-continuous-demo-event" \
  --uid "$ROOT_RUNTIME_UID" \
  --gid "$PAPER_RUNTIME_GID" \
  || die "descriptor-rooted shared demo cache normalization failed"

if (( LEAVE_STOPPED )); then
  echo
  echo "Leaving every managed unit stopped for explicit owner-first staging ..."
  for unit in "${STOP_UNITS[@]}"; do
    if "$SYSTEMCTL_BIN" is-active --quiet "$unit"; then
      die "leave-stopped verification failed: $unit is active"
    fi
    echo "  inactive: $unit"
  done
else
  release_paper_account_lease "normal completion"
  release_demo_account_lease "normal completion"
  restart_previously_active "normal completion"
  if (( SETTLE_SECONDS > 0 )); then
    echo "Waiting ${SETTLE_SECONDS}s before final service verification ..."
    sleep "$SETTLE_SECONDS"
  fi
  for unit in "${RESTART_UNITS[@]}"; do
    if was_active "$unit"; then
      "$SYSTEMCTL_BIN" is-active --quiet "$unit" || die "restart verification failed: $unit is not active"
      echo "  active: $unit"
    fi
  done
fi

if (( LEAVE_STOPPED )); then
  release_paper_account_lease "post-receipt stopped handoff"
  release_demo_account_lease "post-receipt stopped handoff"
fi
RESTART_COMPLETE=1
SERVICES_STOPPED=0

echo
echo "Ledger reset complete."
echo "  archive: $ARCHIVE_PATH"
echo "  archive sha256: $archive_sha"
echo "  archive digest: $SHA_PATH"
echo "  archived/reset targets: ${#EXISTING_TARGETS[@]}"
echo "  account journals/inboxes/captures: archived; fresh empty epoch created"
echo "  boundary truth: demo venue-flat verified; prior paper epoch archived"
if (( LEAVE_STOPPED )); then
  echo "  service state: all managed units stopped and verified"
else
  echo "  service state: restored to the pre-reset active set and verified"
fi
echo "  preserved: configs, locks, signal files, account-equity high-water state, and unselected caches/reports"
