"""audit2b — verification of the flagged kline_store freshness defect.

Flagged defect (kline_store_fresh #1): "Follower newest_ts_ms over-reports
freshness after a leader universe-shrink when recover runs without prune — a
dropped-symbol stale ts still counts as fresh."

VERDICT: NOT a defect within kline_store.py.

``recover_from_disk`` is a documented add-only idempotent keyed MERGE
(see kline_follower.py module docstring). By design it never drops symbols;
dropping a departed symbol is the job of ``keep_only_symbols`` (the follower's
``_prune_to_snapshot`` step). After recovery the cached ``_global_max_ts_ms``
is *always exactly consistent* with the bars actually present in the store, so
``newest_ts_ms()`` never reports a timestamp that is not backed by a real bar.
A dropped-but-not-yet-pruned symbol's bar is genuinely still in the store, and
``keep_only_symbols`` correctly recomputes the global max downward when it is
removed. The over-reporting symptom therefore (a) cannot be fixed inside
``recover_from_disk`` without changing its frozen merge-only semantics and
without effect (the bar is still present), and (b) is corrected by the prune in
kline_follower.py, which is out of this unit's scope.

These tests PIN that verified contract; they pass on the current (unmodified)
source. No source change is made for this audit unit.
"""

from __future__ import annotations

from pathlib import Path

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.kline_store import KlineStore


def _ws_bar(ts_ms: int, *, close: float = 100.0) -> dict:
    return {
        "start": str(ts_ms),
        "open": str(close - 1.0),
        "high": str(close + 1.0),
        "low": str(close - 2.0),
        "close": str(close),
        "volume": "1000",
        "turnover": str(1000.0 * close),
    }


def _max_present_ts(store: KlineStore) -> int:
    # True newest ts of bars actually held, computed independently of the cache.
    return max(max(bars) for bars in store._bars.values() if bars)


def test_recover_keeps_global_max_consistent_with_present_bars(tmp_path: Path) -> None:
    """recover_from_disk is add-only: after every recover, newest_ts_ms() equals
    the true max of bars actually present. It never reports a phantom ts.

    This is the core of the flagged-defect verification: the store's reported
    freshness is always backed by a real stored bar, even across a snapshot that
    shrank the leader universe. (The departed symbol's bar is genuinely still in
    the store until pruned — see the prune test below.)"""
    leader = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    leader.add_bar("AAAUSDT", _ws_bar(100 * MS_PER_HOUR), confirmed=True)
    leader.add_bar("CCCUSDT", _ws_bar(200 * MS_PER_HOUR, close=9.0), confirmed=True)
    leader.flush_to_disk()

    follower = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    follower.recover_from_disk()
    assert follower.newest_ts_ms() == 200 * MS_PER_HOUR
    assert follower.newest_ts_ms() == _max_present_ts(follower)

    # Leader shrinks its universe (drops CCCUSDT), advances AAAUSDT, reflushes.
    leader.keep_only_symbols(["AAAUSDT"])
    leader.add_bar("AAAUSDT", _ws_bar(150 * MS_PER_HOUR, close=5.0), confirmed=True)
    leader.flush_to_disk()

    # recover is add-only: CCCUSDT's bar is still present, so newest_ts_ms()
    # legitimately reflects it — and remains exactly consistent with the bars
    # actually held (no phantom over-report inside the store).
    rows = follower.recover_from_disk()
    assert rows > 0
    assert follower.has_symbol("CCCUSDT")  # add-only merge keeps the departed symbol
    assert follower.newest_ts_ms() == _max_present_ts(follower) == 200 * MS_PER_HOUR


def test_keep_only_symbols_corrects_newest_after_universe_shrink(tmp_path: Path) -> None:
    """The prune corrector: dropping the departed global-max holder recomputes
    the global max downward, so newest_ts_ms() tracks the surviving bars.

    This pins that the fix for the flagged symptom lives in keep_only_symbols
    (already correct), not in recover_from_disk."""
    store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    store.add_bar("AAAUSDT", _ws_bar(150 * MS_PER_HOUR, close=5.0), confirmed=True)
    store.add_bar("CCCUSDT", _ws_bar(200 * MS_PER_HOUR, close=9.0), confirmed=True)
    assert store.newest_ts_ms() == 200 * MS_PER_HOUR  # CCCUSDT holds the max

    dropped = store.keep_only_symbols(["AAAUSDT"])  # leader-shrink -> drop CCCUSDT
    assert dropped == 1
    assert not store.has_symbol("CCCUSDT")
    # Global max recomputed down to the surviving AAAUSDT bar — no over-report.
    assert store.newest_ts_ms() == 150 * MS_PER_HOUR
    assert store.newest_ts_ms() == _max_present_ts(store)


def test_recover_into_empty_store_normal_path_unchanged(tmp_path: Path) -> None:
    """Happy-path guard: a normal round-trip recover (no shrink) is unaffected —
    every bar round-trips and newest_ts_ms() matches the freshest bar."""
    leader = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for hour in range(5):
        ts = hour * MS_PER_HOUR
        leader.add_bar("BTCUSDT", _ws_bar(ts, close=float(hour)), confirmed=True)
        leader.add_bar("ETHUSDT", _ws_bar(ts, close=float(hour + 100)), confirmed=True)
    assert leader.flush_to_disk() == 10

    follower = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    assert follower.recover_from_disk() == 10
    assert follower.newest_ts_ms() == 4 * MS_PER_HOUR
    assert follower.newest_ts_ms() == _max_present_ts(follower)
    assert follower.row_count() == 10
