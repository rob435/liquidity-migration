#!/usr/bin/env bash
# Long-sleeve (LongV11aDivWeekendVol, v11a uni50 sniper retrace 1%/6h fall-through)
# target producer. The account owner handles credentials, orders, fills, and
# Telegram. EXECUTION_ENVIRONMENT is explicit and requires its owner route.
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
    mainnet)
        if [[ "$kernel_required" != "1" || "$paper_kernel_required" != "0" ]]; then
            echo "EXECUTION_ENVIRONMENT=mainnet requires only ACCOUNT_EXECUTION_KERNEL_REQUIRED=1." >&2
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
        echo "EXECUTION_ENVIRONMENT must be explicitly set to demo, paper, or mainnet." >&2
        exit 2
        ;;
esac
if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then
    echo "EXECUTION_ENVIRONMENT requires ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT." >&2
    exit 2
fi

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
OPERATIONAL_PROFILE_FILE="${ACCOUNT_RISK_POLICY_FILE:-}"
if [[ -z "$OPERATIONAL_PROFILE_FILE" || ! -f "$OPERATIONAL_PROFILE_FILE" ]]; then
    echo "ACCOUNT_RISK_POLICY_FILE must name the shared operational profile." >&2
    exit 2
fi
WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"
KLINES_FOLLOW_ROOT="${KLINES_FOLLOW_ROOT:-}"
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

if [[ "${TELEGRAM_ENABLED:-0}" != "0" ]]; then
    echo "Sleeve Telegram is retired; the account execution owner owns notifications." >&2
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
if [[ -n "$KLINES_FOLLOW_ROOT" ]]; then
    if [[ "$KLINES_FOLLOW_ROOT" == "$DATA_ROOT" ]]; then
        echo "KLINES_FOLLOW_ROOT must not equal DATA_ROOT (circular self-follow)." >&2
        exit 2
    fi
    target_route_args+=(--klines-follow-root "$KLINES_FOLLOW_ROOT")
fi
echo "long-native target producer starting"
echo "repo=$REPO_ROOT"
echo "execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS use_daemon=${USE_DAEMON:-1} klines_follow_root=$KLINES_FOLLOW_ROOT"
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
