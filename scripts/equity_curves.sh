#!/usr/bin/env bash
# One command -> standard equity curves.
# LONG and CONTINUOUS come from their strategy modules. CONTINUOUS
# output is demo/forward analysis, not a mainnet approval package.
#
#   bash scripts/equity_curves.sh                 # the LONG sleeve, last 3 years
#   bash scripts/equity_curves.sh --sleeves continuous
#   bash scripts/equity_curves.sh --sleeves continuous --chart-leverage 2.5
#   bash scripts/equity_curves.sh --sleeves long,continuous
#   bash scripts/equity_curves.sh --years 2       # shorter window (lighter on RAM)
#   bash scripts/equity_curves.sh --help          # all options
#
# Thin wrapper around scripts/equity_curves.py. CONTINUOUS delegates to the
# continuous refresh runner so it stays tied to the live/demo construction.
# POLARS_MAX_THREADS is capped because the full-PIT roots are memory-heavy on a 16 GB box.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
# Windows venvs place the interpreter under Scripts/ instead of bin/.
[ -x "$PY" ] || PY="$HERE/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python3"

export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-6}"
exec "$PY" "$HERE/scripts/equity_curves.py" "$@"
