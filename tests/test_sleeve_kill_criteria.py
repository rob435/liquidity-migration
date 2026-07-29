from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration.account_contracts import AccountEvent
from liquidity_migration.sleeve_kill_criteria import (
    CARRY_EPOCH_START_UTC,
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
    assert at_registration == {"continuous": -500.0, "long": -400.0, "carry": -3_000.0}
    deployed = k1_drawdown_limits(250_000.0)
    assert deployed == {"continuous": -12_500.0, "long": -10_000.0, "carry": -75_000.0}
    # The carry registration scales its 30% by the committed carry notional
    # multiplier (the deployed carry book size), and ONLY the carry limit.
    half_sized = k1_drawdown_limits(250_000.0, carry_notional_multiplier=0.5)
    assert half_sized == {"continuous": -12_500.0, "long": -10_000.0, "carry": -37_500.0}

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


def _carry_ns(days_in: float) -> int:
    return int((CARRY_EPOCH_START_UTC + dt.timedelta(days=days_in)).timestamp() * 1e9)


def test_carry_k1_trips_at_thirty_percent_of_scaled_reference() -> None:
    """Carry K1: forward drawdown beyond 30% of capital reference x carry
    notional multiplier (docs/preregistration/carry_sleeve_kill_criteria_2026-07-29.md)."""

    events = [
        _pnl_event(1, pnl_key="c1", net=100.0, ts_ns=_carry_ns(1)),
        _pnl_event(2, pnl_key="c2", net=-3_050.0, ts_ns=_carry_ns(2)),
    ]
    trades = {"carry": _trades(["c1", "c2"])}
    report = _evaluate(
        pnl_events=events,
        trades_by_group=trades,
        now_utc=CARRY_EPOCH_START_UTC + dt.timedelta(days=10),
    )
    carry = report["sleeves"]["carry"]  # type: ignore[index]
    assert carry["k1"]["limit_usdt"] == -3_000.0
    assert carry["k1"]["tripped"] is True
    assert "carry:K1" in report["tripped"]  # type: ignore[operator]

    # The same loss under a half-sized carry book (multiplier 0.5) is judged
    # against -1,500 and still trips; a loss inside the scaled limit does not.
    inside = [
        _pnl_event(1, pnl_key="c1", net=100.0, ts_ns=_carry_ns(1)),
        _pnl_event(2, pnl_key="c2", net=-1_400.0, ts_ns=_carry_ns(2)),
    ]
    ok_report = evaluate_kill_criteria(
        pnl_events=inside,
        trades_by_group={"carry": _trades(["c1", "c2"])},
        now_utc=CARRY_EPOCH_START_UTC + dt.timedelta(days=10),
        capital_reference_usdt=REGISTERED_CAPITAL_REFERENCE_USDT,
        carry_notional_multiplier=0.5,
    )
    assert ok_report["sleeves"]["carry"]["k1"]["limit_usdt"] == -1_500.0
    assert ok_report["sleeves"]["carry"]["k1"]["tripped"] is False


def test_carry_forward_clock_starts_at_the_carry_deployment_epoch() -> None:
    """A carry-attributed PNL row before the carry change point is not forward
    evidence, even though it postdates the 2026-07-19 LONG/CONTINUOUS epoch."""

    before_carry = int((CARRY_EPOCH_START_UTC - dt.timedelta(days=2)).timestamp() * 1e9)
    assert before_carry > int(EPOCH_START_UTC.timestamp() * 1e9)
    events = [_pnl_event(1, pnl_key="c1", net=-9_999.0, ts_ns=before_carry)]
    report = _evaluate(
        pnl_events=events,
        trades_by_group={"carry": _trades(["c1"])},
        now_utc=CARRY_EPOCH_START_UTC + dt.timedelta(days=5),
    )
    carry = report["sleeves"]["carry"]  # type: ignore[index]
    assert carry["attributed_round_trips_in_epoch"] == 0
    assert carry["k1"]["tripped"] is False
    assert carry["epoch_start_utc"] == "2026-07-29T00:00:00Z"


def test_carry_k2_rides_the_120_forward_day_clock_without_day90_gate() -> None:
    """Carry K2's executable subset: >= 120 forward days from the carry epoch
    and cumulative net <= 0 with at least one attributed round trip. The
    deployed-share and consecutiveness clauses stay manual and are named in
    the report."""

    events = [_pnl_event(1, pnl_key="c1", net=-10.0, ts_ns=_carry_ns(3))]
    trades = {"carry": _trades(["c1"])}

    early = _evaluate(
        pnl_events=events,
        trades_by_group=trades,
        now_utc=CARRY_EPOCH_START_UTC + dt.timedelta(days=119),
    )
    carry_early = early["sleeves"]["carry"]  # type: ignore[index]
    assert carry_early["k2"]["requires_day90"] is False
    assert carry_early["k2"]["applicable"] is False
    assert carry_early["k2"]["tripped"] is False

    late = _evaluate(
        pnl_events=events,
        trades_by_group=trades,
        now_utc=CARRY_EPOCH_START_UTC + dt.timedelta(days=121),
    )
    carry_late = late["sleeves"]["carry"]  # type: ignore[index]
    assert carry_late["k2"]["applicable"] is True
    assert carry_late["k2"]["tripped"] is True
    assert "carry:K2" in late["tripped"]  # type: ignore[operator]
    assert any("deployed-share" in item for item in carry_late["manual_criteria"])


def test_carry_has_no_day90_sample_criterion_and_names_manual_checks() -> None:
    """Carry's insufficient-sample clause (K4, deployed days) is manual-only:
    day 90 of the shared epoch must NOT trip a carry K3, and the manual K3/K4
    clauses are named in the weekly report instead of silently absent."""

    report = _evaluate(
        pnl_events=[],
        trades_by_group={"carry": pl.DataFrame()},
        now_utc=EPOCH_DAY90_UTC + dt.timedelta(days=1),
    )
    carry = report["sleeves"]["carry"]  # type: ignore[index]
    assert carry["k3"]["evaluates"] == "manual_only"
    assert carry["k3"]["min_round_trips_at_day90"] is None
    assert carry["k3"]["tripped"] is False
    assert "carry:K3" not in report["tripped"]  # type: ignore[operator]
    manual = "\n".join(carry["manual_criteria"])
    assert "K3 mechanism break" in manual
    assert "K4 insufficient sample" in manual
    # The LONG/CONTINUOUS day-90 semantics are untouched by the carry entry.
    assert report["sleeves"]["long"]["k3"]["tripped"] is True  # type: ignore[index]
    assert report["sleeves"]["continuous"]["k3"]["tripped"] is True  # type: ignore[index]


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
