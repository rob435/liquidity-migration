#!/usr/bin/env bash
# One thin operator-facing router for the surviving demo and research operations.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_TARGET="${SSH_TARGET:-root@208.84.103.4}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
LM_FLEET_MANIFEST="$ROOT_DIR/deploy/fleet_manifest.tsv"
LM_REALM_FIELDS="$ROOT_DIR/deploy/realm_fields.tsv"

# Read before the first command that can fail: the report below names it.
command="${1:-help}"
if [[ "$#" -gt 0 ]]; then
  shift
fi

# A failing step names the operation rather than exiting silently. No secret
# reaches this router -- credential files travel by path -- so the failed command
# is safe to print. Only the outermost report is printed: inside a subshell the
# parent decides what a non-zero status means.
ops_error() {
  local status="$1" line="$2" failed="$3"
  [ "$BASH_SUBSHELL" -eq 0 ] || exit "$status"
  echo "ERROR: ops.sh $command failed (exit $status) at line $line: $failed" >&2
  exit "$status"
}
trap 'ops_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

. "$ROOT_DIR/deploy/lib_sleeves.sh"

# The realms and the funded modes an operator may name, from the generated realm
# fields. Each list is derived on its first use and kept, so a command that names
# no realm never reads the table.
realm_list() {
  if [ -z "${REALM_LIST:-}" ]; then
    REALM_LIST="$(lm_realms | paste -sd ' ' -)"
  fi
}

deploy_modes() {
  local funded_realm
  if [ -z "${DEPLOY_MODES:-}" ]; then
    DEPLOY_MODES="deploy rollback verify"
    for funded_realm in $(lm_funded_realms); do
      DEPLOY_MODES="$DEPLOY_MODES stop-$funded_realm disarm-$funded_realm"
    done
  fi
}

# The canary's accounts: the practice realm, and every funded realm on a venue
# with no practice sibling, whose only route to live evidence the canary is.
# The engine refuses a live-proven realm and a running engine holds the lease,
# so this list is the typo guard, not the gate.
canary_realms() {
  local practice practice_venue funded_realm
  if [ -z "${CANARY_REALMS:-}" ]; then
    practice="$(lm_practice_realm)"
    practice_venue="$(lm_realm_field "$practice" venue)"
    CANARY_REALMS="$practice"
    for funded_realm in $(lm_funded_realms); do
      if [ "$(lm_realm_field "$funded_realm" venue)" != "$practice_venue" ]; then
        CANARY_REALMS="$CANARY_REALMS $funded_realm"
      fi
    done
  fi
}

require_realm() {
  lm_is_realm "$2" && return 0
  realm_list
  die_usage "$1 realm must be one of: $REALM_LIST"
}

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

Operator commands:
  status                       read-only VPS verification
  units                        list the fleet's units and timers
  logs UNIT [LINES]            one unit's journal (default 100 lines)
  restart UNIT...              restart units
  stop UNIT...                 stop units
  start UNIT...                start units
  equity [ARGS...]             standard descriptive equity curves (research)
  execution-study [--json]     read the latest paired execution cost report
  storage [plan]               the host storage reclaimer's receipt and
                               status.json, read on the host. `plan` re-measures
                               the budget and reports what one run would reclaim
                               without pruning, uploading or deleting anything
  curve [REALM] [SAMPLES]      the live account's recorded equity curve, read
                               on the host (default: mainnet, 240 minutes;
                               REALM is a row of deploy/realms.tsv)
  why [REALM]                  why one engine is not trading, read on the host
                               from its heartbeat: health, exposure, blockers
                               (default: mainnet)
  alert-drill [--scope host|REALM]
                               prove the on-call routes end to end, on the host:
                               one Telegram test message, one no-op incident
                               routine fire, one dead-man ping, and nothing else.
                               Only the host scope carries all three routes;
                               see docs/notifications.md §Drill
  flatten --environment REALM [--reason TEXT] [--execute]
                               ask each native directional reducer to close its
                               attributed exposure through durable Rust control
                               commands. Reports without --execute; the signal
                               worker stays live while exits complete
  attest-flat --environment REALM
                               run the installed Rust adapter's credential-wide
                               two-scan flatness proof (read-only)
  verify-account-identity --environment REALM
                               authenticate the realm's read-only probe and
                               print the account id the engine binds
  canary-order --environment REALM --symbol SYMBOL
               --expected-user-id ID [--execute]
                               one bounded live order lifecycle on the realm's
                               account: rest one minimum post-only order away
                               from the touch, cancel it, prove the account
                               clean twice. Without --execute nothing is sent
  research-refresh [ARGS...]   append-first data/features/backtest workflow
  real-money preflight[-REALM] report every remaining arming step for one
                               funded account (read-only). Unsuffixed is the
                               funded Bybit account
  real-money render-profile [--execute --output PATH]
                               render the operational profile from the
                               RM_* dials in the funded credential file
  deploy [MODE]                MODE is deploy (default), rollback, verify, or
                               stop-REALM/disarm-REALM for a funded realm;
                               rollback deploys the last commit whose deploy
                               finished
  help                         show this help and do nothing else

Every REALM above is a row of deploy/realms.tsv; `deploy` and the funded
verbs accept its funded rows.

A UNIT that does not already start with `liquidity-migration-` gets the prefix:
`logs signal-worker-demo.service` reads
`liquidity-migration-signal-worker-demo.service`.

Environment overrides:
  SSH_TARGET   VPS SSH destination (default: root@208.84.103.4)
  REPO_DIR     repository path on the VPS (default: /opt/liquidity-migration)
  PYTHON       Python executable/path for local tools and tests

Mutating verbs run against the live host; see docs/operations.md.
EOF
}

die_usage() {
  echo "ERROR: $*" >&2
  echo >&2
  usage >&2
  exit 2
}

# One remote entry point. Values are serialized as Bash literals and
# reconstructed as a remote array, which preserves argument boundaries
# (including spaces and metacharacters) without eval: the only parsed source is
# generated by Bash's own printf %q.
remote_exec() {
  local script="$1"
  shift
  local -a remote_args=("$@")
  local arg
  {
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'REMOTE_ARGS=('
    for arg in ${remote_args[@]+"${remote_args[@]}"}; do
      printf ' %q' "$arg"
    done
    printf ' )\n'
    printf 'set -euo pipefail\n'
    printf '%s\n' "$script"
  } | ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$SSH_TARGET" bash -s
}

qualify_unit() {
  case "$1" in
    liquidity-migration-*) printf '%s' "$1" ;;
    *) printf 'liquidity-migration-%s' "$1" ;;
  esac
}

qualified_units() {
  local unit
  QUALIFIED_UNITS=()
  for unit in "$@"; do
    QUALIFIED_UNITS+=("$(qualify_unit "$unit")")
  done
}

remote_python_module() {
  local module="$1"
  shift
  remote_exec 'cd "$REPO_DIR"
exec .venv/bin/python -m "${REMOTE_ARGS[@]}"' "$module" "$@"
}

# remote_engine_control REALM MODE [ARGS...]
#   attest-flat and verify-account-identity are read-only and run with the
#   arming switch removed. canary-order keeps REAL_MONEY: a live-canary realm's
#   gateway refuses to build without it, and the command places one order.
#   Every realm value comes from deploy/realms.tsv; the attestor swap is the
#   only choice left to the host, because only the host knows if the owner put
#   a read-only credential file there.
remote_engine_control() {
  local realm="$1" mode="$2"
  shift 2
  remote_exec '
realm="${REMOTE_ARGS[0]}"
mode="${REMOTE_ARGS[1]}"
env_file="${REMOTE_ARGS[2]}"
credential_file="${REMOTE_ARGS[3]}"
inventory_credential_set="${REMOTE_ARGS[4]}"
runtime_user="${REMOTE_ARGS[5]}"
state_dir="${REMOTE_ARGS[6]}"
unset_environment="${REMOTE_ARGS[7]}"
attestor_file="${REMOTE_ARGS[8]}"
attestor_unset_environment="${REMOTE_ARGS[9]}"
engine_args=("${REMOTE_ARGS[@]:10}")
engine_binary=/opt/liquidity-migration-engine/bin/engine

# A funded realm whose venue publishes a read-only key prefers it when the
# owner has installed one; that run holds no write key at all.
if [ -n "$attestor_file" ] && [ -e "$attestor_file" ]; then
  credential_file="$attestor_file"
  inventory_credential_set=attestor
  unset_environment="$attestor_unset_environment"
fi

# The read-only modes write nothing, so the sandbox stays read-only. The canary
# takes the account lease, a kernel lock on a file under the fleet lock root.
writable_paths=""
case "$mode" in
  attest-flat|verify-account-identity) ;;
  canary-order)
    unset_environment="$(printf "%s\n" $unset_environment | grep -vx REAL_MONEY | tr "\n" " ")"
    writable_paths=/run/lock/liquidity-migration
    ;;
  *) echo "invalid engine-control mode: $mode" >&2; exit 2 ;;
esac

[ -x "$engine_binary" ] \
  || { echo "installed Rust engine is missing: $engine_binary" >&2; exit 3; }
for path in "$env_file" "$credential_file"; do
  [ -f "$path" ] \
    || { echo "engine-control input is missing: $path" >&2; exit 3; }
done

# systemd parses the private EnvironmentFiles, then drops privileges. The
# command receives one explicitly selected credential file. For the read-only
# modes the Rust inventory type exposes no mutation method even on the
# execution file. Secrets never enter this router or its argv.
exec systemd-run --quiet --wait --pipe --collect --service-type=exec \
    --unit="liquidity-migration-${mode}-${realm}-$$" \
    --property="User=$runtime_user" \
    --property="Group=liquidity-migration" \
    --property="WorkingDirectory=$state_dir" \
    --property="EnvironmentFile=$env_file" \
    --property="EnvironmentFile=$credential_file" \
    --property="Environment=BYBIT_INVENTORY_CREDENTIAL_SET=$inventory_credential_set" \
    --property="UnsetEnvironment=$unset_environment" \
    --property=NoNewPrivileges=true \
    --property=PrivateTmp=true \
    --property=ProtectSystem=strict \
    --property="ReadWritePaths=$writable_paths" \
    --property=ProtectHome=true \
    --property=UMask=0027 \
    "$engine_binary" "$mode" ${engine_args[@]+"${engine_args[@]}"}
' "$realm" "$mode" \
    "$(lm_realm_field "$realm" engine_env)" \
    "$(lm_realm_field "$realm" credential_env)" \
    "$(lm_realm_field "$realm" inventory_credential_set)" \
    "$(lm_realm_field "$realm" engine_user)" \
    "$(lm_realm_field "$realm" engine_state_dir)" \
    "$(lm_realm_field "$realm" control_unset)" \
    "$(lm_realm_field "$realm" attestor_env)" \
    "$(lm_realm_field "$realm" control_unset_attestor)" \
    "$@"
}

case "$command" in
  help|-h|--help)
    usage
    ;;
  status)
    exec "$ROOT_DIR/scripts/deploy_vps_live.sh" verify
    ;;
  units)
    lm_validate_fleet_manifest || die_usage "fleet manifest is invalid"
    FLEET_UNITS=()
    while IFS= read -r unit; do
      FLEET_UNITS+=("$unit")
    done < <(lm_expected_systemd_units)
    [[ "${#FLEET_UNITS[@]}" -gt 0 ]] || die_usage "fleet manifest has no current units"
    remote_exec 'systemctl list-units "${REMOTE_ARGS[@]}" --all --no-legend --no-pager --plain
systemctl list-timers "${REMOTE_ARGS[@]}" --all --no-pager' "${FLEET_UNITS[@]}"
    ;;
  logs)
    [[ "$#" -ge 1 ]] || die_usage "logs requires a unit name"
    remote_exec 'exec journalctl -u "${REMOTE_ARGS[0]}" -n "${REMOTE_ARGS[1]}" --no-pager -o short-iso' \
      "$(qualify_unit "$1")" "${2:-100}"
    ;;
  restart|stop|start)
    [[ "$#" -ge 1 ]] || die_usage "$command requires at least one unit name"
    qualified_units "$@"
    for unit in "${QUALIFIED_UNITS[@]}"; do
      [[ "$unit" =~ ^liquidity-migration-[A-Za-z0-9_.@:-]+$ ]] \
        || die_usage "invalid systemd unit name '$unit'"
    done
    remote_exec "exec systemctl $command \"\${REMOTE_ARGS[@]}\"" "${QUALIFIED_UNITS[@]}"
    ;;
  equity)
    exec bash "$ROOT_DIR/scripts/research/equity_curves.sh" "$@"
    ;;
  curve)
    # The live account's own recorded curve, read on the host from the file
    # the minute recorder appends to. Read-only, and it says nothing about
    # research backtests -- that is `equity` above.
    curve_realm="${1:-mainnet}"
    require_realm curve "$curve_realm"
    curve_samples="${2:-240}"
    [[ "$curve_samples" =~ ^[1-9][0-9]*$ ]] || die_usage "curve samples must be a positive integer"
    remote_exec 'exec /opt/liquidity-migration-engine/bin/engine-tools record-equity \
      --show "${REMOTE_ARGS[0]}" --samples "${REMOTE_ARGS[1]}"' "$curve_realm" "$curve_samples"
    ;;
  alert-drill)
    # The delivery drill from docs/notifications.md, run where the private
    # files are. Read-only apart from the three messages the drill itself
    # sends. PID 1 reads the credential files; no value enters this router, the
    # argv, or the output.
    drill_scope=host
    while [[ "$#" -gt 0 ]]; do
      case "$1" in
        --scope) drill_scope="${2:-}"; shift 2 ;;
        *) die_usage "alert-drill does not take '$1'" ;;
      esac
    done
    if [[ "$drill_scope" != host ]]; then
      require_realm alert-drill "$drill_scope"
      # `run_delivery_drill` in check_fleet_liveness.py answers any other scope
      # with "delivery drill requires --account-scope host": the dead-man is the
      # host scope's alone, and no realm scope pings it.
      die_usage "alert-drill --scope must be host; the realm scopes carry no dead-man route"
    fi
    # The unit name carries this shell's pid: a leftover unit from an
    # interrupted drill would otherwise refuse the start.
    remote_exec 'exec systemd-run --wait --pipe --collect \
  --unit="liquidity-migration-oncall-drill-$$" \
  --property=Type=oneshot \
  --property=User=liquidity-observer \
  --property=Group=liquidity-migration \
  --property="WorkingDirectory=$REPO_DIR" \
  --property=EnvironmentFile=/etc/liquidity-migration/notifications.env \
  --property=EnvironmentFile=/etc/liquidity-migration/oncall.env \
  "$REPO_DIR/.venv/bin/python" \
  "$REPO_DIR/scripts/runtime/check_fleet_liveness.py" \
  --account-scope "${REMOTE_ARGS[0]}" --require-oncall --delivery-drill' "$drill_scope"
    ;;
  why)
    # The engine's own heartbeat, read where it is written. Read-only: no venue
    # call, no unit change, nothing written.
    why_realm="${1:-mainnet}"
    require_realm why "$why_realm"
    why_heartbeat="$(lm_realm_field "$why_realm" engine_heartbeat)"
    remote_exec 'exec python3 "$REPO_DIR/scripts/runtime/engine_status.py" "${REMOTE_ARGS[0]}"' \
      "$why_heartbeat"
    ;;
  execution-study)
    [[ "$#" -le 1 ]] || die_usage "execution-study accepts only --json"
    study_file=latest.txt
    if [[ "$#" -eq 1 ]]; then
      [[ "$1" == --json ]] || die_usage "execution-study accepts only --json"
      study_file=latest.json
    fi
    remote_exec 'cat -- "/var/lib/liquidity-migration/execution-study/${REMOTE_ARGS[0]}"' "$study_file"
    ;;
  storage)
    # Read-only both ways. Without an argument this prints the last successful
    # run's receipt and the reclaimer's own status file. `plan` runs the
    # reclaimer with --dry-run, which measures the budget and reports the
    # candidates without pruning, uploading or unlinking anything.
    [[ "$#" -le 1 ]] || die_usage "storage accepts only plan"
    case "${1:-report}" in
      report)
        remote_exec 'cat -- /var/lib/liquidity-migration/receipts/storage-reclaim.last-success
exec python3 -m json.tool /var/lib/liquidity-migration/storage-reclaim/status.json'
        ;;
      plan)
        remote_exec 'cd "$REPO_DIR"
exec .venv/bin/python scripts/runtime/reclaim_host_storage.py "${REMOTE_ARGS[@]}"' \
          --dry-run --json
        ;;
      *) die_usage "storage accepts only plan" ;;
    esac
    ;;
  research-refresh)
    exec bash "$ROOT_DIR/scripts/research/research_refresh.sh" "$@"
    ;;
  real-money)
    # The arming surface. `preflight` reads only and never prints
    # a secret. `render-profile` without --execute prints the profile to
    # stdout; with --execute it writes exactly one non-secret artifact and
    # refuses any dial set that does not pass the load-time envelope proof.
    # Neither command sets REAL_MONEY, writes a credential, or starts a unit.
    # No argument means preflight, and it must be passed: an empty argv reaches
    # the module with no subcommand and argparse answers with a usage error.
    if [[ "$#" -eq 0 ]]; then
      set -- preflight
    fi
    real_money_subcommands="render-profile"
    for funded_realm in $(lm_funded_realms); do
      real_money_subcommands="$real_money_subcommands $(lm_realm_field "$funded_realm" preflight_command)"
    done
    case " $real_money_subcommands " in
      *" ${1:-} "*) ;;
      *) die_usage "real-money subcommand must be one of:$(printf ' %s' $real_money_subcommands)" ;;
    esac
    # LOCAL=1 runs it against this checkout instead of the VPS, so the dials
    # can be proved before anything is copied to the host.
    if [[ "${LOCAL:-0}" == "1" ]]; then
      exec "$PYTHON_BIN" -m liquidity_migration.policy.real_money_arming "$@"
    fi
    remote_python_module liquidity_migration.policy.real_money_arming "$@"
    ;;
  flatten)
    # On the engine's own path: durably disable entries, submit one replayable
    # flatten request per native directional reducer, then wait for the engine
    # heartbeat to show no attributed position or opening work.
    #
    # Dry run unless --execute, and made explicit here as well as in the script
    # so neither side alone can turn a report into a close.
    flatten_args=("$@")
    has_execute=0
    for arg in ${flatten_args[@]+"${flatten_args[@]}"}; do
      [ "$arg" = "--execute" ] && has_execute=1
    done
    if (( has_execute == 0 )); then
      flatten_args=(--dry-run ${flatten_args[@]+"${flatten_args[@]}"})
    fi
    # The script body must consume REMOTE_ARGS itself: remote_exec serializes
    # the arguments into a remote shell array, and a bare path here runs the
    # script with no argv at all — which flatten_account.sh refuses.
    remote_exec 'exec bash "$REPO_DIR/scripts/vps/flatten_account.sh" "${REMOTE_ARGS[@]}"' \
      ${flatten_args[@]+"${flatten_args[@]}"}
    ;;
  attest-flat)
    [[ "$#" -eq 2 && "$1" == "--environment" ]] \
      || die_usage "attest-flat requires --environment REALM"
    require_realm attest-flat "$2"
    remote_engine_control "$2" attest-flat
    ;;
  verify-account-identity)
    [[ "$#" -eq 2 && "$1" == "--environment" ]] \
      || die_usage "verify-account-identity requires --environment REALM"
    require_realm verify-account-identity "$2"
    remote_engine_control "$2" verify-account-identity
    ;;
  canary-order)
    canary_environment="" canary_symbol="" canary_user_id="" canary_execute=""
    while [[ "$#" -gt 0 ]]; do
      case "$1" in
        --environment) canary_environment="${2:-}"; shift 2 ;;
        --symbol) canary_symbol="${2:-}"; shift 2 ;;
        --expected-user-id) canary_user_id="${2:-}"; shift 2 ;;
        --execute) canary_execute=1; shift ;;
        *) die_usage "canary-order does not take '$1'" ;;
      esac
    done
    # A realm the fleet already trades is never the canary's account; the
    # engine refuses a live-proven one too, but a typo should stop here, before
    # the host.
    canary_realms
    case " $CANARY_REALMS " in
      *" $canary_environment "*) ;;
      *) die_usage "canary-order requires --environment$(printf ' %s' $CANARY_REALMS)" ;;
    esac
    [[ -n "$canary_symbol" && -n "$canary_user_id" ]] \
      || die_usage "canary-order requires --symbol SYMBOL and --expected-user-id ID"
    remote_engine_control "$canary_environment" canary-order \
      --symbol "$canary_symbol" --expected-user-id "$canary_user_id" \
      ${canary_execute:+--execute}
    ;;
  deploy)
    # A leading --execute is accepted and discarded, for callers that still pass it.
    if [[ "${1:-}" == "--execute" ]]; then
      shift
    fi
    deploy_modes
    case " $DEPLOY_MODES " in
      *" ${1:-deploy} "*) ;;
      *) die_usage "deploy mode must be one of:$(printf ' %s' $DEPLOY_MODES)" ;;
    esac
    exec "$ROOT_DIR/scripts/deploy_vps_live.sh" "${1:-deploy}"
    ;;
  *)
    die_usage "unknown command '$command'"
    ;;
esac
