"""Regression test for the N_SYMBOLS empty-list miscount in
scripts/build_full_pit_bybit.sh (audit2b: sh_nsymbols).

The build script derives a count of symbols from a comma-separated string for a
build-log line. The original logic ``echo "$SYMBOLS" | tr ',' '\\n' | wc -l``
miscounts an EMPTY list as 1, because ``echo ""`` emits a single newline that
``wc -l`` then counts. The fix guards the empty case to produce 0 while leaving
every non-empty (happy-path) count byte-identical.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "build_full_pit_bybit.sh"
)

# The buggy formulation, preserved verbatim to prove the regression existed.
OLD_SNIPPET = 'N_SYMBOLS=$(echo "$SYMBOLS" | tr \',\' \'\\n\' | wc -l)'


def _count_with_new_logic(symbols: str) -> int:
    """Run the script's current N_SYMBOLS logic in isolation via bash."""
    script = (
        f"SYMBOLS={symbols!r}\n"
        "if [ -z \"$SYMBOLS\" ]; then\n"
        "  N_SYMBOLS=0\n"
        "else\n"
        "  N_SYMBOLS=$(echo \"$SYMBOLS\" | tr ',' '\\n' | wc -l)\n"
        "fi\n"
        'echo "$N_SYMBOLS"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _count_with_old_logic(symbols: str) -> int:
    """Run the original buggy N_SYMBOLS logic, for the failing-on-old assertion."""
    script = f"SYMBOLS={symbols!r}\n" + OLD_SNIPPET + "\n" + 'echo "$N_SYMBOLS"\n'
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def test_empty_list_counts_zero_not_one() -> None:
    # OLD code is wrong: blank line counted as one symbol.
    assert _count_with_old_logic("") == 1
    # NEW code: empty list -> 0.
    assert _count_with_new_logic("") == 0


def test_happy_path_counts_unchanged() -> None:
    # Non-empty inputs are byte-identical between old and new logic.
    for symbols in ("BTCUSDT", "BTCUSDT,ETHUSDT", "BTCUSDT,ETHUSDT,SOLUSDT"):
        old = _count_with_old_logic(symbols)
        new = _count_with_new_logic(symbols)
        assert old == new, f"happy path changed for {symbols!r}: {old} != {new}"
    assert _count_with_new_logic("BTCUSDT") == 1
    assert _count_with_new_logic("BTCUSDT,ETHUSDT") == 2
    assert _count_with_new_logic("BTCUSDT,ETHUSDT,SOLUSDT") == 3


def test_script_carries_the_guard() -> None:
    text = SCRIPT.read_text()
    # The empty-list guard is present and the bare buggy one-liner is gone.
    assert 'if [ -z "$SYMBOLS" ]; then' in text
    assert "N_SYMBOLS=0" in text
    assert not re.search(
        r"^N_SYMBOLS=\$\(echo \"\$SYMBOLS\" \| tr",
        text,
        flags=re.MULTILINE,
    ), "the unguarded buggy N_SYMBOLS one-liner is still present"
