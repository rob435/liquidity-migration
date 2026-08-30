"""Target-only decision contract for the CARRY producer: close-keyed venue
view, fail-closed data guards, and Rust-engine target books.

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
from typing import Any

import polars as pl
import pytest

import liquidity_migration.strategy.carry_demo as module
from liquidity_migration.core._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.marketdata.kline_store import KlineStore
from liquidity_migration.strategy.carry_demo import (
    CarryCycleState,
    CarryDecision,
    CarryDemoCycleConfig,
    CarrySleeveError,
    _carry_venue_view,
    _validate_carry_demo_config,
    _validate_carry_view_health,
    carry_decision_ts_ms,
    format_carry_demo_cycle_summary,
    load_carry_config,
)
from liquidity_migration.core.config import ResearchConfig

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

    invalid_standing = healthy.with_columns(
        pl.when(pl.col("symbol") == "A")
        .then(float("nan"))
        .otherwise(pl.col("by_funding"))
        .alias("by_funding")
    )
    with pytest.raises(CarrySleeveError, match="non-finite"):
        _validate_carry_view_health(
            invalid_standing,
            decision_ts_ms=D0,
            standing_symbols={"A"},
        )


@pytest.mark.parametrize("bad_rate", [float("nan"), float("inf"), -float("inf")])
def test_normalized_funding_events_reject_non_finite_rates(bad_rate: float) -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["AUSDT"],
            "funding_ts_ms": [D0],
            "funding_rate": [bad_rate],
        }
    )
    with pytest.raises(CarrySleeveError, match="non-finite"):
        module._normalized_funding_events(frame)


def test_funding_ingress_drops_non_finite_venue_rows(tmp_path: Path) -> None:
    class _NonFiniteMarket:
        def get_funding_history(
            self, symbol: str, start: int, end: int
        ) -> list[dict[str, str]]:
            return [
                {
                    "fundingRateTimestamp": str(D0),
                    "fundingRate": "nan",
                }
            ]

    state = CarryCycleState()
    funding, stats = module._refresh_carry_funding_cache(
        tmp_path,
        _NonFiniteMarket(),
        ["AUSDT"],
        now_ms=D0 + MS_PER_HOUR,
        replay_days=1,
        state=state,
    )
    assert funding.is_empty()
    assert stats["funding_rows_appended"] == 0








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
            "exit_book_removals": 1,
            "entry_book_additions": 2,
            "book_resizes": 1,
            "book_written": True,
            "equity_usdt": 10_000.0,
        }
    )

    assert line.startswith("carry target producer")
    assert "decision_day=2024-10-04" in line
    assert "book_delta exit/entry/resize=1/2/1" in line
    assert "written=True" in line
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
    demo = CarryDemoCycleConfig(
        execution_environment="demo",
        replay_days=45,
        workers=2,
    )

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
        # v7 is an execution-clock version: it reads the same rule file
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




# --- wave-2 boundary anatomy: grouped exits, pre-inbox read elimination, and
# --- the freeze-time equity anchor (2026-08-13) ---

#: 00:19:50 UTC: still pre-deadline, inside the freeze window, close enough to
#: the boundary that the reading it re-stamps is fresh at BOUNDARY_NOW.
REFRESH_NOW = D0 + 20 * 60_000 - 10_000














# ---------------------------------------------------------------------------
# The engine target book: what research decided, written where the Rust engine
# can follow it. Publication is required and failures stop the producer cycle.
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
    entry_deadline = (
        1786665600000
        + module.SIGNAL_VALIDITY_MS
        - module.ENTRY_PUBLISH_GUARD_MS
    )
    assert book["version"] == 2
    assert book["source"] == "carry_hold_v4_live_v1"
    assert book["decision_ts_ms"] == 1786665600000
    assert book["valid_until_ms"] == 1786665600000 + module.DECISION_STALE_MS
    by_symbol = {row["symbol"]: row for row in book["targets"]}
    # weight * sizing equity * multiplier, which is what the sleeve sizes with.
    assert by_symbol["KAITOUSDT"]["notional_usdt"] == pytest.approx(100.0)
    assert by_symbol["COTIUSDT"]["notional_usdt"] == pytest.approx(50.0)
    assert by_symbol["KAITOUSDT"]["stop_loss_fraction"] == 0.35
    assert by_symbol["KAITOUSDT"]["entry_valid_until_ms"] == entry_deadline
    assert by_symbol["COTIUSDT"]["entry_valid_until_ms"] == entry_deadline


def test_no_path_means_no_book(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(module.ENGINE_TARGET_BOOK_PATH_ENV, raising=False)
    with pytest.raises(ValueError, match=module.ENGINE_TARGET_BOOK_PATH_ENV):
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


# NaN is not here on purpose: it never reached the file anyway, because the
# finite-JSON writer refuses it. These two are the cases this guard decides.
@pytest.mark.parametrize("equity", [0.0, -1.0])
def test_an_unusable_equity_leaves_the_standing_book_alone(tmp_path, monkeypatch, equity) -> None:
    # A failed owner-health read returns equity 0.0, and every notional would
    # then render 0.0 -- which the engine reads as an explicit exit, before any
    # validity window. Writing it would flatten the whole sleeve at market on a
    # transient heartbeat gap, so nothing is written and the last book stands.
    path = tmp_path / "carry_targets.json"
    path.write_text('{"targets": "the standing book"}', encoding="utf-8")
    with pytest.raises(ValueError, match="positive sizing equity"):
        _write_book(tmp_path, monkeypatch, sizing_equity_usdt=equity)
    assert path.read_text(encoding="utf-8") == '{"targets": "the standing book"}'


def test_an_empty_decision_writes_an_empty_book(tmp_path, monkeypatch) -> None:
    # Deciding cash is a decision and the engine must be able to act on it.
    book = json.loads(_write_book(tmp_path, monkeypatch, desired={}).read_text(encoding="utf-8"))
    assert book["targets"] == []


def test_a_book_that_cannot_be_written_fails_the_producer_cycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(module.ENGINE_TARGET_BOOK_PATH_ENV, str(tmp_path / "x.json"))
    monkeypatch.setattr(
        module, "publish_target_book", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
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
        cfg = load_carry_config(module._CONFIGS_DIR / "lane2_carry_hold_v7.json")
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
            DEEP_A: (-0.0001, D0 + 9 * MS_PER_HOUR, 12.5),  # -1 bp, pays in 10 min
            DEEP_B: (-0.0025, D0 + 9 * MS_PER_HOUR, 8.0),  # still -25 bp deep
        }
        masked, fires, details = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=state,
            root=tmp_path, now_ms=now, tickers=tickers,
        )
        assert fires == [DEEP_A]
        assert set(masked.weights) == {DEEP_B, RESIZED}
        assert masked.gross == pytest.approx(0.0247 + 0.0125)
        # The exodus trigger rides the same fire, with the contemporaneous
        # mark and settlement captured before the mask deletes the name.
        assert [
            (d.symbol, d.settlement_ts_ms, d.mark_px) for d in details
        ] == [(DEEP_A, D0 + 9 * MS_PER_HOUR, 12.5)]
        # The mask persists in the SAME file the settled-print path owns.
        assert module._early_exit_state_path(tmp_path).exists()
        reloaded = module._load_early_exits(tmp_path)
        assert reloaded == {DEEP_A: D0}

    @pytest.mark.parametrize("stamp", [str(D0), True, 1.5])
    def test_early_exit_state_does_not_coerce_stamps(
        self, tmp_path: Path, stamp: object
    ) -> None:
        path = module._early_exit_state_path(tmp_path)
        path.write_text(json.dumps({"fired": {DEEP_A: stamp}}))

        with pytest.raises(ValueError, match="invalid row"):
            module._load_early_exits(tmp_path)

    def test_boundary_matches_the_registered_state_machine(self, tmp_path: Path) -> None:
        # Identical boundary to the settled-print path: a running rate
        # EXACTLY at -3 bp fires, one strictly below holds.
        rule = load_carry_config()
        pay = D0 + 9 * MS_PER_HOUR
        now = D0 + 8 * MS_PER_HOUR + 50 * 60_000
        _, fires, _ = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "a", now_ms=now,
            tickers={DEEP_A: (-rule.exit_bp / 1e4, pay, 10.0)},
        )
        assert fires == [DEEP_A]
        _, fires, _ = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "b", now_ms=now,
            tickers={DEEP_A: (-rule.exit_bp / 1e4 - 1e-6, pay, 10.0)},
        )
        assert fires == []

    def test_only_fires_with_a_settlement_genuinely_ahead(self, tmp_path: Path) -> None:
        rule = load_carry_config()
        pay = D0 + 9 * MS_PER_HOUR
        # 20 minutes ahead: outside the measured 15-minute window.
        _, fires, _ = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "a", now_ms=pay - 20 * 60_000,
            tickers={DEEP_A: (0.0001, pay, 10.0)},
        )
        assert fires == []
        # Already paid (the ticker not yet rolled): never fire on lead <= 0.
        _, fires, _ = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=self._state(),
            root=tmp_path / "b", now_ms=pay,
            tickers={DEEP_A: (0.0001, pay, 10.0)},
        )
        assert fires == []

    def test_respects_the_standing_mask(self, tmp_path: Path) -> None:
        rule = load_carry_config()
        state = self._state()
        state.early_exits = {DEEP_A: D0}
        masked, fires, details = module._apply_presettle_exits(
            decision=self._decision(), rule=rule, state=state,
            root=tmp_path, now_ms=D0 + 8 * MS_PER_HOUR + 50 * 60_000,
            tickers={DEEP_A: (0.0001, D0 + 9 * MS_PER_HOUR, 10.0)},
        )
        assert fires == []
        assert details == []
        assert DEEP_A not in masked.weights

    def test_fetch_fails_open_and_coerces_venue_strings(self) -> None:
        def _broken() -> _FakeTickerClient:
            raise OSError("edge reset")

        tickers, error = module._fetch_presettle_tickers([DEEP_A], _broken)
        assert tickers == {} and "edge reset" in error

        fake = _FakeTickerClient(
            [
                {"symbol": DEEP_A, "fundingRate": "-0.0001",
                 "nextFundingTime": str(D0 + 9 * MS_PER_HOUR), "markPrice": "12.5"},
                {"symbol": DEEP_B, "fundingRate": "", "nextFundingTime": "x"},
                {"symbol": "UNHELDUSDT", "fundingRate": "0.0001",
                 "nextFundingTime": str(D0 + 9 * MS_PER_HOUR)},
            ]
        )
        tickers, error = module._fetch_presettle_tickers([DEEP_A, DEEP_B], lambda: fake)
        assert error == ""
        # Unparseable rows and unheld names drop; held good rows coerce.
        assert tickers == {DEEP_A: (-0.0001, D0 + 9 * MS_PER_HOUR, 12.5)}




# --- drop exit (part of the exit clock 2026-08-23): sell the zeroed before 00:20 ---


class TestDropExit:
    def _decision(self, ts_ms: int) -> CarryDecision:
        return CarryDecision(
            decision_ts_ms=ts_ms,
            weights={DEEP_A: 0.0125, DEEP_B: 0.0247, RESIZED: 0.0125},
            universe_size=56,
            replay_days=60,
            gross=0.0497,
        )

    def _state_with_upcoming(
        self, *, upcoming_weights: dict[str, float] | None = None
    ) -> CarryCycleState:
        """Yesterday served, today frozen ahead: the drop-exit precondition."""

        state = CarryCycleState()
        state.freeze_decision(
            decision_ts_ms=D0 - MS_PER_DAY,
            decision=self._decision(D0 - MS_PER_DAY),
            trail_by_symbol={},
            universe_eligible=56,
        )
        upcoming = CarryDecision(
            decision_ts_ms=D0,
            weights=(
                {DEEP_B: 0.0247, RESIZED: 0.0125}
                if upcoming_weights is None
                else upcoming_weights
            ),
            universe_size=56,
            replay_days=60,
            gross=sum((upcoming_weights or {}).values()),
        )
        state.freeze_decision(
            decision_ts_ms=D0,
            decision=upcoming,
            trail_by_symbol={},
            universe_eligible=56,
        )
        return state

    def test_masks_exactly_the_names_the_upcoming_book_zeroes(self) -> None:
        masked, dropped, count = module._apply_drop_exits(
            decision=self._decision(D0 - MS_PER_DAY),
            state=self._state_with_upcoming(),
        )
        assert dropped == [DEEP_A]
        assert count == 1
        assert set(masked.weights) == {DEEP_B, RESIZED}
        assert masked.gross == pytest.approx(0.0247 + 0.0125)

    def test_a_smaller_upcoming_weight_is_a_resize_not_a_drop(self) -> None:
        masked, dropped, count = module._apply_drop_exits(
            decision=self._decision(D0 - MS_PER_DAY),
            state=self._state_with_upcoming(
                upcoming_weights={
                    DEEP_A: 0.006,  # halved in place: still desired
                    DEEP_B: 0.0247,
                    RESIZED: 0.0125,
                }
            ),
        )
        assert dropped == []
        assert count == 0
        assert set(masked.weights) == {DEEP_A, DEEP_B, RESIZED}

    def test_no_frozen_upcoming_book_is_a_noop(self) -> None:
        state = CarryCycleState()
        decision = self._decision(D0 - MS_PER_DAY)
        masked, dropped, count = module._apply_drop_exits(
            decision=decision, state=state
        )
        assert dropped == []
        assert count == 0
        assert masked.weights == decision.weights

    def _drop_market(self) -> _FakeCarryMarket:
        class _PersistGoneMarket(_FakeCarryMarket):
            """DEEP_A's crowd never persists: every print shallower than the
            -10 bp entry depth fails the persistence cut, so the D0 replay
            zeroes it while the pre-seeded old-day book still holds it."""

            def get_funding_history(self, symbol: str, start: int, end: int):
                rows = super().get_funding_history(symbol, start, end)
                if symbol == DEEP_A:
                    for row in rows:
                        row["fundingRate"] = "-0.0005"  # -5 bp: holds, never deep
                return rows

        return _PersistGoneMarket()




# --- the exodus short (owner-directed 2026-08-20): the fire flips to a short ---


class TestExodusShort:
    SETTLE = D0 + 9 * MS_PER_HOUR

    def _fire(self) -> "module.PresettleFire":
        return module.PresettleFire(
            symbol=DEEP_A, settlement_ts_ms=self.SETTLE, mark_px=10.0
        )

    def _arm(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        book = tmp_path / "targets" / "exodus-demo.json"
        monkeypatch.setenv("EXODUS_SHORT_PROFILE", "v1")
        monkeypatch.setenv("EXODUS_ENGINE_TARGET_BOOK_PATH", str(book))
        return book

    def test_absent_env_means_absent_sleeve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EXODUS_SHORT_PROFILE", raising=False)
        monkeypatch.delenv("EXODUS_ENGINE_TARGET_BOOK_PATH", raising=False)
        receipt = module._run_exodus_short(
            state=CarryCycleState(), root=tmp_path, fires=[self._fire()],
            carry_holdings={DEEP_A: ("long", 3.25, 8.0)}, entry_leverage=2.0,
            now_ms=self.SETTLE - 10 * 60_000,
        )
        assert receipt == {}
        assert not module._exodus_state_path(tmp_path).exists()

    def test_original_unversioned_empty_state_publishes_a_fresh_book(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        module._exodus_state_path(tmp_path).write_text(
            '{"open": []}\n', encoding="utf-8"
        )
        receipt = module._run_exodus_short(
            state=CarryCycleState(),
            root=tmp_path,
            fires=[],
            carry_holdings=None,
            entry_leverage=2.0,
            now_ms=self.SETTLE - 10 * 60_000,
        )
        assert receipt["exodus_error"] == ""
        assert receipt["exodus_open_names"] == 0
        assert json.loads(book_path.read_text(encoding="utf-8"))["targets"] == []

    def test_a_fire_opens_the_exact_abandoned_quantity_as_a_short(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        state = CarryCycleState()
        now = self.SETTLE - 10 * 60_000
        receipt = module._run_exodus_short(
            state=state, root=tmp_path, fires=[self._fire()],
            carry_holdings={DEEP_A: ("long", 3.25, 8.0)}, entry_leverage=5.0, now_ms=now,
        )
        assert receipt["exodus_opened"] == [DEEP_A]
        assert receipt["exodus_open_names"] == 1
        assert receipt["exodus_error"] == ""
        # The cover clock is the wake accelerator the daemon adopts.
        assert receipt["exodus_next_cover_ts_ms"] == self.SETTLE + 60 * 60_000
        book = json.loads(book_path.read_text(encoding="utf-8"))
        (target,) = book["targets"]
        # The short IS carry's actual attributed quantity. Notional is marked
        # from the same ticker sample; entry price and desired-weight math are
        # deliberately irrelevant.
        assert target["symbol"] == DEEP_A
        assert target["notional_usdt"] == -32.5
        assert target["target_qty"] == -3.25
        assert target["stop_loss_fraction"] == 0.35
        # Leverage is the operational profile's dial, not the registered
        # file's constant (which stays 2.0): the deployment margin knob
        # must reach the book without touching the evidence contract.
        assert target["leverage"] == 5.0
        assert book["valid_until_ms"] == self.SETTLE + 20 * 60_000
        # Persisted: a restart re-renders the same book from disk.
        stored = module._load_exodus_shorts(tmp_path)[0]
        assert stored.notional_usdt == 32.5
        assert stored.target_qty == 3.25
        # The same fire again does not double the position.
        receipt = module._run_exodus_short(
            state=state, root=tmp_path, fires=[self._fire()],
            carry_holdings={DEEP_A: ("long", 3.25, 8.0)}, entry_leverage=2.0,
            now_ms=now + 60_000,
        )
        assert receipt["exodus_opened"] == []
        assert receipt["exodus_open_names"] == 1

    def test_covers_on_the_clock_and_the_book_drains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        module._save_exodus_shorts(
            tmp_path,
            [module.ExodusShortRecord(DEEP_A, 50.0, self.SETTLE, self.SETTLE - 600_000)],
        )
        receipt = module._run_exodus_short(
            state=CarryCycleState(), root=tmp_path, fires=[],
            carry_holdings=None, entry_leverage=2.0,
            now_ms=self.SETTLE + 60 * 60_000,
            exodus_held_symbols=frozenset(),
            exodus_working_entry_symbols=frozenset(),
        )
        assert receipt["exodus_covered"] == [DEEP_A]
        assert receipt["exodus_open_names"] == 0
        assert json.loads(book_path.read_text(encoding="utf-8"))["targets"][0][
            "notional_usdt"
        ] == 0.0
        assert module._load_exodus_shorts(tmp_path) == []

    def test_dial_off_drains_to_flat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        monkeypatch.delenv("EXODUS_SHORT_PROFILE")
        module._save_exodus_shorts(
            tmp_path,
            [module.ExodusShortRecord(DEEP_A, 50.0, self.SETTLE, self.SETTLE - 600_000)],
        )
        receipt = module._run_exodus_short(
            state=CarryCycleState(), root=tmp_path, fires=[],
            carry_holdings=None, entry_leverage=2.0,
            # Well before the cover clock: off means flat NOW, not at S+60.
            now_ms=self.SETTLE - 5 * 60_000,
            exodus_held_symbols=frozenset(),
            exodus_working_entry_symbols=frozenset(),
        )
        assert receipt["exodus_enabled"] is False
        assert receipt["exodus_covered"] == [DEEP_A]
        assert json.loads(book_path.read_text(encoding="utf-8"))["targets"][0][
            "notional_usdt"
        ] == 0.0
        assert module._load_exodus_shorts(tmp_path) == []

    def test_no_actual_carry_holding_blocks_the_entry_for_good(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        receipt = module._run_exodus_short(
            state=CarryCycleState(), root=tmp_path, fires=[self._fire()],
            carry_holdings=None, entry_leverage=2.0,
            now_ms=self.SETTLE - 10 * 60_000,
        )
        assert receipt["exodus_entry_blocked"] == [DEEP_A]
        assert receipt["exodus_opened"] == []
        assert json.loads(book_path.read_text(encoding="utf-8"))["targets"] == []

    @pytest.mark.parametrize(
        "fire,holdings",
        [
            (
                module.PresettleFire(
                    symbol=DEEP_A,
                    settlement_ts_ms=SETTLE,
                    mark_px=None,
                ),
                {DEEP_A: ("long", 3.25, 8.0)},
            ),
            (
                module.PresettleFire(
                    symbol=DEEP_A,
                    settlement_ts_ms=SETTLE,
                    mark_px=10.0,
                ),
                {DEEP_A: ("short", 3.25, 8.0)},
            ),
        ],
    )
    def test_an_incomplete_or_non_long_handoff_is_blocked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fire: "module.PresettleFire",
        holdings: dict[str, tuple[str, float, float]],
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        receipt = module._run_exodus_short(
            state=CarryCycleState(),
            root=tmp_path,
            fires=[fire],
            carry_holdings=holdings,
            entry_leverage=2.0,
            now_ms=self.SETTLE - 10 * 60_000,
        )
        assert receipt["exodus_entry_blocked"] == [DEEP_A]
        assert json.loads(book_path.read_text(encoding="utf-8"))["targets"] == []

    def test_bookkeeping_failure_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXODUS_SHORT_PROFILE", "v1")
        # The book path is a DIRECTORY: the write must fail, be receipted,
        # and leave the carry cycle alone.
        monkeypatch.setenv("EXODUS_ENGINE_TARGET_BOOK_PATH", str(tmp_path))
        receipt = module._run_exodus_short(
            state=CarryCycleState(), root=tmp_path, fires=[self._fire()],
            carry_holdings={DEEP_A: ("long", 3.25, 8.0)}, entry_leverage=2.0,
            now_ms=self.SETTLE - 10 * 60_000,
        )
        assert receipt["exodus_error"] != ""

    def test_an_unknown_profile_is_inert_and_receipted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book_path = self._arm(monkeypatch, tmp_path)
        monkeypatch.setenv("EXODUS_SHORT_PROFILE", "v9")
        receipt = module._run_exodus_short(
            state=CarryCycleState(), root=tmp_path, fires=[self._fire()],
            carry_holdings={DEEP_A: ("long", 3.25, 8.0)}, entry_leverage=2.0,
            now_ms=self.SETTLE - 10 * 60_000,
        )
        assert "unknown exodus profile" in receipt["exodus_error"]
        assert not book_path.exists()
