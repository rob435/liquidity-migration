#!/usr/bin/env bash
set -euo pipefail

cd "${REPO_DIR:-/opt/liquidity-migration}"

. deploy/lib_sleeves.sh
lm_load_sleeve_toggles

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=python3
fi

# audit2 (deploy-env-timers-3 follow-up): the paper shadow USED to follow the demo
# root's kline store + rmom gate (KLINES_FOLLOW_ROOT in the paper unit). Commit
# 7d39d61 DROPPED that follow override so the paper shadow streams its OWN kline pool
# and stays live even when the demo (leader) sleeve is off. But this refresh script was
# left only rebuilding the demo root's gate, so the paper daemon now reads a gate from
# its own root that nothing ever builds -> rmom q25 never resolves -> the paper book
# emits ZERO entries forever and the paper<->demo cost/slippage reconcile has nothing to
# pair. Fix: refresh EACH on sleeve's OWN root from its own kline store.
refreshed=0
if sleeve_on "${CONTINUOUS_SLEEVE:-off}"; then
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-demo-event
    refreshed=1
fi
if sleeve_on "${CONTINUOUS_PAPER_SLEEVE:-off}"; then
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-paper-event
    refreshed=1
fi
if [ "$refreshed" -eq 0 ]; then
    echo "continuous rmom refresh skipped: continuous demo and paper sleeves are off."
fi
