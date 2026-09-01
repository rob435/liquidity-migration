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
SIGNAL_WORKER=/opt/liquidity-migration-engine/bin/signal-worker
HELPER=/opt/liquidity-migration-engine/bin/telegram-control-helper
SUDOERS=/etc/sudoers.d/liquidity-migration-controls
BOT=/opt/liquidity-migration/liquidity_migration/ops/telegram_controls.py
SLEEVES_LIBRARY=/opt/liquidity-migration/deploy/lib_sleeves.sh
SLEEVES_DEFAULT=/opt/liquidity-migration/deploy/sleeves.env
FLEET_MANIFEST=/opt/liquidity-migration/deploy/fleet_manifest.tsv
HOST_SLEEVES=/etc/liquidity-migration/sleeves.env
RESOLVED_SLEEVES=/etc/liquidity-migration/sleeves.resolved.env
HELPER_STATE=/var/lib/liquidity-migration-control-helper
SAVED_SLEEVES="$HELPER_STATE/sleeves-before-pause"
CALLER=liquidity-controls
RUNTIME_GROUP=liquidity-migration
DEMO_ENGINE_USER=liquidity-engine-demo
MAINNET_ENGINE_USER=liquidity-engine-mainnet
DEMO_ENGINE_CONFIG=/etc/liquidity-migration/engine.toml
MAINNET_ENGINE_CONFIG=/etc/liquidity-migration/engine-mainnet.toml
DEMO_HEARTBEAT=/var/lib/liquidity-migration-engine/heartbeat.json
MAINNET_HEARTBEAT=/var/lib/liquidity-migration-engine-mainnet/heartbeat.json

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
        pause-demo|resume-demo|pause-mainnet|resume-mainnet|status-fleet) ;;
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
        --property="InaccessiblePaths=-/etc/liquidity-migration/bybit-demo.env -/etc/liquidity-migration/bybit-mainnet.env -/etc/liquidity-migration/bybit-mainnet-attestor.env -/etc/liquidity-migration/engine.env -/etc/liquidity-migration/engine-mainnet.env -/etc/liquidity-migration/signal-worker-demo.env -/etc/liquidity-migration/signal-worker-mainnet.env -/etc/liquidity-migration/telegram-mainnet.env" \
        /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
            "$HELPER" --worker "$ACTION"
fi

[ "$#" -eq 2 ] && [ "$1" = --worker ] \
    || refuse "invalid privileged worker invocation"
ACTION="$2"
case "$ACTION" in
    pause-demo|resume-demo|pause-mainnet|resume-mainnet|status-fleet) ;;
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

for path in "$ENGINE" "$SIGNAL_WORKER" "$HELPER" "$SUDOERS" "$BOT" \
    "$SLEEVES_LIBRARY" "$SLEEVES_DEFAULT" "$FLEET_MANIFEST"; do
    [ -f "$path" ] \
        || refuse "trusted input is missing: $path"
done



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
LM_FLEET_MANIFEST="$FLEET_MANIFEST"
LM_SYSTEMD_UNIT_DIR=/etc/systemd/system
LM_RUNTIME_SYSTEMD_UNIT_DIR=/run/systemd/system
export LM_HOST_SLEEVES_ENV LM_RESOLVED_SLEEVES_ENV LM_FLEET_MANIFEST \
    LM_SYSTEMD_UNIT_DIR LM_RUNTIME_SYSTEMD_UNIT_DIR
# shellcheck source=lib_sleeves.sh
source "$SLEEVES_LIBRARY"
lm_validate_fleet_manifest || refuse "fleet manifest is invalid"
DEMO_OWNER_UNIT="$(lm_owner_unit demo)" \
    || refuse "fleet manifest has no demo account owner"
MAINNET_OWNER_UNIT="$(lm_owner_unit mainnet)" \
    || refuse "fleet manifest has no funded account owner"

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

runtime_control() {
    local realm="$1" strategy="$2" command="$3" value="$4" request_id="$5"
    local user config
    case "$realm" in
        demo)
            user="$DEMO_ENGINE_USER"
            config="$DEMO_ENGINE_CONFIG"
            ;;
        mainnet)
            user="$MAINNET_ENGINE_USER"
            config="$MAINNET_ENGINE_CONFIG"
            ;;
        *) refuse "unknown runtime-control realm" ;;
    esac
    [ -x /usr/bin/setpriv ] || refuse "setpriv is required for runtime controls"
    case "$command" in
        entries)
            /usr/bin/setpriv --reuid="$user" --regid="$RUNTIME_GROUP" --init-groups \
                /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
                "$ENGINE" set-strategy-entry-permission \
                --config "$config" --strategy "$strategy" \
                --entries-enabled "$value" --request-id "$request_id" --wait-ms 30000
            ;;
        flatten)
            /usr/bin/setpriv --reuid="$user" --regid="$RUNTIME_GROUP" --init-groups \
                /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
                "$ENGINE" flatten-strategy --config "$config" \
                --strategy "$strategy" --request-id "$request_id" --wait-ms 30000
            ;;
        *) refuse "unknown runtime-control command" ;;
    esac
}

heartbeat_entries() {
    local heartbeat="$1"
    [ -f "$heartbeat" ] && [ ! -L "$heartbeat" ] \
        || return 1
    /usr/bin/python3 - "$heartbeat" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload["strategy_entries_enabled"]
    values = {row["strategy"]: row["entries_enabled"] for row in rows}
    if len(values) != len(rows):
        raise ValueError("duplicate strategy")
    for strategy in ("long", "carry", "exodus"):
        value = values[strategy]
        if type(value) is not bool:
            raise ValueError("entry value is not bool")
        print(f"{strategy}|{str(value).lower()}")
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
}

wait_heartbeat_entries() {
    local heartbeat="$1" expected_long="$2" expected_carry="$3" expected_exodus="$4"
    local expected observed attempt
    expected="$(printf 'long|%s\ncarry|%s\nexodus|%s\n' \
        "$expected_long" "$expected_carry" "$expected_exodus")"
    for attempt in $(seq 1 200); do
        observed="$(heartbeat_entries "$heartbeat" 2>/dev/null || true)"
        [ "$observed" = "$expected" ] && return 0
        sleep 0.1
    done
    return 1
}

new_request_prefix() {
    printf '%s-%s-%s\n' "$1" "$(date +%s%N)" "$$"
}

pause_demo() {
    local request
    /usr/bin/systemctl is-active --quiet "$DEMO_OWNER_UNIT" \
        || refuse "demo pause requires the account owner to be active"
    save_original_once
    request="$(new_request_prefix pause-demo)"
    runtime_control demo long entries false "${request}-long" \
        || refuse "demo LONG pause was not durably applied"
    runtime_control demo carry entries false "${request}-carry" \
        || refuse "demo CARRY pause was not durably applied"
    runtime_control demo exodus entries false "${request}-exodus" \
        || refuse "demo Exodus pause was not durably applied"
    wait_heartbeat_entries "$DEMO_HEARTBEAT" false false false \
        || refuse "demo pause was applied but heartbeat did not acknowledge all sleeves"
    atomic_install_text "$HOST_SLEEVES" \
        $'# paused by telegram-control-helper\nLONG_SLEEVE=off\nCARRY_SLEEVE=off\n'
    write_resolved
    echo "paused=demo"
}

resume_demo() {
    local temporary request long_enabled carry_enabled
    /usr/bin/systemctl is-active --quiet "$DEMO_OWNER_UNIT" \
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
    [ "$LONG_SLEEVE" = on ] && long_enabled=true || long_enabled=false
    [ "$CARRY_SLEEVE" = on ] && carry_enabled=true || carry_enabled=false
    request="$(new_request_prefix resume-demo)"
    runtime_control demo long entries "$long_enabled" "${request}-long" \
        || refuse "demo LONG resume was not durably applied"
    runtime_control demo carry entries "$carry_enabled" "${request}-carry" \
        || refuse "demo CARRY resume was not durably applied"
    runtime_control demo exodus entries true "${request}-exodus" \
        || refuse "demo Exodus resume was not durably applied"
    wait_heartbeat_entries "$DEMO_HEARTBEAT" "$long_enabled" "$carry_enabled" true \
        || refuse "demo resume was applied but heartbeat did not acknowledge all sleeves"
    rm -f -- "$SAVED_SLEEVES"
    sync
    printf 'resumed=demo long=%s carry=%s\n' "$LONG_SLEEVE" "$CARRY_SLEEVE"
}

pause_mainnet() {
    local request
    /usr/bin/systemctl is-active --quiet "$MAINNET_OWNER_UNIT" \
        || refuse "funded pause requires the account owner to be active"
    request="$(new_request_prefix pause-mainnet)"
    runtime_control mainnet long entries false "${request}-long" \
        || refuse "funded LONG pause was not durably applied"
    runtime_control mainnet carry entries false "${request}-carry" \
        || refuse "funded CARRY pause was not durably applied"
    runtime_control mainnet exodus entries false "${request}-exodus" \
        || refuse "funded Exodus pause was not durably applied"
    wait_heartbeat_entries "$MAINNET_HEARTBEAT" false false false \
        || refuse "funded pause was applied but heartbeat did not acknowledge all sleeves"
    echo "paused=mainnet"
}

# Undo pause_mainnet, and nothing wider. REAL_MONEY lives in a root-owned file
# this helper never opens, so a resume cannot arm a disarmed account: with the
# switch off the funded owner is not running and the check below refuses.
resume_mainnet() {
    local request
    /usr/bin/systemctl is-active --quiet "$MAINNET_OWNER_UNIT" \
        || refuse "funded resume requires the funded account owner to be active"
    request="$(new_request_prefix resume-mainnet)"
    runtime_control mainnet long entries true "${request}-long" \
        || refuse "funded LONG resume was not durably applied"
    runtime_control mainnet carry entries true "${request}-carry" \
        || refuse "funded CARRY resume was not durably applied"
    runtime_control mainnet exodus entries true "${request}-exodus" \
        || refuse "funded Exodus resume was not durably applied"
    wait_heartbeat_entries "$MAINNET_HEARTBEAT" true true true \
        || refuse "funded resume was applied but heartbeat did not acknowledge all sleeves"
    sync
    printf 'resumed=mainnet\n'
}

status_fleet() {
    local paused=false row unit realm role sleeve active demo_entries mainnet_entries
    lm_load_sleeve_toggles || refuse "cannot resolve demo sleeve state"
    demo_entries="$(heartbeat_entries "$DEMO_HEARTBEAT")" \
        || refuse "demo heartbeat has no exact strategy entry permissions"
    mainnet_entries="$(heartbeat_entries "$MAINNET_HEARTBEAT")" \
        || refuse "funded heartbeat has no exact strategy entry permissions"
    if [ "$demo_entries" = $'long|false\ncarry|false\nexodus|false' ]; then
        paused=true
    fi
    printf 'fleet-status-v1\n'
    printf 'demo-control|paused|%s\n' "$paused"
    printf 'sleeve|long|%s\n' "$LONG_SLEEVE"
    printf 'sleeve|carry|%s\n' "$CARRY_SLEEVE"
    while IFS='|' read -r sleeve active; do
        printf 'entries|demo|%s|%s\n' "$sleeve" "$active"
    done <<< "$demo_entries"
    while IFS='|' read -r sleeve active; do
        printf 'entries|mainnet|%s|%s\n' "$sleeve" "$active"
    done <<< "$mainnet_entries"
    while IFS='|' read -r unit realm role sleeve; do
        [ -n "$unit" ] || continue
        active="$(/usr/bin/systemctl is-active "$unit" 2>/dev/null || true)"
        case "$active" in
            active|reloading|inactive|failed|activating|deactivating|maintenance|refreshing|unknown) ;;
            *) active=unknown ;;
        esac
        printf 'unit|%s|%s|%s|%s|%s\n' \
            "$realm" "$role" "$sleeve" "$unit" "$active"
    done < <(lm_operator_status_rows)
}

case "$ACTION" in
    pause-demo) pause_demo ;;
    resume-demo) resume_demo ;;
    pause-mainnet) pause_mainnet ;;
    resume-mainnet) resume_mainnet ;;
    status-fleet) status_fleet ;;
esac
