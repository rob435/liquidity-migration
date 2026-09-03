#!/usr/bin/env bash
# One-command VPS deploy, rollback, read-only verify, and the funded safety stops.
#
# deploy: fetch the exact commit, build, install, restart the fleet. A realm
#   that does not publish a fresh heartbeat on the new commit is rolled back
#   to the last commit that did.
# rollback: deploy the last commit whose deploy finished (or, when the current
#   one finished, the one before it).
# verify: read-only fleet summary.
# stop-mainnet: stop the funded units; exposure is unchanged.
# disarm-mainnet: stop the funded units and set REAL_MONEY=false.
#
# Units the manifest marks independent (the market recorder, its upload, the
# state backup, the host watchdog) are never stopped by any mode here; deploy
# restarts the recorder only when its own inputs changed.
set -euo pipefail

deploy_usage() {
    cat >&2 <<'USAGE'
usage: deploy_vps_live.sh {deploy|rollback|verify|stop-mainnet|disarm-mainnet}
  EXPECTED_COMMIT=<40-hex>   exact commit to deploy (default: origin/main tip)
USAGE
    exit 2
}

MODE="${1:-verify}"
[ "$#" -le 1 ] || deploy_usage
case "$MODE" in
    deploy|rollback|verify|stop-mainnet|disarm-mainnet) ;;
    *) deploy_usage ;;
esac

SSH_TARGET="${SSH_TARGET:-root@208.84.103.4}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3}"
REPO_URL="${REPO_URL:-https://github.com/rob435/liquidity-migration.git}"
REPO_DIR="${REPO_DIR:-/opt/liquidity-migration}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

if [ -n "$EXPECTED_COMMIT" ] && [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "EXPECTED_COMMIT must be a full lowercase 40-character commit" >&2
    exit 2
fi

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LOCAL_REPOSITORY="$(cd -P -- "$SCRIPT_DIRECTORY/.." && pwd)"
if [ -z "$EXPECTED_COMMIT" ]; then
    EXPECTED_COMMIT="$(
        git -C "$LOCAL_REPOSITORY" rev-parse --verify --quiet \
            "refs/remotes/$REMOTE/$BRANCH^{commit}" 2>/dev/null \
        || git -C "$LOCAL_REPOSITORY" rev-parse --verify 'HEAD^{commit}'
    )" || { echo "cannot resolve a default EXPECTED_COMMIT" >&2; exit 2; }
    echo "EXPECTED_COMMIT defaulted to $EXPECTED_COMMIT" >&2
fi

if { [ "$MODE" = deploy ] || [ "$MODE" = rollback ]; } && [ -z "$GITHUB_TOKEN" ] \
    && [[ "$REPO_URL" == https://github.com/* ]] && command -v gh >/dev/null 2>&1; then
    GITHUB_TOKEN="$(gh auth token --hostname github.com 2>/dev/null || true)"
fi

stage_ci_binaries_if_available() {
    local commit="$1" target="$2"
    local stage_target="/opt/liquidity-migration-engine/staged/${commit}.tar.gz"
    if ssh "${SSH_ARGS[@]}" "$target" "test -f '$stage_target'" 2>/dev/null; then
        echo "deploy: pre-built release binaries already staged on host ($stage_target)" >&2
        return 0
    fi
    command -v gh >/dev/null 2>&1 || return 0
    local artifact_name="engine-binaries-${commit}"
    local run_id=""
    run_id="$(gh api "/repos/rob435/liquidity-migration/actions/artifacts?name=${artifact_name}" --jq '.artifacts[0].workflow_run.id' 2>/dev/null || true)"
    if [ -z "$run_id" ] || [ "$run_id" = "null" ]; then
        run_id="$(gh run list --commit "$commit" --json databaseId,status,conclusion --jq '.[] | select(.status=="completed" and .conclusion=="success") | .databaseId' 2>/dev/null | head -n 1 || true)"
    fi
    if [ -z "$run_id" ] || [ "$run_id" = "null" ]; then
        echo "deploy: no CI pre-built binary artifact found for $commit; host will build via cargo" >&2
        return 0
    fi
    echo "deploy: downloading CI release binaries from GitHub Actions (run $run_id)..." >&2
    local tmp_dir
    tmp_dir="$(mktemp -d)" || return 0
    if gh run download "$run_id" -n "$artifact_name" -D "$tmp_dir" >/dev/null 2>&1; then
        local tarball
        tarball="$(find "$tmp_dir" -name "*.tar.gz" | head -n 1)"
        if [ -n "$tarball" ] && [ -f "$tarball" ]; then
            echo "deploy: staging release binaries onto VPS ($target:$stage_target)..." >&2
            # Staged into place under a temporary name and moved only once the
            # whole file has landed: build_engine reads whatever sits at
            # $stage_target, and a half-copied tarball there is worse than none.
            if ssh "${SSH_ARGS[@]}" "$target" "mkdir -p /opt/liquidity-migration-engine/staged" \
                && scp "${SSH_ARGS[@]}" "$tarball" "$target:$stage_target.partial" \
                && ssh "${SSH_ARGS[@]}" "$target" "mv -f '$stage_target.partial' '$stage_target'"; then
                echo "deploy: pre-built release binaries staged; skipping host compilation" >&2
            else
                ssh "${SSH_ARGS[@]}" "$target" "rm -f '$stage_target.partial'" 2>/dev/null || true
                echo "deploy: could not stage pre-built binaries; host will build via cargo" >&2
            fi
        fi
    fi
    rm -rf "$tmp_dir"
}

read -r -a SSH_ARGS <<< "$SSH_OPTS"
if [ "$MODE" = deploy ]; then
    stage_ci_binaries_if_available "$EXPECTED_COMMIT" "$SSH_TARGET"
fi
{
    printf 'MODE=%q\n' "$MODE"
    printf 'REPO_URL=%q\n' "$REPO_URL"
    printf 'REPO_DIR=%q\n' "$REPO_DIR"
    printf 'REMOTE=%q\n' "$REMOTE"
    printf 'BRANCH=%q\n' "$BRANCH"
    printf 'EXPECTED_COMMIT=%q\n' "$EXPECTED_COMMIT"
    printf 'GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
    cat <<'REMOTE_SCRIPT'
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

fail() { echo "deploy failed: $*" >&2; exit 1; }

# ---------------------------------------------------------------- constants

RUNTIME_GROUP=liquidity-migration
CONTROLS_GROUP=liquidity-controls
CONTROLS_USER=liquidity-controls
SIGNAL_WORKER_USER=liquidity-signal-worker
DEMO_ENGINE_USER=liquidity-engine-demo
MAINNET_ENGINE_USER=liquidity-engine-mainnet
OBSERVER_USER=liquidity-observer
LLM_USER=liquidity-llm
CAPTURE_USER=liquidity-capture

RELEASE_DIR=/opt/liquidity-migration-engine
ENGINE_BINARY=$RELEASE_DIR/bin/engine
SIGNAL_WORKER_BINARY=$RELEASE_DIR/bin/signal-worker
MARKET_TAPE_BINARY=$RELEASE_DIR/bin/market-tape
ENGINE_CONTROL_HELPER=$RELEASE_DIR/bin/telegram-control-helper
# The commit whose deploy last finished, and the one before it: what rollback
# returns to. Seeded from the checkout when no record exists yet.
DEPLOYED_COMMIT_FILE=$RELEASE_DIR/deployed-commit
PREVIOUS_COMMIT_FILE=$RELEASE_DIR/previous-commit
# Each recorder unit records what it was last started from in
# $RELEASE_DIR/<unit>.fingerprint; a deploy that changes none of it leaves
# that recorder running.
CONTROLS_SUDOERS=/etc/sudoers.d/liquidity-migration-controls
CARGO_TARGET_ROOT=/opt/engine-build-target
RUST_TOOLCHAIN_DIR=/opt/rust

ENGINE_ENVIRONMENT=/etc/liquidity-migration/engine.env
ENGINE_DEMO_CONFIG=/etc/liquidity-migration/engine.toml
ENGINE_MAINNET_ENVIRONMENT=/etc/liquidity-migration/engine-mainnet.env
ENGINE_MAINNET_CONFIG=/etc/liquidity-migration/engine-mainnet.toml
MAINNET_CREDENTIAL_ENV=/etc/liquidity-migration/bybit-mainnet.env
MAINNET_TELEGRAM_ENV=/etc/liquidity-migration/telegram-mainnet.env
SIGNAL_WORKER_DEMO_ENV=/etc/liquidity-migration/signal-worker-demo.env
SIGNAL_WORKER_MAINNET_ENV=/etc/liquidity-migration/signal-worker-mainnet.env
DEMO_SIGNAL_SOURCE_ENV=/etc/liquidity-migration/signal-worker-demo-source.env
MAINNET_SIGNAL_SOURCE_ENV=/etc/liquidity-migration/signal-worker-mainnet-source.env

LONG_DEMO_ROOT=/opt/liquidity-migration/data/bybit-long-demo-event
CARRY_DEMO_ROOT=/opt/liquidity-migration/data/bybit-carry-demo-event
EXODUS_DEMO_ROOT=/opt/liquidity-migration/data/bybit-exodus-demo-event
LONG_MAINNET_ROOT=/opt/liquidity-migration/data/bybit-long-mainnet-event
CARRY_MAINNET_ROOT=/opt/liquidity-migration/data/bybit-carry-mainnet-event
EXODUS_MAINNET_ROOT=/opt/liquidity-migration/data/bybit-exodus-mainnet-event
SIGNAL_SPOOL_ROOT=/var/lib/liquidity-migration/signals
CONTROL_SPOOL_ROOT=/var/lib/liquidity-migration/controls

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
    git -C "$REPO_DIR" checkout -B "$BRANCH" "$EXPECTED_COMMIT" \
        || fail "cannot check out $EXPECTED_COMMIT"
    [ "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$EXPECTED_COMMIT" ] \
        || fail "checkout is not at EXPECTED_COMMIT"
    # The remote body runs from the ssh login directory. Every
    # `python -m liquidity_migration.*` below resolves the package from the
    # working directory alone: the venv installs requirements.lock with
    # --no-deps and never the project, and there is no PYTHONPATH.
    cd "$REPO_DIR" || fail "cannot enter $REPO_DIR"
}

# ----------------------------------------------------------------- helpers

mainnet_armed() {
    [ -f "$MAINNET_CREDENTIAL_ENV" ] || return 1
    local value
    value="$(
        sed -n 's/^REAL_MONEY=\([^#]*\).*/\1/p' "$MAINNET_CREDENTIAL_ENV" \
            | head -1 | tr -d "\"' " | tr '[:upper:]' '[:lower:]'
    )"
    case "$value" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac
}

# Wait for a heartbeat this run's process wrote. `since` is read before the
# unit starts, so a file left by the previous generation cannot satisfy it.
wait_fresh_heartbeat() {
    local unit="$1" heartbeat="$2" since="$3" attempt written
    for attempt in $(seq 1 90); do
        if systemctl is-active --quiet "$unit" && [ -f "$heartbeat" ]; then
            written="$(stat -c %Y "$heartbeat")"
            if [ "$written" -ge "$since" ]; then
                echo "heartbeat-ok unit=$unit age=$(( $(date +%s) - written ))s"
                return 0
            fi
        fi
        sleep 2
    done
    fail "$unit did not publish a fresh heartbeat at $heartbeat"
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

rollback_after_failure() {
    local realm="$1" failed="$EXPECTED_COMMIT" target
    if [ "${AUTO_ROLLBACK:-0}" = 1 ]; then
        fail "$realm did not come up on the rolled-back commit $failed either; the fleet is stopped"
    fi
    target="$(rollback_target)" \
        || fail "$realm did not come up on $failed and no earlier finished deploy is recorded"
    [ "$target" != "$failed" ] \
        || fail "$realm did not come up on $failed and the only recorded generation is that commit"
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
    local user
    for user in "$SIGNAL_WORKER_USER" "$DEMO_ENGINE_USER" "$MAINNET_ENGINE_USER" \
        "$OBSERVER_USER" "$LLM_USER" "$CAPTURE_USER"; do
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
    local path
    for path in "$SIGNAL_SPOOL_ROOT" "$SIGNAL_SPOOL_ROOT/demo" "$SIGNAL_SPOOL_ROOT/mainnet"; do
        install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0770 "$path"
    done
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$CONTROL_SPOOL_ROOT"
    install -d -o "$DEMO_ENGINE_USER" -g "$RUNTIME_GROUP" -m 0750 "$CONTROL_SPOOL_ROOT/demo"
    install -d -o "$MAINNET_ENGINE_USER" -g "$RUNTIME_GROUP" -m 0750 "$CONTROL_SPOOL_ROOT/mainnet"
    install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration-signal-worker-demo \
        /var/lib/liquidity-migration-signal-worker-mainnet
    install -d -o "$DEMO_ENGINE_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration-engine
    install -d -o "$MAINNET_ENGINE_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration-engine-mainnet
    install -d -o "$LLM_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/llm-driver-ledger
    install -d -o "$CAPTURE_USER" -g "$RUNTIME_GROUP" -m 0750 \
        /var/lib/liquidity-migration/forward-market
    # The backup and upload receipts; the host watchdog reads their ages.
    install -d -o root -g root -m 0755 /var/lib/liquidity-migration/receipts
    install -d -o "$SIGNAL_WORKER_USER" -g "$RUNTIME_GROUP" -m 0750 \
        "$LONG_DEMO_ROOT" "$CARRY_DEMO_ROOT" "$EXODUS_DEMO_ROOT" \
        "$LONG_MAINNET_ROOT" "$CARRY_MAINNET_ROOT" "$EXODUS_MAINNET_ROOT"
}

# ------------------------------------------------------------ build/install

install_python_environment() {
    [ -d "$REPO_DIR/.venv" ] || /usr/bin/python3 -m venv "$REPO_DIR/.venv" \
        || fail "cannot create the Python environment"
    "$PYTHON" -m pip install --disable-pip-version-check --no-deps \
        --only-binary=:all: -r "$REPO_DIR/requirements.lock" \
        || fail "cannot install locked Python dependencies"
}

build_engine() {
    local staged_tar="/opt/liquidity-migration-engine/staged/${EXPECTED_COMMIT}.tar.gz"
    if [ -f "$staged_tar" ]; then
        echo "deploy: installing pre-built release binaries from CI artifact $staged_tar"
        install -d -o root -g root -m 0755 "$CARGO_TARGET_ROOT/release"
        tar -xzf "$staged_tar" -C "$CARGO_TARGET_ROOT/release" \
            || fail "cannot unpack the staged release artifact $staged_tar"
        # Not conditional: an artifact that omits the manifest is refused, not
        # installed unverified. The manifest travels inside the tarball, so this
        # catches a corrupt or truncated transfer, not a forged one.
        [ -f "$CARGO_TARGET_ROOT/release/binaries.sha256" ] \
            || fail "staged artifact has no binaries.sha256 manifest; refusing unverified binaries"
        (cd "$CARGO_TARGET_ROOT/release" && sha256sum -c binaries.sha256 >/dev/null) \
            || fail "pre-built binary sha256 checksum verification failed"
        test -x "$CARGO_TARGET_ROOT/release/engine" || fail "staged engine binary is missing or not executable"
        test -x "$CARGO_TARGET_ROOT/release/signal-worker" || fail "staged signal-worker binary is missing or not executable"
        echo "deploy: pre-built release binaries verified successfully; skipping host compilation"
        return 0
    fi
    if [ "${AUTO_ROLLBACK:-0}" = 1 ] && [ -f "$ENGINE_BINARY.previous" ] && [ -f "$SIGNAL_WORKER_BINARY.previous" ]; then
        echo "rollback: using cached previous release binaries; skipping compilation"
        return 0
    fi
    local toolchain
    toolchain="$(sed -n 's/^channel = "\(.*\)"/\1/p' "$REPO_DIR/rust-toolchain.toml")"
    [ -n "$toolchain" ] || fail "cannot read the pinned Rust toolchain"
    install -d -o root -g root -m 0755 "$CARGO_TARGET_ROOT"
    (
        cd "$REPO_DIR/engine"
        HOME=/root \
        PATH="$RUST_TOOLCHAIN_DIR/cargo/bin:/usr/bin:/bin" \
        CARGO_HOME="$RUST_TOOLCHAIN_DIR/cargo" \
        RUSTUP_HOME="$RUST_TOOLCHAIN_DIR/rustup" \
        RUSTUP_TOOLCHAIN="$toolchain" \
        nice -n 10 cargo build --release --locked --workspace --bins \
            --jobs 2 \
            --target-dir "$CARGO_TARGET_ROOT"
    ) || fail "cannot build the engine workspace"
}

stop_realm_units() {
    local realm="$1" unit
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        systemctl disable --now "$unit" 2>/dev/null || true
        systemctl reset-failed "$unit" 2>/dev/null || true
    done < <(lm_realm_units "$realm")
}

install_release() {
    install -d -o root -g root -m 0755 "${ENGINE_BINARY%/*}"
    if [ "${AUTO_ROLLBACK:-0}" = 1 ] && [ -f "$ENGINE_BINARY.previous" ] && [ -f "$SIGNAL_WORKER_BINARY.previous" ]; then
        echo "rollback: restoring previous release binaries"
        cp -pf "$ENGINE_BINARY.previous" "$ENGINE_BINARY" || fail "cannot restore engine binary"
        cp -pf "$SIGNAL_WORKER_BINARY.previous" "$SIGNAL_WORKER_BINARY" || fail "cannot restore signal-worker binary"
        if [ -f "$MARKET_TAPE_BINARY.previous" ]; then
            cp -pf "$MARKET_TAPE_BINARY.previous" "$MARKET_TAPE_BINARY" 2>/dev/null || true
        fi
    else
        if [ -f "$ENGINE_BINARY" ]; then
            cp -pf "$ENGINE_BINARY" "$ENGINE_BINARY.previous" 2>/dev/null || true
        fi
        if [ -f "$SIGNAL_WORKER_BINARY" ]; then
            cp -pf "$SIGNAL_WORKER_BINARY" "$SIGNAL_WORKER_BINARY.previous" 2>/dev/null || true
        fi
        if [ -f "$MARKET_TAPE_BINARY" ]; then
            cp -pf "$MARKET_TAPE_BINARY" "$MARKET_TAPE_BINARY.previous" 2>/dev/null || true
        fi
        install -o root -g "$RUNTIME_GROUP" -m 0755 \
            "$CARGO_TARGET_ROOT/release/engine" "$ENGINE_BINARY" \
            || fail "cannot install the engine binary"
        install -o root -g "$RUNTIME_GROUP" -m 0755 \
            "$CARGO_TARGET_ROOT/release/signal-worker" "$SIGNAL_WORKER_BINARY" \
            || fail "cannot install the signal-worker binary"
        if [ -f "$CARGO_TARGET_ROOT/release/market-tape" ]; then
            install -o root -g "$RUNTIME_GROUP" -m 0755 \
                "$CARGO_TARGET_ROOT/release/market-tape" "$MARKET_TAPE_BINARY" \
                || fail "cannot install the market-tape binary"
        fi
    fi
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

# Every liquidity-migration unit on the host except the independent ones. The
# host inventory, not the manifest, so a unit the new manifest retired stops
# too.
stop_fleet() {
    local unit independent
    independent=" $(lm_independent_units | tr '\n' ' ') "
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        case "$independent" in *" $unit "*) continue ;; esac
        systemctl disable --now "$unit" 2>/dev/null || true
        systemctl reset-failed "$unit" 2>/dev/null || true
    done < <(lm_host_liqmig_units)
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
        cat "$REPO_DIR/requirements.lock"
    } 2>/dev/null | sha256sum | cut -c1-64
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
                    && (wait_fresh_heartbeat "$unit" "$CAPTURE_STATUS" "$since"); then
                    printf '%s\n' "$fingerprint" > "$fingerprint_file"
                    echo "capture-ok unit=$unit result=restarted"
                else
                    # Not fatal: a recorder is independent of the fleet in
                    # both directions, and the host watchdog pages on it.
                    echo "warning: $unit did not publish a fresh status file; the fleet deploy continues" >&2
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

# Project the allowlisted worker inputs from the private source env into the
# root-owned env systemd hands the credential-free worker.
write_signal_worker_environment() {
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
allowed = {"OPERATIONAL_PROFILE_FILE", "SIGNAL_WORKER_REALM"}
values = load_private_systemd_environment(source)
filtered = {key: value for key, value in values.items() if key in allowed}
if filtered.get("SIGNAL_WORKER_REALM") not in {"demo", "mainnet"}:
    raise SystemExit(f"{source}: SIGNAL_WORKER_REALM must be demo or mainnet")
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
    local dial_env=""
    [ -f "$MAINNET_CREDENTIAL_ENV" ] && dial_env="$MAINNET_CREDENTIAL_ENV"
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$(dirname "$output")"
    "$PYTHON" -m liquidity_migration.policy.real_money_arming render-profile \
        --from-env "$dial_env" --execute --overwrite --output "$output" \
        || fail "operational dials do not render a loadable profile"
}

render_engine_config() {
    local realm="$1" operational_config="$2" output="$3"
    local template signal_config long_entries carry_entries
    local -a maker_args=()
    case "$realm" in
        demo)
            template="$REPO_DIR/deploy/engine.demo.toml.template"
            signal_config="$REPO_DIR/configs/signal-worker.demo.json"
            case "${LONG_SLEEVE:-off}" in
                on|ON|1|true|TRUE|yes|YES) long_entries=true ;;
                *) long_entries=false ;;
            esac
            case "${CARRY_SLEEVE:-off}" in
                on|ON|1|true|TRUE|yes|YES) carry_entries=true ;;
                *) carry_entries=false ;;
            esac
            ;;
        mainnet)
            template="$REPO_DIR/deploy/engine.mainnet.toml.template"
            signal_config="$REPO_DIR/configs/signal-worker.mainnet.json"
            long_entries=true
            carry_entries=true
            maker_args=(--maker-rule "$REPO_DIR/configs/lane2_toxic_flow_quoter_v1.json")
            ;;
        *) fail "unsupported engine realm: $realm" ;;
    esac
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
        --exodus-entries-enabled true \
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
    install -d -o root -g "$RUNTIME_GROUP" -m 0750 /etc/liquidity-migration
    [ -f "$DEMO_SIGNAL_SOURCE_ENV" ] || install -o root -g root -m 0600 \
        "$REPO_DIR/deploy/signal-worker-demo.env.template" "$DEMO_SIGNAL_SOURCE_ENV"
    [ -f /etc/liquidity-migration/bybit-demo.env ] \
        || fail "missing demo credential file: /etc/liquidity-migration/bybit-demo.env"
    [ -f "$ENGINE_ENVIRONMENT" ] || fail "missing engine environment: $ENGINE_ENVIRONMENT"
    lm_load_sleeve_toggles
    lm_write_resolved_sleeve_toggles
    chown root:root /etc/liquidity-migration/sleeves.resolved.env
    chmod 0600 /etc/liquidity-migration/sleeves.resolved.env
    unset SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    lm_load_private_systemd_environment "$PYTHON" "$DEMO_SIGNAL_SOURCE_ENV" \
        SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    [ "$SIGNAL_WORKER_REALM" = demo ] \
        || fail "demo signal-worker source must declare SIGNAL_WORKER_REALM=demo"
    render_operational_profile "$OPERATIONAL_PROFILE_FILE"
    write_signal_worker_environment "$DEMO_SIGNAL_SOURCE_ENV" "$SIGNAL_WORKER_DEMO_ENV"
    render_engine_config demo "$OPERATIONAL_PROFILE_FILE" "$ENGINE_DEMO_CONFIG"
}

# --------------------------------------------------------- state takeover

run_engine_takeover_command() {
    local realm="$1" config="$2" runtime_user engine_env credential_env
    shift 2
    case "$realm" in
        demo)
            runtime_user="$DEMO_ENGINE_USER"
            engine_env="$ENGINE_ENVIRONMENT"
            credential_env=/etc/liquidity-migration/bybit-demo.env
            ;;
        mainnet)
            runtime_user="$MAINNET_ENGINE_USER"
            engine_env="$ENGINE_MAINNET_ENVIRONMENT"
            credential_env="$MAINNET_CREDENTIAL_ENV"
            ;;
        *) fail "unsupported takeover realm: $realm" ;;
    esac
    (
        unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET \
            BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET \
            BYBIT_REAL_API_KEY_IP BYBIT_REAL_API_KEY_BACKUP_IP \
            BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID REAL_MONEY \
            BYBIT_INVENTORY_CREDENTIAL_SET \
            EXPECTED_ENGINE_ACCOUNT_USER_ID EXPECTED_ENGINE_VENUE EXPECTED_ENGINE_REALM
        case "$realm" in
            demo)
                lm_load_private_systemd_environment "$PYTHON" "$credential_env" \
                    BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET
                ;;
            mainnet)
                # REAL_MONEY comes from the owner's credential file and is read,
                # never written, here: the engine refuses a funded takeover
                # without it, and an unarmed file still refuses.
                lm_load_private_systemd_environment "$PYTHON" "$credential_env" \
                    BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET BYBIT_REAL_API_KEY_IP \
                    BYBIT_REAL_API_KEY_BACKUP_IP BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID \
                    REAL_MONEY BYBIT_INVENTORY_CREDENTIAL_SET
                ;;
        esac
        lm_load_private_systemd_environment "$PYTHON" "$engine_env" \
            EXPECTED_ENGINE_ACCOUNT_USER_ID EXPECTED_ENGINE_VENUE EXPECTED_ENGINE_REALM
        [ -n "${EXPECTED_ENGINE_ACCOUNT_USER_ID:-}" ] \
            || fail "$engine_env does not bind the exact account id"
        exec /usr/bin/setpriv \
            --reuid "$runtime_user" --regid "$RUNTIME_GROUP" --clear-groups \
            "$ENGINE_BINARY" "$@" --config "$config"
    )
}

stage_native_takeover_source() {
    [ "$#" -eq 5 ] || return 2
    local source="$1" template="$2" kind="$3"
    local output_name="$4" temporary_name="$5"
    local staged
    case "$kind" in
        required|carry-early-exits-v1|carry-event-tape-v1) ;;
        *) fail "unsupported takeover source kind: $kind" ;;
    esac
    printf -v "$output_name" '%s' ""
    printf -v "$temporary_name" '%s' ""
    staged="$(mktemp "$template")" \
        || fail "cannot create a staged takeover source for $source"
    printf -v "$output_name" '%s' "$staged"
    printf -v "$temporary_name" '%s' "$staged"
    if ! "$PYTHON" - "$source" "$staged" "$kind" <<'PY'
import sys
from pathlib import Path

source, target, kind = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if source.exists():
    payload = source.read_bytes()
elif kind == "required":
    raise SystemExit(f"required takeover source is missing: {source}")
elif kind == "carry-early-exits-v1":
    payload = b'{"fired":{}}\n'
else:
    payload = b""
target.write_bytes(payload)
PY
    then
        rm -f -- "$staged"
        fail "cannot stage the takeover source bytes for $source"
    fi
    chown root:"$RUNTIME_GROUP" "$staged" && chmod 0640 "$staged" || {
        rm -f -- "$staged"
        fail "cannot secure the staged takeover source for $source"
    }
}

remove_native_takeover_temps() {
    local path
    for path in "$@"; do
        [ -n "$path" ] || continue
        rm -f -- "$path" || echo "cannot remove temporary takeover source: $path" >&2
    done
}

import_native_strategy_state() {
    local realm="$1" config wal long_root carry_root exodus_root
    local long_state carry_checkpoint carry_early_exits carry_book carry_events
    local exodus_identity exodus_state exodus_target_book engine_heartbeat
    local required_present=0 source
    case "$realm" in
        demo)
            config="$ENGINE_DEMO_CONFIG"
            wal=/var/lib/liquidity-migration-engine/engine.wal
            long_root="$LONG_DEMO_ROOT"
            carry_root="$CARRY_DEMO_ROOT"
            exodus_root="$EXODUS_DEMO_ROOT"
            engine_heartbeat=/var/lib/liquidity-migration-engine/heartbeat.json
            ;;
        mainnet)
            config="$ENGINE_MAINNET_CONFIG"
            wal=/var/lib/liquidity-migration-engine-mainnet/engine.wal
            long_root="$LONG_MAINNET_ROOT"
            carry_root="$CARRY_MAINNET_ROOT"
            exodus_root="$EXODUS_MAINNET_ROOT"
            engine_heartbeat=/var/lib/liquidity-migration-engine-mainnet/heartbeat.json
            ;;
        *) fail "unsupported strategy-state import realm: $realm" ;;
    esac
    long_state="/var/lib/liquidity-migration/targets/long-${realm}-state.json"
    carry_checkpoint="$carry_root/.cache/carry_sizing_anchors.json"
    carry_early_exits="$carry_root/carry_early_exits.json"
    carry_book="/var/lib/liquidity-migration/targets/carry-${realm}.json"
    carry_events="$carry_root/carry_presettlement_events.jsonl"
    exodus_identity="$exodus_root/exodus_state_identity.json"
    exodus_state="$exodus_root/exodus_state.json"
    exodus_target_book="/var/lib/liquidity-migration/targets/exodus-${realm}.json"

    if run_engine_takeover_command "$realm" "$config" verify-native-strategy-state; then
        echo "native-state-ok realm=$realm result=already-complete"
        return 0
    fi

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
    [ "$required_present" -eq 5 ] \
        || fail "$realm strategy-state takeover is incomplete: found $required_present of 5 required sources"

    (
        long_state_source="" carry_checkpoint_source="" carry_early_exits_source=""
        carry_book_source="" carry_events_source="" exodus_identity_source=""
        exodus_state_source=""
        long_state_temp="" carry_checkpoint_temp="" carry_early_exits_temp=""
        carry_book_temp="" carry_events_temp="" exodus_identity_temp=""
        exodus_state_temp="" exodus_legacy_paths=""
        cleanup_native_takeover_temps() {
            local status="$?"
            trap - EXIT
            remove_native_takeover_temps \
                "$exodus_legacy_paths" "$long_state_temp" "$carry_checkpoint_temp" \
                "$carry_early_exits_temp" "$carry_book_temp" "$carry_events_temp" \
                "$exodus_identity_temp" "$exodus_state_temp"
            exit "$status"
        }
        trap cleanup_native_takeover_temps EXIT
        stage_native_takeover_source "$long_state" \
            "/run/liquidity-migration/long-${realm}-state.XXXXXX" \
            required long_state_source long_state_temp
        stage_native_takeover_source "$carry_checkpoint" \
            "/run/liquidity-migration/carry-${realm}-sizing.XXXXXX" \
            required carry_checkpoint_source carry_checkpoint_temp
        stage_native_takeover_source "$carry_early_exits" \
            "/run/liquidity-migration/carry-${realm}-early-exits.XXXXXX" \
            carry-early-exits-v1 carry_early_exits_source carry_early_exits_temp
        stage_native_takeover_source "$carry_book" \
            "/run/liquidity-migration/carry-${realm}-target-book.XXXXXX" \
            required carry_book_source carry_book_temp
        stage_native_takeover_source "$carry_events" \
            "/run/liquidity-migration/carry-${realm}-events.XXXXXX" \
            carry-event-tape-v1 carry_events_source carry_events_temp
        stage_native_takeover_source "$exodus_identity" \
            "/run/liquidity-migration/exodus-${realm}-identity.XXXXXX" \
            required exodus_identity_source exodus_identity_temp
        stage_native_takeover_source "$exodus_state" \
            "/run/liquidity-migration/exodus-${realm}-state.XXXXXX" \
            required exodus_state_source exodus_state_temp

        run_engine_takeover_command "$realm" "$config" import-strategy-state \
            --strategy long \
            --source-format long-book-state-v2 \
            --source "state=$long_state_source" \
            || fail "cannot import exact LONG state for $realm"
        run_engine_takeover_command "$realm" "$config" import-strategy-state \
            --strategy carry \
            --source-format carry-sizing-anchors-v1-early-exits-v1-target-book-v1 \
            --source "early_exits=$carry_early_exits_source" \
            --source "sizing_anchors=$carry_checkpoint_source" \
            --source "target_book=$carry_book_source" \
            || fail "cannot import exact CARRY state for $realm"
        exodus_legacy_paths="$(
            mktemp "/run/liquidity-migration/exodus-${realm}-legacy-paths.XXXXXX"
        )" || fail "cannot create the $realm Exodus legacy-path bundle"
        "$PYTHON" - "$carry_events" "$exodus_target_book" "$engine_heartbeat" \
            > "$exodus_legacy_paths" <<'PY'
import json
import sys

event_path, target_book_path, engine_heartbeat_path = sys.argv[1:]
payload = {
    "schema_version": 1,
    "event_path": event_path,
    "target_book_path": target_book_path,
    "engine_heartbeat_path": engine_heartbeat_path,
}
sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
PY
        chown root:"$RUNTIME_GROUP" "$exodus_legacy_paths" \
            && chmod 0640 "$exodus_legacy_paths"
        run_engine_takeover_command "$realm" "$config" import-strategy-state \
            --strategy exodus \
            --source-format exodus-state-v1-v4-event-tape-v1-identity-v2 \
            --source "carry_events=$carry_events_source" \
            --source "identity=$exodus_identity_source" \
            --source "legacy_paths=$exodus_legacy_paths" \
            --source "state=$exodus_state_source" \
            || fail "cannot import exact Exodus state for $realm"
        run_engine_takeover_command "$realm" "$config" verify-native-strategy-state \
            || fail "imported $realm native strategy state failed verification"
    ) || fail "$realm strategy-state takeover failed"
    echo "native-state-ok realm=$realm result=imported"
}

# ----------------------------------------------------------------- mainnet

provision_mainnet() {
    [ -f "$MAINNET_SIGNAL_SOURCE_ENV" ] || install -o root -g root -m 0600 \
        "$REPO_DIR/deploy/signal-worker-mainnet.env.template" "$MAINNET_SIGNAL_SOURCE_ENV"
    "$PYTHON" -m liquidity_migration.policy.real_money_arming default-telegram \
        --from-env /etc/liquidity-migration/bybit-demo.env --execute \
        || fail "cannot default the mainnet Telegram pair"
    unset SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    lm_load_private_systemd_environment "$PYTHON" "$MAINNET_SIGNAL_SOURCE_ENV" \
        SIGNAL_WORKER_REALM OPERATIONAL_PROFILE_FILE
    [ "$SIGNAL_WORKER_REALM" = mainnet ] \
        || fail "funded signal-worker source must declare SIGNAL_WORKER_REALM=mainnet"
    render_operational_profile "$OPERATIONAL_PROFILE_FILE"
    write_signal_worker_environment "$MAINNET_SIGNAL_SOURCE_ENV" "$SIGNAL_WORKER_MAINNET_ENV"
    "$PYTHON" - "$MAINNET_CREDENTIAL_ENV" "$MAINNET_TELEGRAM_ENV" <<'PY'
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
    chown root:root "$MAINNET_TELEGRAM_ENV" && chmod 0600 "$MAINNET_TELEGRAM_ENV" \
        || fail "cannot secure funded notification environment"
    "$PYTHON" -m liquidity_migration.policy.real_money_arming preflight \
        || fail "mainnet preflight has outstanding steps"
    render_engine_config mainnet "$OPERATIONAL_PROFILE_FILE" "$ENGINE_MAINNET_CONFIG"
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
            *" $unit "*) systemctl start "$unit" || fail "cannot start $unit" ;;
            *) start_unit "$unit" ;;
        esac
    done < <(lm_activation_units "$realm" start)
}

# ------------------------------------------------------------------ verify

verify_mode() {
    echo "commit $(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "deployed $(cat "$DEPLOYED_COMMIT_FILE" 2>/dev/null || echo none)"
    echo "rollback-target $(rollback_target 2>/dev/null || echo none)"
    if mainnet_armed; then echo "real-money armed"; else echo "real-money off"; fi
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
}

# ----------------------------------------------------------- mainnet stops

stop_mainnet_units() {
    local unit
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        systemctl disable --now "$unit" 2>/dev/null || true
    done < <(lm_realm_units mainnet)
}

disarm_mainnet_mode() {
    stop_mainnet_units
    if [ ! -f "$MAINNET_CREDENTIAL_ENV" ]; then
        echo "disarm-mainnet-ok real_money=absent units=stopped"
        return 0
    fi
    /usr/bin/python3 -I - "$MAINNET_CREDENTIAL_ENV" <<'PY'
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
    echo "disarm-mainnet-ok real_money=false units=stopped"
    echo "note: disarm does not flatten existing exposure; reconcile/flatten separately"
}

# ------------------------------------------------------------------ deploy

deploy_mode() {
    seed_generation_record
    fetch_exact_commit
    # Re-read the manifest helpers from the exact commit this run installs. A
    # commit from before the independent lifecycle has no helper for it, and a
    # rollback to one must still run.
    . "$REPO_DIR/deploy/lib_sleeves.sh"
    . "$REPO_DIR/deploy/lib_systemd_environment.sh"
    type lm_independent_units >/dev/null 2>&1 || lm_independent_units() { :; }
    ensure_runtime_identities
    install_python_environment
    build_engine
    # Decoupled deployment: stop Demo units only. Mainnet stays live and trading.
    stop_realm_units demo
    install_release
    install_units
    start_independent_units
    prepare_demo_inputs
    import_native_strategy_state demo
    if ! (start_realm demo); then
        rollback_after_failure demo
    fi
    if mainnet_armed; then
        # provision_mainnet renders the funded config with the binary
        # install_release just put down, so it cannot move above it. The cost is
        # a window where the funded engine still runs the old binary and old
        # config while both new ones sit on disk; Restart=always means a crash
        # in that window restarts it on the new binary against the old config.
        echo "staging mainnet configuration while live engine continues trading"
        provision_mainnet
        echo "atomic mainnet handover: swapping binaries and state"
        stop_realm_units mainnet
        import_native_strategy_state mainnet
        if ! (start_realm mainnet); then
            rollback_after_failure mainnet
        fi
    else
        echo "real-money off: funded units stay stopped"
    fi
    record_generation
    echo "deploy-ok commit=$EXPECTED_COMMIT"
    verify_mode
}

rollback_mode() {
    local target
    target="$(rollback_target)" \
        || fail "no earlier finished deploy is recorded; deploy an exact commit instead"
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
    stop-mainnet)
        stop_mainnet_units
        echo "stop-mainnet-ok"
        echo "note: this stopped publication only; exposure is unchanged. Flatten through the account owner."
        ;;
    disarm-mainnet) disarm_mainnet_mode ;;
    *) fail "unknown deploy mode: $MODE" ;;
esac
REMOTE_SCRIPT
} | ssh "${SSH_ARGS[@]}" -- "$SSH_TARGET" bash -s
