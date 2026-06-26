"""Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves.env).

Runs the actual bash helpers against a STATEFUL fake `systemctl` (tracks enabled/active
state so is-active/is-enabled reflect reality), so we test real behavior — not just syntax:
  * on  -> systemctl enable (wants-symlink only) THEN a separate start/restart; the
           unit then verifies active+enabled. Bare `enable` alone is NOT enough —
           verify_sleeve on fails until the unit is started, mirroring the live
           deploy's enable-then-restart ordering (lib_sleeves.sh + deploy_vps_live.sh).
  * off -> systemctl disable --now + the unit verifies NOT active (and fails if still up)
This is the contract the live deploy/verify rely on, on a tree we can't run systemd in.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# NOTE: the `enable` branch deliberately matches real `systemctl enable` (without
# --now): it writes the wants-symlink (.enabled) but does NOT start the unit
# (.active). `enable --now`, `start`, and `restart` are what mark a unit active —
# mirroring lib_sleeves.sh (bare `enable` for on-sleeves) + deploy_vps_live.sh
# (separate `systemctl restart` for on-sleeves). A fake that set .active on bare
# `enable` would let an on-path verify pass with no start step, so a regression
# dropping the deploy's restart lines would stay green (kill-switch-3).
_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# Stateful fake systemctl: logs calls, tracks <unit>.enabled / <unit>.active under $STATE.
echo "$@" >> "$LOG"
cmd="$1"; shift
# Detect --now BEFORE stripping it: `enable --now` both enables AND starts.
now=0; for a in "$@"; do [ "$a" = "--now" ] && now=1; done
args=(); for a in "$@"; do [ "$a" = "--quiet" ] || [ "$a" = "--now" ] || args+=("$a"); done
case "$cmd" in
  enable)  for u in "${args[@]}"; do touch "$STATE/$u.enabled"; [ "$now" = 1 ] && touch "$STATE/$u.active"; done ;;
  disable) for u in "${args[@]}"; do rm -f "$STATE/$u.enabled" "$STATE/$u.active"; done ;;
  restart|start) for u in "${args[@]}"; do touch "$STATE/$u.active"; done ;;
  stop) for u in "${args[@]}"; do rm -f "$STATE/$u.active"; done ;;
  is-active)  for u in "${args[@]}"; do [ -f "$STATE/$u.active"  ] || exit 1; done ;;
  is-enabled) for u in "${args[@]}"; do [ -f "$STATE/$u.enabled" ] || exit 1; done ;;
  list-unit-files)
    for f in "$STATE"/liquidity-migration-*.*.enabled "$STATE"/liquidity-migration-*.*.active; do
      [ -e "$f" ] || continue
      b="$(basename "$f")"; u="${b%.enabled}"; u="${u%.active}"
      echo "$u enabled"
    done | sort -u
    ;;
  list-units)
    for f in "$STATE"/liquidity-migration-*.*.active; do
      [ -e "$f" ] || continue
      b="$(basename "$f")"; u="${b%.active}"
      echo "$u loaded active running fake"
    done | sort -u
    ;;
esac
exit 0
"""


def _run(tmp_path: Path, body: str) -> tuple[int, str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "systemctl").write_text(_FAKE_SYSTEMCTL)
    (fake_bin / "systemctl").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    log.write_text("")
    host_env = tmp_path / "missing-host-sleeves.env"
    resolved_env = tmp_path / "sleeves.resolved.env"
    script = textwrap.dedent(f"""
        set -euo pipefail
        export PATH="{fake_bin}:$PATH" STATE="{state}" LOG="{log}"
        export LM_HOST_SLEEVES_ENV="{host_env}" LM_RESOLVED_SLEEVES_ENV="{resolved_env}"
        cd "{REPO}"
        . deploy/lib_sleeves.sh
        {body}
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return proc.returncode, log.read_text(), proc.stderr


def test_off_sleeve_is_disabled_on_sleeve_is_enabled(tmp_path: Path) -> None:
    rc, calls, err = _run(tmp_path, """
        apply_sleeve_enable on  $CONTINUOUS_SLEEVE_UNITS
        apply_sleeve_enable off $LONG_SLEEVE_UNITS
    """)
    assert rc == 0, err
    # on -> enable each unit; off -> disable --now each unit (never enable).
    assert "enable liquidity-migration-bybit-continuous-demo.service" in calls
    assert "disable --now liquidity-migration-bybit-long-demo.service" in calls
    assert "disable --now liquidity-migration-bybit-long-paper.service" in calls
    assert "enable liquidity-migration-bybit-long-demo.service" not in calls


def test_verify_passes_for_on_after_enable_and_off_after_disable(tmp_path: Path) -> None:
    # Mirror the live deploy's enable-THEN-restart ordering for on-sleeves
    # (apply_sleeve_enable does bare `enable`; deploy_vps_live.sh then `systemctl
    # restart`s the on-sleeves before verify). verify_sleeve on requires BOTH
    # enabled AND active, so the restart is load-bearing (kill-switch-3).
    rc, _calls, err = _run(tmp_path, """
        apply_sleeve_enable on  $LONG_SLEEVE_UNITS
        systemctl restart $LONG_SLEEVE_UNITS          # the deploy's separate start step
        apply_sleeve_enable off $CONTINUOUS_SLEEVE_UNITS
        verify_sleeve on  $LONG_SLEEVE_UNITS          # enabled+active -> passes
        verify_sleeve off $CONTINUOUS_SLEEVE_UNITS    # not active     -> passes
    """)
    assert rc == 0, f"verify should pass for a correctly-applied toggle:\n{err}"


def test_verify_on_fails_when_enabled_but_not_started(tmp_path: Path) -> None:
    # The on-contract is enable + (separate) start. `systemctl enable` without
    # --now writes the wants-symlink but does NOT start the unit, so verify_sleeve
    # on must FAIL after enable alone — this is exactly the deploy bug (dropping
    # the `systemctl restart` lines) the kill-switch test exists to catch. Before
    # the fake-systemctl fix this could not be expressed: bare `enable` wrongly
    # marked the unit active (kill-switch-3).
    rc, _calls, err = _run(tmp_path, """
        apply_sleeve_enable on $LONG_SLEEVE_UNITS
        verify_sleeve on $LONG_SLEEVE_UNITS
    """)
    assert rc != 0, "verify_sleeve on must FAIL when units are enabled but never started"
    assert "not active" in err


def test_verify_fails_when_on_sleeve_is_not_running(tmp_path: Path) -> None:
    # An "on" sleeve that was never enabled must FAIL verify (deploy would abort loudly).
    rc, _calls, err = _run(tmp_path, """
        verify_sleeve on $LONG_SLEEVE_UNITS
    """)
    assert rc != 0, "verify_sleeve on must fail when the units are not active"
    assert "not active" in err


def test_continuous_paper_split_keeps_demo_orders_off_runs_paper(tmp_path: Path) -> None:
    rc, calls, err = _run(tmp_path, """
        apply_sleeve_enable off $CONTINUOUS_SLEEVE_UNITS
        apply_sleeve_enable on  $CONTINUOUS_PAPER_SLEEVE_UNITS
        systemctl restart $CONTINUOUS_PAPER_SLEEVE_UNITS   # the deploy's separate start step for on-sleeves
        verify_sleeve off $CONTINUOUS_SLEEVE_UNITS
        verify_sleeve on  $CONTINUOUS_PAPER_SLEEVE_UNITS
        case " $CONTINUOUS_SLEEVE_UNITS " in *continuous-paper.service*) echo "paper still bundled with demo" >&2; exit 1 ;; esac
        echo "CONTINUOUS_SPLIT_OK"
    """)
    assert rc == 0, err
    assert "disable --now liquidity-migration-bybit-continuous-demo.service" in calls
    assert "enable liquidity-migration-bybit-continuous-paper.service" in calls
    assert "enable liquidity-migration-bybit-continuous-demo.service" not in calls


def test_hedge_lifecycle_off_stops_armed_service(tmp_path: Path) -> None:
    rc, calls, err = _run(tmp_path, """
        systemctl enable --now $CONTINUOUS_HEDGE_TIMERS
        systemctl start $CONTINUOUS_HEDGE_SERVICES
        apply_hedge_timer_enable off
        verify_hedge_timer_enable off
    """)
    assert rc == 0, err
    assert "disable --now liquidity-migration-continuous-hedge.timer" in calls
    assert "stop liquidity-migration-continuous-hedge.service" in calls


def test_hedge_lifecycle_off_verify_fails_when_service_active(tmp_path: Path) -> None:
    rc, _calls, err = _run(tmp_path, """
        systemctl start $CONTINUOUS_HEDGE_SERVICES
        verify_hedge_timer_enable off
    """)
    assert rc != 0
    assert "liquidity-migration-continuous-hedge.service service is OFF" in err


def test_unknown_liquidity_migration_unit_is_cleaned_and_verified(tmp_path: Path) -> None:
    rc, calls, err = _run(tmp_path, """
        systemctl enable --now liquidity-migration-bybit-risk.service
        lm_verify_no_unknown_liqmig_units
        systemctl enable --now liquidity-migration-stale-alpha.service
        if lm_verify_no_unknown_liqmig_units; then
            echo "unknown unit passed verify" >&2
            exit 1
        fi
        lm_cleanup_unknown_liqmig_units
        lm_verify_no_unknown_liqmig_units
    """)
    assert rc == 0, err
    assert "disable --now liquidity-migration-stale-alpha.service" in calls


def test_loaded_toggles_long_continuous_and_paper_on(tmp_path: Path) -> None:
    # Loaded toggles: LONG re-enabled 2026-06-16 by operator (demo diversifier, shares the
    # netted demo account with continuous); continuous demo + paper stay on.
    rc, _calls, err = _run(tmp_path, """
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = on && test "$CONTINUOUS_SLEEVE" = on             && test "$CONTINUOUS_PAPER_SLEEVE" = on
        echo "TOGGLES_OK"
    """)
    assert rc == 0, err


def test_host_override_can_only_turn_repo_on_sleeve_off(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    host_env = tmp_path / "host-sleeves.env"
    resolved_env = tmp_path / "resolved-sleeves.env"
    (lib_dir / "lib_sleeves.sh").write_text((REPO / "deploy" / "lib_sleeves.sh").read_text())
    (lib_dir / "sleeves.env").write_text(
        "LONG_SLEEVE=off\n"
        "CONTINUOUS_SLEEVE=off\n"
        "CONTINUOUS_PAPER_SLEEVE=on\n"
    )
    host_env.write_text(
        "LONG_SLEEVE=on\n"
        "CONTINUOUS_SLEEVE=on\n"
        "CONTINUOUS_PAPER_SLEEVE=off\n"
    )
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}" LM_RESOLVED_SLEEVES_ENV="{resolved_env}"
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = off
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = off
        lm_write_resolved_sleeve_toggles
        lm_verify_resolved_sleeve_toggles
        grep -Fx LONG_SLEEVE=off "{resolved_env}"
        grep -Fx CONTINUOUS_SLEEVE=off "{resolved_env}"
        grep -Fx CONTINUOUS_PAPER_SLEEVE=off "{resolved_env}"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_host_override_keeps_repo_on_sleeve_on_when_host_on(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    host_env = tmp_path / "host-sleeves.env"
    (lib_dir / "lib_sleeves.sh").write_text((REPO / "deploy" / "lib_sleeves.sh").read_text())
    (lib_dir / "sleeves.env").write_text(
        "LONG_SLEEVE=on\n"
        "CONTINUOUS_SLEEVE=on\n"
        "CONTINUOUS_PAPER_SLEEVE=on\n"
    )
    host_env.write_text("LONG_SLEEVE=on\nCONTINUOUS_SLEEVE=off\n")
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}"
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = on
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = on
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_lib_fallback_defaults_every_sleeve_off(tmp_path: Path) -> None:
    """Last-resort fallback (NEITHER sleeves.env present — a stripped checkout):
    EVERY sleeve defaults OFF since audit 2026-06-12 round 3 — LONG previously
    failed OPEN, so an accidentally deleted/renamed sleeves.env would have
    enabled and restarted the order-submitting long demo against the operator's
    LONG=off intent. A missing config disables everything; it can never
    resurrect a sleeve. Exercises the lib's ACTUAL lm_load_sleeve_toggles
    against a copy with no sleeves.env beside it."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    host_env = tmp_path / "missing-host-sleeves.env"
    (lib_dir / "lib_sleeves.sh").write_text((REPO / "deploy" / "lib_sleeves.sh").read_text())
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}"
        unset LONG_SLEEVE CONTINUOUS_SLEEVE CONTINUOUS_PAPER_SLEEVE 2>/dev/null || true
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = off
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = off
        echo "FALLBACK_OK"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "FALLBACK_OK" in proc.stdout


def test_committed_sleeves_env_long_and_continuous_on() -> None:
    # The committed file is the source of truth. 2026-06-16 operator instruction: LONG
    # re-enabled (demo diversifier on the shared demo account) alongside the continuous
    # demo + paper sleeves. 2026-06-11: the daily-short sleeve was ERASED — no SHORT
    # toggles remain. Each line must be systemd-EnvironmentFile-safe (plain KEY=value,
    # no inline comment).
    env = (REPO / "deploy" / "sleeves.env").read_text()
    expected = {
        "LONG_SLEEVE": "on",
        "CONTINUOUS_SLEEVE": "on",
        "CONTINUOUS_PAPER_SLEEVE": "on",
    }
    for flag, value in expected.items():
        line = next(ln for ln in env.splitlines() if ln.startswith(f"{flag}="))
        assert line == f"{flag}={value}", f"{flag} must be plain KEY={value} (no inline comment): {line!r}"
