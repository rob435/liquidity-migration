"""audit2 regression: binance funding-vision backfill — two defects.

(1) In-progress current month cached as a permanent 404. The monthly Vision
    archive for the current (and just-closed-but-not-yet-published) month does
    not exist yet, so _month_rows fetched a 404 and wrote an empty marker
    (cpath.write_bytes(b"")). That stale empty cache was never re-promoted once
    the archive published, so a later re-run still read the hole. Fix: do NOT
    persist an empty-404 marker for months at/after the cache cutoff (current
    month, plus the just-closed month during the publish-lag window). Only
    strictly-older months get a permanent empty marker.

(2) fapi top-up did not paginate. _fapi_topup issued a single GET with limit=1000
    and returned only that page, so a symbol needing >1000 settlements since its
    last archive row silently lost the tail. Fix: loop the fapi fetch, advancing
    startTime to (last fundingTime + 1) until a short page (<1000 rows) returns,
    accumulating all pages.

These tests FAIL against the old code and PASS with the fix.
"""
from __future__ import annotations

import importlib.util
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import zipfile

import polars as pl

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "backfill_binance_funding_vision.py"
)
_spec = importlib.util.spec_from_file_location("backfill_binance_funding_vision", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# --------------------------------------------------------------------------- #
# Defect (1): the current month must NOT be cached as a permanent empty 404.
# --------------------------------------------------------------------------- #
def test_current_month_404_not_cached_as_permanent_marker(tmp_path, monkeypatch):
    cache = tmp_path / "_funding_rebuild_cache"
    cache.mkdir()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")

    # Patch the network fetch to 404 (None) for the current month's archive.
    calls: list[str] = []

    def fake_fetch(url: str, timeout: int = 30):
        calls.append(url)
        return None  # 404 -> no archive yet for the in-progress month

    monkeypatch.setattr(_mod, "_fetch", fake_fetch)

    # cache_cutoff == this_month means the current month is at/after the cutoff,
    # so it must be left uncached.
    rows = _mod._month_rows("BTCUSDT", this_month, cache, this_month)
    assert rows == []
    assert len(calls) == 1  # it actually attempted the fetch

    cpath = cache / f"BTCUSDT-{this_month}.zip"
    # CORRECTED behaviour: no permanent empty marker is written for the current
    # month, so a later re-run is free to re-fetch once the archive publishes.
    # OLD code wrote an empty file here, blocking the re-fetch forever.
    assert not cpath.exists(), "current-month 404 must not be cached as an empty marker"

    # And a re-run still hits the network (no stale empty cache short-circuits it).
    calls.clear()
    _mod._month_rows("BTCUSDT", this_month, cache, this_month)
    assert len(calls) == 1, "re-run must re-attempt the fetch, not read a stale empty cache"


def test_old_month_404_is_cached_permanently(tmp_path, monkeypatch):
    """Normal (unchanged) behaviour: a strictly-older 404 month IS cached."""
    cache = tmp_path / "_funding_rebuild_cache"
    cache.mkdir()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    old_month = "2019-09"  # genuinely perp-funding-free, far in the past

    calls: list[str] = []

    def fake_fetch(url: str, timeout: int = 30):
        calls.append(url)
        return None  # permanent 404 for a long-gone month

    monkeypatch.setattr(_mod, "_fetch", fake_fetch)

    rows = _mod._month_rows("DEADCOINUSDT", old_month, cache, this_month)
    assert rows == []
    cpath = cache / f"DEADCOINUSDT-{old_month}.zip"
    # Strictly-older 404 months ARE cached (empty marker) so re-runs skip the net.
    assert cpath.exists() and cpath.read_bytes() == b""

    calls.clear()
    _mod._month_rows("DEADCOINUSDT", old_month, cache, this_month)
    assert calls == [], "an older cached 404 must short-circuit without re-fetching"


def test_cache_cutoff_protects_just_closed_month_in_publish_lag():
    """Within the publish-lag window the immediately-prior month is also protected."""
    # Day 2 of a month -> cutoff backs up to the prior month (May here).
    early = datetime(2026, 6, 2, tzinfo=timezone.utc)
    assert _mod._cache_cutoff_month(early) == "2026-05"
    # Year boundary.
    early_jan = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _mod._cache_cutoff_month(early_jan) == "2025-12"
    # Well past the lag window -> cutoff is just the current month.
    mid = datetime(2026, 6, 20, tzinfo=timezone.utc)
    assert _mod._cache_cutoff_month(mid) == "2026-06"


def test_vision_rows_preserve_missing_or_nonpositive_interval_as_null(tmp_path):
    cache = tmp_path / "_funding_rebuild_cache"
    cache.mkdir()
    symbol = "ALTUSDT"
    month = "2026-05"
    cpath = cache / f"{symbol}-{month}.zip"
    csv_text = "\n".join(
        [
            "calc_time,last_funding_rate,funding_interval_hours",
            "1000,0.0001,4",
            "2000,0.0002,",
            "3000,0.0003,0",
            "4000,0.0004,0.0",
            "5000,0.0005,-1",
        ]
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{symbol}-fundingRate-{month}.csv", csv_text)
    cpath.write_bytes(buf.getvalue())

    rows = _mod._month_rows(symbol, month, cache, "2026-06")

    assert [r["funding_interval_min"] for r in rows] == [240, None, None, None, None]
    assert all(r["funding_interval_min"] != 480 for r in rows)


def test_fapi_topup_preserves_missing_interval_metadata(monkeypatch):
    since_ms = 1_700_000_000_000

    def fake_fetch(url: str, timeout: int = 30):
        st = int(url.split("startTime=")[1].split("&")[0])
        return _make_page(st, 2)

    monkeypatch.setattr(_mod, "_fetch", fake_fetch)

    rows = _mod._fapi_topup("BTCUSDT", since_ms)

    assert [r["funding_interval_min"] for r in rows] == [None, None]


# --------------------------------------------------------------------------- #
# Defect (2): _fapi_topup must paginate across full pages.
# --------------------------------------------------------------------------- #
def _make_page(start_ts: int, n: int, step: int = 8 * 3_600_000) -> bytes:
    """Build a JSON funding-rate page of n settlements starting at start_ts."""
    rows = [
        {
            "fundingTime": start_ts + i * step,
            "fundingRate": "0.0001",
        }
        for i in range(n)
    ]
    return json.dumps(rows).encode("utf-8")


def test_fapi_topup_paginates_across_full_pages(monkeypatch):
    limit = 1000
    step = 8 * 3_600_000
    since_ms = 1_700_000_000_000

    # Two FULL pages (1000 rows) then a SHORT page (3 rows). The mock serves the
    # next page starting at the requested startTime each call, so the production
    # cursor advance (last fundingTime + 1) chains the pages into one contiguous,
    # strictly-increasing, de-duplicated timeline of 2003 settlements.
    page_sizes = [limit, limit, 3]
    seen_starts: list[int] = []
    call_idx = {"i": 0}

    def fake_fetch(url: str, timeout: int = 30):
        st = int(url.split("startTime=")[1].split("&")[0])
        seen_starts.append(st)
        i = call_idx["i"]
        if i >= len(page_sizes):
            return None  # no more data
        call_idx["i"] += 1
        return _make_page(st, page_sizes[i], step)

    monkeypatch.setattr(_mod, "_fetch", fake_fetch)

    rows = _mod._fapi_topup("BTCUSDT", since_ms)

    # OLD code returned only the first 1000 rows; the fix accumulates all pages.
    assert len(rows) == 2003, f"expected all pages accumulated, got {len(rows)}"
    # It actually advanced startTime across the pages (each request > the prior).
    assert seen_starts[0] == since_ms + 1
    assert len(seen_starts) >= 3
    assert seen_starts == sorted(seen_starts)
    assert seen_starts[1] > seen_starts[0] and seen_starts[2] > seen_starts[1]
    # Sorted + deduped on fundingTime.
    ts = [r["ts_ms"] for r in rows]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)
    assert all(r["symbol"] == "BTCUSDT" for r in rows)
    assert all(r["source"] == "binance_usdm_funding_fapi" for r in rows)


def test_fapi_topup_single_short_page_unchanged(monkeypatch):
    """Normal (unchanged) behaviour: a single short page returns exactly its rows."""
    since_ms = 1_700_000_000_000
    step = 8 * 3_600_000

    def fake_fetch(url: str, timeout: int = 30):
        st = int(url.split("startTime=")[1].split("&")[0])
        return _make_page(st, 5, step)  # one short page, then nothing more

    # Make only the first start serve data; any later start serves an empty list
    # so a (correctly) paginating loop terminates on the short page.
    first_start = since_ms + 1

    def fake_fetch_guard(url: str, timeout: int = 30):
        st = int(url.split("startTime=")[1].split("&")[0])
        if st == first_start:
            return _make_page(st, 5, step)
        return json.dumps([]).encode("utf-8")

    monkeypatch.setattr(_mod, "_fetch", fake_fetch_guard)

    rows = _mod._fapi_topup("ETHUSDT", since_ms)
    assert len(rows) == 5
    assert rows[0]["ts_ms"] == first_start
    assert [r["funding_rate"] for r in rows] == [0.0001] * 5


def test_fapi_topup_empty_response(monkeypatch):
    """No data at all -> empty result (unchanged)."""
    monkeypatch.setattr(_mod, "_fetch", lambda url, timeout=30: None)
    assert _mod._fapi_topup("NOPEUSDT", 1_700_000_000_000) == []


# ============================================================================
# Relocated from tests/test_audit_fix_b09.py (audit bucket b09, backfill-writers-4).
# Vision-first cross-source dedup is a provenance invariant: when vision and fapi
# emit the same (symbol, ts_ms), the VISION row must win deterministically.
# ============================================================================


def test_funding_dedup_keeps_vision_row_deterministically(monkeypatch) -> None:
    """backfill-writers-4: vision-first ordering + unique(keep='first', maintain_order=True)
    must keep the VISION row when vision and fapi emit the same (symbol, ts_ms)."""
    all_rows = [
        {"ts_ms": 100, "symbol": "ETHUSDT", "funding_rate": 0.0001, "mark_price": float("nan"),
         "funding_interval_min": 240, "source": "binance_usdm_funding_vision"},
        {"ts_ms": 100, "symbol": "ETHUSDT", "funding_rate": 0.0001, "mark_price": float("nan"),
         "funding_interval_min": 480, "source": "binance_usdm_funding_fapi"},
    ]
    df = (
        pl.DataFrame(all_rows)
        .unique(["symbol", "ts_ms"], keep="first", maintain_order=True)
        .sort(["symbol", "ts_ms"])
    )
    assert df.height == 1
    assert df["source"].to_list() == ["binance_usdm_funding_vision"]
    assert df["funding_interval_min"].to_list() == [240]
