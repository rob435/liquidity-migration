"""Tests for the Binance Vision OOS acquisition module: the pure parsing and the manifest
coverage filter, which are the parts that can break silently. Network discovery and
download are not exercised here.
"""

from __future__ import annotations

import io
import json
import zipfile
from urllib.error import HTTPError

import polars as pl
import pytest

import liquidity_migration.data.binance_vision as bv
from liquidity_migration.data.binance_vision import (
    _assert_download_completeness,
    parse_month_csv,
    rewrite_manifest_to_coverage,
    validate_pit_manifest_coverage,
)
from liquidity_migration.data.storage import read_dataset, write_dataset
from liquidity_migration.core.symbol_codec import (
    SymbolIdentityError,
    encode_symbol_partition,
    normalize_binance_usdm_symbols,
)

MS_PER_HOUR = 3_600_000


def test_assert_download_completeness_raises_above_tolerance(tmp_path):
    """Too many failed monthly downloads must abort the build, so no
    survivorship-biased universe is produced, and the failed-jobs artifact persists.
    """
    failed = [("AAAUSDT", "2023-01"), ("BBBUSDT", "2023-02")]
    artifact = tmp_path / "failed.json"
    with pytest.raises(RuntimeError, match="survivorship-biased"):
        _assert_download_completeness(
            failed,
            total_jobs=10,
            max_failure_ratio=0.005,
            artifact_path=artifact,
        )
    assert artifact.exists()
    recorded = {row["symbol"] for row in json.loads(artifact.read_text())}
    assert recorded == {"AAAUSDT", "BBBUSDT"}


def test_assert_download_completeness_passes_within_tolerance(tmp_path):
    """1 failure in 1000 (0.1%) is within the 0.5% tolerance — build proceeds."""
    artifact = tmp_path / "failed.json"
    _assert_download_completeness(
        [("AAAUSDT", "2023-01")],
        total_jobs=1000,
        max_failure_ratio=0.005,
        artifact_path=artifact,
    )
    # The artifact is still written (empty-ish) even when within tolerance.
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
    assert r["turnover_quote"] == 105000.0  # column 7, not 6
    assert r["source"] == "binance_vision_um_1h"


def test_parse_month_csv_skips_header_row():
    csv = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tbq,ignore\n"
        "1609459200000,100,110,90,105,1000,1609462799999,105000,50,500,52500,0\n"
    )
    rows = parse_month_csv("AAAUSDT", _zip_csv(csv))
    assert len(rows) == 1  # header dropped, data kept
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


def test_fetch_daily_klines_returns_empty_for_archive_404(monkeypatch) -> None:
    def _not_found(url, timeout=60):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(bv.urllib.request, "urlopen", _not_found)

    assert bv.fetch_daily_klines("AAAUSDT", "2024-01-01", retries=1) == []


def test_s3_listing_parser_retains_unicode_items_and_exact_pagination() -> None:
    prefix = bv.MONTHLY_KLINES_PREFIX
    symbol = "\u5e01\u5b89\u4eba\u751fUSDT"
    page = bv.parse_s3_listing_page(
        (
            "<ListBucketResult>"
            f"<CommonPrefixes><Prefix>{prefix}{symbol}/</Prefix></CommonPrefixes>"
            "<CommonPrefixes><Prefix>unrelated/value/</Prefix></CommonPrefixes>"
            "<IsTruncated>true</IsTruncated>"
            f"<NextMarker>{prefix}{symbol}/&amp;cursor</NextMarker>"
            "</ListBucketResult>"
        ).encode(),
        prefix=prefix,
        listing_kind="common_prefixes",
    )

    assert page.items == (symbol,)
    assert page.is_truncated is True
    assert page.next_marker == f"{prefix}{symbol}/&cursor"


def test_s3_listing_walk_advances_across_truncated_empty_match_page(monkeypatch) -> None:
    prefix = bv.DAILY_KLINES_PREFIX
    payloads = [
        (
            "<ListBucketResult><IsTruncated>true</IsTruncated>"
            "<NextMarker>advance-even-without-match</NextMarker></ListBucketResult>"
        ).encode(),
        (
            f"<ListBucketResult><CommonPrefixes><Prefix>{prefix}BTCUSDT/</Prefix>"
            "</CommonPrefixes><IsTruncated>false</IsTruncated></ListBucketResult>"
        ).encode(),
    ]
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return payloads[len(urls) - 1]

    monkeypatch.setattr(bv, "_fetch_s3_listing_bytes", fetch)

    assert bv._s3_common_prefixes(prefix) == ["BTCUSDT"]
    assert len(urls) == 2
    assert "marker=advance-even-without-match" in urls[1]


def test_s3_listing_walk_rejects_truncated_page_that_cannot_advance(monkeypatch) -> None:
    monkeypatch.setattr(
        bv,
        "_fetch_s3_listing_bytes",
        lambda _url: b"<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>",
    )

    with pytest.raises(RuntimeError, match="no continuation marker"):
        bv._s3_keys(bv.MONTHLY_KLINES_PREFIX)


def test_list_usdm_usdt_daily_symbols_retains_canonical_unicode(monkeypatch) -> None:
    unicode_symbol = "\u5e01\u5b89\u4eba\u751fUSDT"
    monkeypatch.setattr(
        bv,
        "_s3_common_prefixes",
        lambda prefix: ["BTCUSDT", "1000PEPEUSDT", unicode_symbol, "ETHUSDC", "badusdt"],
    )

    assert bv.list_usdm_usdt_daily_symbols() == [
        "1000PEPEUSDT",
        "BTCUSDT",
        unicode_symbol,
    ]


def test_fetch_daily_klines_percent_encodes_unicode_symbol(monkeypatch) -> None:
    symbol = "\u5e01\u5b89\u4eba\u751fUSDT"
    observed: list[str] = []

    def _not_found(url, timeout=60):
        observed.append(url)
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(bv.urllib.request, "urlopen", _not_found)

    assert bv.fetch_daily_klines(symbol, "2026-07-10", retries=1) == []
    assert observed and symbol not in observed[0]
    assert "%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT" in observed[0]


@pytest.mark.parametrize(
    "unsafe",
    [
        "../BTCUSDT",
        "BTC/USDT",
        "BTC\\USDT",
        "BTC\x00USDT",
        "BTC USDT",
        "\uff22\uff34\uff23USDT",
        "%2FUSDT",
        ".USDT",
    ],
)
def test_symbol_codec_rejects_unsafe_or_confusable_identifiers(unsafe: str) -> None:
    with pytest.raises(SymbolIdentityError):
        normalize_binance_usdm_symbols(
            [unsafe],
            source="adversarial test",
            ignore_non_usdt_entries=False,
        )


def _write_klines(root, symbol, date_ms, n_bars):
    rows = [
        {
            "ts_ms": date_ms + i * MS_PER_HOUR,
            "symbol": symbol,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume_base": 1.0,
            "turnover_quote": 1.0,
            "source": "test",
        }
        for i in range(n_bars)
    ]
    write_dataset(pl.DataFrame(rows), root, "klines_1h", partition_by=("date", "symbol"))


def test_rewrite_manifest_to_coverage_drops_thin_and_uncovered(tmp_path):
    root = tmp_path / "root"
    jan01 = 1704067200000  # 2024-01-01 00:00 UTC
    jan02 = jan01 + 24 * MS_PER_HOUR
    # AAA: a full day and a thin day; BBB: a full day
    _write_klines(root, "AAAUSDT", jan01, 24)  # covered
    _write_klines(root, "AAAUSDT", jan02, 10)  # too thin (<20 bars)
    _write_klines(root, "BBBUSDT", jan01, 24)  # covered

    # manifest also lists a symbol-day with no klines at all
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2024-01-01", "url": "x"},
            {"symbol": "AAAUSDT", "date": "2024-01-02", "url": "x"},
            {"symbol": "BBBUSDT", "date": "2024-01-01", "url": "x"},
            {"symbol": "CCCUSDT", "date": "2024-01-01", "url": "x"},
        ]
    )
    write_dataset(manifest, root, "archive_trade_manifest", partition_by=("date",))

    surviving = rewrite_manifest_to_coverage(root)
    assert surviving == 2

    out = read_dataset(root, "archive_trade_manifest")
    pairs = {(r["symbol"], r["date"]) for r in out.iter_rows(named=True)}
    assert pairs == {("AAAUSDT", "2024-01-01"), ("BBBUSDT", "2024-01-01")}


def test_validate_manifest_rejects_missing_current_listing_tail_without_rewrite(
    tmp_path,
):
    root = tmp_path / "root"
    jan01 = 1704067200000
    jan02 = jan01 + 24 * MS_PER_HOUR
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "AAAUSDT", jan02, 24)
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2024-01-01",
                "url": "archive-1",
                "source": "bybit_public_trading_archive",
            },
            {
                "symbol": "AAAUSDT",
                "date": "2024-01-02",
                "url": "archive-2",
                "source": "bybit_public_trading_archive",
            },
            {
                "symbol": "AAAUSDT",
                "date": "2024-01-03",
                "url": "bybit_v5_listing",
                "source": "bybit_v5_listing",
            },
        ]
    )
    write_dataset(
        manifest,
        root,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    with pytest.raises(RuntimeError, match="2024-01-03/AAAUSDT"):
        validate_pit_manifest_coverage(root)

    persisted = read_dataset(root, "archive_trade_manifest").sort(
        ["date", "symbol", "url"]
    )
    assert persisted.to_dicts() == manifest.sort(["date", "symbol", "url"]).to_dicts()


def test_validate_manifest_preserves_archive_only_phantom_boundaries(tmp_path):
    root = tmp_path / "root"
    jan02 = 1704153600000
    jan03 = jan02 + 24 * MS_PER_HOUR
    _write_klines(root, "AAAUSDT", jan02, 24)
    _write_klines(root, "AAAUSDT", jan03, 24)
    manifest = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"] * 4,
            "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
            "url": ["pre", "live-1", "live-2", "post"],
            "source": ["bybit_public_trading_archive"] * 4,
        }
    )
    write_dataset(
        manifest,
        root,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    summary = validate_pit_manifest_coverage(root)

    assert summary["full_pit_universe_pass"] is True
    assert summary["required_date_symbols"] == 2
    assert summary["missing_required_date_symbols"] == 0
    assert read_dataset(root, "archive_trade_manifest").height == 4


def test_coverage_rewrite_refuses_independent_bybit_membership(tmp_path):
    root = tmp_path / "root"
    jan01 = 1704067200000
    _write_klines(root, "AAAUSDT", jan01, 24)
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2024-01-01",
                "url": "https://public.bybit.com/trading/AAAUSDT/day.csv.gz",
                "source": "bybit_public_trading_archive",
            }
        ]
    )
    write_dataset(
        manifest,
        root,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    with pytest.raises(RuntimeError, match="refusing to rewrite independently sourced"):
        rewrite_manifest_to_coverage(root)

    assert read_dataset(root, "archive_trade_manifest").to_dicts() == manifest.to_dicts()


def test_rewrite_manifest_to_coverage_extends_stale_manifest_tail(tmp_path):
    """A narrow REST/current-month top-up can add covered kline days after the
    persisted manifest's old end; the coverage rewrite must synthesize those rows
    instead of clipping at the stale boundary.
    """
    root = tmp_path / "root"
    jan01 = 1704067200000
    jan02 = jan01 + 24 * MS_PER_HOUR
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "AAAUSDT", jan02, 24)

    write_dataset(
        pl.DataFrame([{"symbol": "AAAUSDT", "date": "2024-01-01", "url": "existing"}]),
        root,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    surviving = rewrite_manifest_to_coverage(root)

    assert surviving == 2
    out = read_dataset(root, "archive_trade_manifest").sort(["date", "symbol"])
    assert out.select(["date", "symbol", "url"]).to_dicts() == [
        {"date": "2024-01-01", "symbol": "AAAUSDT", "url": "existing"},
        {"date": "2024-01-02", "symbol": "AAAUSDT", "url": "kline_coverage"},
    ]


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


def test_topup_binance_daily_klines_appends_and_extends_manifest(tmp_path, monkeypatch):
    root = tmp_path / "root"
    jan01 = 1704067200000
    jan02 = jan01 + 24 * MS_PER_HOUR
    _write_klines(root, "AAAUSDT", jan01, 24)
    write_dataset(
        pl.DataFrame([{"symbol": "AAAUSDT", "date": "2024-01-01", "url": "existing"}]),
        root,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    monkeypatch.setattr(bv, "list_usdm_usdt_daily_symbols", lambda: ["AAAUSDT"])

    def _fake_fetch(symbol, day):
        assert symbol == "AAAUSDT"
        assert day == "2024-01-02"
        return [
            {
                "ts_ms": jan02 + i * MS_PER_HOUR,
                "symbol": symbol,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "test_daily",
            }
            for i in range(24)
        ]

    monkeypatch.setattr(bv, "fetch_daily_klines", _fake_fetch)

    summary = bv.topup_binance_daily_klines(
        root,
        start="2024-01-02",
        end="2024-01-03",
        workers=1,
    )

    assert summary["kline_rows"] == 24
    assert summary["manifest_rows"] == 2
    manifest = read_dataset(root, "archive_trade_manifest").sort(["date", "symbol"])
    assert manifest.select(["date", "symbol", "url"]).to_dicts() == [
        {"date": "2024-01-01", "symbol": "AAAUSDT", "url": "existing"},
        {
            "date": "2024-01-02",
            "symbol": "AAAUSDT",
            "url": "binance_vision_archive",
        },
    ]
    assert manifest["source"].unique().to_list() == ["binance_vision_archive"]
    assert manifest["membership_source"].unique().to_list() == ["binance_vision_archive"]
    assert manifest["membership_inferred"].unique().to_list() == [False]
    assert manifest["first_archive_observed_date"].unique().to_list() == ["2024-01-01"]
    assert summary["missing_files"] == 0
    assert json.loads((root / "binance_vision_daily_missing_jobs.json").read_text(encoding="utf-8")) == []


# --------------------------------------------------------------------------
# A valid-but-empty month must NOT count as a download failure.
# --------------------------------------------------------------------------


def _patch_listing(monkeypatch, inventory):
    monkeypatch.setattr(bv, "discover", lambda **k: inventory)


def test_valid_empty_month_not_counted_as_failed_job(tmp_path, monkeypatch):
    """A valid header-only month parses to no rows and is success-with-no-rows, not a
    failed job -- otherwise a 0% failure tolerance trips on it.
    """
    root = tmp_path / "root"
    jan01 = 1704067200000  # 2024-01-01 00:00 UTC

    _patch_listing(monkeypatch, {"AAAUSDT": ["2024-01", "2024-02"]})

    def _fake_fetch(symbol, ym):
        if ym == "2024-01":
            # A full, valid month: 24 hourly bars.
            return [
                {
                    "ts_ms": jan01 + i * MS_PER_HOUR,
                    "symbol": symbol,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume_base": 1.0,
                    "turnover_quote": 1.0,
                    "source": "test",
                }
                for i in range(24)
            ]
        # A valid month with no parseable bars (e.g. header-only CSV).
        return []

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)

    # Zero tolerance: a single miscounted failure would abort.
    summary = bv.build_binance_oos(root, end_date="2024-03-01", max_failure_ratio=0.0)

    assert summary["failed_files"] == 0  # empty-but-valid month not failed
    assert summary["symbols"] == 1
    klines = read_dataset(root, "klines_1h")
    assert klines.height == 24  # exactly the valid month's rows


def test_real_download_failure_still_counted(tmp_path, monkeypatch):
    """A genuine hard failure (None) is still a failed job and still trips the survivorship gate."""
    root = tmp_path / "root"
    jan01 = 1704067200000

    _patch_listing(monkeypatch, {"AAAUSDT": ["2024-01", "2024-02"]})

    def _fake_fetch(symbol, ym):
        if ym == "2024-01":
            return [
                {
                    "ts_ms": jan01 + i * MS_PER_HOUR,
                    "symbol": symbol,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume_base": 1.0,
                    "turnover_quote": 1.0,
                    "source": "test",
                }
                for i in range(24)
            ]
        return None  # hard download/integrity failure

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)

    with pytest.raises(RuntimeError, match="survivorship-biased"):
        bv.build_binance_oos(root, end_date="2024-03-01", max_failure_ratio=0.0)


def test_fetch_month_klines_returns_empty_list_on_valid_empty_zip(monkeypatch):
    """A successful fetch of a header-only month returns an empty *list* (success); a network failure returns None."""

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

    header_only = _zip_csv("open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tbq,ignore\n")
    monkeypatch.setattr(bv.urllib.request, "urlopen", lambda *a, **k: _Resp(header_only))
    monkeypatch.setattr(bv, "_fetch_expected_sha256", lambda *a, **k: None)

    out = bv.fetch_month_klines("AAAUSDT", "2024-01")
    assert out == []  # valid-but-empty success, NOT None
    assert out is not None


def test_normal_input_unchanged_happy_path(tmp_path, monkeypatch):
    """A build where every month has rows produces exactly the same klines and zero
    failed files -- the happy path is untouched.
    """
    root = tmp_path / "root"
    jan01 = 1704067200000

    _patch_listing(monkeypatch, {"AAAUSDT": ["2024-01"], "BBBUSDT": ["2024-01"]})

    def _fake_fetch(symbol, ym):
        return [
            {
                "ts_ms": jan01 + i * MS_PER_HOUR,
                "symbol": symbol,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "test",
            }
            for i in range(24)
        ]

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)
    summary = bv.build_binance_oos(root, end_date="2024-02-01", max_failure_ratio=0.0)

    assert summary["failed_files"] == 0
    assert summary["symbols"] == 2
    klines = read_dataset(root, "klines_1h")
    assert klines.height == 48
    assert set(klines["symbol"].unique().to_list()) == {"AAAUSDT", "BBBUSDT"}
    manifest = read_dataset(root, "archive_trade_manifest")
    assert manifest["source"].unique().to_list() == ["binance_vision_archive"]
    assert manifest["membership_source"].unique().to_list() == ["binance_vision_archive"]
    assert manifest["membership_inferred"].unique().to_list() == [False]


# --------------------------------------------------------------------------
# The _verify_download contract.
# --------------------------------------------------------------------------


def test_verify_download_no_checksum_no_length_requires_valid_zip():
    """``_verify_download`` requires a valid non-empty zip when BOTH the .CHECKSUM
    sidecar and the Content-Length header are absent.
    """
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
# _verify_download both-absent fail-closed contract.
# --------------------------------------------------------------------------


def _valid_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAAUSDT-1h-2024-01.csv", "ts,open,high,low,close\n1,2,3,4,5\n")
    return buf.getvalue()


def test_non_zip_body_with_no_checksum_no_length_raises() -> None:
    """A non-zip body with no .CHECKSUM and no Content-Length must raise rather than let garbage into the root."""
    raw = b"truncated-garbage-not-a-zip"
    with pytest.raises(ValueError, match="unverifiable body rejected"):
        bv._verify_download(raw, expected_sha256=None, content_length=None)


def test_empty_body_with_no_checksum_no_length_raises() -> None:
    """An empty body is also rejected on the both-absent path."""
    with pytest.raises(ValueError, match="unverifiable body rejected"):
        bv._verify_download(b"", expected_sha256=None, content_length=None)


def test_valid_zip_with_no_checksum_no_length_passes() -> None:
    """A structurally-valid zip with neither integrity signal still passes: only
    unverifiable corruption is rejected, not real archives from older months that
    publish no sidecar or header.
    """
    bv._verify_download(_valid_zip_bytes(), expected_sha256=None, content_length=None)


def test_checksum_and_length_branches_unchanged() -> None:
    """The sha256 and Content-Length branches are not weakened by the fail-closed both-absent path."""
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




def test_verify_download_raises_on_sha256_mismatch() -> None:
    """A body whose sha256 disagrees with the published CHECKSUM must raise, so a
    corrupt-but-parseable archive never silently enters the PIT root.
    """
    raw = b"corrupt-but-parseable-zip-bytes"
    wrong_sha = "0" * 64
    with pytest.raises(ValueError, match="sha256 mismatch"):
        bv._verify_download(raw, wrong_sha, content_length=len(raw))


def test_verify_download_passes_on_sha256_match() -> None:
    """A body matching the published sha256 passes."""
    import hashlib

    raw = b"the-real-archive-bytes"
    good_sha = hashlib.sha256(raw).hexdigest()
    # Content-Length deliberately wrong: when a checksum is present it is
    # authoritative and the length check is skipped.
    bv._verify_download(raw, good_sha, content_length=999)


def test_verify_download_falls_back_to_content_length_when_no_checksum() -> None:
    """When the .CHECKSUM sidecar is absent (older months), a truncated body is still
    caught via the advertised Content-Length.
    """
    raw = b"only-half-here"
    with pytest.raises(ValueError, match="Content-Length mismatch"):
        bv._verify_download(raw, expected_sha256=None, content_length=len(raw) + 100)
    # Matching length passes.
    bv._verify_download(raw, expected_sha256=None, content_length=len(raw))
    # With NEITHER checksum NOR Content-Length, a non-zip body is rejected as
    # unverifiable corruption.
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
    """The CHECKSUM sidecar's leading sha256 hex token is parsed (Binance Vision format: '<hex> <filename>')."""
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
    """A single transient per-symbol S3 listing failure is accumulated and ratio-gated,
    not fatal: discover returns the surviving inventory.
    """
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
    """Too many transient listing failures still abort rather than silently produce an under-enumerated universe."""
    symbols = [f"S{i:02d}USDT" for i in range(10)]
    monkeypatch.setattr(bv, "list_usdm_usdt_symbols", lambda: symbols)

    def _fake_list(symbol, *, max_month):
        raise OSError("S3 down")  # every listing fails

    monkeypatch.setattr(bv, "list_symbol_months", _fake_list)
    with pytest.raises(RuntimeError, match="survivorship-biased"):
        bv.discover(max_month="2024-01", workers=4, max_listing_failure_ratio=0.005)


def test_persisted_kline_symbols_reads_partition_dirs(tmp_path) -> None:
    """The persisted-universe probe enumerates on-disk symbols from the klines_1h partition directories."""
    root = tmp_path / "root"
    jan01 = 1704067200000  # 2024-01-01 00:00 UTC
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "BBBUSDT", jan01, 24)
    assert bv._persisted_kline_symbols(root) == {"AAAUSDT", "BBBUSDT"}
    # No prior build -> empty set, not a crash.
    assert bv._persisted_kline_symbols(tmp_path / "absent") == set()


def test_persisted_kline_symbols_decodes_unicode_partition(tmp_path) -> None:
    root = tmp_path / "root"
    symbol = "\u5e01\u5b89\u4eba\u751fUSDT"
    _write_klines(root, symbol, 1704067200000, 24)

    encoded = encode_symbol_partition(symbol)
    assert (root / "klines_1h" / "date=2024-01-01" / f"symbol={encoded}").is_dir()
    assert bv._persisted_kline_symbols(root) == {symbol}


def test_build_rejects_unsafe_discovered_symbol_before_fetch_or_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    fetch_called = False
    monkeypatch.setattr(bv, "discover", lambda **_kwargs: {"../AAAUSDT": ["2024-01"]})

    def _forbidden_fetch(_symbol, _month):
        nonlocal fetch_called
        fetch_called = True
        raise AssertionError("archive fetch must not run")

    monkeypatch.setattr(bv, "fetch_month_klines", _forbidden_fetch)

    with pytest.raises(RuntimeError, match="unsupported/ambiguous Binance symbol"):
        bv.build_binance_oos(root, end_date="2024-02-01", workers=1)

    assert fetch_called is False
    assert not root.exists()


def test_build_binance_oos_refuses_universe_shrink_without_override(tmp_path, monkeypatch) -> None:
    """A rerun that discovers FEWER symbols than the persisted klines_1h must refuse --
    it would strand the dropped symbols' partitions on disk, which
    ``rewrite_manifest_to_coverage`` then silently retains.
    """
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


def test_build_stages_daily_only_symbols_atomically_with_monthly_history(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    dec01 = 1701388800000
    jan01 = 1704067200000
    jan02 = jan01 + 24 * MS_PER_HOUR
    _write_klines(root, "BBBUSDT", jan02, 24)

    monkeypatch.setattr(bv, "discover", lambda **_kwargs: {"AAAUSDT": ["2023-12"]})
    monkeypatch.setattr(
        bv,
        "list_usdm_usdt_daily_symbols",
        lambda: ["AAAUSDT", "BBBUSDT"],
    )

    def _rows(symbol: str, start_ms: int, source: str) -> list[dict[str, object]]:
        return [
            {
                "ts_ms": start_ms + i * MS_PER_HOUR,
                "symbol": symbol,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": source,
            }
            for i in range(24)
        ]

    monkeypatch.setattr(
        bv,
        "fetch_month_klines",
        lambda symbol, month: _rows(symbol, dec01, f"monthly:{month}"),
    )

    def _daily(symbol: str, day: str):
        if symbol == "BBBUSDT" and day == "2024-01-01":
            return []
        return _rows(
            symbol,
            jan01 if day == "2024-01-01" else jan02,
            f"daily:{day}",
        )

    monkeypatch.setattr(bv, "fetch_daily_klines", _daily)

    summary = bv.build_binance_oos(
        root,
        end_date="2024-01-03",
        daily_start="2024-01-01",
        workers=2,
        job_batch_size=2,
        max_failure_ratio=0.0,
    )

    klines = read_dataset(root, "klines_1h")
    assert set(klines["symbol"].unique().to_list()) == {"AAAUSDT", "BBBUSDT"}
    assert summary["daily_jobs"] == 4
    assert summary["daily_rows"] == 72
    assert summary["daily_missing_files"] == 1
    assert summary["daily_failed_files"] == 0
    assert (root / "archive_trade_manifest" / "date=2024-01-02").is_dir()


def test_daily_tail_failure_does_not_publish_partial_canonical_generation(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    dec01 = 1701388800000
    jan02 = 1704067200000 + 24 * MS_PER_HOUR
    _write_klines(root, "BBBUSDT", jan02, 24)
    monkeypatch.setattr(bv, "discover", lambda **_kwargs: {"AAAUSDT": ["2023-12"]})
    monkeypatch.setattr(bv, "list_usdm_usdt_daily_symbols", lambda: ["BBBUSDT"])
    monkeypatch.setattr(
        bv,
        "fetch_month_klines",
        lambda symbol, _month: [
            {
                "ts_ms": dec01 + i * MS_PER_HOUR,
                "symbol": symbol,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "monthly",
            }
            for i in range(24)
        ],
    )
    monkeypatch.setattr(bv, "fetch_daily_klines", lambda _symbol, _day: None)

    with pytest.raises(RuntimeError, match="incomplete"):
        bv.build_binance_oos(
            root,
            end_date="2024-01-03",
            daily_start="2024-01-01",
            workers=1,
            job_batch_size=2,
            max_failure_ratio=0.0,
        )

    assert bv._persisted_kline_symbols(root) == {"BBBUSDT"}
    assert not any(root.parent.glob(f".{root.name}.binance-oos-staging-*"))


def test_daily_inventory_cannot_mask_missing_older_monthly_history(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    dec01 = 1701388800000
    _write_klines(root, "BBBUSDT", dec01, 24)
    monkeypatch.setattr(bv, "discover", lambda **_kwargs: {"AAAUSDT": ["2023-12"]})
    monkeypatch.setattr(bv, "list_usdm_usdt_daily_symbols", lambda: ["BBBUSDT"])

    with pytest.raises(RuntimeError, match="daily inventory cannot replace missing monthly history"):
        bv.build_binance_oos(
            root,
            end_date="2024-01-03",
            daily_start="2024-01-01",
            workers=1,
        )

    assert bv._persisted_kline_symbols(root) == {"BBBUSDT"}


def test_build_binance_oos_clean_rewrite_drops_stale_partitions(tmp_path, monkeypatch) -> None:
    """With ``allow_degraded`` the narrower rerun overwrites klines_1h cleanly: the
    dropped symbol's stale partition must not survive.
    """
    root = tmp_path / "root"
    jan01 = 1704067200000
    _write_klines(root, "AAAUSDT", jan01, 24)
    _write_klines(root, "BBBUSDT", jan01, 24)

    monkeypatch.setattr(bv, "discover", lambda **k: {"AAAUSDT": ["2024-01"]})

    def _fake_fetch(symbol, ym):
        return [
            {
                "ts_ms": jan01 + i * MS_PER_HOUR,
                "symbol": symbol,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "test",
            }
            for i in range(24)
        ]

    monkeypatch.setattr(bv, "fetch_month_klines", _fake_fetch)
    bv.build_binance_oos(root, end_date="2024-02-01", allow_degraded=True)

    # Stale BBB partition is gone; only the freshly-built AAA universe remains.
    klines = read_dataset(root, "klines_1h")
    assert set(klines["symbol"].unique().to_list()) == {"AAAUSDT"}
    assert not (root / "klines_1h" / "date=2024-01-01" / "symbol=BBBUSDT").exists()


def test_monthly_job_rejects_cross_month_or_cross_symbol_rows() -> None:
    row = {
        "ts_ms": 1704067200000,
        "symbol": "AAAUSDT",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume_base": 1.0,
        "turnover_quote": 1.0,
        "source": "test",
    }
    with pytest.raises(RuntimeError, match="another symbol"):
        bv._normalize_monthly_job_frame(
            [{**row, "symbol": "BBBUSDT"}],
            symbol="AAAUSDT",
            month="2024-01",
            end_ms=1800000000000,
        )
    with pytest.raises(RuntimeError, match="out-of-month"):
        bv._normalize_monthly_job_frame(
            [row],
            symbol="AAAUSDT",
            month="2024-02",
            end_ms=1,
        )


def test_build_refuses_incomplete_publication_before_discovery(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / bv._INCOMPLETE_PUBLICATION_MARKER).write_text("{}", encoding="utf-8")

    def _forbidden_discovery(**_kwargs):
        raise AssertionError("discovery must not run while publication state is unresolved")

    monkeypatch.setattr(bv, "discover", _forbidden_discovery)
    with pytest.raises(RuntimeError, match="incomplete publication marker"):
        bv.build_binance_oos(root, end_date="2024-02-01", workers=1)


def test_transactional_publish_restores_both_prior_datasets_on_mid_swap_failure(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    staging = tmp_path / "staging"
    backup = tmp_path / "backup"
    root.mkdir()
    staging.mkdir()
    for dataset in bv._TRANSACTIONAL_DATASETS:
        (root / dataset).mkdir()
        (root / dataset / "generation.txt").write_text("old", encoding="utf-8")
        (staging / dataset).mkdir()
        (staging / dataset / "generation.txt").write_text("new", encoding="utf-8")

    marker = root / bv._INCOMPLETE_PUBLICATION_MARKER

    def _test_marker(path, _payload):
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(bv, "_write_exclusive_json", _test_marker)
    monkeypatch.setattr(bv, "_fsync_directory", lambda _path: None)
    original_rename = bv._rename_publication_tree
    moves = 0

    def _fail_fourth_move(source, destination):
        nonlocal moves
        moves += 1
        if moves == 4:
            raise OSError("injected second-dataset publication failure")
        original_rename(source, destination)

    monkeypatch.setattr(bv, "_rename_publication_tree", _fail_fourth_move)
    with pytest.raises(OSError, match="injected"):
        bv._publish_staged_binance_datasets(
            root,
            staging,
            backup,
            marker_path=marker,
            after_publish=lambda: None,
        )

    assert not marker.exists()
    for dataset in bv._TRANSACTIONAL_DATASETS:
        assert (root / dataset / "generation.txt").read_text(encoding="utf-8") == "old"
