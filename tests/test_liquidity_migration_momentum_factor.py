from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.momentum_factor import (
    MODE_LONG_ONLY,
    MODE_LONG_SHORT,
    MomentumFactorConfig,
    SIZING_EQUAL,
    SIZING_VOL_PARITY,
    _build_target_portfolio,
    _compute_weights,
    _empty_factor_trades,
    _evaluate_promotion,
    _validate_config,
    _vol_target_scale,
    _zscore,
    build_factor_features,
    format_factor_report,
    run_momentum_factor_research,
)
from liquidity_migration.storage import write_dataset


START_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _make_klines_1h(
    *,
    symbols: list[str],
    n_days: int,
    log_trend_per_day: list[float] | None = None,
    base_prices: list[float] | None = None,
    turnover_per_hour: list[float] | None = None,
    start_ms: int = START_MS,
) -> pl.DataFrame:
    base_prices = base_prices or [100.0] * len(symbols)
    log_trend_per_day = log_trend_per_day or [0.0] * len(symbols)
    turnover_per_hour = turnover_per_hour or [1_000_000.0] * len(symbols)
    rows: list[dict] = []
    n_hours = 24 * n_days
    for i, symbol in enumerate(symbols):
        slope = log_trend_per_day[i] / 24.0
        base = base_prices[i]
        for h in range(n_hours):
            ts_ms = start_ms + h * MS_PER_HOUR
            close = base * math.exp(slope * h)
            open_ = base * math.exp(slope * max(h - 1, 0)) if h > 0 else close
            high = max(open_, close) * 1.001
            low = min(open_, close) * 0.999
            day_iso = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()
            rows.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "date": day_iso,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume_base": turnover_per_hour[i] / close,
                    "turnover_quote": turnover_per_hour[i],
                    "source": "fixture",
                }
            )
    return pl.DataFrame(rows).sort(["symbol", "ts_ms"])


def _make_funding_8h(symbols, n_days, rates=None, start_ms=START_MS):
    rows: list[dict] = []
    rates = rates or [0.0001] * len(symbols)
    for i, symbol in enumerate(symbols):
        for d in range(n_days):
            for h in (0, 8, 16):
                ts_ms = start_ms + d * MS_PER_DAY + h * MS_PER_HOUR
                rows.append(
                    {
                        "ts_ms": ts_ms,
                        "symbol": symbol,
                        "funding_rate": rates[i],
                        "funding_rate_8h_equiv": rates[i],
                        "funding_interval_min": 480,
                    }
                )
    return pl.DataFrame(rows).sort(["symbol", "ts_ms"])


def test_validate_config_defaults_pass():
    _validate_config(MomentumFactorConfig())


def test_validate_config_rejects_bad_mode():
    with pytest.raises(ValueError):
        _validate_config(MomentumFactorConfig(mode="bogus"))


def test_validate_config_rejects_empty_lookbacks():
    with pytest.raises(ValueError):
        _validate_config(MomentumFactorConfig(momentum_lookbacks_days=()))


def test_zscore_basic():
    arr = np.asarray([1.0, 2.0, 3.0, 4.0, 5.0])
    z = _zscore(arr)
    assert math.isclose(float(z.mean()), 0.0, abs_tol=1e-9)
    assert math.isclose(float(z.std(ddof=1)), 1.0, rel_tol=1e-6)


def test_zscore_constant_returns_zeros():
    arr = np.asarray([3.0, 3.0, 3.0])
    z = _zscore(arr)
    assert (z == 0.0).all()


def test_zscore_with_nan_preserves_nan():
    arr = np.asarray([1.0, np.nan, 3.0, 5.0])
    z = _zscore(arr)
    assert np.isnan(z[1])
    assert np.isfinite(z[0]) and np.isfinite(z[2]) and np.isfinite(z[3])


def test_compute_weights_equal():
    cfg = MomentumFactorConfig(sizing=SIZING_EQUAL)
    rows = [{"symbol": "A", "realized_vol": 0.5}, {"symbol": "B", "realized_vol": 1.0}]
    w = _compute_weights(rows, config=cfg, side="long")
    assert w["A"] == w["B"] == 1.0


def test_compute_weights_vol_parity_inverse_to_vol():
    cfg = MomentumFactorConfig(sizing=SIZING_VOL_PARITY, vol_floor_annual=0.30)
    rows = [{"symbol": "LOW", "realized_vol": 0.30}, {"symbol": "HIGH", "realized_vol": 1.20}]
    w = _compute_weights(rows, config=cfg, side="long")
    # LOW: 1/0.30, HIGH: 1/1.20 → LOW weight = 4× HIGH
    assert math.isclose(w["LOW"] / w["HIGH"], 4.0, rel_tol=1e-6)


def test_compute_weights_vol_parity_applies_floor():
    cfg = MomentumFactorConfig(sizing=SIZING_VOL_PARITY, vol_floor_annual=0.50)
    rows = [{"symbol": "A", "realized_vol": 0.10}]  # below floor
    w = _compute_weights(rows, config=cfg, side="long")
    # vol_used = max(0.10, 0.50) = 0.50 → w = 1/0.50 = 2.0
    assert math.isclose(w["A"], 2.0, rel_tol=1e-6)


def test_vol_target_scale_shrinks_when_above_target():
    cfg = MomentumFactorConfig(vol_target_annual=0.10, assumed_avg_correlation=0.5, vol_target_max_scale=4.0)
    # High-vol positions, target much lower → scale < 1
    positions = [
        {"symbol": "A", "weight": 0.20, "realized_vol": 1.0},
        {"symbol": "B", "weight": 0.20, "realized_vol": 1.0},
    ]
    scale = _vol_target_scale(positions, config=cfg)
    assert 0.0 < scale < 1.0


def test_vol_target_scale_grows_when_below_target_capped_at_max():
    cfg = MomentumFactorConfig(vol_target_annual=1.0, vol_target_max_scale=2.0, assumed_avg_correlation=0.5)
    # Low-vol positions, target much higher → scale would be > 2; clamps to 2
    positions = [
        {"symbol": "A", "weight": 0.10, "realized_vol": 0.10},
    ]
    scale = _vol_target_scale(positions, config=cfg)
    assert math.isclose(scale, cfg.vol_target_max_scale, rel_tol=1e-6)


def test_passes_entry_filters_blocks_high_carry() -> None:
    cfg = MomentumFactorConfig(max_carry=0.01)
    row = {"carry": 0.02, "_composite": 1.0, "_momentum_avg": 0.1, "ts_momentum": 0.1, "realized_vol": 1.0}
    from liquidity_migration.momentum_factor import _passes_entry_filters

    assert not _passes_entry_filters(row, cfg)
    row_ok = {**row, "carry": 0.005}
    assert _passes_entry_filters(row_ok, cfg)


def test_build_target_portfolio_long_only_picks_top_quantile():
    cfg = MomentumFactorConfig(
        mode=MODE_LONG_ONLY,
        long_quantile=0.20,
        sizing=SIZING_EQUAL,
        carry_weight=0.0,
        gross_exposure=1.0,
        max_position_weight=1.0,  # disable cap for this 2-position test
    )
    # 10 rows, top 20% = top 2
    rows = []
    for i in range(10):
        rows.append(
            {
                "symbol": f"S{i}",
                "in_universe": True,
                "momentum_7d": 0.01 * i,  # increasing momentum
                "momentum_14d": 0.01 * i,
                "momentum_28d": 0.01 * i,
                "ts_momentum": 0.05 * i,
                "carry": 0.0,
                "realized_vol": 0.5,
                "regime_on": True,
            }
        )
    positions = _build_target_portfolio(rows, config=cfg)
    longs = sorted(p["symbol"] for p in positions if p["side"] == "long")
    assert longs == ["S8", "S9"]
    # Gross exposure ≈ 1.0
    total = sum(p["weight"] for p in positions if p["side"] == "long")
    assert math.isclose(total, 1.0, rel_tol=1e-6)


def test_build_target_portfolio_long_short_splits_budget():
    cfg = MomentumFactorConfig(
        mode=MODE_LONG_SHORT,
        long_quantile=0.20,
        short_quantile=0.20,
        sizing=SIZING_EQUAL,
        carry_weight=0.0,
        gross_exposure=1.0,
        max_position_weight=1.0,
    )
    rows = []
    for i in range(10):
        rows.append(
            {
                "symbol": f"S{i}",
                "in_universe": True,
                "momentum_7d": 0.01 * i,
                "momentum_14d": 0.01 * i,
                "momentum_28d": 0.01 * i,
                "ts_momentum": 0.0,
                "carry": 0.0,
                "realized_vol": 0.5,
                "regime_on": True,
            }
        )
    positions = _build_target_portfolio(rows, config=cfg)
    long_total = sum(p["weight"] for p in positions if p["side"] == "long")
    short_total = sum(p["weight"] for p in positions if p["side"] == "short")
    assert math.isclose(long_total, 0.5, rel_tol=1e-6)
    assert math.isclose(short_total, 0.5, rel_tol=1e-6)


def test_build_target_portfolio_carry_penalizes_high_funding():
    cfg = MomentumFactorConfig(
        mode=MODE_LONG_ONLY,
        long_quantile=0.30,
        sizing=SIZING_EQUAL,
        carry_weight=2.0,  # strong carry penalty
        gross_exposure=1.0,
    )
    rows = []
    # Make A and B both have very high momentum, but B has much higher carry (pays more funding).
    # With strong carry penalty, A should win.
    for i, (s, m, c) in enumerate(
        [
            ("A", 0.10, 0.001),
            ("B", 0.10, 0.020),  # high funding
            ("C", 0.05, 0.001),
            ("D", 0.05, 0.001),
            ("E", 0.05, 0.001),
        ]
    ):
        rows.append(
            {
                "symbol": s,
                "in_universe": True,
                "momentum_7d": m,
                "momentum_14d": m,
                "momentum_28d": m,
                "ts_momentum": 0.0,
                "carry": c,
                "realized_vol": 0.5,
                "regime_on": True,
            }
        )
    positions = _build_target_portfolio(rows, config=cfg)
    longs = {p["symbol"] for p in positions if p["side"] == "long"}
    # A (low carry, high momentum) preferred over B (high carry, high momentum)
    assert "A" in longs
    assert "B" not in longs


def test_build_target_portfolio_ts_filter_drops_negative_momentum():
    cfg = MomentumFactorConfig(
        mode=MODE_LONG_ONLY,
        long_quantile=0.50,
        sizing=SIZING_EQUAL,
        carry_weight=0.0,
        require_positive_ts_momentum_for_longs=True,
        gross_exposure=1.0,
    )
    rows = []
    # All have high momentum, but only some have positive ts_momentum.
    for s, m, ts in [("A", 0.10, 0.05), ("B", 0.10, -0.05), ("C", 0.10, 0.05), ("D", 0.10, -0.05)]:
        rows.append(
            {
                "symbol": s,
                "in_universe": True,
                "momentum_7d": m,
                "momentum_14d": m,
                "momentum_28d": m,
                "ts_momentum": ts,
                "carry": 0.0,
                "realized_vol": 0.5,
                "regime_on": True,
            }
        )
    positions = _build_target_portfolio(rows, config=cfg)
    longs = {p["symbol"] for p in positions if p["side"] == "long"}
    # Only A and C have ts_momentum > 0; B and D dropped.
    assert "B" not in longs and "D" not in longs


def test_build_factor_features_smoke():
    n_days = 200
    symbols = ["BTCUSDT", "ETHUSDT", "AAAUSDT", "BBBUSDT"]
    klines = _make_klines_1h(
        symbols=symbols,
        n_days=n_days,
        log_trend_per_day=[0.001, 0.005, 0.010, -0.002],
        turnover_per_hour=[5_000_000.0, 3_000_000.0, 2_000_000.0, 1_000_000.0],
    )
    funding = _make_funding_8h(symbols, n_days)
    cfg = MomentumFactorConfig(
        universe_size=3,
        universe_volume_window_days=30,
        min_listing_history_days=30,
        momentum_lookbacks_days=(7, 14),
        ts_momentum_lookback_days=30,
        vol_estimate_window_days=20,
        carry_lookback_days=7,
        regime_sma_days=50,
    )
    features = build_factor_features(klines, funding=funding, config=cfg)
    required = {
        "ts_ms",
        "symbol",
        "close",
        "realized_vol",
        "momentum_7d",
        "momentum_14d",
        "ts_momentum",
        "turnover_median",
        "in_universe",
        "carry",
        "regime_on",
    }
    assert required.issubset(set(features.columns))


def test_evaluate_promotion_blocks_when_sharpe_below_threshold():
    cfg = MomentumFactorConfig(promotion_min_avg_sharpe=2.0)
    result = _evaluate_promotion(
        split_rows=[
            {"name": "a", "basket_count": 10, "total_return": 0.10, "sharpe_like": 1.0, "max_drawdown": -0.05},
        ],
        summary={"max_drawdown": -0.05},
        funding_mode="modeled",
        full_pit_universe_pass=True,
        config=cfg,
    )
    assert result["promotion_gate_pass"] is False
    assert "avg_split_sharpe_below_threshold" in result["promotion_reasons"]


def test_empty_factor_trades_schema():
    empty = _empty_factor_trades()
    assert {"trade_id", "side", "entry_price", "exit_price", "net_return", "momentum_avg"}.issubset(set(empty.columns))


def test_format_factor_report_smoke():
    metadata = {
        "run_label": "test",
        "config": {
            "mode": "long_only",
            "rebalance_days": 7,
            "momentum_lookbacks_days": [7, 14, 28],
            "momentum_skip_days": 1,
            "universe_size": 30,
            "universe_volume_window_days": 90,
            "long_quantile": 0.2,
            "short_quantile": 0.0,
            "sizing": "vol_parity",
            "gross_exposure": 1.0,
            "vol_target_annual": 0.0,
            "carry_weight": 0.5,
            "require_positive_ts_momentum_for_longs": False,
            "use_regime_filter": True,
            "regime_sma_days": 50,
            "regime_off_scale": 0.3,
            "cost_multiplier": 3.0,
        },
        "pit_manifest": {"full_pit_universe_pass": True, "feature_symbols": 100},
        "rows": {"features": 1000, "rebalances": 100, "trades": 500, "baskets": 500},
        "date_range": {"start": "2024-01-01", "end": "2024-12-31"},
        "summary": {
            "total_return": 0.50,
            "sharpe_like": 2.10,
            "max_drawdown": -0.12,
            "max_underwater_days": 60,
            "worst_30d_return": -0.05,
            "worst_60d_return": -0.08,
            "worst_90d_return": -0.10,
            "worst_120d_return": -0.10,
            "trade_win_rate": 0.55,
            "profit_factor": 1.5,
            "gross_return": 0.55,
            "cost_return": -0.03,
            "funding_return": -0.02,
            "long_return": 0.55,
            "short_return": 0.0,
            "funding_mode": "modeled",
        },
        "promotion": {
            "promotion_gate_pass": True,
            "all_splits_positive": True,
            "splits_with_baskets": 3,
            "avg_split_sharpe": 1.8,
            "funding_mode": "modeled",
            "promotion_reasons": [],
        },
        "splits": [{"name": "a", "basket_count": 5, "total_return": 0.1, "sharpe_like": 2.0, "max_drawdown": -0.05}],
        "lifecycle": {
            "rebalances_total": 100,
            "rebalances_regime_off": 5,
            "rebalances_with_positions": 95,
            "longs_opened": 500,
            "shorts_opened": 0,
            "skipped_no_entry_bar": 0,
            "skipped_no_exit_bar": 0,
        },
        "cost_model": {"effective_round_trip_cost_bps": 18.0},
    }
    report = format_factor_report(metadata)
    assert "Cross-Sectional Momentum Factor" in report
    assert "long_only" in report
    assert "Promotion gate" in report


def test_run_momentum_factor_research_smoke(tmp_path):
    n_days = 200
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AAAUSDT", "BBBUSDT"]
    klines = _make_klines_1h(
        symbols=symbols,
        n_days=n_days,
        log_trend_per_day=[0.001, 0.005, 0.010, -0.002, 0.003],
        turnover_per_hour=[5_000_000.0, 3_000_000.0, 2_000_000.0, 1_500_000.0, 1_000_000.0],
    )
    funding = _make_funding_8h(symbols, n_days)
    instruments = pl.DataFrame(
        [
            {
                "ts_ms": START_MS,
                "symbol": s,
                "category": "linear",
                "contract_type": "LinearPerpetual",
                "status": "Trading",
                "settle_coin": "USDT",
                "launch_time_ms": START_MS - 120 * 24 * MS_PER_HOUR,
                "tick_size": 0.01,
                "qty_step": 0.001,
                "min_order_qty": 0.001,
                "min_notional_value": 5.0,
                "funding_interval_min": 480,
                "is_prelisting": False,
            }
            for s in symbols
        ]
    )
    write_dataset(klines, tmp_path, "klines_1h")
    write_dataset(funding, tmp_path, "funding")
    write_dataset(instruments, tmp_path, "instruments")

    cfg = MomentumFactorConfig(
        universe_size=4,
        universe_volume_window_days=30,
        min_listing_history_days=30,
        momentum_lookbacks_days=(7, 14),
        ts_momentum_lookback_days=20,
        vol_estimate_window_days=20,
        carry_lookback_days=7,
        regime_sma_days=30,
        rebalance_days=7,
        long_quantile=0.50,
        sizing=SIZING_EQUAL,
        require_full_pit_universe=False,
    )
    result = run_momentum_factor_research(tmp_path, config=cfg)
    report_dir = Path(result["report_dir"])
    assert (report_dir / "momentum_factor_research_report.md").exists()
    payload = json.loads((report_dir / "momentum_factor_research_report.json").read_text())
    assert payload["rows"]["features"] > 0
    assert payload["rows"]["trades"] > 0  # weekly rebal × multiple coins should generate many trades
