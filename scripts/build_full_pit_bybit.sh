#!/usr/bin/env bash
# Build the unified Bybit full-PIT data root.
# Window: BYBIT_START (default 2021-01-01) to end-exclusive BYBIT_END.
#
# Stages:
#   [1/4] archive-manifest               — PIT (symbol, date) membership
#   [2/4] archive-download-klines-1h-api — 1h klines via Bybit v5 (manifest-gated)
#   [3/4] validate-manifest              — fail on missing required klines without erasing membership
#   [4/4] download-data ancillaries      — funding, OI, mark/index/premium
#
# Perps-only by construction:
#   * `archive-manifest` scans https://public.bybit.com/trading/ which only
#     exposes Bybit linear/inverse perpetuals; the USDT quote-suffix filter
#     restricts the result to USDT-quoted linear perps.
#   * `archive-download-klines-1h-api` is invoked with `--category linear`.
#   * `download-data` consumes the manifest-derived symbol list directly.
# A post-manifest check rejects any symbol not ending in USDT, catching
# categorical drift if the upstream URL or filters change.
#
# See: docs/data.md
#
# Usage:  bash scripts/build_full_pit_bybit.sh
# Rerunnable: download stages reuse valid existing partitions where their
# owners support it.
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: bash scripts/build_full_pit_bybit.sh' \
    '' \
    'Configuration is environment-only; positional arguments are refused.' \
    'Key variables: BYBIT_FULL_ROOT, BYBIT_START, BYBIT_END, BYBIT_CATEGORY,' \
    'MANIFEST_WORKERS, KLINE_WORKERS, ANCILLARY_WORKERS.'
}

if [ "$#" -ne 0 ]; then
  if [ "$#" -eq 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
    usage
    exit 0
  fi
  echo "FATAL: build_full_pit_bybit.sh accepts no positional arguments" >&2
  usage >&2
  exit 2
fi

ROOT="${BYBIT_FULL_ROOT:-$HOME/SHARED_DATA/bybit_full_pit}"
START="${BYBIT_START:-2021-01-01}"
END="${BYBIT_END:-$(date -u +%Y-%m-%d)}"
CATEGORY="${BYBIT_CATEGORY:-linear}"   # perpetuals only; do not change
MANIFEST_WORKERS="${MANIFEST_WORKERS:-16}"
KLINE_WORKERS="${KLINE_WORKERS:-8}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

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
echo "=============================================================="

echo
echo "[1/4] Bybit — PIT manifest from public.bybit.com archive + v5 instruments-info (USDT perps only)"
# archive-manifest always merges two sources:
#   * public.bybit.com/trading scrape (deep history; the archive root)
#   * Bybit v5 instruments-info listing (currently-Trading perps)
# The v5 listing closes two archive gaps: symbols the scrape never picked up,
# and the ~24h current-day publishing lag. No flag controls this — archive-only
# mode silently drops tradeable symbols. See the ArchiveManifestConfig docstring.
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  archive-manifest --start "$START" --end "$END" --workers "$MANIFEST_WORKERS"

echo
echo "[2/4] Bybit — 1h klines via v5 kline API (category=$CATEGORY, manifest-gated)"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  archive-download-klines-1h-api \
    --category "$CATEGORY" \
    --start "$START" --end "$END" --workers "$KLINE_WORKERS"

echo
echo "[3/4] Bybit — validate independent manifest against ≥20-bar kline coverage"
"$PYTHON_BIN" -m liquidity_migration.binance_vision \
  validate-manifest --data-root "$ROOT"

# Symbol list for download-data, from the validated manifest. Perps-only guard:
# a non-USDT-quoted symbol fails the build rather than reaching the ancillaries.
SYMBOLS=$(ROOT="$ROOT" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
from liquidity_migration.archive_manifest import validate_bybit_manifest_provenance

root = pathlib.Path(os.environ["ROOT"]).expanduser()
df = pl.read_parquet(str(root / "archive_trade_manifest" / "**" / "*.parquet"))
try:
    validate_bybit_manifest_provenance(df)
except RuntimeError as exc:
    print(f"FATAL: {exc}", file=sys.stderr)
    sys.exit(2)
syms = sorted(df["symbol"].unique().to_list())
bad = [s for s in syms if not s.endswith("USDT")]
if bad:
    print(f"FATAL: non-USDT symbols in Bybit manifest: {bad[:5]}...", file=sys.stderr)
    sys.exit(2)
print(",".join(syms))
PY
)
if [ -z "$SYMBOLS" ]; then
  N_SYMBOLS=0
else
  N_SYMBOLS=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l)
fi

echo
echo "[4/4] Bybit — funding + open_interest + mark/index/premium for $N_SYMBOLS symbols"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  download-data \
    --symbols "$SYMBOLS" \
    --start "$START" --end "$END" \
    --datasets funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h \
    --workers "$ANCILLARY_WORKERS"

echo
echo "=============================================================="
echo "Bybit full PIT root ready at: $ROOT"
echo "=============================================================="
