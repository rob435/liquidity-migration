"""Regression tests for audit bucket b14.

Each test pins a finding from the 2026-06-14 audit (ids in the docstrings). They
are written to FAIL on the original bug and PASS on the committed fix:

  archive-integrity-2  liquidity_migration/binance_vision.py — a corrupt-but-
                   parseable monthly archive is rejected via the .CHECKSUM sha256
                   (or Content-Length fallback) BEFORE it enters the PIT root.
  ingestion-3      liquidity_migration/binance_vision.py — a transient per-symbol
                   S3 listing failure is accumulated and ratio-gated, not crashed.
  ingestion-4      liquidity_migration/binance_vision.py — a rerun that discovers a
                   narrower universe refuses (allow_degraded gate) and the
                   klines_1h rewrite is clean (no stale symbol partitions).
  ratelimit-rest-1 liquidity_migration/kline_stream_manager.py — the shared REST
                   limiter is wired onto the market client so every PAGINATED
                   get_klines HTTP call acquires once, not one acquire per symbol.
  ws-pool-1        liquidity_migration/kline_stream_manager.py — the universe-
                   refresh bootstrap honors the manager's refresh-stop signal so a
                   SIGTERM mid-refresh cancels the REST worker pool promptly.
  hedge-1          liquidity_migration/continuous_hedge_manager.py — the 2f
                   single-leg fallback gate counts joint obs over the TRAILING beta
                   window, so an ETH-thin window with a deep full history still
                   falls back instead of silently going to a zero hedge.
  backfill-writers-2  scripts/backfill_binance_metrics_vision.py — a transient all-
                   day download failure is NOT written as a permanent empty marker
                   the resume guard treats as complete.
"""

from __future__ import annotations

import importlib.util
import inspect
import threading
import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import binance_vision as bv
from liquidity_migration.continuous_hedge_manager import (
    FROZEN_HEDGE_RULE,
    ContinuousHedgeConfig,
    compute_hedge_decision_2f,
)
from liquidity_migration.continuous_rebalance import (
    ContinuousHedge2FState,
    compute_continuous_hedge_ratios_2f,
)
from liquidity_migration.kline_stream_manager import KlineStreamManager
from liquidity_migration.storage import read_dataset, write_dataset

MS_PER_HOUR = 3_600_000


# --------------------------------------------------------------------------
# Helpers for loading the backfill script (not an importable package module).
# --------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent


def _load_backfill_metrics():
    path = _REPO / "scripts" / "backfill_binance_metrics_vision.py"
    spec = importlib.util.spec_from_file_location("bf_metrics_b14", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_klines(root: Path, symbol: str, date_ms: int, n_bars: int) -> None:
    rows = [{
        "ts_ms": date_ms + i * MS_PER_HOUR, "symbol": symbol,
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
    } for i in range(n_bars)]
    write_dataset(pl.DataFrame(rows), root, "klines_1h", partition_by=("date", "symbol"))


# ==========================================================================
# archive-integrity-2
# ==========================================================================

def test_verify_download_raises_on_sha256_mismatch() -> None:
    """archive-integrity-2: a body whose sha256 disagrees with the published
    CHECKSUM must raise (the caller turns this into a retryable/failed job), so a
    corrupt-but-parseable archive never silently enters the PIT root."""
    raw = b"corrupt-but-parseable-zip-bytes"
    wrong_sha = "0" * 64
    with pytest.raises(ValueError, match="sha256 mismatch"):
        bv._verify_download(raw, wrong_sha, content_length=len(raw))


def test_verify_download_passes_on_sha256_match() -> None:
    """archive-integrity-2: a body matching the published sha256 passes."""
    import hashlib

    raw = b"the-real-archive-bytes"
    good_sha = hashlib.sha256(raw).hexdigest()
    # Content-Length deliberately wrong: when a checksum is present it is
    # authoritative and the length check is skipped.
    bv._verify_download(raw, good_sha, content_length=999)


def test_verify_download_falls_back_to_content_length_when_no_checksum() -> None:
    """archive-integrity-2: when the .CHECKSUM sidecar is absent (older months),
    a truncated body is still caught via the advertised Content-Length."""
    raw = b"only-half-here"
    with pytest.raises(ValueError, match="Content-Length mismatch"):
        bv._verify_download(raw, expected_sha256=None, content_length=len(raw) + 100)
    # Matching length passes.
    bv._verify_download(raw, expected_sha256=None, content_length=len(raw))
    # audit2c: with NEITHER checksum NOR Content-Length, the both-absent path is no
    # longer a no-op — a non-zip body is now rejected as unverifiable corruption.
    with pytest.raises(ValueError, match="unverifiable body rejected"):
        bv._verify_download(raw, expected_sha256=None, content_length=None)
    # A genuine valid zip with no checksum/length still passes.
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAAUSDT-1h-2024-01.csv", "1,2,3\n")
    bv._verify_download(buf.getvalue(), expected_sha256=None, content_length=None)


def test_fetch_expected_sha256_parses_leading_hex(monkeypatch) -> None:
    """archive-integrity-2: the CHECKSUM sidecar's leading sha256 hex token is
    parsed (Binance Vision format: '<hex>  <filename>')."""
    digest = "a" * 64
    body = f"{digest}  AAAUSDT-1h-2024-01.zip\n".encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(bv.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert bv._fetch_expected_sha256("https://x/AAA.zip") == digest


# ==========================================================================
# ingestion-3
# ==========================================================================

def test_discover_tolerates_a_transient_listing_failure(monkeypatch) -> None:
    """ingestion-3: a single transient per-symbol S3 listing failure must NOT
    crash the whole build — it is accumulated and ratio-gated. With one failure in
    many symbols, discover returns the surviving inventory instead of raising."""
    symbols = [f"S{i:02d}USDT" for i in range(50)]
    monkeypatch.setattr(bv, "list_usdm_usdt_symbols", lambda: symbols)

    def _fake_list(symbol, *, max_month):
        if symbol == "S00USDT":
            raise OSError("transient S3 listing blip")
        return ["2024-01"]

    monkeypatch.setattr(bv, "list_symbol_months", _fake_list)
    inv = bv.discover(max_month="2024-01", workers=4, max_listing_failure_ratio=0.5)
    # The 49 healthy symbols survive; the build did NOT crash on the one failure.
    assert "S00USDT" not in inv
    assert len(inv) == 49


def test_discover_aborts_when_listing_failures_exceed_ratio(monkeypatch) -> None:
    """ingestion-3: too many transient listing failures still abort (survivorship
    gate) rather than silently producing an under-enumerated universe."""
    symbols = [f"S{i:02d}USDT" for i in range(10)]
    monkeypatch.setattr(bv, "list_usdm_usdt_symbols", lambda: symbols)

    def _fake_list(symbol, *, max_month):
        raise OSError("S3 down")  # every listing fails

    monkeypatch.setattr(bv, "list_symbol_months", _fake_list)
    with pytest.raises(RuntimeError, match="survivorship-biased"):
        bv.discover(max_month="2024-01", workers=4, max_listing_failure_ratio=0.005)


# ==========================================================================
# ingestion-4
# ==========================================================================

def test_persisted_kline_symbols_reads_partition_dirs(tmp_path) -> None:
    """ingestion-4 support: the persisted-universe probe enumerates on-disk
    symbols from the klines_1h partition directories."""
    root = tmp_path / "root"
    jan01 = 1704067200000  # 2024-01-01 00:00 UTC
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "BBBUSDT", jan01, 24)
    assert bv._persisted_kline_symbols(root) == {"AAAUSDT", "BBBUSDT"}
    # No prior build -> empty set, not a crash.
    assert bv._persisted_kline_symbols(tmp_path / "absent") == set()


def test_build_binance_oos_refuses_universe_shrink_without_override(tmp_path, monkeypatch) -> None:
    """ingestion-4: a rerun that discovers FEWER symbols than the persisted
    klines_1h must refuse (would otherwise strand the dropped symbols' partitions
    on disk, which rewrite_manifest_to_coverage then silently retains). The
    original append=True build had no such guard."""
    root = tmp_path / "root"
    jan01 = 1704067200000
    # Prior wider build on disk: AAA + BBB.
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "BBBUSDT", jan01, 24)

    # Rerun discovers only AAA (BBB dropped, e.g. a transient listing shortfall).
    monkeypatch.setattr(bv, "discover", lambda **k: {"AAAUSDT": ["2024-01"]})
    with pytest.raises(RuntimeError, match="shrank|REFUSED"):
        bv.build_binance_oos(root, end_date="2024-02-01")
    # BBB's partitions are untouched (the build refused before writing).
    assert (root / "klines_1h" / "date=2024-01-01" / "symbol=BBBUSDT").exists()


def test_build_binance_oos_clean_rewrite_drops_stale_partitions(tmp_path, monkeypatch) -> None:
    """ingestion-4: with allow_degraded, the narrower rerun OVERWRITES klines_1h
    cleanly — the dropped symbol's stale partition must NOT survive. The original
    append=True write left it behind."""
    root = tmp_path / "root"
    jan01 = 1704067200000
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "BBBUSDT", jan01, 24)

    monkeypatch.setattr(bv, "discover", lambda **k: {"AAAUSDT": ["2024-01"]})

    def _fake_fetch(symbol, ym):
        return [{
            "ts_ms": jan01 + i * MS_PER_HOUR, "symbol": symbol,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
        } for i in range(24)]

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)
    bv.build_binance_oos(root, end_date="2024-02-01", allow_degraded=True)

    # Stale BBB partition is gone; only the freshly-built AAA universe remains.
    klines = read_dataset(root, "klines_1h")
    assert set(klines["symbol"].unique().to_list()) == {"AAAUSDT"}
    assert not (root / "klines_1h" / "date=2024-01-01" / "symbol=BBBUSDT").exists()


# ==========================================================================
# ratelimit-rest-1
# ==========================================================================

class _PaginatingMarketData:
    """Mimics BybitMarketData: get_klines paginates and EACH page acquires the
    wired rate_limiter once (as the real _get does). rate_limiter starts None
    exactly like the daemons build it, so the manager must wire its own."""

    def __init__(self, *, instruments, pages_per_symbol: int) -> None:
        self._instruments = instruments
        self._pages = pages_per_symbol
        self.rate_limiter = None
        self.kline_calls: list[str] = []

    def get_instruments_info(self) -> list[dict]:
        return list(self._instruments)

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        self.kline_calls.append(symbol)
        bars = []
        for page in range(self._pages):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()  # one acquire per HTTP page, like _get
            bars.append({
                "ts_ms": start + page * MS_PER_HOUR, "open": 1.0, "high": 1.0,
                "low": 1.0, "close": 1.0, "volume_base": 1.0, "turnover_quote": 1.0,
            })
        return bars


class _CountingLimiter:
    def __init__(self) -> None:
        self.acquires = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            self.acquires += 1


def _instruments_payload(symbols):
    return [{
        "symbol": s, "status": "Trading", "quoteCoin": "USDT",
        "settleCoin": "USDT", "contractType": "LinearPerpetual",
    } for s in symbols]


def test_bootstrap_symbol_no_longer_takes_a_manual_limiter() -> None:
    """ratelimit-rest-1: the manual once-per-symbol acquire is gone — the
    _bootstrap_symbol signature must no longer accept ``shared_limiter`` (the
    limiter now lives on the market client, acquiring once per paginated call)."""
    params = inspect.signature(KlineStreamManager._bootstrap_symbol).parameters
    assert "shared_limiter" not in params


def test_bootstrap_wires_limiter_and_acquires_once_per_page(tmp_path) -> None:
    """ratelimit-rest-1: bootstrap must rate-limit each PAGINATED get_klines call,
    not once per symbol. With a market client whose get_klines makes 3 pages, a
    2-symbol bootstrap must acquire 6 times (the original once-per-symbol code
    acquired only 2)."""
    symbols = ["AAAUSDT", "BBBUSDT"]
    market = _PaginatingMarketData(
        instruments=_instruments_payload(symbols), pages_per_symbol=3,
    )

    class _NoopPool:
        def subscribe(self, *a, **k): pass
        def update_subscriptions(self, *a, **k): return {}
        def close(self): pass
        def start_watchdog(self): pass
        def stop_watchdog(self): pass
        def stats(self): return {}

    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=30.0,
        flush_interval_seconds=0.0, retain_days=30,
        topics_per_connection=10, pool=_NoopPool(),
    )
    # The manager must have wired ITS limiter onto the (None-limiter) client.
    assert market.rate_limiter is not None
    # Swap in a counting limiter on the client to count per-page acquires.
    counter = _CountingLimiter()
    market.rate_limiter = counter
    manager.start()
    try:
        # 2 symbols * 3 pages each = 6 acquires (NOT 2, the once-per-symbol bug).
        assert counter.acquires == 6
        assert sorted(market.kline_calls) == ["AAAUSDT", "BBBUSDT"]
    finally:
        manager.stop()


# ==========================================================================
# ws-pool-1
# ==========================================================================

def test_refresh_bootstrap_honors_shutdown_promptly(tmp_path) -> None:
    """ws-pool-1: a universe-refresh bootstrap must honor the manager's refresh-
    stop signal. With a slow REST fetch and only a couple of workers, setting
    _refresh_stop mid-flight (what stop() does on SIGTERM) must cancel the pool and
    return promptly instead of running to the 1200s bootstrap deadline. The
    original code passed NO shutdown_event on this path, so it ignored the signal."""
    refresh_targets = [f"SYM{i:02d}USDT" for i in range(20)]

    def _instruments(call_n):
        # Refresh (call >=2) adds the new listings to bootstrap.
        base = ["BTCUSDT"]
        return _instruments_payload(base + (refresh_targets if call_n >= 2 else []))

    started = threading.Event()

    def _slow_klines(symbol, interval, start, end):
        started.set()
        time.sleep(0.5)  # slow REST page
        return [{
            "ts_ms": start, "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 1.0, "volume_base": 1.0, "turnover_quote": 1.0,
        }]

    class _FakeMarket:
        def __init__(self):
            self.rate_limiter = None
            self._n = 0

        def get_instruments_info(self):
            self._n += 1
            return _instruments(self._n)

        def get_klines(self, symbol, interval, start, end):
            return _slow_klines(symbol, interval, start, end)

    class _NoopPool:
        def subscribe(self, *a, **k): pass
        def update_subscriptions(self, *a, **k): return {}
        def close(self): pass
        def start_watchdog(self): pass
        def stop_watchdog(self): pass
        def stats(self): return {}

    market = _FakeMarket()
    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,  # no auto refresh thread
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=1200.0,  # the deadline the orphan would run to
        flush_interval_seconds=0.0, retain_days=30,
        topics_per_connection=10, pool=_NoopPool(),
    )
    manager.start()  # bootstraps BTCUSDT only
    try:
        # Fire the refresh in a background thread; it will start a slow bootstrap
        # of the 20 new listings. Set the manager's refresh-stop shortly after the
        # first REST call begins — this is what stop() does on SIGTERM.
        result: dict = {}

        def _run_refresh():
            result["out"] = manager.force_refresh_universe()

        th = threading.Thread(target=_run_refresh)
        t0 = time.monotonic()
        th.start()
        assert started.wait(timeout=5.0), "refresh bootstrap never issued a REST call"
        manager._refresh_stop.set()  # SIGTERM-equivalent mid-bootstrap
        th.join(timeout=10.0)
        elapsed = time.monotonic() - t0
        assert not th.is_alive(), "refresh bootstrap ignored shutdown — still running"
        # Must return WELL under the 1200s deadline (proves the signal was honored).
        assert elapsed < 30.0, f"refresh bootstrap blocked {elapsed:.1f}s past shutdown"
    finally:
        manager._refresh_stop.clear()
        manager.stop()


# ==========================================================================
# hedge-1
# ==========================================================================

def _hedge_unit_series(n: int):
    btc = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    eth = [0.012 if i % 3 == 0 else -0.006 for i in range(n)]
    unit = [-0.4 * a - 0.3 * b + 0.0005 for a, b in zip(btc, eth)]
    return unit, btc, eth


def test_2f_falls_back_when_trailing_window_eth_thin_but_full_history_deep() -> None:
    """hedge-1: the EXACT shape the original full-series count missed — ETH present
    for the OLD half of the history (full joint >= beta_min_obs) but None for the
    recent beta-window half (window joint < beta_min_obs). The trailing-window beta
    is (0,0); the fallback MUST fire (single-leg BTC) instead of leaving the book
    silently unhedged. The original full-series gate did NOT fire here."""
    n = 200
    unit, btc, eth_full = _hedge_unit_series(n)
    # ETH known for the first 100 rows, None for the last 100 (the beta window).
    eth = list(eth_full[:100]) + [None] * 100

    # Full-series joint count is large (>= beta_min_obs); the windowed count is 0.
    full_joint = sum(1 for b, e in zip(btc, eth) if b is not None and e is not None)
    assert full_joint >= FROZEN_HEDGE_RULE.beta_min_obs  # the trap condition

    cfg = ContinuousHedgeConfig()
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    # Bug behaviour: no fallback, ratio_btc == ratio_eth == 0 (book unhedged).
    # Fixed behaviour: fall back to single-leg BTC with a non-zero hedge.
    assert d.fell_back_to_btc, "2f hedge silently went to zero (no fallback)"
    assert d.ratio_btc > 0.0
    assert d.ratio_eth == 0.0
    # n_obs_joint must report the WINDOWED count the beta actually used (0 here),
    # not the misleading full-series count.
    assert d.n_obs_joint == 0


def test_2f_full_window_still_matches_live_twin() -> None:
    """hedge-1 guardrail: when the trailing window IS ETH-complete the windowed
    gate must NOT spuriously fall back — the decision still matches the live twin."""
    unit, btc, eth = _hedge_unit_series(120)
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=10.0)
    d = compute_hedge_decision_2f(
        cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
        live_gross_short_frac=0.5, btc_price=50_000.0, eth_price=3_000.0,
        current_btc_qty=0.0, current_eth_qty=0.0, equity_usdt=10_000.0,
    )
    r1, r2 = compute_continuous_hedge_ratios_2f(
        ContinuousHedge2FState(tuple(unit), tuple(btc), tuple(eth)), FROZEN_HEDGE_RULE, 1.0,
    )
    assert not d.fell_back_to_btc
    assert d.ratio_btc == pytest.approx(r1, abs=1e-15)
    assert d.ratio_eth == pytest.approx(r2, abs=1e-15)
    assert d.n_obs_joint >= FROZEN_HEDGE_RULE.beta_min_obs


# ==========================================================================
# backfill-writers-2
# ==========================================================================

def test_backfill_transient_all_day_failure_does_not_mark_complete(tmp_path, monkeypatch) -> None:
    """backfill-writers-2: when EVERY day for a symbol fails transiently, the
    writer must NOT touch an empty marker nor mark the symbol complete — otherwise
    the resume guard would freeze a transient outage into permanent zero coverage.
    The original code collapsed transient == 404 and wrote the empty .touch()."""
    bf = _load_backfill_metrics()
    out_dir = tmp_path / "metrics"
    out_dir.mkdir()

    # Every day returns the transient sentinel.
    monkeypatch.setattr(bf, "_day_rows", lambda symbol, day: bf._TRANSIENT_DAY)
    st = bf.backfill_symbol("AAAUSDT", ["2024-01-01", "2024-01-02"], out_dir, workers=2)

    assert st["complete"] is False
    assert st["transient_fail"] == 2
    # No empty marker written — the symbol stays re-runnable.
    assert not (out_dir / "AAAUSDT.parquet").exists()


def test_backfill_genuine_404_all_day_marks_complete(tmp_path, monkeypatch) -> None:
    """backfill-writers-2: a genuine all-404 symbol (real no-data tail) DOES write
    the empty marker and is complete — the fix must not regress the normal case."""
    bf = _load_backfill_metrics()
    out_dir = tmp_path / "metrics"
    out_dir.mkdir()

    monkeypatch.setattr(bf, "_day_rows", lambda symbol, day: None)  # genuine 404
    st = bf.backfill_symbol("BBBUSDT", ["2024-01-01", "2024-01-02"], out_dir, workers=2)

    assert st["complete"] is True
    assert st["days_404"] == 2
    assert st["transient_fail"] == 0
    assert (out_dir / "BBBUSDT.parquet").exists()  # empty no-data marker


def test_backfill_partial_transient_is_incomplete_but_keeps_rows(tmp_path, monkeypatch) -> None:
    """backfill-writers-2: a symbol with SOME real rows AND a transient day keeps
    the fetched rows (parquet written) but is marked incomplete so a re-run fills
    the gap rather than freezing it."""
    bf = _load_backfill_metrics()
    out_dir = tmp_path / "metrics"
    out_dir.mkdir()

    def _day_rows(symbol, day):
        if day == "2024-01-01":
            return [{
                "create_time": "2024-01-01 00:00:00", "symbol": symbol,
                **{c: 1.0 for c in bf.NUM_COLS},
            }]
        return bf._TRANSIENT_DAY  # the second day failed transiently

    monkeypatch.setattr(bf, "_day_rows", _day_rows)
    st = bf.backfill_symbol("CCCUSDT", ["2024-01-01", "2024-01-02"], out_dir, workers=2)

    assert st["rows"] == 1
    assert st["transient_fail"] == 1
    assert st["complete"] is False  # incomplete -> re-runnable
    assert (out_dir / "CCCUSDT.parquet").exists()  # good rows preserved
