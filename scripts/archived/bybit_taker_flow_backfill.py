"""Event-anchored bybit taker-flow 5m backfill from public.bybit.com tick trades.

Data layer only — no signal claim (P9 bookDepth precedent: pre-registration is
required only when a signal test is proposed). Builds `taker_flow_5m` inside
`bybit_full_pit`: per (symbol, date) parquet partitions of 5-minute signed
taker flow aggregated from the side-flagged public tick-trade archive
(`https://public.bybit.com/trading/SYMBOL/SYMBOLYYYY-MM-DD.csv.gz`).

Scope: the deduped winner_base ensemble event set (the frozen component
ledgers used by the OI scout), each event needing dates covering
[entry_signal_ts - LOOKBACK_H, entry_signal_ts]. Coverage is therefore
event-anchored, NOT uniform — any consumer must treat absent partitions as
"not downloaded", never as "no trades". A `_manifest.json` records every
attempted (symbol, date) with its status so absence is auditable.

Resume-safe: existing partitions with a manifest entry are skipped.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe \
        scripts/bybit_taker_flow_backfill.py [--scope-only] [--max-files N] [--workers W]
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import continuous_ensemble_rebalance_scout as scout  # noqa: E402

from liquidity_migration.archive import (  # noqa: E402
    ArchiveFileNotFoundError,
    download_public_trade_archive,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOT = SHARED / "bybit_full_pit"
DATASET = ROOT / "taker_flow_5m"
MANIFEST = DATASET / "_manifest.json"
OHLC_DATASET = ROOT / "tick_ohlc_1m"
OHLC_MANIFEST = OHLC_DATASET / "_manifest.json"
EMIT_OHLC = False  # set by --ohlc: also write 1m OHLC-from-ticks partitions
TMP = SHARED / "_bybit_taker_flow_tmp"
COMP_PRIORITY = ["turn4p5", "turn3p3", "turn4p3", "age210tp14"]
MS_H = 3_600_000
MS_5M = 300_000
MS_1M = 60_000
LOOKBACK_H = 50  # covers a 48h causal baseline + the 2h staleness guard
BASE_URL = "https://public.bybit.com/trading"

_manifest_lock = threading.Lock()


def load_bybit_events() -> pl.DataFrame:
    frames = []
    for rank, src in enumerate(COMP_PRIORITY):
        spec = scout.SOURCES[src]
        t = pl.read_csv(
            spec.root / "bybit" / spec.cell / "continuous_trades.csv",
            columns=["symbol", "entry_signal_ts_ms"],
        ).with_columns(pl.lit(rank).alias("prio"))
        frames.append(t)
    return (
        pl.concat(frames)
        .sort("prio")
        .unique(subset=["symbol", "entry_signal_ts_ms"], keep="first")
        .drop("prio")
    )


def needed_symbol_dates(events: pl.DataFrame, *, forward_hours: int = 0) -> list[tuple[str, str]]:
    """Dates covering [t0 - LOOKBACK_H, t0 + forward_hours] per event. forward_hours=0
    is the original pre-entry layer; >0 extends to the POST-entry intra-trade path
    (execution-realism input: stop/TP/sniper fill calibration from the tick tape)."""
    need: set[tuple[str, str]] = set()
    rows = events.select(
        pl.col("symbol").cast(pl.Utf8),
        pl.col("entry_signal_ts_ms").cast(pl.Int64),
    )
    for sym, t0 in rows.iter_rows():
        d0 = dt.datetime.fromtimestamp((t0 - LOOKBACK_H * MS_H) / 1000, tz=dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp((t0 + forward_hours * MS_H) / 1000, tz=dt.timezone.utc).date()
        d = d0
        while d <= d1:
            need.add((sym, d.isoformat()))
            d += dt.timedelta(days=1)
    return sorted(need)


def aggregate_taker_flow_5m(csv_bytes: bytes, *, symbol: str) -> pl.DataFrame:
    header = pl.read_csv(io.BytesIO(csv_bytes), n_rows=0)
    required = {"timestamp", "side", "size", "price"}
    if not required.issubset(set(header.columns)):
        raise ValueError(f"unsupported archive schema for {symbol}: {header.columns}")
    frame = pl.read_csv(
        io.BytesIO(csv_bytes),
        columns=["timestamp", "side", "size", "price"],
        schema_overrides={
            "timestamp": pl.Float64,
            "side": pl.Utf8,
            "size": pl.Float64,
            "price": pl.Float64,
        },
    )
    if frame.is_empty():
        return pl.DataFrame()
    out = (
        frame.select(
            ((pl.col("timestamp").cast(pl.Float64) * 1000).cast(pl.Int64) // MS_5M * MS_5M).alias("ts_ms"),
            pl.col("side").cast(pl.Utf8),
            (pl.col("price").cast(pl.Float64) * pl.col("size").cast(pl.Float64)).alias("quote_value"),
        )
        .group_by("ts_ms")
        .agg(
            pl.col("quote_value").filter(pl.col("side") == "Buy").sum().fill_null(0.0).alias("taker_buy_quote"),
            pl.col("quote_value").filter(pl.col("side") == "Sell").sum().fill_null(0.0).alias("taker_sell_quote"),
            pl.col("side").filter(pl.col("side") == "Buy").len().alias("n_buy"),
            pl.col("side").filter(pl.col("side") == "Sell").len().alias("n_sell"),
        )
        .with_columns(pl.lit(symbol).alias("symbol"), pl.lit("bybit_public_trades").alias("source"))
        .sort("ts_ms")
        .select(["ts_ms", "symbol", "taker_buy_quote", "taker_sell_quote", "n_buy", "n_sell", "source"])
    )
    return out


def aggregate_tick_ohlc_1m(csv_bytes: bytes, *, symbol: str) -> pl.DataFrame:
    """1m OHLC + activity from the side-flagged tick tape (the intra-trade PRICE
    path the 5m flow aggregation discards — execution-realism input: stop/TP
    ordering, fill severity, sniper wick verification)."""
    frame = pl.read_csv(
        io.BytesIO(csv_bytes),
        columns=["timestamp", "size", "price"],
        schema_overrides={"timestamp": pl.Float64, "size": pl.Float64, "price": pl.Float64},
    )
    if frame.is_empty():
        return pl.DataFrame()
    return (
        frame.with_columns(
            ((pl.col("timestamp") * 1000).cast(pl.Int64)).alias("tms"),
        )
        .sort("tms")
        .with_columns((pl.col("tms") // MS_1M * MS_1M).alias("ts_ms"))
        .group_by("ts_ms", maintain_order=True)
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            (pl.col("price") * pl.col("size")).sum().alias("quote_volume"),
            pl.len().alias("n_trades"),
        )
        .with_columns(pl.lit(symbol).alias("symbol"), pl.lit("bybit_public_trades").alias("source"))
        .sort("ts_ms")
    )


def load_manifest(path: Path = MANIFEST) -> dict[str, str]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict[str, str], path: Path = MANIFEST) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def process_one(sym: str, date: str) -> tuple[str, str]:
    key = f"{sym}|{date}"
    part_dir = DATASET / f"date={date}" / f"symbol={sym}"
    url = f"{BASE_URL}/{sym}/{sym}{date}.csv.gz"
    local = TMP / sym / f"{sym}{date}.csv.gz"
    try:
        archive_path = download_public_trade_archive(url, local)
        raw = Path(archive_path).read_bytes()
        csv_bytes = gzip.decompress(raw) if str(archive_path).endswith(".gz") else raw
        flow_part = part_dir / "part.parquet"
        if not flow_part.exists():
            flows = aggregate_taker_flow_5m(csv_bytes, symbol=sym)
            if flows.is_empty():
                status = "empty"
            else:
                part_dir.mkdir(parents=True, exist_ok=True)
                flows.write_parquet(flow_part)
                status = "ok"
        else:
            status = "ok"
        if EMIT_OHLC:
            ohlc = aggregate_tick_ohlc_1m(csv_bytes, symbol=sym)
            if not ohlc.is_empty():
                ohlc_dir = OHLC_DATASET / f"date={date}" / f"symbol={sym}"
                ohlc_dir.mkdir(parents=True, exist_ok=True)
                ohlc.write_parquet(ohlc_dir / "part.parquet")
            elif status == "ok":
                status = "empty"
        Path(archive_path).unlink(missing_ok=True)
        return key, status
    except ArchiveFileNotFoundError:
        return key, "404"
    except Exception as exc:  # noqa: BLE001 - per-file failures must not kill the run
        return key, f"failed: {exc}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope-only", action="store_true", help="report the download set and exit")
    ap.add_argument("--max-files", type=int, default=0, help="stop after N downloads (0 = all)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--forward-hours", type=int, default=0,
                    help="also cover [t0, t0+N hours] per event (post-entry intra-trade path)")
    ap.add_argument("--ohlc", action="store_true",
                    help="ALSO write 1m OHLC-from-ticks partitions (tick_ohlc_1m, own manifest)")
    args = ap.parse_args()
    global EMIT_OHLC
    EMIT_OHLC = bool(args.ohlc)
    manifest_path = OHLC_MANIFEST if args.ohlc else MANIFEST

    events = load_bybit_events()
    pairs = needed_symbol_dates(events, forward_hours=args.forward_hours)
    print(f"events={events.height} unique_symbol_dates={len(pairs)} "
          f"symbols={len({s for s, _ in pairs})} "
          f"date_range=[{min(d for _, d in pairs)} .. {max(d for _, d in pairs)}]", flush=True)
    if args.scope_only:
        return

    manifest = load_manifest(manifest_path)
    todo = [(s, d) for s, d in pairs if f"{s}|{d}" not in manifest
            or manifest[f"{s}|{d}"].startswith("failed")]
    if args.max_files:
        todo = todo[: args.max_files]
    print(f"todo={len(todo)} (manifest has {len(manifest)} entries)", flush=True)
    if not todo:
        return

    TMP.mkdir(parents=True, exist_ok=True)
    done = 0
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_one, s, d): (s, d) for s, d in todo}
        for fut in as_completed(futures):
            key, status = fut.result()
            bucket = status.split(":")[0]
            counts[bucket] = counts.get(bucket, 0) + 1
            with _manifest_lock:
                manifest[key] = status
                done += 1
                if done % 200 == 0:
                    save_manifest(manifest, manifest_path)
                    print(f"  {done}/{len(todo)} {counts}", flush=True)
    save_manifest(manifest, manifest_path)
    print(f"DONE {done}/{len(todo)} {counts}", flush=True)


if __name__ == "__main__":
    main()
