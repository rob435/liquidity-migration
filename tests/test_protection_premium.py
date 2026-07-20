"""R3c protection-premium line in the weekly kill-criteria report (P1.4)."""

from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration.account_contracts import AccountEvent
from liquidity_migration.sleeve_kill_criteria import (
    EPOCH_START_UTC,
    evaluate_kill_criteria,
    protection_premium_section,
)


def _ns(days_in: float) -> int:
    return int((EPOCH_START_UTC + dt.timedelta(days=days_in)).timestamp() * 1e9)


def _event(sequence: int, event_type: str, symbol: str, payload: dict) -> AccountEvent:
    return AccountEvent(
        schema_version=2,
        event_id=f"id-{sequence}",
        sequence=sequence,
        event_type=event_type,
        correlation_id="batch",
        causation_id="",
        account_id="test",
        sleeve="account_accounting",
        symbol=symbol,
        wall_ts_ns=int(payload.get("exchange_ts_ns") or 1),
        monotonic_ns=sequence,
        payload=payload,
        prev_event_hash="0" * 64,
        state_hash="0" * 64,
        event_hash="0" * 64,
    )


def _native_fill(sequence: int, symbol: str, ts_ns: int, fee: float = 0.1) -> AccountEvent:
    return _event(
        sequence, "fill", symbol,
        {
            "external_execution_origin": "verified_native_stop",
            "exchange_ts_ns": ts_ns,
            "fee_usdt": fee,
            "execution_id": f"exec-{sequence}",
            "symbol": symbol,
        },
    )


def _pnl(sequence: int, symbol: str, ts_ns: int, net: float, key: str) -> AccountEvent:
    return _event(
        sequence, "pnl", symbol,
        {"pnl_key": key, "net_pnl_usdt": net, "exchange_ts_ns": ts_ns, "symbol": symbol},
    )


class TestProtectionPremium:
    def test_matches_pnl_by_symbol_and_time(self) -> None:
        fills = [_native_fill(1, "AUSDT", _ns(1))]
        pnl = [
            _pnl(2, "AUSDT", _ns(1) + 30_000_000_000, -4.5, "p-close"),  # +30s
            _pnl(3, "BUSDT", _ns(1), -99.0, "p-other-symbol"),
        ]
        section = protection_premium_section(fills + pnl, epoch_start_ns=_ns(0))
        assert section["native_stop_fills_in_epoch"] == 1
        assert section["matched_pnl_rows"] == 1
        assert section["realized_premium_net_usdt"] == -4.5
        assert section["unmatched_fills"] == 0
        assert section["provisional"] is False

    def test_unmatched_fill_flags_provisional(self) -> None:
        fills = [_native_fill(1, "AUSDT", _ns(1))]
        pnl = [_pnl(2, "AUSDT", _ns(1) + 3_600_000_000_000, -4.5, "p-late")]  # +1h: outside window
        section = protection_premium_section(fills + pnl, epoch_start_ns=_ns(0))
        assert section["unmatched_fills"] == 1
        assert section["provisional"] is True
        assert section["realized_premium_net_usdt"] == 0.0

    def test_two_fills_cannot_claim_one_pnl_row(self) -> None:
        events = [
            _native_fill(1, "AUSDT", _ns(1)),
            _native_fill(2, "AUSDT", _ns(1) + 60_000_000_000),
            _pnl(3, "AUSDT", _ns(1) + 10_000_000_000, -2.0, "p-only"),
        ]
        section = protection_premium_section(events, epoch_start_ns=_ns(0))
        assert section["matched_pnl_rows"] == 1
        assert section["unmatched_fills"] == 1
        assert section["provisional"] is True

    def test_pre_epoch_fills_are_excluded(self) -> None:
        events = [
            _native_fill(1, "AUSDT", _ns(-2)),
            _pnl(2, "AUSDT", _ns(-2), -3.0, "p-pre"),
        ]
        section = protection_premium_section(events, epoch_start_ns=_ns(0))
        assert section["native_stop_fills_in_epoch"] == 0
        assert section["realized_premium_net_usdt"] == 0.0

    def test_ordinary_fills_are_not_counted(self) -> None:
        events = [
            _event(1, "fill", "AUSDT", {"exchange_ts_ns": _ns(1), "fee_usdt": 0.1}),
            _pnl(2, "AUSDT", _ns(1), -1.0, "p"),
        ]
        section = protection_premium_section(events, epoch_start_ns=_ns(0))
        assert section["native_stop_fills_in_epoch"] == 0

    def test_weekly_report_carries_the_line(self) -> None:
        events = [
            _native_fill(1, "AUSDT", _ns(1), fee=0.25),
            _pnl(2, "AUSDT", _ns(1) + 5_000_000_000, -7.25, "p-close"),
        ]
        report = evaluate_kill_criteria(
            pnl_events=events,
            trades_by_group={"continuous": pl.DataFrame({"exit_pnl_key": ["p-close"]}), "long": pl.DataFrame()},
            now_utc=EPOCH_START_UTC + dt.timedelta(days=10),
        )
        line = report["protection_premium"]
        assert line["realized_premium_net_usdt"] == -7.25
        assert line["fill_fees_usdt"] == 0.25
        assert line["symbols"] == ["AUSDT"]
