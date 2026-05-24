#!/usr/bin/env bash
# Long-sleeve (MultiStratV1, v11a uni10 sniper retrace 1%/6h fall-through)
# forward-testing engine. Runs on the same Bybit demo account as the short
# sleeve but with order-link prefix lm-en-l-* so the extended ws_risk routes
# fills back to the long ledger.
#
# Hard gates:
# - SUBMIT_ORDERS=1 requires STRATEGY_PROFILE=MultiStratV1 + CONFIRM_DEMO_ORDERS=1
# - TELEGRAM_ENABLED=1 requires BOT_TOKEN + CHAT_ID + Bybit API key/secret
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-long-demo-event}"
STRATEGY_PROFILE="${STRATEGY_PROFILE:-MultiStratV1}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
LOOKBACK_DAYS="${LOOKBACK_DAYS:-90}"
UNIVERSE_SIZE="${UNIVERSE_SIZE:-10}"
WORKERS="${WORKERS:-4}"
NOTIONAL_MULTIPLIER="${NOTIONAL_MULTIPLIER:-10}"
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-10}"
MAX_ORDER_NOTIONAL_PCT_EQUITY="${MAX_ORDER_NOTIONAL_PCT_EQUITY:-0}"
MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-5}"
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
    if [[ "$STRATEGY_PROFILE" != "MultiStratV1" ]]; then
        echo "Only STRATEGY_PROFILE=MultiStratV1 is allowed to submit long-sleeve demo entry orders." >&2
        exit 2
    fi
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_ORDERS=1 to submit Bybit demo orders." >&2
        exit 2
    fi
    order_args+=(--submit-orders --confirm-demo-orders)
fi

echo "long-native demo engine starting"
echo "repo=$REPO_ROOT"
echo "strategy_profile=$STRATEGY_PROFILE"
echo "data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0} use_daemon=${USE_DAEMON:-1}"
echo "per-position notional_multiplier=${NOTIONAL_MULTIPLIER}x entry_leverage=${ENTRY_LEVERAGE}x universe_size=${UNIVERSE_SIZE}"

mkdir -p "$DATA_ROOT/.locks"

# USE_DAEMON=1 (default): long-running Python process with WS execution
# router. Each cycle reuses the same execution event router so fill
# confirmations arrive in <30ms instead of REST-polling. SIGTERM drains the
# current cycle and exits cleanly (systemctl stop is safe).
if [[ "${USE_DAEMON:-1}" == "1" ]]; then
    echo "long-native demo engine: daemon mode"
    exec "$PYTHON_BIN" -m liquidity_migration \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        long-native-event-demo-cycle \
        --universe-size "$UNIVERSE_SIZE" \
        --lookback-days "$LOOKBACK_DAYS" \
        --workers "$WORKERS" \
        --notional-multiplier "$NOTIONAL_MULTIPLIER" \
        --entry-leverage "$ENTRY_LEVERAGE" \
        --max-order-notional-pct-equity "$MAX_ORDER_NOTIONAL_PCT_EQUITY" \
        --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
        --order-fill-confirm-seconds "$ORDER_FILL_CONFIRM_SECONDS" \
        --order-fill-poll-interval-seconds "$ORDER_FILL_POLL_INTERVAL_SECONDS" \
        --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
        --strategy-profile "$STRATEGY_PROFILE" \
        --daemon --interval-seconds "$INTERVAL_SECONDS" \
        "${telegram_args[@]}" \
        "${order_args[@]}"
fi

echo "long-native demo engine: legacy single-cycle loop (USE_DAEMON=1 enables daemon)"
while true; do
    cycle_start_epoch="$(date +%s)"
    set +e
    "$PYTHON_BIN" -m liquidity_migration \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        long-native-event-demo-cycle \
        --universe-size "$UNIVERSE_SIZE" \
        --lookback-days "$LOOKBACK_DAYS" \
        --workers "$WORKERS" \
        --notional-multiplier "$NOTIONAL_MULTIPLIER" \
        --entry-leverage "$ENTRY_LEVERAGE" \
        --max-order-notional-pct-equity "$MAX_ORDER_NOTIONAL_PCT_EQUITY" \
        --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
        --order-fill-confirm-seconds "$ORDER_FILL_CONFIRM_SECONDS" \
        --order-fill-poll-interval-seconds "$ORDER_FILL_POLL_INTERVAL_SECONDS" \
        --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
        --strategy-profile "$STRATEGY_PROFILE" \
        "${telegram_args[@]}" \
        "${order_args[@]}"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "long-native demo cycle failed with status=$status; sleeping before retry" >&2
    fi
    cycle_elapsed_seconds=$(($(date +%s) - cycle_start_epoch))
    sleep_seconds=$((INTERVAL_SECONDS - cycle_elapsed_seconds))
    if [[ "$sleep_seconds" -gt 0 ]]; then
        sleep "$sleep_seconds"
    else
        echo "long-native demo cycle elapsed=${cycle_elapsed_seconds}s exceeded interval=${INTERVAL_SECONDS}s; starting next cycle immediately" >&2
    fi
done
