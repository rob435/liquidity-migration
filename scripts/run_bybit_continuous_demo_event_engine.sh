#!/usr/bin/env bash
# Continuous-fade demo sleeve — SUB-HOURLY, ticker-driven, SEPARATE ledger.
#
# A 4th forward-demo sleeve alongside short (lm-en-*), long (lm-en-l-*). Order-link
# prefix lm-en-c-* so the extended ws_risk routes fills to the continuous ledger
# (data/bybit-continuous-demo-event). Reuses the shared WS architecture (kline pool,
# TickerCache, PrivateStateCache, ExecutionEventRouter) via the daemon. "No 1h": the
# decile is recomputed off the live ticker price every INTERVAL_SECONDS (heartbeat),
# not gated on the hourly bar close.
#
# Hard gates: SUBMIT_ORDERS=1 requires CONFIRM_DEMO_ORDERS=1. Demo account only.
# The signal needs a fresh data/bybit-continuous-demo-event/residual_momentum.parquet
# (the rmom gate); the continuous-rmom-refresh.timer keeps it current.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-continuous-demo-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
LOOKBACK_DAYS="${LOOKBACK_DAYS:-45}"
WORKERS="${WORKERS:-4}"
MAX_ACTIVE="${MAX_ACTIVE:-25}"
MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-5}"
MAX_HOLD_HOURS="${MAX_HOLD_HOURS:-48}"
STOP_LOSS_PCT="${STOP_LOSS_PCT:-0.25}"  # wide server-side disaster stop; the state exit is profit-only
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-2}"
PER_POSITION_NOTIONAL_PCT_EQUITY="${PER_POSITION_NOTIONAL_PCT_EQUITY:-2}"
LIQ_TURNOVER_MIN="${LIQ_TURNOVER_MIN:-500000}"
FALLBACK_EQUITY_USDT="${FALLBACK_EQUITY_USDT:-10000}"

order_args=()
if [[ "${SUBMIT_ORDERS:-0}" == "1" ]]; then
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_ORDERS=1 to submit Bybit demo orders." >&2
        exit 2
    fi
    order_args+=(--submit-orders --confirm-demo-orders)
fi
[[ "${TELEGRAM_ENABLED:-0}" == "1" ]] && order_args+=(--telegram)

echo "continuous-demo engine: data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0}"
exec "$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    continuous-event-demo-cycle \
    --lookback-days "$LOOKBACK_DAYS" \
    --workers "$WORKERS" \
    --max-active "$MAX_ACTIVE" \
    --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
    --max-hold-hours "$MAX_HOLD_HOURS" \
    --stop-loss-pct "$STOP_LOSS_PCT" \
    --entry-leverage "$ENTRY_LEVERAGE" \
    --per-position-notional-pct-equity "$PER_POSITION_NOTIONAL_PCT_EQUITY" \
    --liq-turnover-min "$LIQ_TURNOVER_MIN" \
    --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${order_args[@]+"${order_args[@]}"}
