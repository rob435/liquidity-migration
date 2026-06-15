"""Tests for the Binance Vision OOS acquisition module.

Network functions (discovery, download) are not exercised here — only the pure
parsing and the manifest coverage filter, which are the parts that can break
silently.
"""
from __future__ import annotations

import io
import json
import zipfile

import polars as pl
import pytest

import liquidity_migration.binance_vision as bv
from liquidity_migration.binance_vision import (
    _assert_download_completeness,
    parse_month_csv,
    rewrite_manifest_to_coverage,
)
from liquidity_migration.storage import read_dataset, write_dataset

MS_PER_HOUR = 3_600_000


def test_assert_download_completeness_raises_above_tolerance(tmp_path):
    """M5: too many failed monthly downloads must abort the build (no silent
    survivorship-biased universe) and persist the failed-jobs artifact."""
    failed = [("AAAUSDT", "2023-01"), ("BBBUSDT", "2023-02")]
    artifact = tmp_path / "failed.json"
    with pytest.raises(RuntimeError, match="survivorship-biased"):
        _assert_download_completeness(
            failed, total_jobs=10, max_failure_ratio=0.005, artifact_path=artifact,
        )
    assert artifact.exists()
    recorded = {row["symbol"] for row in json.loads(artifact.read_text())}
    assert recorded == {"AAAUSDT", "BBBUSDT"}


def test_assert_download_completeness_passes_within_tolerance(tmp_path):
    """1 failure in 1000 (0.1%) is within the 0.5% tolerance — build proceeds."""
    artifact = tmp_path / "failed.json"
    _assert_download_completeness(
        [("AAAUSDT", "2023-01")], total_jobs=1000, max_failure_ratio=0.005, artifact_path=artifact,
    )
    # Artifact still written (empty-ish) for audit even when within tolerance.
    assert artifact.exists()


def _zip_csv(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAAUSDT-1h-2024-01.csv", text)
    return buf.getvalue()


def test_parse_month_csv_basic_row():
    csv = "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 1
    r = rows[0]
    assert r["ts_ms"] == 1609459200000
    assert r["symbol"] == "AAAUSDT"
    assert r["open"] == 100.0 and r["high"] == 110.0
    assert r["low"] == 90.0 and r["close"] == 105.0
    assert r["volume_base"] == 1000.0
    assert r["turnover_quote"] == 105000.0       # column 7, not 6
    assert r["source"] == "binance_vision_um_1h"


def test_parse_month_csv_skips_header_row():
    csv = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tbq,ignore\n"
        "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
    )
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 1                         # header dropped, data kept
    assert rows[0]["ts_ms"] == 1609459200000


def test_parse_month_csv_skips_malformed():
    csv = (
        "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
        "garbage,row,too,short\n"
        "1609462800000,105,108,104,106,900,1609466399999,95000,40,450,47500,0\n"
    )
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 2
    assert [r["ts_ms"] for r in rows] == [1609459200000, 1609462800000]


def _write_klines(root, symbol, date_ms, n_bars):
    rows = [{
        "ts_ms": date_ms + i * MS_PER_HOUR, "symbol": symbol,
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
    } for i in range(n_bars)]
    write_dataset(pl.DataFrame(rows), root, "klines_1h", partition_by=("date", "symbol"))


def test_rewrite_manifest_to_coverage_drops_thin_and_uncovered(tmp_path):
    root = tmp_path / "root"
    jan01 = 1704067200000   # 2024-01-01 00:00 UTC
    jan02 = jan01 + 24 * MS_PER_HOUR
    # AAA: a full day and a thin day; BBB: a full day
    _write_klines(root, "AAAUSDT", jan01, 24)     # covered
    _write_klines(root, "AAAUSDT", jan02, 10)     # too thin (<20 bars)
    _write_klines(root, "BBBUSDT", jan01, 24)     # covered

    # manifest also lists a symbol-day with no klines at all
    manifest = pl.DataFrame([
        {"symbol": "AAAUSDT", "date": "2024-01-01", "url": "x"},
        {"symbol": "AAAUSDT", "date": "2024-01-02", "url": "x"},
        {"symbol": "BBBUSDT", "date": "2024-01-01", "url": "x"},
        {"symbol": "CCCUSDT", "date": "2024-01-01", "url": "x"},
    ])
    write_dataset(manifest, root, "archive_trade_manifest", partition_by=("date",))

    surviving = rewrite_manifest_to_coverage(root)
    assert surviving == 2

    out = read_dataset(root, "archive_trade_manifest")
    pairs = {(r["symbol"], r["date"]) for r in out.iter_rows(named=True)}
    assert pairs == {("AAAUSDT", "2024-01-01"), ("BBBUSDT", "2024-01-01")}


def test_rewrite_manifest_to_coverage_synthesises_when_manifest_absent(tmp_path):
    root = tmp_path / "root"
    jan01 = 1704067200000
    _write_klines(root, "AAAUSDT", jan01, 24)
    # no archive_trade_manifest written at all
    surviving = rewrite_manifest_to_coverage(root)
    assert surviving == 1
    out = read_dataset(root, "archive_trade_manifest")
    assert out["symbol"].to_list() == ["AAAUSDT"]
    assert out["url"].to_list() == ["kline_coverage"]


# --------------------------------------------------------------------------
# audit2b: valid-but-empty month must NOT count as a download failure.
# --------------------------------------------------------------------------

def _patch_listing(monkeypatch, inventory):
    monkeypatch.setattr(bv, "discover", lambda **k: inventory)


def test_valid_empty_month_not_counted_as_failed_job(tmp_path, monkeypatch):
    """audit2b defect-1: one month parses to rows, one is a VALID header-only
    month (empty parse). With a 0% failure tolerance the build must still
    succeed — the empty-but-valid month is NOT a failed job.

    OLD code: fetch_month_klines returned [] for the empty month, the caller
    appended it to failed_jobs, ratio = 1/2 = 50% > 0% -> RuntimeError. NEW
    code: the empty month is success-with-no-rows and is not counted."""
    root = tmp_path / "root"
    jan01 = 1704067200000  # 2024-01-01 00:00 UTC

    _patch_listing(monkeypatch, {"AAAUSDT": ["2024-01", "2024-02"]})

    def _fake_fetch(symbol, ym):
        if ym == "2024-01":
            # A full, valid month: 24 hourly bars.
            return [{
                "ts_ms": jan01 + i * MS_PER_HOUR, "symbol": symbol,
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
            } for i in range(24)]
        # A valid month with no parseable bars (e.g. header-only CSV).
        return []

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)

    # Zero tolerance: a single miscounted failure would abort.
    summary = bv.build_binance_oos(root, end_date="2024-03-01", max_failure_ratio=0.0)

    assert summary["failed_files"] == 0          # empty-but-valid month not failed
    assert summary["symbols"] == 1
    klines = read_dataset(root, "klines_1h")
    assert klines.height == 24                    # exactly the valid month's rows


def test_real_download_failure_still_counted(tmp_path, monkeypatch):
    """audit2b defect-1: a genuine hard failure (None) is still a failed job and
    still trips the survivorship gate — the fix narrows what counts as failure,
    it does not stop counting real failures."""
    root = tmp_path / "root"
    jan01 = 1704067200000

    _patch_listing(monkeypatch, {"AAAUSDT": ["2024-01", "2024-02"]})

    def _fake_fetch(symbol, ym):
        if ym == "2024-01":
            return [{
                "ts_ms": jan01 + i * MS_PER_HOUR, "symbol": symbol,
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
            } for i in range(24)]
        return None  # hard download/integrity failure

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)

    with pytest.raises(RuntimeError, match="survivorship-biased"):
        bv.build_binance_oos(root, end_date="2024-03-01", max_failure_ratio=0.0)


def test_fetch_month_klines_returns_empty_list_on_valid_empty_zip(monkeypatch):
    """audit2b defect-1: a successful fetch of a header-only month returns an
    empty *list* (success), never None. A network failure returns None."""

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def getheader(self, _name):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    header_only = _zip_csv(
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tbq,ignore\n"
    )
    monkeypatch.setattr(bv.urllib.request, "urlopen", lambda *a, **k: _Resp(header_only))
    monkeypatch.setattr(bv, "_fetch_expected_sha256", lambda *a, **k: None)

    out = bv.fetch_month_klines("AAAUSDT", "2024-01")
    assert out == []          # valid-but-empty success, NOT None
    assert out is not None


def test_normal_input_unchanged_happy_path(tmp_path, monkeypatch):
    """audit2b: NORMAL (non-defective) input is numerically unchanged — a build
    where every month has rows produces exactly the same klines and zero failed
    files as before the fix (the happy path is untouched)."""
    root = tmp_path / "root"
    jan01 = 1704067200000

    _patch_listing(monkeypatch, {"AAAUSDT": ["2024-01"], "BBBUSDT": ["2024-01"]})

    def _fake_fetch(symbol, ym):
        return [{
            "ts_ms": jan01 + i * MS_PER_HOUR, "symbol": symbol,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume_base": 1.0, "turnover_quote": 1.0, "source": "test",
        } for i in range(24)]

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)
    summary = bv.build_binance_oos(root, end_date="2024-02-01", max_failure_ratio=0.0)

    assert summary["failed_files"] == 0
    assert summary["symbols"] == 2
    klines = read_dataset(root, "klines_1h")
    assert klines.height == 48
    assert set(klines["symbol"].unique().to_list()) == {"AAAUSDT", "BBBUSDT"}


# --------------------------------------------------------------------------
# audit2b: document the CURRENT _verify_download contract.
# --------------------------------------------------------------------------

def test_verify_download_no_checksum_no_length_requires_valid_zip():
    """audit2c (operator-approved) SUPERSEDES the earlier audit2b 'flagged-not-fixed'
    note: _verify_download now requires a valid non-empty zip when BOTH the .CHECKSUM
    sidecar and the Content-Length header are absent, instead of being a no-op."""
    # Both signals absent + a non-zip body -> now RAISES (was a silent no-op).
    with pytest.raises(ValueError):
        bv._verify_download(b"not-a-zip-but-no-signals", expected_sha256=None, content_length=None)
    # Both signals absent + a VALID zip -> still passes.
    bv._verify_download(_zip_csv("open_time,open\n1,2\n"), expected_sha256=None, content_length=None)
    # A present, matching Content-Length still passes; a wrong one still raises.
    raw = b"half-a-body"
    bv._verify_download(raw, expected_sha256=None, content_length=len(raw))
    with pytest.raises(ValueError, match="Content-Length mismatch"):
        bv._verify_download(raw, expected_sha256=None, content_length=len(raw) + 100)


# --------------------------------------------------------------------------
# audit2c (binance_floor): _verify_download both-absent fail-closed contract.
# --------------------------------------------------------------------------

def _valid_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAAUSDT-1h-2024-01.csv", "ts,open,high,low,close\n1,2,3,4,5\n")
    return buf.getvalue()


def test_non_zip_body_with_no_checksum_no_length_raises() -> None:
    """audit2c: a non-zip body with no .CHECKSUM and no Content-Length must now
    raise (the old both-absent path was a no-op that let garbage into the root)."""
    raw = b"truncated-garbage-not-a-zip"
    with pytest.raises(ValueError, match="unverifiable body rejected"):
        bv._verify_download(raw, expected_sha256=None, content_length=None)


def test_empty_body_with_no_checksum_no_length_raises() -> None:
    """audit2c: an empty body is also rejected on the both-absent path."""
    with pytest.raises(ValueError, match="unverifiable body rejected"):
        bv._verify_download(b"", expected_sha256=None, content_length=None)


def test_valid_zip_with_no_checksum_no_length_passes() -> None:
    """audit2c: a genuine, structurally-valid zip with neither integrity signal
    still passes — the fix only rejects unverifiable corruption, not real archives
    from older months that publish no sidecar/header."""
    bv._verify_download(_valid_zip_bytes(), expected_sha256=None, content_length=None)


def test_checksum_and_length_branches_unchanged() -> None:
    """audit2c guardrail: the sha256 and Content-Length branches are NOT weakened
    by the new fail-closed both-absent path."""
    import hashlib

    raw = _valid_zip_bytes()
    good_sha = hashlib.sha256(raw).hexdigest()
    # sha256 authoritative: matches -> pass even with a deliberately wrong length.
    bv._verify_download(raw, good_sha, content_length=1)
    # sha256 mismatch still raises.
    with pytest.raises(ValueError, match="sha256 mismatch"):
        bv._verify_download(raw, "0" * 64, content_length=len(raw))
    # Content-Length mismatch still raises (no checksum).
    with pytest.raises(ValueError, match="Content-Length mismatch"):
        bv._verify_download(raw, expected_sha256=None, content_length=len(raw) + 99)
    # Matching Content-Length passes without reaching the zip check.
    bv._verify_download(b"anything", expected_sha256=None, content_length=len(b"anything"))


# ==========================================================================
# Relocated from test_audit_fix_b14.py (archive-integrity-2, ingestion-3,
# ingestion-4 — 2026-06-14 audit bucket b14).
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
