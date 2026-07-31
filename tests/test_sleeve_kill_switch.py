"""Functional tests for sleeve topology helpers against a stateful fake systemctl."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

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
  reset-failed) : ;;
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
      printf '\342\227\217 %s loaded active running fake\n' "$u"
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
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    return proc.returncode, log.read_text(), proc.stderr
def test_unknown_liquidity_migration_unit_is_cleaned_and_verified(tmp_path: Path) -> None:
    rc, calls, err = _run(tmp_path, """
        systemctl enable --now liquidity-migration-account-execution.service
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
    assert "reset-failed liquidity-migration-stale-alpha.service" in calls


def test_loaded_toggles_match_committed_sleeve_state(tmp_path: Path) -> None:
    # Loaded toggles must equal the committed deploy/sleeves.env: LONG on,
    # CONTINUOUS is retired from demo AND paper,
    # CARRY on for demo. The paper CARRY producer is off and replaced by
    # PAPER_TARGET_MIRROR: two producers deciding independently off a raced read
    # of one data store cannot be made to agree. One `test` per toggle -- a
    # failing inner member of one `&&` chain is ignored by `set -e`, since only
    # the LAST member's status escapes.
    rc, _calls, err = _run(tmp_path, """
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = on
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = off
        test "$CARRY_SLEEVE" = on
        test "$CARRY_PAPER_SLEEVE" = off
        test "$PAPER_TARGET_MIRROR" = on
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
        "CARRY_SLEEVE=off\n"
        "CARRY_PAPER_SLEEVE=on\n"
    )
    host_env.write_text(
        "LONG_SLEEVE=on\n"
        "CONTINUOUS_SLEEVE=on\n"
        "CONTINUOUS_PAPER_SLEEVE=off\n"
        "CARRY_SLEEVE=on\n"
        "CARRY_PAPER_SLEEVE=off\n"
    )
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}" LM_RESOLVED_SLEEVES_ENV="{resolved_env}"
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = off
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = off
        test "$CARRY_SLEEVE" = off
        test "$CARRY_PAPER_SLEEVE" = off
        lm_write_resolved_sleeve_toggles
        lm_verify_resolved_sleeve_toggles
        grep -Fx LONG_SLEEVE=off "{resolved_env}"
        grep -Fx CONTINUOUS_SLEEVE=off "{resolved_env}"
        grep -Fx CONTINUOUS_PAPER_SLEEVE=off "{resolved_env}"
        grep -Fx CARRY_SLEEVE=off "{resolved_env}"
        grep -Fx CARRY_PAPER_SLEEVE=off "{resolved_env}"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
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
        "CARRY_SLEEVE=on\n"
        "CARRY_PAPER_SLEEVE=on\n"
    )
    host_env.write_text("LONG_SLEEVE=on\nCONTINUOUS_SLEEVE=off\nCARRY_PAPER_SLEEVE=off\n")
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}"
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = on
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = on
        test "$CARRY_SLEEVE" = on
        test "$CARRY_PAPER_SLEEVE" = off
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_host_override_is_parsed_as_data_not_shell_source(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    host_env = tmp_path / "host-sleeves.env"
    marker = tmp_path / "shell-evaluated"
    (lib_dir / "lib_sleeves.sh").write_text(
        (REPO / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (lib_dir / "sleeves.env").write_text(
        "LONG_SLEEVE=on\nCONTINUOUS_SLEEVE=on\nCONTINUOUS_PAPER_SLEEVE=on\n"
        "CARRY_SLEEVE=on\nCARRY_PAPER_SLEEVE=on\n",
        encoding="utf-8",
    )
    host_env.write_text(
        f'LONG_SLEEVE="$(touch {marker})"\n',
        encoding="utf-8",
    )
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}"
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
    """)
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode != 0
    assert "invalid sleeve toggle value" in proc.stderr
    assert not marker.exists()


def test_lib_fallback_defaults_every_sleeve_off(tmp_path: Path) -> None:
    """Last-resort fallback with NEITHER sleeves.env present (a stripped checkout): every
    sleeve defaults OFF, so a deleted or renamed sleeves.env can never enable and
    restart an order-submitting sleeve against the operator's intent. Exercises the
    lib's actual ``lm_load_sleeve_toggles`` against a copy with no sleeves.env beside
    it.
    """
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    host_env = tmp_path / "missing-host-sleeves.env"
    (lib_dir / "lib_sleeves.sh").write_text((REPO / "deploy" / "lib_sleeves.sh").read_text())
    script = textwrap.dedent(f"""
        set -euo pipefail
        export LM_HOST_SLEEVES_ENV="{host_env}"
        unset LONG_SLEEVE CONTINUOUS_SLEEVE CONTINUOUS_PAPER_SLEEVE \
            CARRY_SLEEVE CARRY_PAPER_SLEEVE 2>/dev/null || true
        . "{lib_dir}/lib_sleeves.sh"
        lm_load_sleeve_toggles
        test "$LONG_SLEEVE" = off
        test "$CONTINUOUS_SLEEVE" = off
        test "$CONTINUOUS_PAPER_SLEEVE" = off
        test "$CARRY_SLEEVE" = off
        test "$CARRY_PAPER_SLEEVE" = off
        echo "FALLBACK_OK"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert "FALLBACK_OK" in proc.stdout


def test_committed_sleeves_env_continuous_retired() -> None:
    # The committed file is the source of truth. CONTINUOUS is retired from demo
    # and paper; LONG stays on; CARRY replaces CONTINUOUS on demo; paper CARRY is
    # served by PAPER_TARGET_MIRROR so one fleet decides and both execute. Each
    # line must be systemd-EnvironmentFile-safe (plain KEY=value, no inline
    # comment).
    env = (REPO / "deploy" / "sleeves.env").read_text()
    expected = {
        "LONG_SLEEVE": "on",
        "CONTINUOUS_SLEEVE": "off",
        "CONTINUOUS_PAPER_SLEEVE": "off",
        "CARRY_SLEEVE": "on",
        "CARRY_PAPER_SLEEVE": "off",
        "PAPER_TARGET_MIRROR": "on",
        # Real-money producers. Repo-off is a hard ceiling a host override
        # cannot lift, so arming them is a committed change and not a host edit.
        "CARRY_MAINNET_SLEEVE": "off",
        "LONG_MAINNET_SLEEVE": "off",
    }
    for flag, value in expected.items():
        line = next(ln for ln in env.splitlines() if ln.startswith(f"{flag}="))
        assert line == f"{flag}={value}", f"{flag} must be plain KEY={value} (no inline comment): {line!r}"
    # Exactly the registered keys — no stray toggles the parser would reject.
    keys = [ln.split("=", 1)[0] for ln in env.splitlines() if ln and not ln.startswith("#")]
    assert keys == list(expected)


def test_parser_rejects_duplicate_carry_toggles(tmp_path: Path) -> None:
    duplicate = tmp_path / "dup-sleeves.env"
    duplicate.write_text("CARRY_SLEEVE=on\nCARRY_SLEEVE=off\n", encoding="utf-8")
    script = textwrap.dedent(f"""
        set -euo pipefail
        cd "{REPO}"
        . deploy/lib_sleeves.sh
        lm_parse_sleeve_environment "{duplicate}"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    assert "duplicate CARRY_SLEEVE" in proc.stderr

    duplicate_paper = tmp_path / "dup-paper-sleeves.env"
    duplicate_paper.write_text("CARRY_PAPER_SLEEVE=on\nCARRY_PAPER_SLEEVE=on\n", encoding="utf-8")
    script = textwrap.dedent(f"""
        set -euo pipefail
        cd "{REPO}"
        . deploy/lib_sleeves.sh
        lm_parse_sleeve_environment "{duplicate_paper}"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    assert "duplicate CARRY_PAPER_SLEEVE" in proc.stderr
