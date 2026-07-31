#!/usr/bin/env bash
# Take one account owner's book to flat, on the host, through its own inbox.
#
# This resolves the account roots from the owner's private systemd environment
# file and hands them to `liquidity_migration.ops.account_flatten`, which
# publishes zero targets and watches the journal. It places no order itself and
# it does not stop any unit: a producer left running can republish a nonzero
# target while this is converging, and the module reports that rather than
# fighting it. Stop the producing sleeve first.
#
# Dry run unless --execute is passed.

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
cd "$REPO_DIR"

PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || { echo "missing deployed Python environment" >&2; exit 1; }

ENVIRONMENT=""
OPERATOR="${SUDO_USER:-${USER:-operator}}"
REASON=""
EXECUTE=0
declare -a PASSTHROUGH=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --environment) ENVIRONMENT="${2:-}"; shift 2 ;;
        --operator) OPERATOR="${2:-}"; shift 2 ;;
        --reason) REASON="${2:-}"; shift 2 ;;
        --execute) EXECUTE=1; shift ;;
        --symbol|--sleeve|--wait-seconds|--poll-seconds|--account-id)
            PASSTHROUGH+=("$1" "${2:-}"); shift 2 ;;
        *) echo "unknown flatten argument: $1" >&2; exit 2 ;;
    esac
done

# The paper owner is unprivileged and owns its own route manifests and inbox.
# Flatten runs as that owner rather than as root, so every file it writes is
# already claimable and no ownership exception is needed anywhere.
RUN_AS=""
case "$ENVIRONMENT" in
    demo) ENVIRONMENT_FILE=/etc/liquidity-migration/account-execution.env ;;
    paper)
        ENVIRONMENT_FILE=/etc/liquidity-migration/account-paper-execution.env
        RUN_AS=liquidity-migration-paper
        ;;
    mainnet) ENVIRONMENT_FILE=/etc/liquidity-migration/account-execution-mainnet.env ;;
    *)
        echo "--environment must be named explicitly: demo, paper, or mainnet" >&2
        exit 2
        ;;
esac
[ -n "$REASON" ] || { echo "--reason is required" >&2; exit 2; }
[ -f "$ENVIRONMENT_FILE" ] || {
    echo "no owner environment for '$ENVIRONMENT' at $ENVIRONMENT_FILE" >&2
    exit 2
}

# shellcheck source=../../deploy/lib_systemd_environment.sh
. deploy/lib_systemd_environment.sh
unset ACCOUNT_EXECUTION_ROOT ACCOUNT_INTENT_INBOX_ROOT
lm_load_private_systemd_environment "$PYTHON" "$ENVIRONMENT_FILE" \
    ACCOUNT_EXECUTION_ROOT ACCOUNT_INTENT_INBOX_ROOT
[ -n "${ACCOUNT_EXECUTION_ROOT:-}" ] || { echo "account root is unavailable" >&2; exit 1; }
[ -n "${ACCOUNT_INTENT_INBOX_ROOT:-}" ] || { echo "inbox root is unavailable" >&2; exit 1; }

declare -a ARGS=(
    --account-root "$ACCOUNT_EXECUTION_ROOT"
    --inbox-root "$ACCOUNT_INTENT_INBOX_ROOT"
    --environment "$ENVIRONMENT"
    --operator "$OPERATOR"
    --reason "$REASON"
)
[ "$EXECUTE" -eq 1 ] && ARGS+=(--execute)
ARGS+=(${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"})

if [ -n "$RUN_AS" ] && [ "$(id -un)" != "$RUN_AS" ]; then
    command -v runuser >/dev/null 2>&1 || { echo "runuser is unavailable" >&2; exit 1; }
    exec runuser -u "$RUN_AS" -- "$PYTHON" -m liquidity_migration.ops.account_flatten "${ARGS[@]}"
fi
exec "$PYTHON" -m liquidity_migration.ops.account_flatten "${ARGS[@]}"
