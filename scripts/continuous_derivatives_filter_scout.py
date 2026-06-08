#!/usr/bin/env python3
"""Filter merged continuous-fade trades with causal derivatives-positioning data."""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.continuous_events import _daily_pnl_metrics, _portfolio_mtm_equity  # noqa: E402
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    decompose_continuous_components,
    rebalance_rule_id,
)
from liquidity_migration.signal_harness import _autodetect_dataset_names, _date_str_to_ms, _read_window  # noqa: E402

VENUES = ("bybit", "binance")
FEATURE_WINDOWS = (24, 72)

DERIVATIVE_DATASETS = {
    "bybit": {
        "funding": "funding",
        "premium": "premium_index_1h",
        "mark": "mark_price_1h",
        "index": "index_price_1h",
    },
    "binance": {
        "funding": "binance_usdm_funding",
        "premium": "binance_usdm_premium_index_1h",
        "mark": "binance_usdm_mark_price_1h",
        "index": "binance_usdm_index_price_1h",
    },
}


@dataclass(frozen=True)
class NamedRule:
    name: str
    rule: ContinuousRebalanceRule


RISK_RULES = (
    NamedRule(
        "base",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.025,
            max_scale=4.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    NamedRule(
        "mid",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.035,
            max_scale=6.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    NamedRule(
        "high",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=8.0,
            drawdown_half_threshold=-0.05,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
    NamedRule(
        "tight",
        ContinuousRebalanceRule(
            realized_vol_window_days=90,
            target_daily_vol=0.045,
            max_scale=8.0,
            drawdown_half_threshold=-0.04,
            drawdown_zero_threshold=None,
            resize_cost_bps=10.0,
            strategy_momentum_window_days=0,
        ),
    ),
)


def _finite(value: Any) -> bool:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(x)


def _ge0(value: Any) -> bool:
    return _finite(value) and float(value) >= 0.0


def _lt0(value: Any) -> bool:
    return _finite(value) and float(value) < 0.0


FilterPredicate = Callable[[dict[str, Any]], bool]


FILTERS: tuple[tuple[str, FilterPredicate], ...] = (
    ("all", lambda row: True),
    ("funding_24h_ge0", lambda row: _ge0(row.get("funding_24h_sum"))),
    ("funding_24h_lt0", lambda row: _lt0(row.get("funding_24h_sum"))),
    ("premium_24h_ge0", lambda row: _ge0(row.get("premium_24h_mean"))),
    ("premium_24h_lt0", lambda row: _lt0(row.get("premium_24h_mean"))),
    ("basis_24h_ge0", lambda row: _ge0(row.get("basis_24h_mean"))),
    ("basis_24h_lt0", lambda row: _lt0(row.get("basis_24h_mean"))),
    (
        "funding_premium_24h_ge0",
        lambda row: _ge0(row.get("funding_24h_sum")) and _ge0(row.get("premium_24h_mean")),
    ),
    (
        "premium_basis_24h_ge0",
        lambda row: _ge0(row.get("premium_24h_mean")) and _ge0(row.get("basis_24h_mean")),
    ),
    (
        "all_three_24h_ge0",
        lambda row: (
            _ge0(row.get("funding_24h_sum"))
            and _ge0(row.get("premium_24h_mean"))
            and _ge0(row.get("basis_24h_mean"))
        ),
    ),
    (
        "not_all_three_24h_ge0",
        lambda row: not (
            _ge0(row.get("funding_24h_sum"))
            and _ge0(row.get("premium_24h_mean"))
            and _ge0(row.get("basis_24h_mean"))
        ),
    ),
)


def _parse_mapping(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"expected venue=path mapping, got {item!r}")
        venue, path = item.split("=", 1)
        venue = venue.strip()
        if venue not in VENUES:
            raise ValueError(f"unknown venue in data roots: {venue}")
        out[venue] = Path(path.strip()).expanduser()
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _date_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _entry_signal_col(trades: pl.DataFrame) -> str | None:
    if "entry_signal_ts_ms" in trades.columns:
        return "entry_signal_ts_ms"
    if "signal_ts_ms" in trades.columns:
        return "signal_ts_ms"
    return None


def _trades_with_signal_ts(trades: pl.DataFrame) -> pl.DataFrame:
    signal_col = _entry_signal_col(trades)
    if signal_col is not None:
        return trades.with_columns(pl.col(signal_col).cast(pl.Int64).alias("_signal_ts_ms"))
    return trades.with_columns((pl.col("entry_ts_ms").cast(pl.Int64) - MS_PER_HOUR).alias("_signal_ts_ms"))


def _needed_date_symbol_pairs(trades: pl.DataFrame, *, lookback_days: int = 4) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for signal_ts, symbol in trades.select("_signal_ts_ms", "symbol").iter_rows():
        day = int(signal_ts) // MS_PER_DAY * MS_PER_DAY
        for offset in range(lookback_days + 1):
            pairs.add((_date_from_ms(day - offset * MS_PER_DAY), str(symbol)))
    return pairs


def _partition_files(root: Path, dataset: str, pairs: set[tuple[str, str]]) -> list[Path]:
    base = root / dataset
    files: list[Path] = []
    for date, symbol in sorted(pairs):
        p = base / f"date={date}" / f"symbol={symbol}" / "part.parquet"
        if p.exists():
            files.append(p)
    return files


def _read_partition_subset(root: Path, dataset: str, pairs: set[tuple[str, str]]) -> pl.DataFrame:
    files = _partition_files(root, dataset, pairs)
    if not files:
        return pl.DataFrame()
    return pl.read_parquet([str(p) for p in files])


def _series_frame(df: pl.DataFrame, *, value_col: str, out_col: str) -> pl.DataFrame:
    if df.is_empty() or value_col not in df.columns:
        return pl.DataFrame({"symbol": [], "ts_ms": [], out_col: []})
    return (
        df.select(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("ts_ms").cast(pl.Int64),
            pl.col(value_col).cast(pl.Float64).alias(out_col),
        )
        .drop_nulls(["symbol", "ts_ms", out_col])
        .unique(["symbol", "ts_ms"], keep="last")
        .sort(["symbol", "ts_ms"])
    )


def _load_feature_series(root: Path, venue: str, trades: pl.DataFrame) -> dict[str, pl.DataFrame]:
    datasets = DERIVATIVE_DATASETS[venue]
    pairs = _needed_date_symbol_pairs(trades)

    funding_raw = _read_partition_subset(root, datasets["funding"], pairs)
    funding_col = (
        "funding_rate_8h_equiv"
        if "funding_rate_8h_equiv" in funding_raw.columns
        else "funding_rate"
    )
    funding = _series_frame(funding_raw, value_col=funding_col, out_col="funding")

    premium_raw = _read_partition_subset(root, datasets["premium"], pairs)
    premium = _series_frame(premium_raw, value_col="close", out_col="premium")

    mark_raw = _read_partition_subset(root, datasets["mark"], pairs)
    index_raw = _read_partition_subset(root, datasets["index"], pairs)
    mark = _series_frame(mark_raw, value_col="close", out_col="mark")
    index = _series_frame(index_raw, value_col="close", out_col="index")
    if mark.is_empty() or index.is_empty():
        basis = pl.DataFrame({"symbol": [], "ts_ms": [], "basis": []})
    else:
        basis = (
            mark.join(index, on=["symbol", "ts_ms"], how="inner")
            .filter(pl.col("index") > 0)
            .with_columns((pl.col("mark") / pl.col("index") - 1.0).alias("basis"))
            .select("symbol", "ts_ms", "basis")
            .sort(["symbol", "ts_ms"])
        )
    return {"funding": funding, "premium": premium, "basis": basis}


def _build_prefix_index(series: pl.DataFrame, value_col: str) -> dict[str, tuple[list[int], list[float]]]:
    out: dict[str, tuple[list[int], list[float]]] = {}
    if series.is_empty():
        return out
    for symbol, group in series.group_by("symbol", maintain_order=True):
        symbol_key = str(symbol[0] if isinstance(symbol, tuple) else symbol)
        ordered = group.sort("ts_ms")
        ts = [int(x) for x in ordered["ts_ms"].to_list()]
        values = [float(x) for x in ordered[value_col].to_list()]
        prefix = [0.0]
        for value in values:
            prefix.append(prefix[-1] + value)
        out[symbol_key] = (ts, prefix)
    return out


def _window_stat(
    index: dict[str, tuple[list[int], list[float]]],
    symbol: str,
    signal_ts_ms: int,
    window_hours: int,
    *,
    stat: str,
) -> float | None:
    item = index.get(symbol)
    if item is None:
        return None
    ts, prefix = item
    lo = bisect.bisect_right(ts, signal_ts_ms - window_hours * MS_PER_HOUR)
    hi = bisect.bisect_right(ts, signal_ts_ms)
    n = hi - lo
    if n <= 0:
        return None
    total = prefix[hi] - prefix[lo]
    if stat == "sum":
        return total
    if stat == "mean":
        return total / n
    raise ValueError(f"unknown stat: {stat}")


def _enrich_trades_with_features(root: Path, venue: str, trades: pl.DataFrame) -> pl.DataFrame:
    trades = _trades_with_signal_ts(trades)
    feature_series = _load_feature_series(root, venue, trades)
    indexes = {
        name: _build_prefix_index(series, name)
        for name, series in feature_series.items()
    }

    rows: list[dict[str, Any]] = []
    for row in trades.to_dicts():
        symbol = str(row["symbol"])
        signal_ts = int(row["_signal_ts_ms"])
        for hours in FEATURE_WINDOWS:
            row[f"funding_{hours}h_sum"] = _window_stat(
                indexes["funding"], symbol, signal_ts, hours, stat="sum"
            )
            row[f"premium_{hours}h_mean"] = _window_stat(
                indexes["premium"], symbol, signal_ts, hours, stat="mean"
            )
            row[f"basis_{hours}h_mean"] = _window_stat(
                indexes["basis"], symbol, signal_ts, hours, stat="mean"
            )
        rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None) if rows else trades


def _load_klines(root: Path, config: dict[str, Any]) -> pl.DataFrame:
    names = _autodetect_dataset_names(root)
    start_ms = _date_str_to_ms(str(config["start_date"]))
    end_ms = _date_str_to_ms(str(config["end_date"]))
    pad_fwd = (int(config.get("hold_hours", 24)) + int(config.get("entry_delay_hours", 1)) + 4) * MS_PER_HOUR
    pad_back_days = max(31 if config.get("btc_trend_gate") != "off" else 0, int(config.get("age_days_min", 0)))
    klines = _read_window(
        root,
        names["klines_dataset"],
        start_ms=start_ms - pad_back_days * MS_PER_DAY,
        end_ms=end_ms + pad_fwd,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
    )
    exclude = config.get("exclude_symbols") or []
    if exclude and not klines.is_empty():
        klines = klines.filter(~pl.col("symbol").is_in(list(exclude)))
    return klines


def _source_paths(root: Path, venue: str, cell_id: str) -> tuple[Path, Path]:
    cell_dir = root / venue / cell_id
    return cell_dir / "continuous_report.json", cell_dir / "continuous_trades.csv"


def _empty_scaled() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": [],
            "basket_return": [],
            "scale": [],
            "gross_return": [],
            "entry_cost_return": [],
            "funding_return": [],
            "resize_cost_return": [],
            "equity": [],
            "drawdown": [],
        },
        schema={
            "ts_ms": pl.Int64,
            "basket_return": pl.Float64,
            "scale": pl.Float64,
            "gross_return": pl.Float64,
            "entry_cost_return": pl.Float64,
            "funding_return": pl.Float64,
            "resize_cost_return": pl.Float64,
            "equity": pl.Float64,
            "drawdown": pl.Float64,
        },
    )


def _apply_filter(enriched: pl.DataFrame, predicate: FilterPredicate) -> pl.DataFrame:
    if enriched.is_empty():
        return enriched
    keep_ids = [
        row["trade_id"]
        for row in enriched.to_dicts()
        if predicate(row)
    ]
    if not keep_ids:
        return enriched.head(0)
    return enriched.filter(pl.col("trade_id").is_in(keep_ids))


def _feature_coverage(enriched: pl.DataFrame, venue: str) -> dict[str, Any]:
    row: dict[str, Any] = {"venue": venue, "n_trades": int(enriched.height)}
    for col in [
        "funding_24h_sum",
        "funding_72h_sum",
        "premium_24h_mean",
        "premium_72h_mean",
        "basis_24h_mean",
        "basis_72h_mean",
    ]:
        if col in enriched.columns and enriched.height:
            row[f"{col}_coverage"] = float(enriched[col].is_not_null().mean())
        else:
            row[f"{col}_coverage"] = 0.0
    return row


def _safe_min(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return min(vals) if vals else None


def _safe_mean(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return sum(vals) / len(vals) if vals else None


def _run_cost_case(
    *,
    cost_case: str,
    raw_root: Path,
    data_roots: dict[str, Path],
    cell_id: str,
    out: Path,
    venues: list[str],
    write_ledgers: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    feature_cache: dict[str, pl.DataFrame] = {}
    klines_cache: dict[str, pl.DataFrame] = {}
    config_cache: dict[str, dict[str, Any]] = {}

    for venue in venues:
        report_path, trades_path = _source_paths(raw_root, venue, cell_id)
        if not report_path.exists() or not trades_path.exists():
            raise FileNotFoundError(f"missing raw artifacts for {cost_case}/{venue}: {report_path.parent}")
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        config = payload["config"]
        trades = pl.read_csv(trades_path)
        enriched = _enrich_trades_with_features(data_roots[venue], venue, trades)
        feature_cache[venue] = enriched
        config_cache[venue] = config
        coverage_rows.append(_feature_coverage(enriched, venue))
        if write_ledgers:
            feature_dir = out / cost_case / venue
            feature_dir.mkdir(parents=True, exist_ok=True)
            enriched.write_csv(feature_dir / "feature_enriched_trades.csv")

        klines_cache[venue] = _load_klines(data_roots[venue], config)

    for venue in venues:
        enriched = feature_cache[venue]
        config = config_cache[venue]
        klines = klines_cache[venue]
        for filter_id, predicate in FILTERS:
            filtered = _apply_filter(enriched, predicate)
            mtm = _portfolio_mtm_equity(filtered, klines)
            components = decompose_continuous_components(filtered, mtm.select("ts_ms", "basket_return"), config)
            for named_rule in RISK_RULES:
                scaled = apply_rebalance_rule(components, named_rule.rule) if components.days else _empty_scaled()
                metrics = _daily_pnl_metrics(scaled.select("ts_ms", "equity", "drawdown", "basket_return"))
                scales = [float(x) for x in scaled["scale"].to_list()] if not scaled.is_empty() else []
                rule_id = rebalance_rule_id(named_rule.rule)
                if write_ledgers:
                    out_dir = out / cost_case / venue / filter_id / named_rule.name
                    out_dir.mkdir(parents=True, exist_ok=True)
                    scaled.write_csv(out_dir / "equity.csv")
                    if named_rule.name == "base":
                        filtered.write_csv(out / cost_case / venue / filter_id / "filtered_trades.csv")
                rows.append(
                    {
                        "cost_case": cost_case,
                        "venue": venue,
                        "filter_id": filter_id,
                        "risk_rule": named_rule.name,
                        "rule_id": rule_id,
                        "n_trades": int(filtered.height),
                        "return": metrics.get("total_return"),
                        "mar": metrics.get("mar"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "worst_day_return": metrics.get("worst_day_return"),
                        "avg_scale": _safe_mean(scales) if scales else 0.0,
                        "max_scale_observed": max(scales) if scales else 0.0,
                        "frac_scale_lt_one": (
                            sum(1 for x in scales if x < 1.0 - 1e-12) / len(scales)
                            if scales else 0.0
                        ),
                        "gross_return": float(scaled["gross_return"].sum()) if not scaled.is_empty() else 0.0,
                        "entry_cost_return": (
                            float(scaled["entry_cost_return"].sum()) if not scaled.is_empty() else 0.0
                        ),
                        "funding_return": (
                            float(scaled["funding_return"].sum()) if not scaled.is_empty() else 0.0
                        ),
                        "resize_cost_return": (
                            float(scaled["resize_cost_return"].sum()) if not scaled.is_empty() else 0.0
                        ),
                    }
                )
    return rows, coverage_rows


def _add_control_deltas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    controls: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row["filter_id"] == "all":
            controls[(row["cost_case"], row["venue"], row["risk_rule"])] = row
    out: list[dict[str, Any]] = []
    for row in rows:
        base = controls.get((row["cost_case"], row["venue"], row["risk_rule"]))
        copy = dict(row)
        if base:
            copy["delta_return_vs_all"] = (
                float(row["return"]) - float(base["return"])
                if row["return"] is not None and base["return"] is not None else None
            )
            copy["delta_mar_vs_all"] = (
                float(row["mar"]) - float(base["mar"])
                if row["mar"] is not None and base["mar"] is not None else None
            )
        out.append(copy)
    return out


def _pooled(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    frame = pl.DataFrame(rows, infer_schema_length=None)
    return (
        frame.group_by(["cost_case", "filter_id", "risk_rule"])
        .agg(
            pl.col("n_trades").sum().alias("pooled_trades"),
            pl.col("return").min().alias("min_return"),
            pl.col("return").mean().alias("mean_return"),
            pl.col("mar").min().alias("min_mar"),
            pl.col("mar").mean().alias("mean_mar"),
            pl.col("max_drawdown").min().alias("worst_dd"),
            pl.col("worst_day_return").min().alias("worst_day"),
            pl.col("delta_return_vs_all").mean().alias("mean_delta_return_vs_all"),
            pl.col("delta_mar_vs_all").mean().alias("mean_delta_mar_vs_all"),
            (pl.col("return") > 0).all().alias("positive_return_both"),
            (pl.col("mar") > 0).all().alias("positive_mar_both"),
            (pl.col("return") >= 1.20).all().alias("target_return_both"),
            (pl.col("mar") >= 6.0).all().alias("target_mar_both"),
            (pl.col("max_drawdown") >= -0.12).all().alias("target_dd_ok_both"),
        )
        .sort(
            [
                "cost_case",
                "target_return_both",
                "target_mar_both",
                "target_dd_ok_both",
                "min_mar",
                "mean_return",
            ],
            descending=[False, True, True, True, True, True],
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", required=True, type=Path)
    parser.add_argument("--cost200-root", type=Path)
    parser.add_argument("--data-roots", required=True, help="Comma-separated venue=path mappings.")
    parser.add_argument("--cell-id", default="merged_signal")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--write-ledgers", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    unknown = [v for v in venues if v not in VENUES]
    if unknown:
        raise SystemExit(f"unknown venue(s): {', '.join(unknown)}")
    data_roots = _parse_mapping(args.data_roots)
    missing = [v for v in venues if v not in data_roots]
    if missing:
        raise SystemExit(f"missing --data-roots mapping for: {', '.join(missing)}")

    cost_roots = [("base", args.raw_root.expanduser())]
    if args.cost200_root is not None:
        cost_roots.append(("cost200", args.cost200_root.expanduser()))

    args.out.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for cost_case, root in cost_roots:
        rows, coverage = _run_cost_case(
            cost_case=cost_case,
            raw_root=root,
            data_roots=data_roots,
            cell_id=args.cell_id,
            out=args.out,
            venues=venues,
            write_ledgers=args.write_ledgers,
        )
        all_rows.extend(rows)
        for row in coverage:
            coverage_rows.append({"cost_case": cost_case, **row})

    rows_with_deltas = _add_control_deltas(all_rows)
    _write_csv(args.out / "summary.csv", rows_with_deltas)
    _write_csv(args.out / "feature_coverage.csv", coverage_rows)
    pooled = _pooled(rows_with_deltas)
    pooled.write_csv(args.out / "pooled.csv")

    receipt = {
        "raw_root": str(args.raw_root),
        "cost200_root": str(args.cost200_root) if args.cost200_root else None,
        "data_roots": {venue: str(path) for venue, path in data_roots.items()},
        "cell_id": args.cell_id,
        "venues": venues,
        "filters": [name for name, _ in FILTERS],
        "risk_rules": [
            {"name": item.name, "rule_id": rebalance_rule_id(item.rule)}
            for item in RISK_RULES
        ],
        "outputs": {
            "summary": str(args.out / "summary.csv"),
            "pooled": str(args.out / "pooled.csv"),
            "feature_coverage": str(args.out / "feature_coverage.csv"),
        },
    }
    (args.out / "run_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(f"summary={args.out / 'summary.csv'}")
    print(f"pooled={args.out / 'pooled.csv'}")
    print(f"feature_coverage={args.out / 'feature_coverage.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
