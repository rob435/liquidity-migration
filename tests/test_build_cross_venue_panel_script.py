"""Boundary and sharding tests for the cross-venue panel runner."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from scripts.data.build_cross_venue_panel import main, year_windows


class TestYearWindows:
    def test_a_window_inside_one_year_is_not_split(self) -> None:
        assert year_windows(dt.date(2024, 3, 1), dt.date(2024, 9, 1)) == [
            (dt.date(2024, 3, 1), dt.date(2024, 9, 1))
        ]

    def test_windows_are_half_open_and_contiguous(self) -> None:
        windows = year_windows(dt.date(2023, 6, 1), dt.date(2025, 2, 1))
        assert windows == [
            (dt.date(2023, 6, 1), dt.date(2024, 1, 1)),
            (dt.date(2024, 1, 1), dt.date(2025, 1, 1)),
            (dt.date(2025, 1, 1), dt.date(2025, 2, 1)),
        ]
        for (_, prior_end), (next_start, _) in zip(windows, windows[1:]):
            assert prior_end == next_start

    def test_exclusive_end_on_a_year_boundary_adds_no_empty_shard(self) -> None:
        assert year_windows(dt.date(2024, 1, 1), dt.date(2025, 1, 1)) == [
            (dt.date(2024, 1, 1), dt.date(2025, 1, 1))
        ]


class TestMain:
    def test_end_before_start_is_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(
            ["--start", "2024-02-01", "--end", "2024-01-01", "--out", str(tmp_path / "out")]
        )
        assert code == 2
        assert "end is exclusive" in capsys.readouterr().err

    def test_a_window_with_no_data_is_reported_not_silently_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "out"
        code = main(
            [
                "--start", "2024-01-01", "--end", "2024-02-01",
                "--out", str(out),
                "--bybit-root", str(tmp_path / "empty-bybit"),
                "--binance-root", str(tmp_path / "empty-binance"),
            ]
        )
        assert code == 0
        index = json.loads((out / "index.json").read_text())
        assert index["total_rows"] == 0
        assert [s["status"] for s in index["shards"]] == ["skipped"]
        assert "reason" in index["shards"][0]
        assert "SKIPPED" in capsys.readouterr().err

    def test_shards_are_written_per_year_with_an_index(self, tmp_path: Path) -> None:
        day = dt.date(2025, 12, 31)
        base_ms = int(dt.datetime(2025, 12, 31, tzinfo=dt.timezone.utc).timestamp() * 1000)
        bybit, binance = tmp_path / "bybit", tmp_path / "binance"

        def write(root: Path, dataset: str, on: dt.date, ts: int, cols: dict[str, object]) -> None:
            out = root / dataset / f"date={on.isoformat()}" / "symbol=AAAUSDT"
            out.mkdir(parents=True, exist_ok=True)
            pl.DataFrame([{"ts_ms": ts, "symbol": "AAAUSDT", **cols}]).write_parquet(out / "part.parquet")

        for on, ts in ((day, base_ms), (dt.date(2026, 1, 1), base_ms + 86_400_000)):
            write(bybit, "klines_1h", on, ts, {"close": 10.0, "turnover_quote": 1.0})
            write(bybit, "mark_price_1h", on, ts, {"close": 10.0})
            write(bybit, "index_price_1h", on, ts, {"close": 10.0})
            write(bybit, "premium_index_1h", on, ts, {"close": 0.0})
            write(bybit, "open_interest", on, ts, {"open_interest": 5.0})
            write(binance, "klines_1h", on, ts, {"close": 10.5, "turnover_quote": 1.0})
            write(binance, "binance_usdm_premium_index_1h", on, ts, {"close": 0.0})

        out = tmp_path / "out"
        code = main(
            [
                "--start", "2025-12-31", "--end", "2026-01-02",
                "--out", str(out),
                "--bybit-root", str(bybit),
                "--binance-root", str(binance),
            ]
        )
        assert code == 0
        index = json.loads((out / "index.json").read_text())
        assert [s["year"] for s in index["shards"]] == ["2025", "2026"]
        assert index["total_rows"] == 2
        for year in ("2025", "2026"):
            assert (out / year / "panel.parquet").is_file()
            manifest = json.loads((out / year / "manifest.json").read_text())
            assert len(manifest["panel_sha256"]) == 64

    def test_execution_delay_reaches_the_shard_manifest(self, tmp_path: Path) -> None:
        day = dt.date(2025, 5, 5)
        ts = int(dt.datetime(2025, 5, 5, tzinfo=dt.timezone.utc).timestamp() * 1000)
        bybit, binance = tmp_path / "bybit", tmp_path / "binance"
        for root, dataset, cols in (
            (bybit, "klines_1h", {"close": 1.0, "turnover_quote": 1.0}),
            (bybit, "mark_price_1h", {"close": 1.0}),
            (bybit, "index_price_1h", {"close": 1.0}),
            (bybit, "premium_index_1h", {"close": 0.0}),
            (bybit, "open_interest", {"open_interest": 1.0}),
            (binance, "klines_1h", {"close": 1.0, "turnover_quote": 1.0}),
            (binance, "binance_usdm_premium_index_1h", {"close": 0.0}),
        ):
            out_dir = root / dataset / f"date={day.isoformat()}" / "symbol=AAAUSDT"
            out_dir.mkdir(parents=True, exist_ok=True)
            pl.DataFrame([{"ts_ms": ts, "symbol": "AAAUSDT", **cols}]).write_parquet(out_dir / "part.parquet")

        out = tmp_path / "out"
        assert (
            main(
                [
                    "--start", "2025-05-05", "--end", "2025-05-06",
                    "--out", str(out),
                    "--bybit-root", str(bybit),
                    "--binance-root", str(binance),
                    "--execution-delay-ms", "30000",
                ]
            )
            == 0
        )
        manifest = json.loads((out / "2025" / "manifest.json").read_text())
        assert manifest["timing"]["execution_delay_ms"] == 30_000
        panel = pl.read_parquet(out / "2025" / "panel.parquet")
        assert (panel["decision_ts_ms"] - panel["bar_ts_ms"]).to_list() == [3_600_000 + 30_000]
