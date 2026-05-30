#!/usr/bin/env bash
# Thin wrapper around `python -m liquidity_migration volume-events` that
# fills in the production-baseline flags so a research cell only specifies
# its overrides. Eliminates the 30+ flag boilerplate that's prone to typos.
#
# Usage:
#   bash scripts/volume_events_cell.sh \
#     --venue bybit \
#     --cell-id P2_imp_150 \
#     --phase sweep_2026-05-28 \
#     [--start 2025-01-01] [--end 2026-05-28] \
#     [--overrides 'KEY=VAL,KEY=VAL,...']
#
# The --overrides flag is a comma-separated list of CLI flag overrides where
# each KEY corresponds to a baseline CLI flag (without leading "--"). For
# multi-word flag names use the hyphenated form, e.g.
#   --overrides 'hold-days=2,universe-rank-max=200'
#
# Baseline = the current promoted profile (matches the configs/volume_alpha
# default + the in-flight sweep's 00_baseline cell). Any override silently
# replaces the corresponding baseline value; an override KEY not present in the
# baseline is appended as a new flag. Everything else is preserved.
#
# Reports land in:
#   <DATA_ROOT>/reports/<phase>/<cell-id>/
# unless overridden via --report-dir-suffix.
#
# Set DRY_RUN=1 to print the fully-resolved command to stdout and exit WITHOUT
# executing it (safe inspection of what would run).
#
# Exits non-zero on CLI parse error or volume-events failure. The full
# resolved flag list is printed to stderr before invocation for audit.
#
# Portability: this script is written for bash 3.2 (the macOS system bash) as
# well as bash 4+ (the Linux VPS). It deliberately avoids bash-4-only features
# (no `declare -A` associative arrays); the baseline flag table is held as a
# newline-delimited "KEY=VALUE" list and manipulated with helper functions, so
# behavior is identical on both. As a belt-and-suspenders convenience, if it is
# launched under an old bash and a newer one is installed via Homebrew, it
# re-execs under that — but the script does NOT require bash 4 to work.

# --- bash version guard (convenience only; the script works on bash 3.2) -----
# If running under bash < 4 and a newer bash exists, re-exec under it once. The
# VEC_REEXECED sentinel prevents an infinite loop if the newer bash is somehow
# also < 4.
if [ -z "${VEC_REEXECED:-}" ] && [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    for _candidate in /opt/homebrew/bin/bash /usr/local/bin/bash; do
        if [ -x "$_candidate" ]; then
            export VEC_REEXECED=1
            exec "$_candidate" "$0" "$@"
        fi
    done
fi

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
DEFAULT_START="2025-01-01"
DEFAULT_END="2026-05-28"
CONFIG="${CONFIG:-configs/volume_alpha.default.yaml}"

VENUE=""
CELL_ID=""
PHASE=""
START="$DEFAULT_START"
END="$DEFAULT_END"
OVERRIDES=""
ALLOW_PARTIAL_PIT=""   # full PIT by default (engine aborts on coverage gaps); --allow-partial-pit opts into a BIASED run
EXTRA_FLAGS=()

usage() {
    cat <<EOF
Usage: $0 --venue {bybit|binance} --cell-id ID --phase TAG [options]

Required:
  --venue {bybit|binance}      Picks the data root.
  --cell-id ID                 Cell identifier (used in report path).
  --phase TAG                  Phase identifier (used in report path, e.g.
                               sweep_2026-05-28 or phase2_2026-06-01).

Optional:
  --start DATE                 Inclusive start (default $DEFAULT_START).
  --end DATE                   Exclusive end (default $DEFAULT_END).
  --overrides 'K=V,K=V,...'    Comma-separated CLI overrides.
  --allow-partial-pit          Opt into a BIASED current-universe (survivorship)
                               run; EXPLORATORY only, never promotion evidence.
                               Default is full PIT (engine aborts on coverage gaps).
  --extra 'flag arg'           Append a raw flag/arg to the volume-events call.
                               Repeatable.
  --help                       Show this help.

Environment:
  DRY_RUN=1                    Print the resolved command and exit; do not run.

Baseline flag values applied unless overridden:
  --event-types liquidity_migration
  --thresholds 0.4
  --hold-days 3
  --sides reversal
  --stop-loss-pcts 0.12
  --take-profit-pcts 0.26
  --cost-multipliers 3
  --gross-exposure 1.0
  --entry-delay-hours 1
  --entry-policy promoted_quality_squeeze
  --max-active-symbols 3
  --cooldown-days 5
  --rank-exit-threshold 0.55
  --universe-rank-min 31
  --universe-rank-max 400
  --liquidity-migration-rank-improvement-min 150
  --liquidity-migration-rank-direction improvement
  --liquidity-migration-turnover-ratio-min 6.0
  --liquidity-migration-event-rank-fraction-max 0.90
  --liquidity-migration-day-return-min 0.0
  --liquidity-migration-residual-return-min 0.08
  --liquidity-migration-close-location-min 0.30
  --liquidity-migration-pit-age-days-min 90
  --liquidity-migration-crowding-filter union_pathology
  --stop-pressure-window-days 10
  --stop-pressure-stop-count 7
  --realized-loss-pressure-window-days 5
  --realized-loss-pressure-loss-count 6
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --venue) VENUE="$2"; shift 2 ;;
        --cell-id) CELL_ID="$2"; shift 2 ;;
        --phase) PHASE="$2"; shift 2 ;;
        --start) START="$2"; shift 2 ;;
        --end) END="$2"; shift 2 ;;
        --overrides) OVERRIDES="$2"; shift 2 ;;
        --allow-partial-pit) ALLOW_PARTIAL_PIT="--allow-partial-pit"; shift ;;
        --extra) EXTRA_FLAGS+=("$2"); shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$VENUE" || -z "$CELL_ID" || -z "$PHASE" ]]; then
    echo "Missing required arg(s)." >&2
    usage >&2
    exit 2
fi

case "$VENUE" in
    bybit)   DATA_ROOT="${BYBIT_FULL_PIT_ROOT:-$HOME/SHARED_DATA/bybit_full_pit}" ;;
    binance) DATA_ROOT="${BINANCE_FULL_PIT_ROOT:-$HOME/SHARED_DATA/binance_full_pit}" ;;
    *) echo "--venue must be bybit or binance, got: $VENUE" >&2; exit 2 ;;
esac

if [[ ! -d "$DATA_ROOT" ]]; then
    echo "Data root not found: $DATA_ROOT" >&2
    exit 2
fi

REPORT_DIR="${REPORT_DIR_OVERRIDE:-$DATA_ROOT/reports/$PHASE/$CELL_ID}"
if [[ "${DRY_RUN:-}" != "1" ]]; then
    mkdir -p "$REPORT_DIR"
fi

# Baseline flag table. Keys are flag names without leading "--". Held as a
# newline-delimited "KEY=VALUE" list (bash 3.2 has no associative arrays). The
# value of any single entry may itself contain "=" — only the first "=" on a
# line separates KEY from VALUE.
BASELINE_TABLE="event-types=liquidity_migration
thresholds=0.4
hold-days=3
sides=reversal
stop-loss-pcts=0.12
take-profit-pcts=0.26
cost-multipliers=3
gross-exposure=1.0
entry-delay-hours=1
entry-policy=promoted_quality_squeeze
max-active-symbols=3
cooldown-days=5
rank-exit-threshold=0.55
universe-rank-min=31
universe-rank-max=400
liquidity-migration-rank-improvement-min=150
liquidity-migration-rank-direction=improvement
liquidity-migration-turnover-ratio-min=6.0
liquidity-migration-event-rank-fraction-max=0.90
liquidity-migration-day-return-min=0.0
liquidity-migration-residual-return-min=0.08
liquidity-migration-close-location-min=0.30
liquidity-migration-pit-age-days-min=90
liquidity-migration-crowding-filter=union_pathology
stop-pressure-window-days=10
stop-pressure-stop-count=7
realized-loss-pressure-window-days=5
realized-loss-pressure-loss-count=6"

# set_baseline KEY VALUE
# Replace KEY's entry in BASELINE_TABLE, or append it if not present. Mirrors
# the bash-4 `BASELINE[$key]="$val"` semantics (in-place replace OR insert).
set_baseline() {
    local k="$1"
    local v="$2"
    local line out=""
    local found=0
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if [[ "${line%%=*}" == "$k" ]]; then
            out="${out}${k}=${v}"$'\n'
            found=1
        else
            out="${out}${line}"$'\n'
        fi
    done <<< "$BASELINE_TABLE"
    if [[ "$found" -eq 0 ]]; then
        out="${out}${k}=${v}"$'\n'
    fi
    # Strip the single trailing newline we always append.
    BASELINE_TABLE="${out%$'\n'}"
}

# Apply overrides
if [[ -n "$OVERRIDES" ]]; then
    IFS=',' read -ra PAIRS <<< "$OVERRIDES"
    for pair in "${PAIRS[@]}"; do
        if [[ "$pair" != *"="* ]]; then
            echo "override missing '=': $pair" >&2
            exit 2
        fi
        key="${pair%%=*}"
        val="${pair#*=}"
        set_baseline "$key" "$val"
    done
fi

# Build the flag list. Sort by KEY for reproducibility (identical to the prior
# `sort` over associative-array keys). Sorting on the KEY field only — not the
# whole KEY=VALUE line — preserves the exact prior ordering even when values
# differ between keys that share a prefix. LC_ALL=C pins a byte-wise collation
# so the emitted flag order is identical on macOS (BSD sort) and the Linux VPS
# (GNU sort) regardless of each host's locale.
FLAGS=()
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    FLAGS+=("--$key" "$val")
done < <(printf '%s\n' "$BASELINE_TABLE" | LC_ALL=C sort -t= -k1,1)

# Print resolved invocation to stderr (audit log)
echo "[volume_events_cell] venue=$VENUE cell=$CELL_ID phase=$PHASE" >&2
echo "[volume_events_cell] window: $START → $END" >&2
echo "[volume_events_cell] report_dir: $REPORT_DIR" >&2
if [[ -n "$OVERRIDES" ]]; then
    echo "[volume_events_cell] overrides: $OVERRIDES" >&2
fi

# Compose the final command
CMD=(
    "$PYTHON_BIN" -m liquidity_migration
    --data-root "$DATA_ROOT"
    --config "$CONFIG"
    volume-events
    --start "$START"
    --end "$END"
    --report-dir "$REPORT_DIR"
)
[[ -n "$ALLOW_PARTIAL_PIT" ]] && CMD+=("$ALLOW_PARTIAL_PIT")
# Use the `${arr[@]+...}` guard so an EMPTY array does not trip `set -u` on
# bash 3.2 (where expanding an empty array under `set -u` is an "unbound
# variable" error; bash 4.4+ is lenient). FLAGS is always populated, but
# EXTRA_FLAGS is empty unless --extra was passed.
CMD+=(${FLAGS[@]+"${FLAGS[@]}"} ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"})

if [[ "${DRY_RUN:-}" == "1" ]]; then
    # Print the exact command that would be executed, one safely-quoted token
    # per the rules of the shell, to stdout. Does not execute.
    printf '%q ' "${CMD[@]}"
    printf '\n'
    exit 0
fi

exec "${CMD[@]}"
