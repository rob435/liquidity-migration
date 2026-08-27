#!/usr/bin/env bash
# Long-sleeve target producer (uni50 sniper retrace 1%/6h fall-through).
# LONG_STRATEGY_PROFILE selects the registered profile: v11a
# (LongV11aDivWeekendVol) or v12 (LongV12WideStop, the 3x-ATR stop decayed to
# 1.5x after 48h). The account owner handles credentials, orders, fills, and
# Telegram. EXECUTION_ENVIRONMENT is explicit and requires its owner route.
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

for required_name in LONG_ENGINE_TARGET_BOOK_PATH LONG_ENGINE_BOOK_STATE_PATH LIVENESS_ENGINE_HEARTBEAT_FILE EXPECTED_ENGINE_ACCOUNT_USER_ID; do
    if [[ -z "${!required_name:-}" ]]; then
        echo "$required_name is required: this producer supports only Rust target-book execution." >&2
        exit 2
    fi
done
CONFIG_PATH="${CONFIG_PATH:-configs/volume_alpha.default.yaml}"
DATA_ROOT="${DATA_ROOT:-data/bybit-long-demo-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
# The engine requires at least 95 days for its factor windows.
LOOKBACK_DAYS="${LOOKBACK_DAYS:-100}"
WORKERS="${WORKERS:-4}"
# Registered strategy profile; each value is a distinct persisted execution
# identity. Unset defaults to v11a; anything else must match a registered name.
LONG_STRATEGY_PROFILE="${LONG_STRATEGY_PROFILE:-v11a}"
case "$LONG_STRATEGY_PROFILE" in
    v11a|v12) ;;
    *)
        echo "LONG_STRATEGY_PROFILE must be v11a or v12, got: $LONG_STRATEGY_PROFILE" >&2
        exit 2
        ;;
esac
OPERATIONAL_PROFILE_FILE="${ACCOUNT_RISK_POLICY_FILE:-}"
if [[ -z "$OPERATIONAL_PROFILE_FILE" || ! -f "$OPERATIONAL_PROFILE_FILE" ]]; then
    echo "ACCOUNT_RISK_POLICY_FILE must name the shared operational profile." >&2
    exit 2
fi
WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"
WS_KLINES_BOOTSTRAP_WORKERS="${WS_KLINES_BOOTSTRAP_WORKERS:-16}"
WS_KLINES_LOOKBACK_DAYS="${WS_KLINES_LOOKBACK_DAYS:-100}"
WS_KLINES_UNIVERSE_REFRESH_SECONDS="${WS_KLINES_UNIVERSE_REFRESH_SECONDS:-3600}"
WS_KLINES_TOPICS_PER_CONNECTION="${WS_KLINES_TOPICS_PER_CONNECTION:-180}"
WS_KLINES_STALE_WARNING_SECONDS="${WS_KLINES_STALE_WARNING_SECONDS:-60}"
WS_KLINES_STALE_RECONNECT_SECONDS="${WS_KLINES_STALE_RECONNECT_SECONDS:-180}"

ws_klines_args=()
if [[ "$WS_KLINES_ENABLED" == "1" ]]; then
    ws_klines_args+=(--ws-klines-enabled)
else
    ws_klines_args+=(--no-ws-klines)
fi
ws_klines_args+=(--ws-klines-bootstrap-workers "$WS_KLINES_BOOTSTRAP_WORKERS")
ws_klines_args+=(--ws-klines-lookback-days "$WS_KLINES_LOOKBACK_DAYS")
ws_klines_args+=(--ws-klines-universe-refresh-seconds "$WS_KLINES_UNIVERSE_REFRESH_SECONDS")
ws_klines_args+=(--ws-klines-topics-per-connection "$WS_KLINES_TOPICS_PER_CONNECTION")
ws_klines_args+=(--ws-klines-stale-warning-seconds "$WS_KLINES_STALE_WARNING_SECONDS")
ws_klines_args+=(--ws-klines-stale-reconnect-seconds "$WS_KLINES_STALE_RECONNECT_SECONDS")

target_route_args=(
    --strategy-profile "$LONG_STRATEGY_PROFILE"
    --execution-environment "$EXECUTION_ENVIRONMENT"

    --operational-profile-file "$OPERATIONAL_PROFILE_FILE"
)
if [[ -n "${STRATEGY_TARGET_CAPTURE_PATH:-}" ]]; then
    target_route_args+=(--strategy-target-capture-path "$STRATEGY_TARGET_CAPTURE_PATH")
fi
if [[ -n "${CANDIDATE_UNIVERSE_FILE:-}" ]]; then
    target_route_args+=(--candidate-universe-file "$CANDIDATE_UNIVERSE_FILE")
fi
echo "long-native target producer starting"
echo "repo=$REPO_ROOT"
echo "strategy_profile=$LONG_STRATEGY_PROFILE"
echo "execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS use_daemon=${USE_DAEMON:-1}"
echo "sizing/account risk profile=$OPERATIONAL_PROFILE_FILE"

mkdir -p "$DATA_ROOT/.locks"

# USE_DAEMON=1 (default): long-running producer reusing one public market-data
# plane. SIGTERM drains the current cycle, so `systemctl stop` is safe.
if [[ "${USE_DAEMON:-1}" == "1" ]]; then
    echo "long-native demo engine: daemon mode"
    exec "$PYTHON_BIN" -m liquidity_migration \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        long-native-event-demo-cycle \
        --lookback-days "$LOOKBACK_DAYS" \
        --workers "$WORKERS" \
        --daemon --interval-seconds "$INTERVAL_SECONDS" \
        "${target_route_args[@]}" \
        "${ws_klines_args[@]}"
fi

echo "long-native demo engine: single-cycle loop (USE_DAEMON=1 enables daemon)"
while true; do
    cycle_start_epoch="$(date +%s)"
    set +e
    "$PYTHON_BIN" -m liquidity_migration \
        --config "$CONFIG_PATH" \
        --data-root "$DATA_ROOT" \
        long-native-event-demo-cycle \
        --lookback-days "$LOOKBACK_DAYS" \
        --workers "$WORKERS" \
        "${target_route_args[@]}" \
        "${ws_klines_args[@]}"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
        echo "long-native demo cycle failed with status=$status; sleeping before retry" >&2
    fi
    if [[ "${RUN_ONCE:-0}" == "1" ]]; then
        exit "$status"
    fi
    cycle_elapsed_seconds=$(($(date +%s) - cycle_start_epoch))
    sleep_seconds=$((INTERVAL_SECONDS - cycle_elapsed_seconds))
    if [[ "$sleep_seconds" -gt 0 ]]; then
        sleep "$sleep_seconds"
    else
        echo "long-native demo cycle elapsed=${cycle_elapsed_seconds}s exceeded interval=${INTERVAL_SECONDS}s; starting next cycle immediately" >&2
    fi
done
