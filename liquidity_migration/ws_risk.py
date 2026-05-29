from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from ._common import MS_PER_DAY
from .bybit import BybitPrivateWebSocketStream, BybitPublicTickerStream, build_ws_trade_client, resolve_private_credentials
from .config import ResearchConfig
from decimal import Decimal

from .event_demo import (
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    PENDING_ORDER_GUARD_MS,
    PENDING_ORDER_STATUSES,
    _active_position_by_symbol,
    _bool,
    _build_private_client,
    _column_values,
    _decimal_text,
    _demo_event_config,
    _empty_trades,
    _execution_summary,
    _execute_risk_exits,
    _execute_stop_repairs,
    _float,
    _orphan_close_pnl_backfill,
    _normalized_position_side,
    _open_trades,
    _order_params,
    _price_lookup_from_positions,
    _quantity_text,
    _risk_order_link_id,
    _risk_reconcile_missing_positions,
    _reconcile_pending_order_fills,
    _selected_scenario,
    _split_qty_for_max_order_size,
    _live_open_order_symbols,
    _safe_open_orders,
    _safe_raw_positions,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    _terminalize_stale_pending_entry_orders,
    _maybe_notify,
    _telegram_notification_reason,
    _upsert_rows,
    _write_order_rows,
    _write_trade_rows,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    decode_entry_order_link_id,
    format_event_risk_cycle_report,
    plan_risk_exits,
    plan_stop_repairs,
    summarize_position_pnl,
)
from .long_native_event_demo import MULTI_STRAT_V1_STRATEGY_ID
from .volume_events import VolumeEventResearchConfig
from .storage import exclusive_file_lock, read_dataset, write_dataset


_logger = logging.getLogger("liquidity_migration.ws_risk")


def _ensure_default_log_handler() -> None:
    """Attach a stderr handler to the package root logger when nothing else
    has configured logging. systemd captures stderr → journald, so this is
    what makes journalctl show risk-engine events. Idempotent: only adds a
    handler once per process and only if no upstream handler is configured.
    """
    root_pkg_logger = logging.getLogger("liquidity_migration")
    if root_pkg_logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_pkg_logger.addHandler(handler)
    level_name = os.environ.get("LIQMIG_LOG_LEVEL", "INFO").upper()
    root_pkg_logger.setLevel(getattr(logging, level_name, logging.INFO))


# Default per-list cap on the append-only telemetry logs in WebSocketRiskState.
# Reports only ever display the last 20; 2000 is a generous audit tail that
# bounds a long-lived daemon's memory to a few MB. Overridable via
# EventWebSocketRiskConfig.telemetry_log_retention.
_LOG_RETENTION = 2000


@dataclass(frozen=True, slots=True)
class EventWebSocketRiskConfig:
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-risk-ws"
    repair_stops: bool = True
    order_submit_mode: str = "ws_then_rest"
    rest_fallback: bool = True
    rest_reconcile_seconds: float = 30.0
    heartbeat_seconds: float = 10.0
    max_runtime_seconds: float = 0.0
    # When True, a background thread (its OWN private client = separate HTTP
    # session, so no shared-client concurrency) keeps the positions + open-orders
    # REST snapshot fresh, and rest_reconcile reads it non-blocking instead of
    # making the blocking REST calls on the consumer thread (which would stall
    # stop-trigger processing for the fetch duration). Default OFF — enabling it
    # on the live risk daemon is a reviewed deploy decision. See
    # docs/preregistration/round2/r-latency-event-driven-optimization.md.
    reconcile_prefetch_enabled: bool = False
    # 15s was too tight on a quiet demo account: Bybit's private WS only
    # pushes when state changes (orders, fills, balance moves). The ticker
    # WS keeps last_ws_event_monotonic fresh under normal load but during
    # deploy churn / pybit reconnects the gap can briefly exceed 15s,
    # producing a false-positive "position_report_error: websocket stale"
    # telegram. 60s is short enough to catch a real WS death (the WS
    # backbone reconnects in <10s) but tolerates ordinary brief silences.
    stale_ws_seconds: float = 60.0
    stream_start_timeout_seconds: float = 3.0
    # Longer budget specifically for the WS trade-client connect, which now
    # retries with jittered backoff (de-syncing the multi-daemon demo storm);
    # the 3s stream-start timeout is too tight for the retry. Startup-only.
    ws_trade_connect_timeout_seconds: float = 15.0
    fast_execution_stream: bool = False
    stop_tolerance_bps: float = 1.0
    pending_exit_guard_seconds: float = 120.0
    adopt_untracked_positions: bool = True
    exit_untracked_positions: bool = False
    untracked_position_grace_seconds: float = 90.0
    adopt_stop_loss_pct: float = 0.12
    adopt_take_profit_pct: float = 0.21
    adopt_hold_days: float = 3.0
    # Strategy IDs used to reconstruct the deterministic trade_id when an
    # adopted position's orderLinkId decodes back to a known signal_ts.
    # Empty string means "use the canonical promoted scenario_id at startup"
    # (derived once via _demo_event_config to avoid hardcoding it here).
    # Set explicitly if running a non-default strategy profile.
    adopt_short_strategy_id: str = ""
    adopt_long_strategy_id: str = ""
    # How many recent orders per symbol to scan when looking for the
    # original entry order's orderLinkId. 50 is Bybit's default page;
    # bigger is safer for older positions but adds REST cost per
    # adoption. Adoption fires once per orphan + grace period, so even
    # 100 is fine.
    adopt_order_history_limit: int = 50
    # Long-sleeve dual-side support: when long_data_root is set, this engine
    # ALSO reads the long-side ledger (long_native_demo_trades /
    # long_native_demo_orders by default) from that root and routes write
    # updates back to it per the per-row `sleeve` column. Set to "" to keep
    # short-only behavior. Per owner: extend ws_risk to handle both sides
    # rather than running two processes.
    long_data_root: str = ""
    long_trades_dataset: str = "long_native_demo_trades"
    long_orders_dataset: str = "long_native_demo_orders"
    # Per-list cap on the append-only telemetry logs (exits/repairs/
    # reconciliations/pending_fill_reconciliations/errors) so a long-lived
    # daemon can't OOM. Configurable; reports only ever display the last 20.
    telemetry_log_retention: int = _LOG_RETENTION


@dataclass(slots=True)
class WebSocketRiskState:
    """Mutable engine state for EventWebSocketRiskEngine.

    THREADING INVARIANT: every field here is mutated ONLY by the single
    consumer thread -- the EventWebSocketRiskEngine.run() loop that drains
    self.events. pybit WebSocket callbacks fire on background threads and MUST
    only enqueue onto that queue.Queue; they must never touch this state
    directly. None of these fields is lock-protected, so a callback that called
    a state-mutating method (on_*/mark_*/record_*) directly would race instantly
    -- dict mutation during a to_dicts() snapshot, lost set updates. Keep all
    mutation on the consumer thread; handle_event() asserts this.
    """

    all_trades: pl.DataFrame = field(default_factory=pl.DataFrame)
    open_trades: pl.DataFrame = field(default_factory=_empty_trades)
    positions_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    price_by_symbol: dict[str, float] = field(default_factory=dict)
    pending_entry_symbols: set[str] = field(default_factory=set)
    submitted_symbols: set[str] = field(default_factory=set)
    live_entry_order_symbols: set[str] = field(default_factory=set)
    live_exit_order_symbols: set[str] = field(default_factory=set)
    submitted_symbol_ts_ms: dict[str, int] = field(default_factory=dict)
    untracked_first_seen_ms: dict[str, int] = field(default_factory=dict)
    submitted_link_to_trade_id: dict[str, str] = field(default_factory=dict)
    submitted_link_submit_mode: dict[str, str] = field(default_factory=dict)
    # Running (filled_qty, value) aggregate per order link -- not the raw
    # execution rows, so it grows with order count, not execution-message count,
    # and needs no O(n) re-sum on each new execution.
    executions_by_link: dict[str, dict[str, float]] = field(default_factory=dict)
    subscribed_symbols: set[str] = field(default_factory=set)
    last_ws_event_monotonic: float = field(default_factory=time.monotonic)
    last_stale_reconcile_monotonic: float = 0.0
    last_report_monotonic: float = 0.0
    last_reconcile_monotonic: float = 0.0
    exits: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    # Index from order_link_id -> the same dict that lives in `orders`. Maintained
    # in lockstep with `orders` mutations (see _record_orders / _record_order on
    # EventWebSocketRiskEngine). Lets link-based lookups be O(1) instead of
    # scanning the growing orders list on every fill/cancel/reconcile.
    orders_by_link: dict[str, dict[str, Any]] = field(default_factory=dict)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    reconciliations: list[dict[str, Any]] = field(default_factory=list)
    pending_fill_reconciliations: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Cumulative count of telemetry-log rows pruned to bound daemon memory.
    # The history lists above (exits/repairs/reconciliations/
    # pending_fill_reconciliations/errors) are append-only display logs; left
    # unbounded they grow for the daemon's lifetime and eventually OOM-kill it
    # (which would orphan an open position mid-flight). _prune_state_logs caps
    # each to _LOG_RETENTION and accumulates the dropped count here so the
    # cumulative report counters stay exact.
    exits_evicted: int = 0
    repairs_evicted: int = 0
    reconciliations_evicted: int = 0
    pending_fill_reconciliations_evicted: int = 0
    errors_evicted: int = 0
    # Last error string from the most recent ``_safe_raw_positions`` call (or
    # empty when the snapshot was clean). Plumbed into the orphan reconciler
    # so a transient REST failure -- which leaves ``positions_by_symbol``
    # empty -- does not false-positive orphan-close every open trade.
    last_position_error: str = ""
    ws_order_unavailable: str = ""
    telegram_keys_sent: set[str] = field(default_factory=set)


class EventWebSocketRiskEngine:
    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        risk_config: EventWebSocketRiskConfig | None = None,
        private_client: Any | None = None,
        private_stream: Any | None = None,
        public_stream: Any | None = None,
        trade_client: Any | None = None,
    ) -> None:
        self.root = Path(data_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.risk = risk_config or EventWebSocketRiskConfig()
        _validate_ws_risk_config(self.risk)
        # Dual-side support: when long_data_root is set, this engine also
        # owns the long-sleeve ledger. Reads concat both sides (tagged by
        # `sleeve` column); writes route per-row via _write_*_rows_routed.
        # When long_data_root is "" / unset, the long_root is None and the
        # engine behaves identically to the short-only legacy path.
        self.long_root: Path | None = (
            Path(self.risk.long_data_root).expanduser() if self.risk.long_data_root else None
        )
        if self.long_root is not None:
            self.long_root.mkdir(parents=True, exist_ok=True)
        self.private_client = private_client
        self.private_stream = private_stream
        self.public_stream = public_stream
        self.trade_client = trade_client
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.state = WebSocketRiskState()
        self.report_dir = self.root / "reports" / self.risk.data_name
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.state.telegram_keys_sent = set(_read_telegram_dedupe_keys(self.report_dir))
        # Captured by run(): the one thread allowed to mutate self.state.
        self._consumer_thread_ident: int | None = None
        # Telegram notifications are sent on a background daemon thread so the
        # consumer thread never blocks on the HTTP round-trip — a slow Telegram
        # RTT would otherwise stall stop-enforcement event processing during a
        # cascade. Lazily started on first enqueue; drained + stopped in close().
        # Downside is bounded: a dropped/late notification, never an order or
        # state error (the dedupe + state mutation stay on the consumer thread).
        self._telegram_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._telegram_thread: threading.Thread | None = None
        # Background reconcile-prefetcher (opt-in via reconcile_prefetch_enabled).
        # Holds the latest positions + open-orders REST snapshot so rest_reconcile
        # reads it non-blocking. Written by the prefetcher via atomic reference
        # swap; read by the consumer. Default off -> these stay None/idle.
        self._reconcile_prefetch: dict[str, Any] | None = None
        self._reconcile_prefetch_thread: threading.Thread | None = None
        self._reconcile_prefetch_stop = threading.Event()

    # ------------------------------------------------------------------
    # Dual-side ledger routing
    #
    # ws_risk now reads the short ledger (self.root) and optionally the long
    # ledger (self.long_root). Both are concatenated into self.state.all_trades
    # with a `sleeve` column ("short" or "long"). All writes are routed via
    # the two helpers below — they inspect each row's `sleeve` field and write
    # to the appropriate root/dataset. Existing callsites that used to call
    # _write_trade_rows / _write_order_rows directly are migrated to these
    # helpers; the legacy module-level helpers are kept for callers outside
    # this engine that already pass a per-root path.
    # ------------------------------------------------------------------
    def _read_trades_combined(self) -> pl.DataFrame:
        short = read_dataset(self.root, "event_demo_trades")
        short = _ensure_sleeve_column(short, "short")
        if self.long_root is None:
            return short
        try:
            long_trades = read_dataset(self.long_root, self.risk.long_trades_dataset)
        except Exception:  # noqa: BLE001 - dual-ledger reads must fail open
            long_trades = pl.DataFrame()
        long_trades = _ensure_sleeve_column(long_trades, "long")
        if short.is_empty():
            return long_trades
        if long_trades.is_empty():
            return short
        return pl.concat([short, long_trades], how="diagonal_relaxed")

    def _read_orders_combined(self) -> pl.DataFrame:
        short = read_dataset(self.root, "event_demo_orders")
        short = _ensure_sleeve_column(short, "short")
        if self.long_root is None:
            return short
        try:
            long_orders = read_dataset(self.long_root, self.risk.long_orders_dataset)
        except Exception:  # noqa: BLE001
            long_orders = pl.DataFrame()
        long_orders = _ensure_sleeve_column(long_orders, "long")
        if short.is_empty():
            return long_orders
        if long_orders.is_empty():
            return short
        return pl.concat([short, long_orders], how="diagonal_relaxed")

    @staticmethod
    def _sleeve_of(row: dict[str, Any]) -> str:
        sleeve = str(row.get("sleeve") or "").lower()
        return sleeve if sleeve in {"long", "short"} else "short"

    def _write_trade_rows_routed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        short_rows: list[dict[str, Any]] = []
        long_rows: list[dict[str, Any]] = []
        for row in rows:
            if self._sleeve_of(row) == "long" and self.long_root is not None:
                long_rows.append(row)
            else:
                short_rows.append(row)
        if short_rows:
            _write_trade_rows(self.root, pl.DataFrame(short_rows, infer_schema_length=None))
        if long_rows:
            assert self.long_root is not None
            write_dataset(
                pl.DataFrame(long_rows, infer_schema_length=None),
                self.long_root, self.risk.long_trades_dataset, partition_by=(),
            )

    def _write_order_rows_routed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        short_rows: list[dict[str, Any]] = []
        long_rows: list[dict[str, Any]] = []
        for row in rows:
            if self._sleeve_of(row) == "long" and self.long_root is not None:
                long_rows.append(row)
            else:
                short_rows.append(row)
        if short_rows:
            _write_order_rows(self.root, pl.DataFrame(short_rows, infer_schema_length=None))
        if long_rows:
            assert self.long_root is not None
            write_dataset(
                pl.DataFrame(long_rows, infer_schema_length=None),
                self.long_root, self.risk.long_orders_dataset, partition_by=(),
            )

    def _reconcile_prefetch_loop(self) -> None:
        """Background: keep the positions + open-orders REST snapshot fresh on a
        SEPARATE private client (own HTTP session — no concurrency with the
        consumer's client / its place_order calls). The consumer's rest_reconcile
        reads the snapshot non-blocking, so the slow REST never stalls stop-trigger
        processing. A failed fetch is logged and retried next tick."""
        client = _build_private_client(self.config)
        interval = max(1.0, self.risk.rest_reconcile_seconds / 3.0)
        while not self._reconcile_prefetch_stop.wait(timeout=interval):
            try:
                positions, pos_err = _safe_raw_positions(client, settle_coin=self.risk.settle_coin)
                open_orders, oo_err = _safe_open_orders(client, settle_coin=self.risk.settle_coin)
            except Exception as exc:  # noqa: BLE001 - the prefetcher must never die silently
                _logger.warning("reconcile prefetch failed: %s", exc)
                continue
            # Atomic reference swap — the consumer reads the latest snapshot.
            self._reconcile_prefetch = {
                "positions": positions, "positions_error": pos_err,
                "open_orders": open_orders, "open_orders_error": oo_err,
                "monotonic": time.monotonic(),
            }

    def _start_reconcile_prefetcher(self) -> None:
        if not self.risk.reconcile_prefetch_enabled:
            return
        if self._reconcile_prefetch_thread is not None and self._reconcile_prefetch_thread.is_alive():
            return
        self._reconcile_prefetch_stop.clear()
        self._reconcile_prefetch_thread = threading.Thread(
            target=self._reconcile_prefetch_loop, name="ws-risk-reconcile-prefetch", daemon=True
        )
        self._reconcile_prefetch_thread.start()

    def run(self) -> dict[str, Any]:
        self._consumer_thread_ident = threading.get_ident()
        started = time.monotonic()
        self.bootstrap()
        self._start_reconcile_prefetcher()
        self.write_report(reason="startup")
        while True:
            if self.risk.max_runtime_seconds > 0 and time.monotonic() - started >= self.risk.max_runtime_seconds:
                return self.write_report(reason="max_runtime")
            timeout = max(min(self.risk.heartbeat_seconds, self.risk.rest_reconcile_seconds, 1.0), 0.05)
            try:
                event_type, message = self.events.get(timeout=timeout)
            except queue.Empty:
                self.on_idle()
                continue
            self.handle_event(event_type, message)

    def bootstrap(self) -> None:
        self.private_client = self.private_client or _build_private_client(self.config)
        self.state.all_trades = self._read_trades_combined()
        self.state.open_trades = _open_trades(self.state.all_trades)
        orders = self._read_orders_combined()
        raw_positions, error = _safe_raw_positions(self.private_client, settle_coin=self.risk.settle_coin)
        if error:
            self.state.errors.append(error)
        # Plumb the error to state so reconcile_positions can bail-on-bad-snapshot
        # instead of false-positive orphan-closing every trade.
        self.state.last_position_error = error
        self.state.positions_by_symbol = _active_position_by_symbol(raw_positions)
        self.state.price_by_symbol.update(_price_lookup_from_positions(self.state.positions_by_symbol))
        open_orders_ok = self.refresh_live_exit_order_symbols()
        self.reconcile_pending_order_fills(orders)
        orders = self._read_orders_combined()
        self.load_pending_entry_orders(orders)
        self.load_pending_exit_orders(orders)
        if not error and open_orders_ok:
            self.reconcile_flat_pending_exit_orders(orders)
            orders = self._read_orders_combined()
            self.terminalize_stale_pending_entry_orders(orders)
        self.reconcile_positions(write=True)
        self.evaluate_symbols(set(self.state.positions_by_symbol))
        self.repair_exchange_stops()
        self.adopt_untracked_positions()
        self.exit_untracked_positions()
        self.start_streams()
        self.state.last_reconcile_monotonic = time.monotonic()
        _logger.info(
            "bootstrap complete positions=%d open_trades=%d pending_entry_symbols=%d errors=%d",
            len(self.state.positions_by_symbol),
            self.state.open_trades.height if not self.state.open_trades.is_empty() else 0,
            len(self.state.pending_entry_symbols),
            len(self.state.errors),
        )
        if self.state.errors:
            for err in self.state.errors[-5:]:
                _logger.error("bootstrap_error: %s", err)

    def start_streams(self) -> None:
        if self.private_stream is None:
            stream, error = _call_with_timeout(
                "private websocket stream construction",
                lambda: _build_private_stream(self.config),
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.errors.append(error)
            else:
                self.private_stream = stream
        if self.private_stream is not None:
            _, error = _call_with_timeout(
                "private websocket subscriptions",
                self._subscribe_private_stream,
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.errors.append(error)
        if self.public_stream is None:
            stream, error = _call_with_timeout(
                "public ticker websocket stream construction",
                lambda: BybitPublicTickerStream(
                    category=self.config.exchange.category,
                    testnet=self.config.exchange.testnet,
                    demo=False,
                ),
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.errors.append(error)
            else:
                self.public_stream = stream
        self.subscribe_tickers(set(self.state.positions_by_symbol) | set(_column_values(self.state.open_trades, "symbol")))
        if self.risk.order_submit_mode in {"ws", "ws_then_rest"} and self.trade_client is None:
            # WS-first exits: actually ATTEMPT the WS trade client (with jittered
            # retry) rather than pre-emptively giving up in ws_then_rest mode —
            # WS order submission saves ~150-200ms per exit (lowest-latency
            # stops). Falls back to REST on genuine failure (the seatbelt).
            client, error = _call_with_timeout(
                "websocket trade client construction",
                lambda: _build_ws_trade_client(self.config),
                timeout_seconds=max(
                    self.risk.stream_start_timeout_seconds,
                    self.risk.ws_trade_connect_timeout_seconds,
                ),
            )
            if error:
                # ws-only mode with live submission must not silently REST;
                # ws_then_rest degrades to REST with the operator-friendly note.
                if self.risk.order_submit_mode == "ws" and self.risk.submit_orders:
                    raise RuntimeError(error)
                self.state.ws_order_unavailable = _DEMO_WS_TRADE_UNAVAILABLE
            else:
                self.trade_client = client

    def _subscribe_private_stream(self) -> None:
        assert self.private_stream is not None
        self.private_stream.subscribe_positions(lambda message: self.events.put(("position", message)))
        self.private_stream.subscribe_orders(lambda message: self.events.put(("order", message)))
        self.private_stream.subscribe_executions(
            lambda message: self.events.put(("execution", message)),
            fast=self.risk.fast_execution_stream,
        )

    def subscribe_tickers(self, symbols: set[str]) -> None:
        missing = sorted(symbol for symbol in symbols if symbol and symbol not in self.state.subscribed_symbols)
        if not missing or self.public_stream is None:
            return
        _, error = _call_with_timeout(
            f"public ticker subscription {','.join(missing[:8])}",
            lambda: self.public_stream.subscribe_tickers(missing, lambda message: self.events.put(("ticker", message))),
            timeout_seconds=self.risk.stream_start_timeout_seconds,
        )
        if error:
            self.state.errors.append(error)
            return
        self.state.subscribed_symbols.update(missing)

    def handle_event(self, event_type: str, message: dict[str, Any]) -> None:
        if self._consumer_thread_ident is not None and threading.get_ident() != self._consumer_thread_ident:
            raise RuntimeError(
                "WebSocketRiskState mutated off the consumer thread -- WS callbacks "
                "must enqueue onto self.events, never dispatch state changes directly."
            )
        self.state.last_ws_event_monotonic = time.monotonic()
        if event_type == "position":
            self.on_position_message(message)
        elif event_type == "ticker":
            self.on_ticker_message(message)
        elif event_type == "execution":
            self.on_execution_message(message)
        elif event_type == "order":
            self.on_order_message(message)
        elif event_type == "ws_order_ack":
            self.on_ws_order_ack(message)
        self.on_idle()

    def on_position_message(self, message: dict[str, Any]) -> None:
        changed_symbols: set[str] = set()
        for row in _message_rows(message):
            symbol = str(row.get("symbol", ""))
            if not symbol:
                continue
            changed_symbols.add(symbol)
            if _float(row.get("size")) > 0.0:
                self.state.positions_by_symbol[symbol] = row
                price = _position_price(row)
                if price > 0.0:
                    self.state.price_by_symbol[symbol] = price
            else:
                self.state.positions_by_symbol.pop(symbol, None)
        self.subscribe_tickers(changed_symbols)
        reconcile_rows = self.reconcile_positions(write=True)
        if reconcile_rows:
            self.write_report(reason="position_stream_reconcile")
        self.adopt_untracked_positions()
        self.exit_untracked_positions()
        self.evaluate_symbols(changed_symbols)

    def on_ticker_message(self, message: dict[str, Any]) -> None:
        changed_symbols: set[str] = set()
        for row in _message_rows(message):
            symbol = str(row.get("symbol", ""))
            price = _first_price(row, ("markPrice", "lastPrice", "indexPrice"))
            if symbol and price > 0.0:
                self.state.price_by_symbol[symbol] = price
                changed_symbols.add(symbol)
        self.evaluate_symbols(changed_symbols)

    def on_order_message(self, message: dict[str, Any]) -> None:
        updates: list[dict[str, Any]] = []
        for row in _message_rows(message):
            link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
            if not link:
                continue
            status = str(row.get("orderStatus") or row.get("order_status") or "").lower()
            terminal_statuses = {
                "rejected",
                "cancelled",
                "canceled",
                "deactivated",
                "partiallyfilledcanceled",
                "partiallyfilledcancelled",
            }
            fill_statuses = {
                "filled",
                "partiallyfilled",
                "partial",
                "partiallyfilledcanceled",
                "partiallyfilledcancelled",
            }
            if status in fill_statuses:
                filled_qty = _float(
                    row.get("cumExecQty")
                    or row.get("cum_exec_qty")
                    or row.get("executedQty")
                    or row.get("execQty")
                )
                if filled_qty <= 0.0 and status == "filled":
                    filled_qty = _float(row.get("qty")) or self.order_target_qty(link)
                if filled_qty > 0.0:
                    avg_price = _float(row.get("avgPrice") or row.get("price")) or self.order_avg_price(link)
                    if link in self.state.submitted_link_to_trade_id:
                        self.record_tracked_exit_stream_fill(
                            order_link_id=link,
                            filled_qty=filled_qty,
                            exit_price=avg_price,
                            source="order",
                        )
                    else:
                        updates.extend(
                            self.mark_order_filled_from_execution(
                                order_link_id=link,
                                filled_qty=filled_qty,
                                exit_price=avg_price,
                            )
                        )
                        self.update_stream_order_guards(updates)
            if status in terminal_statuses:
                updates.extend(self.mark_order_terminal_from_order_update(order_link_id=link, status=status, row=row))
        if updates:
            self._write_order_rows_routed(updates)

    def on_ws_order_ack(self, message: dict[str, Any]) -> None:
        ret_code = _int(message.get("retCode"))
        if ret_code == 0:
            return
        ret_msg = str(message.get("retMsg") or message.get("ret_msg") or message)[:500]
        self.state.errors.append(f"websocket order ack failed: {ret_msg}")
        link = _ack_order_link(message)
        order = self.order_row(link) if link else {}
        if not order:
            self.write_report(reason="ws_order_ack_failed")
            return
        was_pending = str(order.get("status", "")) in PENDING_ORDER_STATUSES
        updates = self.mark_order_terminal_from_order_update(
            order_link_id=link,
            status="rejected",
            row={"symbol": order.get("symbol", ""), "rejectReason": ret_msg},
        )
        if updates:
            self._write_order_rows_routed(updates)
        if (
            was_pending
            and self.risk.submit_orders
            and self.risk.rest_fallback
            and self.risk.order_submit_mode == "ws_then_rest"
        ):
            exit_plan = self.exit_plan_from_order(order)
            if exit_plan is not None:
                rows, orders = self.rest_exit([exit_plan], submit_orders=True)
                self.record_exit_submission_result(str(exit_plan.get("symbol", "")), rows, orders)
                self.write_report(reason="ws_order_ack_rest_fallback")
                return
        self.write_report(reason="ws_order_ack_failed")

    def on_execution_message(self, message: dict[str, Any]) -> None:
        for row in _message_rows(message):
            link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
            if not link:
                continue
            agg = self.state.executions_by_link.setdefault(link, {"filled_qty": 0.0, "value": 0.0})
            agg["filled_qty"] += _float(row.get("execQty"))
            agg["value"] += _float(row.get("execValue")) or _float(row.get("execQty")) * _float(row.get("execPrice"))
            filled_qty = agg["filled_qty"]
            value = agg["value"]
            exit_price = value / filled_qty if filled_qty > 0.0 else 0.0
            if link in self.state.submitted_link_to_trade_id:
                self.record_tracked_exit_stream_fill(
                    order_link_id=link,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                    source="execution",
                )
            else:
                order_updates = self.mark_order_filled_from_execution(
                    order_link_id=link,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                )
                self.update_stream_order_guards(order_updates)
                if order_updates:
                    self._write_order_rows_routed(order_updates)

    def record_tracked_exit_stream_fill(
        self,
        *,
        order_link_id: str,
        filled_qty: float,
        exit_price: float,
        source: str,
    ) -> None:
        trade_id = self.state.submitted_link_to_trade_id.get(order_link_id, "")
        if not trade_id or self.state.all_trades.is_empty():
            return
        trades = {str(row["trade_id"]): row for row in self.state.all_trades.to_dicts()}
        trade = dict(trades.get(trade_id, {}))
        # Load-bearing: REST fallback (on_ws_order_ack -> rest_exit) may have
        # already closed this trade. A late `execution` stream message for the
        # same order_link_id must not append a second close to state.exits. Do
        # not remove this guard without re-checking the WS-then-REST race.
        if not trade or str(trade.get("status")) == "closed":
            return
        if filled_qty <= 0.0:
            return
        order = self.order_row(order_link_id)
        previous_filled_qty = _float(order.get("filled_qty"))
        delta_qty = max(filled_qty - previous_filled_qty, 0.0)
        order_target_qty = self.order_target_qty(order_link_id)
        current_trade_qty = _float(trade.get("qty"))
        remaining_qty = max(current_trade_qty - delta_qty, 0.0)
        fully_filled = (
            order_target_qty > 0.0
            and filled_qty + max(order_target_qty * 1e-8, 1e-12) >= order_target_qty
        ) or remaining_qty <= max(current_trade_qty * 1e-8, 1e-12)
        if delta_qty <= 0.0 and not fully_filled:
            return
        now_ms = _now_ms()
        exit_price = exit_price if exit_price > 0.0 else _float(order.get("avg_price")) or _float(trade.get("exit_price"))
        exit_reason = str(order.get("exit_reason") or trade.get("exit_reason") or f"{source}_confirmed")
        if fully_filled:
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or now_ms,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "exit_order_link_id": order_link_id,
                    "exit_order_id": order.get("order_id", ""),
                    "submit_mode": self.state.submitted_link_submit_mode.get(order_link_id, f"{source}_confirmed"),
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            self.state.exits.append(trade)
            self.clear_submitted_symbol(str(trade.get("symbol", "")))
            self.state.positions_by_symbol.pop(str(trade.get("symbol", "")), None)
            report_reason = f"ws_{source}_fill"
        else:
            trade.update(
                {
                    "status": "open",
                    "qty": _quantity_text(remaining_qty),
                    "notional_usdt": abs(_float(trade.get("entry_price")) * remaining_qty),
                    "partial_exit_order_link_id": order_link_id,
                    "partial_exit_order_id": order.get("order_id", ""),
                    "partial_exit_price": exit_price,
                    "partial_exit_reason": exit_reason,
                    "partial_exit_qty": _quantity_text(filled_qty),
                    "partial_exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or now_ms,
                    "partial_exit_ts_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            self.state.pending_fill_reconciliations.append(trade)
            self.mark_submitted_symbol(str(trade.get("symbol", "")), now_ms=now_ms)
            report_reason = f"ws_{source}_partial_fill"
        self.state.all_trades = _upsert_rows(self.state.all_trades, [trade], key="trade_id")
        self.state.open_trades = _open_trades(self.state.all_trades)
        self._write_trade_rows_routed([trade])
        order_updates = self.mark_order_filled_from_execution(
            order_link_id=order_link_id,
            filled_qty=filled_qty,
            exit_price=exit_price,
        )
        if order_updates:
            self._write_order_rows_routed(order_updates)
        self.write_report(reason=report_reason)

    def _record_orders(self, orders: list[dict[str, Any]]) -> None:
        # Append to state.orders and mirror into state.orders_by_link by
        # order_link_id. Single point of mutation so the list and the index
        # stay in lockstep -- the link-based mutator methods below
        # (mark_order_filled_from_execution, mark_order_terminal_from_order
        # _update) assume the index points at the same dict that lives in
        # the list, so dict-in-place updates flow both ways.
        #
        # Uniqueness invariant: at most one order per link_id. Bybit
        # guarantees order_link_id uniqueness within 36 hours, and
        # load_pending_exit_orders dedups on ingest. If a duplicate ever
        # slipped in, the index would point at the last write and earlier
        # copies in the list would become orphans -- mutator methods would
        # silently only touch the latest. Don't introduce paths that add
        # without dedup.
        self.state.orders.extend(orders)
        index = self.state.orders_by_link
        for order in orders:
            link = str(order.get("order_link_id") or "")
            if link:
                index[link] = order

    def mark_order_filled_from_execution(self, *, order_link_id: str, filled_qty: float, exit_price: float) -> list[dict[str, Any]]:
        order = self.state.orders_by_link.get(order_link_id)
        if order is None:
            return []
        target_qty = _float(order.get("target_qty") or order.get("qty"))
        fully_filled = target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty
        order["status"] = "filled" if fully_filled else "partial" if filled_qty > 0.0 else order.get("status", "")
        order["filled_qty"] = _quantity_text(filled_qty) if filled_qty > 0.0 else ""
        order["avg_price"] = exit_price
        order["notional_usdt"] = abs(exit_price * filled_qty) if exit_price > 0.0 else 0.0
        return [order]

    def update_stream_order_guards(self, order_updates: list[dict[str, Any]]) -> None:
        for order in order_updates:
            symbol = str(order.get("symbol", ""))
            if str(order.get("status", "")) == "filled":
                self.clear_submitted_symbol(symbol)
                if str(order.get("exit_reason", "")) == "untracked_position":
                    self.state.positions_by_symbol.pop(symbol, None)
            elif str(order.get("status", "")) in PENDING_ORDER_STATUSES:
                self.mark_submitted_symbol(symbol)

    def order_target_qty(self, order_link_id: str) -> float:
        order = self.state.orders_by_link.get(order_link_id)
        if order is None:
            return 0.0
        return _float(order.get("target_qty") or order.get("qty"))

    def order_avg_price(self, order_link_id: str) -> float:
        order = self.state.orders_by_link.get(order_link_id)
        if order is None:
            return 0.0
        return _float(order.get("avg_price"))

    def order_row(self, order_link_id: str) -> dict[str, Any]:
        return self.state.orders_by_link.get(order_link_id) or {}

    def mark_order_terminal_from_order_update(
        self,
        *,
        order_link_id: str,
        status: str,
        row: dict[str, Any],
    ) -> list[dict[str, Any]]:
        normalized_status = "cancelled" if status in {"cancelled", "canceled", "deactivated"} else "rejected"
        order = self.state.orders_by_link.get(order_link_id)
        if order is None:
            return []
        symbol = str(row.get("symbol") or order.get("symbol") or "")
        order["status"] = normalized_status
        order["error"] = str(row.get("rejectReason") or row.get("cancelType") or row.get("orderStatus") or "")[:500]
        self.clear_submitted_symbol(symbol)
        return [order]

    def evaluate_symbols(self, symbols: set[str]) -> None:
        """Plan + submit intrabar safety exits for the given symbols.

        Exit-ownership contract (see event_demo.plan_demo_exits for the
        peer half): this function owns stop_loss + take_profit (the
        intrabar trigger checks) with order prefix `lm-ux-*`. The demo
        cycle's plan_demo_exits owns cadence-based exits (event_decay,
        rank_exit, failed_fade, time_stop) with prefix `lm-ex-*`.

        Cross-process race protection: ``exit_submission_active(symbol)``
        skips a symbol with an in-flight reduce-only order so we don't
        submit a competing one while the cycle's submission is settling
        (or vice versa). reduce_only=True caps both paths' worst case
        at position size.
        """
        if self.state.open_trades.is_empty() or not symbols:
            return
        self.expire_stale_submitted_symbols()
        trades = self.state.open_trades.filter(pl.col("symbol").is_in(sorted(symbols)))
        if trades.is_empty():
            return
        exits = plan_risk_exits(
            trades,
            position_by_symbol=self.state.positions_by_symbol,
            price_by_symbol=self.state.price_by_symbol,
            now_ms=_now_ms(),
        )
        for exit_plan in exits:
            symbol = str(exit_plan.get("symbol", ""))
            if symbol and not self.exit_submission_active(symbol):
                self.submit_exit(exit_plan)

    def submit_exit(self, exit_plan: dict[str, Any]) -> None:
        symbol = str(exit_plan["symbol"])
        # Cross-process double-submit guard (P1-2, 2026-05-27), now from in-memory
        # state — NO synchronous parquet read on the stop-submission hot path. The
        # demo cycle and this ws_risk daemon both submit reduce-only exits;
        # ``live_exit_order_symbols`` is refreshed every rest_reconcile (30s) from
        # the authoritative order ledger and covers the demo cycle's reduce-only
        # exits. A reduce-only the demo cycle landed in the last <30s may be missed
        # here, but this is purely an EFFICIENCY guard, not a safety one: the only
        # consequence of a miss is a redundant reduce-only order, which the venue
        # caps/rejects (reduce-only can never flip a position or over-close) and the
        # next cycle's residual pickup resolves — never a missed stop. Trading the
        # rare wasted REST for removing a full cross-process glob-read from the
        # latency-critical stop path is the right call on the risk daemon.
        if (
            self.risk.submit_orders
            and not self.state.all_trades.is_empty()
            and symbol in self.state.live_exit_order_symbols
            and symbol not in self.state.submitted_symbols
        ):
            _logger.info(
                "submit_exit skipped: live reduce-only order on %s already tracked "
                "(in-memory cross-process double-submit guard)",
                symbol,
            )
            self.mark_submitted_symbol(symbol)
            return
        if not self.risk.submit_orders:
            rows, orders = self.rest_exit([exit_plan], submit_orders=False)
        elif self.trade_client is not None and self.risk.order_submit_mode in {"ws", "ws_then_rest"}:
            try:
                rows, orders = self.ws_exit(exit_plan)
            except Exception as exc:  # noqa: BLE001 - REST fallback is the explicit last resort
                self.state.errors.append(str(exc)[:500])
                if not self.risk.rest_fallback:
                    raise
                rows, orders = self.rest_exit([exit_plan], submit_orders=True)
        elif self.risk.rest_fallback:
            rows, orders = self.rest_exit([exit_plan], submit_orders=True)
        else:
            raise RuntimeError("No available risk exit order path")
        self.record_exit_submission_result(symbol, rows, orders)
        self.write_report(reason="exit_submitted")

    def record_exit_submission_result(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> None:
        # _execute_risk_exits / _execute_stop_repairs come from event_demo and
        # don't know about the dual-sleeve world — they emit rows/orders
        # without a `sleeve` column. Tag both lists from the originating trade
        # so _write_*_rows_routed sends them to the correct ledger. Without
        # this, every long-sleeve exit/repair lands in the short ledger.
        self._tag_sleeve_from_trades(rows, orders, fallback_symbol=symbol)
        if rows:
            self.state.all_trades = _upsert_rows(self.state.all_trades, rows, key="trade_id")
            self.state.open_trades = _open_trades(self.state.all_trades)
            self._write_trade_rows_routed(rows)
            self.state.exits.extend(rows)
            for row in rows:
                if str(row.get("status", "")) == "closed":
                    self.state.positions_by_symbol.pop(str(row.get("symbol", "")), None)
        if orders:
            for order in orders:
                link = str(order.get("order_link_id") or "")
                trade_id = str(order.get("trade_id") or "")
                if link and trade_id:
                    self.state.submitted_link_to_trade_id[link] = trade_id
                    self.state.submitted_link_submit_mode[link] = str(order.get("submit_mode") or "submitted")
            self._write_order_rows_routed(orders)
            self._record_orders(orders)
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        has_pending_order = any(str(order.get("status", "")) in PENDING_ORDER_STATUSES for order in orders)
        if symbol in open_symbols and has_pending_order:
            self.mark_submitted_symbol(symbol)
        else:
            self.clear_submitted_symbol(symbol)

    def _tag_sleeve_from_trades(
        self,
        trade_rows: list[dict[str, Any]],
        order_rows: list[dict[str, Any]],
        *,
        fallback_symbol: str = "",
    ) -> None:
        """Fill in the `sleeve` column on rows/orders from event_demo helpers
        that don't carry it. Looks up the row's trade_id in the combined
        ledger; falls back to the symbol lookup; final fallback is 'short'."""
        if not trade_rows and not order_rows:
            return
        trade_index = {
            str(row.get("trade_id") or ""): str(row.get("sleeve") or "")
            for row in self.state.all_trades.to_dicts()
        } if not self.state.all_trades.is_empty() else {}
        symbol_index: dict[str, str] = {}
        if not self.state.all_trades.is_empty():
            for row in self.state.all_trades.to_dicts():
                sym = str(row.get("symbol") or "")
                sleeve = str(row.get("sleeve") or "")
                if sym and sleeve and sym not in symbol_index:
                    symbol_index[sym] = sleeve

        def _resolve(row: dict[str, Any]) -> str:
            existing = str(row.get("sleeve") or "")
            if existing:
                return existing
            tid = str(row.get("trade_id") or "")
            sleeve = trade_index.get(tid, "")
            if sleeve:
                return sleeve
            sym = str(row.get("symbol") or fallback_symbol)
            return symbol_index.get(sym, "short")

        for row in trade_rows:
            row["sleeve"] = _resolve(row)
        for order in order_rows:
            order["sleeve"] = _resolve(order)

    def exit_plan_from_order(self, order: dict[str, Any]) -> dict[str, Any] | None:
        trade_id = str(order.get("trade_id") or "")
        symbol = str(order.get("symbol") or "")
        if not trade_id or not symbol or self.state.open_trades.is_empty():
            return None
        trade_lookup = {str(row["trade_id"]): row for row in self.state.open_trades.to_dicts()}
        trade = trade_lookup.get(trade_id)
        if not trade:
            return None
        bybit_side = str(order.get("side") or "")
        side = str(trade.get("side") or ("short" if bybit_side == "Buy" else "long" if bybit_side == "Sell" else ""))
        return {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "qty": str(order.get("target_qty") or order.get("qty") or trade.get("qty") or ""),
            "exit_reason": str(order.get("exit_reason") or "ws_order_ack_failed"),
            "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or _now_ms(),
            "planned_exit_price": self.state.price_by_symbol.get(symbol, _float(order.get("avg_price"))),
        }

    def ws_exit(self, exit_plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        trade_lookup = {str(row["trade_id"]): row for row in self.state.all_trades.to_dicts()}
        trade = dict(trade_lookup[str(exit_plan["trade_id"])])
        side = str(exit_plan.get("side") or trade.get("side") or "short")
        bybit_side = "Buy" if side == "short" else "Sell"
        symbol = str(exit_plan["symbol"])
        qty = str(exit_plan.get("qty") or trade.get("qty"))
        # Propagate sleeve from the trade into the order so _write_order_rows_routed
        # writes the exit back into the correct ledger. Without this, long-side
        # WS exits land in the short ledger and the long sleeve's reconciliation
        # never sees them.
        sleeve = str(trade.get("sleeve") or ("long" if side == "long" else "short"))
        base_link = _risk_order_link_id("wx", symbol=symbol, ts_ms=_now_ms(), attempt=0)

        # Same split rationale as the main cycle's _execute_exits: a single
        # reduce-only market order > maxMktOrderQty is rejected outright.
        # Trade rows persist max_market_order_qty since 2026-05-27; legacy
        # rows lack it and fall through to no split.
        target_qty_decimal = Decimal(qty) if qty else Decimal("0")
        max_qty_per_order = _float(trade.get("max_market_order_qty"))
        qty_step = _float(trade.get("qty_step"))
        sub_qty_decimals = _split_qty_for_max_order_size(
            target_qty=target_qty_decimal,
            max_qty_per_order=max_qty_per_order,
            qty_step=qty_step,
        )
        sub_qty_strs = [_decimal_text(q) for q in sub_qty_decimals] if target_qty_decimal > 0 else [qty]
        if len(sub_qty_strs) > 1:
            _logger.info(
                "ws_exit split into %d sub-orders symbol=%s target_qty=%s "
                "max_mkt_qty=%s sub_qtys=%s",
                len(sub_qty_strs),
                symbol,
                qty,
                max_qty_per_order,
                sub_qty_strs,
            )

        order_rows: list[dict[str, Any]] = []
        now_ms = _now_ms()
        for idx, sub_qty_str in enumerate(sub_qty_strs):
            sub_link = base_link if len(sub_qty_strs) == 1 else f"{base_link}-s{idx}"
            sub_order_params = _order_params(
                symbol=symbol,
                side=bybit_side,
                qty=sub_qty_str,
                order_type="Market",
                order_link_id=sub_link,
                reduce_only=True,
            )

            def _enqueue_ack(message: dict[str, Any], _link: str = sub_link) -> None:
                payload = dict(message) if isinstance(message, dict) else {"message": message}
                payload["_lm_order_link_id"] = _link
                self.events.put(("ws_order_ack", payload))

            self.trade_client.place_order(_enqueue_ack, **sub_order_params)
            self.state.submitted_link_to_trade_id[sub_link] = str(trade["trade_id"])
            self.state.submitted_link_submit_mode[sub_link] = "ws_submitted"
            order_rows.append(
                {
                    "order_link_id": sub_link,
                    "ts_ms": now_ms,
                    "trade_id": str(trade["trade_id"]),
                    "sleeve": sleeve,
                    "symbol": symbol,
                    "side": bybit_side,
                    "order_type": "Market",
                    "qty": sub_qty_str,
                    "reduce_only": True,
                    "order_id": "",
                    "submit_mode": "ws_submitted",
                    "avg_price": 0.0,
                    "notional_usdt": 0.0,
                    "status": "submitted_unconfirmed",
                    "exit_reason": str(exit_plan["exit_reason"]),
                    "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
                    "target_qty": sub_qty_str,
                    "filled_qty": "",
                }
            )
        return [], order_rows

    def rest_exit(self, exits: list[dict[str, Any]], *, submit_orders: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rest_risk = EventRiskCycleConfig(
            submit_orders=submit_orders,
            confirm_demo_orders=self.risk.confirm_demo_orders,
            telegram=False,
            repair_stops=False,
            exit_order_mode="market",
            settle_coin=self.risk.settle_coin,
        )
        return _execute_risk_exits(
            exits,
            self.state.all_trades,
            trading_client=self.private_client,
            risk=rest_risk,
            now_ms=_now_ms(),
            price_by_symbol=self.state.price_by_symbol,
            tick_size_by_symbol={},
        )

    def repair_exchange_stops(self) -> None:
        if not self.risk.repair_stops:
            return
        repairs = plan_stop_repairs(
            self.state.open_trades,
            position_by_symbol=self.state.positions_by_symbol,
            skip_symbols=self.state.submitted_symbols | self.state.live_exit_order_symbols,
            tolerance_bps=self.risk.stop_tolerance_bps,
        )
        if not repairs:
            return
        rows = _execute_stop_repairs(
            repairs,
            trading_client=self.private_client,
            risk=EventRiskCycleConfig(
                submit_orders=self.risk.submit_orders,
                confirm_demo_orders=self.risk.confirm_demo_orders,
                repair_stops=True,
                settle_coin=self.risk.settle_coin,
            ),
            now_ms=_now_ms(),
        )
        if rows:
            # _execute_stop_repairs emits rows without `sleeve` (it lives in
            # event_demo, which is short-only by design). Tag from the
            # originating trade so the repair order routes to the right ledger.
            self._tag_sleeve_from_trades([], rows)
            self._write_order_rows_routed(rows)
            self.state.repairs.extend(rows)

    def reconcile_positions(self, *, write: bool) -> list[dict[str, Any]]:
        # ``trading_client`` enables the B3 closed-PnL backfill: when an orphan
        # is detected, the reconciler calls ``get_closed_pnl`` to fill in
        # ``exit_price`` / ``gross_trade_return`` / ``net_return`` /
        # ``exit_order_id`` from the actual Bybit close, rather than leaving a
        # zero-PnL "bybit_position_missing" row in the ledger. Without this
        # argument the backfill silently no-ops -- observed live as REQUSDT
        # closing at exit_price=0 after a real venue stop fired.
        # ``position_error`` plumbs the last-known-REST-snapshot health: when
        # the REST positions probe failed the empty ``positions_by_symbol``
        # would otherwise look like "every open trade has vanished" and
        # false-positive orphan-close them all on a transient API hiccup.
        reconciled, rows = _risk_reconcile_missing_positions(
            self.state.open_trades,
            position_by_symbol=self.state.positions_by_symbol,
            now_ms=_now_ms(),
            enabled=self.risk.submit_orders and self.private_client is not None,
            position_error=self.state.last_position_error,
            trading_client=self.private_client,
        )
        self.state.open_trades = reconciled
        if rows:
            self.state.all_trades = _upsert_rows(self.state.all_trades, rows, key="trade_id")
            self.state.reconciliations.extend(rows)
            for row in rows:
                self.clear_submitted_symbol(str(row.get("symbol", "")))
            if write:
                self._write_trade_rows_routed(rows)
        return rows

    def rest_reconcile(self) -> None:
        # When the prefetcher is enabled and has a fresh snapshot, read positions +
        # open-orders from it (non-blocking) instead of making the blocking REST
        # calls on this (consumer) thread. Stale/absent -> inline fetch (the exact
        # legacy path). Default off -> prefetch is always None -> legacy path.
        prefetch = self._reconcile_prefetch if self.risk.reconcile_prefetch_enabled else None
        prefetch_fresh = prefetch is not None and (
            time.monotonic() - float(prefetch["monotonic"]) <= max(self.risk.rest_reconcile_seconds, 1.0)
        )
        if prefetch_fresh:
            raw_positions, error = prefetch["positions"], prefetch["positions_error"]
        else:
            raw_positions, error = _safe_raw_positions(self.private_client, settle_coin=self.risk.settle_coin)
        if error:
            self.state.errors.append(error)
            self.state.last_position_error = error
            return
        # REST snapshot is clean: clear any stale error flag from a prior probe
        # so the orphan reconciler is allowed to act on this fresh state.
        self.state.last_position_error = ""
        snapshot_positions = _active_position_by_symbol(raw_positions)
        if prefetch_fresh:
            # UNION with the WS-maintained positions: a position the WS added since
            # the (slightly older) prefetch snapshot is never dropped — so no stop
            # goes unchecked. Orphan-close then only fires for a symbol absent from
            # BOTH the snapshot and the live WS state (conservative; it may delay a
            # close by a cycle in a rare WS-over-report drift, never miss a stop).
            self.state.positions_by_symbol = {**self.state.positions_by_symbol, **snapshot_positions}
        else:
            self.state.positions_by_symbol = snapshot_positions
        self.state.price_by_symbol.update(_price_lookup_from_positions(self.state.positions_by_symbol))
        self.state.all_trades = self._read_trades_combined()
        self.state.open_trades = _open_trades(self.state.all_trades)
        orders = self._read_orders_combined()
        open_orders_ok = self.refresh_live_exit_order_symbols(
            prefetched=(prefetch["open_orders"], prefetch["open_orders_error"]) if prefetch_fresh else None
        )
        self.reconcile_pending_order_fills(orders)
        orders = self._read_orders_combined()
        self.load_pending_entry_orders(orders)
        self.load_pending_exit_orders(orders)
        if open_orders_ok:
            self.reconcile_flat_pending_exit_orders(orders)
            orders = self._read_orders_combined()
            self.terminalize_stale_pending_entry_orders(orders)
        self.reconcile_positions(write=True)
        self.evaluate_symbols(set(self.state.positions_by_symbol))
        self.repair_exchange_stops()
        self.reconcile_untracked_exit_orders()
        self.adopt_untracked_positions()
        self.exit_untracked_positions()
        # rest_reconcile is also the recovery path when reconcile_stale_websocket
        # fires after a WS silence: this call re-subscribes any tickers that the
        # public stream dropped. Don't move it out of rest_reconcile.
        self.subscribe_tickers(set(self.state.positions_by_symbol) | set(_column_values(self.state.open_trades, "symbol")))
        self.state.last_reconcile_monotonic = time.monotonic()

    def adopt_untracked_positions(self) -> None:
        """Adopt exchange positions that have no ledger trade as tracked trades,
        so the normal stop-loss / take-profit / max-hold exit logic manages them.
        Leftover positions after a restart are taken over rather than flattened.
        Runs before exit_untracked_positions so an adopted position is no longer
        seen as untracked."""
        if not self.risk.adopt_untracked_positions:
            return
        self.expire_stale_submitted_symbols()
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        now_ms = _now_ms()
        grace_ms = int(max(self.risk.untracked_position_grace_seconds, 0.0) * 1000.0)
        adopted: list[dict[str, Any]] = []
        active_position_symbols: set[str] = set()
        for position in list(self.state.positions_by_symbol.values()):
            symbol = str(position.get("symbol", ""))
            if not symbol or _float(position.get("size")) <= 0.0:
                continue
            active_position_symbols.add(symbol)
            if (
                symbol in open_symbols
                or symbol in self.state.pending_entry_symbols
                or self.exit_submission_active(symbol)
            ):
                self.state.untracked_first_seen_ms.pop(symbol, None)
                continue
            first_seen = self.state.untracked_first_seen_ms.setdefault(symbol, now_ms)
            if now_ms - first_seen < grace_ms:
                continue
            trade = self._build_adopted_trade(position, now_ms=now_ms)
            if trade is None:
                continue
            ok, reason = _validate_trade_row_invariants(trade)
            if not ok:
                _logger.warning(
                    "adoption: dropping trade for %s — invariant violation: %s; row=%s",
                    symbol, reason, {k: trade.get(k) for k in ("trade_id", "signal_ts_ms", "entry_ts_ms", "opened_at_ms", "planned_exit_ts_ms")},
                )
                continue
            adopted.append(trade)
            open_symbols.add(symbol)
            self.state.untracked_first_seen_ms.pop(symbol, None)
        for stale_symbol in [s for s in self.state.untracked_first_seen_ms if s not in active_position_symbols]:
            self.state.untracked_first_seen_ms.pop(stale_symbol, None)
        if not adopted:
            return
        self.state.all_trades = _upsert_rows(self.state.all_trades, adopted, key="trade_id")
        self.state.open_trades = _open_trades(self.state.all_trades)
        # _build_adopted_trade tags each row's `sleeve` from the venue position
        # side, so a LONG orphan lands in the long ledger and a SHORT orphan
        # in the short ledger via the routed writer below.
        self._write_trade_rows_routed(adopted)
        for trade in adopted:
            _logger.warning(
                "untracked_position adopt symbol=%s side=%s qty=%s entry_price=%s stop=%s tp=%s planned_exit_ts_ms=%s",
                trade.get("symbol"),
                trade.get("side"),
                trade.get("qty"),
                trade.get("entry_price"),
                trade.get("stop_price"),
                trade.get("take_profit_price"),
                trade.get("planned_exit_ts_ms"),
            )
        self.write_report(reason="untracked_positions_adopted")
        self.evaluate_symbols({str(trade.get("symbol", "")) for trade in adopted})
        self.repair_exchange_stops()

    def _build_adopted_trade(self, position: dict[str, Any], *, now_ms: int) -> dict[str, Any] | None:
        symbol = str(position.get("symbol", ""))
        qty = str(position.get("size") or "")
        entry_price = _first_price(position, ("avgPrice", "avg_price", "entryPrice", "entry_price"))
        side = _normalized_position_side(position.get("side"))
        if not symbol or _float(qty) <= 0.0 or entry_price <= 0.0 or side not in {"long", "short"}:
            return None
        opened_ms = _int(position.get("createdTime") or position.get("created_time")) or now_ms
        stop_loss_pct = max(self.risk.adopt_stop_loss_pct, 0.0)
        take_profit_pct = max(self.risk.adopt_take_profit_pct, 0.0)
        tick_size = _float(position.get("tickSize") or position.get("tick_size"))
        stop_price = (
            _stop_price_for_entry(entry_price=entry_price, side=side, stop_loss_pct=stop_loss_pct, tick_size=tick_size)
            if stop_loss_pct > 0.0
            else 0.0
        )
        take_profit_price = _take_profit_price_for_entry(
            entry_price=entry_price, side=side, take_profit_pct=take_profit_pct, tick_size=tick_size
        )
        planned_exit_ts_ms = opened_ms + int(max(self.risk.adopt_hold_days, 0.0) * MS_PER_DAY)
        # Sleeve tag drives the routed writer: a LONG orphan must land in the
        # long ledger, not the short. Without this tag _sleeve_of() defaults to
        # 'short' and the adopted trade goes to event_demo_trades — downstream
        # plan_risk_exits then correctly computes a Sell reduce-only (from the
        # `side` column), but ws_risk would write the close into the wrong
        # ledger so the long sleeve's open-trade tracking diverges from venue
        # reality. Tag from the venue-observed position side.
        sleeve = "long" if side == "long" else "short"
        # Rebuild-safe recovery: before falling back to the lossy adopted-*
        # trade_id, look up Bybit's order history for this symbol and try to
        # find the original entry order. Our entry order_link_ids encode
        # signal_ts (lm-en-{base}-{ts36} short, lm-en-l-{base}-{ts36} long),
        # so we can decode them back to (sleeve, signal_ts_ms) and rebuild
        # the deterministic strategy trade_id verbatim — which is what the
        # paper sleeve uses, so reconciliation can now pair on these post-
        # rebuild positions instead of seeing 3 demo_only / 3 paper_only.
        recovered = self._recover_entry_link_metadata(symbol=symbol, side=side)
        if recovered is not None:
            link, strategy_id, signal_ts_ms, decoded_sleeve = recovered
            trade_id = f"{strategy_id}-{symbol}-{signal_ts_ms}"
            # entry_ts_ms must reflect the actual fill time (Bybit's
            # createdTime) not signal_ts. The cycle's exit logic computes
            # planned_exit_ts_ms = entry_ts_ms + hold_days*MS_PER_DAY and
            # event_decay rank-checks start FROM entry_ts_ms — putting
            # signal_ts (which can be 1-6h earlier than the actual fill)
            # in entry_ts_ms makes the position look older than it is and
            # trips both exits prematurely. Observed live 2026-05-25:
            # WAVESUSDT got event_decay on demo ~13h after signal while
            # paper (correct entry_ts) still held the position.
            return {
                "trade_id": trade_id,
                "sleeve": decoded_sleeve,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": side,
                "status": "open",
                "qty": qty,
                "entry_price": entry_price,
                # Adopted positions carry zero fee/venue-time on the ledger by
                # default; the demo↔Bybit reconciliation will surface the real
                # fee as a pnl_gap on this trade, which is the correct semantic
                # ("we don't know what we paid; ask the venue"). A future
                # enhancement could query get_trade_history to backfill.
                "entry_fee_usdt": 0.0,
                "entry_exec_time_ms": opened_ms,
                "notional_usdt": abs(entry_price * _float(qty)),
                "ts_ms": now_ms,
                "entry_ts_ms": opened_ms,
                "opened_at_ms": opened_ms,
                "updated_at_ms": now_ms,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "planned_exit_ts_ms": opened_ms + int(max(self.risk.adopt_hold_days, 0.0) * MS_PER_DAY),
                "entry_order_link_id": link,
                "entry_order_id": "",
                "signal_ts_ms": signal_ts_ms,
                "submit_mode": "adopted_recovered",
            }
        return {
            "trade_id": f"adopted-{symbol}-{opened_ms}",
            "sleeve": sleeve,
            "strategy_id": "adopted",
            "symbol": symbol,
            "side": side,
            "status": "open",
            "qty": qty,
            "entry_price": entry_price,
            # See above: zero fee/venue-time on adopted ledger; reconciliation
            # surfaces the real fee as pnl_gap.
            "entry_fee_usdt": 0.0,
            "entry_exec_time_ms": opened_ms,
            "notional_usdt": abs(entry_price * _float(qty)),
            "ts_ms": now_ms,
            "entry_ts_ms": opened_ms,
            "opened_at_ms": opened_ms,
            "updated_at_ms": now_ms,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "planned_exit_ts_ms": planned_exit_ts_ms,
            "entry_order_link_id": "",
            "entry_order_id": "",
            # Signal_ts unknown for hand-placed positions — leave 0 so the
            # reconciliation doesn't accidentally pair a random other trade.
            "signal_ts_ms": 0,
            "submit_mode": "adopted",
        }

    def _recover_entry_link_metadata(
        self, *, symbol: str, side: str,
    ) -> tuple[str, str, int, str] | None:
        """Find the original bot-placed entry order for ``symbol`` and decode
        its orderLinkId into (link, strategy_id, signal_ts_ms, sleeve).
        Returns None when the symbol has no bot-generated entry in the recent
        order history — the caller falls back to the lossy adopted-* path
        (typically hand-placed positions or positions older than the order
        history window)."""
        client = self.private_client
        if client is None:
            return None
        try:
            history = client.get_order_history(
                symbol=symbol, limit=int(self.risk.adopt_order_history_limit),
            )
        except Exception as exc:  # noqa: BLE001 - recovery is best-effort; never break adoption
            _logger.warning(
                "adoption recovery: get_order_history failed symbol=%s: %s; "
                "falling back to adopted-*", symbol, exc,
            )
            return None
        venue_side = "Buy" if side == "long" else "Sell"
        for order in history:
            order_side = str(order.get("side") or "")
            if order_side != venue_side:
                continue
            link = str(order.get("orderLinkId") or order.get("order_link_id") or "")
            decoded = decode_entry_order_link_id(link)
            if decoded is None:
                continue
            decoded_sleeve, signal_ts_ms = decoded
            strategy_id = self._adopt_strategy_id_for_sleeve(decoded_sleeve)
            if not strategy_id:
                continue
            return link, strategy_id, signal_ts_ms, decoded_sleeve
        return None

    def _adopt_strategy_id_for_sleeve(self, sleeve: str) -> str:
        """Resolve the strategy_id used to reconstruct a deterministic
        trade_id for a recovered adoption. Falls back to canonical defaults
        when adopt_*_strategy_id was left empty in EventWebSocketRiskConfig."""
        if sleeve == "long":
            return self.risk.adopt_long_strategy_id or MULTI_STRAT_V1_STRATEGY_ID
        if sleeve == "short":
            if self.risk.adopt_short_strategy_id:
                return self.risk.adopt_short_strategy_id
            # Match the canonical promoted scenario the live demo daemon runs.
            # Derived via _selected_scenario(_demo_event_config(...)) so any
            # change to the promoted scenario's deterministic id automatically
            # flows through here.
            try:
                strategy = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
                scenario = _selected_scenario(strategy)
            except Exception:  # noqa: BLE001 - never let derivation break adoption
                return ""
            return str(getattr(scenario, "scenario_id", "") or "")
        return ""

    def exit_untracked_positions(self) -> None:
        if not self.risk.exit_untracked_positions:
            return
        self.expire_stale_submitted_symbols()
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        now_ms = _now_ms()
        grace_ms = int(max(self.risk.untracked_position_grace_seconds, 0.0) * 1000.0)
        rows: list[dict[str, Any]] = []
        active_position_symbols: set[str] = set()
        for position in list(self.state.positions_by_symbol.values()):
            symbol = str(position.get("symbol", ""))
            qty = str(position.get("size") or "")
            if not symbol or _float(qty) <= 0.0:
                continue
            active_position_symbols.add(symbol)
            if (
                symbol in open_symbols
                or symbol in self.state.pending_entry_symbols
                or self.exit_submission_active(symbol)
            ):
                self.state.untracked_first_seen_ms.pop(symbol, None)
                continue
            first_seen = self.state.untracked_first_seen_ms.setdefault(symbol, now_ms)
            if now_ms - first_seen < grace_ms:
                continue
            side_text = str(position.get("side") or "").lower()
            close_side = "Sell" if side_text in {"buy", "long"} else "Buy"
            attempt = sum(
                1
                for order in self.state.orders
                if str(order.get("symbol", "")) == symbol and str(order.get("exit_reason", "")) == "untracked_position"
            )
            link = _risk_order_link_id("ux", symbol=symbol, ts_ms=now_ms, attempt=attempt)
            order_result: dict[str, Any] = {}
            exec_summary: dict[str, Any] = {}
            submit_mode = "dry_run"
            status = "planned"
            error = ""
            if self.risk.submit_orders:
                if not self.risk.rest_fallback:
                    submit_mode = "error"
                    status = "failed"
                    error = "untracked position exit requires REST fallback in Bybit demo mode"
                else:
                    try:
                        assert self.private_client is not None
                        order_result = self.private_client.place_order(
                            **_order_params(
                                symbol=symbol,
                                side=close_side,
                                qty=qty,
                                order_type="Market",
                                order_link_id=link,
                                reduce_only=True,
                            )
                        )
                        submit_mode = "submitted"
                    except Exception as exc:  # noqa: BLE001 - untracked positions must be surfaced and retried
                        submit_mode = "error"
                        status = "failed"
                        error = str(exc)[:500]
                        self.state.errors.append(error)
                    if submit_mode == "submitted":
                        try:
                            exec_summary = _execution_summary(
                                self.private_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
                            )
                        except Exception as exc:  # noqa: BLE001 - accepted reduce-only order remains pending for reconciliation
                            status = "submitted_unconfirmed"
                            error = f"fill confirmation failed: {exc}"[:500]
                            self.mark_submitted_symbol(symbol, now_ms=now_ms)
                        else:
                            filled_qty = _float(exec_summary.get("qty"))
                            target_qty = _float(qty)
                            if target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty:
                                status = "filled"
                                self.state.positions_by_symbol.pop(symbol, None)
                            elif filled_qty > 0.0:
                                status = "partial"
                                self.mark_submitted_symbol(symbol, now_ms=now_ms)
                            else:
                                status = "submitted_unconfirmed"
                                self.mark_submitted_symbol(symbol, now_ms=now_ms)
            filled_qty = _float(exec_summary.get("qty")) if exec_summary else 0.0
            avg_price = _float(exec_summary.get("avg_price")) or _position_price(position)
            rows.append(
                {
                    "order_link_id": link,
                    "ts_ms": now_ms,
                    "trade_id": "",
                    "symbol": symbol,
                    "side": close_side,
                    "order_type": "Market",
                    "qty": qty,
                    "reduce_only": True,
                    "order_id": order_result.get("orderId", ""),
                    "submit_mode": submit_mode,
                    "avg_price": avg_price,
                    "notional_usdt": abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0,
                    "status": status,
                    "exit_reason": "untracked_position",
                    "target_qty": qty,
                    "filled_qty": str(filled_qty) if filled_qty > 0.0 else "",
                    "error": error,
                }
            )
        for stale_symbol in [s for s in self.state.untracked_first_seen_ms if s not in active_position_symbols]:
            self.state.untracked_first_seen_ms.pop(stale_symbol, None)
        if not rows:
            return
        self._write_order_rows_routed(rows)
        self._record_orders(rows)
        for row in rows:
            _logger.warning(
                "untracked_position close symbol=%s side=%s qty=%s status=%s submit_mode=%s grace_seconds=%.1f error=%s",
                row.get("symbol"),
                row.get("side"),
                row.get("qty"),
                row.get("status"),
                row.get("submit_mode"),
                self.risk.untracked_position_grace_seconds,
                row.get("error") or "",
            )
        self.write_report(reason="untracked_exit_submitted")

    def reconcile_untracked_exit_orders(self) -> None:
        if self.private_client is None:
            return
        active_symbols = set(self.state.positions_by_symbol)
        updates: list[dict[str, Any]] = []
        for order in self.state.orders:
            if str(order.get("exit_reason", "")) != "untracked_position":
                continue
            if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            symbol = str(order.get("symbol", ""))
            link = str(order.get("order_link_id", ""))
            target_qty = _float(order.get("target_qty") or order.get("qty"))
            position_flat = symbol and symbol not in active_symbols
            try:
                summary = _execution_summary(self.private_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50))
            except Exception as exc:  # noqa: BLE001 - keep pending guard active and retry
                if position_flat:
                    summary = {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0}
                else:
                    order["error"] = f"fill reconciliation failed: {exc}"[:500]
                    order["updated_at_ms"] = _now_ms()
                    updates.append(dict(order))
                    self.mark_submitted_symbol(symbol)
                    continue
            filled_qty = _float(summary.get("qty"))
            avg_price = _float(summary.get("avg_price")) or _float(order.get("avg_price"))
            if filled_qty <= 0.0 and position_flat:
                filled_qty = target_qty
            if filled_qty <= 0.0:
                continue
            full = target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty
            order["status"] = "filled" if full or position_flat else "partial"
            order["filled_qty"] = str(filled_qty)
            order["avg_price"] = avg_price
            order["notional_usdt"] = abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0
            updates.append(dict(order))
            if order["status"] == "filled":
                self.clear_submitted_symbol(symbol)
            else:
                self.mark_submitted_symbol(symbol)
        if updates:
            self._write_order_rows_routed(updates)

    def reconcile_flat_pending_exit_orders(self, orders: pl.DataFrame) -> None:
        if orders.is_empty():
            return
        active_symbols = set(self.state.positions_by_symbol)
        trade_lookup = {str(row["trade_id"]): row for row in self.state.open_trades.to_dicts()}
        now_ms = _now_ms()
        order_updates: list[dict[str, Any]] = []
        trade_updates: list[dict[str, Any]] = []
        for order in orders.to_dicts():
            if not _bool(order.get("reduce_only")):
                continue
            if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            if not str(order.get("exit_reason", "")):
                continue
            symbol = str(order.get("symbol") or "")
            link = str(order.get("order_link_id") or "")
            if not symbol or not link:
                continue
            if symbol in active_symbols or symbol in self.state.live_exit_order_symbols:
                continue
            target_qty = str(order.get("target_qty") or order.get("qty") or "")
            filled_qty = target_qty if _float(target_qty) > 0.0 else str(order.get("filled_qty") or "")
            avg_price = _float(order.get("avg_price"))
            filled_qty_float = _float(filled_qty)
            order_update = dict(order)
            order_update.update(
                {
                    "status": "filled",
                    "filled_qty": filled_qty,
                    "notional_usdt": abs(avg_price * filled_qty_float) if avg_price > 0.0 else _float(order.get("notional_usdt")),
                    "updated_at_ms": now_ms,
                }
            )
            if not str(order_update.get("error") or ""):
                order_update["error"] = "filled inferred from flat Bybit position"
            order_updates.append(order_update)
            self.clear_submitted_symbol(symbol)
            existing = self.state.orders_by_link.get(link)
            if existing is not None:
                existing.update(order_update)
            else:
                self._record_orders([order_update])

            trade_id = str(order.get("trade_id") or "")
            trade = dict(trade_lookup.get(trade_id, {}))
            if not trade:
                continue
            close_exit_price = avg_price
            close_exit_ts_ms = now_ms
            close_exit_trigger_ts_ms = _int(order.get("exit_trigger_ts_ms")) or now_ms
            close_submit_mode = str(order.get("submit_mode") or "position_flat_reconciled")
            close_exit_order_id = order.get("order_id", "")
            # If the recovered order has no avg_price (failed lm-rx fill
            # confirmation while the venue later went flat under its own
            # stop), fall back to closed-PnL backfill so the trade row
            # closes with a real venue price instead of exit_price=0. Same
            # backfill helper the orphan reconciler uses. Observed live as
            # a DRIFT-style close: failed lm-rx, closed trade, null
            # exit_price — broken audit / reconciliation downstream.
            if close_exit_price <= 0.0 and self.private_client is not None:
                backfill = _orphan_close_pnl_backfill(
                    trade, now_ms=now_ms, trading_client=self.private_client
                )
                if backfill:
                    close_exit_price = _float(backfill.get("exit_price")) or close_exit_price
                    close_exit_ts_ms = int(backfill.get("exit_ts_ms") or close_exit_ts_ms)
                    close_exit_trigger_ts_ms = int(
                        backfill.get("exit_trigger_ts_ms") or close_exit_trigger_ts_ms
                    )
                    close_submit_mode = str(backfill.get("submit_mode") or close_submit_mode)
                    close_exit_order_id = backfill.get("exit_order_id") or close_exit_order_id
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": close_exit_ts_ms,
                    "exit_trigger_ts_ms": close_exit_trigger_ts_ms,
                    "exit_price": close_exit_price,
                    "exit_reason": str(order.get("exit_reason") or "pending_exit_position_flat"),
                    "exit_order_link_id": link,
                    "exit_order_id": close_exit_order_id,
                    "submit_mode": close_submit_mode,
                    "closed_at_ms": close_exit_ts_ms,
                    "updated_at_ms": now_ms,
                }
            )
            trade_updates.append(trade)
        if order_updates:
            self._write_order_rows_routed(order_updates)
        if trade_updates:
            self.state.all_trades = _upsert_rows(self.state.all_trades, trade_updates, key="trade_id")
            self.state.open_trades = _open_trades(self.state.all_trades)
            self.state.pending_fill_reconciliations.extend(trade_updates)
            self._write_trade_rows_routed(trade_updates)

    def refresh_live_exit_order_symbols(self, prefetched: tuple[Any, str] | None = None) -> bool:
        if prefetched is not None:
            open_orders, error = prefetched
        else:
            open_orders, error = _safe_open_orders(self.private_client, settle_coin=self.risk.settle_coin)
        if error:
            self.state.errors.append(error)
            return False
        self.state.live_entry_order_symbols = _live_open_order_symbols(open_orders, reduce_only=False)
        self.state.live_exit_order_symbols = _live_open_order_symbols(open_orders, reduce_only=True)
        return True

    def exit_submission_active(self, symbol: str) -> bool:
        return symbol in self.state.submitted_symbols or symbol in self.state.live_exit_order_symbols

    def reconcile_pending_order_fills(self, orders: pl.DataFrame) -> None:
        if orders.is_empty() or self.private_client is None:
            return
        trade_rows, order_rows = _reconcile_pending_order_fills(
            orders,
            self.state.all_trades,
            trading_client=self.private_client,
            demo=EventDemoCycleConfig(
                submit_orders=self.risk.submit_orders,
                confirm_demo_orders=self.risk.confirm_demo_orders,
            ),
            now_ms=_now_ms(),
            live_position_symbols=set(self.state.positions_by_symbol),
            live_open_order_symbols=self.state.live_entry_order_symbols | self.state.live_exit_order_symbols,
        )
        if trade_rows:
            self.state.all_trades = _upsert_rows(self.state.all_trades, trade_rows, key="trade_id")
            self.state.open_trades = _open_trades(self.state.all_trades)
            self.state.pending_fill_reconciliations.extend(trade_rows)
            self._write_trade_rows_routed(trade_rows)
        if order_rows:
            for update in order_rows:
                link = str(update.get("order_link_id") or "")
                if not link:
                    continue
                order = self.state.orders_by_link.get(link)
                if order is not None:
                    order.update(update)
            self._write_order_rows_routed(order_rows)

    def terminalize_stale_pending_entry_orders(self, orders: pl.DataFrame) -> None:
        if orders.is_empty():
            return
        order_rows = _terminalize_stale_pending_entry_orders(
            orders,
            live_position_symbols=set(self.state.positions_by_symbol),
            live_open_entry_order_symbols=self.state.live_entry_order_symbols,
            now_ms=_now_ms(),
        )
        if not order_rows:
            return
        for update in order_rows:
            symbol = str(update.get("symbol") or "")
            if symbol:
                self.state.pending_entry_symbols.discard(symbol)
            link = str(update.get("order_link_id") or "")
            if not link:
                continue
            order = self.state.orders_by_link.get(link)
            if order is not None:
                order.update(update)
        self._write_order_rows_routed(order_rows)

    def on_idle(self) -> None:
        now = time.monotonic()
        self.reconcile_stale_websocket(now)
        if self.risk.rest_reconcile_seconds > 0 and now - self.state.last_reconcile_monotonic >= self.risk.rest_reconcile_seconds:
            self.rest_reconcile()
        if self.risk.heartbeat_seconds > 0 and now - self.state.last_report_monotonic >= self.risk.heartbeat_seconds:
            self.write_report(reason="heartbeat")

    def reconcile_stale_websocket(self, now: float) -> None:
        if self.risk.stale_ws_seconds <= 0.0 or not self.risk.rest_fallback:
            return
        has_active_work = bool(self.state.subscribed_symbols or self.state.positions_by_symbol) or not self.state.open_trades.is_empty()
        if not has_active_work:
            return
        ws_age = now - self.state.last_ws_event_monotonic
        if ws_age < self.risk.stale_ws_seconds:
            return
        if now - self.state.last_stale_reconcile_monotonic < self.risk.stale_ws_seconds:
            return
        self.state.errors.append(f"websocket stale for {ws_age:.1f}s; forced REST reconcile")
        self.rest_reconcile()
        self.state.last_stale_reconcile_monotonic = now

    def load_pending_exit_orders(self, orders: pl.DataFrame) -> None:
        if orders.is_empty():
            return
        open_trade_ids = set(_column_values(self.state.open_trades, "trade_id"))
        loaded_order_links = set(self.state.orders_by_link)
        now_ms = _now_ms()
        max_age_ms = max(self.risk.pending_exit_guard_seconds, 0.0) * 1000.0
        for row in orders.to_dicts():
            link = str(row.get("order_link_id") or "")
            trade_id = str(row.get("trade_id") or "")
            symbol = str(row.get("symbol") or "")
            exit_reason = str(row.get("exit_reason", ""))
            is_untracked_exit = exit_reason == "untracked_position"
            if not link or not symbol:
                continue
            if trade_id:
                if trade_id not in open_trade_ids:
                    continue
            elif not is_untracked_exit:
                continue
            if not _bool(row.get("reduce_only")) or not exit_reason:
                continue
            if str(row.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            ts_ms = int(row.get("ts_ms") or 0)
            if ts_ms > 0 and max_age_ms > 0 and now_ms - ts_ms > max_age_ms:
                continue
            if trade_id:
                self.state.submitted_link_to_trade_id[link] = trade_id
            self.state.submitted_link_submit_mode[link] = str(row.get("submit_mode") or "submitted")
            self.mark_submitted_symbol(symbol, now_ms=ts_ms or now_ms)
            if link not in loaded_order_links:
                self._record_orders([dict(row)])
                loaded_order_links.add(link)

    def load_pending_entry_orders(self, orders: pl.DataFrame) -> None:
        self.state.pending_entry_symbols.clear()
        if orders.is_empty():
            return
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        now_ms = _now_ms()
        for row in orders.to_dicts():
            symbol = str(row.get("symbol") or "")
            link = str(row.get("order_link_id") or "")
            trade_id = str(row.get("trade_id") or "")
            if not symbol or not link or not trade_id or symbol in open_symbols:
                continue
            if _bool(row.get("reduce_only")):
                continue
            if str(row.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            ts_ms = _int(row.get("ts_ms"))
            if ts_ms > 0 and now_ms - ts_ms > PENDING_ORDER_GUARD_MS:
                continue
            self.state.pending_entry_symbols.add(symbol)

    def mark_submitted_symbol(self, symbol: str, *, now_ms: int | None = None) -> None:
        if not symbol:
            return
        self.state.submitted_symbols.add(symbol)
        self.state.submitted_symbol_ts_ms[symbol] = now_ms if now_ms is not None else _now_ms()

    def clear_submitted_symbol(self, symbol: str) -> None:
        if not symbol:
            return
        self.state.submitted_symbols.discard(symbol)
        self.state.submitted_symbol_ts_ms.pop(symbol, None)

    def expire_stale_submitted_symbols(self) -> None:
        max_age_ms = max(self.risk.pending_exit_guard_seconds, 0.0) * 1000.0
        if max_age_ms <= 0.0:
            return
        now_ms = _now_ms()
        for symbol, ts_ms in list(self.state.submitted_symbol_ts_ms.items()):
            if ts_ms > 0 and now_ms - ts_ms > max_age_ms:
                self.clear_submitted_symbol(symbol)

    def _prune_state_logs(self) -> None:
        """Cap the append-only telemetry logs so a long-lived daemon can't OOM
        (which would orphan an open position). Cumulative report counters add
        the evicted total back, so the reported counts stay exact."""
        retention = getattr(self.risk, "telemetry_log_retention", _LOG_RETENTION)
        for name in (
            "exits", "repairs", "reconciliations", "pending_fill_reconciliations", "errors",
        ):
            log = getattr(self.state, name)
            overflow = len(log) - retention
            if overflow > 0:
                evicted_attr = f"{name}_evicted"
                setattr(self.state, evicted_attr, getattr(self.state, evicted_attr) + overflow)
                del log[:overflow]

    def write_report(self, *, reason: str) -> dict[str, Any]:
        self._prune_state_logs()
        now_ms = _now_ms()
        position_snapshot = build_position_pnl_snapshot(list(self.state.positions_by_symbol.values()))
        bybit_summary = summarize_position_pnl(position_snapshot)
        # P1-3 alignment: prefer position-level markPrice over ticker mark for
        # ledger uPnL so it matches Bybit's own position uPnL by construction.
        ledger_positions = build_ledger_position_pnl_snapshot(
            self.state.open_trades,
            self.state.price_by_symbol,
            position_by_symbol=self.state.positions_by_symbol,
        )
        ledger_summary = summarize_position_pnl(ledger_positions)
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        pending_entry_fills = sum(
            1
            for row in self.state.pending_fill_reconciliations
            if str(row.get("status", "")) == "open" and not str(row.get("partial_exit_order_link_id") or "")
        )
        pending_exit_fills = sum(
            1
            for row in self.state.pending_fill_reconciliations
            if str(row.get("status", "")) == "closed" or str(row.get("partial_exit_order_link_id") or "")
        )
        pending_entry_positions = [
            row
            for row in position_snapshot
            if str(row.get("symbol", "")) and str(row.get("symbol", "")) in self.state.pending_entry_symbols
            and str(row.get("symbol", "")) not in open_symbols
        ]
        untracked_positions = [
            row
            for row in position_snapshot
            if str(row.get("symbol", ""))
            and str(row.get("symbol", "")) not in open_symbols
            and str(row.get("symbol", "")) not in self.state.pending_entry_symbols
        ]
        cycle = {
            "cycle_id": f"ws-risk-{now_ms}",
            "ts_ms": now_ms,
            "mode": "ws_risk_submit" if self.risk.submit_orders else "ws_risk_dry_run",
            "reason": reason,
            "symbols": len(open_symbols),
            "entry_candidates": 0,
            "entries_executed": 0,
            "exit_candidates": len(self.state.orders),
            "exits_executed": len(self.state.exits) + self.state.exits_evicted,
            "stop_repairs": len(self.state.repairs) + self.state.repairs_evicted,
            "pending_entry_positions": len(pending_entry_positions),
            "pending_fills_reconciled": len(self.state.pending_fill_reconciliations) + self.state.pending_fill_reconciliations_evicted,
            "pending_order_fills_reconciled": len(self.state.pending_fill_reconciliations) + self.state.pending_fill_reconciliations_evicted,
            "pending_entry_fills_reconciled": pending_entry_fills,
            "pending_exit_fills_reconciled": pending_exit_fills,
            "untracked_exits_submitted": sum(1 for row in self.state.orders if str(row.get("exit_reason", "")) == "untracked_position"),
            "bybit_live_exit_open_orders": len(self.state.live_exit_order_symbols),
            "open_trades_before": self.state.open_trades.height,
            "open_trades_after": self.state.open_trades.height,
            "equity_usdt": 0.0,
            "bybit_positions": bybit_summary["positions"],
            "bybit_position_value_usdt": bybit_summary["position_value_usdt"],
            "bybit_unrealized_pnl_usdt": bybit_summary["unrealized_pnl_usdt"],
            "bybit_position_pnl_pct": bybit_summary["pnl_pct"],
            "ledger_positions": ledger_summary["positions"],
            "ledger_position_value_usdt": ledger_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_summary["pnl_pct"],
            "position_report_error": "; ".join(self.state.errors[-3:]),
            "untracked_positions": len(untracked_positions),
            "ws_order_unavailable": self.state.ws_order_unavailable,
            "telegram_sent": False,
            "telegram_error": "",
        }
        payload = {
            "cycle": cycle,
            "risk_config": asdict(self.risk),
            "exits": self.state.exits[-20:],
            "exit_orders": self.state.orders[-20:],
            "stop_repairs": self.state.repairs[-20:],
            "reconciliations": self.state.reconciliations[-20:],
            "pending_fill_reconciliations": self.state.pending_fill_reconciliations[-20:],
            "pending_entry_positions": pending_entry_positions,
            "untracked_positions": untracked_positions,
            "bybit_positions": position_snapshot,
            "bybit_position_summary": bybit_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_summary,
            "report_dir": str(self.report_dir),
        }
        telegram_sent, telegram_error = self.maybe_notify(payload)
        cycle["telegram_sent"] = telegram_sent
        cycle["telegram_error"] = telegram_error
        payload["cycle"] = cycle
        latest_json_path = self.report_dir / "latest_event_ws_risk_cycle.json"
        latest_md_path = self.report_dir / "latest_event_ws_risk_cycle.md"
        payload["report_path"] = str(latest_md_path)
        if _persist_ws_risk_history(payload):
            history_json_path = self.report_dir / f"event_ws_risk_cycle_{cycle['cycle_id']}.json"
            history_md_path = self.report_dir / f"event_ws_risk_cycle_{cycle['cycle_id']}.md"
            payload["history_report_path"] = str(history_md_path)
            history_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            history_md_path.write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
        # Date-partitioned: append-only telemetry, see event_demo.py. partition_by=()
        # made every cycle read + rewrite the whole (unbounded) dataset.
        write_dataset(pl.DataFrame([cycle]), self.root, "event_demo_cycles", partition_by=("date",))
        latest_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        latest_md_path.write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
        self.state.last_report_monotonic = time.monotonic()
        return payload

    def _telegram_sender_loop(self) -> None:
        """Background daemon: drain queued payloads and do the blocking HTTP send.
        A None payload is the shutdown sentinel."""
        while True:
            payload = self._telegram_queue.get()
            if payload is None:
                return
            try:
                _maybe_notify(payload, enabled=True)
            except Exception as exc:  # noqa: BLE001 - a notification must never crash the daemon
                _logger.warning("background telegram send failed: %s", exc)

    def _enqueue_telegram(self, payload: dict[str, Any]) -> None:
        if self._telegram_thread is None or not self._telegram_thread.is_alive():
            self._telegram_thread = threading.Thread(
                target=self._telegram_sender_loop, name="ws-risk-telegram", daemon=True
            )
            self._telegram_thread.start()
        self._telegram_queue.put(payload)

    def maybe_notify(self, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.risk.telegram:
            return False, "disabled"
        reason = _telegram_notification_reason(payload)
        if not reason:
            return False, "quiet_no_material_event"
        key = _telegram_dedupe_key(reason, payload)
        if key in self.state.telegram_keys_sent:
            return False, "duplicate_material_event"
        # Optimistic dedupe + offload the blocking HTTP send: record the dedupe key
        # on the consumer thread (cheap state mutation) and hand the network
        # round-trip to the background sender, returning immediately so a slow
        # Telegram RTT cannot stall stop-enforcement event processing. A failed
        # send is logged by the sender, not retried — this is a notification, not
        # an order, so optimistic dedupe (assume it sends) is acceptable.
        self.state.telegram_keys_sent.add(key)
        _write_telegram_dedupe_keys(self.report_dir, self.state.telegram_keys_sent)
        self._enqueue_telegram(payload)
        return True, "enqueued"

    def close(self) -> None:
        # Stop the background reconcile-prefetcher.
        self._reconcile_prefetch_stop.set()
        if self._reconcile_prefetch_thread is not None and self._reconcile_prefetch_thread.is_alive():
            self._reconcile_prefetch_thread.join(timeout=5.0)
        # Drain + stop the background telegram sender (sentinel after any pending
        # payloads so queued notifications still go out before exit).
        if self._telegram_thread is not None and self._telegram_thread.is_alive():
            self._telegram_queue.put(None)
            self._telegram_thread.join(timeout=5.0)
        for client in (self.private_stream, self.public_stream, self.trade_client):
            close = getattr(client, "close", None)
            if callable(close):
                close()


def run_event_ws_risk(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    risk_config: EventWebSocketRiskConfig | None = None,
    private_client: Any | None = None,
    private_stream: Any | None = None,
    public_stream: Any | None = None,
    trade_client: Any | None = None,
) -> dict[str, Any]:
    _ensure_default_log_handler()
    root = Path(data_root).expanduser()
    _logger.info(
        "event_ws_risk starting data_root=%s submit_orders=%s order_submit_mode=%s "
        "rest_reconcile_seconds=%.1f untracked_position_grace_seconds=%.1f",
        root,
        (risk_config or EventWebSocketRiskConfig()).submit_orders,
        (risk_config or EventWebSocketRiskConfig()).order_submit_mode,
        (risk_config or EventWebSocketRiskConfig()).rest_reconcile_seconds,
        (risk_config or EventWebSocketRiskConfig()).untracked_position_grace_seconds,
    )
    with exclusive_file_lock(root / ".locks" / "event_ws_risk_cycle.lock", stale_seconds=0, poll_seconds=0.05):
        engine = EventWebSocketRiskEngine(
            root,
            config=config,
            risk_config=risk_config,
            private_client=private_client,
            private_stream=private_stream,
            public_stream=public_stream,
            trade_client=trade_client,
        )
        try:
            return engine.run()
        finally:
            engine.close()


def _build_private_stream(config: ResearchConfig) -> BybitPrivateWebSocketStream:
    api_key, api_secret, demo = resolve_private_credentials()
    return BybitPrivateWebSocketStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
        api_key=api_key,
        api_secret=api_secret,
    )


def _build_ws_trade_client(config: ResearchConfig) -> Any:
    api_key, api_secret, demo = resolve_private_credentials()
    # Jittered retry de-syncs the multi-daemon demo connect storm and rides
    # through transient rejects so WS exits actually establish (lowest-latency
    # stop submission); permanent errors (no pybit / no creds) raise fast.
    return build_ws_trade_client(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
        api_key=api_key,
        api_secret=api_secret,
    )


def _persist_ws_risk_history(payload: dict[str, Any]) -> bool:
    reason = str(payload.get("cycle", {}).get("reason") or "")
    return reason != "heartbeat" or bool(_telegram_notification_reason(payload))


def _ensure_sleeve_column(df: pl.DataFrame, default: str) -> pl.DataFrame:
    """Ensure the DataFrame has a `sleeve` column populated with `default`
    for rows that don't already specify one. Used by _read_*_combined so
    legacy short-side rows (written before the sleeve column existed) and
    new long-side rows can be concatenated and routed correctly on write-back.
    """
    if df.is_empty():
        return df
    if "sleeve" not in df.columns:
        return df.with_columns(pl.lit(default).alias("sleeve"))
    return df.with_columns(pl.col("sleeve").fill_null(default))


def _validate_ws_risk_config(config: EventWebSocketRiskConfig) -> None:
    from .bybit import validate_order_submit_allowed

    validate_order_submit_allowed(
        submit_orders=config.submit_orders,
        confirm_demo_orders=config.confirm_demo_orders,
    )
    if config.order_submit_mode not in {"ws", "ws_then_rest", "rest"}:
        raise ValueError("order_submit_mode must be ws, ws_then_rest, or rest")
    if config.order_submit_mode == "ws" and config.rest_fallback:
        raise ValueError("pure ws order mode must set rest_fallback=False")
    if config.rest_reconcile_seconds < 0.0 or config.heartbeat_seconds < 0.0:
        raise ValueError("heartbeat and reconcile intervals must be non-negative")
    if config.max_runtime_seconds < 0.0:
        raise ValueError("max_runtime_seconds must be non-negative")
    if config.stream_start_timeout_seconds < 0.0:
        raise ValueError("stream_start_timeout_seconds must be non-negative")
    if config.pending_exit_guard_seconds < 0.0:
        raise ValueError("pending_exit_guard_seconds must be non-negative")
    if config.exit_untracked_positions and config.order_submit_mode == "ws" and not config.rest_fallback:
        raise ValueError("exit_untracked_positions requires REST fallback in Bybit demo mode")
    if config.exit_untracked_positions and not config.long_data_root:
        # exit_untracked_positions flattens any Bybit position not found in this
        # engine's ledger(s). With long_data_root set the engine reads BOTH the
        # short and long ledgers, so the long sleeve's positions are recognised.
        # Without it, on a SHARED account the long sleeve's open positions look
        # untracked and would be force-closed. Warn rather than raise: a dedicated
        # single-sleeve account is a legitimate (if rare) setup, and the launch
        # script hard-fails this combination for the shared demo account.
        _logger.warning(
            "exit_untracked_positions=ON with long_data_root unset: this engine will "
            "FLATTEN any Bybit position absent from the short ledger. If another sleeve "
            "shares this account its positions WILL be closed. Set long_data_root or "
            "disable exit_untracked_positions unless this account is single-sleeve."
        )
    if config.untracked_position_grace_seconds < 0.0:
        raise ValueError("untracked_position_grace_seconds must be non-negative")
    if config.adopt_stop_loss_pct < 0.0 or config.adopt_take_profit_pct < 0.0:
        raise ValueError("adopt stop-loss and take-profit percentages must be non-negative")
    if config.adopt_hold_days < 0.0:
        raise ValueError("adopt_hold_days must be non-negative")


_DEMO_WS_TRADE_UNAVAILABLE = (
    "Bybit demo WebSocket Trade order entry is unavailable; using REST fallback for demo reduce-only exits."
)
TELEGRAM_DEDUPE_RETENTION_SECONDS = 24 * 60 * 60


def _message_rows(message: dict[str, Any]) -> list[dict[str, Any]]:
    data = message.get("data", message)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _ack_order_link(message: dict[str, Any]) -> str:
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    return str(
        message.get("_lm_order_link_id")
        or message.get("orderLinkId")
        or message.get("order_link_id")
        or data.get("orderLinkId")
        or data.get("order_link_id")
        or ""
    )


def _position_price(row: dict[str, Any]) -> float:
    return _first_price(row, ("markPrice", "mark_price", "lastPrice", "indexPrice", "avgPrice"))


def _first_price(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value > 0.0:
            return value
    return 0.0


def _validate_trade_row_invariants(row: dict[str, Any]) -> tuple[bool, str]:
    """Cheap defensive check before writing a trade row to the ledger.

    Catches the 2026-05-25 class of bug where entry_ts_ms collapsed onto
    signal_ts_ms (1-6h before the actual venue fill), which made
    planned_exit_ts_ms + event_decay trip prematurely. The cycle's exit
    logic uses entry_ts as the basis for hold-window math; any divergence
    between entry_ts and the actual fill time silently corrupts every
    exit decision.

    See docs/timestamp_glossary.md for the full reasoning. Returns
    ``(ok, reason)`` — callers should log + skip the row on a failed
    invariant rather than write it.
    """
    signal_ts = int(row.get("signal_ts_ms") or 0)
    entry_ts = int(row.get("entry_ts_ms") or 0)
    opened_at = int(row.get("opened_at_ms") or 0)
    planned_exit = int(row.get("planned_exit_ts_ms") or 0)
    if signal_ts > 0 and entry_ts > 0 and entry_ts < signal_ts:
        return False, f"entry_ts_ms ({entry_ts}) < signal_ts_ms ({signal_ts})"
    if planned_exit > 0 and entry_ts > 0 and planned_exit <= entry_ts:
        return False, f"planned_exit_ts_ms ({planned_exit}) must exceed entry_ts_ms ({entry_ts})"
    if signal_ts > 0 and opened_at > 0 and opened_at < signal_ts:
        return False, f"opened_at_ms ({opened_at}) < signal_ts_ms ({signal_ts})"
    return True, ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _telegram_dedupe_key(reason: str, payload: dict[str, Any]) -> str:
    cycle = payload.get("cycle", {})
    order_links = sorted(
        str(row.get("order_link_id") or "")
        for row in payload.get("exit_orders", [])
        if str(row.get("order_link_id") or "")
    ) + sorted(
        str(row.get("entry_order_link_id") or row.get("exit_order_link_id") or row.get("order_link_id") or "")
        for row in payload.get("pending_fill_reconciliations", [])
        if str(row.get("entry_order_link_id") or row.get("exit_order_link_id") or row.get("order_link_id") or "")
    )
    symbols = sorted(
        str(row.get("symbol") or "")
        for row in payload.get("untracked_positions", []) + payload.get("bybit_positions", [])
        if str(row.get("symbol") or "")
    )
    repairs = sorted(
        "|".join(
            [
                str(row.get("symbol") or ""),
                f"{_float(row.get('stop_price')):.12g}",
                f"{_float(row.get('take_profit_price')):.12g}",
                str(row.get("status") or ""),
                str(row.get("submit_mode") or ""),
                str(row.get("error") or "")[:160],
            ]
        )
        for row in payload.get("stop_repairs", [])
        if str(row.get("symbol") or "")
    )
    error = str(cycle.get("position_report_error") or "")[:160]
    return "|".join(
        [
            reason,
            ",".join(order_links[-8:]),
            ",".join(repairs[-8:]),
            ",".join(symbols),
            error,
        ]
    )


def _telegram_dedupe_path(report_dir: Path) -> Path:
    return report_dir / "telegram_dedupe_keys.json"


def _read_telegram_dedupe_keys(report_dir: Path, *, now: float | None = None) -> set[str]:
    current = time.time() if now is None else now
    payload = _read_telegram_dedupe_key_payload(report_dir)
    return {
        key
        for key, sent_at in payload.items()
        if current - sent_at <= TELEGRAM_DEDUPE_RETENTION_SECONDS
    }


def _write_telegram_dedupe_keys(report_dir: Path, keys: set[str], *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    existing = _read_telegram_dedupe_key_payload(report_dir)
    output = {
        key: float(existing.get(key, current))
        for key in sorted(keys)
        if current - float(existing.get(key, current)) <= TELEGRAM_DEDUPE_RETENTION_SECONDS
    }
    path = _telegram_dedupe_path(report_dir)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_telegram_dedupe_key_payload(report_dir: Path) -> dict[str, float]:
    path = _telegram_dedupe_path(report_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, list):
        timestamp = time.time()
        return {str(item): timestamp for item in payload if item}
    if not isinstance(payload, dict):
        return {}
    output: dict[str, float] = {}
    for key, value in payload.items():
        try:
            output[str(key)] = float(value)
        except (TypeError, ValueError):
            output[str(key)] = time.time()
    return output


def _call_with_timeout(label: str, func: Any, *, timeout_seconds: float) -> tuple[Any, str]:
    timeout = max(float(timeout_seconds), 0.0)
    if timeout <= 0.0:
        try:
            return func(), ""
        except Exception as exc:  # noqa: BLE001 - caller surfaces third-party transport failures
            return None, f"{label} failed: {exc}"[:500]
    result_queue: queue.Queue[tuple[Any, str]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put((func(), ""))
        except Exception as exc:  # noqa: BLE001 - caller surfaces third-party transport failures
            result_queue.put((None, f"{label} failed: {exc}"[:500]))

    thread = threading.Thread(target=worker, name=f"lm-{_thread_name(label)}", daemon=True)
    thread.start()
    try:
        return result_queue.get(timeout=timeout)
    except queue.Empty:
        return None, f"{label} timed out after {timeout:.2f}s; REST reconciliation remains active"


def _thread_name(label: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in label.lower())[:48]


def _now_ms() -> int:
    return int(time.time() * 1000)
