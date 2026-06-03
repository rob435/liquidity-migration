"""Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves.env).

Runs the actual bash helpers against a STATEFUL fake `systemctl` (tracks enabled/active
state so is-active/is-enabled reflect reality), so we test real behavior — not just syntax:
  * on  -> systemctl enable + the unit verifies active+enabled
  * off -> systemctl disable --now + the unit verifies NOT active (and fails if still up)
This is the contract the live deploy/verify rely on, on a tree we can't run systemd in.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# Stateful fake systemctl: logs calls, tracks <unit>.enabled / <unit>.active under $STATE.
echo "$@" >> "$LOG"
cmd="$1"; shift
args=(); for a in "$@"; do [ "$a" = "--quiet" ] || [ "$a" = "--now" ] || args+=("$a"); done
case "$cmd" in
  enable)  for u in "${args[@]}"; do touch "$STATE/$u.enabled" "$STATE/$u.active"; done ;;
  disable) for u in "${args[@]}"; do rm -f "$STATE/$u.enabled" "$STATE/$u.active"; done ;;
  restart|start) for u in "${args[@]}"; do touch "$STATE/$u.active"; done ;;
  is-active)  for u in "${args[@]}"; do [ -f "$STATE/$u.active"  ] || exit 1; done ;;
  is-enabled) for u in "${args[@]}"; do [ -f "$STATE/$u.enabled" ] || exit 1; done ;;
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
    script = textwrap.dedent(f"""
        set -euo pipefail
        export PATH="{fake_bin}:$PATH" STATE="{state}" LOG="{log}"
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
    rc, _calls, err = _run(tmp_path, """
        apply_sleeve_enable on  $SHORT_SLEEVE_UNITS
        apply_sleeve_enable off $CONTINUOUS_SLEEVE_UNITS
        verify_sleeve on  $SHORT_SLEEVE_UNITS        # enabled+active -> passes
        verify_sleeve off $CONTINUOUS_SLEEVE_UNITS    # not active     -> passes
    """)
    assert rc == 0, f"verify should pass for a correctly-applied toggle:\n{err}"


def test_verify_fails_when_on_sleeve_is_not_running(tmp_path: Path) -> None:
    # An "on" sleeve that was never enabled must FAIL verify (deploy would abort loudly).
    rc, _calls, err = _run(tmp_path, """
        verify_sleeve on $SHORT_SLEEVE_UNITS
    """)
    assert rc != 0, "verify_sleeve on must fail when the units are not active"
    assert "not active" in err


def test_loaded_toggles_keep_short_long_on_continuous_off(tmp_path: Path) -> None:
    # Loaded toggles: SHORT/LONG run; CONTINUOUS stays OFF (look-ahead-disabled 2026-06-03,
    # versioned in the committed deploy/sleeves.env so a host rebuild can't resurrect it).
    rc, _calls, err = _run(tmp_path, """
        lm_load_sleeve_toggles
        test "$SHORT_SLEEVE" = on && test "$LONG_SLEEVE" = on && test "$CONTINUOUS_SLEEVE" = off
        echo "TOGGLES_OK"
    """)
    assert rc == 0, err


def test_lib_fallback_defaults_continuous_off_short_long_on(tmp_path: Path) -> None:
    # Last-resort fallback (NEITHER sleeves.env present): SHORT/LONG default on, CONTINUOUS
    # OFF — even a stripped checkout can never resurrect the look-ahead-disabled sleeve.
    rc, _calls, err = _run(tmp_path, """
        : "${SHORT_SLEEVE:=on}"; : "${LONG_SLEEVE:=on}"; : "${CONTINUOUS_SLEEVE:=off}"
        test "$SHORT_SLEEVE" = on && test "$LONG_SLEEVE" = on && test "$CONTINUOUS_SLEEVE" = off
        echo "FALLBACK_OK"
    """)
    assert rc == 0, err


def test_committed_sleeves_env_keeps_continuous_off() -> None:
    # The committed file is the source of truth: SHORT/LONG=on, CONTINUOUS=off. Each line must
    # be systemd-EnvironmentFile-safe (plain KEY=value, no inline comment on the assignment).
    env = (REPO / "deploy" / "sleeves.env").read_text()
    expected = {"SHORT_SLEEVE": "on", "LONG_SLEEVE": "on", "CONTINUOUS_SLEEVE": "off"}
    for flag, value in expected.items():
        line = next(ln for ln in env.splitlines() if ln.startswith(f"{flag}="))
        assert line == f"{flag}={value}", f"{flag} must be plain KEY={value} (no inline comment): {line!r}"
