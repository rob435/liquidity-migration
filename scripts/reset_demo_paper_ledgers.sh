#!/usr/bin/env bash
# Archive and rebuild demo/paper account state and strategy projections on the VPS.
#
# Safe defaults:
#   * no flag means DRY RUN (no service or file mutation)
#   * --execute is required to stop services, archive, and rebuild projections
#   * concurrent execute attempts are refused by a nonblocking process lock
#   * execute holds the authenticated demo-account lease from quiescence through
#     archive/reset, then releases it only for the owner-first restart handoff
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

Archive and rebuild demo + paper account state and strategy trade/order
projections. Canonical account journals begin a new epoch only after the venue
flatness guard; the prior journals remain in the verified archive.
Default mode is a read-only preview. Mutation requires --execute.

Options:
  --execute                 stop writers, verify demo account flat, archive, rebuild,
                            restart previously-active units, and verify them
  --leave-stopped           execute the reset but leave every managed unit stopped;
                            required before explicit paper-owner/demo-owner staging
  --dry-run                 explicit preview (the default)
  --sleeves LIST            all (default), long, continuous, or comma-separated list
  --archive-dir DIR         archive destination (default: data/_archive)
  --label LABEL             optional safe suffix added after the UTC timestamp
  --include-reports         also archive/reset reports/ in selected roots
  --include-caches          also archive/reset .cache/ in selected roots (slow rebuild)
  --receipt FILE            write one source-reopening success receipt (mode 0600);
                            requires --execute --leave-stopped and an absolute new path
  --env-file FILE           demo credential env (default: /etc/liquidity-migration/bybit-demo.env)
  --account-env-file FILE   demo owner route env (default: /etc/liquidity-migration/account-execution.env)
  --paper-account-env-file FILE
                            paper owner route env (default: /etc/liquidity-migration/account-paper-execution.env)
  --settle-seconds N        wait before restart verification (default: 3; max: 60)
  -h, --help                show this help

After a continuous reset it writes fresh demo + paper cycle heartbeats. The demo
heartbeat records the venue-verified flat boundary; the paper heartbeat records
that the old deterministic epoch was archived and not carried forward.

Legacy strategy trade/order ledgers are rebuildable projections of their
canonical journals. The account-owner roots, intent inboxes, and raw captures
are a separate execution epoch: execute archives them in full, verifies the
archive, then creates fresh empty directories. It never discards execution
history without a durable archive. Demo reset additionally requires venue-flat
proof; paper reset explicitly retires the archived simulated epoch without
pretending the demo proof applies to it.

The command never removes configs, .locks, canonical_journal/, residual_momentum.parquet, root-level
market-data datasets, or continuous_account_equity_state.json. The equity state is
snapshotted into the archive but retained live so a ledger reset cannot erase the
account drawdown high-water. It never cancels orders or closes positions: execute
refuses until the configured Bybit demo account is already flat with no open orders.
Execute also refuses if another reset holds the process lock or if any submit-armed
systemd unit does not load the same resolved credential env file. Tests may override
the default /run/lock/liquidity-migration-ledger-reset.lock with
LEDGER_RESET_LOCK_FILE. The authenticated demo-account lock path has no operator
override; it is derived from Bybit `userID` through the same repository helper as
the owner, rule probe, and venue-accounting command.
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
RECEIPT_PATH=""
RECEIPT_CANDIDATE_COMMIT=""
RESET_STARTED_TS_NS=""
ENV_FILE="/etc/liquidity-migration/bybit-demo.env"
ACCOUNT_ENV_FILE="${ACCOUNT_EXECUTION_ENV_FILE:-/etc/liquidity-migration/account-execution.env}"
PAPER_ACCOUNT_ENV_FILE="${ACCOUNT_PAPER_EXECUTION_ENV_FILE:-/etc/liquidity-migration/account-paper-execution.env}"
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
    --receipt)
      [[ "$#" -ge 2 ]] || die "--receipt requires a value"
      RECEIPT_PATH="$2"
      shift 2
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

ACCOUNT_STATE_TARGETS=(
  "$DEMO_ACCOUNT_ROOT"
  "$DEMO_ACCOUNT_INBOX_ROOT"
  "$DEMO_ACCOUNT_CAPTURE_ROOT"
  "$PAPER_ACCOUNT_ROOT"
  "$PAPER_ACCOUNT_INBOX_ROOT"
  "$PAPER_ACCOUNT_CAPTURE_ROOT"
)

# Account-owner state is an independently reset execution epoch. Refuse route
# layouts that overlap each other or any strategy/legacy root: otherwise a
# seemingly narrow account reset could recursively erase a preserved canonical
# journal, cache, config, or an unselected sleeve. The normal deployment uses six
# sibling roots, so overlap indicates a bad env file rather than a supported
# layout.
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
        "data/bybit-continuous-hedge-event",
        "data/bybit-demo-event",
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

# Parse and canonicalise sleeve selection. "all" also retires the obsolete
# shared compatibility root; continuous retires the obsolete hedge ledger root.
SELECT_LONG=0
SELECT_CONTINUOUS=0
RETIRE_LEGACY_SHARED=0
normalised_sleeves="$(printf '%s' "$SLEEVES_RAW" | tr ',' ' ')"
[[ -n "${normalised_sleeves//[[:space:]]/}" ]] || die "--sleeves must not be empty"
for sleeve in $normalised_sleeves; do
  case "$(lower "$sleeve")" in
    all)
      SELECT_LONG=1
      SELECT_CONTINUOUS=1
      RETIRE_LEGACY_SHARED=1
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
(( RETIRE_LEGACY_SHARED )) && SELECTED_SLEEVES+=("retire-shared-compat")

# Static allowlist of generated projections/epoch telemetry. The canonical
# journal is intentionally absent: no caller-supplied path can delete it.
LONG_LEDGER_TARGETS=(
  data/bybit-long-demo-event/long_native_demo_trades
  data/bybit-long-demo-event/long_native_demo_orders
  data/bybit-long-demo-event/long_native_demo_cycles
  data/bybit-long-demo-event/strategy_event_tape.jsonl
  data/bybit-long-demo-event/strategy_event_decision_tape.jsonl
  data/bybit-long-demo-event/strategy_target_scheduling_capture.jsonl
  data/bybit-long-demo-event/natural-effective-runtime-config.json
  data/bybit-long-paper-event/long_native_paper_trades
  data/bybit-long-paper-event/long_native_paper_orders
  data/bybit-long-paper-event/long_native_paper_cycles
  data/bybit-long-paper-event/strategy_event_tape.jsonl
  data/bybit-long-paper-event/strategy_event_decision_tape.jsonl
  data/bybit-long-paper-event/strategy_target_scheduling_capture.jsonl
)
CONTINUOUS_LEDGER_TARGETS=(
  data/bybit-continuous-demo-event/continuous_fade_demo_trades
  data/bybit-continuous-demo-event/continuous_fade_demo_orders
  data/bybit-continuous-demo-event/continuous_fade_demo_cycles
  data/bybit-continuous-demo-event/continuous_risk_events.jsonl
  data/bybit-continuous-demo-event/continuous_lifecycle_events.jsonl
  data/bybit-continuous-demo-event/strategy_event_tape.jsonl
  data/bybit-continuous-demo-event/strategy_event_decision_tape.jsonl
  data/bybit-continuous-demo-event/strategy_target_scheduling_capture.jsonl
  data/bybit-continuous-demo-event/natural-effective-runtime-config.json
  data/bybit-continuous-paper-event/continuous_fade_paper_trades
  data/bybit-continuous-paper-event/continuous_fade_paper_orders
  data/bybit-continuous-paper-event/continuous_fade_paper_cycles
  data/bybit-continuous-paper-event/continuous_risk_events.jsonl
  data/bybit-continuous-paper-event/continuous_lifecycle_events.jsonl
  data/bybit-continuous-paper-event/strategy_event_tape.jsonl
  data/bybit-continuous-paper-event/strategy_event_decision_tape.jsonl
  data/bybit-continuous-paper-event/strategy_target_scheduling_capture.jsonl
)
NATURAL_RUNTIME_TARGETS=(data/bybit-natural-account-cutover)
LEGACY_CONTINUOUS_TARGETS=(data/bybit-continuous-hedge-event)
LEGACY_SHARED_TARGETS=(data/bybit-demo-event)
LONG_ROOTS=(data/bybit-long-demo-event data/bybit-long-paper-event)
CONTINUOUS_ROOTS=(
  data/bybit-continuous-demo-event
  data/bybit-continuous-paper-event
)
CANONICAL_SPECS=()
if (( SELECT_LONG )); then
  CANONICAL_SPECS+=(
    "data/bybit-long-demo-event|long_native_demo_trades|long_native_demo_orders|demo|long"
    "data/bybit-long-paper-event|long_native_paper_trades|long_native_paper_orders|paper|long"
  )
fi
if (( SELECT_CONTINUOUS )); then
  CANONICAL_SPECS+=(
    "data/bybit-continuous-demo-event|continuous_fade_demo_trades|continuous_fade_demo_orders|demo|continuous"
    "data/bybit-continuous-paper-event|continuous_fade_paper_trades|continuous_fade_paper_orders|paper|continuous"
  )
fi
CONTINUOUS_PRESERVED_AUDIT_TARGETS=(
  data/bybit-continuous-demo-event/continuous_account_equity_state.json
  data/bybit-continuous-paper-event/continuous_account_equity_state.json
)

OUT=()
(( SELECT_LONG )) && append_unique "${LONG_LEDGER_TARGETS[@]}"
if (( SELECT_CONTINUOUS )); then
  append_unique "${CONTINUOUS_LEDGER_TARGETS[@]}" "${LEGACY_CONTINUOUS_TARGETS[@]}"
fi
(( RETIRE_LEGACY_SHARED )) && append_unique "${LEGACY_SHARED_TARGETS[@]}"
append_unique "${ACCOUNT_STATE_TARGETS[@]}"
append_unique "${NATURAL_RUNTIME_TARGETS[@]}"
TARGETS=("${OUT[@]}")

OUT=()
(( SELECT_LONG )) && append_unique "${LONG_ROOTS[@]}"
(( SELECT_CONTINUOUS )) && append_unique "${CONTINUOUS_ROOTS[@]}"
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

# The archive must never sit inside anything that will be added to the archive,
# whether that path is reset or retained live. Besides losing the recovery copy,
# tar could consume its own growing output. Canonical journals may be created by
# the pre-archive bootstrap below, so guard their potential paths even when they
# do not exist yet. Canonicalise through existing symlinks and collapse '.',
# '..', and relative/absolute aliases; a lexical prefix check is bypassable with
# e.g. ``target/./_archive``.
canonical_path() {
  "$CANONICAL_PYTHON" -c '
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve(strict=False))
' "$1"
}
archive_compare="$(canonical_path "$ARCHIVE_DIR")"
ARCHIVE_INPUT_PATHS=("${TARGETS[@]}" "${PRESERVED_AUDIT_TARGETS[@]}")
for root in "${SELECTED_ROOTS[@]}"; do
  ARCHIVE_INPUT_PATHS+=("$root/canonical_journal")
done
for target in "${ARCHIVE_INPUT_PATHS[@]}"; do
  target_compare="$(canonical_path "$target")"
  case "$archive_compare/" in
    "$target_compare/"*)
      die "--archive-dir must be outside reset targets (archive '$ARCHIVE_DIR', target '$target')"
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
  liquidity-migration-continuous-rmom-refresh.timer
  liquidity-migration-continuous-hedge.timer
  liquidity-migration-demo-liveness.timer
)
RESTART_UNITS=("${OWNER_RESTART_UNITS[@]}" "${DOWNSTREAM_RESTART_UNITS[@]}")

if [[ -n "$RECEIPT_PATH" ]]; then
  [[ "$MODE" == "execute" ]] \
    || die "--receipt is a success artifact and requires --execute"
  (( LEAVE_STOPPED )) \
    || die "--receipt requires --leave-stopped so no owner can write the fresh roots before receipt creation"
  [[ "$RECEIPT_PATH" == /* && "$RECEIPT_PATH" != *$'\n'* ]] \
    || die "--receipt must be one absolute path"
  RECEIPT_CANDIDATE_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
  [[ "$RECEIPT_CANDIDATE_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    || die "--receipt requires a repository checkout with one full Git HEAD"
  receipt_preflight_args=(
    preflight
    --output "$RECEIPT_PATH"
  )
  for target in "${ACCOUNT_STATE_TARGETS[@]}"; do
    receipt_preflight_args+=(--forbidden-root "$PWD/$target")
  done
  "$CANONICAL_PYTHON" -m liquidity_migration.account_reset_receipt \
    "${receipt_preflight_args[@]}" >/dev/null \
    || die "account reset receipt output preflight failed"
  RESET_STARTED_TS_NS="$("$CANONICAL_PYTHON" -c 'import time; print(time.time_ns())')"
fi

echo "Ledger reset plan"
echo "  mode: $MODE"
echo "  sleeves: ${SELECTED_SLEEVES[*]}"
echo "  archive dir: $ARCHIVE_DIR"
echo "  include reports: $INCLUDE_REPORTS"
echo "  include caches: $INCLUDE_CACHES"
echo "  leave managed units stopped: $LEAVE_STOPPED"
[[ -z "$RECEIPT_PATH" ]] || echo "  structured success receipt: $RECEIPT_PATH"
echo "  canonical account state: ${ACCOUNT_STATE_TARGETS[*]}"
echo "  existing targets: ${#EXISTING_TARGETS[@]}"
for target in "${EXISTING_TARGETS[@]}"; do
  size="$(du -sh "$target" 2>/dev/null | awk '{print $1}' || true)"
  echo "    - $target (${size:-size unknown})"
done

echo "  preserved risk-state snapshots: ${#PRESERVED_AUDIT_TARGETS[@]}"
for target in "${PRESERVED_AUDIT_TARGETS[@]}"; do
  echo "    - $target (archived, retained live)"
done

echo "  preserved by default: strategy canonical_journal/, configs/, .locks/, reports/, .cache/, residual_momentum.parquet, root-level market data, account-equity high-water state"
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
  (( LEAVE_STOPPED )) && execute_hint="$execute_hint --leave-stopped"
  echo "  $execute_hint"
  echo "Execute will refuse REAL_MONEY, missing demo credentials, any open demo position/order, missing units, or restart verification failure."
  exit 0
fi

# Even a first-time/fully-absent layout must pass through the flatness guard,
# create all six fresh roots, and leave a durable archive+manifest receipt. A
# successful no-op here would make a missing epoch indistinguishable from a
# completed reset.
[[ -r "$ENV_FILE" ]] || die "demo env file is missing or unreadable: $ENV_FILE"
command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || die "systemctl command not found: $SYSTEMCTL_BIN"

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

# This read-only authenticated call resolves the venue account before any
# service mutation. The ordinary module constructor has no path override; the
# returned path is account-wide across API keys because it is keyed by Bybit
# userID, not by a caller-selected ledger root or API-key fingerprint.
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

acquire_demo_account_lease() {
  local lock_parent previous_umask rc

  lock_parent="${DEMO_ACCOUNT_LEASE_PATH%/*}"
  [[ "$lock_parent" != "$DEMO_ACCOUNT_LEASE_PATH" ]] || lock_parent="."
  if ! mkdir -p -- "$lock_parent"; then
    die "cannot create canonical demo-account lease directory: $lock_parent"
  fi

  previous_umask="$(umask)"
  umask 077
  if ! { exec 8<>"$DEMO_ACCOUNT_LEASE_PATH"; }; then
    umask "$previous_umask"
    die "cannot open canonical demo-account lease: $DEMO_ACCOUNT_LEASE_PATH (no alternate path is allowed)"
  fi
  umask "$previous_umask"

  if "$PYTHON" -c '
import fcntl
import json
import os
import sys
import time


fd = int(sys.argv[1])
path = sys.argv[2]
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(73)
except OSError as exc:
    print(f"cannot lock {path}: {exc}", file=sys.stderr)
    raise SystemExit(74)
os.fchmod(fd, 0o600)
metadata = {
    "environment": "demo",
    "pid": os.getppid(),
    "role": "ledger_reset",
    "started_at_ns": time.time_ns(),
    "venue": "bybit",
}
payload = (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8")
os.ftruncate(fd, 0)
os.lseek(fd, 0, os.SEEK_SET)
os.write(fd, payload)
os.fsync(fd)
' 8 "$DEMO_ACCOUNT_LEASE_PATH"; then
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

cleanup() {
  local rc="$?"
  trap - EXIT INT TERM
  [[ -z "$MANIFEST_DIR" ]] || rm -rf -- "$MANIFEST_DIR"
  if (( SERVICES_STOPPED )) && (( ! RESTART_COMPLETE )); then
    release_demo_account_lease "failure recovery"
    if (( FAILURE_RECOVERY_ALLOWED )) && (( ! LEAVE_STOPPED )); then
      echo "Reset did not complete; attempting to restore pre-reset service state." >&2
      restart_previously_active "failure recovery" || true
    else
      echo "Reset handoff did not complete; refusing an automatic retry. Managed units remain stopped." >&2
    fi
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

acquire_demo_account_lease

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
# Bybit documents the omitted orderFilter as all linear order kinds, but issue
# an explicit StopOrder query as well. Reset is a destructive epoch boundary;
# it must not rely on a wrapper/default continuing to expose conditional rows.
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

# Import any pre-journal rows before touching projections. Demo rows receive the
# venue-flat fact proven immediately above and become awaiting_pnl without a
# fabricated close. Paper rows receive a distinct archived-epoch fact and become
# inactive; no demo venue observation or realized P&L is attributed to them.
echo
echo "Bootstrapping and verifying canonical journals ..."
"$PYTHON" - --prepare-canonical-reset "${CANONICAL_SPECS[@]}" <<'PY'
import datetime as dt
import sys
from pathlib import Path

from liquidity_migration.canonical_journal import (
    journal_path,
    rebuild_all_registered_projections,
    record_archived_paper_epoch_reset,
    record_verified_flat_snapshot,
    verify_journal,
)
from liquidity_migration.lifecycle_bridge import bootstrap_legacy_ledgers


now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
reset_id = f"ledger-rebuild-{now_ms}"
for raw in sys.argv[2:]:
    root_raw, trades, orders, mode, sleeve = raw.split("|", 4)
    root = Path(root_raw)
    root.mkdir(parents=True, exist_ok=True)
    bootstrap_legacy_ledgers(
        root,
        trade_dataset=trades,
        order_dataset=orders,
        mode=mode,
        sleeve=sleeve,
        now_ms=now_ms,
    )
    if mode == "demo":
        record_verified_flat_snapshot(
            root,
            now_ms=now_ms,
            verification_id=f"{reset_id}:demo-venue-flat",
            source="reset_workflow_demo_positions_and_open_orders_flat",
        )
        boundary = "demo_venue_flat"
    elif mode == "paper":
        record_archived_paper_epoch_reset(
            root,
            now_ms=now_ms,
            reset_id=f"{reset_id}:paper-epoch-archived",
            source="reset_workflow_archived_deterministic_paper_epoch",
        )
        boundary = "paper_epoch_archived"
    else:
        raise ValueError(f"unsupported reset mode: {mode}")
    rebuild_all_registered_projections(root)
    receipt = verify_journal(root)
    print(
        f"  canonical-ok root={root} events={receipt['events']} "
        f"trades={receipt['trades']} boundary={boundary} path={journal_path(root)}"
    )
PY

# The preview inventory was collected while producers were still running.
# Refresh after quiescence and bootstrap so an inbox/capture/projection created
# during that interval cannot survive into the fresh epoch merely because its
# path did not exist during planning.
refresh_existing_targets

# Snapshot each now-existing journal in the archive while retaining it live.
for root in "${SELECTED_ROOTS[@]}"; do
  canonical="$root/canonical_journal"
  [[ -e "$canonical" ]] && PRESERVED_AUDIT_TARGETS+=("$canonical")
done

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
if [[ -n "$RECEIPT_PATH" && "$git_head" != "$RECEIPT_CANDIDATE_COMMIT" ]]; then
  die "repository HEAD changed after the reset receipt preflight"
fi
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
echo "Removing only archived generated projections and epoch telemetry ..."
for target in "${EXISTING_TARGETS[@]}"; do
  rm -rf -- "$target"
  [[ ! -e "$target" && ! -L "$target" ]] || die "failed to remove target: $target"
  echo "  removed $target"
done

echo
echo "Creating fresh empty canonical account roots, inboxes, and captures ..."
for target in "${ACCOUNT_STATE_TARGETS[@]}"; do
  mkdir -p -- "$target"
  chmod 0700 "$target"
  echo "  created $target"
done

echo
echo "Rebuilding trade/order projections from canonical journals ..."
"$PYTHON" - --rebuild-canonical-projections "${CANONICAL_SPECS[@]}" <<'PY'
import sys
from pathlib import Path

from liquidity_migration.canonical_journal import rebuild_all_registered_projections, verify_journal


seen: set[Path] = set()
for raw in sys.argv[2:]:
    root = Path(raw.split("|", 1)[0])
    if root in seen:
        continue
    seen.add(root)
    counts = rebuild_all_registered_projections(root)
    receipt = verify_journal(root)
    print(f"  projection-rebuild-ok root={root} events={receipt['events']} rows={counts}")
PY

# A continuous reset deliberately removes both the trades and cycles datasets.
# The hedge manager treats that exact shape as UNKNOWN (correct for an arbitrary
# missing ledger) and its already-enabled OnBootSec timer can fire before the
# restarted continuous daemon writes its first cycle, producing a false failed
# hedge run immediately after a reset that just proved the account flat. Record a
# new-epoch boundary heartbeat while every writer is still quiesced. This is not
# restored trading history: it is the durable post-reset fact established by the
# flat-account guard above, and it lets the hedge manager distinguish a controlled
# flat reset from corrupt/missing state without weakening its fail-closed default.
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

FAILURE_RECOVERY_ALLOWED=0
release_demo_account_lease "normal completion"
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
RESTART_COMPLETE=1
SERVICES_STOPPED=0

if [[ -n "$RECEIPT_PATH" ]]; then
  receipt_create_args=(
    create
    --repository-root "$PWD"
    --candidate-commit "$RECEIPT_CANDIDATE_COMMIT"
    --started-ts-ns "$RESET_STARTED_TS_NS"
    --archive "$(canonical_path "$ARCHIVE_PATH")"
    --sha256-sidecar "$(canonical_path "$SHA_PATH")"
    --output "$RECEIPT_PATH"
    --leave-stopped
    --demo-account-root "$(canonical_path "$DEMO_ACCOUNT_ROOT")"
    --demo-inbox-root "$(canonical_path "$DEMO_ACCOUNT_INBOX_ROOT")"
    --demo-capture-root "$(canonical_path "$DEMO_ACCOUNT_CAPTURE_ROOT")"
    --paper-account-root "$(canonical_path "$PAPER_ACCOUNT_ROOT")"
    --paper-inbox-root "$(canonical_path "$PAPER_ACCOUNT_INBOX_ROOT")"
    --paper-capture-root "$(canonical_path "$PAPER_ACCOUNT_CAPTURE_ROOT")"
  )
  (( INCLUDE_REPORTS )) && receipt_create_args+=(--include-reports)
  (( INCLUDE_CACHES )) && receipt_create_args+=(--include-caches)
  for sleeve in "${SELECTED_SLEEVES[@]}"; do
    receipt_create_args+=(--sleeve "$sleeve")
  done
  for unit in "${STOP_UNITS[@]}"; do
    receipt_create_args+=(--managed-unit "$unit" --inactive-after-unit "$unit")
  done
  for unit in "${ACTIVE_BEFORE[@]:-}"; do
    [[ -z "$unit" ]] || receipt_create_args+=(--active-before-unit "$unit")
  done
  "$PYTHON" -m liquidity_migration.account_reset_receipt "${receipt_create_args[@]}" \
    || die "completed reset could not produce its structured success receipt"
fi

echo
echo "Ledger reset complete."
echo "  archive: $ARCHIVE_PATH"
echo "  archive sha256: $archive_sha"
echo "  archive digest: $SHA_PATH"
[[ -z "$RECEIPT_PATH" ]] || echo "  structured reset receipt: $RECEIPT_PATH"
echo "  archived/reset targets: ${#EXISTING_TARGETS[@]}"
echo "  strategy journals: preserved; compatibility projections rebuilt by replay"
echo "  account journals/inboxes/captures: archived; fresh empty epoch created"
echo "  boundary truth: demo venue-flat verified; prior paper epoch archived, not venue-verified"
if (( LEAVE_STOPPED )); then
  echo "  service state: all managed units stopped and verified"
else
  echo "  service state: restored to the pre-reset active set and verified"
fi
echo "  preserved: configs, locks, signal files, account-equity high-water state, and unselected caches/reports"
