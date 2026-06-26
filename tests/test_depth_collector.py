"""Tests for the Bybit forward depth collector: band aggregation + universe hardening."""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import liquidity_migration.depth_collector as depth_collector
from liquidity_migration.depth_collector import _refresh_universe, band_notionals, trading_universe


def test_band_cumulative_notional_and_null_beyond_span() -> None:
    # mid = 100; bids at -0.1%, -0.9%, -1.8%; asks at +0.1%, +0.5% only (thin ask side)
    bids = [(99.9, 10.0), (99.1, 20.0), (98.2, 30.0)]
    asks = [(100.1, 5.0), (100.5, 5.0)]
    out = band_notionals(bids, asks)
    assert out is not None
    assert out["mid"] == pytest.approx(100.0)
    # bid side spans 1.8% -> 0.2% and 1% bands measured, 2%+ unmeasured (None)
    assert out["bid_0p2"] == pytest.approx(99.9 * 10.0)
    assert out["bid_1p0"] == pytest.approx(99.9 * 10.0 + 99.1 * 20.0)
    assert out["bid_2p0"] is None and out["bid_5p0"] is None
    assert out["bid_span_pct"] == pytest.approx(1.8, abs=1e-3)
    # ask side spans only 0.5% -> even the 1% band is unmeasured
    assert out["ask_0p2"] == pytest.approx(100.1 * 5.0)
    assert out["ask_1p0"] is None
    assert out["n_bid_levels"] == 3 and out["n_ask_levels"] == 2


def test_deep_book_measures_all_bands() -> None:
    bids = [(100.0 - i * 0.5, 1.0) for i in range(1, 13)]  # to -6%
    asks = [(100.0 + i * 0.5, 1.0) for i in range(1, 13)]
    out = band_notionals(bids, asks)
    assert out is not None
    for band in ("0p2", "1p0", "2p0", "3p0", "4p0", "5p0"):
        assert out[f"bid_{band}"] is not None
        assert out[f"ask_{band}"] is not None
    # 5% band on the bid side: levels 99.5 .. 95.0 (10 levels within -5% of mid 100)
    assert out["bid_5p0"] == pytest.approx(sum(100.0 - i * 0.5 for i in range(1, 11)))


def test_empty_side_returns_none() -> None:
    assert band_notionals([], [(100.1, 1.0)]) is None
    assert band_notionals([(99.9, 1.0)], []) is None


def test_trading_universe_breaks_on_repeating_cursor(monkeypatch) -> None:
    """A misbehaving endpoint that returns the same nextPageCursor forever must
    not spin trading_universe into an unbounded request loop."""
    calls: list[str] = []

    def fake_get_json(url: str) -> dict:
        calls.append(url)
        return {
            "result": {
                "list": [{"symbol": "AAAUSDT", "status": "Trading"}],
                "nextPageCursor": "samecursor",
            }
        }

    monkeypatch.setattr(depth_collector, "_get_json", fake_get_json)
    assert trading_universe() == ["AAAUSDT"]
    # page 1 (empty cursor) yields "samecursor"; page 2 repeats it -> break
    assert len(calls) == 2


def test_trading_universe_caps_at_ten_pages(monkeypatch) -> None:
    """Distinct cursors forever: pagination is hard-capped at 10 pages
    (mirrors the liquidation collector's defensive cap)."""
    counter = {"n": 0}

    def fake_get_json(url: str) -> dict:
        counter["n"] += 1
        return {
            "result": {
                "list": [
                    {"symbol": f"S{counter['n']:02d}USDT", "status": "Trading"},
                    {"symbol": f"X{counter['n']:02d}USDT", "status": "PreLaunch"},
                    {"symbol": f"P{counter['n']:02d}USDC", "status": "Trading"},
                ],
                "nextPageCursor": f"cursor-{counter['n']}",
            }
        }

    monkeypatch.setattr(depth_collector, "_get_json", fake_get_json)
    symbols = trading_universe()
    assert counter["n"] == 10
    # only Trading USDT perps survive the filter, one per page
    assert symbols == sorted(f"S{i:02d}USDT" for i in range(1, 11))


def test_refresh_universe_failure_keeps_previous_universe(monkeypatch) -> None:
    """A transient instruments-info failure must not propagate out of the
    periodic refresh (it used to raise out of main() and kill the daemon)."""

    def network_boom() -> list[str]:
        raise urllib.error.URLError("instruments endpoint down")

    monkeypatch.setattr(depth_collector, "trading_universe", network_boom)
    assert _refresh_universe(["BTCUSDT", "ETHUSDT"]) == ["BTCUSDT", "ETHUSDT"]
    assert _refresh_universe([]) == []  # nothing to fall back on -> empty, no raise

    def payload_boom() -> list[str]:
        raise json.JSONDecodeError("bad payload", "doc", 0)

    monkeypatch.setattr(depth_collector, "trading_universe", payload_boom)
    assert _refresh_universe(["BTCUSDT"]) == ["BTCUSDT"]

    monkeypatch.setattr(depth_collector, "trading_universe", lambda: ["NEWUSDT"])
    assert _refresh_universe(["BTCUSDT"]) == ["NEWUSDT"]  # success replaces previous


# --------------------------------------------------------------------------
# depth-collector-2: per-cycle wall-clock budget aborts before bleeding the hour
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def test_collect_cycle_aborts_on_budget_and_reports_skipped(tmp_path, monkeypatch) -> None:
    """depth-collector-2: a slow cycle must stop at the wall-clock budget instead of
    bleeding past the hour, and must report how many symbols were skipped."""
    symbols = [f"S{i}USDT" for i in range(10)]

    # Each snapshot advances a fake monotonic clock by 1s; pacing is a no-op.
    clock = {"t": 0.0}
    monkeypatch.setattr(depth_collector.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_a, **_k: None)

    def fake_snapshot(sym: str) -> dict:
        clock["t"] += 1.0
        return {"recv_ms": 1, "venue": "bybit", "symbol": sym, "mid": 100.0}

    monkeypatch.setattr(depth_collector, "snapshot_symbol", fake_snapshot)
    monkeypatch.setattr(depth_collector.os, "fsync", lambda *_a, **_k: None)

    stats = depth_collector.collect_cycle(tmp_path, symbols, cycle_budget_seconds=3.0)
    # Budget hit after ~3 snapshots; the remaining are skipped, not captured.
    assert stats["skipped"] > 0
    assert stats["ok"] + stats["skipped"] == len(symbols)
    assert stats["ok"] < len(symbols)


def test_collect_cycle_no_budget_collects_all(tmp_path, monkeypatch) -> None:
    """depth-collector-2: with the budget disabled (<=0) every symbol is captured."""
    symbols = [f"S{i}USDT" for i in range(5)]
    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(depth_collector.os, "fsync", lambda *_a, **_k: None)
    monkeypatch.setattr(
        depth_collector,
        "snapshot_symbol",
        lambda sym: {"recv_ms": 1, "venue": "bybit", "symbol": sym, "mid": 100.0},
    )
    stats = depth_collector.collect_cycle(tmp_path, symbols, cycle_budget_seconds=0.0)
    assert stats["ok"] == 5
    assert stats["skipped"] == 0


# --------------------------------------------------------------------------
# depth-collector-3: retention (gzip/prune) + free-disk floor
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def test_enforce_retention_gzips_old_and_prunes_ancient(tmp_path) -> None:
    """depth-collector-3: day files older than the gzip window are compressed; files
    past the prune window are deleted; the current day is left untouched."""
    bybit = tmp_path / "bybit"
    bybit.mkdir(parents=True)
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)

    def day_file(days_ago: int) -> Path:
        d = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        p = bybit / f"{d}.jsonl"
        p.write_text('{"recv_ms": 1}\n', encoding="utf-8")
        return p

    current = day_file(0)
    recent = day_file(3)  # inside gzip window -> untouched
    old = day_file(10)  # past gzip window -> gzipped
    ancient = day_file(90)  # past prune window -> deleted

    stats = depth_collector.enforce_retention(
        tmp_path, gzip_after_days=7, prune_after_days=60, now=now
    )
    assert stats["gzipped"] == 1
    assert stats["pruned"] == 1
    assert current.exists()
    assert recent.exists()
    assert not old.exists() and old.with_suffix(".jsonl.gz").exists()
    assert not ancient.exists()


def test_free_disk_bytes_walks_to_existing_ancestor(tmp_path) -> None:
    """depth-collector-3: the free-disk probe resolves to an existing ancestor even
    when the target root does not exist yet (used as the pre-cycle guard)."""
    missing = tmp_path / "does" / "not" / "exist" / "depth"
    free = depth_collector.free_disk_bytes(missing)
    assert isinstance(free, int)
    assert free > 0


def test_min_free_disk_floor_is_defined_and_positive() -> None:
    """depth-collector-3: the guard threshold the daemon loop checks must exist."""
    assert isinstance(depth_collector.MIN_FREE_DISK_BYTES, int)
    assert depth_collector.MIN_FREE_DISK_BYTES > 0


# --------------------------------------------------------------------------
# depth-collector-4: tolerant reader skips a truncated trailing line; flush/fsync
# (relocated from tests/test_audit_fix_b06.py)
# --------------------------------------------------------------------------


def test_iter_jsonl_rows_tolerates_truncated_trailing_line(tmp_path) -> None:
    """depth-collector-4: a crash mid-write can leave a truncated final line. The
    reader skips ONLY a bad trailing line; a bad EARLIER line is genuine corruption."""
    path = tmp_path / "2026-06-14.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3', encoding="utf-8")  # truncated tail
    rows = list(depth_collector.iter_jsonl_rows(path))
    assert rows == [{"a": 1}, {"b": 2}]


def test_iter_jsonl_rows_raises_on_interior_corruption(tmp_path) -> None:
    """depth-collector-4: a parse error on a NON-final line is real corruption and
    must propagate, not be silently swallowed."""
    path = tmp_path / "2026-06-14.jsonl"
    path.write_text('{"a": 1}\nNOT JSON\n{"c": 3}\n', encoding="utf-8")
    import json as _json

    with pytest.raises(_json.JSONDecodeError):
        list(depth_collector.iter_jsonl_rows(path))


def test_collect_cycle_flushes_to_disk(tmp_path, monkeypatch) -> None:
    """depth-collector-4: the writer flushes at cycle end so the captured rows are
    durably readable (and fsync is invoked)."""
    fsync_calls = {"n": 0}

    def fake_fsync(_fd: int) -> None:
        fsync_calls["n"] += 1

    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(depth_collector.os, "fsync", fake_fsync)
    monkeypatch.setattr(
        depth_collector,
        "snapshot_symbol",
        lambda sym: {"recv_ms": 1, "venue": "bybit", "symbol": sym, "mid": 100.0},
    )
    depth_collector.collect_cycle(tmp_path, ["AAAUSDT", "BBBUSDT"], cycle_budget_seconds=0.0)
    assert fsync_calls["n"] >= 1
    # Rows are durably on disk and round-trip through the tolerant reader.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = tmp_path / "bybit" / f"{day}.jsonl"
    rows = list(depth_collector.iter_jsonl_rows(out))
    assert len(rows) == 2


def test_band_notionals_rejects_crossed_or_locked_book() -> None:
    """audit-iter2 collectors-1: a crossed (best_bid > best_ask) or locked
    (best_bid == best_ask) snapshot must return None, not a bogus mid + bands."""
    assert band_notionals([(101.0, 5.0)], [(100.0, 5.0)]) is None  # crossed
    assert band_notionals([(100.0, 5.0)], [(100.0, 5.0)]) is None  # locked
    # a normal book still aggregates
    assert band_notionals([(99.9, 5.0)], [(100.1, 5.0)]) is not None


def test_main_cycles_runs_bounded_number_of_cycles(tmp_path, monkeypatch) -> None:
    """A finite multi-cycle local capture can prove hourly accrual without
    enabling the systemd daemon or leaving the collector running forever."""
    calls: list[list[str]] = []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "depth_collector",
            "--root",
            str(tmp_path),
            "--cycles",
            "2",
            "--symbols",
            "BTCUSDT,ETHUSDT",
        ],
    )
    monkeypatch.setattr(depth_collector, "free_disk_bytes", lambda _root: depth_collector.MIN_FREE_DISK_BYTES + 1)
    monkeypatch.setattr(depth_collector, "collect_cycle", lambda _root, symbols: calls.append(list(symbols)) or {})
    monkeypatch.setattr(depth_collector, "enforce_retention", lambda _root: {})
    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(depth_collector.time, "time", lambda: 0.0)

    depth_collector.main()

    assert calls == [["BTCUSDT", "ETHUSDT"], ["BTCUSDT", "ETHUSDT"]]


def test_cycles_and_once_are_mutually_exclusive() -> None:
    parser = depth_collector.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--cycles", "2"])


def test_cycles_must_be_positive(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["depth_collector", "--cycles", "0"])
    with pytest.raises(SystemExit):
        depth_collector.main()
