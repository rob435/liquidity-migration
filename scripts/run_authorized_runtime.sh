#!/usr/bin/env bash
# Verify the exact inherited environment, then replace this wrapper with the
# registered workload for UNIT/ENTRYPOINT. Callers cannot append argv; the
# authorized commit owns each complete command line.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: run_authorized_runtime.sh UNIT ENTRYPOINT" >&2
    exit 2
fi

UNIT="$1"
ENTRYPOINT="$2"

case "$UNIT:$ENTRYPOINT" in
    liquidity-migration-account-execution.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/run_account_execution_service.sh)
        ;;
    liquidity-migration-account-execution.service:readiness)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            -m liquidity_migration.account_owner_readiness
            --environment demo
            --account-root "${ACCOUNT_EXECUTION_ROOT:?ACCOUNT_EXECUTION_ROOT is required}"
            --inbox-root "${ACCOUNT_INTENT_INBOX_ROOT:?ACCOUNT_INTENT_INBOX_ROOT is required}"
            --capture-root "${ACCOUNT_CAPTURE_ROOT:?ACCOUNT_CAPTURE_ROOT is required}"
            --expected-invocation-id "${INVOCATION_ID:?INVOCATION_ID is required}"
            --timeout-seconds 180
            --max-age-seconds 30
        )
        ;;
    liquidity-migration-account-execution-mainnet.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/run_account_execution_service.sh)
        ;;
    liquidity-migration-account-execution-mainnet.service:readiness)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            -m liquidity_migration.account_owner_readiness
            --environment mainnet
            --account-root "${ACCOUNT_EXECUTION_ROOT:?ACCOUNT_EXECUTION_ROOT is required}"
            --inbox-root "${ACCOUNT_INTENT_INBOX_ROOT:?ACCOUNT_INTENT_INBOX_ROOT is required}"
            --capture-root "${ACCOUNT_CAPTURE_ROOT:?ACCOUNT_CAPTURE_ROOT is required}"
            --expected-invocation-id "${INVOCATION_ID:?INVOCATION_ID is required}"
            --timeout-seconds 180
            --max-age-seconds 30
        )
        ;;
    liquidity-migration-account-paper-execution.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/run_account_paper_execution_service.sh)
        ;;
    liquidity-migration-account-paper-execution.service:readiness)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            -m liquidity_migration.account_owner_readiness
            --environment paper
            --account-root "${ACCOUNT_EXECUTION_ROOT:?ACCOUNT_EXECUTION_ROOT is required}"
            --inbox-root "${ACCOUNT_INTENT_INBOX_ROOT:?ACCOUNT_INTENT_INBOX_ROOT is required}"
            --capture-root "${ACCOUNT_PAPER_CAPTURE_ROOT:?ACCOUNT_PAPER_CAPTURE_ROOT is required}"
            --expected-invocation-id "${INVOCATION_ID:?INVOCATION_ID is required}"
            --timeout-seconds 180
            --max-age-seconds 30
        )
        ;;
    liquidity-migration-bybit-long-demo.service:main | \
    liquidity-migration-bybit-long-paper.service:main | \
    liquidity-migration-bybit-long-mainnet.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/run_bybit_long_demo_event_engine.sh)
        ;;
    liquidity-migration-bybit-continuous-demo.service:main | \
    liquidity-migration-bybit-continuous-paper.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/run_bybit_continuous_demo_event_engine.sh)
        ;;
    liquidity-migration-bybit-carry-demo.service:main | \
    liquidity-migration-bybit-carry-paper.service:main | \
    liquidity-migration-bybit-carry-mainnet.service:main)
        # The sleeve contract names the paper follow root CARRY_MARKET_FOLLOW_ROOT
        # (sleeve-scoped, greppable beside KLINES_FOLLOW_ROOT); the carry runner
        # consumes the generic MARKET_FOLLOW_ROOT. Map it here so the authorized
        # commit owns the translation; the demo unit sets neither.
        if [ -n "${CARRY_MARKET_FOLLOW_ROOT:-}" ]; then
            MARKET_FOLLOW_ROOT="$CARRY_MARKET_FOLLOW_ROOT"
            export MARKET_FOLLOW_ROOT
        fi
        COMMAND=(/opt/liquidity-migration/scripts/run_bybit_carry_demo_event_engine.sh)
        ;;
    liquidity-migration-continuous-hedge.service:main)
        COMMAND=(/bin/bash /opt/liquidity-migration/scripts/run_continuous_hedge.sh)
        ;;
    liquidity-migration-continuous-rmom-refresh.service:main)
        COMMAND=(/bin/bash /opt/liquidity-migration/scripts/run_continuous_rmom_refresh.sh)
        ;;
    liquidity-migration-demo-liveness.service:main)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            scripts/check_demo_liveness.py
            --account-scope "${ACCOUNT_LIVENESS_SCOPE:?ACCOUNT_LIVENESS_SCOPE is required}"
            --account-paper-environment-file /etc/liquidity-migration/account-paper-execution.env
            --max-cycle-age-min 10
            --cooldown-min 360
            --telegram
        )
        ;;
    *)
        echo "unregistered authorized runtime entrypoint: $UNIT:$ENTRYPOINT" >&2
        exit 2
        ;;
esac

/opt/liquidity-migration/.venv/bin/python \
    -m liquidity_migration.operational_runtime_authority verify-runtime \
    --receipt /etc/liquidity-migration/account-execution-operational-ready \
    --repo-root /opt/liquidity-migration \
    --unit "$UNIT"

exec "${COMMAND[@]}"
