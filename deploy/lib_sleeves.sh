# Shared sleeve, systemd-manifest, and topology helpers: one place for the
# sleeve->units mapping and the on/off predicate, so deploy and verify cannot
# drift. Toggles come from deploy/sleeves.env plus a host override that can only
# narrow a repo-on sleeve to off. bash-3.2-safe (no associative arrays).

LM_HOST_SLEEVES_ENV="${LM_HOST_SLEEVES_ENV:-/etc/liquidity-migration/sleeves.env}"
LM_RESOLVED_SLEEVES_ENV="${LM_RESOLVED_SLEEVES_ENV:-/etc/liquidity-migration/sleeves.resolved.env}"
LM_SYSTEMD_UNIT_DIR="${LM_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
LM_RUNTIME_SYSTEMD_UNIT_DIR="${LM_RUNTIME_SYSTEMD_UNIT_DIR:-/run/systemd/system}"

# These units keep their whole workload argv in run_authorized_runtime.sh, so a
# drop-in or alternate fragment cannot replace it after the commit is reviewed.
LM_AUTHORIZED_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-mainnet.service liquidity-migration-bybit-carry-demo.service liquidity-migration-bybit-carry-mainnet.service liquidity-migration-demo-liveness.service liquidity-migration-mainnet-liveness.service liquidity-migration-telegram-controls.service liquidity-migration-llm-ledger.service"

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
                # Paper trading was retired 2026-08-03; the mainnet sleeve
                # toggles were retired the same day when REAL_MONEY in
                # /etc/liquidity-migration/bybit-mainnet.env became the single
                # arming switch; the continuous sleeve's units left the deploy
                # set on 2026-08-03 too (sleeve retired 2026-07-29). A stale
                # host override may still carry these keys; they toggle
                # nothing and must not brick the deploy.
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
    _lesu_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    for _lesu_path in "$_lesu_dir"/systemd/liquidity-migration-*.service "$_lesu_dir"/systemd/liquidity-migration-*.timer; do
        [ -e "$_lesu_path" ] && basename "$_lesu_path"
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
    for _lvgus_unit in $LM_AUTHORIZED_UNITS; do
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
            *"argv[]=/opt/liquidity-migration/scripts/run_authorized_runtime.sh $_lvgus_unit main ;"*) ;;
            *)
                echo "verify failed: guarded unit has unexpected effective ExecStart: $_lvgus_unit -> $_lvgus_exec" >&2
                return 1
                ;;
        esac
        # No unit keeps an ExecStartPost readiness gate any more. The only one
        # that ever did was the mainnet account owner, and it is gone.
    done
}

# Install exactly the checked-in unit manifest without enabling or starting any
# service/timer. Unknown historical units are stopped and removed so a retired
# order mutator cannot survive alongside the single-owner topology.
lm_install_current_systemd_units() {
    _licsu_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ ! -f "$_licsu_dir/systemd/liquidity-migration-engine.service" ]; then
        echo "install failed: required account owner unit is absent from manifest: liquidity-migration-engine.service" >&2
        return 1
    fi
    mkdir -p "$LM_SYSTEMD_UNIT_DIR"
    for _licsu_path in \
        "$_licsu_dir"/systemd/liquidity-migration-*.service \
        "$_licsu_dir"/systemd/liquidity-migration-*.timer; do
        [ -e "$_licsu_path" ] || continue
        cp "$_licsu_path" "$LM_SYSTEMD_UNIT_DIR/$(basename "$_licsu_path")"
    done

    systemctl daemon-reload
    lm_cleanup_unknown_liqmig_units
    systemctl daemon-reload
    lm_verify_no_unknown_liqmig_units
    lm_verify_guarded_unit_surfaces
}
