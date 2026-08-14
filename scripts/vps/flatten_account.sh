#!/usr/bin/env bash
# Take an account to zero exposure through the engine.
#
# Flatten used to publish zero targets into the Python owner's intent inbox.
# That owner is gone. The engine reads one absolute target book per sleeve, and
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
#   2. **Name every symbol at zero, not an empty list.** An empty book only
#      reaches the names the plug already has in hand. A book of explicit zero
#      rows reaches any name at all, which is what an operator asking to be
#      flat means. The names come from the engine's own heartbeat.
#
# Dry run unless --execute, like every other mutating operator command here.

set -Eeuo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: flatten_account.sh --environment demo|mainnet [--reason TEXT] [--execute]

  Without --execute: say what would be written and stopped, change nothing.
  With --execute:    stop the producers, write a zero book per sleeve, and
                     wait for the engine's heartbeat to show nothing held.

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
    )
    LONG_STATE=/var/lib/liquidity-migration/targets/long-demo-state.json
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
    )
    LONG_STATE=/var/lib/liquidity-migration/targets/long-mainnet-state.json
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
    printf 'flatten status=already_flat environment=%s reason=%s\n' "$ENVIRONMENT" "$REASON"
    exit 0
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

# LONG keeps its own record of what it asked for, and it does not read the book
# back. Left alone, a restarted producer would republish everything this just
# closed.
if [ -e "$LONG_STATE" ]; then
    printf '{\n  "held": [],\n  "left_at_ms": {},\n  "version": 1\n}\n' > "$LONG_STATE"
    printf 'cleared path=%s\n' "$LONG_STATE"
fi

deadline=$(( $(date +%s) + WAIT_SECONDS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 5
    left="$(held_symbols || true)"
    if [ -z "$left" ]; then
        printf 'flatten status=flat environment=%s\n' "$ENVIRONMENT"
        echo "note: the producers are stopped but not disabled. A deploy activate will"
        echo "      start them again; set the sleeve off in /etc/liquidity-migration/sleeves.env"
        echo "      to make this stick."
        exit 0
    fi
    printf 'still held=%s\n' "$left"
done

printf 'flatten status=timed_out environment=%s still_held=%s\n' "$ENVIRONMENT" "$left" >&2
exit 5
