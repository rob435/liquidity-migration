from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "research" / "continuous_fade" / "continuous_fade_research.py"
    spec = importlib.util.spec_from_file_location("continuous_fade_research", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unit_short_path_exits_at_take_profit() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [h, 2 * h, 3 * h],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 89.0, 88.0],
            "close": [100.0, 91.0, 92.0],
        }
    )

    got = mod._simulate_unit_short_from_entry(
        bars,
        entry_ts_ms=h,
        entry_price=100.0,
        hold_hours=24,
        take_profit_pct=0.10,
    )

    assert got is not None
    assert got["exit_reason"] == "take_profit"
    assert got["exit_ts_ms"] == 2 * h
    assert got["unit_return"] == pytest.approx(0.10)
    assert got["time_to_first_profit_hours"] == pytest.approx(1.0)


def test_adverse_limit_timing_fills_at_limit_price() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [h, 2 * h, 3 * h],
            "high": [100.5, 101.5, 102.0],
            "low": [99.0, 98.0, 94.0],
            "close": [100.0, 100.5, 95.0],
        }
    )
    row = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "symbol": "AAAUSDT",
        "signal_ts_ms": 0,
        "entry_bar_end_ts_ms": h,
    }

    got = mod._simulate_timing_candidate(row, {"AAAUSDT": bars}, "adverse_1pct")

    assert got["filled"] is True
    assert got["entry_ts_ms"] == 2 * h
    assert got["unit_return"] == pytest.approx(1.0 - 95.0 / 101.0)


def test_delay_15m_timing_uses_5m_bar_close() -> None:
    mod = _load_module()
    m5 = mod.MS_PER_5M
    rows = []
    for idx in range(1, 292):
        ts = idx * m5
        rows.append(
            {
                "bar_end_ts_ms": ts,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 99.0 if ts == 3 * m5 else 90.0,
            }
        )
    bars = pl.DataFrame(rows)
    row = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "symbol": "AAAUSDT",
        "signal_ts_ms": 0,
        "entry_bar_end_ts_ms": 0,
    }

    got = mod._simulate_timing_candidate(
        row,
        {"AAAUSDT": bars},
        "delay_15m",
        dataset="klines_5m",
        interval_ms=mod.MS_PER_5M,
        require_complete_path=True,
    )

    assert got["filled"] is True
    assert got["entry_ts_ms"] == 3 * m5
    assert got["unit_return"] == pytest.approx(1.0 - 90.0 / 99.0)


def test_next_red_15m_fills_on_resampled_red_bar() -> None:
    mod = _load_module()
    m5 = mod.MS_PER_5M
    rows = []
    for idx in range(1, 292):
        ts = idx * m5
        rows.append(
            {
                "bar_end_ts_ms": ts,
                "open": 101.0 if idx == 1 else 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 99.0 if idx == 3 else 95.0,
            }
        )
    bars = pl.DataFrame(rows)
    row = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "symbol": "AAAUSDT",
        "signal_ts_ms": 0,
        "entry_bar_end_ts_ms": 0,
    }

    got = mod._simulate_timing_candidate(
        row,
        {"AAAUSDT": bars},
        "next_red_15m",
        dataset="klines_5m",
        interval_ms=mod.MS_PER_5M,
        require_complete_path=True,
    )

    assert got["filled"] is True
    assert got["entry_ts_ms"] == 3 * m5
    assert got["unit_return"] == pytest.approx(1.0 - 95.0 / 99.0)


def test_5m_timing_rejects_incomplete_post_entry_path() -> None:
    mod = _load_module()
    m5 = mod.MS_PER_5M
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [0, 3 * m5, 4 * m5, 6 * m5],
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 99.0, 98.0],
        }
    )
    row = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "symbol": "AAAUSDT",
        "signal_ts_ms": 0,
        "entry_bar_end_ts_ms": 0,
    }

    got = mod._simulate_timing_candidate(
        row,
        {"AAAUSDT": bars},
        "delay_15m",
        dataset="klines_5m",
        interval_ms=mod.MS_PER_5M,
        require_complete_path=True,
    )

    assert got["filled"] is False
    assert got["reason"] == "incomplete_path_tail"


def test_stop_frontier_simulates_short_stop_loss() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [h, 2 * h, 3 * h],
            "high": [101.0, 121.0, 95.0],
            "low": [99.0, 100.0, 89.0],
            "close": [100.0, 118.0, 90.0],
        }
    )
    row = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "symbol": "AAAUSDT",
        "entry_signal_ts_ms": 0,
        "entry_ts_ms": h,
        "exit_ts_ms": 3 * h,
        "entry_price": 100.0,
        "take_profit_price": 90.0,
        "notional_weight": 0.02,
        "component_weight": 0.5,
        "portfolio_cost_return": 0.0,
        "portfolio_funding_return": 0.0,
        "gross_trade_return": 0.10,
        "portfolio_net_return": 0.001,
    }

    got = mod._simulate_trade_with_stop(row, {"AAAUSDT": bars}, 0.20)

    assert got["stop_hit"] is True
    assert got["post_stop_original_tp_hit"] is True
    assert got["gross_trade_return"] == pytest.approx(-0.20)
    assert got["portfolio_net_return"] == pytest.approx(-0.002)


def test_timing_replay_transform_only_changes_timing_fields() -> None:
    mod = _load_module()
    cfg = mod.ContinuousEventConfig(entry_delay_hours=1, take_profit_pct=0.12)

    got = mod._timing_replay_transform(3, 0.01, 24)(cfg)

    assert cfg.entry_delay_hours == 1
    assert got.entry_delay_hours == 3
    assert got.entry_adverse_limit_pct == pytest.approx(0.01)
    assert got.entry_adverse_limit_wait_hours == 24
    assert got.take_profit_pct == pytest.approx(cfg.take_profit_pct)
    assert got.config_hash() != cfg.config_hash()


def test_stop_replay_transform_sets_fixed_stop_only() -> None:
    mod = _load_module()
    cfg = mod.ContinuousEventConfig(
        stop_loss_pct=0.0,
        stop_approach_frac=0.8,
        stop_vol_mult=4.0,
        take_profit_pct=0.12,
    )

    got = mod._stop_replay_transform(0.40)(cfg)

    assert cfg.stop_loss_pct == 0.0
    assert got.stop_loss_pct == pytest.approx(0.40)
    assert got.stop_approach_frac == 0.0
    assert got.stop_vol_mult == 0.0
    assert got.take_profit_pct == pytest.approx(cfg.take_profit_pct)
    assert got.config_hash() != cfg.config_hash()


def test_regime_replay_transform_sets_gate_and_lookback_only() -> None:
    mod = _load_module()
    cfg = mod.ContinuousEventConfig(
        btc_trend_gate="uptrend",
        btc_trend_lookback_days=30,
        take_profit_pct=0.12,
    )

    got = mod._regime_replay_transform("off", 20)(cfg)

    assert cfg.btc_trend_gate == "uptrend"
    assert cfg.btc_trend_lookback_days == 30
    assert got.btc_trend_gate == "off"
    assert got.btc_trend_lookback_days == 20
    assert got.take_profit_pct == pytest.approx(cfg.take_profit_pct)
    assert got.config_hash() != cfg.config_hash()


def test_skip_replay_transform_sets_external_size_skip_only() -> None:
    mod = _load_module()
    cfg = mod.ContinuousEventConfig(
        entry_skip_external_size_multiplier_lte=0.0,
        take_profit_pct=0.12,
    )

    got = mod._skip_replay_transform(0.35)(cfg)

    assert cfg.entry_skip_external_size_multiplier_lte == 0.0
    assert got.entry_skip_external_size_multiplier_lte == pytest.approx(0.35)
    assert got.take_profit_pct == pytest.approx(cfg.take_profit_pct)
    assert got.config_hash() != cfg.config_hash()


def test_skip_replay_transform_can_disable_btc_gate() -> None:
    mod = _load_module()
    cfg = mod.ContinuousEventConfig(
        btc_trend_gate="uptrend",
        btc_trend_lookback_days=30,
        entry_skip_external_size_multiplier_lte=0.0,
        take_profit_pct=0.12,
    )

    got = mod._skip_replay_transform(
        0.35,
        btc_trend_gate="off",
        btc_trend_lookback_days=30,
    )(cfg)

    assert cfg.btc_trend_gate == "uptrend"
    assert got.btc_trend_gate == "off"
    assert got.btc_trend_lookback_days == 30
    assert got.entry_skip_external_size_multiplier_lte == pytest.approx(0.35)
    assert got.take_profit_pct == pytest.approx(cfg.take_profit_pct)
    assert got.config_hash() != cfg.config_hash()


def test_regime_replay_complete_requires_every_variant_for_every_venue() -> None:
    mod = _load_module()
    complete_rows = [
        {"venue": venue, "variant": variant}
        for venue in ("bybit", "binance")
        for variant in mod.REGIME_PORTFOLIO_REPLAY_VARIANT_NAMES
    ]
    complete = pl.DataFrame(complete_rows)
    partial = complete.filter(
        ~((pl.col("venue") == "binance") & (pl.col("variant") == mod.REGIME_PORTFOLIO_REPLAY_VARIANT_NAMES[-1]))
    )

    assert mod._regime_replay_complete(complete, ["bybit", "binance"]) is True
    assert mod._regime_replay_complete(partial, ["bybit", "binance"]) is False


def test_skip_replay_complete_requires_every_variant_for_every_venue() -> None:
    mod = _load_module()
    complete_rows = [
        {"venue": venue, "variant": variant}
        for venue in ("bybit", "binance")
        for variant in mod.SKIP_PORTFOLIO_REPLAY_VARIANT_NAMES
    ]
    complete = pl.DataFrame(complete_rows)
    partial = complete.filter(
        ~((pl.col("venue") == "bybit") & (pl.col("variant") == "skip_btc_tail_035"))
    )

    assert mod._skip_replay_complete(complete, ["bybit", "binance"]) is True
    assert mod._skip_replay_complete(partial, ["bybit", "binance"]) is False


def test_synthetic_squeeze_survival_uses_active_book_and_equity_context(tmp_path: Path) -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    output_root = tmp_path / "run"
    (output_root / "tables").mkdir(parents=True)
    (output_root / "bybit").mkdir()
    pl.DataFrame(
        {
            "ts_ms": [0, h, 2 * h, 3 * h],
            "equity": [1.0, 1.02, 1.05, 1.04],
            "hedge_ratio_leg1": [0.0, 0.0, 0.0, 0.0],
            "hedge_ratio_leg2": [0.0, 0.0, 0.0, 0.0],
        }
    ).write_csv(output_root / "bybit" / "continuous_equity.csv")
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "symbol": "AAAUSDT",
                "entry_ts_ms": h,
                "exit_ts_ms": 3 * h,
                "notional_weight": 0.20,
                "component_weight": 1.0,
            },
            {
                "venue": "bybit",
                "symbol": "BBBUSDT",
                "entry_ts_ms": 2 * h,
                "exit_ts_ms": 3 * h,
                "notional_weight": 0.10,
                "component_weight": 1.0,
            },
        ]
    )

    artifacts = mod.write_synthetic_squeeze_survival_tables(output_root, trades)
    out = pl.read_csv(artifacts["synthetic_squeeze_survival"])
    worst_one = out.filter((pl.col("scenario") == "one_coin_100pct") & (pl.col("placement") == "worst_active")).row(
        0,
        named=True,
    )
    worst_three = out.filter(
        (pl.col("scenario") == "three_coins_50pct") & (pl.col("placement") == "worst_active")
    ).row(0, named=True)

    assert worst_one["event_ts_ms"] == h
    assert worst_one["net_loss_pct_equity"] == pytest.approx(0.20)
    assert worst_one["hit_symbols"] == "AAAUSDT"
    assert worst_one["post_event_drawdown_pct"] == pytest.approx((1.02 - 0.20) / 1.02 - 1.0)
    assert worst_three["event_ts_ms"] == 2 * h
    assert worst_three["net_loss_pct_equity"] == pytest.approx(0.15)
    assert worst_three["active_symbols"] == 2


def test_synthetic_squeeze_survival_credits_btc_eth_hedge_offset(tmp_path: Path) -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    output_root = tmp_path / "run"
    (output_root / "tables").mkdir(parents=True)
    (output_root / "bybit").mkdir()
    pl.DataFrame(
        {
            "ts_ms": [0, h],
            "equity": [1.0, 1.0],
            "hedge_ratio_leg1": [0.05, 0.05],
            "hedge_ratio_leg2": [0.03, 0.03],
        }
    ).write_csv(output_root / "bybit" / "continuous_equity.csv")
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "symbol": "AAAUSDT",
                "entry_ts_ms": h,
                "exit_ts_ms": 2 * h,
                "notional_weight": 0.20,
                "component_weight": 1.0,
            },
        ]
    )

    artifacts = mod.write_synthetic_squeeze_survival_tables(output_root, trades)
    out = pl.read_csv(artifacts["synthetic_squeeze_survival"])
    row = out.filter((pl.col("scenario") == "btc10_alts30") & (pl.col("placement") == "worst_active")).row(
        0,
        named=True,
    )

    assert row["short_loss_pct_equity"] == pytest.approx(0.06)
    assert row["hedge_offset_pct_equity"] == pytest.approx(0.008)
    assert row["net_loss_pct_equity"] == pytest.approx(0.052)


def test_cluster_risk_of_ruin_bootstrap_writes_tail_injected_scenarios(tmp_path: Path) -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    output_root = tmp_path / "run"
    (output_root / "tables").mkdir(parents=True)
    pl.DataFrame(
        [
            {
                "venue": "bybit",
                "scenario": "one_coin_100pct",
                "placement": "worst_active",
                "net_loss_pct_equity": 0.20,
            },
            {
                "venue": "bybit",
                "scenario": "exchange_down_1h_one_coin_100pct",
                "placement": "worst_active",
                "net_loss_pct_equity": 0.25,
            },
            {
                "venue": "bybit",
                "scenario": "one_coin_100pct",
                "placement": "p95_active",
                "net_loss_pct_equity": 0.10,
            },
        ]
    ).write_csv(output_root / "tables" / "synthetic_squeeze_survival.csv")
    trades = pl.DataFrame(
        [
            {"venue": "bybit", "entry_signal_ts_ms": 0, "portfolio_net_return": 0.02},
            {"venue": "bybit", "entry_signal_ts_ms": 0, "portfolio_net_return": -0.03},
            {"venue": "bybit", "entry_signal_ts_ms": h, "portfolio_net_return": 0.01},
            {"venue": "bybit", "entry_signal_ts_ms": 2 * h, "portfolio_net_return": -0.02},
            {"venue": "bybit", "entry_signal_ts_ms": 3 * h, "portfolio_net_return": 0.03},
        ]
    )

    artifacts = mod.write_cluster_risk_of_ruin_tables(output_root, trades, trials=300, seed=7)
    out = pl.read_csv(artifacts["cluster_risk_of_ruin"])
    base = out.filter(pl.col("scenario") == "cluster_bootstrap").row(0, named=True)
    injected = out.filter(pl.col("scenario") == "tail_injected_one_worst_100pct").row(0, named=True)
    three = out.filter(pl.col("scenario") == "tail_injected_three_p95_100pct").row(0, named=True)

    assert base["trials"] == 300
    assert base["clusters_per_path"] == 4
    assert base["observed_cluster_return_sum"] == pytest.approx(0.01)
    assert injected["tail_loss_pct_equity"] == pytest.approx(0.20)
    assert injected["prob_drawdown_20pct"] >= base["prob_drawdown_20pct"]
    assert three["tail_count"] == 3


def test_dynamic_tail_row_overlays_5m_path_and_slippage() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    m5 = mod.MS_PER_5M
    bars = pl.DataFrame(
        [
            {
                "bar_end_ts_ms": h + idx * m5,
                "high": 110.0 if idx == 6 else 105.0,
                "low": 99.0,
                "close": 100.0 if idx == 0 else 105.0,
            }
            for idx in range(13)
        ]
    )
    equity = pl.DataFrame(
        {
            "ts_ms": [0, h],
            "equity": [1.10, 1.00],
            "hedge_ratio_leg1": [0.0, 0.0],
            "hedge_ratio_leg2": [0.0, 0.0],
        }
    )
    survival_row = {
        "venue": "bybit",
        "scenario": "exchange_down_1h_one_coin_100pct",
        "placement": "worst_active",
        "event_ts_ms": h,
        "event_date": "2024-01-01",
        "active_positions": 1,
        "active_symbols": 1,
        "symbol_shock_pct": 1.0,
        "extra_slippage_pct": 0.10,
        "active_notional_pct_equity": 0.20,
        "hedge_offset_pct_equity": 0.02,
        "net_loss_pct_equity": 0.08,
    }
    scenario = mod._squeeze_scenario_by_name()["exchange_down_1h_one_coin_100pct"]

    row = mod._dynamic_tail_row(
        survival_row=survival_row,
        scenario=scenario,
        hit_exposures=[("AAAUSDT", 0.10, 1)],
        bars_by_symbol={"AAAUSDT": bars},
        equity=equity,
    )

    assert row is not None
    assert row["coverage_status"] == "ok"
    assert row["min_path_bars"] == 12
    assert row["max_symbol_peak_move_pct"] == pytest.approx(1.20)
    assert row["max_symbol_flatten_move_pct"] == pytest.approx(1.10)
    assert row["dynamic_peak_short_loss_pct_equity"] == pytest.approx(0.12)
    assert row["dynamic_flatten_short_loss_pct_equity"] == pytest.approx(0.11)
    assert row["dynamic_execution_loss_pct_equity"] == pytest.approx(0.01)
    assert row["dynamic_peak_net_loss_pct_equity"] == pytest.approx(0.10)
    assert row["dynamic_flatten_net_loss_pct_equity"] == pytest.approx(0.10)
    assert row["dynamic_peak_loss_increment_vs_static_pct_equity"] == pytest.approx(0.02)
    assert row["survives_equity_positive"] is True
    assert row["survives_account_maintenance_proxy"] is True
    assert row["positions_liquidated_account_level"] == 0


def test_disaster_sizing_tables_compare_current_to_loss_budget(tmp_path: Path) -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    output_root = tmp_path / "run"
    (output_root / "tables").mkdir(parents=True)
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "entry_signal_ts_ms": h,
                "signal_id": "a",
                "notional_weight": 0.002,
                "component_weight": 1.0,
                "portfolio_net_return": 0.01,
                "adverse_excursion_pct": 0.10,
            },
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "BBBUSDT",
                "entry_signal_ts_ms": 2 * h,
                "signal_id": "b",
                "notional_weight": 0.004,
                "component_weight": 1.0,
                "portfolio_net_return": -0.02,
                "adverse_excursion_pct": 0.60,
            },
        ]
    )

    artifacts = mod.write_disaster_sizing_tables(output_root, trades)
    summary = pl.read_csv(artifacts["disaster_sizing_summary"])
    fixed = summary.filter(
        (pl.col("scenario") == "fixed_100pct") & (pl.col("trade_loss_budget_pct_equity") == 0.001)
    ).row(0, named=True)
    winner_floor = summary.filter(
        (pl.col("scenario") == "winner_mae_p95_floor_50pct")
        & (pl.col("trade_loss_budget_pct_equity") == 0.001)
    ).row(0, named=True)

    assert fixed["safe_notional_pct_equity"] == pytest.approx(0.001)
    assert fixed["pct_trades_over_budget"] == pytest.approx(1.0)
    assert fixed["median_current_to_safe_notional"] == pytest.approx(3.0)
    assert fixed["max_current_to_safe_notional"] == pytest.approx(4.0)
    assert winner_floor["catastrophic_move_pct"] == pytest.approx(0.50)
    assert winner_floor["safe_notional_pct_equity"] == pytest.approx(0.002)
    assert winner_floor["pct_trades_over_budget"] == pytest.approx(0.5)


def test_conditional_scale_in_trade_row_models_threshold_addon() -> None:
    mod = _load_module()
    row = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "signal_id": "s1",
        "symbol": "AAAUSDT",
        "entry_signal_ts_ms": 1,
        "entry_price": 100.0,
        "exit_price": 90.0,
        "adverse_excursion_pct": 0.10,
        "notional_weight": 0.02,
        "component_weight": 0.5,
        "portfolio_net_return": 0.001,
    }

    got = mod._conditional_scale_in_trade_row(row, trigger_mae_pct=0.05, addon_fraction=0.5)

    assert got["filled"] is True
    assert got["primary_notional_pct_equity"] == pytest.approx(0.01)
    assert got["addon_notional_pct_equity"] == pytest.approx(0.005)
    assert got["addon_entry_price"] == pytest.approx(105.0)
    assert got["addon_gross_return"] == pytest.approx(1.0 - 90.0 / 105.0)
    assert got["addon_cost_return"] == pytest.approx(-0.005 * 15.0 / 10_000.0)
    assert got["combined_portfolio_net_return"] == pytest.approx(0.001 + got["addon_net_return"])


def test_scale_in_child_trade_row_fills_trigger_and_exits_next_bar_tp() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    cfg = mod.ContinuousEventConfig(flat_round_trip_bps=10.0, take_profit_pct=0.12)
    parent = {
        "trade_id": "parent",
        "basket_id": "basket",
        "entry_signal_ts_ms": 0,
        "entry_ts_ms": h,
        "exit_ts_ms": 5 * h,
        "symbol": "AAAUSDT",
        "side": "short",
        "entry_price": 100.0,
        "exit_price": 90.0,
        "notional_weight": 0.02,
        "score": 1.0,
        "rank": 9,
    }
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [h, 2 * h, 3 * h, 4 * h],
            "high": [101.0, 106.0, 104.0, 103.0],
            "low": [99.0, 100.0, 92.0, 91.0],
            "close": [100.0, 104.0, 93.0, 92.0],
            "turnover_quote": [1_000_000.0] * 4,
        }
    )

    got = mod._scale_in_child_trade_row(
        parent,
        bars,
        cfg,
        variant="mae05_add50",
        trigger_mae_pct=0.05,
        addon_fraction=0.50,
        funding_lookup=None,
    )

    assert got is not None
    assert got["entry_ts_ms"] == 2 * h
    assert got["entry_price"] == pytest.approx(105.0)
    assert got["exit_ts_ms"] == 3 * h
    assert got["exit_reason"] == "scale_in_take_profit"
    assert got["notional_weight"] == pytest.approx(0.01)
    assert got["gross_trade_return"] == pytest.approx(0.12)
    assert got["cost_return"] == pytest.approx(-0.01 * 10.0 / 10_000.0)
    assert got["net_return"] == pytest.approx(got["gross_return"] + got["cost_return"])


def test_scale_in_child_trade_row_does_not_take_profit_on_fill_bar() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    cfg = mod.ContinuousEventConfig(flat_round_trip_bps=10.0, take_profit_pct=0.12)
    parent = {
        "trade_id": "parent",
        "basket_id": "basket",
        "entry_signal_ts_ms": 0,
        "entry_ts_ms": h,
        "exit_ts_ms": 4 * h,
        "symbol": "AAAUSDT",
        "side": "short",
        "entry_price": 100.0,
        "exit_price": 101.0,
        "notional_weight": 0.02,
    }
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [h, 2 * h, 3 * h, 4 * h],
            "high": [101.0, 106.0, 104.0, 103.0],
            "low": [99.0, 90.0, 100.0, 100.0],
            "close": [100.0, 104.0, 102.0, 101.0],
            "turnover_quote": [1_000_000.0] * 4,
        }
    )

    got = mod._scale_in_child_trade_row(
        parent,
        bars,
        cfg,
        variant="mae05_add50",
        trigger_mae_pct=0.05,
        addon_fraction=0.50,
        funding_lookup=None,
    )

    assert got is not None
    assert got["entry_ts_ms"] == 2 * h
    assert got["exit_ts_ms"] == 4 * h
    assert got["exit_reason"] == "scale_in_parent_exit"
    assert got["exit_price"] == pytest.approx(101.0)
    assert got["gross_trade_return"] == pytest.approx(1.0 - 101.0 / 105.0)


def test_conditional_scale_in_tables_write_by_trade_and_summary(tmp_path: Path) -> None:
    mod = _load_module()
    output_root = tmp_path / "run"
    (output_root / "tables").mkdir(parents=True)
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "signal_id": "s1",
                "symbol": "AAAUSDT",
                "entry_signal_ts_ms": 1,
                "entry_price": 100.0,
                "exit_price": 90.0,
                "adverse_excursion_pct": 0.10,
                "notional_weight": 0.02,
                "component_weight": 0.5,
                "portfolio_net_return": 0.001,
            },
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "signal_id": "s2",
                "symbol": "BBBUSDT",
                "entry_signal_ts_ms": 2,
                "entry_price": 100.0,
                "exit_price": 101.0,
                "adverse_excursion_pct": 0.01,
                "notional_weight": 0.02,
                "component_weight": 0.5,
                "portfolio_net_return": -0.0001,
            },
        ]
    )

    artifacts = mod.write_conditional_scale_in_tables(output_root, trades)
    by_trade = pl.read_csv(artifacts["conditional_scale_in_by_trade"])
    summary = pl.read_csv(artifacts["conditional_scale_in_summary"])
    row = summary.filter(
        (pl.col("venue") == "bybit")
        & (pl.col("trigger_mae_pct") == 0.05)
        & (pl.col("addon_fraction_of_primary") == 0.5)
    ).row(0, named=True)

    assert by_trade.height == len(mod.SCALE_IN_TRIGGERS) * len(mod.SCALE_IN_FRACTIONS) * 2
    assert row["trades"] == 2
    assert row["fills"] == 1
    assert row["fill_rate"] == pytest.approx(0.5)
    assert row["primary_net_return"] == pytest.approx(0.0009)
    assert row["combined_net_return"] > row["primary_net_return"]


def test_signal_invalidation_trade_row_exits_losing_short_on_candidate_pressure() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    trade = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "signal_id": "s1",
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "entry_signal_ts_ms": 0,
        "entry_ts_ms": h,
        "exit_ts_ms": 10 * h,
        "hold_hours": 9.0,
        "entry_price": 100.0,
        "exit_price": 90.0,
        "notional_weight": 0.02,
        "component_weight": 0.5,
        "portfolio_gross_return": 0.001,
        "portfolio_cost_return": -0.00005,
        "portfolio_funding_return": 0.00009,
        "portfolio_net_return": 0.00104,
    }
    signals = [
        {
            "signal_ts_ms": 3 * h,
            "order_submit_ts_ms": 4 * h,
            "reason": "cooldown",
            "component_score": 0.96,
            "volume_zscore": 2.0,
        }
    ]
    bars = pl.DataFrame(
        {
            "bar_end_ts_ms": [4 * h, 5 * h],
            "close": [110.0, 108.0],
        }
    )

    got = mod._signal_invalidation_trade_row(
        trade,
        signals,
        {"AAAUSDT": bars},
        mod.SIGNAL_INVALIDATION_RULES[0],
    )

    assert got["invalidated"] is True
    assert got["invalidation_reason"] == "cooldown"
    assert got["invalidation_fill_ts_ms"] == 4 * h
    assert got["invalidation_unrealized_return"] == pytest.approx(-0.10)
    assert got["scenario_portfolio_gross_return"] == pytest.approx(-0.001)
    assert got["scenario_funding_return"] == pytest.approx(0.00009 * 3.0 / 9.0)
    assert got["scenario_portfolio_net_return"] < got["original_portfolio_net_return"]


def test_signal_invalidation_trade_row_ignores_candidate_when_short_not_losing() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    trade = {
        "venue": "bybit",
        "component_id": "turn3p3",
        "signal_id": "s1",
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "entry_signal_ts_ms": 0,
        "entry_ts_ms": h,
        "exit_ts_ms": 10 * h,
        "entry_price": 100.0,
        "exit_price": 90.0,
        "notional_weight": 0.02,
        "component_weight": 0.5,
        "portfolio_gross_return": 0.001,
        "portfolio_cost_return": 0.0,
        "portfolio_funding_return": 0.0,
        "portfolio_net_return": 0.001,
    }
    signals = [
        {
            "signal_ts_ms": 3 * h,
            "order_submit_ts_ms": 4 * h,
            "reason": "cooldown",
            "component_score": 1.0,
            "volume_zscore": 5.0,
        }
    ]
    bars = pl.DataFrame({"bar_end_ts_ms": [4 * h], "close": [95.0]})

    got = mod._signal_invalidation_trade_row(
        trade,
        signals,
        {"AAAUSDT": bars},
        mod.SIGNAL_INVALIDATION_RULES[0],
    )

    assert got["invalidated"] is False
    assert got["scenario_portfolio_net_return"] == pytest.approx(got["original_portfolio_net_return"])


def test_signal_invalidation_tables_write_by_trade_and_summary(tmp_path: Path) -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    output_root = tmp_path / "run"
    (output_root / "tables").mkdir(parents=True)
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "signal_id": "s1",
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "entry_signal_ts_ms": 0,
                "entry_ts_ms": h,
                "exit_ts_ms": 10 * h,
                "hold_hours": 9.0,
                "entry_price": 100.0,
                "exit_price": 90.0,
                "notional_weight": 0.02,
                "component_weight": 0.5,
                "portfolio_gross_return": 0.001,
                "portfolio_cost_return": 0.0,
                "portfolio_funding_return": 0.0,
                "portfolio_net_return": 0.001,
            },
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "signal_id": "s2",
                "trade_id": "t2",
                "symbol": "BBBUSDT",
                "side": "short",
                "entry_signal_ts_ms": 0,
                "entry_ts_ms": h,
                "exit_ts_ms": 10 * h,
                "hold_hours": 9.0,
                "entry_price": 100.0,
                "exit_price": 90.0,
                "notional_weight": 0.02,
                "component_weight": 0.5,
                "portfolio_gross_return": 0.001,
                "portfolio_cost_return": 0.0,
                "portfolio_funding_return": 0.0,
                "portfolio_net_return": 0.001,
            },
        ]
    )
    candidates = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "signal_ts_ms": 3 * h,
                "order_submit_ts_ms": 4 * h,
                "reason": "cooldown",
                "selected": False,
                "component_score": 0.96,
                "volume_zscore": 2.0,
            },
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "BBBUSDT",
                "signal_ts_ms": 3 * h,
                "order_submit_ts_ms": 4 * h,
                "reason": "cooldown",
                "selected": False,
                "component_score": 0.96,
                "volume_zscore": 2.0,
            },
        ]
    )
    bars = {
        "AAAUSDT": pl.DataFrame({"bar_end_ts_ms": [4 * h], "close": [110.0]}),
        "BBBUSDT": pl.DataFrame({"bar_end_ts_ms": [4 * h], "close": [95.0]}),
    }
    mod.venue_root = lambda venue: Path(".")
    mod._load_klines_for_rows = lambda root, rows, sparse_windows=False: bars

    artifacts = mod.write_signal_invalidation_tables(output_root, trades, candidates)
    by_trade = pl.read_csv(artifacts["signal_invalidation_by_trade"])
    summary = pl.read_csv(artifacts["signal_invalidation_summary"])
    row = summary.filter(
        (pl.col("venue") == "bybit") & (pl.col("rule") == "candidate_pressure_3h_score95")
    ).row(0, named=True)

    assert by_trade.height == len(mod.SIGNAL_INVALIDATION_RULES) * 2
    assert row["trades"] == 2
    assert row["invalidations"] == 1
    assert row["invalidation_rate"] == pytest.approx(0.5)
    assert row["scenario_net_return"] < row["original_net_return"]


def test_hourly_state_grid_builds_open_trade_hours() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "entry_ts_ms": h,
                "exit_ts_ms": 4 * h,
                "entry_price": 100.0,
                "trade_id": "trade-1",
                "side": "short",
            }
        ]
    )

    got = mod._hourly_trade_state_grid(trades)

    assert got["state_ts_ms"].to_list() == [2 * h, 3 * h, 4 * h]
    assert got["state_age_hours"].to_list() == [1.0, 2.0, 3.0]
    assert got["trade_id"].to_list() == ["trade-1", "trade-1", "trade-1"]


def test_signal_invalidation_state_panel_joins_candidate_and_causal_state() -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "entry_ts_ms": h,
                "exit_ts_ms": 4 * h,
                "entry_price": 100.0,
                "portfolio_net_return": -0.05,
            }
        ]
    )
    candidates = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "signal_ts_ms": 3 * h,
                "reason": "cooldown",
                "selected": False,
                "component_score": 0.97,
            }
        ]
    )
    price_state = pl.DataFrame(
        {
            "symbol": ["AAAUSDT", "AAAUSDT", "AAAUSDT"],
            "state_ts_ms": [2 * h, 3 * h, 4 * h],
            "close": [105.0, 110.0, 95.0],
            "high": [106.0, 112.0, 96.0],
            "low": [104.0, 108.0, 94.0],
        }
    )
    oi_state = pl.DataFrame(
        {"symbol": ["AAAUSDT", "AAAUSDT"], "ts_ms": [2 * h, 3 * h], "open_interest": [10.0, 11.0]}
    )
    funding_state = pl.DataFrame({"symbol": ["AAAUSDT"], "ts_ms": [2 * h], "funding_rate": [0.0001]})
    btc_state = pl.DataFrame({"state_ts_ms": [2 * h, 3 * h, 4 * h], "btc_close": [50000.0, 50100.0, 49900.0]})

    got = mod._build_signal_invalidation_state_panel_for_venue(
        trades,
        candidates,
        price_state=price_state,
        open_interest_state=oi_state,
        funding_state=funding_state,
        btc_state=btc_state,
    )
    row = got.filter(pl.col("state_ts_ms") == 3 * h).row(0, named=True)

    assert got.height == 3
    assert row["candidate_state_available"] is True
    assert row["candidate_reason"] == "cooldown"
    assert row["unrealized_return"] == pytest.approx(-0.10)
    assert row["open_interest_available"] is True
    assert row["funding_available"] is True
    assert row["btc_state_available"] is True
    assert row["spread_depth_available"] is False
    assert row["sector_proxy_available"] is False


def test_signal_invalidation_state_panel_writer_summarizes_missing_full_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    h = mod.MS_PER_HOUR
    output_root = tmp_path
    trades = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "entry_ts_ms": h,
                "exit_ts_ms": 4 * h,
                "entry_price": 100.0,
            }
        ]
    )
    candidates = pl.DataFrame(
        [
            {
                "venue": "bybit",
                "component_id": "turn3p3",
                "symbol": "AAAUSDT",
                "signal_ts_ms": 3 * h,
                "reason": "cooldown",
                "selected": False,
                "component_score": 0.97,
            }
        ]
    )
    bars = {
        "AAAUSDT": pl.DataFrame(
            {
                "bar_end_ts_ms": [2 * h, 3 * h, 4 * h],
                "close": [105.0, 110.0, 95.0],
                "high": [106.0, 112.0, 96.0],
                "low": [104.0, 108.0, 94.0],
            }
        )
    }

    def fake_state_dataset(
        root: Path,
        dataset: str,
        rows: pl.DataFrame,
        *,
        value_cols: list[str],
        pad_start_ms: int = 0,
        pad_end_ms: int = 0,
    ) -> pl.DataFrame:
        del root, rows, value_cols, pad_start_ms, pad_end_ms
        if dataset == "open_interest":
            return pl.DataFrame(
                {"symbol": ["AAAUSDT", "AAAUSDT"], "ts_ms": [2 * h, 3 * h], "open_interest": [10.0, 11.0]}
            )
        if dataset == "funding":
            return pl.DataFrame({"symbol": ["AAAUSDT"], "ts_ms": [2 * h], "funding_rate": [0.0001]})
        return pl.DataFrame()

    monkeypatch.setattr(mod, "venue_root", lambda venue: Path("."))
    monkeypatch.setattr(mod, "_load_klines_for_rows", lambda root, rows, sparse_windows=False: bars)
    monkeypatch.setattr(mod, "_load_state_dataset_for_rows", fake_state_dataset)
    monkeypatch.setattr(
        mod,
        "_load_btc_state_for_rows",
        lambda root, rows: pl.DataFrame(
            {"state_ts_ms": [2 * h, 3 * h, 4 * h], "btc_close": [50000.0, 50100.0, 49900.0]}
        ),
    )

    artifacts = mod.write_signal_invalidation_state_panel(output_root, trades, candidates, ["bybit"])
    panel = pl.read_parquet(artifacts["signal_invalidation_hourly_state_panel"])
    summary = pl.read_csv(artifacts["signal_invalidation_state_panel_summary"])
    row = summary.row(0, named=True)

    assert panel.height == 3
    assert row["state_rows"] == 3
    assert row["candidate_state_rows"] == 1
    assert row["candidate_state_coverage"] == pytest.approx(1 / 3)
    assert row["price_coverage"] == pytest.approx(1.0)
    assert row["open_interest_coverage"] == pytest.approx(1.0)
    assert row["funding_coverage"] == pytest.approx(1.0)
    assert row["btc_state_coverage"] == pytest.approx(1.0)
    assert row["spread_depth_coverage"] == pytest.approx(0.0)
    assert row["sector_proxy_coverage"] == pytest.approx(0.0)
    assert row["full_hourly_state_panel_ready"] is False


def test_overfit_diagnostics_write_dsr_and_pbo_tables(tmp_path: Path) -> None:
    mod = _load_module()
    output_root = tmp_path / "run"
    tables = output_root / "tables"
    tables.mkdir(parents=True)

    baseline_dir = output_root / "baseline" / "bybit"
    variant_dir = output_root / "variant" / "bybit"
    baseline_dir.mkdir(parents=True)
    variant_dir.mkdir(parents=True)
    dates = [f"2024-01-{idx + 1:02d}" for idx in range(24)]
    baseline_returns = [0.0010, 0.0004, 0.0008, -0.0001] * 6
    variant_returns = [0.0004, -0.0002, 0.0002, -0.0003] * 6
    pl.DataFrame({"date": dates, "basket_return": baseline_returns}).write_csv(
        baseline_dir / "continuous_equity.csv"
    )
    pl.DataFrame({"date": dates, "basket_return": variant_returns}).write_csv(
        variant_dir / "continuous_equity.csv"
    )
    pl.DataFrame(
        [
            {
                "variant": "baseline_current",
                "variant_kind": "baseline",
                "venue": "bybit",
                "component_trades": 10,
                "full_return_pct": 2.0,
                "max_drawdown_pct": -0.5,
                "mar": 4.0,
                "sharpe_daily_ann": 5.0,
                "worst_day_pct": -0.1,
                "summary_path": str(baseline_dir / "continuous_equity_summary.json"),
                "artifact_root": str(output_root / "baseline"),
            },
            {
                "variant": "delay_plus_1h",
                "variant_kind": "delay",
                "venue": "bybit",
                "component_trades": 10,
                "full_return_pct": 1.0,
                "max_drawdown_pct": -0.8,
                "mar": 1.5,
                "sharpe_daily_ann": 1.0,
                "worst_day_pct": -0.3,
                "summary_path": str(variant_dir / "continuous_equity_summary.json"),
                "artifact_root": str(output_root / "variant"),
            },
        ],
        infer_schema_length=None,
    ).write_csv(tables / "timing_portfolio_replay.csv")

    artifacts = mod.write_overfit_diagnostics(output_root, ["bybit"])

    assert set(artifacts) == {
        "overfit_variant_universe",
        "deflated_sharpe",
        "pbo_cscv_summary",
        "pbo_cscv_splits",
    }
    variants = pl.read_csv(artifacts["overfit_variant_universe"])
    dsr = pl.read_csv(artifacts["deflated_sharpe"])
    pbo = pl.read_csv(artifacts["pbo_cscv_summary"])
    splits = pl.read_csv(artifacts["pbo_cscv_splits"])

    assert variants.height == 2
    assert set(variants["variant"].to_list()) == {"baseline_current", "delay_plus_1h"}
    baseline = dsr.filter(pl.col("variant") == "baseline_current").row(0, named=True)
    assert baseline["is_baseline"] is True
    assert baseline["trial_count"] == 2
    assert baseline["dsr_probability"] is not None
    assert pbo.row(0, named=True)["splits"] == 70
    assert 0.0 <= pbo.row(0, named=True)["pbo"] <= 1.0
    assert splits.height == 70
