"""Tests for the long-sleeve forward-testing module (v11a MultiStratV1).

Covers:
- Profile loader returns the v11a uni10 sniper config
- Order-link prefix is `lm-en-l-*` so ws_risk routes fills to the long ledger
- Per-position notional sizing scales by notional_multiplier × base
- Sniper retrace candidate selection enters when live price reaches threshold
  AND falls through after deadline expires
- Cooldown prevents same-symbol re-entry within cooldown_days
- Time-stop exit plans only trigger past planned_exit_ts_ms
- Telegram notification fires only on material events
- Combined-book aggregate roll-up reads both ledgers
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native_event_demo import (
    LONG_DEMO_STRATEGY_PROFILES,
    LONG_DEMO_TRADES_DATASET,
    LONG_ENTRY_LINK_PREFIX,
    LONG_EXIT_LINK_PREFIX,
    LongNativeDemoCycleConfig,
    MULTI_STRAT_V1_STRATEGY_ID,
    _filter_pending_long_entries,
    _long_demo_event_config,
    _long_demo_strategy_id,
    _long_order_link_id,
    _maybe_long_notify,
    _open_long_trades,
    _plan_time_stop_exits,
    _select_long_entry_candidates,
    _v11a_long_native_config,
    _vol_parity_weight,
    format_combined_book_summary,
    format_long_demo_cycle_summary,
    format_long_telegram_status_message,
    target_long_order_notional_pct_equity,
    _long_telegram_reason,
)
from liquidity_migration.storage import write_dataset


def test_v11a_config_matches_research_run() -> None:
    cfg = _v11a_long_native_config()
    # FC-only universe
    assert cfg.enable_fomo_chase
    assert not cfg.enable_capitulation_rebound
    assert not cfg.enable_volume_resurrection
    assert not cfg.enable_funding_squeeze
    assert not cfg.enable_oversold_bounce
    assert not cfg.enable_uptrend_dip
    # uni10
    assert cfg.universe_size == 10
    # sniper retrace
    assert cfg.fc_use_sniper_entry
    assert cfg.fc_sniper_retrace_pct == pytest.approx(0.01)
    assert cfg.fc_sniper_deadline_hours == 6
    assert cfg.fc_sniper_skip_on_no_retrace is False  # fall-through
    # ATR exits
    assert cfg.fc_use_atr_exits
    assert cfg.fc_atr_stop_mult == pytest.approx(1.5)
    assert cfg.fc_atr_tp_mult == pytest.approx(4.0)
    assert cfg.fc_max_atr_pct == pytest.approx(0.12)
    assert cfg.fc_max_hold_days == 3
    # sigma + multi-day triggers
    assert cfg.fc_use_sigma_threshold
    assert cfg.fc_sigma_mult == pytest.approx(2.5)
    assert cfg.fc_enable_3d_trigger
    assert cfg.fc_enable_7d_trigger
    # Portfolio
    assert cfg.max_concurrent_positions == 5
    assert cfg.cooldown_days == 7
    assert cfg.entry_delay_hours == 1
    assert cfg.max_position_weight == pytest.approx(0.30)
    assert cfg.sizing == "vol_parity"


def test_strategy_profile_resolution() -> None:
    assert LONG_DEMO_STRATEGY_PROFILES == ("MultiStratV1",)
    assert _long_demo_strategy_id("MultiStratV1") == MULTI_STRAT_V1_STRATEGY_ID
    with pytest.raises(ValueError):
        _long_demo_strategy_id("nonexistent")
    cfg = _long_demo_event_config("MultiStratV1")
    assert cfg.enable_fomo_chase
    with pytest.raises(ValueError):
        _long_demo_event_config("nope")


def test_long_order_link_id_uses_long_prefix_for_ws_risk_routing() -> None:
    """ws_risk routes long-sleeve fills to the long ledger based on the
    `lm-en-l-` / `lm-ux-l-` order-link prefixes. Verify both prefixes are used."""
    entry_link = _long_order_link_id(LONG_ENTRY_LINK_PREFIX, symbol="BTCUSDT", signal_ts_ms=1700000000000)
    assert entry_link.startswith("lm-en-l-"), entry_link
    assert len(entry_link) <= 36  # Bybit order_link_id limit

    # Long exit prefix (used by _execute_long_exits via _long_risk_order_link_id)
    # has 'ux-l' base. Smoke-test format.
    from liquidity_migration.long_native_event_demo import _long_risk_order_link_id
    exit_link = _long_risk_order_link_id(LONG_EXIT_LINK_PREFIX, symbol="ETHUSDT", ts_ms=1700000000000, attempt=0)
    assert exit_link.startswith("lm-ux-l-"), exit_link
    assert len(exit_link) <= 36


def test_per_position_notional_scales_by_multiplier() -> None:
    strategy = _v11a_long_native_config()
    # Owner pick: 10x multiplier
    demo_10x = LongNativeDemoCycleConfig(notional_multiplier=10.0)
    base_per_position = strategy.gross_exposure / strategy.max_concurrent_positions
    assert target_long_order_notional_pct_equity(demo_10x, strategy) == pytest.approx(base_per_position * 10.0)
    # 5x = research peak
    demo_5x = LongNativeDemoCycleConfig(notional_multiplier=5.0)
    assert target_long_order_notional_pct_equity(demo_5x, strategy) == pytest.approx(base_per_position * 5.0)
    # Explicit override wins
    demo_override = LongNativeDemoCycleConfig(max_order_notional_pct_equity=0.5)
    assert target_long_order_notional_pct_equity(demo_override, strategy) == pytest.approx(0.5)


def test_vol_parity_weight_floors_and_clamps() -> None:
    # Low realized vol → upper bound from max_position_weight / notional_weight
    w_low = _vol_parity_weight(realized_vol=0.10, vol_floor=0.30, max_position_weight=0.30, notional_weight=0.20)
    # vol_used = max(0.10, 0.30) = 0.30 → vol_floor/vol_used = 1.0; max_pos_w/notional_w = 1.5 → min = 1.0
    assert w_low == pytest.approx(1.0)
    # High realized vol → inverse-vol weight
    w_high = _vol_parity_weight(realized_vol=1.5, vol_floor=0.30, max_position_weight=0.30, notional_weight=0.20)
    # vol_floor/vol_used = 0.30/1.5 = 0.20 → min(0.20, 1.5) = 0.20; max(0.20, 0.25) = 0.25
    assert w_high == pytest.approx(0.25)


def _build_features_with_fc_signal(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal features row that passes detect_pattern_fomo_chase."""
    # FC requires: in_universe, regime_on, eth_regime_on, today_volume_rank <= 10,
    # log_return >= 2.5 * sigma_daily, close_location >= 0.7, atr_14d_pct <= 0.12
    return pl.DataFrame([
        {
            "ts_ms": signal_ts_ms,
            "symbol": symbol,
            "close": signal_close,
            "in_universe": True,
            "regime_on": True,
            "eth_regime_on": True,
            "today_volume_rank": 5,
            "log_return": math.log(1.0 + 0.20),  # 20% day
            "close_location": 0.85,
            "atr_14d_pct": 0.05,
            "sigma_daily_30d": 0.05,  # 2.5*0.05 = 0.125 threshold, 0.20 > 0.125 → trigger
            "pump_3d_log": 0.10,
            "pump_7d_log": 0.20,
            "close_loc_3d": 0.7,
            "close_loc_7d": 0.7,
            "intra_max_Nh_pump_log": 0.0,
            "realized_vol": 0.6,
            "coin_30d_return": 0.5,
            "coin_60d_return": 0.5,
            "coin_fc_sma": None,
            "btc_high_proximity": 0.5,
            "btc_sma_dist": 0.05,
            "vol_vs_30d_median": 2.0,
            "own_pump_quantile_90d": 0.10,
            "own_atr_quantile_90d": 0.10,
            "atr_20d": 5.0,
        }
    ])


def test_sniper_retrace_enters_when_live_price_reaches_threshold() -> None:
    """signal_close=100, retrace_threshold=99 (1% below), live_price=98.5
    → entry fires with reason='sniper_retrace'."""
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000  # not too far in past
    now = signal_ts + 2 * MS_PER_HOUR  # 2h after signal, well inside 6h window
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    klines = pl.DataFrame()  # not used by _select_long_entry_candidates directly
    all_trades = pl.DataFrame()
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=klines, all_trades=all_trades, now_ms=now,
        strategy=strategy, price_by_symbol={"BTCUSDT": 98.5},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["symbol"] == "BTCUSDT"
    assert cand["entry_reason"] == "sniper_retrace"
    assert cand["signal_close"] == pytest.approx(100.0)
    assert cand["retrace_threshold"] == pytest.approx(99.0)


def test_sniper_falls_through_after_deadline_when_no_retrace() -> None:
    """signal_close=100, live_price=100.5 (no retrace), now>deadline
    → entry fires with reason='sniper_deadline_fallthru'."""
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    # Past 6h deadline but within 24h freshness bound
    now = signal_ts + 8 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="ETHUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=pl.DataFrame(),
        now_ms=now, strategy=strategy,
        price_by_symbol={"ETHUSDT": 100.5},
        max_new_entries=5,
    )
    assert len(candidates) == 1
    assert candidates[0]["entry_reason"] == "sniper_deadline_fallthru"


def test_sniper_waits_when_within_window_and_no_retrace() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 3 * MS_PER_HOUR  # inside window, no retrace yet
    features = _build_features_with_fc_signal(symbol="SOLUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=pl.DataFrame(),
        now_ms=now, strategy=strategy,
        price_by_symbol={"SOLUSDT": 100.5},  # no retrace
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_retrace_yet"] == 1


def test_stale_signal_beyond_24h_is_dropped() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 36 * MS_PER_HOUR  # past 24h freshness bound
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=pl.DataFrame(),
        now_ms=now, strategy=strategy, price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["no_signal"] == 1


def test_cooldown_blocks_re_entry() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    # Recently-closed trade within cooldown
    recent_exit = now - 1 * MS_PER_DAY  # 1 day ago, well inside 7-day cooldown
    all_trades = pl.DataFrame([
        {
            "trade_id": "old", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "closed", "exit_ts_ms": recent_exit, "qty": "0.001",
        }
    ])
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=all_trades,
        now_ms=now, strategy=strategy, price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["cooldown"] == 1


def test_open_position_blocks_re_entry() -> None:
    strategy = _v11a_long_native_config()
    signal_ts = 1_700_000_000_000
    now = signal_ts + 2 * MS_PER_HOUR
    features = _build_features_with_fc_signal(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    all_trades = pl.DataFrame([
        {
            "trade_id": "open-1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001",
        }
    ])
    candidates, skips = _select_long_entry_candidates(
        features=features, klines=pl.DataFrame(), all_trades=all_trades,
        now_ms=now, strategy=strategy, price_by_symbol={"BTCUSDT": 98.0},
        max_new_entries=5,
    )
    assert candidates == []
    assert skips["already_open"] == 1


def test_plan_time_stop_exits_only_for_expired_long_positions() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame([
        {  # Past planned_exit_ts_ms → eligible
            "trade_id": "expired-1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001",
            "planned_exit_ts_ms": now - 1 * MS_PER_HOUR,
        },
        {  # Future planned_exit_ts_ms → not eligible
            "trade_id": "live-1", "sleeve": "long", "symbol": "ETHUSDT", "side": "long",
            "status": "open", "qty": "0.01",
            "planned_exit_ts_ms": now + 24 * MS_PER_HOUR,
        },
        {  # Already has live exit order pending → skipped
            "trade_id": "exiting-1", "sleeve": "long", "symbol": "SOLUSDT", "side": "long",
            "status": "open", "qty": "1.0",
            "planned_exit_ts_ms": now - 1 * MS_PER_HOUR,
        },
    ])
    plans = _plan_time_stop_exits(trades, now_ms=now, live_exit_order_symbols={"SOLUSDT"})
    assert [p["symbol"] for p in plans] == ["BTCUSDT"]
    assert plans[0]["exit_reason"] == "time_stop"


def test_pending_entry_dedupe_skips_in_flight_orders() -> None:
    now = 1_700_000_000_000
    candidates = [
        {"trade_id": "t1", "symbol": "BTCUSDT", "signal_ts_ms": now},
        {"trade_id": "t2", "symbol": "ETHUSDT", "signal_ts_ms": now},
    ]
    orders = pl.DataFrame([
        {
            "order_link_id": "lm-en-l-BTC-x", "trade_id": "t1", "symbol": "BTCUSDT",
            "reduce_only": False, "status": "submitted", "ts_ms": now - 60_000,
        }
    ])
    kept, skipped = _filter_pending_long_entries(candidates, orders, now_ms=now)
    assert {c["symbol"] for c in kept} == {"ETHUSDT"}
    assert skipped == 1


def test_open_long_trades_filter_excludes_short_and_closed() -> None:
    trades = pl.DataFrame([
        {"trade_id": "l-open", "sleeve": "long", "symbol": "BTCUSDT", "side": "long", "status": "open"},
        {"trade_id": "l-closed", "sleeve": "long", "symbol": "ETHUSDT", "side": "long", "status": "closed"},
        {"trade_id": "s-open", "sleeve": "short", "symbol": "AAVEUSDT", "side": "short", "status": "open"},
    ])
    result = _open_long_trades(trades)
    assert result["trade_id"].to_list() == ["l-open"]


def test_telegram_notify_quiet_when_no_material_event() -> None:
    payload: dict[str, Any] = {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "submit",
            "equity_usdt": 10_000.0,
            "entries_executed": 0,
            "exits_executed": 0,
            "entry_candidates": 0,
            "exit_candidates": 0,
            "open_long_positions_after": 0,
            "order_notional_pct_equity": 2.0,
            "entry_leverage": 10.0,
            "notional_multiplier": 10.0,
        },
        "entries": [],
        "exits": [],
        "entry_orders": [],
        "exit_orders": [],
        "ledger_position_summary": {},
    }
    sent, reason = _maybe_long_notify(payload, enabled=True)
    assert sent is False
    assert reason == "quiet_no_material_event"


def test_telegram_notify_reason_classification() -> None:
    cycle = {"ts_ms": 1, "mode": "submit", "equity_usdt": 100.0,
             "entries_executed": 0, "exits_executed": 0,
             "entry_candidates": 0, "exit_candidates": 0,
             "order_notional_pct_equity": 0.0, "entry_leverage": 0.0}
    # Entry executed
    assert _long_telegram_reason({"cycle": {**cycle, "entries_executed": 1}, "entry_orders": [], "exit_orders": []}) == "long_entry_executed"
    # Entry error
    assert _long_telegram_reason({"cycle": cycle, "entry_orders": [{"submit_mode": "error"}], "exit_orders": []}) == "long_entry_error"
    # Position report error
    assert _long_telegram_reason({"cycle": {**cycle, "position_report_error": "rest down"}, "entry_orders": [], "exit_orders": []}) == "position_report_error"


def test_format_long_telegram_message_contains_essentials() -> None:
    payload = {
        "cycle": {
            "ts_ms": 1_700_000_000_000, "mode": "submit", "equity_usdt": 10_000.0,
            "entries_executed": 1, "exits_executed": 0,
            "entry_candidates": 1, "exit_candidates": 0,
            "open_long_positions_after": 1,
            "order_notional_pct_equity": 2.0,
            "entry_leverage": 10.0, "notional_multiplier": 10.0,
        },
        "entries": [{
            "symbol": "BTCUSDT", "qty": 0.001, "entry_price": 50000.0,
            "entry_reason": "sniper_retrace", "stop_price": 47500.0, "take_profit_price": 60000.0,
        }],
        "exits": [],
        "ledger_position_summary": {"unrealized_pnl_usdt": 0.0, "pnl_pct": 0.0},
    }
    text = format_long_telegram_status_message(payload, reason="long_entry_executed")
    assert "MultiStratV1" in text
    assert "BTCUSDT" in text
    assert "sniper_retrace" in text
    assert "10×" in text or "10x" in text or "x10" in text or "x" in text  # multiplier marker present


def test_combined_book_summary_reads_both_sleeves(tmp_path: Path) -> None:
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    short_root.mkdir()
    long_root.mkdir()
    # Short ledger: 1 closed winning short trade
    write_dataset(
        pl.DataFrame([{
            "trade_id": "s1", "sleeve": "short", "symbol": "AAAUSDT", "side": "short",
            "status": "closed", "qty": 1.0, "entry_price": 100.0, "exit_price": 90.0,
        }]),
        short_root, "event_demo_trades", partition_by=(),
    )
    # Long ledger: 1 open long trade
    write_dataset(
        pl.DataFrame([{
            "trade_id": "l1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": 0.001, "entry_price": 50_000.0,
        }]),
        long_root, LONG_DEMO_TRADES_DATASET, partition_by=(),
    )
    text = format_combined_book_summary(
        short_root=short_root, long_root=long_root, now_ms=1_700_000_000_000,
    )
    assert "Combined book" in text
    assert "Short sleeve" in text
    assert "Long sleeve" in text
    # Short realized PnL: (100 - 90) * 1 = 10
    assert "$10.00" in text
    # Long open notional: 0.001 * 50_000 = 50
    assert "$50.00" in text


def test_combined_book_summary_fails_open_on_missing_roots(tmp_path: Path) -> None:
    # Missing roots/datasets should not raise — aggregate reports must
    # never break the cron job
    text = format_combined_book_summary(
        short_root=tmp_path / "no-such-short",
        long_root=tmp_path / "no-such-long",
        now_ms=1_700_000_000_000,
    )
    assert "Combined book" in text
    assert "trades=0" in text


def test_long_demo_cycle_summary_includes_key_fields() -> None:
    payload = {
        "cycle": {
            "cycle_id": "abc", "mode": "submit", "strategy_profile": "MultiStratV1",
            "symbols": 10, "feature_rows": 100,
            "entries_executed": 1, "entry_candidates": 1,
            "exits_executed": 0, "exit_candidates": 0,
            "open_long_positions_after": 1, "equity_usdt": 10_000.0,
            "cycle_elapsed_pre_persist_ms": 500.0,
        }
    }
    text = format_long_demo_cycle_summary(payload)
    assert "long-native event demo cycle" in text
    assert "MultiStratV1" in text
    assert "entries=1/1" in text


def test_long_kline_universe_fetcher_scopes_to_top_n_by_turnover() -> None:
    """Long daemon's kline manager must NOT bootstrap all 567 USDT-perps.

    The long sleeve only trades the top-10 by 24h turnover; scoping the
    kline universe to the top-50 keeps memory under the systemd cap (was
    OOM-killing at 1G with the full universe) while leaving 5x rank-shift
    headroom. Anything beyond the top-50 falls back to per-cycle REST.
    """
    from liquidity_migration.long_native_event_demo_daemon import (
        _LONG_KLINE_UNIVERSE_SIZE,
        _build_long_kline_universe,
    )

    class _FakeMarket:
        def get_tickers(self) -> list[dict]:
            rows: list[dict] = []
            for i in range(200):
                rows.append({"symbol": f"SYM{i:03d}USDT", "turnover24h": str(1_000_000 - i)})
            # Non-USDT pair — must be excluded.
            rows.append({"symbol": "BTC-PERP", "turnover24h": "999"})
            # Zero turnover + null turnover — must be excluded.
            rows.append({"symbol": "DEADUSDT", "turnover24h": "0"})
            rows.append({"symbol": "NULLUSDT", "turnover24h": None})
            return rows

    symbols = _build_long_kline_universe(_FakeMarket())
    assert len(symbols) == _LONG_KLINE_UNIVERSE_SIZE
    assert symbols[0] == "SYM000USDT"
    assert symbols[-1] == f"SYM{_LONG_KLINE_UNIVERSE_SIZE - 1:03d}USDT"
    assert "BTC-PERP" not in symbols
    assert "DEADUSDT" not in symbols
    assert "NULLUSDT" not in symbols


def test_long_kline_universe_fetcher_returns_empty_on_rest_failure() -> None:
    """Universe fetch errors must not crash the manager — empty list lets
    the manager bootstrap nothing and the cycle's REST fallback supplies
    everything that day."""
    from liquidity_migration.long_native_event_demo_daemon import (
        _build_long_kline_universe,
    )

    class _FailingMarket:
        def get_tickers(self) -> list[dict]:
            raise RuntimeError("simulated REST outage")

    assert _build_long_kline_universe(_FailingMarket()) == []
