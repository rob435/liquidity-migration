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
    liquidity-migration-bybit-long-demo.service:main | \
    liquidity-migration-bybit-long-mainnet.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_bybit_long_demo_event_engine.sh)
        ;;
    liquidity-migration-bybit-carry-demo.service:main | \
    liquidity-migration-bybit-carry-mainnet.service:main)
        COMMAND=(/opt/liquidity-migration/scripts/runtime/run_bybit_carry_demo_event_engine.sh)
        ;;
    liquidity-migration-telegram-controls.service:main)
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            -m liquidity_migration.ops.telegram_controls
        )
        ;;
    liquidity-migration-engine.service:main)
        # A compiled binary, not a Python module, and installed outside the
        # checkout — but the reason for coming through here is the same as for
        # every other unit: the command line is committed and reviewed, so the
        # host's environment file can say "live" and cannot say anything else.
        COMMAND=(
            /opt/liquidity-migration-engine/bin/engine
            run
            --config "${ENGINE_CONFIG_FILE:?ENGINE_CONFIG_FILE is required}"
        )
        # Shadow computes and sends nothing, and is what the config already
        # says. The flag only ever turns shadow off; a config that says
        # `shadow = false` is live whatever this variable holds.
        case "${ENGINE_LIVE:-}" in
            on|ON|On|1|true|TRUE|True|yes|YES|Yes) COMMAND+=(--live) ;;
        esac
        ;;
    liquidity-migration-engine-mainnet.service:main)
        # The same binary and the same command line as the demo engine above.
        # What differs is entirely in the unit's environment files: which
        # config it reads, and therefore which venue adapter it selects. There
        # is no realm argument here on purpose — a realm named on a command
        # line is a realm somebody can mistype.
        #
        # Reaching the funded account still needs REAL_MONEY armed in
        # /etc/liquidity-migration/bybit-mainnet.env, by the owner. Without it
        # the engine refuses to build a mainnet gateway and exits saying so.
        COMMAND=(
            /opt/liquidity-migration-engine/bin/engine
            run
            --config "${ENGINE_CONFIG_FILE:?ENGINE_CONFIG_FILE is required}"
        )
        case "${ENGINE_LIVE:-}" in
            on|ON|On|1|true|TRUE|True|yes|YES|Yes) COMMAND+=(--live) ;;
        esac
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
    liquidity-migration-llm-ledger.service:main)
        # Forward-only research: nominates movers and journals LLM driver
        # judgments. Public market data only; no account, no orders.
        COMMAND=(
            /opt/liquidity-migration/.venv/bin/python
            scripts/research/llm_driver_ledger.py
            --once
            --ledger-dir /var/lib/liquidity-migration/llm-driver-ledger
        )
        ;;
    *)
        echo "unregistered authorized runtime entrypoint: $UNIT:$ENTRYPOINT" >&2
        exit 2
        ;;
esac

exec "${COMMAND[@]}"
