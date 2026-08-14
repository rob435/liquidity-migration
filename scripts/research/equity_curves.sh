#!/usr/bin/env bash
# One command -> standard equity curves, from the LONG strategy module and the
# registered CARRY research config.
#
#   bash scripts/research/equity_curves.sh                 # the LONG sleeve, last 3 years
#   bash scripts/research/equity_curves.sh --sleeves carry
#   bash scripts/research/equity_curves.sh --sleeves long,carry
#   bash scripts/research/equity_curves.sh --years 2       # shorter window (lighter on RAM)
#   bash scripts/research/equity_curves.sh --help          # all options
#
# Thin wrapper around scripts/research/equity_curves.py.
# POLARS_MAX_THREADS is capped because the full-PIT roots are memory-heavy on a 16 GB box.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$HERE/.venv/bin/python"
# Windows venvs place the interpreter under Scripts/ instead of bin/.
[ -x "$PY" ] || PY="$HERE/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python3"

export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-6}"
exec "$PY" "$HERE/scripts/research/equity_curves.py" "$@"
