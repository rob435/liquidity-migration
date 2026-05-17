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
CANARY_SYMBOL="${CANARY_SYMBOL:-DOGEUSDT}"
CANARY_SIDE="${CANARY_SIDE:-Buy}"
CANARY_NOTIONAL_USDT="${CANARY_NOTIONAL_USDT:-8}"
CANARY_PRICE_DISTANCE_BPS="${CANARY_PRICE_DISTANCE_BPS:-2000}"
CANARY_CANCEL_VERIFY_SECONDS="${CANARY_CANCEL_VERIFY_SECONDS:-5}"
CANARY_CANCEL_VERIFY_POLL_SECONDS="${CANARY_CANCEL_VERIFY_POLL_SECONDS:-0.25}"

order_args=()
if [[ "${SUBMIT_CANARY:-0}" == "1" ]]; then
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_CANARY=1 to submit Bybit demo canary orders." >&2
        exit 2
    fi
    if [[ -z "${BYBIT_DEMO_API_KEY:-}" || -z "${BYBIT_DEMO_API_SECRET:-}" ]]; then
        echo "Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET for demo canary submission." >&2
        exit 2
    fi
    order_args+=(--submit-order --confirm-demo-orders)
fi

mkdir -p "$DATA_ROOT/.locks"

echo "demo canary starting"
echo "repo=$REPO_ROOT"
echo "data_root=$DATA_ROOT symbol=$CANARY_SYMBOL side=$CANARY_SIDE submit_canary=${SUBMIT_CANARY:-0}"

"$PYTHON_BIN" -m aggression_carry \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    demo-canary \
    --symbol "$CANARY_SYMBOL" \
    --side "$CANARY_SIDE" \
    --order-notional-usdt "$CANARY_NOTIONAL_USDT" \
    --price-distance-bps "$CANARY_PRICE_DISTANCE_BPS" \
    --cancel-verify-seconds "$CANARY_CANCEL_VERIFY_SECONDS" \
    --cancel-verify-poll-seconds "$CANARY_CANCEL_VERIFY_POLL_SECONDS" \
    "${order_args[@]}"
