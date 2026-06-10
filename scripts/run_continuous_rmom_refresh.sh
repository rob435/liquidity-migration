#!/usr/bin/env bash
set -euo pipefail

cd "${REPO_DIR:-/opt/liquidity-migration}"

. deploy/lib_sleeves.sh
lm_load_sleeve_toggles

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=python3
fi

# The paper shadow FOLLOWS the demo root's kline store + rmom gate (one shared WS
# data plane per box — KLINES_FOLLOW_ROOT in the paper unit). So: keep the FOLLOWED
# demo root's gate fresh whenever EITHER continuous sleeve is on (paper-only
# evidence still needs it), and never rebuild the paper root's gate — its own kline
# store is dormant and a gate built from it would be stale.
if continuous_rmom_refresh_on; then
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py --root data/bybit-continuous-demo-event
else
    echo "continuous rmom refresh skipped: continuous demo and paper sleeves are off."
fi
