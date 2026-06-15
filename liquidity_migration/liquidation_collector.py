"""Live liquidation collectors — Bybit allLiquidation + Binance forceOrder (P3, 2026-06-10).

Raw liquidation HISTORY is unbuyable (Binance deleted its Vision archive, Bybit never
published one, third-party archives gap our window), so forward collection is the only
way this data will ever exist. Two append-only WS listeners writing one JSONL file per
venue per UTC day under ``data/liquidations/{venue}/{YYYY-MM-DD}.jsonl``. NO order
path, no credentials, public streams only. Known stream caveat (recorded in the
scoping note): both venues sample/throttle their public liquidation broadcasts, so
this is a FLOOR on liquidation activity — still the best obtainable signal.

    .venv/bin/python -m liquidity_migration.liquidation_collector --root data/liquidations

Row schema (one JSON object per line; this is a RAW tape — fields are stored as the
venue reports them, NOT normalized — so the eventual P12 calibration consumer must
read this note):

  * recv_ms : int  — local receive wall-clock ms (used for UTC-day file rotation).
  * venue   : str  — "bybit" | "binance".
  * symbol  : str  — venue-native perp symbol (e.g. "BTCUSDT").
  * side    : str  — RAW, per-venue casing AND semantics:
      - bybit:   d["S"] is title-case "Buy"/"Sell" = the side of the LIQUIDATED order.
      - binance: o["S"] is upper-case "BUY"/"SELL" = the side of the liquidation
                 (forceOrder); "SELL" means a LONG was liquidated, "BUY" a short.
    A consumer normalizing on, e.g., side == "Buy" would silently miss every binance
    row — lower-case and reconcile the per-venue meaning before deriving any feature.
  * qty     : float — base-asset size; only qty > 0 rows are kept.
  * price   : float — bybit "p"; binance "ap" (avg fill) else "p" fallback. Only
                price > 0 rows are kept, so notional (qty * price) is never polluted
                by a zero-price row (audit 2026-06-12 round 4).
  * ts_ms   : int  — venue event time (bybit "T"; binance "T" else "E" else recv_ms).
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
# Force a clean reconnect once a connection is this old. The bybit leg
# subscribes a symbol list fetched once per connection, so a weeks-long healthy
# connection would never subscribe newly listed perps; closing hands control
# back to the reconnect loop, which re-fetches the universe and resubscribes.
MAX_CONNECTION_AGE_SECONDS = 24 * 60 * 60


def now_ms() -> int:
    return int(time.time() * 1000)


def connection_expired(
    opened_monotonic: float,
    now_monotonic: float | None = None,
    max_age_seconds: float = MAX_CONNECTION_AGE_SECONDS,
) -> bool:
    """True when a WS connection opened at ``opened_monotonic`` has outlived
    ``max_age_seconds`` (monotonic clock — immune to wall-clock jumps).

    Checked in the message/heartbeat path so the age cap needs no extra timer
    thread; an expired connection is closed cleanly and the existing
    reconnect/backoff machinery re-fetches the symbol universe.
    """
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    return (now_monotonic - opened_monotonic) >= max_age_seconds


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
    # Drop zero/negative price the same way zero qty is dropped: a price=0 row (a
    # missing/garbage 'p') would pollute any notional aggregation in the consumer
    # (audit 2026-06-12 round 4).
    return [r for r in rows if r["symbol"] and r["qty"] > 0.0 and r["price"] > 0.0]


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
                # ap (avg fill) else p fallback. A string "0"/"0.00" ap is TRUTHY,
                # so `o.get("ap") or o.get("p")` would keep the zero ap and never
                # fall back; parse numerically and fall back only when ap is not > 0.
                "price": (float(o.get("ap") or 0.0) or float(o.get("p") or 0.0)),
                "ts_ms": int(o.get("T", e.get("E", recv_ms))),
            })
        except (TypeError, ValueError):
            continue
    # Drop zero/negative price (see parse_bybit_event): protects the consumer's
    # notional aggregations from a missing/garbage avg-price field.
    return [r for r in rows if r["symbol"] and r["qty"] > 0.0 and r["price"] > 0.0]


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
        # Rows lost to writer OSErrors (disk full / permissions). Counted, not
        # raised: an OSError previously propagated into websocket-client's
        # dispatcher and tore down the WS connection on EVERY message — a
        # reconnect storm against the venue while the disk stayed full, with
        # rows silently dropped anyway (audit 2026-06-12 round 3).
        self.dropped = 0

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._lock:
            # Group by (venue, path) so the per-venue counter can be advanced by the
            # rows that ACTUALLY landed, not by a pre-computed total — on a partial
            # disk-full failure that distinction is what keeps written_by_venue (the
            # silent-leg heartbeat) honest, the same way self.dropped must.
            by_path: dict[tuple[str, Path], list[str]] = {}
            for r in rows:
                day = datetime.fromtimestamp(r["recv_ms"] / 1000, tz=timezone.utc).date().isoformat()
                venue = str(r["venue"])
                p = self.root / venue / f"{day}.jsonl"
                by_path.setdefault((venue, p), []).append(json.dumps(r, separators=(",", ":")))
            for (venue, p), lines in by_path.items():
                # Write one line at a time (NOT a single "\n".join batch): a disk-full
                # OSError mid-batch then tears at most the in-flight line — every line
                # that already returned from fh.write is a complete "...\n" record — and
                # the dropped counter reflects ONLY the lines that did not land, instead
                # of over-counting earlier lines that were already flushed. This keeps
                # the append-only JSONL day file parseable by a strict
                # `for line in f: json.loads(line)` consumer (the unbuilt P12
                # liquidation-proxy tape reader) even when the box fills mid-flush
                # (audit 2026-06-12 round 4).
                written_here = 0
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with p.open("a", encoding="utf-8") as fh:
                        for line in lines:
                            fh.write(line + "\n")
                            written_here += 1
                except OSError as exc:
                    dropped_here = len(lines) - written_here
                    self.dropped += dropped_here
                    _logger.warning(
                        "liquidation writer OSError (%s): wrote %d, dropped %d row(s) for %s",
                        exc, written_here, dropped_here, p,
                    )
                self.written += written_here
                if written_here:
                    self.written_by_venue[venue] = self.written_by_venue.get(venue, 0) + written_here


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
            opened = {"monotonic": time.monotonic()}

            def _close_if_expired(ws: Any) -> bool:
                """Max-connection-age guard: the symbol list above is fetched once
                per connection, so a long-lived connection never subscribes newly
                listed perps. Closing returns control to this reconnect loop,
                which re-fetches the universe and resubscribes."""
                if not connection_expired(opened["monotonic"]):
                    return False
                _logger.info(
                    "bybit collector: connection older than %ds; closing to refresh the symbol universe",
                    MAX_CONNECTION_AGE_SECONDS,
                )
                ws.close()
                return True

            def on_open(ws: Any) -> None:
                opened["monotonic"] = time.monotonic()
                # websocket-client swallows an on_open exception (logs it, does
                # NOT reconnect), so a mid-burst send() failure would silently
                # leave the symbol-universe tail unsubscribed for the life of the
                # connection. Close on any failure to force a full re-subscribe.
                for i in range(0, len(symbols), BYBIT_TOPICS_PER_SUBSCRIBE):
                    chunk = symbols[i:i + BYBIT_TOPICS_PER_SUBSCRIBE]
                    try:
                        ws.send(json.dumps({"op": "subscribe",
                                            "args": [f"allLiquidation.{s}" for s in chunk]}))
                    except Exception as exc:  # noqa: BLE001 - any send failure must trigger reconnect
                        _logger.warning(
                            "bybit collector: subscribe send failed at chunk %d/%d (%s); "
                            "closing to force resubscribe",
                            i // BYBIT_TOPICS_PER_SUBSCRIBE + 1,
                            (len(symbols) + BYBIT_TOPICS_PER_SUBSCRIBE - 1) // BYBIT_TOPICS_PER_SUBSCRIBE,
                            exc,
                        )
                        ws.close()
                        return

            def on_message(ws: Any, message: str) -> None:
                try:
                    payload = json.loads(message)
                except (json.JSONDecodeError, TypeError, ValueError):
                    # Still honour the age cap even on an unparseable frame so a quiet
                    # connection past its cap is recycled.
                    _close_if_expired(ws)
                    return
                # Surface a failed subscribe ack (e.g. Bybit subscribe-rate-limit
                # during the burst) instead of dropping it silently — parse_bybit_event
                # returns [] for ack frames, so without this it would never be seen.
                if isinstance(payload, dict) and payload.get("op") == "subscribe" and payload.get("success") is False:
                    _logger.warning("bybit collector: subscribe REJECTED: %s", payload.get("ret_msg") or payload)
                    return
                # Write THIS frame's rows BEFORE the age-cap close: _close_if_expired
                # closes the ws but the payload is already in hand, so checking expiry
                # first discarded the rows of the frame that happened to trip the
                # ~24h rollover — avoidable loss of unbuyable data (audit 2026-06-12
                # round 4). Write-then-check loses nothing; the rollover is rare.
                try:
                    writer.write(parse_bybit_event(payload, now_ms()))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                _close_if_expired(ws)

            def on_pong(ws: Any, _message: Any) -> None:
                # Heartbeat path: pongs arrive every ping_interval, so the age
                # check fires even when the liquidation stream itself is silent.
                _close_if_expired(ws)

            ws = websocket.WebSocketApp(BYBIT_WS, on_open=on_open, on_message=on_message, on_pong=on_pong)
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


def build_arg_parser() -> argparse.ArgumentParser:
    # Exposed for the unit↔argparse parity test (unit ExecStart args must parse).
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/liquidations")
    ap.add_argument("--venues", default="bybit,binance")
    return ap


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_arg_parser().parse_args()
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
                "liquidation collector alive: %d rows written (%s) dropped=%d",
                writer.written, per_venue, writer.dropped,
            )
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
