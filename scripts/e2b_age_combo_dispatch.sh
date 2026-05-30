#!/usr/bin/env bash
# E2b — age-threshold sensitivity + prior30xage combo.
# Pre-registration: docs/preregistration/e2b-age-combo-2026-05-30.md
#   00_baseline     control
#   01_age200       pit-age-days-min=200
#   02_age400       pit-age-days-min=400
#   03_prior30_age  prior30-max-return-max=0.14 + pit-age-days-min=300  (stack E2 winners)
# (E2's age_min=300 is reused as the 200/300/400 midpoint.)
# Same realistic baseline as E1/E2. SERIAL (one ~23 GB cell on the 32 GB box); resumable.
set -uo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8

PHASE="${PHASE:-e2b_age_combo_2026-05-30}"
VENUES="${VENUES:-bybit binance}"
START="2023-04-01"; END="2026-05-28"
FIXED="max-active-symbols=12,cost-multipliers=1"

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

echo "=== E2b dispatch: phase=$PHASE venues='$VENUES' ==="
overall=0
for venue in $VENUES; do
    run_cell "$venue" 00_baseline    ""                                                                  || overall=1
    run_cell "$venue" 01_age200      "liquidity-migration-pit-age-days-min=200"                          || overall=1
    run_cell "$venue" 02_age400      "liquidity-migration-pit-age-days-min=400"                          || overall=1
    run_cell "$venue" 03_prior30_age "liquidity-migration-prior30-max-return-max=0.14,liquidity-migration-pit-age-days-min=300" || overall=1
done
echo "=== E2b dispatch complete (overall rc=$overall) ==="
exit $overall
