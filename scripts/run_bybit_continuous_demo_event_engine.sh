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
    if [[ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]]; then
        echo "Kernel latch is set but account-execution-capture-enabled is absent." >&2
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
    if [[ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]]; then
        echo "Paper kernel latch is set but account-execution-capture-enabled is absent." >&2
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
MAX_HOLD_HOURS="${MAX_HOLD_HOURS:-24}"
# Default lifecycle: 3-component ensemble, inverse-vol component sizing, and
# fill-anchored TP/24h exits owned by the account execution service.
STRATEGY_PROFILE="${STRATEGY_PROFILE:-continuous_ensemble_v2}"
FEATURE_SET="${FEATURE_SET:-max_ret168}"
ENTRY_EVENT_TRIGGER="${ENTRY_EVENT_TRIGGER:-none}"
# Default to the DEPLOYED gate (uptrend) so a dropped env line cannot silently
# disable the 30d-BTC trend gate. The systemd units pin BTC_TREND_GATE explicitly;
# set it to "off" there (demo + paper together) for a plumbing test.
BTC_TREND_GATE="${BTC_TREND_GATE:-uptrend}"
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-2}"
NOTIONAL_MULTIPLIER="${NOTIONAL_MULTIPLIER:-1}"
PER_POSITION_NOTIONAL_PCT_EQUITY="${PER_POSITION_NOTIONAL_PCT_EQUITY:-2}"
SIZING_MODE="${SIZING_MODE:-inverse_vol}"
TARGET_VOL_PER_NAME="${TARGET_VOL_PER_NAME:-0.01}"
VOL_WEIGHT_CLAMP="${VOL_WEIGHT_CLAMP:-2}"
LIQ_TURNOVER_MIN="${LIQ_TURNOVER_MIN:-500000}"

order_args=(
    --execution-environment "$EXECUTION_ENVIRONMENT"
    --account-intent-inbox-root "$ACCOUNT_INTENT_INBOX_ROOT"
    --account-execution-root "$ACCOUNT_EXECUTION_ROOT"
)
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
    order_args+=(--klines-follow-root "$KLINES_FOLLOW_ROOT")
fi
echo "continuous target producer: execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS profile=$STRATEGY_PROFILE notional_x=$NOTIONAL_MULTIPLIER entry_leverage=$ENTRY_LEVERAGE klines_follow_root=${KLINES_FOLLOW_ROOT:-}"
exec "$PYTHON_BIN" -m liquidity_migration \
    --config "$CONFIG_PATH" \
    --data-root "$DATA_ROOT" \
    continuous-event-demo-cycle \
    --lookback-days "$LOOKBACK_DAYS" \
    --workers "$WORKERS" \
    --max-active "$MAX_ACTIVE" \
    --max-new-entries-per-cycle "$MAX_NEW_ENTRIES_PER_CYCLE" \
    --max-hold-hours "$MAX_HOLD_HOURS" \
    --strategy-profile "$STRATEGY_PROFILE" \
    --feature-set "$FEATURE_SET" \
    --entry-event-trigger "$ENTRY_EVENT_TRIGGER" \
    --btc-trend-gate "$BTC_TREND_GATE" \
    --entry-leverage "$ENTRY_LEVERAGE" \
    --notional-multiplier "$NOTIONAL_MULTIPLIER" \
    --per-position-notional-pct-equity "$PER_POSITION_NOTIONAL_PCT_EQUITY" \
    --sizing-mode "$SIZING_MODE" \
    --target-vol-per-name "$TARGET_VOL_PER_NAME" \
    --vol-weight-clamp "$VOL_WEIGHT_CLAMP" \
    --liq-turnover-min "$LIQ_TURNOVER_MIN" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${order_args[@]+"${order_args[@]}"}
