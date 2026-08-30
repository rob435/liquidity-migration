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
MAX_HEARTBEAT_AGE_SECONDS="${FLATTEN_MAX_HEARTBEAT_AGE_SECONDS:-30}"
POLL_SECONDS="${FLATTEN_POLL_SECONDS:-5}"

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

case "$WAIT_SECONDS" in
    ''|*[!0-9]*) echo "--wait-seconds must be a non-negative integer" >&2; usage ;;
esac
case "$MAX_HEARTBEAT_AGE_SECONDS" in
    ''|*[!0-9]*) echo "FLATTEN_MAX_HEARTBEAT_AGE_SECONDS must be a positive integer" >&2; exit 2 ;;
    0) echo "FLATTEN_MAX_HEARTBEAT_AGE_SECONDS must be positive" >&2; exit 2 ;;
esac

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LM_FLEET_MANIFEST="$REPOSITORY_ROOT/deploy/fleet_manifest.tsv"
# shellcheck source=deploy/lib_sleeves.sh
source "$REPOSITORY_ROOT/deploy/lib_sleeves.sh"
lm_validate_fleet_manifest || {
    echo "flatten refused: fleet manifest is invalid" >&2
    exit 2
}

if [ "$ENVIRONMENT" = demo ]; then
    DEFAULT_HEARTBEAT=/var/lib/liquidity-migration-engine/heartbeat.json
    DEFAULT_ENGINE_ENV=/etc/liquidity-migration/engine.env
    PRODUCER_LONG=on
    PRODUCER_CARRY=on
    PRODUCER_MAINNET=off
else
    DEFAULT_HEARTBEAT=/var/lib/liquidity-migration-engine-mainnet/heartbeat.json
    DEFAULT_ENGINE_ENV=/etc/liquidity-migration/engine-mainnet.env
    PRODUCER_LONG=off
    PRODUCER_CARRY=off
    PRODUCER_MAINNET=on
fi

HEARTBEAT="${FLATTEN_HEARTBEAT_PATH:-$DEFAULT_HEARTBEAT}"
ENGINE_ENV="${FLATTEN_ENGINE_ENV_PATH:-$DEFAULT_ENGINE_ENV}"
TARGET_ROOT="${FLATTEN_TARGET_ROOT:-/var/lib/liquidity-migration/targets}"
ENGINE_UNIT="$(lm_owner_unit "$ENVIRONMENT")" || {
    echo "flatten refused: fleet manifest has no $ENVIRONMENT account owner" >&2
    exit 2
}
PRODUCERS=()
while IFS= read -r unit; do
    [ -n "$unit" ] && PRODUCERS+=("$unit")
done < <(
    lm_target_producer_units "$ENVIRONMENT" stop \
        "$PRODUCER_LONG" "$PRODUCER_CARRY" "$PRODUCER_MAINNET"
)
[ "${#PRODUCERS[@]}" -gt 0 ] || {
    echo "flatten refused: fleet manifest has no $ENVIRONMENT target producers" >&2
    exit 2
}
BOOKS=()
for unit in "${PRODUCERS[@]}"; do
    artifact="$(lm_output_artifact_for_unit "$unit")" || {
        echo "flatten refused: fleet manifest has no target book for $unit" >&2
        exit 2
    }
    BOOKS+=("$TARGET_ROOT/$(basename "$artifact")")
done

heartbeat_state() {
    python3 - "$HEARTBEAT" "$ENGINE_ENV" "$ENVIRONMENT" "$MAX_HEARTBEAT_AGE_SECONDS" <<'PY'
import json
import math
import sys
import time

heartbeat_path, environment_path, requested_realm, raw_max_age = sys.argv[1:]


def unknown(message):
    print(f"heartbeat unknown: {message}", file=sys.stderr)
    raise SystemExit(4)


contract = {}
try:
    with open(environment_path, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                unknown(f"{environment_path}:{number} is not KEY=value")
            key, value = line.split("=", 1)
            key = key.strip()
            if key in contract:
                unknown(f"{environment_path}:{number} repeats {key}")
            contract[key] = value.strip()
except OSError as exc:
    unknown(f"cannot read identity contract {environment_path}: {exc}")

required = {
    "account_user_id": contract.get("EXPECTED_ENGINE_ACCOUNT_USER_ID", ""),
    "venue": contract.get("EXPECTED_ENGINE_VENUE", ""),
    "realm": contract.get("EXPECTED_ENGINE_REALM", ""),
}
missing = [name for name, value in required.items() if not value]
if missing:
    unknown("identity contract is missing " + ", ".join(missing))
if required["realm"] != requested_realm:
    unknown(
        f"identity contract realm {required['realm']!r} does not match"
        f" requested realm {requested_realm!r}"
    )

try:
    with open(heartbeat_path, "rb") as handle:
        beat = json.load(handle)
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    unknown(f"cannot read {heartbeat_path}: {exc}")
if not isinstance(beat, dict):
    unknown("heartbeat is not a JSON object")

for field, expected in required.items():
    observed = beat.get(field)
    if observed != expected:
        unknown(f"{field} expected {expected!r}, observed {observed!r}")

engine_version = beat.get("engine_version")
if not isinstance(engine_version, str) or not engine_version:
    unknown("engine_version is missing or invalid")
expected_version = contract.get("EXPECTED_ENGINE_VERSION", "")
if expected_version and engine_version != expected_version:
    unknown(
        f"engine_version expected {expected_version!r}, observed {engine_version!r}"
    )

now_ms = int(time.time() * 1_000)
max_age_ms = int(raw_max_age) * 1_000
for field in ("wall_ts_ms", "account_observed_wall_ts_ms"):
    stamp = beat.get(field)
    if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp <= 0:
        unknown(f"{field} is missing or invalid")
    age_ms = now_ms - stamp
    if age_ms < 0:
        unknown(f"{field} is {-age_ms}ms in the future")
    if age_ms > max_age_ms:
        unknown(f"{field} is {age_ms / 1_000:.1f}s old")

rows = beat.get("positions")
if not isinstance(rows, list):
    unknown("positions is not an array")
symbols = []
seen = set()
for index, row in enumerate(rows):
    if not isinstance(row, dict):
        unknown(f"position {index} is not an object")
    symbol = row.get("symbol")
    qty = row.get("qty")
    if (
        not isinstance(symbol, str)
        or not symbol
        or symbol != symbol.upper()
        or not symbol.isalnum()
    ):
        unknown(f"position {index} has an invalid symbol")
    if symbol in seen:
        unknown(f"positions repeats {symbol}")
    if (
        isinstance(qty, bool)
        or not isinstance(qty, (int, float))
        or not math.isfinite(float(qty))
        or float(qty) <= 0.0
    ):
        unknown(f"position {symbol} has an invalid quantity")
    seen.add(symbol)
    symbols.append(symbol)

if symbols:
    print(f"HELD\t{beat['wall_ts_ms']}\t" + " ".join(sorted(symbols)))
else:
    print(f"FLAT\t{beat['wall_ts_ms']}")
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

load_heartbeat_state() {
    SNAPSHOT="$(heartbeat_state)" || return $?
    case "$SNAPSHOT" in
        $'FLAT\t'*) HEARTBEAT_STATE=FLAT; SYMBOLS="" ;;
        $'HELD\t'*)
            HEARTBEAT_STATE=HELD
            SNAPSHOT_REST="${SNAPSHOT#*$'\t'}"
            SYMBOLS="${SNAPSHOT_REST#*$'\t'}"
            ;;
        *) echo "flatten refused: heartbeat parser returned an invalid state" >&2; return 4 ;;
    esac
}

if [ "$EXECUTE" -eq 0 ]; then
    if load_heartbeat_state; then
        :
    else
        status=$?
        echo "flatten refused: configured-position state is unknown" >&2
        exit "$status"
    fi
    printf 'flatten environment=%s reason=%s configured_state=%s held=%s\n' \
        "$ENVIRONMENT" "$REASON" "$HEARTBEAT_STATE" "${SYMBOLS:-none}"
    for unit in "${PRODUCERS[@]}"; do
        printf 'would stop unit=%s\n' "$unit"
    done
    for book in "${BOOKS[@]}"; do
        printf 'would write zero book path=%s symbols=%s\n' "$book" "${SYMBOLS:-none}"
    done
    if [ "$HEARTBEAT_STATE" = FLAT ]; then
        printf 'flatten status=no_configured_positions global_flat=unproven environment=%s reason=%s\n' "$ENVIRONMENT" "$REASON"
        exit 6
    fi
    echo "flatten status=planned (pass --execute to do it)"
    exit 0
fi

printf 'flatten environment=%s reason=%s action=execute\n' "$ENVIRONMENT" "$REASON"
for unit in "${PRODUCERS[@]}"; do
    printf 'would stop unit=%s\n' "$unit"
    if ! systemctl stop "$unit"; then
        printf 'flatten refused: failed to stop producer unit=%s; no books written\n' "$unit" >&2
        exit 5
    fi
done
for unit in "${PRODUCERS[@]}"; do
    active_state="$(systemctl show --property=ActiveState --value "$unit")" || {
        printf 'flatten refused: could not verify producer unit=%s inactive; no books written\n' "$unit" >&2
        exit 5
    }
    if [ "$active_state" != inactive ]; then
        printf 'flatten refused: producer unit=%s state=%s, expected inactive; no books written\n' \
            "$unit" "${active_state:-unknown}" >&2
        exit 5
    fi
    printf 'stopped unit=%s\n' "$unit"
done

if ! systemctl is-active --quiet "$ENGINE_UNIT"; then
    echo "flatten refused: $ENGINE_UNIT stopped before it could read the books; no books written" >&2
    exit 5
fi

if load_heartbeat_state; then
    :
else
    status=$?
    echo "flatten refused: configured-position state is unknown; producers remain stopped; no books written" >&2
    exit "$status"
fi
printf 'flatten configured_state=%s held=%s\n' "$HEARTBEAT_STATE" "${SYMBOLS:-none}"
for book in "${BOOKS[@]}"; do
    printf 'would write zero book path=%s symbols=%s\n' "$book" "${SYMBOLS:-none}"
done

# The book is written to every sleeve because a name belongs to whichever
# sleeve is holding it, and this does not need to know which.
for book in "${BOOKS[@]}"; do
    source_name="$(basename "$book" .json | tr -c 'A-Za-z0-9_-' '_')"
    write_zero_book "$book" "flatten_$source_name" "$SYMBOLS"
    printf 'wrote path=%s\n' "$book"
done
books_written_ms="$(python3 - <<'PY'
import time
print(int(time.time() * 1_000))
PY
)"

# Deliberately leave LONG producer state untouched. The heartbeat is scoped to
# configured symbols and cannot authorize a schema-v2 state reset.

left="$SYMBOLS"
deadline=$(( $(date +%s) + WAIT_SECONDS ))
while :; do
    SNAPSHOT="$(heartbeat_state)" || {
        echo "flatten status=heartbeat_unknown global_flat=unproven; producers remain stopped" >&2
        exit 5
    }
    case "$SNAPSHOT" in
        $'FLAT\t'*)
            observed_ms="${SNAPSHOT#*$'\t'}"
            left=""
            ;;
        $'HELD\t'*)
            SNAPSHOT_REST="${SNAPSHOT#*$'\t'}"
            observed_ms="${SNAPSHOT_REST%%$'\t'*}"
            left="${SNAPSHOT_REST#*$'\t'}"
            ;;
        *) echo "flatten status=heartbeat_unknown global_flat=unproven; producers remain stopped" >&2; exit 5 ;;
    esac

    if [ "$observed_ms" -gt "$books_written_ms" ]; then
        if [ -z "$left" ]; then
            printf 'flatten status=configured_positions_closed global_flat=unproven state_reset=refused environment=%s\n' "$ENVIRONMENT" >&2
            echo "note: producers remain stopped; use venue-global account evidence before any state reset or restart." >&2
            exit 6
        fi
        printf 'still held=%s\n' "$left"
    else
        printf 'waiting for post-write heartbeat last_wall_ts_ms=%s books_written_ms=%s\n' \
            "$observed_ms" "$books_written_ms"
    fi

    if [ "$(date +%s)" -ge "$deadline" ]; then
        break
    fi
    sleep "$POLL_SECONDS"
done

printf 'flatten status=timed_out environment=%s still_held=%s\n' "$ENVIRONMENT" "${left:-unknown}" >&2
exit 5
