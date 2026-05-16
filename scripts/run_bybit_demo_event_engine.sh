#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-demo-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-45}"
UNIVERSE_RANK_END="${UNIVERSE_RANK_END:-220}"
UNIVERSE_MAX_SYMBOLS="${UNIVERSE_MAX_SYMBOLS:-220}"
WORKERS="${WORKERS:-8}"
MAX_ORDER_NOTIONAL_PCT_EQUITY="${MAX_ORDER_NOTIONAL_PCT_EQUITY:-0.10}"
MAX_ENTRY_LAG_MINUTES="${MAX_ENTRY_LAG_MINUTES:-180}"
MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-6}"
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-1}"
FALLBACK_EQUITY_USDT="${FALLBACK_EQUITY_USDT:-10000}"

telegram_args=()
if [[ "${TELEGRAM_ENABLED:-1}" == "1" ]]; then
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
echo "data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0}"

while true; do
    set +e
    python -m aggression_carry \
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
