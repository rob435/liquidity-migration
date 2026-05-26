#!/usr/bin/env bash
# Orchestrator: archive old roots → build both new per-venue full-PIT roots
# → run verification. Deletion of old roots is a deliberate manual step.
#
# See: docs/data_roots.md
#
# Usage:  bash scripts/build_full_pit_roots.sh
#
# Stage skip toggles (set =1 to skip):
#   SKIP_ARCHIVE=1
#   SKIP_BYBIT=1
#   SKIP_BINANCE=1
#   SKIP_VERIFY=1
set -euo pipefail

cd "$(dirname "$0")/.."

SKIP_ARCHIVE="${SKIP_ARCHIVE:-0}"
SKIP_BYBIT="${SKIP_BYBIT:-0}"
SKIP_BINANCE="${SKIP_BINANCE:-0}"
SKIP_VERIFY="${SKIP_VERIFY:-0}"

START_TS=$(date -u +%s)
echo "=============================================================="
echo "Full PIT roots rebuild — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=============================================================="

if [ "$SKIP_ARCHIVE" = "0" ]; then
  echo
  echo "### [0/4] Archive old roots' reports/ + _download_markers/"
  bash scripts/archive_pre_rebuild_reports.sh
else
  echo "### [0/4] archive — skipped (SKIP_ARCHIVE=1)"
fi

if [ "$SKIP_BYBIT" = "0" ]; then
  echo
  echo "### [1/4] Bybit full PIT build"
  bash scripts/build_full_pit_bybit.sh
else
  echo "### [1/4] Bybit build — skipped (SKIP_BYBIT=1)"
fi

if [ "$SKIP_BINANCE" = "0" ]; then
  echo
  echo "### [2/4] Binance full PIT build"
  bash scripts/build_full_pit_binance.sh
else
  echo "### [2/4] Binance build — skipped (SKIP_BINANCE=1)"
fi

if [ "$SKIP_VERIFY" = "0" ]; then
  echo
  echo "### [3/4] Verification gates"
  bash scripts/verify_full_pit_rebuild.sh
else
  echo "### [3/4] verify — skipped (SKIP_VERIFY=1)"
fi

ELAPSED=$(( $(date -u +%s) - START_TS ))
echo
echo "=============================================================="
echo "Rebuild complete in ${ELAPSED}s ($(printf '%dh%dm' $((ELAPSED/3600)) $(((ELAPSED%3600)/60))))"
echo
echo "Per-venue full-PIT roots:"
echo "      ~/SHARED_DATA/bybit_full_pit"
echo "      ~/SHARED_DATA/binance_full_pit"
echo
echo "Old roots (bybit_fullpit_1h, bybit_oos_pre2023, binance_oos_pit)"
echo "were deleted on 2026-05-25 after their reports + download markers"
echo "were archived to ~/SHARED_DATA/archive/2026-05-24_pre_full_pit_rebuild/."
echo "If you re-encounter any of those paths, treat them as a bug — every"
echo "operational caller now defaults to the new per-venue roots above."
echo "=============================================================="
