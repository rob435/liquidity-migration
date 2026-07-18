from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.venue_lifecycle import (
    DELISTING_PROXY_EXACTNESS,
    DELISTING_PROXY_METHOD,
    load_venue_delisting_settlements,
)


def _row(*, symbol: str = "AUSDT", effective_ts_ms: int = 7_200_000) -> dict[str, object]:
    return {
        "symbol": symbol,
        "effective_ts_ms": effective_ts_ms,
        "dispatch_ts_ms": effective_ts_ms,
        "proxy_price": 10.0,
        "proxy_price_decimal": "10.000000000000000000",
        "announcement_published_ts_ms": effective_ts_ms - 3_600_000,
        "announcement_url": ("https://announcements.bybit.com/en/article/test-bltabc/"),
        "announcement_uid": "bltabc",
        "announcement_sha256": "a" * 64,
        "index_api_sha256": "b" * 64,
        "index_api_canonical_sha256": "c" * 64,
        "proxy_method": DELISTING_PROXY_METHOD,
        "proxy_exactness": DELISTING_PROXY_EXACTNESS,
        "settlement_fee_usdt": 0.0,
        "source_scope": "official_bybit_announcement_and_index_price_api",
    }


def test_loader_sorts_and_validates_frozen_delisting_events(tmp_path: Path) -> None:
    path = tmp_path / "events.parquet"
    pl.from_dicts(
        [
            _row(symbol="BUSDT", effective_ts_ms=10_800_000),
            _row(),
        ]
    ).write_parquet(path)

    events = load_venue_delisting_settlements(path)

    assert [event.symbol for event in events] == ["AUSDT", "BUSDT"]
    assert events[0].proxy_price_decimal == "10.000000000000000000"
    assert events[0].settlement_fee_usdt == 0.0


def test_loader_rejects_duplicate_event_identity(tmp_path: Path) -> None:
    path = tmp_path / "events.parquet"
    pl.from_dicts([_row(), _row()]).write_parquet(path)

    with pytest.raises(ValueError, match="duplicate events"):
        load_venue_delisting_settlements(path)


def test_loader_rejects_nonzero_structural_settlement_fee(tmp_path: Path) -> None:
    path = tmp_path / "events.parquet"
    row = {**_row(), "settlement_fee_usdt": 0.01}
    pl.from_dicts([row]).write_parquet(path)

    with pytest.raises(ValueError, match="settlement fee must be zero"):
        load_venue_delisting_settlements(path)
