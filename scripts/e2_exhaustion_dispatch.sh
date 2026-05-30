#!/usr/bin/env bash
# E2 exhaustion-quality SELECTION refinement (SELECTION-vs-EXECUTION plan §E2, pivoted).
# Pre-registration: docs/preregistration/e2-exhaustion-selection-2026-05-30.md
#
# E1 verdict = selection-dominant, so E2 tests a pre-registered exhaustion-quality gate on
# the SELECTION filter (cross-venue-clean features only; directions fixed by E1 within-
# selection IC; common round thresholds = one rule both venues). Components + combined:
#   00_baseline            control
#   01_prior30_cap         prior30-max-return-max=0.14   (drop top-tercile prior-spike)
#   02_age_min             pit-age-days-min=300          (drop youngest-tercile)
#   03_liq_tighten         universe-rank-max=110         (keep more-liquid band)
#   04_exhaustion_combined all three
# Same realistic baseline as E1 (capped10, max12, 15bps, full-PIT, 2023-04..2026-05).
# SERIAL (one ~23 GB cell at a time on the 32 GB box); resumable (skip-if-report-exists).
set -uo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8

PHASE="${PHASE:-e2_exhaustion_select_2026-05-30}"
VENUES="${VENUES:-bybit binance}"
START="2023-04-01"; END="2026-05-28"
FIXED="max-active-symbols=12,cost-multipliers=1"
PRIOR30="liquidity-migration-prior30-max-return-max=0.14"
AGE="liquidity-migration-pit-age-days-min=300"
LIQ="universe-rank-max=110"

root_for () { case "$1" in
    bybit) echo "$HOME/SHARED_DATA/bybit_full_pit";;
    binance) echo "$HOME/SHARED_DATA/binance_full_pit";;
    *) echo "unknown venue: $1" >&2; return 2;; esac; }

run_cell () {
    local venue="$1" cell="$2" extra="$3" root rep_dir rep_json rc s e ovr
    root="$(root_for "$venue")" || return 2
    rep_dir="$root/reports/$PHASE/$cell"; rep_json="$rep_dir/volume_event_research_report.json"
    if [[ -f "$rep_json" ]]; then echo "[skip] $venue/$cell — exists"; return 0; fi
    mkdir -p "$rep_dir"
    ovr="$FIXED"; [[ -n "$extra" ]] && ovr="${FIXED},${extra}"
    echo "[run ] $venue/$cell start=$(date -u +%H:%M:%S)  extra='${extra:-none}'"
    s=$(date +%s)
    bash scripts/volume_events_cell.sh --venue "$venue" --cell-id "$cell" --phase "$PHASE" \
        --start "$START" --end "$END" --overrides "$ovr" > "$rep_dir/dispatch.log" 2>&1
    rc=$?; e=$(date +%s)
    if [[ $rc -ne 0 ]]; then echo "[FAIL] $venue/$cell rc=$rc ($((e-s))s) — see $rep_dir/dispatch.log; clear ${root}/.locks/*.lock if OOM" >&2
    else echo "[done] $venue/$cell rc=0 ($((e-s))s)"; fi
    return $rc
}

echo "=== E2 exhaustion dispatch: phase=$PHASE venues='$VENUES' ==="
overall=0
for venue in $VENUES; do
    run_cell "$venue" 00_baseline            ""                                  || overall=1
    run_cell "$venue" 01_prior30_cap         "$PRIOR30"                          || overall=1
    run_cell "$venue" 02_age_min             "$AGE"                              || overall=1
    run_cell "$venue" 03_liq_tighten         "$LIQ"                              || overall=1
    run_cell "$venue" 04_exhaustion_combined "${PRIOR30},${AGE},${LIQ}"          || overall=1
done
echo "=== E2 dispatch complete (overall rc=$overall) ==="
exit $overall
