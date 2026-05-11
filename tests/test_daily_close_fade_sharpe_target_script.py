from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from aggression_carry.storage import write_dataset
from aggression_carry.downloaders import parse_date_ms
from scripts import run_daily_close_fade_sharpe_target as sharpe_target


def test_sharpe_target_reports_missing_required_datasets(tmp_path: Path) -> None:
    output_dir = tmp_path / "reports"

    rc = sharpe_target.main(
        [
            "--data-root",
            str(tmp_path),
            "--report-dir",
            str(output_dir),
            "--start",
            "2025-05-08",
            "--end",
            "2026-05-08",
        ]
    )

    payload = json.loads((output_dir / "daily_close_fade_sharpe_target.json").read_text(encoding="utf-8"))
    assert rc == 2
    assert payload["status"] == "blocked_missing_data"
    assert "klines_1m" in payload["missing"]
    assert "premium_index_1h or mark_price_1h+index_price_1h" in payload["missing"]


def test_sharpe_target_accepts_mark_index_as_premium_alternative(tmp_path: Path) -> None:
    row = {"ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT"}
    for dataset in ("klines_1m", "instruments", "archive_trade_manifest", "funding", "open_interest", "signed_flow_1h", "mark_price_1h", "index_price_1h"):
        write_dataset(pl.DataFrame([{**row, "date": "2023-11-14", "close": 1.0, "url": "https://example.test/a.csv.gz"}]), tmp_path, dataset)

    status = sharpe_target.build_dataset_status(tmp_path)

    assert sharpe_target.missing_required_datasets(status) == []


def test_sharpe_target_writes_pit_symbol_file_from_manifest(tmp_path: Path) -> None:
    output_dir = tmp_path / "reports"
    write_dataset(
        pl.DataFrame(
            [
                {"symbol": "ethusdt", "date": "2025-05-08", "url": "https://example.test/e.csv.gz"},
                {"symbol": "BTCUSDT", "date": "2025-05-08", "url": "https://example.test/b.csv.gz"},
            ]
        ),
        tmp_path,
        "archive_trade_manifest",
    )

    path = sharpe_target.write_pit_symbols_file(tmp_path, output_dir)

    assert path == str(output_dir / "pit_symbols.txt")
    assert (output_dir / "pit_symbols.txt").read_text(encoding="utf-8") == "BTCUSDT,ETHUSDT\n"


def test_sharpe_target_filters_pit_symbols_and_checks_context_coverage(tmp_path: Path) -> None:
    output_dir = tmp_path / "reports"
    write_dataset(
        pl.DataFrame(
            [
                {"symbol": "BTCUSDT", "date": "2025-01-01", "url": "https://example.test/b.csv.gz"},
                {"symbol": "ETHUSDT", "date": "2025-01-01", "url": "https://example.test/e.csv.gz"},
            ]
        ),
        tmp_path,
        "archive_trade_manifest",
    )
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("btcusdt\n", encoding="utf-8")
    symbols = sharpe_target._symbol_filters("", str(symbols_file))
    manifest = sharpe_target.build_filtered_archive_manifest(tmp_path, start="2025-01-01", end="2025-01-02", symbols=symbols)

    assert manifest.select("symbol").to_series().to_list() == ["BTCUSDT"]
    assert sharpe_target.write_pit_symbols_file(tmp_path, output_dir, symbols=symbols) == str(output_dir / "pit_symbols.txt")
    assert (output_dir / "pit_symbols.txt").read_text(encoding="utf-8") == "BTCUSDT\n"

    hourly_rows = [
        {"ts_ms": parse_date_ms("2025-01-01") + hour * 60 * 60 * 1000, "symbol": "BTCUSDT", "close": 1.0}
        for hour in range(20)
    ]
    write_dataset(pl.DataFrame(hourly_rows), tmp_path, "open_interest")
    write_dataset(pl.DataFrame(hourly_rows), tmp_path, "premium_index_1h")
    write_dataset(
        pl.DataFrame(
            [
                {
                    **row,
                    "imbalance": 0.0,
                    "signed_quote": 0.0,
                    "total_quote": 1.0,
                    "trade_count": 1,
                }
                for row in hourly_rows
            ]
        ),
        tmp_path,
        "signed_flow_1h",
    )
    write_dataset(
        pl.DataFrame(
            [
                {"ts_ms": parse_date_ms("2025-01-01"), "symbol": "BTCUSDT", "funding_rate": 0.0},
                {"ts_ms": parse_date_ms("2025-01-01") + 16 * 60 * 60 * 1000, "symbol": "BTCUSDT", "funding_rate": 0.0},
                {"ts_ms": parse_date_ms("2025-01-02"), "symbol": "BTCUSDT", "funding_rate": 0.0},
            ]
        ),
        tmp_path,
        "funding",
    )

    context = sharpe_target.build_context_coverage_summary(tmp_path, manifest)

    assert sharpe_target.context_coverage_blockers(context) == []


def test_sharpe_target_next_commands_use_full_manifest_without_symbol_placeholder() -> None:
    commands = sharpe_target._next_commands(Path("data/volume_alpha"), "2025-05-08", "2026-05-08")
    joined = "\n".join(commands)

    assert "--symbols" not in joined
    assert "<PIT_SYMBOL_CSV>" not in joined


def test_sharpe_target_context_blockers_ignore_optional_feeds(tmp_path: Path) -> None:
    summary = {
        "min_coverage_rate": 0.95,
        "rows": [
            {"label": "funding", "required": True, "manifest_rows": 100, "covered_rows": 100, "coverage_rate": 1.0},
            {"label": "open_interest", "required": False, "manifest_rows": 100, "covered_rows": 50, "coverage_rate": 0.5},
            {"label": "signed_flow_1h", "required": True, "manifest_rows": 100, "covered_rows": 50, "coverage_rate": 0.5},
        ],
    }

    blockers = sharpe_target.context_coverage_blockers(summary)

    assert "open_interest" not in "\n".join(blockers)
    assert blockers == ["signed_flow_1h context coverage 50.00% is below 95% (50/100 symbol-date rows)"]


def test_sharpe_target_full_manifest_mode_does_not_block_on_global_context_density() -> None:
    summary = {
        "min_coverage_rate": 0.95,
        "rows": [
            {"label": "signed_flow_1h", "required": True, "manifest_rows": 100, "covered_rows": 50, "coverage_rate": 0.5},
        ],
    }

    assert sharpe_target.context_coverage_blockers(summary, require_full_manifest_rate=False) == []


def test_sharpe_target_rejects_short_dataset_coverage(tmp_path: Path) -> None:
    row = {"ts_ms": parse_date_ms("2025-05-08"), "symbol": "BTCUSDT", "date": "2025-05-08"}
    for dataset in ("klines_1m", "funding", "open_interest", "signed_flow_1h", "premium_index_1h"):
        write_dataset(pl.DataFrame([{**row, "close": 1.0}]), tmp_path, dataset)
    write_dataset(pl.DataFrame([{"symbol": "BTCUSDT"}]), tmp_path, "instruments")
    write_dataset(
        pl.DataFrame([{**row, "url": "https://example.test/2025-05-08.csv.gz"}]),
        tmp_path,
        "archive_trade_manifest",
    )

    status = sharpe_target.build_dataset_status(tmp_path)
    missing = sharpe_target.missing_required_datasets(status, start="2025-05-08", end="2026-05-08")

    assert "klines_1m coverage 2025-05-08 00:00 UTC to 2025-05-08 00:00 UTC does not span requested window" in missing
    assert "archive_trade_manifest coverage 2025-05-08 to 2025-05-08 does not span requested window" in missing


def test_sharpe_target_rejects_endpoint_only_dataset_density(tmp_path: Path) -> None:
    start_ts = parse_date_ms("2025-05-08")
    end_ts = parse_date_ms("2025-05-11") - 60_000
    rows = [
        {"ts_ms": start_ts, "symbol": "BTCUSDT", "date": "2025-05-08", "close": 1.0},
        {"ts_ms": end_ts, "symbol": "BTCUSDT", "date": "2025-05-10", "close": 1.0},
    ]
    for dataset in ("klines_1m", "funding", "open_interest", "signed_flow_1h", "premium_index_1h"):
        write_dataset(pl.DataFrame(rows), tmp_path, dataset)
    write_dataset(pl.DataFrame([{"symbol": "BTCUSDT"}]), tmp_path, "instruments")
    write_dataset(
        pl.DataFrame(
            [
                {"symbol": "BTCUSDT", "date": "2025-05-08", "url": "https://example.test/a.csv.gz"},
                {"symbol": "BTCUSDT", "date": "2025-05-10", "url": "https://example.test/b.csv.gz"},
            ]
        ),
        tmp_path,
        "archive_trade_manifest",
    )

    status = sharpe_target.build_dataset_status(tmp_path)
    missing = sharpe_target.missing_required_datasets(status, start="2025-05-08", end="2025-05-11")

    assert "klines_1m density 2/4320 minute_count is below 95%" in missing
    assert "archive_trade_manifest density 2/3 date_count is below 100%" in missing
    assert "premium density is below 90% of requested hourly slots" in missing


def test_sharpe_target_candidate_selection_requires_proof_gates() -> None:
    valid = {
        "sharpe_like": 2.1,
        "total_return": 0.5,
        "max_drawdown": -0.2,
        "trade_count": 100,
        "all_splits_positive": True,
        "funding_mode": "modeled",
        "max_trade_notional_pct_of_day_turnover": 0.002,
        "market_impact_bps_per_1pct_turnover": 1.0,
        "post_twap_exit_le16": 10,
    }
    bad_cluster = {**valid, "grid_id": "bad_cluster", "post_twap_exit_le16": 80}
    bad_splits = {**valid, "grid_id": "bad_splits", "all_splits_positive": False}
    bad_context = {**valid, "grid_id": "bad_context", "score": "context_fade_score", "all_context_rate": 0.5}
    good_context = {
        **valid,
        "grid_id": "good_context",
        "score": "context_fade_score",
        "funding_context_rate": 1.0,
        "open_interest_context_rate": 1.0,
        "premium_context_rate": 1.0,
        "trade_flow_context_rate": 1.0,
        "all_context_rate": 1.0,
    }
    good = {**valid, "grid_id": "good"}

    candidates = sharpe_target.select_candidates(
        [bad_cluster, bad_splits, bad_context, good_context, good],
        target_sharpe=2.0,
        max_drawdown=-0.35,
        min_trades=50,
    )

    assert [row["grid_id"] for row in candidates] == ["good_context", "good"]


def test_sharpe_target_blocks_low_archive_manifest_coverage() -> None:
    blockers = sharpe_target.archive_coverage_blockers(
        {
            "manifest_rows": 100,
            "covered_rows": 80,
            "usable_rows": 70,
            "coverage_rate": 0.80,
            "usable_rate": 0.70,
        }
    )

    assert "archive PIT processed coverage 80.00% is below 95% (80/100 processed rows)" in blockers
    assert "archive PIT usable coverage 70.00% is below 95% (70/100 close-fade usable rows)" in blockers


def test_sharpe_target_full_manifest_mode_still_blocks_low_processed_coverage() -> None:
    blockers = sharpe_target.archive_coverage_blockers(
        {
            "manifest_rows": 100,
            "processed_rows": 80,
            "covered_rows": 50,
            "usable_rows": 50,
            "processed_rate": 0.80,
            "coverage_rate": 0.50,
            "usable_rate": 0.50,
        },
        require_usable_rate=False,
    )

    assert blockers == ["archive PIT processed coverage 80.00% is below 95% (80/100 processed rows)"]
