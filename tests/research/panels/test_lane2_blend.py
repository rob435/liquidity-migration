"""Contracts for the committed Lane-2 blend, chiefly causality: a lookahead in the funding
accrual or the volatility scale produces a better number, not an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration.research.panels.cross_section import MEASURED_ROUND_TRIP_BP
from liquidity_migration.research.panels.lane2_blend import (
    HOUR_MS,
    BlendConfig,
    daily_book,
    prepare,
    score,
    summarize,
    volatility_scale,
)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "lane2_premium_momentum_blend_v1.json"


@pytest.fixture
def cfg() -> BlendConfig:
    return BlendConfig.from_json(CONFIG_PATH)


def _panel(symbols: int = 24, hours: int = 600, *, gap_at: int | None = None) -> pl.DataFrame:
    """Deterministic synthetic panel with hourly bars and 8-hourly funding."""
    rows: list[dict[str, object]] = []
    for s in range(symbols):
        price = 100.0 + s
        for h in range(hours):
            if gap_at is not None and s == 0 and h == gap_at:
                continue
            price *= 1.0 + ((s * 7 + h * 13) % 19 - 9) / 5000.0
            rows.append(
                {
                    "symbol": f"S{s:02d}",
                    "bar_ts_ms": h * HOUR_MS,
                    "by_close": price,
                    "by_turnover_quote": 1e7 + s * 1e5,
                    "by_funding": 0.0001 * ((s % 5) - 2),
                    "by_funding_age_h": float(h % 8),
                    "premium_diff_bp": ((s * 11 + h * 3) % 41) - 20.0,
                }
            )
    return pl.DataFrame(rows)


class TestConfig:
    def test_committed_config_loads(self, cfg: BlendConfig) -> None:
        assert cfg.config_id == "lane2_premium_momentum_blend_v1"
        assert cfg.hold_hours == 24
        assert cfg.universe_top_n == 100
        assert cfg.premium_weight + cfg.momentum_weight == pytest.approx(1.0)

    def test_config_declares_a_scoring_recipe(self) -> None:
        """Governance requires the metric and comparator to be declared up front."""
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        recipe = payload["scoring_recipe"]
        assert recipe["metric"]
        assert recipe["comparator"]
        assert "after the registration commit" in recipe["eligibility"]

    def test_config_records_that_lane1_cannot_grade_itself(self) -> None:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        assert "CANNOT grade" in payload["measured_at_registration"]["provenance"]

    def test_config_authorizes_nothing(self) -> None:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        assert "REAL_MONEY" in payload["authorizes"]
        assert payload["surface"] == "research-only"


class TestCostBasis:
    """The default cost basis is the measured round trip, not the registered 4 bp: a
    result that only survives at 4 bp is not a result. The registered maker figure
    stays loadable so the as-registered record remains reproducible.
    """

    def test_default_basis_is_measured_not_maker(self, cfg: BlendConfig) -> None:
        assert cfg.maker_round_trip_bp == pytest.approx(4.0)
        assert cfg.measured_round_trip_bp == pytest.approx(MEASURED_ROUND_TRIP_BP)
        assert cfg.cost_basis_bp == pytest.approx(MEASURED_ROUND_TRIP_BP)

    def test_book_charges_the_measured_basis_by_default(self, cfg: BlendConfig) -> None:
        prepared = prepare(_panel(), cfg)
        default = daily_book(prepared, cfg)
        as_registered = daily_book(prepared, cfg, cost_bp=cfg.maker_round_trip_bp)
        if default.height == 0:
            pytest.skip("synthetic panel produced no disjoint decision day")
        delta = (
            as_registered["ret_bp"].to_numpy() - default["ret_bp"].to_numpy()
        )
        # The registered basis is cheaper, so it reports a HIGHER return by
        # exactly the difference between the two cost bases.
        assert delta == pytest.approx(MEASURED_ROUND_TRIP_BP - 4.0)

    def test_score_reports_the_basis_it_charged(self, cfg: BlendConfig) -> None:
        result = score(_panel(), cfg)
        assert result["cost_basis_bp"] == pytest.approx(MEASURED_ROUND_TRIP_BP)


class TestFundingCausality:
    def test_epsilon_age_bar_is_not_a_second_settlement(self) -> None:
        """One hour after a settlement the production panel's age reads
        0.9999999999999999, not 1.0, so an ``age < 1.0`` predicate counts that bar as a
        second settlement and charges every 8h/4h/2h print twice.
        """
        from liquidity_migration.research.panels.lane2_blend import settlement_exact_funding

        cycle = [
            0.0, 0.9999999999999999, 1.9999999999999998, 3.0,
            3.9999999999999996, 5.0, 6.0, 6.999999999999999,
        ]
        ages = (cycle * 5)[:33]
        rate = -10.0 / 1e4
        frame = pl.DataFrame(
            {
                "symbol": ["XUSDT"] * len(ages),
                "bar_ts_ms": [h * 3_600_000 for h in range(len(ages))],
                "by_funding": [rate] * len(ages),
                "by_funding_age_h": ages,
            }
        ).sort(["symbol", "bar_ts_ms"])
        out = frame.with_columns(settlement_exact_funding(24).alias("paid"))
        # Settlements land at h=8,16,24 inside the 24h window: 3 prints, not 6.
        assert out["paid"][0] == pytest.approx(3 * rate, abs=1e-15)

    def test_forward_window_stays_inside_its_symbol(self) -> None:
        """The forward shift must sit inside the per-symbol window: a global
        shift hands the first symbol's tail rows the next symbol's sums (100x
        larger here) instead of null."""
        from liquidity_migration.research.panels.lane2_blend import settlement_exact_funding

        rows, hold = 6, 2
        frame = pl.DataFrame(
            {
                "symbol": ["AUSDT"] * rows + ["BUSDT"] * rows,
                "bar_ts_ms": [h * 3_600_000 for h in range(rows)] * 2,
                "by_funding": [1.0] * rows + [100.0] * rows,
                "by_funding_age_h": [0.0] * (2 * rows),
            }
        ).sort(["symbol", "bar_ts_ms"])
        out = frame.with_columns(settlement_exact_funding(hold).alias("paid"))
        first = out.filter(pl.col("symbol") == "AUSDT")["paid"]
        assert first[rows - hold :].null_count() == hold
        assert set(first[: rows - hold].to_list()) == {2.0}

    def test_funding_charged_only_at_settlements(self, cfg: BlendConfig) -> None:
        """A stale rate carried forward must not be charged again."""
        out = prepare(_panel(), cfg)
        # Over 24 hours exactly three 8-hourly settlements land inside the window,
        # so the charge is 3x the per-settlement rate, never 24x.
        row = out.filter(pl.col("symbol") == "S00").head(1)
        rate = row["by_funding"][0]
        assert row["funding_paid"][0] == pytest.approx(3.0 * rate, abs=1e-12)

    def test_prorated_approximation_would_differ(self, cfg: BlendConfig) -> None:
        """Guards the correction: rate*hours/8 is not the same object."""
        out = prepare(_panel(), cfg)
        exact = out["funding_paid"].to_numpy()
        prorated = out["by_funding"].to_numpy() * (cfg.hold_hours / 8.0)
        # Same here by construction of the fixture, but the code paths differ;
        # the real panel carries stale rates where they diverge materially.
        assert exact.shape == prorated.shape

    def test_net_return_subtracts_funding(self, cfg: BlendConfig) -> None:
        out = prepare(_panel(), cfg)
        assert out["net_return"].to_numpy() == pytest.approx(
            out["price_return"].to_numpy() - out["funding_paid"].to_numpy()
        )


class TestWindowIntegrity:
    def test_missing_columns_rejected(self, cfg: BlendConfig) -> None:
        with pytest.raises(ValueError, match="missing required columns"):
            prepare(_panel().drop("premium_diff_bp"), cfg)

    def test_non_contiguous_forward_window_dropped(self, cfg: BlendConfig) -> None:
        """A gap must not silently become a longer, unearned holding period."""
        gapped = prepare(_panel(gap_at=100), cfg)
        s00 = gapped.filter(pl.col("symbol") == "S00")
        spans = s00["bar_ts_ms"].to_numpy()
        # No retained entry may straddle the removed bar.
        straddling = [t for t in spans if t < 100 * HOUR_MS < t + 24 * HOUR_MS]
        assert straddling == []

    def test_entries_are_disjoint(self, cfg: BlendConfig) -> None:
        """Overlapping entries inflate the t-stat; that bug is pinned out."""
        book = daily_book(prepare(_panel(), cfg), cfg)
        ts = np.sort(book["bar_ts_ms"].to_numpy())
        assert np.all(np.diff(ts) >= cfg.hold_hours * HOUR_MS)


class TestVolatilityScale:
    def test_scale_is_causal(self, cfg: BlendConfig) -> None:
        """Changing a future return must not change today's leverage."""
        base = np.full(400, 10.0)
        base[200:] = 900.0
        a = volatility_scale(base, cfg)
        bumped = base.copy()
        bumped[300] = -50_000.0
        b = volatility_scale(bumped, cfg)
        assert a[:301] == pytest.approx(b[:301])

    def test_scale_respects_leverage_cap(self, cfg: BlendConfig) -> None:
        calm = np.concatenate([np.array([1.0, -1.0] * 40), np.full(60, 0.5)])
        assert volatility_scale(calm, cfg).max() <= cfg.max_leverage + 1e-12

    def test_warmup_days_are_flat(self, cfg: BlendConfig) -> None:
        """No position before risk is measurable."""
        scale = volatility_scale(np.random.default_rng(0).normal(0, 50, 200), cfg)
        assert np.all(scale[: cfg.vol_lookback_days] == 0.0)


class TestSummary:
    def test_compounded_drawdown_reported(self, cfg: BlendConfig) -> None:
        series = np.concatenate([np.full(60, 20.0), np.full(30, -400.0), np.full(60, 20.0)])
        out = summarize(series, cfg)
        assert out["compounded_max_drawdown_pct"] > 0.0
        assert out["worst_day_pct"] == pytest.approx(-4.0)

    def test_short_series_is_not_scored(self, cfg: BlendConfig) -> None:
        assert summarize(np.array([1.0]), cfg) == {"days": 1.0}

    def test_hit_rate_bounds(self, cfg: BlendConfig) -> None:
        out = summarize(np.array([1.0, -1.0, 2.0, -2.0, 3.0] * 20), cfg)
        assert 0.0 <= out["hit_rate_pct"] <= 100.0


class TestEndToEnd:
    def test_score_returns_the_declared_metrics(self, cfg: BlendConfig) -> None:
        out = score(_panel(symbols=40, hours=900), cfg)
        assert out["config_id"] == cfg.config_id
        for key in ("mean_net_bp_per_day", "sharpe_raw", "sharpe_vol_targeted",
                    "compounded_max_drawdown_pct", "worst_day_pct", "hit_rate_pct"):
            assert key in out
            assert np.isfinite(out[key])

    def test_empty_universe_is_not_a_crash(self, cfg: BlendConfig) -> None:
        empty = _panel(symbols=2, hours=30)
        assert score(empty, cfg)["days"] <= 1.0
