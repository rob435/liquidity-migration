#!/usr/bin/env bash
set -euo pipefail

REMOTE="${REMOTE:-https://github.com/rob435/MODEL05042026.git}"
RUN_NAME="${RUN_NAME:-fullpit-1h-all-usdt-20230503-20260503}"
MANIFEST_NAME="${MANIFEST_NAME:-pit-all-usdt-20230503-20260503}"
START_DATE="${START_DATE:-2023-05-03}"
END_DATE="${END_DATE:-2026-05-03}"
MANIFEST_WORKERS="${MANIFEST_WORKERS:-32}"
DOWNLOAD_WORKERS="${DOWNLOAD_WORKERS:-64}"
MIN_EXISTING_BARS="${MIN_EXISTING_BARS:-20}"
RUN_TESTS="${RUN_TESTS:-1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

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
if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"

if [ "$RUN_TESTS" != "0" ]; then
  section "Smoke tests"
  python -m pytest \
    tests/test_aggression_carry_cli.py::test_cli_parses_volume_events \
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

section "Download full PIT 1h klines"
AGC_ARCHIVE_DOWNLOAD_BACKEND="${AGC_ARCHIVE_DOWNLOAD_BACKEND:-curl}" \
AGC_ARCHIVE_DOWNLOAD_RETRIES="${AGC_ARCHIVE_DOWNLOAD_RETRIES:-8}" \
python -m aggression_carry \
  --data-root "$DATA_ROOT" \
  --config "$CONFIG_PATH" \
  archive-download-klines-1h \
  --name "$RUN_NAME" \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --workers "$DOWNLOAD_WORKERS" \
  --min-existing-bars "$MIN_EXISTING_BARS" \
  --discard-archives-after-success

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
report_path = root / "reports" / f"archive_klines_1h_{run_name}.csv"

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

section "Run best full PIT volume event backtest"
REPORT_DIR="$DATA_ROOT/reports/volume_event_research_fullpit_pvb_q20_cont_h5_halfgross_$(date -u +%Y%m%dT%H%M%SZ)"
python -m aggression_carry \
  --data-root "$DATA_ROOT" \
  --config "$CONFIG_PATH" \
  volume-events \
  --event-types persistent_volume_breakout \
  --thresholds 0.2 \
  --hold-days 5 \
  --sides continuation \
  --stop-loss-pcts 0 \
  --cost-multipliers 1,3 \
  --gross-exposure 0.5 \
  --max-active-symbols 6 \
  --cooldown-days 7 \
  --report-dir "$REPORT_DIR"

section "Done"
echo "Log: $LOG_FILE"
echo "Data root: $DATA_ROOT"
echo "Report dir: $REPORT_DIR"
find "$REPORT_DIR" -maxdepth 1 -type f -print | sort
