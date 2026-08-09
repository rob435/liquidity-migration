#!/usr/bin/env bash
# Bybit account execution owner (demo or mainnet, per ACCOUNT_VENUE_REALM).
# Inert until the operator supplies verified demo rules and an explicit
# disaster-stop fraction; no defaults.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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
ACCOUNT_RAW_MARKET_PERSISTENCE="${ACCOUNT_RAW_MARKET_PERSISTENCE:-0}"
ACCOUNT_SHARED_LEVERAGE_AUTHORITY="${ACCOUNT_SHARED_LEVERAGE_AUTHORITY:-0}"
CONTINUOUS_CYCLE_ROOT="${CONTINUOUS_CYCLE_ROOT:-}"
CONTINUOUS_CYCLE_MAX_AGE_MINUTES="${CONTINUOUS_CYCLE_MAX_AGE_MINUTES:-15}"

ACCOUNT_VENUE_REALM="${ACCOUNT_VENUE_REALM:-demo}"
case "$ACCOUNT_VENUE_REALM" in
    demo) ACCOUNT_ID_DEFAULT="bybit-demo-unified" ;;
    mainnet) ACCOUNT_ID_DEFAULT="bybit-mainnet-unified" ;;
    *)
        echo "ACCOUNT_VENUE_REALM must be demo or mainnet; there is no default beyond demo." >&2
        exit 2
        ;;
esac
ACCOUNT_ID="${ACCOUNT_ID:-$ACCOUNT_ID_DEFAULT}"

if [[ -z "$ACCOUNT_ROOT" || -z "$ACCOUNT_INTENT_INBOX_ROOT" || -z "$ACCOUNT_CAPTURE_ROOT" ]]; then
    echo "ACCOUNT_EXECUTION_ROOT, ACCOUNT_INTENT_INBOX_ROOT, and ACCOUNT_CAPTURE_ROOT are required." >&2
    exit 2
fi
# The realm and the credentials must agree here, not only inside the client: a
# mainnet owner on demo keys would reconcile the wrong account. The demo arm
# re-checks nothing its own unit set two lines earlier.
case "$ACCOUNT_VENUE_REALM" in
    mainnet)
        if [[ "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-}" != "1" ]]; then
            echo "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1 is required for the mainnet account owner." >&2
            exit 2
        fi
        if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
            echo "CONFIRM_DEMO_ORDERS=1 is required for the mainnet account owner." >&2
            exit 2
        fi
        if [[ -z "${BYBIT_REAL_API_KEY:-}" || -z "${BYBIT_REAL_API_SECRET:-}" ]]; then
            echo "ACCOUNT_VENUE_REALM=mainnet requires BYBIT_REAL_API_KEY and BYBIT_REAL_API_SECRET." >&2
            exit 2
        fi
        # Vocabulary matches liquidity_migration/core/env_flags.py, which lower-cases
        # before comparing; keep the two in step or this gate crash-loops.
        case "$(printf '%s' "${REAL_MONEY:-}" | tr '[:upper:]' '[:lower:]')" in
            1|true|yes|on) ;;
            *)
                echo "ACCOUNT_VENUE_REALM=mainnet requires REAL_MONEY to be explicitly armed by the owner." >&2
                exit 2
                ;;
        esac
        ;;
    demo)
        if [[ -n "${BYBIT_REAL_API_KEY:-}${BYBIT_REAL_API_SECRET:-}" ]]; then
            echo "The demo owner must not receive mainnet credentials." >&2
            exit 2
        fi
        case "$(printf '%s' "${REAL_MONEY:-}" | tr '[:upper:]' '[:lower:]')" in
            ""|0|false|no|off) ;;
            *)
                echo "REAL_MONEY must be unset or explicitly false for the demo owner." >&2
                exit 2
                ;;
        esac
        ;;
esac
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

# Bulk raw-market persistence is a diagnostic, off unless asked for.
case "$ACCOUNT_RAW_MARKET_PERSISTENCE" in
    1) raw_market_args=(--persist-raw-market) ;;
    *) raw_market_args=(--no-persist-raw-market) ;;
esac

# Set this when somebody else also changes leverage on the account -- the owner
# trading the same account by hand. The adapter then drops a symbol's cached
# leverage when the symbol goes flat, so the next entry re-asserts it and pays
# one set_leverage round trip (~190 ms) rather than sizing against a leverage
# this process did not set. Unset means off, as it has been since 2026-08-08.
leverage_authority_args=()
case "$ACCOUNT_SHARED_LEVERAGE_AUTHORITY" in
    1) leverage_authority_args=(--shared-leverage-authority) ;;
esac

# A notification channel never keeps the account owner down: misconfigured
# Telegram degrades to no Telegram, and the owner still executes and protects.
telegram_args=()
if [[ "${TELEGRAM_ENABLED:-0}" == "1" ]]; then
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Telegram is enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is missing; running without Telegram." >&2
    else
        telegram_args+=(--telegram)
    fi
fi

# A sleeve with no configured cycle root is not running. Pass nothing rather
# than a stale root, so the Telegram digest carries no permanent STALE line.
continuous_cycle_args=()
if [[ -n "$CONTINUOUS_CYCLE_ROOT" ]]; then
    continuous_cycle_args+=(
        --continuous-cycle-root "$CONTINUOUS_CYCLE_ROOT"
        --continuous-cycle-max-age-minutes "$CONTINUOUS_CYCLE_MAX_AGE_MINUTES"
    )
fi

exec "$PYTHON_BIN" -m liquidity_migration.runtime.account_service_runner \
    --realm "$ACCOUNT_VENUE_REALM" \
    --account-id "$ACCOUNT_ID" \
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
    "${leverage_authority_args[@]}" \
    --disaster-stop-fraction "$DISASTER_STOP_FRACTION" \
    "${telegram_args[@]}"
