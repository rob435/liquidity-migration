from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import report_archive_pit_coverage as coverage_script

from aggression_carry.storage import write_dataset


def test_archive_pit_coverage_marks_missing_sparse_and_next_day(tmp_path: Path) -> None:
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2025-01-01", "url": "https://example/a"},
            {"symbol": "AAAUSDT", "date": "2025-01-02", "url": "https://example/b"},
            {"symbol": "BBBUSDT", "date": "2025-01-01", "url": "https://example/c"},
        ]
    )
    klines = pl.DataFrame(
        [
            *[
                {
                    "ts_ms": 1_735_689_600_000 + minute * 60_000,
                    "symbol": "AAAUSDT",
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume_base": 1.0,
                    "turnover_quote": 1.0,
                    "source": "fixture",
                }
                for minute in range(3)
            ],
            {
                "ts_ms": 1_735_776_000_000,
                "symbol": "AAAUSDT",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "fixture",
            },
        ]
    )
    write_dataset(manifest, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)
    write_dataset(klines, tmp_path, "klines_1m", partition_by=("date", "symbol"), append=False)

    coverage = coverage_script.build_archive_pit_coverage(
        tmp_path,
        manifest,
        min_bars_per_day=2,
        require_next_day=True,
    ).sort(["symbol", "date"])
    monthly = coverage_script.summarize_coverage_monthly(coverage)

    rows = coverage.select(["symbol", "date", "status", "next_status", "usable_for_close_fade"]).to_dicts()
    assert rows == [
        {
            "symbol": "AAAUSDT",
            "date": "2025-01-01",
            "status": "covered",
            "next_status": "sparse",
            "usable_for_close_fade": True,
        },
        {
            "symbol": "AAAUSDT",
            "date": "2025-01-02",
            "status": "sparse",
            "next_status": "missing",
            "usable_for_close_fade": False,
        },
        {
            "symbol": "BBBUSDT",
            "date": "2025-01-01",
            "status": "missing",
            "next_status": "missing",
            "usable_for_close_fade": False,
        },
    ]
    assert monthly.row(0, named=True)["covered_rows"] == 1
    assert monthly.row(0, named=True)["sparse_rows"] == 1
    assert monthly.row(0, named=True)["missing_rows"] == 1
    assert monthly.row(0, named=True)["processed_rows"] == 2
    assert monthly.row(0, named=True)["usable_rows"] == 1


def test_archive_pit_coverage_allows_same_day_only_mode(tmp_path: Path) -> None:
    manifest = pl.DataFrame([{"symbol": "AAAUSDT", "date": "2025-01-01", "url": "https://example/a"}])
    klines = pl.DataFrame(
        [
            {
                "ts_ms": 1_735_689_600_000 + minute * 60_000,
                "symbol": "AAAUSDT",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "fixture",
            }
            for minute in range(2)
        ]
    )
    write_dataset(klines, tmp_path, "klines_1m", partition_by=("date", "symbol"), append=False)

    coverage = coverage_script.build_archive_pit_coverage(
        tmp_path,
        manifest,
        min_bars_per_day=2,
        require_next_day=False,
    )

    assert coverage.row(0, named=True)["usable_for_close_fade"] is True


def test_archive_pit_coverage_rejects_sparse_same_day_partition(tmp_path: Path) -> None:
    manifest = pl.DataFrame([{"symbol": "AAAUSDT", "date": "2025-01-01", "url": "https://example/a"}])
    klines = pl.DataFrame(
        [
            {
                "ts_ms": 1_735_689_600_000,
                "symbol": "AAAUSDT",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "fixture",
            }
        ]
    )
    write_dataset(klines, tmp_path, "klines_1m", partition_by=("date", "symbol"), append=False)

    coverage = coverage_script.build_archive_pit_coverage(
        tmp_path,
        manifest,
        min_bars_per_day=2,
        require_next_day=False,
    )

    assert coverage.row(0, named=True)["status"] == "sparse"
    assert coverage.row(0, named=True)["usable_for_close_fade"] is False


def test_archive_pit_coverage_requires_min_flow_hours_for_usable_row(tmp_path: Path) -> None:
    manifest = pl.DataFrame([{"symbol": "AAAUSDT", "date": "2025-01-01", "url": "https://example/a"}])
    klines = pl.DataFrame(
        [
            {
                "ts_ms": 1_735_689_600_000 + minute * 60_000,
                "symbol": "AAAUSDT",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
                "source": "fixture",
            }
            for minute in range(2)
        ]
    )
    sparse_flow = pl.DataFrame(
        [{"ts_ms": 1_735_689_600_000, "symbol": "AAAUSDT", "total_quote": 1.0, "trade_count": 1}]
    )
    dense_flow = pl.DataFrame(
        [
            {
                "ts_ms": 1_735_689_600_000 + hour * 60 * 60 * 1000,
                "symbol": "AAAUSDT",
                "total_quote": 1.0,
                "trade_count": 1,
            }
            for hour in range(2)
        ]
    )
    write_dataset(klines, tmp_path, "klines_1m", partition_by=("date", "symbol"), append=False)
    write_dataset(sparse_flow, tmp_path, "signed_flow_1h", partition_by=("date", "symbol"), append=False)

    sparse_coverage = coverage_script.build_archive_pit_coverage(
        tmp_path,
        manifest,
        min_bars_per_day=2,
        require_next_day=False,
        require_flow=True,
        min_flow_hours_per_day=2,
    )
    write_dataset(dense_flow, tmp_path, "signed_flow_1h", partition_by=("date", "symbol"), append=False)
    dense_coverage = coverage_script.build_archive_pit_coverage(
        tmp_path,
        manifest,
        min_bars_per_day=2,
        require_next_day=False,
        require_flow=True,
        min_flow_hours_per_day=2,
    )

    assert sparse_coverage.row(0, named=True)["flow_status"] == "sparse"
    assert sparse_coverage.row(0, named=True)["usable_for_close_fade"] is False
    assert dense_coverage.row(0, named=True)["flow_status"] == "covered"
    assert dense_coverage.row(0, named=True)["usable_for_close_fade"] is True


def test_archive_pit_coverage_threshold_gate() -> None:
    summary = {"coverage_rate": 0.80, "usable_rate": 0.60}

    assert coverage_script._coverage_thresholds_pass(summary, min_coverage_rate=0.80, min_usable_rate=0.60)
    assert not coverage_script._coverage_thresholds_pass(summary, min_coverage_rate=0.90, min_usable_rate=0.60)
    assert not coverage_script._coverage_thresholds_pass(summary, min_coverage_rate=0.80, min_usable_rate=0.70)
