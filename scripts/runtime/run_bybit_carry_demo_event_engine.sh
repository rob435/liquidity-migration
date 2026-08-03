#!/usr/bin/env bash
# Carry-hold demo sleeve — daily-decision target producer on a 60s diff loop.
#
# Whether it runs is toggled per-sleeve in deploy/sleeves.env. The paper service
# uses the same runner with EXECUTION_ENVIRONMENT=paper and MARKET_FOLLOW_ROOT
# pointed at the demo root, reading the leader's kline + carry_funding_events
# caches instead of REST. The account owners handle execution and fills.
#
# The daemon wakes every INTERVAL_SECONDS, but the decision is DAILY: computed
# for the current 00:00 UTC boundary once klines allow (00:20); every other
# cycle is an idempotent diff against the standing book.
#
# EXECUTION_ENVIRONMENT is explicit and requires its account-owner route. Sizing
# comes from the shared operational profile (ACCOUNT_RISK_POLICY_FILE); the rule
# is configs/lane2_carry_hold_v3.json.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

# The route a producer publishes onto is EXECUTION_ENVIRONMENT plus its owner
# roots. The kernel-latch variables only restated what the unit files already
# hard-code, so they are no longer re-derived or cross-checked here.
case "${EXECUTION_ENVIRONMENT:-}" in
    demo | paper) ;;
    mainnet)
        # The unit strips these; fail loudly if the strip ever misses.
        if [[ -n "${BYBIT_REAL_API_KEY:-}${BYBIT_REAL_API_SECRET:-}${BYBIT_DEMO_API_KEY:-}${BYBIT_DEMO_API_SECRET:-}" ]]; then
            echo "A target producer must not receive venue credentials." >&2
            exit 2
        fi
        case "$(printf '%s' "${REAL_MONEY:-}" | tr '[:upper:]' '[:lower:]')" in
            ""|0|false|no|off) ;;
            *)
                echo "A target producer must not receive REAL_MONEY; it submits no orders." >&2
                exit 2
                ;;
        esac
        ;;
    *)
        echo "EXECUTION_ENVIRONMENT must be explicitly set to demo, paper, or mainnet." >&2
        exit 2
        ;;
esac
if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then
    echo "EXECUTION_ENVIRONMENT requires ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT." >&2
    exit 2
fi

CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-carry-demo-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
# LOOKBACK_DAYS carries the fleet-wide env name; for carry it is the stateless
# hysteresis replay window (engine floor 45d, deployed default 90d).
LOOKBACK_DAYS="${LOOKBACK_DAYS:-90}"
WORKERS="${WORKERS:-4}"
OPERATIONAL_PROFILE_FILE="${ACCOUNT_RISK_POLICY_FILE:-}"
if [[ -z "$OPERATIONAL_PROFILE_FILE" || ! -f "$OPERATIONAL_PROFILE_FILE" ]]; then
    echo "ACCOUNT_RISK_POLICY_FILE must name the shared operational profile." >&2
    exit 2
fi

target_route_args=(
    --execution-environment "$EXECUTION_ENVIRONMENT"
    --account-intent-inbox-root "$ACCOUNT_INTENT_INBOX_ROOT"
    --account-execution-root "$ACCOUNT_EXECUTION_ROOT"
    --risk-policy-file "$OPERATIONAL_PROFILE_FILE"
)
if [[ -n "${STRATEGY_TARGET_CAPTURE_PATH:-}" ]]; then
    target_route_args+=(--strategy-target-capture-path "$STRATEGY_TARGET_CAPTURE_PATH")
fi
if [[ -n "${CANDIDATE_UNIVERSE_FILE:-}" ]]; then
    target_route_args+=(--candidate-universe-file "$CANDIDATE_UNIVERSE_FILE")
fi
# MARKET_FOLLOW_ROOT: the paper shadow reads the demo root's kline and settled-
# funding caches read-only instead of running a second REST plane, giving one
# market-data plane per box and identical decision inputs. Empty = this sleeve
# fetches its own data (the demo leader).
if [[ -n "${MARKET_FOLLOW_ROOT:-}" ]]; then
    if [[ "$MARKET_FOLLOW_ROOT" == "$DATA_ROOT" ]]; then
        # A follower never writes the caches, so self-following freezes its
        # market view permanently.
        echo "MARKET_FOLLOW_ROOT must not equal DATA_ROOT (circular self-follow)." >&2
        exit 2
    fi
    target_route_args+=(--market-follow-root "$MARKET_FOLLOW_ROOT")
fi
echo "carry target producer: execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS operational_profile=$OPERATIONAL_PROFILE_FILE market_follow_root=${MARKET_FOLLOW_ROOT:-}"
exec "$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    carry-demo-cycle \
    --replay-days "$LOOKBACK_DAYS" \
    --workers "$WORKERS" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${target_route_args[@]+"${target_route_args[@]}"}
