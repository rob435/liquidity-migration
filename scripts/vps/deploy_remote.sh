#!/usr/bin/env bash
# Sent by deploy_vps_live.sh after its quoted runtime variables.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

fail() { echo "deploy failed: $*" >&2; exit 1; }

# ------------------------------------------------------------- realm facts

# The generated realm facts and their helpers are shipped with this script, so
# every realm fact below is this commit's whatever the host's checkout still
# holds, and the steps that run before fetch_exact_commit can read them.
[ -n "${LM_REALM_FIELDS_TEXT:-}" ] || fail "deploy_vps_live.sh shipped no realm fields"
[ -n "${LM_REALMS_SH:-}" ] || fail "deploy_vps_live.sh shipped no realm helpers"
export LM_REALM_FIELDS_TEXT
eval "$LM_REALMS_SH"

# The realm the deploy soaks on before any funded handover.
PRACTICE_REALM="$(lm_practice_realm)"
[ -n "$PRACTICE_REALM" ] || fail "the realm fields name no practice realm"
# Where the operational dials live: the funded Bybit credential file, for every
# realm's profile.
DIALS_REALM=mainnet

# ---------------------------------------------------------------- constants

RUNTIME_GROUP=liquidity-migration
CONTROLS_GROUP=liquidity-controls
CONTROLS_USER=liquidity-controls
SIGNAL_WORKER_USER=liquidity-signal-worker
OBSERVER_USER=liquidity-observer
LLM_USER=liquidity-llm
CAPTURE_USER=liquidity-capture

RELEASE_DIR=/opt/liquidity-migration-engine
ENGINE_BINARY=$RELEASE_DIR/bin/engine
ENGINE_TOOLS_BINARY=$RELEASE_DIR/bin/engine-tools
SIGNAL_WORKER_BINARY=$RELEASE_DIR/bin/signal-worker
ENGINE_CONTROL_HELPER=$RELEASE_DIR/bin/telegram-control-helper
# The commit whose deploy last finished, and the one before it: what rollback
# returns to. Seeded from the checkout when no record exists yet.
DEPLOYED_COMMIT_FILE=$RELEASE_DIR/deployed-commit
PREVIOUS_COMMIT_FILE=$RELEASE_DIR/previous-commit
# Each recorder unit records what it was last started from in
# $RELEASE_DIR/<unit>.fingerprint; a deploy that changes none of it leaves
# that recorder running.
CONTROLS_SUDOERS=/etc/sudoers.d/liquidity-migration-controls
QUALIFIED_RELEASE_DIR=""
CANDIDATE_RELEASE_DIR=""
INCUMBENT_STAGE=""
SOAK_OVERRIDE=20-demo-soak.conf
# How long a unit must hold one main process after publishing a fresh
# heartbeat. Longer than the engine's and the signal worker's restart cycle
# (RestartSec=5 plus the seconds each spends before it aborts).
HEARTBEAT_SETTLE_SECONDS=12
# A worker replaces its own stream when the instrument lane lands and reads
# degraded for the seconds the socket is down; one heartbeat is not a verdict.
HEARTBEAT_UNHEALTHY_SAMPLES=5

NOTIFICATIONS_ENVIRONMENT=/etc/liquidity-migration/notifications.env
ONCALL_ENVIRONMENT=/etc/liquidity-migration/oncall.env
LEGACY_LIVENESS_ENVIRONMENT=/etc/liquidity-migration/liveness.env
SIGNAL_SPOOL_ROOT=/var/lib/liquidity-migration/signals
CONTROL_SPOOL_ROOT=/var/lib/liquidity-migration/controls
# What `verify_mode` breaks the filesystem down by. A `capture-disk` page turns
# on which writer holds the disk — the recorders block above a floor they share
# with every other writer — and the `df` line alone cannot name one. Read-only,
# one level deep, no filenames: directory totals only.
# `/` and `/var/lib` are here so the totals account for the whole filesystem:
# without them a consumer outside the fleet's own trees is invisible, and the
# page cannot be closed. `du -x` keeps each walk on this filesystem.
DISK_REPORT_ROOTS=${DISK_REPORT_ROOTS:-/ /var/lib /var/lib/liquidity-migration /var/log/journal /opt}
#: Directory totals printed per `verify_mode`, largest first.
DISK_REPORT_LINES=${DISK_REPORT_LINES:-30}
# `verify_mode` reads journals for the units it expects to be running, so a
# failed unit outside that list — the backup, the tape upload, a research
# timer — reaches an on-call session as a name under `systemctl --failed` and
# nothing else. These bound one journal read per failed fleet unit.
FAILED_UNIT_JOURNAL_LINES=${FAILED_UNIT_JOURNAL_LINES:-20}
FAILED_UNIT_REPORT_MAX=${FAILED_UNIT_REPORT_MAX:-5}

PYTHON="$REPO_DIR/.venv/bin/python"

# ------------------------------------------------------------------- locks

if [ "$MODE" != verify ]; then
    install -d -m 0755 /run/liquidity-migration
    exec 9> /run/liquidity-migration/deploy.lock
    flock -n 9 || fail "another deploy is already running"
fi

# --------------------------------------------------------------------- git

git_authorized() {
    if [ -n "$GITHUB_TOKEN" ] && [[ "$REPO_URL" == https://github.com/* ]]; then
        # Keep the credential off argv: a 0600 config file written by a shell
        # builtin puts only its path on the command line.
        local auth config_file status=0
        auth="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
        config_file="$(mktemp)" || fail "cannot create the authenticated git config"
        chmod 0600 "$config_file"
        printf '[http "https://github.com/"]\n\textraheader = AUTHORIZATION: Basic %s\n' \
            "$auth" > "$config_file"
        GIT_CONFIG_GLOBAL="$config_file" GIT_TERMINAL_PROMPT=0 \
            git -C "$REPO_DIR" "$@" || status=$?
        rm -f "$config_file"
        return "$status"
    fi
    GIT_TERMINAL_PROMPT=0 git -C "$REPO_DIR" "$@"
}

fetch_exact_commit() {
    [ -d "$REPO_DIR/.git" ] || fail "$REPO_DIR is not a git checkout"
    git_authorized fetch --no-tags "$REMOTE" "$BRANCH" \
        || fail "cannot fetch $REMOTE/$BRANCH"
    git -C "$REPO_DIR" merge-base --is-ancestor \
        "$EXPECTED_COMMIT" "refs/remotes/$REMOTE/$BRANCH" \
        || fail "EXPECTED_COMMIT $EXPECTED_COMMIT is not on $REMOTE/$BRANCH"
    if [ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$EXPECTED_COMMIT" ] \
        && git -C "$REPO_DIR" merge-base --is-ancestor "$EXPECTED_COMMIT" HEAD; then
        rollback_runtime_compatible "$EXPECTED_COMMIT" \
            || fail "an older deploy has the same compatibility requirements as rollback; use a forward repair"
    fi
    prepare_recorder_runtime
    git -C "$REPO_DIR" checkout -B "$BRANCH" "$EXPECTED_COMMIT" \
        || fail "cannot check out $EXPECTED_COMMIT"
    [ "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$EXPECTED_COMMIT" ] \
        || fail "checkout is not at EXPECTED_COMMIT"
    # The remote body runs from the ssh login directory. Every
    # `python -m liquidity_migration.*` below resolves the package from the
    # working directory alone: the venv installs requirements-runtime.lock with
    # --no-deps and never the project, and there is no PYTHONPATH.
    cd "$REPO_DIR" || fail "cannot enter $REPO_DIR"
}

# ----------------------------------------------------------------- helpers

credential_armed() {
    local credential="$1" value
    [ -f "$credential" ] || return 1
    value="$(
        sed -n 's/^REAL_MONEY=\([^#]*\).*/\1/p' "$credential" \
            | head -1 | tr -d "\"' " | tr '[:upper:]' '[:lower:]'
    )"
    case "$value" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac
}

realm_armed() { credential_armed "$(lm_realm_field "$1" credential_env)"; }

# The installed engine's own evidence gate for one funded realm. `engine run`
# accepts live-proven and, as the owner's forward test, live-canary; it refuses
# every other readiness, so starting those units would fail the handover and
# roll the fleet back. The realm stays stopped instead, whatever its arming
# switch says. Sets FUNDED_REALM_READINESS to what the binary printed, or
# `unknown` when it printed nothing.
realm_run_ready() {
    local realm="$1" venue_name
    venue_name="$(lm_realm_field "$realm" engine_venue)" \
        || fail "unsupported readiness realm: $realm"
    FUNDED_REALM_READINESS="$(
        "$ENGINE_BINARY" venues 2>/dev/null \
            | awk -F '\t' -v name="$venue_name" '$1 == name { print $5 }'
    )"
    FUNDED_REALM_READINESS="${FUNDED_REALM_READINESS:-unknown}"
    case "$FUNDED_REALM_READINESS" in
        live-proven|live-canary) return 0 ;;
        *) return 1 ;;
    esac
}

# The owner's credential file for one funded realm.
funded_credential_env() {
    [ "$(lm_realm_field "$1" kind 2>/dev/null)" = funded ] \
        || fail "unsupported funded realm: $1"
    lm_realm_field "$1" credential_env
}

# Wait for a heartbeat this run's process wrote. `since` is read before the
# unit starts, so a file left by the previous generation cannot satisfy it.
# The engine and the signal worker both write the heartbeat before they read
# the state that can abort them, so a crash loop publishes a fresh heartbeat
# on every restart: freshness alone cannot tell a live unit from a restarting
# one. The unit must also hold one main process across HEARTBEAT_SETTLE_SECONDS.
wait_fresh_heartbeat() {
    local unit="$1" heartbeat="$2" since="$3" _attempt written pid restarts restarting=0 unhealthy=0
    for _attempt in $(seq 1 90); do
        if [ "$(systemctl show --property=ActiveState --value "$unit")" = "active" ] \
            && [ -f "$heartbeat" ]; then
            written="$(stat -c %Y "$heartbeat")"
            pid="$(systemctl show --property=MainPID --value "$unit")"
            restarts="$(systemctl show --property=NRestarts --value "$unit")"
            if [ "$written" -ge "$since" ] && [ "${pid:-0}" != "0" ]; then
                sleep "$HEARTBEAT_SETTLE_SECONDS"
                if [ "$(systemctl show --property=ActiveState --value "$unit")" = "active" ] \
                    && [ "$(systemctl show --property=MainPID --value "$unit")" = "$pid" ] \
                    && [ "$(systemctl show --property=NRestarts --value "$unit")" = "$restarts" ]; then
                    if "$PYTHON" "$REPO_DIR/scripts/runtime/check_fleet_liveness.py" \
                        --check-heartbeat "$unit" "$heartbeat" "$pid" "$since"; then
                        echo "heartbeat-ok unit=$unit age=$(( $(date +%s) - written ))s pid=$pid"
                        return 0
                    fi
                    unhealthy=$((unhealthy + 1))
                    [ "$unhealthy" -lt "$HEARTBEAT_UNHEALTHY_SAMPLES" ] \
                        || fail "$unit published an unhealthy heartbeat after startup"
                    echo "heartbeat-unhealthy unit=$unit sample=$unhealthy pid=$pid; sampling again"
                    continue
                fi
                restarting=1
                continue
            fi
        fi
        sleep 2
    done
    [ "$restarting" -eq 0 ] \
        || fail "$unit restarts after each heartbeat at $heartbeat"
    fail "$unit did not publish a fresh heartbeat at $heartbeat"
}

capture_status_ready() {
    "$PYTHON" - "$1" "$2" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    expected_pid = int(sys.argv[2])
except (OSError, ValueError):
    raise SystemExit(1)
if not isinstance(payload, dict):
    raise SystemExit(1)
last_receive = payload.get("last_receive_ns")
shards = payload.get("shards")
ready = (
    payload.get("pid") == expected_pid
    and expected_pid > 0
    and isinstance(last_receive, (int, float))
    and not isinstance(last_receive, bool)
    and last_receive > 0
    and isinstance(shards, list)
    and any(isinstance(shard, dict) and shard.get("connected") is True for shard in shards)
)
raise SystemExit(0 if ready else 1)
PY
}

wait_capture_ready() {
    local unit="$1" heartbeat="$2" since="$3" _attempt written main_pid
    for _attempt in $(seq 1 90); do
        if systemctl is-active --quiet "$unit" && [ -f "$heartbeat" ]; then
            main_pid="$(systemctl show --property=MainPID --value "$unit")"
            written="$(stat -c %Y "$heartbeat")"
            if [ "$written" -ge "$since" ] \
                && capture_status_ready "$heartbeat" "$main_pid"; then
                echo "capture-ready unit=$unit pid=$main_pid age=$(( $(date +%s) - written ))s"
                return 0
            fi
        fi
        sleep 2
    done
    fail "$unit did not publish a received-frame status at $heartbeat"
}

start_unit() {
    systemctl enable --now "$1" || fail "cannot start $1"
}

# ------------------------------------------------------------- generations

seed_generation_record() {
    [ -f "$DEPLOYED_COMMIT_FILE" ] && return 0
    local head
    head="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
    [ -n "$head" ] || return 0
    install -d -o root -g root -m 0755 "$RELEASE_DIR"
    printf '%s\n' "$head" > "$DEPLOYED_COMMIT_FILE"
}

record_generation() {
    local deployed=""
    [ -f "$DEPLOYED_COMMIT_FILE" ] && deployed="$(cat "$DEPLOYED_COMMIT_FILE")"
    if [ -n "$deployed" ] && [ "$deployed" != "$EXPECTED_COMMIT" ]; then
        printf '%s\n' "$deployed" > "$PREVIOUS_COMMIT_FILE"
    fi
    printf '%s\n' "$EXPECTED_COMMIT" > "$DEPLOYED_COMMIT_FILE"
}

# The commit a rollback returns to. A checkout that moved past the last
# finished deploy is a deploy that failed: go back to the finished one. A
# checkout at the last finished deploy goes back to the one before it.
rollback_target() {
    local deployed="" previous="" head
    [ -f "$DEPLOYED_COMMIT_FILE" ] && deployed="$(cat "$DEPLOYED_COMMIT_FILE")"
    [ -f "$PREVIOUS_COMMIT_FILE" ] && previous="$(cat "$PREVIOUS_COMMIT_FILE")"
    head="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
    if [ -n "$deployed" ] && [ "$deployed" != "$head" ]; then
        printf '%s\n' "$deployed"
    elif [ -n "$previous" ]; then
        printf '%s\n' "$previous"
    else
        return 1
    fi
}

rollback_runtime_compatible() {
    local target="$1" current deployed runtime
    current="$(git -C "$REPO_DIR" rev-parse --verify HEAD)" || return 1
    deployed="$(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || true)"
    # The worker has no read-only checkpoint compatibility command. Its
    # check-config only checks configuration; an older WAL reader may refuse
    # state that this generation has already committed.
    for runtime in "$current" "$deployed"; do
        [ -n "$runtime" ] || continue
        if ! git -C "$REPO_DIR" diff --quiet "$target" "$runtime" -- \
            engine rust-toolchain.toml .cargo .github/workflows/vps-deploy.yml; then
            echo "rollback refused: $target has different or unavailable runtime inputs from $runtime; current binaries, services and durable state are unchanged; use a forward repair" >&2
            return 1
        fi
    done
}

rollback_after_failure() {
    local realm="$1" failed="$EXPECTED_COMMIT" target
    if [ "${AUTO_ROLLBACK:-0}" = 1 ]; then
        fail "$realm did not come up on the rolled-back commit $failed either; inspect the installed runtime and use a forward repair"
    fi
    target="$(rollback_target)" \
        || fail "$realm did not come up on $failed and no earlier finished deploy is recorded"
    [ "$target" != "$failed" ] \
        || fail "$realm did not come up on $failed and the only recorded generation is that commit"
    rollback_runtime_compatible "$target" \
        || fail "$realm did not come up on $failed; $failed remains installed for a forward repair"
    echo "deploy failed: $realm did not come up on $failed; rolling back to $target" >&2
    AUTO_ROLLBACK=1 EXPECTED_COMMIT="$target" deploy_mode
    fail "$failed did not come up; the fleet runs $target again"
}

# ------------------------------------------------------- identities & dirs

ensure_runtime_identities() {
    getent group "$RUNTIME_GROUP" >/dev/null || groupadd --system "$RUNTIME_GROUP"
    getent group "$CONTROLS_GROUP" >/dev/null || groupadd --system "$CONTROLS_GROUP"
    id -u "$CONTROLS_USER" >/dev/null 2>&1 \
        || useradd --system --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --gid "$CONTROLS_GROUP" "$CONTROLS_USER"
    local user realm
    for user in "$SIGNAL_WORKER_USER" "$OBSERVER_USER" "$LLM_USER" "$CAPTURE_USER" \
        $(for realm in $(lm_realms); do lm_realm_field "$realm" engine_user; done); do
        id -u "$user" >/dev/null 2>&1 \
            || useradd --system --no-create-home --home-dir /nonexistent \
                --shell /usr/sbin/nologin --gid "$RUNTIME_GROUP" "$user"
    done
    install -d -o root -g root -m 0755 /etc/tmpfiles.d
    printf 'd /run/liquidity-migration 0755 root root -\nd /run/lock/liquidity-migration 0770 root %s -\nf /run/lock/liquidity-migration-ledger-reset.lock 0600 root root -\n' \
        "$RUNTIME_GROUP" > /etc/tmpfiles.d/liquidity-migration.conf
    systemd-tmpfiles --create /etc/tmpfiles.d/liquidity-migration.conf \
        || fail "cannot create the runtime lock directories"
    install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/targets
    install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0770 "$SIGNAL_SPOOL_ROOT"
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$CONTROL_SPOOL_ROOT"
    local engine_user
    for realm in $(lm_realms); do
        engine_user="$(lm_realm_field "$realm" engine_user)"
        install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0770 \
            "$(lm_realm_field "$realm" spool_dir)"
        install -d -o "$engine_user" -g "$RUNTIME_GROUP" -m 0750 \
            "$(lm_realm_field "$realm" control_dir)"
        install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0750 \
            "$(lm_realm_field "$realm" worker_state_dir)"
        install -d -o "$engine_user" -g "$RUNTIME_GROUP" -m 0750 \
            "$(lm_realm_field "$realm" engine_state_dir)"
    done
    install -d -o "$LLM_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/llm-driver-ledger
    install -d -o "$CAPTURE_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/forward-market
    # The backup and upload receipts; the host watchdog reads their ages.
    install -d -o root -g root -m 0755 /var/lib/liquidity-migration/receipts
    local sleeve
    for realm in $(lm_realms); do
        for sleeve in long_root carry_root exodus_root; do
            install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0750 \
                "$(lm_realm_field "$realm" "$sleeve")"
        done
    done
}

# ------------------------------------------------------------ build/install

python_requirements_path() {
    if [ -f "$REPO_DIR/requirements-runtime.lock" ]; then
        printf '%s\n' "$REPO_DIR/requirements-runtime.lock"
    else
        printf '%s\n' "$REPO_DIR/requirements.lock"
    fi
}

install_python_environment() {
    [ -d "$REPO_DIR/.venv" ] || /usr/bin/python3 -m venv "$REPO_DIR/.venv" \
        || fail "cannot create the Python environment"
    "$PYTHON" -m pip install --disable-pip-version-check --no-deps \
        --only-binary=:all: -r "$(python_requirements_path)" \
        || fail "cannot install locked Python dependencies"
    if [ -f "$REPO_DIR/requirements-runtime.lock" ]; then
        "$PYTHON" "$REPO_DIR/scripts/vps/sync_runtime_dependencies.py" "$REPO_DIR/requirements-runtime.lock" \
            || fail "cannot remove non-runtime Python dependencies"
    fi
}

release_artifact() {
    [ -n "${RELEASE_ARTIFACT_PY:-}" ] || fail "missing release artifact verifier"
    printf '%s\n' "$RELEASE_ARTIFACT_PY" | /usr/bin/python3 -I - "$@"
}

cleanup_release() {
    if [ -n "$QUALIFIED_RELEASE_DIR" ]; then
        rm -rf -- "$QUALIFIED_RELEASE_DIR"
        QUALIFIED_RELEASE_DIR=""
    fi
    if [ -n "$INCUMBENT_STAGE" ]; then
        rm -rf -- "$INCUMBENT_STAGE"
        INCUMBENT_STAGE=""
    fi
}
trap cleanup_release EXIT

build_engine() {
    local staged_tar="$RELEASE_DIR/staged/${EXPECTED_COMMIT}.tar.gz"
    [ -f "$staged_tar" ] || fail "missing release artifact $staged_tar; host compilation is disabled"
    cleanup_release
    QUALIFIED_RELEASE_DIR="$(mktemp -d "$RELEASE_DIR/staged/.qualified.XXXXXX")" \
        || fail "cannot create a fresh release extraction directory"
    release_artifact unpack --commit "$EXPECTED_COMMIT" --artifact "$staged_tar" \
        --require-candidate --output "$QUALIFIED_RELEASE_DIR" >/dev/null \
        || fail "release artifact verification failed for $EXPECTED_COMMIT"
    echo "deploy: release bytes verified for $EXPECTED_COMMIT"
}

stop_realm_units() {
    local realm="$1" unit
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        systemctl stop "$unit" || fail "cannot stop $unit for the $realm handover"
        systemctl reset-failed "$unit" 2>/dev/null || true
    done < <(lm_realm_units "$realm")
}

# Freeze what one armed funded realm runs from, so a restart during the demo
# soak cannot pick up the candidate binaries the soak has not qualified yet. A
# realm with no rendered config or worker environment has never been
# provisioned on this host and has no incumbent to pin.
pin_realm_runtime() {
    local realm="$1" engine_config worker_env owner_unit worker_unit
    [ "$(lm_realm_field "$realm" kind 2>/dev/null)" = funded ] \
        || fail "unsupported pinned realm: $realm"
    engine_config="$(lm_realm_field "$realm" engine_config)"
    worker_env="$(lm_realm_field "$realm" worker_env)"
    owner_unit="$(lm_realm_field "$realm" engine_unit)"
    worker_unit="$(lm_realm_field "$realm" worker_unit)"
    credential_armed "$(funded_credential_env "$realm")" || return 0
    [ -f "$engine_config" ] && [ -f "$worker_env" ] || return 0
    cd "$REPO_DIR" || fail "cannot read the incumbent runtime inputs"
    local pinned="$RELEASE_DIR/incumbent-$realm" unit binary pid source
    if [ ! -d "$pinned" ]; then
        INCUMBENT_STAGE="$(mktemp -d "$RELEASE_DIR/.incumbent-$realm.XXXXXX")" \
            || fail "cannot stage incumbent $realm runtime"
        chmod 0755 "$INCUMBENT_STAGE"
        for binary in engine signal-worker; do
            case "$binary" in
                engine) unit="$owner_unit" ;;
                signal-worker) unit="$worker_unit" ;;
            esac
            pid="$(systemctl show --property=MainPID --value "$unit")"
            source="$RELEASE_DIR/bin/$binary"
            if [ "${pid:-0}" != 0 ]; then source="/proc/$pid/exe"; fi
            install -o root -g "$RUNTIME_GROUP" -m 0755 "$source" "$INCUMBENT_STAGE/$binary" \
                || fail "cannot retain the incumbent $realm $binary"
        done
        "$PYTHON" - "$INCUMBENT_STAGE" "$pinned" "$engine_config" "$worker_env" "$worker_unit" <<'PY'
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment

directory = Path(sys.argv[1])
final_directory = Path(sys.argv[2])
raw = subprocess.check_output([
    "systemctl", "show", sys.argv[5], "--property=Environment", "--value",
], text=True)
values = dict(word.split("=", 1) for word in shlex.split(raw) if "=" in word)
values["ENGINE_CONFIG_FILE"] = sys.argv[3]
values["OPERATIONAL_PROFILE_FILE"] = load_private_systemd_environment(Path(sys.argv[4]))["OPERATIONAL_PROFILE_FILE"]
lines = []
for key in ("SIGNAL_WORKER_CONFIG_FILE", "LONG_NATIVE_RULE_FILE", "CARRY_SIGNAL_CONFIG_FILE",
            "ENGINE_CONFIG_FILE", "OPERATIONAL_PROFILE_FILE"):
    source = Path(values[key])
    if not source.is_absolute():
        raise SystemExit(f"incumbent worker input {key} is not absolute")
    target = directory / (key.lower() + (".toml" if key == "ENGINE_CONFIG_FILE" else ".json"))
    shutil.copyfile(source, target)
    target.chmod(0o640)
    lines.append("Environment=" + json.dumps(key + "=" + str(final_directory / target.name)))
(directory / "worker-inputs.conf").write_text("\n".join(lines) + "\n")
PY
        chgrp -R "$RUNTIME_GROUP" "$INCUMBENT_STAGE" || fail "cannot secure incumbent $realm inputs"
        mv -- "$INCUMBENT_STAGE" "$pinned" || fail "cannot publish incumbent $realm runtime"
        INCUMBENT_STAGE=""
    fi
    [ -f "$pinned/worker-inputs.conf" ] || fail "incomplete incumbent $realm snapshot at $pinned"
    for unit in "$owner_unit" "$worker_unit"; do
        install -d -m 0755 "$LM_SYSTEMD_UNIT_DIR/$unit.d"
    done
    cat > "$LM_SYSTEMD_UNIT_DIR/$owner_unit.d/$SOAK_OVERRIDE" <<EOF
[Service]
Type=simple
WatchdogSec=0
ExecStart=
ExecStart=$pinned/engine run --config $pinned/engine_config_file.toml
EOF
    {
        cat <<EOF
[Service]
ExecStart=
ExecStart=$pinned/signal-worker live --signal-config $pinned/signal_worker_config_file.json --long-rule $pinned/long_native_rule_file.json --carry-config $pinned/carry_signal_config_file.json --operational-config $pinned/operational_profile_file.json --engine-config $pinned/engine_config_file.toml --spool-dir \${SIGNAL_WORKER_SPOOL_DIR} --state-dir \${SIGNAL_WORKER_STATE_DIR} --heartbeat \${SIGNAL_WORKER_HEARTBEAT_FILE}
EOF
        cat "$pinned/worker-inputs.conf"
    } > "$LM_SYSTEMD_UNIT_DIR/$worker_unit.d/$SOAK_OVERRIDE"
    systemctl daemon-reload || fail "cannot pin incumbent $realm restart paths"
}

pin_funded_runtimes() {
    local realm
    for realm in $(lm_funded_realms); do
        pin_realm_runtime "$realm"
    done
}

stage_demo_candidate() {
    CANDIDATE_RELEASE_DIR="$RELEASE_DIR/releases/$EXPECTED_COMMIT"
    install -d -o root -g "$RUNTIME_GROUP" -m 0755 "$CANDIDATE_RELEASE_DIR"
    local binary unit
    for binary in engine engine-tools signal-worker; do
        if [ -f "$CANDIDATE_RELEASE_DIR/$binary" ]; then
            cmp -s "$QUALIFIED_RELEASE_DIR/$binary" "$CANDIDATE_RELEASE_DIR/$binary" \
                || fail "candidate path contains different $binary bytes"
        else
            install -o root -g "$RUNTIME_GROUP" -m 0755 \
                "$QUALIFIED_RELEASE_DIR/$binary" "$CANDIDATE_RELEASE_DIR/$binary" \
                || fail "cannot stage demo $binary"
        fi
    done
    local practice_engine practice_worker
    practice_engine="$(lm_realm_field "$PRACTICE_REALM" engine_unit)"
    practice_worker="$(lm_realm_field "$PRACTICE_REALM" worker_unit)"
    for unit in "$practice_engine" "$practice_worker"; do
        install -d -m 0755 "$LM_SYSTEMD_UNIT_DIR/$unit.d"
    done
    cat > "$LM_SYSTEMD_UNIT_DIR/$practice_engine.d/$SOAK_OVERRIDE" <<EOF
[Service]
ExecStart=
ExecStart=$CANDIDATE_RELEASE_DIR/engine run --config \${ENGINE_CONFIG_FILE}
EOF
    cat > "$LM_SYSTEMD_UNIT_DIR/$practice_worker.d/$SOAK_OVERRIDE" <<EOF
[Service]
ExecStart=
ExecStart=$CANDIDATE_RELEASE_DIR/signal-worker live --signal-config \${SIGNAL_WORKER_CONFIG_FILE} --long-rule \${LONG_NATIVE_RULE_FILE} --carry-config \${CARRY_SIGNAL_CONFIG_FILE} --operational-config \${OPERATIONAL_PROFILE_FILE} --engine-config \${ENGINE_CONFIG_FILE} --spool-dir \${SIGNAL_WORKER_SPOOL_DIR} --state-dir \${SIGNAL_WORKER_STATE_DIR} --heartbeat \${SIGNAL_WORKER_HEARTBEAT_FILE}
EOF
}

prepare_recorder_runtime() {
    local unit=liquidity-migration-equity-recorder.service target candidate override
    target="$(git -C "$REPO_DIR" show "$EXPECTED_COMMIT:deploy/systemd/$unit")" \
        || fail "cannot read the target recorder unit"
    case "$(printf '%s\n' "$target" | sed -n 's/^ExecStart=//p')" in
        "$RELEASE_DIR/bin/engine-tools record-equity "*) ;;
        *) return 0 ;;
    esac
    candidate="$RELEASE_DIR/releases/$EXPECTED_COMMIT/engine-tools"
    install -d -o root -g root -m 0755 "${candidate%/*}"
    if [ -f "$candidate" ]; then
        cmp -s "$QUALIFIED_RELEASE_DIR/engine-tools" "$candidate" \
            || fail "candidate path contains different engine-tools bytes"
    else
        install -o root -g root -m 0755 "$QUALIFIED_RELEASE_DIR/engine-tools" "$candidate.new" \
            || fail "cannot stage the recorder runtime"
        mv -f -- "$candidate.new" "$candidate" || fail "cannot publish the recorder executable"
    fi
    override="$LM_SYSTEMD_UNIT_DIR/$unit.d/20-recorder-runtime.conf"
    install -d -m 0755 "${override%/*}"
    cat > "$override.new" <<EOF
[Service]
ExecStart=
ExecStart=$candidate record-equity --manifest $REPO_DIR/deploy/fleet_manifest.tsv
EOF
    mv -f -- "$override.new" "$override" || fail "cannot publish the recorder runtime"
    systemctl daemon-reload || fail "cannot activate the recorder runtime"
    # Checkout can delete the Python entrypoint. Finish any existing oneshot first.
    if [ -f "$LM_SYSTEMD_UNIT_DIR/$unit" ]; then
        systemctl start "$unit" || fail "recorder handover failed before checkout"
    fi
}

clear_recorder_runtime() {
    local override="$LM_SYSTEMD_UNIT_DIR/liquidity-migration-equity-recorder.service.d/20-recorder-runtime.conf"
    if [ -f "$override" ]; then
        rm -f -- "$override" || fail "cannot remove the recorder runtime override"
        systemctl daemon-reload || fail "cannot activate the installed recorder unit"
    fi
}

clear_realm_soak_overrides() {
    local realm="$1" unit
    for unit in "$(lm_owner_unit "$realm")" "$(lm_signal_worker_unit "$realm")"; do
        rm -f -- "$LM_SYSTEMD_UNIT_DIR/$unit.d/$SOAK_OVERRIDE"
    done
    systemctl daemon-reload || fail "cannot activate the qualified $realm restart paths"
    if [ "$realm" != "$PRACTICE_REALM" ]; then rm -rf -- "$RELEASE_DIR/incumbent-$realm"; fi
}

clear_demo_candidate_override() { clear_realm_soak_overrides "$PRACTICE_REALM"; }

demo_candidate_running() {
    local pid
    pid="$(systemctl show --property=MainPID --value \
        "$(lm_realm_field "$PRACTICE_REALM" engine_unit)")"
    [ "${pid:-0}" != 0 ] && cmp -s "/proc/$pid/exe" "$CANDIDATE_RELEASE_DIR/engine"
}

wait_demo_soak() {
    (
        lm_load_private_systemd_environment "$PYTHON" "$NOTIFICATIONS_ENVIRONMENT" \
            TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_ALERT_CHAT_ID TELEGRAM_CONTROL_USER_IDS
        lm_load_private_systemd_environment "$PYTHON" "$ONCALL_ENVIRONMENT" \
            INCIDENT_ROUTINE_FIRE_URL INCIDENT_ROUTINE_FIRE_TOKEN ONCALL_DEADMAN_URL
        "$PYTHON" "$REPO_DIR/scripts/runtime/check_fleet_liveness.py" \
            --account-scope demo --require-oncall --demo-soak
    ) || fail "demo soak refused; incumbent mainnet restart paths remain pinned"
}

install_release() {
    install -d -o root -g root -m 0755 "${ENGINE_BINARY%/*}"
    install -o root -g "$RUNTIME_GROUP" -m 0755 \
        "$QUALIFIED_RELEASE_DIR/engine" "$ENGINE_BINARY" \
        || fail "cannot install the engine binary"
    install -o root -g "$RUNTIME_GROUP" -m 0755 \
        "$QUALIFIED_RELEASE_DIR/engine-tools" "$ENGINE_TOOLS_BINARY" \
        || fail "cannot install the engine-tools binary"
    install -o root -g "$RUNTIME_GROUP" -m 0755 \
        "$QUALIFIED_RELEASE_DIR/signal-worker" "$SIGNAL_WORKER_BINARY" \
        || fail "cannot install the signal-worker binary"
    cleanup_release
    install -o root -g root -m 0755 \
        "$REPO_DIR/deploy/telegram_control_helper.sh" "$ENGINE_CONTROL_HELPER" \
        || fail "cannot install the Telegram control helper"
    install -o root -g root -m 0440 \
        "$REPO_DIR/deploy/liquidity-controls.sudoers" "$CONTROLS_SUDOERS.new" \
        || fail "cannot stage the controls sudoers fragment"
    /usr/sbin/visudo -cf "$CONTROLS_SUDOERS.new" >/dev/null \
        || fail "staged controls sudoers fragment is invalid"
    mv -f "$CONTROLS_SUDOERS.new" "$CONTROLS_SUDOERS"
    # Retired generation-gate artifacts.
    rm -f /opt/liquidity-migration-engine/bin/run-authorized-runtime \
        /opt/liquidity-migration-engine/bin/engine.release \
        /opt/liquidity-migration-engine/bin/activation.complete
}

install_units() {
    lm_install_current_systemd_units || fail "cannot install the fleet's systemd units"
}

# What one recorder unit runs from: its unit file, the capture config the unit
# names, the symbol file, the market_tape package, and the Python dependencies.
capture_fingerprint() {
    local unit="$1" config
    config="$(sed -n 's/.*--config \([^ \\]*\).*/\1/p' "$REPO_DIR/deploy/systemd/$unit" | head -n 1)"
    {
        cat "$REPO_DIR/deploy/systemd/$unit"
        if [ -n "$config" ]; then cat "$REPO_DIR/$config"; fi
        cat "$REPO_DIR/deploy/forward-capture-symbols.txt"
        find "$REPO_DIR/market_tape" -name '*.py' -print0 | sort -z | xargs -0 cat
        cat "$(python_requirements_path)"
    } 2>/dev/null | sha256sum | cut -c1-64
}

# What one realm's long-running processes run from: the engine workspace
# source, the realm's worker config, the fleet manifest and unit files, and
# the rendered config and environment files on this host. The binary itself is
# not in it: it embeds the commit hash, so it differs on every commit even when
# nothing it does changed. A path an older commit lacks hashes as absent.
realm_fingerprint() {
    local realm="$1" commit="${2:-$EXPECTED_COMMIT}" source_env profile=""
    source_env="$(lm_realm_field "$realm" worker_source_env)"
    if [ -f "$source_env" ]; then
        profile="$(
            unset OPERATIONAL_PROFILE_FILE
            lm_load_private_systemd_environment "$PYTHON" "$source_env" OPERATIONAL_PROFILE_FILE 2>/dev/null || true
            printf '%s' "${OPERATIONAL_PROFILE_FILE:-}"
        )"
    fi
    {
        git -C "$REPO_DIR" rev-parse "$commit:engine" "$commit:deploy/systemd" \
            "$commit:deploy/fleet_manifest.tsv" "$commit:deploy/lib_sleeves.sh" \
            "$commit:deploy/lib_realms.sh" "$commit:deploy/realms.tsv" \
            "$commit:deploy/realm_fields.tsv" \
            "$commit:$(lm_realm_field "$realm" worker_config_repo)" 2>/dev/null || true
        cat "$(lm_realm_field "$realm" engine_config)" \
            "$(lm_realm_field "$realm" engine_env)" \
            "$(lm_realm_field "$realm" worker_env)" 2>/dev/null || true
        # A funded realm also runs from its own Telegram route and the owner's
        # credential file, arming switch included.
        if [ "$(lm_realm_field "$realm" kind)" = funded ]; then
            cat "$(lm_realm_field "$realm" telegram_env)" \
                "$(lm_realm_field "$realm" credential_env)" 2>/dev/null || true
        fi
        if [ -n "$profile" ]; then cat "$profile" 2>/dev/null || true; fi
        cat /etc/liquidity-migration/sleeves.resolved.env 2>/dev/null || true
    } | sha256sum | cut -c1-64
}

# True when the realm runs from exactly what this deploy would install and both
# of its long-running units are active: then there is nothing to hand over, and
# the realm — the funded engine included — is left trading.
realm_unchanged() {
    local realm="$1" worker_unit owner_unit recorded
    [ ! -f "/etc/liquidity-migration/reconcile-clear.$realm.note" ] || return 1
    worker_unit="$(lm_signal_worker_unit "$realm")" || return 1
    owner_unit="$(lm_owner_unit "$realm")" || return 1
    recorded="$(cat "$RELEASE_DIR/$realm.fingerprint" 2>/dev/null || true)"
    [ -n "$recorded" ] && [ "$recorded" = "$(realm_fingerprint "$realm")" ] \
        && systemctl is-active --quiet "$worker_unit" && systemctl is-active --quiet "$owner_unit"
}

record_realm_fingerprint() {
    realm_fingerprint "$1" > "$RELEASE_DIR/$1.fingerprint"
}

# The first gated deploy finds no record. A realm that is up was started by the
# last finished deploy, so what it runs from is that commit's inputs against
# the host files as they stand now — recorded here, before this deploy renders
# anything, so the comparison below is against what actually runs.
seed_realm_fingerprints() {
    local realm deployed worker_unit owner_unit
    deployed="$(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || true)"
    [ -n "$deployed" ] || return 0
    for realm in $(lm_realms); do
        [ -f "$RELEASE_DIR/$realm.fingerprint" ] && continue
        worker_unit="$(lm_signal_worker_unit "$realm" 2>/dev/null)" || continue
        owner_unit="$(lm_owner_unit "$realm" 2>/dev/null)" || continue
        systemctl is-active --quiet "$worker_unit" && systemctl is-active --quiet "$owner_unit" || continue
        realm_fingerprint "$realm" "$deployed" > "$RELEASE_DIR/$realm.fingerprint"
        echo "$realm-fingerprint seeded from $deployed"
    done
}

# Independent units run through the deploy. Timers are (re)started so a
# changed schedule applies; a recorder is restarted only when its own inputs
# changed, and then this waits for a status file the new process wrote.
start_independent_units() {
    local unit fingerprint recorded since fingerprint_file
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        case "$unit" in
            liquidity-migration-forward-capture*.service)
                CAPTURE_STATUS="$(lm_output_artifact_for_unit "$unit")" \
                    || fail "the fleet manifest names no status file for $unit"
                fingerprint_file="$RELEASE_DIR/${unit%.service}.fingerprint"
                fingerprint="$(capture_fingerprint "$unit")"
                recorded="$(cat "$fingerprint_file" 2>/dev/null || true)"
                if [ "$recorded" = "$fingerprint" ] && systemctl is-active --quiet "$unit"; then
                    systemctl enable "$unit" 2>/dev/null || fail "cannot enable $unit"
                    echo "capture-ok unit=$unit result=unchanged-left-running"
                    continue
                fi
                since="$(date +%s)"
                systemctl enable "$unit" 2>/dev/null || fail "cannot enable $unit"
                if systemctl restart "$unit" \
                    && (wait_capture_ready "$unit" "$CAPTURE_STATUS" "$since"); then
                    printf '%s\n' "$fingerprint" > "$fingerprint_file"
                    echo "capture-ok unit=$unit result=restarted"
                else
                    # Not fatal: a recorder is independent of the fleet in
                    # both directions, and the host watchdog pages on it.
                    echo "warning: $unit did not receive a market frame from its new process; the fleet deploy continues" >&2
                fi
                ;;
            *.timer)
                systemctl enable "$unit" 2>/dev/null || fail "cannot enable $unit"
                systemctl restart "$unit" || fail "cannot start $unit"
                ;;
            *) ;;
        esac
    done < <(lm_independent_units)
}

# ------------------------------------------------------------ realm inputs

prepare_oncall_inputs() {
    "$PYTHON" -m liquidity_migration.policy.oncall_environment \
        --notifications "$NOTIFICATIONS_ENVIRONMENT" \
        --oncall "$ONCALL_ENVIRONMENT" \
        --legacy-telegram "$(lm_realm_field "$PRACTICE_REALM" credential_env)" \
        --legacy-liveness "$LEGACY_LIVENESS_ENVIRONMENT" \
        --execute \
        || fail "notification and on-call routing is incomplete"
    chown root:root "$NOTIFICATIONS_ENVIRONMENT" "$ONCALL_ENVIRONMENT" \
        && chmod 0600 "$NOTIFICATIONS_ENVIRONMENT" "$ONCALL_ENVIRONMENT" \
        || fail "cannot secure notification and on-call routing"
}

# Project the allowlisted worker inputs from the private source env into the
# root-owned env systemd hands the credential-free worker.
write_signal_worker_environment() {
    local source="$1" target="$2"
    "$PYTHON" - "$source" "$target" "$(lm_realms | tr '\n' ' ')" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment
source = Path(sys.argv[1])
target = Path(sys.argv[2])
allowed = {"OPERATIONAL_PROFILE_FILE", "SIGNAL_WORKER_REALM"}
values = load_private_systemd_environment(source)
filtered = {key: value for key, value in values.items() if key in allowed}
realms = set(sys.argv[3].split())
if filtered.get("SIGNAL_WORKER_REALM") not in realms:
    raise SystemExit(
        f"{source}: SIGNAL_WORKER_REALM must be one of {' '.join(sorted(realms))}"
    )
value = str(filtered.get("OPERATIONAL_PROFILE_FILE") or "")
if not value or not Path(value).is_absolute():
    raise SystemExit(f"{source}: OPERATIONAL_PROFILE_FILE must be an absolute path")
target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in sorted(filtered.items()):
            handle.write(f"{key}={shlex.quote(str(value))}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
    chown root:root "$target" && chmod 0600 "$target" \
        || fail "cannot secure signal-worker environment $target"
    unset OPERATIONAL_PROFILE_FILE
    lm_load_private_systemd_environment "$PYTHON" "$source" OPERATIONAL_PROFILE_FILE
    local directory
    [ -f "$OPERATIONAL_PROFILE_FILE" ] \
        || fail "signal-worker input is missing: $OPERATIONAL_PROFILE_FILE"
    chown root:"$RUNTIME_GROUP" "$OPERATIONAL_PROFILE_FILE" \
        && chmod 0640 "$OPERATIONAL_PROFILE_FILE"
    directory="$(dirname "$OPERATIONAL_PROFILE_FILE")"
    chown root:"$RUNTIME_GROUP" "$directory" && chmod 0750 "$directory"
    chgrp "$RUNTIME_GROUP" /etc/liquidity-migration \
        && chmod 0750 /etc/liquidity-migration
}

# One operational profile for both realms, rendered from the dials in the
# funded credential file when that file exists and from the committed defaults
# otherwise. The same bytes land in each realm's signal-worker source directory.
render_operational_profile() {
    local output="$1"
    local dial_env="" dial_file
    dial_file="$(lm_realm_field "$DIALS_REALM" credential_env)"
    [ -f "$dial_file" ] && dial_env="$dial_file"
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$(dirname "$output")"
    "$PYTHON" -m liquidity_migration.policy.real_money_arming render-profile \
        --from-env "$dial_env" --execute --overwrite --output "$output" \
        || fail "operational dials do not render a loadable profile"
}

# What one sleeve's entries resolve to: the table's own word, or the sleeve
# toggle it defers to.
resolve_entries() {
    case "$1" in
        toggles)
            case "${2:-off}" in
                on|ON|1|true|TRUE|yes|YES) printf 'true\n' ;;
                *) printf 'false\n' ;;
            esac
            ;;
        true|false) printf '%s\n' "$1" ;;
        *) fail "invalid entry permission in the realm table: $1" ;;
    esac
}

render_engine_config() {
    local realm="$1" operational_config="$2" output="$3"
    local template signal_config long_entries carry_entries exodus_entries
    local -a maker_args=()
    template="$REPO_DIR/$(lm_realm_field "$realm" engine_toml_template)" \
        || fail "unsupported engine realm: $realm"
    signal_config="$REPO_DIR/$(lm_realm_field "$realm" worker_config_repo)"
    [ -f "$template" ] || fail "unsupported engine realm: $realm"
    long_entries="$(resolve_entries "$(lm_realm_field "$realm" long_entries)" "${LONG_SLEEVE:-off}")"
    carry_entries="$(resolve_entries "$(lm_realm_field "$realm" carry_entries)" "${CARRY_SLEEVE:-off}")"
    exodus_entries="$(resolve_entries "$(lm_realm_field "$realm" exodus_entries)" off)"
    # The maker canary renders only into a template that declares its block.
    if grep -q 'BEGIN GENERATED MAKER CANARY RULE' "$template"; then
        maker_args=(--maker-rule "$REPO_DIR/configs/lane2_toxic_flow_quoter_v1.json")
    fi
    local staged
    staged="$(mktemp "${output}.new.XXXXXX")" || fail "cannot stage $realm engine config"
    if ! "$ENGINE_BINARY" render-native-config \
        --realm "$realm" \
        --signal-config "$signal_config" \
        --long-rule "$REPO_DIR/configs/long_native_v12.json" \
        --carry-rule "$REPO_DIR/configs/lane2_carry_hold_v7.json" \
        --exodus-rule "$REPO_DIR/configs/lane2_exodus_short_v1.json" \
        --operational-config "$operational_config" \
        --long-entries-enabled "$long_entries" \
        --carry-entries-enabled "$carry_entries" \
        --exodus-entries-enabled "$exodus_entries" \
        --template "$template" \
        "${maker_args[@]}" \
        --output "$staged"; then
        rm -f -- "$staged"
        fail "cannot render $realm engine config"
    fi
    chown root:"$RUNTIME_GROUP" "$staged" && chmod 0640 "$staged"
    mv -f -- "$staged" "$output" || fail "cannot install $realm engine config"
}

prepare_demo_inputs() {
    local realm="$PRACTICE_REALM" source_env engine_env credential_env
    source_env="$(lm_realm_field "$realm" worker_source_env)"
    engine_env="$(lm_realm_field "$realm" engine_env)"
    credential_env="$(lm_realm_field "$realm" credential_env)"
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 /etc/liquidity-migration
    [ -f "$source_env" ] || install -o root -g root -m 0600 \
        "$REPO_DIR/$(lm_realm_field "$realm" worker_env_template)" "$source_env"
    [ -f "$credential_env" ] \
        || fail "missing $realm credential file: $credential_env"
    [ -f "$engine_env" ] || fail "missing engine environment: $engine_env"
    lm_load_sleeve_toggles
    lm_write_resolved_sleeve_toggles
    chown root:root /etc/liquidity-migration/sleeves.resolved.env
    chmod 0600 /etc/liquidity-migration/sleeves.resolved.env
    unset SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    lm_load_private_systemd_environment "$PYTHON" "$source_env" \
        SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    [ "$SIGNAL_WORKER_REALM" = "$realm" ] \
        || fail "$realm signal-worker source must declare SIGNAL_WORKER_REALM=$realm"
    render_operational_profile "$OPERATIONAL_PROFILE_FILE"
    write_signal_worker_environment "$source_env" "$(lm_realm_field "$realm" worker_env)"
    render_engine_config "$realm" "$OPERATIONAL_PROFILE_FILE" \
        "$(lm_realm_field "$realm" engine_config)"
}

# --------------------------------------------------------- state takeover

run_engine_takeover_command() {
    local realm="$1" config="$2" runtime_user engine_env credential_env credential_vars
    shift 2
    runtime_user="$(lm_realm_field "$realm" engine_user)" \
        || fail "unsupported takeover realm: $realm"
    engine_env="$(lm_realm_field "$realm" engine_env)"
    credential_env="$(lm_realm_field "$realm" credential_env)"
    # REAL_MONEY is among a funded realm's own variables: it comes from the
    # owner's credential file and is read, never written, here. The engine
    # refuses a funded takeover without it, and an unarmed file still refuses.
    credential_vars="$(lm_realm_field "$realm" takeover_vars)"
    (
        unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
            BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET \
            BYBIT_REAL_API_KEY_IP BYBIT_REAL_API_KEY_BACKUP_IP \
            BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID REAL_MONEY \
            BYBIT_INVENTORY_CREDENTIAL_SET \
            MEXC_REAL_API_KEY MEXC_REAL_API_SECRET \
            HYPERLIQUID_REAL_ACCOUNT_ADDRESS HYPERLIQUID_REAL_API_WALLET_KEY \
            EXPECTED_ENGINE_ACCOUNT_USER_ID EXPECTED_ENGINE_VENUE EXPECTED_ENGINE_REALM
        # shellcheck disable=SC2086 # one variable name per word, by construction
        lm_load_private_systemd_environment "$PYTHON" "$credential_env" $credential_vars
        lm_load_private_systemd_environment "$PYTHON" "$engine_env" \
            EXPECTED_ENGINE_ACCOUNT_USER_ID EXPECTED_ENGINE_VENUE EXPECTED_ENGINE_REALM
        [ -n "${EXPECTED_ENGINE_ACCOUNT_USER_ID:-}" ] \
            || fail "$engine_env does not bind the exact account id"
        exec /usr/bin/setpriv \
            --reuid "$runtime_user" --regid "$RUNTIME_GROUP" --clear-groups \
            "$ENGINE_BINARY" "$@" --config "$config"
    )
}

retire_legacy_signal_sources() {
    local realm="$1" config plan
    config="$(lm_realm_field "$realm" engine_config)" \
        || fail "unsupported legacy retirement realm: $realm"
    plan="/etc/liquidity-migration/legacy-signal-retirements.$realm.json"
    [ -f "$plan" ] || return 0
    run_engine_takeover_command "$realm" "$config" retire-legacy-signal-sources \
        --plan "$plan" --execute
}

clear_reconciliation_if_requested() {
    local realm="$1" config pending note
    config="$(lm_realm_field "$realm" engine_config)" \
        || fail "unsupported reconciliation realm: $realm"
    pending="/etc/liquidity-migration/reconcile-clear.$realm.note"
    [ -f "$pending" ] || return 0
    note="$(cat -- "$pending")" || fail "cannot read $realm reconciliation note"
    [ -n "$note" ] || fail "$realm reconciliation note is empty"
    run_engine_takeover_command "$realm" "$config" reconcile-clear \
        --note "$note" --execute || return $?
    mv -- "$pending" "$pending.applied" \
        || fail "cannot retire the applied $realm reconciliation note"
}

ensure_native_strategy_state() {
    local realm="$1" config wal carry_root exodus_root
    local long_state carry_checkpoint carry_book exodus_identity exodus_state
    local required_present=0 source
    config="$(lm_realm_field "$realm" engine_config)" \
        || fail "unsupported native strategy-state realm: $realm"
    wal="$(lm_realm_field "$realm" engine_wal)"
    carry_root="$(lm_realm_field "$realm" carry_root)"
    exodus_root="$(lm_realm_field "$realm" exodus_root)"
    long_state="/var/lib/liquidity-migration/targets/long-${realm}-state.json"
    carry_checkpoint="$carry_root/.cache/carry_sizing_anchors.json"
    carry_book="/var/lib/liquidity-migration/targets/carry-${realm}.json"
    exodus_identity="$exodus_root/exodus_state_identity.json"
    exodus_state="$exodus_root/exodus_state.json"

    if run_engine_takeover_command "$realm" "$config" verify-native-strategy-state; then
        echo "native-state-ok realm=$realm result=already-complete"
        return 0
    fi

    # A realm with no state yet is initialized, whatever configs were retained
    # for it: deploy renders and retains a funded realm's config on every armed
    # run, including the runs where the realm stayed stopped, so a retained
    # previous config does not mean the realm has ever run.
    for source in \
        "$long_state" "$carry_checkpoint" "$carry_book" "$exodus_identity" "$exodus_state"; do
        [ -e "$source" ] && required_present=$((required_present + 1))
    done
    if [ "$required_present" -eq 0 ] && [ ! -s "$wal" ]; then
        run_engine_takeover_command "$realm" "$config" initialize-native-strategy-state \
            || fail "cannot initialize empty native strategy state for $realm"
        run_engine_takeover_command "$realm" "$config" verify-native-strategy-state \
            || fail "initialized $realm native strategy state failed verification"
        echo "native-state-ok realm=$realm result=initialized-empty"
        return 0
    fi

    # The state was written under the config the realm last ran with. That is
    # the last deployed commit's render only for a realm that ran through every
    # deploy; a realm that sat stopped kept being re-rendered, so every
    # retained render is a candidate, newest first, and the engine's dry run
    # picks the one whose identities match the checkpoints it replays.
    local candidate
    for candidate in $(retained_realm_configs "$realm"); do
        run_engine_takeover_command "$realm" "$config" rebind-native-strategy-state \
            --previous-config "$candidate" >/dev/null 2>&1 || continue
        run_engine_takeover_command "$realm" "$config" rebind-native-strategy-state \
            --previous-config "$candidate" --execute \
            && run_engine_takeover_command "$realm" "$config" verify-native-strategy-state \
            && { echo "native-state-ok realm=$realm result=rebound previous=$candidate"; return 0; }
        fail "$realm native checkpoint configuration change is incompatible"
    done
    fail "$realm canonical native strategy state is unavailable and no retained render matches its checkpoints; recover retained legacy snapshots with the compatible retained release before deployment"
}

# Every retained render of one realm's engine config, the last deployed
# commit's first, then newest first.
retained_realm_configs() {
    local realm="$1" deployed deployed_config="" candidate
    deployed="$(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || true)"
    if [ -n "$deployed" ]; then
        deployed_config="$RELEASE_DIR/checkpoint-configs/$deployed/engine.$realm.toml"
        if [ -f "$deployed_config" ]; then echo "$deployed_config"; fi
    fi
    for candidate in "$RELEASE_DIR"/checkpoint-configs/*/"engine.$realm.toml"; do
        [ -f "$candidate" ] || continue
        printf '%s\t%s\n' \
            "$(stat -c %Y "$candidate" 2>/dev/null || stat -f %m "$candidate")" "$candidate"
    done | sort -rn | cut -f2- | while IFS= read -r candidate; do
        if [ "$candidate" != "$deployed_config" ]; then echo "$candidate"; fi
    done
}

# ------------------------------------------------------------ funded realms

# Project the allowlisted Telegram values out of one credential file into a
# notification-only file for that realm's observer.
project_realm_telegram() {
    local source="$1" target="$2"
    "$PYTHON" - "$source" "$target" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path
from liquidity_migration.policy.systemd_environment import load_private_systemd_environment
source = Path(sys.argv[1])
target = Path(sys.argv[2])
values = load_private_systemd_environment(source)
allowed = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERT_CHAT_ID")
filtered = {key: values[key] for key in allowed if str(values.get(key) or "")}
if not filtered.get("TELEGRAM_BOT_TOKEN") or not (
    filtered.get("TELEGRAM_CHAT_ID") or filtered.get("TELEGRAM_ALERT_CHAT_ID")
):
    raise SystemExit("funded watchdog requires a Telegram token and chat id")
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in sorted(filtered.items()):
            handle.write(f"{key}={shlex.quote(str(value))}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
PY
    chown root:root "$target" && chmod 0600 "$target" \
        || fail "cannot secure funded notification environment $target"
}

# One funded realm's inputs, staged while the live engines keep trading. The
# operational dials stay in the funded Bybit credential file; every realm
# installs the same rendered bytes.
provision_funded_realm() {
    local realm="$1" source_env
    source_env="$(lm_realm_field "$realm" worker_source_env)"
    [ -f "$source_env" ] || install -o root -g root -m 0600 \
        "$REPO_DIR/$(lm_realm_field "$realm" worker_env_template)" "$source_env"
    "$PYTHON" -m liquidity_migration.policy.real_money_arming default-telegram \
        --credential-env "$(lm_realm_field "$realm" credential_env)" \
        --from-env "$(lm_realm_field "$PRACTICE_REALM" credential_env)" --execute \
        || fail "cannot default the $realm Telegram pair"
    unset SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    lm_load_private_systemd_environment "$PYTHON" "$source_env" \
        SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    [ "$SIGNAL_WORKER_REALM" = "$realm" ] \
        || fail "$realm signal-worker source must declare SIGNAL_WORKER_REALM=$realm"
    render_operational_profile "$OPERATIONAL_PROFILE_FILE"
    write_signal_worker_environment "$source_env" "$(lm_realm_field "$realm" worker_env)"
    project_realm_telegram "$(lm_realm_field "$realm" credential_env)" \
        "$(lm_realm_field "$realm" telegram_env)"
    "$PYTHON" -m liquidity_migration.policy.real_money_arming \
        "$(lm_realm_field "$realm" preflight_command)" \
        || fail "$realm preflight has outstanding steps"
    render_engine_config "$realm" "$OPERATIONAL_PROFILE_FILE" \
        "$(lm_realm_field "$realm" engine_config)"
}

# ------------------------------------------------------------------- start

start_realm() {
    local realm="$1" worker_unit owner_unit worker_heartbeat owner_heartbeat unit since
    worker_unit="$(lm_signal_worker_unit "$realm")" \
        || fail "$realm manifest does not name one signal worker"
    owner_unit="$(lm_owner_unit "$realm")" \
        || fail "$realm manifest does not name one account owner"
    worker_heartbeat="$(lm_output_artifact_for_unit "$worker_unit")" \
        || fail "$realm signal worker has no heartbeat artifact"
    owner_heartbeat="$(lm_output_artifact_for_unit "$owner_unit")" \
        || fail "$realm engine has no heartbeat artifact"
    since="$(date +%s)"
    start_unit "$worker_unit"
    start_unit "$owner_unit"
    wait_fresh_heartbeat "$worker_unit" "$worker_heartbeat" "$since"
    wait_fresh_heartbeat "$owner_unit" "$owner_heartbeat" "$since"
    local immediate_jobs
    immediate_jobs=" $(lm_immediate_timer_jobs "$realm" | tr '\n' ' ') "
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        [ "$unit" = "$worker_unit" ] && continue
        case "$immediate_jobs" in
            *" $unit "*) continue ;;
            *) start_unit "$unit" ;;
        esac
    done < <(lm_activation_units "$realm" start)
    # A job-now unit is the realm's liveness watchdog: it runs to completion
    # here, and it alerts on any manifest unit that is not active. Its stop
    # order puts it ahead of the realm's timers in the start list, so run it
    # only once every other unit above is up — otherwise its first pass pages
    # CRITICAL on the timers this same function has not enabled yet.
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        systemctl start "$unit" || fail "cannot start $unit"
    done < <(lm_immediate_timer_jobs "$realm")
}

# `stop_realm_units` takes the realm's own timers down, and `start_realm`
# reaches them only after the realm's owner and worker publish healthy
# heartbeats. A handover that aborts at that gate leaves the realm's liveness
# watchdog stopped while the realm's engine keeps running, so nothing watches
# the realm the deploy just failed on. Non-fatal: a restore failure must not
# replace the handover's own error.
restore_realm_timers() {
    local realm="$1" unit
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        case "$unit" in
            *.timer)
                if systemctl enable --now "$unit"; then
                    echo "watch-restored realm=$realm unit=$unit"
                else
                    echo "warning: cannot restore $unit after the failed $realm handover" >&2
                fi
                ;;
        esac
    done < <(lm_realm_units "$realm")
}

handover_realm() {
    local realm="$1"
    if ! (
        stop_realm_units "$realm" \
            && { [ "$realm" = "$PRACTICE_REALM" ] || clear_realm_soak_overrides "$realm"; } \
            && retire_legacy_signal_sources "$realm" \
            && ensure_native_strategy_state "$realm" \
            && clear_reconciliation_if_requested "$realm" \
            && start_realm "$realm"
    ); then
        restore_realm_timers "$realm"
        rollback_after_failure "$realm"
        return 1
    fi
    record_realm_fingerprint "$realm"
}

# ------------------------------------------------------------------ verify

verify_mode() {
    echo "commit $(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "deployed $(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || echo none)"
    echo "rollback-target $(rollback_target 2>/dev/null || echo none)"
    local realm label
    for realm in $(lm_funded_realms); do
        # The dials realm keeps its own name in this line: it is the one an
        # operator reads as the fleet's real-money switch.
        label="$realm"
        if [ "$realm" = "$DIALS_REALM" ]; then label="real-money"; fi
        if realm_armed "$realm"; then echo "$label armed"; else echo "$label off"; fi
    done
    if [ -x "$ENGINE_BINARY" ]; then
        for realm in $(lm_funded_realms); do
            realm_run_ready "$realm" || true
            echo "$realm readiness=$FUNDED_REALM_READINESS"
        done
    fi
    local unit state heartbeat age now
    now="$(date +%s)"
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        state="$(systemctl is-active "$unit" 2>/dev/null || true)"
        heartbeat="$(lm_output_artifact_for_unit "$unit" 2>/dev/null || true)"
        if [ -n "$heartbeat" ] && [ "$heartbeat" != "-" ] && [ -f "$heartbeat" ]; then
            age=$(( now - $(stat -c %Y "$heartbeat") ))
            printf '%-55s %-10s heartbeat %ss\n' "$unit" "$state" "$age"
        else
            printf '%-55s %-10s\n' "$unit" "$state"
        fi
    done < <(lm_expected_systemd_units)
    df -h /var/lib | tail -1
    report_disk_usage
    report_failed_units
    report_engine_status
}

# Each realm's engine heartbeat read as health, exposure and blockers: the
# "why is it not trading" answer, from the engine's own statement. Read-only,
# and never fatal to a verify: a realm with no heartbeat yet is skipped.
report_engine_status() {
    local realm unit heartbeat
    for realm in $(lm_realms); do
        unit="$(lm_owner_unit "$realm" 2>/dev/null || true)"
        [ -n "$unit" ] || continue
        heartbeat="$(lm_output_artifact_for_unit "$unit" 2>/dev/null || true)"
        [ -n "$heartbeat" ] && [ "$heartbeat" != "-" ] && [ -f "$heartbeat" ] || continue
        echo "engine-status $realm"
        python3 "$REPO_DIR/scripts/runtime/engine_status.py" "$heartbeat" || true
    done
}

# Why each failed fleet unit failed, as `failed-unit <id>` then its result
# properties and the tail of its journal. Only `liquidity-migration-*` units
# are read, so an unrelated system unit's journal never reaches a diagnose run.
report_failed_units() {
    local unit count=0
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        count=$((count + 1))
        if [ "$count" -gt "$FAILED_UNIT_REPORT_MAX" ]; then
            echo "failed-unit-report truncated at $FAILED_UNIT_REPORT_MAX units"
            break
        fi
        echo "failed-unit $unit"
        systemctl show "$unit" \
            --property=Id,Result,ExecMainStatus,ExecMainCode,NRestarts,InactiveEnterTimestamp \
            --no-pager 2>/dev/null || true
        journalctl -u "$unit" -n "$FAILED_UNIT_JOURNAL_LINES" --no-pager -o short-iso 2>/dev/null || true
    done < <(systemctl list-units 'liquidity-migration-*' --state=failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}')
}

# Allocated bytes rounded to KiB; only directory totals reach diagnostics.
# The roots nest, so a path walked as a root and again as a parent's child
# reports twice; the first line of each path wins and the rest are dropped,
# because a duplicate spends a line of the cap without naming a new consumer.
report_disk_usage() {
    local root
    for root in $DISK_REPORT_ROOTS; do
        [ -d "$root" ] || continue
        du -kx -d 1 "$root" 2>/dev/null || true
    done | sort -rn | awk '!seen[$2]++' | head -n "$DISK_REPORT_LINES" \
        | awk '{printf "disk %.0f %s\n", $1 * 1024, $2}'
}

# ------------------------------------------------------------- funded stops

# One funded realm's units, disabled as well as stopped: `ops.sh start` never
# re-enables them, so an operator stop survives a reboot.
stop_funded_units() {
    local realm="$1" unit
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        systemctl disable --now "$unit" 2>/dev/null || true
    done < <(lm_realm_units "$realm")
}

disarm_funded_mode() {
    local realm="$1" credential
    credential="$(funded_credential_env "$realm")"
    stop_funded_units "$realm"
    if [ ! -f "$credential" ]; then
        echo "disarm-$realm-ok real_money=absent units=stopped"
        return 0
    fi
    /usr/bin/python3 -I - "$credential" <<'PY'
import os
import re
import shlex
import sys
import tempfile

path = sys.argv[1]
KEY = re.compile(r"[A-Z][A-Z0-9_]*")
with open(path, "rb") as handle:
    data = handle.read()
if b"\0" in data:
    raise SystemExit("disarm refused: credential contains invalid bytes")
values: dict[str, str] = {}
for raw_line in data.decode("utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith(("#", ";")):
        continue
    key, separator, raw_value = line.partition("=")
    if separator != "=" or not KEY.fullmatch(key) or key in values:
        raise SystemExit("disarm refused: credential assignment is invalid or repeated")
    parsed = shlex.split(raw_value, comments=False, posix=True)
    if len(parsed) > 1:
        raise SystemExit("disarm refused: credential value is ambiguous")
    values[key] = "" if not parsed else parsed[0]
values["REAL_MONEY"] = "false"
fd, temporary = tempfile.mkstemp(
    prefix=f".{os.path.basename(path)}.", dir=os.path.dirname(path)
)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        if os.geteuid() == 0:
            os.fchown(handle.fileno(), 0, 0)
        for key, value in sorted(values.items()):
            handle.write(f"{key}={shlex.quote(value)}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
    echo "disarm-$realm-ok real_money=false units=stopped"
    echo "note: disarm does not flatten existing exposure; reconcile/flatten separately"
}

# ------------------------------------------------------------------ deploy

retain_native_checkpoint_configs() {
    local deployed realm source destination staged
    deployed="$(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || true)"
    [ -n "$deployed" ] || return 0
    for realm in $(lm_realms); do
        source="$(lm_realm_field "$realm" engine_config)"
        [ -f "$source" ] || continue
        destination="$RELEASE_DIR/checkpoint-configs/$deployed/engine.$realm.toml"
        [ ! -f "$destination" ] || continue
        install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$(dirname "$destination")"
        staged="$(mktemp "${destination}.new.XXXXXX")" || fail "cannot stage $realm checkpoint configuration"
        install -o root -g "$RUNTIME_GROUP" -m 0640 "$source" "$staged" \
            && mv -- "$staged" "$destination" \
            || fail "cannot retain $realm checkpoint source configuration"
    done
}

deploy_mode() {
    seed_generation_record
    retain_native_checkpoint_configs
    build_engine
    pin_funded_runtimes
    fetch_exact_commit
    # Re-read the manifest helpers from the exact commit this run installs. A
    # commit from before the independent lifecycle has no helper for it, and a
    # rollback to one must still run.
    . "$REPO_DIR/deploy/lib_sleeves.sh"
    . "$REPO_DIR/deploy/lib_systemd_environment.sh"
    type lm_independent_units >/dev/null 2>&1 || lm_independent_units() { :; }
    # Realm facts now come from the checkout this run installs; the shipped
    # text only had to cover the steps that ran before it existed.
    if [ -f "$REPO_DIR/deploy/realm_fields.tsv" ]; then
        unset LM_REALM_FIELDS_TEXT
        LM_REALM_FIELDS="$REPO_DIR/deploy/realm_fields.tsv"
    fi
    ensure_runtime_identities
    install_python_environment
    seed_realm_fingerprints
    stage_demo_candidate
    ENGINE_BINARY="$CANDIDATE_RELEASE_DIR/engine"
    prepare_oncall_inputs
    install_units
    start_independent_units
    prepare_demo_inputs
    if realm_unchanged "$PRACTICE_REALM" && demo_candidate_running; then
        echo "$PRACTICE_REALM-ok result=unchanged-left-running"
    else
        handover_realm "$PRACTICE_REALM"
    fi
    wait_demo_soak
    ENGINE_BINARY="$RELEASE_DIR/bin/engine"
    install_release
    clear_recorder_runtime
    clear_demo_candidate_override
    local realm
    for realm in $(lm_funded_realms); do
        if ! realm_armed "$realm"; then
            echo "real-money off: $realm units stay stopped"
            continue
        fi
        # Rendered and projected whenever armed, so the canary has the config it
        # runs against; started only once the engine itself would boot.
        echo "staging $realm configuration while live engines continue trading"
        provision_funded_realm "$realm"
        if ! realm_run_ready "$realm"; then
            echo "$realm armed but the installed engine reports $(lm_realm_field "$realm" engine_venue) readiness=$FUNDED_REALM_READINESS: units stay stopped, the engine refuses to run at that readiness"
        elif [ "$(lm_realm_field "$realm" posture)" = stopped ]; then
            stop_funded_units "$realm"
            echo "$realm posture=stopped in deploy/realms.tsv: units stay stopped"
        elif realm_unchanged "$realm"; then
            clear_realm_soak_overrides "$realm"
            echo "$realm-ok result=unchanged-left-running"
        else
            echo "atomic $realm handover: swapping binaries and state"
            handover_realm "$realm"
        fi
    done
    record_generation
    echo "deploy-ok commit=$EXPECTED_COMMIT"
    verify_mode
}

rollback_mode() {
    local target
    target="$(rollback_target)" \
        || fail "no earlier finished deploy is recorded; deploy an exact commit instead"
    rollback_runtime_compatible "$target" \
        || fail "rollback compatibility is unverified; the installed runtime remains available for a forward repair"
    echo "rollback to $target"
    EXPECTED_COMMIT="$target"
    deploy_mode
}

# The manifest helpers come from the checkout this run installs or verifies.
. "$REPO_DIR/deploy/lib_sleeves.sh"
. "$REPO_DIR/deploy/lib_systemd_environment.sh"

case "$MODE" in
    deploy) deploy_mode ;;
    rollback) rollback_mode ;;
    verify) verify_mode ;;
    stop-*)
        [ "$(lm_realm_field "${MODE#stop-}" kind 2>/dev/null)" = funded ] \
            || fail "unknown deploy mode: $MODE"
        stop_funded_units "${MODE#stop-}"
        echo "${MODE}-ok"
        echo "note: this stopped publication only; exposure is unchanged. Flatten through the account owner."
        ;;
    disarm-*)
        [ "$(lm_realm_field "${MODE#disarm-}" kind 2>/dev/null)" = funded ] \
            || fail "unknown deploy mode: $MODE"
        disarm_funded_mode "${MODE#disarm-}"
        ;;
    *) fail "unknown deploy mode: $MODE" ;;
esac
