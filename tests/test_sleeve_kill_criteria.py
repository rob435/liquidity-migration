from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration.account_contracts import AccountEvent
from liquidity_migration.sleeve_kill_criteria import (
    EPOCH_DAY90_UTC,
    EPOCH_START_UTC,
    K3_EXTENSION_DAYS,
    REGISTERED_CAPITAL_REFERENCE_USDT,
    evaluate_kill_criteria,
    k1_drawdown_limits,
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


def _evaluate(
    *,
    pnl_events: list[AccountEvent],
    trades_by_group: dict[str, pl.DataFrame],
    now_utc: dt.datetime,
    capital_reference_usdt: float = REGISTERED_CAPITAL_REFERENCE_USDT,
) -> dict[str, object]:
    return evaluate_kill_criteria(
        pnl_events=pnl_events,
        trades_by_group=trades_by_group,
        now_utc=now_utc,
        capital_reference_usdt=capital_reference_usdt,
    )


def test_healthy_record_reports_no_trip() -> None:
    events = [
        _pnl_event(1, pnl_key="p1", net=5.0, ts_ns=_epoch_ns(1)),
        _pnl_event(2, pnl_key="p2", net=-2.0, ts_ns=_epoch_ns(2)),
    ]
    report = _evaluate(
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
    report = _evaluate(
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
    report = _evaluate(
        pnl_events=events,
        trades_by_group={"continuous": _trades(["p1"]), "long": pl.DataFrame()},
        now_utc=_mid_epoch_now(),
    )
    assert report["verdict"] == "NO TRIP"
    assert report["sleeves"]["continuous"]["attributed_round_trips_in_epoch"] == 0


def test_material_unattributed_pnl_marks_k1_provisional() -> None:
    events = [_pnl_event(1, pnl_key="orphan", net=-60.0, ts_ns=_epoch_ns(1))]
    report = _evaluate(
        pnl_events=events,
        trades_by_group={"continuous": pl.DataFrame(), "long": pl.DataFrame()},
        now_utc=_mid_epoch_now(),
    )
    assert report["unattributed"]["pnl_rows_in_epoch"] == 1
    assert report["unattributed"]["k1_read_provisional"] is True


def test_continuous_k2_and_k3_evaluate_only_at_day90() -> None:
    keys = [f"p{i}" for i in range(35)]
    events = [
        _pnl_event(i + 1, pnl_key=key, net=-1.0, ts_ns=_epoch_ns(1 + i * 0.1))
        for i, key in enumerate(keys)
    ]
    trades = {"continuous": _trades(keys), "long": pl.DataFrame()}
    mid = _evaluate(
        pnl_events=events, trades_by_group=trades, now_utc=_mid_epoch_now()
    )
    assert mid["sleeves"]["continuous"]["k2"]["tripped"] is False
    at_day90 = _evaluate(
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


def test_long_k2_evaluates_as_soon_as_forty_round_trips_exist() -> None:
    """The registration gates the day-90/60-forward-day condition on CONTINUOUS
    only and says LONG K2 evaluates "once 40 completed round trips exist
    (whenever that occurs)". Gating LONG on day 90 too let a dead run keep
    trading for up to two extra months (2026-07-27 audit H2)."""

    keys = [f"L{i}" for i in range(40)]
    events = [
        _pnl_event(i + 1, pnl_key=key, net=-1.0, ts_ns=_epoch_ns(1 + i * 0.1))
        for i, key in enumerate(keys)
    ]
    trades = {"continuous": pl.DataFrame(), "long": _trades(keys)}
    report = _evaluate(
        pnl_events=events, trades_by_group=trades, now_utc=_mid_epoch_now()
    )
    long_sleeve = report["sleeves"]["long"]  # type: ignore[index]
    assert long_sleeve["k2"]["requires_day90"] is False
    assert long_sleeve["k2"]["applicable"] is True
    assert long_sleeve["k2"]["tripped"] is True
    assert long_sleeve["expectancy_per_trade_usdt"] < 0.0
    assert "long:K2" in report["tripped"]  # type: ignore[operator]

    # One round trip short: still not applicable, and never trips early.
    short_keys = keys[:39]
    short_report = _evaluate(
        pnl_events=[event for event in events if event.payload["pnl_key"] in short_keys],
        trades_by_group={"continuous": pl.DataFrame(), "long": _trades(short_keys)},
        now_utc=_mid_epoch_now(),
    )
    assert short_report["sleeves"]["long"]["k2"]["applicable"] is False  # type: ignore[index]
    assert short_report["sleeves"]["long"]["k2"]["tripped"] is False  # type: ignore[index]


def test_k1_limits_are_the_registered_percentage_of_the_committed_capital_reference() -> None:
    """Commit 58c3432 scaled the profile 25x without amending the absolute
    limits, redefining K1 from ~10% of maximum deployed gross to ~0.4% and
    making a false trip on one routine stop-out near-certain (audit H3)."""

    at_registration = k1_drawdown_limits(REGISTERED_CAPITAL_REFERENCE_USDT)
    assert at_registration == {"continuous": -500.0, "long": -400.0}
    deployed = k1_drawdown_limits(250_000.0)
    assert deployed == {"continuous": -12_500.0, "long": -10_000.0}

    # A routine 1.5-ATR stop-out at the deployed sizing must not trip K1.
    events = [_pnl_event(1, pnl_key="p1", net=-1_100.0, ts_ns=_epoch_ns(1))]
    trades = {"continuous": pl.DataFrame(), "long": _trades(["p1"])}
    report = _evaluate(
        pnl_events=events,
        trades_by_group=trades,
        now_utc=_mid_epoch_now(),
        capital_reference_usdt=250_000.0,
    )
    assert report["sleeves"]["long"]["k1"]["tripped"] is False  # type: ignore[index]
    assert report["sleeves"]["long"]["k1"]["limit_usdt"] == -10_000.0  # type: ignore[index]
    assert report["capital_reference_usdt"] == 250_000.0  # type: ignore[index]

    # The same loss at the original reference does trip, unchanged.
    at_original = _evaluate(
        pnl_events=events, trades_by_group=trades, now_utc=_mid_epoch_now()
    )
    assert at_original["sleeves"]["long"]["k1"]["tripped"] is True  # type: ignore[index]


def test_long_k3_extension_retires_on_an_insufficient_extended_sample() -> None:
    keys = [f"E{i}" for i in range(20)]
    events = [
        _pnl_event(i + 1, pnl_key=key, net=1.0, ts_ns=_epoch_ns(1 + i * 0.1))
        for i, key in enumerate(keys)
    ]
    trades = {"continuous": pl.DataFrame(), "long": _trades(keys)}
    at_extension = _evaluate(
        pnl_events=events,
        trades_by_group=trades,
        now_utc=EPOCH_START_UTC + dt.timedelta(days=90 + K3_EXTENSION_DAYS + 1),
    )
    long_sleeve = at_extension["sleeves"]["long"]  # type: ignore[index]
    assert long_sleeve["k3"]["tripped"] is False
    assert long_sleeve["k3"]["extension_tripped"] is True
    assert "long:K3-extension" in at_extension["tripped"]  # type: ignore[operator]
