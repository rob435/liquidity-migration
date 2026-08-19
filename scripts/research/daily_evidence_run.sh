#!/usr/bin/env bash
# The daily evidence run: data refresh -> panel rebuild -> forward-ledger
# append. Run by hand for now; scheduling belongs on the dedicated research
# box when it arrives (owner decision 2026-08-19), never on the owner's Mac.
# Run after Binance publishes its daily archive, which lands late morning
# UTC — a run before that fails the refresh's final validation on
# yesterday's Binance dailies.
# Status goes to $STATUS_FILE, one JSON line, read by a human.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
DATA_ROOT="${SHARED_DATA:-$HOME/SHARED_DATA}"
PANEL_OUT="$DATA_ROOT/cross_venue_panel_v1"
LOG_DIR="$DATA_ROOT/bybit_full_pit/reports/financed_longs_forward"
STATUS_FILE="$LOG_DIR/daily_run_status.json"
LOCK_DIR="$LOG_DIR/.daily_run.lock"

mkdir -p "$LOG_DIR"

# One run at a time: a second refresh against the same roots mid-run would
# interleave appends.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) another run holds $LOCK_DIR; exiting" >&2
  exit 1
fi
trap 'rmdir "$LOCK_DIR"' EXIT

fail() {
  printf '{"utc":"%s","status":"fail","step":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$1" > "$STATUS_FILE"
  exit 1
}

echo "=== daily evidence run $(date -u +%FT%TZ) ==="
# --force-rmom-full-rewrite: live roots are rolling stores, so recent
# residual-momentum rows legitimately move as trailing windows fill in;
# the append-overlap guard is for hand runs and definition changes, and
# its own error text prescribes full-rewrite for a deployed daily refresh.
"$PYTHON" "$ROOT/scripts/research/research_refresh.py" run \
  --force-rmom-full-rewrite || fail refresh

# --end is EXCLUSIVE: today's date covers through yesterday, the last
# complete UTC day.
"$PYTHON" "$ROOT/scripts/data/build_cross_venue_panel.py" \
  --start 2021-01-01 --end "$(date -u +%F)" --out "$PANEL_OUT" || fail panel

"$PYTHON" "$ROOT/scripts/research/score_financed_longs_forward.py" || fail ledger

printf '{"utc":"%s","status":"ok"}\n' "$(date -u +%FT%TZ)" > "$STATUS_FILE"
echo "=== done $(date -u +%FT%TZ) ==="
