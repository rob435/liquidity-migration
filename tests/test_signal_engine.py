from __future__ import annotations

import asyncio
import sqlite3
from collections import deque
from pathlib import Path

import aiohttp
import numpy as np

from alerting import TelegramNotifier
from config import Settings
from database import SignalDatabase
from signal_engine import SignalEngine
from state import IntrabarObservation, MarketState


def test_signal_engine_logs_full_cross_section(tmp_path: Path) -> None:
    asyncio.run(_exercise_signal_engine(tmp_path))


def test_signal_engine_only_sets_cooldown_after_successful_alert(tmp_path: Path) -> None:
    asyncio.run(_exercise_cooldown_behavior(tmp_path))


def test_signal_engine_handles_alert_exceptions_without_losing_logs(tmp_path: Path) -> None:
    asyncio.run(_exercise_alert_exception_behavior(tmp_path))


def test_signal_engine_emits_emerging_once_per_transition_and_ignores_confirmed_stage(tmp_path: Path) -> None:
    asyncio.run(_exercise_emerging_transition_behavior(tmp_path))


def test_signal_engine_can_disable_all_signal_telegram_alerts(tmp_path: Path) -> None:
    asyncio.run(_exercise_signal_alert_toggle(tmp_path))


def test_signal_engine_blocks_entry_ready_when_intraday_regime_is_not_tradeable(tmp_path: Path) -> None:
    asyncio.run(_exercise_intraday_regime_block(tmp_path))


def test_signal_engine_can_disable_intraday_regime_filter(tmp_path: Path) -> None:
    asyncio.run(_exercise_intraday_regime_disabled(tmp_path))


def test_signal_engine_confirmed_stage_emits_no_trade_signals(tmp_path: Path) -> None:
    asyncio.run(_exercise_confirmed_stage_is_inert(tmp_path))


def test_signal_engine_entry_ready_includes_residual_diagnostics(tmp_path: Path) -> None:
    asyncio.run(_exercise_entry_ready_diagnostics(tmp_path))


async def _exercise_signal_engine(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "signals.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
    )
    state = MarketState(settings=settings)

    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray([20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)], dtype=float)

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()

    async with aiohttp.ClientSession() as session:
        engine = SignalEngine(
            settings=settings,
            state=state,
            database=database,
            notifier=TelegramNotifier(session=session, bot_token=None, chat_id=None),
        )
        await engine.process(cycle_time_ms=timestamps[-1], stage="emerging")

    with sqlite3.connect(settings.sqlite_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        ranked = connection.execute(
            "SELECT ticker, rank, price, dom_state, dom_change_pct FROM signals ORDER BY composite_score DESC"
        ).fetchall()
        assert count == 3
        assert ranked[0][0] == "AAAUSDT"
        assert ranked[0][1] == 1
        assert ranked[0][2] > 0
        assert ranked[0][3] in {"falling", "neutral", "rising"}
        assert isinstance(ranked[0][4], float)


async def _exercise_cooldown_behavior(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "cooldown.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        watchlist_telegram_enabled=True,
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray([20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)], dtype=float)
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms
    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 1.5) is True

    class StubNotifier:
        def __init__(self, should_succeed: bool) -> None:
            self.should_succeed = should_succeed

        async def send(self, payload) -> bool:
            return self.should_succeed

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()

    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=StubNotifier(should_succeed=False),
    )
    await engine.process(cycle_time_ms=next_timestamp, stage="emerging")
    assert "AAAUSDT" not in state.last_alerted
    state.intrabar_state["AAAUSDT"] = "neutral"

    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=StubNotifier(should_succeed=True),
    )
    await engine.process(cycle_time_ms=next_timestamp, stage="emerging")
    assert "AAAUSDT" in state.last_alerted


async def _exercise_alert_exception_behavior(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "alert-errors.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray([20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)], dtype=float)

    class ExplodingNotifier:
        async def send(self, payload) -> bool:
            raise RuntimeError("network down")

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=ExplodingNotifier(),
    )
    await engine.process(cycle_time_ms=timestamps[-1], stage="emerging")

    assert "AAAUSDT" not in state.last_alerted
    with sqlite3.connect(settings.sqlite_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert count == 3


async def _exercise_emerging_transition_behavior(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "emerging.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        watchlist_telegram_enabled=True,
        top_n=1,
        emerging_top_n=1,
        entry_ready_top_n=1,
        watchlist_top_n=5,
        emerging_min_observations=3,
        emerging_min_rank_improvement=2,
        entry_ready_min_observations=5,
        entry_ready_min_rank_improvement=2,
        entry_ready_min_composite_gain=0.01,
        intraday_regime_filter_enabled=False,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray(
        [20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)],
        dtype=float,
    )

    class RecordingNotifier:
        def __init__(self) -> None:
            self.kinds: list[str] = []

        async def send(self, payload) -> bool:
            if payload.ticker == "AAAUSDT":
                self.kinds.append(payload.signal_kind)
            return True

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    notifier = RecordingNotifier()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=notifier,
    )

    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 1.5) is True
    watchlist_signals = await engine.process(cycle_time_ms=next_timestamp, stage="emerging")
    assert any(
        signal.ticker == "AAAUSDT" and signal.signal_kind == "watchlist"
        for signal in watchlist_signals
    )
    assert notifier.kinds == ["watchlist"]

    state.intrabar_observations["AAAUSDT"] = deque(
        [
            IntrabarObservation(observed_at_ms=next_timestamp - 20_000, rank=3, composite_score=0.1),
            IntrabarObservation(observed_at_ms=next_timestamp - 10_000, rank=2, composite_score=0.2),
        ],
        maxlen=state.intrabar_observations["AAAUSDT"].maxlen,
    )
    state.intrabar_state["AAAUSDT"] = "watchlist"

    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 2.0) is True
    emerging_signals = await engine.process(cycle_time_ms=next_timestamp + 20_000, stage="emerging")
    assert any(
        signal.ticker == "AAAUSDT" and signal.signal_kind == "emerging"
        for signal in emerging_signals
    )
    assert notifier.kinds == ["watchlist", "emerging"]

    state.intrabar_observations["AAAUSDT"] = deque(
        [
            IntrabarObservation(observed_at_ms=next_timestamp - 40_000, rank=4, composite_score=0.05),
            IntrabarObservation(observed_at_ms=next_timestamp - 30_000, rank=3, composite_score=0.10),
            IntrabarObservation(observed_at_ms=next_timestamp - 20_000, rank=2, composite_score=0.15),
            IntrabarObservation(observed_at_ms=next_timestamp - 10_000, rank=1, composite_score=0.20),
        ],
        maxlen=state.intrabar_observations["AAAUSDT"].maxlen,
    )

    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 2.5) is True
    entry_ready_signals = await engine.process(cycle_time_ms=next_timestamp + 40_000, stage="emerging")
    assert any(
        signal.ticker == "AAAUSDT" and signal.signal_kind == "entry_ready"
        for signal in entry_ready_signals
    )
    assert notifier.kinds == ["watchlist", "emerging", "entry_ready"]

    assert state.append_close("BTCUSDT", next_timestamp, btc_prices[-1] + 50.0) is True
    assert state.append_close("AAAUSDT", next_timestamp, strong[-1] + 2.5) is True
    assert state.append_close("BBBUSDT", next_timestamp, base[-1] + 0.02) is True
    assert state.append_close("CCCUSDT", next_timestamp, weak[-1] + 0.01) is True

    confirmed_signals = await engine.process(cycle_time_ms=next_timestamp, stage="confirmed")
    assert confirmed_signals == []
    assert notifier.kinds == ["watchlist", "emerging", "entry_ready"]


async def _exercise_intraday_regime_block(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "intraday-regime-block.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        watchlist_telegram_enabled=True,
        top_n=1,
        emerging_top_n=1,
        entry_ready_top_n=1,
        watchlist_top_n=5,
        emerging_min_observations=3,
        emerging_min_rank_improvement=2,
        entry_ready_min_observations=5,
        entry_ready_min_rank_improvement=2,
        entry_ready_min_composite_gain=0.01,
        intraday_regime_filter_enabled=True,
        intraday_regime_lookback_bars=16,
        intraday_regime_min_basket_return=0.10,
        intraday_regime_min_pass_count=4,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray(
        [20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)],
        dtype=float,
    )

    class SilentNotifier:
        async def send(self, payload) -> bool:
            return True

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=SilentNotifier(),
    )

    state.intrabar_observations["AAAUSDT"] = deque(
        [
            IntrabarObservation(observed_at_ms=next_timestamp - 40_000, rank=5, composite_score=-0.1),
            IntrabarObservation(observed_at_ms=next_timestamp - 30_000, rank=3, composite_score=0.1),
            IntrabarObservation(observed_at_ms=next_timestamp - 20_000, rank=2, composite_score=0.2),
            IntrabarObservation(observed_at_ms=next_timestamp - 10_000, rank=1, composite_score=0.3),
        ],
        maxlen=state.intrabar_observations["AAAUSDT"].maxlen,
    )
    state.intrabar_state["AAAUSDT"] = "emerging"
    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 2.5) is True

    signals = await engine.process(cycle_time_ms=next_timestamp, stage="emerging")

    aaa_signal = next(signal for signal in signals if signal.ticker == "AAAUSDT")
    assert aaa_signal.signal_kind in {"watchlist", "emerging"}
    assert aaa_signal.signal_kind != "entry_ready"
    regime = engine.last_intraday_regime["emerging"]
    assert regime is not None
    assert regime.tradeable is False
    assert "basket_return" in regime.blockers


async def _exercise_intraday_regime_disabled(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "intraday-regime-disabled.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        watchlist_telegram_enabled=True,
        top_n=1,
        emerging_top_n=1,
        entry_ready_top_n=1,
        watchlist_top_n=5,
        emerging_min_observations=3,
        emerging_min_rank_improvement=2,
        entry_ready_min_observations=5,
        entry_ready_min_rank_improvement=2,
        entry_ready_min_composite_gain=0.01,
        intraday_regime_filter_enabled=False,
        intraday_regime_lookback_bars=16,
        intraday_regime_min_basket_return=0.10,
        intraday_regime_min_pass_count=4,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray(
        [20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)],
        dtype=float,
    )

    class SilentNotifier:
        async def send(self, payload) -> bool:
            return True

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=SilentNotifier(),
    )

    state.intrabar_observations["AAAUSDT"] = deque(
        [
            IntrabarObservation(observed_at_ms=next_timestamp - 40_000, rank=5, composite_score=-0.1),
            IntrabarObservation(observed_at_ms=next_timestamp - 30_000, rank=3, composite_score=0.1),
            IntrabarObservation(observed_at_ms=next_timestamp - 20_000, rank=2, composite_score=0.2),
            IntrabarObservation(observed_at_ms=next_timestamp - 10_000, rank=1, composite_score=0.3),
        ],
        maxlen=state.intrabar_observations["AAAUSDT"].maxlen,
    )
    state.intrabar_state["AAAUSDT"] = "emerging"
    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 2.5) is True

    signals = await engine.process(cycle_time_ms=next_timestamp, stage="emerging")

    aaa_signal = next(signal for signal in signals if signal.ticker == "AAAUSDT")
    assert aaa_signal.signal_kind == "entry_ready"
    regime = engine.last_intraday_regime["emerging"]
    assert regime is not None
    assert regime.tradeable is True


async def _exercise_signal_alert_toggle(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "signal-toggle.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        telegram_signal_alerts_enabled=False,
        watchlist_telegram_enabled=True,
        top_n=1,
        emerging_top_n=1,
        entry_ready_top_n=1,
        watchlist_top_n=5,
        emerging_min_observations=3,
        emerging_min_rank_improvement=2,
        entry_ready_min_observations=5,
        entry_ready_min_rank_improvement=2,
        entry_ready_min_composite_gain=0.01,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray(
        [20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)],
        dtype=float,
    )

    class RecordingNotifier:
        def __init__(self) -> None:
            self.kinds: list[str] = []

        async def send(self, payload) -> bool:
            self.kinds.append(payload.signal_kind)
            return True

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    notifier = RecordingNotifier()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=notifier,
    )

    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 1.5) is True
    signals = await engine.process(cycle_time_ms=next_timestamp, stage="emerging")

    assert any(signal.ticker == "AAAUSDT" and signal.signal_kind == "watchlist" for signal in signals)
    assert notifier.kinds == []

    with sqlite3.connect(settings.sqlite_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        watchlist_count = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_kind = 'watchlist'"
        ).fetchone()[0]
        assert row_count >= 1
        assert watchlist_count >= 1


async def _exercise_confirmed_stage_is_inert(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "confirmed-persistence.db"),
        universe=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        top_n=1,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [100 + idx * 0.03 for idx in range(settings.state_window)]
    base = [100 + idx * 0.02 for idx in range(settings.state_window)]
    weak = [100 + idx * 0.01 for idx in range(settings.state_window)]

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("BBBUSDT", list(zip(timestamps, base)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray(
        [20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)],
        dtype=float,
    )
    class RecordingNotifier:
        def __init__(self) -> None:
            self.kinds: list[str] = []

        async def send(self, payload) -> bool:
            if payload.ticker == "AAAUSDT":
                self.kinds.append(payload.signal_kind)
            return True

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    notifier = RecordingNotifier()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=notifier,
    )

    confirmed_signals = await engine.process(cycle_time_ms=next_timestamp, stage="confirmed")

    assert confirmed_signals == []
    assert notifier.kinds == []
    with sqlite3.connect(settings.sqlite_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert row_count == 0


async def _exercise_entry_ready_diagnostics(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_path=str(tmp_path / "entry-diagnostics.db"),
        universe=["AAAUSDT", "AABUSDT", "CCCUSDT"],
        telegram_bot_token=None,
        telegram_chat_id=None,
        momentum_reference_mode="cluster_relative",
        cluster_assignment_mode="dynamic",
        cluster_correlation_lookback_bars=24,
        cluster_correlation_threshold=0.9,
        top_n=1,
        emerging_top_n=1,
        entry_ready_top_n=1,
        watchlist_top_n=5,
        emerging_min_observations=1,
        emerging_min_rank_improvement=0,
        entry_ready_min_observations=1,
        entry_ready_min_rank_improvement=0,
        entry_ready_min_composite_gain=0.0,
        hurst_cutoff=-1.0,
        intraday_regime_filter_enabled=False,
        regime_thresholds={0: None, 1: None, 2: None, 3: None},
    )
    state = MarketState(settings=settings)
    timestamps = [idx * settings.ticker_interval_ms for idx in range(settings.state_window)]
    btc_prices = [20_000 + idx * 50 for idx in range(settings.state_window)]
    strong = [
        100 + (idx * 0.03) + (0.2 if idx % 5 == 0 else -0.1 if idx % 7 == 0 else 0.05)
        for idx in range(settings.state_window)
    ]
    peer = [
        100 + (idx * 0.029) + (0.18 if idx % 5 == 0 else -0.08 if idx % 7 == 0 else 0.04)
        for idx in range(settings.state_window)
    ]
    weak = [
        100 + (idx * 0.004) + (0.15 if idx % 3 == 0 else -0.12 if idx % 4 == 0 else 0.01)
        for idx in range(settings.state_window)
    ]
    next_timestamp = timestamps[-1] + settings.ticker_interval_ms

    state.replace_history("BTCUSDT", list(zip(timestamps, btc_prices)))
    state.replace_history("AAAUSDT", list(zip(timestamps, strong)))
    state.replace_history("AABUSDT", list(zip(timestamps, peer)))
    state.replace_history("CCCUSDT", list(zip(timestamps, weak)))
    state.global_state.btc_daily_closes = np.asarray(
        [20_000 + idx * 100 for idx in range(settings.btc_daily_lookback)],
        dtype=float,
    )

    class SilentNotifier:
        async def send(self, payload) -> bool:
            return True

    database = SignalDatabase(settings.sqlite_path)
    await database.initialize()
    engine = SignalEngine(
        settings=settings,
        state=state,
        database=database,
        notifier=SilentNotifier(),
    )

    assert state.update_provisional("AAAUSDT", next_timestamp, strong[-1] + 2.0) is True
    assert state.update_provisional("AABUSDT", next_timestamp, peer[-1] + 1.9) is True
    signals = await engine.process(cycle_time_ms=next_timestamp, stage="emerging")
    aaa_signal = next(signal for signal in signals if signal.ticker == "AAAUSDT")
    assert aaa_signal.signal_kind == "entry_ready"
    assert "ref=cluster_relative:" in aaa_signal.entry_diagnostics
    assert "cluster=corr:" in aaa_signal.entry_diagnostics
    assert "rank_gain=" in aaa_signal.entry_diagnostics
