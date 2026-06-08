#!/usr/bin/env bash
set -euo pipefail

cd "${REPO_DIR:-/opt/liquidity-migration}"

. deploy/lib_sleeves.sh
lm_load_sleeve_toggles

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=python3
fi

ran=0
if sleeve_on "$CONTINUOUS_SLEEVE"; then
    ran=1
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-demo-event
fi

if sleeve_on "$CONTINUOUS_PAPER_SLEEVE"; then
    ran=1
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-paper-event
fi

if [ "$ran" -eq 0 ]; then
    echo "continuous rmom refresh skipped: continuous demo and paper sleeves are off."
fi
