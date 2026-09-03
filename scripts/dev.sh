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
  shellcheck [ARGS...]   run ShellCheck (warning level) over every tracked
                         shell script
  types [MYPY_ARGS...]   run package and supported developer-script mypy
  test [PYTEST_ARGS...]  run pytest (-q by default)
  check [PYTEST_ARGS...] run doctor, Ruff, ShellCheck, mypy, pytest, and the
                         engine's rustfmt, clippy, and tests in sequence
  help                   show this help

Environment:
  PYTHON  explicit Python executable; defaults to the repository .venv

Operational and research commands intentionally live elsewhere:
  scripts/ops.sh --help
  python -m liquidity_migration --help
EOF
}

cd "$ROOT_DIR"

# Every tracked shell file, for both `shellcheck` and `check`.
SHELL_FILES=('*.sh' '*.command' 'scripts/git-hooks/*')

# One list, used by both `types` and `check`.
MYPY_TARGETS=(
  --exclude
  '^liquidity_migration/research/venue_wal_accounting\.py$'
  liquidity_migration
  market_tape
  liquidity_migration/research/venue_wal_accounting.py
  scripts/research/capture_bybit_account_history.py
  scripts/research/reconcile_venue_wal.py
  scripts/devtools/repo_doctor.py
  scripts/data/build_candidate_tape.py
  scripts/runtime/check_fleet_liveness.py
  scripts/runtime/record_equity.py
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
    exec "$PYTHON_BIN" -m ruff check liquidity_migration market_tape scripts tests "$@"
    ;;
  shellcheck)
    git ls-files -z -- "${SHELL_FILES[@]}" | xargs -0 shellcheck -S warning "$@"
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
    "$PYTHON_BIN" -m ruff check liquidity_migration market_tape scripts tests
    if command -v shellcheck >/dev/null 2>&1; then
      echo "[dev] shellcheck"
      git ls-files -z -- "${SHELL_FILES[@]}" | xargs -0 shellcheck -S warning
    else
      echo "[dev] shellcheck skipped (not installed; CI runs it)"
    fi
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
      # Without rustup the local cargo ignores rust-toolchain.toml, so CI's
      # pinned clippy can refuse what a newer local clippy accepts.
      echo "[dev] cargo fmt"
      (cd "$ROOT_DIR/engine" && cargo fmt --all -- --check)
      echo "[dev] cargo clippy"
      (cd "$ROOT_DIR/engine" && cargo clippy --workspace --all-targets --quiet -- -D warnings)
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
