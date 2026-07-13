#!/usr/bin/env bash
set -euo pipefail

cd "${REPO_DIR:-/opt/liquidity-migration}"

. deploy/lib_sleeves.sh
lm_load_sleeve_toggles

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=python3
fi

# Demo and deterministic-paper target producers keep independent kline/RMOM
# roots so one dead feed cannot be hidden by the other. Refresh every enabled
# producer's gate from its own current store; otherwise q25 never resolves and
# that producer silently emits no targets.
refreshed=0
if sleeve_on "${CONTINUOUS_SLEEVE:-off}"; then
    # Live event_demo_klines_1h roots are rolling operational stores, not stable
    # research archives. Append-mode overlap equivalence is correct for stable
    # roots, but these live roots should rebuild the gate from the current store
    # instead of parking the daily timer in FAILED when old overlap rows drift.
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-demo-event --full-rewrite
    refreshed=1
fi
if sleeve_on "${CONTINUOUS_PAPER_SLEEVE:-off}"; then
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-paper-event --full-rewrite
    refreshed=1
fi
if [ "$refreshed" -eq 0 ]; then
    echo "continuous rmom refresh skipped: continuous demo and paper sleeves are off."
fi
