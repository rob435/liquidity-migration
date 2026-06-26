#!/usr/bin/env bash
# One command -> official equity curves.
# LONG and CONTINUOUS come from liquidity_migration/promoted.py. CONTINUOUS
# output is demo/forward analysis, not a mainnet approval package.
#
#   bash scripts/equity_curves.sh                 # the promoted LONG sleeve, last 3 years
#   bash scripts/equity_curves.sh --sleeves continuous
#   bash scripts/equity_curves.sh --sleeves continuous --chart-leverage 2.5
#   bash scripts/equity_curves.sh --sleeves long,continuous
#   bash scripts/equity_curves.sh --years 2       # shorter window (lighter on RAM)
#   bash scripts/equity_curves.sh --help          # all options
#
# Thin wrapper around scripts/equity_curves.py. LONG runs from
# liquidity_migration/promoted.py; CONTINUOUS delegates to the continuous refresh
# runner so it stays tied to the live/demo continuous construction.
# POLARS_MAX_THREADS is capped because the full-PIT roots are memory-heavy on a 16 GB box.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-6}"
exec "$PY" "$HERE/scripts/equity_curves.py" "$@"
