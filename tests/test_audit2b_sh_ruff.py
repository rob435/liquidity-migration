"""Regression test for the gate-7 ruff fallback in
scripts/verify_full_pit_rebuild.sh (audit2b: sh_ruff).

The verification script runs ``set -euo pipefail`` and, in gate 7, linted with::

    .venv/bin/ruff check liquidity_migration tests || ruff check liquidity_migration tests

The ``||`` was intended only as a fallback for a *missing* ``.venv/bin/ruff``
(exit 127), but it fires on ANY non-zero exit — including a genuine lint
failure (ruff exits 1 when it finds errors). So if the canonical venv ruff found
a real lint error, the gate silently re-checked against a different PATH ruff;
when that one passed (version/config drift), the gate reported PASS and the
script printed "All gates PASSED" despite a real lint failure.

The fix selects the ruff binary up-front (prefer ``.venv/bin/ruff`` if
executable, else PATH ``ruff``) and runs it exactly once, so its exit code —
including a lint failure — propagates and fails the gate. When the venv binary
is absent the fallback to PATH ruff is preserved, and the happy path (venv ruff
present and clean) is byte-identical.
"""

from __future__ import annotations

import pathlib
import subprocess

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_full_pit_rebuild.sh"
)

# Stub ruff binaries: a passing stub (exit 0) and a failing stub (exit 1,
# mimicking ruff finding a lint error).
_PASS_STUB = '#!/usr/bin/env bash\nexit 0\n'
_FAIL_STUB = '#!/usr/bin/env bash\necho "F401 unused import"\nexit 1\n'


def _make_stub(path: pathlib.Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run_old_gate(tmp_path: pathlib.Path, venv_body: str, path_body: str | None) -> int:
    """Model the OLD gate-7 lint line: ``$VENV check || ruff check``.

    Returns the exit code under ``set -euo pipefail`` (what the script as a whole
    would have done at that line). ``path_body=None`` means no PATH ruff exists.
    """
    venv = tmp_path / "venv_ruff"
    _make_stub(venv, venv_body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if path_body is not None:
        _make_stub(bindir / "ruff", path_body)
    script = (
        "set -euo pipefail\n"
        f'"{venv}" check liquidity_migration tests'
        " || ruff check liquidity_migration tests\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    ).returncode


def _run_new_gate(tmp_path: pathlib.Path, venv_body: str | None, path_body: str | None) -> int:
    """Model the NEW gate-7 lint logic: pick the binary, then run it once.

    ``venv_body=None`` means ``.venv/bin/ruff`` is absent (fallback to PATH).
    """
    venv = tmp_path / "venv_ruff"
    if venv_body is not None:
        _make_stub(venv, venv_body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if path_body is not None:
        _make_stub(bindir / "ruff", path_body)
    script = (
        "set -euo pipefail\n"
        f'if [ -x "{venv}" ]; then\n'
        f'  RUFF_BIN="{venv}"\n'
        "else\n"
        '  RUFF_BIN="ruff"\n'
        "fi\n"
        '"$RUFF_BIN" check liquidity_migration tests\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    ).returncode


def test_old_logic_masks_a_real_lint_failure(tmp_path: pathlib.Path) -> None:
    # Canonical venv ruff finds a lint error (exit 1); PATH ruff passes.
    # OLD: the `||` swallows the failure -> gate exits 0 (masked).
    assert _run_old_gate(tmp_path, _FAIL_STUB, _PASS_STUB) == 0


def test_new_logic_fails_the_gate_on_a_real_lint_failure(tmp_path: pathlib.Path) -> None:
    # Same inputs as above. NEW: venv ruff is chosen and its exit 1 propagates.
    assert _run_new_gate(tmp_path, _FAIL_STUB, _PASS_STUB) != 0


def test_new_logic_happy_path_unchanged(tmp_path: pathlib.Path) -> None:
    # Venv ruff present and clean -> gate passes, identical to old behavior.
    assert _run_old_gate(tmp_path, _PASS_STUB, _PASS_STUB) == 0
    assert _run_new_gate(tmp_path, _PASS_STUB, _PASS_STUB) == 0


def test_new_logic_falls_back_when_venv_ruff_absent(tmp_path: pathlib.Path) -> None:
    # The original fallback intent is preserved: missing venv ruff -> PATH ruff.
    assert _run_new_gate(tmp_path, None, _PASS_STUB) == 0
    # And a PATH-ruff lint failure still fails the gate.
    assert _run_new_gate(tmp_path, None, _FAIL_STUB) != 0


def test_script_carries_the_fix() -> None:
    text = SCRIPT.read_text()
    # The single-binary selection is present.
    assert 'if [ -x .venv/bin/ruff ]; then' in text
    assert 'RUFF_BIN=".venv/bin/ruff"' in text
    assert '"$RUFF_BIN" check liquidity_migration tests' in text
    # The masking `||` fallback one-liner is gone.
    assert ".venv/bin/ruff check liquidity_migration tests || ruff check" not in text
