#!/usr/bin/env bash
# Ask every native directional reducer to close its own attributed exposure.
# The signal worker stays live: CARRY still needs settlement observations while
# entries are paused and positions are leaving.

set -Eeuo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: flatten_account.sh --environment demo|mainnet [--reason TEXT] [--execute]

  Without --execute: show the durable controls that would be submitted.
  With --execute:    disable entries for LONG, CARRY, and Exodus, submit one
                     replayable flatten command to each, then wait for the
                     engine heartbeat to show no positions.

  --wait-seconds N   how long to wait for flat (default 300)

The result is scoped to the engine heartbeat. It does not prove venue-global
flatness and it does not reset reducer state.
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
        --reason) [ "$#" -ge 2 ] || usage; REASON="$2"; shift 2 ;;
        --wait-seconds) [ "$#" -ge 2 ] || usage; WAIT_SECONDS="$2"; shift 2 ;;
        --execute) EXECUTE=1; shift ;;
        --dry-run) EXECUTE=0; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

case "$ENVIRONMENT" in
    demo)
        ENGINE_UNIT=liquidity-migration-engine.service
        ENGINE_USER=liquidity-engine-demo
        ENGINE_CONFIG="${FLATTEN_ENGINE_CONFIG_PATH:-/etc/liquidity-migration/engine.toml}"
        ENGINE_ENV="${FLATTEN_ENGINE_ENV_PATH:-/etc/liquidity-migration/engine.env}"
        HEARTBEAT="${FLATTEN_HEARTBEAT_PATH:-/var/lib/liquidity-migration-engine/heartbeat.json}"
        ;;
    mainnet)
        ENGINE_UNIT=liquidity-migration-engine-mainnet.service
        ENGINE_USER=liquidity-engine-mainnet
        ENGINE_CONFIG="${FLATTEN_ENGINE_CONFIG_PATH:-/etc/liquidity-migration/engine-mainnet.toml}"
        ENGINE_ENV="${FLATTEN_ENGINE_ENV_PATH:-/etc/liquidity-migration/engine-mainnet.env}"
        HEARTBEAT="${FLATTEN_HEARTBEAT_PATH:-/var/lib/liquidity-migration-engine-mainnet/heartbeat.json}"
        ;;
    *) echo "--environment must be demo or mainnet, and has no default" >&2; usage ;;
esac

case "$WAIT_SECONDS" in
    ''|*[!0-9]*) echo "--wait-seconds must be a non-negative integer" >&2; usage ;;
esac
case "$MAX_HEARTBEAT_AGE_SECONDS" in
    ''|*[!0-9]*|0) echo "FLATTEN_MAX_HEARTBEAT_AGE_SECONDS must be positive" >&2; exit 2 ;;
esac

ENGINE="${FLATTEN_ENGINE_BINARY:-/opt/liquidity-migration-engine/bin/engine}"
RUNTIME_GROUP=liquidity-migration
STRATEGIES=(long carry exodus)

heartbeat_state() {
    python3 - "$HEARTBEAT" "$ENGINE_ENV" "$ENVIRONMENT" "$MAX_HEARTBEAT_AGE_SECONDS" "$@" <<'PY'
import json
import math
import sys
import time

heartbeat_path, environment_path, requested_realm, raw_max_age, *expected_flatten_ids = sys.argv[1:]

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
if not all(required.values()) or required["realm"] != requested_realm:
    unknown("identity contract is incomplete or names another realm")
try:
    with open(heartbeat_path, "rb") as handle:
        beat = json.load(handle)
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    unknown(f"cannot read {heartbeat_path}: {exc}")
if not isinstance(beat, dict):
    unknown("heartbeat is not a JSON object")
for field, expected in required.items():
    if beat.get(field) != expected:
        unknown(f"{field} expected {expected!r}, observed {beat.get(field)!r}")
now_ms = int(time.time() * 1_000)
for field in ("wall_ts_ms", "account_observed_wall_ts_ms"):
    stamp = beat.get(field)
    if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp <= 0:
        unknown(f"{field} is missing or invalid")
    age_ms = now_ms - stamp
    if age_ms < 0 or age_ms > int(raw_max_age) * 1_000:
        unknown(f"{field} is outside the accepted age")

permissions = beat.get("strategy_entries_enabled")
if not isinstance(permissions, list):
    unknown("strategy_entries_enabled is not an array")
values = {}
for row in permissions:
    if not isinstance(row, dict):
        unknown("strategy entry row is not an object")
    name, enabled = row.get("strategy"), row.get("entries_enabled")
    if not isinstance(name, str) or type(enabled) is not bool or name in values:
        unknown("strategy entry row is invalid or duplicated")
    values[name] = enabled
for name in ("long", "carry", "exodus"):
    if name not in values:
        unknown(f"strategy entry status omits {name}")

pending = beat.get("pending_flatten_requests")
if not isinstance(pending, list):
    unknown("pending_flatten_requests is not an array")
pending_pairs = set()
for row in pending:
    if not isinstance(row, dict):
        unknown("pending flatten row is not an object")
    strategy, request_id = row.get("strategy"), row.get("request_id")
    if strategy not in ("long", "carry", "exodus") or not isinstance(request_id, str) or not request_id:
        unknown("pending flatten row is invalid")
    pair = (strategy, request_id)
    if pair in pending_pairs:
        unknown("pending flatten row is duplicated")
    pending_pairs.add(pair)

working = beat.get("working_entries")
if not isinstance(working, list):
    unknown("working_entries is not an array")
directional_working = []
for row in working:
    if not isinstance(row, dict):
        unknown("working entry row is not an object")
    strategy, symbol = row.get("strategy"), row.get("symbol")
    if not isinstance(strategy, str) or not isinstance(symbol, str) or not symbol:
        unknown("working entry row is invalid")
    if strategy in ("long", "carry", "exodus"):
        directional_working.append((strategy, symbol))

positions = beat.get("positions")
if not isinstance(positions, list):
    unknown("positions is not an array")
symbols = []
for index, row in enumerate(positions):
    if not isinstance(row, dict):
        unknown(f"position {index} is not an object")
    symbol, qty = row.get("symbol"), row.get("qty")
    if not isinstance(symbol, str) or not symbol or symbol in symbols:
        unknown(f"position {index} has an invalid symbol")
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(float(qty)) or float(qty) <= 0:
        unknown(f"position {symbol} has an invalid quantity")
    symbols.append(symbol)
permission_text = ",".join(f"{name}={str(values[name]).lower()}" for name in ("long", "carry", "exodus"))
state = "FLAT" if not symbols else "HELD"
pending_expected = sum(request_id in expected_flatten_ids for _, request_id in pending_pairs)
print("|".join((
    state,
    str(beat["wall_ts_ms"]),
    permission_text,
    " ".join(sorted(symbols)),
    str(pending_expected),
    str(len(directional_working)),
)))
PY
}

load_snapshot() {
    local raw
    raw="$(heartbeat_state "$@")" || return
    IFS='|' read -r HEARTBEAT_STATE HEARTBEAT_STAMP PERMISSIONS SYMBOLS \
        PENDING_EXPECTED_COUNT DIRECTIONAL_WORKING_COUNT <<<"$raw"
    [ -n "$HEARTBEAT_STATE" ] && [ -n "$HEARTBEAT_STAMP" ] \
        && [ -n "$PERMISSIONS" ] && [ -n "$PENDING_EXPECTED_COUNT" ] \
        && [ -n "$DIRECTIONAL_WORKING_COUNT" ]
}

if ! systemctl is-active --quiet "$ENGINE_UNIT"; then
    echo "flatten refused: $ENGINE_UNIT is not running" >&2
    exit 5
fi

load_snapshot || {
    echo "flatten refused: engine state is unknown" >&2
    exit 4
}

printf 'flatten environment=%s reason=%s configured_state=%s held=%s\n' \
    "$ENVIRONMENT" "$REASON" "$HEARTBEAT_STATE" "${SYMBOLS:-none}"
for strategy in "${STRATEGIES[@]}"; do
    printf 'would set entries_enabled=false strategy=%s\n' "$strategy"
done
for strategy in "${STRATEGIES[@]}"; do
    printf 'would request flatten strategy=%s\n' "$strategy"
done

if [ "$EXECUTE" -eq 0 ]; then
    if [ "$HEARTBEAT_STATE" = FLAT ]; then
        printf 'flatten status=no_engine_positions global_flat=unproven environment=%s reason=%s\n' \
            "$ENVIRONMENT" "$REASON"
        exit 6
    fi
    echo "flatten status=planned (pass --execute to do it)"
    exit 0
fi

[ -x "$ENGINE" ] || { echo "flatten refused: engine binary is not executable" >&2; exit 5; }
[ -x /usr/bin/setpriv ] || { echo "flatten refused: setpriv is unavailable" >&2; exit 5; }
request_prefix="flatten-${ENVIRONMENT}-$(date +%s%N)-$$"
run_engine() {
    /usr/bin/setpriv --reuid="$ENGINE_USER" --regid="$RUNTIME_GROUP" --init-groups \
        /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        "$ENGINE" "$@"
}

for strategy in "${STRATEGIES[@]}"; do
    run_engine set-strategy-entry-permission --config "$ENGINE_CONFIG" \
        --strategy "$strategy" --entries-enabled false \
        --request-id "${request_prefix}-pause-${strategy}" --wait-ms 30000 \
        || { echo "flatten refused: could not durably pause $strategy" >&2; exit 5; }
done
FLATTEN_REQUEST_IDS=()
for strategy in "${STRATEGIES[@]}"; do
    flatten_request_id="${request_prefix}-flatten-${strategy}"
    FLATTEN_REQUEST_IDS+=("$flatten_request_id")
    run_engine flatten-strategy --config "$ENGINE_CONFIG" --strategy "$strategy" \
        --request-id "$flatten_request_id" --wait-ms 30000 \
        || { echo "flatten refused: could not durably request $strategy flatten" >&2; exit 5; }
done

deadline=$(( $(date +%s) + WAIT_SECONDS ))
while :; do
    load_snapshot "${FLATTEN_REQUEST_IDS[@]}" || {
        echo "flatten status=heartbeat_unknown global_flat=unproven" >&2
        exit 5
    }
    [ "$PERMISSIONS" = "long=false,carry=false,exodus=false" ] || {
        echo "flatten refused: runtime entry pause is not present in heartbeat" >&2
        exit 5
    }
    if [ "$HEARTBEAT_STATE" = FLAT ] \
        && [ "$PENDING_EXPECTED_COUNT" = 0 ] \
        && [ "$DIRECTIONAL_WORKING_COUNT" = 0 ]; then
        printf 'flatten status=engine_positions_closed global_flat=unproven state_reset=refused environment=%s\n' \
            "$ENVIRONMENT" >&2
        echo "note: entries remain paused; venue-global evidence is required before any state reset." >&2
        exit 6
    fi
    printf 'still held=%s pending_flatten_acks=%s directional_working_entries=%s\n' \
        "${SYMBOLS:-none}" "$PENDING_EXPECTED_COUNT" "$DIRECTIONAL_WORKING_COUNT"
    [ "$(date +%s)" -lt "$deadline" ] || break
    sleep "$POLL_SECONDS"
done

printf 'flatten status=timed_out environment=%s still_held=%s\n' \
    "$ENVIRONMENT" "${SYMBOLS:-unknown}" >&2
exit 5
