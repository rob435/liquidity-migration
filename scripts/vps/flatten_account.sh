#!/usr/bin/env bash
# Reduce positions visible to the configured-symbol engine heartbeat.
#
# The engine reads one absolute target book per sleeve, and
# an absolute book that names nothing is a decision to hold nothing -- so this
# writes that book, for every sleeve, and lets the engine do the closing. The
# exits it produces are reduce-only, and reduce-only orders pass every gate the
# engine has: the boot latch exempts them, the risk kernel returns before its
# staleness and loss checks, and a book's expiry stops entries but never exits.
#
# Two things this must do that writing the file does not:
#
#   1. **Stop the producers first.** They rewrite their book every cycle, so a
#      running producer would undo this within a minute. Stopping the unit is
#      enough while the box stays up; the durable off-switch is the sleeve
#      toggle, and a deploy's activate will start a stopped sleeve again.
#   2. **Name every observed symbol at zero, not an empty list.** An empty book
#      only reaches names the plug already has in hand. The names come from the
#      engine heartbeat and are therefore limited to configured SymbolIds; this
#      command cannot see or attest unknown/delisted residual positions.
#
# This helper never resets producer state and never reports venue-global flat.
# A future reset requires an independently reviewed venue-global flat attestation.
#
# Dry run unless --execute, like every other mutating operator command here.

set -Eeuo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: flatten_account.sh --environment demo|mainnet [--reason TEXT] [--execute]

  Without --execute: say what would be written and stopped, change nothing.
  With --execute:    stop the producers, write a zero book per sleeve, and
                     wait for no configured-symbol positions. This does not
                      prove venue-global flatness or reset producer state.

  --wait-seconds N   how long to wait for flat (default 300)
USAGE
    exit 2
}

ENVIRONMENT=""
REASON="operator flatten"
EXECUTE=0
WAIT_SECONDS=300

while [ "$#" -gt 0 ]; do
    case "$1" in
        --environment) [ "$#" -ge 2 ] || usage; ENVIRONMENT="$2"; shift 2 ;;
        --reason)      [ "$#" -ge 2 ] || usage; REASON="$2"; shift 2 ;;
        --wait-seconds) [ "$#" -ge 2 ] || usage; WAIT_SECONDS="$2"; shift 2 ;;
        --execute)     EXECUTE=1; shift ;;
        --dry-run)     EXECUTE=0; shift ;;
        -h|--help)     usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

case "$ENVIRONMENT" in
    demo|mainnet) ;;
    *) echo "--environment must be demo or mainnet, and has no default" >&2; usage ;;
esac

if [ "$ENVIRONMENT" = demo ]; then
    HEARTBEAT=/var/lib/liquidity-migration-engine/heartbeat.json
    ENGINE_UNIT=liquidity-migration-engine.service
    PRODUCERS=(
        liquidity-migration-bybit-carry-demo.service
        liquidity-migration-bybit-long-demo.service
    )
    BOOKS=(
        /var/lib/liquidity-migration/targets/carry-demo.json
        /var/lib/liquidity-migration/targets/long-demo.json
        /var/lib/liquidity-migration/targets/exodus-demo.json
    )
else
    HEARTBEAT=/var/lib/liquidity-migration-engine-mainnet/heartbeat.json
    ENGINE_UNIT=liquidity-migration-engine-mainnet.service
    PRODUCERS=(
        liquidity-migration-bybit-carry-mainnet.service
        liquidity-migration-bybit-long-mainnet.service
    )
    BOOKS=(
        /var/lib/liquidity-migration/targets/carry-mainnet.json
        /var/lib/liquidity-migration/targets/long-mainnet.json
        /var/lib/liquidity-migration/targets/exodus-mainnet.json
    )
fi

held_symbols() {
    python3 - "$HEARTBEAT" <<'PY'
import json, sys
try:
    beat = json.loads(open(sys.argv[1]).read())
except OSError:
    sys.exit(3)
rows = beat.get("positions")
if not isinstance(rows, list):
    # An engine too old to say what it holds. Refusing is the only honest
    # answer: this command's whole job is to close what is there.
    sys.exit(4)
print(" ".join(sorted(str(r.get("symbol") or "") for r in rows if r.get("symbol"))))
PY
}

write_zero_book() {
    python3 - "$1" "$2" "$3" <<'PY'
import json, os, sys, time
path, source, symbols = sys.argv[1], sys.argv[2], sys.argv[3].split()
now_ms = int(time.time() * 1000)
book = {
    "version": 1,
    "source": source,
    "decision_ts_ms": now_ms,
    # Long enough that nothing expires mid-close. Entries are shut either way:
    # every row is zero, and a zero row is an exit whatever the window says.
    "valid_until_ms": now_ms + 24 * 3600 * 1000,
    "targets": [
        # The stop and leverage are required by the reader and unused on an
        # exit. They are filler, and saying so here is cheaper than somebody
        # later wondering which flatten policy these numbers encode.
        {"symbol": s, "notional_usdt": 0.0, "stop_loss_fraction": 0.5, "leverage": 1.0}
        for s in sorted(set(symbols))
    ],
}
tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(tmp, "w") as handle:
    handle.write(json.dumps(book, indent=2, sort_keys=True) + "\n")
os.replace(tmp, path)
PY
}

if ! systemctl is-active --quiet "$ENGINE_UNIT"; then
    echo "flatten refused: $ENGINE_UNIT is not running, so nothing would read the book" >&2
    exit 5
fi

SYMBOLS="$(held_symbols)" || {
    status=$?
    case "$status" in
        3) echo "flatten refused: no engine heartbeat at $HEARTBEAT" >&2 ;;
        4) echo "flatten refused: this engine does not publish what it holds" >&2 ;;
        *) echo "flatten refused: could not read $HEARTBEAT" >&2 ;;
    esac
    exit "$status"
}

if [ -z "$SYMBOLS" ]; then
    printf 'flatten status=no_configured_positions global_flat=unproven environment=%s reason=%s\n' "$ENVIRONMENT" "$REASON"
    exit 6
fi

printf 'flatten environment=%s reason=%s held=%s\n' "$ENVIRONMENT" "$REASON" "$SYMBOLS"
for unit in "${PRODUCERS[@]}"; do
    printf 'would stop unit=%s\n' "$unit"
done
for book in "${BOOKS[@]}"; do
    printf 'would write zero book path=%s symbols=%s\n' "$book" "$SYMBOLS"
done

if [ "$EXECUTE" -eq 0 ]; then
    echo "flatten status=planned (pass --execute to do it)"
    exit 0
fi

for unit in "${PRODUCERS[@]}"; do
    systemctl stop "$unit" 2>/dev/null || true
    printf 'stopped unit=%s\n' "$unit"
done

# The book is written to every sleeve because a name belongs to whichever
# sleeve is holding it, and this does not need to know which.
for book in "${BOOKS[@]}"; do
    source_name="$(basename "$book" .json | tr -c 'A-Za-z0-9_-' '_')"
    write_zero_book "$book" "flatten_$source_name" "$SYMBOLS"
    printf 'wrote path=%s\n' "$book"
done

# Deliberately leave LONG producer state untouched. The heartbeat is scoped to
# configured symbols and cannot authorize a schema-v2 state reset.

left="$(held_symbols || true)"
deadline=$(( $(date +%s) + WAIT_SECONDS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 5
    left="$(held_symbols || true)"
    if [ -z "$left" ]; then
        printf 'flatten status=configured_positions_closed global_flat=unproven state_reset=refused environment=%s\n' "$ENVIRONMENT" >&2
        echo "note: producers remain stopped; use venue-global account evidence before any state reset or restart." >&2
        exit 6
    fi
    printf 'still held=%s\n' "$left"
done

printf 'flatten status=timed_out environment=%s still_held=%s\n' "$ENVIRONMENT" "$left" >&2
exit 5
