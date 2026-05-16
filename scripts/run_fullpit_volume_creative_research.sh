#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/Users/jhbvdnsbkvnsd/Desktop/MODEL050426/data/agc-bybit-fullpit-1h-20230503-20260503}"
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
WAIT_FOR_FULL_PIT="${WAIT_FOR_FULL_PIT:-1}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-300}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
cd "$REPO_ROOT"

LOG_DIR="$DATA_ROOT/logs"
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_BASE="$DATA_ROOT/reports/fullpit_creative_volume_research_${RUN_TAG}"
RUN_INDEX="$REPORT_BASE/fullpit_creative_volume_runs.csv"
mkdir -p "$LOG_DIR" "$REPORT_BASE"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

full_pit_ready() {
  DATA_ROOT="$DATA_ROOT" "$PYTHON_BIN" - <<'PY'
import os
import sys
from pathlib import Path

from pyarrow import parquet as pq

from aggression_carry.storage import dataset_path, read_dataset

root = Path(os.environ["DATA_ROOT"])
manifest = read_dataset(root, "archive_trade_manifest").select(["symbol", "date"]).unique()
if manifest.is_empty():
    print({"ready": False, "reason": "missing_archive_trade_manifest"})
    sys.exit(1)
base = dataset_path(root, "klines_1h")
missing = 0
thin = 0
for row in manifest.to_dicts():
    part = base / f"date={row['date']}" / f"symbol={row['symbol']}" / "part.parquet"
    if not part.exists():
        missing += 1
        continue
    try:
        rows = pq.ParquetFile(part).metadata.num_rows
    except Exception:
        missing += 1
        continue
    if rows < 20:
        thin += 1
print({"ready": missing == 0 and thin == 0, "manifest_rows": manifest.height, "missing_partitions": missing, "thin_partitions": thin})
sys.exit(0 if missing == 0 and thin == 0 else 1)
PY
}

if [ "$WAIT_FOR_FULL_PIT" != "0" ]; then
  log "waiting for full PIT coverage before running research"
  started_at="$(date +%s)"
  until full_pit_ready; do
    if [ "$MAX_WAIT_SECONDS" -gt 0 ]; then
      elapsed="$(( $(date +%s) - started_at ))"
      if [ "$elapsed" -ge "$MAX_WAIT_SECONDS" ]; then
        log "full PIT wait timed out after ${elapsed}s"
        exit 1
      fi
    fi
    sleep "$CHECK_INTERVAL_SECONDS"
  done
else
  log "checking full PIT coverage once"
  full_pit_ready
fi

log "full PIT coverage passed"
echo "run_name,report_dir,universe_rank_min,universe_rank_max,entry_delay_hours,rank_exit_threshold,extra" > "$RUN_INDEX"

run_case() {
  local name="$1"
  shift
  local report_dir="$REPORT_BASE/$name"
  log "starting $name"
  "$PYTHON_BIN" -m aggression_carry \
    --data-root "$DATA_ROOT" \
    --config "$CONFIG_PATH" \
    volume-events \
    "$@" \
    --report-dir "$report_dir"
  log "finished $name"
  printf '%s,%s,%s,%s,%s,%s,%s\n' \
    "$name" \
    "$report_dir" \
    "${CASE_UNIVERSE_RANK_MIN:-}" \
    "${CASE_UNIVERSE_RANK_MAX:-}" \
    "${CASE_ENTRY_DELAY:-}" \
    "${CASE_RANK_EXIT_THRESHOLD:-}" \
    "${CASE_EXTRA:-}" >> "$RUN_INDEX"
}

for entry_delay in 1 6 12 24; do
  CASE_UNIVERSE_RANK_MIN=1
  CASE_UNIVERSE_RANK_MAX=150
  CASE_ENTRY_DELAY="$entry_delay"
  CASE_RANK_EXIT_THRESHOLD=0.5
  CASE_EXTRA="pvb_top150_delay"
  run_case "pit_top150_pvb_delay_${entry_delay}h" \
    --event-types persistent_volume_breakout \
    --thresholds 0.2 \
    --hold-days 5 \
    --sides continuation \
    --stop-loss-pcts 0 \
    --cost-multipliers 1,3 \
    --gross-exposure 0.5 \
    --entry-delay-hours "$entry_delay" \
    --max-active-symbols 6 \
    --cooldown-days 7 \
    --rank-exit-threshold 0.5 \
    --universe-rank-max 150
done

for rank_exit in 0.45 0.55 0.65; do
  CASE_UNIVERSE_RANK_MIN=1
  CASE_UNIVERSE_RANK_MAX=150
  CASE_ENTRY_DELAY=6
  CASE_RANK_EXIT_THRESHOLD="$rank_exit"
  CASE_EXTRA="top150_spike_breakout_shape"
  run_case "pit_top150_spike_breakout_rx${rank_exit//./p}" \
    --event-types fresh_volume_spike,persistent_volume_breakout \
    --thresholds 0.1,0.2,0.3 \
    --hold-days 1,3,5,7 \
    --sides continuation,reversal \
    --stop-loss-pcts 0,0.03,0.05,0.08 \
    --cost-multipliers 1,3 \
    --gross-exposure 0.5 \
    --entry-delay-hours 6 \
    --max-active-symbols 6 \
    --cooldown-days 3 \
    --rank-exit-threshold "$rank_exit" \
    --universe-rank-max 150
done

for min_day_return in 0.03 0.05 0.08; do
  CASE_UNIVERSE_RANK_MIN=1
  CASE_UNIVERSE_RANK_MAX=150
  CASE_ENTRY_DELAY=6
  CASE_RANK_EXIT_THRESHOLD=0.5
  CASE_EXTRA="top150_exhaustion_min_day_return_${min_day_return}"
  run_case "pit_top150_exhaustion_mdr${min_day_return//./p}" \
    --event-types volume_exhaustion \
    --thresholds 0.2,0.3 \
    --hold-days 1,3,5 \
    --sides continuation,reversal \
    --stop-loss-pcts 0,0.03,0.05,0.08,0.12 \
    --cost-multipliers 1,3 \
    --gross-exposure 0.5 \
    --entry-delay-hours 6 \
    --max-active-symbols 6 \
    --cooldown-days 7 \
    --rank-exit-threshold 0.5 \
    --universe-rank-max 150 \
    --exhaustion-min-day-return "$min_day_return"
done

for tail_min in 81 151; do
  if [ "$tail_min" -eq 81 ]; then
    tail_max=160
  else
    tail_max=300
  fi
  for improvement in 20 40; do
    CASE_UNIVERSE_RANK_MIN="$tail_min"
    CASE_UNIVERSE_RANK_MAX="$tail_max"
    CASE_ENTRY_DELAY=6
    CASE_RANK_EXIT_THRESHOLD=0.5
    CASE_EXTRA="tail_jump_improvement_${improvement}"
    run_case "pit_tail_jump_r${tail_min}_${tail_max}_i${improvement}" \
      --event-types tail_liquidity_jump \
      --thresholds 0.2,0.3 \
      --hold-days 3,5,7 \
      --sides continuation,reversal \
      --stop-loss-pcts 0,0.05,0.12 \
      --cost-multipliers 1,3 \
      --gross-exposure 0.5 \
      --entry-delay-hours 6 \
      --max-active-symbols 6 \
      --cooldown-days 7 \
      --rank-exit-threshold 0.5 \
      --universe-rank-min "$tail_min" \
      --universe-rank-max "$tail_max" \
      --tail-rank-min "$tail_min" \
      --tail-rank-max "$tail_max" \
      --tail-rank-improvement-min "$improvement"
  done
done

CASE_UNIVERSE_RANK_MIN=1
CASE_UNIVERSE_RANK_MAX=0
CASE_ENTRY_DELAY=6
CASE_RANK_EXIT_THRESHOLD=0.5
CASE_EXTRA="all_pit_continuation_sanity"
run_case "pit_all_symbols_continuation_sanity" \
  --event-types persistent_volume_breakout,volume_exhaustion \
  --thresholds 0.2,0.3 \
  --hold-days 3,5 \
  --sides continuation \
  --stop-loss-pcts 0,0.05,0.12 \
  --cost-multipliers 1,3 \
  --gross-exposure 0.5 \
  --entry-delay-hours 6 \
  --max-active-symbols 12 \
  --cooldown-days 7 \
  --rank-exit-threshold 0.5

DATA_ROOT="$DATA_ROOT" REPORT_BASE="$REPORT_BASE" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

import polars as pl

base = Path(os.environ["REPORT_BASE"])
frames = []
for path in sorted(base.glob("*/volume_event_scenario_summary.csv")):
    frame = pl.read_csv(path)
    if frame.is_empty():
        continue
    frames.append(frame.with_columns(pl.lit(path.parent.name).alias("run_name")))
if not frames:
    raise SystemExit("no scenario summaries were produced")
all_rows = pl.concat(frames, how="diagonal_relaxed")
sort_cols = ["promotion_gate_pass", "min_split_return", "avg_split_sharpe", "total_return", "max_drawdown"]
all_rows = all_rows.sort(sort_cols, descending=[True, True, True, True, True])
all_rows.write_csv(base / "fullpit_creative_volume_all_scenarios.csv")
top = all_rows.head(50)
top.write_csv(base / "fullpit_creative_volume_top50.csv")
lines = [
    "# Full PIT Creative Volume Research",
    "",
    "All rows were run through `volume-events` with full-PIT coverage required.",
    "",
    f"- Runs: {len(frames)}",
    f"- Scenarios: {all_rows.height}",
    f"- Promotion-pass rows: {all_rows.filter(pl.col('promotion_gate_pass')).height}",
    "",
    "| Rank | Run | Promote | Event | Side | Threshold | Hold | Stop | Cost | Trades | Return | Max DD | Sharpe | Pos Splits | Min Split | Avg Split Sharpe | Reason |",
    "|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
]
for idx, row in enumerate(top.head(25).to_dicts(), start=1):
    def pct(key: str) -> str:
        value = row.get(key)
        return "" if value is None else f"{float(value):.2%}"
    def num(key: str) -> str:
        value = row.get(key)
        return "" if value is None else f"{float(value):.2f}"
    lines.append(
        f"| {idx} | {row.get('run_name', '')} | {row.get('promotion_gate_pass', False)} | "
        f"{row.get('event_type', '')} | {row.get('side_hypothesis', '')} | {pct('threshold')} | "
        f"{row.get('hold_days', '')} | {pct('stop_loss_pct')} | {row.get('cost_multiplier', '')}x | "
        f"{row.get('trades', '')} | {pct('total_return')} | {pct('max_drawdown')} | {num('sharpe_like')} | "
        f"{row.get('positive_splits', '')}/3 | {pct('min_split_return')} | {num('avg_split_sharpe')} | "
        f"{row.get('promotion_reason', '')} |"
    )
(base / "fullpit_creative_volume_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(base / "fullpit_creative_volume_summary.md")
PY

log "creative full-PIT research complete: $REPORT_BASE"
