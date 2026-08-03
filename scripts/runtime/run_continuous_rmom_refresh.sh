#!/usr/bin/env bash
set -euo pipefail

cd "${REPO_DIR:-/opt/liquidity-migration}"

. deploy/lib_sleeves.sh

# The systemd unit inherits the exact resolved sleeve file bound into the
# operational authorization. Do not re-read mutable repo/host toggle sources.
: "${CONTINUOUS_SLEEVE:?CONTINUOUS_SLEEVE is required}"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=python3
fi

CONTINUOUS_DEMO_DATA_ROOT="${CONTINUOUS_DEMO_DATA_ROOT-data/bybit-continuous-demo-event}"
if [ -z "$CONTINUOUS_DEMO_DATA_ROOT" ]; then
    echo "CONTINUOUS_DEMO_DATA_ROOT must be non-empty." >&2
    exit 2
fi

if sleeve_on "${CONTINUOUS_SLEEVE:-off}"; then
    # Live event_demo_klines_1h roots are rolling stores, not stable archives:
    # rebuild the gate from the current store rather than fail the daily timer
    # on drifting append-mode overlap rows.
    "$PYTHON_BIN" -u scripts/data/precompute_residual_momentum.py \
        --root "$CONTINUOUS_DEMO_DATA_ROOT" --full-rewrite
else
    echo "continuous rmom refresh skipped: continuous sleeve is off."
fi
