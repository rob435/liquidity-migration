#!/usr/bin/env bash
# Continuous-fade demo sleeve — sub-hourly target producer.
#
# Continuous fade runner. The target-publishing demo service uses this script; whether
# it runs is toggled per-sleeve in deploy/sleeves.env (CONTINUOUS_SLEEVE — the single
# source of truth, don't hardcode its state here). The paper service uses the same
# runner with EXECUTION_ENVIRONMENT=paper. The shared account owners exclusively
# handle execution, fills, reconciliation, and notifications.
# The daemon wakes every INTERVAL_SECONDS, but the active continuous_ensemble_v2
# entries come from confirmed-bar +1h membership, not intra-hour live-decile membership.
#
# Hard gate: EXECUTION_ENVIRONMENT is explicit and requires its account-owner route.
# The signal needs a fresh data/bybit-continuous-demo-event/residual_momentum.parquet
# (the rmom gate); the continuous-rmom-refresh.timer keeps it current.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

case "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-0}" in
    1|true|TRUE|yes|YES|on|ON) kernel_required=1 ;;
    0|false|FALSE|no|NO|off|OFF|"") kernel_required=0 ;;
    *) echo "Invalid ACCOUNT_EXECUTION_KERNEL_REQUIRED value." >&2; exit 2 ;;
esac
if [[ "$kernel_required" == "1" ]]; then
    if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then
        echo "Kernel latch requires ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT." >&2
        exit 2
    fi
fi
case "${ACCOUNT_PAPER_KERNEL_REQUIRED:-0}" in
    1|true|TRUE|yes|YES|on|ON) paper_kernel_required=1 ;;
    0|false|FALSE|no|NO|off|OFF|"") paper_kernel_required=0 ;;
    *) echo "Invalid ACCOUNT_PAPER_KERNEL_REQUIRED value." >&2; exit 2 ;;
esac
if [[ "$paper_kernel_required" == "1" ]]; then
    if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then
        echo "Paper kernel latch requires ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT." >&2
        exit 2
    fi
fi

case "${EXECUTION_ENVIRONMENT:-}" in
    demo)
        if [[ "$kernel_required" != "1" || "$paper_kernel_required" != "0" ]]; then
            echo "EXECUTION_ENVIRONMENT=demo requires only ACCOUNT_EXECUTION_KERNEL_REQUIRED=1." >&2
            exit 2
        fi
        ;;
    paper)
        if [[ "$paper_kernel_required" != "1" || "$kernel_required" != "0" ]]; then
            echo "EXECUTION_ENVIRONMENT=paper requires only ACCOUNT_PAPER_KERNEL_REQUIRED=1." >&2
            exit 2
        fi
        ;;
    *)
        echo "EXECUTION_ENVIRONMENT must be explicitly set to demo or paper." >&2
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
MAX_ACTIVE="${MAX_ACTIVE:-25}"
MAX_NEW_ENTRIES_PER_CYCLE="${MAX_NEW_ENTRIES_PER_CYCLE:-5}"
# Default to the DEPLOYED gate (uptrend) so a dropped env line cannot silently
# disable the 30d-BTC trend gate. The systemd units pin BTC_TREND_GATE explicitly;
# set it to "off" there (demo + paper together) for a plumbing test.
BTC_TREND_GATE="${BTC_TREND_GATE:-uptrend}"
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-2}"
NOTIONAL_MULTIPLIER="${NOTIONAL_MULTIPLIER:-1}"
PER_POSITION_NOTIONAL_PCT_EQUITY="${PER_POSITION_NOTIONAL_PCT_EQUITY:-2}"

target_route_args=(
    --execution-environment "$EXECUTION_ENVIRONMENT"
    --account-intent-inbox-root "$ACCOUNT_INTENT_INBOX_ROOT"
    --account-execution-root "$ACCOUNT_EXECUTION_ROOT"
)
if [[ -n "${STRATEGY_TARGET_CAPTURE_PATH:-}" ]]; then
    target_route_args+=(--strategy-target-capture-path "$STRATEGY_TARGET_CAPTURE_PATH")
fi
if [[ -n "${CANDIDATE_UNIVERSE_FILE:-}" ]]; then
    target_route_args+=(--candidate-universe-file "$CANDIDATE_UNIVERSE_FILE")
fi
# KLINES_FOLLOW_ROOT: the paper shadow follows the demo root's flushed kline
# snapshot (+rmom gate) read-only instead of running a second WS pool — one
# shared market-data plane per box. Empty = this sleeve runs its own pool.
if [[ -n "${KLINES_FOLLOW_ROOT:-}" ]]; then
    if [[ "$KLINES_FOLLOW_ROOT" == "$DATA_ROOT" ]]; then
        # A follower never writes the snapshot — following your own root means a
        # permanently frozen kline store. Only the SHADOW sleeve sets this.
        echo "KLINES_FOLLOW_ROOT must not equal DATA_ROOT (circular self-follow)." >&2
        exit 2
    fi
    target_route_args+=(--klines-follow-root "$KLINES_FOLLOW_ROOT")
fi
echo "continuous target producer: execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS notional_x=$NOTIONAL_MULTIPLIER entry_leverage=$ENTRY_LEVERAGE klines_follow_root=${KLINES_FOLLOW_ROOT:-}"
exec "$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    continuous-event-demo-cycle \
    --lookback-days "$LOOKBACK_DAYS" \
    --workers "$WORKERS" \
    --max-active "$MAX_ACTIVE" \
    --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
    --btc-trend-gate "$BTC_TREND_GATE" \
    --entry-leverage "$ENTRY_LEVERAGE" \
    --notional-multiplier "$NOTIONAL_MULTIPLIER" \
    --per-position-notional-pct-equity "$PER_POSITION_NOTIONAL_PCT_EQUITY" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${target_route_args[@]+"${target_route_args[@]}"}
