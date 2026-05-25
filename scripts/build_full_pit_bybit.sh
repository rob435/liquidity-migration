#!/usr/bin/env bash
# Build the unified Bybit full-PIT data root.
#
# Replaces the two-root patchwork (bybit_fullpit_1h + bybit_oos_pre2023)
# with a single root spanning from BYBIT_START (default 2021-01-01) to today.
#
# Stages:
#   [1/5] archive-manifest               — PIT (symbol, date) membership
#   [2/5] archive-download-klines-1h-api — 1h klines via Bybit v5 (manifest-gated)
#   [3/5] filter-manifest                — drop rows with <20h kline coverage
#   [4/5] download-data ancillaries      — funding, OI, mark/index/premium
#   [5/5] download-data raw trades       — for signed-flow construction (optional)
#
# Perps-only by construction:
#   * `archive-manifest` scans https://public.bybit.com/trading/ which only
#     exposes Bybit linear/inverse perpetuals; the USDT quote-suffix filter
#     restricts the result to USDT-quoted linear perps.
#   * `archive-download-klines-1h-api` is invoked with `--category linear`.
#   * `download-data` consumes the manifest-derived symbol list directly,
#     so no spot symbol can leak in.
# A post-manifest sanity check rejects any symbol that does not end with USDT
# (catches accidental categorical drift if upstream URL or filters change).
#
# See: docs/full_pit_rebuild_and_punchlist.md section A.3
#
# Usage:  bash scripts/build_full_pit_bybit.sh
# Resumable: each stage skips work already done.
set -euo pipefail

ROOT="${BYBIT_FULL_ROOT:-$HOME/SHARED_DATA/bybit_full_pit}"
START="${BYBIT_START:-2021-01-01}"
END="${BYBIT_END:-$(date -u +%Y-%m-%d)}"
CATEGORY="${BYBIT_CATEGORY:-linear}"   # perpetuals only; do not change
MANIFEST_WORKERS="${MANIFEST_WORKERS:-16}"
KLINE_WORKERS="${KLINE_WORKERS:-8}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"
INCLUDE_SIGNED_FLOW="${INCLUDE_SIGNED_FLOW:-1}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

if [ "$CATEGORY" != "linear" ]; then
  echo "FATAL: BYBIT_CATEGORY must be 'linear' (USDT perpetuals). Got: $CATEGORY" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
mkdir -p "$ROOT"

echo "=============================================================="
echo "Bybit full PIT build  (perpetuals-only, category=$CATEGORY)"
echo "  root:        $ROOT"
echo "  window:      $START → $END (exclusive)"
echo "  workers:     manifest=$MANIFEST_WORKERS kline=$KLINE_WORKERS ancillary=$ANCILLARY_WORKERS"
echo "  signed_flow: $INCLUDE_SIGNED_FLOW"
echo "=============================================================="

echo
echo "[1/5] Bybit — PIT manifest from public.bybit.com archive (USDT perps only)"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  archive-manifest --start "$START" --end "$END" --workers "$MANIFEST_WORKERS"

echo
echo "[2/5] Bybit — 1h klines via v5 kline API (category=$CATEGORY, manifest-gated)"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  archive-download-klines-1h-api \
    --category "$CATEGORY" \
    --start "$START" --end "$END" --workers "$KLINE_WORKERS"

echo
echo "[3/5] Bybit — filter manifest to ≥20-bar coverage"
"$PYTHON_BIN" -m liquidity_migration.binance_vision \
  filter-manifest --data-root "$ROOT"

# Derive the symbol list from the filtered manifest. Required by download-data.
# Perps-only guard: any symbol not USDT-quoted fails the build loudly rather
# than silently slipping spot or inverse symbols into the ancillary datasets.
SYMBOLS=$(ROOT="$ROOT" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
root = pathlib.Path(os.environ["ROOT"]).expanduser()
df = pl.read_parquet(str(root / "archive_trade_manifest" / "**" / "*.parquet"))
syms = sorted(df["symbol"].unique().to_list())
bad = [s for s in syms if not s.endswith("USDT")]
if bad:
    print(f"FATAL: non-USDT symbols in Bybit manifest: {bad[:5]}...", file=sys.stderr)
    sys.exit(2)
print(",".join(syms))
PY
)
N_SYMBOLS=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l)

echo
echo "[4/5] Bybit — funding + open_interest + mark/index/premium for $N_SYMBOLS symbols"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  download-data \
    --symbols "$SYMBOLS" \
    --start "$START" --end "$END" \
    --datasets funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h \
    --workers "$ANCILLARY_WORKERS"

if [ "$INCLUDE_SIGNED_FLOW" = "1" ]; then
  echo
  echo "[5/5] Bybit — archive_trades → signed_flow_{1m,1h} (raw trades discarded)"
  # download-data with archive_trades pulls the daily csv.gz, aggregates
  # signed_flow_1m + signed_flow_1h via ingestion.aggregate_signed_flow_*, and
  # --skip-raw-public-trades prevents persisting the raw-trades parquet
  # (which would inflate disk ~10x for the same downstream feature surface).
  "$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
    download-data \
      --symbols "$SYMBOLS" \
      --start "$START" --end "$END" \
      --datasets archive_trades \
      --archive-url-template "https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz" \
      --skip-raw-public-trades \
      --workers "$KLINE_WORKERS"
else
  echo
  echo "[5/5] signed_flow build skipped (INCLUDE_SIGNED_FLOW=0)"
fi

echo
echo "=============================================================="
echo "Bybit full PIT root ready at: $ROOT"
echo "=============================================================="
