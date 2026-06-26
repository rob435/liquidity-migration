# Shared per-sleeve kill-switch helpers - sourced by deploy_vps_live.sh + verify_vps_live.sh
# so the sleeve->units mapping and the on/off predicate live in ONE place (no drift between
# deploy and verify). Toggles come from deploy/sleeves.env (+ host override). bash-3.2-safe
# (no associative arrays). See deploy/sleeves.env for semantics.

# Space-separated unit lists per sleeve (entry/exit daemons + paper shadow). The risk service
# is intentionally NOT here - it always runs and protects every sleeve's open positions.
# The daily SHORT sleeve was ERASED from the system (operator order 2026-06-11);
# its units are gone and the deploy actively removes them from a live host.
# Continuous is split: the DEMO order-submitting sleeve vs the no-order PAPER
# evidence collector.
RETIRED_SLEEVE_UNITS="liquidity-migration-bybit-demo.service liquidity-migration-bybit-paper.service"
LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"
CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"
CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"
# Timer the continuous sleeve owns (the daily rmom-gate refresh). It runs when either
# continuous demo or continuous paper is on, because both need residual_momentum.parquet.
CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"
# The BTC-beta hedge is an order-submitting continuous-demo addon, not paper evidence.
CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"

# Load the toggles: committed defaults first, then an optional per-host override. Resolves the
# repo dir from this file's location so it works regardless of the caller's CWD.
lm_load_sleeve_toggles() {
    _lm_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$_lm_dir/sleeves.env" ] && . "$_lm_dir/sleeves.env"
    [ -f /etc/liquidity-migration/sleeves.env ] && . /etc/liquidity-migration/sleeves.env
    # Fallbacks if NEITHER file set a toggle (a stripped checkout): EVERY sleeve
    # fails safe to OFF (audit 2026-06-12 round 3 - LONG previously failed OPEN,
    # so an accidentally deleted/renamed sleeves.env would have enabled and
    # restarted the order-submitting long demo against the operator's LONG=off
    # intent). The committed deploy/sleeves.env is the real source of truth;
    # these are last-resort. A missing config disables everything; it can never
    # resurrect a sleeve.
    : "${LONG_SLEEVE:=off}"
    : "${CONTINUOUS_SLEEVE:=off}"
    : "${CONTINUOUS_PAPER_SLEEVE:=off}"
}

# sleeve_on <value> -> 0 (true) if the toggle means "run this sleeve".
# An EMPTY/unset value is OFF (fail-safe, round 3) - callers load toggles first.
sleeve_on() {
    case "${1:-off}" in
        on|ON|On|1|true|TRUE|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

continuous_rmom_refresh_on() {
    sleeve_on "${CONTINUOUS_SLEEVE:-off}" || sleeve_on "${CONTINUOUS_PAPER_SLEEVE:-off}"
}

apply_timer_enable() {
    _ate_flag="$1"; shift
    if sleeve_on "$_ate_flag"; then
        for _ate_u in "$@"; do systemctl enable --now "$_ate_u"; done
    else
        echo "kill-switch: timer group OFF -> disable --now: $*" >&2
        for _ate_u in "$@"; do systemctl disable --now "$_ate_u" 2>/dev/null || true; done
    fi
}

verify_timer() {
    _vt_flag="$1"; shift
    if sleeve_on "$_vt_flag"; then
        for _vt_u in "$@"; do
            systemctl is-enabled --quiet "$_vt_u" || { echo "verify failed: $_vt_u timer not enabled" >&2; return 1; }
            systemctl is-active --quiet "$_vt_u" || { echo "verify failed: $_vt_u timer not active" >&2; return 1; }
        done
    else
        for _vt_u in "$@"; do
            if systemctl is-active --quiet "$_vt_u" 2>/dev/null; then
                echo "verify failed: $_vt_u timer is OFF in sleeves.env but still active" >&2; return 1
            fi
            if systemctl is-enabled --quiet "$_vt_u" 2>/dev/null; then
                echo "verify failed: $_vt_u timer is OFF in sleeves.env but still enabled" >&2; return 1
            fi
        done
    fi
}

# apply_sleeve_enable <flag-value> <unit...> - on: `systemctl enable` each unit; off:
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

# verify_sleeve <flag-value> <unit...> - on: each unit must be active AND enabled; off: each
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
            if systemctl is-enabled --quiet "$_vs_u" 2>/dev/null; then
                echo "verify failed: $_vs_u is OFF in sleeves.env but still enabled" >&2; return 1
            fi
        done
    fi
}
