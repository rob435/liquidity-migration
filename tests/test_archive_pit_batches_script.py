from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_archive_pit_batches as batch_script


def test_archive_pit_batches_stops_when_complete(tmp_path: Path) -> None:
    calls = []
    payloads = [
        {"rows": 2, "workers": 1, "downloaded": 2, "cached": 0, "empty": 0, "failures": 0, "created_at": "a"},
        {"rows": 0, "workers": 1, "downloaded": 0, "cached": 0, "empty": 0, "failures": 0, "created_at": "b"},
    ]

    def fake_download(*_args, **kwargs):
        calls.append(kwargs["config"])
        return payloads[len(calls) - 1]

    summary = batch_script.run_archive_pit_batches(
        tmp_path,
        config=batch_script.ArchivePitBatchConfig(batch_rows=2, coverage_every=0, include_flow=True),
        report_dir=tmp_path / "reports",
        download_func=fake_download,
    )

    assert summary["stop_reason"] == "complete"
    assert summary["batches"] == 2
    assert summary["downloaded"] == 2
    assert calls[0].max_rows == 2
    assert calls[0].include_flow is True
    assert calls[0].keep_archives is False
    assert (tmp_path / "reports" / "archive_pit_batch_summary.md").exists()


def test_archive_pit_batches_stops_on_no_progress_failure(tmp_path: Path) -> None:
    def fake_download(*_args, **_kwargs):
        return {"rows": 5, "workers": 1, "downloaded": 0, "cached": 0, "empty": 0, "failures": 5, "created_at": "a"}

    summary = batch_script.run_archive_pit_batches(
        tmp_path,
        config=batch_script.ArchivePitBatchConfig(batch_rows=5, max_batches=10, coverage_every=0),
        report_dir=tmp_path / "reports",
        download_func=fake_download,
    )

    assert summary["stop_reason"] == "no_progress_failure_batch"
    assert summary["batches"] == 1
    assert summary["failures"] == 5


def test_archive_pit_batches_can_skip_previously_failed_rows(tmp_path: Path) -> None:
    batch_dir = tmp_path / "reports" / "batches"
    batch_dir.mkdir(parents=True)
    (batch_dir / "archive_klines_old.csv").write_text(
        "symbol,date,url,status,bar_rows,flow_1m_rows,flow_1h_rows,error\n"
        "AAAUSDT,2025-01-01,https://example/a,failed,0,0,0,403\n",
        encoding="utf-8",
    )
    calls = []

    def fake_download(*_args, **kwargs):
        calls.append(kwargs["config"])
        return {"rows": 0, "workers": 1, "downloaded": 0, "cached": 0, "empty": 0, "failures": 0, "created_at": "a"}

    summary = batch_script.run_archive_pit_batches(
        tmp_path,
        config=batch_script.ArchivePitBatchConfig(batch_rows=5, coverage_every=0, skip_failed_rows=True),
        report_dir=tmp_path / "reports",
        download_func=fake_download,
    )

    assert calls[0].exclude_keys == ("AAAUSDT|2025-01-01",)
    assert summary["initial_failed_keys"] == 1
    assert summary["final_failed_keys"] == 1


def test_archive_pit_batches_loads_symbols_file(tmp_path: Path) -> None:
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("btcusdt,ethusdt\nSOLUSDT\n", encoding="utf-8")

    assert batch_script._symbol_filters("xrpusdt", str(symbols_file)) == ("XRPUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")
