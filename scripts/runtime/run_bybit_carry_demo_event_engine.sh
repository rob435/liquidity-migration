#!/usr/bin/env bash
# Carry-hold demo sleeve — daily-decision target producer on a 60s diff loop.
#
# Whether it runs is toggled per-sleeve in deploy/sleeves.env. The account
# owner handles execution and fills.
#
# The daemon wakes every INTERVAL_SECONDS, but the decision is DAILY: computed
# for the current 00:00 UTC boundary once klines allow (00:20); every other
# cycle is an idempotent diff against the standing book.
#
# EXECUTION_ENVIRONMENT is explicit and requires its account-owner route. Sizing
# comes from the shared operational profile (ACCOUNT_RISK_POLICY_FILE); the rule
# file follows CARRY_STRATEGY_PROFILE (v7 trades v6's registered file with
# the pre-settlement exit read on top).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

# The route a producer publishes onto is EXECUTION_ENVIRONMENT plus its owner
# roots. The kernel-latch variables restate what the unit files already
# hard-code, so they are not re-derived or cross-checked here.
case "${EXECUTION_ENVIRONMENT:-}" in
    demo) ;;
    mainnet)
        if [[ "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-}" != "1" ]]; then
            echo "EXECUTION_ENVIRONMENT=mainnet requires ACCOUNT_EXECUTION_KERNEL_REQUIRED=1." >&2
            exit 2
        fi
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
        echo "EXECUTION_ENVIRONMENT must be explicitly set to demo or mainnet." >&2
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
# CARRY_STRATEGY_PROFILE selects the registered deployment, exactly like
# LONG_STRATEGY_PROFILE on the LONG units. Switching versions is this env
# line plus a registered config file — never a code edit.
CARRY_STRATEGY_PROFILE="${CARRY_STRATEGY_PROFILE:-v7}"
case "$CARRY_STRATEGY_PROFILE" in
    v3|v4|v6|v7) ;;
    *)
        echo "CARRY_STRATEGY_PROFILE must be v3, v4, v6 or v7, got: $CARRY_STRATEGY_PROFILE" >&2
        exit 2
        ;;
esac
# CARRY_EARLY_EXIT=1 sells an exiting name at the settled print that ends it
# instead of the next midnight (owner-directed 2026-08-19). Unset means off:
# the registered midnight exit clock.
CARRY_EARLY_EXIT="${CARRY_EARLY_EXIT:-0}"
case "$CARRY_EARLY_EXIT" in
    0|1) ;;
    *)
        echo "CARRY_EARLY_EXIT must be 0 or 1, got: $CARRY_EARLY_EXIT" >&2
        exit 2
        ;;
esac
# CARRY_DROP_EXIT=1 sells a held name the upcoming decision zeroes (universe
# rank, persistence cut, suspend) at the first post-midnight cycle instead of
# the 00:20 clock; entries keep that clock either way (owner-directed
# 2026-08-23). Unset means off.
CARRY_DROP_EXIT="${CARRY_DROP_EXIT:-0}"
case "$CARRY_DROP_EXIT" in
    0|1) ;;
    *)
        echo "CARRY_DROP_EXIT must be 0 or 1, got: $CARRY_DROP_EXIT" >&2
        exit 2
        ;;
esac
# WS klines are the primary bar source; REST covers gaps. 0 disables the
# stream and returns to REST-on-cycle.
WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"
WS_KLINES_BOOTSTRAP_WORKERS="${WS_KLINES_BOOTSTRAP_WORKERS:-16}"
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
if [[ "$WS_KLINES_ENABLED" == "1" ]]; then
    target_route_args+=(--ws-klines-enabled)
    target_route_args+=(--ws-klines-bootstrap-workers "$WS_KLINES_BOOTSTRAP_WORKERS")
else
    target_route_args+=(--no-ws-klines)
fi
if [[ "$CARRY_EARLY_EXIT" == "1" ]]; then
    target_route_args+=(--early-exit)
else
    target_route_args+=(--no-early-exit)
fi
if [[ "$CARRY_DROP_EXIT" == "1" ]]; then
    target_route_args+=(--drop-exit)
else
    target_route_args+=(--no-drop-exit)
fi
echo "carry target producer: execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS operational_profile=$OPERATIONAL_PROFILE_FILE strategy_profile=$CARRY_STRATEGY_PROFILE early_exit=$CARRY_EARLY_EXIT drop_exit=$CARRY_DROP_EXIT"
exec "$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    carry-demo-cycle \
    --strategy-profile "$CARRY_STRATEGY_PROFILE" \
    --replay-days "$LOOKBACK_DAYS" \
    --workers "$WORKERS" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${target_route_args[@]+"${target_route_args[@]}"}
