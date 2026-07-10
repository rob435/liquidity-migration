#!/usr/bin/env bash
# Build the unified Binance full-PIT data root.
#
# Replaces binance_oos_pit with a single root spanning Binance USDM
# perpetuals launch (≈2019-09) to today.
#
# Stages:
#   [1/3] binance_vision build-binance-oos  — monthly klines + PIT manifest
#   [2/3] binance_vision topup-daily-klines — current-month daily kline tail
#   [3/3] download-binance-proxy            — funding, OI, mark/index/premium, taker_flow
#
# Perps-only by construction:
#   * both `binance_vision` stages read only `data/futures/um/...` from
#     data.binance.vision (USD-Margined futures = USDT-quoted perpetuals).
#   * `download-binance-proxy` resolves to `binance_usdm_*` dataset names,
#     which target Binance's USD-M REST endpoints exclusively.
# A post-manifest sanity check rejects any non-USDT symbol that slips through.
#
# See: docs/data_roots.md
#
# Usage:  bash scripts/build_full_pit_binance.sh
# Resumable: each stage skips work already done.
set -euo pipefail

ROOT="${BINANCE_FULL_ROOT:-$HOME/SHARED_DATA/binance_full_pit}"
START="${BINANCE_START:-2019-09-01}"
END="${BINANCE_END:-$(date -u +%Y-%m-%d)}"
VISION_WORKERS="${BINANCE_WORKERS:-24}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

cd "$(dirname "$0")/.."
mkdir -p "$ROOT"

# Monthly ZIPs do not cover an in-progress month. Re-fetch the month containing
# END-1 day from daily archives after the clean monthly rewrite. The top-up is
# append/idempotent and rewrites membership from actual >=20-bar coverage.
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
echo "  workers: vision=$VISION_WORKERS ancillary=$ANCILLARY_WORKERS"
echo "=============================================================="

echo
echo "[1/3] Binance — full PIT root from data.binance.vision USD-M monthly archives"
"$PYTHON_BIN" -m liquidity_migration.binance_vision \
  build-binance-oos --data-root "$ROOT" --end "$END" --workers "$VISION_WORKERS"

echo
echo "[2/3] Binance — daily archive tail for the month not guaranteed by monthly ZIPs"
"$PYTHON_BIN" -m liquidity_migration.binance_vision \
  topup-daily-klines --data-root "$ROOT" \
  --start "$DAILY_START" --end "$END" --workers "$VISION_WORKERS"

# Derive symbol list from the manifest.
# Perps-only guard: any symbol not USDT-quoted fails the build loudly.
SYMBOLS=$(ROOT="$ROOT" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
root = pathlib.Path(os.environ["ROOT"]).expanduser()
df = pl.read_parquet(str(root / "archive_trade_manifest" / "**" / "*.parquet"))
syms = sorted(df["symbol"].unique().to_list())
bad = [s for s in syms if not s.endswith("USDT")]
if bad:
    print(f"FATAL: non-USDT symbols in Binance manifest: {bad[:5]}...", file=sys.stderr)
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
echo "[3/3] Binance — ancillary datasets for $N_SYMBOLS symbols"
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
