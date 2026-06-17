#!/usr/bin/env bash
# Back-compat alias. The full demo<->backtest<->paper three-way is now the DEFAULT
# of the single front door `scripts/reconcile.sh`; this wrapper just forwards to it
# so older invocations / docs keep working.
#
#   bash scripts/reconcile.sh                     # <- the canonical one command
#   bash scripts/reconcile_three_way.sh           # <- identical (this alias)
#
# (For the fast paper<->demo execution-only check: `bash scripts/reconcile.sh --quick`.)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$HERE/scripts/reconcile.sh" "$@"
