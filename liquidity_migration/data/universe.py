from __future__ import annotations

import logging
from datetime import UTC, datetime

import polars as pl

from liquidity_migration.core.config import UniverseConfig
from liquidity_migration.core._common import MS_PER_DAY

_logger = logging.getLogger(__name__)


# Bybit returns TradFi linear perps through the same endpoint/category. Empty is
# its normal crypto label and ``innovation`` its crypto innovation-zone label;
# every other symbolType is outside the strategy domain.
CRYPTO_LINEAR_SYMBOL_TYPES: tuple[str, ...] = ("", "innovation")


def build_current_universe_table(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    *,
    universe_config: UniverseConfig,
    snapshot_ts_ms: int | None = None,
) -> pl.DataFrame:
    if instruments.is_empty() or tickers.is_empty():
        return _empty_universe_table()
    snapshot_ts_ms = snapshot_ts_ms or int(datetime.now(tz=UTC).timestamp() * 1000)
    exclude = {symbol.upper() for symbol in universe_config.exclude_symbols}
    joined = instruments.join(tickers, on="symbol", how="inner", suffix="_ticker")
    # The inner join drops any symbol present on only one side (a fresh listing
    # without a ticker row, or a throttled get_tickers), narrowing the tradable
    # universe. Log the count so a partial fetch is observable per cycle.
    if "symbol" in instruments.columns and "symbol" in tickers.columns:
        dropped = instruments.height - joined.height
        if dropped > 0:
            _logger.info(
                "universe join dropped %d instrument symbol(s) lacking a ticker row "
                "(instruments=%d, tickers=%d, joined=%d)",
                dropped,
                instruments.height,
                tickers.height,
                joined.height,
            )
    # ``contract_type`` is required, not optional: Bybit's v5 `linear` category
    # returns LinearPerpetual and dated LinearFutures, both USDT-settled, so the
    # settle_coin filter does not exclude dated futures and the contract_type
    # allow-list below is the only barrier keeping the universe perpetuals-only.
    required = {
        "symbol",
        "status",
        "settle_coin",
        "is_prelisting",
        "turnover_24h",
        "contract_type",
        "symbol_type",
    }
    missing = required - set(joined.columns)
    if missing:
        raise RuntimeError(f"Universe inputs missing required columns: {sorted(missing)}")

    normalized_symbol_type = (
        pl.col("symbol_type")
        .cast(pl.String)
        .fill_null("")
        .str.strip_chars()
        .str.to_lowercase()
    )
    filtered = joined.filter(
        (pl.col("status") == "Trading")
        & (pl.col("settle_coin") == "USDT")
        & (~pl.col("is_prelisting"))
        & (pl.col("turnover_24h").is_not_null())
        & (pl.col("turnover_24h") >= universe_config.min_turnover_24h)
        & normalized_symbol_type.is_in(list(CRYPTO_LINEAR_SYMBOL_TYPES))
    )
    if exclude:
        filtered = filtered.filter(~pl.col("symbol").is_in(sorted(exclude)))
    # Unconditional; a None contract_type is dropped with everything else not in
    # the allow-list.
    filtered = filtered.filter(pl.col("contract_type").is_in(["LinearPerpetual", "linear", "Linear"]))
    # Bybit sets a non-null, non-zero delivery_time_ms only on dated delivery
    # contracts, so this catches one whose contract_type slipped through.
    if "delivery_time_ms" in filtered.columns:
        filtered = filtered.filter(
            pl.col("delivery_time_ms").is_null() | (pl.col("delivery_time_ms") <= 0)
        )

    filtered = filtered.with_columns(
        [
            pl.lit(snapshot_ts_ms).alias("snapshot_ts_ms"),
            pl.from_epoch(pl.lit(snapshot_ts_ms), time_unit="ms").dt.strftime("%Y-%m-%d").alias("snapshot_date"),
            ((pl.lit(snapshot_ts_ms) - pl.col("launch_time_ms")) / MS_PER_DAY).alias("listing_age_days"),
        ]
    )
    # A null launch_time_ms gives a null listing_age_days, invisible to the age
    # gates (each requires is_not_null). With an age floor active such a symbol is
    # dropped, but in unlimited-universe mode no age filter runs at all, so log
    # how many unknown-age contracts entered the pool.
    if "launch_time_ms" in filtered.columns:
        null_launch = int(filtered.select(pl.col("launch_time_ms").is_null().sum()).item() or 0)
        if null_launch > 0:
            age_gated = universe_config.min_age_days > 0 or universe_config.max_age_days > 0
            _logger.info(
                "universe has %d symbol(s) with null launch_time_ms (unknown listing age); "
                "%s",
                null_launch,
                "dropped by the active age gate" if age_gated
                else "PASSED THROUGH (no age gate active in unlimited mode)",
            )
    if universe_config.min_age_days > 0:
        filtered = filtered.filter(pl.col("listing_age_days").is_not_null() & (pl.col("listing_age_days") >= universe_config.min_age_days))
    if universe_config.max_age_days > 0:
        filtered = filtered.filter(pl.col("listing_age_days").is_not_null() & (pl.col("listing_age_days") <= universe_config.max_age_days))

    ranked = filtered.sort(["turnover_24h", "symbol"], descending=[True, False]).with_row_index("liquidity_rank", offset=1)
    ranked = ranked.filter(pl.col("liquidity_rank") >= universe_config.rank_start)
    if universe_config.rank_end > 0:
        ranked = ranked.filter(pl.col("liquidity_rank") <= universe_config.rank_end)
    if universe_config.max_symbols > 0:
        ranked = ranked.head(universe_config.max_symbols)

    columns = [
        "snapshot_ts_ms",
        "snapshot_date",
        "liquidity_rank",
        "symbol",
        "turnover_24h",
        "volume_24h",
        "open_interest",
        "open_interest_value",
        "funding_rate",
        "launch_time_ms",
        "listing_age_days",
        "status",
        "contract_type",
        "symbol_type",
        "settle_coin",
        "min_notional_value",
        "tick_size",
        "qty_step",
        # Lot-size bounds the cycle's entry sizing enforces: Bybit returns
        # minOrderQty, maxOrderQty (limit), and maxMktOrderQty (market).
        "min_order_qty",
        "max_order_qty",
        "max_market_order_qty",
    ]
    return ranked.select([col for col in columns if col in ranked.columns]).sort("liquidity_rank")


def _empty_universe_table() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "snapshot_ts_ms": pl.Series([], dtype=pl.Int64),
            "snapshot_date": pl.Series([], dtype=pl.String),
            "liquidity_rank": pl.Series([], dtype=pl.UInt32),
            "symbol": pl.Series([], dtype=pl.String),
            "turnover_24h": pl.Series([], dtype=pl.Float64),
            "volume_24h": pl.Series([], dtype=pl.Float64),
            "open_interest": pl.Series([], dtype=pl.Float64),
            "open_interest_value": pl.Series([], dtype=pl.Float64),
            "funding_rate": pl.Series([], dtype=pl.Float64),
            "launch_time_ms": pl.Series([], dtype=pl.Int64),
            "listing_age_days": pl.Series([], dtype=pl.Float64),
            "symbol_type": pl.Series([], dtype=pl.String),
        }
    )
