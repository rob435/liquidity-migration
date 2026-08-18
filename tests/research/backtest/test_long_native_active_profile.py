from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.account.account_kernel import AccountRiskPolicy
from liquidity_migration.core.config import CostConfig
from liquidity_migration.account.execution_adapters import ExecutionTwinConfig, LatencyProfile
from liquidity_migration.research.backtest.historical_account_replay import (
    HistoricalAccountSession,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.research.backtest.long_native import (
    _diagnostic_data_end,
    _finalize_trade,
    _methodology_run_label,
    _run_long_pipeline,
    format_long_native_report,
)
from liquidity_migration.rules.long_identity import LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    long_v11a_profile,
    long_v12_profile,
)
from liquidity_migration.research.backtest.run_diagnostics import diagnose

def _feature(symbol: str, ts_ms: int, *, day_return: float = 0.20) -> dict[str, object]:
    return {
        "symbol": symbol,
        "ts_ms": ts_ms,
        "in_universe": True,
        "regime_on": True,
        "eth_regime_on": True,
        "log_return": math.log1p(day_return),
        "close_location": 0.95,
        "today_volume_rank": 3,
        "realized_vol": 0.60,
        "sigma_daily_30d": 0.03,
        "atr_14d_pct": 0.02,
        "pump_3d_log": math.log1p(day_return),
        "pump_7d_log": math.log1p(day_return),
        "close_loc_3d": 0.90,
        "close_loc_7d": 0.90,
        "btc_rv_30": 0.60,
    }


def _bars(signal_ts_ms: int, *, retrace_hour: int | None = None, hours: int = 96) -> dict[str, object]:
    ends = np.asarray(
        [signal_ts_ms + hour * MS_PER_HOUR for hour in range(hours)],
        dtype=np.int64,
    )
    closes = np.full(hours, 100.0, dtype=np.float64)
    highs = np.full(hours, 100.5, dtype=np.float64)
    lows = np.full(hours, 99.5, dtype=np.float64)
    if retrace_hour is not None:
        closes[retrace_hour] = 98.0
        lows[retrace_hour] = 98.0
    return {
        "ends": ends.tolist(),
        "by_end": {int(value): index for index, value in enumerate(ends)},
        "bar_end_ts_ms": ends,
        "open": np.full(hours, 100.0, dtype=np.float64),
        "high": highs,
        "low": lows,
        "close": closes,
    }


def _session(
    root: Path,
    symbols: tuple[str, ...],
    *,
    risk_limit: float = 1e12,
) -> HistoricalAccountSession:
    return HistoricalAccountSession(
        root,
        account_id=LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
        risk_policy=AccountRiskPolicy(
            risk_limit,
            risk_limit,
            risk_limit,
            risk_limit,
            10.0,
        ),
        instrument_rules=synthetic_historical_rules_for_symbols(
            list(symbols),
            max_leverage=10.0,
            observed_ts_ns=1,
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="long-v11a-test",
    )


def _run(
    tmp_path: Path,
    features: list[dict[str, object]],
    bars: dict[str, dict[str, object]],
    *,
    config: LongNativeConfig | None = None,
    risk_limit: float = 1e12,
):
    decisions = []
    session = _session(tmp_path / "account", tuple(bars), risk_limit=risk_limit)
    result = _run_long_pipeline(
        features=pl.DataFrame(features),
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config or long_v11a_profile(),
        costs=CostConfig(),
        kernel_session=session,
        kernel_decision_sink=decisions,
    )
    return (*result, decisions, session)


def test_diagnostic_end_translates_exclusive_boundary_to_last_data_day() -> None:
    assert _diagnostic_data_end("2026-07-16") == "2026-07-15"
    assert _diagnostic_data_end("") is None
    common = {
        "full_pit_universe_pass": True,
        "funding_mode": "modeled",
        "archive_manifest_empty": False,
        "requested_end": _diagnostic_data_end("2026-07-16"),
        "n_features": 1,
        "n_trades": 1,
    }
    complete = diagnose(**common, data_end="2026-07-15")
    stale = diagnose(**common, data_end="2026-07-14")
    assert all(warning.code != "WINDOW_CLIPPED_END" for warning in complete)
    assert any(warning.code == "WINDOW_CLIPPED_END" for warning in stale)


def test_finalize_trade_notional_multiplier_scales_gross() -> None:
    position = {
        "entry_price": 100.0,
        "position_weight": 1.0,
        "symbol": "WIFUSDT",
        "entry_ts_ms": 2 * MS_PER_HOUR,
        "entry_signal_ts_ms": MS_PER_HOUR,
        "basket_id": "basket",
        "planned_exit_ts_ms": 4 * MS_PER_HOUR,
        "stop_price": 90.0,
        "take_profit_price": 130.0,
        "pattern": "fomo_chase",
    }
    kwargs = {
        "exit_ts_ms": 4 * MS_PER_HOUR,
        "exit_price": 110.0,
        "reason": "take_profit",
        "notional_weight": 0.2,
        "round_trip_cost_bps": 0.0,
        "funding_lookup": None,
    }
    base = _finalize_trade(position, **kwargs)
    scaled = _finalize_trade(position, **kwargs, notional_multiplier=10.0)
    assert base["gross_return"] == pytest.approx(0.02)
    assert scaled["gross_return"] == pytest.approx(0.20)
    assert scaled["gross_trade_return"] == base["gross_trade_return"]


def test_pipeline_routes_entry_and_exit_through_account_kernel(tmp_path: Path) -> None:
    signal_ts = 1_700_000_000_000
    trades, stats, events, decisions, session = _run(
        tmp_path,
        [_feature("WIFUSDT", signal_ts)],
        {"WIFUSDT": _bars(signal_ts)},
    )
    assert trades.height == 1
    assert events == {"fomo_chase": 1}
    assert stats["skipped_account_kernel"] == 0
    assert decisions[0].intent.intent.signed_notional_usdt > 0.0
    assert decisions[1].intent.intent.signed_notional_usdt == 0.0
    assert len(session.outputs) == 2
    assert all(output.target_result.accepted for output in session.outputs)


def test_account_risk_rejection_does_not_create_trade(tmp_path: Path) -> None:
    signal_ts = 1_700_000_000_000
    trades, stats, _events, decisions, session = _run(
        tmp_path,
        [_feature("WIFUSDT", signal_ts)],
        {"WIFUSDT": _bars(signal_ts)},
        risk_limit=100.0,
    )
    assert trades.is_empty()
    assert stats["skipped_account_kernel"] == 1
    assert len(decisions) == 1
    assert session.kernel is not None
    assert session.kernel.state().positions == {}


def test_sniper_capacity_is_allocated_at_actual_entry_time(tmp_path: Path) -> None:
    signal_ts = 1_700_000_000_000
    config = dataclasses.replace(long_v11a_profile(), max_concurrent_positions=1)
    trades, stats, _events, _decisions, _session = _run(
        tmp_path,
        [
            _feature("WIFUSDT", signal_ts, day_return=0.30),
            _feature("PEPEUSDT", signal_ts),
        ],
        {
            "WIFUSDT": _bars(signal_ts, retrace_hour=6),
            "PEPEUSDT": _bars(signal_ts, retrace_hour=2),
        },
        config=config,
    )
    assert trades["symbol"].to_list() == ["PEPEUSDT"]
    assert stats["skipped_capacity"] == 1


def test_final_bar_scan_honors_stop_without_later_feature_row(tmp_path: Path) -> None:
    signal_ts = 1_700_000_000_000
    bars = _bars(signal_ts)
    bars["low"][7] = 90.0
    trades, stats, _events, decisions, _session = _run(
        tmp_path,
        [_feature("WIFUSDT", signal_ts)],
        {"WIFUSDT": bars},
    )
    assert trades["exit_reason"].to_list() == ["stop_loss"]
    assert stats["exits_stop"] == 1
    assert decisions[-1].intent.intent.reason == "stop_loss"


def test_wide_scan_keeps_marks_for_later_exit_groups(tmp_path: Path) -> None:
    signal_ts = 1_700_000_000_000
    later_ts = signal_ts + 4 * 24 * MS_PER_HOUR
    inactive = _feature("WIFUSDT", later_ts)
    inactive["close_location"] = 0.0

    trades, stats, _events, _decisions, session = _run(
        tmp_path,
        [
            _feature("WIFUSDT", signal_ts, day_return=0.30),
            _feature("PEPEUSDT", signal_ts),
            inactive,
        ],
        {
            "WIFUSDT": _bars(signal_ts, retrace_hour=1, hours=120),
            "PEPEUSDT": _bars(signal_ts, retrace_hour=6, hours=120),
        },
    )

    assert trades.height == 2
    assert set(trades["symbol"].to_list()) == {"WIFUSDT", "PEPEUSDT"}
    assert stats["exits_time"] == 2
    assert all(output.target_result.accepted for output in session.outputs)


def test_report_is_descriptive_not_a_legacy_promotion_gate() -> None:
    report = format_long_native_report(
        {
            "methodology_run_label": "exploratory",
            "run_label": "full_pit_universe",
            "config": dataclasses.asdict(long_v11a_profile()),
            "rows": {"features": 10, "trades": 2},
            "date_range": {"start": "2023-01-01", "end": "2023-01-31"},
            "pit_manifest": {"full_pit_universe_pass": True},
            "summary": {},
            "lifecycle": {},
            "event_counts": {"fomo_chase": 3},
        }
    )
    assert "- Run label: `exploratory`" in report
    assert "- Data integrity label: `full_pit_universe`" in report
    assert "- fomo_chase: 3" in report
    assert "Promotion gate" not in report
    assert (
        _methodology_run_label(
            full_pit_universe_pass=True,
            archive_manifest_empty=False,
            tainted=False,
        )
        == "exploratory"
    )


def test_v12_widens_the_stop_then_tightens_it_after_two_days(tmp_path: Path) -> None:
    """v12's stop starts at 3xATR and ratchets to 1.5xATR at the 48h mark.

    Bars sit flat at 100 with one dip to 96.0 -- 4% down, which is inside
    3xATR (6%) but outside 1.5xATR (3%). Entry is the 6h sniper deadline, so
    the early dip goes at h12 (6h held, before the decay) and the late one at
    h60 (54h held, after it). v11a stops on either; v12 rides out the early one
    and stops on the late one, which is the whole point of the rule.

    h6 would not work as the early dip: that is the entry bar itself, and the
    exit scan starts one bar after entry.
    """
    signal_ts = 1_700_000_000_000
    atr_pct = 0.02

    def _dip_bars(dip_hour: int) -> dict[str, object]:
        bars = _bars(signal_ts, hours=96)
        bars["low"] = np.array(bars["low"], dtype=np.float64)
        bars["low"][dip_hour] = 96.0
        return bars

    for dip_hour, v11a_stops, v12_stops in ((12, True, False), (60, True, True)):
        features = [_feature("AAAUSDT", signal_ts)]
        bars = {"AAAUSDT": _dip_bars(dip_hour)}
        for cfg, expected in (
            (long_v11a_profile(), v11a_stops),
            (long_v12_profile(), v12_stops),
        ):
            trades, stats, _events, _decisions, _session = _run(
                tmp_path / f"{dip_hour}-{cfg.execution_strategy_id}",
                features,
                bars,
                config=cfg,
            )
            assert trades.height == 1
            stopped = trades["exit_reason"][0] == "stop_loss"
            assert stopped is expected, (
                f"dip at h{dip_hour} under {cfg.execution_strategy_id}: "
                f"expected stop={expected}, got {trades['exit_reason'][0]}"
            )
            assert stats["exits_stop"] == (1 if expected else 0)

    assert long_v12_profile().fc_atr_stop_mult == 3.0
    assert long_v11a_profile().fc_stop_time_decay_hours == 0
    assert abs(long_v12_profile().fc_stop_time_decay_atr_mult - 1.5) < 1e-12
    assert atr_pct * 3.0 > 0.04 > atr_pct * 1.5
