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
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

BYBIT_ORDERBOOK = "https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}&limit=500"
BYBIT_INSTRUMENTS = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000&cursor={cursor}"
BANDS_PCT = (0.2, 1.0, 2.0, 3.0, 4.0, 5.0)
REQUEST_PACING_SECONDS = 0.25  # ~4 req/s, far inside bybit's public REST limits
HTTP_TIMEOUT = 10.0


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


def collect_cycle(root: Path, symbols: list[str]) -> dict[str, int]:
    # CONSUMER CONTRACT: the filename day is stamped at CYCLE START, so a cycle
    # that straddles UTC midnight (only under extreme HTTP latency; cycles are
    # top-of-hour aligned) appends its tail rows to the PREVIOUS day's file.
    # Always key on each row's `recv_ms`, never on the filename day.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = root / "bybit" / f"{day}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = err = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for sym in symbols:
            try:
                row = snapshot_symbol(sym)
                if row is not None:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                    ok += 1
            except Exception as exc:  # noqa: BLE001 - one bad symbol never kills the cycle
                err += 1
                _logger.warning("depth snapshot failed for %s: %s", sym, exc)
            time.sleep(REQUEST_PACING_SECONDS)
    _logger.info("depth cycle done: %d ok, %d errors -> %s", ok, err, out_path)
    return {"ok": ok, "errors": err}


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
        collect_cycle(root, symbols)
        if args.once:
            return
        # align the next cycle to the next top-of-hour
        now = time.time()
        time.sleep(max(60.0, 3600.0 - (now % 3600.0)))


if __name__ == "__main__":
    main()
