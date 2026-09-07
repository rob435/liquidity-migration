#!/usr/bin/env bash
# The weekly rehearsal and the one-command demo rollback use the same release switch.
set -euo pipefail
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${CHAOS_DRILL_PYTHON:-/opt/liquidity-migration/.venv/bin/python}"
exec "$PYTHON" "$REPOSITORY_ROOT/scripts/runtime/demo_rollback.py" "$@"
