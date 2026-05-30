#!/usr/bin/env bash
# E1 execution-premium dispatcher (SELECTION-vs-EXECUTION plan §E1).
# Pre-registration: docs/preregistration/e1-execution-premium-2026-05-29.md
#
# Runs the two-arm contrast on the daily liquidity-migration candidate pool,
# holding selection + costs + concentration fixed and varying ONLY --entry-policy:
#   00_baseline       = fixed_delay              (immediate entry at +1h, control)
#   01_quality_squeeze = promoted_quality_squeeze (confirmed pop->giveback, treatment)
# on BOTH venues, at the realistic baseline (capped10 stops, max_active=12, full-PIT,
# 2023-04-01 -> 2026-05-28).
#
# SERIAL by design: one full-PIT cell peaks ~23 GB on a 32 GB box; two concurrent
# OOMs. Each cell writes a self-contained report dir, so the run is resumable —
# a cell whose research-report.json already exists is skipped.
#
# Env knobs:
#   PHASE  sweep tag / report subdir   (default e1_exec_premium_2026-05-29)
#   COST   --cost-multipliers value    (default 1 = 15 bps honest; 3 = 45 bps)
#   VENUES space-separated venue list  (default "bybit binance")
set -uo pipefail

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8

PHASE="${PHASE:-e1_exec_premium_2026-05-29}"
COST="${COST:-1}"
VENUES="${VENUES:-bybit binance}"
START="2023-04-01"
END="2026-05-28"
OVR_FIXED="max-active-symbols=12,cost-multipliers=${COST}"

root_for () {
    case "$1" in
        bybit)   echo "$HOME/SHARED_DATA/bybit_full_pit" ;;
        binance) echo "$HOME/SHARED_DATA/binance_full_pit" ;;
        *) echo "unknown venue: $1" >&2; return 2 ;;
    esac
}

run_cell () {
    local venue="$1" cell="$2" policy="$3"
    local root rep_dir rep_json log rc start_s end_s
    root="$(root_for "$venue")" || return 2
    rep_dir="$root/reports/$PHASE/$cell"
    rep_json="$rep_dir/volume_event_research_report.json"
    if [[ -f "$rep_json" ]]; then
        echo "[skip] $venue/$cell — report already exists"
        return 0
    fi
    mkdir -p "$rep_dir"
    log="$rep_dir/dispatch.log"
    echo "[run ] $venue/$cell policy=$policy cost=$COST start=$(date -u +%H:%M:%S)"
    start_s=$(date +%s)
    bash scripts/volume_events_cell.sh \
        --venue "$venue" --cell-id "$cell" --phase "$PHASE" \
        --start "$START" --end "$END" \
        --overrides "${OVR_FIXED},entry-policy=${policy}" \
        > "$log" 2>&1
    rc=$?
    end_s=$(date +%s)
    if [[ $rc -ne 0 ]]; then
        echo "[FAIL] $venue/$cell rc=$rc ($((end_s - start_s))s) — see $log"
        echo "       clear ${root}/.locks/*.lock if this was an OOM, then re-run." >&2
    else
        echo "[done] $venue/$cell rc=0 ($((end_s - start_s))s)"
    fi
    return $rc
}

echo "=== E1 dispatch: phase=$PHASE cost=$COST venues='$VENUES' ==="
overall=0
for venue in $VENUES; do
    run_cell "$venue" 00_baseline       fixed_delay              || overall=1
    run_cell "$venue" 01_quality_squeeze promoted_quality_squeeze || overall=1
done
echo "=== E1 dispatch complete (overall rc=$overall) ==="
exit $overall
