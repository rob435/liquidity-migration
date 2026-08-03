#!/usr/bin/env bash
# Continuous-fade demo sleeve — sub-hourly target producer.
#
# Whether it runs is toggled per-sleeve in deploy/sleeves.env (CONTINUOUS_SLEEVE).
# The account owner handles execution and fills.
#
# The daemon wakes every INTERVAL_SECONDS, but active continuous_ensemble_v2
# entries come from confirmed-bar +1h membership, not intra-hour live deciles.
#
# EXECUTION_ENVIRONMENT is explicit and requires its account-owner route. The
# signal needs a fresh data/bybit-continuous-demo-event/residual_momentum.parquet
# (the rmom gate), kept current by continuous-rmom-refresh.timer.
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
    demo) ;;
    *)
        echo "EXECUTION_ENVIRONMENT must be explicitly set to demo." >&2
        exit 2
        ;;
esac
if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then
    echo "EXECUTION_ENVIRONMENT requires ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT." >&2
    exit 2
fi

CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-continuous-demo-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
LOOKBACK_DAYS="${LOOKBACK_DAYS:-45}"
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
    --operational-profile-file "$OPERATIONAL_PROFILE_FILE"
)
if [[ -n "${STRATEGY_TARGET_CAPTURE_PATH:-}" ]]; then
    target_route_args+=(--strategy-target-capture-path "$STRATEGY_TARGET_CAPTURE_PATH")
fi
if [[ -n "${CANDIDATE_UNIVERSE_FILE:-}" ]]; then
    target_route_args+=(--candidate-universe-file "$CANDIDATE_UNIVERSE_FILE")
fi
echo "continuous target producer: execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS operational_profile=$OPERATIONAL_PROFILE_FILE"
exec "$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    continuous-event-demo-cycle \
    --lookback-days "$LOOKBACK_DAYS" \
    --workers "$WORKERS" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${target_route_args[@]+"${target_route_args[@]}"}
