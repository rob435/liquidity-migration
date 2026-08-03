#!/usr/bin/env bash
# Non-operational developer entry point: doctor, lint, types, tests.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
else
  PYTHON_BIN="python3"
fi

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh <command> [arguments]

Non-operational developer commands:
  doctor [--json] [--strict-lock]
                         inspect Git, Python, dependency, skill, and map state
  lint [RUFF_ARGS...]    run Ruff over package, scripts, and tests
  types [MYPY_ARGS...]   run package and supported developer-script mypy
  test [PYTEST_ARGS...]  run pytest (-q by default)
  check [PYTEST_ARGS...] run doctor, Ruff, mypy, and pytest in sequence
  help                   show this help

Environment:
  PYTHON  explicit Python executable; defaults to the repository .venv

Operational and research commands intentionally live elsewhere:
  scripts/ops.sh --help
  python -m liquidity_migration --help
EOF
}

cd "$ROOT_DIR"

# One list, used by both `types` and `check`.
MYPY_TARGETS=(
  liquidity_migration
  scripts/devtools/repo_doctor.py
  scripts/research/build_trade_diagnostics.py
  scripts/data/build_candidate_tape.py
  scripts/runtime/check_fleet_liveness.py
)

command="${1:-help}"
if [[ "$#" -gt 0 ]]; then
  shift
fi

case "$command" in
  help|-h|--help)
    usage
    ;;
  doctor)
    exec "$PYTHON_BIN" scripts/devtools/repo_doctor.py --repo "$ROOT_DIR" "$@"
    ;;
  lint)
    exec "$PYTHON_BIN" -m ruff check liquidity_migration scripts tests "$@"
    ;;
  types)
    exec "$PYTHON_BIN" -m mypy "${MYPY_TARGETS[@]}" "$@"
    ;;
  test)
    exec "$PYTHON_BIN" -m pytest -q "$@"
    ;;
  check)
    echo "[dev] repository doctor"
    "$PYTHON_BIN" scripts/devtools/repo_doctor.py --repo "$ROOT_DIR"
    echo "[dev] ruff"
    "$PYTHON_BIN" -m ruff check liquidity_migration scripts tests
    echo "[dev] mypy"
    "$PYTHON_BIN" -m mypy "${MYPY_TARGETS[@]}"
    echo "[dev] pytest"
    "$PYTHON_BIN" -m pytest -q "$@"
    ;;
  *)
    echo "ERROR: unknown developer command '$command'" >&2
    usage >&2
    exit 2
    ;;
esac
