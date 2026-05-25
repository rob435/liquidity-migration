#!/usr/bin/env bash
# Verify the new per-venue full-PIT roots before allowing old-root deletion.
# Exits non-zero on any gate failure; gates print "PASS" on success.
#
# See: docs/full_pit_rebuild_and_punchlist.md section A.6
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
NEW_BYBIT="${BYBIT_FULL_ROOT:-$HOME/SHARED_DATA/bybit_full_pit}"
NEW_BINANCE="${BINANCE_FULL_ROOT:-$HOME/SHARED_DATA/binance_full_pit}"
OLD_BYBIT="$HOME/SHARED_DATA/bybit_fullpit_1h"
OLD_BYBIT_OOS="$HOME/SHARED_DATA/bybit_oos_pre2023"
OLD_BINANCE="$HOME/SHARED_DATA/binance_oos_pit"

cd "$(dirname "$0")/.."

echo "=============================================================="
echo "Full PIT rebuild — verification gates"
echo "=============================================================="

echo
echo "[gate 1/7] data-layer-audit — new Bybit root"
"$PYTHON_BIN" -m liquidity_migration --data-root "$NEW_BYBIT" data-layer-audit

echo
echo "[gate 2/7] data-layer-audit — new Binance root"
"$PYTHON_BIN" -m liquidity_migration --data-root "$NEW_BINANCE" data-layer-audit

echo
echo "[gate 3/7] coverage parity — new Bybit vs old canonical (overlap 2023-05-03 → 2026-05-17)"
NEW="$NEW_BYBIT" OLD="$OLD_BYBIT" WIN_START="2023-05-03" WIN_END="2026-05-17" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
new = pathlib.Path(os.environ["NEW"]).expanduser() / "klines_1h"
old = pathlib.Path(os.environ["OLD"]).expanduser() / "klines_1h"
if not old.exists():
    print(f"  old root absent ({old}) — skipping parity check")
    sys.exit(0)
ws, we = os.environ["WIN_START"], os.environ["WIN_END"]
nd = pl.read_parquet(str(new / "**" / "*.parquet")).filter((pl.col("date") >= ws) & (pl.col("date") <= we))
od = pl.read_parquet(str(old / "**" / "*.parquet"))
n_rows, o_rows = nd.height, od.height
n_syms = nd["symbol"].n_unique()
o_syms = od["symbol"].n_unique()
print(f"  old: {o_rows:,} rows / {o_syms} symbols")
print(f"  new: {n_rows:,} rows / {n_syms} symbols")
assert n_rows >= o_rows * 0.98, f"new root has <98% of old row count: {n_rows} < {o_rows*0.98:.0f}"
assert n_syms >= o_syms * 0.98, f"new root has <98% of old symbol count: {n_syms} < {o_syms*0.98:.0f}"
print("  PASS")
PY

echo
echo "[gate 4/7] coverage parity — new Bybit vs old pre-2023 OOS (overlap 2021-01-01 → 2023-05-02)"
NEW="$NEW_BYBIT" OLD="$OLD_BYBIT_OOS" WIN_START="2021-01-01" WIN_END="2023-05-02" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
new = pathlib.Path(os.environ["NEW"]).expanduser() / "klines_1h"
old = pathlib.Path(os.environ["OLD"]).expanduser() / "klines_1h"
if not old.exists():
    print(f"  old root absent ({old}) — skipping parity check")
    sys.exit(0)
ws, we = os.environ["WIN_START"], os.environ["WIN_END"]
nd = pl.read_parquet(str(new / "**" / "*.parquet")).filter((pl.col("date") >= ws) & (pl.col("date") <= we))
od = pl.read_parquet(str(old / "**" / "*.parquet"))
n_rows, o_rows = nd.height, od.height
n_syms = nd["symbol"].n_unique()
o_syms = od["symbol"].n_unique()
print(f"  old: {o_rows:,} rows / {o_syms} symbols")
print(f"  new: {n_rows:,} rows / {n_syms} symbols")
assert n_rows >= o_rows * 0.98, f"new root has <98% of old row count: {n_rows} < {o_rows*0.98:.0f}"
assert n_syms >= o_syms * 0.98, f"new root has <98% of old symbol count: {n_syms} < {o_syms*0.98:.0f}"
print("  PASS")
PY

echo
echo "[gate 5/7] coverage parity — new Binance vs old Binance OOS (overlap 2020-01-01 → 2023-04-30)"
NEW="$NEW_BINANCE" OLD="$OLD_BINANCE" WIN_START="2020-01-01" WIN_END="2023-04-30" "$PYTHON_BIN" - <<'PY'
import os, pathlib, sys
import polars as pl
new = pathlib.Path(os.environ["NEW"]).expanduser() / "klines_1h"
old = pathlib.Path(os.environ["OLD"]).expanduser() / "klines_1h"
if not old.exists():
    print(f"  old root absent ({old}) — skipping parity check")
    sys.exit(0)
ws, we = os.environ["WIN_START"], os.environ["WIN_END"]
nd = pl.read_parquet(str(new / "**" / "*.parquet")).filter((pl.col("date") >= ws) & (pl.col("date") <= we))
od = pl.read_parquet(str(old / "**" / "*.parquet"))
n_rows, o_rows = nd.height, od.height
n_syms = nd["symbol"].n_unique()
o_syms = od["symbol"].n_unique()
print(f"  old: {o_rows:,} rows / {o_syms} symbols")
print(f"  new: {n_rows:,} rows / {n_syms} symbols")
assert n_rows >= o_rows * 0.98, f"new root has <98% of old row count: {n_rows} < {o_rows*0.98:.0f}"
assert n_syms >= o_syms * 0.98, f"new root has <98% of old symbol count: {n_syms} < {o_syms*0.98:.0f}"
print("  PASS")
PY

echo
echo "[gate 6/7] smoke FC sweep — v11a baseline 0.15 on new Bybit root"
"$PYTHON_BIN" scripts/long_native_sweep_fc_min_day.py \
  --data-root "$NEW_BYBIT" --values 0.15

echo
echo "[gate 7/7] tests + lint"
"$PYTHON_BIN" -m pytest -q
.venv/bin/ruff check liquidity_migration tests || ruff check liquidity_migration tests

echo
echo "=============================================================="
echo "All gates PASSED. Safe to delete old roots."
echo "=============================================================="
