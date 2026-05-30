#!/usr/bin/env bash
# One-command demo-forward reconciliation (short sleeve).
#
#   bash scripts/reconcile.sh                 # the whole pipeline, sane defaults
#   bash scripts/reconcile.sh --dry-run       # print every command, run nothing
#   bash scripts/reconcile.sh --no-pull       # use local ledgers as-is
#   bash scripts/reconcile.sh --diagnostic    # current-universe membership (biased)
#   bash scripts/reconcile.sh --with-bybit    # also reconcile demo<->Bybit
#   bash scripts/reconcile.sh --help          # all options
#
# Thin wrapper around scripts/reconcile.py so the invocation stays trivial and
# the orchestration logic lives in well-tested Python. See docs/pit_gate.md.
set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" "$HERE/scripts/reconcile.py" "$@"
