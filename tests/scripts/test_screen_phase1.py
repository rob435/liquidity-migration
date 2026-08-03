"""Contracts for the Phase 1 re-screen. A look-ahead in the BTC gate, an overlapping hold
that inflates a t-statistic, or a basket leg that is not the universe mean each produce a
better number rather than an error, so each is pinned against a hand-computable answer.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "screen_phase1", REPO_ROOT / "scripts" / "research" / "screen_phase1.py"
)
assert _spec and _spec.loader
screen_phase1 = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules, so register before exec.
sys.modules["screen_phase1"] = screen_phase1
_spec.loader.exec_module(screen_phase1)

HOUR_MS = 3_600_000


class TestBonferroniThreshold:
    def test_threshold_is_the_corrected_one_not_two(self) -> None:
        """docs/research/governance.md 2 owns the bar; the constant follows it."""
        assert screen_phase1.PROGRAM_T == pytest.approx(2.5)
        assert screen_phase1.LEGACY_BONFERRONI_T == pytest.approx(3.25)


class TestBtcTrendCausality:
    def test_current_bar_is_excluded_from_its_own_gate(self) -> None:
        """The gate value on bar t must not contain bar t's own return: a 30-day window that
        includes the current bar gates a signal on information the signal produced.
        """
        h = 2 * 24
        n = h + 5
        # A flat series that jumps only on the final bar. If the last bar leaked
        # into its own gate value, that gate would move.
        closes = [100.0] * (n - 1) + [500.0]
        panel = pl.DataFrame(
            {
                "symbol": ["BTCUSDT"] * n,
                "bar_ts_ms": [i * HOUR_MS for i in range(n)],
                "by_close": closes,
            }
        )
        out = screen_phase1.btc_trend(panel, lookback_days=2)
        last = out.filter(pl.col("bar_ts_ms") == (n - 1) * HOUR_MS)["btc_30d"][0]
        assert last == pytest.approx(0.0), "final bar's own jump leaked into its gate"

    def test_gate_reflects_prior_window_only(self) -> None:
        h = 2 * 24
        n = h + 4
        closes = [100.0] * n
        closes[-2] = 110.0  # the bar before the last one
        panel = pl.DataFrame(
            {
                "symbol": ["BTCUSDT"] * n,
                "bar_ts_ms": [i * HOUR_MS for i in range(n)],
                "by_close": closes,
            }
        )
        out = screen_phase1.btc_trend(panel, lookback_days=2)
        last = out.filter(pl.col("bar_ts_ms") == (n - 1) * HOUR_MS)["btc_30d"][0]
        # shift(1) at the last bar sees 110 against a 100 baseline h bars earlier.
        assert last == pytest.approx(0.10)


class TestDisjointSampling:
    def test_entries_are_spaced_by_the_full_hold(self) -> None:
        n = 100
        frame = pl.DataFrame(
            {
                "symbol": ["A"] * n,
                "bar_ts_ms": [i * HOUR_MS for i in range(n)],
            }
        )
        out = screen_phase1._disjoint(frame, 24)
        offs = sorted(int(t) // HOUR_MS for t in out["bar_ts_ms"].to_list())
        gaps = {b - a for a, b in zip(offs, offs[1:])}
        assert gaps == {24}, f"holds overlap: gaps {gaps}"

    def test_hold_of_one_keeps_every_bar(self) -> None:
        n = 10
        frame = pl.DataFrame(
            {"symbol": ["A"] * n, "bar_ts_ms": [i * HOUR_MS for i in range(n)]}
        )
        assert screen_phase1._disjoint(frame, 1).height == n


class TestBasketShort:
    def test_short_leg_is_the_universe_mean(self) -> None:
        """Variant B shorts the whole universe, not the opposite decile."""
        rets = [0.10, 0.02, 0.01, -0.03]  # universe mean = 0.025
        frame = pl.DataFrame(
            {
                "bar_ts_ms": [0] * 4,
                "sig": [1.0, 2.0, 3.0, 4.0],
                "net_return": rets,
            }
        )
        # cut=0.25 selects the single lowest-signal name -> its return is 0.10
        out = screen_phase1.basket_short_book(frame, "sig", 0.25)
        expected = (0.10 - float(np.mean(rets))) * 1e4
        assert out["ret_bp"][0] == pytest.approx(expected)

    def test_a_flat_universe_earns_nothing(self) -> None:
        frame = pl.DataFrame(
            {"bar_ts_ms": [0] * 4, "sig": [1.0, 2.0, 3.0, 4.0], "net_return": [0.05] * 4}
        )
        out = screen_phase1.basket_short_book(frame, "sig", 0.25)
        assert out["ret_bp"][0] == pytest.approx(0.0)


class TestConditionalShort:
    def test_short_only_book_inverts_the_sign_of_the_move(self) -> None:
        frame = pl.DataFrame(
            {
                "bar_ts_ms": [0, 0],
                "drop_4d": [-0.20, -0.30],
                "btc_30d": [0.05, 0.05],
                "net_return": [-0.04, -0.06],  # both fell further -> short profits
            }
        )
        out = screen_phase1.conditional_short_book(
            frame, drop_threshold=-0.10, require_btc_uptrend=False
        )
        assert out["ret_bp"][0] == pytest.approx(500.0)

    def test_btc_gate_excludes_downtrend_bars(self) -> None:
        frame = pl.DataFrame(
            {
                "bar_ts_ms": [0, 1],
                "drop_4d": [-0.20, -0.20],
                "btc_30d": [0.05, -0.05],
                "net_return": [-0.04, -0.04],
            }
        )
        gated = screen_phase1.conditional_short_book(
            frame, drop_threshold=-0.10, require_btc_uptrend=True
        )
        assert gated.height == 1
        assert int(gated["bar_ts_ms"][0]) == 0


class TestVenueView:
    """2A's two arms must be the same code over different columns, not two books."""

    @staticmethod
    def _panel() -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": ["A"],
                "bar_ts_ms": [0],
                "by_close": [100.0],
                "by_turnover_quote": [1.0],
                "by_funding": [0.001],
                "by_funding_age_h": [0.0],
                "bn_close": [200.0],
                "bn_turnover_quote": [2.0],
                "bn_funding": [0.002],
                "bn_funding_age_h": [1.0],
                "premium_diff_bp": [5.0],
            }
        )

    def test_bybit_view_is_the_panel_unchanged(self) -> None:
        p = self._panel()
        assert screen_phase1.venue_view(p, "bybit").equals(p)

    def test_binance_view_swaps_price_and_its_own_funding(self) -> None:
        v = screen_phase1.venue_view(self._panel(), "binance")
        assert v["by_close"][0] == pytest.approx(200.0)
        assert v["by_funding"][0] == pytest.approx(0.002)
        assert v["by_turnover_quote"][0] == pytest.approx(2.0)

    def test_premium_diff_is_preserved_as_a_cross_venue_observable(self) -> None:
        v = screen_phase1.venue_view(self._panel(), "binance")
        assert v["premium_diff_bp"][0] == pytest.approx(5.0)

    def test_binance_view_drops_the_bybit_columns_it_replaces(self) -> None:
        """Leaving both would let a later join silently read the wrong venue."""
        v = screen_phase1.venue_view(self._panel(), "binance")
        assert "bn_close" not in v.columns
        assert v.columns.count("by_close") == 1

    def test_unknown_venue_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            screen_phase1.venue_view(self._panel(), "okx")


class TestMeasureTurnover:
    """The cost charge is derived from this, so it is pinned to hand answers."""

    @staticmethod
    def _universe(period_signals: list[list[float]]) -> pl.DataFrame:
        rows: list[dict[str, object]] = []
        for p, sigs in enumerate(period_signals):
            for i, s in enumerate(sigs):
                rows.append({"bar_ts_ms": p * 24 * HOUR_MS, "symbol": f"S{i}", "sig": s})
        return pl.DataFrame(rows)

    def test_full_rebalance_into_a_disjoint_set_trades_four_units(self) -> None:
        """Close both legs, open both legs = 4.0 units one-way = 2x the round trip."""
        # 4 names, cut=0.25 -> 1 long (lowest) + 1 short (highest) each period.
        # Period 0 longs S0 / shorts S3; period 1 longs S3 / shorts S0 — disjoint.
        u = self._universe([[1.0, 2.0, 3.0, 4.0], [4.0, 2.0, 3.0, 1.0]])
        assert screen_phase1.measure_turnover(u, [("sig", +1, 1.0)], 0.25) == pytest.approx(4.0)

    def test_an_unchanged_book_trades_nothing(self) -> None:
        u = self._universe([[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]])
        assert screen_phase1.measure_turnover(u, [("sig", +1, 1.0)], 0.25) == pytest.approx(0.0)

    def test_overlap_reduces_the_charge_below_four(self) -> None:
        """Only the short leg rotates, so half the book is held through."""
        # Period 0: long S0, short S3. Period 1: long S0 (unchanged), short S2.
        u = self._universe([[1.0, 3.0, 2.0, 4.0], [1.0, 3.0, 4.0, 2.0]])
        turn = screen_phase1.measure_turnover(u, [("sig", +1, 1.0)], 0.25)
        assert 0.0 < turn < 4.0
        assert turn == pytest.approx(2.0)

    def test_a_single_period_cannot_be_measured_and_assumes_no_overlap(self) -> None:
        u = self._universe([[1.0, 2.0, 3.0, 4.0]])
        assert screen_phase1.measure_turnover(u, [("sig", +1, 1.0)], 0.25) == pytest.approx(4.0)


class TestEraSplit:
    def test_cost_is_charged_inside_each_era(self) -> None:
        day = 86_400_000
        # 2021-01-01 onward, 10 daily bars, all +40 bp gross
        start = 1_609_459_200_000
        book = pl.DataFrame(
            {"bar_ts_ms": [start + i * day for i in range(10)], "ret_bp": [40.0] * 10}
        )
        eras = screen_phase1.era_split(book, 31.12)
        assert set(eras) == {2021}
        n, mean, t = eras[2021]
        assert n == 10
        assert mean == pytest.approx(40.0 - 31.12)
        assert math.isnan(t)  # zero variance -> no confident t
