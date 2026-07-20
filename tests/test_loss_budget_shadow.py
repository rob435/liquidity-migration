"""R3a shadow loss-budget governor tests (log-only; staged, not installed)."""

from __future__ import annotations

import datetime as dt

from liquidity_migration.account_contracts import AccountEvent
from liquidity_migration.loss_budget_shadow import (
    CAPITAL_REFERENCE_USDT,
    THRESHOLD_FRACTION,
    arm_for_utc_date,
    evaluate_loss_budget_shadow,
)

DAY = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.timezone.utc)  # ordinal even -> arm B


def _ns(hour: int, minute: int = 0) -> int:
    return int(DAY.replace(hour=hour, minute=minute).timestamp() * 1e9)


def _pnl(sequence: int, *, net: float, ts_ns: int, key: str | None = None) -> AccountEvent:
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
        payload={"pnl_key": key or f"p{sequence}", "net_pnl_usdt": net, "exchange_ts_ns": ts_ns},
        prev_event_hash="0" * 64,
        state_hash="0" * 64,
        event_hash="0" * 64,
    )


class TestTrigger:
    def test_no_breach_no_block(self) -> None:
        events = [_pnl(1, net=-50.0, ts_ns=_ns(3)), _pnl(2, net=20.0, ts_ns=_ns(5))]
        report = evaluate_loss_budget_shadow(events, now_utc=DAY)
        assert report["breached"] is False
        assert report["would_block_new_entries_now"] is False
        assert report["realized_day_net_usdt"] == -30.0
        assert report["acts_on_anything"] is False

    def test_breach_records_first_crossing(self) -> None:
        events = [
            _pnl(1, net=-100.0, ts_ns=_ns(2)),
            _pnl(2, net=-60.0, ts_ns=_ns(4)),  # cumulative -160 <= -150 here
            _pnl(3, net=-40.0, ts_ns=_ns(6)),
        ]
        report = evaluate_loss_budget_shadow(events, now_utc=DAY)
        assert report["breached"] is True
        assert report["first_breach_utc"] == "2026-07-20T04:00:00Z"
        assert report["threshold_usdt"] == THRESHOLD_FRACTION * CAPITAL_REFERENCE_USDT

    def test_recovery_does_not_untrip(self) -> None:
        events = [
            _pnl(1, net=-160.0, ts_ns=_ns(2)),
            _pnl(2, net=+200.0, ts_ns=_ns(3)),  # day recovers, block still holds
        ]
        report = evaluate_loss_budget_shadow(events, now_utc=DAY)
        assert report["breached"] is True
        assert report["realized_day_net_usdt"] == 40.0

    def test_out_of_day_rows_are_ignored(self) -> None:
        prev_day = int((DAY - dt.timedelta(days=1)).timestamp() * 1e9)
        events = [_pnl(1, net=-500.0, ts_ns=prev_day), _pnl(2, net=-10.0, ts_ns=_ns(1))]
        report = evaluate_loss_budget_shadow(events, now_utc=DAY)
        assert report["breached"] is False
        assert report["pnl_rows_in_day"] == 1

    def test_unsorted_input_is_ordered_by_exchange_ts(self) -> None:
        events = [
            _pnl(1, net=-60.0, ts_ns=_ns(4)),
            _pnl(2, net=-100.0, ts_ns=_ns(2)),  # arrives second, happened first
        ]
        report = evaluate_loss_budget_shadow(events, now_utc=DAY)
        assert report["first_breach_utc"] == "2026-07-20T04:00:00Z"


class TestArmAssignment:
    def test_parity_is_deterministic_and_alternates(self) -> None:
        d0 = dt.date(2026, 7, 20)
        assert arm_for_utc_date(d0) == ("B" if d0.toordinal() % 2 == 0 else "A")
        assert arm_for_utc_date(d0) != arm_for_utc_date(d0 + dt.timedelta(days=1))
        assert arm_for_utc_date(d0) == arm_for_utc_date(d0 + dt.timedelta(days=2))

    def test_armed_block_requires_arm_b(self) -> None:
        events = [_pnl(1, net=-200.0, ts_ns=_ns(2))]
        report = evaluate_loss_budget_shadow(events, now_utc=DAY)
        assert report["would_block_new_entries_now"] is True
        assert report["armed_experiment_would_block"] == (report["arm"] == "B")


class TestValidation:
    def test_rejects_nonnegative_threshold(self) -> None:
        try:
            evaluate_loss_budget_shadow([], now_utc=DAY, threshold_fraction=0.01)
        except ValueError:
            pass
        else:
            raise AssertionError("positive threshold must be rejected")

    def test_rejects_nonpositive_capital(self) -> None:
        try:
            evaluate_loss_budget_shadow([], now_utc=DAY, capital_reference_usdt=0.0)
        except ValueError:
            pass
        else:
            raise AssertionError("zero capital reference must be rejected")
