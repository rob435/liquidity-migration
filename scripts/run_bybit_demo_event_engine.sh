#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-demo-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-45}"
UNIVERSE_RANK_END="${UNIVERSE_RANK_END:-220}"
UNIVERSE_MAX_SYMBOLS="${UNIVERSE_MAX_SYMBOLS:-220}"
WORKERS="${WORKERS:-8}"
MAX_ORDER_NOTIONAL_PCT_EQUITY="${MAX_ORDER_NOTIONAL_PCT_EQUITY:-0}"
MAX_ENTRY_LAG_MINUTES="${MAX_ENTRY_LAG_MINUTES:-15}"
MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-5}"
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-2}"
ORDER_FILL_CONFIRM_SECONDS="${ORDER_FILL_CONFIRM_SECONDS:-2}"
ORDER_FILL_POLL_INTERVAL_SECONDS="${ORDER_FILL_POLL_INTERVAL_SECONDS:-0.2}"
FALLBACK_EQUITY_USDT="${FALLBACK_EQUITY_USDT:-10000}"

telegram_args=()
if [[ "${TELEGRAM_ENABLED:-1}" == "1" ]]; then
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID when TELEGRAM_ENABLED=1." >&2
        exit 2
    fi
    if [[ -z "${BYBIT_DEMO_API_KEY:-}" || -z "${BYBIT_DEMO_API_SECRET:-}" ]]; then
        echo "Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET so Telegram can report positions and PnL." >&2
        exit 2
    fi
    telegram_args+=(--telegram)
fi

order_args=()
if [[ "${SUBMIT_ORDERS:-0}" == "1" ]]; then
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_ORDERS=1 to submit Bybit demo orders." >&2
        exit 2
    fi
    order_args+=(--submit-orders --confirm-demo-orders)
fi

echo "event demo engine starting"
echo "repo=$REPO_ROOT"
echo "strategy=liqmig_union_q40_h3_tp25_g097_crowd_union"
echo "data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0}"

mkdir -p "$DATA_ROOT/.locks"

while true; do
    set +e
    "$PYTHON_BIN" -m aggression_carry \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        event-demo-cycle \
        --lookback-days "$LOOKBACK_DAYS" \
        --universe-rank-end "$UNIVERSE_RANK_END" \
        --universe-max-symbols "$UNIVERSE_MAX_SYMBOLS" \
        --workers "$WORKERS" \
        --max-order-notional-pct-equity "$MAX_ORDER_NOTIONAL_PCT_EQUITY" \
        --max-entry-lag-minutes "$MAX_ENTRY_LAG_MINUTES" \
        --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
        --entry-leverage "$ENTRY_LEVERAGE" \
        --order-fill-confirm-seconds "$ORDER_FILL_CONFIRM_SECONDS" \
        --order-fill-poll-interval-seconds "$ORDER_FILL_POLL_INTERVAL_SECONDS" \
        --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
        "${telegram_args[@]}" \
        "${order_args[@]}"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "event demo cycle failed with status=$status; sleeping before retry" >&2
    fi
    sleep "$INTERVAL_SECONDS"
done
