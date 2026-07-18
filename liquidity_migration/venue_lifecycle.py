"""Frozen venue-lifecycle events used by structural historical replays."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from ._common import MS_PER_HOUR

DELISTING_PROXY_METHOD = "mean_of_30_official_one_minute_index_closes"
DELISTING_PROXY_EXACTNESS = "structural_proxy_not_exact_venue_per_second_settlement_average"


@dataclass(frozen=True, slots=True)
class VenueDelistingSettlement:
    symbol: str
    effective_ts_ms: int
    dispatch_ts_ms: int
    proxy_price: float
    proxy_price_decimal: str
    announcement_published_ts_ms: int
    announcement_url: str
    announcement_uid: str
    announcement_sha256: str
    index_api_sha256: str
    index_api_canonical_sha256: str
    proxy_method: str
    proxy_exactness: str
    settlement_fee_usdt: float
    source_scope: str

    def __post_init__(self) -> None:
        normalized = self.symbol.strip().upper()
        if not normalized or normalized != self.symbol:
            raise ValueError("venue lifecycle symbol must be normalized uppercase")
        if self.effective_ts_ms <= 0:
            raise ValueError("venue lifecycle effective time must be positive")
        expected_dispatch = ((self.effective_ts_ms + MS_PER_HOUR - 1) // MS_PER_HOUR) * MS_PER_HOUR
        if self.dispatch_ts_ms != expected_dispatch:
            raise ValueError("venue lifecycle dispatch must be the effective-hour ceiling")
        if not math.isfinite(self.proxy_price) or self.proxy_price <= 0.0:
            raise ValueError("venue lifecycle proxy price must be positive and finite")
        try:
            decimal_price = Decimal(self.proxy_price_decimal)
        except InvalidOperation as exc:
            raise ValueError("venue lifecycle decimal proxy price is invalid") from exc
        if not decimal_price.is_finite() or decimal_price <= 0:
            raise ValueError("venue lifecycle decimal proxy price must be positive")
        if not math.isclose(
            float(decimal_price),
            self.proxy_price,
            rel_tol=1e-15,
            abs_tol=0.0,
        ):
            raise ValueError("venue lifecycle float and decimal proxy prices disagree")
        if not 0 < self.announcement_published_ts_ms < self.effective_ts_ms:
            raise ValueError("venue lifecycle announcement must precede the event")
        if not self.announcement_url.startswith("https://announcements.bybit.com/"):
            raise ValueError("venue lifecycle announcement must be an official Bybit URL")
        if not self.announcement_uid or not self.announcement_sha256:
            raise ValueError("venue lifecycle announcement identity is incomplete")
        if not self.index_api_sha256 or not self.index_api_canonical_sha256:
            raise ValueError("venue lifecycle index API identity is incomplete")
        if self.proxy_method != DELISTING_PROXY_METHOD:
            raise ValueError("venue lifecycle proxy method is not registered")
        if self.proxy_exactness != DELISTING_PROXY_EXACTNESS:
            raise ValueError("venue lifecycle proxy exactness label is not registered")
        if self.settlement_fee_usdt != 0.0:
            raise ValueError("structural venue lifecycle settlement fee must be zero")
        if self.source_scope != "official_bybit_announcement_and_index_price_api":
            raise ValueError("venue lifecycle source scope is not registered")


def load_venue_delisting_settlements(
    path: str | Path,
) -> tuple[VenueDelistingSettlement, ...]:
    """Load and validate the create-only lifecycle event table."""

    resolved = Path(path).expanduser().resolve(strict=True)
    frame = pl.read_parquet(resolved)
    required = {
        "symbol",
        "effective_ts_ms",
        "dispatch_ts_ms",
        "proxy_price",
        "proxy_price_decimal",
        "announcement_published_ts_ms",
        "announcement_url",
        "announcement_uid",
        "announcement_sha256",
        "index_api_sha256",
        "index_api_canonical_sha256",
        "proxy_method",
        "proxy_exactness",
        "settlement_fee_usdt",
        "source_scope",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"venue lifecycle event table is missing columns: {missing}")
    events = tuple(
        VenueDelistingSettlement(
            symbol=str(row["symbol"]),
            effective_ts_ms=int(row["effective_ts_ms"]),
            dispatch_ts_ms=int(row["dispatch_ts_ms"]),
            proxy_price=float(row["proxy_price"]),
            proxy_price_decimal=str(row["proxy_price_decimal"]),
            announcement_published_ts_ms=int(row["announcement_published_ts_ms"]),
            announcement_url=str(row["announcement_url"]),
            announcement_uid=str(row["announcement_uid"]),
            announcement_sha256=str(row["announcement_sha256"]),
            index_api_sha256=str(row["index_api_sha256"]),
            index_api_canonical_sha256=str(row["index_api_canonical_sha256"]),
            proxy_method=str(row["proxy_method"]),
            proxy_exactness=str(row["proxy_exactness"]),
            settlement_fee_usdt=float(row["settlement_fee_usdt"]),
            source_scope=str(row["source_scope"]),
        )
        for row in frame.sort(["dispatch_ts_ms", "effective_ts_ms", "symbol"]).to_dicts()
    )
    identities = [(event.symbol, event.effective_ts_ms) for event in events]
    if len(identities) != len(set(identities)):
        raise ValueError("venue lifecycle event table contains duplicate events")
    return events
