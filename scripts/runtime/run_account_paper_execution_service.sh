#!/usr/bin/env bash
# Ready-gated paper account owner. All paper sleeves publish to this one inbox;
# only the integration-only uncalibrated twin mutates the canonical paper journal.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

ACCOUNT_ROOT="${ACCOUNT_EXECUTION_ROOT:-}"
ACCOUNT_INTENT_INBOX_ROOT="${ACCOUNT_INTENT_INBOX_ROOT:-}"
ACCOUNT_CAPTURE_ROOT="${ACCOUNT_PAPER_CAPTURE_ROOT:-}"
ACCOUNT_SYMBOLS_FILE="${ACCOUNT_SYMBOLS_FILE:-/etc/liquidity-migration/account-paper-execution/symbols.txt}"
ACCOUNT_DEMO_RULES_FILE="${ACCOUNT_DEMO_RULES_FILE:-/etc/liquidity-migration/account-paper-execution/demo-rules.json}"
ACCOUNT_RISK_POLICY_FILE="${ACCOUNT_RISK_POLICY_FILE:-/etc/liquidity-migration/account-paper-execution/risk-policy.json}"
# No fallback: the deploy path derives this from the committed operational
# profile's capital_reference_usdt (prepare_paper_runtime_boundary). A default
# would silently mis-scale the twin against the deployed reference.
PAPER_EQUITY_USDT="${PAPER_EQUITY_USDT:-}"
MAX_DEMO_RULE_AGE_HOURS="${MAX_DEMO_RULE_AGE_HOURS:-168}"
ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS="${ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS:-30}"
ACCOUNT_RAW_MARKET_PERSISTENCE="${ACCOUNT_RAW_MARKET_PERSISTENCE:-}"

if [[ "${ACCOUNT_PAPER_KERNEL_REQUIRED:-}" != "1" ]]; then
    echo "ACCOUNT_PAPER_KERNEL_REQUIRED=1 is required for the paper account owner." >&2
    exit 2
fi
if [[ -z "$ACCOUNT_ROOT" || -z "$ACCOUNT_INTENT_INBOX_ROOT" || -z "$ACCOUNT_CAPTURE_ROOT" ]]; then
    echo "ACCOUNT_EXECUTION_ROOT, ACCOUNT_INTENT_INBOX_ROOT, and ACCOUNT_PAPER_CAPTURE_ROOT are required." >&2
    exit 2
fi
if [[ -z "$PAPER_EQUITY_USDT" ]]; then
    echo "PAPER_EQUITY_USDT is required; it is written from the committed operational profile's capital_reference_usdt." >&2
    exit 2
fi
for required in "$ACCOUNT_SYMBOLS_FILE" "$ACCOUNT_DEMO_RULES_FILE" "$ACCOUNT_RISK_POLICY_FILE"; do
    if [[ ! -s "$required" ]]; then
        echo "Required paper account input is missing/empty: $required" >&2
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

continuous_cycle_args=()
if [[ -n "${CONTINUOUS_CYCLE_ROOT:-}" ]]; then
    continuous_cycle_args+=(--continuous-cycle-root "$CONTINUOUS_CYCLE_ROOT")
    continuous_cycle_args+=(
        --continuous-cycle-max-age-minutes "${CONTINUOUS_CYCLE_MAX_AGE_MINUTES:-15}"
    )
fi

exec "$PYTHON_BIN" -m liquidity_migration.runtime.account_paper_runner \
    --account-root "$ACCOUNT_ROOT" \
    --inbox-root "$ACCOUNT_INTENT_INBOX_ROOT" \
    --capture-root "$ACCOUNT_CAPTURE_ROOT" \
    --symbols-file "$ACCOUNT_SYMBOLS_FILE" \
    --demo-rules-file "$ACCOUNT_DEMO_RULES_FILE" \
    --risk-policy-file "$ACCOUNT_RISK_POLICY_FILE" \
    --equity-usdt "$PAPER_EQUITY_USDT" \
    --max-demo-rule-age-hours "$MAX_DEMO_RULE_AGE_HOURS" \
    --request-market-warmup-timeout-seconds "$ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS" \
    "${raw_market_args[@]}" \
    "${continuous_cycle_args[@]}" \
    "${telegram_args[@]}"
