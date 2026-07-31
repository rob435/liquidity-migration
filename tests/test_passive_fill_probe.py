"""Pre-declared measurement logic for the passive-vs-taker execution A/B."""

from __future__ import annotations

import math

import pytest

from liquidity_migration.research.execution.passive_fill_probe import (
    ARM_POST_ONLY,
    ARM_TAKER,
    KILL_MIN_FILL_RATE,
    REGISTERED_MIN_ATTEMPTS_PER_ARM,
    TERMINAL_FILLED,
    TERMINAL_REJECTED_WOULD_CROSS,
    TERMINAL_TIMEOUT_CANCELLED,
    AttemptRecord,
    adverse_selection_bp,
    allocate_arm,
    itt_cost_bp,
    price_cost_bp,
    summarize,
)

TAKER_FEE = 7.78


def _record(**overrides) -> AttemptRecord:
    values = dict(
        symbol="ABCUSDT",
        arm=ARM_TAKER,
        side="Sell",
        attempt_index=0,
        decision_ts_ns=1_000_000_000,
        decision_bid=99.9,
        decision_ask=100.1,
        terminal_state=TERMINAL_FILLED,
        terminal_ts_ns=2_000_000_000,
        fill_price=100.0,
    )
    values.update(overrides)
    return AttemptRecord(**values)


class TestAllocation:
    def test_is_deterministic(self) -> None:
        first = allocate_arm(salt="s", symbol="BTCUSDT", attempt_index=7)
        assert all(
            allocate_arm(salt="s", symbol="BTCUSDT", attempt_index=7) == first
            for _ in range(5)
        )

    def test_is_case_insensitive_on_symbol(self) -> None:
        assert allocate_arm(salt="s", symbol="btcusdt", attempt_index=3) == allocate_arm(
            salt="s", symbol="BTCUSDT", attempt_index=3
        )

    def test_is_roughly_balanced(self) -> None:
        arms = [allocate_arm(salt="s", symbol="BTCUSDT", attempt_index=i) for i in range(400)]
        taker = arms.count(ARM_TAKER)
        assert 140 <= taker <= 260  # ~6 sigma around 200

    def test_rejects_negative_index(self) -> None:
        with pytest.raises(ValueError, match="nonnegative"):
            allocate_arm(salt="s", symbol="BTCUSDT", attempt_index=-1)


class TestPriceCost:
    def test_sell_below_mid_is_a_cost(self) -> None:
        # Short entry filled below mid paid the spread: positive cost.
        assert price_cost_bp(side="Sell", reference_mid=100.0, fill_price=99.9) == pytest.approx(10.0)

    def test_sell_above_mid_is_an_improvement(self) -> None:
        # A passive sell filled at the ask earns half the spread: negative cost.
        assert price_cost_bp(side="Sell", reference_mid=100.0, fill_price=100.1) == pytest.approx(-10.0)

    def test_buy_signs_mirror(self) -> None:
        assert price_cost_bp(side="Buy", reference_mid=100.0, fill_price=100.1) == pytest.approx(10.0)


class TestIntentionToTreat:
    def test_taker_fill_uses_fallback_fee_when_unobserved(self) -> None:
        record = _record(fill_price=99.9)  # sell at bid: pays half spread
        cost = itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE)
        assert cost == pytest.approx(price_cost_bp(side="Sell", reference_mid=100.0, fill_price=99.9) + TAKER_FEE)

    def test_maker_fill_with_observed_fee_uses_it(self) -> None:
        record = _record(arm=ARM_POST_ONLY, fill_price=100.1, fill_fee_bp=2.0, fee_observed=True)
        cost = itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE)
        assert cost == pytest.approx(-10.0 + 2.0)

    def test_maker_fill_without_observed_fee_returns_none_not_a_fabricated_rebate(self) -> None:
        record = _record(arm=ARM_POST_ONLY, fill_price=100.1)
        assert itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE) is None

    def test_unfilled_passive_is_charged_taker_at_terminal_quote(self) -> None:
        # Price drifted against the short while waiting; ITT charges the drift
        # plus crossing the terminal spread plus the taker fee.
        record = _record(
            arm=ARM_POST_ONLY,
            terminal_state=TERMINAL_TIMEOUT_CANCELLED,
            fill_price=None,
            terminal_bid=100.4,
            terminal_ask=100.6,
        )
        cost = itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE)
        # The sell fallback fills at the terminal bid 100.4 vs decision mid
        # 100.0 — the wait happened to help the short, so the price leg is an
        # improvement and only the taker fee remains a cost.
        assert cost == pytest.approx(-40.0 + TAKER_FEE)

    def test_unfilled_passive_with_adverse_drift_pays_it(self) -> None:
        record = _record(
            arm=ARM_POST_ONLY,
            terminal_state=TERMINAL_REJECTED_WOULD_CROSS,
            fill_price=None,
            terminal_bid=99.4,
            terminal_ask=99.6,
        )
        cost = itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE)
        assert cost == pytest.approx(60.0 + TAKER_FEE)

    def test_unfilled_passive_without_terminal_quote_is_unresolved(self) -> None:
        record = _record(
            arm=ARM_POST_ONLY, terminal_state=TERMINAL_TIMEOUT_CANCELLED, fill_price=None
        )
        assert itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE) is None


class TestAdverseSelection:
    def test_short_fill_followed_by_rally_is_adverse(self) -> None:
        record = _record(arm=ARM_POST_ONLY, fill_price=100.0, post_delay_mid=100.2)
        assert adverse_selection_bp(record) == pytest.approx(20.0)

    def test_unfilled_attempt_has_no_adverse_selection(self) -> None:
        record = _record(
            arm=ARM_POST_ONLY,
            terminal_state=TERMINAL_TIMEOUT_CANCELLED,
            fill_price=None,
            post_delay_mid=None,
        )
        assert adverse_selection_bp(record) is None


class TestRecordValidation:
    def test_filled_requires_fill_price(self) -> None:
        with pytest.raises(ValueError, match="fill price"):
            _record(fill_price=None)

    def test_unfilled_rejects_fill_price(self) -> None:
        with pytest.raises(ValueError, match="cannot carry"):
            _record(terminal_state=TERMINAL_TIMEOUT_CANCELLED, fill_price=100.0)

    def test_malformed_quote_rejected(self) -> None:
        with pytest.raises(ValueError, match="bid <= ask"):
            _record(decision_bid=100.2, decision_ask=100.1)


class TestSummarize:
    def _records(self, per_arm: int) -> list[AttemptRecord]:
        rows: list[AttemptRecord] = []
        for index in range(per_arm):
            rows.append(_record(arm=ARM_TAKER, attempt_index=2 * index, fill_price=99.9))
            rows.append(
                _record(
                    arm=ARM_POST_ONLY,
                    attempt_index=2 * index + 1,
                    fill_price=100.1,
                    fill_fee_bp=1.0,
                    fee_observed=True,
                )
            )
        return rows

    def test_underpowered_sample_reports_no_verdicts(self) -> None:
        summary = summarize(self._records(5), taker_fee_bp_fallback=TAKER_FEE)
        assert summary["powered"] is False
        assert summary["kill_low_fill_rate"] is None
        assert summary["kill_no_cost_edge"] is None

    def test_powered_sample_with_passive_edge_kills_nothing(self) -> None:
        summary = summarize(
            self._records(REGISTERED_MIN_ATTEMPTS_PER_ARM), taker_fee_bp_fallback=TAKER_FEE
        )
        assert summary["powered"] is True
        assert summary["kill_low_fill_rate"] is False
        assert summary["kill_no_cost_edge"] is False
        diff = summary["itt_diff_bp_post_only_minus_taker"]
        # passive: -10 + 1 = -9; taker: +10 + 7.78 = +17.78; diff = -26.78
        assert diff == pytest.approx(-26.78)

    def test_low_fill_rate_triggers_its_kill(self) -> None:
        rows: list[AttemptRecord] = []
        for index in range(REGISTERED_MIN_ATTEMPTS_PER_ARM):
            rows.append(_record(arm=ARM_TAKER, attempt_index=2 * index, fill_price=99.9))
        # Passive arm: mostly unfilled (fallback-charged), a few fills.
        filled = int(KILL_MIN_FILL_RATE * REGISTERED_MIN_ATTEMPTS_PER_ARM) - 5
        for index in range(REGISTERED_MIN_ATTEMPTS_PER_ARM):
            if index < filled:
                rows.append(
                    _record(
                        arm=ARM_POST_ONLY,
                        attempt_index=2 * index + 1,
                        fill_price=100.1,
                        fill_fee_bp=1.0,
                        fee_observed=True,
                    )
                )
            else:
                rows.append(
                    _record(
                        arm=ARM_POST_ONLY,
                        attempt_index=2 * index + 1,
                        terminal_state=TERMINAL_TIMEOUT_CANCELLED,
                        fill_price=None,
                        terminal_bid=99.9,
                        terminal_ask=100.1,
                    )
                )
        summary = summarize(rows, taker_fee_bp_fallback=TAKER_FEE)
        assert summary["powered"] is True
        assert summary["kill_low_fill_rate"] is True
        assert summary["arms"][ARM_POST_ONLY]["fill_rate"] < KILL_MIN_FILL_RATE

    def test_registered_parameters_are_recorded_in_every_summary(self) -> None:
        summary = summarize([], taker_fee_bp_fallback=TAKER_FEE)
        registered = summary["registered"]
        assert registered["taker_fee_bp_fallback"] == TAKER_FEE
        assert registered["min_attempts_per_arm"] == REGISTERED_MIN_ATTEMPTS_PER_ARM
        assert not math.isnan(registered["kill_min_fill_rate"])
