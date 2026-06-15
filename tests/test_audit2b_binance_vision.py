"""audit2b regression tests for liquidity_migration.binance_vision.

Defect 1 (FIXED): a valid-but-zero-parseable-row Vision month was miscounted as
a hard download failure. ``fetch_month_klines`` returned ``[]`` both on a hard
download/integrity failure AND on a valid month with no parseable bars, so
``build_binance_oos`` appended the valid-empty month to ``failed_jobs`` and it
counted against the survivorship-completeness gate (``max_failure_ratio``),
which could spuriously abort an otherwise-complete build. The fix makes
``fetch_month_klines`` return ``None`` only on a hard failure and a list (maybe
empty) on success, and the caller treats only ``None`` as a failed job.

Defect 2 (NOT fixed here — flagged): the Content-Length integrity floor gap.
See the module-level note below; a corresponding behavioral test would have to
live inside ``_verify_download``, but the safe variant is non-number-changing
defense-in-depth only and the meaningful variant contradicts a frozen existing
test, so it is left to the operator. This file documents the current contract.
"""
from __future__ import annotations

import io
import zipfile

import pytest

import liquidity_migration.binance_vision as bv
from liquidity_migration.storage import read_dataset

MS_PER_HOUR = 3_600_000


def _zip_csv(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAAUSDT-1h-2024-01.csv", text)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Defect 1: valid-but-empty month must NOT count as a download failure.
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
# Defect 2: document the CURRENT _verify_download contract (NOT changed here).
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
