#!/usr/bin/env bash
# Archive and rebuild demo account state and strategy projections on the VPS.
#
# All logic lives in liquidity_migration/ops/demo_ledger_reset.py (safe
# defaults unchanged: dry-run unless --execute, REAL_MONEY refused, venue-flat
# proof required). This wrapper only pins PATH against lookalike binaries,
# anchors on the repository that contains it, and selects that checkout's
# Python.
set -euo pipefail

# Maintenance executes as root on the VPS.  Do not let an inherited interactive
# PATH replace systemctl, Git, archive, or filesystem utilities with lookalikes.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

RESET_SCRIPT_DIRECTORY="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESET_REPOSITORY_DIRECTORY="$(cd -P -- "$RESET_SCRIPT_DIRECTORY/../.." && pwd)"
cd -- "$RESET_REPOSITORY_DIRECTORY"

PYTHON="$RESET_REPOSITORY_DIRECTORY/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "ERROR: Python runtime not found (.venv/bin/python or python3)" >&2
  exit 1
fi

exec "$PYTHON" -m liquidity_migration.ops.demo_ledger_reset "$@"
