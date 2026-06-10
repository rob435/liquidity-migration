"""Live liquidation collectors — Bybit allLiquidation + Binance forceOrder (P3, 2026-06-10).

Raw liquidation HISTORY is unbuyable (Binance deleted its Vision archive, Bybit never
published one, third-party archives gap our window), so forward collection is the only
way this data will ever exist. Two append-only WS listeners writing one JSONL file per
venue per UTC day under ``data/liquidations/{venue}/{YYYY-MM-DD}.jsonl``. NO order
path, no credentials, public streams only. Known stream caveat (recorded in the
scoping note): both venues sample/throttle their public liquidation broadcasts, so
this is a FLOOR on liquidation activity — still the best obtainable signal.

    .venv/bin/python -m liquidity_migration.liquidation_collector --root data/liquidations
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
BYBIT_INSTRUMENTS = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
BINANCE_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"
BYBIT_TOPICS_PER_SUBSCRIBE = 10


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_bybit_event(msg: dict[str, Any], recv_ms: int) -> list[dict[str, Any]]:
    """Rows from a Bybit v5 ``allLiquidation.*`` message (empty for non-liq frames)."""
    topic = str(msg.get("topic", ""))
    if not topic.startswith("allLiquidation"):
        return []
    data = msg.get("data")
    if isinstance(data, dict):
        data = [data]
    rows = []
    for d in data or []:
        try:
            rows.append({
                "recv_ms": recv_ms,
                "venue": "bybit",
                "symbol": str(d.get("s", "")),
                "side": str(d.get("S", "")),          # side of the LIQUIDATED order
                "qty": float(d.get("v", 0.0)),
                "price": float(d.get("p", 0.0)),
                "ts_ms": int(d.get("T", recv_ms)),
            })
        except (TypeError, ValueError):
            continue
    return [r for r in rows if r["symbol"] and r["qty"] > 0.0]


def parse_binance_event(msg: dict[str, Any], recv_ms: int) -> list[dict[str, Any]]:
    """Rows from a Binance UM ``!forceOrder@arr`` message (single or list shape)."""
    events = msg if isinstance(msg, list) else [msg]
    rows = []
    for e in events:
        if not isinstance(e, dict) or e.get("e") != "forceOrder":
            continue
        o = e.get("o") or {}
        try:
            rows.append({
                "recv_ms": recv_ms,
                "venue": "binance",
                "symbol": str(o.get("s", "")),
                "side": str(o.get("S", "")),
                "qty": float(o.get("q", 0.0)),
                "price": float(o.get("ap") or o.get("p") or 0.0),
                "ts_ms": int(o.get("T", e.get("E", recv_ms))),
            })
        except (TypeError, ValueError):
            continue
    return [r for r in rows if r["symbol"] and r["qty"] > 0.0]


class JsonlDayWriter:
    """Thread-safe append-only writer, one file per venue per UTC day."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self.written = 0
        # Per-venue counts so the alive heartbeat can show a SILENT leg (a venue
        # stuck at 0 while the other streams) — a totals-only counter hid exactly
        # that on 2026-06-10 (binance leg quiet for 70+ min, indistinguishable
        # from healthy in the journal).
        self.written_by_venue: dict[str, int] = {}

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._lock:
            by_path: dict[Path, list[str]] = {}
            venue_counts: dict[str, int] = {}
            for r in rows:
                day = datetime.fromtimestamp(r["recv_ms"] / 1000, tz=timezone.utc).date().isoformat()
                p = self.root / r["venue"] / f"{day}.jsonl"
                by_path.setdefault(p, []).append(json.dumps(r, separators=(",", ":")))
                venue = str(r["venue"])
                venue_counts[venue] = venue_counts.get(venue, 0) + 1
            for p, lines in by_path.items():
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                self.written += len(lines)
            for venue, count in venue_counts.items():
                self.written_by_venue[venue] = self.written_by_venue.get(venue, 0) + count


def bybit_linear_symbols() -> list[str]:
    """All currently trading linear symbols (cursor-paginated REST; public)."""
    import urllib.request

    symbols: list[str] = []
    cursor = ""
    for _ in range(10):  # paginate defensively
        url = BYBIT_INSTRUMENTS + (f"&cursor={cursor}" if cursor else "")
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - public API
            payload = json.loads(resp.read())
        result = payload.get("result", {})
        for item in result.get("list", []):
            if str(item.get("status", "")) == "Trading":
                symbols.append(str(item.get("symbol", "")))
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    return [s for s in symbols if s]


def _run_bybit(writer: JsonlDayWriter, stop: threading.Event) -> None:
    import websocket

    while not stop.is_set():
        try:
            symbols = bybit_linear_symbols()
            _logger.info("bybit collector: subscribing allLiquidation for %d symbols", len(symbols))

            def on_open(ws: Any) -> None:
                for i in range(0, len(symbols), BYBIT_TOPICS_PER_SUBSCRIBE):
                    chunk = symbols[i:i + BYBIT_TOPICS_PER_SUBSCRIBE]
                    ws.send(json.dumps({"op": "subscribe",
                                        "args": [f"allLiquidation.{s}" for s in chunk]}))

            def on_message(_ws: Any, message: str) -> None:
                try:
                    writer.write(parse_bybit_event(json.loads(message), now_ms()))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            ws = websocket.WebSocketApp(BYBIT_WS, on_open=on_open, on_message=on_message)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:  # noqa: BLE001 — collector must outlive any disconnect
            _logger.exception("bybit collector error; reconnecting")
        if not stop.is_set():
            time.sleep(10)


def _run_binance(writer: JsonlDayWriter, stop: threading.Event) -> None:
    import websocket

    while not stop.is_set():
        try:
            def on_message(_ws: Any, message: str) -> None:
                try:
                    writer.write(parse_binance_event(json.loads(message), now_ms()))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            # Connection-state logging is load-bearing here: run_forever returns
            # (rather than raises) on a refused/dropped connection, so without
            # these handlers a permanently-failing leg retried SILENTLY forever
            # and the journal showed nothing — observed 2026-06-10 while
            # diagnosing a zero-row binance leg.
            def on_open(_ws: Any) -> None:
                _logger.info("binance collector: connected to %s", BINANCE_WS)

            def on_error(_ws: Any, error: Any) -> None:
                _logger.warning("binance collector ws error: %s", error)

            def on_close(_ws: Any, status_code: Any, msg: Any) -> None:
                _logger.warning("binance collector ws closed (code=%s msg=%s)", status_code, msg)

            ws = websocket.WebSocketApp(
                BINANCE_WS, on_message=on_message, on_open=on_open,
                on_error=on_error, on_close=on_close,
            )
            ws.run_forever(ping_interval=180, ping_timeout=30)
        except Exception:  # noqa: BLE001
            _logger.exception("binance collector error; reconnecting")
        if not stop.is_set():
            time.sleep(10)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/liquidations")
    ap.add_argument("--venues", default="bybit,binance")
    args = ap.parse_args()
    writer = JsonlDayWriter(Path(args.root))
    stop = threading.Event()
    threads = []
    venues = {v.strip() for v in args.venues.split(",") if v.strip()}
    if "bybit" in venues:
        threads.append(threading.Thread(target=_run_bybit, args=(writer, stop), daemon=True, name="bybit-liq"))
    if "binance" in venues:
        threads.append(threading.Thread(target=_run_binance, args=(writer, stop), daemon=True, name="binance-liq"))
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(600)
            per_venue = " ".join(
                f"{venue}={writer.written_by_venue.get(venue, 0)}" for venue in sorted(venues)
            )
            _logger.info(
                "liquidation collector alive: %d rows written (%s)", writer.written, per_venue,
            )
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
