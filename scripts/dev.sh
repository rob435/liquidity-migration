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
                         inspect Git, Python, dependency, and skill-link state
  lint [RUFF_ARGS...]    run Ruff over package, scripts, and tests
  types [MYPY_ARGS...]   run package and supported developer-script mypy
  test [PYTEST_ARGS...]  run pytest (-q by default)
  check [PYTEST_ARGS...] run doctor, Ruff, mypy, pytest, and the engine's
                         Rust tests in sequence
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
  --exclude
  '^liquidity_migration/research/venue_wal_accounting\.py$'
  liquidity_migration
  liquidity_migration/research/venue_wal_accounting.py
  scripts/research/capture_bybit_account_history.py
  scripts/research/reconcile_venue_wal.py
  scripts/devtools/repo_doctor.py
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
    # The Cargo workspace root is engine/, not the repository root.
    if [[ ! -f "$ROOT_DIR/engine/Cargo.toml" ]]; then
      echo "[dev] engine tests skipped (no engine workspace)"
    elif ! command -v cargo >/dev/null 2>&1; then
      echo "[dev] engine tests skipped (no cargo toolchain)"
    else
      echo "[dev] cargo test"
      (cd "$ROOT_DIR/engine" && cargo test --workspace --quiet)
    fi
    ;;
  *)
    echo "ERROR: unknown developer command '$command'" >&2
    usage >&2
    exit 2
    ;;
esac
