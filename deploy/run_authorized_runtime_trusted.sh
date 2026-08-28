#!/bin/bash
# Root-installed generation gate. The mutable checkout never decides whether
# its own workloads are allowed to start.
set -euo pipefail

REPOSITORY=/opt/liquidity-migration
ENGINE=/opt/liquidity-migration-engine/bin/engine
LAUNCHER=/opt/liquidity-migration-engine/bin/run-authorized-runtime
CONTROL_HELPER=/opt/liquidity-migration-engine/bin/telegram-control-helper
TELEGRAM_BOT=/opt/liquidity-migration/liquidity_migration/ops/telegram_controls.py
MARKER=/opt/liquidity-migration-engine/bin/engine.release
ACTIVATION_RECEIPT=/opt/liquidity-migration-engine/bin/activation.complete
ACTIVATION_PERMIT=/run/liquidity-migration/activation.permit
ACTIVATION_LEASE_SECONDS=6
CHECKOUT_WRAPPER=/opt/liquidity-migration/scripts/run_authorized_runtime.sh

refuse() {
    echo "authorized runtime refused: $*" >&2
    exit 78
}

process_start_ticks_match() {
    local expected_pid="$1" expected_start_ticks="$2" process_stat process_tail
    local -a process_fields=()
    [[ "$expected_pid" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$expected_start_ticks" =~ ^[1-9][0-9]*$ ]] \
        && [ -r "/proc/$expected_pid/stat" ] \
        || return 1
    process_stat="$(<"/proc/$expected_pid/stat")" || return 1
    process_tail="${process_stat##*) }"
    read -r -a process_fields <<< "$process_tail" || return 1
    [ "${#process_fields[@]}" -ge 20 ] \
        && [ "${process_fields[0]}" != Z ] \
        && [ "${process_fields[0]}" != X ] \
        && [ "${process_fields[19]}" = "$expected_start_ticks" ]
}

# The ordinary launcher cannot inspect the root rollout process because every
# workload retains ProtectProc=invisible. A short-lived root transient service
# owns this lease instead. It is the only process that verifies PID reuse via
# /proc; unprivileged launchers merely consume its root-owned freshness record.
watchdog_permit_matches() {
    local path="$ACTIVATION_PERMIT" line1 line2 line3 line4 line5 line6
    local line7 line8 line9 line10 extra file_boot_id file_owner_pid
    local file_owner_start_ticks file_not_after current_epoch
    [ -f "$path" ] && [ ! -L "$path" ] \
        && [ "$(stat -c %u "$path")" -eq 0 ] \
        && [ "$(stat -c %g "$path")" -eq 0 ] \
        && [ "$(stat -c %a "$path")" = 644 ] \
        || return 1
    {
        IFS= read -r line1 \
            && IFS= read -r line2 \
            && IFS= read -r line3 \
            && IFS= read -r line4 \
            && IFS= read -r line5 \
            && IFS= read -r line6 \
            && IFS= read -r line7 \
            && IFS= read -r line8 \
            && IFS= read -r line9 \
            && IFS= read -r line10 \
            && ! IFS= read -r extra
    } < "$path" || return 1
    [ "$line1" = "commit=$WATCHDOG_MARKER_COMMIT" ] \
        && [ "$line2" = "sha256=$WATCHDOG_MARKER_DIGEST" ] \
        && [ "$line3" = "launcher_sha256=$WATCHDOG_MARKER_LAUNCHER_DIGEST" ] \
        && [ "$line4" = "control_helper_sha256=$WATCHDOG_MARKER_HELPER_DIGEST" ] \
        && [ "$line5" = "controls_sudoers_sha256=$WATCHDOG_MARKER_SUDOERS_DIGEST" ] \
        && [ "$line6" = "telegram_bot_sha256=$WATCHDOG_MARKER_BOT_DIGEST" ] \
        || return 1
    file_boot_id="${line7#boot_id=}"
    file_owner_pid="${line8#owner_pid=}"
    file_owner_start_ticks="${line9#owner_start_ticks=}"
    file_not_after="${line10#not_after_epoch=}"
    [ "$line7" = "boot_id=$file_boot_id" ] \
        && [ "$line8" = "owner_pid=$file_owner_pid" ] \
        && [ "$line9" = "owner_start_ticks=$file_owner_start_ticks" ] \
        && [ "$line10" = "not_after_epoch=$file_not_after" ] \
        && [ "$file_boot_id" = "$WATCHDOG_BOOT_ID" ] \
        && [ "$file_owner_pid" = "$WATCHDOG_OWNER_PID" ] \
        && [ "$file_owner_start_ticks" = "$WATCHDOG_OWNER_START_TICKS" ] \
        && [[ "$file_not_after" =~ ^[0-9]{10,}$ ]] \
        || return 1
    printf -v current_epoch '%(%s)T' -1 || return 1
    [ "$current_epoch" -lt "$file_not_after" ] \
        && [ "$file_not_after" -le "$((current_epoch + ACTIVATION_LEASE_SECONDS + 2))" ] \
        && process_start_ticks_match \
            "$WATCHDOG_OWNER_PID" "$WATCHDOG_OWNER_START_TICKS"
}

watchdog_remove_owned_permit() {
    local permit_owner_pid="" permit_owner_start_ticks=""
    if [ -f "$ACTIVATION_PERMIT" ] && [ ! -L "$ACTIVATION_PERMIT" ]; then
        permit_owner_pid="$(sed -n 's/^owner_pid=//p' "$ACTIVATION_PERMIT" 2>/dev/null || true)"
        permit_owner_start_ticks="$(sed -n 's/^owner_start_ticks=//p' "$ACTIVATION_PERMIT" 2>/dev/null || true)"
        if [ "$permit_owner_pid" = "${WATCHDOG_OWNER_PID:-}" ] \
            && [ "$permit_owner_start_ticks" = "${WATCHDOG_OWNER_START_TICKS:-}" ]; then
            rm -f -- "$ACTIVATION_PERMIT" 2>/dev/null || true
        fi
    fi
}

watchdog_open_permit_is_current() {
    local descriptor_path="/proc/self/fd/$WATCHDOG_PERMIT_FD"
    [ -e "$descriptor_path" ] \
        && [ -f "$ACTIVATION_PERMIT" ] \
        && [ ! -L "$ACTIVATION_PERMIT" ] \
        && [ "$ACTIVATION_PERMIT" -ef "$descriptor_path" ] \
        && [ "$(stat -Lc %h "$descriptor_path")" -eq 1 ] \
        && [ "$(stat -Lc %u "$descriptor_path")" -eq 0 ] \
        && [ "$(stat -Lc %g "$descriptor_path")" -eq 0 ] \
        && [ "$(stat -Lc %a "$descriptor_path")" = 644 ]
}

watchdog_refresh_permit() {
    local current_epoch not_after descriptor_path status=0
    printf -v current_epoch '%(%s)T' -1 || return 1
    not_after="$((current_epoch + ACTIVATION_LEASE_SECONDS))"
    descriptor_path="/proc/self/fd/$WATCHDOG_PERMIT_FD"
    watchdog_open_permit_is_current || return 1
    /usr/bin/flock -x "$WATCHDOG_PERMIT_FD" || return 1
    # Keep one inode for the lifetime of this activation. An unlink after the
    # identity check can make this descriptor anonymous, but it cannot make the
    # watchdog recreate the pathname. Readers take a shared lock, so they see
    # either the prior complete lease or the next complete lease.
    if ! watchdog_open_permit_is_current \
        || ! watchdog_permit_matches \
        || ! printf 'commit=%s\nsha256=%s\nlauncher_sha256=%s\ncontrol_helper_sha256=%s\ncontrols_sudoers_sha256=%s\ntelegram_bot_sha256=%s\nboot_id=%s\nowner_pid=%s\nowner_start_ticks=%s\nnot_after_epoch=%s\n' \
            "$WATCHDOG_MARKER_COMMIT" "$WATCHDOG_MARKER_DIGEST" \
            "$WATCHDOG_MARKER_LAUNCHER_DIGEST" "$WATCHDOG_MARKER_HELPER_DIGEST" \
            "$WATCHDOG_MARKER_SUDOERS_DIGEST" "$WATCHDOG_MARKER_BOT_DIGEST" \
            "$WATCHDOG_BOOT_ID" "$WATCHDOG_OWNER_PID" \
            "$WATCHDOG_OWNER_START_TICKS" "$not_after" > "$descriptor_path" \
        || ! watchdog_open_permit_is_current; then
        status=1
    fi
    /usr/bin/flock -u "$WATCHDOG_PERMIT_FD" || status=1
    return "$status"
}

activation_watchdog_mode() {
    local pinned_permit_fd initial_permit_identity pinned_permit_identity
    [ "$#" -eq 3 ] || refuse "activation watchdog expected OWNER_PID and OWNER_START_TICKS"
    [ "$EUID" -eq 0 ] || refuse "activation watchdog must run as root"
    WATCHDOG_OWNER_PID="$2"
    WATCHDOG_OWNER_START_TICKS="$3"
    [[ "$WATCHDOG_OWNER_PID" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$WATCHDOG_OWNER_START_TICKS" =~ ^[1-9][0-9]*$ ]] \
        || refuse "activation watchdog owner identity is invalid"
    [ "$(readlink -f "$0")" = "$LAUNCHER" ] \
        && [ -f "$LAUNCHER" ] && [ ! -L "$LAUNCHER" ] \
        && [ "$(stat -c %u "$LAUNCHER")" -eq 0 ] \
        && [ "$(stat -c %g "$LAUNCHER")" -eq 0 ] \
        && [ "$(stat -c %a "$LAUNCHER")" = 755 ] \
        || refuse "activation watchdog launcher boundary is invalid"
    [ -d "${LAUNCHER%/*}" ] && [ ! -L "${LAUNCHER%/*}" ] \
        && [ "$(stat -c %u "${LAUNCHER%/*}")" -eq 0 ] \
        && [ "$(stat -c %g "${LAUNCHER%/*}")" -eq 0 ] \
        && [ "$(stat -c %a "${LAUNCHER%/*}")" = 755 ] \
        || refuse "activation watchdog release directory boundary is invalid"
    [ -f "$MARKER" ] && [ ! -L "$MARKER" ] \
        && [ "$(stat -c %u "$MARKER")" -eq 0 ] \
        && [ "$(stat -c %g "$MARKER")" -eq 0 ] \
        && [ "$(stat -c %a "$MARKER")" = 644 ] \
        || refuse "activation watchdog release marker boundary is invalid"
    [ -d "${ACTIVATION_PERMIT%/*}" ] && [ ! -L "${ACTIVATION_PERMIT%/*}" ] \
        && [ "$(stat -c %u "${ACTIVATION_PERMIT%/*}")" -eq 0 ] \
        && [ "$(stat -c %g "${ACTIVATION_PERMIT%/*}")" -eq 0 ] \
        && [ "$(stat -c %a "${ACTIVATION_PERMIT%/*}")" = 755 ] \
        || refuse "activation watchdog permit directory boundary is invalid"
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
' "$MARKER" || refuse "activation watchdog release marker schema is invalid"
    WATCHDOG_MARKER_COMMIT="$(sed -n 's/^commit=//p' "$MARKER")"
    WATCHDOG_MARKER_DIGEST="$(sed -n 's/^sha256=//p' "$MARKER")"
    WATCHDOG_MARKER_LAUNCHER_DIGEST="$(sed -n 's/^launcher_sha256=//p' "$MARKER")"
    WATCHDOG_MARKER_HELPER_DIGEST="$(sed -n 's/^control_helper_sha256=//p' "$MARKER")"
    WATCHDOG_MARKER_SUDOERS_DIGEST="$(sed -n 's/^controls_sudoers_sha256=//p' "$MARKER")"
    WATCHDOG_MARKER_BOT_DIGEST="$(sed -n 's/^telegram_bot_sha256=//p' "$MARKER")"
    [[ "$WATCHDOG_MARKER_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
        && [[ "$WATCHDOG_MARKER_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$WATCHDOG_MARKER_LAUNCHER_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$WATCHDOG_MARKER_HELPER_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$WATCHDOG_MARKER_SUDOERS_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$WATCHDOG_MARKER_BOT_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
        || refuse "activation watchdog marker digests are invalid"
    [ "$(sha256sum "$LAUNCHER" | awk '{print $1}')" \
        = "$WATCHDOG_MARKER_LAUNCHER_DIGEST" ] \
        || refuse "activation watchdog launcher digest does not match the marker"
    WATCHDOG_BOOT_ID="$(< /proc/sys/kernel/random/boot_id)" \
        || refuse "activation watchdog cannot read the boot identity"
    [[ "$WATCHDOG_BOOT_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
        || refuse "activation watchdog boot identity is invalid"
    trap 'watchdog_remove_owned_permit' EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    trap 'exit 129' HUP
    initial_permit_identity="$(stat -c '%d:%i' "$ACTIVATION_PERMIT")" \
        || refuse "activation watchdog cannot identify the initial permit inode"
    [[ "$initial_permit_identity" =~ ^[0-9]+:[0-9]+$ ]] \
        || refuse "activation watchdog initial permit inode identity is invalid"
    watchdog_permit_matches \
        || refuse "activation watchdog initial permit or owner identity is invalid"
    # A direct <> open carries O_CREAT. Pin read-only first, then reopen that
    # already-open inode through procfs for writing. An unlink or replacement
    # in either gap can only make the identity check fail; it cannot recreate
    # the activation pathname.
    exec {pinned_permit_fd}<"$ACTIVATION_PERMIT" \
        || refuse "activation watchdog cannot pin the permit inode read-only"
    [ "$ACTIVATION_PERMIT" -ef "/proc/self/fd/$pinned_permit_fd" ] \
        || refuse "activation watchdog permit changed while pinning its inode"
    pinned_permit_identity="$(stat -Lc '%d:%i' "/proc/self/fd/$pinned_permit_fd")" \
        || refuse "activation watchdog cannot identify the pinned permit inode"
    [ "$pinned_permit_identity" = "$initial_permit_identity" ] \
        || refuse "activation watchdog permit inode changed after validation"
    exec {WATCHDOG_PERMIT_FD}<>"/proc/self/fd/$pinned_permit_fd" \
        || refuse "activation watchdog cannot pin the permit inode"
    exec {pinned_permit_fd}<&- \
        || refuse "activation watchdog cannot close the read-only permit pin"
    /usr/bin/flock -x "$WATCHDOG_PERMIT_FD" \
        || refuse "activation watchdog cannot lock the pinned permit inode"
    if ! watchdog_open_permit_is_current || ! watchdog_permit_matches; then
        /usr/bin/flock -u "$WATCHDOG_PERMIT_FD" 2>/dev/null || true
        refuse "activation watchdog pinned permit changed after validation"
    fi
    /usr/bin/flock -u "$WATCHDOG_PERMIT_FD" \
        || refuse "activation watchdog cannot unlock the pinned permit inode"
    while :; do
        watchdog_refresh_permit \
            || refuse "activation watchdog cannot refresh the permit"
        sleep 1
        watchdog_permit_matches \
            || refuse "activation watchdog permit expired, changed, or lost its owner"
    done
}

if [ "${1:-}" = --activation-watchdog ]; then
    activation_watchdog_mode "$@"
fi

trusted_checkout_directory() {
    local directory="$1" mode
    [ -d "$directory" ] && [ ! -L "$directory" ] \
        && [ "$(readlink -f "$directory")" = "$directory" ] \
        && [ "$(stat -c %u "$directory")" -eq 0 ] \
        || return 1
    mode="$(stat -c %a "$directory")" || return 1
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] \
        && (( (8#$mode & 0022) == 0 ))
}

[ "$#" -eq 2 ] || refuse "expected UNIT and ENTRYPOINT"
[ "$(readlink -f "$0")" = "$LAUNCHER" ] \
    || refuse "launcher did not execute from its fixed path"
for directory in "${REPOSITORY%/*}" "$REPOSITORY" "$REPOSITORY/scripts" \
    "$REPOSITORY/liquidity_migration" "$REPOSITORY/liquidity_migration/ops" \
    "$REPOSITORY/.git"; do
    trusted_checkout_directory "$directory" \
        || refuse "trusted checkout ancestry is missing, linked, non-root-owned, or group/world-writable: $directory"
done
for path in "$ENGINE" "$LAUNCHER" "$CONTROL_HELPER" "$TELEGRAM_BOT" \
    "$MARKER" "$CHECKOUT_WRAPPER"; do
    [ -f "$path" ] && [ ! -L "$path" ] \
        || refuse "release input is missing, linked, or not regular: $path"
    [ "$(stat -c %u "$path")" -eq 0 ] \
        || refuse "release input is not root-owned: $path"
done
[ "$(stat -c %a "$ENGINE")" = 755 ] \
    && [ "$(stat -c %a "$LAUNCHER")" = 755 ] \
    && [ "$(stat -c %a "$CONTROL_HELPER")" = 755 ] \
    && [ "$(stat -c %a "$TELEGRAM_BOT")" = 644 ] \
    && [ "$(stat -c %a "$CHECKOUT_WRAPPER")" = 755 ] \
    && [ "$(stat -c %a "$MARKER")" = 644 ] \
    || refuse "release input modes do not match the installed contract"
unsafe_git_metadata="$(
    /usr/bin/find "$REPOSITORY/.git" -xdev -mindepth 1 \
        \( ! -uid 0 -o -perm /022 -o -type l \
            -o \( ! -type f -a ! -type d \) \) \
        -print -quit
)" || refuse "cannot inspect trusted checkout metadata permissions"
[ -z "$unsafe_git_metadata" ] \
    || refuse "trusted checkout metadata contains a non-root-owned, writable, linked, or special entry: $unsafe_git_metadata"
[ -d "${ENGINE%/*}" ] && [ ! -L "${ENGINE%/*}" ] \
    && [ "$(stat -c %u "${ENGINE%/*}")" -eq 0 ] \
    && [ "$(stat -c %a "${ENGINE%/*}")" = 755 ] \
    || refuse "release directory is not a root-owned, non-writable fixed boundary"
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
marker_digest="$(sed -n 's/^sha256=//p' "$MARKER")"
marker_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "$MARKER")"
marker_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "$MARKER")"
marker_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "$MARKER")"
marker_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "$MARKER")"
[[ "$marker_commit" =~ ^[0-9a-f]{40}$ ]] \
    || refuse "release marker commit is invalid"
[[ "$marker_digest" =~ ^[0-9a-f]{64}$ ]] \
    || refuse "release marker engine digest is invalid"
[[ "$marker_launcher_digest" =~ ^[0-9a-f]{64}$ ]] \
    || refuse "release marker launcher digest is invalid"
[[ "$marker_helper_digest" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$marker_sudoers_digest" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$marker_bot_digest" =~ ^[0-9a-f]{64}$ ]] \
    || refuse "release marker control boundary digests are invalid"

activation_authority_matches_unlocked() {
    local path="$1" kind="$2" file_commit file_digest file_launcher_digest
    local file_helper_digest file_sudoers_digest file_bot_digest
    local file_boot_id file_owner_pid file_owner_start_ticks file_not_after current_epoch
    [ -f "$path" ] && [ ! -L "$path" ] \
        && [ "$(stat -c %u "$path")" -eq 0 ] \
        && [ "$(stat -c %g "$path")" -eq 0 ] \
        && [ "$(stat -c %a "$path")" = 644 ] \
        || return 1
    file_commit="$(sed -n 's/^commit=//p' "$path")"
    file_digest="$(sed -n 's/^sha256=//p' "$path")"
    file_launcher_digest="$(sed -n 's/^launcher_sha256=//p' "$path")"
    file_helper_digest="$(sed -n 's/^control_helper_sha256=//p' "$path")"
    file_sudoers_digest="$(sed -n 's/^controls_sudoers_sha256=//p' "$path")"
    file_bot_digest="$(sed -n 's/^telegram_bot_sha256=//p' "$path")"
    [ "$file_commit" = "$marker_commit" ] \
        && [ "$file_digest" = "$marker_digest" ] \
        && [ "$file_launcher_digest" = "$marker_launcher_digest" ] \
        && [ "$file_helper_digest" = "$marker_helper_digest" ] \
        && [ "$file_sudoers_digest" = "$marker_sudoers_digest" ] \
        && [ "$file_bot_digest" = "$marker_bot_digest" ] \
        || return 1
    case "$kind" in
        complete)
            awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
{ exit 1 }
END { if (NR != 6) exit 1 }
' "$path" >/dev/null
            ;;
        permit)
            # ProcSubset=pid intentionally hides the kernel boot-id file from
            # workload users. /run is boot-ephemeral; the root watchdog alone
            # compares this canonical value with the real kernel boot id.
            [ -d "${ACTIVATION_PERMIT%/*}" ] \
                && [ ! -L "${ACTIVATION_PERMIT%/*}" ] \
                && [ "$(stat -c %u "${ACTIVATION_PERMIT%/*}")" -eq 0 ] \
                && [ "$(stat -c %g "${ACTIVATION_PERMIT%/*}")" -eq 0 ] \
                && [ "$(stat -c %a "${ACTIVATION_PERMIT%/*}")" = 755 ] \
                || return 1
            awk '
NR == 1 && /^commit=/ { next }
NR == 2 && /^sha256=/ { next }
NR == 3 && /^launcher_sha256=/ { next }
NR == 4 && /^control_helper_sha256=/ { next }
NR == 5 && /^controls_sudoers_sha256=/ { next }
NR == 6 && /^telegram_bot_sha256=/ { next }
NR == 7 && /^boot_id=/ { next }
NR == 8 && /^owner_pid=/ { next }
NR == 9 && /^owner_start_ticks=/ { next }
NR == 10 && /^not_after_epoch=/ { next }
{ exit 1 }
END { if (NR != 10) exit 1 }
' "$path" >/dev/null || return 1
            file_boot_id="$(sed -n 's/^boot_id=//p' "$path")"
            file_owner_pid="$(sed -n 's/^owner_pid=//p' "$path")"
            file_owner_start_ticks="$(sed -n 's/^owner_start_ticks=//p' "$path")"
            file_not_after="$(sed -n 's/^not_after_epoch=//p' "$path")"
            [[ "$file_boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
                && [[ "$file_owner_pid" =~ ^[1-9][0-9]*$ ]] \
                && [[ "$file_owner_start_ticks" =~ ^[1-9][0-9]*$ ]] \
                && [[ "$file_not_after" =~ ^[0-9]{10,}$ ]] \
                || return 1
            current_epoch="$(date -u +%s)" || return 1
            [ "$current_epoch" -lt "$file_not_after" ] \
                && [ "$file_not_after" -le "$((current_epoch + ACTIVATION_LEASE_SECONDS + 2))" ]
            ;;
        *) return 1 ;;
    esac
}

with_locked_activation_authority() {
    local validator="$1" path="$2" kind="$3" authority_fd descriptor_path
    local status=1
    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    exec {authority_fd}<"$path" || return 1
    descriptor_path="/proc/self/fd/$authority_fd"
    if /usr/bin/flock -s "$authority_fd" \
        && [ "$path" -ef "$descriptor_path" ] \
        && [ "$(stat -Lc %h "$descriptor_path")" -eq 1 ] \
        && "$validator" "$path" "$kind" \
        && [ "$path" -ef "$descriptor_path" ]; then
        status=0
    fi
    /usr/bin/flock -u "$authority_fd" || status=1
    exec {authority_fd}<&- || status=1
    return "$status"
}

activation_authority_matches() {
    with_locked_activation_authority \
        activation_authority_matches_unlocked "$1" "$2"
}

# Hot supervision check: both authority directories were proved root-owned and
# non-writable above/in activation_authority_matches. A shared inode lock makes
# each hot read atomic with the watchdog's in-place lease renewal.
activation_authority_content_matches_unlocked() {
    local path="$1" kind="$2" line1 line2 line3 line4 line5 line6
    local line7 line8 line9 line10 extra file_boot_id file_owner_pid
    local file_owner_start_ticks file_not_after current_epoch
    [ -f "$path" ] && [ ! -L "$path" ] || return 1
    case "$kind" in
        complete)
            {
                IFS= read -r line1 \
                    && IFS= read -r line2 \
                    && IFS= read -r line3 \
                    && IFS= read -r line4 \
                    && IFS= read -r line5 \
                    && IFS= read -r line6 \
                    && ! IFS= read -r extra
            } < "$path" || return 1
            ;;
        permit)
            {
                IFS= read -r line1 \
                    && IFS= read -r line2 \
                    && IFS= read -r line3 \
                    && IFS= read -r line4 \
                    && IFS= read -r line5 \
                    && IFS= read -r line6 \
                    && IFS= read -r line7 \
                    && IFS= read -r line8 \
                    && IFS= read -r line9 \
                    && IFS= read -r line10 \
                    && ! IFS= read -r extra
            } < "$path" || return 1
            ;;
        *) return 1 ;;
    esac
    [ "$line1" = "commit=$marker_commit" ] \
        && [ "$line2" = "sha256=$marker_digest" ] \
        && [ "$line3" = "launcher_sha256=$marker_launcher_digest" ] \
        && [ "$line4" = "control_helper_sha256=$marker_helper_digest" ] \
        && [ "$line5" = "controls_sudoers_sha256=$marker_sudoers_digest" ] \
        && [ "$line6" = "telegram_bot_sha256=$marker_bot_digest" ] \
        || return 1
    [ "$kind" = complete ] && return 0
    file_boot_id="${line7#boot_id=}"
    file_owner_pid="${line8#owner_pid=}"
    file_owner_start_ticks="${line9#owner_start_ticks=}"
    file_not_after="${line10#not_after_epoch=}"
    [ "$line7" = "boot_id=$file_boot_id" ] \
        && [ "$line8" = "owner_pid=$file_owner_pid" ] \
        && [ "$line9" = "owner_start_ticks=$file_owner_start_ticks" ] \
        && [ "$line10" = "not_after_epoch=$file_not_after" ] \
        && [[ "$file_boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
        && [[ "$file_owner_pid" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$file_owner_start_ticks" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$file_not_after" =~ ^[0-9]{10,}$ ]] \
        || return 1
    printf -v current_epoch '%(%s)T' -1 || return 1
    [ "$current_epoch" -lt "$file_not_after" ] \
        && [ "$file_not_after" -le "$((current_epoch + ACTIVATION_LEASE_SECONDS + 2))" ]
}

activation_authority_content_matches() {
    with_locked_activation_authority \
        activation_authority_content_matches_unlocked "$1" "$2"
}
checkout_commit="$(
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
        /usr/bin/git --no-pager --no-optional-locks \
        --git-dir="$REPOSITORY/.git" --work-tree="$REPOSITORY" \
        -c "safe.directory=$REPOSITORY" -c core.hooksPath=/dev/null \
        rev-parse HEAD
)" || refuse "cannot read checkout HEAD"
[ "$checkout_commit" = "$marker_commit" ] \
    || refuse "checkout HEAD is not the installed release generation"
# HEAD alone does not bind checked-out bytes. Refuse tracked modifications,
# and independently bind the mutable checkout dispatcher to its committed
# blob before handing it control.
/usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
    GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git --no-pager --no-optional-locks \
    --git-dir="$REPOSITORY/.git" --work-tree="$REPOSITORY" \
    -c "safe.directory=$REPOSITORY" -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null diff-index --quiet "$marker_commit" -- \
    || refuse "tracked checkout bytes differ from the installed generation"
committed_wrapper_digest="$(
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
        /usr/bin/git --no-pager --no-optional-locks \
        --git-dir="$REPOSITORY/.git" --work-tree="$REPOSITORY" \
        -c "safe.directory=$REPOSITORY" -c core.hooksPath=/dev/null \
        show "$marker_commit:scripts/run_authorized_runtime.sh" \
        | sha256sum | awk '{print $1}'
)" || refuse "cannot digest the committed runtime dispatcher"
[ "$(sha256sum "$CHECKOUT_WRAPPER" | awk '{print $1}')" = "$committed_wrapper_digest" ] \
    || refuse "runtime dispatcher differs from its committed blob"
[ "$(sha256sum "$ENGINE" | awk '{print $1}')" = "$marker_digest" ] \
    || refuse "engine digest does not match the release marker"
[ "$(sha256sum "$LAUNCHER" | awk '{print $1}')" = "$marker_launcher_digest" ] \
    || refuse "launcher digest does not match the release marker"
[ "$(sha256sum "$CONTROL_HELPER" | awk '{print $1}')" = "$marker_helper_digest" ] \
    || refuse "control helper digest does not match the release marker"
[ "$(sha256sum "$TELEGRAM_BOT" | awk '{print $1}')" = "$marker_bot_digest" ] \
    || refuse "Telegram controls bot digest does not match the release marker"

activation_authority_valid() {
    activation_authority_content_matches "$ACTIVATION_RECEIPT" complete \
        || activation_authority_content_matches "$ACTIVATION_PERMIT" permit \
        || activation_authority_content_matches "$ACTIVATION_RECEIPT" complete
}

if activation_authority_matches "$ACTIVATION_RECEIPT" complete; then
    :
elif activation_authority_matches "$ACTIVATION_PERMIT" permit; then
    :
elif activation_authority_matches "$ACTIVATION_RECEIPT" complete; then
    # The completion receipt may have committed while the permit validator was
    # waiting on the watchdog's inode lock. Recheck the durable side of the OR
    # before refusing this generation.
    :
else
    refuse "generation has no valid activation completion receipt or live activation permit"
fi

# A permit is a root-watchdog freshness lease, not a static timeout. Checking
# only at exec would let a killed deploy leave the already-started subset
# running in the same boot. Keep the launcher as the service main process and
# revoke within one polling interval after the short lease expires or vanishes.
child_pid=""
terminate_child() {
    local attempt
    trap - INT TERM HUP
    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        kill -TERM "$child_pid" 2>/dev/null || true
        for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
            kill -0 "$child_pid" 2>/dev/null || break
            sleep 0.25
        done
        kill -KILL "$child_pid" 2>/dev/null || true
    fi
    wait "$child_pid" 2>/dev/null || true
}
trap 'terminate_child; exit 130' INT
trap 'terminate_child; exit 143' TERM
trap 'terminate_child; exit 129' HUP

/bin/bash "$CHECKOUT_WRAPPER" "$@" &
child_pid=$!
# The workload inherited the unit's reviewed priority. Demote only this
# supervisor after the fork so its two-second timer and file reads cannot
# preempt an execution engine or producer.
if ! /usr/bin/renice -n 19 -p "$$" >/dev/null; then
    terminate_child
    refuse "cannot demote the activation-authority supervisor"
fi
while kill -0 "$child_pid" 2>/dev/null; do
    process_stat="$(<"/proc/$child_pid/stat")" 2>/dev/null || break
    process_tail="${process_stat##*) }"
    process_state="${process_tail%% *}"
    [ "$process_state" != Z ] || break
    if ! activation_authority_valid; then
        echo "authorized runtime revoking workload: activation authority disappeared, expired, or changed" >&2
        terminate_child
        exit 78
    fi
    sleep 2
done
child_status=0
wait "$child_pid" || child_status=$?
exit "$child_status"
