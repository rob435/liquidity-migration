"""Tests for scripts/backfill_binance_metrics_vision.py.

backfill-writers-2 (2026-06-14 audit bucket b14, relocated from
test_audit_fix_b14.py): a transient all-day download failure must NOT be written
as a permanent empty marker that the resume guard treats as complete. The
original code collapsed transient == 404 and wrote an empty .touch().
"""

from __future__ import annotations

import csv
import importlib.util
import io
import zipfile
from pathlib import Path

import polars as pl

_REPO = Path(__file__).resolve().parent.parent


def _load_backfill_metrics():
    path = _REPO / "scripts" / "backfill_binance_metrics_vision.py"
    spec = importlib.util.spec_from_file_location("bf_metrics_b14", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# helpers (relocated from test_audit_int_iH.py)
# --------------------------------------------------------------------------- #
def _metrics_zip(mv, day: str, rows: list[dict]) -> bytes:
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


def _metrics_row(mv, create_time: str) -> dict:
    return {"create_time": create_time,
            **{c: "1.0" for c in mv.NUM_COLS}}


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


# --------------------------------------------------------------------------- #
# backfill-writers-1  (metrics: read-merge + coverage stamp)
# relocated from test_audit_int_iH.py
# --------------------------------------------------------------------------- #
def test_metrics_stamp_coverage_adds_self_describing_column():
    mv = _load_backfill_metrics()
    df = pl.DataFrame({"symbol": ["X"], "ts_ms": [1]})
    out = mv._stamp_coverage(df, "full")
    assert out["coverage"].to_list() == ["full"]


def test_metrics_merge_absent_file_is_identity(tmp_path):
    mv = _load_backfill_metrics()
    df = pl.DataFrame({"symbol": ["X"], "ts_ms": [10]})
    out = mv._merge_with_existing(df, tmp_path / "missing.parquet")
    assert out.sort("ts_ms")["ts_ms"].to_list() == [10]


def test_metrics_merge_empty_touch_marker_is_identity(tmp_path):
    mv = _load_backfill_metrics()
    marker = tmp_path / "X.parquet"
    marker.touch()  # 0-byte marker from a genuine no-data symbol
    df = pl.DataFrame({"symbol": ["X"], "ts_ms": [10]})
    out = mv._merge_with_existing(df, marker)
    assert out["ts_ms"].to_list() == [10]


def test_metrics_merge_unions_prior_and_new_rows(tmp_path):
    """The core backfill-writers-1 fix: a previously-written parquet is UNIONED with
    fresh rows, not clobbered. Prior day-1/day-2 rows survive a run that only
    fetched day-3."""
    mv = _load_backfill_metrics()
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
    mv = _load_backfill_metrics()
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
    mv = _load_backfill_metrics()
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
    mv = _load_backfill_metrics()
    monkeypatch.setattr(mv.time, "sleep", lambda *_: None)

    blobs = {
        "2024-01-01": _metrics_zip(mv, "2024-01-01", [_metrics_row(mv, "2024-01-01 00:00:00")]),
        "2024-01-02": _metrics_zip(mv, "2024-01-02", [_metrics_row(mv, "2024-01-02 00:00:00")]),
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
    assert st2["new_rows"] == 1
    assert st2["max_date"] == "2024-01-02"


def test_metrics_existing_max_date_reads_symbol_tail(tmp_path):
    mv = _load_backfill_metrics()
    path = tmp_path / "XUSDT.parquet"
    pl.DataFrame(
        {
            "symbol": ["XUSDT", "XUSDT"],
            "ts_ms": [1_704_067_200_000, 1_704_153_600_000],
            **{c: [1.0, 1.0] for c in mv.NUM_COLS},
        }
    ).write_parquet(path)

    assert mv._max_date_from_file(path) == "2024-01-02"


def test_metrics_all_404_tail_preserves_existing_manifest_rows(tmp_path, monkeypatch):
    mv = _load_backfill_metrics()
    path = tmp_path / "XUSDT.parquet"
    pl.DataFrame(
        {
            "symbol": ["XUSDT"],
            "ts_ms": [1_704_067_200_000],
            **{c: [1.0] for c in mv.NUM_COLS},
        }
    ).write_parquet(path)
    monkeypatch.setattr(mv, "_day_rows", lambda symbol, day: None)

    st = mv.backfill_symbol("XUSDT", ["2024-01-02", "2024-01-03"], tmp_path, workers=2)

    assert st["rows"] == 1
    assert st["new_rows"] == 0
    assert st["max_date"] == "2024-01-03"
    assert pl.read_parquet(path).height == 1


def test_metrics_backfill_symbol_transient_still_no_marker(tmp_path, monkeypatch):
    """Sanity that the already-shipped writers-2 fix on the metrics side coexists with
    the new merge: all-transient -> incomplete, no marker, coverage still tagged."""
    mv = _load_backfill_metrics()
    monkeypatch.setattr(mv, "_day_rows", lambda sym, d: mv._TRANSIENT_DAY)
    st = mv.backfill_symbol("ZUSDT", ["2024-01-01", "2024-01-02"], tmp_path, workers=2)
    assert st["complete"] is False
    assert st["transient_fail"] == 2
    assert not (tmp_path / "ZUSDT.parquet").exists()
