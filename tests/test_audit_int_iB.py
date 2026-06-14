"""Audit-integration regression tests — bucket iB.

cross-sleeve-2: the closed-trade reservation GC in
``cross_sleeve.write_account_state`` was tested on the owned side but never
exercised in production because the sole caller —
``EventWebSocketRiskEngine._refresh_cross_sleeve_account_state`` — omitted the
``closed_trade_ids`` argument, so a closed trade's symbol reservation lingered
until the 180s TTL (or an explicit release) freed it. The completion diffs the
prior-pass open trade_ids against the current open set and passes the
open->closed delta so a sibling sleeve can re-enter the freed symbol promptly.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration import cross_sleeve as _cross_sleeve
from liquidity_migration.config import ResearchConfig
from liquidity_migration.cross_sleeve import (
    claim_symbol_reservation,
    read_account_state,
)
from liquidity_migration.ws_risk import (
    EventWebSocketRiskConfig,
    EventWebSocketRiskEngine,
    _now_ms,
)


def _open_trades_frame(trade_ids: list[str]) -> pl.DataFrame:
    """A minimal open_trades snapshot carrying just the trade_id column the
    close-GC diff reads. compute_im_used tolerates extra/missing columns and the
    engine's private_client is None (equity 0.0), so this is enough to drive a
    real _refresh_cross_sleeve_account_state pass end to end."""
    return pl.DataFrame({"trade_id": pl.Series(trade_ids, dtype=pl.String)})


def _engine(root: Path) -> EventWebSocketRiskEngine:
    # No sibling roots / no private client: the refresh writes ONLY the control
    # row (IM/equity=0, reservations GC'd) into `root`, exactly as in production.
    return EventWebSocketRiskEngine(
        root, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig()
    )


def _reserved_symbols(root: Path, *, now_ms: int) -> set[str]:
    return {r["symbol"] for r in read_account_state(root).active_reservations(now_ms=now_ms)}


def _seed_control_row(root: Path, *, now_ms: int) -> None:
    """Create the cross-sleeve control row so claim_symbol_reservation PERSISTS.
    Without an existing row, claim is safe-by-default fail-open ("ws_risk not
    deployed yet") and records nothing; in production ws_risk seeds the row on
    bootstrap before any sleeve claims a symbol."""
    _cross_sleeve.write_account_state(
        root, equity_usdt=0.0, account_im_used_pct=0.0, im_used_pct_by_sleeve={}, now_ms=now_ms,
    )


def test_refresh_gcs_reservation_of_trade_that_closed_since_last_pass(tmp_path: Path) -> None:
    """The end-to-end wiring: a sibling sleeve reserves two symbols; ws_risk sees
    both trades open, then one closes. The next refresh frees ONLY the closed
    trade's reservation — without waiting out the TTL — and keeps the live one.

    Reservations are anchored to the real clock (_now_ms) because the engine's
    write uses _now_ms() for TTL filtering; that keeps both reservations well
    within their 180s TTL, so the close-GC is the ONLY thing that can drop one."""
    engine = _engine(tmp_path)
    now = _now_ms()
    _seed_control_row(tmp_path, now_ms=now)  # ws_risk seeds the row before sleeves claim
    assert claim_symbol_reservation(
        tmp_path, symbol="CLOSEDUSDT", sleeve="continuous", trade_id="t-closed", now_ms=now
    ) is True
    assert claim_symbol_reservation(
        tmp_path, symbol="LIVEUSDT", sleeve="continuous", trade_id="t-live", now_ms=now
    ) is True

    # Pass 1: both trades open -> baseline recorded, nothing closed yet, both kept.
    engine.state.open_trades = _open_trades_frame(["t-closed", "t-live"])
    engine._refresh_cross_sleeve_account_state()
    assert engine._cross_sleeve_open_trade_ids == {"t-closed", "t-live"}
    assert _reserved_symbols(tmp_path, now_ms=now) == {"CLOSEDUSDT", "LIVEUSDT"}

    # Pass 2: t-closed dropped out of open_trades (the trade closed). Both
    # reservations are well inside the 180s TTL, so only the close-GC can free
    # CLOSEDUSDT — LIVEUSDT must remain.
    engine.state.open_trades = _open_trades_frame(["t-live"])
    engine._refresh_cross_sleeve_account_state()
    assert engine._cross_sleeve_open_trade_ids == {"t-live"}
    assert _reserved_symbols(tmp_path, now_ms=now) == {"LIVEUSDT"}


def test_first_pass_closes_nothing_with_no_prior_baseline(tmp_path: Path) -> None:
    """On the very first refresh the prior-open baseline is empty, so the diff
    yields no closed ids — a fresh deploy must not GC reservations a sleeve just
    claimed for trades it has not yet observed as open."""
    engine = _engine(tmp_path)
    now = _now_ms()
    _seed_control_row(tmp_path, now_ms=now)  # ws_risk seeds the row before sleeves claim
    assert claim_symbol_reservation(
        tmp_path, symbol="AAAUSDT", sleeve="continuous", trade_id="t-a", now_ms=now
    ) is True
    assert engine._cross_sleeve_open_trade_ids == set()

    engine.state.open_trades = _open_trades_frame(["t-a"])
    engine._refresh_cross_sleeve_account_state()
    # Baseline now seeded; the just-claimed reservation is untouched.
    assert engine._cross_sleeve_open_trade_ids == {"t-a"}
    assert _reserved_symbols(tmp_path, now_ms=now) == {"AAAUSDT"}


def test_refresh_passes_closed_trade_ids_argument(tmp_path: Path, monkeypatch) -> None:
    """Lock the wiring itself: write_account_state must RECEIVE the open->closed
    delta as closed_trade_ids, independent of the owned-side GC semantics."""
    captured: list[set[str]] = []

    def _spy(*args, **kwargs):  # noqa: ANN002, ANN003
        captured.append(set(kwargs.get("closed_trade_ids", ())))

    monkeypatch.setattr(_cross_sleeve, "write_account_state", _spy)
    engine = _engine(tmp_path)

    engine.state.open_trades = _open_trades_frame(["t1", "t2"])
    engine._refresh_cross_sleeve_account_state()
    assert captured[-1] == set()  # nothing closed on the baseline pass

    engine.state.open_trades = _open_trades_frame(["t2"])
    engine._refresh_cross_sleeve_account_state()
    assert captured[-1] == {"t1"}  # t1 transitioned open->closed


def test_baseline_not_advanced_when_write_raises(tmp_path: Path, monkeypatch) -> None:
    """If write_account_state raises before completing, the prior-open baseline
    must NOT advance, so the same close-GC retries next pass (the GC is
    idempotent). The refresh itself stays self-swallowing — a control-row fault
    must never break the reconcile loop."""
    engine = _engine(tmp_path)
    engine.state.open_trades = _open_trades_frame(["t1", "t2"])
    engine._refresh_cross_sleeve_account_state()
    assert engine._cross_sleeve_open_trade_ids == {"t1", "t2"}

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated control-row write failure")

    monkeypatch.setattr(_cross_sleeve, "write_account_state", _boom)
    engine.state.open_trades = _open_trades_frame(["t2"])  # t1 closed
    # Self-swallowing: no exception propagates out of the refresh.
    engine._refresh_cross_sleeve_account_state()
    # Baseline unchanged -> t1 is re-diffed as closed on the next (successful) pass.
    assert engine._cross_sleeve_open_trade_ids == {"t1", "t2"}
