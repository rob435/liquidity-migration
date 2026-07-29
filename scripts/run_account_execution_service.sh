#!/usr/bin/env bash
# Ready-gated demo account execution owner. This script is inert until the
# operator has supplied verified demo rules and an explicit disaster-stop risk
# choice; it never falls back to public/mainnet minima or real-money settings.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

ACCOUNT_ROOT="${ACCOUNT_EXECUTION_ROOT:-}"
ACCOUNT_INTENT_INBOX_ROOT="${ACCOUNT_INTENT_INBOX_ROOT:-}"
ACCOUNT_CAPTURE_ROOT="${ACCOUNT_CAPTURE_ROOT:-}"
ACCOUNT_SYMBOLS_FILE="${ACCOUNT_SYMBOLS_FILE:-/etc/liquidity-migration/account-execution/symbols.txt}"
ACCOUNT_DEMO_RULES_FILE="${ACCOUNT_DEMO_RULES_FILE:-/etc/liquidity-migration/account-execution/demo-rules.json}"
ACCOUNT_RISK_POLICY_FILE="${ACCOUNT_RISK_POLICY_FILE:-/etc/liquidity-migration/account-execution/risk-policy.json}"
MAX_DEMO_RULE_AGE_HOURS="${MAX_DEMO_RULE_AGE_HOURS:-168}"
ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS="${ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS:-30}"
ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS="${ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS:-180}"
ACCOUNT_RAW_MARKET_PERSISTENCE="${ACCOUNT_RAW_MARKET_PERSISTENCE:-}"
CONTINUOUS_CYCLE_ROOT="${CONTINUOUS_CYCLE_ROOT:-}"
CONTINUOUS_CYCLE_MAX_AGE_MINUTES="${CONTINUOUS_CYCLE_MAX_AGE_MINUTES:-15}"

if [[ "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-}" != "1" ]]; then
    echo "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1 is required for the demo account owner." >&2
    exit 2
fi
if [[ -z "$ACCOUNT_ROOT" || -z "$ACCOUNT_INTENT_INBOX_ROOT" || -z "$ACCOUNT_CAPTURE_ROOT" ]]; then
    echo "ACCOUNT_EXECUTION_ROOT, ACCOUNT_INTENT_INBOX_ROOT, and ACCOUNT_CAPTURE_ROOT are required." >&2
    exit 2
fi
if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
    echo "CONFIRM_DEMO_ORDERS=1 is required for the demo account execution owner." >&2
    exit 2
fi
if [[ -z "${DISASTER_STOP_FRACTION:-}" ]]; then
    echo "Set an explicit DISASTER_STOP_FRACTION; no hidden default is allowed." >&2
    exit 2
fi
for required in "$ACCOUNT_SYMBOLS_FILE" "$ACCOUNT_DEMO_RULES_FILE" "$ACCOUNT_RISK_POLICY_FILE"; do
    if [[ ! -s "$required" ]]; then
        echo "Required account execution input is missing/empty: $required" >&2
        exit 2
    fi
done

case "$ACCOUNT_RAW_MARKET_PERSISTENCE" in
    1) raw_market_args=(--persist-raw-market) ;;
    0) raw_market_args=(--no-persist-raw-market) ;;
    *)
        echo "ACCOUNT_RAW_MARKET_PERSISTENCE must be explicitly set to 0 or 1." >&2
        exit 2
        ;;
esac

telegram_args=()
if [[ "${TELEGRAM_ENABLED:-0}" == "1" ]]; then
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Telegram is enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is missing." >&2
        exit 2
    fi
    telegram_args+=(--telegram)
fi

# A sleeve with no configured cycle root is not running (CONTINUOUS retired
# 2026-07-29). Pass nothing rather than a stale root, so the hourly Telegram
# digest carries no permanently STALE gate line for a retired sleeve.
continuous_cycle_args=()
if [[ -n "$CONTINUOUS_CYCLE_ROOT" ]]; then
    continuous_cycle_args+=(
        --continuous-cycle-root "$CONTINUOUS_CYCLE_ROOT"
        --continuous-cycle-max-age-minutes "$CONTINUOUS_CYCLE_MAX_AGE_MINUTES"
    )
fi

exec "$PYTHON_BIN" -m liquidity_migration.account_service_runner \
    --account-root "$ACCOUNT_ROOT" \
    --inbox-root "$ACCOUNT_INTENT_INBOX_ROOT" \
    --capture-root "$ACCOUNT_CAPTURE_ROOT" \
    --symbols-file "$ACCOUNT_SYMBOLS_FILE" \
    --demo-rules-file "$ACCOUNT_DEMO_RULES_FILE" \
    --risk-policy-file "$ACCOUNT_RISK_POLICY_FILE" \
    --max-demo-rule-age-hours "$MAX_DEMO_RULE_AGE_HOURS" \
    --request-market-warmup-timeout-seconds "$ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS" \
    --private-ws-reconnect-seconds "$ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS" \
    "${continuous_cycle_args[@]}" \
    "${raw_market_args[@]}" \
    --disaster-stop-fraction "$DISASTER_STOP_FRACTION" \
    --confirm-demo-orders \
    "${telegram_args[@]}"
