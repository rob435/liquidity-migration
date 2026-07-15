#!/usr/bin/env bash
# Ready-gated paper account owner. All paper sleeves publish to this one inbox;
# only the deterministic execution twin mutates the canonical paper journal.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

ACCOUNT_ROOT="${ACCOUNT_EXECUTION_ROOT:-}"
ACCOUNT_INTENT_INBOX_ROOT="${ACCOUNT_INTENT_INBOX_ROOT:-}"
ACCOUNT_CAPTURE_ROOT="${ACCOUNT_PAPER_CAPTURE_ROOT:-}"
ACCOUNT_SYMBOLS_FILE="${ACCOUNT_SYMBOLS_FILE:-/etc/liquidity-migration/account-paper-execution/symbols.txt}"
ACCOUNT_DEMO_RULES_FILE="${ACCOUNT_DEMO_RULES_FILE:-/etc/liquidity-migration/account-execution/demo-rules.json}"
ACCOUNT_RISK_POLICY_FILE="${ACCOUNT_RISK_POLICY_FILE:-/etc/liquidity-migration/account-paper-execution/risk-policy.json}"
ACCOUNT_TWIN_CALIBRATION_FILE="${ACCOUNT_TWIN_CALIBRATION_FILE:-}"
PAPER_EQUITY_USDT="${PAPER_EQUITY_USDT:-10000}"
MAX_DEMO_RULE_AGE_HOURS="${MAX_DEMO_RULE_AGE_HOURS:-168}"
ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS="${ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS:-30}"
ACCOUNT_TWIN_LATENCY_QUANTILE="${ACCOUNT_TWIN_LATENCY_QUANTILE:-p50}"
ACCOUNT_TWIN_SLIPPAGE_QUANTILE="${ACCOUNT_TWIN_SLIPPAGE_QUANTILE:-p50}"

if [[ "${ACCOUNT_PAPER_KERNEL_REQUIRED:-}" != "1" ]]; then
    echo "ACCOUNT_PAPER_KERNEL_REQUIRED=1 is required for paper-kernel cutover." >&2
    exit 2
fi
if [[ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]]; then
    echo "account-execution-capture-enabled is required for the paper account owner." >&2
    exit 2
fi
if [[ -z "$ACCOUNT_ROOT" || -z "$ACCOUNT_INTENT_INBOX_ROOT" || -z "$ACCOUNT_CAPTURE_ROOT" || -z "$ACCOUNT_TWIN_CALIBRATION_FILE" ]]; then
    echo "ACCOUNT_EXECUTION_ROOT, ACCOUNT_INTENT_INBOX_ROOT, ACCOUNT_PAPER_CAPTURE_ROOT, and ACCOUNT_TWIN_CALIBRATION_FILE are required." >&2
    exit 2
fi
for required in "$ACCOUNT_SYMBOLS_FILE" "$ACCOUNT_DEMO_RULES_FILE" "$ACCOUNT_RISK_POLICY_FILE" "$ACCOUNT_TWIN_CALIBRATION_FILE"; do
    if [[ ! -s "$required" ]]; then
        echo "Required paper account input is missing/empty: $required" >&2
        exit 2
    fi
done

exec "$PYTHON_BIN" -m liquidity_migration.account_paper_runner \
    --account-root "$ACCOUNT_ROOT" \
    --inbox-root "$ACCOUNT_INTENT_INBOX_ROOT" \
    --capture-root "$ACCOUNT_CAPTURE_ROOT" \
    --symbols-file "$ACCOUNT_SYMBOLS_FILE" \
    --demo-rules-file "$ACCOUNT_DEMO_RULES_FILE" \
    --risk-policy-file "$ACCOUNT_RISK_POLICY_FILE" \
    --calibration-file "$ACCOUNT_TWIN_CALIBRATION_FILE" \
    --latency-quantile "$ACCOUNT_TWIN_LATENCY_QUANTILE" \
    --slippage-quantile "$ACCOUNT_TWIN_SLIPPAGE_QUANTILE" \
    --equity-usdt "$PAPER_EQUITY_USDT" \
    --max-demo-rule-age-hours "$MAX_DEMO_RULE_AGE_HOURS" \
    --request-market-warmup-timeout-seconds "$ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS"
