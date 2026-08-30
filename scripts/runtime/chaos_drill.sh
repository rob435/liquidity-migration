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
PYTHON="/opt/liquidity-migration/.venv/bin/python"
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

pid="$(systemctl show -p MainPID --value "$UNIT")"
if [ -z "$pid" ] || [ "$pid" -le 0 ]; then
    report "DEMO chaos drill skipped: the engine is not running, so there is nothing to rehearse. ref chaos_drill"
    exit 0
fi

kill -9 "$pid"
started=$SECONDS

verdict="timeout"
while [ $((SECONDS - started)) -lt "$WAIT_S" ]; do
    sleep 5
    systemctl is-active --quiet "$UNIT" || continue
    new_pid="$(systemctl show -p MainPID --value "$UNIT")"
    { [ -n "$new_pid" ] && [ "$new_pid" -gt 0 ] && [ "$new_pid" != "$pid" ]; } || continue
    beat_age=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0) ))
    [ "$beat_age" -le 30 ] || continue
    if grep -q '"may_open": true' "$HEARTBEAT"; then
        verdict="clean"
    else
        verdict="latched"
    fi
    break
done

took=$((SECONDS - started))
case "$verdict" in
clean)
    report "DEMO chaos drill: killed the engine (pid $pid); back in ${took}s with a fresh heartbeat and may open. Recovery clean."
    exit 0
    ;;
latched)
    report "DEMO chaos drill: the engine came back in ${took}s but LATCHED — it will not open until someone runs reconcile-clear. That is it failing safe, and it still needs a person. ref chaos_drill"
    exit 1
    ;;
*)
    report "DEMO chaos drill: the engine did NOT come back healthy within ${WAIT_S}s of being killed. Go and look now. ref chaos_drill"
    exit 1
    ;;
esac
