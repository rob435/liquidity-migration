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
# replaces the corresponding baseline value; everything else is preserved.
#
# Reports land in:
#   <DATA_ROOT>/reports/<phase>/<cell-id>/
# unless overridden via --report-dir-suffix.
#
# Exits non-zero on CLI parse error or volume-events failure. The full
# resolved flag list is printed to stderr before invocation for audit.
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
mkdir -p "$REPORT_DIR"

# Baseline flag table (assoc array). Keys are flag names without leading "--".
declare -A BASELINE=(
    [event-types]="liquidity_migration"
    [thresholds]="0.4"
    [hold-days]="3"
    [sides]="reversal"
    [stop-loss-pcts]="0.12"
    [take-profit-pcts]="0.26"
    [cost-multipliers]="3"
    [gross-exposure]="1.0"
    [entry-delay-hours]="1"
    [entry-policy]="promoted_quality_squeeze"
    [max-active-symbols]="3"
    [cooldown-days]="5"
    [rank-exit-threshold]="0.55"
    [universe-rank-min]="31"
    [universe-rank-max]="400"
    [liquidity-migration-rank-improvement-min]="150"
    [liquidity-migration-rank-direction]="improvement"
    [liquidity-migration-turnover-ratio-min]="6.0"
    [liquidity-migration-event-rank-fraction-max]="0.90"
    [liquidity-migration-day-return-min]="0.0"
    [liquidity-migration-residual-return-min]="0.08"
    [liquidity-migration-close-location-min]="0.30"
    [liquidity-migration-pit-age-days-min]="90"
    [liquidity-migration-crowding-filter]="union_pathology"
    [stop-pressure-window-days]="10"
    [stop-pressure-stop-count]="7"
    [realized-loss-pressure-window-days]="5"
    [realized-loss-pressure-loss-count]="6"
)

# Apply overrides
if [[ -n "$OVERRIDES" ]]; then
    IFS=',' read -ra PAIRS <<< "$OVERRIDES"
    for pair in "${PAIRS[@]}"; do
        if [[ ! "$pair" == *"="* ]]; then
            echo "override missing '=': $pair" >&2
            exit 2
        fi
        key="${pair%%=*}"
        val="${pair#*=}"
        BASELINE[$key]="$val"
    done
fi

# Build the flag list. Sort keys for reproducibility.
FLAGS=()
for key in $(echo "${!BASELINE[@]}" | tr ' ' '\n' | sort); do
    FLAGS+=("--$key" "${BASELINE[$key]}")
done

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
CMD+=("${FLAGS[@]}" "${EXTRA_FLAGS[@]}")

exec "${CMD[@]}"
