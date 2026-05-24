#!/usr/bin/env bash
# Sweep long-continuation variants of each crypto-native event type already
# coded in volume_events.py. Target: find a long-side analog of the short
# sleeve's Sharpe 3.37.
set -u
cd "$(dirname "$0")/.."

LOG=/tmp/sweep_long_events.log
echo "starting long-event sweep at $(date)" > "$LOG"

# Each event type tested with two stop/TP profiles:
#   tight  — 8% stop / 20% TP (asymmetric upside)
#   wide   — 12% stop / 30% TP (more room to breathe)
# Hold days 5, max 5 concurrent, cooldown 7, fixed_delay entry (skip the
# liquidity_migration-specific promoted_quality_squeeze logic which doesn't
# apply to other event types).

run_one() {
    local name=$1
    local event_type=$2
    local stop=$3
    local tp=$4
    local hold=$5
    local out=~/SHARED_DATA/bybit_fullpit_1h/reports/long_event_${name}
    echo "=== $name ($(date)) ===" | tee -a "$LOG"
    .venv/bin/python -m liquidity_migration \
        --data-root ~/SHARED_DATA/bybit_fullpit_1h \
        volume-events \
        --event-types "$event_type" \
        --thresholds 0.4 \
        --sides continuation \
        --hold-days "$hold" \
        --stop-loss-pcts "$stop" \
        --take-profit-pcts "$tp" \
        --cost-multipliers 3.0 \
        --max-active-symbols 5 \
        --cooldown-days 7 \
        --entry-policy fixed_delay \
        --start 2023-05-03 --end 2026-05-18 \
        --allow-partial-pit \
        --report-dir "$out" 2>&1 | tail -2 | tee -a "$LOG"
}

# All variants are LONG (continuation maps to long for these event types)
run_one "capitulation_reclaim_tight"    "capitulation_reclaim"   "0.08" "0.20" "5"
run_one "capitulation_reclaim_wide"     "capitulation_reclaim"   "0.12" "0.30" "7"
run_one "dryup_reacceleration_tight"    "dryup_reacceleration"   "0.08" "0.20" "5"
run_one "dryup_reacceleration_wide"     "dryup_reacceleration"   "0.12" "0.30" "7"
run_one "volume_shelf_reclaim_tight"    "volume_shelf_reclaim"   "0.08" "0.20" "5"
run_one "volume_shelf_reclaim_wide"     "volume_shelf_reclaim"   "0.12" "0.30" "7"
run_one "reclaim_breakout_tight"        "reclaim_breakout"       "0.08" "0.20" "5"
run_one "reclaim_breakout_wide"         "reclaim_breakout"       "0.12" "0.30" "7"
run_one "top_volume_leadership_tight"   "top_volume_leadership"  "0.08" "0.20" "5"
run_one "top_volume_leadership_wide"    "top_volume_leadership"  "0.12" "0.30" "7"
run_one "persistent_volume_breakout"    "persistent_volume_breakout" "0.10" "0.25" "5"
run_one "orderly_leadership_pullback"   "orderly_leadership_pullback" "0.08" "0.20" "5"

echo "done at $(date)" >> "$LOG"
echo "--- SUMMARY ---" >> "$LOG"
.venv/bin/python -c "
import json, glob, os
paths = sorted(glob.glob('/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_event_*/volume_event_research_report.json'))
rows = []
for p in paths:
    d = json.load(open(p))
    b = d.get('best_scenario') or {}
    if not b:
        continue
    name = os.path.basename(os.path.dirname(p)).replace('long_event_', '')
    rows.append((b.get('sharpe_like',0), name, b.get('total_return',0), b.get('max_drawdown',0), b.get('trades',0)))
rows.sort(key=lambda r: -r[0])
for sh, n, ret, dd, t in rows:
    print(f'{n:<40} sharpe={sh:+.2f} ret={ret:+.2%} dd={dd:+.2%} trades={t}')
" 2>&1 | tee -a "$LOG"
