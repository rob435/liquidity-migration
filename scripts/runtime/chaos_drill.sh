#!/usr/bin/env bash
# Kill the DEMO engine without warning and watch it come back: crash recovery
# as a weekly rehearsed fact instead of a documented one. The engine's whole
# crash-safety design — WAL replay, boot reconciliation, the may-open latch —
# is only ever exercised by a crash, and a drill is a crash whose timing an
# operator knows.
#
# DEMO ONLY, hardcoded. The funded engine's crashes are not for rehearsing.
set -euo pipefail

UNIT="liquidity-migration-engine.service"
HEARTBEAT="${LIVENESS_ENGINE_HEARTBEAT_FILE:?LIVENESS_ENGINE_HEARTBEAT_FILE is required: recovery is judged by the heartbeat}"
EXPECTED_ACCOUNT="${EXPECTED_ENGINE_ACCOUNT_USER_ID:?EXPECTED_ENGINE_ACCOUNT_USER_ID is required}"
EXPECTED_VENUE="${EXPECTED_ENGINE_VENUE:?EXPECTED_ENGINE_VENUE is required}"
EXPECTED_REALM="${EXPECTED_ENGINE_REALM:?EXPECTED_ENGINE_REALM is required}"
PYTHON="${CHAOS_DRILL_PYTHON:-/opt/liquidity-migration/.venv/bin/python}"
PROC_ROOT="${CHAOS_DRILL_PROC_ROOT:-/proc}"
WAIT_S="${CHAOS_DRILL_WAIT_S:-180}"

report() {
    echo "$1"
    TEXT="$1" "$PYTHON" - <<'PY' || true
import os
import sys

sys.path.insert(0, "/opt/liquidity-migration")
from liquidity_migration.ops.telegram import send_telegram_message

send_telegram_message(
    os.environ["TEXT"],
    channel="alerts",
    enabled=os.environ.get("TELEGRAM_ENABLED") == "1",
)
PY
}

read_heartbeat() {
    local cgroup="$1" minimum_wall_ms="$2" old_engine_pid="$3"
    "$PYTHON" - "$HEARTBEAT" "$EXPECTED_ACCOUNT" "$EXPECTED_VENUE" \
        "$EXPECTED_REALM" "$cgroup" "$minimum_wall_ms" "$old_engine_pid" "$PROC_ROOT" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
expected_account, expected_venue, expected_realm = sys.argv[2:5]
expected_cgroup = sys.argv[5]
minimum_wall_ms = int(sys.argv[6])
old_engine_pid = int(sys.argv[7])
proc_root = Path(sys.argv[8])

try:
    row = json.loads(path.read_bytes())
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"unreadable heartbeat: {exc}") from exc
if not isinstance(row, dict):
    raise SystemExit("heartbeat is not an object")

def exact_int(name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit(f"heartbeat {name} is not an integer")
    return value

now_ms = time.time_ns() // 1_000_000
wall_ms = exact_int("wall_ts_ms")
observed_ms = exact_int("account_observed_wall_ts_ms")
for name, value in (("wall_ts_ms", wall_ms), ("account_observed_wall_ts_ms", observed_ms)):
    if value < minimum_wall_ms or value > now_ms + 5_000 or now_ms - value > 30_000:
        raise SystemExit(f"heartbeat {name} is not fresh for this engine generation")
for name, expected in (
    ("account_user_id", expected_account),
    ("venue", expected_venue),
    ("realm", expected_realm),
    ("mode", "live"),
):
    if row.get(name) != expected:
        raise SystemExit(f"heartbeat {name} does not match the demo engine")
if type(row.get("may_open")) is not bool:
    raise SystemExit("heartbeat may_open is not a boolean")

engine_pid = exact_int("pid")
if engine_pid <= 0 or engine_pid == old_engine_pid:
    raise SystemExit("heartbeat belongs to the prior engine process")
try:
    memberships = (proc_root / str(engine_pid) / "cgroup").read_text().splitlines()
except OSError as exc:
    raise SystemExit(f"heartbeat process is not alive: {exc}") from exc
paths = [line.split(":", 2)[2] for line in memberships if line.count(":") >= 2]
if expected_cgroup not in paths:
    raise SystemExit("heartbeat process is outside the engine service cgroup")

print(engine_pid, "clean" if row["may_open"] else "latched")
PY
}

main_pid="$(systemctl show -p MainPID --value "$UNIT")"
control_group="$(systemctl show -p ControlGroup --value "$UNIT")"
invocation_id="$(systemctl show -p InvocationID --value "$UNIT")"
if [ -z "$main_pid" ] || [ "$main_pid" -le 0 ]; then
    report "DEMO chaos drill skipped: the engine is not running, so there is nothing to rehearse. ref chaos_drill"
    exit 0
fi
case "$control_group" in
    /*) ;;
    *) report "DEMO chaos drill refused: the engine service cgroup is unavailable. ref chaos_drill"; exit 1 ;;
esac
if [[ ! "$invocation_id" =~ ^[0-9a-f]{32}$ ]]; then
    report "DEMO chaos drill refused: the engine service generation is unavailable. ref chaos_drill"
    exit 1
fi

baseline="$(read_heartbeat "$control_group" 0 0)" || {
    report "DEMO chaos drill refused: the running engine has no fresh, exact-account heartbeat. ref chaos_drill"
    exit 1
}
read -r engine_pid baseline_verdict <<<"$baseline"
if [ "$baseline_verdict" != clean ]; then
    report "DEMO chaos drill refused: the running engine is already latched. ref chaos_drill"
    exit 1
fi

killed_wall_ms="$(date +%s%3N)"
kill_status=0
# systemd can report one vanished cgroup member after signaling the others.
# The new process generation below decides whether the crash actually happened.
systemctl kill --signal=KILL "$UNIT" || kill_status=$?
started=$SECONDS

verdict="timeout"
while [ $((SECONDS - started)) -lt "$WAIT_S" ]; do
    sleep 5
    systemctl is-active --quiet "$UNIT" || continue
    new_invocation_id="$(systemctl show -p InvocationID --value "$UNIT")"
    { [[ "$new_invocation_id" =~ ^[0-9a-f]{32}$ ]] && [ "$new_invocation_id" != "$invocation_id" ]; } \
        || continue
    new_main_pid="$(systemctl show -p MainPID --value "$UNIT")"
    { [ -n "$new_main_pid" ] && [ "$new_main_pid" -gt 0 ] && [ "$new_main_pid" != "$main_pid" ]; } || continue
    control_group="$(systemctl show -p ControlGroup --value "$UNIT")"
    recovered="$(read_heartbeat "$control_group" "$killed_wall_ms" "$engine_pid" 2>/dev/null)" || continue
    read -r _new_engine_pid verdict <<<"$recovered"
    break
done

took=$((SECONDS - started))
case "$verdict" in
clean)
    if [ "$kill_status" -eq 0 ]; then
        report "DEMO chaos drill: killed the engine (pid $engine_pid); back in ${took}s with a fresh exact-account heartbeat and may open. Recovery clean."
    else
        report "DEMO chaos drill: systemctl returned status $kill_status after dispatch, but engine pid $engine_pid was replaced and the new generation has a fresh exact-account heartbeat and may open. Recovery clean."
    fi
    exit 0
    ;;
latched)
    report "DEMO chaos drill: the engine came back in ${took}s but LATCHED — it will not open until someone runs reconcile-clear. That is it failing safe, and it still needs a person. ref chaos_drill"
    exit 1
    ;;
*)
    if [ "$kill_status" -eq 0 ]; then
        report "DEMO chaos drill: the engine did NOT come back healthy within ${WAIT_S}s of being killed. Go and look now. ref chaos_drill"
    else
        report "DEMO chaos drill: systemctl kill returned status $kill_status and no fresh new engine generation was proved within ${WAIT_S}s. No recovery claim is possible. Go and look now. ref chaos_drill"
    fi
    exit 1
    ;;
esac
