#!/usr/bin/env bash
# drop_all_4 re-evaluation at the DEPLOYED promoted concentration (max_active=5).
# LOCAL ONLY — no ssh/rsync/network beyond reading the local full-PIT roots.
# Runs 4 backtests serially (parallel would OOM the 16GB box): {bybit,binance}
# x {baseline, drop_all_4}, all else = deployed promoted profile.
#
# drop_all_4 drops 4 vetoes/bounds to non-binding sentinels:
#   day_return_min 0.0->-1.0 ; stop_pressure_stop_count 7->999 ;
#   realized_loss_pressure_loss_count 6->999 ; universe_rank_max 150->99999
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TAG="drop4_eval_max5_$(date -u +%Y-%m-%d)"
OUT="data/reconcile/$TAG"
START=2023-04-01
END=2026-05-28

# Deployed promoted-profile baseline flags (max_active=5, rank 31-150).
common=(
  --config configs/volume_alpha.default.yaml
  volume-events
  --start "$START" --end "$END"
  --event-types liquidity_migration
  --thresholds 0.4 --hold-days 3 --sides reversal
  --stop-loss-pcts 0.12 --take-profit-pcts 0.26
  --cost-multipliers 3 --gross-exposure 1.0
  --entry-delay-hours 1 --entry-policy promoted_quality_squeeze
  --max-active-symbols 5 --cooldown-days 5 --rank-exit-threshold 0.55
  --universe-rank-min 31
  --liquidity-migration-rank-improvement-min 150
  --liquidity-migration-rank-direction improvement
  --liquidity-migration-turnover-ratio-min 6.0
  --liquidity-migration-event-rank-fraction-max 0.90
  --liquidity-migration-residual-return-min 0.08
  --liquidity-migration-close-location-min 0.30
  --liquidity-migration-pit-age-days-min 90
  --liquidity-migration-crowding-filter union_pathology
  --allow-partial-pit
)

run () { # venue cell rank_max day_ret stopcnt losscnt
  local venue=$1 cell=$2 rmax=$3 dret=$4 scnt=$5 lcnt=$6
  local root="$HOME/SHARED_DATA/${venue}_full_pit"
  local rpt="$OUT/${venue}/${cell}"
  mkdir -p "$rpt"
  echo "[$(date -u +%H:%M:%S)] >>> $venue/$cell (rank_max=$rmax day_ret=$dret stop=$scnt loss=$lcnt)"
  "$PY" -m liquidity_migration --data-root "$root" "${common[@]}" \
    --universe-rank-max "$rmax" \
    --liquidity-migration-day-return-min "$dret" \
    --stop-pressure-stop-count "$scnt" \
    --realized-loss-pressure-loss-count "$lcnt" \
    --report-dir "$rpt" 2>&1 | tail -2
}

for venue in bybit binance; do
  run "$venue" 00_baseline  150   0.0  7   6
  run "$venue" drop_all_4   99999 -1.0 999 999
done
echo "ALL_DONE tag=$TAG out=$OUT"
