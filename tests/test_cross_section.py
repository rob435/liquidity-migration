"""Tests for the cross-sectional evaluation primitives.

Constructed so each property is checked against a hand-computable answer,
because these primitives decide whether an anomaly read is believed.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from liquidity_migration.cross_section import (
    MEASURED_ROUND_TRIP_BP,
    PASSIVE_FLOOR_ROUND_TRIP_BP,
    CrossSectionError,
    long_short,
    summary,
    top_by,
)


def frame(rows: list[tuple[int, str, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "bar_ts_ms": [r[0] for r in rows],
            "symbol": [r[1] for r in rows],
            "sig": [r[2] for r in rows],
            "ret": [r[3] for r in rows],
        }
    )


def one_period(n: int, *, slope: float) -> pl.DataFrame:
    """n names in one period; return is `slope * signal` so the answer is exact."""

    return frame([(0, f"S{i}", float(i), slope * float(i)) for i in range(n)])


class TestLongShort:
    def test_long_low_short_high_on_a_positive_slope_loses(self) -> None:
        """ret rises with sig, so long-low/short-high must be negative."""

        out = long_short(one_period(20, slope=0.001), signal="sig", ret="ret", cut=0.1)
        # centred deciles over 20 names select {S0,S1} vs {S18,S19}
        assert out["ret_bp"][0] == pytest.approx(((0.0 + 0.001) / 2 - (0.018 + 0.019) / 2) * 1e4)

    def test_sign_inverts_the_book(self) -> None:
        d = one_period(20, slope=0.001)
        a = long_short(d, signal="sig", ret="ret", sign=1)["ret_bp"][0]
        b = long_short(d, signal="sig", ret="ret", sign=-1)["ret_bp"][0]
        assert a == pytest.approx(-b)

    def test_cut_width_selects_more_names(self) -> None:
        d = one_period(100, slope=0.001)
        wide = long_short(d, signal="sig", ret="ret", cut=0.25)["ret_bp"][0]
        narrow = long_short(d, signal="sig", ret="ret", cut=0.01)["ret_bp"][0]
        # a narrower tail reaches further into the extremes -> larger magnitude
        assert abs(narrow) > abs(wide)

    def test_sparse_periods_are_dropped(self) -> None:
        d = pl.concat([one_period(20, slope=0.001), frame([(1, "A", 1.0, 0.5), (1, "B", 2.0, -0.5)])])
        out = long_short(d, signal="sig", ret="ret", min_names=10)
        assert out["bar_ts_ms"].to_list() == [0]

    def test_nulls_are_dropped_not_ranked(self) -> None:
        d = frame([(0, f"S{i}", float(i), 0.001 * i) for i in range(20)])
        d = d.with_columns(
            pl.when(pl.col("symbol") == "S0").then(None).otherwise(pl.col("sig")).alias("sig")
        )
        out = long_short(d, signal="sig", ret="ret", cut=0.1, min_names=5)
        # 19 names remain; centred deciles select {S1,S2} vs {S18,S19}
        assert out["ret_bp"][0] == pytest.approx(((0.001 + 0.002) / 2 - (0.018 + 0.019) / 2) * 1e4)

    def test_legs_carry_equal_weight_when_counts_differ(self) -> None:
        """With an odd universe the tails can differ by a name; the book must
        still be neutral, i.e. leg means are differenced, not pooled."""

        d = one_period(21, slope=0.001)
        out = long_short(d, signal="sig", ret="ret", cut=0.1, min_names=5)
        lo = d.sort("sig").head(2)["ret"].mean()
        hi = d.sort("sig").tail(2)["ret"].mean()
        assert out["ret_bp"][0] == pytest.approx((lo - hi) * 1e4)

    def test_invalid_cut_and_sign_are_rejected(self) -> None:
        d = one_period(20, slope=0.001)
        with pytest.raises(CrossSectionError, match="cut must be"):
            long_short(d, signal="sig", ret="ret", cut=0.9)
        with pytest.raises(CrossSectionError, match="sign must be"):
            long_short(d, signal="sig", ret="ret", sign=0)

    def test_empty_input_returns_an_empty_frame_not_an_error(self) -> None:
        out = long_short(frame([]), signal="sig", ret="ret")
        assert out.height == 0


class TestTopBy:
    def test_restricts_each_period_independently(self) -> None:
        d = pl.DataFrame(
            {
                "bar_ts_ms": [0, 0, 0, 1, 1, 1],
                "symbol": list("ABCABC"),
                "adv": [3.0, 2.0, 1.0, 1.0, 2.0, 3.0],
            }
        )
        out = top_by(d, "adv", 2)
        assert sorted(out.filter(pl.col("bar_ts_ms") == 0)["symbol"]) == ["A", "B"]
        assert sorted(out.filter(pl.col("bar_ts_ms") == 1)["symbol"]) == ["B", "C"]

    def test_non_positive_n_is_rejected(self) -> None:
        with pytest.raises(CrossSectionError):
            top_by(pl.DataFrame({"bar_ts_ms": [0], "adv": [1.0]}), "adv", 0)


class TestSummary:
    def test_known_series_statistics(self) -> None:
        r = np.array([10.0, -5.0, 20.0, -5.0, 10.0, 0.0])
        s = summary(r, periods_per_year=365, cost_bp=0.0)
        assert s.n == 6
        assert s.mean_bp == pytest.approx(5.0)
        assert s.hit_rate_pct == pytest.approx(50.0)
        assert s.ann_pct == pytest.approx(5.0 / 1e4 * 365 * 100)

    def test_cost_is_charged_once_per_period(self) -> None:
        r = np.full(100, 10.0)
        assert summary(r, periods_per_year=365, cost_bp=4.0).mean_bp == pytest.approx(6.0)

    def test_default_cost_basis_is_the_measured_round_trip_not_gross(self) -> None:
        """Omitting ``cost_bp`` must not silently produce a gross number.

        A gross read is a diagnostic, not a result (``docs/governance.md`` §2),
        so the default is the measured round trip and a gross read has to be
        asked for.
        """
        r = np.full(100, 30.0)
        assert MEASURED_ROUND_TRIP_BP == pytest.approx(15.56)
        assert summary(r, periods_per_year=365).mean_bp == pytest.approx(
            30.0 - MEASURED_ROUND_TRIP_BP
        )

    def test_passive_floor_is_above_the_retired_maker_assumption(self) -> None:
        """The 4 bp maker assumption was never reachable, even at a 100% fill rate."""
        assert PASSIVE_FLOOR_ROUND_TRIP_BP == pytest.approx(5.40)
        assert PASSIVE_FLOOR_ROUND_TRIP_BP > 4.0
        assert PASSIVE_FLOOR_ROUND_TRIP_BP < MEASURED_ROUND_TRIP_BP

    def test_max_drawdown_is_measured_on_the_equity_path(self) -> None:
        # +100bp then -300bp then +100bp -> peak at 1.0%, trough at -1.0%
        r = np.array([100.0, -300.0, 100.0] + [0.0] * 10)
        assert summary(r, periods_per_year=365, cost_bp=0.0).max_drawdown_pct == pytest.approx(3.0)

    def test_tail_concentration_flags_a_fat_left_tail(self) -> None:
        fat = np.array([1.0] * 99 + [-1000.0])
        even = np.array([1.0, -1.0] * 50)
        assert summary(fat, periods_per_year=365, cost_bp=0.0).tail_concentration_pct == pytest.approx(100.0)
        assert summary(even, periods_per_year=365, cost_bp=0.0).tail_concentration_pct < 10.0

    def test_short_series_returns_nan_rather_than_a_confident_number(self) -> None:
        s = summary(np.array([1.0, 2.0]), periods_per_year=365, cost_bp=0.0)
        assert s.n == 2
        assert np.isnan(s.sharpe) and np.isnan(s.t_stat)

    def test_zero_variance_series_does_not_divide_by_zero(self) -> None:
        s = summary(np.full(50, 3.0), periods_per_year=365, cost_bp=0.0)
        assert s.mean_bp == pytest.approx(3.0)
        assert np.isnan(s.sharpe)

    def test_non_finite_values_are_excluded(self) -> None:
        r = np.array([10.0, np.nan, 10.0, np.inf, 10.0, 10.0, 10.0])
        s = summary(r, periods_per_year=365, cost_bp=0.0)
        assert s.n == 5
        assert s.mean_bp == pytest.approx(10.0)

    def test_accepts_a_polars_series(self) -> None:
        s = summary(pl.Series([1.0, 2.0, 3.0, 4.0, 5.0]), periods_per_year=365, cost_bp=0.0)
        assert s.mean_bp == pytest.approx(3.0)
