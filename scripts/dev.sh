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
                         LM_TARGET_FREE_GIB (30) GiB free. Supplies and prints a
                         pytest --basetemp outside the repository; one given in
                         the arguments or PYTEST_BASETEMP is checked the same
                         way, and one inside the repository is refused
  help                   show this help

Environment:
  PYTHON              explicit Python executable; defaults to the repository .venv
  CARGO_TARGET_DIR    the Cargo target directory prune measures and cleans
                      (default engine/target)
  LM_TARGET_FREE_GIB  free-space floor, in GiB, under which check prunes (30)
  PYTEST_BASETEMP     explicit check basetemp; one inside the repository is
                      refused, as is an explicit --basetemp argument there

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
  scripts/runtime/engine_status.py
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

# ", "-joined list. ${array[*]} joins on IFS's first character only, which
# would drop the space.
join_comma() {
  local out="" item
  for item in "$@"; do
    if [[ -z "$out" ]]; then out="$item"; else out="$out, $item"; fi
  done
  printf '%s' "$out"
}

# `check`'s pytest basetemp. Fixtures build Git repositories and large trees,
# so one inside the checkout would be found by repository walks and left behind:
# that is refused, not relocated. PYTEST_BASETEMP overrides the default.
resolve_pytest_basetemp() {
  local candidate real_root parent
  real_root="$(cd "$ROOT_DIR" && pwd -P)"
  candidate="${PYTEST_BASETEMP:-}"
  if [[ -z "$candidate" ]]; then
    local tmp_root="${TMPDIR:-/tmp}"
    mkdir -p "$tmp_root"
    tmp_root="$(cd "$tmp_root" && pwd -P)"
    candidate="$tmp_root/liquidity-migration-pytest-$(date +%Y%m%d%H%M%S)-$$"
  fi
  parent="$(dirname "$candidate")"
  mkdir -p "$parent"
  parent="$(cd "$parent" && pwd -P)"
  candidate="$parent/$(basename "$candidate")"
  case "$candidate" in
    "$real_root"|"$real_root"/*)
      echo "[dev] refusing pytest basetemp inside repository: $candidate" >&2
      return 1
      ;;
  esac
  mkdir -p "$candidate"
  printf '%s\n' "$candidate"
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
    # An explicit --basetemp is lifted out of the arguments and resolved like
    # the default one, so the containment check cannot be bypassed by naming
    # a path. Before any gate runs: a refused basetemp must not cost a full doctor.
    pytest_args=()
    explicit_basetemp=""
    expect_basetemp=0
    for arg in "$@"; do
      if [[ "$expect_basetemp" -eq 1 ]]; then
        explicit_basetemp="$arg"
        expect_basetemp=0
        continue
      fi
      case "$arg" in
        --basetemp) expect_basetemp=1 ;;
        --basetemp=*) explicit_basetemp="${arg#--basetemp=}" ;;
        *) pytest_args+=("$arg") ;;
      esac
    done
    if [[ "$expect_basetemp" -eq 1 ]]; then
      echo "[dev] --basetemp needs a path" >&2
      exit 2
    fi
    pytest_basetemp="$(PYTEST_BASETEMP="${explicit_basetemp:-${PYTEST_BASETEMP:-}}" resolve_pytest_basetemp)"
    pytest_args+=(--basetemp "$pytest_basetemp")
    echo "[dev] pytest basetemp: $pytest_basetemp"
    ran=()
    skipped=()
    echo "[dev] repository doctor"
    "$PYTHON_BIN" scripts/devtools/repo_doctor.py --repo "$ROOT_DIR"
    ran+=(doctor)
    echo "[dev] ruff"
    "$PYTHON_BIN" -m ruff check liquidity_migration market_tape scripts tests deploy
    ran+=(ruff)
    if command -v shellcheck >/dev/null 2>&1; then
      echo "[dev] shellcheck"
      git ls-files -z -- "${SHELL_FILES[@]}" | xargs -0 shellcheck -S warning
      ran+=(shellcheck)
    else
      echo "[dev] shellcheck skipped (not installed; CI runs it)"
      skipped+=("shellcheck (not installed)")
    fi
    echo "[dev] mypy"
    "$PYTHON_BIN" -m mypy "${MYPY_TARGETS[@]}"
    ran+=(mypy)
    echo "[dev] pytest"
    "$PYTHON_BIN" -m pytest -q "${pytest_args[@]}"
    ran+=(pytest)
    # The Cargo workspace root is engine/, not the repository root.
    if [[ ! -f "$ROOT_DIR/engine/Cargo.toml" ]]; then
      echo "[dev] engine tests skipped (no engine workspace)"
      skipped+=("engine (no workspace)")
    elif ! command -v cargo >/dev/null 2>&1; then
      echo "[dev] engine tests skipped (no cargo toolchain)"
      skipped+=("engine (no cargo toolchain)")
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
      ran+=("cargo fmt" "cargo clippy" "cargo test")
    fi
    ran_list="$(join_comma "${ran[@]}")"
    if [[ "${#skipped[@]}" -eq 0 ]]; then
      skipped_list="none"
    else
      skipped_list="$(join_comma "${skipped[@]}")"
    fi
    echo "[dev] check complete; ran: $ran_list; skipped: $skipped_list"
    ;;
  *)
    echo "ERROR: unknown developer command '$command'" >&2
    usage >&2
    exit 2
    ;;
esac
