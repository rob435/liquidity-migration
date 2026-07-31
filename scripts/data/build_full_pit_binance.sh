#!/usr/bin/env bash
# Build the unified Binance full-PIT data root.
# Window: BINANCE_START (default 2019-09-01) to end-exclusive BINANCE_END.
#
# Stages:
#   [1/2] binance_vision build-binance-oos  — atomic monthly + daily-tail klines/PIT pair
#   [2/2] download-binance-proxy            — funding, OI, mark/index/premium, taker_flow
#
# Perps-only by construction:
#   * the `binance_vision` stage reads only `data/futures/um/...` from
#     data.binance.vision (USD-Margined futures = USDT-quoted perpetuals).
#   * `download-binance-proxy` resolves to `binance_usdm_*` dataset names,
#     which target Binance's USD-M REST endpoints exclusively.
# A post-manifest sanity check rejects any non-USDT symbol that slips through.
#
# See: docs/data.md
#
# Usage:  bash scripts/data/build_full_pit_binance.sh
# Rerunnable: the monthly pair rebuilds in sibling staging; later stages reuse
# valid existing partitions where their owners support it.
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: bash scripts/data/build_full_pit_binance.sh' \
    '' \
    'Configuration is environment-only; positional arguments are refused.' \
    'Key variables: BINANCE_FULL_ROOT, BINANCE_START, BINANCE_END,' \
    'BINANCE_WORKERS, BINANCE_JOB_BATCH_SIZE, BINANCE_MAX_FAILURE_RATIO.'
}

if [ "$#" -ne 0 ]; then
  if [ "$#" -eq 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
    usage
    exit 0
  fi
  echo "FATAL: build_full_pit_binance.sh accepts no positional arguments" >&2
  usage >&2
  exit 2
fi

ROOT="${BINANCE_FULL_ROOT:-$HOME/SHARED_DATA/binance_full_pit}"
START="${BINANCE_START:-2019-09-01}"
END="${BINANCE_END:-$(date -u +%Y-%m-%d)}"
VISION_WORKERS="${BINANCE_WORKERS:-24}"
JOB_BATCH_SIZE="${BINANCE_JOB_BATCH_SIZE:-48}"
MAX_FAILURE_RATIO="${BINANCE_MAX_FAILURE_RATIO:-0}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

cd "$(dirname "$0")/../.."
mkdir -p "$ROOT"

# Monthly ZIPs do not cover an in-progress month. Stage the month containing
# END-1 day from daily archives in the same unpublished generation as monthly
# history, then derive membership from their combined >=20-bar coverage.
DAILY_START="${BINANCE_DAILY_START:-$("$PYTHON_BIN" - "$END" <<'PY'
import datetime as dt
import sys

end = dt.date.fromisoformat(sys.argv[1])
print((end - dt.timedelta(days=1)).replace(day=1).isoformat())
PY
)}"

echo "=============================================================="
echo "Binance full PIT build  (USD-M perpetuals only)"
echo "  root:    $ROOT"
echo "  window:  $START → $END (exclusive)"
echo "  daily:   $DAILY_START → $END (exclusive)"
echo "  workers: vision=$VISION_WORKERS batch=$JOB_BATCH_SIZE ancillary=$ANCILLARY_WORKERS"
echo "  max monthly failure ratio: $MAX_FAILURE_RATIO"
echo "=============================================================="

echo
echo "[1/2] Binance — atomic full PIT root from USD-M monthly + current daily archives"
"$PYTHON_BIN" -m liquidity_migration.data.binance_vision \
  build-binance-oos --data-root "$ROOT" --end "$END" --workers "$VISION_WORKERS" \
  --job-batch-size "$JOB_BATCH_SIZE" --max-failure-ratio "$MAX_FAILURE_RATIO" \
  --daily-start "$DAILY_START"

# Derive symbol list from the manifest.
# Perps-only guard: any symbol not USDT-quoted fails the build loudly.
SYMBOLS=$(ROOT="$ROOT" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
from liquidity_migration.data.binance_vision import validate_usdm_usdt_symbols

root = pathlib.Path(os.environ["ROOT"]).expanduser()
df = pl.read_parquet(str(root / "archive_trade_manifest" / "**" / "*.parquet"))
try:
    syms = validate_usdm_usdt_symbols(
        df["symbol"].unique().to_list(),
        source="canonical Binance manifest",
        ignore_non_usdt_entries=False,
    )
except RuntimeError as exc:
    print(f"FATAL: {exc}", file=sys.stderr)
    sys.exit(2)
print(",".join(syms))
PY
)
if [ -z "$SYMBOLS" ]; then
  echo "FATAL: Binance manifest produced no symbols" >&2
  exit 2
fi
N_SYMBOLS=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l)

echo
echo "[2/2] Binance — ancillary datasets for $N_SYMBOLS symbols"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  download-binance-proxy \
    --symbols "$SYMBOLS" \
    --start "$START" --end "$END" \
    --datasets funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h,taker_flow_1h \
    --workers "$ANCILLARY_WORKERS"

echo
echo "=============================================================="
echo "Binance full PIT root ready at: $ROOT"
echo "=============================================================="
