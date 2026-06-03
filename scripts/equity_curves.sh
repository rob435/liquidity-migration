#!/usr/bin/env bash
# One command → the promoted-profile equity curve for every sleeve.
#
#   bash scripts/equity_curves.sh                 # all sleeves, last 3 years
#   bash scripts/equity_curves.sh --sleeves short # one sleeve
#   bash scripts/equity_curves.sh --years 2       # shorter window (lighter on RAM)
#   bash scripts/equity_curves.sh --help          # all options
#
# Thin wrapper around scripts/equity_curves.py. Each curve runs the EXACT deployed
# profile from liquidity_migration/promoted.py — the one place "what's promoted" lives.
# POLARS_MAX_THREADS is capped because the full-PIT roots are memory-heavy on a 16 GB box.
set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-6}"
exec "$PY" "$HERE/scripts/equity_curves.py" "$@"
