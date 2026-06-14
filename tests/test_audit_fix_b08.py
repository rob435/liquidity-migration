"""Regression tests for audit bucket b08.

Each test pins the ROOT-CAUSE fix for one audit finding and would FAIL against the
pre-fix code. Findings covered:

- cross-sleeve-1  VALID_SLEEVES omitted 'continuous_addon' -> equal-split mis-allocated and
                  the addon sleeve was never clamped.
- cross-sleeve-5  empty-dict budget {} round-tripped to None (write/read asymmetry).
- cross-sleeve-6  partition_claimable took the control-row lock once PER candidate; the batch
                  claim resolves all candidates under ONE lock+read+write.
- archive-integrity-1  downloaded bytes were never size-verified -> a truncated body was
                       promoted into the canonical full-PIT root as a silently-thin day.
- archive-integrity-3  the st_size>0 cache guard re-served a partial/corrupt cached archive
                       forever; the cache hit is now structurally validated.
- code-quality-8  the 1h-kline fast-path fallbacks swallowed the exception silently; they now
                  log at WARNING before falling back.
- telegram-alert-3  the 429 retry helper raised AttributeError when HTTPError.headers is None.
- telegram-alert-4  the 429 rate-limit retry branch had zero test coverage.
- w4-w5-stages-1  the W5 Stage 0 W4-overlap falsifier passed VACUOUSLY when the W4 control
                  artifacts were absent.

cross-sleeve-2 (closed_trade_ids GC unwired in ws_risk) and cross-sleeve-4 (seed_margin_budget
has no production caller) require changes outside this bucket's owned files (ws_risk / a new
CLI subcommand) and are reported as needs_integration / needs_operator respectively; the
owned-side behavior they depend on is exercised in test_liquidity_migration_cross_sleeve.py.
"""
from __future__ import annotations

import gzip
import logging
import urllib.error
import zipfile
from pathlib import Path

import pytest

from liquidity_migration import archive as archive_module
from liquidity_migration import telegram
from liquidity_migration.archive import (
    ArchiveDownloadIncompleteError,
    download_archive_bytes,
    download_public_trade_archive,
    read_public_trade_archive_klines_1h,
)
from liquidity_migration.cross_sleeve import (
    CrossSleeveAccountState,
    VALID_SLEEVES,
    claim_symbol_reservation,
    claim_symbols_reservation,
    clamp_max_new_entries,
    encode_control_row,
    equal_split_budget,
    partition_claimable,
    read_account_state,
    seed_margin_budget,
)
from liquidity_migration.telegram import TelegramConfig, send_telegram_message


# --------------------------------------------------------------------------------------
# cross-sleeve-1: continuous_addon is budgeted and clamped consistently
# --------------------------------------------------------------------------------------
def test_continuous_addon_is_a_valid_budgeted_sleeve() -> None:
    assert "continuous_addon" in VALID_SLEEVES


def test_equal_split_includes_continuous_addon_in_denominator() -> None:
    # PRE-FIX: 'continuous_addon' was dropped, so this returned {'long':0.5,'continuous':0.5}
    # (denominator 2) and the three sleeves over-committed the one shared account.
    split = equal_split_budget(["long", "continuous", "continuous_addon"])
    assert split == {"long": 1 / 3, "continuous": 1 / 3, "continuous_addon": 1 / 3}
    assert sum(split.values()) == pytest.approx(1.0)
    # addon alone -> 100%, not dropped to {} (which the pre-fix code did, leaving long at 100%)
    assert equal_split_budget(["continuous_addon"]) == {"continuous_addon": 1.0}


def test_clamp_fires_for_an_at_ceiling_continuous_addon() -> None:
    # PRE-FIX: budget_for('continuous_addon') was None (never budgeted) so clamp returned
    # (5, False) -> the addon ran UNCAPPED even at 99% IM.
    state = CrossSleeveAccountState(
        margin_budget_pct_by_sleeve={"long": 1 / 3, "continuous": 1 / 3, "continuous_addon": 1 / 3},
        im_used_pct_by_sleeve={"continuous_addon": 0.40},  # over its 1/3 ceiling
    )
    assert clamp_max_new_entries(5, sleeve="continuous_addon", state=state) == (0, True)
    # a still-under sibling is unchanged
    assert clamp_max_new_entries(5, sleeve="long", state=state) == (5, False)


# --------------------------------------------------------------------------------------
# cross-sleeve-5: empty-dict budget {} round-trips symmetrically (write {} -> read None)
# --------------------------------------------------------------------------------------
def test_empty_dict_budget_round_trips_symmetrically(tmp_path: Path) -> None:
    # equal_split_budget([]) -> {} ; persisting it must NOT read back differently from how it
    # was written. PRE-FIX: encode wrote '{}' but _loads_budget decoded '{}' -> None, so
    # write({}) != read(). Now {} canonicalizes to None on BOTH sides (== no clamp).
    seed_margin_budget(tmp_path, equal_split_budget([]), now_ms=1_000)
    assert read_account_state(tmp_path).margin_budget_pct_by_sleeve is None


def test_encode_control_row_normalizes_empty_budget_to_blank() -> None:
    # The on-disk encoding of an empty-dict budget must equal the encoding of a None budget,
    # so the round-trip cannot silently distinguish "no sleeves budgeted" from "no budget".
    none_row = encode_control_row(CrossSleeveAccountState(margin_budget_pct_by_sleeve=None))
    empty_row = encode_control_row(CrossSleeveAccountState(margin_budget_pct_by_sleeve={}))
    assert none_row["margin_budget_pct_by_sleeve"] == ""
    assert empty_row["margin_budget_pct_by_sleeve"] == ""
    # a NON-empty budget still encodes to a JSON string (unchanged behavior)
    real_row = encode_control_row(CrossSleeveAccountState(margin_budget_pct_by_sleeve={"long": 0.5}))
    assert real_row["margin_budget_pct_by_sleeve"] == '{"long": 0.5}'


# --------------------------------------------------------------------------------------
# cross-sleeve-6: batch claim takes the control-row lock ONCE, preserving the per-candidate
# contract. Equivalence with N sequential single-claims + a single lock acquisition.
# --------------------------------------------------------------------------------------
def _count_lock_acquisitions(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap cross_sleeve.exclusive_file_lock to count how many times the control-row lock is
    actually acquired. partition_claimable must take it ONCE per call regardless of N."""
    from liquidity_migration import cross_sleeve as cs_module

    counter = {"n": 0}
    real_lock = cs_module.exclusive_file_lock

    def counting_lock(*args, **kwargs):  # noqa: ANN002, ANN003
        counter["n"] += 1
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(cs_module, "exclusive_file_lock", counting_lock)
    return counter


def test_partition_claimable_takes_lock_once_for_many_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_margin_budget(tmp_path, {"continuous": 0.5}, now_ms=1_000)  # create the dataset (1 lock)
    counter = _count_lock_acquisitions(monkeypatch)
    candidates = [{"symbol": f"S{i}USDT", "trade_id": f"t{i}"} for i in range(6)]

    granted, skipped = partition_claimable(
        tmp_path, candidates, sleeve="continuous", now_ms=1_700_000_000_000
    )

    assert {c["symbol"] for c in granted} == {c["symbol"] for c in candidates}
    assert skipped == 0
    # PRE-FIX: one lock cycle PER candidate (6). The batch claim takes it exactly once.
    assert counter["n"] == 1
    # the reservations were persisted by the single write
    reserved = {r["symbol"] for r in read_account_state(tmp_path).reservations}
    assert reserved == {c["symbol"] for c in candidates}


def test_batch_claim_rejects_foreign_and_live_but_grants_own_reclaim(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"long": 0.5}, now_ms=1_000)
    now = 1_700_000_000_000
    # a sibling already holds BBBUSDT
    assert claim_symbol_reservation(tmp_path, symbol="BBBUSDT", sleeve="short", trade_id="s1", now_ms=now) is True
    # continuous also already holds CCCUSDT (its own -> later re-claim must grant)
    assert claim_symbol_reservation(tmp_path, symbol="CCCUSDT", sleeve="continuous", trade_id="c0", now_ms=now) is True

    candidates = [
        {"symbol": "AAAUSDT", "trade_id": "c1"},   # free -> grant
        {"symbol": "BBBUSDT", "trade_id": "c2"},   # foreign reservation -> deny
        {"symbol": "CCCUSDT", "trade_id": "c3"},   # own re-claim -> grant
        {"symbol": "DDDUSDT", "trade_id": "c4"},   # live venue position -> deny
    ]
    granted, skipped, denied = claim_symbols_reservation(
        tmp_path, candidates, sleeve="continuous", now_ms=now,
        live_position_symbols={"DDDUSDT"},
    )
    assert {c["symbol"] for c in granted} == {"AAAUSDT", "CCCUSDT"}
    assert {c["symbol"] for c in denied} == {"BBBUSDT", "DDDUSDT"}
    assert skipped == 2
    # the sibling's BBBUSDT reservation survived (batch only ever appends its own grants)
    syms = {(r["symbol"], r["sleeve"]) for r in read_account_state(tmp_path).reservations}
    assert ("BBBUSDT", "short") in syms
    assert ("AAAUSDT", "continuous") in syms


def test_batch_claim_with_no_candidates_never_takes_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_margin_budget(tmp_path, {"long": 0.5}, now_ms=1_000)
    counter = _count_lock_acquisitions(monkeypatch)
    granted, skipped, denied = claim_symbols_reservation(tmp_path, [], sleeve="long", now_ms=1)
    assert (granted, skipped, denied) == ([], 0, [])
    assert counter["n"] == 0  # empty batch short-circuits before touching the lock


def test_batch_claim_fails_open_when_no_control_dataset(tmp_path: Path) -> None:
    # No ws_risk writer yet -> the whole batch is GRANTED (legacy venue-snapshot exclusion only).
    candidates = [{"symbol": "XUSDT", "trade_id": "t1"}, {"symbol": "YUSDT", "trade_id": "t2"}]
    granted, skipped, denied = claim_symbols_reservation(tmp_path, candidates, sleeve="long", now_ms=1)
    assert {c["symbol"] for c in granted} == {"XUSDT", "YUSDT"}
    assert skipped == 0 and denied == []


# --------------------------------------------------------------------------------------
# archive-integrity-1: a truncated download (Content-Length mismatch) is rejected, not promoted
# --------------------------------------------------------------------------------------
class _FakeResponse:
    """Minimal stand-in for urllib's HTTPResponse with a Content-Length header."""

    def __init__(self, body: bytes, *, content_length: int | None) -> None:
        self._body = body
        self._content_length = content_length

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: object = None) -> object:
        if name == "Content-Length" and self._content_length is not None:
            return str(self._content_length)
        return default


def test_download_archive_bytes_rejects_truncated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The server advertised 100 bytes but the socket closed mid-stream after 10. urlopen.read()
    # returns the short body WITHOUT raising; the size check must reject it as incomplete.
    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        return _FakeResponse(b"x" * 10, content_length=100)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    with pytest.raises(ArchiveDownloadIncompleteError):
        download_archive_bytes("https://example.test/AAAUSDT2025-01-01.csv.gz")


def test_download_archive_bytes_accepts_matching_length(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"timestamp,symbol\n1.0,AAAUSDT\n"

    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        return _FakeResponse(body, content_length=len(body))

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    assert download_archive_bytes("https://example.test/x.csv") == body


def test_download_archive_bytes_accepts_when_no_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    # No Content-Length advertised -> we cannot assert, so accept (the cache-hit structural
    # check and read-time parse remain as later defenses).
    body = b"some bytes"

    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        return _FakeResponse(body, content_length=None)

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    assert download_archive_bytes("https://example.test/x.csv") == body


def test_truncated_download_is_retried_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A truncated body is TRANSIENT: download_public_trade_archive must retry and succeed once a
    # full body arrives, rather than promote the partial file. PRE-FIX the partial was accepted.
    full = gzip.compress(
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1735689600.0,AAAUSDT,Buy,0.001,100.0,PlusTick,e1,0,0.001,0.1\n"
    )
    attempts = {"n": 0}

    def fake_urlopen(url, **_kwargs):  # noqa: ANN001
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _FakeResponse(full[:5], content_length=len(full))  # truncated
        return _FakeResponse(full, content_length=len(full))  # complete

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _s: None)

    dest = tmp_path / "AAAUSDT2025-01-01.csv.gz"
    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.gz", dest, retries=3)
    assert out == dest
    assert attempts["n"] == 2  # first truncated attempt rejected, second accepted
    assert dest.read_bytes() == full


# --------------------------------------------------------------------------------------
# archive-integrity-3: a partial/corrupt CACHED archive is re-validated, not re-served forever
# --------------------------------------------------------------------------------------
def test_corrupt_gz_cache_is_unlinked_and_redownloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "AAAUSDT2025-01-01.csv.gz"
    # a previously-written CORRUPT .gz cache (non-empty, so the old st_size>0 guard re-served it)
    dest.write_bytes(b"\x1f\x8b corrupt not really gzip")
    good = gzip.compress(b"timestamp,symbol\n1.0,AAAUSDT\n")

    def fake_download(_url, *, timeout_seconds):  # noqa: ANN001, ANN002, ANN003
        return good

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)

    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.gz", dest)
    # PRE-FIX: the corrupt cache was returned untouched (st_size>0). Now it is re-downloaded.
    assert out == dest
    assert dest.read_bytes() == good


def test_valid_gz_cache_is_reserved_without_redownload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "AAAUSDT2025-01-01.csv.gz"
    good = gzip.compress(b"timestamp,symbol\n1.0,AAAUSDT\n")
    dest.write_bytes(good)

    def fail_download(_url, *, timeout_seconds):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("a valid cache hit must NOT re-download")

    monkeypatch.setattr(archive_module, "download_archive_bytes", fail_download)
    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.gz", dest)
    assert out == dest and dest.read_bytes() == good


def test_corrupt_zip_cache_is_unlinked_and_redownloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "AAAUSDT2025-01-01.csv.zip"
    dest.write_bytes(b"PK not really a zip file")  # non-empty but not a valid zip container

    def fake_download(_url, *, timeout_seconds):  # noqa: ANN001, ANN002, ANN003
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("AAAUSDT2025-01-01.csv", "timestamp,symbol\n1.0,AAAUSDT\n")
        return buf.getvalue()

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)
    out = download_public_trade_archive("https://example.test/AAAUSDT2025-01-01.csv.zip", dest)
    assert out == dest
    # the re-downloaded file is now a valid zip
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["AAAUSDT2025-01-01.csv"]


# --------------------------------------------------------------------------------------
# code-quality-8: the 1h-kline fast-path fallbacks log at WARNING before falling back
# --------------------------------------------------------------------------------------
def test_scalar_1h_fastpath_failure_is_logged_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        "1735689600.0,BTCUSDT,Buy,0.001,100.0,PlusTick,e1,0,0.001,0.1\n"
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    # Force the scalar fast path to blow up so the fallback fires.
    import liquidity_migration.archive as am

    def boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated scalar fast-path fault")

    monkeypatch.setattr(am, "_public_trade_text_handle", boom)

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.archive"):
        result = read_public_trade_archive_klines_1h(archive)

    # correctness preserved: the slow aggregate path still produced the bar
    assert not result.is_empty()
    assert result.to_dicts()[0]["close"] == 100.0
    # PRE-FIX: the exception was swallowed with no log. Now a WARNING surfaces the fault.
    assert any(
        rec.levelno == logging.WARNING and "fast path failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_vectorized_1h_fastpath_failure_is_logged_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    archive = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    csv_text = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        "1735689600.0,BTCUSDT,Buy,0.001,100.0,PlusTick,e1,0,0.001,0.1\n"
    )
    archive.write_bytes(gzip.compress(csv_text.encode("utf-8")))

    import liquidity_migration.archive as am

    monkeypatch.setenv(am.ARCHIVE_VECTORIZE_1H_ENV, "1")

    def boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated vectorized fast-path fault")

    monkeypatch.setattr(am, "_read_public_trade_archive_klines_1h_vectorized", boom)

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.archive"):
        result = read_public_trade_archive_klines_1h(archive)

    assert not result.is_empty()
    assert any(
        rec.levelno == logging.WARNING and "vectorized 1h-kline fast path failed" in rec.getMessage()
        for rec in caplog.records
    )


# --------------------------------------------------------------------------------------
# telegram-alert-3 + telegram-alert-4: the 429 rate-limit retry branch
# --------------------------------------------------------------------------------------
class _TelegramResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_TelegramResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(code: int, *, hdrs) -> urllib.error.HTTPError:  # noqa: ANN001
    return urllib.error.HTTPError("https://api.telegram.org/x", code, "err", hdrs=hdrs, fp=None)


def _install_telegram_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")


def test_429_with_none_headers_does_not_raise_attribute_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # telegram-alert-3: HTTPError(headers=None) on a 429 must NOT raise AttributeError out of
    # the handler. With null headers the retry-after defaults to 1s; the retry below succeeds.
    _install_telegram_credentials(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs=None)  # PRE-FIX: exc.headers.get(...) -> AttributeError
        return _TelegramResponse(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert calls["n"] == 2           # one retry happened
    assert sleeps == [1.0]           # null headers -> default 1s wait (max(1.0, 0.5) == 1.0)


def test_429_retry_sleeps_parsed_retry_after_then_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # telegram-alert-4: a 429 with a Retry-After inside the cap sleeps exactly once for the
    # parsed duration and returns the retried response's 2xx/non-2xx verdict.
    _install_telegram_credentials(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": "3"})
        return _TelegramResponse(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    assert send_telegram_message("hi") is True
    assert calls["n"] == 2
    assert sleeps == [3.0]  # exactly one sleep, of the parsed Retry-After


def test_429_retry_after_beyond_cap_propagates_without_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    # telegram-alert-4: a Retry-After beyond the cap must NOT sleep and must propagate the 429
    # (sleeping longer than the cap inside a cycle-thread send is worse than dropping the page).
    _install_telegram_credentials(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))

    cfg = TelegramConfig(rate_limit_retry_cap_seconds=5.0)
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        raise _http_error(429, hdrs={"Retry-After": "30"})  # beyond the 5s cap

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as info:
        send_telegram_message("hi", config=cfg)
    assert info.value.code == 429
    assert calls["n"] == 1   # no retry attempted
    assert sleeps == []      # never slept


def test_429_retry_returns_false_on_non_2xx_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the single retry itself comes back non-2xx, the send reports False (not a crash).
    _install_telegram_credentials(monkeypatch)
    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, hdrs={"Retry-After": "1"})
        return _TelegramResponse(500)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    assert send_telegram_message("hi") is False
    assert calls["n"] == 2


def test_non_429_http_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 429 branch must not change the contract for other HTTP errors: they propagate.
    _install_telegram_credentials(monkeypatch)

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        raise _http_error(502, hdrs=None)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as info:
        send_telegram_message("hi")
    assert info.value.code == 502


# --------------------------------------------------------------------------------------
# w4-w5-stages-1: the W4-overlap falsifier is fail-closed (no vacuous pass)
# --------------------------------------------------------------------------------------
def _comp(*, available: bool, exact: bool | None = None) -> dict[str, object]:
    overlap: dict[str, object] = {"available": available}
    if exact is not None:
        overlap["exact"] = exact
    return {"w4_overlap": overlap}


def test_w4_overlap_gate_fails_closed_when_artifacts_absent() -> None:
    from scripts.w5_continuous_stage0_candidate_tape import _w4_overlap_gate

    # PRE-FIX idiom: all(exact for c if available) over an all-unavailable set == all([]) == True.
    comp_vals = [_comp(available=False), _comp(available=False)]
    gate = _w4_overlap_gate(comp_vals)
    assert gate["w4_artifacts_present"] is False
    assert gate["w4_overlap_exact"] is False  # MUST be False, not a vacuous True
    assert gate["available_count"] == 0
    assert gate["component_count"] == 2


def test_w4_overlap_gate_passes_only_when_all_present_and_exact() -> None:
    from scripts.w5_continuous_stage0_candidate_tape import _w4_overlap_gate

    gate = _w4_overlap_gate([_comp(available=True, exact=True), _comp(available=True, exact=True)])
    assert gate["w4_artifacts_present"] is True
    assert gate["w4_overlap_exact"] is True
    assert gate["available_count"] == 2


def test_w4_overlap_gate_fails_on_partial_availability() -> None:
    from scripts.w5_continuous_stage0_candidate_tape import _w4_overlap_gate

    # one component missing its W4 control -> not all present -> fail-closed
    gate = _w4_overlap_gate([_comp(available=True, exact=True), _comp(available=False)])
    assert gate["w4_artifacts_present"] is False
    assert gate["w4_overlap_exact"] is False


def test_w4_overlap_gate_fails_on_a_real_mismatch() -> None:
    from scripts.w5_continuous_stage0_candidate_tape import _w4_overlap_gate

    gate = _w4_overlap_gate([_comp(available=True, exact=True), _comp(available=True, exact=False)])
    assert gate["w4_artifacts_present"] is True
    assert gate["w4_overlap_exact"] is False  # a genuine mismatch still fails


def test_w4_overlap_gate_empty_component_set_is_not_a_pass() -> None:
    from scripts.w5_continuous_stage0_candidate_tape import _w4_overlap_gate

    gate = _w4_overlap_gate([])
    assert gate["w4_artifacts_present"] is False
    assert gate["w4_overlap_exact"] is False
