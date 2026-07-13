#!/usr/bin/env bash
# Periodic BTC+ETH hedge target for the continuous demo book.
# Dry-run by default; SUBMIT_HEDGE=1 arms publication to the mandatory account
# owner inbox. This launcher has no venue credentials or order authority.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

if [[ "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-}" != "1" ]]; then
    echo "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1 is required for the hedge target publisher." >&2
    exit 2
fi
if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then
    echo "ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT are required." >&2
    exit 2
fi
if [[ ! -e /etc/liquidity-migration/account-execution-capture-enabled ]]; then
    echo "account-execution-capture-enabled is required for the hedge target publisher." >&2
    exit 2
fi
VENUE="${HEDGE_VENUE:-bybit}"
PRIMARY_ROOT="${PRIMARY_ROOT:-data/bybit-continuous-demo-event}"
case "${CONTINUOUS_HEDGE_TIMER:-off}" in
    on|ON|On|1|true|TRUE|yes|YES) ;;
    *)
        echo "continuous hedge lifecycle is off; refusing to run armed hedge service." >&2
        exit 0
        ;;
esac
args=(--venue "$VENUE" --primary-root "$PRIMARY_ROOT")
if [[ "${SUBMIT_HEDGE:-0}" == "1" ]]; then
    args+=(--submit)
fi
args+=(
    --account-inbox-root "$ACCOUNT_INTENT_INBOX_ROOT"
    --account-root "$ACCOUNT_EXECUTION_ROOT"
)
exec "$PYTHON_BIN" scripts/run_continuous_hedge.py "${args[@]}"
