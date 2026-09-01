# Shared sleeve, systemd-manifest, and topology helpers: one place for the
# sleeve->units mapping and the on/off predicate, so deploy and verify cannot
# drift. Toggles come from deploy/sleeves.env plus a host override that can only
# narrow a repo-on sleeve to off. bash-3.2-safe (no associative arrays).
# shellcheck shell=bash

LM_HOST_SLEEVES_ENV="${LM_HOST_SLEEVES_ENV:-/etc/liquidity-migration/sleeves.env}"
LM_RESOLVED_SLEEVES_ENV="${LM_RESOLVED_SLEEVES_ENV:-/etc/liquidity-migration/sleeves.resolved.env}"
LM_SYSTEMD_UNIT_DIR="${LM_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
LM_RUNTIME_SYSTEMD_UNIT_DIR="${LM_RUNTIME_SYSTEMD_UNIT_DIR:-/run/systemd/system}"
_LM_DEPLOY_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LM_FLEET_MANIFEST="${LM_FLEET_MANIFEST:-$_LM_DEPLOY_DIRECTORY/fleet_manifest.tsv}"

lm_validate_fleet_manifest() {
    [ -f "$LM_FLEET_MANIFEST" ] || {
        echo "fleet manifest is missing: $LM_FLEET_MANIFEST" >&2
        return 1
    }
    [ "$(sed -n '1p' "$LM_FLEET_MANIFEST")" = "# fleet-manifest-v2" ] || {
        echo "fleet manifest has an unsupported schema: $LM_FLEET_MANIFEST" >&2
        return 1
    }
    [ "$(sed -n '2p' "$LM_FLEET_MANIFEST")" = \
        "# unit|kind|realm|lifecycle|stop_order|activation|operator|depends_on|health|output_artifact|timer_service|first_delay_s|cadence_s|accuracy_s|runtime_s|input_artifact" ] || {
        echo "fleet manifest column contract is invalid: $LM_FLEET_MANIFEST" >&2
        return 1
    }
    LC_ALL=C awk -F '|' '
function fail_at(line, message) {
    print "invalid fleet manifest at " FILENAME ":" line ": " message > "/dev/stderr"
    failed = 1
}
function is_uint(value) { return value ~ /^[1-9][0-9]*$/ }
BEGIN { expected_fields = 16 }
/\r/ { fail_at(NR, "carriage returns are not allowed"); next }
/^#/ { next }
/^[[:space:]]*$/ { next }
{
    if (NF != expected_fields) {
        fail_at(NR, "expected 16 fields, found " NF)
        next
    }
    unit = $1
    if (unit !~ /^liquidity-migration-[A-Za-z0-9_.@-]+\.(service|timer)$/) {
        fail_at(NR, "invalid unit name " unit)
    }
    if (seen[unit]++) fail_at(NR, "duplicate unit " unit)
    names[unit] = 1
    row_line[unit] = NR
    kind[unit] = $2
    realm[unit] = $3
    phase[unit] = $4
    order[unit] = $5
    activation[unit] = $6
    operator[unit] = $7
    dependencies[unit] = $8
    health[unit] = $9
    artifact[unit] = $10
    timer_service[unit] = $11

    if ($2 !~ /^(service|timer)$/) fail_at(NR, "invalid kind for " unit)
    if (unit !~ ("\\." $2 "$") ) fail_at(NR, "kind disagrees with unit suffix for " unit)
    if ($3 !~ /^(demo|mainnet|shared)$/) fail_at(NR, "invalid realm for " unit)
    if ($4 !~ /^(downstream|owner|independent)$/) fail_at(NR, "invalid lifecycle phase for " unit)
    if (!is_uint($5)) fail_at(NR, "invalid stop order for " unit)
    order_key = $4 SUBSEP $5
    if (order_seen[order_key]++) fail_at(NR, "duplicate stop order in " $4 ": " $5)
    if ($6 !~ /^(always|mainnet|job|job-now)$/) fail_at(NR, "invalid activation for " unit)
    if ($7 !~ /^(direct|funded|none)$/) fail_at(NR, "invalid operator policy for " unit)
    if ($8 != "-" && $8 !~ /^liquidity-migration-[A-Za-z0-9_.@-]+\.(service|timer)(,liquidity-migration-[A-Za-z0-9_.@-]+\.(service|timer))*$/) {
        fail_at(NR, "invalid dependency list for " unit)
    }
    if ($9 !~ /^(active|timer|none)$/) {
        fail_at(NR, "invalid health policy for " unit)
    }
    if ($10 != "-" && $10 !~ /^\//) fail_at(NR, "output artifact must be an absolute path for " unit)
    if ($10 != "-" && output_artifact_seen[$10]++) {
        fail_at(NR, "duplicate output artifact " $10)
    }
    if ($16 != "-" && $16 !~ /^\//) fail_at(NR, "input artifact must be an absolute path for " unit)
    if ($16 != "-" && ($2 != "service" || $8 == "-")) {
        fail_at(NR, "input artifact requires a dependent service: " unit)
    }
    if (unit ~ /^liquidity-migration-signal-worker-(demo|mainnet)\.service$/) {
        expected_worker = "liquidity-migration-signal-worker-" $3 ".service"
        expected_activation = ($3 == "demo" ? "always" : "mainnet")
        if (unit != expected_worker || ($3 != "demo" && $3 != "mainnet") ||
            $2 != "service" || $4 != "downstream" || $6 != expected_activation ||
            $8 != "-" || $9 != "active" || $10 == "-" || $16 != "-") {
            fail_at(NR, "directional signal-worker policy is incomplete: " unit)
        }
        signal_worker_count[$3]++
    }
    if ($7 == "funded" && $3 != "mainnet") fail_at(NR, "funded operator policy requires mainnet realm: " unit)
    if ($6 == "mainnet" && $3 != "mainnet") fail_at(NR, "mainnet activation requires mainnet realm: " unit)
    if ($4 == "independent" && ($3 != "shared" || ($6 != "always" && $6 != "job") || $7 == "funded")) {
        fail_at(NR, "an independent unit is shared, always-on or timer-driven, and never funded: " unit)
    }

    if ($2 == "timer") {
        if ($4 == "owner" || $6 == "job" || $6 == "job-now" || $9 != "timer" || $10 != "-" || $11 == "-") {
            fail_at(NR, "timer policy is incomplete for " unit)
        }
        if (!is_uint($12) || !is_uint($13) || !is_uint($14) || !is_uint($15)) {
            fail_at(NR, "timer bounds must be positive integers for " unit)
        }
        referenced_jobs[$11]++
    } else {
        if ($11 != "-" || $12 != "-" || $13 != "-" || $14 != "-" || $15 != "-") {
            fail_at(NR, "service carries timer-only fields: " unit)
        }
        if ($6 == "job" || $6 == "job-now") {
            timer_jobs[unit] = 1
            if ($9 != "none") fail_at(NR, "timer job must be checked through its timer: " unit)
        } else if ($9 != "active") {
            fail_at(NR, "active service lacks an active health check: " unit)
        }
    }
    if ($4 == "owner") {
        if ($2 != "service" || ($3 != "demo" && $3 != "mainnet")) {
            fail_at(NR, "account owner must be a realm service: " unit)
        }
        owner_count[$3]++
    }
}
END {
    if (length(names) == 0) fail_at(1, "manifest has no units")
    for (unit in dependencies) {
        if (dependencies[unit] == "-") continue
        count = split(dependencies[unit], values, ",")
        for (dep_index = 1; dep_index <= count; dep_index++) {
            dependency = values[dep_index]
            if (!(dependency in names)) {
                fail_at(row_line[unit], unit " depends on unknown unit " dependency)
                continue
            }
            if (phase[unit] == "owner" && phase[dependency] == "downstream") {
                fail_at(row_line[unit], "owner depends on downstream unit: " unit)
            }
            if (phase[unit] == phase[dependency] && order[unit] >= order[dependency]) {
                fail_at(row_line[unit], unit " must stop before dependency " dependency)
            }
            if (phase[unit] == "independent" && phase[dependency] != "independent") {
                fail_at(row_line[unit], "independent unit depends on a fleet unit: " unit)
            }
        }
    }
    for (job in referenced_jobs) {
        if (!(job in names) || kind[job] != "service" ||
            (activation[job] != "job" && activation[job] != "job-now")) {
            fail_at(1, "timer references a non-job service " job)
        }
        if (referenced_jobs[job] != 1) fail_at(row_line[job], "job has more than one timer " job)
    }
    for (job in timer_jobs) {
        if (!(job in referenced_jobs)) fail_at(row_line[job], "job has no timer " job)
    }
    if (owner_count["demo"] != 1) fail_at(1, "manifest must have one demo owner")
    if (owner_count["mainnet"] != 1) fail_at(1, "manifest must have one mainnet owner")
    if (signal_worker_count["demo"] != 1) fail_at(1, "manifest must have one demo signal worker")
    if (signal_worker_count["mainnet"] != 1) fail_at(1, "manifest must have one mainnet signal worker")
    exit failed ? 1 : 0
}
' "$LM_FLEET_MANIFEST"
}

lm_fleet_manifest_rows() {
    LC_ALL=C awk -F '|' '!/^#/ && !/^[[:space:]]*$/ { print }' "$LM_FLEET_MANIFEST"
}

lm_realm_units() {
    _lru_realm="$1"
    case "$_lru_realm" in demo|mainnet|shared) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows \
        | awk -F '|' -v realm="$_lru_realm" '
            $3 == realm {
                phase = ($4 == "downstream" ? 0 : 1)
                print phase "|" $5 "|" $1
            }
        ' \
        | sort -t '|' -k1,1n -k2,2n \
        | cut -d '|' -f3
}

# The units a deploy never stops: they run through fleet restarts, funded
# stops, and disarms, and start again at boot. Highest stop order first, so a
# recorder starts before the timer that ships its output.
lm_independent_units() {
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows \
        | awk -F '|' '$4 == "independent" { print $5 "|" $1 }' \
        | sort -t '|' -k1,1nr \
        | cut -d '|' -f2
}

lm_activation_units() {
    _lau_realm="$1"
    _lau_direction="$2"
    case "$_lau_realm" in demo|mainnet) ;; *) return 2 ;; esac
    case "$_lau_direction" in
        start) _lau_sort=-k1,1nr ;;
        stop) _lau_sort=-k1,1n ;;
        *) return 2 ;;
    esac
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' \
        -v realm="$_lau_realm" '
        $4 != "downstream" || $10 != "-" { next }
        realm == "demo" &&
            (($6 == "always" && ($3 == "demo" || $3 == "shared")) ||
             ($6 == "job-now" && $3 == "demo")) {
            print $5 "|" $1
        }
        realm == "mainnet" &&
            ($6 == "mainnet" || $6 == "job-now") && $3 == "mainnet" {
            print $5 "|" $1
        }
    ' | sort -t '|' "$_lau_sort" | cut -d '|' -f2
}

lm_immediate_timer_jobs() {
    _litj_realm="$1"
    case "$_litj_realm" in demo|mainnet) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows \
        | awk -F '|' -v realm="$_litj_realm" '
            $2 == "service" && $3 == realm &&
                $4 == "downstream" && $6 == "job-now" { print $5 "|" $1 }
        ' \
        | sort -t '|' -k1,1nr \
        | cut -d '|' -f2
}

lm_owner_unit() {
    _lou_realm="$1"
    case "$_lou_realm" in demo|mainnet) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    _lou_unit="$(
        lm_fleet_manifest_rows | awk -F '|' -v realm="$_lou_realm" '
            $2 == "service" && $3 == realm && $4 == "owner" {
                print $1
            }
        '
    )"
    [ -n "$_lou_unit" ] || return 1
    printf '%s\n' "$_lou_unit"
}

lm_signal_worker_unit() {
    _lswu_realm="$1"
    case "$_lswu_realm" in demo|mainnet) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    _lswu_unit="$(
        lm_fleet_manifest_rows | awk -F '|' -v realm="$_lswu_realm" '
            $1 == "liquidity-migration-signal-worker-" realm ".service" &&
                $2 == "service" && $3 == realm &&
                $4 == "downstream" && $9 == "active" && $10 != "-" {
                print $1
            }
        '
    )"
    [ -n "$_lswu_unit" ] && [ "${_lswu_unit#*$'\n'}" = "$_lswu_unit" ] || return 1
    printf '%s\n' "$_lswu_unit"
}

lm_operator_status_rows() {
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' '
        $2 != "service" || ($3 != "demo" && $3 != "mainnet") ||
            ($4 != "owner" && $1 !~ /^liquidity-migration-signal-worker-(demo|mainnet)\.service$/) { next }
        {
            role = ($4 == "owner" ? "owner" : "signal")
            sleeve = (role == "signal" ? "directional" : "-")
            realm_order = ($3 == "demo" ? 0 : 1)
            role_order = (role == "owner" ? 0 : 1)
            print realm_order "|" role_order "|" sleeve "|" \
                $1 "|" $3 "|" role "|" sleeve
        }
    ' | sort -t '|' -k1,1n -k2,2n -k3,3 -k4,4 | cut -d '|' -f4-
}

lm_output_artifact_for_unit() {
    _loafu_unit="$1"
    lm_validate_fleet_manifest || return 1
    _loafu_artifact="$(
        lm_fleet_manifest_rows | awk -F '|' -v unit="$_loafu_unit" '
            $1 == unit && $10 != "-" { print $10 }
        '
    )"
    [ -n "$_loafu_artifact" ] || return 1
    printf '%s\n' "$_loafu_artifact"
}

lm_parse_sleeve_environment() {
    _lpe_file="$1"
    _LM_PARSED_LONG_PRESENT=0
    _LM_PARSED_CARRY_PRESENT=0
    _LM_PARSED_LONG=""
    _LM_PARSED_CARRY=""
    _lpe_line_number=0
    while IFS= read -r _lpe_line || [ -n "$_lpe_line" ]; do
        _lpe_line_number=$((_lpe_line_number + 1))
        case "$_lpe_line" in
            ""|\#*) continue ;;
            *=*)
                _lpe_key="${_lpe_line%%=*}"
                _lpe_value="${_lpe_line#*=}"
                ;;
            *)
                echo "invalid sleeve environment assignment at $_lpe_file:$_lpe_line_number" >&2
                return 1
                ;;
        esac
        case "$_lpe_value" in
            ""|on|ON|On|1|true|TRUE|yes|YES|off|OFF|Off|0|false|FALSE|no|NO)
                ;;
            *)
                echo "invalid sleeve toggle value at $_lpe_file:$_lpe_line_number" >&2
                return 1
                ;;
        esac
        case "$_lpe_key" in
            LONG_SLEEVE)
                [ "$_LM_PARSED_LONG_PRESENT" -eq 0 ] || {
                    echo "duplicate LONG_SLEEVE at $_lpe_file:$_lpe_line_number" >&2
                    return 1
                }
                _LM_PARSED_LONG_PRESENT=1
                _LM_PARSED_LONG="$_lpe_value"
                ;;
            CARRY_SLEEVE)
                [ "$_LM_PARSED_CARRY_PRESENT" -eq 0 ] || {
                    echo "duplicate CARRY_SLEEVE at $_lpe_file:$_lpe_line_number" >&2
                    return 1
                }
                _LM_PARSED_CARRY_PRESENT=1
                _LM_PARSED_CARRY="$_lpe_value"
                ;;
            *)
                echo "unknown sleeve toggle at $_lpe_file:$_lpe_line_number" >&2
                return 1
                ;;
        esac
    done < "$_lpe_file"
}

# Load the toggles as strict data: committed defaults, then an optional per-host
# override that may only narrow (repo-on -> off, never repo-off -> on). The repo
# dir comes from this file's location, so the caller's CWD is irrelevant.
lm_load_sleeve_toggles() {
    _lm_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    unset LONG_SLEEVE CARRY_SLEEVE 2>/dev/null || true
    if [ -f "$_lm_dir/sleeves.env" ]; then
        lm_parse_sleeve_environment "$_lm_dir/sleeves.env"
        [ "$_LM_PARSED_LONG_PRESENT" -eq 0 ] || LONG_SLEEVE="$_LM_PARSED_LONG"
        [ "$_LM_PARSED_CARRY_PRESENT" -eq 0 ] || CARRY_SLEEVE="$_LM_PARSED_CARRY"
    fi
    _lm_repo_long="${LONG_SLEEVE:-off}"
    _lm_repo_carry="${CARRY_SLEEVE:-off}"
    if [ -f "$LM_HOST_SLEEVES_ENV" ]; then
        lm_parse_sleeve_environment "$LM_HOST_SLEEVES_ENV"
        [ "$_LM_PARSED_LONG_PRESENT" -eq 0 ] || LONG_SLEEVE="$_LM_PARSED_LONG"
        [ "$_LM_PARSED_CARRY_PRESENT" -eq 0 ] || CARRY_SLEEVE="$_LM_PARSED_CARRY"
    fi
    if ! sleeve_on "$_lm_repo_long"; then LONG_SLEEVE=off; fi
    if ! sleeve_on "$_lm_repo_carry"; then CARRY_SLEEVE=off; fi
    # Missing toggles fail safe to off; a missing config cannot resurrect a sleeve.
    : "${LONG_SLEEVE:=off}"
    : "${CARRY_SLEEVE:=off}"
}

# sleeve_on <value> -> 0 (true) if the toggle means "run this sleeve".
# Empty or unset is off.
sleeve_on() {
    case "${1:-off}" in
        on|ON|On|1|true|TRUE|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

lm_write_resolved_sleeve_toggles() {
    _lr_dir="$(dirname "$LM_RESOLVED_SLEEVES_ENV")"
    mkdir -p "$_lr_dir"
    _lr_tmp="$(mktemp "$LM_RESOLVED_SLEEVES_ENV.tmp.XXXXXX")"
    {
        echo "# Generated by deploy/lib_sleeves.sh. Do not edit by hand."
        echo "# Host overrides may only turn repo-on sleeves off; repo-off is a hard ceiling."
        printf 'LONG_SLEEVE=%s\n' "${LONG_SLEEVE:-off}"
        printf 'CARRY_SLEEVE=%s\n' "${CARRY_SLEEVE:-off}"
    } > "$_lr_tmp"
    chmod 0600 "$_lr_tmp"
    mv "$_lr_tmp" "$LM_RESOLVED_SLEEVES_ENV"
}

lm_expected_systemd_units() {
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' '{ print $1 }'
}

lm_host_liqmig_units() {
    {
        systemctl list-unit-files 'liquidity-migration-*' --no-legend --no-pager 2>/dev/null || true
        systemctl list-units 'liquidity-migration-*' --all --no-legend --no-pager --plain 2>/dev/null || true
        for _lhlu_path in \
            "$LM_SYSTEMD_UNIT_DIR"/liquidity-migration-*.service \
            "$LM_SYSTEMD_UNIT_DIR"/liquidity-migration-*.timer \
            "$LM_RUNTIME_SYSTEMD_UNIT_DIR"/liquidity-migration-*.service \
            "$LM_RUNTIME_SYSTEMD_UNIT_DIR"/liquidity-migration-*.timer; do
            [ -e "$_lhlu_path" ] && basename "$_lhlu_path"
        done
        # A unit file may already be gone while an operator/runtime drop-in
        # survives. Surface its owning unit name so cleanup removes the orphaned
        # override too; manifest-unit drop-ins remain intact.
        for _lhlu_dropin in \
            "$LM_SYSTEMD_UNIT_DIR"/liquidity-migration-*.service.d \
            "$LM_SYSTEMD_UNIT_DIR"/liquidity-migration-*.timer.d \
            "$LM_RUNTIME_SYSTEMD_UNIT_DIR"/liquidity-migration-*.service.d \
            "$LM_RUNTIME_SYSTEMD_UNIT_DIR"/liquidity-migration-*.timer.d; do
            [ -d "$_lhlu_dropin" ] && basename "$_lhlu_dropin" .d
        done
        # Broken enablement symlinks can outlive both the unit file and
        # systemd's list-unit-files output. Inventory them explicitly so an old
        # wants/requires link cannot resurrect after a later file reappears.
        for _lhlu_root in "$LM_SYSTEMD_UNIT_DIR" "$LM_RUNTIME_SYSTEMD_UNIT_DIR"; do
            [ -d "$_lhlu_root" ] || continue
            find "$_lhlu_root" -type l \
                \( -name 'liquidity-migration-*.service' -o -name 'liquidity-migration-*.timer' \) \
                -print 2>/dev/null
        done | while IFS= read -r _lhlu_link; do basename "$_lhlu_link"; done
    } | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^liquidity-migration-.*\.(service|timer)$/) {print $i; break}}' | sed '/^$/d' | sort -u
}

lm_cleanup_unknown_liqmig_units() {
    _lcu_expected=" $(lm_expected_systemd_units | tr '\n' ' ') "
    for _lcu_unit in $(lm_host_liqmig_units); do
        case "$_lcu_expected" in
            *" $_lcu_unit "*) continue ;;
        esac
        echo "cleanup: unknown liquidity-migration unit -> disable/remove $_lcu_unit" >&2
        systemctl disable --now "$_lcu_unit" 2>/dev/null || true
        for _lcu_root in "$LM_SYSTEMD_UNIT_DIR" "$LM_RUNTIME_SYSTEMD_UNIT_DIR"; do
            rm -f "$_lcu_root/$_lcu_unit"
            if [ -d "$_lcu_root" ]; then
                find "$_lcu_root" -type l -name "$_lcu_unit" -delete 2>/dev/null || true
            fi
            _lcu_dropin_dir="$_lcu_root/$_lcu_unit.d"
            if [ -d "$_lcu_dropin_dir" ]; then
                find "$_lcu_dropin_dir" -mindepth 1 -delete
                rmdir "$_lcu_dropin_dir" 2>/dev/null || true
            fi
        done
        systemctl reset-failed "$_lcu_unit" 2>/dev/null || true
    done
}

# Install exactly the checked-in unit manifest without enabling or starting any
# service/timer. Units absent from the manifest are stopped and removed.
lm_install_current_systemd_units() {
    _licsu_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    lm_validate_fleet_manifest || return 1
    mkdir -p "$LM_SYSTEMD_UNIT_DIR"
    for _licsu_unit in $(lm_expected_systemd_units); do
        cp "$_licsu_dir/systemd/$_licsu_unit" "$LM_SYSTEMD_UNIT_DIR/$_licsu_unit" || return 1
    done
    systemctl daemon-reload
    lm_cleanup_unknown_liqmig_units
    systemctl daemon-reload
}
