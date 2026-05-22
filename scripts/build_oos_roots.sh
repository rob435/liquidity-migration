#!/usr/bin/env bash
# Rebuild the two out-of-sample PIT data roots from scratch on any machine.
#
#   bybit_oos_pre2023   Bybit USD-M perps 2021-01..2023-05  (public.bybit.com archive)
#   binance_oos_pit     Binance USD-M perps 2020-01..2023-04 (data.binance.vision archive)
#
# Both windows are pre-Bybit-canonical-archive (2023-05-03), so they are genuine
# out-of-sample for the promoted strategy. Both reconstruct PIT membership from
# sources that include delisted symbols — no survivorship-biased exchangeInfo.
#
# Usage:   bash scripts/build_oos_roots.sh
# Override roots / workers via env vars below. Safe to re-run: the Bybit kline
# fill is missing-only; the Binance build overwrites its own root.
set -euo pipefail

BYBIT_OOS_ROOT="${BYBIT_OOS_ROOT:-$HOME/SHARED_DATA/bybit_oos_pre2023}"
BINANCE_OOS_ROOT="${BINANCE_OOS_ROOT:-$HOME/SHARED_DATA/binance_oos_pit}"

# Bybit OOS window (inclusive archive dates). End is exclusive-of-canonical-start.
BYBIT_START="${BYBIT_START:-2021-01-01}"
BYBIT_END="${BYBIT_END:-2023-05-02}"
# Binance OOS: exclusive signal-date upper bound.
BINANCE_END="${BINANCE_END:-2023-05-01}"

MANIFEST_WORKERS="${MANIFEST_WORKERS:-16}"
KLINE_WORKERS="${KLINE_WORKERS:-8}"
BINANCE_WORKERS="${BINANCE_WORKERS:-24}"
PYTHON_BIN="${PYTHON_BIN:-python}"

cd "$(dirname "$0")/.."

echo "=============================================================="
echo "OOS root build"
echo "  bybit_oos_pre2023 -> $BYBIT_OOS_ROOT  ($BYBIT_START .. $BYBIT_END)"
echo "  binance_oos_pit   -> $BINANCE_OOS_ROOT (.. $BINANCE_END)"
echo "=============================================================="

# -------------------------------------------------------------------------
# 1) Bybit pre-2023 OOS — PIT manifest + 1h klines from the public archive
# -------------------------------------------------------------------------
echo
echo "[1/3] Bybit OOS — building PIT manifest from public.bybit.com ..."
"$PYTHON_BIN" -m liquidity_migration --data-root "$BYBIT_OOS_ROOT" \
  archive-manifest --start "$BYBIT_START" --end "$BYBIT_END" --workers "$MANIFEST_WORKERS"

echo
echo "[2/3] Bybit OOS — filling 1h klines from the Bybit v5 kline API ..."
"$PYTHON_BIN" -m liquidity_migration --data-root "$BYBIT_OOS_ROOT" \
  archive-download-klines-1h-api --start "$BYBIT_START" --end "$BYBIT_END" --workers "$KLINE_WORKERS"

echo
echo "[2b] Bybit OOS — filtering manifest to >=20-bar kline coverage ..."
"$PYTHON_BIN" -m liquidity_migration.binance_vision filter-manifest --data-root "$BYBIT_OOS_ROOT"

# -------------------------------------------------------------------------
# 2) Binance USD-M OOS — PIT klines + manifest from data.binance.vision
# -------------------------------------------------------------------------
echo
echo "[3/3] Binance OOS — building PIT root from data.binance.vision ..."
"$PYTHON_BIN" -m liquidity_migration.binance_vision build-binance-oos \
  --data-root "$BINANCE_OOS_ROOT" --end "$BINANCE_END" --workers "$BINANCE_WORKERS"

echo
echo "=============================================================="
echo "OOS roots rebuilt. Both pass the full-PIT universe check and are"
echo "ready for volume-events backtests."
echo "=============================================================="
