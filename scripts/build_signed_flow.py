"""Parallel builder for the signed_flow_1h dataset from Bybit public trade archives.

The CLI `download-data --datasets archive_trades` path ingests archives serially,
which is impractical for the full canonical window. This script reuses the exact
same leaf functions (download_public_trade_archive, read_public_trade_archive,
aggregate_signed_flow_1m, aggregate_signed_flow_1h, write_dataset) but downloads
and parses concurrently, so the produced aggregates are identical to the CLI path.

Each archive is streamed to a temp file and deleted after parsing, so peak disk
stays small even though the transient download volume is large. Already-present
(date, symbol) partitions are skipped, so the build is resumable.
"""
from __future__ import annotations

import argparse
import glob
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.archive import download_public_trade_archive, read_public_trade_archive
from liquidity_migration.config import load_config
from liquidity_migration.ingestion import aggregate_signed_flow_1h, aggregate_signed_flow_1m
from liquidity_migration.storage import write_dataset


def _dates(start: str, end: str) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


def _load_manifest(data_root: Path, dates: list[str]) -> pl.DataFrame:
    files: list[str] = []
    for d in dates:
        files += glob.glob(str(data_root / "archive_trade_manifest" / f"date={d}") + "/**/*.parquet", recursive=True)
    if not files:
        return pl.DataFrame()
    return pl.read_parquet(files).select(["symbol", "date", "url"]).unique()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="~/SHARED_DATA/bybit_fullpit_1h")
    ap.add_argument("--start", default=None, help="Inclusive start date YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="Inclusive end date YYYY-MM-DD")
    ap.add_argument("--symbols", default="", help="Optional comma-separated symbol allowlist")
    ap.add_argument("--jobs-csv", default=None, help="CSV with explicit symbol,date pairs to build")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--config", default="configs/volume_alpha.default.yaml")
    ap.add_argument("--flush-every", type=int, default=3000)
    args = ap.parse_args()

    root = Path(args.data_root).expanduser()
    cfg = load_config(args.config).trade_flow

    if args.jobs_csv:
        wanted = pl.read_csv(args.jobs_csv).select(
            pl.col("symbol").cast(pl.Utf8).str.to_uppercase(), pl.col("date").cast(pl.Utf8)
        ).unique()
        dates = sorted(wanted["date"].unique().to_list())
        manifest = _load_manifest(root, dates).join(wanted, on=["symbol", "date"], how="inner")
        scope = f"jobs-csv {args.jobs_csv} ({wanted.height} pairs)"
    else:
        if not args.start or not args.end:
            raise SystemExit("provide --start/--end or --jobs-csv")
        dates = _dates(args.start, args.end)
        manifest = _load_manifest(root, dates)
        if args.symbols:
            allow = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
            manifest = manifest.filter(pl.col("symbol").is_in(list(allow)))
        scope = f"{args.start}..{args.end}"

    if manifest.is_empty():
        print(f"no manifest rows for {scope}", flush=True)
        return 1

    jobs = manifest.sort(["date", "symbol"]).to_dicts()
    flow_root = root / "signed_flow_1h"
    pending = [
        j for j in jobs
        if not (flow_root / f"date={j['date']}" / f"symbol={j['symbol']}" / "part.parquet").exists()
    ]
    print(
        f"build signed_flow_1h: {scope}  "
        f"manifest={len(jobs)}  already_present={len(jobs) - len(pending)}  to_build={len(pending)}  "
        f"workers={args.workers}",
        flush=True,
    )
    if not pending:
        print("nothing to build", flush=True)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="signed_flow_"))

    def work(job: dict) -> tuple[dict, pl.DataFrame | None, str | None]:
        lp = tmp / job["symbol"] / f"{job['symbol']}{job['date']}.csv.gz"
        try:
            path = download_public_trade_archive(job["url"], lp)
            trades = read_public_trade_archive(path, symbol=job["symbol"])
            flow_1h = aggregate_signed_flow_1h(aggregate_signed_flow_1m(trades, config=cfg))
            return job, flow_1h, None
        except Exception as exc:  # noqa: BLE001 - record and continue
            return job, None, f"{type(exc).__name__}: {exc}"
        finally:
            try:
                lp.unlink(missing_ok=True)
            except OSError:
                pass

    started = time.time()
    done = ok = empty = failed = 0
    buffer: list[pl.DataFrame] = []
    failures: list[str] = []
    written_rows = 0

    def flush() -> None:
        nonlocal buffer, written_rows
        frames = [f for f in buffer if f is not None and not f.is_empty()]
        if frames:
            batch = pl.concat(frames, how="vertical_relaxed")
            write_dataset(batch, root, "signed_flow_1h")
            written_rows += batch.height
        buffer = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed([ex.submit(work, j) for j in pending]):
            job, frame, err = fut.result()
            done += 1
            if err is not None:
                failed += 1
                if len(failures) < 50:
                    failures.append(f"{job['symbol']} {job['date']}: {err}")
            elif frame is None or frame.is_empty():
                empty += 1
            else:
                ok += 1
                buffer.append(frame)
            if len(buffer) >= args.flush_every:
                flush()
            if done % 2000 == 0 or done == len(pending):
                el = time.time() - started
                rate = done / el if el else 0.0
                eta = (len(pending) - done) / rate / 60 if rate else 0.0
                print(
                    f"  {done}/{len(pending)}  ok={ok} empty={empty} failed={failed}  "
                    f"{rate:.1f}/s  eta={eta:.0f}min  written_rows={written_rows}",
                    flush=True,
                )
    flush()
    el = time.time() - started
    print(
        f"DONE in {el / 60:.1f}min  ok={ok} empty={empty} failed={failed}  "
        f"signed_flow_1h rows written this run={written_rows}",
        flush=True,
    )
    if failures:
        print(f"first {len(failures)} failures:", flush=True)
        for line in failures:
            print(f"  {line}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
