from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

import aggression_carry.forward_test as forward_test_module
from aggression_carry.config import CostConfig, DailyCloseFadeConfig, ForwardTestConfig, ResearchConfig
from aggression_carry.forward_test import (
    default_forward_sleeves,
    run_forward_from_latest_scan,
    run_forward_mark_only,
    run_forward_once,
    run_forward_scan,
    run_forward_sleeves,
)
from aggression_carry.storage import read_dataset


def test_default_forward_sleeves_only_expose_stage4_selected() -> None:
    base = DailyCloseFadeConfig(top_n=5, liquidity_rank_min=31, liquidity_rank_max=0)
    sleeves = default_forward_sleeves(base)

    assert tuple(sleeves) == ("stage4_selected",)
    assert sleeves["stage4_selected"].liquidity_rank_min == 31
    assert sleeves["stage4_selected"].liquidity_rank_max == 0
    assert sleeves["stage4_selected"].top_n == base.top_n


def test_forward_scan_uses_live_universe_filters_and_pump_selection(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 15, tzinfo=UTC)
    config = _research_config(tmp_path)
    payload = run_forward_scan(tmp_path, config=config, now=now, client=_FakeBybit(now))

    features = read_dataset(tmp_path, "forward_scan_features")
    symbols = [row["symbol"] for row in payload["candidates"]]

    assert payload["status"] == "candidates_ready"
    assert symbols == ["OLD1USDT", "OLD2USDT"]
    assert "YOUNGUSDT" not in symbols
    assert "BTCUSDT" not in symbols
    old1 = features.filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)
    assert old1["eligible"] is True
    assert old1["bar_coverage"] == 1.0
    assert old1["signal_ts_ms"] == int(datetime(2026, 1, 15, 22, 0, tzinfo=UTC).timestamp() * 1000)
    assert old1["last_available_bar_ts_ms"] == int(datetime(2026, 1, 15, 21, 59, tzinfo=UTC).timestamp() * 1000)


def test_forward_scan_reports_api_runtime_backoff_stats(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 15, tzinfo=UTC)
    fake = _FakeBybitWithStats(now)

    payload = run_forward_scan(tmp_path, config=_research_config(tmp_path), now=now, client=fake)
    report = (tmp_path / "reports" / "forward_scan_report.md").read_text(encoding="utf-8")

    assert payload["rows"]["api_backoff_events"] == 2
    assert payload["api"]["retry_events"] == 1
    assert payload["api"]["slow_calls"] == 1
    assert "| API/backoff events | 2 |" in report
    assert "| Retry events | 1 |" in report


def test_forward_run_opens_paper_trades_without_orders(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    fake = _FakeBybit(now)
    payload = run_forward_once(tmp_path, config=_research_config(tmp_path), now=now, client=fake)
    trades = read_dataset(tmp_path, "forward_paper_trades").sort("entry_rank")

    assert payload["rows"]["new_trades"] == 2
    assert trades["status"].to_list() == ["open", "open"]
    assert trades["symbol"].to_list() == ["OLD1USDT", "OLD2USDT"]
    assert trades["side"].to_list() == ["short", "short"]
    assert fake.private_order_calls == 0


def test_forward_run_without_injected_client_leaves_scan_fetch_parallelizable(tmp_path: Path, monkeypatch) -> None:
    seen_scan_clients = []

    class FakeMarket(_FakeBybit):
        def __init__(self, **kwargs) -> None:
            del kwargs
            super().__init__(datetime(2026, 1, 15, 22, 16, tzinfo=UTC))

    def fake_scan(*args, **kwargs):
        del args
        seen_scan_clients.append(kwargs.get("client"))
        return {
            "scan_id": "2026-01-15-1320",
            "now": "2026-01-15T22:16:00+00:00",
            "scan_end": "2026-01-15T22:00:00+00:00",
            "signal_time": "2026-01-15T22:00:00+00:00",
            "status": "no_candidates",
            "rows": {"universe": 0, "features": 0, "candidates": 0, "fetch_failures": 0},
            "candidates": [],
            "fetch_failures": [],
        }

    monkeypatch.setattr(forward_test_module, "BybitMarketData", FakeMarket)
    monkeypatch.setattr(forward_test_module, "run_forward_scan", fake_scan)

    payload = run_forward_once(
        tmp_path,
        config=_research_config(tmp_path),
        now=datetime(2026, 1, 15, 22, 16, tzinfo=UTC),
    )

    assert payload["rows"]["new_trades"] == 0
    assert seen_scan_clients == [None]


def test_forward_run_starts_twap_only_on_first_due_slice(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 1, tzinfo=UTC)
    config = _research_config(tmp_path)
    twap_config = replace(config, daily_close_fade=replace(config.daily_close_fade, entry_twap_minutes=60))

    payload = run_forward_once(tmp_path, config=twap_config, now=now, client=_FakeBybit(now))
    trades = read_dataset(tmp_path, "forward_paper_trades")
    slices = read_dataset(tmp_path, "forward_paper_slices")

    assert payload["scan"]["rows"]["candidates"] == 2
    assert payload["rows"]["new_trades"] == 2
    assert payload["rows"]["paper_slices"] == 120
    assert trades.sort("entry_rank")["entry_fill_count"].to_list() == [1, 1]
    first_symbol_slices = slices.filter(pl.col("symbol") == "OLD1USDT").sort("slice_index")
    assert first_symbol_slices.height == 60
    assert first_symbol_slices.row(0, named=True)["scheduled_time"] == "2026-01-15T22:01:00+00:00"
    assert first_symbol_slices.row(59, named=True)["scheduled_time"] == "2026-01-15T23:00:00+00:00"
    assert first_symbol_slices.filter(pl.col("status") == "filled").height == 1
    assert first_symbol_slices.filter(pl.col("status") == "pending").height == 59


def test_forward_run_refuses_late_twap_open_instead_of_retroactive_fills(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    config = _research_config(tmp_path)
    twap_config = replace(config, daily_close_fade=replace(config.daily_close_fade, entry_twap_minutes=60))

    payload = run_forward_once(tmp_path, config=twap_config, now=now, client=_FakeBybit(now))
    trades = read_dataset(tmp_path, "forward_paper_trades")
    slices = read_dataset(tmp_path, "forward_paper_slices")

    assert payload["scan"]["rows"]["candidates"] == 2
    assert payload["rows"]["new_trades"] == 0
    assert payload["rows"]["paper_slices"] == 0
    assert trades.is_empty()
    assert slices.is_empty()


def test_forward_scan_candidates_can_open_exact_first_slice_without_rescan(tmp_path: Path) -> None:
    scan_now = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
    open_now = datetime(2026, 1, 15, 22, 1, tzinfo=UTC)
    config = _research_config(tmp_path)
    twap_config = replace(config, daily_close_fade=replace(config.daily_close_fade, entry_twap_minutes=60))

    scan = run_forward_once(tmp_path, config=twap_config, now=scan_now, client=_FakeBybit(scan_now))
    payload = run_forward_from_latest_scan(
        tmp_path,
        config=twap_config,
        now=open_now,
        client=_FakeBybit(open_now),
        require_first_slice=True,
    )
    trades = read_dataset(tmp_path, "forward_paper_trades").sort("entry_rank")
    slices = read_dataset(tmp_path, "forward_paper_slices")

    assert scan["rows"]["new_trades"] == 0
    assert payload["scan"]["status"] == "cached_scan_candidates_ready"
    assert payload["rows"]["new_trades"] == 2
    assert trades["entry_fill_count"].to_list() == [1, 1]
    assert slices.filter(pl.col("status") == "filled").height == 2
    assert slices.filter(pl.col("scheduled_time") == "2026-01-15T22:01:00+00:00").height == 2


def test_forward_open_from_scan_refuses_late_first_slice_when_required(tmp_path: Path) -> None:
    scan_now = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
    late_now = datetime(2026, 1, 15, 22, 2, tzinfo=UTC)
    config = _research_config(tmp_path)
    twap_config = replace(config, daily_close_fade=replace(config.daily_close_fade, entry_twap_minutes=60))

    run_forward_once(tmp_path, config=twap_config, now=scan_now, client=_FakeBybit(scan_now))
    payload = run_forward_from_latest_scan(
        tmp_path,
        config=twap_config,
        now=late_now,
        client=_FakeBybit(late_now),
        require_first_slice=True,
    )
    trades = read_dataset(tmp_path, "forward_paper_trades")

    assert payload["scan"]["status"] == "cached_scan_not_first_slice_due"
    assert payload["rows"]["new_trades"] == 0
    assert trades.is_empty()


def test_forward_empty_scan_clears_cached_candidates(tmp_path: Path) -> None:
    scan_now = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
    config = _research_config(tmp_path)
    run_forward_scan(tmp_path, config=config, now=scan_now, client=_FakeBybit(scan_now))

    no_candidate_config = replace(config, daily_close_fade=replace(config.daily_close_fade, min_age_days=9999))
    run_forward_scan(tmp_path, config=no_candidate_config, now=scan_now, client=_FakeBybit(scan_now))
    payload = run_forward_from_latest_scan(
        tmp_path,
        config=config,
        now=datetime(2026, 1, 15, 22, 1, tzinfo=UTC),
        client=_FakeBybit(datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
        require_first_slice=True,
    )
    candidates = read_dataset(tmp_path, "forward_scan_candidates")

    assert candidates.is_empty()
    assert payload["scan"]["status"] == "no_cached_scan_candidates"
    assert payload["rows"]["new_trades"] == 0


def test_forward_pre_signal_scan_does_not_cache_openable_candidates(tmp_path: Path) -> None:
    pre_signal_now = datetime(2026, 1, 15, 21, 59, tzinfo=UTC)
    config = _research_config(tmp_path)

    scan = run_forward_scan(tmp_path, config=config, now=pre_signal_now, client=_FakeBybit(pre_signal_now))
    payload = run_forward_from_latest_scan(
        tmp_path,
        config=config,
        now=datetime(2026, 1, 15, 22, 1, tzinfo=UTC),
        client=_FakeBybit(datetime(2026, 1, 15, 22, 1, tzinfo=UTC)),
        require_first_slice=True,
    )
    candidates = read_dataset(tmp_path, "forward_scan_candidates")

    assert scan["status"] == "pre_signal"
    assert scan["rows"]["candidates"] == 0
    assert candidates.is_empty()
    assert payload["scan"]["status"] == "no_cached_scan_candidates"
    assert payload["rows"]["new_trades"] == 0


def test_forward_mark_only_does_not_scan_for_new_candidates(tmp_path: Path) -> None:
    config = _research_config(tmp_path)
    first_now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=first_now, client=_FakeBybit(first_now))

    second_now = datetime(2026, 1, 15, 22, 40, tzinfo=UTC)
    payload = run_forward_mark_only(tmp_path, config=config, now=second_now, client=_FakeBybit(second_now))

    assert payload["scan"]["status"] == "mark_only"
    assert payload["rows"]["new_trades"] == 0
    assert payload["rows"]["total_trades"] == 2


def test_forward_open_twap_trade_preserves_stored_schedule_after_config_change(tmp_path: Path) -> None:
    config = _research_config(tmp_path)
    open_config = replace(config, daily_close_fade=replace(config.daily_close_fade, entry_twap_minutes=60))
    first_now = datetime(2026, 1, 15, 22, 1, tzinfo=UTC)
    run_forward_once(tmp_path, config=open_config, now=first_now, client=_FakeBybit(first_now))

    changed_fade = replace(config.daily_close_fade, entry_delay_minutes=10, entry_twap_minutes=1)
    mark_config = replace(config, daily_close_fade=changed_fade)
    second_now = datetime(2026, 1, 15, 22, 40, tzinfo=UTC)
    run_forward_once(tmp_path, config=mark_config, now=second_now, client=_FakeBybit(second_now))

    trades = read_dataset(tmp_path, "forward_paper_trades").sort("entry_rank")
    slices = read_dataset(tmp_path, "forward_paper_slices")
    first_symbol_slices = slices.filter(pl.col("symbol") == "OLD1USDT").sort("slice_index")

    assert trades["entry_fill_count"].to_list() == [40, 40]
    assert trades["entry_twap_minutes"].to_list() == [60, 60]
    assert trades["entry_delay_minutes"].to_list() == [1, 1]
    assert slices.height == 120
    assert first_symbol_slices.height == 60
    assert first_symbol_slices.row(0, named=True)["scheduled_time"] == "2026-01-15T22:01:00+00:00"
    assert first_symbol_slices.row(59, named=True)["scheduled_time"] == "2026-01-15T23:00:00+00:00"
    assert first_symbol_slices.filter(pl.col("status") == "filled").height == 40
    assert first_symbol_slices.filter(pl.col("status") == "pending").height == 20


def test_forward_run_marks_short_stop_with_linear_pnl(tmp_path: Path) -> None:
    config = _research_config(tmp_path)
    first_now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=first_now, client=_FakeBybit(first_now))

    second_now = datetime(2026, 1, 15, 22, 40, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=second_now, client=_FakeBybit(second_now, stop_old1=True))
    trades = read_dataset(tmp_path, "forward_paper_trades")
    old1 = trades.filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)
    old2 = trades.filter(pl.col("symbol") == "OLD2USDT").row(0, named=True)

    assert old1["status"] == "closed"
    assert old1["exit_reason"] == "stop_loss"
    assert round(old1["gross_return"], 6) == -0.2
    assert round(old1["weighted_net_return"], 6) == -0.1
    assert old2["status"] == "open"


def test_forward_run_marks_short_take_profit_with_linear_pnl(tmp_path: Path) -> None:
    config = _research_config(tmp_path, take_profit_pct=0.005, stop_delay_minutes=0)
    first_now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=first_now, client=_FakeBybit(first_now))

    second_now = datetime(2026, 1, 15, 22, 40, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=second_now, client=_FakeBybit(second_now))
    trades = read_dataset(tmp_path, "forward_paper_trades").sort("entry_rank")

    assert trades["status"].to_list() == ["closed", "closed"]
    assert trades["exit_reason"].to_list() == ["take_profit", "take_profit"]
    assert round(trades["gross_return"].min(), 6) == 0.005


def test_forward_open_trade_uses_stored_exit_config_after_config_change(tmp_path: Path) -> None:
    open_config = _research_config(tmp_path, take_profit_pct=0.005, stop_delay_minutes=0)
    first_now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    run_forward_once(tmp_path, config=open_config, now=first_now, client=_FakeBybit(first_now))

    mark_config = _research_config(tmp_path, take_profit_pct=0.0, stop_delay_minutes=15)
    second_now = datetime(2026, 1, 15, 22, 40, tzinfo=UTC)
    run_forward_once(tmp_path, config=mark_config, now=second_now, client=_FakeBybit(second_now))
    trades = read_dataset(tmp_path, "forward_paper_trades").sort("entry_rank")

    assert trades["status"].to_list() == ["closed", "closed"]
    assert trades["exit_reason"].to_list() == ["take_profit", "take_profit"]


def test_forward_run_does_not_reenter_same_symbol_date(tmp_path: Path) -> None:
    config = _research_config(tmp_path, take_profit_pct=0.005, stop_delay_minutes=0)
    first_now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=first_now, client=_FakeBybit(first_now))
    second_now = datetime(2026, 1, 15, 22, 40, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=second_now, client=_FakeBybit(second_now))
    third = run_forward_once(tmp_path, config=config, now=datetime(2026, 1, 15, 22, 50, tzinfo=UTC), client=_FakeBybit(second_now))
    trades = read_dataset(tmp_path, "forward_paper_trades")

    assert third["rows"]["new_trades"] == 0
    assert trades.height == 2
    assert trades["symbol"].n_unique() == 2


def test_forward_run_stop_gap_fills_at_bar_open(tmp_path: Path) -> None:
    config = _research_config(tmp_path, stop_delay_minutes=15)
    first_now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    run_forward_once(tmp_path, config=config, now=first_now, client=_FakeBybit(first_now, gap_stop_old1=True))
    trades = read_dataset(tmp_path, "forward_paper_trades").sort("entry_rank")
    old1 = trades.filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)

    assert old1["exit_reason"] == "stop_loss"
    assert round(old1["gross_return"], 6) == -0.30


def test_forward_sleeves_use_isolated_roots_and_reports(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    config = _research_config(tmp_path)
    sleeves = {
        "primary": replace(config.daily_close_fade, liquidity_rank_min=1, liquidity_rank_max=10),
        "comparison": replace(
            config.daily_close_fade,
            liquidity_rank_min=1,
            liquidity_rank_max=10,
            gross_exposure=0.5,
            exclude_symbols=(),
        ),
    }

    payload = run_forward_sleeves(tmp_path, config=config, now=now, client=_FakeBybit(now), sleeves=sleeves)

    primary_trades = read_dataset(tmp_path / "forward_sleeves" / "primary", "forward_paper_trades")
    comparison_trades = read_dataset(tmp_path / "forward_sleeves" / "comparison", "forward_paper_trades")
    assert payload["rows"]["sleeves"] == 2
    assert {row["sleeve"] for row in payload["results"]} == {"primary", "comparison"}
    assert primary_trades.height == 2
    assert comparison_trades.height >= 2
    assert (tmp_path / "reports" / "forward_sleeves_report.md").exists()
    assert (tmp_path / "reports" / "forward_sleeves" / "primary" / "forward_paper_report.md").exists()
    assert (tmp_path / "reports" / "forward_sleeves" / "comparison" / "forward_paper_report.md").exists()


def _research_config(tmp_path: Path, *, take_profit_pct: float = 0.0, stop_delay_minutes: int = 15) -> ResearchConfig:
    fade = DailyCloseFadeConfig(
        signal_minute=22 * 60,
        top_n=2,
        hold_minutes=180,
        entry_delay_minutes=1,
        entry_twap_minutes=0,
        gross_exposure=1.0,
        score="vol_adjusted_day_return",
        pump_filter="pump",
        min_age_days=10,
        liquidity_rank_min=1,
        liquidity_rank_max=10,
        max_position_weight=0.0,
        coin_excess_vs_market_min=0.0,
        coin_vwap_extension_min=0.0,
        coin_late_volume_ratio_min=0.0,
        position_sizing="equal",
        stop_loss_pct=0.20,
        take_profit_pct=take_profit_pct,
        stop_delay_minutes=stop_delay_minutes,
        profit_protection_delay_minutes=None,
        exclude_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"),
    )
    forward = ForwardTestConfig(
        min_turnover_24h=0.0,
        max_spread_bps=100.0,
        min_open_interest_value=0.0,
        workers=1,
        max_entry_lag_minutes=60,
    )
    costs = CostConfig(maker_fee_bps=0, taker_fee_bps=0, maker_adverse_selection_bps=0, taker_slippage_bps_liquid=0)
    return ResearchConfig(data_root=tmp_path, daily_close_fade=fade, forward_test=forward, costs=costs)


class _FakeBybit:
    def __init__(self, now: datetime, *, stop_old1: bool = False, gap_stop_old1: bool = False) -> None:
        self.now = now
        self.stop_old1 = stop_old1
        self.gap_stop_old1 = gap_stop_old1
        self.private_order_calls = 0

    def create_order(self, *args, **kwargs):  # pragma: no cover - fails test if called
        del args, kwargs
        self.private_order_calls += 1
        raise AssertionError("paper forward test must not create orders")

    def get_instruments_info(self) -> list[dict]:
        day = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = []
        for symbol, launch_days in {
            "OLD1USDT": 40,
            "OLD2USDT": 40,
            "OLD3USDT": 40,
            "YOUNGUSDT": 2,
            "BTCUSDT": 400,
        }.items():
            rows.append(
                {
                    "symbol": symbol,
                    "contractType": "LinearPerpetual",
                    "status": "Trading",
                    "settleCoin": "USDT",
                    "launchTime": str(int((day - timedelta(days=launch_days)).timestamp() * 1000)),
                    "isPreListing": False,
                }
            )
        return rows

    def get_tickers(self) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "100",
                "ask1Price": "100.2",
                "turnover24h": "10000000",
                "openInterestValue": "5000000",
            }
            for symbol in ["OLD1USDT", "OLD2USDT", "OLD3USDT", "YOUNGUSDT", "BTCUSDT"]
        ]

    def get_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1000) -> list:
        del limit
        if interval == "D":
            return self._daily_klines(symbol, start, end)
        return self._minute_klines(symbol, start, end)

    def _daily_klines(self, symbol: str, start: int, end: int) -> list:
        rows = []
        current = datetime.fromtimestamp(start / 1000, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        last = datetime.fromtimestamp(end / 1000, tz=UTC)
        base = _base_price(symbol)
        index = 0
        while current <= last:
            ts_ms = int(current.timestamp() * 1000)
            close = base * (1.0 + 0.01 * ((index % 5) - 2))
            rows.append([str(ts_ms), str(base), str(close * 1.01), str(close * 0.99), str(close), "1000", str(close * 1000)])
            current += timedelta(days=1)
            index += 1
        return rows

    def _minute_klines(self, symbol: str, start: int, end: int) -> list:
        rows = []
        requested_start = datetime.fromtimestamp(start / 1000, tz=UTC)
        current = requested_start.replace(hour=0, minute=0, second=0, microsecond=0)
        last = datetime.fromtimestamp(end / 1000, tz=UTC)
        day = current.replace(hour=0, minute=0, second=0, microsecond=0)
        signal_minute = 22 * 60
        base = _base_price(symbol)
        pump = {
            "OLD1USDT": 0.30,
            "OLD2USDT": 0.22,
            "OLD3USDT": 0.05,
            "YOUNGUSDT": 0.60,
            "BTCUSDT": 0.40,
        }[symbol]
        previous = base
        while current <= last:
            minute = int((current - day).total_seconds() // 60)
            if minute <= signal_minute:
                close = base * (1.0 + pump * minute / signal_minute)
            else:
                entry = base * (1.0 + pump)
                if self.gap_stop_old1 and symbol == "OLD1USDT" and minute >= signal_minute + 15:
                    close = entry * 1.30
                elif self.stop_old1 and symbol == "OLD1USDT" and minute >= signal_minute + 16:
                    close = entry * 1.22
                else:
                    close = entry * (1.0 - 0.04 * min(minute - signal_minute, 180) / 180.0)
            open_price = previous
            high = max(open_price, close) * 1.001
            low = min(open_price, close) * 0.999
            ts_ms = int(current.timestamp() * 1000)
            if current >= requested_start:
                rows.append([str(ts_ms), str(open_price), str(high), str(low), str(close), "100", str(close * 100)])
            previous = close
            current += timedelta(minutes=1)
        return rows


class _FakeBybitWithStats(_FakeBybit):
    def stats(self) -> dict:
        return {
            "logical_calls": 7,
            "http_calls": 8,
            "retry_events": 1,
            "rate_limit_events": 0,
            "error_events": 0,
            "slow_calls": 1,
            "total_call_ms": 1250.0,
            "slow_call_ms": 1100.0,
            "last_error": "",
        }


def _base_price(symbol: str) -> float:
    return {
        "OLD1USDT": 100.0,
        "OLD2USDT": 90.0,
        "OLD3USDT": 80.0,
        "YOUNGUSDT": 70.0,
        "BTCUSDT": 50000.0,
    }[symbol]
