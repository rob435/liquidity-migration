from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from aggression_carry.config import CostConfig, DailyCloseFadeConfig, ForwardTestConfig, ResearchConfig
from aggression_carry.forward_test import run_forward_once, run_forward_scan, run_forward_sleeves
from aggression_carry.storage import read_dataset


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
    assert features.filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)["eligible"] is True
    assert features.filter(pl.col("symbol") == "OLD1USDT").row(0, named=True)["bar_coverage"] == 1.0


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


def test_forward_sleeves_use_isolated_roots_and_reports(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 22, 16, tzinfo=UTC)
    config = _research_config(tmp_path)
    sleeves = {
        "core": replace(config.daily_close_fade, liquidity_rank_min=1, liquidity_rank_max=10),
        "control": replace(
            config.daily_close_fade,
            liquidity_rank_min=1,
            liquidity_rank_max=10,
            gross_exposure=0.5,
            exclude_symbols=(),
        ),
    }

    payload = run_forward_sleeves(tmp_path, config=config, now=now, client=_FakeBybit(now), sleeves=sleeves)

    core_trades = read_dataset(tmp_path / "forward_sleeves" / "core", "forward_paper_trades")
    control_trades = read_dataset(tmp_path / "forward_sleeves" / "control", "forward_paper_trades")
    assert payload["rows"]["sleeves"] == 2
    assert {row["sleeve"] for row in payload["results"]} == {"core", "control"}
    assert core_trades.height == 2
    assert control_trades.height >= 2
    assert (tmp_path / "reports" / "forward_sleeves_report.md").exists()
    assert (tmp_path / "reports" / "forward_sleeves" / "core" / "forward_paper_report.md").exists()
    assert (tmp_path / "reports" / "forward_sleeves" / "control" / "forward_paper_report.md").exists()


def _research_config(tmp_path: Path, *, take_profit_pct: float = 0.0, stop_delay_minutes: int = 15) -> ResearchConfig:
    fade = DailyCloseFadeConfig(
        signal_minute=22 * 60 + 15,
        top_n=2,
        hold_minutes=180,
        entry_delay_minutes=1,
        gross_exposure=1.0,
        score="vol_adjusted_day_return",
        pump_filter="pump",
        min_age_days=10,
        stop_loss_pct=0.20,
        take_profit_pct=take_profit_pct,
        stop_delay_minutes=stop_delay_minutes,
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
    def __init__(self, now: datetime, *, stop_old1: bool = False) -> None:
        self.now = now
        self.stop_old1 = stop_old1
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
        signal_minute = 22 * 60 + 15
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
                if self.stop_old1 and symbol == "OLD1USDT" and minute >= signal_minute + 16:
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


def _base_price(symbol: str) -> float:
    return {
        "OLD1USDT": 100.0,
        "OLD2USDT": 90.0,
        "OLD3USDT": 80.0,
        "YOUNGUSDT": 70.0,
        "BTCUSDT": 50000.0,
    }[symbol]
