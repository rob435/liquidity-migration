#!/usr/bin/env python3
"""Quote lab: rest tiny post-only orders at the touch on many illiquid Bybit
demo symbols at once, reprice as the touch moves, flatten every fill at once
with a reduce-only market order, and write an auditable receipt.

Operating constraints (same shape as probe_passive_fill_ab.py):
- Demo credentials only, via ``resolve_demo_credentials``.
- ``DemoAccountMutationLease`` refuses to run beside any other mutator.
- Preflight requires a flat account with no open orders; the run ends with a
  full cancel + flatten and the final flatness lands in the receipt.
- Per-order size is minimum-notional, capped by --max-quote-notional-usdt;
  total inventory is capped inside the engine.

Usage:
  python scripts/research/run_quote_lab.py \
      --symbols AAAUSDT,BBBUSDT --duration-minutes 90 \
      --output-dir <run-dir> --confirm-demo-quote-lab
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.account.account_owner_lease import (  # noqa: E402
    DemoAccountIdentity,
    DemoAccountMutationLease,
)
from liquidity_migration.account.market_capture import BybitRawPublicMarketStream  # noqa: E402
from liquidity_migration.core.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.marketdata.bybit_market_data import BybitRestRateLimiter  # noqa: E402
from liquidity_migration.research.execution.passive_fill_probe import (  # noqa: E402
    ARM_POST_ONLY,
    AttemptRecord,
    itt_cost_bp,
)
from liquidity_migration.research.execution.quote_lab.engine import (  # noqa: E402
    EngineConfig,
    QuoteEngine,
    SymbolSpec,
    WouldCrossReject,
)
from liquidity_migration.research.execution.quote_lab.records import (  # noqa: E402
    TERMINAL_FILLED,
    TERMINAL_REJECTED_WOULD_CROSS,
    TERMINAL_TIMEOUT_CANCELLED,
    JsonlWriter,
    QuoteAttempt,
)
from liquidity_migration.venue.bybit import (  # noqa: E402
    BybitPrivateClient,
    BybitRequestRejected,
    api_key_allows_order_submit,
    resolve_demo_credentials,
)

RECEIPT_KIND = "liquidity_migration_quote_lab_receipt"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_NAME = "quote_lab_receipt.json"
EVENTS_JOURNAL_NAME = "quote_lab_events.jsonl"
ATTEMPTS_JOURNAL_NAME = "quote_lab_attempts.jsonl"
LINK_PREFIX = "lm-qlab-"
# Measured per-side taker cost; the ITT fallback fee for unfilled passive attempts.
TAKER_FEE_BP_FALLBACK = 7.78
INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info?category=linear&symbol="
WS_WARMUP_SECONDS = 10.0


class SystemClock:
    def now_ns(self) -> int:
        return time.time_ns()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class BybitQuoteVenue:
    """QuoteVenue over the private demo client. A create-time reject on a
    post-only order is classified would-cross, matching the passive probe."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def place_post_only(
        self, symbol: str, side: str, price: str, qty: str, order_link_id: str
    ) -> Mapping[str, Any]:
        try:
            return self._client.place_order(
                symbol=symbol,
                side=side,
                orderType="Limit",
                price=price,
                qty=qty,
                timeInForce="PostOnly",
                orderLinkId=order_link_id,
            )
        except BybitRequestRejected as exc:
            raise WouldCrossReject(str(exc)[:300]) from exc

    def place_reduce_only_market(
        self, symbol: str, side: str, qty: str, order_link_id: str
    ) -> Mapping[str, Any]:
        return self._client.place_order(
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty,
            reduceOnly=True,
            orderLinkId=order_link_id,
        )

    def cancel(self, symbol: str, order_link_id: str) -> None:
        self._client.cancel_order(symbol=symbol, order_link_id=order_link_id)

    def open_orders(self) -> list[dict[str, Any]]:
        return self._client.get_open_orders(settle_coin="USDT")

    def positions(self) -> list[dict[str, Any]]:
        return self._client.get_positions(settle_coin="USDT")

    def order_row(self, symbol: str, order_link_id: str) -> Mapping[str, Any] | None:
        for row in self._client.get_order_history(
            symbol=symbol, order_link_id=order_link_id, limit=10
        ):
            if str(row.get("orderLinkId") or "") == order_link_id:
                return dict(row)
        return None

    def fill_fee_bp(self, symbol: str, order_link_id: str) -> tuple[float | None, bool]:
        total_fee = 0.0
        total_value = 0.0
        observed = False
        for row in self._client.get_trade_history(symbol=symbol, order_link_id=order_link_id):
            if str(row.get("orderLinkId") or "") != order_link_id:
                continue
            fee_raw, value_raw = row.get("execFee"), row.get("execValue")
            if fee_raw in (None, "") or value_raw in (None, ""):
                continue
            total_fee += float(fee_raw)
            total_value += float(value_raw)
            observed = True
        if not observed or total_value <= 0.0:
            return None, False
        return total_fee / total_value * 1e4, True


class _TouchBook:
    __slots__ = ("bids", "asks", "has_snapshot", "gap", "last_update_id", "last_monotonic")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.has_snapshot = False
        self.gap = False
        self.last_update_id = 0
        self.last_monotonic = 0.0


def _apply_levels(book: dict[float, float], raw: Any) -> None:
    if not isinstance(raw, (list, tuple)):
        return
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError):
            continue
        if price <= 0.0:
            continue
        if qty == 0.0:
            book.pop(price, None)
        else:
            book[price] = qty


class WsTouchSource:
    """Minimal top-of-book mirror over the raw public stream (depth 1).

    Raw shape (confirmed against market_capture parsing): topic
    ``orderbook.<depth>.<symbol>``, type snapshot/delta, data has ``s``, ``u``,
    and ``b``/``a`` arrays of [price, qty] strings; ``u == 1`` is a restart
    snapshot; a non-increasing ``u`` marks the book unhealthy until the next
    snapshot.
    """

    def __init__(self, symbols: list[str], *, stale_seconds: float = 10.0) -> None:
        self._books = {symbol.upper(): _TouchBook() for symbol in symbols}
        self._stale_seconds = stale_seconds
        self._lock = threading.Lock()

    def on_message(self, message: Mapping[str, Any]) -> None:
        topic = str(message.get("topic") or "")
        if not topic.startswith("orderbook."):
            return
        data = message.get("data")
        if not isinstance(data, Mapping):
            return
        symbol = str(data.get("s") or topic.rsplit(".", 1)[-1]).upper()
        message_type = str(message.get("type") or "").lower()
        try:
            update_id = int(data.get("u") or 0)
        except (TypeError, ValueError):
            update_id = 0
        with self._lock:
            book = self._books.get(symbol)
            if book is None:
                return
            is_snapshot = message_type == "snapshot" or update_id == 1
            if is_snapshot:
                book.bids.clear()
                book.asks.clear()
                _apply_levels(book.bids, data.get("b"))
                _apply_levels(book.asks, data.get("a"))
                book.has_snapshot = True
                book.gap = False
            elif not book.has_snapshot or book.gap:
                pass
            elif update_id and book.last_update_id and update_id <= book.last_update_id:
                book.gap = True
            else:
                _apply_levels(book.bids, data.get("b"))
                _apply_levels(book.asks, data.get("a"))
            book.last_update_id = update_id
            book.last_monotonic = time.monotonic()

    def best_bid(self, symbol: str) -> float | None:
        with self._lock:
            book = self._books.get(symbol.upper())
            if book is None or not book.bids:
                return None
            return max(book.bids)

    def best_ask(self, symbol: str) -> float | None:
        with self._lock:
            book = self._books.get(symbol.upper())
            if book is None or not book.asks:
                return None
            return min(book.asks)

    def healthy(self, symbol: str) -> bool:
        with self._lock:
            book = self._books.get(symbol.upper())
            if book is None or not book.has_snapshot or book.gap:
                return False
            if not book.bids or not book.asks:
                return False
            return time.monotonic() - book.last_monotonic < self._stale_seconds


class RestTouchSource:
    """Fallback touch source: round-robin ticker polling under the shared limiter."""

    def __init__(self, client: Any, symbols: list[str]) -> None:
        self._client = client
        self._symbols = [symbol.upper() for symbol in symbols]
        self._quotes: dict[str, tuple[float, float, float]] = {}
        self._cursor = 0
        self._stale_seconds = max(15.0, 2.0 * len(self._symbols))

    def refresh_next(self, count: int = 1) -> None:
        for _ in range(max(1, count)):
            symbol = self._symbols[self._cursor % len(self._symbols)]
            self._cursor += 1
            try:
                tickers = self._client.get_tickers(symbol=symbol)
            except Exception:  # noqa: BLE001 - a failed poll leaves the quote stale
                continue
            if not tickers:
                continue
            row = tickers[0]
            try:
                bid = float(row.get("bid1Price") or 0.0)
                ask = float(row.get("ask1Price") or 0.0)
            except (TypeError, ValueError):
                continue
            if bid > 0.0 and ask >= bid:
                self._quotes[symbol] = (bid, ask, time.monotonic())

    def best_bid(self, symbol: str) -> float | None:
        quote = self._quotes.get(symbol.upper())
        return quote[0] if quote else None

    def best_ask(self, symbol: str) -> float | None:
        quote = self._quotes.get(symbol.upper())
        return quote[1] if quote else None

    def healthy(self, symbol: str) -> bool:
        quote = self._quotes.get(symbol.upper())
        return quote is not None and time.monotonic() - quote[2] < self._stale_seconds


def _decimals(step_text: str) -> int:
    exponent = Decimal(step_text).normalize().as_tuple().exponent
    return max(0, -int(exponent))


def _reference_price(client: Any, symbol: str) -> float:
    tickers = client.get_tickers(symbol=symbol)
    if not tickers:
        raise RuntimeError(f"{symbol}: ticker unavailable")
    row = tickers[0]
    bid = float(row.get("bid1Price") or 0.0)
    ask = float(row.get("ask1Price") or 0.0)
    if bid > 0.0 and ask >= bid:
        return 0.5 * (bid + ask)
    last = float(row.get("lastPrice") or 0.0)
    if last > 0.0:
        return last
    raise RuntimeError(f"{symbol}: no usable reference price")


def _instrument_row(symbol: str) -> Mapping[str, Any]:
    with urllib.request.urlopen(INSTRUMENTS_URL + symbol, timeout=15) as response:
        payload = json.load(response)
    if payload.get("retCode") != 0:
        raise RuntimeError(f"{symbol}: instruments-info failed: {payload}")
    rows = payload.get("result", {}).get("list", [])
    if not rows:
        raise RuntimeError(f"{symbol}: unknown linear instrument")
    return rows[0]


def _symbol_spec(
    symbol: str, *, reference_price: float, max_quote_notional: float
) -> tuple[SymbolSpec | None, str]:
    row = _instrument_row(symbol)
    price_filter = row.get("priceFilter", {})
    lot = row.get("lotSizeFilter", {})
    tick_text = str(price_filter.get("tickSize") or "")
    qty_step_text = str(lot.get("qtyStep") or "")
    if not tick_text or not qty_step_text:
        return None, "missing tickSize/qtyStep"
    tick_size = float(tick_text)
    qty_step = float(qty_step_text)
    min_qty = float(lot.get("minOrderQty") or 0.0)
    min_notional = float(lot.get("minNotionalValue") or 5.0)
    target_notional = max(min_notional * 1.1, 5.0)
    steps = math.ceil(target_notional / reference_price / qty_step)
    quote_qty = max(steps * qty_step, min_qty)
    if quote_qty * reference_price > max_quote_notional:
        return None, (
            f"minimum executable notional {quote_qty * reference_price:.2f} exceeds "
            f"--max-quote-notional-usdt {max_quote_notional:.2f}"
        )
    return (
        SymbolSpec(
            symbol=symbol,
            tick_size=tick_size,
            qty_step=qty_step,
            min_qty=min_qty,
            quote_qty=quote_qty,
            price_decimals=_decimals(tick_text),
            qty_decimals=_decimals(qty_step_text),
        ),
        "",
    )


def _flatness(client: Any) -> dict[str, Any]:
    positions = [
        dict(row)
        for row in client.get_positions(settle_coin="USDT")
        if abs(float(row.get("size") or 0.0)) > 0.0
    ]
    orders = [dict(row) for row in client.get_open_orders(settle_coin="USDT")]
    return {"positions": positions, "open_orders": orders, "flat": not positions and not orders}


def _summarize_attempts(attempts: list[QuoteAttempt]) -> dict[str, Any]:
    resolved_states = {TERMINAL_FILLED, TERMINAL_TIMEOUT_CANCELLED, TERMINAL_REJECTED_WOULD_CROSS}
    by_state: dict[str, int] = {}
    for attempt in attempts:
        by_state[attempt.terminal_state] = by_state.get(attempt.terminal_state, 0) + 1
    resolved = [attempt for attempt in attempts if attempt.terminal_state in resolved_states]
    filled = [attempt for attempt in resolved if attempt.terminal_state == TERMINAL_FILLED]
    time_to_fill = [
        (int(attempt.metadata["fill_ts_ns"]) - attempt.decision_ts_ns) / 1e9
        for attempt in filled
        if "fill_ts_ns" in attempt.metadata
    ]
    itt_costs: list[float] = []
    for attempt in resolved:
        record = AttemptRecord(
            symbol=attempt.symbol,
            arm=ARM_POST_ONLY,
            side=attempt.side,
            attempt_index=attempt.attempt_index,
            decision_ts_ns=attempt.decision_ts_ns,
            decision_bid=attempt.decision_bid,
            decision_ask=attempt.decision_ask,
            terminal_state=attempt.terminal_state,
            terminal_ts_ns=attempt.terminal_ts_ns,
            fill_price=attempt.fill_price,
            fill_fee_bp=attempt.fill_fee_bp,
            fee_observed=attempt.fee_observed,
            terminal_bid=attempt.terminal_bid,
            terminal_ask=attempt.terminal_ask,
        )
        cost = itt_cost_bp(record, taker_fee_bp_fallback=TAKER_FEE_BP_FALLBACK)
        if cost is not None:
            itt_costs.append(cost)
    per_symbol: dict[str, dict[str, int]] = {}
    for attempt in attempts:
        bucket = per_symbol.setdefault(attempt.symbol, {"attempts": 0, "filled": 0, "reprices": 0})
        bucket["attempts"] += 1
        bucket["reprices"] += attempt.reprices
        if attempt.terminal_state == TERMINAL_FILLED:
            bucket["filled"] += 1
    return {
        "n_attempts": len(attempts),
        "n_resolved": len(resolved),
        "by_terminal_state": by_state,
        "fills": len(filled),
        "fill_rate": (len(filled) / len(resolved)) if resolved else float("nan"),
        "median_time_to_fill_s": statistics.median(time_to_fill) if time_to_fill else float("nan"),
        "itt_cost_bp_n": len(itt_costs),
        "itt_cost_bp_mean": statistics.fmean(itt_costs) if itt_costs else float("nan"),
        "itt_cost_bp_median": statistics.median(itt_costs) if itt_costs else float("nan"),
        "taker_fee_bp_fallback": TAKER_FEE_BP_FALLBACK,
        "per_symbol": per_symbol,
    }


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _write_private_receipt(path: Path, payload: Mapping[str, Any]) -> Path:
    if str(payload.get("artifact_sha256") or "") != _self_hash(payload):
        raise ValueError("quote-lab receipt artifact_sha256 is invalid")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.resolve(strict=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated symbols to quote")
    parser.add_argument("--side", default="Buy", choices=("Buy", "Sell"))
    parser.add_argument("--duration-minutes", type=float, required=True)
    parser.add_argument(
        "--output-dir", required=True, help="run directory for journals and the receipt"
    )
    parser.add_argument("--max-quote-notional-usdt", type=float, default=25.0)
    parser.add_argument("--confirm-demo-quote-lab", action="store_true")
    parser.add_argument("--reprice-seconds", type=float, default=15.0)
    parser.add_argument("--chase-ticks", type=int, default=2)
    parser.add_argument("--attempt-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--touch-source", default="ws", choices=("ws", "rest"))
    parser.add_argument("--max-private-requests-per-second", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.confirm_demo_quote_lab:
        parser.error("--confirm-demo-quote-lab is required because this submits demo orders")
    if args.duration_minutes <= 0.0:
        parser.error("--duration-minutes must be positive")
    symbols = sorted({part.strip().upper() for part in args.symbols.split(",") if part.strip()})
    if not symbols:
        parser.error("at least one symbol is required")
    output_dir = Path(args.output_dir).expanduser()
    receipt_path = output_dir / RECEIPT_NAME
    if os.path.lexists(receipt_path):
        raise SystemExit(f"receipt already exists; preserve it and choose a new run dir: {receipt_path}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        raise SystemExit("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
    limiter = BybitRestRateLimiter(
        max_requests=args.max_private_requests_per_second, per_seconds=1.0
    )
    bootstrap = BybitPrivateClient(
        category="linear", testnet=False, demo=True,
        api_key=api_key, api_secret=api_secret, rate_limiter=limiter,
    )
    api_key_info = bootstrap.get_api_key_information()
    allowed, reason = api_key_allows_order_submit(api_key_info)
    if not allowed:
        raise SystemExit(f"Bybit demo API key cannot submit quote-lab orders: {reason}")
    identity = DemoAccountIdentity.from_api_key_info(api_key=api_key, api_key_info=api_key_info)
    lease = DemoAccountMutationLease(identity)
    lease.acquire()

    stop_signal: dict[str, str] = {}

    def _handle_signal(signum: int, _frame: Any) -> None:
        stop_signal.setdefault("reason", f"signal_{signal.Signals(signum).name}")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    clock = SystemClock()
    stream: BybitRawPublicMarketStream | None = None
    events_writer: JsonlWriter | None = None
    attempts_writer: JsonlWriter | None = None
    engine: QuoteEngine | None = None
    try:
        client = BybitPrivateClient(
            category="linear", testnet=False, demo=True,
            api_key=api_key, api_secret=api_secret,
            rate_limiter=limiter, mutation_lease=lease,
        )
        preflight = _flatness(client)
        if not preflight["flat"]:
            raise SystemExit(
                "quote lab requires a flat account with no open orders; "
                "stop the fleet and flatten first"
            )
        specs: list[SymbolSpec] = []
        skipped: dict[str, str] = {}
        for symbol in symbols:
            reference = _reference_price(client, symbol)
            spec, skip_reason = _symbol_spec(
                symbol,
                reference_price=reference,
                max_quote_notional=args.max_quote_notional_usdt,
            )
            if spec is None:
                skipped[symbol] = skip_reason
                print(f"quote-lab-skip symbol={symbol} reason={skip_reason}", file=sys.stderr)
                continue
            specs.append(spec)
        if not specs:
            raise SystemExit("no quotable symbols remain after spec construction")
        active_symbols = [spec.symbol for spec in specs]

        rest_touch: RestTouchSource | None = None
        if args.touch_source == "ws":
            ws_touch = WsTouchSource(active_symbols)
            stream = BybitRawPublicMarketStream(
                depth=1,
                include_public_trades=False,
                on_message=ws_touch.on_message,
            )
            stream.start(active_symbols)
            warmup_deadline = clock.monotonic() + WS_WARMUP_SECONDS
            while clock.monotonic() < warmup_deadline:
                if all(ws_touch.healthy(symbol) for symbol in active_symbols):
                    break
                clock.sleep(0.5)
            touch: Any = ws_touch
        else:
            rest_touch = RestTouchSource(client, active_symbols)
            rest_touch.refresh_next(len(active_symbols))
            touch = rest_touch

        events_writer = JsonlWriter(output_dir / EVENTS_JOURNAL_NAME)
        attempts_writer = JsonlWriter(output_dir / ATTEMPTS_JOURNAL_NAME)
        config = EngineConfig(
            side=args.side,
            reprice_seconds=args.reprice_seconds,
            chase_ticks=args.chase_ticks,
            attempt_timeout_seconds=args.attempt_timeout_seconds,
            max_runtime_seconds=args.duration_minutes * 60.0,
        )
        engine = QuoteEngine(
            venue=BybitQuoteVenue(client),
            touch=touch,
            clock=clock,
            config=config,
            specs=specs,
            on_event=lambda event: events_writer.write(asdict(event)),  # type: ignore[union-attr]
            on_attempt=lambda attempt: attempts_writer.write(asdict(attempt)),  # type: ignore[union-attr]
            link_prefix=LINK_PREFIX,
        )
        deadline = clock.monotonic() + args.duration_minutes * 60.0
        while clock.monotonic() < deadline and engine.active and "reason" not in stop_signal:
            if rest_touch is not None:
                rest_touch.refresh_next(2)
            engine.tick()
            clock.sleep(config.poll_seconds)
        end_reason = stop_signal.get(
            "reason",
            f"engine_aborted: {engine.abort_reason}" if engine.aborted else "duration_complete",
        )
        engine.shutdown(end_reason)
        events_writer.flush()
        attempts_writer.flush()
        final = _flatness(client)
        summary = _summarize_attempts(engine.attempts)
        payload: dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": RECEIPT_KIND,
            "environment": "demo",
            "verified_ts_ns": time.time_ns(),
            "config": {
                "side": args.side,
                "duration_minutes": args.duration_minutes,
                "max_quote_notional_usdt": args.max_quote_notional_usdt,
                "reprice_seconds": args.reprice_seconds,
                "chase_ticks": args.chase_ticks,
                "attempt_timeout_seconds": args.attempt_timeout_seconds,
                "cooldown_seconds": config.cooldown_seconds,
                "max_inventory_notional_usdt": config.max_inventory_notional_usdt,
                "poll_seconds": config.poll_seconds,
                "touch_source": args.touch_source,
                "max_private_requests_per_second": args.max_private_requests_per_second,
                "link_prefix": LINK_PREFIX,
            },
            "symbols": active_symbols,
            "symbol_specs": {spec.symbol: asdict(spec) for spec in specs},
            "skipped_symbols": skipped,
            "preflight_flatness": preflight,
            "final_flatness": final,
            "engine": {
                "shutdown_reason": engine.shutdown_reason,
                "abort_reason": engine.abort_reason,
                "shutdown_flat": engine.shutdown_flat,
            },
            "summary": summary,
            "attempts": [asdict(attempt) for attempt in engine.attempts],
            "artifact_sha256": "",
        }
        payload["artifact_sha256"] = _self_hash(payload)
        written = _write_private_receipt(receipt_path, payload)
        print(json.dumps({
            "status": "quote_lab_complete",
            "output": str(written),
            "end_reason": end_reason,
            "fills": summary["fills"],
            "fill_rate": summary["fill_rate"],
            "final_flat": final["flat"],
        }, sort_keys=True))
        return 0 if final["flat"] and not engine.aborted else 1
    except BaseException as exc:
        if engine is not None:
            try:
                engine.shutdown(f"exception: {type(exc).__name__}")
            except Exception:  # noqa: BLE001 - the failure receipt still gets written
                pass
        failure = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": RECEIPT_KIND + "_failure",
            "environment": "demo",
            "failed_ts_ns": time.time_ns(),
            "error": {"type": type(exc).__name__, "message": str(exc)[:4000]},
            "partial_attempts": [asdict(attempt) for attempt in engine.attempts] if engine else [],
            "artifact_sha256": "",
        }
        failure["artifact_sha256"] = _self_hash(failure)
        failure_path = receipt_path.with_name(f"{RECEIPT_NAME}.failed-{time.time_ns()}.json")
        _write_private_receipt(failure_path, failure)
        print(
            json.dumps({
                "status": "quote_lab_failed",
                "failure_receipt": str(failure_path),
                "error": str(exc)[:1000],
            }, sort_keys=True),
            file=sys.stderr,
        )
        raise
    finally:
        if stream is not None:
            stream.close()
        for writer in (events_writer, attempts_writer):
            if writer is not None:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - closing must not mask the run outcome
                    pass
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
