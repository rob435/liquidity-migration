from __future__ import annotations

import polars as pl

from liquidity_migration.crowding import audit_crowding_model, classify_liquidity_migration_crowding


def test_crowding_classifier_separates_idio_market_theme_and_artifact(tmp_path) -> None:
    events = pl.DataFrame(
        [
            {
                "symbol": "IDIOUSDT",
                "entry_ts_ms": 1_700_000_000_000,
                "net_return": 0.02,
                "market_pct_up_1d": 0.45,
                "btc_return_1d": 0.01,
                "daily_return_1d": 0.14,
                "residual_return_1d": 0.11,
                "signal_day_last6h_turnover_share": 0.25,
                "signal_day_last6h_return": 0.02,
                "liquidity_migration_turnover_ratio": 8.0,
                "pit_age_days": 200.0,
            },
            {
                "symbol": "MKT1USDT",
                "entry_ts_ms": 1_700_010_000_000,
                "net_return": -0.01,
                "market_pct_up_1d": 0.82,
                "btc_return_1d": 0.04,
                "daily_return_1d": 0.09,
                "residual_return_1d": 0.04,
                "signal_day_last6h_turnover_share": 0.20,
                "signal_day_last6h_return": 0.01,
                "liquidity_migration_turnover_ratio": 6.0,
                "pit_age_days": 300.0,
            },
            {
                "symbol": "MKT2USDT",
                "entry_ts_ms": 1_700_010_100_000,
                "net_return": -0.02,
                "market_pct_up_1d": 0.80,
                "btc_return_1d": 0.04,
                "daily_return_1d": 0.08,
                "residual_return_1d": 0.03,
                "signal_day_last6h_turnover_share": 0.30,
                "signal_day_last6h_return": 0.02,
                "liquidity_migration_turnover_ratio": 7.0,
                "pit_age_days": 300.0,
            },
            {
                "symbol": "THEME1USDT",
                "entry_ts_ms": 1_700_020_000_000,
                "net_return": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.00,
                "daily_return_1d": 0.16,
                "residual_return_1d": 0.12,
                "signal_day_last6h_turnover_share": 0.40,
                "signal_day_last6h_return": 0.03,
                "liquidity_migration_turnover_ratio": 10.0,
                "pit_age_days": 400.0,
            },
            {
                "symbol": "THEME2USDT",
                "entry_ts_ms": 1_700_020_100_000,
                "net_return": 0.01,
                "market_pct_up_1d": 0.56,
                "btc_return_1d": 0.00,
                "daily_return_1d": 0.15,
                "residual_return_1d": 0.11,
                "signal_day_last6h_turnover_share": 0.42,
                "signal_day_last6h_return": 0.03,
                "liquidity_migration_turnover_ratio": 9.0,
                "pit_age_days": 400.0,
            },
            {
                "symbol": "ARTUSDT",
                "entry_ts_ms": 1_700_030_000_000,
                "net_return": -0.01,
                "market_pct_up_1d": 0.50,
                "btc_return_1d": 0.00,
                "daily_return_1d": 0.12,
                "residual_return_1d": 0.10,
                "signal_day_last6h_turnover_share": 0.95,
                "signal_day_last6h_return": 0.08,
                "liquidity_migration_turnover_ratio": 120.0,
                "pit_age_days": 300.0,
            },
        ]
    )

    classified = classify_liquidity_migration_crowding(events)
    classes = dict(zip(classified["symbol"].to_list(), classified["crowding_class"].to_list()))

    assert classes["IDIOUSDT"] == "isolated_idiosyncratic_event"
    assert classes["MKT1USDT"] == "full_market_impulse"
    assert classes["MKT2USDT"] == "full_market_impulse"
    assert classes["THEME1USDT"] == "sector_theme_wave"
    assert classes["THEME2USDT"] == "sector_theme_wave"
    assert classes["ARTUSDT"] == "exchange_liquidity_artifact"
    assert classified.filter(pl.col("crowding_tradeable")).height == 1

    payload = audit_crowding_model(events, output_dir=tmp_path)
    assert payload["status"] == "present"
    assert payload["non_tradeable_rows"] == 5
    assert (tmp_path / "crowding_model_trades.csv").exists()
    assert (tmp_path / "crowding_model_summary.csv").exists()
    assert (tmp_path / "crowding_model_report.md").exists()
