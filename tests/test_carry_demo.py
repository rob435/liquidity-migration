"""Target-only decision contract for the CARRY demo and paper producer.

These tests cover the close-keyed venue view, the fail-closed data guards,
the diff-based target planner, and the strict account-owner publication
boundary. Venue orders, fills, P&L, protection, and notifications belong to
the account service and are intentionally absent from this suite.

The integration tests replay the REAL registered rule (decide_book over
``configs/lane2_carry_hold_v3.json``) on a deterministic synthetic market:
period-3 price pattern (ret_3d exactly 0 so the toxic-band filter never
engages; 30d daily vol ~6.5% so the dead-name floor passes) and 8h funding
prints that are benign except for the named deep symbols.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

import liquidity_migration.carry_demo as module
import liquidity_migration.strategy_planning as planning_module
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    entry_attempt_key,
)
from liquidity_migration.account_route import AccountRoute, ensure_account_route
from liquidity_migration.carry_demo import (
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
from liquidity_migration.config import ResearchConfig
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration.strategy_target_replay import PublishedTargetCyclePayload

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
    """Hourly bars with opens in [start, end) — the REST pager's contract."""

    opens = pl.DataFrame({"ts_ms": pl.int_range(start_ms, end_ms, MS_PER_HOUR, eager=True)})
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
        account_id=("bybit-demo-unified" if environment == "demo" else "bybit-paper-unified"),
        environment=environment,
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _routed_config(tmp_path: Path, *, environment: str = "demo", **overrides: Any) -> CarryDemoCycleConfig:
    values: dict[str, Any] = {
        "execution_environment": environment,
        "account_execution_root": str(tmp_path / "account"),
        "account_intent_inbox_root": str(tmp_path / "inbox"),
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
        return SimpleNamespace(equity_usdt=EQUITY)

    monkeypatch.setattr(planning_module, "require_recent_account_owner_health", owner_health)
    monkeypatch.setattr(
        planning_module, "canonical_strategy_trade_rows", lambda *_a, **_k: frame
    )
    monkeypatch.setattr(
        planning_module, "terminal_entry_attempt_keys", lambda *_a, **_k: frozenset()
    )


def _patch_demo_market_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the heavy kline path through the synthetic generator.

    The shim honours the real downloader's boundary contract (opens in
    [start, end)); the funding cache, venue view, and decision replay all run
    for real on top of it.
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
        "standing_notional": {},
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

    def test_diff_emits_exit_resize_and_respects_dead_band(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(
                standing_notional={
                    "AUSDT": 0.05 * EQUITY,  # matches target: no action
                    "CUSDT": 100.0,  # resize up to 0.03 * equity
                    "GONEUSDT": 250.0,  # not desired: exit
                },
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
                standing_notional={"GONEUSDT": 250.0, "CUSDT": 100.0},
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

    def test_missing_decision_plans_nothing(self) -> None:
        plan = _carry_target_plan(
            **_plan_kwargs(decision=None, standing_notional={"GONEUSDT": 250.0})
        )

        assert plan.exit_intents == [] and plan.entry_intents == [] and plan.resize_intents == []
        assert plan.entry_blocked_reason == "decision_unavailable"


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
    with pytest.raises(ValueError, match="operational demo/paper mode requires"):
        _validate_carry_demo_config(CarryDemoCycleConfig(execution_environment="demo"))
    with pytest.raises(ValueError, match="declared_stop_loss_fraction"):
        _validate_carry_demo_config(_routed_config(tmp_path, declared_stop_loss_fraction=1.0))
    with pytest.raises(ValueError, match="replay_days"):
        _validate_carry_demo_config(_routed_config(tmp_path, replay_days=30))
    with pytest.raises(ValueError, match="pure REST"):
        _validate_carry_demo_config(_routed_config(tmp_path, ws_klines_enabled=True))

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
    _validate_carry_demo_config(_routed_config(tmp_path, environment="paper"))


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
    entry_channel = [item.request.intents[0].intent for item in publication.entry_requests]
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


def test_paper_follower_reads_leader_and_fails_closed_on_stale_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _route(tmp_path / "route", environment="paper")
    _patch_planning(monkeypatch, standing=pl.DataFrame())
    leader = tmp_path / "leader"
    window_start_open = D0 - MS_PER_HOUR - 90 * MS_PER_DAY
    klines = _synth_klines(list(ALL_SYMBOLS), start_ms=window_start_open, end_ms=D0)
    compact_dir = leader / ".cache" / "event_demo_klines_1h"
    compact_dir.mkdir(parents=True)
    klines.write_parquet(compact_dir / "latest_window.parquet")
    funding_rows = [
        {
            "symbol": symbol,
            "funding_ts_ms": int(row["fundingRateTimestamp"]),
            "funding_rate": float(row["fundingRate"]),
        }
        for symbol in ALL_SYMBOLS
        for row in _funding_rows(symbol, window_start_open - 2 * MS_PER_DAY, D0 + 1)
    ]
    write_dataset(
        pl.DataFrame(funding_rows),
        leader,
        CARRY_FUNDING_DATASET,
        partition_by=("symbol",),
    )
    demo_config = _routed_config(
        tmp_path / "route",
        environment="paper",
        market_follow_root=str(leader),
    )

    payload = run_carry_demo_cycle(
        tmp_path / "paper-producer",
        config=ResearchConfig(),
        demo_config=demo_config,
        now_ms=NOW_MS,
    )

    assert payload["data_source"] == "follower"
    assert payload["decision_error"] is None
    assert payload["desired_book_size"] == 3
    # Nothing standing on the paper book yet: all three desired names enter.
    assert payload["entry_targets_queued"] == 3
    assert payload["exit_targets_queued"] == 0

    # A leader whose newest kline close is older than the freshness gate must
    # be treated exactly like a demo data failure: hold, do not decide.
    stale = run_carry_demo_cycle(
        tmp_path / "paper-producer-stale",
        config=ResearchConfig(),
        demo_config=demo_config,
        now_ms=NOW_MS + 3 * MS_PER_DAY,
    )
    assert stale["decision_error"] and "stale" in stale["decision_error"]
    assert stale["target_intents_queued"] == 0


def test_run_cycle_refuses_circular_self_follow(tmp_path: Path) -> None:
    _route(tmp_path / "route", environment="paper")
    own_root = tmp_path / "producer"
    own_root.mkdir()

    with pytest.raises(ValueError, match="circular self-follow"):
        run_carry_demo_cycle(
            own_root,
            config=ResearchConfig(),
            demo_config=_routed_config(
                tmp_path / "route",
                environment="paper",
                market_follow_root=str(own_root),
            ),
            now_ms=NOW_MS,
        )


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


def test_cold_cache_view_trims_leading_partial_day_to_midnight() -> None:
    # A cold-started cache begins at the bootstrap hour; the first cycle's
    # view then opens mid-day and decide_book's phase guard refuses it
    # (observed live 2026-07-29: window starting 03:00 UTC). The cycle layer
    # must trim to the first 00:00 UTC key so the daily grid keeps the
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
