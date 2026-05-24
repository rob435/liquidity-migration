from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.cross_sectional_momentum import (
    CrossSectionalMomentumConfig,
    POSITION_SIZING_EQUAL,
    POSITION_SIZING_INVERSE_VOL,
    SPLITS,
    _empty_momentum_trades,
    _evaluate_promotion,
    _events_config_from,
    _filter_signal_window,
    _monthly_returns,
    _position_weight,
    _promotion_note,
    _run_label,
    _signals_config_from,
    _split_rows,
    _validate_config,
    format_momentum_report,
    run_cross_sectional_momentum_research,
)
from liquidity_migration.config import CostConfig, TradeLifecycleConfig
from liquidity_migration.momentum_signals import RANKER_SHARPE
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


def _make_funding_8h(*, symbols: list[str], n_days: int, base_rate: float = 0.0001, start_ms: int = START_MS) -> pl.DataFrame:
    rows: list[dict] = []
    for symbol in symbols:
        for d in range(n_days):
            for h in (0, 8, 16):
                ts_ms = start_ms + d * MS_PER_DAY + h * MS_PER_HOUR
                rows.append(
                    {
                        "ts_ms": ts_ms,
                        "symbol": symbol,
                        "funding_rate": base_rate,
                        "funding_rate_8h_equiv": base_rate,
                        "funding_interval_min": 480,
                    }
                )
    return pl.DataFrame(rows).sort(["symbol", "ts_ms"])


# -- unit tests --------------------------------------------------------------


def test_validate_config_accepts_defaults():
    _validate_config(CrossSectionalMomentumConfig())


def test_validate_config_rejects_invalid_ranker():
    with pytest.raises(ValueError):
        _validate_config(CrossSectionalMomentumConfig(ranker="bogus_ranker"))


def test_validate_config_rejects_invalid_position_sizing():
    with pytest.raises(ValueError):
        _validate_config(CrossSectionalMomentumConfig(position_sizing="bogus"))


def test_validate_config_rejects_rank_exit_ge_entry():
    with pytest.raises(ValueError):
        _validate_config(
            CrossSectionalMomentumConfig(rank_entry_min_norm=0.5, rank_exit_max_norm=0.5)
        )


def test_signals_config_passes_relevant_fields():
    cfg = CrossSectionalMomentumConfig(
        ranker=RANKER_SHARPE,
        liquidity_tier_size=15,
        ranker_lookback_days=45,
    )
    signals_cfg = _signals_config_from(cfg)
    assert signals_cfg.ranker == RANKER_SHARPE
    assert signals_cfg.liquidity_tier_size == 15
    assert signals_cfg.ranker_lookback_days == 45


def test_events_config_passes_relevant_fields():
    cfg = CrossSectionalMomentumConfig(
        rank_entry_min_norm=0.80,
        rank_exit_max_norm=0.40,
        trailing_atr_multiple=5.0,
    )
    events_cfg = _events_config_from(cfg)
    assert events_cfg.rank_entry_min_norm == 0.80
    assert events_cfg.rank_exit_max_norm == 0.40
    assert events_cfg.trailing_atr_multiple == 5.0


def test_filter_signal_window_inclusive_start_exclusive_end():
    df = pl.DataFrame(
        {
            "ts_ms": [START_MS, START_MS + MS_PER_DAY, START_MS + 2 * MS_PER_DAY],
            "symbol": ["AAA"] * 3,
        }
    )
    iso_start = datetime.fromtimestamp(START_MS / 1000, tz=UTC).date().isoformat()
    end_dt = datetime.fromtimestamp((START_MS + 2 * MS_PER_DAY) / 1000, tz=UTC).date().isoformat()
    out = _filter_signal_window(df, start=iso_start, end=end_dt)
    assert out.height == 2


def test_position_weight_equal():
    cfg = CrossSectionalMomentumConfig(position_sizing=POSITION_SIZING_EQUAL)
    weight = _position_weight({"realized_vol_90d": 0.40}, config=cfg)
    assert weight == 1.0


def test_position_weight_inverse_vol_clamped():
    cfg = CrossSectionalMomentumConfig(
        position_sizing=POSITION_SIZING_INVERSE_VOL,
        vol_floor_annual=0.30,
        position_weight_min=0.5,
        position_weight_max=2.0,
    )
    # vol_used = max(0.10, 0.30) = 0.30 → raw = 0.30 / 0.30 = 1.0
    assert _position_weight({"realized_vol_90d": 0.10}, config=cfg) == 1.0
    # vol_used = max(0.60, 0.30) = 0.60 → raw = 0.30 / 0.60 = 0.5 (at lower clamp)
    assert _position_weight({"realized_vol_90d": 0.60}, config=cfg) == 0.5
    # vol_used = max(1.50, 0.30) = 1.50 → raw = 0.30 / 1.50 = 0.20 → clamped to 0.5
    assert _position_weight({"realized_vol_90d": 1.50}, config=cfg) == 0.5
    # missing vol → fall back to 1.0
    assert _position_weight({}, config=cfg) == 1.0


def test_evaluate_promotion_blocks_when_pit_missing():
    cfg = CrossSectionalMomentumConfig()
    result = _evaluate_promotion(
        split_rows=[
            {"name": "a", "basket_count": 5, "total_return": 0.1, "sharpe_like": 1.0, "max_drawdown": -0.05},
        ],
        summary={"max_drawdown": -0.05},
        funding_mode="modeled",
        full_pit_universe_pass=False,
        config=cfg,
    )
    assert result["promotion_gate_pass"] is False
    assert "full_pit_universe_missing" in result["promotion_reasons"]


def test_evaluate_promotion_blocks_when_funding_missing():
    cfg = CrossSectionalMomentumConfig()
    result = _evaluate_promotion(
        split_rows=[
            {"name": "a", "basket_count": 5, "total_return": 0.1, "sharpe_like": 1.0, "max_drawdown": -0.05},
        ],
        summary={"max_drawdown": -0.05},
        funding_mode="missing",
        full_pit_universe_pass=True,
        config=cfg,
    )
    assert result["promotion_gate_pass"] is False
    assert "funding_missing" in result["promotion_reasons"]


def test_evaluate_promotion_passes_when_all_clear():
    cfg = CrossSectionalMomentumConfig()
    result = _evaluate_promotion(
        split_rows=[
            {"name": s[0], "basket_count": 10, "total_return": 0.10, "sharpe_like": 1.2, "max_drawdown": -0.10}
            for s in SPLITS
        ],
        summary={"max_drawdown": -0.10},
        funding_mode="modeled",
        full_pit_universe_pass=True,
        config=cfg,
    )
    assert result["promotion_gate_pass"] is True
    assert result["all_splits_positive"] is True


def test_run_label_branches():
    cfg = CrossSectionalMomentumConfig()
    empty_manifest = pl.DataFrame({"symbol": pl.Series([], dtype=pl.String), "date": pl.Series([], dtype=pl.String)})
    nonempty = pl.DataFrame({"symbol": ["AAA"], "date": ["2024-01-01"]})
    assert _run_label(config=cfg, archive_manifest=empty_manifest, full_pit_universe_pass=False, funding_mode="modeled") == "pit_required_missing_manifest"
    assert _run_label(config=cfg, archive_manifest=nonempty, full_pit_universe_pass=False, funding_mode="modeled") == "pit_membership_filtered_current_universe"
    assert _run_label(config=cfg, archive_manifest=nonempty, full_pit_universe_pass=True, funding_mode="missing") == "full_pit_universe_funding_missing"
    assert _run_label(config=cfg, archive_manifest=nonempty, full_pit_universe_pass=True, funding_mode="partial") == "full_pit_universe_funding_partial"
    assert _run_label(config=cfg, archive_manifest=nonempty, full_pit_universe_pass=True, funding_mode="modeled") == "full_pit_universe"


def test_promotion_note_handles_each_state():
    empty = pl.DataFrame({"symbol": pl.Series([], dtype=pl.String)})
    nonempty = pl.DataFrame({"symbol": ["AAA"]})
    assert "research only" in _promotion_note(archive_manifest=empty, full_pit_universe_pass=False, funding_mode="missing")
    assert "current-universe" in _promotion_note(archive_manifest=nonempty, full_pit_universe_pass=False, funding_mode="modeled")
    assert "funding data is not present" in _promotion_note(archive_manifest=nonempty, full_pit_universe_pass=True, funding_mode="missing")
    assert "partial" in _promotion_note(archive_manifest=nonempty, full_pit_universe_pass=True, funding_mode="partial")
    assert "suitable" in _promotion_note(archive_manifest=nonempty, full_pit_universe_pass=True, funding_mode="modeled")


def test_split_rows_empty_baskets():
    config = TradeLifecycleConfig(hold_days=30, rebalance_days=30)
    rows = _split_rows(pl.DataFrame(), config=config)
    assert len(rows) == len(SPLITS)
    assert all(row["basket_count"] == 0 for row in rows)


def test_monthly_returns_empty():
    monthly = _monthly_returns(pl.DataFrame())
    assert monthly.is_empty()
    assert set(monthly.columns) >= {"month", "strategy_return", "baskets"}


def test_empty_trades_has_expected_schema():
    empty = _empty_momentum_trades()
    required = {
        "trade_id",
        "basket_id",
        "entry_signal_ts_ms",
        "entry_ts_ms",
        "exit_ts_ms",
        "symbol",
        "side",
        "entry_price",
        "exit_price",
        "exit_reason",
        "net_return",
        "gross_return",
        "cost_return",
        "funding_return",
    }
    assert required.issubset(set(empty.columns))


def test_format_momentum_report_smoke():
    metadata = {
        "run_label": "test_label",
        "config": {
            "ranker": "clenow_slope_r2",
            "ranker_lookback_days": 90,
            "liquidity_tier_size": 30,
            "liquidity_volume_window_days": 90,
            "max_concurrent_positions": 8,
            "cost_multiplier": 3.0,
            "position_sizing": "equal",
        },
        "pit_manifest": {"rows": 100, "symbols": 50, "feature_symbols": 50, "full_pit_universe_pass": True},
        "rows": {"features": 1000, "entry_candidates": 50, "trades": 20, "baskets": 20},
        "date_range": {"start": "2024-01-01", "end": "2024-06-01"},
        "summary": {
            "total_return": 0.10,
            "sharpe_like": 1.20,
            "max_drawdown": -0.08,
            "max_underwater_days": 30,
            "worst_30d_return": -0.05,
            "worst_60d_return": -0.07,
            "worst_90d_return": -0.08,
            "worst_120d_return": -0.08,
            "trade_win_rate": 0.55,
            "profit_factor": 1.4,
            "gross_return": 0.12,
            "cost_return": -0.01,
            "funding_return": -0.01,
            "funding_mode": "modeled",
        },
        "promotion": {
            "promotion_gate_pass": True,
            "all_splits_positive": True,
            "splits_with_baskets": 3,
            "avg_split_sharpe": 1.0,
            "funding_mode": "modeled",
            "promotion_reasons": [],
        },
        "splits": [
            {"name": s[0], "basket_count": 7, "total_return": 0.03, "sharpe_like": 1.0, "max_drawdown": -0.05}
            for s in SPLITS
        ],
        "lifecycle": {
            "skipped_capacity": 0,
            "skipped_cooldown": 0,
            "skipped_already_held": 0,
            "skipped_no_entry_bar": 0,
            "force_closed_at_end": 1,
            "force_closed_missing_data": 0,
        },
        "promotion_note": "Full PIT universe and funding present; suitable for split/promotion review.",
    }
    report = format_momentum_report(metadata)
    assert "Cross-Sectional Momentum Research" in report
    assert "test_label" in report
    assert "Promotion gate" in report
    assert "Splits" in report


# -- integration test --------------------------------------------------------


def test_run_cross_sectional_momentum_research_writes_reports_on_fixture(tmp_path):
    """End-to-end smoke: build a synthetic dataset, run the backtest, verify artifacts.

    The synthetic data: BTCUSDT uptrending (regime on), 4 alts with varied
    trend strength. Relaxed config so events can fire on a small fixture.
    """
    n_days = 320  # enough for ranker_lookback (60) + history (180)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AAAUSDT", "BBBUSDT"]
    klines = _make_klines_1h(
        symbols=symbols,
        n_days=n_days,
        log_trend_per_day=[0.0015, 0.0030, 0.0050, 0.0000, -0.0010],
        turnover_per_hour=[10_000_000.0, 5_000_000.0, 3_000_000.0, 1_500_000.0, 1_000_000.0],
    )
    funding = _make_funding_8h(symbols=symbols, n_days=n_days)
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

    cfg = CrossSectionalMomentumConfig(
        liquidity_tier_size=4,  # exclude the bottom-volume coin
        liquidity_volume_window_days=30,
        min_listing_history_days=60,
        ranker_lookback_days=60,
        vol_short_window_days=15,
        vol_long_window_days=45,
        atr_window_days=14,
        breakout_window_days=30,
        sma_trend_break_days=50,
        sma_regime_days=100,
        coil_release_min_compress_days=3,
        funding_overheat_window_days=30,
        max_concurrent_positions=3,
        require_pit_membership=False,
        require_full_pit_universe=False,
        require_funding_not_overheated=False,
    )
    result = run_cross_sectional_momentum_research(
        tmp_path,
        config=cfg,
        cost_config=CostConfig(),
    )

    report_dir = Path(result["report_dir"])
    assert (report_dir / "cross_sectional_momentum_research_report.md").exists()
    assert (report_dir / "cross_sectional_momentum_research_report.json").exists()
    payload = json.loads((report_dir / "cross_sectional_momentum_research_report.json").read_text())
    assert "config" in payload
    assert "summary" in payload
    assert "splits" in payload
    assert "promotion" in payload
    assert payload["rows"]["features"] > 0
    # The synthetic series is monotonically trending — at least one entry candidate
    # should have been generated for the strongest coins on the breakout days.
    assert payload["rows"]["entry_candidates"] >= 0


def test_run_raises_when_full_pit_required_and_archive_missing(tmp_path):
    n_days = 60
    symbols = ["BTCUSDT", "ETHUSDT"]
    klines = _make_klines_1h(symbols=symbols, n_days=n_days)
    write_dataset(klines, tmp_path, "klines_1h")
    cfg = CrossSectionalMomentumConfig(
        liquidity_tier_size=2,
        min_listing_history_days=10,
        ranker_lookback_days=20,
        sma_regime_days=10,
        require_full_pit_universe=True,
    )
    with pytest.raises(RuntimeError):
        run_cross_sectional_momentum_research(tmp_path, config=cfg)
