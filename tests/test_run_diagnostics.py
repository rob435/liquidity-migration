from __future__ import annotations

from liquidity_migration.run_diagnostics import (
    diagnose,
    is_tainted,
    render,
)


def _codes(warnings) -> set[str]:
    return {w.code for w in warnings}


def test_clean_run_has_no_warnings() -> None:
    ws = diagnose(
        full_pit_universe_pass=True,
        funding_mode="modeled",
        archive_manifest_empty=False,
        requested_start="2021-01-01",
        requested_end="2024-12-31",
        data_start="2021-01-01",
        data_end="2024-12-31",
        n_features=1000,
        n_trades=200,
    )
    assert ws == []
    assert not is_tainted(ws)
    assert "no warnings" in render(ws, title="t")


def test_survivorship_is_tainted_not_just_a_label() -> None:
    ws = diagnose(
        full_pit_universe_pass=False,
        funding_mode="modeled",
        archive_manifest_empty=False,
        n_features=1000,
        n_trades=200,
    )
    assert "PIT_SURVIVORSHIP" in _codes(ws)
    assert is_tainted(ws)
    # The fix names the current membership rebuild path, not a retired wrapper.
    surv = next(w for w in ws if w.code == "PIT_SURVIVORSHIP")
    assert "archive-manifest" in surv.fix


def test_empty_manifest_is_tainted() -> None:
    ws = diagnose(
        full_pit_universe_pass=False,
        funding_mode="modeled",
        archive_manifest_empty=True,
        n_features=1,
        n_trades=1,
    )
    assert "PIT_MANIFEST_EMPTY" in _codes(ws)
    # survivorship is subsumed by the empty-manifest tainted warning (not double-counted)
    assert "PIT_SURVIVORSHIP" not in _codes(ws)
    assert is_tainted(ws)


def test_funding_missing_warns_but_does_not_taint() -> None:
    ws = diagnose(
        full_pit_universe_pass=True,
        funding_mode="missing",
        archive_manifest_empty=False,
        n_features=10,
        n_trades=5,
    )
    assert "FUNDING_MISSING" in _codes(ws)
    assert not is_tainted(ws)  # a data gap is a warning, never a block


def test_funding_partial_warns() -> None:
    ws = diagnose(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
        n_features=10,
        n_trades=5,
    )
    assert "FUNDING_PARTIAL" in _codes(ws)
    assert not is_tainted(ws)


def test_window_clip_is_informational() -> None:
    ws = diagnose(
        full_pit_universe_pass=True,
        funding_mode="modeled",
        archive_manifest_empty=False,
        requested_start="2020-01-01",
        requested_end="2025-06-01",
        data_start="2021-01-01",
        data_end="2024-12-31",
        n_features=10,
        n_trades=5,
    )
    assert {"WINDOW_CLIPPED_START", "WINDOW_CLIPPED_END"} <= _codes(ws)
    assert not is_tainted(ws)


def test_no_trades_and_no_features() -> None:
    ws = diagnose(
        full_pit_universe_pass=True,
        funding_mode="modeled",
        archive_manifest_empty=False,
        n_features=0,
        n_trades=0,
    )
    assert "NO_FEATURES" in _codes(ws)
    assert "NO_TRADES" not in _codes(ws)  # NO_FEATURES subsumes NO_TRADES

    ws2 = diagnose(
        full_pit_universe_pass=True,
        funding_mode="modeled",
        archive_manifest_empty=False,
        n_features=100,
        n_trades=0,
    )
    assert "NO_TRADES" in _codes(ws2)


def test_render_marks_tainted_loudly() -> None:
    ws = diagnose(
        full_pit_universe_pass=False,
        funding_mode="missing",
        archive_manifest_empty=False,
        n_features=10,
        n_trades=5,
    )
    out = render(ws, title="long_native bybit")
    assert "TAINTED" in out
    assert "FUNDING_MISSING" in out
    assert "PIT_SURVIVORSHIP" in out
