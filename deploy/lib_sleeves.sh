# Shared per-sleeve kill-switch helpers — sourced by deploy_vps_live.sh + verify_vps_live.sh
# so the sleeve->units mapping and the on/off predicate live in ONE place (no drift between
# deploy and verify). Toggles come from deploy/sleeves.env (+ host override). bash-3.2-safe
# (no associative arrays). See deploy/sleeves.env for semantics.

# Space-separated unit lists per sleeve (entry/exit daemons + paper shadow). The risk service
# is intentionally NOT here — it always runs and protects every sleeve's open positions.
SHORT_SLEEVE_UNITS="liquidity-migration-bybit-demo.service liquidity-migration-bybit-paper.service"
LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"
CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service liquidity-migration-bybit-continuous-paper.service"
# Timer the continuous sleeve owns (the daily rmom-gate refresh). Toggled with the sleeve.
CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"

# Load the toggles: committed defaults first, then an optional per-host override. Resolves the
# repo dir from this file's location so it works regardless of the caller's CWD.
lm_load_sleeve_toggles() {
    _lm_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$_lm_dir/sleeves.env" ] && . "$_lm_dir/sleeves.env"
    [ -f /etc/liquidity-migration/sleeves.env ] && . /etc/liquidity-migration/sleeves.env
    : "${SHORT_SLEEVE:=on}"
    : "${LONG_SLEEVE:=on}"
    : "${CONTINUOUS_SLEEVE:=on}"
}

# sleeve_on <value> -> 0 (true) if the toggle means "run this sleeve".
sleeve_on() {
    case "${1:-on}" in
        on|ON|On|1|true|TRUE|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# apply_sleeve_enable <flag-value> <unit...> — on: `systemctl enable` each unit; off:
# `systemctl disable --now` each (stops it + survives the deploy). Default on => identical
# to the previous unconditional enables.
apply_sleeve_enable() {
    _ase_flag="$1"; shift
    if sleeve_on "$_ase_flag"; then
        for _ase_u in "$@"; do systemctl enable "$_ase_u"; done
    else
        echo "kill-switch: sleeve OFF -> disable --now: $*" >&2
        for _ase_u in "$@"; do systemctl disable --now "$_ase_u" 2>/dev/null || true; done
    fi
}

# verify_sleeve <flag-value> <unit...> — on: each unit must be active AND enabled; off: each
# unit must NOT be active (the kill-switch actually stopped it). Returns 1 (fail-loud) on mismatch.
verify_sleeve() {
    _vs_flag="$1"; shift
    if sleeve_on "$_vs_flag"; then
        for _vs_u in "$@"; do
            systemctl is-active --quiet "$_vs_u" || { echo "verify failed: $_vs_u not active" >&2; return 1; }
            systemctl is-enabled --quiet "$_vs_u" || { echo "verify failed: $_vs_u not enabled" >&2; return 1; }
        done
    else
        for _vs_u in "$@"; do
            if systemctl is-active --quiet "$_vs_u" 2>/dev/null; then
                echo "verify failed: $_vs_u is OFF in sleeves.env but still active" >&2; return 1
            fi
        done
    fi
}
