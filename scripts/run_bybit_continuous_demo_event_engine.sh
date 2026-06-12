#!/usr/bin/env bash
# Continuous-fade demo sleeve — SUB-HOURLY, ticker-driven, SEPARATE ledger.
#
# Continuous fade runner. The order-submitting demo service uses this script; whether
# it runs is toggled per-sleeve in deploy/sleeves.env (CONTINUOUS_SLEEVE — the single
# source of truth, don't hardcode its state here). The paper service uses the same
# runner with SUBMIT_ORDERS=0, RECORD_DRY_RUN=1, PAPER_MODE=1. Order-link prefix lm-en-c-*
# lets ws_risk route any continuous fills to data/bybit-continuous-demo-event. "No 1h":
# the decile is recomputed off the live ticker price every INTERVAL_SECONDS heartbeat,
# not gated on the hourly bar close.
#
# Hard gates: SUBMIT_ORDERS=1 requires CONFIRM_DEMO_ORDERS=1. Demo account only.
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
MAX_HOLD_HOURS="${MAX_HOLD_HOURS:-48}"
# 2026-06-10: the live default is the validated winner_base 4-component ensemble
# (frozen receipt weights). The old single-component profiles remain selectable
# via the env for rollback/diagnostics only.
STRATEGY_PROFILE="${STRATEGY_PROFILE:-continuous_ensemble_v1}"
FEATURE_SET="${FEATURE_SET:-rv_168h,vov,dist_low,xsret7,xsret3}"
ENTRY_EVENT_TRIGGER="${ENTRY_EVENT_TRIGGER:-none}"
BTC_TREND_GATE="${BTC_TREND_GATE:-off}"
STOP_LOSS_PCT="${STOP_LOSS_PCT:-0.25}"  # wide server-side disaster stop; the state exit is profit-only
ENTRY_LEVERAGE="${ENTRY_LEVERAGE:-2}"
PER_POSITION_NOTIONAL_PCT_EQUITY="${PER_POSITION_NOTIONAL_PCT_EQUITY:-2}"
LIQ_TURNOVER_MIN="${LIQ_TURNOVER_MIN:-500000}"
FALLBACK_EQUITY_USDT="${FALLBACK_EQUITY_USDT:-10000}"
DAILY_REBALANCE_ENABLED="${DAILY_REBALANCE_ENABLED:-0}"
DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS="${DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS:-90}"
DAILY_REBALANCE_TARGET_DAILY_VOL="${DAILY_REBALANCE_TARGET_DAILY_VOL:-0.025}"
DAILY_REBALANCE_MAX_SCALE="${DAILY_REBALANCE_MAX_SCALE:-4}"
DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD="${DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD:--0.04}"
DAILY_REBALANCE_RESIZE_COST_BPS="${DAILY_REBALANCE_RESIZE_COST_BPS:-10}"
DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS="${DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS:-180}"
DAILY_REBALANCE_STRATEGY_MOMENTUM_MIN_RETURN="${DAILY_REBALANCE_STRATEGY_MOMENTUM_MIN_RETURN:-0.02}"
DAILY_REBALANCE_STRATEGY_MOMENTUM_SCALE_WHEN_BELOW="${DAILY_REBALANCE_STRATEGY_MOMENTUM_SCALE_WHEN_BELOW:-0}"

order_args=()
if [[ "${SUBMIT_ORDERS:-0}" == "1" ]]; then
    if [[ "${CONFIRM_DEMO_ORDERS:-0}" != "1" ]]; then
        echo "Set CONFIRM_DEMO_ORDERS=1 with SUBMIT_ORDERS=1 to submit Bybit demo orders." >&2
        exit 2
    fi
    order_args+=(--submit-orders --confirm-demo-orders)
fi
# PAPER_MODE=1 routes writes to continuous_fade_paper_* datasets so
# reconcile-continuous-paper-demo can pair this shadow against the live demo
# ledger. It REQUIRES record_dry_run and forbids submit_orders (validated in
# _validate_continuous_demo_config) — same invariant as the long paper sleeve.
if [[ "${PAPER_MODE:-0}" == "1" ]]; then
    order_args+=(--paper-mode)
fi
[[ "${RECORD_DRY_RUN:-0}" == "1" ]] && order_args+=(--record-dry-run)
# Same fail-loud guard as the ws_risk/long runners: TELEGRAM_ENABLED=1 with a
# missing token/chat-id previously came up "healthy" with telegram silently
# broken on the one LIVE order-submitting daemon (audit 2026-06-12 round 3).
if [[ "${TELEGRAM_ENABLED:-0}" == "1" ]]; then
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID when TELEGRAM_ENABLED=1." >&2
        exit 2
    fi
    order_args+=(--telegram)
fi
if [[ "$DAILY_REBALANCE_ENABLED" == "1" ]]; then
    order_args+=(--daily-rebalance-enabled)
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
    order_args+=(--klines-follow-root "$KLINES_FOLLOW_ROOT")
fi
# S1 Amendment 6 sniper (Tier-2 demo candidate, wired 2026-06-10): resting PostOnly
# Sell limit at entry*(1+8%) per fresh short, quarter-size, disaster stop attached.
# Off by default; the operator arms it with CONTINUOUS_SNIPER=1.
[[ "${CONTINUOUS_SNIPER:-0}" == "1" ]] && order_args+=(--sniper-enabled)

echo "continuous-demo engine: data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS submit_orders=${SUBMIT_ORDERS:-0} profile=$STRATEGY_PROFILE klines_follow_root=${KLINES_FOLLOW_ROOT:-}"
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
    --stop-loss-pct "$STOP_LOSS_PCT" \
    --entry-leverage "$ENTRY_LEVERAGE" \
    --per-position-notional-pct-equity "$PER_POSITION_NOTIONAL_PCT_EQUITY" \
    --liq-turnover-min "$LIQ_TURNOVER_MIN" \
    --fallback-equity-usdt "$FALLBACK_EQUITY_USDT" \
    --daily-rebalance-realized-vol-window-days "$DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS" \
    --daily-rebalance-target-daily-vol "$DAILY_REBALANCE_TARGET_DAILY_VOL" \
    --daily-rebalance-max-scale "$DAILY_REBALANCE_MAX_SCALE" \
    --daily-rebalance-drawdown-half-threshold "$DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD" \
    --daily-rebalance-resize-cost-bps "$DAILY_REBALANCE_RESIZE_COST_BPS" \
    --daily-rebalance-strategy-momentum-window-days "$DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS" \
    --daily-rebalance-strategy-momentum-min-return "$DAILY_REBALANCE_STRATEGY_MOMENTUM_MIN_RETURN" \
    --daily-rebalance-strategy-momentum-scale-when-below "$DAILY_REBALANCE_STRATEGY_MOMENTUM_SCALE_WHEN_BELOW" \
    --daemon --interval-seconds "$INTERVAL_SECONDS" \
    ${order_args[@]+"${order_args[@]}"}
