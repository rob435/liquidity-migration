#!/usr/bin/env bash
# Reset (archive + wipe) the demo + paper TRADING LEDGERS after a strategy
# overhaul, so the forward-demo/paper run starts from a clean slate and the
# Tier-3 30-day clock restarts on the new config.
#
# WHAT IT TOUCHES (only these per-root datasets):
#   data/bybit-demo-event       : event_demo_trades  event_demo_orders  event_demo_cycles
#   data/bybit-paper-event      : event_demo_trades  event_demo_orders  event_demo_cycles
#   data/bybit-long-demo-event  : long_native_demo_trades  long_native_demo_orders  long_native_demo_cycles
#   data/bybit-long-paper-event : long_native_paper_trades long_native_paper_orders long_native_paper_cycles
#
# WHAT IT PRESERVES: the WS kline stores (event_demo_klines_1h, …), instruments,
# manifests, configs, and every other dataset — wiping those would force a slow
# multi-day re-bootstrap and is NOT a "trading log".
#
# It ALWAYS archives the wiped datasets to a single timestamped tarball under
# data/_archive/ BEFORE removing them (same convention as the 2026-05-28 manual
# wipe), so the decision is auditable and reversible. If the archive step fails,
# nothing is removed.
#
# This is a DATA operation on the VPS — it is NOT run by CI. See the runbook in
# docs/event_demo_daemon.md ("Resetting the demo/paper ledgers"). The daemons
# recreate the emptied datasets on their next cycle.
#
# Usage (run from the repo root, e.g. /opt/liquidity-migration):
#   scripts/reset_demo_paper_ledgers.sh --dry-run      # preview, touches nothing
#   scripts/reset_demo_paper_ledgers.sh                # archive + wipe
#   scripts/reset_demo_paper_ledgers.sh --archive-dir /some/other/dir
set -euo pipefail

DRY_RUN=0
ARCHIVE_DIR="data/_archive"
LABEL=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --archive-dir) ARCHIVE_DIR="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Safety: must be run from the repo root so the relative data/ paths resolve to
# the live ledgers and not some unrelated directory.
if [ ! -d liquidity_migration ] || [ ! -d data ]; then
  echo "ERROR: run from the repo root (a directory with liquidity_migration/ and data/)." >&2
  exit 1
fi

# (root, dataset) pairs. Keep this list in sync with the deployed systemd units'
# DATA_ROOT values and storage.py dataset names.
PAIRS="
data/bybit-demo-event:event_demo_trades
data/bybit-demo-event:event_demo_orders
data/bybit-demo-event:event_demo_cycles
data/bybit-paper-event:event_demo_trades
data/bybit-paper-event:event_demo_orders
data/bybit-paper-event:event_demo_cycles
data/bybit-long-demo-event:long_native_demo_trades
data/bybit-long-demo-event:long_native_demo_orders
data/bybit-long-demo-event:long_native_demo_cycles
data/bybit-long-paper-event:long_native_paper_trades
data/bybit-long-paper-event:long_native_paper_orders
data/bybit-long-paper-event:long_native_paper_cycles
"

# Collect the targets that actually exist on disk.
EXISTING=""
for pair in $PAIRS; do
  root="${pair%%:*}"
  ds="${pair##*:}"
  path="$root/$ds"
  if [ -d "$path" ]; then
    EXISTING="$EXISTING $path"
  fi
done
EXISTING="${EXISTING# }"

if [ -z "$EXISTING" ]; then
  echo "Nothing to reset: none of the target ledger datasets exist under data/."
  exit 0
fi

echo "Target ledger datasets:"
for p in $EXISTING; do
  size="$(du -sh "$p" 2>/dev/null | cut -f1)"
  echo "  - $p  (${size:-?})"
done

if [ -z "$LABEL" ]; then
  LABEL="$(date -u +%Y%m%dT%H%M%SZ)"
fi
ARCHIVE_PATH="$ARCHIVE_DIR/ledger-reset-$LABEL.tar.gz"

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "[dry-run] would archive the above to: $ARCHIVE_PATH"
  echo "[dry-run] would then 'rm -rf' each listed dataset directory."
  echo "[dry-run] kline stores and all other datasets are left untouched."
  exit 0
fi

mkdir -p "$ARCHIVE_DIR"
echo ""
echo "Archiving to $ARCHIVE_PATH ..."
# shellcheck disable=SC2086
tar -czf "$ARCHIVE_PATH" $EXISTING
echo "Archived $(du -sh "$ARCHIVE_PATH" | cut -f1) -> $ARCHIVE_PATH"

echo "Wiping live ledger datasets ..."
for p in $EXISTING; do
  rm -rf "$p"
  echo "  removed $p"
done

echo ""
echo "Done. Ledgers reset; archive kept at $ARCHIVE_PATH."
echo "Restart the daemons so they recreate the emptied datasets from a clean slate."
