#!/usr/bin/env bash
# Replace this wrapper with the registered workload for UNIT/ENTRYPOINT.
# Callers cannot append argv; the committed script owns each complete
# command line.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: run_authorized_runtime.sh UNIT ENTRYPOINT" >&2
    exit 2
fi

UNIT="$1"
ENTRYPOINT="$2"

case "$UNIT:$ENTRYPOINT" in
    liquidity-migration-account-execution.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_account_execution_service.sh)
        ;;
    liquidity-migration-account-execution.service:readiness)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            -m liquidity_migration.runtime.account_owner_readiness
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
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_account_execution_service.sh)
        ;;
    liquidity-migration-account-execution-mainnet.service:readiness)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            -m liquidity_migration.runtime.account_owner_readiness
            --environment mainnet
            --account-root "${ACCOUNT_EXECUTION_ROOT:?ACCOUNT_EXECUTION_ROOT is required}"
            --inbox-root "${ACCOUNT_INTENT_INBOX_ROOT:?ACCOUNT_INTENT_INBOX_ROOT is required}"
            --capture-root "${ACCOUNT_CAPTURE_ROOT:?ACCOUNT_CAPTURE_ROOT is required}"
            --expected-invocation-id "${INVOCATION_ID:?INVOCATION_ID is required}"
            --timeout-seconds 180
            --max-age-seconds 30
        )
        ;;
    liquidity-migration-bybit-long-demo.service:main | \
    liquidity-migration-bybit-long-mainnet.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_bybit_long_demo_event_engine.sh)
        ;;
    liquidity-migration-bybit-continuous-demo.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_bybit_continuous_demo_event_engine.sh)
        ;;
    liquidity-migration-bybit-carry-demo.service:main | \
    liquidity-migration-bybit-carry-mainnet.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_bybit_carry_demo_event_engine.sh)
        ;;
    liquidity-migration-continuous-hedge.service:main)
        COMMAND=(/bin/bash /opt/liquidity-migration/scripts/runtime/run_continuous_hedge.sh)
        ;;
    liquidity-migration-continuous-rmom-refresh.service:main)
        COMMAND=(/bin/bash /opt/liquidity-migration/scripts/runtime/run_continuous_rmom_refresh.sh)
        ;;
    liquidity-migration-demo-liveness.service:main)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            scripts/runtime/check_fleet_liveness.py
            --account-scope demo
            --max-cycle-age-min 10
            --cooldown-min 60
            --telegram
        )
        ;;
    liquidity-migration-mainnet-liveness.service:main)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            scripts/runtime/check_fleet_liveness.py
            --account-scope mainnet
            --carry-mainnet-root /opt/liquidity-migration/data/bybit-carry-mainnet-event
            --long-mainnet-root /opt/liquidity-migration/data/bybit-long-mainnet-event
            --max-cycle-age-min 10
            --cooldown-min 60
            --telegram
        )
        ;;
    *)
        echo "unregistered authorized runtime entrypoint: $UNIT:$ENTRYPOINT" >&2
        exit 2
        ;;
esac

exec "${COMMAND[@]}"
