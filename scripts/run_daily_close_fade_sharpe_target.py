from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import load_config
from aggression_carry.daily_close_fade import run_daily_close_fade_grid
from aggression_carry.downloaders import parse_date_ms
from aggression_carry.storage import read_dataset
from scripts.report_archive_pit_coverage import _filtered_manifest, _payload_summary, _symbol_filters, build_archive_pit_coverage


REQUIRED_DATASETS = (
    "klines_1m",
    "instruments",
    "archive_trade_manifest",
    "funding",
    "open_interest",
    "signed_flow_1h",
)
PREMIUM_ALTERNATIVES = (
    ("premium_index_1h",),
    ("mark_price_1h", "index_price_1h"),
)
MS_PER_MINUTE = 60_000
MS_PER_HOUR = 60 * MS_PER_MINUTE
MS_PER_DAY = 24 * MS_PER_HOUR
MIN_ARCHIVE_COVERAGE_RATE = 0.95
MIN_ARCHIVE_PROCESSED_RATE = 0.95
MIN_ARCHIVE_USABLE_RATE = 0.95
MIN_BARS_PER_DAY = 1200
MIN_FLOW_HOURS_PER_DAY = 20
MIN_CONTEXT_COVERAGE_RATE = 0.95
MIN_CONTEXT_TRADE_RATE = 0.95
DEFAULT_CONFIG_PATH = Path("configs/volume_alpha.default.yaml")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_root = Path(args.data_root).expanduser()
    output_dir = Path(args.report_dir).expanduser() if args.report_dir else data_root / "reports" / "daily_close_fade_sharpe_target"
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = _resolve_config_path(args.config)
    config = load_config(config_path, data_root=data_root)
    start, end = _resolve_window(args.start, args.end)
    symbols = _symbol_filters(args.symbols, args.symbols_file)
    full_manifest_mode = not symbols
    dataset_status = build_dataset_status(data_root, symbols=symbols)
    symbols_file = write_pit_symbols_file(data_root, output_dir, symbols=symbols) if symbols else ""
    archive_manifest = build_filtered_archive_manifest(data_root, start=start, end=end, symbols=symbols)
    archive_coverage = build_archive_coverage_summary(data_root, manifest=archive_manifest)
    required_context_labels = _required_context_labels(config.daily_close_fade, config.daily_close_fade_grid)
    context_coverage = build_context_coverage_summary(
        data_root,
        archive_manifest,
        min_coverage_rate=args.min_context_coverage_rate,
        required_labels=required_context_labels,
    )
    blockers = missing_required_datasets(dataset_status, start=start, end=end)
    blockers.extend(archive_coverage_blockers(archive_coverage, require_usable_rate=not full_manifest_mode))
    blockers.extend(context_coverage_blockers(context_coverage, require_full_manifest_rate=not full_manifest_mode))
    if blockers:
        payload = {
            "status": "blocked_missing_data",
            "full_manifest_mode": full_manifest_mode,
            "target_sharpe": args.target_sharpe,
            "window": {"start": start, "end": end},
            "config_path": config_path,
            "symbols": list(symbols),
            "missing": blockers,
            "dataset_status": dataset_status,
            "archive_coverage": archive_coverage,
            "context_coverage": context_coverage,
            "symbols_file": symbols_file,
            "next_commands": _next_commands(data_root, start, end, symbols_file=symbols_file, config_path=config_path),
        }
        write_report(output_dir, payload, candidates=[])
        print(f"blocked_missing_data report={output_dir / 'daily_close_fade_sharpe_target.md'}")
        return 2

    base = replace(
        config.daily_close_fade,
        include_symbols=symbols,
        require_archive_membership=True,
    )
    grid = replace(
        config.daily_close_fade_grid,
        start_ms=parse_date_ms(start),
        end_ms=parse_date_ms(end),
    )
    grid_payload = run_daily_close_fade_grid(
        data_root,
        grid_config=grid,
        base_fade_config=base,
        cost_config=config.costs,
        max_workers=args.workers,
        report_dir=output_dir,
    )
    candidates = select_candidates(
        grid_payload.get("results", []),
        target_sharpe=args.target_sharpe,
        max_drawdown=args.max_drawdown,
        min_trades=args.min_trades,
    )
    payload = {
        "status": "target_hit" if candidates else "target_not_hit",
        "full_manifest_mode": full_manifest_mode,
        "target_sharpe": args.target_sharpe,
        "max_drawdown": args.max_drawdown,
        "min_trades": args.min_trades,
        "window": {"start": start, "end": end},
        "config_path": config_path,
        "symbols": list(symbols),
        "dataset_status": dataset_status,
        "archive_coverage": archive_coverage,
        "context_coverage": context_coverage,
        "symbols_file": symbols_file,
        "rows": {
            "grid": grid_payload.get("rows", 0),
            "candidates": len(candidates),
        },
        "best_total_return": grid_payload.get("best_total_return", {}),
        "best_sharpe_like": grid_payload.get("best_sharpe_like", {}),
    }
    write_report(output_dir, payload, candidates=candidates)
    print(f"{payload['status']} candidates={len(candidates)} report={output_dir / 'daily_close_fade_sharpe_target.md'}")
    return 0 if candidates else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a gated past-year daily-close-fade Sharpe target search.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--start", default="", help="Inclusive UTC start date/time. Defaults to one year before --end.")
    parser.add_argument("--end", default="", help="Exclusive UTC end date/time. Defaults to current UTC date.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated PIT research universe.")
    parser.add_argument("--symbols-file", default="", help="Optional file containing comma- or newline-separated PIT research universe.")
    parser.add_argument("--target-sharpe", type=float, default=2.0)
    parser.add_argument("--max-drawdown", type=float, default=-0.35, help="Reject candidates below this drawdown, e.g. -0.35.")
    parser.add_argument("--min-trades", type=int, default=50)
    parser.add_argument("--min-context-coverage-rate", type=float, default=MIN_CONTEXT_COVERAGE_RATE)
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args(argv)


def _resolve_config_path(value: str | None) -> str | None:
    if value:
        return value
    return str(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else None


def _resolve_window(start: str, end: str) -> tuple[str, str]:
    end_dt = _parse_date_arg(end) if end else datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = _parse_date_arg(start) if start else end_dt - timedelta(days=365)
    return start_dt.date().isoformat(), end_dt.date().isoformat()


def _parse_date_arg(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _exclusive_end_to_manifest_end(end: str) -> str:
    return (_parse_date_arg(end) - timedelta(days=1)).date().isoformat()


def build_dataset_status(data_root: Path, *, symbols: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    datasets = sorted({*REQUIRED_DATASETS, *(item for option in PREMIUM_ALTERNATIVES for item in option)})
    return {dataset: _dataset_status(data_root, dataset, symbols=symbols) for dataset in datasets}


def _dataset_status(data_root: Path, dataset: str, *, symbols: tuple[str, ...] = ()) -> dict[str, Any]:
    path = data_root / dataset
    files = sorted(path.glob("**/*.parquet")) if path.exists() else []
    status: dict[str, Any] = {
        "exists": path.exists(),
        "path": str(path),
        "parquet_files": len(files),
        "bytes": sum(file.stat().st_size for file in files),
    }
    if not files:
        return status
    try:
        scan = pl.scan_parquet([str(file) for file in files])
        columns = scan.collect_schema().names()
        if symbols and "symbol" in columns:
            scan = scan.filter(pl.col("symbol").cast(pl.String).str.to_uppercase().is_in(symbols))
        status["columns"] = columns
        expressions: list[pl.Expr] = [pl.len().alias("row_count")]
        if "symbol" in columns:
            expressions.append(pl.col("symbol").n_unique().alias("symbol_count"))
        if "ts_ms" in columns:
            expressions.extend(
                [
                    pl.col("ts_ms").min().alias("min_ts_ms"),
                    pl.col("ts_ms").max().alias("max_ts_ms"),
                    pl.col("ts_ms").n_unique().alias("timestamp_count"),
                    (pl.col("ts_ms").cast(pl.Int64) // MS_PER_MINUTE).n_unique().alias("minute_count"),
                    (pl.col("ts_ms").cast(pl.Int64) // MS_PER_HOUR).n_unique().alias("hour_count"),
                    (pl.col("ts_ms").cast(pl.Int64) // MS_PER_DAY).n_unique().alias("ts_date_count"),
                ]
            )
        if "date" in columns:
            expressions.extend(
                [
                    pl.col("date").min().alias("min_date"),
                    pl.col("date").max().alias("max_date"),
                    pl.col("date").n_unique().alias("date_count"),
                ]
            )
        stats = scan.select(expressions).collect().to_dicts()[0]
        status.update({key: _json_scalar(value) for key, value in stats.items()})
    except Exception as exc:  # pragma: no cover - defensive report metadata only
        status["scan_error"] = str(exc)
    return status


def missing_required_datasets(
    dataset_status: dict[str, dict[str, Any]],
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    missing = [
        dataset
        for dataset in REQUIRED_DATASETS
        if not _has_dataset_rows(dataset_status.get(dataset, {}))
    ]
    has_premium = any(
        all(_has_dataset_rows(dataset_status.get(dataset, {})) for dataset in option)
        for option in PREMIUM_ALTERNATIVES
    )
    if not has_premium:
        missing.append("premium_index_1h or mark_price_1h+index_price_1h")
    if start and end:
        missing.extend(_coverage_blockers(dataset_status, start=start, end=end, already_missing=set(missing)))
    return missing


def _has_dataset_rows(status: dict[str, Any]) -> bool:
    return int(status.get("parquet_files", 0) or 0) > 0 and int(status.get("row_count", 0) or 0) > 0


def _coverage_blockers(
    dataset_status: dict[str, dict[str, Any]],
    *,
    start: str,
    end: str,
    already_missing: set[str],
) -> list[str]:
    blockers: list[str] = []
    start_dt = _parse_date_arg(start)
    end_dt = _parse_date_arg(end)
    start_ms = parse_date_ms(start_dt.date().isoformat())
    end_ms = parse_date_ms(end_dt.date().isoformat())
    expected_days = max(1, (end_dt.date() - start_dt.date()).days)
    ts_requirements = {
        "klines_1m": end_ms - MS_PER_MINUTE,
        "funding": end_ms - 8 * MS_PER_HOUR,
        "open_interest": end_ms - MS_PER_HOUR,
        "signed_flow_1h": end_ms - MS_PER_HOUR,
    }
    for dataset, required_max_ts_ms in ts_requirements.items():
        if dataset in already_missing or not _has_dataset_rows(dataset_status.get(dataset, {})):
            continue
        status = dataset_status[dataset]
        if not _ts_coverage_ok(status, required_min_ts_ms=start_ms, required_max_ts_ms=required_max_ts_ms):
            blockers.append(f"{dataset} coverage {_format_ts_coverage(status)} does not span requested window")
    density_requirements = {
        "klines_1m": ("minute_count", expected_days * 24 * 60, 0.95),
        "funding": ("timestamp_count", expected_days * 3, 0.90),
        "open_interest": ("hour_count", expected_days * 24, 0.90),
        "signed_flow_1h": ("hour_count", expected_days * 24, 0.95),
    }
    for dataset, (field, expected, min_ratio) in density_requirements.items():
        if dataset in already_missing or not _has_dataset_rows(dataset_status.get(dataset, {})):
            continue
        status = dataset_status[dataset]
        if not _density_ok(status, field=field, expected=expected, min_ratio=min_ratio):
            blockers.append(f"{dataset} density {_format_density(status, field=field, expected=expected)} is below {min_ratio:.0%}")

    manifest = dataset_status.get("archive_trade_manifest", {})
    if "archive_trade_manifest" not in already_missing and _has_dataset_rows(manifest):
        start_date = start_dt.date().isoformat()
        end_date = (end_dt - timedelta(days=1)).date().isoformat()
        if not _date_coverage_ok(manifest, required_min_date=start_date, required_max_date=end_date):
            blockers.append(f"archive_trade_manifest coverage {_format_date_coverage(manifest)} does not span requested window")
        if not _density_ok(manifest, field="date_count", expected=expected_days, min_ratio=1.0):
            blockers.append(f"archive_trade_manifest density {_format_density(manifest, field='date_count', expected=expected_days)} is below 100%")

    premium_rows_present = any(all(_has_dataset_rows(dataset_status.get(dataset, {})) for dataset in option) for option in PREMIUM_ALTERNATIVES)
    if premium_rows_present:
        premium_coverage_ok = any(
            all(
                _ts_coverage_ok(
                    dataset_status.get(dataset, {}),
                    required_min_ts_ms=start_ms,
                    required_max_ts_ms=end_ms - MS_PER_HOUR,
                )
                for dataset in option
            )
            for option in PREMIUM_ALTERNATIVES
        )
        if not premium_coverage_ok:
            blockers.append("premium coverage does not span requested window")
        premium_density_ok = any(
            all(
                _density_ok(
                    dataset_status.get(dataset, {}),
                    field="hour_count",
                    expected=expected_days * 24,
                    min_ratio=0.90,
                )
                for dataset in option
            )
            for option in PREMIUM_ALTERNATIVES
        )
        if not premium_density_ok:
            blockers.append("premium density is below 90% of requested hourly slots")
    return blockers


def _ts_coverage_ok(status: dict[str, Any], *, required_min_ts_ms: int, required_max_ts_ms: int) -> bool:
    min_ts = status.get("min_ts_ms")
    max_ts = status.get("max_ts_ms")
    if min_ts is None or max_ts is None:
        return False
    return int(min_ts) <= required_min_ts_ms and int(max_ts) >= required_max_ts_ms


def _date_coverage_ok(status: dict[str, Any], *, required_min_date: str, required_max_date: str) -> bool:
    min_date = status.get("min_date")
    max_date = status.get("max_date")
    if min_date is None or max_date is None:
        return False
    return str(min_date) <= required_min_date and str(max_date) >= required_max_date


def _density_ok(status: dict[str, Any], *, field: str, expected: int, min_ratio: float) -> bool:
    if expected <= 0:
        return True
    observed = int(status.get(field, 0) or 0)
    return observed >= math.ceil(expected * min_ratio)


def _format_density(status: dict[str, Any], *, field: str, expected: int) -> str:
    observed = int(status.get(field, 0) or 0)
    return f"{observed}/{expected} {field}"


def build_filtered_archive_manifest(data_root: Path, *, start: str, end: str, symbols: tuple[str, ...] = ()) -> pl.DataFrame:
    manifest_end = _exclusive_end_to_manifest_end(end)
    manifest = _filtered_manifest(
        read_dataset(data_root, "archive_trade_manifest"),
        start=start,
        end=manifest_end,
        symbols=symbols,
        max_rows=0,
    )
    return manifest


def build_archive_coverage_summary(data_root: Path, *, manifest: pl.DataFrame) -> dict[str, Any]:
    if manifest.is_empty():
        return {
            "manifest_rows": 0,
            "processed_rows": 0,
            "covered_rows": 0,
            "sparse_rows": 0,
            "missing_rows": 0,
            "usable_rows": 0,
            "processed_rate": 0.0,
            "coverage_rate": 0.0,
            "usable_rate": 0.0,
            "min_processed_rate": MIN_ARCHIVE_PROCESSED_RATE,
            "min_coverage_rate": MIN_ARCHIVE_COVERAGE_RATE,
            "min_usable_rate": MIN_ARCHIVE_USABLE_RATE,
            "min_bars_per_day": MIN_BARS_PER_DAY,
            "require_flow": True,
            "min_flow_hours_per_day": MIN_FLOW_HOURS_PER_DAY,
            "require_next_day": True,
        }
    coverage = build_archive_pit_coverage(
        data_root,
        manifest,
        min_bars_per_day=MIN_BARS_PER_DAY,
        require_next_day=True,
        require_flow=True,
        min_flow_hours_per_day=MIN_FLOW_HOURS_PER_DAY,
    )
    return {
        **_payload_summary(coverage),
        "min_processed_rate": MIN_ARCHIVE_PROCESSED_RATE,
        "min_coverage_rate": MIN_ARCHIVE_COVERAGE_RATE,
        "min_usable_rate": MIN_ARCHIVE_USABLE_RATE,
        "min_bars_per_day": MIN_BARS_PER_DAY,
        "require_flow": True,
        "min_flow_hours_per_day": MIN_FLOW_HOURS_PER_DAY,
        "require_next_day": True,
    }


def build_context_coverage_summary(
    data_root: Path,
    manifest: pl.DataFrame,
    *,
    min_coverage_rate: float = MIN_CONTEXT_COVERAGE_RATE,
    required_labels: tuple[str, ...] = ("funding", "open_interest", "signed_flow_1h", "premium"),
) -> dict[str, Any]:
    base = _manifest_keys(manifest)
    rows: list[dict[str, Any]] = []
    required = set(required_labels)
    if base.is_empty():
        return {"min_coverage_rate": min_coverage_rate, "rows": rows}

    rows.append(
        _with_required(
            _daily_context_coverage(
                data_root,
                base,
                label="funding",
                datasets=("funding",),
                min_rows_per_day=2,
                next_day_min_rows=1,
            ),
            required="funding" in required,
        )
    )
    rows.append(
        _with_required(
            _daily_context_coverage(
                data_root,
                base,
                label="open_interest",
                datasets=("open_interest",),
                min_rows_per_day=20,
            ),
            required="open_interest" in required,
        )
    )
    rows.append(
        _with_required(
            _daily_context_coverage(
                data_root,
                base,
                label="signed_flow_1h",
                datasets=("signed_flow_1h",),
                min_rows_per_day=MIN_FLOW_HOURS_PER_DAY,
            ),
            required="signed_flow_1h" in required,
        )
    )
    premium = _premium_context_coverage(data_root, base)
    rows.append(_with_required(premium, required="premium" in required))
    return {"min_coverage_rate": min_coverage_rate, "required_labels": sorted(required), "rows": rows}


def _required_context_labels(fade_config: Any, grid_config: Any) -> tuple[str, ...]:
    labels = {"funding"}
    if getattr(fade_config, "require_all_context", False) or any(getattr(grid_config, "require_all_contexts", ())):
        labels.update(("funding", "open_interest", "signed_flow_1h", "premium"))
    if getattr(fade_config, "require_funding_context", False):
        labels.add("funding")
    if getattr(fade_config, "require_open_interest_context", False) or any(
        getattr(grid_config, "require_open_interest_contexts", ())
    ):
        labels.add("open_interest")
    if getattr(fade_config, "require_trade_flow_context", False):
        labels.add("signed_flow_1h")
    if getattr(fade_config, "require_premium_context", False):
        labels.add("premium")
    return tuple(sorted(labels))


def _with_required(row: dict[str, Any], *, required: bool) -> dict[str, Any]:
    return {**row, "required": required}


def _manifest_keys(manifest: pl.DataFrame) -> pl.DataFrame:
    if manifest.is_empty():
        return pl.DataFrame()
    return manifest.select(["symbol", "date"]).unique().sort(["symbol", "date"])


def _daily_context_coverage(
    data_root: Path,
    base: pl.DataFrame,
    *,
    label: str,
    datasets: tuple[str, ...],
    min_rows_per_day: int,
    next_day_min_rows: int = 0,
) -> dict[str, Any]:
    counts = [_dataset_daily_counts(data_root, dataset) for dataset in datasets]
    if not counts or any(item.is_empty() for item in counts):
        return _empty_context_coverage(label, datasets, base.height, min_rows_per_day, next_day_min_rows)
    daily = counts[0]
    if len(counts) > 1:
        for index, frame in enumerate(counts[1:], start=2):
            daily = daily.join(frame.rename({"rows": f"rows_{index}"}), on=["symbol", "date"], how="inner")
        row_columns = [name for name in daily.columns if name.startswith("rows")]
        daily = daily.with_columns(pl.min_horizontal(row_columns).alias("rows")).select(["symbol", "date", "rows"])

    joined = base.join(daily, on=["symbol", "date"], how="left").with_columns(pl.col("rows").fill_null(0).cast(pl.Int64))
    if next_day_min_rows > 0:
        next_counts = daily.rename({"date": "next_date", "rows": "next_rows"})
        joined = (
            joined.with_columns(_next_date_expr("date").alias("next_date"))
            .join(next_counts, on=["symbol", "next_date"], how="left")
            .with_columns(pl.col("next_rows").fill_null(0).cast(pl.Int64))
        )
        covered_expr = (pl.col("rows") >= min_rows_per_day) & (pl.col("next_rows") >= next_day_min_rows)
    else:
        covered_expr = pl.col("rows") >= min_rows_per_day
    covered_rows = int(joined.select(covered_expr.sum().alias("covered_rows")).item())
    return {
        "label": label,
        "datasets": list(datasets),
        "manifest_rows": base.height,
        "covered_rows": covered_rows,
        "missing_rows": base.height - covered_rows,
        "coverage_rate": covered_rows / base.height if base.height else 0.0,
        "min_rows_per_day": min_rows_per_day,
        "next_day_min_rows": next_day_min_rows,
    }


def _premium_context_coverage(data_root: Path, base: pl.DataFrame) -> dict[str, Any]:
    premium = _dataset_daily_counts(data_root, "premium_index_1h")
    if not premium.is_empty():
        return _daily_context_coverage(
            data_root,
            base,
            label="premium",
            datasets=("premium_index_1h",),
            min_rows_per_day=20,
        )
    return _daily_context_coverage(
        data_root,
        base,
        label="premium",
        datasets=("mark_price_1h", "index_price_1h"),
        min_rows_per_day=20,
    )


def _dataset_daily_counts(data_root: Path, dataset: str) -> pl.DataFrame:
    frame = read_dataset(data_root, dataset)
    if frame.is_empty() or not {"symbol", "ts_ms"}.issubset(frame.columns):
        return pl.DataFrame()
    return (
        frame.select(
            [
                pl.col("symbol").cast(pl.String).str.to_uppercase().alias("symbol"),
                pl.from_epoch(pl.col("ts_ms").cast(pl.Int64), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"),
            ]
        )
        .group_by(["symbol", "date"])
        .len(name="rows")
    )


def _empty_context_coverage(
    label: str,
    datasets: tuple[str, ...],
    manifest_rows: int,
    min_rows_per_day: int,
    next_day_min_rows: int,
) -> dict[str, Any]:
    return {
        "label": label,
        "datasets": list(datasets),
        "manifest_rows": manifest_rows,
        "covered_rows": 0,
        "missing_rows": manifest_rows,
        "coverage_rate": 0.0,
        "min_rows_per_day": min_rows_per_day,
        "next_day_min_rows": next_day_min_rows,
    }


def _next_date_expr(column: str) -> pl.Expr:
    return (pl.col(column).str.strptime(pl.Date, "%Y-%m-%d") + pl.duration(days=1)).dt.strftime("%Y-%m-%d")


def context_coverage_blockers(summary: dict[str, Any], *, require_full_manifest_rate: bool = True) -> list[str]:
    if not require_full_manifest_rate:
        return []
    min_rate = float(summary.get("min_coverage_rate", MIN_CONTEXT_COVERAGE_RATE) or MIN_CONTEXT_COVERAGE_RATE)
    blockers: list[str] = []
    for row in summary.get("rows", []):
        if not bool(row.get("required", True)):
            continue
        manifest_rows = int(row.get("manifest_rows", 0) or 0)
        if manifest_rows <= 0:
            continue
        coverage_rate = float(row.get("coverage_rate", 0.0) or 0.0)
        if coverage_rate + 1e-12 < min_rate:
            blockers.append(
                f"{row.get('label', 'context')} context coverage {coverage_rate:.2%} is below {min_rate:.0%} "
                f"({int(row.get('covered_rows', 0) or 0)}/{manifest_rows} symbol-date rows)"
            )
    return blockers


def archive_coverage_blockers(summary: dict[str, Any], *, require_usable_rate: bool = True) -> list[str]:
    manifest_rows = int(summary.get("manifest_rows", 0) or 0)
    if manifest_rows <= 0:
        return []
    blockers: list[str] = []
    processed_rate = float(summary.get("processed_rate", summary.get("coverage_rate", 0.0)) or 0.0)
    usable_rate = float(summary.get("usable_rate", 0.0) or 0.0)
    min_processed_rate = float(summary.get("min_processed_rate", MIN_ARCHIVE_PROCESSED_RATE) or MIN_ARCHIVE_PROCESSED_RATE)
    if processed_rate + 1e-12 < min_processed_rate:
        blockers.append(
            f"archive PIT processed coverage {processed_rate:.2%} is below {min_processed_rate:.0%} "
            f"({int(summary.get('processed_rows', summary.get('covered_rows', 0)) or 0)}/{manifest_rows} processed rows)"
        )
    if require_usable_rate and usable_rate + 1e-12 < MIN_ARCHIVE_USABLE_RATE:
        blockers.append(
            f"archive PIT usable coverage {usable_rate:.2%} is below {MIN_ARCHIVE_USABLE_RATE:.0%} "
            f"({int(summary.get('usable_rows', 0) or 0)}/{manifest_rows} close-fade usable rows)"
        )
    return blockers


def _format_ts_coverage(status: dict[str, Any]) -> str:
    min_ts = status.get("min_ts_ms")
    max_ts = status.get("max_ts_ms")
    if min_ts is None or max_ts is None:
        return "unknown"
    return f"{_format_ts(int(min_ts))} to {_format_ts(int(max_ts))}"


def _format_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_date_coverage(status: dict[str, Any]) -> str:
    min_date = status.get("min_date")
    max_date = status.get("max_date")
    if min_date is None or max_date is None:
        return "unknown"
    return f"{min_date} to {max_date}"


def select_candidates(
    rows: list[dict[str, Any]],
    *,
    target_sharpe: float,
    max_drawdown: float,
    min_trades: int,
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in rows
        if float(row.get("sharpe_like", 0.0) or 0.0) >= target_sharpe
        and float(row.get("total_return", 0.0) or 0.0) > 0.0
        and float(row.get("max_drawdown", 0.0) or 0.0) >= max_drawdown
        and int(row.get("trade_count", 0) or 0) >= min_trades
        and bool(row.get("all_splits_positive", False))
        and str(row.get("funding_mode", "")) == "modeled"
        and _capacity_enabled(row)
        and float(row.get("market_impact_bps_per_1pct_turnover", 0.0) or 0.0) > 0.0
        and _post_twap_cluster_rate(row) <= 0.50
        and _context_gate_ok(row)
    ]
    return sorted(
        candidates,
        key=lambda row: (
            float(row.get("sharpe_like", 0.0) or 0.0),
            float(row.get("total_return", 0.0) or 0.0),
        ),
        reverse=True,
    )


def _capacity_enabled(row: dict[str, Any]) -> bool:
    return (
        float(row.get("max_trade_notional_pct_of_day_turnover", 0.0) or 0.0) > 0.0
        or float(row.get("max_trade_notional_pct_of_baseline_turnover", 0.0) or 0.0) > 0.0
    )


def _post_twap_cluster_rate(row: dict[str, Any]) -> float:
    total = float(row.get("trade_count", 0) or 0)
    if total <= 0.0:
        return 1.0
    return float(row.get("post_twap_exit_le16", 0) or 0) / total


def _context_gate_ok(row: dict[str, Any]) -> bool:
    if str(row.get("score", "")) != "context_fade_score":
        return True
    required = (
        "funding_context_rate",
        "open_interest_context_rate",
        "premium_context_rate",
        "trade_flow_context_rate",
        "all_context_rate",
    )
    return all(float(row.get(field, 0.0) or 0.0) + 1e-12 >= MIN_CONTEXT_TRADE_RATE for field in required)


def write_report(output_dir: Path, payload: dict[str, Any], *, candidates: list[dict[str, Any]]) -> None:
    (output_dir / "daily_close_fade_sharpe_target.json").write_text(json.dumps({**payload, "candidates": candidates}, indent=2), encoding="utf-8")
    (output_dir / "daily_close_fade_sharpe_target.md").write_text(format_report(payload, candidates), encoding="utf-8")


def write_pit_symbols_file(data_root: Path, output_dir: Path, *, symbols: tuple[str, ...] = ()) -> str:
    path = data_root / "archive_trade_manifest"
    files = sorted(path.glob("**/*.parquet")) if path.exists() else []
    if not files:
        return ""
    try:
        scan = pl.scan_parquet([str(file) for file in files]).select(pl.col("symbol").cast(pl.String).str.to_uppercase().alias("symbol"))
        if symbols:
            scan = scan.filter(pl.col("symbol").is_in(symbols))
        output_symbols = scan.select(pl.col("symbol").unique().sort()).collect().get_column("symbol").to_list()
    except Exception:  # pragma: no cover - report convenience only
        return ""
    if not output_symbols:
        return ""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "pit_symbols.txt"
    output_path.write_text(",".join(output_symbols) + "\n", encoding="utf-8")
    return str(output_path)


def format_report(payload: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# Daily Close Fade Sharpe Target",
        "",
        f"Status: `{payload['status']}`",
        f"Window: {payload['window']['start']} to {payload['window']['end']}",
        f"Target Sharpe-like: {payload['target_sharpe']:.2f}",
        f"Config: `{payload.get('config_path') or 'dataclass defaults'}`",
        f"Symbol filter: {_format_symbol_filter(payload.get('symbols', []))}",
        "",
        "## Dataset Gate",
        "",
        "| Dataset | Files | Rows | Symbols | Dates | Hours | Minutes | First | Last | Bytes | Path |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|---:|---|",
    ]
    for dataset, status in payload.get("dataset_status", {}).items():
        first_seen, last_seen = _format_dataset_coverage(status)
        lines.append(
            f"| {dataset} | {status.get('parquet_files', 0)} | {status.get('row_count', 0)} | "
            f"{status.get('symbol_count', 0)} | {_date_count(status)} | {status.get('hour_count', 0)} | "
            f"{status.get('minute_count', 0)} | {first_seen} | {last_seen} | {status.get('bytes', 0)} | `{status.get('path')}` |"
        )
    if payload.get("archive_coverage"):
        coverage = payload["archive_coverage"]
        lines.extend(
            [
                "",
                "## Archive Coverage Gate",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Manifest rows | {coverage.get('manifest_rows', 0)} |",
                f"| Processed rows | {coverage.get('processed_rows', 0)} |",
                f"| Covered rows | {coverage.get('covered_rows', 0)} |",
                f"| Sparse rows | {coverage.get('sparse_rows', 0)} |",
                f"| Missing rows | {coverage.get('missing_rows', 0)} |",
                f"| Usable close-fade rows | {coverage.get('usable_rows', 0)} |",
                f"| Processed rate | {float(coverage.get('processed_rate', 0.0) or 0.0):.2%} |",
                f"| Coverage rate | {float(coverage.get('coverage_rate', 0.0) or 0.0):.2%} |",
                f"| Usable rate | {float(coverage.get('usable_rate', 0.0) or 0.0):.2%} |",
                f"| Required processed rate | {float(coverage.get('min_processed_rate', 0.0) or 0.0):.2%} |",
                f"| Required usable rate | {float(coverage.get('min_usable_rate', 0.0) or 0.0):.2%} |",
                f"| Require signed-flow coverage | {coverage.get('require_flow', False)} |",
                f"| Min signed-flow hours/day | {coverage.get('min_flow_hours_per_day', 0)} |",
            ]
        )
        if payload.get("full_manifest_mode"):
            lines.extend(
                [
                    "",
                    "Full-manifest mode audits sparse/inactive listing rows but does not require 95% dense usable rows before the run. "
                    "The strategy still requires per-row bar coverage and configured context before a row can be selected.",
                ]
            )
    if payload.get("context_coverage"):
        context = payload["context_coverage"]
        lines.extend(
            [
                "",
                "## Context Coverage Gate",
                "",
                "| Context | Required | Datasets | Covered | Missing | Coverage | Min Rows/Day | Next-Day Rows |",
                "|---|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in context.get("rows", []):
            lines.append(
                f"| {row.get('label')} | {bool(row.get('required', True))} | {','.join(row.get('datasets', []))} | "
                f"{row.get('covered_rows', 0)}/{row.get('manifest_rows', 0)} | {row.get('missing_rows', 0)} | "
                f"{float(row.get('coverage_rate', 0.0) or 0.0):.2%} | {row.get('min_rows_per_day', 0)} | "
                f"{row.get('next_day_min_rows', 0)} |"
            )
    if payload.get("missing"):
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {item}" for item in payload["missing"])
        if payload.get("symbols_file"):
            lines.extend(["", "## PIT Symbols", "", f"`{payload['symbols_file']}`"])
        lines.extend(["", "## Next Commands", ""])
        lines.extend(f"```bash\n{command}\n```" for command in payload.get("next_commands", []))
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "| Rank | Sharpe | Return | Max DD | Min Split | Trades | Max Hold | <=16m Exits | Context | Score | Top N | Hold | Profit Delay | TP | Time TP | Cost | Impact |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(candidates[:25], start=1):
        lines.append(
            f"| {index} | {float(row.get('sharpe_like', 0.0)):.2f} | {float(row.get('total_return', 0.0)):.2%} | "
            f"{float(row.get('max_drawdown', 0.0)):.2%} | {float(row.get('min_split_return', 0.0)):.2%} | "
            f"{int(row.get('trade_count', 0) or 0)} | {int(row.get('exit_max_hold', 0) or 0)} | "
            f"{_post_twap_cluster_rate(row):.1%} | "
            f"{float(row.get('all_context_rate', 0.0) or 0.0):.1%} | {row.get('score')} | "
            f"{row.get('top_n')} | {row.get('hold_minutes')} | "
            f"{row.get('profit_protection_delay_minutes')} | {float(row.get('take_profit_pct', 0.0)):.1%} | "
            f"{float(row.get('time_decay_take_profit_floor_pct', 0.0)):.1%}/{int(row.get('time_decay_take_profit_minutes', 0) or 0)}m | "
            f"{float(row.get('cost_multiplier', 0.0)):.1f}x | "
            f"{float(row.get('market_impact_bps_per_1pct_turnover', 0.0)):.1f} |"
        )
    return "\n".join(lines) + "\n"


def _format_dataset_coverage(status: dict[str, Any]) -> tuple[str, str]:
    if status.get("min_ts_ms") is not None and status.get("max_ts_ms") is not None:
        return _format_ts(int(status["min_ts_ms"])), _format_ts(int(status["max_ts_ms"]))
    if status.get("min_date") is not None and status.get("max_date") is not None:
        return str(status["min_date"]), str(status["max_date"])
    return "", ""


def _date_count(status: dict[str, Any]) -> int:
    return max(int(status.get("date_count", 0) or 0), int(status.get("ts_date_count", 0) or 0))


def _format_symbol_filter(symbols: list[str]) -> str:
    if not symbols:
        return "full PIT manifest"
    if len(symbols) <= 12:
        return ",".join(symbols)
    return f"{len(symbols)} symbols"


def _json_scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _next_commands(data_root: Path, start: str, end: str, *, symbols_file: str = "", config_path: str | None = None) -> list[str]:
    root = str(data_root)
    symbols_arg = f" --symbols-file {symbols_file}" if symbols_file else ""
    config_arg = f"--config {config_path} " if config_path else ""
    return [
        f"python -m aggression_carry {config_arg}--data-root {root} archive-manifest{symbols_arg} --start {start} --end {end} --quote-suffix USDT --workers 16",
        (
            f"python scripts/run_archive_pit_batches.py --data-root {root} "
            f"--start {start} --end {end}{symbols_arg} --batch-rows 1000 --workers 16 --include-flow --coverage-every 1"
        ),
        (
            f"python scripts/report_archive_pit_coverage.py --data-root {root} "
            f"--start {start} --end {end}{symbols_arg} --min-bars-per-day 1200 --require-flow --min-usable-rate 0.95"
        ),
        (
            f"python -m aggression_carry {config_arg}--data-root {root} download-data "
            f"--start {start} --end {end}{symbols_arg} "
            "--datasets instruments,funding,open_interest,premium_index_1h "
            "--workers 8"
        ),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
