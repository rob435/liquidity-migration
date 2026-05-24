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
STRATEGY_PROFILE="${STRATEGY_PROFILE:-promoted}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
LOOKBACK_DAYS="${LOOKBACK_DAYS:-45}"
# Universe must cover prior-week ranks of rocket-symbols: promoted needs
# rank_max(150) + rank_improvement_min(150) = 300, demo_relaxed needs
# 260 + 80 = 340. 400 covers both with buffer; _validate_demo_config rejects
# anything below the per-profile minimum.
if [[ "$STRATEGY_PROFILE" == "demo_relaxed" ]]; then
    UNIVERSE_RANK_END="${UNIVERSE_RANK_END:-400}"
    UNIVERSE_MAX_SYMBOLS="${UNIVERSE_MAX_SYMBOLS:-400}"
    UNIVERSE_MIN_TURNOVER_24H="${UNIVERSE_MIN_TURNOVER_24H:-0}"
    MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-10}"
else
    UNIVERSE_RANK_END="${UNIVERSE_RANK_END:-400}"
    UNIVERSE_MAX_SYMBOLS="${UNIVERSE_MAX_SYMBOLS:-400}"
    UNIVERSE_MIN_TURNOVER_24H="${UNIVERSE_MIN_TURNOVER_24H:-0}"
    MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-5}"
fi
WORKERS="${WORKERS:-8}"
MAX_ACTIVE_SYMBOLS="${MAX_ACTIVE_SYMBOLS:-0}"
MAX_ORDER_NOTIONAL_PCT_EQUITY="${MAX_ORDER_NOTIONAL_PCT_EQUITY:-0}"
MAX_ENTRY_LAG_MINUTES="${MAX_ENTRY_LAG_MINUTES:-360}"
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
    if [[ "$STRATEGY_PROFILE" != "promoted" ]]; then
        echo "Only STRATEGY_PROFILE=promoted is allowed to submit demo entry orders." >&2
        exit 2
    fi
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_ORDERS=1 to submit Bybit demo orders." >&2
        exit 2
    fi
    order_args+=(--submit-orders --confirm-demo-orders)
fi

echo "event demo engine starting"
echo "repo=$REPO_ROOT"
echo "strategy_profile=$STRATEGY_PROFILE"
echo "data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0} use_daemon=${USE_DAEMON:-0}"

mkdir -p "$DATA_ROOT/.locks"

# USE_DAEMON=1: single long-running Python process that subscribes once to the
# Bybit private execution WebSocket, runs cycles internally, and routes
# WS-pushed fills through ExecutionEventRouter so _wait_for_execution_summary
# returns in <30ms instead of REST-polling get_trade_history. REST is the
# fallback, never the sole path. SIGTERM drains the current cycle and exits
# cleanly so `systemctl stop` is safe. Drop USE_DAEMON to fall back to the
# legacy bash-loop runner below — rollback is a single env-var change.
if [[ "${USE_DAEMON:-0}" == "1" ]]; then
    echo "event demo engine: daemon mode (long-running, WS fill confirmation)"
    exec "$PYTHON_BIN" -m liquidity_migration \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        event-demo-cycle \
        --lookback-days "$LOOKBACK_DAYS" \
        --universe-rank-end "$UNIVERSE_RANK_END" \
        --universe-max-symbols "$UNIVERSE_MAX_SYMBOLS" \
        --universe-min-turnover-24h "$UNIVERSE_MIN_TURNOVER_24H" \
        --workers "$WORKERS" \
        --max-order-notional-pct-equity "$MAX_ORDER_NOTIONAL_PCT_EQUITY" \
        --max-entry-lag-minutes "$MAX_ENTRY_LAG_MINUTES" \
        --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
        --max-active-symbols "$MAX_ACTIVE_SYMBOLS" \
        --entry-leverage "$ENTRY_LEVERAGE" \
        --order-fill-confirm-seconds "$ORDER_FILL_CONFIRM_SECONDS" \
        --order-fill-poll-interval-seconds "$ORDER_FILL_POLL_INTERVAL_SECONDS" \
        --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
        --strategy-profile "$STRATEGY_PROFILE" \
        --daemon --interval-seconds "$INTERVAL_SECONDS" \
        "${telegram_args[@]}" \
        "${order_args[@]}"
fi

echo "event demo engine: legacy bash-loop mode (USE_DAEMON=1 to enable daemon)"
while true; do
    cycle_start_epoch="$(date +%s)"
    set +e
    "$PYTHON_BIN" -m liquidity_migration \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        event-demo-cycle \
        --lookback-days "$LOOKBACK_DAYS" \
        --universe-rank-end "$UNIVERSE_RANK_END" \
        --universe-max-symbols "$UNIVERSE_MAX_SYMBOLS" \
        --universe-min-turnover-24h "$UNIVERSE_MIN_TURNOVER_24H" \
        --workers "$WORKERS" \
        --max-order-notional-pct-equity "$MAX_ORDER_NOTIONAL_PCT_EQUITY" \
        --max-entry-lag-minutes "$MAX_ENTRY_LAG_MINUTES" \
        --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
        --max-active-symbols "$MAX_ACTIVE_SYMBOLS" \
        --entry-leverage "$ENTRY_LEVERAGE" \
        --order-fill-confirm-seconds "$ORDER_FILL_CONFIRM_SECONDS" \
        --order-fill-poll-interval-seconds "$ORDER_FILL_POLL_INTERVAL_SECONDS" \
        --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
        --strategy-profile "$STRATEGY_PROFILE" \
        "${telegram_args[@]}" \
        "${order_args[@]}"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "event demo cycle failed with status=$status; sleeping before retry" >&2
    fi
    cycle_elapsed_seconds=$(($(date +%s) - cycle_start_epoch))
    sleep_seconds=$((INTERVAL_SECONDS - cycle_elapsed_seconds))
    if [[ "$sleep_seconds" -gt 0 ]]; then
        sleep "$sleep_seconds"
    else
        echo "event demo cycle elapsed=${cycle_elapsed_seconds}s exceeded interval=${INTERVAL_SECONDS}s; starting next cycle immediately" >&2
    fi
done
