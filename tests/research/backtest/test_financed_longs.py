"""Contracts for the committed financed-longs configs.

A lookahead in the hysteresis state, a gross cap that levers instead of diluting, or a
funding accrual with the wrong sign each make the number better rather than raise, so
each is pinned on deterministic synthetic frames.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration.research.backtest.financed_longs import (
    FinancedLeadersConfig,
    btc_gate,
    daily_scores,
    financed_leaders_weights,
    prepare,
    score_carry_hold,
    venue_view,
    volatility_scale,
)
from liquidity_migration.rules.carry_hold import (
    HOUR_MS,
    CarryHoldConfig,
    FinancedLongsError,
    carry_hold_weights,
    daily_grid,
    top_n_universe,
)

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"


@pytest.fixture
def carry_cfg() -> CarryHoldConfig:
    return CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v1.json")


@pytest.fixture
def leaders_cfg() -> FinancedLeadersConfig:
    return FinancedLeadersConfig.from_json(CONFIG_DIR / "lane2_financed_leaders_v1.json")


def _panel(
    symbols: int = 14,
    hours: int = 480,
    funding_bp: dict[str, list[float]] | None = None,
    drift: dict[str, float] | None = None,
) -> pl.DataFrame:
    """Hourly panel; funding settles every 8h (bar_ts divisible by 8h)."""
    rows: list[dict[str, object]] = []
    funding_bp = funding_bp or {}
    drift = drift or {}
    for s in range(symbols):
        sym = f"S{s:02d}USDT" if s else "BTCUSDT"
        price = 100.0
        f_series = funding_bp.get(sym)
        for h in range(hours):
            ts = h * HOUR_MS
            price *= 1.0 + drift.get(sym, 0.0)
            settle = (h % 8) == 0
            idx = h // 8
            rate = (f_series[idx % len(f_series)] if f_series else 1.0) / 1e4
            rows.append(
                {
                    "symbol": sym,
                    "bar_ts_ms": ts,
                    "by_close": price,
                    "by_turnover_quote": 1_000_000.0 * (symbols - s),
                    "by_funding": rate,
                    "by_funding_age_h": 0.0 if settle else float((h % 8)),
                    "bn_close": price * 1.001,
                    "bn_turnover_quote": 900_000.0 * (symbols - s),
                    "bn_funding": rate / 2.0,
                    "bn_funding_age_h": 0.0 if settle else float((h % 8)),
                }
            )
    return pl.DataFrame(rows)


def _universe(panel: pl.DataFrame, top_n: int = 100) -> pl.DataFrame:
    return top_n_universe(daily_grid(prepare(panel)), top_n)


class TestFundingSpread:
    """Cross-venue neutral spread book: sign convention, hysteresis, fees.

    The fixture ties bn_funding = by_funding / 2, so a deep bybit print of -60 bp
    gives an 8h-settled spread of 3 x (-60 + 30) = -90 bp/day: long bybit / short
    binance, receiving the differential.
    """

    def test_config_round_trip_and_dispatch(self) -> None:
        from liquidity_migration.research.backtest.financed_longs import FundingSpreadConfig, config_scores

        cfg = FundingSpreadConfig.from_json(CONFIG_DIR / "lane2_funding_spread_v1.json")
        assert (cfg.enter_bp_per_day, cfg.exit_bp_per_day) == (80.0, 20.0)
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-60.0]})
        _, _, cid, venue = config_scores(panel, CONFIG_DIR / "lane2_funding_spread_v1.json")
        assert cid == "lane2_funding_spread_v1"
        assert venue == "bybit+binance"

    def test_neutral_return_is_the_funding_differential_when_prices_track(self) -> None:
        from liquidity_migration.research.backtest.financed_longs import prepare_spread

        # Flat prices on both venues: the neutral fwd-24h return must equal
        # -(paid_by) + paid_bn = 3 x (60 - 30) bp = +90 bp for the deep name.
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-60.0]})
        u = daily_grid(prepare_spread(panel))
        row = u.filter(pl.col("symbol") == "S01USDT").head(1)
        assert row["spread_bpd"][0] == pytest.approx(-90.0, rel=1e-9)
        assert row["net_return"][0] == pytest.approx(90.0 / 1e4, rel=1e-6)

    def test_hysteresis_enters_deep_and_exits_converged(self) -> None:
        from liquidity_migration.research.backtest.financed_longs import (
            FundingSpreadConfig,
            funding_spread_weights,
            prepare_spread,
        )

        cfg = FundingSpreadConfig.from_json(CONFIG_DIR / "lane2_funding_spread_v1.json")
        n = 1100 // 8
        # deep spread (-90 bp/day) for the first half, converged (-22.5) after:
        # exit fires when |spread| < 20?  -22.5 stays above 20 -> pick -6 bp
        # prints -> spread -9 bp/day < 20 -> exit.
        pattern = [-60.0] * (n // 2) + [-6.0] * (n - n // 2)
        panel = _panel(hours=1100, funding_bp={"S01USDT": pattern})
        u = daily_grid(prepare_spread(panel))
        w = funding_spread_weights(u, cfg).filter(pl.col("symbol") == "S01USDT")
        assert w.height > 0
        spreads = dict(zip(u.filter(pl.col("symbol") == "S01USDT")["bar_ts_ms"].to_list(),
                           u.filter(pl.col("symbol") == "S01USDT")["spread_bpd"].to_list()))
        held_spreads = [spreads[t] for t in w["bar_ts_ms"].to_list()]
        assert all(abs(s) >= cfg.exit_bp_per_day for s in held_spreads)

    def test_fees_charged_on_both_legs(self) -> None:
        from liquidity_migration.research.backtest.financed_longs import FundingSpreadConfig, score_funding_spread

        cfg = FundingSpreadConfig.from_json(CONFIG_DIR / "lane2_funding_spread_v1.json")
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-60.0]})
        out = score_funding_spread(panel, cfg)
        assert out["config_id"] == "lane2_funding_spread_v1"
        assert out["days"] > 0
        # entry day cost = 0.10 notional x BOTH legs x per-side fee
        from liquidity_migration.research.backtest.financed_longs import (
            funding_spread_weights, prepare_spread,
        )
        u = top_n_universe(daily_grid(prepare_spread(panel)), cfg.universe_top_n)
        w = funding_spread_weights(u, cfg)
        s = daily_scores(w, u, 2.0 * cfg.fee_side_bp).sort("bar_ts_ms")
        assert s["cost_bp"][0] == pytest.approx(0.10 * 2.0 * cfg.fee_side_bp, rel=1e-6)


class TestFinancedLeaders:
    def test_funding_cap_excludes_paying_longs(self, leaders_cfg: FinancedLeadersConfig) -> None:
        # S01 rallies with negative funding (financed); S02 rallies with positive.
        # 24 symbols so the momentum decile holds both leaders; 1,100 hours so
        # the 30-day BTC gate window exists on the daily grid.
        panel = _panel(
            symbols=24,
            hours=1100,
            funding_bp={"S01USDT": [-2.0], "S02USDT": [5.0]},
            drift={"S01USDT": 0.0041, "S02USDT": 0.004},
        )
        grid = daily_grid(prepare(panel))
        u = top_n_universe(grid, 100)
        gate = btc_gate(grid, leaders_cfg.btc_gate_lookback_days)
        w = financed_leaders_weights(u, gate, leaders_cfg)
        names = set(w["symbol"].to_list())
        assert "S01USDT" in names
        assert "S02USDT" not in names

    def test_gate_uses_prior_days_only(self, leaders_cfg: FinancedLeadersConfig) -> None:
        grid = daily_grid(prepare(_panel()))
        gate = btc_gate(grid, leaders_cfg.btc_gate_lookback_days)
        # flat BTC: trend defined only once a full prior window exists, and nulls before
        head = gate.head(leaders_cfg.btc_gate_lookback_days)
        assert head["btc_trend"].null_count() == head.height

    def test_config_round_trip(self, leaders_cfg: FinancedLeadersConfig) -> None:
        assert leaders_cfg.funding_cap_bp == 0.0
        assert leaders_cfg.btc_gate_threshold == -0.05
        assert leaders_cfg.venue == "bybit"


class TestResearchEquityChart:
    """The wrapper-supported standard-format render for registered configs, so a Lane-2
    config never needs a hand-built lookalike chart.
    """

    def test_renders_standard_chart_with_research_label(self, tmp_path: Path) -> None:
        from liquidity_migration.research.backtest.financed_longs import research_equity_chart

        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        payload = research_equity_chart(
            panel,
            CONFIG_DIR / "lane2_carry_hold_v1.json",
            tmp_path,
            start="1970-01-01",
            end="1970-02-01",
        )
        assert payload["config_id"] == "lane2_carry_hold_v1"
        assert payload["run_label"] == "lane2_carry_hold_v1_research_seen_data_corrected_scorer"
        png = tmp_path / "lane2_carry_hold_v1_equity_btc.png"
        assert png.exists() and png.stat().st_size > 0
        assert payload["png"] == str(png)
        assert (tmp_path / "lane2_carry_hold_v1_daily_equity.csv").exists()
        assert (tmp_path / "lane2_carry_hold_v1_summary.json").exists()
        # The chart must carry the full standard metric tile set.
        assert {"total_return_pct", "max_drawdown_pct", "sharpe_daily_ann", "mar", "years"} <= set(
            payload["metrics"]
        )

    def test_config_scores_dispatches_both_rule_shapes(self) -> None:
        from liquidity_migration.research.backtest.financed_longs import config_scores

        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        _, _, carry_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v1.json")
        _, _, carry2_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v2.json")
        _, _, leaders_id, _ = config_scores(panel, CONFIG_DIR / "lane2_financed_leaders_v1.json")
        assert carry_id == "lane2_carry_hold_v1"
        assert carry2_id == "lane2_carry_hold_v2"
        assert leaders_id == "lane2_financed_leaders_v1"


class TestAccounting:
    def test_long_receives_negative_funding(self) -> None:
        # price flat, funding -10 bp/8h: net_return over 24h = +30 bp of funding
        panel = _panel(funding_bp={"S01USDT": [-10.0]})
        u = _universe(panel)
        row = u.filter(pl.col("symbol") == "S01USDT").head(1)
        assert row["net_return"][0] == pytest.approx(3 * 10.0 / 1e4, rel=1e-6)

    def test_turnover_charged_on_entry_and_exit(self, carry_cfg: CarryHoldConfig) -> None:
        panel = _panel(funding_bp={"S01USDT": [1.0, -15.0, -15.0, 1.0, 1.0]})
        u = _universe(panel)
        w = carry_hold_weights(u, carry_cfg)
        scores = daily_scores(w, u, carry_cfg.fee_side_bp).sort("bar_ts_ms")
        # first held day pays entry on 0.10 notional
        assert scores["cost_bp"][0] == pytest.approx(0.10 * carry_cfg.fee_side_bp, rel=1e-6)
        # Total one-way turnover must be even: every entry has a matching exit.
        # Iterating only bars PRESENT in `weights` leaves a flat decision day
        # charging neither the exit into it nor the re-entry out of it, while
        # gross treats the book as liquidated.
        held = {int(value) for value in w["bar_ts_ms"].unique().to_list()}
        decision_bars = sorted(
            value
            for value in {int(item) for item in u["bar_ts_ms"].unique().to_list()}
            if min(held) <= value <= max(held)
        )
        assert scores["bar_ts_ms"].to_list() == decision_bars
        flat_days = [ts for ts in decision_bars if ts not in held]
        assert flat_days, "fixture must contain at least one interior flat day"
        by_ts = dict(zip(scores["bar_ts_ms"].to_list(), scores["oneway"].to_list()))
        # Every held bar charges an entry; the FIRST flat bar after each held
        # block charges the matching exit; a second consecutive flat bar charges
        # nothing. The old behaviour charged only the 5 entries (0.50).
        entries = [ts for ts in decision_bars if ts in held]
        exits = [
            ts
            for index, ts in enumerate(decision_bars)
            if ts not in held and index > 0 and decision_bars[index - 1] in held
        ]
        assert len(entries) == 5
        assert len(exits) == 4
        for ts in entries + exits:
            assert by_ts[ts] == pytest.approx(0.10, rel=1e-6)
        for ts in decision_bars:
            if ts not in entries and ts not in exits:
                assert by_ts[ts] == pytest.approx(0.0, abs=1e-12)
        assert scores["oneway"].sum() == pytest.approx(0.10 * 9, rel=1e-9)

    def test_vol_scale_uses_strict_prior_window(self) -> None:
        rng = np.random.default_rng(7)
        net = rng.normal(0.0, 50.0, 120)
        scale = volatility_scale(net, target_annual=0.15, lookback_days=30, max_leverage=3.0)
        assert (scale[:30] == 0).all()
        assert scale.max() <= 3.0

    def test_venue_view_rejects_unknown(self) -> None:
        with pytest.raises(FinancedLongsError):
            venue_view(_panel(), "okx")

    def test_binance_view_scores(self) -> None:
        cfg = FinancedLeadersConfig.from_json(CONFIG_DIR / "lane2_financed_leaders_binance_v1.json")
        assert cfg.venue == "binance"
        view = venue_view(_panel(), "binance")
        assert "by_close" in view.columns and "bn_close" not in view.columns

    def test_score_carry_hold_end_to_end(self, carry_cfg: CarryHoldConfig) -> None:
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        out = score_carry_hold(panel, carry_cfg)
        assert out["config_id"] == "lane2_carry_hold_v1"
        assert out["days"] > 0
