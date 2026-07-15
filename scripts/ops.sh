#!/usr/bin/env bash
# One operator-facing entry point for routine demo/paper operations.
#
# This file is intentionally a thin router. The canonical scripts retain all
# validation, reconciliation, research-integrity, and deploy logic.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_TARGET="${SSH_TARGET:-root@116.202.15.128}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
else
  PYTHON_BIN="python3"
fi

# The canonical VPS scripts consume these environment variables.
export SSH_TARGET REPO_DIR

usage() {
  cat <<'EOF'
Usage: scripts/ops.sh <command> [arguments]

Safe operator commands:
  status [ARGS...]             read-only VPS verification
  equity [ARGS...]             official equity-curve runner
  reset [ARGS...]              remote ledger-reset preview (dry-run by default)
  data-audit [ARGS...]         read-only granular/PIT coverage audit
  data-build --execute [...]   explicit granular-data download/backfill
  tail-plan [ARGS...]          validate and print the preregistered tail-run plan
  tail-run [ARGS...]           run the preregistered tail-survival experiment
  overhaul-plan [ARGS...]      strategy-overhaul shallow readiness plan
  overhaul-phase0 [ARGS...]    outcome-blind strategy-overhaul inventory
  event-parity [ARGS...]       compare bound historical/paper/demo event tapes
  target-replay [ARGS...]      replay a frozen target capture offline in 3 modes
  account-replay [ARGS...]     replay frozen demo account inputs into 2 local roots
  account-parity [ARGS...]     compare historical/paper/demo account journals
  account-parity-scope [ARGS...]
                               freeze source-bound kernel-parity scope
  natural-freeze [ARGS...]     produce/verify source-backed natural cutover freeze
  natural-run-config [ARGS...] bind the freeze to canonical natural runtime paths
  natural-effective-config [ARGS...]
                               bind the exact LONG/CONT runtime configurations
  natural-sufficiency [ARGS...] verify fixed 120h event/lifecycle evidence floors
  clock-offset --execute [...] capture VPS-vs-Bybit public clock evidence
  clock-series [ARGS...]       bind periodic public clock receipts to the window
  demo-calibration --execute   publish bounded target-only demo calibration tape
  natural-safety-flatten --execute
                               publish captured post-T1 demo zero targets
  twin-calibrate [ARGS...]     calibrate the execution twin from demo tapes
  twin-drift [ARGS...]         verify freeze-bound archived-V7 vs natural drift
  v7-archive [ARGS...]         materialize stopped V7 sources and freeze source map
  stopped-epoch [ARGS...]      create/verify the stopped natural-source seal
  fresh-deploy-epoch [ARGS...] create/verify fresh roots derived from that seal
  fresh-deploy-env [ARGS...]   materialize/verify bound per-unit fresh-root overrides
  authorized-deploy-epoch [ARGS...]
                               prepare/verify the authority-bound stopped/fresh epoch
  venue-accounting [ARGS...]   capture/reconcile read-only demo accounting evidence
  cutover-authority [ARGS...]  review/issue/verify evidence-bound deploy authority
  test [PYTEST_ARGS...]        run pytest
  deploy --execute [ARGS...]   checked VPS deploy; explicit handshake required
  help                         show this help and do nothing else

Environment overrides:
  SSH_TARGET   VPS SSH destination (default: root@116.202.15.128)
  REPO_DIR     repository path on the VPS (default: /opt/liquidity-migration)
  PYTHON       one Python executable/path for data, tail, overhaul, and test commands

Safety contract:
  * This interface never enables REAL_MONEY or mainnet trading.
  * reset is a remote dry-run unless --execute reaches the guarded reset script.
  * clock-offset/demo-calibration require --execute and run on the VPS clock.
  * natural-safety-flatten requires --execute, but can only write RISK zero targets.
  * deploy refuses unless its first argument is exactly --execute.
  * Research runs remain research artifacts and are never auto-promoted.

Details: docs/operations.md
EOF
}

die_usage() {
  echo "ERROR: $*" >&2
  echo >&2
  usage >&2
  exit 2
}

remote_reset() {
  local -a reset_args=("$@")
  local arg has_execute=0 has_dry_run=0
  for arg in "${reset_args[@]:-}"; do
    if [[ "$arg" == "--execute" ]]; then
      has_execute=1
    elif [[ "$arg" == "--dry-run" ]]; then
      has_dry_run=1
    fi
  done

  # Make the safe default explicit at the remote boundary. The canonical reset
  # script independently defaults to dry-run and requires --execute as well.
  if (( has_execute == 0 && has_dry_run == 0 )); then
    reset_args=(--dry-run "${reset_args[@]}")
  fi

  # Serialize values as Bash literals, then reconstruct a remote array. This
  # preserves argument boundaries (including spaces/metacharacters) without
  # eval. The only parsed source is generated by Bash's own printf %q.
  {
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'RESET_ARGS=('
    for arg in "${reset_args[@]}"; do
      printf ' %q' "$arg"
    done
    printf ' )\n'
    cat <<'REMOTE_SCRIPT'
set -euo pipefail
cd "$REPO_DIR"
exec bash scripts/reset_demo_paper_ledgers.sh "${RESET_ARGS[@]}"
REMOTE_SCRIPT
  } | ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$SSH_TARGET" bash -s
}

remote_python_script() {
  local script_path="$1"
  shift
  local -a script_args=("$@")
  local arg
  {
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'SCRIPT_PATH=%q\n' "$script_path"
    printf 'SCRIPT_ARGS=('
    for arg in "${script_args[@]}"; do
      printf ' %q' "$arg"
    done
    printf ' )\n'
    cat <<'REMOTE_SCRIPT'
set -euo pipefail
cd "$REPO_DIR"
exec .venv/bin/python "$SCRIPT_PATH" "${SCRIPT_ARGS[@]}"
REMOTE_SCRIPT
  } | ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$SSH_TARGET" bash -s
}

command="${1:-help}"
if [[ "$#" -gt 0 ]]; then
  shift
fi

case "$command" in
  help|-h|--help)
    usage
    ;;
  status)
    exec "$ROOT_DIR/scripts/verify_vps_live.sh" "$@"
    ;;
  equity)
    exec bash "$ROOT_DIR/scripts/equity_curves.sh" "$@"
    ;;
  reset)
    remote_reset "$@"
    ;;
  data-audit)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/granular_data_surface.py" "$@"
    ;;
  data-build)
    [[ "${1:-}" == "--execute" ]] \
      || die_usage "data-build performs network/backfill writes; its first argument must be --execute"
    shift
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/granular_data_surface.py" --execute "$@"
    ;;
  tail-plan)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/continuous_tail_survival_2026_07_10.py" --plan "$@"
    ;;
  tail-run)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/continuous_tail_survival_2026_07_10.py" "$@"
    ;;
  overhaul-plan)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/strategy_overhaul_scout_2026_07_10.py" --plan "$@"
    ;;
  overhaul-phase0)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/strategy_overhaul_scout_2026_07_10.py" --phase0-inventory "$@"
    ;;
  event-parity)
    exec "$PYTHON_BIN" -m liquidity_migration.strategy_event_parity "$@"
    ;;
  target-replay)
    exec "$PYTHON_BIN" -m liquidity_migration.strategy_target_replay "$@"
    ;;
  account-replay)
    exec "$PYTHON_BIN" -m liquidity_migration.captured_account_replay "$@"
    ;;
  account-parity)
    exec "$PYTHON_BIN" -m liquidity_migration.kernel_parity "$@"
    ;;
  account-parity-scope)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/build_kernel_parity_scope.py" "$@"
    ;;
  natural-freeze)
    exec "$PYTHON_BIN" -m liquidity_migration.natural_cutover_freeze_manifest "$@"
    ;;
  natural-run-config)
    exec "$PYTHON_BIN" -m liquidity_migration.natural_run_config "$@"
    ;;
  natural-effective-config)
    exec "$PYTHON_BIN" -m liquidity_migration.natural_effective_config "$@"
    ;;
  natural-sufficiency)
    exec "$PYTHON_BIN" -m liquidity_migration.natural_tape_sufficiency "$@"
    ;;
  clock-offset)
    [[ "${1:-}" == "--execute" ]] \
      || die_usage "clock-offset writes a VPS-bound receipt; its first argument must be --execute"
    shift
    remote_python_script scripts/capture_bybit_clock_offset.py "$@"
    ;;
  clock-series)
    exec "$PYTHON_BIN" -m liquidity_migration.clock_offset_series "$@"
    ;;
  demo-calibration)
    [[ "${1:-}" == "--execute" ]] \
      || die_usage "demo-calibration emits demo orders; its first argument must be --execute"
    shift
    remote_python_script scripts/run_demo_execution_calibration.py \
      --confirm-demo-calibration "$@"
    ;;
  natural-safety-flatten)
    [[ "${1:-}" == "--execute" ]] \
      || die_usage "natural-safety-flatten publishes demo zero targets; its first argument must be --execute"
    shift
    remote_python_script scripts/publish_natural_safety_flatten.py \
      --confirm-demo-safety-flatten "$@"
    ;;
  twin-calibrate)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/calibrate_execution_twin.py" "$@"
    ;;
  twin-drift)
    exec "$PYTHON_BIN" -m liquidity_migration.execution_twin_drift "$@"
    ;;
  v7-archive)
    exec "$PYTHON_BIN" -m liquidity_migration.v7_archive_materialization "$@"
    ;;
  stopped-epoch)
    exec "$PYTHON_BIN" -m liquidity_migration.stopped_natural_epoch "$@"
    ;;
  fresh-deploy-epoch)
    exec "$PYTHON_BIN" -m liquidity_migration.fresh_deploy_epoch "$@"
    ;;
  fresh-deploy-env)
    exec "$PYTHON_BIN" -m liquidity_migration.fresh_deploy_environment "$@"
    ;;
  authorized-deploy-epoch)
    exec "$PYTHON_BIN" -m liquidity_migration.authorized_deploy_epoch "$@"
    ;;
  venue-accounting)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/reconcile_bybit_demo_accounting.py" "$@"
    ;;
  cutover-authority)
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/account_execution_cutover_authority.py" "$@"
    ;;
  test)
    exec "$PYTHON_BIN" -m pytest "$@"
    ;;
  deploy)
    [[ "${1:-}" == "--execute" ]] \
      || die_usage "deploy is mutating; its first argument must be --execute"
    shift
    exec "$ROOT_DIR/scripts/deploy_vps_live.sh" "$@"
    ;;
  *)
    die_usage "unknown command '$command'"
    ;;
esac
