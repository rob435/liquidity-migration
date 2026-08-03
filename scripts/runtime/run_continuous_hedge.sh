#!/usr/bin/env bash
# Periodic BTC+ETH hedge target for the explicit demo account route.
# HEDGE_ACTION defaults to dry-run; only HEDGE_ACTION=execute publishes targets
# to the owner inbox.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python)"

# The route this hedge publishes onto is EXECUTION_ENVIRONMENT plus its owner
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
    --operational-profile-file "${ACCOUNT_RISK_POLICY_FILE:-}"
)
exec "$PYTHON_BIN" scripts/runtime/run_continuous_hedge.py "${args[@]}"
