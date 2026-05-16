#!/usr/bin/env bash
set -euo pipefail

REMOTE="${REMOTE:-https://github.com/rob435/MODEL05042026.git}"
RUN_NAME="${RUN_NAME:-fullpit-1h-all-usdt-20230503-20260503}"
MANIFEST_NAME="${MANIFEST_NAME:-pit-all-usdt-20230503-20260503}"
START_DATE="${START_DATE:-2023-05-03}"
END_DATE="${END_DATE:-2026-05-03}"
MANIFEST_WORKERS="${MANIFEST_WORKERS:-32}"
DOWNLOAD_WORKERS="${DOWNLOAD_WORKERS:-16}"
MIN_EXISTING_BARS="${MIN_EXISTING_BARS:-1}"
RUN_TESTS="${RUN_TESTS:-1}"
PYTHON_BIN="${PYTHON_BIN:-}"
RUN_CHAMPION_BACKTEST="${RUN_CHAMPION_BACKTEST:-1}"
CHAMPION_GROSS_EXPOSURE="${CHAMPION_GROSS_EXPOSURE:-1.25}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel >/dev/null 2>&1; then
  DEFAULT_REPO="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
else
  DEFAULT_REPO="$HOME/MODEL050426"
fi

REPO="${REPO:-${REPO_DIR:-$DEFAULT_REPO}}"
DATA_ROOT="${DATA_ROOT:-$HOME/agc-bybit-fullpit-1h-20230503-20260503}"
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
LOG_DIR="$DATA_ROOT/logs"
export DATA_ROOT RUN_NAME
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/fullpit_volume_overnight_$(date -u +%Y%m%dT%H%M%SZ).log"

exec > >(tee -a "$LOG_FILE") 2>&1

on_error() {
  local exit_code="$?"
  echo
  echo "FAILED at line $1 with exit code $exit_code"
  echo "Log: $LOG_FILE"
  exit "$exit_code"
}
trap 'on_error "$LINENO"' ERR

section() {
  echo
  echo "== $1 =="
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

section "Sync repo"
if [ ! -d "$REPO/.git" ]; then
  git clone "$REMOTE" "$REPO"
fi

cd "$REPO"
git update-index -q --refresh || true
if ! git diff-index --quiet HEAD --; then
  echo "Tracked local changes exist in $REPO; refusing to overwrite them."
  git status --short
  exit 2
fi

git fetch origin main --prune
git switch main
git pull --ff-only origin main

echo "Repo: $REPO"
echo "Commit: $(git rev-parse --short HEAD)"
git log --oneline -3

section "Install runtime"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "No python3 or python executable found on PATH."
    exit 2
  fi
fi
echo "Python bootstrap: $PYTHON_BIN"

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi
if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
  source .venv/Scripts/activate
else
  echo "Could not find venv activation script under .venv/bin or .venv/Scripts"
  exit 2
fi
python -m pip install -U pip
python -m pip install -e ".[dev]"

if [ "$RUN_TESTS" != "0" ]; then
  section "Smoke tests"
  python -m pytest \
    tests/test_aggression_carry_cli.py::test_cli_parses_volume_events_research_overrides \
    tests/test_aggression_carry_archive.py::test_archive_hourly_kline_download_writes_1h_partitions \
    tests/test_aggression_carry_archive.py::test_archive_hourly_downloader_processes_each_symbol_in_date_order \
    tests/test_aggression_carry_volume_events.py
fi

section "Build full PIT manifest"
python -m aggression_carry \
  --data-root "$DATA_ROOT" \
  --config "$CONFIG_PATH" \
  archive-manifest \
  --name "$MANIFEST_NAME" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --workers "$MANIFEST_WORKERS"

section "Fill full PIT 1h klines from Bybit v5 API"
python -m aggression_carry \
  --data-root "$DATA_ROOT" \
  --config "$CONFIG_PATH" \
  archive-download-klines-1h-api \
  --name "$RUN_NAME" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --workers "$DOWNLOAD_WORKERS" \
  --min-existing-bars "$MIN_EXISTING_BARS" \
  --limit 1000 \
  --retries 8 \
  --timeout-seconds 30 \
  --request-sleep-seconds 0.02

section "Validate full PIT coverage"
python - <<'PY'
import csv
import os
import sys
from collections import Counter
from pathlib import Path

from pyarrow import parquet as pq

from aggression_carry.storage import dataset_path, read_dataset

root = Path(os.environ["DATA_ROOT"])
run_name = os.environ["RUN_NAME"]
report_path = root / "reports" / f"archive_klines_1h_api_{run_name}.csv"

if not report_path.exists():
    raise SystemExit(f"missing downloader report: {report_path}")

with report_path.open(newline="") as handle:
    rows = list(csv.DictReader(handle))

status = Counter(row["status"] for row in rows)
failures = [row for row in rows if row["status"] == "failed" or row.get("error")]
print({"download_report_rows": len(rows), "status": dict(status), "failures": len(failures)})
if failures:
    print("failure_sample", failures[:20])
    sys.exit(1)

manifest = read_dataset(root, "archive_trade_manifest").select(["symbol", "date"]).unique()
base = dataset_path(root, "klines_1h")
missing = []
thin = []
for row in manifest.to_dicts():
    part = base / f"date={row['date']}" / f"symbol={row['symbol']}" / "part.parquet"
    if not part.exists():
        missing.append(row)
        continue
    count = pq.ParquetFile(part).metadata.num_rows
    if count < 20:
        thin.append({**row, "rows": count})

print(
    {
        "manifest_rows": manifest.height,
        "missing_partitions": len(missing),
        "thin_partitions": len(thin),
    }
)
if missing:
    print("missing_sample", missing[:20])
    sys.exit(1)
PY

EVENT_REPORT_INDEX="$DATA_ROOT/reports/fullpit_volume_event_runs_$(date -u +%Y%m%dT%H%M%SZ).csv"
echo "run_type,max_active_symbols,cooldown_days,entry_delay_hours,rank_exit_threshold,universe_rank_min,universe_rank_max,liquidity_migration_rank_improvement_min,liquidity_migration_turnover_ratio_min,liquidity_migration_event_rank_fraction_max,liquidity_migration_event_rank_fraction_exclude_min,liquidity_migration_event_rank_fraction_exclude_max,liquidity_migration_day_return_min,liquidity_migration_residual_return_min,liquidity_migration_market_pct_up_max,stop_pressure_window_days,stop_pressure_stop_count,event_types,thresholds,hold_days,sides,stop_loss_pcts,take_profit_pcts,cost_multipliers,gross_exposure,report_dir" > "$EVENT_REPORT_INDEX"

if [ "$RUN_CHAMPION_BACKTEST" != "0" ]; then
  section "Run selected full PIT volume event backtest"
  CHAMPION_REPORT_DIR="$DATA_ROOT/reports/SELECTED_liqmig_res8_q30_h3_tp20_g125_$(date -u +%Y%m%dT%H%M%SZ)"
  python -m aggression_carry \
    --data-root "$DATA_ROOT" \
    --config "$CONFIG_PATH" \
    volume-events \
    --event-types liquidity_migration \
    --thresholds 0.3 \
    --hold-days 3 \
    --sides reversal \
    --stop-loss-pcts 0.12 \
    --take-profit-pcts 0.20 \
    --cost-multipliers 3 \
    --gross-exposure "$CHAMPION_GROSS_EXPOSURE" \
    --entry-delay-hours 1 \
    --max-active-symbols 6 \
    --cooldown-days 5 \
    --rank-exit-threshold 0.55 \
    --universe-rank-min 31 \
    --universe-rank-max 150 \
    --liquidity-migration-rank-improvement-min 150 \
    --liquidity-migration-turnover-ratio-min 6.0 \
    --liquidity-migration-event-rank-fraction-max 0.90 \
    --liquidity-migration-event-rank-fraction-exclude-min 0 \
    --liquidity-migration-event-rank-fraction-exclude-max 0 \
    --liquidity-migration-day-return-min 0.0 \
    --liquidity-migration-residual-return-min 0.08 \
    --liquidity-migration-market-pct-up-max 0.55 \
    --stop-pressure-window-days 14 \
    --stop-pressure-stop-count 12 \
    --report-dir "$CHAMPION_REPORT_DIR"
  echo "champion,6,5,1,0.55,31,150,150,6.0,0.90,0,0,0.0,0.08,0.55,14,12,liquidity_migration,0.3,3,reversal,0.12,0.20,3,$CHAMPION_GROSS_EXPOSURE,$CHAMPION_REPORT_DIR" >> "$EVENT_REPORT_INDEX"
fi

section "Done"
echo "Log: $LOG_FILE"
echo "Data root: $DATA_ROOT"
echo "Event report index: $EVENT_REPORT_INDEX"
cat "$EVENT_REPORT_INDEX"
