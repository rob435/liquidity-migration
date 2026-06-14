"""Audit integration regression tests — bucket iH.

Cross-file completion of the binance Vision backfill writers:

  backfill-writers-1 (scripts/backfill_binance_metrics_vision.py):
    backfill_symbol must READ-MERGE the existing per-symbol parquet instead of
    clobbering just the days it was passed, and stamp a self-describing
    `coverage` column on disk (+ in the manifest dict). The dir is a CANONICAL
    PIT root shared with the event-anchored wrapper, so a bare overwrite of the
    window-days would truncate a previously-full symbol into a present-but-sparse
    file a reader mistakes for "no data".

  backfill-writers-2 (scripts/backfill_binance_bookdepth_vision.py):
    a TRANSIENT all-day outage must not be frozen into a permanent empty
    `.touch()` marker the resume guard treats as complete. _day_rows must return
    a _TRANSIENT_DAY sentinel (distinct from a genuine-404 None) and
    backfill_symbol must count transient days separately, set complete=False, and
    NOT write the empty marker when any day failed transiently.

Network is never touched: _day_rows / _fetch are monkeypatched.
"""
from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path

import polars as pl
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import backfill_binance_metrics_vision as mv  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _metrics_zip(day: str, rows: list[dict]) -> bytes:
    """A real metrics daily .zip blob _day_rows can parse (header + rows)."""
    buf = io.StringIO()
    cols = ["create_time", *mv.NUM_COLS]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(f"X-metrics-{day}.csv", buf.getvalue())
    return out.getvalue()


def _metrics_row(create_time: str) -> dict:
    return {"create_time": create_time,
            **{c: "1.0" for c in mv.NUM_COLS}}


# --------------------------------------------------------------------------- #
# backfill-writers-1  (metrics: read-merge + coverage stamp)
# --------------------------------------------------------------------------- #
def test_metrics_stamp_coverage_adds_self_describing_column():
    df = pl.DataFrame({"symbol": ["X"], "ts_ms": [1]})
    out = mv._stamp_coverage(df, "full")
    assert out["coverage"].to_list() == ["full"]


def test_metrics_merge_absent_file_is_identity(tmp_path):
    df = pl.DataFrame({"symbol": ["X"], "ts_ms": [10]})
    out = mv._merge_with_existing(df, tmp_path / "missing.parquet")
    assert out.sort("ts_ms")["ts_ms"].to_list() == [10]


def test_metrics_merge_empty_touch_marker_is_identity(tmp_path):
    marker = tmp_path / "X.parquet"
    marker.touch()  # 0-byte marker from a genuine no-data symbol
    df = pl.DataFrame({"symbol": ["X"], "ts_ms": [10]})
    out = mv._merge_with_existing(df, marker)
    assert out["ts_ms"].to_list() == [10]


def test_metrics_merge_unions_prior_and_new_rows(tmp_path):
    """The core backfill-writers-1 fix: a previously-written parquet is UNIONED with
    fresh rows, not clobbered. Prior day-1/day-2 rows survive a run that only
    fetched day-3."""
    path = tmp_path / "X.parquet"
    prior = pl.DataFrame({"symbol": ["X", "X"], "ts_ms": [1, 2],
                          **{c: [1.0, 1.0] for c in mv.NUM_COLS}})
    prior.write_parquet(path)
    new = pl.DataFrame({"symbol": ["X"], "ts_ms": [3],
                        **{c: [9.0] for c in mv.NUM_COLS}})
    merged = mv._merge_with_existing(new, path)
    assert merged.sort("ts_ms")["ts_ms"].to_list() == [1, 2, 3]


def test_metrics_merge_last_write_wins_on_overlap(tmp_path):
    """A re-fetched/corrected day supersedes the stale row (keep=last)."""
    path = tmp_path / "X.parquet"
    prior = pl.DataFrame({"symbol": ["X"], "ts_ms": [1],
                          **{c: [1.0] for c in mv.NUM_COLS}})
    prior.write_parquet(path)
    new = pl.DataFrame({"symbol": ["X"], "ts_ms": [1],
                        **{c: [7.0] for c in mv.NUM_COLS}})
    merged = mv._merge_with_existing(new, path)
    assert merged.height == 1
    assert merged["sum_open_interest"].to_list() == [7.0]


def test_metrics_merge_drops_stale_coverage_column(tmp_path):
    """A prior parquet that already carried a `coverage` column must not break the
    concat (column is dropped before union; caller re-stamps)."""
    path = tmp_path / "X.parquet"
    prior = pl.DataFrame({"symbol": ["X"], "ts_ms": [1],
                          **{c: [1.0] for c in mv.NUM_COLS},
                          "coverage": ["event_anchored"]})
    prior.write_parquet(path)
    new = pl.DataFrame({"symbol": ["X"], "ts_ms": [2],
                        **{c: [1.0] for c in mv.NUM_COLS}})
    merged = mv._merge_with_existing(new, path)
    assert "coverage" not in merged.columns  # dropped; backfill_symbol re-stamps
    assert merged.sort("ts_ms")["ts_ms"].to_list() == [1, 2]


def test_metrics_backfill_symbol_merges_and_stamps_full_coverage(tmp_path, monkeypatch):
    """End-to-end through backfill_symbol: a second run that fetches a new day must
    UNION with the first run's rows and the written parquet must carry coverage='full'.
    Closes the clobber-of-canonical-root hazard (backfill-writers-1)."""
    monkeypatch.setattr(mv.time, "sleep", lambda *_: None)

    blobs = {
        "2024-01-01": _metrics_zip("2024-01-01", [_metrics_row("2024-01-01 00:00:00")]),
        "2024-01-02": _metrics_zip("2024-01-02", [_metrics_row("2024-01-02 00:00:00")]),
    }
    def fake_fetch(url, timeout=30):
        # ".../X-metrics-2024-01-01.zip" -> "2024-01-01"
        day = url.split("-metrics-", 1)[1].removesuffix(".zip")
        return blobs[day]

    monkeypatch.setattr(mv, "_fetch", fake_fetch)

    st1 = mv.backfill_symbol("XUSDT", ["2024-01-01"], tmp_path, workers=2)
    assert st1["coverage"] == "full"
    assert st1["complete"] is True
    df1 = pl.read_parquet(tmp_path / "XUSDT.parquet")
    assert "coverage" in df1.columns and set(df1["coverage"].to_list()) == {"full"}
    assert df1.height == 1

    # Second run fetches day-2; day-1 rows must SURVIVE (merge, not clobber).
    st2 = mv.backfill_symbol("XUSDT", ["2024-01-02"], tmp_path, workers=2)
    df2 = pl.read_parquet(tmp_path / "XUSDT.parquet")
    assert df2.height == 2, "day-1 rows were clobbered — read-merge failed"
    assert set(df2["coverage"].to_list()) == {"full"}
    assert st2["rows"] == 2


def test_metrics_backfill_symbol_transient_still_no_marker(tmp_path, monkeypatch):
    """Sanity that the already-shipped writers-2 fix on the metrics side coexists with
    the new merge: all-transient -> incomplete, no marker, coverage still tagged."""
    monkeypatch.setattr(mv, "_day_rows", lambda sym, d: mv._TRANSIENT_DAY)
    st = mv.backfill_symbol("ZUSDT", ["2024-01-01", "2024-01-02"], tmp_path, workers=2)
    assert st["complete"] is False
    assert st["transient_fail"] == 2
    assert not (tmp_path / "ZUSDT.parquet").exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
