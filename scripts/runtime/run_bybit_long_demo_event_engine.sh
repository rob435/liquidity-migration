#!/usr/bin/env bash
# Long-sleeve target producer (uni50 sniper retrace 1%/6h fall-through).
# LONG_STRATEGY_PROFILE selects the registered profile: v11a
# (LongV11aDivWeekendVol) or v12 (LongV12WideStop, the 3x-ATR stop decayed to
# 1.5x after 48h). The Rust engine handles credentials, orders, fills, and
# Telegram. EXECUTION_ENVIRONMENT is explicit.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
# The Python resolver records that its CLI values came through this deployed
# environment-to-argument boundary, rather than claiming they were typed by an
# operator at the process command line.
export LONG_RUNTIME_CONFIG_SOURCE="scripts/runtime/run_bybit_long_demo_event_engine.sh"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Pinned Python runtime is unavailable: $PYTHON_BIN" >&2
    exit 2
fi

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

for required_name in DATA_ROOT INTERVAL_SECONDS USE_DAEMON LONG_STRATEGY_PROFILE LONG_ENGINE_TARGET_BOOK_PATH LONG_ENGINE_BOOK_STATE_PATH LIVENESS_ENGINE_HEARTBEAT_FILE EXPECTED_ENGINE_ACCOUNT_USER_ID OPERATIONAL_PROFILE_FILE PRODUCER_REALM; do
    if [[ -z "${!required_name:-}" ]]; then
        echo "$required_name is required: this producer supports only Rust target-book execution." >&2
        exit 2
    fi
done
[ "$PRODUCER_REALM" = "$EXECUTION_ENVIRONMENT" ] || {
    echo "PRODUCER_REALM must equal EXECUTION_ENVIRONMENT." >&2
    exit 2
}
export ENGINE_ACCOUNT_HEARTBEAT_FILE="$LIVENESS_ENGINE_HEARTBEAT_FILE"
if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "INTERVAL_SECONDS must be a non-negative integer number of seconds." >&2
    exit 2
fi
# Registered strategy profile; each value is a distinct persisted execution
# identity and must be selected explicitly by the service.
case "$LONG_STRATEGY_PROFILE" in
    v11a|v12) ;;
    *)
        echo "LONG_STRATEGY_PROFILE must be v11a or v12, got: $LONG_STRATEGY_PROFILE" >&2
        exit 2
        ;;
esac
if [[ -z "$OPERATIONAL_PROFILE_FILE" || ! -f "$OPERATIONAL_PROFILE_FILE" ]]; then
    echo "OPERATIONAL_PROFILE_FILE must name the shared operational profile." >&2
    exit 2
fi
ws_klines_args=()
if [[ -n "${WS_KLINES_ENABLED:-}" ]]; then
    case "$WS_KLINES_ENABLED" in
        1) ws_klines_args+=(--ws-klines-enabled) ;;
        0) ws_klines_args+=(--no-ws-klines) ;;
        *) echo "WS_KLINES_ENABLED must be 0 or 1." >&2; exit 2 ;;
    esac
fi
for mapping in \
    WS_KLINES_BOOTSTRAP_WORKERS:ws-klines-bootstrap-workers \
    WS_KLINES_LOOKBACK_DAYS:ws-klines-lookback-days \
    WS_KLINES_UNIVERSE_REFRESH_SECONDS:ws-klines-universe-refresh-seconds \
    WS_KLINES_TOPICS_PER_CONNECTION:ws-klines-topics-per-connection \
    WS_KLINES_STALE_WARNING_SECONDS:ws-klines-stale-warning-seconds \
    WS_KLINES_STALE_RECONNECT_SECONDS:ws-klines-stale-reconnect-seconds; do
    variable="${mapping%%:*}"
    flag="${mapping#*:}"
    if [[ -n "${!variable:-}" ]]; then
        ws_klines_args+=("--$flag" "${!variable}")
    fi
done

cycle_args=()
for mapping in \
    UNIVERSE_SUPERSET_SIZE:universe-superset-size \
    LOOKBACK_DAYS:lookback-days \
    WORKERS:workers \
    DATA_NAME:data-name \
    MIN_CYCLE_INTERVAL_SECONDS:min-cycle-interval-seconds \
    TICKER_RECONCILE_INTERVAL_SECONDS:ticker-reconcile-interval-seconds \
    STATE_CACHE_STALE_SECONDS:state-cache-stale-seconds; do
    variable="${mapping%%:*}"
    flag="${mapping#*:}"
    if [[ -n "${!variable:-}" ]]; then
        cycle_args+=("--$flag" "${!variable}")
    fi
done
if [[ -n "${EVENT_DRIVEN_CYCLE:-}" ]]; then
    case "$EVENT_DRIVEN_CYCLE" in
        1) cycle_args+=(--event-driven-cycle) ;;
        0) cycle_args+=(--no-event-driven-cycle) ;;
        *) echo "EVENT_DRIVEN_CYCLE must be 0 or 1." >&2; exit 2 ;;
    esac
fi

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
echo "execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS use_daemon=$USE_DAEMON"
echo "sizing/account risk profile=$OPERATIONAL_PROFILE_FILE"

mkdir -p "$DATA_ROOT/.locks"

# USE_DAEMON=1 (default): long-running producer reusing one public market-data
# plane. SIGTERM drains the current cycle, so `systemctl stop` is safe.
if [[ "$USE_DAEMON" == "1" ]]; then
    echo "long-native demo engine: daemon mode"
    exec "$PYTHON_BIN" -m liquidity_migration \
        --data-root "$DATA_ROOT" \
        long-native-event-demo-cycle \
        --daemon --interval-seconds "$INTERVAL_SECONDS" \
        "${target_route_args[@]}" \
        "${cycle_args[@]}" \
        "${ws_klines_args[@]}"
fi
if [[ "$USE_DAEMON" != "0" ]]; then
    echo "USE_DAEMON must be 0 or 1." >&2
    exit 2
fi

echo "long-native demo engine: single-cycle loop (USE_DAEMON=1 enables daemon)"
while true; do
    cycle_start_epoch="$(date +%s)"
    set +e
    "$PYTHON_BIN" -m liquidity_migration \
        --data-root "$DATA_ROOT" \
        long-native-event-demo-cycle \
        --single-cycle --interval-seconds "$INTERVAL_SECONDS" \
        "${target_route_args[@]}" \
        "${cycle_args[@]}" \
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
