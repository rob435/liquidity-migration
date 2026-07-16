#!/usr/bin/env bash
# Periodic BTC+ETH hedge target for one explicit demo or paper account route.
# HEDGE_ACTION defaults to dry-run; only HEDGE_ACTION=execute publishes targets
# to the mandatory owner inbox. This launcher has no credentials/order authority.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

case "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-0}" in
    1|true|TRUE|yes|YES|on|ON) demo_kernel_required=1 ;;
    0|false|FALSE|no|NO|off|OFF|"") demo_kernel_required=0 ;;
    *) echo "Invalid ACCOUNT_EXECUTION_KERNEL_REQUIRED value." >&2; exit 2 ;;
esac
case "${ACCOUNT_PAPER_KERNEL_REQUIRED:-0}" in
    1|true|TRUE|yes|YES|on|ON) paper_kernel_required=1 ;;
    0|false|FALSE|no|NO|off|OFF|"") paper_kernel_required=0 ;;
    *) echo "Invalid ACCOUNT_PAPER_KERNEL_REQUIRED value." >&2; exit 2 ;;
esac
case "${EXECUTION_ENVIRONMENT:-}" in
    demo)
        if [[ "$demo_kernel_required" != "1" || "$paper_kernel_required" != "0" ]]; then
            echo "EXECUTION_ENVIRONMENT=demo requires only ACCOUNT_EXECUTION_KERNEL_REQUIRED=1." >&2
            exit 2
        fi
        ;;
    paper)
        if [[ "$paper_kernel_required" != "1" || "$demo_kernel_required" != "0" ]]; then
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
    echo "ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT are required." >&2
    exit 2
fi
PRIMARY_ROOT="${PRIMARY_ROOT:-data/bybit-continuous-demo-event}"
case "${CONTINUOUS_HEDGE_TIMER:-off}" in
    on|ON|On|1|true|TRUE|yes|YES) ;;
    *)
        echo "continuous hedge lifecycle is off; refusing to run armed hedge service." >&2
        exit 0
        ;;
esac
args=(
    --execution-environment "$EXECUTION_ENVIRONMENT"
    --primary-root "$PRIMARY_ROOT"
)
case "${HEDGE_ACTION:-dry-run}" in
    dry-run) ;;
    execute) args+=(--execute) ;;
    *) echo "HEDGE_ACTION must be dry-run or execute." >&2; exit 2 ;;
esac
args+=(
    --account-inbox-root "$ACCOUNT_INTENT_INBOX_ROOT"
    --account-root "$ACCOUNT_EXECUTION_ROOT"
)
exec "$PYTHON_BIN" scripts/run_continuous_hedge.py "${args[@]}"
