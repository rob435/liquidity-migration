#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-demo-event}"
ORDER_SUBMIT_MODE="${ORDER_SUBMIT_MODE:-ws_then_rest}"
REST_RECONCILE_SECONDS="${REST_RECONCILE_SECONDS:-30}"
HEARTBEAT_SECONDS="${HEARTBEAT_SECONDS:-10}"
STREAM_START_TIMEOUT_SECONDS="${STREAM_START_TIMEOUT_SECONDS:-3}"
STOP_TOLERANCE_BPS="${STOP_TOLERANCE_BPS:-1}"
FAST_EXECUTION_STREAM="${FAST_EXECUTION_STREAM:-0}"
PENDING_EXIT_GUARD_SECONDS="${PENDING_EXIT_GUARD_SECONDS:-120}"
EXIT_UNTRACKED_POSITIONS="${EXIT_UNTRACKED_POSITIONS:-1}"
UNTRACKED_POSITION_GRACE_SECONDS="${UNTRACKED_POSITION_GRACE_SECONDS:-90}"
# Dual-side support (combined-book deployment): set LONG_DATA_ROOT to the
# long-sleeve data root (e.g. data/bybit-long-demo-event) and this single ws_risk
# instance will read/write BOTH ledgers, routing by per-row `sleeve` column.
# Leave empty to keep short-only behavior (legacy default).
LONG_DATA_ROOT="${LONG_DATA_ROOT:-}"

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
    order_args+=(--submit-orders --confirm-demo-orders)
fi

if [[ -z "${BYBIT_DEMO_API_KEY:-}" || -z "${BYBIT_DEMO_API_SECRET:-}" ]]; then
    echo "Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET so the WS risk engine can stream and enforce demo positions." >&2
    exit 2
fi

fallback_args=()
if [[ "${REST_FALLBACK:-1}" != "1" ]]; then
    fallback_args+=(--no-rest-fallback)
fi

execution_stream_args=()
if [[ "$FAST_EXECUTION_STREAM" != "1" ]]; then
    execution_stream_args+=(--no-fast-execution-stream)
fi

untracked_args=()
if [[ "$EXIT_UNTRACKED_POSITIONS" != "1" ]]; then
    untracked_args+=(--no-exit-untracked-positions)
fi

dual_sleeve_args=()
if [[ -n "$LONG_DATA_ROOT" ]]; then
    dual_sleeve_args+=(--long-data-root "$LONG_DATA_ROOT")
    mkdir -p "$LONG_DATA_ROOT/.locks"
fi

mkdir -p "$DATA_ROOT/.locks"

echo "event websocket risk engine starting"
echo "repo=$REPO_ROOT"
echo "data_root=$DATA_ROOT long_data_root=${LONG_DATA_ROOT:-(unset)} submit_orders=${SUBMIT_ORDERS:-0} order_submit_mode=$ORDER_SUBMIT_MODE rest_fallback=${REST_FALLBACK:-1}"

"$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    event-risk-ws \
    --order-submit-mode "$ORDER_SUBMIT_MODE" \
    --rest-reconcile-seconds "$REST_RECONCILE_SECONDS" \
    --heartbeat-seconds "$HEARTBEAT_SECONDS" \
    --stream-start-timeout-seconds "$STREAM_START_TIMEOUT_SECONDS" \
    --stop-tolerance-bps "$STOP_TOLERANCE_BPS" \
    --pending-exit-guard-seconds "$PENDING_EXIT_GUARD_SECONDS" \
    --untracked-position-grace-seconds "$UNTRACKED_POSITION_GRACE_SECONDS" \
    "${telegram_args[@]}" \
    "${order_args[@]}" \
    "${fallback_args[@]}" \
    "${execution_stream_args[@]}" \
    "${untracked_args[@]}" \
    "${dual_sleeve_args[@]}"
