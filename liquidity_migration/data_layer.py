from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from ._common import date_ms, parse_date, pct
from .storage import dataset_path


NATIVE_AUX_DATASETS = (
    "klines_1h",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    "signed_flow_1h",
)
BINANCE_PROXY_DATASETS = (
    "binance_usdm_klines_1h",
    "binance_usdm_funding",
    "binance_usdm_open_interest",
    "binance_usdm_mark_price_1h",
    "binance_usdm_index_price_1h",
    "binance_usdm_premium_index_1h",
    "binance_usdm_taker_flow_1h",
)
DEFAULT_DATA_LAYER_DATASETS = NATIVE_AUX_DATASETS + BINANCE_PROXY_DATASETS
HOURLY_DATASETS = {
    "klines_1h",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    "signed_flow_1h",
    "binance_usdm_klines_1h",
    "binance_usdm_open_interest",
    "binance_usdm_mark_price_1h",
    "binance_usdm_index_price_1h",
    "binance_usdm_premium_index_1h",
    "binance_usdm_taker_flow_1h",
}
MAX_EXACT_PARTITION_METADATA_FILES = 10_000
FEATURE_PACKS = {
    "native_basis_funding": ("klines_1h", "funding", "mark_price_1h", "index_price_1h", "premium_index_1h"),
    "native_leverage_flow": ("klines_1h", "open_interest", "signed_flow_1h"),
    "native_full_aux": NATIVE_AUX_DATASETS,
    "binance_basis_funding_proxy": (
        "binance_usdm_klines_1h",
        "binance_usdm_funding",
        "binance_usdm_mark_price_1h",
        "binance_usdm_index_price_1h",
        "binance_usdm_premium_index_1h",
    ),
    "binance_recent_leverage_flow_proxy": (
        "binance_usdm_klines_1h",
        "binance_usdm_open_interest",
        "binance_usdm_taker_flow_1h",
    ),
}


@dataclass(frozen=True, slots=True)
class DataLayerAuditConfig:
    name: str = "serious_data_layer"
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    datasets: tuple[str, ...] = DEFAULT_DATA_LAYER_DATASETS
    min_full_coverage: float = 0.95
    output_dir: str | Path | None = None


@dataclass(frozen=True, slots=True)
class DatasetCoverageSnapshot:
    dataset: str
    rows: int
    symbols: int
    min_ts_ms: int | None
    max_ts_ms: int | None
    min_date: str
    max_date: str
    pairs: pl.DataFrame
    row_count_estimated: bool = False


def run_data_layer_audit(data_root: str | Path, *, config: DataLayerAuditConfig | None = None) -> dict[str, Any]:
    cfg = config or DataLayerAuditConfig()
    root = Path(data_root).expanduser()
    output_dir = Path(cfg.output_dir).expanduser() if cfg.output_dir else root / "reports" / f"data_layer_{cfg.name}"
    output_dir.mkdir(parents=True, exist_ok=True)

    start_date = _parse_date(cfg.start)
    end_exclusive = _parse_date(cfg.end)
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in cfg.symbols))

    klines_snapshot = _load_coverage_snapshot(root, "klines_1h", start_date=start_date, end_exclusive=end_exclusive, symbols=symbols)
    reference_pairs = _reference_pairs(klines_snapshot, start_date=start_date, end_exclusive=end_exclusive, symbols=symbols)
    reference_pair_count = int(reference_pairs.height)
    rows: list[dict[str, Any]] = []
    pairs_by_dataset: dict[str, pl.DataFrame] = {}

    for dataset in cfg.datasets:
        snapshot = (
            klines_snapshot
            if dataset == "klines_1h"
            else _load_coverage_snapshot(root, dataset, start_date=start_date, end_exclusive=end_exclusive, symbols=symbols)
        )
        pairs_by_dataset[dataset] = snapshot.pairs
        rows.append(
            _dataset_row(
                dataset,
                snapshot,
                reference_pairs=reference_pairs,
                reference_pair_count=reference_pair_count,
                start_date=start_date,
                end_exclusive=end_exclusive,
                min_full_coverage=cfg.min_full_coverage,
            )
        )

    intersections = [
        _intersection_row(
            name,
            datasets,
            pairs_by_dataset,
            reference_pair_count=reference_pair_count,
            min_full_coverage=cfg.min_full_coverage,
        )
        for name, datasets in FEATURE_PACKS.items()
        if any(dataset in cfg.datasets for dataset in datasets)
    ]

    coverage = pl.DataFrame(rows, infer_schema_length=None)
    intersection_df = pl.DataFrame(intersections, infer_schema_length=None)
    coverage_path = output_dir / "data_layer_coverage.csv"
    intersection_path = output_dir / "data_layer_intersections.csv"
    markdown_path = output_dir / "data_layer_audit.md"
    json_path = output_dir / "data_layer_audit.json"
    coverage.write_csv(coverage_path)
    intersection_df.write_csv(intersection_path)
    payload = {
        "data_root": str(root),
        "name": cfg.name,
        "start": start_date.isoformat() if start_date else None,
        "end_exclusive": end_exclusive.isoformat() if end_exclusive else None,
        "symbols": list(symbols),
        "reference_pair_count": reference_pair_count,
        "min_full_coverage": cfg.min_full_coverage,
        "coverage": coverage.to_dicts(),
        "intersections": intersection_df.to_dicts(),
        "output_files": {
            "coverage": str(coverage_path),
            "intersections": str(intersection_path),
            "markdown": str(markdown_path),
            "json": str(json_path),
        },
    }
    markdown_path.write_text(format_data_layer_audit(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def format_data_layer_audit(payload: dict[str, Any]) -> str:
    lines = [
        "# Data Layer Audit",
        "",
        f"- Data root: `{payload['data_root']}`",
        f"- Window: `{payload.get('start') or 'dataset-min'}` to `{payload.get('end_exclusive') or 'dataset-max'}` end-exclusive",
        f"- Symbols: `{len(payload['symbols'])}` explicit filters" if payload["symbols"] else "- Symbols: all symbols present in datasets",
        f"- Reference symbol-days: {payload['reference_pair_count']}",
        f"- Full-window coverage threshold: {_pct(payload['min_full_coverage'])}",
        "",
        "This report separates Bybit-native datasets from Binance USD-M proxy datasets. Proxy coverage is useful for feature discovery, but it is not promotion-grade Bybit PIT evidence.",
        "",
        "## Dataset Coverage",
        "",
        "| Dataset | Source | Status | Rows | Symbols | Dates | Symbol-Day Coverage | Bar Coverage | Notes |",
        "|---|---|---|---:|---:|---|---:|---:|---|",
    ]
    for row in payload["coverage"]:
        lines.append(
            f"| `{row['dataset']}` | {row['source_tier']} | {row['status']} | {int(row['rows'])} | "
            f"{int(row['symbols'])} | {_date_span(row)} | {_pct(row['symbol_day_coverage'])} | "
            f"{_pct(row.get('bar_coverage'))} | {row.get('notes') or ''} |"
        )
    lines.extend(
        [
            "",
            "## Usable Feature Windows",
            "",
            "| Feature Pack | Datasets | Pair Coverage | Pairs | Dates | Symbols | Promotion Use |",
            "|---|---|---:|---:|---|---:|---|",
        ]
    )
    for row in payload["intersections"]:
        lines.append(
            f"| `{row['feature_pack']}` | `{row['datasets']}` | {_pct(row['pair_coverage'])} | {int(row['pairs'])} | "
            f"{_date_span(row)} | {int(row['symbols'])} | {row['promotion_use']} |"
        )
    lines.extend(
        [
            "",
            "## Rules",
            "",
            "- `NATIVE_FULL` can support Model Court promotion tests if the strategy run also passes costs, funding, slippage, drift, and negative controls.",
            "- `NATIVE_PARTIAL` is allowed for exploratory tests only; reports must state the exact window used.",
            "- `PROXY_*` is Binance USD-M research support. It can suggest a feature, but cannot by itself prove a Bybit demo strategy.",
            "- Binance open-interest and taker-flow REST history are recent-window only per Binance docs, so they should be treated as live calibration / short-window validation unless separately archived.",
            "",
        ]
    )
    return "\n".join(lines)


def _dataset_row(
    dataset: str,
    snapshot: DatasetCoverageSnapshot,
    *,
    reference_pairs: pl.DataFrame,
    reference_pair_count: int,
    start_date: date | None,
    end_exclusive: date | None,
    min_full_coverage: float,
) -> dict[str, Any]:
    source_tier = "binance_proxy" if dataset.startswith("binance_usdm_") else "bybit_native"
    row_count = snapshot.rows
    symbol_count = snapshot.symbols
    min_date = snapshot.min_date
    max_date = snapshot.max_date
    covered_pairs = _covered_pairs(snapshot.pairs, reference_pairs) if reference_pair_count else int(snapshot.pairs.height)
    pair_coverage = covered_pairs / reference_pair_count if reference_pair_count else 0.0
    expected_bars = reference_pair_count * 24 if dataset in HOURLY_DATASETS else 0
    bar_coverage = min(row_count / expected_bars, 1.0) if expected_bars else None
    span_ok = _span_covers(min_date, max_date, start_date=start_date, end_exclusive=end_exclusive)
    if row_count == 0:
        status = "MISSING"
    elif source_tier == "binance_proxy":
        status = "PROXY_FULL" if pair_coverage >= min_full_coverage and span_ok else "PROXY_PARTIAL"
    else:
        status = "NATIVE_FULL" if pair_coverage >= min_full_coverage and span_ok else "NATIVE_PARTIAL"
    notes = _dataset_notes(dataset)
    if snapshot.row_count_estimated:
        notes = "; ".join(item for item in (notes, "row count estimated from partition coverage for speed") if item)
    return {
        "dataset": dataset,
        "source_tier": source_tier,
        "status": status,
        "rows": row_count,
        "symbols": symbol_count,
        "min_ts_ms": snapshot.min_ts_ms,
        "max_ts_ms": snapshot.max_ts_ms,
        "min_date": min_date,
        "max_date": max_date,
        "symbol_day_pairs": int(snapshot.pairs.height),
        "covered_reference_pairs": covered_pairs,
        "reference_pairs": reference_pair_count,
        "symbol_day_coverage": pair_coverage,
        "bar_coverage": bar_coverage,
        "notes": notes,
    }


def _intersection_row(
    name: str,
    datasets: tuple[str, ...],
    pairs_by_dataset: dict[str, pl.DataFrame],
    *,
    reference_pair_count: int,
    min_full_coverage: float,
) -> dict[str, Any]:
    present = [dataset for dataset in datasets if dataset in pairs_by_dataset]
    missing = [dataset for dataset in datasets if dataset not in pairs_by_dataset or pairs_by_dataset[dataset].is_empty()]
    current: pl.DataFrame | None = None
    for dataset in present:
        pairs = pairs_by_dataset[dataset]
        current = pairs if current is None else current.join(pairs, on=["symbol", "date"], how="inner")
    current = current if current is not None else pl.DataFrame({"symbol": [], "date": []}, schema={"symbol": pl.Utf8, "date": pl.Utf8})
    pair_count = int(current.height)
    coverage = pair_count / reference_pair_count if reference_pair_count else 0.0
    source = "proxy" if any(dataset.startswith("binance_usdm_") for dataset in datasets) else "native"
    if source == "proxy":
        promotion_use = "exploratory proxy only"
    elif missing:
        promotion_use = "blocked: missing " + ",".join(missing)
    elif coverage < min_full_coverage:
        promotion_use = "exploratory partial native window only"
    else:
        promotion_use = "eligible if Model Court passes"
    return {
        "feature_pack": name,
        "datasets": ",".join(datasets),
        "missing_datasets": ",".join(missing),
        "pairs": pair_count,
        "pair_coverage": coverage,
        "symbols": int(current["symbol"].n_unique()) if pair_count else 0,
        "min_date": str(current["date"].min()) if pair_count else "",
        "max_date": str(current["date"].max()) if pair_count else "",
        "promotion_use": promotion_use,
    }


def _reference_pairs(
    klines_snapshot: DatasetCoverageSnapshot,
    *,
    start_date: date | None,
    end_exclusive: date | None,
    symbols: tuple[str, ...],
) -> pl.DataFrame:
    pairs = klines_snapshot.pairs
    if not pairs.is_empty():
        return pairs
    if symbols and start_date and end_exclusive and end_exclusive > start_date:
        rows = [{"symbol": symbol, "date": day.isoformat()} for symbol in symbols for day in _date_range(start_date, end_exclusive)]
        return pl.DataFrame(rows, schema={"symbol": pl.Utf8, "date": pl.Utf8}) if rows else _empty_pairs()
    return _empty_pairs()


def _load_coverage_snapshot(
    root: Path,
    dataset: str,
    *,
    start_date: date | None,
    end_exclusive: date | None,
    symbols: tuple[str, ...],
) -> DatasetCoverageSnapshot:
    path = dataset_path(root, dataset)
    if not path.exists():
        return _empty_snapshot(dataset)
    partition_snapshot = _partition_coverage_snapshot(
        dataset,
        path,
        start_date=start_date,
        end_exclusive=end_exclusive,
        symbols=symbols,
    )
    if partition_snapshot is not None:
        return partition_snapshot
    files = sorted(path.glob("**/*.parquet"))
    if not files:
        return _empty_snapshot(dataset)
    lf = pl.scan_parquet([str(file) for file in files])
    schema = lf.collect_schema()
    columns = set(schema.names())
    if "date" not in columns and "ts_ms" in columns:
        lf = lf.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms")
            .dt.strftime("%Y-%m-%d")
            .alias("date")
        )
        columns.add("date")
    if "date" in columns:
        lf = lf.with_columns(pl.col("date").cast(pl.Utf8))
    if symbols and "symbol" in columns:
        lf = lf.filter(pl.col("symbol").is_in(list(symbols)))
    if "date" in columns:
        if start_date:
            lf = lf.filter(pl.col("date") >= start_date.isoformat())
        if end_exclusive:
            lf = lf.filter(pl.col("date") < end_exclusive.isoformat())
    stats_exprs = [pl.len().alias("rows")]
    stats_exprs.append(pl.col("symbol").n_unique().alias("symbols") if "symbol" in columns else pl.lit(0).alias("symbols"))
    stats_exprs.append(pl.col("ts_ms").min().alias("min_ts_ms") if "ts_ms" in columns else pl.lit(None).alias("min_ts_ms"))
    stats_exprs.append(pl.col("ts_ms").max().alias("max_ts_ms") if "ts_ms" in columns else pl.lit(None).alias("max_ts_ms"))
    stats_exprs.append(pl.col("date").min().alias("min_date") if "date" in columns else pl.lit("").alias("min_date"))
    stats_exprs.append(pl.col("date").max().alias("max_date") if "date" in columns else pl.lit("").alias("max_date"))
    stats = lf.select(stats_exprs).collect().to_dicts()[0]
    pairs = (
        lf.select(["symbol", "date"]).drop_nulls().unique().sort(["symbol", "date"]).collect()
        if {"symbol", "date"}.issubset(columns)
        else _empty_pairs()
    )
    return DatasetCoverageSnapshot(
        dataset=dataset,
        rows=int(stats["rows"] or 0),
        symbols=int(stats["symbols"] or 0),
        min_ts_ms=int(stats["min_ts_ms"]) if stats["min_ts_ms"] is not None else None,
        max_ts_ms=int(stats["max_ts_ms"]) if stats["max_ts_ms"] is not None else None,
        min_date=str(stats["min_date"] or ""),
        max_date=str(stats["max_date"] or ""),
        pairs=pairs,
    )


def _partition_coverage_snapshot(
    dataset: str,
    root: Path,
    *,
    start_date: date | None,
    end_exclusive: date | None,
    symbols: tuple[str, ...],
) -> DatasetCoverageSnapshot | None:
    pairs: set[tuple[str, str]] = set()
    partition_files: list[Path] = []
    row_count = 0
    min_ts: int | None = None
    max_ts: int | None = None
    symbol_filter = set(symbols)
    saw_partition = False
    date_dirs = [item for item in root.iterdir() if item.is_dir() and item.name.startswith("date=")]
    if not date_dirs:
        return None
    for date_dir in date_dirs:
        day = date_dir.name.split("=", 1)[1]
        if not day:
            continue
        saw_partition = True
        if start_date and day < start_date.isoformat():
            continue
        if end_exclusive and day >= end_exclusive.isoformat():
            continue
        for symbol_dir in date_dir.iterdir():
            if not symbol_dir.is_dir() or not symbol_dir.name.startswith("symbol="):
                continue
            symbol = symbol_dir.name.split("=", 1)[1]
            if symbol_filter and symbol not in symbol_filter:
                continue
            file = symbol_dir / "part.parquet"
            if not file.exists():
                parquet_files = list(symbol_dir.glob("*.parquet"))
                if not parquet_files:
                    continue
                file = parquet_files[0]
            pairs.add((symbol, day))
            partition_files.append(file)
    if not saw_partition:
        return None
    estimate_rows = len(partition_files) > MAX_EXACT_PARTITION_METADATA_FILES
    if estimate_rows:
        row_count = len(partition_files) * _estimated_partition_rows(dataset)
        if pairs:
            min_day = min(day for _, day in pairs)
            max_day = max(day for _, day in pairs)
            min_ts = _date_start_ms(min_day)
            max_ts = _date_start_ms(max_day) + 24 * 60 * 60_000 - 1
    else:
        for file in partition_files:
            parquet_file = pq.ParquetFile(file)
            metadata = parquet_file.metadata
            row_count += int(metadata.num_rows)
            file_min, file_max = _parquet_ts_bounds(parquet_file)
            if file_min is not None:
                min_ts = file_min if min_ts is None else min(min_ts, file_min)
            if file_max is not None:
                max_ts = file_max if max_ts is None else max(max_ts, file_max)
    pair_rows = [{"symbol": symbol, "date": day} for symbol, day in sorted(pairs)]
    pair_frame = pl.DataFrame(pair_rows, schema={"symbol": pl.Utf8, "date": pl.Utf8}) if pair_rows else _empty_pairs()
    dates = [day for _, day in pairs]
    return DatasetCoverageSnapshot(
        dataset=dataset,
        rows=row_count,
        symbols=len({symbol for symbol, _ in pairs}),
        min_ts_ms=min_ts,
        max_ts_ms=max_ts,
        min_date=min(dates) if dates else "",
        max_date=max(dates) if dates else "",
        pairs=pair_frame,
        row_count_estimated=estimate_rows,
    )


def _parquet_ts_bounds(parquet_file: pq.ParquetFile) -> tuple[int | None, int | None]:
    try:
        schema_names = parquet_file.schema_arrow.names
        ts_index = schema_names.index("ts_ms")
    except (ValueError, OSError):
        return None, None
    metadata = parquet_file.metadata
    min_ts: int | None = None
    max_ts: int | None = None
    for row_group_index in range(metadata.num_row_groups):
        stats = metadata.row_group(row_group_index).column(ts_index).statistics
        if stats is None:
            continue
        if stats.min is not None:
            value = int(stats.min)
            min_ts = value if min_ts is None else min(min_ts, value)
        if stats.max is not None:
            value = int(stats.max)
            max_ts = value if max_ts is None else max(max_ts, value)
    return min_ts, max_ts


def _estimated_partition_rows(dataset: str) -> int:
    if dataset in HOURLY_DATASETS:
        return 24
    if dataset == "funding" or dataset.endswith("_funding"):
        return 3
    return 1


def _date_start_ms(value: str) -> int:
    return date_ms(value)


def _empty_snapshot(dataset: str) -> DatasetCoverageSnapshot:
    return DatasetCoverageSnapshot(
        dataset=dataset,
        rows=0,
        symbols=0,
        min_ts_ms=None,
        max_ts_ms=None,
        min_date="",
        max_date="",
        pairs=_empty_pairs(),
    )


def _covered_pairs(pairs: pl.DataFrame, reference_pairs: pl.DataFrame) -> int:
    if pairs.is_empty() or reference_pairs.is_empty():
        return 0
    return int(pairs.join(reference_pairs, on=["symbol", "date"], how="inner").height)


def _empty_pairs() -> pl.DataFrame:
    return pl.DataFrame({"symbol": [], "date": []}, schema={"symbol": pl.Utf8, "date": pl.Utf8})


def _parse_date(value: str | None) -> date | None:
    return parse_date(value)


def _date_range(start: date, end_exclusive: date) -> list[date]:
    output = []
    current = start
    while current < end_exclusive:
        output.append(current)
        current += timedelta(days=1)
    return output


def _span_covers(min_date: str, max_date: str, *, start_date: date | None, end_exclusive: date | None) -> bool:
    if not min_date or not max_date:
        return False
    if start_date and min_date > start_date.isoformat():
        return False
    if end_exclusive:
        last_required = (end_exclusive - timedelta(days=1)).isoformat()
        if max_date < last_required:
            return False
    return True


def _dataset_notes(dataset: str) -> str:
    if dataset in {"binance_usdm_open_interest", "binance_usdm_taker_flow_1h"}:
        return "Binance REST history is recent-window only; archive separately for long tests"
    if dataset.startswith("binance_usdm_"):
        return "Binance USD-M proxy; do not treat as Bybit-native evidence"
    if dataset == "signed_flow_1h":
        return "Requires public trade archive ingestion or recent-trade snapshots"
    return ""


def _date_span(row: dict[str, Any]) -> str:
    min_date = row.get("min_date") or ""
    max_date = row.get("max_date") or ""
    return f"{min_date}..{max_date}" if min_date or max_date else ""


def _pct(value: Any) -> str:
    return pct(value)
