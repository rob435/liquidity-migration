#!/usr/bin/env bash
# Exodus target producer: consume CARRY's durable event tape on a 60s loop.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Pinned Python runtime is unavailable: $PYTHON_BIN" >&2
    exit 2
fi

case "${EXECUTION_ENVIRONMENT:-}" in
    demo) ;;
    mainnet)
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

for required_name in EXODUS_EVENT_TAPE EXODUS_ENGINE_TARGET_BOOK_PATH LIVENESS_ENGINE_HEARTBEAT_FILE EXPECTED_ENGINE_ACCOUNT_USER_ID OPERATIONAL_PROFILE_FILE PRODUCER_REALM; do
    if [[ -z "${!required_name:-}" ]]; then
        echo "$required_name is required." >&2
        exit 2
    fi
done
[[ "$PRODUCER_REALM" == "$EXECUTION_ENVIRONMENT" ]] || {
    echo "PRODUCER_REALM must equal EXECUTION_ENVIRONMENT." >&2
    exit 2
}
[[ "$EXODUS_EVENT_TAPE" == /* ]] || {
    echo "EXODUS_EVENT_TAPE must be absolute." >&2
    exit 2
}
[[ "$EXODUS_ENGINE_TARGET_BOOK_PATH" == /* ]] || {
    echo "EXODUS_ENGINE_TARGET_BOOK_PATH must be absolute." >&2
    exit 2
}
[[ -f "$OPERATIONAL_PROFILE_FILE" ]] || {
    echo "OPERATIONAL_PROFILE_FILE must name the shared operational profile." >&2
    exit 2
}

export ENGINE_ACCOUNT_HEARTBEAT_FILE="$LIVENESS_ENGINE_HEARTBEAT_FILE"
DATA_ROOT="${DATA_ROOT:-data/bybit-exodus-event}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
EXODUS_SHORT_PROFILE="${EXODUS_SHORT_PROFILE:-v1}"
case "$EXODUS_SHORT_PROFILE" in
    v1) ;;
    *)
        echo "EXODUS_SHORT_PROFILE must be v1, got: $EXODUS_SHORT_PROFILE" >&2
        exit 2
        ;;
esac

echo "exodus target producer: execution_environment=$EXECUTION_ENVIRONMENT data_root=$DATA_ROOT interval_seconds=$INTERVAL_SECONDS strategy_profile=$EXODUS_SHORT_PROFILE"
exec "$PYTHON_BIN" -m liquidity_migration \
    --data-root "$DATA_ROOT" \
    exodus-cycle \
    --strategy-profile "$EXODUS_SHORT_PROFILE" \
    --event-tape "$EXODUS_EVENT_TAPE" \
    --target-book "$EXODUS_ENGINE_TARGET_BOOK_PATH" \
    --operational-profile-file "$OPERATIONAL_PROFILE_FILE" \
    --execution-environment "$EXECUTION_ENVIRONMENT" \
    --daemon --interval-seconds "$INTERVAL_SECONDS"
