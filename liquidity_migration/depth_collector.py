"""Forward Bybit order-book depth collector — hourly band snapshots (DC1 follow-on).

Bybit publishes NO historical order-book data, so forward capture is the only way
the DEPLOYED venue will ever have the DC1-style depth/capacity measurement (the
binance `bookdepth` layer exists because Vision archives it; bybit has nothing).
Hourly public-REST snapshots (no credentials, no order path) aggregated to the
binance-bookdepth-compatible band schema: cumulative quote notional within
±{0.2, 1, 2, 3, 4, 5}% of mid.

Honesty rule: a 500-level snapshot may not REACH a band on deep books — bands
beyond the snapshot's span are written as NULL, never zero, and ``bid_span_pct`` /
``ask_span_pct`` record how far each side actually reaches.

Append-only JSONL, one file per UTC day under ``<root>/bybit/<YYYY-MM-DD>.jsonl``
(the liquidation-collector convention).

    .venv/bin/python -m liquidity_migration.depth_collector --root data/depth --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

_logger = logging.getLogger(__name__)

BYBIT_ORDERBOOK = "https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}&limit=500"
BYBIT_INSTRUMENTS = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000&cursor={cursor}"
BANDS_PCT = (0.2, 1.0, 2.0, 3.0, 4.0, 5.0)
REQUEST_PACING_SECONDS = 0.25  # ~4 req/s, far inside bybit's public REST limits
HTTP_TIMEOUT = 10.0
# A single hourly cycle must not bleed into the next hour: an exchange-latency spike
# would otherwise stretch one serial cycle past 60 min, collapsing the next-cycle gap
# to the 60s floor or skipping an hour, silently degrading the documented HOURLY
# cadence (depth-collector-2). Abort the remaining symbols once the budget is spent.
CYCLE_BUDGET_SECONDS = 2400.0  # 40 min — leaves headroom inside the hour
# Retention: gzip day files older than this and prune compressed files past the
# window so two always-on append-only collectors cannot silently fill a small VPS
# disk (depth-collector-3).
RETENTION_GZIP_AFTER_DAYS = 7
RETENTION_PRUNE_AFTER_DAYS = 60
# Free-disk floor: refuse to start a cycle below this so an ENOSPC cannot tear down
# the collector mid-write and (worse) starve the co-located live trading services.
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024  # 512 MiB


def band_notionals(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> dict[str, Any] | None:
    """Cumulative quote notional to each ±band of mid; NULL beyond the book's span.

    ``bids``/``asks`` are (price, size) levels sorted best-first (bybit order).
    Returns None when either side is empty (no mid).
    """
    if not bids or not asks:
        return None
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    if best_bid <= 0 or best_ask <= 0:
        return None
    mid = 0.5 * (best_bid + best_ask)
    out: dict[str, Any] = {"mid": mid}
    for side, levels, sign in (("bid", bids, -1.0), ("ask", asks, 1.0)):
        worst = float(levels[-1][0])
        span_pct = abs(worst - mid) / mid * 100.0
        out[f"{side}_span_pct"] = round(span_pct, 4)
        out[f"n_{side}_levels"] = len(levels)
        cum = 0.0
        idx = 0
        for band in BANDS_PCT:
            edge = mid * (1.0 + sign * band / 100.0)
            while idx < len(levels):
                px, sz = float(levels[idx][0]), float(levels[idx][1])
                inside = px >= edge if side == "bid" else px <= edge
                if not inside:
                    break
                cum += px * sz
                idx += 1
            key = f"{side}_{str(band).replace('.', 'p')}"
            # the band is only MEASURED if the snapshot reaches its edge
            out[key] = round(cum, 2) if span_pct >= band else None
    return out


def _get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "liqmig-depth-collector"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def trading_universe() -> list[str]:
    """All currently-trading USDT linear perps (cursor-paginated public REST).

    Pagination is capped at 10 pages (mirrors the liquidation collector's
    defensive cap) and a repeating cursor breaks the loop — a misbehaving
    endpoint must not spin this into an unbounded request loop.
    """
    symbols: list[str] = []
    cursor = ""
    seen_cursors: set[str] = set()
    for _ in range(10):  # paginate defensively
        payload = _get_json(BYBIT_INSTRUMENTS.format(cursor=cursor))
        result = payload.get("result") or {}
        for item in result.get("list") or []:
            sym = str(item.get("symbol", ""))
            if item.get("status") == "Trading" and sym.endswith("USDT"):
                symbols.append(sym)
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    return sorted(set(symbols))


def _refresh_universe(previous: list[str]) -> list[str]:
    """Fetch the live Trading universe; on failure keep ``previous``.

    A transient instruments-info failure used to raise out of main()'s loop and
    kill the daemon. Network/HTTP errors (URLError/HTTPError/timeouts are all
    OSError) and bad-payload errors (JSONDecodeError is a ValueError) are
    logged and the previous universe is kept until the next cycle's refresh.
    """
    try:
        return trading_universe()
    except (OSError, ValueError) as exc:
        _logger.warning(
            "universe refresh failed (%s); keeping previous universe of %d symbols",
            exc,
            len(previous),
        )
        return list(previous)


def snapshot_symbol(symbol: str) -> dict[str, Any] | None:
    payload = _get_json(BYBIT_ORDERBOOK.format(symbol=symbol))
    result = payload.get("result") or {}
    bids = [(float(p), float(s)) for p, s in result.get("b") or []]
    asks = [(float(p), float(s)) for p, s in result.get("a") or []]
    bands = band_notionals(bids, asks)
    if bands is None:
        return None
    return {"recv_ms": int(time.time() * 1000), "venue": "bybit", "symbol": symbol, **bands}


def iter_jsonl_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed rows from a depth JSONL file, TOLERATING a truncated trailing
    line (depth-collector-4). A SIGKILL/OOM/power-loss mid-write can leave the final
    buffered line without its newline; a naive line-by-line loader would raise. A
    parse error on the LAST line is skipped (the bounded crash-boundary damage);
    a parse error on any EARLIER line is a genuine corruption and re-raised.
    """
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    last = len(lines) - 1
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError:
            if idx == last:
                _logger.warning("skipping truncated trailing line in %s", path)
                continue
            raise


def collect_cycle(
    root: Path, symbols: list[str], *, cycle_budget_seconds: float = CYCLE_BUDGET_SECONDS
) -> dict[str, int]:
    # CONSUMER CONTRACT: the filename day is stamped at CYCLE START, so a cycle
    # that straddles UTC midnight (only under extreme HTTP latency; cycles are
    # top-of-hour aligned) appends its tail rows to the PREVIOUS day's file.
    # Always key on each row's `recv_ms`, never on the filename day.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = root / "bybit" / f"{day}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + max(float(cycle_budget_seconds), 0.0) if cycle_budget_seconds > 0 else None
    ok = err = skipped = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for i, sym in enumerate(symbols):
            if deadline is not None and time.monotonic() >= deadline:
                # Cadence guard (depth-collector-2): abort the rest so the cycle
                # cannot bleed past the hour. The captured slice is still written;
                # the gap is observable in the duration/skipped telemetry below.
                skipped = len(symbols) - i
                _logger.warning(
                    "depth cycle hit %.0fs budget after %d/%d symbols; skipping %d remaining",
                    cycle_budget_seconds, i, len(symbols), skipped,
                )
                break
            try:
                row = snapshot_symbol(sym)
                if row is not None:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                    ok += 1
            except Exception as exc:  # noqa: BLE001 - one bad symbol never kills the cycle
                err += 1
                _logger.warning("depth snapshot failed for %s: %s", sym, exc)
            time.sleep(REQUEST_PACING_SECONDS)
        # Per-cycle flush+fsync (depth-collector-4): hourly data, so flushing once at
        # cycle end is cheap and shrinks the partial-line window to the fsync boundary.
        f.flush()
        os.fsync(f.fileno())
    duration = time.monotonic() - started
    _logger.info(
        "depth cycle done: %d ok, %d errors, %d skipped in %.1fs -> %s",
        ok, err, skipped, duration, out_path,
    )
    return {"ok": ok, "errors": err, "skipped": skipped, "duration_s": int(duration)}


def _file_day(path: Path) -> datetime | None:
    """Parse the UTC day from a ``<YYYY-MM-DD>.jsonl[.gz]`` filename, or None."""
    stem = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    try:
        return datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def enforce_retention(
    root: Path,
    *,
    gzip_after_days: int = RETENTION_GZIP_AFTER_DAYS,
    prune_after_days: int = RETENTION_PRUNE_AFTER_DAYS,
    now: datetime | None = None,
) -> dict[str, int]:
    """gzip day files older than ``gzip_after_days`` and delete (gz or raw) files
    older than ``prune_after_days`` (depth-collector-3). The CURRENT/most-recent day
    is never gzipped (it is still being appended to). Bounds disk growth of an
    always-on append-only collector on a small VPS.
    """
    bybit_dir = root / "bybit"
    if not bybit_dir.is_dir():
        return {"gzipped": 0, "pruned": 0}
    now = now or datetime.now(timezone.utc)
    gzip_cutoff = now - timedelta(days=gzip_after_days)
    prune_cutoff = now - timedelta(days=prune_after_days)
    gzipped = pruned = 0
    for path in sorted(bybit_dir.iterdir()):
        if not path.is_file():
            continue
        file_day = _file_day(path)
        if file_day is None:
            continue
        if file_day < prune_cutoff:
            path.unlink()
            pruned += 1
            continue
        if path.name.endswith(".jsonl") and file_day < gzip_cutoff:
            import gzip

            gz_path = path.with_suffix(path.suffix + ".gz")
            with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
            gzipped += 1
    if gzipped or pruned:
        _logger.info("depth retention: gzipped %d, pruned %d under %s", gzipped, pruned, bybit_dir)
    return {"gzipped": gzipped, "pruned": pruned}


def free_disk_bytes(path: Path) -> int:
    """Bytes free on the filesystem backing ``path`` (the first existing ancestor)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def build_arg_parser() -> argparse.ArgumentParser:
    # Exposed for the unit↔argparse parity test (unit ExecStart args must parse).
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/depth")
    ap.add_argument("--once", action="store_true", help="One cycle then exit (smoke/test).")
    ap.add_argument("--symbols", default=None, help="Comma list override (default: live Trading universe).")
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.root)
    symbols: list[str] = []
    while True:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",")]
        else:
            symbols = _refresh_universe(symbols)
        if not symbols:
            # First refresh failed and there is no previous universe to fall
            # back on — wait and retry instead of dying or spinning hot.
            time.sleep(60.0)
            continue
        _logger.info("depth collector: %d symbols", len(symbols))
        # Free-disk guard (depth-collector-3): two always-on append-only collectors
        # share a small VPS; refuse a cycle below the floor so an ENOSPC mid-write
        # cannot tear down this writer or starve the co-located live trading services.
        free = free_disk_bytes(root)
        if free < MIN_FREE_DISK_BYTES:
            _logger.error(
                "depth collector: free disk %d B below floor %d B under %s; skipping cycle",
                free, MIN_FREE_DISK_BYTES, root,
            )
            if args.once:
                return
            time.sleep(max(60.0, 3600.0 - (time.time() % 3600.0)))
            continue
        collect_cycle(root, symbols)
        # Retention pass (depth-collector-3): gzip aged day files and prune past the
        # window so disk growth of this append-only collector stays bounded.
        try:
            enforce_retention(root)
        except OSError as exc:  # retention must never kill the capture loop
            _logger.warning("depth retention pass failed: %s", exc)
        if args.once:
            return
        # align the next cycle to the next top-of-hour
        now = time.time()
        time.sleep(max(60.0, 3600.0 - (now % 3600.0)))


if __name__ == "__main__":
    main()
