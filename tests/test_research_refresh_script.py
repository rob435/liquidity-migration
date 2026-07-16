from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

import scripts.research_refresh as refresh


def _date_partitions(root: Path, venue: str, day: str) -> None:
    datasets = ("archive_trade_manifest", "klines_1h") + (
        refresh.BYBIT_ANCILLARY if venue == "bybit" else refresh.BINANCE_ANCILLARY
    )
    for dataset in datasets:
        (root / dataset / f"date={day}").mkdir(parents=True)


def test_latest_partition_date_ignores_malformed_names(tmp_path: Path) -> None:
    dataset = tmp_path / "klines_1h"
    (dataset / "date=2026-07-10").mkdir(parents=True)
    (dataset / "date=2026-07-15").mkdir()
    (dataset / "date=not-a-date").mkdir()
    (dataset / "symbol=BTCUSDT").mkdir()

    assert refresh.latest_partition_date(tmp_path, "klines_1h") == dt.date(2026, 7, 15)


def test_tail_start_uses_stalest_dataset_with_overlap(tmp_path: Path) -> None:
    (tmp_path / "funding" / "date=2026-07-10").mkdir(parents=True)
    (tmp_path / "mark_price_1h" / "date=2026-07-12").mkdir(parents=True)

    start = refresh.tail_start(
        tmp_path,
        ("funding", "mark_price_1h"),
        base_start=dt.date(2021, 1, 1),
        end=dt.date(2026, 7, 16),
        overlap_days=7,
    )

    assert start == dt.date(2026, 7, 3)


def test_rmom_step_migrates_legacy_schema_with_atomic_full_rewrite(tmp_path: Path) -> None:
    pl.DataFrame(
        {"symbol": ["BTCUSDT"], "ts_ms": [1], "residual_momentum": [0.1]}
    ).write_parquet(tmp_path / "residual_momentum.parquet")

    step = refresh._rmom_step(tmp_path, venue="binance", end=dt.date(2026, 7, 16))

    assert "--full-rewrite" in step.command
    assert step.fingerprint_extra["full_rewrite"] is True


def test_rmom_step_keeps_checked_append_for_current_schema(tmp_path: Path) -> None:
    pl.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "ts_ms": [1],
            "residual_momentum": [0.1],
            "is_provisional": [False],
        }
    ).write_parquet(tmp_path / "residual_momentum.parquet")

    step = refresh._rmom_step(tmp_path, venue="bybit", end=dt.date(2026, 7, 16))

    assert "--full-rewrite" not in step.command
    assert step.fingerprint_extra["full_rewrite"] is False


def test_rmom_step_full_rewrites_after_canonical_data_refresh(tmp_path: Path) -> None:
    pl.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "ts_ms": [1],
            "residual_momentum": [0.1],
            "is_provisional": [False],
        }
    ).write_parquet(tmp_path / "residual_momentum.parquet")

    step = refresh._rmom_step(
        tmp_path,
        venue="bybit",
        end=dt.date(2026, 7, 16),
        force_full_rewrite=True,
    )

    assert "--full-rewrite" in step.command
    assert step.fingerprint_extra["full_rewrite"] is True
    assert step.fingerprint_extra["rewrite_reason"] == "canonical_data_mode"


def test_run_ledger_resumes_exact_success_without_reexecuting(tmp_path: Path) -> None:
    output = tmp_path / "result.txt"
    step = refresh.CommandStep(
        step_id="test.step",
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(output)!r}).write_text('once', encoding='utf-8')",
        ),
        expected_paths=(output,),
    )
    ledger = refresh.RunLedger(tmp_path / "run")

    assert ledger.run(step) is True
    output.write_text("preserved", encoding="utf-8")
    assert ledger.run(step) is True

    assert output.read_text(encoding="utf-8") == "preserved"
    statuses = [
        __import__("json").loads(line)["status"]
        for line in ledger.path.read_text(encoding="utf-8").splitlines()
    ]
    assert statuses == ["started", "succeeded", "skipped_completed"]


def test_canonical_builder_freezes_end_and_strict_failure_ratio(tmp_path: Path) -> None:
    args = refresh.build_parser().parse_args(
        ["plan", "--end", "2026-07-16", "--binance-root", str(tmp_path)]
    )
    step = refresh._canonical_builder_step(
        venue="binance",
        root=tmp_path,
        end=dt.date(2026, 7, 16),
        args=args,
    )

    assert step.env["BINANCE_END"] == "2026-07-16"
    assert step.env["BINANCE_MAX_FAILURE_RATIO"] == "0"
    assert step.expected_paths[-1] == tmp_path / "klines_1h" / "date=2026-07-15"


def test_require_coverage_rejects_one_stale_ancillary(tmp_path: Path) -> None:
    _date_partitions(tmp_path, "bybit", "2026-07-15")
    stale = tmp_path / "funding" / "date=2026-07-15"
    stale.rmdir()
    (tmp_path / "funding" / "date=2026-07-14").mkdir()

    with pytest.raises(RuntimeError, match="funding"):
        refresh._require_coverage(tmp_path, venue="bybit", end=dt.date(2026, 7, 16))


def test_plan_with_all_phases_skipped_is_read_only(tmp_path: Path) -> None:
    output = tmp_path / "reports"
    result = refresh.main(
        [
            "plan",
            "--end",
            "2026-07-16",
            "--bybit-root",
            str(tmp_path / "bybit"),
            "--binance-root",
            str(tmp_path / "binance"),
            "--output-root",
            str(output),
            "--skip-data",
            "--skip-features",
            "--skip-backtests",
        ]
    )

    assert result == 0
    assert not output.exists()
    assert not (tmp_path / "bybit").exists()
    assert not (tmp_path / "binance").exists()
