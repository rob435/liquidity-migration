#!/usr/bin/env bash
# P3b — validated backtest of the residual-momentum SELECTION gate (engine-integrated).
# Pre-registration: docs/preregistration/p3b-rmom-gate-backtest-2026-05-30.md
#   00_baseline   age300 (no rmom gate)
#   01_rmom_gated age300 + --liquidity-migration-residual-momentum-max=<per-venue median M>
# Requires <root>/residual_momentum.parquet (scripts/precompute_residual_momentum.py).
# Per-venue threshold via env BYBIT_RMOM_MAX / BINANCE_RMOM_MAX (the per-venue signal median).
# Same realistic baseline (capped10, max12, 15bps, full-PIT, 2023-04..2026-05). SERIAL; resumable.
set -uo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8

PHASE="${PHASE:-p3b_rmom_gate_2026-05-30}"
VENUES="${VENUES:-bybit binance}"
START="2023-04-01"; END="2026-05-28"
FIXED="max-active-symbols=12,cost-multipliers=1,liquidity-migration-pit-age-days-min=300"

rmom_max_for () { case "$1" in
    bybit) echo "${BYBIT_RMOM_MAX:?set BYBIT_RMOM_MAX to the bybit signal median}";;
    binance) echo "${BINANCE_RMOM_MAX:?set BINANCE_RMOM_MAX to the binance signal median}";;
esac; }
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

echo "=== P3b rmom-gate dispatch: phase=$PHASE venues='$VENUES' ==="
overall=0
for venue in $VENUES; do
    m="$(rmom_max_for "$venue")"
    run_cell "$venue" 00_baseline   ""                                                || overall=1
    run_cell "$venue" 01_rmom_gated "liquidity-migration-residual-momentum-max=${m}"  || overall=1
done
echo "=== P3b dispatch complete (overall rc=$overall) ==="
exit $overall
