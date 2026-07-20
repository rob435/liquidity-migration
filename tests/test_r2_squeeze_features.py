"""P2.1 squeeze-feature PIT audit: per-group no-lookahead property tests.

Each feature group must be invariant to mutations of source rows at/after
the decision bar (one-bar availability lag), proving decisions at bar t use
only <= t-1 source data.
"""

from __future__ import annotations

import polars as pl

from scripts.research_v3.r2_squeeze_features import (
    breadth_features,
    funding_features,
    oi_features,
    premium_features,
    taker_features,
)

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H)


def _cutoff(n_bars: int = 60) -> int:
    return T0 + (n_bars - 6) * H


def _assert_past_invariant(base: pl.DataFrame, mutated: pl.DataFrame, cutoff_ts: int) -> None:
    b = base.filter(pl.col("ts_ms") < cutoff_ts).sort([c for c in ("symbol", "ts_ms") if c in base.columns])
    m = mutated.filter(pl.col("ts_ms") < cutoff_ts).sort([c for c in ("symbol", "ts_ms") if c in mutated.columns])
    assert b.equals(m), "feature values before the mutation cutoff changed — lookahead leak"


class TestOi:
    def test_no_lookahead(self) -> None:
        rows = [{"symbol": "A", "ts_ms": T0 + i * H, "open_interest": 100.0 + i} for i in range(60)]
        base = pl.from_dicts(rows)
        cut = _cutoff()
        mutated = base.with_columns(
            pl.when(pl.col("ts_ms") >= cut).then(pl.col("open_interest") * 100).otherwise(pl.col("open_interest")).alias("open_interest")
        )
        _assert_past_invariant(oi_features(base), oi_features(mutated), cut)

    def test_lag_means_current_bar_unseen(self) -> None:
        rows = [{"symbol": "A", "ts_ms": T0 + i * H, "open_interest": 100.0} for i in range(30)]
        spiked = pl.from_dicts(rows).with_columns(
            pl.when(pl.col("ts_ms") == T0 + 29 * H).then(1e9).otherwise(pl.col("open_interest")).alias("open_interest")
        )
        out = oi_features(spiked)
        last = out.filter(pl.col("ts_ms") == T0 + 29 * H)
        assert last["oi_change_1h"][0] is not None and abs(last["oi_change_1h"][0]) < 1e-9, (
            "the decision bar's own OI row leaked into its feature"
        )


class TestTaker:
    def test_no_lookahead(self) -> None:
        rows = [
            {"symbol": "A", "ts_ms": T0 + i * 300_000, "taker_buy_quote": 5.0 + (i % 3), "taker_sell_quote": 4.0}
            for i in range(60 * 12)
        ]
        base = pl.from_dicts(rows)
        cut = _cutoff()
        mutated = base.with_columns(
            pl.when(pl.col("ts_ms") >= cut).then(1e6).otherwise(pl.col("taker_buy_quote")).alias("taker_buy_quote")
        )
        _assert_past_invariant(taker_features(base), taker_features(mutated), cut)


class TestPremium:
    def test_no_lookahead(self) -> None:
        rows = [{"symbol": "A", "ts_ms": T0 + i * H, "close": 0.0001 * ((i % 7) - 3)} for i in range(400)]
        base = pl.from_dicts(rows)
        cut = T0 + 380 * H
        mutated = base.with_columns(
            pl.when(pl.col("ts_ms") >= cut).then(0.5).otherwise(pl.col("close")).alias("close")
        )
        _assert_past_invariant(premium_features(base), premium_features(mutated), cut)


class TestFunding:
    def test_no_lookahead(self) -> None:
        rows = [{"symbol": "A", "ts_ms": T0 + i * 8 * H, "funding_rate": 0.0001 * (i % 5)} for i in range(40)]
        base = pl.from_dicts(rows)
        cut = T0 + 30 * 8 * H
        mutated = base.with_columns(
            pl.when(pl.col("ts_ms") >= cut).then(0.02).otherwise(pl.col("funding_rate")).alias("funding_rate")
        )
        _assert_past_invariant(funding_features(base), funding_features(mutated), cut)

    def test_jump_uses_prior_settlement_only(self) -> None:
        rows = [
            {"symbol": "A", "ts_ms": T0, "funding_rate": 0.0001},
            {"symbol": "A", "ts_ms": T0 + 8 * H, "funding_rate": 0.0011},
        ]
        out = funding_features(pl.from_dicts(rows)).sort("ts_ms")
        assert out["funding_jump"][0] is None
        assert abs(out["funding_jump"][1] - 0.001) < 1e-12


class TestBreadth:
    def test_no_lookahead(self) -> None:
        rows = [
            {"symbol": s, "ts_ms": T0 + i * H, "close": 100.0 * (1.0 + 0.001 * i), "turnover_quote": 1.0}
            for i in range(60)
            for s in ("A", "B", "C")
        ]
        base = pl.from_dicts(rows)
        cut = _cutoff()
        mutated = base.with_columns(
            pl.when(pl.col("ts_ms") >= cut).then(pl.col("close") * 5).otherwise(pl.col("close")).alias("close")
        )
        _assert_past_invariant(breadth_features(base), breadth_features(mutated), cut)

    def test_current_hour_move_not_visible_same_hour(self) -> None:
        rows = [
            {"symbol": s, "ts_ms": T0 + i * H, "close": 100.0, "turnover_quote": 1.0}
            for i in range(30)
            for s in ("A", "B")
        ]
        frame = pl.from_dicts(rows).with_columns(
            pl.when((pl.col("ts_ms") == T0 + 29 * H) & (pl.col("symbol") == "A"))
            .then(150.0)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        out = breadth_features(frame).sort("ts_ms")
        last = out.filter(pl.col("ts_ms") == T0 + 29 * H)
        assert last["breadth_melt_1h"][0] == 0.0, "same-hour melt leaked into the decision bar"
