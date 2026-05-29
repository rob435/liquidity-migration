#!/usr/bin/env bash
# E1b knob-engagement probe (SELECTION-vs-EXECUTION plan §E1 follow-on).
# Pre-registration: docs/preregistration/e1b-knob-engagement-2026-05-30.md
#
# Rules out "the default squeeze under-engaged and manufactured the E1 null" by
# forcing EVERY candidate through the pop->giveback wait loop (h1 gate -> 0):
#   00_baseline   = fixed_delay                  (immediate +1h, control)
#   01_engage_all = promoted_quality_squeeze with h1-return-bps=0,
#                   h1-close-location-min=0.0    (all candidates wait for
#                   giveback or 4h deadline; pop25/give25/wait4 defaults)
# Same realistic baseline as E1 (capped10, max12, 15bps, full-PIT, 2023-04..2026-05).
# SERIAL (one ~23 GB cell at a time on the 32 GB box); resumable (skip-if-report-exists).
set -uo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8

PHASE="${PHASE:-e1_knob_engage_2026-05-30}"
VENUES="${VENUES:-bybit binance}"
START="2023-04-01"; END="2026-05-28"
FIXED="max-active-symbols=12,cost-multipliers=1"

root_for () { case "$1" in
    bybit) echo "$HOME/SHARED_DATA/bybit_full_pit";;
    binance) echo "$HOME/SHARED_DATA/binance_full_pit";;
    *) echo "unknown venue: $1" >&2; return 2;; esac; }

run_cell () {
    local venue="$1" cell="$2" ovr="$3" root rep_dir rep_json rc s e
    root="$(root_for "$venue")" || return 2
    rep_dir="$root/reports/$PHASE/$cell"; rep_json="$rep_dir/volume_event_research_report.json"
    if [[ -f "$rep_json" ]]; then echo "[skip] $venue/$cell — exists"; return 0; fi
    mkdir -p "$rep_dir"
    echo "[run ] $venue/$cell start=$(date -u +%H:%M:%S)  overrides=$ovr"
    s=$(date +%s)
    bash scripts/volume_events_cell.sh --venue "$venue" --cell-id "$cell" --phase "$PHASE" \
        --start "$START" --end "$END" --overrides "$ovr" > "$rep_dir/dispatch.log" 2>&1
    rc=$?; e=$(date +%s)
    if [[ $rc -ne 0 ]]; then echo "[FAIL] $venue/$cell rc=$rc ($((e-s))s) — see $rep_dir/dispatch.log; clear ${root}/.locks/*.lock if OOM" >&2
    else echo "[done] $venue/$cell rc=0 ($((e-s))s)"; fi
    return $rc
}

echo "=== E1b knob-engage dispatch: phase=$PHASE venues='$VENUES' ==="
overall=0
for venue in $VENUES; do
    run_cell "$venue" 00_baseline   "${FIXED},entry-policy=fixed_delay" || overall=1
    run_cell "$venue" 01_engage_all "${FIXED},entry-policy=promoted_quality_squeeze,entry-quality-squeeze-h1-return-bps=0,entry-quality-squeeze-h1-close-location-min=0.0" || overall=1
done
echo "=== E1b dispatch complete (overall rc=$overall) ==="
exit $overall
