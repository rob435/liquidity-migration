#!/usr/bin/env bash
# Archive each old research root's reports/ + _download_markers/ before any
# destructive rebuild step. Preserves research history (~100 MB) without
# carrying ~5 GB of raw data forward.
#
# See: docs/full_pit_rebuild_and_punchlist.md section A.2
#
# Usage:  bash scripts/archive_pre_rebuild_reports.sh
# Override target via env:  ARCHIVE_DIR=~/custom/path bash scripts/archive_pre_rebuild_reports.sh
set -euo pipefail

ARCHIVE_DIR="${ARCHIVE_DIR:-$HOME/SHARED_DATA/archive/$(date -u +%Y-%m-%d)_pre_full_pit_rebuild}"
mkdir -p "$ARCHIVE_DIR"

# Pick a tar compressor: zstd preferred, gzip fallback.
if tar --help 2>&1 | grep -q -- "--zstd"; then
  TAR_COMPRESS="--zstd"
  EXT="tar.zst"
else
  TAR_COMPRESS="--gzip"
  EXT="tar.gz"
fi

ROOTS=(
  "bybit_fullpit_1h"
  "bybit_oos_pre2023"
  "binance_oos_pit"
)

for root in "${ROOTS[@]}"; do
  src="$HOME/SHARED_DATA/$root"
  if [ ! -d "$src" ]; then
    echo "[skip] $src — not present"
    continue
  fi
  echo "[archive] $root"
  for subdir in reports _download_markers; do
    if [ -d "$src/$subdir" ]; then
      out="$ARCHIVE_DIR/${root}_${subdir}.${EXT}"
      (cd "$src" && tar $TAR_COMPRESS -cf "$out" "$subdir/")
      echo "          -> $out"
    fi
  done
done

echo
echo "Archive complete: $ARCHIVE_DIR"
ls -lh "$ARCHIVE_DIR" 2>/dev/null || true
