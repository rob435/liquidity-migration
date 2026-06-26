"""Regression tests for audit2 unit ``bookdepth_backfill``.

Ports the metrics-backfill transient-vs-404 fix to
scripts/backfill_binance_bookdepth_vision.py (the canonical pattern lives in
scripts/backfill_binance_metrics_vision.py and its tests in
tests/test_audit_fix_b14.py / tests/test_audit_int_iH.py).

The bug: ``_fetch`` returned ``None`` after exhausting retries on a TRANSIENT
error (5xx/DNS/connection) IDENTICALLY to a genuine 404, so a transient failure
on day D was frozen as a permanent no-data gap that the resume guard never
re-attempts — silent data loss, a non-negotiable correctness class in this repo
(data_roots.md: "absence == not-downloaded, never no-data").

Each test FAILS on the original code and PASSES on the fix. The network is never
touched: ``_fetch`` / ``_day_rows`` are monkeypatched and zip blobs are built
in-memory.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import polars as pl

_REPO = Path(__file__).resolve().parent.parent


def _load_backfill_bookdepth():
    path = _REPO / "scripts" / "backfill_binance_bookdepth_vision.py"
    spec = importlib.util.spec_from_file_location("bf_bookdepth_audit2", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _bookdepth_zip(day: str, rows: list[dict]) -> bytes:
    """A real bookDepth daily .zip blob ``_day_rows`` can parse (header + rows)."""
    buf = io.StringIO()
    cols = ["timestamp", "percentage", "depth", "notional"]
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"X-bookDepth-{day}.csv", buf.getvalue())
    return zbuf.getvalue()


def _bookdepth_row(ts: str, pct: str = "1") -> dict:
    return {"timestamp": ts, "percentage": pct, "depth": "100.0", "notional": "5000.0"}


# ---------------------------------------------------------------------------
# _fetch: transient exhaustion signals __RETRY__, genuine 404 signals None
# ---------------------------------------------------------------------------


def test_fetch_returns_retry_sentinel_on_exhausted_transient(monkeypatch) -> None:
    """A persistent connection error (never an HTTPError 404) must exhaust retries
    and return the b"__RETRY__" sentinel — NOT None. On the old code this returned
    None, indistinguishable from a real 404."""
    bf = _load_backfill_bookdepth()
    calls = {"n": 0}

    def _boom(req, timeout=30):
        calls["n"] += 1
        raise OSError("transient connection blip")

    monkeypatch.setattr(bf.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(bf.time, "sleep", lambda *_a, **_k: None)  # no real backoff
    out = bf._fetch("https://x/AAA.zip")
    assert out == b"__RETRY__"
    assert calls["n"] == 4  # 4 attempts, then give up (no infinite retry)


def test_fetch_returns_none_on_genuine_404(monkeypatch) -> None:
    """A genuine HTTP 404 must return None immediately (real absence), with no retry
    storm — the normal pre-listing / delisted-tail case is unchanged."""
    bf = _load_backfill_bookdepth()
    calls = {"n": 0}

    def _not_found(req, timeout=30):
        calls["n"] += 1
        raise bf.urllib.error.HTTPError("https://x/AAA.zip", 404, "Not Found", {}, None)

    monkeypatch.setattr(bf.urllib.request, "urlopen", _not_found)
    monkeypatch.setattr(bf.time, "sleep", lambda *_a, **_k: None)
    out = bf._fetch("https://x/AAA.zip")
    assert out is None
    assert calls["n"] == 1  # 404 short-circuits — no retries


# ---------------------------------------------------------------------------
# _day_rows: distinct sentinel for transient vs genuine no-data
# ---------------------------------------------------------------------------


def test_day_rows_transient_returns_sentinel_not_none(monkeypatch) -> None:
    """When both fetch attempts exhaust retries, _day_rows yields the distinct
    _TRANSIENT_DAY sentinel (not None), so the caller can tell an outage from a 404."""
    bf = _load_backfill_bookdepth()
    monkeypatch.setattr(bf, "_fetch", lambda url, timeout=30: b"__RETRY__")
    out = bf._day_rows("XUSDT", "2024-01-01")
    assert out is bf._TRANSIENT_DAY


def test_day_rows_genuine_404_returns_none(monkeypatch) -> None:
    """A genuine 404 (None blob) stays None — a real no-data day."""
    bf = _load_backfill_bookdepth()
    monkeypatch.setattr(bf, "_fetch", lambda url, timeout=30: None)
    out = bf._day_rows("XUSDT", "2024-01-01")
    assert out is None


def test_day_rows_normal_day_unchanged(monkeypatch) -> None:
    """Numerical-equivalence gate: a normal day with two snapshots in the same hour
    aggregates to one (hour, percentage) row with the documented mean/last/n values —
    the happy path is unchanged by the fix."""
    bf = _load_backfill_bookdepth()
    blob = _bookdepth_zip(
        "2024-01-01",
        [
            _bookdepth_row("2024-01-01 00:00:00", pct="1"),  # depth 100, notional 5000
            {"timestamp": "2024-01-01 00:01:00", "percentage": "1",
             "depth": "200.0", "notional": "7000.0"},
        ],
    )
    monkeypatch.setattr(bf, "_fetch", lambda url, timeout=30: blob)
    out = bf._day_rows("XUSDT", "2024-01-01")
    assert isinstance(out, list) and len(out) == 1
    row = out[0]
    assert row["symbol"] == "XUSDT"
    assert row["hour"] == "2024-01-01 00"
    assert row["percentage"] == "1"
    assert row["depth_mean"] == 150.0       # (100 + 200) / 2
    assert row["notional_mean"] == 6000.0   # (5000 + 7000) / 2
    assert row["depth_last"] == 200.0       # last snapshot
    assert row["notional_last"] == 7000.0
    assert row["n_snaps"] == 2


# ---------------------------------------------------------------------------
# backfill_symbol: transient -> incomplete, NO permanent marker
# ---------------------------------------------------------------------------


def test_backfill_transient_all_day_does_not_mark_complete(tmp_path, monkeypatch) -> None:
    """When EVERY day fails transiently, the writer must NOT touch an empty marker
    nor mark the symbol complete. The original code collapsed transient == 404 and
    wrote the empty .touch(), freezing the outage into permanent zero coverage."""
    bf = _load_backfill_bookdepth()
    out_dir = tmp_path / "bookdepth"
    out_dir.mkdir()
    monkeypatch.setattr(bf, "_day_rows", lambda symbol, day: bf._TRANSIENT_DAY)
    st = bf.backfill_symbol("AAAUSDT", ["2024-01-01", "2024-01-02"], out_dir, workers=2)
    assert st["rows"] == 0
    assert st["complete"] is False
    assert st["transient_fail"] == 2
    # No permanent no-data marker was written — the symbol stays eligible for re-fetch.
    assert not (out_dir / "AAAUSDT.parquet").exists()


def test_backfill_genuine_404_all_day_marks_complete(tmp_path, monkeypatch) -> None:
    """Control: when every day is a genuine 404 the writer DOES touch the empty
    marker and is complete — the fix must not regress the normal absent-symbol case."""
    bf = _load_backfill_bookdepth()
    out_dir = tmp_path / "bookdepth"
    out_dir.mkdir()
    monkeypatch.setattr(bf, "_day_rows", lambda symbol, day: None)  # genuine 404
    st = bf.backfill_symbol("BBBUSDT", ["2024-01-01", "2024-01-02"], out_dir, workers=2)
    assert st["rows"] == 0
    assert st["complete"] is True
    assert st["transient_fail"] == 0
    assert (out_dir / "BBBUSDT.parquet").exists()  # empty marker for a real no-data symbol


def test_backfill_partial_transient_keeps_rows_but_incomplete(tmp_path, monkeypatch) -> None:
    """A symbol with SOME real rows AND a transient day keeps the fetched rows (parquet
    written) but is marked incomplete so a re-run fills the transient gap."""
    bf = _load_backfill_bookdepth()
    out_dir = tmp_path / "bookdepth"
    out_dir.mkdir()

    def _day_rows(symbol, day):
        if day == "2024-01-01":
            return [{"symbol": symbol, "hour": "2024-01-01 00", "percentage": "1",
                     "depth_mean": 1.0, "notional_mean": 2.0, "depth_last": 1.0,
                     "notional_last": 2.0, "n_snaps": 1}]
        return bf._TRANSIENT_DAY  # the second day failed transiently

    monkeypatch.setattr(bf, "_day_rows", _day_rows)
    st = bf.backfill_symbol("CCCUSDT", ["2024-01-01", "2024-01-02"], out_dir, workers=2)
    assert st["rows"] == 1
    assert st["transient_fail"] == 1
    assert st["complete"] is False  # incomplete -> re-runnable
    assert (out_dir / "CCCUSDT.parquet").exists()  # rows preserved, not clobbered


def test_backfill_missing_days_merges_existing_symbol_parquet(tmp_path, monkeypatch) -> None:
    """The manifest-driven top-up fetches only listed days, replaces those dates in
    the existing symbol parquet, and preserves unrelated dates."""
    bf = _load_backfill_bookdepth()
    out_dir = tmp_path / "bookdepth"
    out_dir.mkdir()
    old = bf._bookdepth_frame(
        [
            {"symbol": "AAAUSDT", "hour": "2024-01-01 00", "percentage": "1",
             "depth_mean": 1.0, "notional_mean": 2.0, "depth_last": 1.0,
             "notional_last": 2.0, "n_snaps": 1},
            {"symbol": "AAAUSDT", "hour": "2024-01-02 00", "percentage": "1",
             "depth_mean": 10.0, "notional_mean": 20.0, "depth_last": 10.0,
             "notional_last": 20.0, "n_snaps": 1},
        ]
    )
    old.write_parquet(out_dir / "AAAUSDT.parquet")

    def _day_rows(symbol, day):
        if day == "2024-01-02":
            return [{"symbol": symbol, "hour": "2024-01-02 00", "percentage": "1",
                     "depth_mean": 30.0, "notional_mean": 40.0, "depth_last": 30.0,
                     "notional_last": 40.0, "n_snaps": 1}]
        return None

    monkeypatch.setattr(bf, "_day_rows", _day_rows)
    st = bf.backfill_symbol_missing_days("AAAUSDT", ["2024-01-02", "2024-01-03"], out_dir, workers=1)
    rows = pl.read_parquet(out_dir / "AAAUSDT.parquet").sort(["hour", "percentage"]).to_dicts()

    assert st["days_requested"] == 2
    assert st["rows_fetched"] == 1
    assert st["days_404"] == 1
    assert st["complete"] is True
    assert [(row["hour"], row["depth_mean"]) for row in rows] == [
        ("2024-01-01 00", 1.0),
        ("2024-01-02 00", 30.0),
    ]


def test_main_missing_days_dry_run_does_not_touch_root(tmp_path, monkeypatch, capsys) -> None:
    """Dry-run plans deduped symbol-days without fetching or creating the output root."""
    bf = _load_backfill_bookdepth()
    manifest = tmp_path / "missing_days.csv"
    manifest.write_text(
        "symbol,date\nAAAUSDT,2024-01-02\nAAAUSDT,2024-01-02\nBBBUSDT,2024-01-03\n",
        encoding="utf-8",
    )
    root = tmp_path / "binance_full_pit"

    def _should_not_fetch(*_args, **_kwargs):
        raise AssertionError("dry-run should not fetch")

    monkeypatch.setattr(bf, "_day_rows", _should_not_fetch)
    monkeypatch.setattr(
        bf.sys,
        "argv",
        ["prog", "--root", str(root), "--missing-days-csv", str(manifest), "--dry-run"],
    )
    rc = bf.main()
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["mode"] == "missing_days"
    assert out["dry_run"] is True
    assert out["symbols"] == 2
    assert out["symbol_days"] == 2
    assert not (root / "binance_usdm_bookdepth_1h").exists()


def test_main_missing_days_resume_skips_complete_symbols(tmp_path, monkeypatch, capsys) -> None:
    """A completed missing-day request is resume-skipped; incomplete symbols rerun."""
    bf = _load_backfill_bookdepth()
    manifest_csv = tmp_path / "missing_days.csv"
    manifest_csv.write_text(
        "symbol,date\nAAAUSDT,2024-01-02\nBBBUSDT,2024-01-03\n",
        encoding="utf-8",
    )
    root = tmp_path / "binance_full_pit"
    out_dir = root / "binance_usdm_bookdepth_1h"
    out_dir.mkdir(parents=True)
    (out_dir / "AAAUSDT.parquet").touch()
    (out_dir / "_missing_day_manifest.json").write_text(
        json.dumps(
            {
                "AAAUSDT": {
                    "symbol": "AAAUSDT",
                    "requested_days": ["2024-01-01", "2024-01-02"],
                    "complete": True,
                },
                "BBBUSDT": {
                    "symbol": "BBBUSDT",
                    "requested_days": ["2024-01-02"],
                    "complete": False,
                },
            }
        ),
        encoding="utf-8",
    )
    seen: list[tuple[str, list[str]]] = []

    def _fake_backfill(symbol, days, out, workers):
        seen.append((symbol, days))
        return {
            "symbol": symbol,
            "days_requested": len(days),
            "rows_fetched": 0,
            "rows_after_merge": 0,
            "days_404": 1,
            "transient_fail": 0,
            "complete": True,
        }

    monkeypatch.setattr(bf, "backfill_symbol_missing_days", _fake_backfill)
    monkeypatch.setattr(
        bf.sys,
        "argv",
        ["prog", "--root", str(root), "--missing-days-csv", str(manifest_csv), "--workers", "1"],
    )
    rc = bf.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert seen == [("BBBUSDT", ["2024-01-03"])]
    assert "skipped complete 1" in out
    updated = json.loads((out_dir / "_missing_day_manifest.json").read_text(encoding="utf-8"))
    assert updated["BBBUSDT"]["days_requested"] == 2
    assert updated["BBBUSDT"]["requested_days"] == ["2024-01-02", "2024-01-03"]


# ---------------------------------------------------------------------------
# main() resume guard: incomplete-by-transient symbol is re-attempted
# ---------------------------------------------------------------------------


def _write_klines(root: Path, symbol: str, day: str) -> None:
    """Minimal klines_1h coverage so _kline_spans yields the symbol for one day."""
    ddir = root / "klines_1h" / f"date={day}" / f"symbol={symbol}"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "data.parquet").write_bytes(b"")  # presence is all _kline_spans needs


def test_main_reattempts_transient_incomplete_symbol(tmp_path, monkeypatch) -> None:
    """End-to-end resume guard: a symbol whose parquet exists but whose manifest marks
    it complete=False (a prior transient outage) MUST be retried, while a genuine
    no-data symbol (complete=True, empty marker) is treated as covered and skipped.

    On the old code there is no "complete" flag and the guard skips any symbol whose
    parquet exists, so the transient symbol would never be re-fetched."""
    bf = _load_backfill_bookdepth()
    root = tmp_path / "binance_full_pit"
    out_dir = root / "binance_usdm_bookdepth_1h"
    out_dir.mkdir(parents=True)

    # TRN: prior transient outage — parquet marker exists, manifest says incomplete.
    _write_klines(root, "TRNUSDT", "2024-01-01")
    (out_dir / "TRNUSDT.parquet").touch()
    # DON: genuinely no data — parquet marker exists, manifest says complete.
    _write_klines(root, "DONUSDT", "2024-01-01")
    (out_dir / "DONUSDT.parquet").touch()
    manifest = {
        "TRNUSDT": {"symbol": "TRNUSDT", "rows": 0, "days_404": 0,
                    "transient_fail": 1, "complete": False},
        "DONUSDT": {"symbol": "DONUSDT", "rows": 0, "days_404": 1,
                    "transient_fail": 0, "complete": True},
    }
    (out_dir / "_manifest.json").write_text(json.dumps(manifest))

    seen: list[str] = []

    def _fake_backfill_symbol(symbol, days, out, workers):
        seen.append(symbol)
        return {"symbol": symbol, "rows": 0, "days_404": 0,
                "transient_fail": 0, "complete": True}

    monkeypatch.setattr(bf, "backfill_symbol", _fake_backfill_symbol)
    monkeypatch.setattr(
        bf.sys, "argv",
        ["prog", "--root", str(root), "--start", "2024-01-01", "--workers", "1"],
    )
    rc = bf.main()
    assert rc == 0
    # The transient-incomplete symbol is re-attempted; the genuine-404 symbol is not.
    assert "TRNUSDT" in seen
    assert "DONUSDT" not in seen
