#!/usr/bin/env bash
# Archive and reset demo/paper trading ledgers on the VPS.
#
# Safe defaults:
#   * no flag means DRY RUN (no service or file mutation)
#   * --execute is required to stop services, archive, and remove ledgers
#   * concurrent execute attempts are refused by a nonblocking process lock
#   * REAL_MONEY/mainnet configuration is refused
#   * submit-armed systemd units must load the same resolved demo env file
#   * the Bybit demo account must have no positions and no open orders
#   * only initially-active daemons/timers are restarted and verified
#   * configs, locks, reports, signal files, market-data caches, and the
#     continuous account-equity high-water risk state are preserved
#
# Examples (from /opt/liquidity-migration):
#   scripts/reset_demo_paper_ledgers.sh
#   scripts/reset_demo_paper_ledgers.sh --sleeves continuous
#   scripts/reset_demo_paper_ledgers.sh --execute --sleeves all --label exit-overhaul
#   scripts/reset_demo_paper_ledgers.sh --execute --sleeves long --include-reports
#
# --include-caches is deliberately separate: it removes selected roots' .cache
# directories and can force a slow market-data bootstrap. residual_momentum.parquet
# and root-level kline datasets are still preserved.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/reset_demo_paper_ledgers.sh [options]

Archive/reset demo + paper trade, order, cycle, and operational-event ledgers.
Default mode is a read-only preview. Mutation requires --execute.

Options:
  --execute                 stop writers, verify demo account flat, archive, reset,
                            restart previously-active units, and verify them
  --dry-run                 explicit preview (the default)
  --sleeves LIST            all (default), long, continuous, or comma-separated list
  --archive-dir DIR         archive destination (default: data/_archive)
  --label LABEL             optional safe suffix added after the UTC timestamp
  --include-reports         also archive/reset reports/ in selected roots
  --include-caches          also archive/reset .cache/ in selected roots (slow rebuild)
  --env-file FILE           demo credential env (default: /etc/liquidity-migration/bybit-demo.env)
  --settle-seconds N        wait before restart verification (default: 3; max: 60)
  -h, --help                show this help

The command never removes configs, .locks, residual_momentum.parquet, root-level
market-data datasets, or continuous_account_equity_state.json. The equity state is
snapshotted into the archive but retained live so a ledger reset cannot erase the
account drawdown high-water. It never cancels orders or closes positions: execute
refuses until the configured Bybit demo account is already flat with no open orders.
Execute also refuses if another reset holds the process lock or if any submit-armed
systemd unit does not load the same resolved credential env file. Tests may override
the default /run/lock/liquidity-migration-ledger-reset.lock with
LEDGER_RESET_LOCK_FILE.
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
ENV_FILE="/etc/liquidity-migration/bybit-demo.env"
SETTLE_SECONDS="${LEDGER_RESET_SETTLE_SECONDS:-3}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
LOCK_FILE="${LEDGER_RESET_LOCK_FILE:-/run/lock/liquidity-migration-ledger-reset.lock}"

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

[[ -d liquidity_migration && -d data ]] \
  || die "run from the repo root (expected liquidity_migration/ and data/)"
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

# Parse and canonicalise sleeve selection. "all" also includes the shared
# compatibility ledger owned by ws_risk; selected named sleeves do not.
SELECT_LONG=0
SELECT_CONTINUOUS=0
SELECT_SHARED=0
normalised_sleeves="$(printf '%s' "$SLEEVES_RAW" | tr ',' ' ')"
[[ -n "${normalised_sleeves//[[:space:]]/}" ]] || die "--sleeves must not be empty"
for sleeve in $normalised_sleeves; do
  case "$(lower "$sleeve")" in
    all)
      SELECT_LONG=1
      SELECT_CONTINUOUS=1
      SELECT_SHARED=1
      ;;
    long)
      SELECT_LONG=1
      ;;
    continuous)
      SELECT_CONTINUOUS=1
      ;;
    *)
      echo "unknown sleeve: $sleeve" >&2
      echo "expected: all, long, continuous, or a comma-separated combination" >&2
      exit 2
      ;;
  esac
done

SELECTED_SLEEVES=()
(( SELECT_LONG )) && SELECTED_SLEEVES+=("long")
(( SELECT_CONTINUOUS )) && SELECTED_SLEEVES+=("continuous")
(( SELECT_SHARED )) && SELECTED_SLEEVES+=("shared-compat")

# Static allowlist of removable paths. Dataset names mirror storage.py and the
# deployed unit roots; the continuous JSONLs are append-only operational
# ledgers consumed by reconciliation. No caller-supplied path reaches rm -rf.
LONG_LEDGER_TARGETS=(
  data/bybit-long-demo-event/long_native_demo_trades
  data/bybit-long-demo-event/long_native_demo_orders
  data/bybit-long-demo-event/long_native_demo_cycles
  data/bybit-long-paper-event/long_native_paper_trades
  data/bybit-long-paper-event/long_native_paper_orders
  data/bybit-long-paper-event/long_native_paper_cycles
)
CONTINUOUS_LEDGER_TARGETS=(
  data/bybit-continuous-demo-event/continuous_fade_demo_trades
  data/bybit-continuous-demo-event/continuous_fade_demo_orders
  data/bybit-continuous-demo-event/continuous_fade_demo_cycles
  data/bybit-continuous-demo-event/continuous_risk_events.jsonl
  data/bybit-continuous-demo-event/continuous_lifecycle_events.jsonl
  data/bybit-continuous-demo-event/continuous_dynexit_shadow.jsonl
  data/bybit-continuous-paper-event/continuous_fade_paper_trades
  data/bybit-continuous-paper-event/continuous_fade_paper_orders
  data/bybit-continuous-paper-event/continuous_fade_paper_cycles
  data/bybit-continuous-paper-event/continuous_risk_events.jsonl
  data/bybit-continuous-paper-event/continuous_lifecycle_events.jsonl
  data/bybit-continuous-paper-event/continuous_dynexit_shadow.jsonl
  data/bybit-continuous-hedge-event/continuous_fade_demo_trades
  data/bybit-continuous-hedge-event/continuous_fade_demo_orders
)
SHARED_LEDGER_TARGETS=(
  data/bybit-demo-event/event_demo_trades
  data/bybit-demo-event/event_demo_orders
  data/bybit-demo-event/event_demo_cycles
)
LONG_ROOTS=(data/bybit-long-demo-event data/bybit-long-paper-event)
CONTINUOUS_ROOTS=(
  data/bybit-continuous-demo-event
  data/bybit-continuous-paper-event
  data/bybit-continuous-hedge-event
)
SHARED_ROOTS=(data/bybit-demo-event)
CONTINUOUS_PRESERVED_AUDIT_TARGETS=(
  data/bybit-continuous-demo-event/continuous_account_equity_state.json
  data/bybit-continuous-paper-event/continuous_account_equity_state.json
)

OUT=()
(( SELECT_LONG )) && append_unique "${LONG_LEDGER_TARGETS[@]}"
(( SELECT_CONTINUOUS )) && append_unique "${CONTINUOUS_LEDGER_TARGETS[@]}"
(( SELECT_SHARED )) && append_unique "${SHARED_LEDGER_TARGETS[@]}"
TARGETS=("${OUT[@]}")

OUT=()
(( SELECT_LONG )) && append_unique "${LONG_ROOTS[@]}"
(( SELECT_CONTINUOUS )) && append_unique "${CONTINUOUS_ROOTS[@]}"
(( SELECT_SHARED )) && append_unique "${SHARED_ROOTS[@]}"
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

EXISTING_TARGETS=()
for target in "${TARGETS[@]}"; do
  case "$target" in
    data/bybit-*) ;;
    *) die "internal safety error: non-data target '$target'" ;;
  esac
  [[ "$target" != *".."* ]] || die "internal safety error: traversal target '$target'"
  [[ -e "$target" || -L "$target" ]] && EXISTING_TARGETS+=("$target")
done

# Account drawdown is an account-level risk memory, not a disposable ledger.
# Snapshot it at the reset boundary for auditability but deliberately do not add
# it to TARGETS/EXISTING_TARGETS: removing it would make the next cycle seed its
# high-water from the post-loss balance and report a false zero drawdown.
PRESERVED_AUDIT_TARGETS=()
if (( SELECT_CONTINUOUS )); then
  for target in "${CONTINUOUS_PRESERVED_AUDIT_TARGETS[@]}"; do
    [[ -e "$target" || -L "$target" ]] && PRESERVED_AUDIT_TARGETS+=("$target")
  done
fi

# The archive must never sit inside a directory that is about to be archived
# and removed. Besides losing the recovery copy, tar could consume its own
# growing output. Canonicalise through existing symlinks and collapse '.', '..',
# and relative/absolute aliases; a lexical prefix check is bypassable with e.g.
# ``target/./_archive``.
CANONICAL_PYTHON="$PWD/.venv/bin/python"
if [[ ! -x "$CANONICAL_PYTHON" ]]; then
  CANONICAL_PYTHON="$(command -v python3 || true)"
fi
[[ -n "$CANONICAL_PYTHON" && -x "$CANONICAL_PYTHON" ]] \
  || die "Python runtime is required to canonicalise the archive safety boundary"
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
done

# The account is shared, so every writer and the shared risk authority must be
# quiesced even for a one-sleeve reset. Otherwise the unselected sleeve could
# submit while ws_risk is deliberately down. Timers/readers are stopped to avoid
# a hedge launch or false liveness page during the maintenance window.
STOP_UNITS=(
  liquidity-migration-demo-liveness.timer
  liquidity-migration-combined-book-report.timer
  liquidity-migration-continuous-hedge.timer
  liquidity-migration-continuous-rmom-refresh.timer
  liquidity-migration-bybit-long-demo.service
  liquidity-migration-bybit-long-paper.service
  liquidity-migration-bybit-continuous-demo.service
  liquidity-migration-bybit-continuous-paper.service
  liquidity-migration-continuous-hedge.service
  liquidity-migration-continuous-rmom-refresh.service
  liquidity-migration-demo-liveness.service
  liquidity-migration-combined-book-report.service
  liquidity-migration-bybit-risk.service
)
# These units can submit or manage demo-account orders. Every one must use the
# exact credential file selected by --env-file (after symlink/path resolution)
# before any unit is stopped. Paper/read-only units are intentionally excluded.
ACCOUNT_BOUND_UNITS=(
  liquidity-migration-bybit-risk.service
  liquidity-migration-bybit-long-demo.service
  liquidity-migration-bybit-continuous-demo.service
  liquidity-migration-continuous-hedge.service
)
NON_RESTARTABLE_ONESHOTS=(
  liquidity-migration-continuous-hedge.service
  liquidity-migration-continuous-rmom-refresh.service
  liquidity-migration-demo-liveness.service
  liquidity-migration-combined-book-report.service
)
RESTART_UNITS=(
  liquidity-migration-bybit-risk.service
  liquidity-migration-bybit-long-demo.service
  liquidity-migration-bybit-long-paper.service
  liquidity-migration-bybit-continuous-demo.service
  liquidity-migration-bybit-continuous-paper.service
  liquidity-migration-continuous-rmom-refresh.timer
  liquidity-migration-continuous-hedge.timer
  liquidity-migration-combined-book-report.timer
  liquidity-migration-demo-liveness.timer
)

echo "Ledger reset plan"
echo "  mode: $MODE"
echo "  sleeves: ${SELECTED_SLEEVES[*]}"
echo "  archive dir: $ARCHIVE_DIR"
echo "  include reports: $INCLUDE_REPORTS"
echo "  include caches: $INCLUDE_CACHES"
echo "  existing targets: ${#EXISTING_TARGETS[@]}"
for target in "${EXISTING_TARGETS[@]}"; do
  size="$(du -sh "$target" 2>/dev/null | awk '{print $1}' || true)"
  echo "    - $target (${size:-size unknown})"
done

echo "  preserved risk-state snapshots: ${#PRESERVED_AUDIT_TARGETS[@]}"
for target in "${PRESERVED_AUDIT_TARGETS[@]}"; do
  echo "    - $target (archived, retained live)"
done

echo "  preserved by default: configs/, .locks/, reports/, .cache/, residual_momentum.parquet, root-level market data, account-equity high-water state"
(( INCLUDE_REPORTS )) && echo "  selected exception: reports/ will be archived and reset"
(( INCLUDE_CACHES )) && echo "  selected exception: .cache/ will be archived and reset; expect market-data bootstrap"
echo "  quiesced units: ${#STOP_UNITS[@]} (all shared-account writers plus maintenance readers/timers)"

if [[ "$MODE" == "dry-run" ]]; then
  echo
  echo "DRY RUN: no services or files were changed."
  echo "Execute only after reviewing the plan:"
  execute_hint="scripts/reset_demo_paper_ledgers.sh --execute --sleeves $SLEEVES_RAW"
  [[ -n "$LABEL" ]] && execute_hint="$execute_hint --label $LABEL"
  (( INCLUDE_REPORTS )) && execute_hint="$execute_hint --include-reports"
  (( INCLUDE_CACHES )) && execute_hint="$execute_hint --include-caches"
  echo "  $execute_hint"
  echo "Execute will refuse REAL_MONEY, missing demo credentials, any open demo position/order, missing units, or restart verification failure."
  exit 0
fi

[[ "${#EXISTING_TARGETS[@]}" -gt 0 ]] || {
  echo
  echo "Nothing to reset: none of the selected allowlisted targets exists."
  exit 0
}
[[ -r "$ENV_FILE" ]] || die "demo env file is missing or unreadable: $ENV_FILE"
command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || die "systemctl command not found: $SYSTEMCTL_BIN"

PYTHON="$PWD/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || die "Python runtime not found (.venv/bin/python or python3)"

# Validate the final service environment before acquiring the execute lock or
# querying/stopping systemd. This runs in a subshell so values from the secret
# env file never leak into this script's later command environment or output.
(
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  validate_real_money_value "$ENV_FILE" "${REAL_MONEY-__unset__}"
)

# Keep fd 9 open for the entire execute process. BSD/Linux flock locks are tied
# to this inherited open-file description, so the lock remains held while the
# EXIT trap performs failure recovery and restarts previously-active services.
[[ -n "$LOCK_FILE" ]] || die "LEDGER_RESET_LOCK_FILE must not be empty"
lock_parent="${LOCK_FILE%/*}"
[[ "$lock_parent" != "$LOCK_FILE" ]] || lock_parent="."
[[ -d "$lock_parent" ]] || die "ledger-reset lock directory does not exist: $lock_parent"
if ! { exec 9>"$LOCK_FILE"; }; then
  die "cannot open ledger-reset process lock: $LOCK_FILE"
fi
if ! "$PYTHON" -c '
import fcntl
import sys

try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
' 9; then
  die "another demo/paper ledger reset is already executing (lock: $LOCK_FILE)"
fi

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
MANIFEST_DIR=""

restart_previously_active() {
  local context="$1" unit failed=0
  echo
  echo "Restarting previously-active daemons/timers ($context) ..."
  for unit in "${RESTART_UNITS[@]}"; do
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

cleanup() {
  local rc="$?"
  trap - EXIT INT TERM
  [[ -z "$MANIFEST_DIR" ]] || rm -rf -- "$MANIFEST_DIR"
  if (( SERVICES_STOPPED )) && (( ! RESTART_COMPLETE )); then
    echo "Reset did not complete; attempting to restore pre-reset service state." >&2
    restart_previously_active "failure recovery" || true
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
echo "  quiescence verified"

echo
echo "Checking demo/mainnet boundary and flat-account precondition ..."
(
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  validate_real_money_value "$ENV_FILE" "${REAL_MONEY-__unset__}"
  "$PYTHON" - <<'PY'
import sys

from liquidity_migration.bybit import BybitPrivateClient, resolve_private_credentials


def amount(row: dict) -> float:
    for key in ("size", "qty", "positionAmt"):
        try:
            value = abs(float(row.get(key) or 0.0))
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            return value
    return 0.0


api_key, api_secret, demo = resolve_private_credentials()
if not demo:
    print("ERROR: resolved credentials select REAL_MONEY/mainnet; refusing reset", file=sys.stderr)
    raise SystemExit(1)
if not api_key or not api_secret:
    print("ERROR: missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET; cannot prove account is flat", file=sys.stderr)
    raise SystemExit(1)

client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=True)
positions = [row for row in client.get_positions(settle_coin="USDT") if amount(row) > 0.0]
orders = list(client.get_open_orders(settle_coin="USDT"))
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

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
suffix=""
[[ -z "$LABEL" ]] || suffix="-$LABEL"
mkdir -p -- "$ARCHIVE_DIR"
archive_base="$ARCHIVE_DIR/ledger-reset-$STAMP$suffix"
ARCHIVE_PATH="$archive_base.tar.gz"
archive_counter=2
while [[ -e "$ARCHIVE_PATH" ]]; do
  ARCHIVE_PATH="$archive_base-$archive_counter.tar.gz"
  archive_counter=$((archive_counter + 1))
done
umask 077

MANIFEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ledger-reset-manifest.XXXXXX")"
MANIFEST_PATH="$MANIFEST_DIR/ledger-reset-manifest.txt"
git_head="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
{
  echo "ledger_reset_utc=$STAMP"
  echo "git_head=$git_head"
  echo "sleeves=${SELECTED_SLEEVES[*]}"
  echo "include_reports=$INCLUDE_REPORTS"
  echo "include_caches=$INCLUDE_CACHES"
  echo "env_file=$ENV_FILE"
  echo "active_before=${ACTIVE_BEFORE[*]}"
  for target in "${EXISTING_TARGETS[@]}"; do
    echo "target=$target"
  done
  for target in "${PRESERVED_AUDIT_TARGETS[@]}"; do
    echo "preserved_risk_state=$target"
  done
} > "$MANIFEST_PATH"

echo
echo "Archiving ${#EXISTING_TARGETS[@]} reset target(s) and ${#PRESERVED_AUDIT_TARGETS[@]} preserved risk-state snapshot(s) ..."
tar -czf "$ARCHIVE_PATH" \
  -C "$PWD" "${EXISTING_TARGETS[@]}" "${PRESERVED_AUDIT_TARGETS[@]}" \
  -C "$MANIFEST_DIR" ledger-reset-manifest.txt
tar -tzf "$ARCHIVE_PATH" >/dev/null
archive_size="$(du -sh "$ARCHIVE_PATH" 2>/dev/null | cut -f1 || true)"
if command -v sha256sum >/dev/null 2>&1; then
  archive_sha="$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  archive_sha="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')"
else
  die "no SHA-256 tool is available; refusing to remove ledgers without a durable archive digest"
fi
SHA_PATH="$ARCHIVE_PATH.sha256"
printf '%s  %s\n' "$archive_sha" "$(basename "$ARCHIVE_PATH")" > "$SHA_PATH"
# Durability boundary: close+fsync both archive artifacts and their directory
# before deleting any live ledger. A successful tar listing alone does not prove
# the bytes reached stable storage after a power loss.
"$PYTHON" -c '
import os
import pathlib
import sys

for raw in sys.argv[1:]:
    fd = os.open(raw, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
parent = pathlib.Path(sys.argv[1]).resolve().parent
fd = os.open(parent, os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
' "$ARCHIVE_PATH" "$SHA_PATH"
echo "  archive verified: $ARCHIVE_PATH (${archive_size:-size unknown})"
echo "  sha256: $archive_sha"
echo "  digest sidecar: $SHA_PATH"

echo
echo "Removing only the archived allowlisted ledger targets ..."
for target in "${EXISTING_TARGETS[@]}"; do
  rm -rf -- "$target"
  [[ ! -e "$target" && ! -L "$target" ]] || die "failed to remove target: $target"
  echo "  removed $target"
done

restart_previously_active "normal completion"
if (( SETTLE_SECONDS > 0 )); then
  echo "Waiting ${SETTLE_SECONDS}s before service verification ..."
  sleep "$SETTLE_SECONDS"
fi
for unit in "${RESTART_UNITS[@]}"; do
  if was_active "$unit"; then
    "$SYSTEMCTL_BIN" is-active --quiet "$unit" || die "restart verification failed: $unit is not active"
    echo "  active: $unit"
  fi
done
RESTART_COMPLETE=1
SERVICES_STOPPED=0

echo
echo "Ledger reset complete."
echo "  archive: $ARCHIVE_PATH"
echo "  archive sha256: $archive_sha"
echo "  archive digest: $SHA_PATH"
echo "  removed targets: ${#EXISTING_TARGETS[@]}"
echo "  service state: restored to the pre-reset active set and verified"
echo "  preserved: configs, locks, signal files, account-equity high-water state, and unselected caches/reports"
