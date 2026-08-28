#!/usr/bin/env bash
# Narrow root boundary for the unprivileged Telegram control panel. The only
# accepted argv are the fixed actions in the final case statement; no path,
# unit, environment name, or toggle value is caller supplied.
set -euo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

REPOSITORY=/opt/liquidity-migration
ENGINE=/opt/liquidity-migration-engine/bin/engine
LAUNCHER=/opt/liquidity-migration-engine/bin/run-authorized-runtime
MARKER=/opt/liquidity-migration-engine/bin/engine.release
HELPER=/opt/liquidity-migration-engine/bin/telegram-control-helper
SUDOERS=/etc/sudoers.d/liquidity-migration-controls
BOT=/opt/liquidity-migration/liquidity_migration/ops/telegram_controls.py
SLEEVES_LIBRARY=/opt/liquidity-migration/deploy/lib_sleeves.sh
SLEEVES_DEFAULT=/opt/liquidity-migration/deploy/sleeves.env
ACTIVATION_RECEIPT=/opt/liquidity-migration-engine/bin/activation.complete
HOST_SLEEVES=/etc/liquidity-migration/sleeves.env
RESOLVED_SLEEVES=/etc/liquidity-migration/sleeves.resolved.env
HELPER_STATE=/var/lib/liquidity-migration-control-helper
SAVED_SLEEVES="$HELPER_STATE/sleeves-before-pause"
MAINTENANCE_LOCK=/run/liquidity-migration/maintenance.lock
CALLER=liquidity-controls

refuse() {
    echo "telegram control helper refused: $*" >&2
    exit 78
}

[ "$(id -u)" -eq 0 ] || refuse "helper must run as root"
[ "$(readlink -f "$0")" = "$HELPER" ] \
    && [ -f "$HELPER" ] && [ ! -L "$HELPER" ] \
    && [ "$(stat -c %u "$HELPER")" -eq 0 ] \
    && [ "$(stat -c %g "$HELPER")" -eq 0 ] \
    && [ "$(stat -c %a "$HELPER")" = 755 ] \
    || refuse "helper did not execute from its root:root mode-0755 fixed path"

# sudo inherits the caller's systemd mount namespace. Keep the bot's checkout
# and /etc views read-only, and ask PID 1 to run the fixed worker in a fresh
# root service. The only caller-controlled word crosses an exact allow-list;
# no path, unit, environment name, or value reaches the worker.
if [ "${1:-}" != --worker ]; then
    [ "$#" -eq 1 ] || refuse "expected one fixed action"
    ACTION="$1"
    case "$ACTION" in
        pause-demo|resume-demo|pause-mainnet|status-demo) ;;
        *) refuse "unsupported action" ;;
    esac
    [ "${SUDO_USER:-}" = "$CALLER" ] \
        && [ "${SUDO_UID:-}" = "$(id -u "$CALLER")" ] \
        || refuse "helper is callable only through the dedicated sudo identity"
    [ -z "${BASH_ENV:-}" ] && [ -z "${ENV:-}" ] \
        || refuse "shell startup injection variables are forbidden"
    exec /usr/bin/systemd-run --quiet --wait --pipe --collect --service-type=exec \
        --unit="liquidity-migration-telegram-control-${ACTION}-$$-$RANDOM" \
        --property=User=root \
        --property=Group=root \
        --property=WorkingDirectory=/ \
        --property=NoNewPrivileges=true \
        --property=PrivateTmp=true \
        --property=PrivateDevices=true \
        --property=ProtectProc=invisible \
        --property=ProcSubset=pid \
        --property=ProtectHome=true \
        --property=ProtectSystem=true \
        --property=RestrictAddressFamilies=AF_UNIX \
        --property=UMask=0077 \
        --property="InaccessiblePaths=-/etc/liquidity-migration/bybit-demo.env -/etc/liquidity-migration/bybit-mainnet.env -/etc/liquidity-migration/bybit-mainnet-attestor.env -/etc/liquidity-migration/engine.env -/etc/liquidity-migration/engine-mainnet.env -/etc/liquidity-migration/engine.toml -/etc/liquidity-migration/engine-mainnet.toml -/etc/liquidity-migration/producer-demo.env -/etc/liquidity-migration/producer-mainnet.env -/etc/liquidity-migration/telegram-mainnet.env -/etc/liquidity-migration/attestor-bootstrap -/etc/liquidity-migration/rollout-attestor-operator-public.pem" \
        /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
            "$HELPER" --worker "$ACTION"
fi

[ "$#" -eq 2 ] && [ "$1" = --worker ] \
    || refuse "invalid privileged worker invocation"
ACTION="$2"
case "$ACTION" in
    pause-demo|resume-demo|pause-mainnet|status-demo) ;;
    *) refuse "unsupported privileged worker action" ;;
esac
[ -z "${SUDO_USER:-}" ] && [ -z "${BASH_ENV:-}" ] && [ -z "${ENV:-}" ] \
    || refuse "privileged worker environment is not clean"
[ -d "$REPOSITORY" ] && [ ! -L "$REPOSITORY" ] \
    && [ "$(readlink -f "$REPOSITORY")" = "$REPOSITORY" ] \
    && [ "$(stat -c %u "$REPOSITORY")" -eq 0 ] \
    && [ "$(stat -c %a "$REPOSITORY")" = 755 ] \
    || refuse "trusted checkout root is missing, linked, or writable"
[ -d "$REPOSITORY/.git" ] && [ ! -L "$REPOSITORY/.git" ] \
    && [ "$(stat -c %u "$REPOSITORY/.git")" -eq 0 ] \
    || refuse "trusted checkout metadata is missing, linked, or not root-owned"

for path in "$ENGINE" "$LAUNCHER" "$MARKER" "$HELPER" "$SUDOERS" "$BOT" \
    "$SLEEVES_LIBRARY" "$SLEEVES_DEFAULT"; do
    [ -f "$path" ] && [ ! -L "$path" ] \
        && [ "$(stat -c %u "$path")" -eq 0 ] \
        || refuse "trusted input is missing, linked, or not root-owned: $path"
done
[ "$(stat -c %g "$SUDOERS")" -eq 0 ] \
    && [ "$(stat -c %a "$SUDOERS")" = 440 ] \
    || refuse "sudoers boundary is not root:root mode 0440"
[ "$(stat -c %a "$MARKER")" = 644 ] \
    && [ "$(stat -c %a "$BOT")" = 644 ] \
    && [ "$(stat -c %a "$SLEEVES_LIBRARY")" = 644 ] \
    && [ "$(stat -c %a "$SLEEVES_DEFAULT")" = 644 ] \
    || refuse "tracked control inputs have unsafe modes"

awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
NR == 7 && $0 == "rustc=1.90.0" { next }
{ exit 1 }
END { if (NR != 7) exit 1 }
' "$MARKER" || refuse "release marker schema is invalid"
marker_commit="$(sed -n 's/^commit=//p' "$MARKER")"
marker_engine="$(sed -n 's/^sha256=//p' "$MARKER")"
marker_launcher="$(sed -n 's/^launcher_sha256=//p' "$MARKER")"
marker_helper="$(sed -n 's/^control_helper_sha256=//p' "$MARKER")"
marker_sudoers="$(sed -n 's/^controls_sudoers_sha256=//p' "$MARKER")"
marker_bot="$(sed -n 's/^telegram_bot_sha256=//p' "$MARKER")"
[[ "$marker_commit" =~ ^[0-9a-f]{40}$ ]] \
    && [[ "$marker_engine" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$marker_launcher" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$marker_helper" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$marker_sudoers" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$marker_bot" =~ ^[0-9a-f]{64}$ ]] \
    || refuse "release marker control digests are invalid"
[ "$(sha256sum "$ENGINE" | awk '{print $1}')" = "$marker_engine" ] \
    && [ "$(sha256sum "$LAUNCHER" | awk '{print $1}')" = "$marker_launcher" ] \
    && [ "$(sha256sum "$HELPER" | awk '{print $1}')" = "$marker_helper" ] \
    && [ "$(sha256sum "$SUDOERS" | awk '{print $1}')" = "$marker_sudoers" ] \
    && [ "$(sha256sum "$BOT" | awk '{print $1}')" = "$marker_bot" ] \
    || refuse "installed control boundary differs from its release marker"

checkout_commit="$(
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
        /usr/bin/git --no-pager --no-optional-locks \
        --git-dir="$REPOSITORY/.git" --work-tree="$REPOSITORY" \
        -c "safe.directory=$REPOSITORY" -c core.hooksPath=/dev/null \
        rev-parse HEAD
)" || refuse "cannot read checkout generation"
[ "$checkout_commit" = "$marker_commit" ] \
    || refuse "checkout is not the installed release generation"
/usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
    GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git --no-pager --no-optional-locks \
    --git-dir="$REPOSITORY/.git" --work-tree="$REPOSITORY" \
    -c "safe.directory=$REPOSITORY" -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null diff-index --quiet "$marker_commit" -- \
    || refuse "tracked checkout differs from the installed generation"
for relative in deploy/lib_sleeves.sh deploy/sleeves.env; do
    committed="$(
        /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
            GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
            /usr/bin/git --no-pager --no-optional-locks \
            --git-dir="$REPOSITORY/.git" --work-tree="$REPOSITORY" \
            -c "safe.directory=$REPOSITORY" -c core.hooksPath=/dev/null \
            show "$marker_commit:$relative" | sha256sum | awk '{print $1}'
    )" || refuse "cannot digest committed helper input: $relative"
    [ "$(sha256sum "$REPOSITORY/$relative" | awk '{print $1}')" = "$committed" ] \
        || refuse "helper input differs from its committed blob: $relative"
done

[ -f "$MAINTENANCE_LOCK" ] && [ ! -L "$MAINTENANCE_LOCK" ] \
    && [ "$(stat -c %u "$MAINTENANCE_LOCK")" -eq 0 ] \
    && [ "$(stat -c %g "$MAINTENANCE_LOCK")" -eq 0 ] \
    && [ "$(stat -c %a "$MAINTENANCE_LOCK")" = 600 ] \
    || refuse "trusted maintenance lock is missing"
exec 9<>"$MAINTENANCE_LOCK" || refuse "cannot open the maintenance lock"
flock --exclusive --nonblock 9 || refuse "deployment or maintenance is active"

if [ -e "$HELPER_STATE" ]; then
    [ -d "$HELPER_STATE" ] && [ ! -L "$HELPER_STATE" ] \
        && [ "$(stat -c %u "$HELPER_STATE")" -eq 0 ] \
        && [ "$(stat -c %g "$HELPER_STATE")" -eq 0 ] \
        && [ "$(stat -c %a "$HELPER_STATE")" = 700 ] \
        || refuse "helper state root is linked or not root:root mode 0700"
else
    install -d -o root -g root -m 0700 "$HELPER_STATE" \
        || refuse "cannot create the helper state root"
fi
LM_HOST_SLEEVES_ENV="$HOST_SLEEVES"
LM_RESOLVED_SLEEVES_ENV="$RESOLVED_SLEEVES"
LM_SYSTEMD_UNIT_DIR=/etc/systemd/system
LM_RUNTIME_SYSTEMD_UNIT_DIR=/run/systemd/system
export LM_HOST_SLEEVES_ENV LM_RESOLVED_SLEEVES_ENV \
    LM_SYSTEMD_UNIT_DIR LM_RUNTIME_SYSTEMD_UNIT_DIR
# shellcheck source=lib_sleeves.sh
source "$SLEEVES_LIBRARY"

validate_private_state_file() {
    local path="$1"
    [ -f "$path" ] && [ ! -L "$path" ] \
        && [ "$(stat -c %u "$path")" -eq 0 ] \
        && [ "$(stat -c %g "$path")" -eq 0 ] \
        && [ "$(stat -c %a "$path")" = 600 ]
}

validate_host_sleeves() {
    local original="$LM_HOST_SLEEVES_ENV" status=0
    LM_HOST_SLEEVES_ENV="$1"
    export LM_HOST_SLEEVES_ENV
    lm_load_sleeve_toggles || status=$?
    LM_HOST_SLEEVES_ENV="$original"
    export LM_HOST_SLEEVES_ENV
    return "$status"
}

activation_complete() {
    [ -f "$ACTIVATION_RECEIPT" ] && [ ! -L "$ACTIVATION_RECEIPT" ] \
        && [ "$(stat -c %u "$ACTIVATION_RECEIPT")" -eq 0 ] \
        && [ "$(stat -c %g "$ACTIVATION_RECEIPT")" -eq 0 ] \
        && [ "$(stat -c %a "$ACTIVATION_RECEIPT")" = 644 ] \
        || return 1
    awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
{ exit 1 }
END { if (NR != 6) exit 1 }
' "$ACTIVATION_RECEIPT" >/dev/null || return 1
    [ "$(sed -n 's/^commit=//p' "$ACTIVATION_RECEIPT")" = "$marker_commit" ] \
        && [ "$(sed -n 's/^sha256=//p' "$ACTIVATION_RECEIPT")" = "$marker_engine" ] \
        && [ "$(sed -n 's/^launcher_sha256=//p' "$ACTIVATION_RECEIPT")" = "$marker_launcher" ] \
        && [ "$(sed -n 's/^control_helper_sha256=//p' "$ACTIVATION_RECEIPT")" = "$marker_helper" ] \
        && [ "$(sed -n 's/^controls_sudoers_sha256=//p' "$ACTIVATION_RECEIPT")" = "$marker_sudoers" ] \
        && [ "$(sed -n 's/^telegram_bot_sha256=//p' "$ACTIVATION_RECEIPT")" = "$marker_bot" ]
}

atomic_install_text() {
    local destination="$1" content="$2" temporary
    [ ! -L "$destination" ] || refuse "state destination is linked: $destination"
    temporary="$(mktemp "${destination}.tmp.XXXXXX")" \
        || refuse "cannot stage state file"
    printf '%s' "$content" > "$temporary" || refuse "cannot write staged state"
    chown root:root "$temporary" && chmod 0600 "$temporary" \
        || refuse "cannot secure staged state"
    mv -f "$temporary" "$destination" || refuse "cannot atomically install state"
    sync
}

save_original_once() {
    local temporary
    [ ! -L "$SAVED_SLEEVES" ] || refuse "saved sleeve state is linked"
    if [ -e "$SAVED_SLEEVES" ]; then
        validate_private_state_file "$SAVED_SLEEVES" \
            || refuse "saved sleeve state is not root:root mode 0600"
        return 0
    fi
    if [ -e "$HOST_SLEEVES" ]; then
        validate_private_state_file "$HOST_SLEEVES" \
            || refuse "host sleeve override is not root:root mode 0600"
        validate_host_sleeves "$HOST_SLEEVES" \
            || refuse "host sleeve override is invalid"
        temporary="$(mktemp "${SAVED_SLEEVES}.tmp.XXXXXX")" \
            || refuse "cannot stage the saved pre-pause sleeve state"
        install -o root -g root -m 0600 "$HOST_SLEEVES" "$temporary" \
            && mv -f "$temporary" "$SAVED_SLEEVES" \
            || refuse "cannot atomically save the pre-pause sleeve state"
    else
        atomic_install_text "$SAVED_SLEEVES" $'__ABSENT__\n'
    fi
    sync
}

write_resolved() {
    lm_load_sleeve_toggles
    lm_write_resolved_sleeve_toggles
    chown root:root "$RESOLVED_SLEEVES" && chmod 0600 "$RESOLVED_SLEEVES"
    validate_private_state_file "$RESOLVED_SLEEVES" \
        || refuse "resolved sleeve state failed installed validation"
    sync
}

quarantine_pair() {
    local first="$1" second="$2" unit enabled failed=0
    /usr/bin/systemctl --quiet disable --now "$first" "$second" \
        2>/dev/null || true
    for unit in "$first" "$second"; do
        /usr/bin/systemctl --quiet disable "$unit" 2>/dev/null || true
        /usr/bin/systemctl --quiet stop "$unit" 2>/dev/null || true
    done
    for unit in "$first" "$second"; do
        if /usr/bin/systemctl is-active --quiet "$unit"; then
            failed=1
        fi
        enabled="$(/usr/bin/systemctl is-enabled "$unit" 2>/dev/null || true)"
        case "$enabled" in
            disabled|masked|masked-runtime|not-found) ;;
            *) failed=1 ;;
        esac
    done
    sync
    [ "$failed" -eq 0 ]
}

pause_demo() {
    save_original_once
    quarantine_pair \
        liquidity-migration-bybit-long-demo.service \
        liquidity-migration-bybit-carry-demo.service \
        || refuse "demo pause could not quarantine both producers"
    atomic_install_text "$HOST_SLEEVES" \
        $'# paused by telegram-control-helper\nLONG_SLEEVE=off\nCARRY_SLEEVE=off\n'
    write_resolved
    echo "paused=demo"
}

resume_demo() {
    local temporary start_status=0
    activation_complete \
        || refuse "demo resume requires this generation's completed activation receipt"
    /usr/bin/systemctl is-active --quiet liquidity-migration-engine.service \
        || refuse "demo resume requires the account owner to be active"
    validate_private_state_file "$SAVED_SLEEVES" \
        || refuse "no validated pre-pause sleeve state exists"
    if [ "$(cat "$SAVED_SLEEVES")" = "__ABSENT__" ]; then
        rm -f -- "$HOST_SLEEVES"
    else
        validate_host_sleeves "$SAVED_SLEEVES" \
            || refuse "saved pre-pause sleeve state is invalid"
        temporary="$(mktemp "${HOST_SLEEVES}.tmp.XXXXXX")" \
            || refuse "cannot stage restored sleeve state"
        install -o root -g root -m 0600 "$SAVED_SLEEVES" "$temporary"
        mv -f "$temporary" "$HOST_SLEEVES" \
            || refuse "cannot atomically restore sleeve state"
    fi
    write_resolved
    if sleeve_on "$LONG_SLEEVE"; then
        /usr/bin/systemctl --quiet enable --now \
            liquidity-migration-bybit-long-demo.service || start_status=$?
    fi
    if [ "$start_status" -eq 0 ] && sleeve_on "$CARRY_SLEEVE"; then
        /usr/bin/systemctl --quiet enable --now \
            liquidity-migration-bybit-carry-demo.service || start_status=$?
    fi
    if [ "$start_status" -eq 0 ] && sleeve_on "$LONG_SLEEVE" \
        && ! /usr/bin/systemctl is-active --quiet \
            liquidity-migration-bybit-long-demo.service; then
        start_status=1
    fi
    if [ "$start_status" -eq 0 ] && sleeve_on "$CARRY_SLEEVE" \
        && ! /usr/bin/systemctl is-active --quiet \
            liquidity-migration-bybit-carry-demo.service; then
        start_status=1
    fi
    if [ "$start_status" -ne 0 ]; then
        quarantine_pair \
            liquidity-migration-bybit-long-demo.service \
            liquidity-migration-bybit-carry-demo.service 2>/dev/null || true
        refuse "demo resume failed; both producers were re-quarantined"
    fi
    rm -f -- "$SAVED_SLEEVES"
    sync
    printf 'resumed=demo long=%s carry=%s\n' "$LONG_SLEEVE" "$CARRY_SLEEVE"
}

pause_mainnet() {
    quarantine_pair \
        liquidity-migration-bybit-long-mainnet.service \
        liquidity-migration-bybit-carry-mainnet.service \
        || refuse "mainnet pause could not quarantine both funded producers"
    echo "paused=mainnet"
}

status_demo() {
    lm_load_sleeve_toggles || refuse "cannot resolve demo sleeve state"
    if [ -e "$SAVED_SLEEVES" ]; then
        validate_private_state_file "$SAVED_SLEEVES" \
            || refuse "saved sleeve state is invalid"
        echo "paused=true"
    else
        echo "paused=false"
    fi
    printf 'LONG_SLEEVE=%s\nCARRY_SLEEVE=%s\n' "$LONG_SLEEVE" "$CARRY_SLEEVE"
}

case "$ACTION" in
    pause-demo) pause_demo ;;
    resume-demo) resume_demo ;;
    pause-mainnet) pause_mainnet ;;
    status-demo) status_demo ;;
esac
