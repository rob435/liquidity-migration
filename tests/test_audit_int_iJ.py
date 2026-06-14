"""Audit integration bucket iJ — cross-file completion regression tests.

deploy-ci-4: the combined-book report systemd unit wired
``--short-data-root data/bybit-demo-event`` for the daily-SHORT sleeve that was
ERASED 2026-06-11. The owning side (the report renderer / sleeve toggles) already
fails open on a missing short root and defaults SHORT_SLEEVE to "off"; this bucket
completes the drift cleanup in the foreign systemd unit so the live report unit no
longer keeps a dead-sleeve concept wired.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_REPORT_UNIT = (
    _REPO / "deploy" / "systemd" / "liquidity-migration-combined-book-report.service"
)


def _unit_text() -> str:
    return _REPORT_UNIT.read_text(encoding="utf-8")


def test_report_unit_no_longer_wires_erased_short_data_root() -> None:
    """deploy-ci-4: the erased daily-SHORT sleeve's root must not appear in the
    live combined-book report ExecStart. Both the flag and the erased root path
    are gone, so a reader/operator can't mistake a dead short book for a reported
    one."""
    text = _unit_text()
    assert "--short-data-root" not in text, (
        "report unit still wires --short-data-root for the erased daily-SHORT sleeve"
    )
    assert "bybit-demo-event" not in text, (
        "report unit still references the erased daily-SHORT data root"
    )


def test_report_unit_still_wires_every_surviving_sleeve_root() -> None:
    """Dropping the short arg must not disturb the surviving sleeves: long demo,
    continuous demo, continuous paper, and the continuous hedge ledger all stay
    wired so the daily aggregate keeps covering the live books."""
    text = _unit_text()
    assert "combined-book-telegram-report" in text
    for arg in (
        "--long-data-root data/bybit-long-demo-event",
        "--continuous-data-root data/bybit-continuous-demo-event",
        "--continuous-paper-data-root data/bybit-continuous-paper-event",
        "--continuous-hedge-data-root data/bybit-continuous-hedge-event",
        "--include-live-positions",
    ):
        assert arg in text, f"report unit dropped a surviving-sleeve arg: {arg}"


def test_report_unit_execstart_still_well_formed() -> None:
    """The multi-line ExecStart continuation must stay intact after the edit:
    every line but the last in the ExecStart block ends with a backslash, and
    the unit still declares a single ExecStart."""
    lines = _unit_text().splitlines()
    exec_indices = [i for i, ln in enumerate(lines) if ln.startswith("ExecStart=")]
    assert len(exec_indices) == 1, "expected exactly one ExecStart in the report unit"

    start = exec_indices[0]
    # Walk the continued command; every continued line ends with a trailing '\'.
    i = start
    saw_continuation = False
    while lines[i].rstrip().endswith("\\"):
        saw_continuation = True
        i += 1
        assert i < len(lines), "ExecStart continuation runs off the end of the unit"
    assert saw_continuation, "ExecStart should span multiple continued lines"
    # The final command line of the block must not dangle a continuation.
    assert not lines[i].rstrip().endswith("\\")
