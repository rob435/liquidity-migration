from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Any
from urllib.request import Request, urlopen

import certifi
import polars as pl
import ssl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.storage import read_dataset


DEFAULT_EXCLUDES = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
DEFAULT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ArchiveLiquidityUniverseConfig:
    start: str
    top_n: int = 50
    min_content_length: int = 1
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDES
    workers: int = 16
    name: str = "archive_liquidity_universe"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_root = Path(args.data_root).expanduser()
    report_dir = Path(args.report_dir).expanduser() if args.report_dir else data_root / "reports" / "universes"
    report_dir.mkdir(parents=True, exist_ok=True)
    config = ArchiveLiquidityUniverseConfig(
        start=args.start,
        top_n=args.top_n,
        min_content_length=args.min_content_length,
        exclude_symbols=_csv_symbols(args.exclude_symbols),
        workers=args.workers,
        name=args.name,
    )
    payload = select_archive_liquidity_universe(data_root, config=config, report_dir=report_dir)
    print(f"archive liquidity universe symbols={len(payload['symbols'])} path={payload['symbols_file']}")
    return 0 if payload["symbols"] else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select a PIT archive universe ranked by start-date public-trade archive size.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--start", required=True, help="Causal ranking date YYYY-MM-DD.")
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--min-content-length", type=int, default=1)
    parser.add_argument("--exclude-symbols", default=",".join(DEFAULT_EXCLUDES))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--name", default="archive_liquidity_universe")
    return parser.parse_args(argv)


def select_archive_liquidity_universe(
    data_root: str | Path,
    *,
    config: ArchiveLiquidityUniverseConfig,
    report_dir: str | Path,
    content_length_func: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    content_length_func = content_length_func or fetch_content_length
    manifest = _start_date_manifest(Path(data_root), config.start, exclude_symbols=config.exclude_symbols)
    rows = _rank_manifest_rows_by_content_length(manifest, workers=config.workers, content_length_func=content_length_func)
    ranked = (
        pl.DataFrame(rows, infer_schema_length=None)
        if rows
        else pl.DataFrame(schema={"symbol": pl.String, "date": pl.String, "url": pl.String, "content_length": pl.Int64, "error": pl.String})
    )
    if not ranked.is_empty():
        ranked = ranked.sort(["content_length", "symbol"], descending=[True, False])
    selected = ranked.filter(pl.col("content_length") >= config.min_content_length).head(config.top_n)
    symbols = selected.get_column("symbol").to_list() if not selected.is_empty() else []

    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = config.name
    ranked_path = output_dir / f"{prefix}.csv"
    symbols_path = output_dir / f"{prefix}_symbols.txt"
    json_path = output_dir / f"{prefix}.json"
    md_path = output_dir / f"{prefix}.md"
    ranked.write_csv(ranked_path)
    symbols_path.write_text(",".join(symbols) + ("\n" if symbols else ""), encoding="utf-8")
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "start": config.start,
        "top_n": config.top_n,
        "min_content_length": config.min_content_length,
        "exclude_symbols": list(config.exclude_symbols),
        "rows": ranked.height,
        "symbols": symbols,
        "ranked_file": str(ranked_path),
        "symbols_file": str(symbols_path),
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_format_report(payload, selected), encoding="utf-8")
    return payload


def fetch_content_length(url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    context = ssl.create_default_context(cafile=certifi.where())
    request = Request(url, method="HEAD")
    with urlopen(request, timeout=timeout_seconds, context=context) as response:  # noqa: S310 - public Bybit research archive URL
        value = response.headers.get("Content-Length")
    return int(value) if value else 0


def _start_date_manifest(data_root: Path, start: str, *, exclude_symbols: tuple[str, ...]) -> pl.DataFrame:
    manifest = read_dataset(data_root, "archive_trade_manifest")
    if manifest.is_empty():
        raise RuntimeError("archive_trade_manifest is empty; run archive-manifest first")
    excludes = set(exclude_symbols)
    return (
        manifest.select(["symbol", "date", "url"])
        .unique()
        .filter((pl.col("date") == start[:10]) & (~pl.col("symbol").is_in(excludes)))
        .sort("symbol")
    )


def _rank_manifest_rows_by_content_length(
    manifest: pl.DataFrame,
    *,
    workers: int,
    content_length_func: Callable[[str], int],
) -> list[dict[str, Any]]:
    rows = manifest.to_dicts()
    if not rows:
        return []
    output: list[dict[str, Any]] = []
    max_workers = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_head_row, row, content_length_func): row for row in rows}
        for future in as_completed(futures):
            output.append(future.result())
    return output


def _head_row(row: dict[str, Any], content_length_func: Callable[[str], int]) -> dict[str, Any]:
    try:
        content_length = int(content_length_func(str(row["url"])))
        error = ""
    except Exception as exc:  # noqa: BLE001 - network failures should be reported per row
        content_length = 0
        error = str(exc)
    return {
        "symbol": str(row["symbol"]),
        "date": str(row["date"]),
        "url": str(row["url"]),
        "content_length": content_length,
        "error": error,
    }


def _format_report(payload: dict[str, Any], selected: pl.DataFrame) -> str:
    lines = [
        "# Archive Liquidity Universe",
        "",
        f"Created: {payload['created_at']}",
        f"Ranking date: {payload['start']}",
        f"Selected symbols: {len(payload['symbols'])}",
        "Ranking proxy: public-trade archive `Content-Length` on the ranking date.",
        "",
        "## Selected",
        "",
        "| Rank | Symbol | Content Length |",
        "|---:|---|---:|",
    ]
    selected_rows = selected.to_dicts() if not selected.is_empty() else []
    for index, row in enumerate(selected_rows, start=1):
        lines.append(f"| {index} | {row['symbol']} | {row['content_length']} |")
    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- Ranked CSV: `{payload['ranked_file']}`",
            f"- Symbols file: `{payload['symbols_file']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _csv_symbols(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


if __name__ == "__main__":
    raise SystemExit(main())
