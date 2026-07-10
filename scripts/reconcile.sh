#!/usr/bin/env bash
# ONE command for the whole reconciliation. By default it runs the FULL
# demo <-> backtest <-> paper three-way for BOTH sleeves (long + continuous):
# refresh point-in-time data, pull the live ledgers, run each sleeve's backtest
# over the live forward window, and reconcile the model against demo + paper —
# all in a single run.
#
#   bash scripts/reconcile.sh                     # full three-way, both sleeves (default)
#   bash scripts/reconcile.sh --no-data-refresh   # full three-way, skip the PIT download
#   bash scripts/reconcile.sh --sleeves long      # full three-way, one sleeve
#   bash scripts/reconcile.sh --quick             # FAST two-way only (paper<->demo execution)
#   bash scripts/reconcile.sh --dry-run           # print every command, run nothing
#   bash scripts/reconcile.sh --help              # full option list
#
# --quick (alias --two-way) routes to the fast execution-only check
# (scripts/reconcile.py): no PIT download, no backtest — use it for a quick
# "is the live executor matching the model?" pass once the data root is already
# current. Everything else routes to the full three-way
# (scripts/reconcile_three_way.py).
#
# Thin dispatcher so the invocation stays trivial and the orchestration logic
# lives in well-tested Python. Read-only against the VPS, demo only, never real
# money. Full design: docs/pit_gate.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
if [ -x "$HERE/.venv/Scripts/python.exe" ]; then
  PY="$HERE/.venv/Scripts/python.exe"
elif [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
else
  PY="python3"
fi

export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# --quick / --two-way -> the fast paper<->demo execution check. Parse the mode
# flag independent of position so ordinary argparse-style option ordering works
# (for example, `--dry-run --quick`). Do not forward the dispatcher-only flag.
mode="full"
forward_args=()
for arg in "$@"; do
  case "$arg" in
    --quick|--two-way)
      mode="quick"
      ;;
    *)
      forward_args+=("$arg")
      ;;
  esac
done
if [ "$mode" = "quick" ]; then
  exec "$PY" "$HERE/scripts/reconcile.py" "${forward_args[@]}"
fi

# Default: the full demo<->backtest<->paper three-way (the whole reconciliation).
exec "$PY" "$HERE/scripts/reconcile_three_way.py" "$@"
