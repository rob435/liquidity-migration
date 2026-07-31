#!/usr/bin/env bash
set -euo pipefail

cd "${REPO_DIR:-/opt/liquidity-migration}"

. deploy/lib_sleeves.sh

# The systemd unit inherits the exact resolved sleeve file bound into the
# operational authorization. Do not re-read mutable repo/host toggle sources.
: "${CONTINUOUS_SLEEVE:?CONTINUOUS_SLEEVE is required}"
: "${CONTINUOUS_PAPER_SLEEVE:?CONTINUOUS_PAPER_SLEEVE is required}"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN=python3
fi

CONTINUOUS_DEMO_DATA_ROOT="${CONTINUOUS_DEMO_DATA_ROOT-data/bybit-continuous-demo-event}"
if [ -z "$CONTINUOUS_DEMO_DATA_ROOT" ]; then
    echo "CONTINUOUS_DEMO_DATA_ROOT must be non-empty." >&2
    exit 2
fi

# Demo owns the one bounded kline/RMOM plane. The co-located paper producer
# follows this root read-only, so computing a second identical gate is waste.
if sleeve_on "${CONTINUOUS_SLEEVE:-off}" || sleeve_on "${CONTINUOUS_PAPER_SLEEVE:-off}"; then
    # Live event_demo_klines_1h roots are rolling stores, not stable archives:
    # rebuild the gate from the current store rather than fail the daily timer
    # on drifting append-mode overlap rows.
    "$PYTHON_BIN" -u scripts/precompute_residual_momentum.py \
        --root "$CONTINUOUS_DEMO_DATA_ROOT" --full-rewrite
    rmom_path="$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet"
    if [[ -e "$rmom_path" && "$(id -u)" -eq 0 ]] \
        && getent group liquidity-migration-paper >/dev/null 2>&1; then
        chgrp liquidity-migration-paper "$rmom_path"
        chmod 0640 "$rmom_path"
    fi
else
    echo "continuous rmom refresh skipped: continuous demo and paper sleeves are off."
fi
