"""Cross-file integration regression tests — bucket iE.

archive-integrity-4 (fresh-download integrity gate):
``download_public_trade_archive`` validates a *cache hit* with
``_archive_cache_is_complete`` before re-serving it, but the just-downloaded
temp body was never gated before being promoted via ``temp_output.replace(output)``.
A truncated/corrupt ``.gz`` that arrives WITHOUT a matching Content-Length header
(the curl backend, or a server omitting the header — both bypass the
``download_archive_bytes`` Content-Length guard) would therefore be promoted into
the canonical full-PIT name as a silently-thin day. ``docs/backtesting_errors_we_never_repeat.md``
treats incomplete data as a correctness bug, so the byte-ingestion layer needs an
executable guard pinning "a short/corrupt download must NOT be written as a complete
partition." These tests lock that in.

The lever is ``_download_archive_to_path`` (the writer that fills the temp file,
backend-agnostic): monkeypatching it to write a truncated gzip reproduces exactly
the "no Content-Length" corruption that slips past the download-time guard, without
any network or backend (curl/urllib) dependence.
"""

from __future__ import annotations

import gzip

import pytest

from liquidity_migration import archive as archive_module
from liquidity_migration.archive import (
    ArchiveDownloadIncompleteError,
    download_public_trade_archive,
)

_CSV_HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
)


def _full_gzip_body() -> bytes:
    """A complete, drainable gzip body (passes _archive_cache_is_complete)."""
    payload = _CSV_HEADER.encode("utf-8") + (
        b"1700000000.0,BTCUSDT,Buy,0.1,50000,ZeroMinusTick,abc,5000,0.1,5000\n" * 4000
    )
    return gzip.compress(payload)


def _truncated_gzip_body() -> bytes:
    """A gzip body cut mid-stream — fails to fully decompress, so the drain-to-end
    integrity check rejects it. This is what a clean mid-stream socket close (with no
    Content-Length to catch the short read) leaves behind."""
    full = _full_gzip_body()
    truncated = full[: len(full) // 2]
    assert len(truncated) < len(full)
    return truncated


def test_fresh_truncated_gz_download_is_rejected_not_promoted(tmp_path, monkeypatch) -> None:
    """archive-integrity-4: a truncated fresh download must NOT be promoted to the
    canonical name. With retries exhausted the loop raises (transient failure), and
    crucially the canonical output partition is never written thin."""
    destination = tmp_path / "BTCUSDT2025-01-23.csv.gz"

    calls = {"n": 0}

    def fake_download(_url, output, *, timeout_seconds):
        # Mock the writer itself (backend-agnostic, no network): a header-less
        # truncated body lands in the temp file exactly as a mid-stream socket close
        # would leave it — bypassing the download-time Content-Length guard.
        calls["n"] += 1
        output.write_bytes(_truncated_gzip_body())

    monkeypatch.setattr(archive_module, "_download_archive_to_path", fake_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError):  # retries exhausted -> RuntimeError wrapping the incomplete error
        download_public_trade_archive(
            "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-23.csv.gz",
            destination,
            retries=2,
        )

    assert calls["n"] == 2, f"a corrupt fresh download must be re-fetched (transient), got {calls['n']} attempts"
    assert not destination.exists(), "a truncated body must NOT be promoted to the canonical full-PIT name"
    assert not list(tmp_path.glob("*.tmp")), "the corrupt temp file must be cleaned up"


def test_fresh_download_integrity_failure_recovers_on_next_attempt(tmp_path, monkeypatch) -> None:
    """The integrity failure is TRANSIENT: a corrupt body on attempt 1 followed by a
    complete body on attempt 2 must promote the good body — same recovery a corrupt
    *cache* gets, just at the fresh-download boundary."""
    destination = tmp_path / "BTCUSDT2025-01-24.csv.gz"
    full = _full_gzip_body()
    attempts = 0

    def flaky_download(_url, output, *, timeout_seconds):
        nonlocal attempts
        attempts += 1
        output.write_bytes(_truncated_gzip_body() if attempts == 1 else full)

    monkeypatch.setattr(archive_module, "_download_archive_to_path", flaky_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    output = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-24.csv.gz",
        destination,
        retries=3,
    )

    assert output == destination
    assert attempts == 2, f"first (corrupt) attempt must be retried; got {attempts}"
    assert destination.read_bytes() == full, "the complete body from the retry must be the promoted partition"
    assert not list(tmp_path.glob("*.tmp"))


def test_complete_fresh_download_still_promotes(tmp_path, monkeypatch) -> None:
    """Guard against over-rejection: a valid fresh .gz must still be promoted on the
    first attempt (the integrity gate is not a blanket reject)."""
    destination = tmp_path / "BTCUSDT2025-01-25.csv.gz"
    full = _full_gzip_body()
    calls = {"n": 0}

    def fake_download(_url, *, timeout_seconds):
        calls["n"] += 1
        return full

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)

    output = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-25.csv.gz",
        destination,
        retries=2,
    )

    assert output == destination
    assert calls["n"] == 1, "a complete fresh download must promote on the first attempt"
    assert destination.read_bytes() == full
    assert not list(tmp_path.glob("*.tmp"))


def test_incomplete_error_is_transient_not_file_not_found() -> None:
    """The fresh-download integrity failure must be a TRANSIENT error (retried), not
    a permanent ArchiveFileNotFoundError (404, skipped). Pin the type hierarchy the
    retry loop depends on."""
    assert issubclass(ArchiveDownloadIncompleteError, RuntimeError)
    assert not issubclass(ArchiveDownloadIncompleteError, archive_module.ArchiveFileNotFoundError)
