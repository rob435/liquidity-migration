# Shared sleeve, systemd-manifest, and topology helpers: one place for the
# sleeve->units mapping and the on/off predicate, so deploy and verify cannot
# drift. Toggles come from deploy/sleeves.env plus a host override that can only
# narrow a repo-on sleeve to off. bash-3.2-safe (no associative arrays).

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
    [ "$(sed -n '1p' "$LM_FLEET_MANIFEST")" = "# fleet-manifest-v1" ] || {
        echo "fleet manifest has an unsupported schema: $LM_FLEET_MANIFEST" >&2
        return 1
    }
    [ "$(sed -n '2p' "$LM_FLEET_MANIFEST")" = \
        "# unit|state|kind|realm|lifecycle|stop_order|activation|operator|depends_on|health|output_artifact|timer_service|first_delay_s|cadence_s|accuracy_s|runtime_s|input_artifact" ] || {
        echo "fleet manifest column contract is invalid: $LM_FLEET_MANIFEST" >&2
        return 1
    }
    LC_ALL=C awk -F '|' '
function fail_at(line, message) {
    print "invalid fleet manifest at " FILENAME ":" line ": " message > "/dev/stderr"
    failed = 1
}
function is_uint(value) { return value ~ /^[1-9][0-9]*$/ }
BEGIN { expected_fields = 17 }
/\r/ { fail_at(NR, "carriage returns are not allowed"); next }
/^#/ { next }
/^[[:space:]]*$/ { next }
{
    if (NF != expected_fields) {
        fail_at(NR, "expected 17 fields, found " NF)
        next
    }
    unit = $1
    if (unit !~ /^liquidity-migration-[A-Za-z0-9_.@-]+\.(service|timer)$/) {
        fail_at(NR, "invalid unit name " unit)
    }
    if (seen[unit]++) fail_at(NR, "duplicate unit " unit)
    names[unit] = 1
    row_line[unit] = NR
    state[unit] = $2
    kind[unit] = $3
    realm[unit] = $4
    phase[unit] = $5
    order[unit] = $6
    activation[unit] = $7
    operator[unit] = $8
    dependencies[unit] = $9
    health[unit] = $10
    artifact[unit] = $11
    timer_service[unit] = $12

    if ($2 !~ /^(current|retired)$/) fail_at(NR, "invalid state for " unit)
    if ($3 !~ /^(service|timer)$/) fail_at(NR, "invalid kind for " unit)
    if (unit !~ ("\\." $3 "$") ) fail_at(NR, "kind disagrees with unit suffix for " unit)
    if ($4 !~ /^(demo|mainnet|shared)$/) fail_at(NR, "invalid realm for " unit)
    if ($5 !~ /^(downstream|owner)$/) fail_at(NR, "invalid lifecycle phase for " unit)
    if (!is_uint($6)) fail_at(NR, "invalid stop order for " unit)
    order_key = $5 SUBSEP $6
    if (order_seen[order_key]++) fail_at(NR, "duplicate stop order in " $5 ": " $6)
    if ($7 !~ /^(always|mainnet|long|carry|job|job-now|never)$/) fail_at(NR, "invalid activation for " unit)
    if ($8 !~ /^(direct|funded|none)$/) fail_at(NR, "invalid operator policy for " unit)
    if ($9 != "-" && $9 !~ /^liquidity-migration-[A-Za-z0-9_.@-]+\.(service|timer)(,liquidity-migration-[A-Za-z0-9_.@-]+\.(service|timer))*$/) {
        fail_at(NR, "invalid dependency list for " unit)
    }
    if ($10 !~ /^(active|timer|none|cycle:(long|carry|exodus):data\/bybit-[a-z-]+-event:[a-z0-9_]+)$/) {
        fail_at(NR, "invalid health policy for " unit)
    }
    if ($11 != "-" && $11 !~ /^\//) fail_at(NR, "output artifact must be an absolute path for " unit)
    if ($11 != "-" && output_artifact_seen[$11]++) {
        fail_at(NR, "duplicate output artifact " $11)
    }
    if ($17 != "-" && $17 !~ /^\//) fail_at(NR, "input artifact must be an absolute path for " unit)
    if ($17 != "-" && ($3 != "service" || $9 == "-")) {
        fail_at(NR, "input artifact requires a dependent service: " unit)
    }

    if ($2 == "retired") {
        if ($5 != "downstream" || $7 != "never" || $8 != "none" || $9 != "-" ||
            $10 != "none" || $11 != "-" || $12 != "-" || $13 != "-" ||
            $14 != "-" || $15 != "-" || $16 != "-" || $17 != "-") {
            fail_at(NR, "retired unit carries live policy: " unit)
        }
        next
    }

    current[unit] = 1
    if ($7 == "never") fail_at(NR, "current unit cannot use never activation: " unit)
    if ($8 == "funded" && $4 != "mainnet") fail_at(NR, "funded operator policy requires mainnet realm: " unit)
    if ($7 == "mainnet" && $4 != "mainnet") fail_at(NR, "mainnet activation requires mainnet realm: " unit)
    if (($7 == "long" || $7 == "carry") && $4 != "demo") fail_at(NR, "sleeve activation requires demo realm: " unit)

    if ($3 == "timer") {
        if ($5 != "downstream" || $7 == "job" || $7 == "job-now" || $10 != "timer" || $11 != "-" || $12 == "-") {
            fail_at(NR, "timer policy is incomplete for " unit)
        }
        if (!is_uint($13) || !is_uint($14) || !is_uint($15) || !is_uint($16)) {
            fail_at(NR, "timer bounds must be positive integers for " unit)
        }
        referenced_jobs[$12]++
    } else {
        if ($12 != "-" || $13 != "-" || $14 != "-" || $15 != "-" || $16 != "-") {
            fail_at(NR, "service carries timer-only fields: " unit)
        }
        if ($7 == "job" || $7 == "job-now") {
            timer_jobs[unit] = 1
            if ($10 != "none") fail_at(NR, "timer job must be checked through its timer: " unit)
        } else if ($10 != "active" && $10 !~ /^cycle:/) {
            fail_at(NR, "active service lacks an active health check: " unit)
        }
        if ($10 ~ /^cycle:/) {
            health_count = split($10, health_parts, ":")
            health_sleeve = health_parts[2]
            health_root = health_parts[3]
            health_dataset = health_parts[4]
            expected_health_root = "data/bybit-" health_sleeve "-" $4 "-event"
            if (health_count != 4 || unit != "liquidity-migration-bybit-" health_sleeve "-" $4 ".service") {
                fail_at(NR, "cycle health sleeve disagrees with unit identity: " unit)
            }
            if (health_root != expected_health_root) {
                fail_at(NR, "cycle health root disagrees with unit identity: " unit)
            }
            if (health_dataset == "") fail_at(NR, "cycle health dataset is empty: " unit)
            if ($11 == "-") fail_at(NR, "cycle health service lacks an output artifact: " unit)
        }
    }
    if ($5 == "owner") {
        if ($3 != "service" || ($4 != "demo" && $4 != "mainnet")) {
            fail_at(NR, "account owner must be a realm service: " unit)
        }
        owner_count[$4]++
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
            if (state[unit] == "current" && !(dependency in current)) {
                fail_at(row_line[unit], unit " depends on retired unit " dependency)
            }
            if (phase[unit] == "owner" && phase[dependency] == "downstream") {
                fail_at(row_line[unit], "owner depends on downstream unit: " unit)
            }
            if (phase[unit] == phase[dependency] && order[unit] >= order[dependency]) {
                fail_at(row_line[unit], unit " must stop before dependency " dependency)
            }
        }
    }
    for (job in referenced_jobs) {
        if (!(job in current) || kind[job] != "service" ||
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
    exit failed ? 1 : 0
}
' "$LM_FLEET_MANIFEST"
}

lm_fleet_manifest_rows() {
    LC_ALL=C awk -F '|' '!/^#/ && !/^[[:space:]]*$/ { print }' "$LM_FLEET_MANIFEST"
}

lm_rollout_units() {
    _lru_phase="$1"
    case "$_lru_phase" in downstream|owner) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows \
        | awk -F '|' -v phase="$_lru_phase" '$5 == phase { print $6 "|" $1 }' \
        | sort -t '|' -k1,1n \
        | cut -d '|' -f2
}

lm_realm_units() {
    _lru_realm="$1"
    case "$_lru_realm" in demo|mainnet|shared) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows \
        | awk -F '|' -v realm="$_lru_realm" '
            $2 == "current" && $4 == realm {
                phase = ($5 == "downstream" ? 0 : 1)
                print phase "|" $6 "|" $1
            }
        ' \
        | sort -t '|' -k1,1n -k2,2n \
        | cut -d '|' -f3
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
        $2 != "current" || $5 != "downstream" || $11 != "-" { next }
        realm == "demo" &&
            (($7 == "always" && ($4 == "demo" || $4 == "shared")) ||
             ($7 == "job-now" && $4 == "demo")) {
            print $6 "|" $1
        }
        realm == "mainnet" &&
            ($7 == "mainnet" || $7 == "job-now") && $4 == "mainnet" {
            print $6 "|" $1
        }
    ' | sort -t '|' "$_lau_sort" | cut -d '|' -f2
}

lm_immediate_timer_jobs() {
    _litj_realm="$1"
    case "$_litj_realm" in demo|mainnet) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows \
        | awk -F '|' -v realm="$_litj_realm" '
            $2 == "current" && $3 == "service" && $4 == realm &&
                $5 == "downstream" && $7 == "job-now" { print $6 "|" $1 }
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
            $2 == "current" && $3 == "service" && $4 == realm && $5 == "owner" {
                print $1
            }
        '
    )"
    [ -n "$_lou_unit" ] || return 1
    printf '%s\n' "$_lou_unit"
}

lm_target_producer_units() {
    _ltpu_realm="$1"
    _ltpu_direction="$2"
    _ltpu_long="$3"
    _ltpu_carry="$4"
    _ltpu_mainnet="$5"
    case "$_ltpu_realm" in demo|mainnet) ;; *) return 2 ;; esac
    case "$_ltpu_direction" in
        start) _ltpu_sort=-k1,1nr ;;
        stop) _ltpu_sort=-k1,1n ;;
        *) return 2 ;;
    esac
    for _ltpu_value in "$_ltpu_long" "$_ltpu_carry" "$_ltpu_mainnet"; do
        case "$_ltpu_value" in on|off) ;; *) return 2 ;; esac
    done
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' \
        -v realm="$_ltpu_realm" -v long="$_ltpu_long" \
        -v carry="$_ltpu_carry" -v mainnet="$_ltpu_mainnet" '
        $2 != "current" || $3 != "service" || $4 != realm ||
            $5 != "downstream" || ($10 != "active" && $10 !~ /^cycle:/) ||
            $11 == "-" { next }
        {
            expected = "off"
            if ($7 == "always") expected = "on"
            else if ($7 == "long") expected = long
            else if ($7 == "carry") expected = carry
            else if ($7 == "mainnet") expected = mainnet
            if (expected == "on") print $6 "|" $1
        }
    ' | sort -t '|' "$_ltpu_sort" | cut -d '|' -f2
}

lm_operator_status_rows() {
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' '
        $2 != "current" || $3 != "service" || ($4 != "demo" && $4 != "mainnet") ||
            ($5 != "owner" && ($10 !~ /^cycle:/ || $11 == "-")) { next }
        {
            role = ($5 == "owner" ? "owner" : "producer")
            sleeve = "-"
            if (role == "producer") {
                split($10, health_parts, ":")
                sleeve = health_parts[2]
            }
            realm_order = ($4 == "demo" ? 0 : 1)
            role_order = (role == "owner" ? 0 : 1)
            print realm_order "|" role_order "|" sleeve "|" \
                $1 "|" $4 "|" role "|" sleeve
        }
    ' | sort -t '|' -k1,1n -k2,2n -k3,3 -k4,4 | cut -d '|' -f4-
}

lm_unit_for_output_artifact() {
    _lufoa_artifact="$1"
    case "$_lufoa_artifact" in /*) ;; *) return 2 ;; esac
    lm_validate_fleet_manifest || return 1
    _lufoa_unit="$(
        lm_fleet_manifest_rows | awk -F '|' -v artifact="$_lufoa_artifact" '
            $2 == "current" && $11 == artifact { print $1 }
        '
    )"
    [ -n "$_lufoa_unit" ] && [ "${_lufoa_unit#*$'\n'}" = "$_lufoa_unit" ] || return 1
    printf '%s\n' "$_lufoa_unit"
}

lm_output_artifact_for_unit() {
    _loafu_unit="$1"
    lm_validate_fleet_manifest || return 1
    _loafu_artifact="$(
        lm_fleet_manifest_rows | awk -F '|' -v unit="$_loafu_unit" '
            $1 == unit && $2 == "current" && $11 != "-" { print $11 }
        '
    )"
    [ -n "$_loafu_artifact" ] || return 1
    printf '%s\n' "$_loafu_artifact"
}

lm_guarded_units() {
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' '$2 == "current" && $3 == "service" { print $1 }'
}

lm_manifest_operator_policy() {
    _lmop_unit="$1"
    lm_validate_fleet_manifest || return 1
    _lmop_policy="$(
        lm_fleet_manifest_rows | awk -F '|' -v unit="$_lmop_unit" '$1 == unit && $2 == "current" { print $8 }'
    )"
    [ -n "$_lmop_policy" ] || return 1
    printf '%s\n' "$_lmop_policy"
}

lm_fleet_health_rows() {
    _lfhr_long="$1"
    _lfhr_carry="$2"
    _lfhr_mainnet="$3"
    for _lfhr_value in "$_lfhr_long" "$_lfhr_carry" "$_lfhr_mainnet"; do
        case "$_lfhr_value" in on|off) ;; *) return 2 ;; esac
    done
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' \
        -v long="$_lfhr_long" -v carry="$_lfhr_carry" -v mainnet="$_lfhr_mainnet" '
        $2 != "current" || $10 == "none" { next }
        {
            expected = "off"
            if ($7 == "always") expected = "on"
            else if ($7 == "long") expected = long
            else if ($7 == "carry") expected = carry
            else if ($7 == "mainnet") expected = mainnet
            print $1 "|" expected "|" $10 "|" $11 "|" $12 "|" $13 "|" $14 "|" $15 "|" $16
        }
    '
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
            CONTINUOUS_SLEEVE|CONTINUOUS_HEDGE_TIMER|CONTINUOUS_PAPER_SLEEVE|CARRY_PAPER_SLEEVE|PAPER_TARGET_MIRROR|CARRY_MAINNET_SLEEVE|LONG_MAINNET_SLEEVE)
                # These toggles are retired and control nothing. A stale host
                # override may still carry them, and must not brick the deploy.
                echo "retired sleeve toggle ignored at $_lpe_file:$_lpe_line_number: $_lpe_key" >&2
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

lm_verify_resolved_sleeve_toggles() {
    [ -f "$LM_RESOLVED_SLEEVES_ENV" ] || {
        echo "verify failed: missing resolved sleeve env $LM_RESOLVED_SLEEVES_ENV" >&2
        return 1
    }
    grep -Fx "LONG_SLEEVE=${LONG_SLEEVE:-off}" "$LM_RESOLVED_SLEEVES_ENV" >/dev/null || {
        echo "verify failed: resolved LONG_SLEEVE does not match loaded toggle" >&2
        return 1
    }
    grep -Fx "CARRY_SLEEVE=${CARRY_SLEEVE:-off}" "$LM_RESOLVED_SLEEVES_ENV" >/dev/null || {
        echo "verify failed: resolved CARRY_SLEEVE does not match loaded toggle" >&2
        return 1
    }
}

lm_expected_systemd_units() {
    lm_validate_fleet_manifest || return 1
    lm_fleet_manifest_rows | awk -F '|' '$2 == "current" { print $1 }'
}

lm_verify_source_systemd_manifest() {
    _lvssm_expected=" $(lm_expected_systemd_units | tr '\n' ' ') " || return 1
    for _lvssm_unit in $(lm_expected_systemd_units); do
        [ -f "$_LM_DEPLOY_DIRECTORY/systemd/$_lvssm_unit" ] || {
            echo "fleet manifest names a missing systemd unit: $_lvssm_unit" >&2
            return 1
        }
    done
    for _lvssm_path in \
        "$_LM_DEPLOY_DIRECTORY"/systemd/liquidity-migration-*.service \
        "$_LM_DEPLOY_DIRECTORY"/systemd/liquidity-migration-*.timer; do
        [ -e "$_lvssm_path" ] || continue
        _lvssm_unit="$(basename "$_lvssm_path")"
        case "$_lvssm_expected" in
            *" $_lvssm_unit "*) ;;
            *)
                echo "systemd unit is absent from the fleet manifest: $_lvssm_unit" >&2
                return 1
                ;;
        esac
    done
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
        # A retired unit file may already be gone while an operator/runtime
        # drop-in survives. Surface its owning unit name so cleanup removes the
        # orphaned override too; expected current-unit drop-ins remain intact.
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

lm_verify_no_unknown_liqmig_units() {
    _lvnu_expected=" $(lm_expected_systemd_units | tr '\n' ' ') "
    for _lvnu_unit in $(lm_host_liqmig_units); do
        case "$_lvnu_expected" in
            *" $_lvnu_unit "*) continue ;;
        esac
        echo "verify failed: unknown liquidity-migration unit present: $_lvnu_unit" >&2
        return 1
    done
}

# Fail closed unless systemd's effective guarded-unit surface is exactly the
# checked manifest. A current-unit drop-in is never deleted here — it may be
# operator work; deployment stops and names the conflicting path instead.
lm_verify_guarded_unit_surfaces() {
    _lvgus_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    lm_validate_fleet_manifest || return 1
    for _lvgus_unit in $(lm_guarded_units); do
        _lvgus_source="$_lvgus_dir/systemd/$_lvgus_unit"
        _lvgus_installed="$LM_SYSTEMD_UNIT_DIR/$_lvgus_unit"
        if [ ! -f "$_lvgus_source" ] || [ ! -f "$_lvgus_installed" ]; then
            echo "verify failed: guarded unit fragment is missing: $_lvgus_unit" >&2
            return 1
        fi
        if ! cmp -s "$_lvgus_source" "$_lvgus_installed"; then
            echo "verify failed: guarded unit differs from checked manifest: $_lvgus_installed" >&2
            return 1
        fi

        for _lvgus_root in "$LM_SYSTEMD_UNIT_DIR" "$LM_RUNTIME_SYSTEMD_UNIT_DIR"; do
            _lvgus_dropin_dir="$_lvgus_root/$_lvgus_unit.d"
            if [ -d "$_lvgus_dropin_dir" ] \
                && [ -n "$(find "$_lvgus_dropin_dir" -mindepth 1 -print -quit)" ]; then
                echo "verify failed: guarded unit has an unreviewed drop-in: $_lvgus_dropin_dir" >&2
                return 1
            fi
        done

        _lvgus_fragment="$(systemctl show "$_lvgus_unit" --property=FragmentPath --value --no-pager)" || return 1
        if [ "$_lvgus_fragment" != "$_lvgus_installed" ]; then
            echo "verify failed: guarded unit loaded from unexpected fragment: $_lvgus_unit -> $_lvgus_fragment" >&2
            return 1
        fi
        _lvgus_dropins="$(systemctl show "$_lvgus_unit" --property=DropInPaths --value --no-pager)" || return 1
        if [ -n "$_lvgus_dropins" ]; then
            echo "verify failed: guarded unit has effective drop-ins: $_lvgus_unit -> $_lvgus_dropins" >&2
            return 1
        fi
        _lvgus_exec="$(systemctl show "$_lvgus_unit" --property=ExecStart --value --no-pager)" || return 1
        case "$_lvgus_exec" in
            *"argv[]=/opt/liquidity-migration-engine/bin/run-authorized-runtime $_lvgus_unit main ;"*) ;;
            *)
                echo "verify failed: guarded unit has unexpected effective ExecStart: $_lvgus_unit -> $_lvgus_exec" >&2
                return 1
                ;;
        esac
    done
}

# Install exactly the checked-in unit manifest without enabling or starting any
# service/timer. Unknown historical units are stopped and removed so a retired
# order mutator cannot survive alongside the single-owner topology.
lm_install_current_systemd_units() {
    _licsu_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    lm_verify_source_systemd_manifest || return 1
    mkdir -p "$LM_SYSTEMD_UNIT_DIR"
    for _licsu_unit in $(lm_expected_systemd_units); do
        cp "$_licsu_dir/systemd/$_licsu_unit" "$LM_SYSTEMD_UNIT_DIR/$_licsu_unit"
    done

    systemctl daemon-reload
    lm_cleanup_unknown_liqmig_units
    systemctl daemon-reload
    lm_verify_no_unknown_liqmig_units
    lm_verify_guarded_unit_surfaces
}
