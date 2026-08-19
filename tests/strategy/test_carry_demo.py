"""Target-only decision contract for the CARRY demo producer: close-keyed venue
view, fail-closed data guards, diff-based target planner, account-owner publication.

The integration tests replay the registered rule (``decide_book`` over
``configs/lane2_carry_hold_v4.json``) on a deterministic synthetic market: period-3
price pattern (ret_3d exactly 0, outside v4's [-0.30, 0.0) toxic band because the
high edge is exclusive; 30d daily vol ~6.5% so the dead-name floor passes) and 8h
funding prints that are benign except for the named deep symbols — deep every
settlement, so v4's crowding-persistence multiplier stays at full size.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

import liquidity_migration.strategy.carry_demo as module
import liquidity_migration.strategy.strategy_planning as planning_module
from liquidity_migration.core._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.marketdata.kline_store import KlineStore
from liquidity_migration.account.account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    entry_attempt_key,
)
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.strategy.carry_demo import (
    CARRY_COMPONENT_ID,
    CARRY_CYCLES_DATASET,
    CARRY_FUNDING_DATASET,
    CARRY_STRATEGY_ID,
    CarryCycleState,
    CarryDecision,
    CarryDemoCycleConfig,
    CarrySleeveError,
    _carry_target_plan,
    _carry_venue_view,
    _validate_carry_demo_config,
    _validate_carry_view_health,
    carry_decision_ts_ms,
    format_carry_demo_cycle_summary,
    load_carry_config,
    run_carry_demo_cycle,
)
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.data.storage import read_dataset
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload

# A day boundary far from any real calendar edge; divisible by 8h so the synth
# funding grid lands exactly on 00:00/08:00/16:00.
D0 = 20_000 * MS_PER_DAY
NOW_MS = D0 + 25 * 60_000  # 00:25 UTC: past the 20-minute kline lag
DEEP_A = "DEEPAUSDT"
DEEP_B = "DEEPBUSDT"
RESIZED = "RESIZEUSDT"
STANDGONE = "STANDGONEUSDT"
FILLER = tuple(f"F{index:02d}USDT" for index in range(52))
ALL_SYMBOLS = (*FILLER, DEEP_A, DEEP_B, RESIZED, STANDGONE)
#: Period-3 multiplier: ret_3d == 0 at every bar (toxic band never engages)
#: while 24h returns cycle {+9.0%, -6.4%, -2.0%} (30d vol ~6.5% > the 5% floor).
PATTERN = (1.00, 1.09, 1.02)
DEEP_SINCE_MS = D0 - 12 * MS_PER_DAY
EQUITY = 10_000.0


def _base_price(symbol: str) -> float:
    return 50.0 + (ALL_SYMBOLS.index(symbol) % 40)


def _synth_klines(symbols: list[str], *, start_ms: int, end_ms: int) -> pl.DataFrame:
    """Hourly bars with opens in [start, end] — INCLUSIVE, the real reader's
    contract (store, cache, and REST all treat end as the newest requested
    bar's open). This shim used to claim and implement an exclusive end; that
    mirrored the production +1h window bug instead of catching it."""

    opens = pl.DataFrame(
        {"ts_ms": pl.int_range(start_ms, end_ms + MS_PER_HOUR, MS_PER_HOUR, eager=True)}
    )
    per_symbol = pl.DataFrame(
        {
            "symbol": list(symbols),
            "base": [_base_price(symbol) for symbol in symbols],
            "turnover_quote": [
                1_000_000.0 * (len(ALL_SYMBOLS) - ALL_SYMBOLS.index(symbol))
                for symbol in symbols
            ],
        }
    )
    pattern_index = (pl.col("ts_ms") // MS_PER_DAY) % 3
    return opens.join(per_symbol, how="cross").with_columns(
        (
            pl.col("base")
            * pl.when(pattern_index == 0)
            .then(PATTERN[0])
            .when(pattern_index == 1)
            .then(PATTERN[1])
            .otherwise(PATTERN[2])
        ).alias("close")
    ).select("ts_ms", "symbol", "close", "turnover_quote")


def _funding_rate(symbol: str, ts_ms: int) -> float:
    if symbol == DEEP_B and ts_ms > DEEP_SINCE_MS:
        return -0.0025  # -25 bp: the deepest trail, must rank first
    if symbol in (DEEP_A, RESIZED) and ts_ms > DEEP_SINCE_MS:
        return -0.0015  # -15 bp: below the -10 bp entry print
    return 0.0001  # +1 bp: benign, never enters


def _funding_rows(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    grid = 8 * MS_PER_HOUR
    first = ((start_ms + grid - 1) // grid) * grid
    return [
        {"fundingRateTimestamp": str(ts), "fundingRate": str(_funding_rate(symbol, ts))}
        for ts in range(first, end_ms, grid)
    ]


class _FakeCarryMarket:
    """Public-data-only fake; any order-authority attribute would be a bug."""

    def __init__(self, *, tickers_fail: bool = False) -> None:
        self.tickers_fail = tickers_fail
        self.funding_calls: list[tuple[str, int, int]] = []

    def get_tickers(self) -> list[dict[str, Any]]:
        if self.tickers_fail:
            raise RuntimeError("synthetic ticker outage")
        return [
            {
                "symbol": symbol,
                "turnover24h": str(1_000_000.0 * (len(ALL_SYMBOLS) - index) * 24),
            }
            for index, symbol in enumerate(ALL_SYMBOLS)
        ]

    def get_funding_history(self, symbol: str, start: int, end: int) -> list[dict[str, Any]]:
        self.funding_calls.append((symbol, start, end))
        return _funding_rows(symbol, start, end)


def _route(tmp_path: Path, *, environment: str = "demo") -> AccountRoute:
    return ensure_account_route(
        account_id=("bybit-demo-unified" if environment == "demo" else "bybit-mainnet-unified"),
        environment=environment,
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _routed_config(tmp_path: Path, *, environment: str = "demo", **overrides: Any) -> CarryDemoCycleConfig:
    values: dict[str, Any] = {
        "execution_environment": environment,
        "account_execution_root": str(tmp_path / "account"),
        "account_intent_inbox_root": str(tmp_path / "inbox"),
        # These integration fixtures hand-compute expected weights under the
        # v4 rule (straight depth ladder, no flow/whale halvings); they test
        # the cycle machinery, not the deployed default. v6-specific behavior
        # has its own tests (TestWhaleFeed / TestV6DecidesLive below).
        "strategy_profile": "v4",
    }
    values.update(overrides)
    return CarryDemoCycleConfig(**values)


def _standing_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "trade_id": CARRY_COMPONENT_ID,
                "target_key": f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/{STANDGONE}",
                "strategy_id": CARRY_STRATEGY_ID,
                "symbol": STANDGONE,
                "status": "open",
                "signed_qty": 2.0,
                "target_reference_price": 100.0,
            },
            {
                "trade_id": CARRY_COMPONENT_ID,
                "target_key": f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/{RESIZED}",
                "strategy_id": CARRY_STRATEGY_ID,
                "symbol": RESIZED,
                "status": "open",
                "signed_qty": 1.0,
                "target_reference_price": 100.0,
            },
        ]
    )


def _patch_planning(
    monkeypatch: pytest.MonkeyPatch,
    *,
    standing: pl.DataFrame | None = None,
    owner_health_error: bool = False,
) -> None:
    frame = standing if standing is not None else pl.DataFrame()

    def owner_health(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        if owner_health_error:
            raise RuntimeError("owner health receipt is stale")
        return SimpleNamespace(equity_usdt=EQUITY, observed_wall_ts_ms=0)

    monkeypatch.setattr(planning_module, "require_recent_engine_account", owner_health)
    monkeypatch.setattr(
        planning_module, "canonical_strategy_trade_rows", lambda *_a, **_k: frame
    )
    monkeypatch.setattr(
        planning_module, "terminal_entry_attempt_keys", lambda *_a, **_k: frozenset()
    )


def _patch_demo_market_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the heavy kline path through the synthetic generator.

    The shim honours the real downloader's boundary contract (opens in
    [start, end], inclusive); the funding cache, venue view, and decision
    replay all run for real on top of it.
    """

    def download(symbols: list[str], *, start_ms: int, end_ms: int, **_kwargs: Any) -> tuple[pl.DataFrame, dict[str, int]]:
        frame = _synth_klines(symbols, start_ms=start_ms, end_ms=end_ms)
        return frame, {
            "cache_rows": 0,
            "fetched_rows": frame.height,
            "output_rows": frame.height,
            "fetch_symbols": len(symbols),
        }

    monkeypatch.setattr(module, "_download_recent_1h_klines", download)
    monkeypatch.setattr(
        module,
        "_demo_instruments",
        lambda *_a, **_k: pl.DataFrame(
            {
                "symbol": pl.Series([], dtype=pl.String),
                "launch_time_ms": pl.Series([], dtype=pl.Int64),
            }
        ),
    )


def _patch_demo_market_data_ws_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same synthetic bars, reported as a clean WS-store-served build.

    The freeze-ahead path only trusts a build the WS store served without REST
    repair (``fetched_rows == 0``, ``store_rows > 0``); the default shim above
    reports a REST build and must keep refusing to freeze ahead.
    """

    def download(symbols: list[str], *, start_ms: int, end_ms: int, **_kwargs: Any) -> tuple[pl.DataFrame, dict[str, int]]:
        frame = _synth_klines(symbols, start_ms=start_ms, end_ms=end_ms)
        return frame, {
            "cache_rows": frame.height,
            "fetched_rows": 0,
            "output_rows": frame.height,
            "fetch_symbols": 0,
            "store_rows": frame.height,
        }

    monkeypatch.setattr(module, "_download_recent_1h_klines", download)
    monkeypatch.setattr(
        module,
        "_demo_instruments",
        lambda *_a, **_k: pl.DataFrame(
            {
                "symbol": pl.Series([], dtype=pl.String),
                "launch_time_ms": pl.Series([], dtype=pl.Int64),
            }
        ),
    )


def test_carry_decision_day_rolls_at_20_minutes_after_midnight() -> None:
    assert carry_decision_ts_ms(D0 + 19 * 60_000) == D0 - MS_PER_DAY
    assert carry_decision_ts_ms(D0 + 20 * 60_000) == D0
    assert carry_decision_ts_ms(D0 + 23 * MS_PER_HOUR) == D0


def test_carry_venue_view_close_keys_bars_and_ages_funding_exactly() -> None:
    klines = pl.DataFrame(
        {
            "ts_ms": [D0 - 3 * MS_PER_HOUR, D0 - 2 * MS_PER_HOUR, D0 - MS_PER_HOUR],
            "symbol": ["AUSDT"] * 3,
            "close": [10.0, 11.0, 12.0],
            "turnover_quote": [5.0, 6.0, 7.0],
        }
    )
    funding = pl.DataFrame(
        {
            "symbol": ["AUSDT"],
            "funding_ts_ms": [D0 - 2 * MS_PER_HOUR],
            "funding_rate": [-0.001],
        }
    )

    view = _carry_venue_view(
        klines, funding, window_start_ms=D0 - 2 * MS_PER_HOUR, max_bar_ts_ms=D0
    )

    # A kline stamped with open T is only knowable at T+1h; the row keyed T
    # must therefore carry the PREVIOUS hour's close.
    assert view.get_column("bar_ts_ms").to_list() == [D0 - 2 * MS_PER_HOUR, D0 - MS_PER_HOUR, D0]
    assert view.get_column("by_close").to_list() == [10.0, 11.0, 12.0]
    rows = {int(row["bar_ts_ms"]): row for row in view.to_dicts()}
    # The settlement bar must be EXACTLY 0.0 (the registered settlement
    # detector depends on it); later ages carry the same float-epsilon noise
    # as the research panel's identical expression (one hour reads as
    # 0.999...9, which is precisely what _settlement_flag's <0.5 predicate
    # was calibrated against).
    assert rows[D0 - 2 * MS_PER_HOUR]["by_funding_age_h"] == 0.0
    assert rows[D0 - MS_PER_HOUR]["by_funding_age_h"] == pytest.approx(1.0)
    assert rows[D0]["by_funding_age_h"] == pytest.approx(2.0)
    assert all(row["by_funding"] == -0.001 for row in view.to_dicts())

    no_prior = _carry_venue_view(
        klines,
        funding.with_columns(pl.col("funding_ts_ms") + 10 * MS_PER_HOUR),
        window_start_ms=D0 - 2 * MS_PER_HOUR,
        max_bar_ts_ms=D0,
    )
    assert no_prior.get_column("by_funding").null_count() == no_prior.height


def test_view_health_guards_refuse_broken_funding_inputs() -> None:
    healthy = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "bar_ts_ms": [D0, D0],
            "by_close": [1.0, 2.0],
            "by_turnover_quote": [1.0, 2.0],
            "by_funding": [0.0001, -0.001],
            "by_funding_age_h": [0.0, 8.0],
        }
    )
    _validate_carry_view_health(healthy, decision_ts_ms=D0, standing_symbols={"A"})

    all_null = healthy.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("by_funding"),
        pl.lit(None, dtype=pl.Float64).alias("by_funding_age_h"),
    )
    with pytest.raises(CarrySleeveError, match="coverage"):
        _validate_carry_view_health(all_null, decision_ts_ms=D0, standing_symbols=set())

    stale_standing = healthy.with_columns(
        pl.when(pl.col("symbol") == "A")
        .then(26.0)
        .otherwise(pl.col("by_funding_age_h"))
        .alias("by_funding_age_h")
    )
    with pytest.raises(CarrySleeveError, match="stale funding"):
        _validate_carry_view_health(stale_standing, decision_ts_ms=D0, standing_symbols={"A"})
    # The same staleness on a NON-standing symbol is not a hold-blocker.
    _validate_carry_view_health(stale_standing, decision_ts_ms=D0, standing_symbols={"B"})


def _rows(
    standing_notional: dict[str, float],
    *,
    price: float = 1.0,
    strategy_id: str = CARRY_STRATEGY_ID,
) -> dict[str, tuple[float, float, str]]:
    """Planner standing book: {symbol: (notional, signed_qty, filing id)}.

    The planner needs the accepted quantity too, so it can tell real exposure from a
    zero-quantity reservation (a completed exit desire with nothing left to reduce),
    and the filing id because a component is revised under the id it was born with.
    """

    return {
        symbol: (notional, notional / price, strategy_id)
        for symbol, notional in standing_notional.items()
    }


def _plan_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "decision": CarryDecision(
            decision_ts_ms=D0,
            weights={"AUSDT": 0.05, "BUSDT": 0.04, "CUSDT": 0.03},
            universe_size=100,
            replay_days=90,
            gross=0.12,
        ),
        "rule": load_carry_config(),
        "standing_rows": {},
        "trail_by_symbol": {"AUSDT": -0.0020, "BUSDT": -0.0045, "CUSDT": -0.0010},
        "demo": CarryDemoCycleConfig(max_new_entries_per_cycle=2),
        "equity_usdt": EQUITY,
        "account_owner_health_error": "",
        "cycle_now_ms": NOW_MS,
    }
    values.update(overrides)
    return values


class TestCarryTargetPlan:
    def test_caps_new_entries_by_deepest_trailing_funding(self) -> None:
        plan = _carry_target_plan(**_plan_kwargs())

        assert plan.planned_entries == 2
        assert plan.entry_cap_deferrals == 1
        # BUSDT has the deepest (most negative) trailing crowd payment.
        assert [item.intent.symbol for item in plan.entry_intents] == ["BUSDT", "AUSDT"]
        first = plan.entry_intents[0].intent
        assert first.signed_notional_usdt == pytest.approx(0.04 * EQUITY)
        assert first.metadata["signal_ts_ms"] == D0
        assert first.metadata["signal_valid_until_ms"] == D0 + 6 * MS_PER_HOUR
        assert first.metadata[ENTRY_ATTEMPT_METADATA_KEY] == entry_attempt_key(first.target_key)
        assert first.metadata["stop_loss_pct"] == pytest.approx(0.35)
        assert "take_profit_pct" not in first.metadata
        # Decision keys stay unique across same-cycle intents despite the
        # shared component id: they are symbol-qualified.
        assert first.decision_key.endswith("/BUSDT")

    def test_the_size_floor_admits_a_ten_dollar_name_and_counts_real_dust(self) -> None:
        # The 2026-08-06 funded regression: equity 99.94, per-name weight 0.1
        # -> 9.994 USDT, six cents under the old 10.0 floor, and the whole
        # book silently stayed in cash. The venue floor is 5 USDT, so a
        # ~10 USDT name must trade; a 5 USDT name still sits under the 6.0
        # pre-filter and must be skipped AND counted.
        plan = _carry_target_plan(
            **_plan_kwargs(
                decision=CarryDecision(
                    decision_ts_ms=D0,
                    weights={"AUSDT": 0.1, "BUSDT": 0.05},
                    universe_size=100,
                    replay_days=90,
                    gross=0.15,
                ),
                trail_by_symbol={"AUSDT": -0.0020, "BUSDT": -0.0045},
                equity_usdt=99.94,
            )
        )

        assert [item.intent.symbol for item in plan.entry_intents] == ["AUSDT"]
        assert plan.entry_intents[0].intent.signed_notional_usdt == pytest.approx(9.994)
        assert plan.planned_entries == 1
        assert plan.entry_dust_skips == 1

    def test_diff_emits_exit_resize_and_respects_dead_band(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(
                standing_rows=_rows({
                    "AUSDT": 0.05 * EQUITY,  # matches target: no action
                    "CUSDT": 100.0,  # resize up to 0.03 * equity
                    "GONEUSDT": 250.0,  # not desired: exit
                }),
            )
        )

        assert [item.intent.symbol for item in plan.exit_intents] == ["GONEUSDT"]
        assert plan.exit_intents[0].intent.signed_notional_usdt == 0.0
        assert [item.intent.symbol for item in plan.resize_intents] == ["CUSDT"]
        resize = plan.resize_intents[0].intent
        assert resize.signed_notional_usdt == pytest.approx(0.03 * EQUITY)
        assert ENTRY_ATTEMPT_METADATA_KEY not in resize.metadata
        assert resize.metadata["stop_loss_pct"] == pytest.approx(0.35)
        assert plan.planned_entries == 1  # BUSDT only: A converged, C resized

    def test_owner_health_error_blocks_entries_but_not_exits(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(
                standing_rows=_rows({"GONEUSDT": 250.0, "CUSDT": 100.0}),
                equity_usdt=0.0,
                account_owner_health_error="AccountOwnerHealthHeadPending: stale",
            )
        )

        assert [item.intent.symbol for item in plan.exit_intents] == ["GONEUSDT"]
        assert plan.entry_intents == [] and plan.resize_intents == []
        assert plan.entry_blocked_reason == "account_owner_health_unavailable"

    def test_late_cycle_skips_entries_instead_of_publishing_expired_signals(self) -> None:
        plan = _carry_target_plan(**_plan_kwargs(cycle_now_ms=D0 + 6 * MS_PER_HOUR - 60_000))

        # An entry the service can only expire would terminally suppress the
        # symbol forever (stable per-symbol attempt keys); it must be skipped.
        assert plan.entry_intents == []
        assert plan.entry_validity_expired_skips == 3

    def test_intraday_equity_drift_alone_never_publishes_a_resize(self) -> None:
        """A converged book stays silent while the mark moves: sizing is anchored to the
        decision, so equity moving is not by itself an instruction to trade.
        """

        state = CarryCycleState()
        converged = {
            "AUSDT": 0.05 * EQUITY,
            "BUSDT": 0.04 * EQUITY,
            "CUSDT": 0.03 * EQUITY,
        }
        first = _carry_target_plan(
            **_plan_kwargs(standing_rows=_rows(converged), cycle_state=state)
        )
        assert first.resize_intents == []

        # Equity swings well past the old 0.1% dead-band, in both directions.
        for drift in (1.02, 0.97, 1.05, 0.93):
            plan = _carry_target_plan(
                **_plan_kwargs(
                    standing_rows=_rows(converged),
                    equity_usdt=EQUITY * drift,
                    cycle_state=state,
                )
            )
            assert plan.resize_intents == [], f"drift {drift} published a resize"

    def test_sizing_is_clamped_to_the_capital_reference(self) -> None:
        """The owner's six pre-trade caps are absolute USDT numbers calibrated against
        ``capital_reference_usdt``, but sizing reads live venue equity. Without this
        clamp the load-time envelope proof stops being true once the account grows past
        the reference.
        """

        demo = CarryDemoCycleConfig(
            max_new_entries_per_cycle=2, capital_reference_usdt=EQUITY
        )
        rich = _carry_target_plan(
            **_plan_kwargs(demo=demo, equity_usdt=EQUITY * 2.0, cycle_state=CarryCycleState())
        )
        at_reference = _carry_target_plan(
            **_plan_kwargs(demo=demo, equity_usdt=EQUITY, cycle_state=CarryCycleState())
        )

        assert [i.intent.signed_notional_usdt for i in rich.entry_intents] == pytest.approx(
            [i.intent.signed_notional_usdt for i in at_reference.entry_intents]
        )

    def test_the_clamp_is_a_ceiling_not_a_floor(self) -> None:
        """A smaller account must keep sizing off its own equity, never be
        levelled up to a reference it has not funded."""

        demo = CarryDemoCycleConfig(
            max_new_entries_per_cycle=2, capital_reference_usdt=EQUITY
        )
        small = _carry_target_plan(
            **_plan_kwargs(demo=demo, equity_usdt=EQUITY * 0.1, cycle_state=CarryCycleState())
        )

        assert small.entry_intents
        for item in small.entry_intents:
            assert abs(item.intent.signed_notional_usdt) < 0.2 * EQUITY

    def test_an_unset_capital_reference_leaves_sizing_unclamped(self) -> None:
        demo = CarryDemoCycleConfig(
            max_new_entries_per_cycle=2, capital_reference_usdt=0.0
        )
        rich = _carry_target_plan(
            **_plan_kwargs(demo=demo, equity_usdt=EQUITY * 2.0, cycle_state=CarryCycleState())
        )
        base = _carry_target_plan(
            **_plan_kwargs(demo=demo, equity_usdt=EQUITY, cycle_state=CarryCycleState())
        )

        assert abs(rich.entry_intents[0].intent.signed_notional_usdt) > abs(
            base.entry_intents[0].intent.signed_notional_usdt
        )

    def test_a_new_decision_re_anchors_the_sizing_equity(self) -> None:
        """Anchoring must not freeze the book: tomorrow sizes off tomorrow."""

        state = CarryCycleState()
        _carry_target_plan(**_plan_kwargs(cycle_state=state))
        assert state.sizing_equity_usdt == pytest.approx(EQUITY)
        assert state.sizing_equity_decision_ts_ms == D0

        tomorrow = CarryDecision(
            decision_ts_ms=D0 + MS_PER_DAY,
            weights={"AUSDT": 0.05},
            universe_size=100,
            replay_days=90,
            gross=0.05,
        )
        plan = _carry_target_plan(
            **_plan_kwargs(
                decision=tomorrow,
                equity_usdt=EQUITY * 1.5,
                cycle_now_ms=NOW_MS + MS_PER_DAY,
                cycle_state=state,
            )
        )
        assert state.sizing_equity_usdt == pytest.approx(EQUITY * 1.5)
        assert plan.entry_intents[0].intent.signed_notional_usdt == pytest.approx(
            0.05 * EQUITY * 1.5
        )

    def test_a_failed_equity_read_never_poisons_the_anchor(self) -> None:
        state = CarryCycleState()
        _carry_target_plan(**_plan_kwargs(cycle_state=state))
        _carry_target_plan(
            **_plan_kwargs(
                equity_usdt=0.0,
                account_owner_health_error="AccountOwnerHealthHeadPending: stale",
                cycle_state=state,
            )
        )
        assert state.sizing_equity_usdt == pytest.approx(EQUITY)

    def test_a_genuine_weight_change_still_clears_the_dead_band(self) -> None:
        """The band suppresses noise, not decisions."""

        state = CarryCycleState()
        plan = _carry_target_plan(
            **_plan_kwargs(
                standing_rows=_rows({"CUSDT": 0.03 * EQUITY * 0.5}),
                cycle_state=state,
            )
        )
        assert [item.intent.symbol for item in plan.resize_intents] == ["CUSDT"]
        assert plan.resize_intents[0].intent.signed_notional_usdt == pytest.approx(
            0.03 * EQUITY
        )

    def test_missing_decision_plans_nothing(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(decision=None, standing_rows=_rows({"GONEUSDT": 250.0}))
        )

        assert plan.exit_intents == [] and plan.entry_intents == [] and plan.resize_intents == []
        assert plan.entry_blocked_reason == "decision_unavailable"

    def test_a_zero_quantity_reservation_is_never_re_exited(self) -> None:
        """Once a position has closed, its accepted zero target is a reservation with
        nothing left to reduce, so re-planning its exit must publish nothing.
        """

        plan = _carry_target_plan(
            **_plan_kwargs(standing_rows={"STRANDEDUSDT": (0.0, 0.0, CARRY_STRATEGY_ID)})
        )

        assert plan.exit_intents == []
        assert plan.planned_exits == 0
        assert plan.stranded_zero_quantity_reservations == 1

    def test_a_stranded_reservation_is_inert_on_every_path(self) -> None:
        """Not re-exiting is not the same as forgetting.

        The symbol must not be re-entered underneath its own unconverged target, and it
        must not escape through the RESIZE branch either: a zero standing notional
        clears any dead-band, so a still-desired stranded name would otherwise republish
        its whole target every cycle.
        """

        decision = CarryDecision(
            decision_ts_ms=D0,
            weights={"STRANDEDUSDT": 0.05},
            universe_size=100,
            replay_days=90,
            gross=0.05,
        )
        plan = _carry_target_plan(
            **_plan_kwargs(
                decision=decision,
                standing_rows={"STRANDEDUSDT": (0.0, 0.0, CARRY_STRATEGY_ID)},
                trail_by_symbol={"STRANDEDUSDT": -0.002},
            )
        )

        assert plan.entry_intents == []
        assert plan.exit_intents == []
        assert plan.resize_intents == []
        assert plan.planned_entries == 0
        assert plan.planned_resizes == 0
        # Counted whether or not it is still desired: an operator has to clear it.
        assert plan.stranded_zero_quantity_reservations == 1

    def test_real_exposure_is_still_exited(self) -> None:
        plan = _carry_target_plan(**_plan_kwargs(standing_rows=_rows({"GONEUSDT": 250.0})))

        assert [item.intent.symbol for item in plan.exit_intents] == ["GONEUSDT"]
        assert plan.stranded_zero_quantity_reservations == 0


class TestFrozenDailyDecision:
    def _decision(self, weights: dict[str, float]) -> CarryDecision:
        return CarryDecision(
            decision_ts_ms=D0,
            weights=weights,
            universe_size=100,
            replay_days=90,
            gross=sum(weights.values()),
        )

    def test_a_bar_keeps_the_first_book_it_computed(self) -> None:
        state = CarryCycleState()
        first = self._decision({"AUSDT": 0.1, "BUSDT": 0.1})
        state.freeze_decision(
            decision_ts_ms=D0, decision=first, trail_by_symbol={"AUSDT": -0.02}, universe_eligible=103
        )

        frozen = state.frozen_decision(D0)

        assert frozen is not None
        decision, trail, eligible = frozen
        assert decision is first
        assert trail == {"AUSDT": -0.02}
        assert eligible == 103

    def test_a_new_bar_is_not_served_the_previous_book(self) -> None:
        state = CarryCycleState()
        state.freeze_decision(
            decision_ts_ms=D0,
            decision=self._decision({"AUSDT": 0.1}),
            trail_by_symbol={},
            universe_eligible=100,
        )

        assert state.frozen_decision(D0 + MS_PER_DAY) is None

    def test_the_frozen_trail_is_a_copy(self) -> None:
        state = CarryCycleState()
        trail = {"AUSDT": -0.02}
        state.freeze_decision(
            decision_ts_ms=D0,
            decision=self._decision({"AUSDT": 0.1}),
            trail_by_symbol=trail,
            universe_eligible=100,
        )
        trail["AUSDT"] = 0.0

        frozen = state.frozen_decision(D0)
        assert frozen is not None
        assert frozen[1] == {"AUSDT": -0.02}


def test_validate_carry_demo_config_rejections(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="execution_environment"):
        _validate_carry_demo_config(CarryDemoCycleConfig())
    with pytest.raises(ValueError, match="configured together"):
        _validate_carry_demo_config(
            CarryDemoCycleConfig(
                execution_environment="demo",
                account_intent_inbox_root=str(tmp_path / "inbox-only"),
            )
        )
    with pytest.raises(ValueError, match="operational target mode requires"):
        _validate_carry_demo_config(CarryDemoCycleConfig(execution_environment="demo"))
    with pytest.raises(ValueError, match="declared_stop_loss_fraction"):
        _validate_carry_demo_config(_routed_config(tmp_path, declared_stop_loss_fraction=1.0))
    with pytest.raises(ValueError, match="replay_days"):
        _validate_carry_demo_config(_routed_config(tmp_path, replay_days=30))
    with pytest.raises(ValueError, match="ws_klines_lookback_days"):
        _validate_carry_demo_config(
            _routed_config(tmp_path, ws_klines_enabled=True, ws_klines_lookback_days=45)
        )

    class _WithTelegram(CarryDemoCycleConfig):
        __slots__ = ()

        @property
        def telegram(self) -> bool:
            return True

    with pytest.raises(ValueError, match="Telegram"):
        _validate_carry_demo_config(
            _WithTelegram(
                execution_environment="demo",
                account_execution_root=str(tmp_path / "account"),
                account_intent_inbox_root=str(tmp_path / "inbox"),
            )
        )

    _validate_carry_demo_config(_routed_config(tmp_path))
    _validate_carry_demo_config(_routed_config(tmp_path, environment="mainnet"))


def test_run_cycle_publishes_exit_first_diff_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path / "route")
    demo_config = _routed_config(tmp_path / "route")
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch, standing=_standing_frame())
    market = _FakeCarryMarket()

    payload = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=demo_config,
        market_client=market,
        now_ms=NOW_MS,
    )

    assert type(payload) is PublishedTargetCyclePayload
    assert payload.route == route
    assert payload["decision_error"] is None
    assert payload["decision_stale"] is False
    assert payload["decision_ts_ms"] == D0
    assert payload["desired_book_size"] == 3  # DEEP_A, DEEP_B, RESIZED
    expected_gross = 0.1 * (45.0 / 120.0) * 2 + 0.1 * (75.0 / 120.0)
    assert payload["desired_gross_weight"] == pytest.approx(expected_gross)
    assert payload["exit_targets_queued"] == 1
    assert payload["entry_targets_queued"] == 2
    assert payload["resize_targets_queued"] == 1
    assert payload["standing_symbols"] == 2
    assert payload["universe_fetched"] == len(ALL_SYMBOLS)
    assert payload["equity_usdt"] == pytest.approx(EQUITY)

    publication = payload.publication
    exit_intent = publication.exit_requests[0].request.intents[0].intent
    assert exit_intent.symbol == STANDGONE
    assert exit_intent.signed_notional_usdt == 0.0
    assert exit_intent.target_key == f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/{STANDGONE}"
    # One grouped request carries every entry and resize of the cycle.
    assert len(publication.entry_requests) == 1
    entry_channel = [item.intent for item in publication.entry_requests[0].request.intents]
    # Deepest trailing funding first (DEEP_B at -75 bp/day), then DEEP_A,
    # then the resize revision of the already-standing RESIZED component.
    assert [intent.symbol for intent in entry_channel] == [DEEP_B, DEEP_A, RESIZED]
    deep_b, deep_a, resized = entry_channel
    assert deep_b.signed_notional_usdt == pytest.approx(0.1 * (75.0 / 120.0) * EQUITY)
    assert deep_a.signed_notional_usdt == pytest.approx(0.1 * (45.0 / 120.0) * EQUITY)
    assert deep_a.metadata["signal_ts_ms"] == D0
    assert deep_a.metadata["signal_valid_until_ms"] == D0 + 6 * MS_PER_HOUR
    assert deep_a.metadata[ENTRY_ATTEMPT_METADATA_KEY] == entry_attempt_key(deep_a.target_key)
    assert "take_profit_pct" not in deep_a.metadata
    assert resized.signed_notional_usdt == pytest.approx(0.1 * (45.0 / 120.0) * EQUITY)
    assert ENTRY_ATTEMPT_METADATA_KEY not in resized.metadata
    all_intents = [exit_intent, *entry_channel]
    assert all(intent.leverage == demo_config.entry_leverage for intent in all_intents)
    decision_keys = [intent.decision_key for intent in all_intents]
    assert len(set(decision_keys)) == len(decision_keys)

    cycles = read_dataset(tmp_path / "producer", CARRY_CYCLES_DATASET)
    assert cycles.height == 1
    assert cycles.get_column("cycle_id").to_list() == [payload["cycle_id"]]
    funding_cache = read_dataset(tmp_path / "producer", CARRY_FUNDING_DATASET)
    assert funding_cache.get_column("symbol").n_unique() == len(ALL_SYMBOLS)

    # Second cycle one minute later: the published requests are still
    # unresolved in the inbox, so the identical diff is fully suppressed —
    # the 60s cadence never duplicates in-flight work.
    second = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=demo_config,
        market_client=market,
        now_ms=NOW_MS + 60_000,
    )

    assert second["decision_error"] is None
    assert second["target_intents_queued"] == 0
    assert second["unresolved_exit_target_suppressions"] == 1
    assert second["unresolved_entry_target_suppressions"] == 3
    assert second.publication.exit_requests == ()
    assert second.publication.entry_requests == ()


def test_run_cycle_holds_standing_book_on_data_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _route(tmp_path / "route")
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch, standing=_standing_frame())

    payload = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=_FakeCarryMarket(tickers_fail=True),
        now_ms=NOW_MS,
    )

    # The ticker outage shrinks the build to the standing names, the decision
    # bar comes up thin, and the cycle must HOLD: no exits, no entries, a
    # loud decision_error, and (with no prior success anywhere) a stale flag.
    assert payload["decision_error"]
    assert payload["decision_stale"] is True
    assert payload["target_intents_queued"] == 0
    assert payload.publication.exit_requests == ()
    assert payload.publication.entry_requests == ()
    cycles = read_dataset(tmp_path / "producer", CARRY_CYCLES_DATASET)
    assert cycles.height == 1
    assert cycles.get_column("decision_error").to_list()[0]


def test_run_cycle_blocks_entries_but_exits_when_owner_health_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _route(tmp_path / "route")
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch, standing=_standing_frame(), owner_health_error=True)

    payload = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=_FakeCarryMarket(),
        now_ms=NOW_MS,
    )

    # 7af59f3 convention: equity is null, never 0.0, on an owner-health error.
    assert payload["equity_usdt"] is None
    assert payload["account_owner_health_error"]
    assert payload["entry_blocked_reason"] == "account_owner_health_unavailable"
    assert payload["exit_targets_queued"] == 1  # risk-reducing exits still flow
    assert payload["entry_targets_queued"] == 0
    assert payload["resize_targets_queued"] == 0


def test_cycle_state_throttles_funding_sweep_to_hour_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _route(tmp_path / "route")
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch)
    market = _FakeCarryMarket()
    state = CarryCycleState()

    first = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=market,
        now_ms=NOW_MS,
        cycle_state=state,
    )
    calls_after_first = len(market.funding_calls)
    second = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=market,
        now_ms=NOW_MS + 60_000,
        cycle_state=state,
    )

    assert first["funding_swept"] is True
    assert calls_after_first == len(ALL_SYMBOLS)
    # Same wall hour, same daemon state: settled prints cannot have changed,
    # so the second cycle reads the cache and makes zero funding REST calls.
    assert second["funding_swept"] is False
    assert len(market.funding_calls) == calls_after_first
    assert second["decision_error"] is None


def test_a_later_cycle_never_changes_the_days_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered rule decides once per 00:00 UTC bar and holds.

    The funding data is rewritten between two same-bar cycles so a recomputation
    WOULD move the book; the frozen decision must ignore it until the next bar.
    """

    _route(tmp_path / "route")
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch)
    state = CarryCycleState()

    first = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=_FakeCarryMarket(),
        now_ms=NOW_MS,
        cycle_state=state,
    )
    assert first["decision_frozen"] is False
    assert first["desired_book_size"] > 0

    # Make every symbol's crowd payment benign: a fresh decision on this data
    # would empty the book and flatten everything the sleeve holds.
    monkeypatch.setattr(module, "_trailing_settled_funding", lambda *_a, **_k: {})
    monkeypatch.setattr(
        module, "decide_book", lambda *_a, **_k: pytest.fail("froze bar was recomputed")
    )

    second = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=_FakeCarryMarket(),
        now_ms=NOW_MS + 60_000,
        cycle_state=state,
    )

    assert second["decision_frozen"] is True
    assert second["desired_book_size"] == first["desired_book_size"]
    assert second["desired_gross_weight"] == first["desired_gross_weight"]
    assert second["exit_targets_queued"] == 0


def test_a_failed_decision_is_not_frozen_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data hiccup at 00:20 must keep retrying, not pin an error for the day."""

    _route(tmp_path / "route")
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise CarrySleeveError("synthetic build failure")

    monkeypatch.setattr(module, "decide_book", boom)
    state = CarryCycleState()
    failed = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=_FakeCarryMarket(),
        now_ms=NOW_MS,
        cycle_state=state,
    )
    assert failed["decision_error"] is not None
    assert state.frozen_decision(carry_decision_ts_ms(NOW_MS)) is None

    monkeypatch.undo()
    _patch_demo_market_data(monkeypatch)
    _patch_planning(monkeypatch)
    recovered = run_carry_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=_routed_config(tmp_path / "route"),
        market_client=_FakeCarryMarket(),
        now_ms=NOW_MS + 60_000,
        cycle_state=state,
    )
    assert recovered["decision_error"] is None
    assert recovered["decision_frozen"] is False
    assert recovered["desired_book_size"] > 0


def test_summary_formatter_renders_flat_payload() -> None:
    line = format_carry_demo_cycle_summary(
        {
            "cycle_id": "carry-target-carry_hold_v3-1",
            "mode": "demo_target",
            "decision_ts_ms": D0,
            "decision_stale": False,
            "decision_error": None,
            "desired_book_size": 3,
            "desired_gross_weight": 0.1375,
            "standing_symbols": 2,
            "open_positions": 2,
            "exit_targets_queued": 1,
            "entry_targets_queued": 2,
            "resize_targets_queued": 1,
            "equity_usdt": 10_000.0,
        }
    )

    assert line.startswith("carry target producer")
    assert "decision_day=2024-10-04" in line
    assert "pub exit/entry/resize=1/2/1" in line
    assert "err=none" in line


def test_summary_formatter_surfaces_dust_skipped_entries() -> None:
    payload = {
        "cycle_id": "carry-target-carry_hold_v3-1",
        "mode": "mainnet_target",
        "decision_ts_ms": D0,
        "decision_stale": False,
        "decision_error": None,
        "desired_book_size": 2,
        "desired_gross_weight": 0.2,
        "standing_symbols": 0,
        "open_positions": 0,
        "exit_targets_queued": 0,
        "entry_targets_queued": 0,
        "resize_targets_queued": 0,
        "entry_dust_skips": 2,
        "equity_usdt": 99.94,
    }
    assert " dust=2 " in format_carry_demo_cycle_summary(payload)

    payload["entry_dust_skips"] = 0
    assert "dust=" not in format_carry_demo_cycle_summary(payload)


def test_cold_cache_view_trims_leading_partial_day_to_midnight() -> None:
    # A cold-started cache begins at the bootstrap hour, so the first cycle's
    # view opens mid-day and decide_book's phase guard refuses it. The cycle
    # layer must trim to the first 00:00 UTC key so the daily grid keeps the
    # registered decision clock. Replicates the trim expression directly.
    day_ms = 86_400_000
    hour_ms = 3_600_000
    start = 40 * day_ms + 3 * hour_ms  # 03:00 UTC cache start
    bars = pl.DataFrame(
        {
            "bar_ts_ms": list(range(start, start + 3 * day_ms, hour_ms)),
        }
    )
    first_ts = int(bars.get_column("bar_ts_ms").min())
    assert first_ts % day_ms != 0
    aligned_start = ((first_ts // day_ms) + 1) * day_ms
    trimmed = bars.filter(pl.col("bar_ts_ms") >= aligned_start)
    assert int(trimmed.get_column("bar_ts_ms").min()) % day_ms == 0
    # nothing beyond the partial day is lost
    assert trimmed.height == bars.height - (24 - 3)


class _CountingKlineStore(KlineStore):
    """The REAL store with a read counter.

    A hand-rolled fake here previously answered the coverage probe
    unconditionally, which hid a live defect: the carry caller passed a window
    end one bar in the future, the real probe could never pass, and the store
    never served a cycle (kline_store_rows=0 in production while this test
    stayed green). Probe semantics must come from the production class.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.get_klines_calls = 0

    def get_klines(self, symbols: list[str], *, start_ms: int, end_ms: int) -> pl.DataFrame:
        self.get_klines_calls += 1
        return super().get_klines(symbols, start_ms=start_ms, end_ms=end_ms)


def _bootstrapped_store(
    symbols: tuple[str, ...], *, newest_open_ms: int, span_days: int
) -> _CountingKlineStore:
    store = _CountingKlineStore(cache_root=None, retain_days=span_days + 14, flush_interval_seconds=0.0)
    first_open_ms = newest_open_ms - span_days * 24 * MS_PER_HOUR
    for symbol in symbols:
        store.bootstrap_symbol(
            symbol,
            [
                {
                    "start": ts_ms,
                    "open": "100.0",
                    "high": "101.0",
                    "low": "99.0",
                    "close": "100.5",
                    "volume": "10.0",
                    "turnover": "1000.0",
                }
                for ts_ms in range(first_open_ms, newest_open_ms + MS_PER_HOUR, MS_PER_HOUR)
            ],
        )
    return store


class _FakeTickerCache:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def is_seeded(self) -> bool:
        return True

    def is_stale(self, *, stale_seconds: float) -> bool:
        return False

    def snapshot_list(self, max_age_seconds: float | None = None) -> list[dict[str, Any]]:
        return list(self.rows)


def test_carry_market_build_uses_the_ws_store_and_ticker_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from liquidity_migration.strategy.carry_demo import (
        CarryCycleState,
        _build_carry_demo_market_data,
    )
    import liquidity_migration.strategy.carry_demo as carry_module

    monkeypatch.setattr(carry_module, "_demo_instruments", lambda *args, **kwargs: pl.DataFrame())
    # tickers_fail=True: a REST ticker call would raise, proving the cache served.
    market = _FakeCarryMarket(tickers_fail=True)
    cache_rows = [
        {"symbol": symbol, "turnover24h": str(1_000_000.0 * (len(ALL_SYMBOLS) - index) * 24)}
        for index, symbol in enumerate(ALL_SYMBOLS)
    ]
    now_ms = 1_760_000_000_000 - (1_760_000_000_000 % MS_PER_HOUR) + 25 * 60 * 1000
    # Mid-hour, the newest CLOSED bar's open is floor(now) - 1h. The store
    # holds exactly the cycle window (45 replay days + margin) ending there —
    # the live steady state the WS plane maintains.
    newest_open_ms = now_ms - (now_ms % MS_PER_HOUR) - MS_PER_HOUR
    store = _bootstrapped_store(ALL_SYMBOLS, newest_open_ms=newest_open_ms, span_days=46)
    config = ResearchConfig()
    demo = _routed_config(tmp_path, replay_days=45, workers=2)

    klines, funding, stats = _build_carry_demo_market_data(
        root=tmp_path / "carry-root",
        config=config,
        demo=demo,
        market=market,
        now_ms=now_ms,
        standing_symbols=set(),
        state=CarryCycleState(),
        kline_store=store,
        ticker_cache=_FakeTickerCache(cache_rows),
        state_cache_stale_seconds=120.0,
    )

    assert stats["ticker_source"] == "ws_cache"
    assert stats["data_source"] == "ws_store"
    assert store.get_klines_calls == 1
    assert int(stats["kline_fetched_rows"]) == 0
    # The store served every row; the on-disk cache was never consulted.
    assert int(stats["kline_store_rows"]) > 0
    assert int(stats["kline_store_rows"]) == int(stats["kline_output_rows"])
    assert int(stats["kline_cache_rows"]) == 0
    assert not klines.is_empty()
    # The served window ends at the newest CLOSED bar's open — the reader's
    # inclusive bar-open convention. The old +1h end made this unreachable.
    assert int(klines["ts_ms"].max()) == newest_open_ms
    assert set(klines["symbol"].unique().to_list()) == set(ALL_SYMBOLS)
    # Funding has no stream on the venue: the REST sweep still ran, once per symbol.
    assert sorted({symbol for symbol, _s, _e in market.funding_calls}) == sorted(ALL_SYMBOLS)
    assert not funding.is_empty()


class TestLegacyFilingIdDrain:
    """A component keeps the filing id it was born with; only NEW components
    file under the version-free ``CARRY_STRATEGY_ID``."""

    LEGACY = "carry_hold_v3"

    def test_legacy_component_exits_under_its_own_id(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(standing_rows=_rows({"GONEUSDT": 250.0}, strategy_id=self.LEGACY))
        )
        exit_intent = plan.exit_intents[0].intent
        assert exit_intent.target_key == (
            f"carry/{self.LEGACY}/{CARRY_COMPONENT_ID}/GONEUSDT"
        )

    def test_legacy_component_resizes_under_its_own_id_and_new_entries_do_not(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(standing_rows=_rows({"CUSDT": 100.0}, strategy_id=self.LEGACY))
        )
        resize = plan.resize_intents[0].intent
        assert resize.target_key == f"carry/{self.LEGACY}/{CARRY_COMPONENT_ID}/CUSDT"
        for item in plan.entry_intents:
            assert item.intent.target_key.startswith(
                f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/"
            )
        assert plan.planned_entries == 2  # AUSDT + BUSDT under the new id

    def test_one_symbol_under_two_filing_ids_fails_closed(self) -> None:
        frame = pl.DataFrame(
            [
                {
                    "symbol": "SPLITUSDT",
                    "strategy_id": CARRY_STRATEGY_ID,
                    "signed_qty": 1.0,
                    "target_reference_price": 100.0,
                },
                {
                    "symbol": "SPLITUSDT",
                    "strategy_id": self.LEGACY,
                    "signed_qty": 2.0,
                    "target_reference_price": 100.0,
                },
            ]
        )
        with pytest.raises(RuntimeError, match="more than one filing id"):
            module._carry_standing_rows(frame)

    def test_the_standing_notional_is_the_desire_not_the_rounded_quantity(self) -> None:
        """A resize diffs desire against desire.

        Rebuilding the standing size from the venue-rounded quantity left a gap
        one quantity step wide that no resize could ever close, so the producer
        re-proposed it every cycle and the kernel emitted nothing.
        """

        frame = pl.DataFrame(
            [
                {
                    "symbol": "STEPUSDT",
                    "strategy_id": CARRY_STRATEGY_ID,
                    "signed_qty": 3.0,  # rounded down from 3.7 by a step of 1
                    "target_reference_price": 100.0,
                    "raw_target_notional_usdt": 370.0,
                }
            ]
        )
        standing, qty, filing = module._carry_standing_rows(frame)["STEPUSDT"]
        assert standing == 370.0
        assert qty == 3.0
        assert filing == CARRY_STRATEGY_ID

    def test_a_target_without_a_raw_notional_still_reconstructs(self) -> None:
        frame = pl.DataFrame(
            [
                {
                    "symbol": "OLDUSDT",
                    "strategy_id": CARRY_STRATEGY_ID,
                    "signed_qty": 3.0,
                    "target_reference_price": 100.0,
                    "raw_target_notional_usdt": 0.0,
                }
            ]
        )
        standing, _qty, _filing = module._carry_standing_rows(frame)["OLDUSDT"]
        assert standing == 300.0


class TestCarryStrategyProfileDial:
    def test_registered_profiles_resolve_to_their_files(self) -> None:
        v3 = module.resolve_carry_strategy_profile("v3")
        v4 = module.resolve_carry_strategy_profile("v4")
        v6 = module.resolve_carry_strategy_profile("v6")
        assert v3.profile_name == "carry_hold_v3_live_v1"
        assert v3.config_path.name == "lane2_carry_hold_v3.json"
        assert v4.profile_name == "carry_hold_v4_live_v1"
        assert v4.config_path.name == "lane2_carry_hold_v4.json"
        assert v6.profile_name == "carry_hold_v6_live_v1"
        assert v6.config_path == module.CARRY_CONFIG_PATH
        # v7 (promoted 2026-08-19, later the same day as v6) is an
        # execution-clock version: it trades v6's registered membership file
        # UNCHANGED, so the config forward grade continues under one id.
        v7 = module.resolve_carry_strategy_profile("v7")
        assert v7.profile_name == "carry_hold_v7_live_v1"
        assert v7.config_path == v6.config_path
        assert v7.presettle_exit and not v6.presettle_exit
        assert module.DEFAULT_CARRY_STRATEGY_PROFILE == "v7"
        assert module.CARRY_STRATEGY_PROFILE_CHOICES == ("v3", "v4", "v6", "v7")
        # All files load through the registered rule loader; the hysteresis
        # thresholds never moved across the family.
        assert load_carry_config(v3.config_path).enter_bp == pytest.approx(
            load_carry_config(v6.config_path).enter_bp
        )
        # The whale halving is what makes v6 need the Binance feed.
        assert load_carry_config(v6.config_path).whale_cut is not None
        assert load_carry_config(v4.config_path).whale_cut is None

    def test_unknown_profile_fails_startup_validation(self) -> None:
        config = CarryDemoCycleConfig(
            execution_environment="demo",
            account_execution_root="/tmp/x",
            account_intent_inbox_root="/tmp/y",
            strategy_profile="v99",
        )
        with pytest.raises(ValueError, match="unknown CARRY strategy profile"):
            _validate_carry_demo_config(config)


# --- freeze-ahead + deadline build-skip (the fast 00:20 boundary, 2026-08-13) ---

#: 00:19 UTC: inside the 90s pre-deadline window, before the 00:20 decision
#: roll, and every input row for the D0 decision bar (the 23:00-00:00 kline
#: close and the 00:00 settlement) is already public.
PREWARM_NOW = D0 + 19 * 60_000
#: 00:20:00.001 UTC: the deadline wake's first instant.
BOUNDARY_NOW = D0 + 20 * 60_000 + 1


class TestFreezeAheadDeadline:
    def _prewarm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state: CarryCycleState,
    ) -> PublishedTargetCyclePayload:
        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)
        _patch_planning(monkeypatch)
        return run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=PREWARM_NOW,
            cycle_state=state,
            freeze_ahead_decision_ts_ms=D0,
        )

    def test_a_pre_deadline_cycle_freezes_tomorrows_book(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CarryCycleState()
        payload = self._prewarm(tmp_path, monkeypatch, state)

        # The cycle itself still lives on the OLD decision day and publishes
        # nothing (the old day's entry signals expired hours ago).
        assert payload["decision_ts_ms"] == D0 - MS_PER_DAY
        assert payload["target_intents_queued"] == 0
        assert payload["freeze_ahead_frozen"] is True
        assert state.frozen_decision(D0) is not None
        # Two-day store: warming tomorrow must not evict today's frozen book,
        # or the two freezes recompute each other once a minute.
        assert state.frozen_decision(D0 - MS_PER_DAY) is not None

    def test_the_deadline_cycle_publishes_the_frozen_book_without_a_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CarryCycleState()
        self._prewarm(tmp_path, monkeypatch, state)

        def refuse_build(**_kwargs: Any) -> None:
            raise AssertionError("the deadline pass must not rebuild market data")

        monkeypatch.setattr(module, "_build_carry_demo_market_data", refuse_build)
        boundary = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=BOUNDARY_NOW,
            cycle_state=state,
            cycle_kind="market_boundary",
        )

        assert boundary["decision_error"] is None
        assert boundary["data_build_skipped"] is True
        assert boundary["decision_frozen"] is True
        assert boundary["decision_frozen_ahead"] is True
        assert boundary["decision_ts_ms"] == D0
        assert len(boundary.publication.entry_requests) == 1
        entry_symbols = [
            item.intent.symbol
            for item in boundary.publication.entry_requests[0].request.intents
        ]
        assert entry_symbols == [DEEP_B, DEEP_A, RESIZED]
        summary = format_carry_demo_cycle_summary(dict(boundary))
        assert "build_skipped=True" in summary

    def test_the_frozen_ahead_book_equals_the_boundary_computed_book(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warmed = CarryCycleState()
        self._prewarm(tmp_path, monkeypatch, warmed)

        # A separate route and producer root, or the first run's unresolved
        # inbox requests would suppress the second run's identical entries.
        _route(tmp_path / "route2")
        fresh = CarryCycleState()
        payload = run_carry_demo_cycle(
            tmp_path / "producer2",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route2"),
            market_client=_FakeCarryMarket(),
            now_ms=BOUNDARY_NOW,
            cycle_state=fresh,
            cycle_kind="market_boundary",
        )

        assert payload["data_build_skipped"] is False
        warmed_decision = warmed.frozen_decision(D0)
        fresh_decision = fresh.frozen_decision(D0)
        assert warmed_decision is not None and fresh_decision is not None
        # Discrete-decision equality: identical inputs, identical book.
        assert warmed_decision[0].weights == fresh_decision[0].weights
        assert warmed_decision[0].universe_size == fresh_decision[0].universe_size
        assert warmed_decision[1] == fresh_decision[1]  # trailing-funding ranks
        assert warmed_decision[2] == fresh_decision[2]  # eligible universe

    def test_freeze_ahead_refuses_a_rest_degraded_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default shim reports a REST-repaired build (fetched_rows > 0):
        # the boundary's own rebuild could see different rows, so the cycle
        # must refuse to pin tomorrow's book from it.
        _route(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch)
        state = CarryCycleState()
        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=PREWARM_NOW,
            cycle_state=state,
            freeze_ahead_decision_ts_ms=D0,
        )

        assert payload["freeze_ahead_frozen"] is False
        assert state.frozen_decision(D0) is None

    def test_an_unfrozen_deadline_cycle_still_builds_and_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)
        _patch_planning(monkeypatch)
        state = CarryCycleState()
        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=BOUNDARY_NOW,
            cycle_state=state,
            cycle_kind="market_boundary",
        )

        assert payload["data_build_skipped"] is False
        assert payload["decision_error"] is None
        assert payload["decision_ts_ms"] == D0
        assert state.frozen_decision(D0) is not None

    def _run_prewarm_with_stats(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state: CarryCycleState,
        stats: dict[str, int],
        market: Any | None = None,
    ) -> PublishedTargetCyclePayload:
        """One pre-deadline cycle whose kline build reports the given stats —
        each gate of `_freeze_decision_ahead` can be probed in isolation."""

        _route(tmp_path / "route")
        _patch_planning(monkeypatch)

        def download(
            symbols: list[str], *, start_ms: int, end_ms: int, **_kwargs: Any
        ) -> tuple[pl.DataFrame, dict[str, int]]:
            frame = _synth_klines(symbols, start_ms=start_ms, end_ms=end_ms)
            return frame, {
                "cache_rows": frame.height,
                "output_rows": frame.height,
                "fetch_symbols": 0,
                **stats,
            }

        monkeypatch.setattr(module, "_download_recent_1h_klines", download)
        monkeypatch.setattr(
            module,
            "_demo_instruments",
            lambda *_a, **_k: pl.DataFrame(
                {
                    "symbol": pl.Series([], dtype=pl.String),
                    "launch_time_ms": pl.Series([], dtype=pl.Int64),
                }
            ),
        )
        return run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=market or _FakeCarryMarket(),
            now_ms=PREWARM_NOW,
            cycle_state=state,
            freeze_ahead_decision_ts_ms=D0,
        )

    def test_freeze_ahead_refuses_rest_repair_even_with_a_serving_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The realistic degraded state: the store serves most bars AND REST
        # repaired a tail. The boundary's own rebuild could repair differently,
        # so this alone must refuse — independent of the store gate.
        state = CarryCycleState()
        payload = self._run_prewarm_with_stats(
            tmp_path, monkeypatch, state, {"fetched_rows": 5, "store_rows": 100_000}
        )
        assert payload["freeze_ahead_frozen"] is False
        assert state.frozen_decision(D0) is None

    def test_freeze_ahead_refuses_a_store_that_never_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cache-only build, zero fetches: fetched_rows==0 passes the first
        # gate, so the store gate alone must refuse.
        state = CarryCycleState()
        payload = self._run_prewarm_with_stats(
            tmp_path, monkeypatch, state, {"fetched_rows": 0, "store_rows": 0}
        )
        assert payload["freeze_ahead_frozen"] is False
        assert state.frozen_decision(D0) is None

    def test_freeze_ahead_refuses_a_failing_funding_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A funding outage that heals before 00:20 would hand the deadline's
        # rebuild settlement prints this build never saw; a build with funding
        # fetch failures must not pin tomorrow's book.
        class _FundingOutageMarket(_FakeCarryMarket):
            def get_funding_history(
                self, symbol: str, start: int, end: int
            ) -> list[dict[str, Any]]:
                if symbol == DEEP_A:
                    raise RuntimeError("synthetic funding outage")
                return super().get_funding_history(symbol, start, end)

        state = CarryCycleState()
        payload = self._run_prewarm_with_stats(
            tmp_path,
            monkeypatch,
            state,
            {"fetched_rows": 0, "store_rows": 100_000},
            market=_FundingOutageMarket(),
        )
        assert payload["decision_error"] is None  # current day still decides
        assert payload["freeze_ahead_frozen"] is False
        assert state.frozen_decision(D0) is None

    def test_the_second_in_window_cycle_does_not_claim_the_freeze(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two grid cycles can land inside the 90s window; the receipt must
        # name the one that actually froze the day, not both.
        state = CarryCycleState()
        first = self._prewarm(tmp_path, monkeypatch, state)
        second = self._prewarm(tmp_path, monkeypatch, state)
        assert first["freeze_ahead_frozen"] is True
        assert second["freeze_ahead_frozen"] is False
        assert state.frozen_decision(D0) is not None

    def test_a_journal_wake_serves_the_frozen_book_without_a_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CarryCycleState()
        self._prewarm(tmp_path, monkeypatch, state)
        mid_day_now = D0 + 25 * 60_000
        # The hourly funding sweep is fresh for this hour, so the wake owes
        # no maintenance and reacts on the frozen book alone.
        state.funding_swept_hour_ts = mid_day_now - mid_day_now % 3_600_000

        def refuse_build(**_kwargs: Any) -> None:
            raise AssertionError("a maintenance-free journal wake must not rebuild market data")

        monkeypatch.setattr(module, "_build_carry_demo_market_data", refuse_build)
        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=mid_day_now,
            cycle_state=state,
            cycle_kind="journal_change",
        )

        assert payload["decision_error"] is None
        assert payload["data_build_skipped"] is True
        assert payload["decision_frozen"] is True
        assert payload["decision_ts_ms"] == D0

    def test_a_journal_wake_still_builds_when_the_funding_sweep_is_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CarryCycleState()
        self._prewarm(tmp_path, monkeypatch, state)
        mid_day_now = D0 + 25 * 60_000
        # Sweep stamped for the PREVIOUS hour: this wake owes maintenance.
        state.funding_swept_hour_ts = (mid_day_now - mid_day_now % 3_600_000) - 3_600_000

        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=mid_day_now,
            cycle_state=state,
            cycle_kind="journal_change",
        )

        assert payload["decision_error"] is None
        assert payload["data_build_skipped"] is False
        assert payload["decision_frozen"] is True

    def test_a_journal_wake_inside_the_freeze_window_still_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CarryCycleState()
        self._prewarm(tmp_path, monkeypatch, state)
        window_now = D0 + MS_PER_DAY + 19 * 60_000
        state.funding_swept_hour_ts = window_now - window_now % 3_600_000

        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=window_now,
            cycle_state=state,
            cycle_kind="journal_change",
            freeze_ahead_decision_ts_ms=D0 + MS_PER_DAY,
        )

        assert payload["decision_error"] is None
        assert payload["data_build_skipped"] is False
        assert payload["freeze_ahead_frozen"] is True


# --- wave-2 boundary anatomy: grouped exits, pre-inbox read elimination, and
# --- the freeze-time equity anchor (2026-08-13) ---

#: 00:19:50 UTC: still pre-deadline, inside the freeze window, close enough to
#: the boundary that the reading it re-stamps is fresh at BOUNDARY_NOW.
REFRESH_NOW = D0 + 20 * 60_000 - 10_000


def _standing_frame_two_exits() -> pl.DataFrame:
    """Standing book with TWO symbols leaving the desired book (F00 and
    STANDGONE both carry benign funding) plus the resized survivor."""

    def row(symbol: str, qty: float) -> dict[str, Any]:
        return {
            "trade_id": CARRY_COMPONENT_ID,
            "target_key": f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/{symbol}",
            "strategy_id": CARRY_STRATEGY_ID,
            "symbol": symbol,
            "status": "open",
            "signed_qty": qty,
            "target_reference_price": 100.0,
        }

    return pl.DataFrame([row("F00USDT", 1.0), row(STANDGONE, 2.0), row(RESIZED, 1.0)])


class TestGroupedExitPublication:
    def test_two_exits_publish_as_one_request_each(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from liquidity_migration.account.account_service import AccountIntentInbox

        _route(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=_standing_frame_two_exits())

        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS,
        )

        assert payload["decision_error"] is None
        assert payload["exit_targets_queued"] == 2
        publication = payload.publication
        # Review probes 2026-08-13: grouping exits removed per-symbol
        # independence (one dead symbol fails the whole grouped request at
        # owner admission), so carry publishes one request per exit again.
        assert publication.exit_publication_mode == "independent"
        assert len(publication.exit_requests) == 2
        assert [
            item.intent.symbol
            for published in publication.exit_requests
            for item in published.request.intents
        ] == ["F00USDT", STANDGONE]
        assert all(
            item.intent.signed_notional_usdt == 0.0
            for published in publication.exit_requests
            for item in published.request.intents
        )
        created = NOW_MS * 1_000_000
        assert [item.request.created_ts_ns for item in publication.exit_requests] == [
            created,
            created + 1,
        ]
        # Entries keep their deterministic created stamp after the exits.
        assert len(publication.entry_requests) == 1
        assert publication.entry_requests[0].request.created_ts_ns == created + 2
        # The durable receipt names every exit request with its intent count.
        receipts = json.loads(payload["account_target_requests_json"])
        assert receipts["exit_publication_mode"] == "independent"
        assert receipts["exit_requests"] == [
            {
                "request_id": item.request.request_id,
                "batch_id": item.request.batch_id,
                "intent_count": 1,
            }
            for item in publication.exit_requests
        ]
        # Arrival order at the owner: every exit precedes the entry group.
        inbox = AccountIntentInbox(payload.route)
        first = inbox.claim_next()
        second = inbox.claim_next()
        third = inbox.claim_next()
        assert first is not None and second is not None and third is not None
        assert first[1].request_id == publication.exit_requests[0].request.request_id
        assert second[1].request_id == publication.exit_requests[1].request.request_id
        assert third[1].request_id == publication.entry_requests[0].request.request_id


class TestPreInboxReadElimination:
    def test_route_verification_is_memoized_after_the_first_cycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import liquidity_migration.account.account_route as route_module

        _route(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=_standing_frame())
        state = CarryCycleState()
        parses: list[str] = []
        real_read = route_module._read_manifest

        def counting_read(path, **kwargs):
            parses.append(str(path))
            return real_read(path, **kwargs)

        monkeypatch.setattr(route_module, "_read_manifest", counting_read)
        run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS,
            cycle_state=state,
        )
        first_cycle_parses = len(parses)
        assert first_cycle_parses > 0  # the first verification is a real read

        parses.clear()
        second = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS + 60_000,
            cycle_state=state,
        )

        # Unchanged manifest files: the second cycle re-verifies by lstat
        # identity alone and parses ZERO manifests.
        assert parses == []
        assert second["decision_error"] is None

    def test_route_memo_reverifies_when_a_manifest_file_changes(self, tmp_path: Path) -> None:
        import liquidity_migration.account.account_route as route_module

        route = _route(tmp_path / "route")
        verified = route_module.require_account_route(
            account_id=route.account_id,
            environment=route.environment,
            account_root=route.account_root,
            inbox_root=route.inbox_root,
        )
        assert verified == route
        manifest = Path(route.account_root) / route_module.ACCOUNT_ROUTE_FILENAME
        original = manifest.read_bytes()
        manifest.unlink()
        manifest.write_bytes(original + b" ")  # non-canonical bytes, new inode
        manifest.chmod(0o600)

        with pytest.raises(route_module.AccountRouteIntegrityError):
            route_module.require_account_route(
                account_id=route.account_id,
                environment=route.environment,
                account_root=route.account_root,
                inbox_root=route.inbox_root,
            )

    def test_registered_rule_is_parsed_once_across_cycles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=_standing_frame())
        monkeypatch.setattr(module, "_REGISTERED_RULE_CACHE", {})
        loads: list[str] = []
        real_load = module.load_carry_config

        def counting_load(path=None):
            loads.append(str(path))
            return real_load(path)

        monkeypatch.setattr(module, "load_carry_config", counting_load)
        state = CarryCycleState()
        for offset in (0, 60_000):
            payload = run_carry_demo_cycle(
                tmp_path / "producer",
                config=ResearchConfig(),
                demo_config=_routed_config(tmp_path / "route"),
                market_client=_FakeCarryMarket(),
                now_ms=NOW_MS + offset,
                cycle_state=state,
            )
            assert payload["decision_error"] is None

        assert len(loads) == 1  # one parse per process per rule file, ever

    def test_open_positions_telemetry_still_lands_in_the_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The open-trades filter moved AFTER the publish call (it feeds only
        # telemetry); the persisted cycle row must still carry it.
        _route(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=_standing_frame())

        payload = run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS,
        )

        assert payload["open_positions"] == 2
        cycles = read_dataset(tmp_path / "producer", CARRY_CYCLES_DATASET)
        assert cycles.get_column("open_positions").to_list() == [2]


class _CountingTimeModule:
    """`time` stand-in for strategy_planning: sleeps counted, rest passed through."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))

    def __getattr__(self, name: str) -> Any:
        import time as real_time

        return getattr(real_time, name)


class TestFreezeTimeEquityAnchorAndBoundaryHealth:
    """A4: the day's equity anchors at the freeze pass (~90s early, declared),
    and a boundary wake with a fresh freeze-time owner reading performs no
    health read and none of the head-retry sleeps."""

    def _patch_health(
        self, monkeypatch: pytest.MonkeyPatch, health: Any
    ) -> None:
        monkeypatch.setattr(planning_module, "require_recent_engine_account", health)
        monkeypatch.setattr(
            planning_module, "canonical_strategy_trade_rows", lambda *_a, **_k: pl.DataFrame()
        )
        monkeypatch.setattr(
            planning_module, "terminal_entry_attempt_keys", lambda *_a, **_k: frozenset()
        )

    def _run(
        self,
        tmp_path: Path,
        state: CarryCycleState,
        *,
        now_ms: int,
        cycle_kind: str = "timer",
        freeze_ahead: int | None = None,
    ) -> PublishedTargetCyclePayload:
        return run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(tmp_path / "route"),
            market_client=_FakeCarryMarket(),
            now_ms=now_ms,
            cycle_state=state,
            cycle_kind=cycle_kind,
            freeze_ahead_decision_ts_ms=freeze_ahead,
        )

    def test_boundary_with_fresh_freeze_reading_does_zero_health_reads_or_sleeps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)
        self._patch_health(
            monkeypatch, lambda *_a, **_k: SimpleNamespace(equity_usdt=EQUITY, observed_wall_ts_ms=0)
        )
        state = CarryCycleState()
        prewarm = self._run(tmp_path, state, now_ms=PREWARM_NOW, freeze_ahead=D0)
        assert prewarm["freeze_ahead_frozen"] is True
        refresh = self._run(tmp_path, state, now_ms=REFRESH_NOW, freeze_ahead=D0)
        assert refresh["decision_error"] is None
        reading = state.owner_health_reading
        assert reading is not None
        assert reading.read_wall_ts_ns == REFRESH_NOW * 1_000_000
        assert reading.equity_usdt == pytest.approx(EQUITY)

        # From here a live read fails. There is no retry ladder any more --
        # the engine heartbeat is one file replaced by rename, so a read lands
        # or it does not -- but a live read is still a read, and the boundary
        # must never reach it.
        live_reads: list[str] = []

        def unreadable(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            live_reads.append("read")
            raise OSError("synthetic unreadable heartbeat")

        self._patch_health(monkeypatch, unreadable)
        counting_time = _CountingTimeModule()
        monkeypatch.setattr(planning_module, "time", counting_time)

        # CONTROL — no stored reading forces the live read.
        control_state = CarryCycleState()
        control_state.frozen_decisions = dict(state.frozen_decisions)
        control = self._run(
            tmp_path, control_state, now_ms=BOUNDARY_NOW, cycle_kind="market_boundary"
        )
        assert control["data_build_skipped"] is True
        assert len(live_reads) == 1
        assert counting_time.sleeps == []
        assert control["equity_usdt"] is None
        assert control["entry_blocked_reason"] == "account_owner_health_unavailable"

        # TREATED — the freeze-window reading is 10.001s old at the boundary,
        # inside the 30s freshness bound: zero reads, zero sleeps, entries on.
        live_reads.clear()
        counting_time.sleeps.clear()
        boundary = self._run(
            tmp_path, state, now_ms=BOUNDARY_NOW, cycle_kind="market_boundary"
        )

        assert boundary["data_build_skipped"] is True
        assert live_reads == []
        assert counting_time.sleeps == []
        assert boundary["equity_usdt"] == pytest.approx(EQUITY)
        assert boundary["entry_blocked_reason"] == ""
        assert boundary["entry_targets_queued"] > 0

    def test_the_day_sizes_off_freeze_time_equity_not_boundary_equity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declared numerical change: freeze-time equity X sizes the day even
        when boundary-time equity is Y != X; the dead-band absorbs the drift."""

        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)
        freeze_equity = 10_000.0
        boundary_equity = 20_000.0
        live_equity = {"value": freeze_equity}
        self._patch_health(
            monkeypatch,
            lambda *_a, **_k: SimpleNamespace(equity_usdt=live_equity["value"], observed_wall_ts_ms=0),
        )
        state = CarryCycleState()
        prewarm = self._run(tmp_path, state, now_ms=PREWARM_NOW, freeze_ahead=D0)
        assert prewarm["freeze_ahead_frozen"] is True
        assert state.sizing_equity_by_decision[D0] == pytest.approx(freeze_equity)

        # Equity doubles before the boundary, and the stored reading ages past
        # the 95s freeze-window bound — stale — so the boundary live-reads Y,
        # and the day must STILL size off the freeze-time anchor X.
        live_equity["value"] = boundary_equity
        state.owner_health_reading = dataclasses.replace(
            state.owner_health_reading,
            read_wall_ts_ns=(BOUNDARY_NOW - 96_500) * 1_000_000,
        )
        boundary = self._run(
            tmp_path, state, now_ms=BOUNDARY_NOW, cycle_kind="market_boundary"
        )

        assert boundary["decision_error"] is None
        assert boundary["equity_usdt"] == pytest.approx(boundary_equity)
        assert boundary["sizing_equity_usdt"] == pytest.approx(freeze_equity)
        assert boundary["sizing_equity_decision_ts_ms"] == D0
        entry_intents = [
            item.intent
            for item in boundary.publication.entry_requests[0].request.intents
            if ENTRY_ATTEMPT_METADATA_KEY in item.intent.metadata
        ]
        assert entry_intents
        for intent in entry_intents:
            weight = float(intent.metadata["target_weight"])
            assert intent.signed_notional_usdt == pytest.approx(weight * freeze_equity)
            assert intent.signed_notional_usdt != pytest.approx(weight * boundary_equity)

    def test_a_stale_freeze_reading_falls_back_to_the_live_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)
        live_reads: list[str] = []

        def live_health(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            live_reads.append("read")
            return SimpleNamespace(equity_usdt=EQUITY, observed_wall_ts_ms=0)

        self._patch_health(monkeypatch, live_health)
        state = CarryCycleState()
        self._run(tmp_path, state, now_ms=PREWARM_NOW, freeze_ahead=D0)
        assert state.owner_health_reading is not None

        live_reads.clear()
        # Age the stamp past the 95s freeze-window bound: the boundary must
        # NOT trust it and must read live.
        state.owner_health_reading = dataclasses.replace(
            state.owner_health_reading,
            read_wall_ts_ns=(BOUNDARY_NOW - 96_500) * 1_000_000,
        )
        boundary = self._run(
            tmp_path, state, now_ms=BOUNDARY_NOW, cycle_kind="market_boundary"
        )

        assert live_reads  # the live read ran
        assert boundary["equity_usdt"] == pytest.approx(EQUITY)
        assert boundary["entry_blocked_reason"] == ""

    def test_an_unusable_fresh_freeze_reading_blocks_entries_without_a_live_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)

        def broken_health(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            raise RuntimeError("owner health receipt is stale")

        self._patch_health(monkeypatch, broken_health)
        state = CarryCycleState()
        self._run(tmp_path, state, now_ms=PREWARM_NOW, freeze_ahead=D0)
        refresh = self._run(tmp_path, state, now_ms=REFRESH_NOW, freeze_ahead=D0)
        assert refresh["account_owner_health_error"]
        reading = state.owner_health_reading
        assert reading is not None and reading.error

        # The owner heals, but the boundary's authority is the fresh stored
        # reading: unusable, so entries stay blocked exactly as a live failure
        # would block them — and no live read runs.
        live_reads: list[str] = []

        def healed_health(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            live_reads.append("read")
            return SimpleNamespace(equity_usdt=EQUITY, observed_wall_ts_ms=0)

        self._patch_health(monkeypatch, healed_health)
        boundary = self._run(
            tmp_path, state, now_ms=BOUNDARY_NOW, cycle_kind="market_boundary"
        )

        assert live_reads == []
        assert boundary["equity_usdt"] is None
        assert boundary["entry_blocked_reason"] == "account_owner_health_unavailable"
        assert boundary["entry_targets_queued"] == 0


class TestBoundaryOnlyStoredHealth:
    """The stored reading serves ONLY the market boundary; journal wakes and
    the day's anchor stay live. Review finding 2026-08-13: a journal-change
    wake fires BECAUSE a fill landed, so serving it stored equity could hand
    it a number that predates the very fill that woke it — and a reading that
    anchors a day must have been read live, or age launders into the anchor.
    """

    _run = TestFreezeTimeEquityAnchorAndBoundaryHealth._run
    _patch_health = TestFreezeTimeEquityAnchorAndBoundaryHealth._patch_health

    def test_a_journal_change_wake_reads_live_even_with_a_fresh_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails without the fix: journal_change served the stored reading."""

        _route(tmp_path / "route")
        _patch_demo_market_data_ws_served(monkeypatch)
        live_reads: list[str] = []

        def live_health(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            live_reads.append("read")
            return SimpleNamespace(equity_usdt=EQUITY, observed_wall_ts_ms=0)

        self._patch_health(monkeypatch, live_health)
        state = module.CarryCycleState()
        self._run(tmp_path, state, now_ms=PREWARM_NOW, freeze_ahead=D0)
        assert state.owner_health_reading is not None

        live_reads.clear()
        wake = self._run(
            tmp_path, state, now_ms=PREWARM_NOW + 5_000, cycle_kind="journal_change"
        )

        assert live_reads  # the journal wake paid its own live read
        assert wake["equity_usdt"] == pytest.approx(EQUITY)


# ---------------------------------------------------------------------------
# The engine target book: what research decided, written where the Rust engine
# can follow it. Off unless the path is set; never able to stop the sleeve.
# ---------------------------------------------------------------------------


def _write_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Path:
    path = tmp_path / "carry_targets.json"
    monkeypatch.setenv(module.ENGINE_TARGET_BOOK_PATH_ENV, str(path))
    kwargs: dict[str, Any] = {
        "desired": {"KAITOUSDT": 0.10, "COTIUSDT": 0.05},
        "decision_ts_ms": 1786665600000,
        "sizing_equity_usdt": 1000.0,
        "notional_multiplier": 1.0,
        "stop_loss_fraction": 0.35,
        "entry_leverage": 2.0,
        "strategy_profile": "carry_hold_v4_live_v1",
    }
    kwargs.update(overrides)
    module._write_engine_target_book(**kwargs)
    return path


def test_target_book_records_the_decided_notionals(tmp_path, monkeypatch) -> None:
    book = json.loads(_write_book(tmp_path, monkeypatch).read_text(encoding="utf-8"))
    assert book["source"] == "carry_hold_v4_live_v1"
    assert book["decision_ts_ms"] == 1786665600000
    assert book["valid_until_ms"] == 1786665600000 + module.SIGNAL_VALIDITY_MS
    by_symbol = {row["symbol"]: row for row in book["targets"]}
    # weight * sizing equity * multiplier, which is what the sleeve sizes with.
    assert by_symbol["KAITOUSDT"]["notional_usdt"] == pytest.approx(100.0)
    assert by_symbol["COTIUSDT"]["notional_usdt"] == pytest.approx(50.0)
    assert by_symbol["KAITOUSDT"]["stop_loss_fraction"] == 0.35


def test_no_path_means_no_book(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(module.ENGINE_TARGET_BOOK_PATH_ENV, raising=False)
    module._write_engine_target_book(
        desired={"KAITOUSDT": 0.1},
        decision_ts_ms=1786665600000,
        sizing_equity_usdt=1000.0,
        notional_multiplier=1.0,
        stop_loss_fraction=0.35,
        entry_leverage=2.0,
        strategy_profile="carry_hold_v4_live_v1",
    )
    assert list(tmp_path.iterdir()) == []


def test_an_empty_decision_writes_an_empty_book(tmp_path, monkeypatch) -> None:
    # Deciding cash is a decision and the engine must be able to act on it.
    book = json.loads(_write_book(tmp_path, monkeypatch, desired={}).read_text(encoding="utf-8"))
    assert book["targets"] == []


def test_a_book_that_cannot_be_written_never_stops_the_sleeve(tmp_path, monkeypatch) -> None:
    # The sleeve is trading; bookkeeping for a component that trades nothing
    # yet must not be able to raise into it.
    monkeypatch.setenv(module.ENGINE_TARGET_BOOK_PATH_ENV, str(tmp_path / "x.json"))
    monkeypatch.setattr(
        module, "write_target_book", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    module._write_engine_target_book(
        desired={"KAITOUSDT": 0.1},
        decision_ts_ms=1786665600000,
        sizing_equity_usdt=1000.0,
        notional_multiplier=1.0,
        stop_loss_fraction=0.35,
        entry_leverage=2.0,
        strategy_profile="carry_hold_v4_live_v1",
    )


# --- v6 (promoted 2026-08-19): the Binance whale feed and the live decision ---


from liquidity_migration.marketdata.binance import BinanceDataError  # noqa: E402


def _whale_day_ends(now_ms: int) -> list[int]:
    newest = (now_ms // MS_PER_DAY) * MS_PER_DAY
    return [newest - k * MS_PER_DAY for k in range(module.WHALE_FEED_DAYS)]


class _FakeWhaleClient:
    """Canned ratio endpoint. ``series[symbol]`` = list of (ts_ms, ratio);
    ``end`` is exclusive, matching the real client's paged contract."""

    calls: list[tuple[str, int, int]] = []

    def __init__(
        self,
        series: dict[str, list[tuple[int, float]]],
        *,
        transient: set[str] | None = None,
        permanent: set[str] | None = None,
    ) -> None:
        self.series = series
        self.transient = transient or set()
        self.permanent = permanent or set()

    def get_top_trader_ls_position_ratio(
        self, symbol: str, period: str, start: int, end: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        assert period == "5m"
        type(self).calls.append((symbol, start, end))
        if symbol in self.transient:
            raise BinanceDataError("synthetic transport failure")
        if symbol in self.permanent:
            err = BinanceDataError("Binance rejected: HTTP 400 Invalid symbol")
            err.permanent = True
            raise err
        return [
            {"timestamp": ts, "longShortRatio": str(value)}
            for ts, value in self.series.get(symbol, [])
            if start <= ts < end
        ]


def _flat_series(symbols: list[str], day_ends: list[int], value: float = 1.3) -> dict:
    return {
        sym: [(end - 5 * 60_000, value) for end in day_ends] for sym in symbols
    }


class TestWhaleFeed:
    def test_refresh_fetches_eods_and_serves_events(self, tmp_path: Path) -> None:
        state = CarryCycleState()
        ends = _whale_day_ends(NOW_MS)
        series = _flat_series(["AUSDT"], ends)
        # BUSDT's newest day drops 1.3 -> 1.0; GONEUSDT is not on Binance.
        series["BUSDT"] = [(end - 5 * 60_000, 1.0 if end == ends[0] else 1.3) for end in ends]
        fake = _FakeWhaleClient(series, permanent={"GONEUSDT"})
        _FakeWhaleClient.calls = []

        events, stats = module._refresh_carry_whale_cache(
            tmp_path, ["AUSDT", "BUSDT", "GONEUSDT"], now_ms=NOW_MS, state=state,
            client_factory=lambda: fake,
        )

        assert stats["whale_pairs_fetched"] == 3 * module.WHALE_FEED_DAYS
        assert stats["whale_pairs_missing"] == 0
        # Events carry only known values; the venue-absent name is held as
        # nulls in the store (so it is never refetched) and excluded here.
        assert events.height == 2 * module.WHALE_FEED_DAYS
        assert set(events.get_column("symbol").to_list()) == {"AUSDT", "BUSDT"}
        newest_b = events.filter(
            (pl.col("symbol") == "BUSDT") & (pl.col("_tt_ls_ts_ms") == ends[0])
        )
        assert newest_b.get_column("bn_tt_ls").to_list() == [1.0]
        assert module._whale_store_path(tmp_path).exists()

        # Nothing missing now: a second pass makes zero network calls even
        # though the cooldown has notionally expired.
        _FakeWhaleClient.calls = []
        state.whale_last_attempt_ms = None
        events_again, stats_again = module._refresh_carry_whale_cache(
            tmp_path, ["AUSDT", "BUSDT", "GONEUSDT"], now_ms=NOW_MS, state=state,
            client_factory=lambda: fake,
        )
        assert _FakeWhaleClient.calls == []
        assert events_again.height == events.height
        assert stats_again["whale_pairs_fetched"] == 0

    def test_restart_reloads_the_disk_store(self, tmp_path: Path) -> None:
        ends = _whale_day_ends(NOW_MS)
        fake = _FakeWhaleClient(_flat_series(["AUSDT"], ends))
        module._refresh_carry_whale_cache(
            tmp_path, ["AUSDT"], now_ms=NOW_MS, state=CarryCycleState(),
            client_factory=lambda: fake,
        )
        # Fresh state (a producer restart): served from disk, no fetching.
        _FakeWhaleClient.calls = []
        events, _stats = module._refresh_carry_whale_cache(
            tmp_path, ["AUSDT"], now_ms=NOW_MS, state=CarryCycleState(),
            client_factory=lambda: fake,
        )
        assert _FakeWhaleClient.calls == []
        assert events.height == module.WHALE_FEED_DAYS

    def test_transient_failure_leaves_pair_missing_and_cooldown_gates_retry(
        self, tmp_path: Path
    ) -> None:
        state = CarryCycleState()
        fake = _FakeWhaleClient({}, transient={"AUSDT"})
        _FakeWhaleClient.calls = []
        events, stats = module._refresh_carry_whale_cache(
            tmp_path, ["AUSDT"], now_ms=NOW_MS, state=state, client_factory=lambda: fake,
        )
        assert events.height == 0
        assert stats["whale_pairs_fetched"] == 0
        assert stats["whale_pairs_missing"] == module.WHALE_FEED_DAYS
        first_calls = len(_FakeWhaleClient.calls)
        assert first_calls == module.WHALE_FEED_DAYS

        # Inside the cooldown: no new attempts.
        module._refresh_carry_whale_cache(
            tmp_path, ["AUSDT"], now_ms=NOW_MS + 60_000, state=state,
            client_factory=lambda: fake,
        )
        assert len(_FakeWhaleClient.calls) == first_calls

        # Past the cooldown: retried, and a healed feed fills the store.
        fake.transient = set()
        fake.series = _flat_series(["AUSDT"], _whale_day_ends(NOW_MS))
        events, stats = module._refresh_carry_whale_cache(
            tmp_path,
            ["AUSDT"],
            now_ms=NOW_MS + module._WHALE_REFRESH_COOLDOWN_MS,
            state=state,
            client_factory=lambda: fake,
        )
        assert len(_FakeWhaleClient.calls) == 2 * first_calls
        assert events.height == module.WHALE_FEED_DAYS

    def test_attach_matches_the_panel_convention(self) -> None:
        klines = pl.DataFrame(
            {
                "ts_ms": [D0 - 2 * MS_PER_HOUR, D0 - MS_PER_HOUR] * 2,
                "symbol": ["AUSDT", "AUSDT", "NOWHALEUSDT", "NOWHALEUSDT"],
                "close": [10.0, 11.0, 5.0, 6.0],
                "turnover_quote": [1.0, 1.0, 1.0, 1.0],
            }
        )
        funding = pl.DataFrame(
            {
                "symbol": ["AUSDT"],
                "funding_ts_ms": [D0 - MS_PER_HOUR],
                "funding_rate": [-0.001],
            }
        )
        events = pl.DataFrame(
            {
                "symbol": ["AUSDT", "AUSDT"],
                "_tt_ls_ts_ms": [D0 - MS_PER_DAY, D0],
                "bn_tt_ls": [1.5, 1.2],
            }
        )
        view = _carry_venue_view(
            klines, funding, window_start_ms=D0 - MS_PER_HOUR, max_bar_ts_ms=D0,
            whale_events=events,
        )
        rows = {
            (row["symbol"], int(row["bar_ts_ms"])): row for row in view.to_dicts()
        }
        # Backward as-of: the D0-1h bar still reads yesterday's EOD (age 23h);
        # the D0 bar reads the value stamped at D0 with age exactly 0 — the
        # same shape the research panel attaches bn_tt_ls with.
        assert rows[("AUSDT", D0 - MS_PER_HOUR)]["bn_tt_ls"] == 1.5
        assert rows[("AUSDT", D0 - MS_PER_HOUR)]["bn_tt_ls_age_h"] == pytest.approx(23.0)
        assert rows[("AUSDT", D0)]["bn_tt_ls"] == 1.2
        assert rows[("AUSDT", D0)]["bn_tt_ls_age_h"] == 0.0
        # A name with no events carries nulls (the rule fails open on them).
        assert rows[("NOWHALEUSDT", D0)]["bn_tt_ls"] is None
        assert rows[("NOWHALEUSDT", D0)]["bn_tt_ls_age_h"] is None

        empty = _carry_venue_view(
            klines, funding, window_start_ms=D0 - MS_PER_HOUR, max_bar_ts_ms=D0,
            whale_events=pl.DataFrame(
                schema={"symbol": pl.String, "_tt_ls_ts_ms": pl.Int64, "bn_tt_ls": pl.Float64}
            ),
        )
        assert empty.get_column("bn_tt_ls").null_count() == empty.height
        assert empty.get_column("bn_tt_ls_age_h").null_count() == empty.height

        # No whale leg (v1..v4): the view is bit-identical to before the feed.
        plain = _carry_venue_view(
            klines, funding, window_start_ms=D0 - MS_PER_HOUR, max_bar_ts_ms=D0
        )
        assert "bn_tt_ls" not in plain.columns
        assert "bn_tt_ls_age_h" not in plain.columns


class TestV6DecidesLive:
    """The promoted rule end to end on the live frame: bent depth ladder,
    flow halving (flat synthetic turnover growth is 0 <= +0.40, so it fires
    for every name), and the whale halving fed by attached Binance EODs."""

    START_MS = D0 - 60 * MS_PER_DAY

    def _whale_events(self) -> pl.DataFrame:
        stamps = [D0 - k * MS_PER_DAY for k in range(5, 0, -1)] + [D0]
        rows = []
        for stamp in stamps:
            # DEEP_B: 1.3 until the newest EOD drops to 1.0 -> 3d change -0.30
            # (below the -0.26 cut). DEEP_A: flat 1.3 -> change 0, full size.
            rows.append({"symbol": DEEP_B, "_tt_ls_ts_ms": stamp,
                         "bn_tt_ls": 1.0 if stamp == D0 else 1.3})
            rows.append({"symbol": DEEP_A, "_tt_ls_ts_ms": stamp, "bn_tt_ls": 1.3})
        return pl.DataFrame(rows).sort(["_tt_ls_ts_ms", "symbol"])

    def _funding_frame(self) -> pl.DataFrame:
        grid = 8 * MS_PER_HOUR
        rows = []
        for symbol in ALL_SYMBOLS:
            for ts in range(self.START_MS, D0 + 1, grid):
                rows.append(
                    {"symbol": symbol, "funding_ts_ms": ts,
                     "funding_rate": _funding_rate(symbol, ts)}
                )
        return pl.DataFrame(rows)

    def test_decide_book_halves_the_whale_flagged_name_only(self) -> None:
        klines = _synth_klines(
            list(ALL_SYMBOLS), start_ms=self.START_MS - MS_PER_HOUR, end_ms=D0 - MS_PER_HOUR
        )
        view = _carry_venue_view(
            klines,
            self._funding_frame(),
            window_start_ms=self.START_MS,
            max_bar_ts_ms=D0,
            whale_events=self._whale_events(),
        )
        cfg = load_carry_config(module._CONFIGS_DIR / "lane2_carry_hold_v6.json")
        decision = module.decide_book(view, cfg, D0)

        assert set(decision.weights) == {DEEP_A, DEEP_B, RESIZED}
        # DEEP_A / RESIZED trail -45 bp: (45/120)^1.5 = 0.2296 floors at 0.25;
        # flow halves; whale change is 0 (DEEP_A) or null (RESIZED) - no cut.
        assert decision.weights[DEEP_A] == pytest.approx(0.1 * 0.25 * 0.5)
        assert decision.weights[RESIZED] == pytest.approx(0.1 * 0.25 * 0.5)
        # DEEP_B trail -75 bp: (75/120)^1.5 above the floor, flow halves, and
        # the -0.30 whale change halves again.
        assert decision.weights[DEEP_B] == pytest.approx(
            0.1 * (75.0 / 120.0) ** 1.5 * 0.5 * 0.5
        )

    def test_run_cycle_wires_the_whale_feed_through_the_live_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        demo_config = _routed_config(tmp_path / "route", strategy_profile="v6")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=None)
        ends = _whale_day_ends(NOW_MS)
        series = _flat_series([DEEP_A], ends)
        series[DEEP_B] = [
            (end - 5 * 60_000, 1.0 if end == ends[0] else 1.3) for end in ends
        ]
        fake = _FakeWhaleClient(series)
        monkeypatch.setattr(module, "_whale_client_factory", lambda: fake)

        payload = module.run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=demo_config,
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS,
        )

        assert payload["decision_error"] is None
        assert payload["decision_ts_ms"] == D0
        assert payload["desired_book_size"] == 3
        expected_gross = (
            0.1 * 0.25 * 0.5 * 2  # DEEP_A + RESIZED: floored ladder, flow halved
            + 0.1 * (75.0 / 120.0) ** 1.5 * 0.5 * 0.5  # DEEP_B: whale-halved too
        )
        assert payload["desired_gross_weight"] == pytest.approx(expected_gross)
        # The cycle's own receipt shows the feed ran and the store persisted.
        assert payload["whale_pairs_fetched"] > 0
        assert module._whale_store_path(tmp_path / "producer").exists()


# --- early exit (owner-directed 2026-08-19): sell at the print that ends it ---


class TestEarlyExit:
    def _decision(self) -> CarryDecision:
        return CarryDecision(
            decision_ts_ms=D0,
            weights={DEEP_A: 0.0125, DEEP_B: 0.0247, RESIZED: 0.0125},
            universe_size=56,
            replay_days=60,
            gross=0.0497,
        )

    def _funding(self, rows: list[tuple[str, int, float]]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": [r[0] for r in rows],
                "funding_ts_ms": [r[1] for r in rows],
                "funding_rate": [r[2] for r in rows],
            }
        )

    def test_fires_on_recovered_post_decision_print_and_masks(self, tmp_path: Path) -> None:
        state = CarryCycleState()
        rule = load_carry_config()
        # DEEP_A's 08:00 print recovered to +1 bp; DEEP_B still deep at -25 bp.
        funding = self._funding(
            [
                (DEEP_A, D0 + 8 * MS_PER_HOUR, 0.0001),
                (DEEP_B, D0 + 8 * MS_PER_HOUR, -0.0025),
            ]
        )
        masked, fires = module._apply_early_exits(
            decision=self._decision(), rule=rule, funding=funding,
            state=state, root=tmp_path, now_ms=D0 + 9 * MS_PER_HOUR,
        )
        assert fires == [DEEP_A]
        assert DEEP_A not in masked.weights
        assert set(masked.weights) == {DEEP_B, RESIZED}
        assert masked.gross == pytest.approx(0.0247 + 0.0125)
        assert module._early_exit_state_path(tmp_path).exists()

        # Next cycle: no new fire, the mask still applies (funding unchanged).
        masked2, fires2 = module._apply_early_exits(
            decision=self._decision(), rule=rule, funding=funding,
            state=state, root=tmp_path, now_ms=D0 + 10 * MS_PER_HOUR,
        )
        assert fires2 == []
        assert DEEP_A not in masked2.weights

    def test_exit_boundary_matches_the_registered_state_machine(self, tmp_path: Path) -> None:
        # The registered test is `not (fv < -exit_)`: a print EXACTLY at
        # -3 bp exits, one strictly below it holds.
        rule = load_carry_config()
        at_boundary = self._funding([(DEEP_A, D0 + MS_PER_HOUR, -rule.exit_bp / 1e4)])
        _, fires = module._apply_early_exits(
            decision=self._decision(), rule=rule, funding=at_boundary,
            state=CarryCycleState(), root=tmp_path / "a", now_ms=D0 + 2 * MS_PER_HOUR,
        )
        assert fires == [DEEP_A]
        below = self._funding([(DEEP_A, D0 + MS_PER_HOUR, -rule.exit_bp / 1e4 - 1e-6)])
        _, fires = module._apply_early_exits(
            decision=self._decision(), rule=rule, funding=below,
            state=CarryCycleState(), root=tmp_path / "b", now_ms=D0 + 2 * MS_PER_HOUR,
        )
        assert fires == []

    def test_ignores_prints_at_or_before_the_decision_bar(self, tmp_path: Path) -> None:
        # The decision-bar print itself (or older) must never fire: held
        # names always carry a below-threshold print at the bar, and stale
        # recovered prints belong to a previous day's decision.
        funding = self._funding([(DEEP_A, D0, 0.0001), (DEEP_A, D0 - MS_PER_HOUR, 0.0001)])
        _, fires = module._apply_early_exits(
            decision=self._decision(), rule=load_carry_config(), funding=funding,
            state=CarryCycleState(), root=tmp_path, now_ms=D0 + MS_PER_HOUR,
        )
        assert fires == []

    def test_mask_survives_restart_and_expires_with_the_decision_day(self, tmp_path: Path) -> None:
        rule = load_carry_config()
        funding = self._funding([(DEEP_A, D0 + 8 * MS_PER_HOUR, 0.0001)])
        module._apply_early_exits(
            decision=self._decision(), rule=rule, funding=funding,
            state=CarryCycleState(), root=tmp_path, now_ms=D0 + 9 * MS_PER_HOUR,
        )
        # Fresh state (a producer restart): the on-disk mask still applies.
        masked, fires = module._apply_early_exits(
            decision=self._decision(), rule=rule, funding=None,
            state=CarryCycleState(), root=tmp_path, now_ms=D0 + 10 * MS_PER_HOUR,
        )
        assert fires == []
        assert DEEP_A not in masked.weights
        # A new decision day drops yesterday's mask entirely.
        tomorrow = dataclasses.replace(self._decision(), decision_ts_ms=D0 + MS_PER_DAY)
        fresh_state = CarryCycleState()
        unmasked, fires = module._apply_early_exits(
            decision=tomorrow, rule=rule, funding=None,
            state=fresh_state, root=tmp_path, now_ms=D0 + MS_PER_DAY + MS_PER_HOUR,
        )
        assert fires == []
        assert set(unmasked.weights) == {DEEP_A, DEEP_B, RESIZED}
        assert fresh_state.early_exits == {}

    def test_run_cycle_sells_the_recovered_name_intraday(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _RecoveringMarket(_FakeCarryMarket):
            """DEEP_A's prints recover after the decision bar."""

            def get_funding_history(self, symbol: str, start: int, end: int):
                rows = super().get_funding_history(symbol, start, end)
                if symbol == DEEP_A:
                    for row in rows:
                        if int(row["fundingRateTimestamp"]) > D0:
                            row["fundingRate"] = "0.0001"
                return rows

        _route(tmp_path / "route")
        demo_config = _routed_config(tmp_path / "route", early_exit_enabled=True)
        _patch_demo_market_data(monkeypatch)
        standing = pl.DataFrame(
            [
                {
                    "trade_id": CARRY_COMPONENT_ID,
                    "target_key": f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/{DEEP_A}",
                    "strategy_id": CARRY_STRATEGY_ID,
                    "symbol": DEEP_A,
                    "status": "open",
                    "signed_qty": 2.0,
                    "target_reference_price": 100.0,
                }
            ]
        )
        _patch_planning(monkeypatch, standing=standing)
        now = D0 + 8 * MS_PER_HOUR + 25 * 60_000  # past DEEP_A's 08:00 recovery

        payload = module.run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=demo_config,
            market_client=_RecoveringMarket(),
            now_ms=now,
        )

        assert payload["decision_error"] is None
        assert payload["early_exit_enabled"] is True
        assert payload["early_exit_fired"] == [DEEP_A]
        assert payload["early_exit_masked"] == 1
        # The desired book no longer carries the recovered name, and the
        # standing position gets its zero-target sell THIS cycle.
        assert payload["desired_book_size"] == 2
        assert payload["exit_targets_queued"] == 1
        exit_intent = payload.publication.exit_requests[0].request.intents[0].intent
        assert exit_intent.symbol == DEEP_A
        assert exit_intent.signed_notional_usdt == 0.0

        # Second cycle: mask holds, nothing re-fires, book unchanged.
        payload2 = module.run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=demo_config,
            market_client=_RecoveringMarket(),
            now_ms=now + 60_000,
        )
        assert payload2["early_exit_fired"] == []
        assert payload2["early_exit_masked"] == 1
        assert payload2["desired_book_size"] == 2
        # The standing snapshot in this harness never drains, so the same
        # exit re-proposes rather than re-entering: the mask held.
        assert payload2["entry_targets_queued"] == 0

    def test_off_by_default_keeps_the_registered_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        demo_config = _routed_config(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=None)
        payload = module.run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=demo_config,
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS,
        )
        assert payload["early_exit_enabled"] is False
        assert payload["early_exit_fired"] == []
        assert payload["desired_book_size"] == 3


# --- v7 pre-settlement exit (owner-directed 2026-08-19): sell before it pays ---


class _FakeTickerClient:
    """Canned public tickers batch. Venue fields arrive as strings."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows
        self.calls = 0

    def get_tickers(self) -> list[dict[str, str]]:
        self.calls += 1
        return self._rows


class TestPresettleExit:
    def _decision(self) -> CarryDecision:
        return CarryDecision(
            decision_ts_ms=D0,
            weights={DEEP_A: 0.0125, DEEP_B: 0.0247, RESIZED: 0.0125},
            universe_size=56,
            replay_days=60,
            gross=0.0497,
        )

    def _state(self) -> CarryCycleState:
        state = CarryCycleState()
        state.early_exits = {}
        return state

    def test_fires_inside_the_window_and_masks(self, tmp_path: Path) -> None:
        rule = load_carry_config()
        state = self._state()
        now = D0 + 8 * MS_PER_HOUR + 50 * 60_000
        tickers = {
            DEEP_A: (-0.0001, D0 + 9 * MS_PER_HOUR),  # -1 bp, pays in 10 min
            DEEP_B: (-0.0025, D0 + 9 * MS_PER_HOUR),  # still -25 bp deep
        }
        masked, fires = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=state,
            root=tmp_path, now_ms=now, tickers=tickers,
        )
        assert fires == [DEEP_A]
        assert set(masked.weights) == {DEEP_B, RESIZED}
        assert masked.gross == pytest.approx(0.0247 + 0.0125)
        # The mask persists in the SAME file the settled-print path owns.
        assert module._early_exit_state_path(tmp_path).exists()
        reloaded = module._load_early_exits(tmp_path)
        assert reloaded == {DEEP_A: D0}

    def test_boundary_matches_the_registered_state_machine(self, tmp_path: Path) -> None:
        # Identical boundary to the settled-print path: a running rate
        # EXACTLY at -3 bp fires, one strictly below holds.
        rule = load_carry_config()
        pay = D0 + 9 * MS_PER_HOUR
        now = D0 + 8 * MS_PER_HOUR + 50 * 60_000
        _, fires = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "a", now_ms=now,
            tickers={DEEP_A: (-rule.exit_bp / 1e4, pay)},
        )
        assert fires == [DEEP_A]
        _, fires = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "b", now_ms=now,
            tickers={DEEP_A: (-rule.exit_bp / 1e4 - 1e-6, pay)},
        )
        assert fires == []

    def test_only_fires_with_a_settlement_genuinely_ahead(self, tmp_path: Path) -> None:
        rule = load_carry_config()
        pay = D0 + 9 * MS_PER_HOUR
        # 20 minutes ahead: outside the measured 15-minute window.
        _, fires = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "a", now_ms=pay - 20 * 60_000,
            tickers={DEEP_A: (0.0001, pay)},
        )
        assert fires == []
        # Already paid (the ticker not yet rolled): never fire on lead <= 0.
        _, fires = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "b", now_ms=pay,
            tickers={DEEP_A: (0.0001, pay)},
        )
        assert fires == []

    def test_respects_the_standing_mask(self, tmp_path: Path) -> None:
        rule = load_carry_config()
        state = self._state()
        state.early_exits = {DEEP_A: D0}
        masked, fires = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=state,
            root=tmp_path, now_ms=D0 + 8 * MS_PER_HOUR + 50 * 60_000,
            tickers={DEEP_A: (0.0001, D0 + 9 * MS_PER_HOUR)},
        )
        assert fires == []
        assert DEEP_A not in masked.weights

    def test_fetch_fails_open_and_coerces_venue_strings(self) -> None:
        def _broken() -> _FakeTickerClient:
            raise OSError("edge reset")

        tickers, error = module._fetch_presettle_tickers([DEEP_A], _broken)
        assert tickers == {} and "edge reset" in error

        fake = _FakeTickerClient(
            [
                {"symbol": DEEP_A, "fundingRate": "-0.0001",
                 "nextFundingTime": str(D0 + 9 * MS_PER_HOUR)},
                {"symbol": DEEP_B, "fundingRate": "", "nextFundingTime": "x"},
                {"symbol": "UNHELDUSDT", "fundingRate": "0.0001",
                 "nextFundingTime": str(D0 + 9 * MS_PER_HOUR)},
            ]
        )
        tickers, error = module._fetch_presettle_tickers([DEEP_A, DEEP_B], lambda: fake)
        assert error == ""
        # Unparseable rows and unheld names drop; held good rows coerce.
        assert tickers == {DEEP_A: (-0.0001, D0 + 9 * MS_PER_HOUR)}

    def test_run_cycle_sells_before_the_print_pays_under_v7(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _route(tmp_path / "route")
        demo_config = _routed_config(
            tmp_path / "route", strategy_profile="v7", early_exit_enabled=True
        )
        _patch_demo_market_data(monkeypatch)
        ends = _whale_day_ends(D0 + 8 * MS_PER_HOUR + 50 * 60_000)
        monkeypatch.setattr(
            module, "_whale_client_factory",
            lambda: _FakeWhaleClient(_flat_series([DEEP_A], ends)),
        )
        standing = pl.DataFrame(
            [
                {
                    "trade_id": CARRY_COMPONENT_ID,
                    "target_key": f"carry/{CARRY_STRATEGY_ID}/{CARRY_COMPONENT_ID}/{DEEP_A}",
                    "strategy_id": CARRY_STRATEGY_ID,
                    "symbol": DEEP_A,
                    "status": "open",
                    "signed_qty": 2.0,
                    "target_reference_price": 100.0,
                }
            ]
        )
        _patch_planning(monkeypatch, standing=standing)
        # 08:50: the 09:00 settlement is 10 min out (inside window AND the
        # hour-boundary fetch gate); DEEP_A's running rate says the fee died.
        now = D0 + 8 * MS_PER_HOUR + 50 * 60_000
        fake = _FakeTickerClient(
            [
                {"symbol": DEEP_A, "fundingRate": "-0.0001",
                 "nextFundingTime": str(D0 + 9 * MS_PER_HOUR)},
                {"symbol": DEEP_B, "fundingRate": "-0.0025",
                 "nextFundingTime": str(D0 + 9 * MS_PER_HOUR)},
            ]
        )
        monkeypatch.setattr(module, "_presettle_ticker_factory", lambda: fake)

        payload = module.run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=demo_config,
            market_client=_FakeCarryMarket(),
            now_ms=now,
        )

        assert payload["decision_error"] is None
        assert payload["strategy_profile"] == "carry_hold_v7_live_v1"
        assert payload["presettle_exit_enabled"] is True
        assert payload["presettle_error"] == ""
        assert payload["presettle_fired"] == [DEEP_A]
        # The settled-print path saw only deep prints: the sniper alone fired.
        assert payload["early_exit_fired"] == []
        assert payload["early_exit_masked"] == 1
        assert payload["desired_book_size"] == 2
        assert payload["exit_targets_queued"] == 1
        exit_intent = payload.publication.exit_requests[0].request.intents[0].intent
        assert exit_intent.symbol == DEEP_A
        assert exit_intent.signed_notional_usdt == 0.0
        assert fake.calls == 1

    def test_fetch_gate_skips_mid_hour_and_v6_keeps_the_sniper_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode() -> None:
            raise AssertionError("ticker read must not run here")

        monkeypatch.setattr(module, "_presettle_ticker_factory", _explode)
        _route(tmp_path / "route")
        _patch_demo_market_data(monkeypatch)
        _patch_planning(monkeypatch, standing=None)
        # v7 at 00:25: mid-hour, no settlement within window+slack -> no read.
        payload = module.run_carry_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=_routed_config(
                tmp_path / "route", strategy_profile="v7", early_exit_enabled=True
            ),
            market_client=_FakeCarryMarket(),
            now_ms=NOW_MS,
        )
        assert payload["presettle_exit_enabled"] is True
        assert payload["presettle_fired"] == []
        # v6 at 08:50 (inside the gate): the profile keeps the sniper off.
        ends = _whale_day_ends(D0 + 8 * MS_PER_HOUR + 50 * 60_000)
        monkeypatch.setattr(
            module, "_whale_client_factory",
            lambda: _FakeWhaleClient(_flat_series([DEEP_A], ends)),
        )
        payload = module.run_carry_demo_cycle(
            tmp_path / "producer2",
            config=ResearchConfig(),
            demo_config=_routed_config(
                tmp_path / "route", strategy_profile="v6", early_exit_enabled=True
            ),
            market_client=_FakeCarryMarket(),
            now_ms=D0 + 8 * MS_PER_HOUR + 50 * 60_000,
        )
        assert payload["presettle_exit_enabled"] is False
        assert payload["presettle_fired"] == []
