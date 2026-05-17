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
INTERVAL_SECONDS="${RISK_INTERVAL_SECONDS:-0.25}"
EXIT_ORDER_MODE="${EXIT_ORDER_MODE:-market}"
LIMIT_CHASE_ATTEMPTS="${LIMIT_CHASE_ATTEMPTS:-3}"
LIMIT_CHASE_INITIAL_BPS="${LIMIT_CHASE_INITIAL_BPS:-2}"
LIMIT_CHASE_STEP_BPS="${LIMIT_CHASE_STEP_BPS:-3}"
LIMIT_CHASE_MAX_BPS="${LIMIT_CHASE_MAX_BPS:-15}"
LIMIT_CHASE_WAIT_SECONDS="${LIMIT_CHASE_WAIT_SECONDS:-0.05}"
STOP_TOLERANCE_BPS="${STOP_TOLERANCE_BPS:-1}"

telegram_args=()
if [[ "${TELEGRAM_ENABLED:-1}" == "1" ]]; then
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID when TELEGRAM_ENABLED=1." >&2
        exit 2
    fi
    telegram_args+=(--telegram)
fi

order_args=()
if [[ "${SUBMIT_ORDERS:-0}" == "1" ]]; then
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_ORDERS=1 to submit Bybit demo risk orders." >&2
        exit 2
    fi
    if [[ -z "${BYBIT_DEMO_API_KEY:-}" || -z "${BYBIT_DEMO_API_SECRET:-}" ]]; then
        echo "Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET with SUBMIT_ORDERS=1." >&2
        exit 2
    fi
    order_args+=(--submit-orders --confirm-demo-orders)
fi

fallback_args=()
if [[ "${LIMIT_CHASE_FALLBACK_MARKET:-1}" != "1" ]]; then
    fallback_args+=(--no-limit-chase-fallback-market)
fi

loop_log_args=()
if [[ "${RISK_LOG_EVERY_CYCLE:-0}" != "1" ]]; then
    loop_log_args+=(--quiet-loop)
fi

echo "event risk engine starting"
echo "repo=$REPO_ROOT"
echo "data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0} exit_order_mode=$EXIT_ORDER_MODE"

mkdir -p "$DATA_ROOT/.locks"

if [[ -z "${BYBIT_DEMO_API_KEY:-}" || -z "${BYBIT_DEMO_API_SECRET:-}" ]]; then
    echo "Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET so the risk engine can read and enforce live demo positions." >&2
    exit 2
fi

while true; do
    set +e
    "$PYTHON_BIN" -m aggression_carry \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        event-risk-cycle \
        --loop \
        "${loop_log_args[@]}" \
        --interval-seconds "$INTERVAL_SECONDS" \
        --exit-order-mode "$EXIT_ORDER_MODE" \
        --limit-chase-attempts "$LIMIT_CHASE_ATTEMPTS" \
        --limit-chase-initial-bps "$LIMIT_CHASE_INITIAL_BPS" \
        --limit-chase-step-bps "$LIMIT_CHASE_STEP_BPS" \
        --limit-chase-max-bps "$LIMIT_CHASE_MAX_BPS" \
        --limit-chase-wait-seconds "$LIMIT_CHASE_WAIT_SECONDS" \
        --stop-tolerance-bps "$STOP_TOLERANCE_BPS" \
        "${telegram_args[@]}" \
        "${order_args[@]}" \
        "${fallback_args[@]}"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "event risk cycle failed with status=$status; sleeping before retry" >&2
    fi
    sleep "$INTERVAL_SECONDS"
done
