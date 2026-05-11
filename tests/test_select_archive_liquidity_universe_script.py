from __future__ import annotations

from pathlib import Path

import polars as pl

from aggression_carry.storage import write_dataset
from scripts.select_archive_liquidity_universe import ArchiveLiquidityUniverseConfig, select_archive_liquidity_universe


def test_select_archive_liquidity_universe_ranks_start_date_content_length(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {"symbol": "AAAUSDT", "date": "2025-05-08", "url": "https://example.test/a.csv.gz"},
                {"symbol": "BBBUSDT", "date": "2025-05-08", "url": "https://example.test/b.csv.gz"},
                {"symbol": "BTCUSDT", "date": "2025-05-08", "url": "https://example.test/btc.csv.gz"},
                {"symbol": "CCCUSDT", "date": "2025-05-09", "url": "https://example.test/c.csv.gz"},
            ]
        ),
        tmp_path,
        "archive_trade_manifest",
        partition_by=("date",),
    )

    lengths = {
        "https://example.test/a.csv.gz": 100,
        "https://example.test/b.csv.gz": 300,
        "https://example.test/btc.csv.gz": 1000,
    }
    payload = select_archive_liquidity_universe(
        tmp_path,
        config=ArchiveLiquidityUniverseConfig(start="2025-05-08", top_n=2, exclude_symbols=("BTCUSDT",), workers=2),
        report_dir=tmp_path / "reports",
        content_length_func=lambda url: lengths[url],
    )

    assert payload["symbols"] == ["BBBUSDT", "AAAUSDT"]
    assert (tmp_path / "reports" / "archive_liquidity_universe_symbols.txt").read_text(encoding="utf-8") == "BBBUSDT,AAAUSDT\n"
