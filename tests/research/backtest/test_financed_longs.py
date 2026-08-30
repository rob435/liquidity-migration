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
    daily_scores,
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

    def test_config_scores_dispatches_carry_and_rejects_dead_shapes(self) -> None:
        import json as _json

        from liquidity_migration.research.backtest.financed_longs import config_scores

        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        _, _, carry_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v1.json")
        _, _, carry2_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v2.json")
        assert carry_id == "lane2_carry_hold_v1"
        assert carry2_id == "lane2_carry_hold_v2"
        # The leaders ("signal") and spread ("spread") shapes were deleted
        # 2026-08-19 by operator override; a leftover config must fail loudly.
        import tempfile

        for shape in ("signal", "spread"):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                _json.dump({"config_id": "ghost", "rule": {shape: {}}}, f)
            with pytest.raises(FinancedLongsError, match="unrecognized"):
                config_scores(panel, f.name)


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
        decision_bars = sorted({int(item) for item in u["bar_ts_ms"].unique().to_list()})
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
        assert len(exits) == 5
        for ts in entries + exits:
            assert by_ts[ts] == pytest.approx(0.10, rel=1e-6)
        for ts in decision_bars:
            if ts not in entries and ts not in exits:
                assert by_ts[ts] == pytest.approx(0.0, abs=1e-12)
        assert scores["oneway"].sum() == pytest.approx(0.10 * 10, rel=1e-9)

    def test_terminal_hold_is_liquidated_on_a_final_cash_row(self) -> None:
        universe = pl.DataFrame(
            {
                "bar_ts_ms": [0, 86_400_000],
                "symbol": ["AAAUSDT", "AAAUSDT"],
                "net_return": [0.01, 0.02],
            }
        )
        weights = pl.DataFrame(
            {
                "bar_ts_ms": [0, 86_400_000],
                "symbol": ["AAAUSDT", "AAAUSDT"],
                "w": [0.25, 0.25],
            }
        )

        scores = daily_scores(weights, universe, fee_side_bp=5.0)

        assert scores["bar_ts_ms"].to_list() == [0, 86_400_000]
        assert scores["gross_bp"].to_list() == pytest.approx([25.0, 50.0])
        assert scores["oneway"].to_list() == pytest.approx([0.25, 0.25])
        assert scores["cost_bp"].to_list() == pytest.approx([1.25, 1.25])

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
        view = venue_view(_panel(), "binance")
        assert "by_close" in view.columns and "bn_close" not in view.columns

    def test_score_carry_hold_end_to_end(self, carry_cfg: CarryHoldConfig) -> None:
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        out = score_carry_hold(panel, carry_cfg)
        assert out["config_id"] == "lane2_carry_hold_v1"
        assert out["days"] > 0
