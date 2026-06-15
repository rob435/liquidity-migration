"""audit2c regression: binance_floor.

Pins the corrected behaviour of ``binance_vision._verify_download`` when BOTH
integrity signals are absent. Previously the both-absent branch was a NO-OP, so
a truncated/garbage body passed the integrity gate and entered the PIT root. The
fix requires the raw body to at least be a non-empty, structurally-valid zip when
neither a ``.CHECKSUM`` sidecar nor a ``Content-Length`` header is available.

These tests FAIL on the old code (which returned silently for None,None) and
PASS on the fix.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from liquidity_migration import binance_vision as bv


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
