from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration.account_contracts import AccountEvent
from liquidity_migration.sleeve_kill_criteria import (
    EPOCH_DAY90_UTC,
    EPOCH_START_UTC,
    evaluate_kill_criteria,
)


def _pnl_event(sequence: int, *, pnl_key: str, net: float, ts_ns: int) -> AccountEvent:
    return AccountEvent(
        schema_version=2,
        event_id=f"id-{sequence}",
        sequence=sequence,
        event_type="pnl",
        correlation_id="batch",
        causation_id="",
        account_id="test",
        sleeve="account_accounting",
        symbol="XUSDT",
        wall_ts_ns=ts_ns,
        monotonic_ns=sequence,
        payload={
            "pnl_key": pnl_key,
            "net_pnl_usdt": net,
            "exchange_ts_ns": ts_ns,
        },
        prev_event_hash="0" * 64,
        state_hash="0" * 64,
        event_hash="0" * 64,
    )


def _epoch_ns(days_in: float) -> int:
    return int((EPOCH_START_UTC + dt.timedelta(days=days_in)).timestamp() * 1e9)


def _trades(keys: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"exit_pnl_key": keys})


def _mid_epoch_now() -> dt.datetime:
    return EPOCH_START_UTC + dt.timedelta(days=10)


def test_healthy_record_reports_no_trip() -> None:
    events = [
        _pnl_event(1, pnl_key="p1", net=5.0, ts_ns=_epoch_ns(1)),
        _pnl_event(2, pnl_key="p2", net=-2.0, ts_ns=_epoch_ns(2)),
    ]
    report = evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group={"continuous": _trades(["p1", "p2"]), "long": pl.DataFrame()},
        now_utc=_mid_epoch_now(),
    )
    assert report["verdict"] == "NO TRIP"
    cont = report["sleeves"]["continuous"]
    assert cont["attributed_round_trips_in_epoch"] == 2
    assert cont["cumulative_net_usdt"] == 3.0
    assert cont["peak_to_trough_usdt"] == -2.0
    assert report["unattributed"]["pnl_rows_in_epoch"] == 0


def test_k1_trips_on_epoch_drawdown() -> None:
    events = [
        _pnl_event(1, pnl_key="p1", net=100.0, ts_ns=_epoch_ns(1)),
        _pnl_event(2, pnl_key="p2", net=-650.0, ts_ns=_epoch_ns(2)),
    ]
    report = evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group={"continuous": _trades(["p1", "p2"]), "long": pl.DataFrame()},
        now_utc=_mid_epoch_now(),
    )
    assert report["sleeves"]["continuous"]["k1"]["tripped"] is True
    assert "continuous:K1" in report["tripped"]
    assert report["verdict"].startswith("TRIP")


def test_pre_epoch_pnl_is_excluded() -> None:
    before = int((EPOCH_START_UTC - dt.timedelta(days=2)).timestamp() * 1e9)
    events = [_pnl_event(1, pnl_key="p1", net=-9_999.0, ts_ns=before)]
    report = evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group={"continuous": _trades(["p1"]), "long": pl.DataFrame()},
        now_utc=_mid_epoch_now(),
    )
    assert report["verdict"] == "NO TRIP"
    assert report["sleeves"]["continuous"]["attributed_round_trips_in_epoch"] == 0


def test_material_unattributed_pnl_marks_k1_provisional() -> None:
    events = [_pnl_event(1, pnl_key="orphan", net=-60.0, ts_ns=_epoch_ns(1))]
    report = evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group={"continuous": pl.DataFrame(), "long": pl.DataFrame()},
        now_utc=_mid_epoch_now(),
    )
    assert report["unattributed"]["pnl_rows_in_epoch"] == 1
    assert report["unattributed"]["k1_read_provisional"] is True


def test_k2_and_k3_evaluate_only_at_day90() -> None:
    keys = [f"p{i}" for i in range(35)]
    events = [
        _pnl_event(i + 1, pnl_key=key, net=-1.0, ts_ns=_epoch_ns(1 + i * 0.1))
        for i, key in enumerate(keys)
    ]
    trades = {"continuous": _trades(keys), "long": pl.DataFrame()}
    mid = evaluate_kill_criteria(
        pnl_events=events, trades_by_group=trades, now_utc=_mid_epoch_now()
    )
    assert mid["sleeves"]["continuous"]["k2"]["tripped"] is False
    at_day90 = evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group=trades,
        now_utc=EPOCH_DAY90_UTC + dt.timedelta(hours=1),
    )
    cont = at_day90["sleeves"]["continuous"]
    # 35 round trips, cumulative -35: K2 dead-run trips; K3 sample is met.
    assert cont["k2"]["tripped"] is True
    assert cont["k3"]["tripped"] is False
    # LONG has zero round trips at day 90: K3 trips.
    assert at_day90["sleeves"]["long"]["k3"]["tripped"] is True
