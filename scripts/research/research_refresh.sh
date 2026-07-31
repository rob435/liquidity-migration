#!/usr/bin/env bash
# One command for append-first market refresh, current features, standard
# backtests, and optional demo/paper/backtest reconciliation.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$PYTHON_BIN" "$ROOT/scripts/research/research_refresh.py" "$@"
