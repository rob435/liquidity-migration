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
  prune                  delete the workspace crates' debug build artifacts
                         (cargo clean -p per member; dependency builds stay)
  check [PYTEST_ARGS...] run doctor, Ruff, ShellCheck, mypy, pytest, and the
                         engine's rustfmt, clippy, and tests in sequence;
                         prunes first when the target volume has under
                         LM_TARGET_FREE_GIB (30) GiB free
  help                   show this help

Environment:
  PYTHON              explicit Python executable; defaults to the repository .venv
  CARGO_TARGET_DIR    the Cargo target directory prune measures and cleans
                      (default engine/target)
  LM_TARGET_FREE_GIB  free-space floor, in GiB, under which check prunes (30)

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
  scripts/release_artifact.py
  scripts/data/build_candidate_tape.py
  scripts/runtime/check_fleet_liveness.py
  scripts/runtime/reclaim_host_storage.py
  deploy/grafana/render_dashboard.py
)

# Without rustup the local cargo ignores rust-toolchain.toml, so CI's pinned
# clippy can refuse what a newer local clippy accepts.
use_rustup_cargo() {
  if command -v rustup >/dev/null 2>&1; then
    local rustup_cargo_dir
    rustup_cargo_dir="$(dirname "$(rustup which cargo)")"
    export PATH="$rustup_cargo_dir:$PATH"
  fi
}

engine_target_dir() { printf '%s\n' "${CARGO_TARGET_DIR:-$ROOT_DIR/engine/target}"; }

# Whole GiB free on the volume holding the Cargo target directory.
engine_target_free_gib() {
  local dir
  dir="$(engine_target_dir)"
  mkdir -p "$dir"
  df -Pk "$dir" | awk 'NR == 2 { printf "%d\n", $4 / 1048576 }'
}

# Repeated gates pile up the workspace crates' own debug artifacts by the tens
# of GiB, and cargo never garbage-collects. Cleaning the members by name keeps
# the third-party dependency builds, so the next gate rebuilds the workspace only.
prune_engine_target() {
  local member
  local -a clean_args=()
  while IFS= read -r member; do
    [[ -n "$member" ]] && clean_args+=(-p "$member")
  done < <(
    cd "$ROOT_DIR/engine" && cargo metadata --no-deps --format-version 1 --locked \
      | "$PYTHON_BIN" -c 'import json, sys; print("\n".join(p["name"] for p in json.load(sys.stdin)["packages"]))'
  )
  if [[ "${#clean_args[@]}" -eq 0 ]]; then
    echo "ERROR: cargo metadata listed no workspace members" >&2
    return 1
  fi
  (cd "$ROOT_DIR/engine" && cargo clean --profile dev "${clean_args[@]}")
  rm -rf "$(engine_target_dir)/debug/incremental"
}

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
    exec "$PYTHON_BIN" -m ruff check liquidity_migration market_tape scripts tests deploy "$@"
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
  prune)
    if ! command -v cargo >/dev/null 2>&1; then
      echo "ERROR: prune needs a cargo toolchain" >&2
      exit 2
    fi
    use_rustup_cargo
    echo "[dev] cargo prune: $(engine_target_free_gib) GiB free before"
    prune_engine_target
    echo "[dev] cargo prune: $(engine_target_free_gib) GiB free after"
    ;;
  check)
    echo "[dev] repository doctor"
    "$PYTHON_BIN" scripts/devtools/repo_doctor.py --repo "$ROOT_DIR"
    echo "[dev] ruff"
    "$PYTHON_BIN" -m ruff check liquidity_migration market_tape scripts tests deploy
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
      use_rustup_cargo
      free_gib="$(engine_target_free_gib)"
      if [[ "$free_gib" -lt "${LM_TARGET_FREE_GIB:-30}" ]]; then
        echo "[dev] cargo prune: ${free_gib} GiB free, under the ${LM_TARGET_FREE_GIB:-30} GiB floor"
        prune_engine_target
      fi
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
