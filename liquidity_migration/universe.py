from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitMarketData
from .config import ResearchConfig, UniverseConfig
from .downloaders import _normalize_instruments, _normalize_tickers
from .storage import write_dataset
from ._common import MS_PER_DAY, safe_name


def run_discover_universe(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    universe_config: UniverseConfig | None = None,
    name: str = "auto",
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    universe_config = universe_config or config.universe
    client = BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    instruments = _normalize_instruments(client.get_instruments_info())
    tickers = _normalize_tickers(client.get_tickers())
    table = build_current_universe_table(instruments, tickers, universe_config=universe_config)
    symbols = table["symbol"].to_list() if not table.is_empty() else []

    payload = {
        "name": name,
        "config": asdict(universe_config),
        "rows": table.height,
        "symbols": symbols,
        "symbol_csv": ",".join(symbols),
        "snapshot": datetime.now(tz=UTC).isoformat(),
        "survivorship_warning": (
            "This is a current Bybit tradable-universe snapshot. Historical backtests using this symbol list "
            "still have survivorship bias unless delisted/dead contracts are added from an archive."
        ),
        "universe": table.to_dicts(),
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(name)
    (output_dir / f"universe_{safe_name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / f"universe_{safe_name}.md").write_text(format_universe_report(payload), encoding="utf-8")
    (output_dir / f"universe_{safe_name}_symbols.txt").write_text(payload["symbol_csv"] + "\n", encoding="utf-8")
    if not table.is_empty():
        table.write_csv(output_dir / f"universe_{safe_name}.csv")
    write_dataset(table, data_root, "universe_current", partition_by=("snapshot_date",), append=False)
    return payload


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
    required = {"symbol", "status", "settle_coin", "is_prelisting", "turnover_24h"}
    missing = required - set(joined.columns)
    if missing:
        raise RuntimeError(f"Universe inputs missing required columns: {sorted(missing)}")

    filtered = joined.filter(
        (pl.col("status") == "Trading")
        & (pl.col("settle_coin") == "USDT")
        & (~pl.col("is_prelisting"))
        & (pl.col("turnover_24h").is_not_null())
        & (pl.col("turnover_24h") >= universe_config.min_turnover_24h)
    )
    if exclude:
        filtered = filtered.filter(~pl.col("symbol").is_in(sorted(exclude)))
    if "contract_type" in filtered.columns:
        filtered = filtered.filter(pl.col("contract_type").is_in(["LinearPerpetual", "linear", "Linear"]))

    filtered = filtered.with_columns(
        [
            pl.lit(snapshot_ts_ms).alias("snapshot_ts_ms"),
            pl.from_epoch(pl.lit(snapshot_ts_ms), time_unit="ms").dt.strftime("%Y-%m-%d").alias("snapshot_date"),
            ((pl.lit(snapshot_ts_ms) - pl.col("launch_time_ms")) / MS_PER_DAY).alias("listing_age_days"),
        ]
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
        "settle_coin",
        "min_notional_value",
        "tick_size",
        "qty_step",
    ]
    return ranked.select([col for col in columns if col in ranked.columns]).sort("liquidity_rank")


def format_universe_report(payload: dict[str, Any]) -> str:
    config = payload["config"]
    lines = [
        f"# Bybit Universe Snapshot: {payload['name']}",
        "",
        f"Snapshot: {payload['snapshot']}",
        f"Rows: {payload['rows']}",
        "",
        "## Filters",
        "",
        f"- Min 24h turnover: ${config['min_turnover_24h']:,.0f}",
        f"- Listing age: {_age_filter_label(config)}",
        f"- Liquidity ranks: {config['rank_start']}-{config['rank_end'] or 'all'}",
        f"- Max symbols: {config['max_symbols'] or 'unlimited'}",
        f"- Excluded symbols: {', '.join(config['exclude_symbols']) if config['exclude_symbols'] else 'none'}",
        "",
        "## Symbol CSV",
        "",
        "```text",
        payload["symbol_csv"],
        "```",
        "",
        "## Warning",
        "",
        payload["survivorship_warning"],
        "",
        "## Top Rows",
        "",
        "| Rank | Symbol | 24h Turnover | Listing Age Days | OI Value | Funding |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for row in payload["universe"][:50]:
        lines.append(
            f"| {row['liquidity_rank']} | {row['symbol']} | ${row.get('turnover_24h') or 0:,.0f} | "
            f"{(row.get('listing_age_days') or 0):.0f} | ${row.get('open_interest_value') or 0:,.0f} | "
            f"{(row.get('funding_rate') or 0):.4%} |"
        )
    lines.extend([""])
    return "\n".join(lines)


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
        }
    )


def _age_filter_label(config: dict[str, Any]) -> str:
    min_age = int(config.get("min_age_days") or 0)
    max_age = int(config.get("max_age_days") or 0)
    if min_age and max_age:
        return f"{min_age}-{max_age} days"
    if min_age:
        return f">= {min_age} days"
    if max_age:
        return f"<= {max_age} days"
    return "disabled"


def _safe_name(name: str) -> str:
    return safe_name(name)
