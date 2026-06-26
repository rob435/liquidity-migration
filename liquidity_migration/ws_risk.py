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

from ._common import MS_PER_DAY, coerce_int
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
    _prune_cycle_reports,
    _quantity_text,
    _ratio_or_zero,
    _risk_order_link_id,
    _risk_reconcile_missing_positions,
    _reconcile_pending_order_fills,
    _split_order_link_id,
    _split_qty_for_max_order_size,
    _live_open_order_symbols,
    _safe_open_orders,
    _safe_raw_positions,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    _terminalize_stale_pending_entry_orders,
    _telegram_notification_reason,
    _trade_return,
    _upsert_rows,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    decode_entry_order_link_id,
    format_event_risk_cycle_report,
    format_telegram_status_message,
    plan_risk_exits,
    plan_stop_repairs,
    summarize_position_pnl,
)
from .long_native_event_demo import LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
from .storage import exclusive_file_lock, read_dataset, read_ledger_window, write_dataset
from . import cross_sleeve as _cross_sleeve
from .event_demo import wallet_equity_usdt
from .telegram import send_telegram_message, telegram_configured


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
    # docs/research_summary.md.
    reconcile_prefetch_enabled: bool = False
    # 15s was too tight on a quiet demo account: Bybit's private WS only
    # pushes when state changes (orders, fills, balance moves). The ticker
    # WS keeps last_ws_event_monotonic fresh under normal load but during
    # deploy churn / pybit reconnects the gap can briefly exceed 15s,
    # producing a false-positive "position_report_error: websocket stale"
    # telegram. 60s is short enough to catch a real WS death (the WS
    # backbone reconnects in <10s) but tolerates ordinary brief silences.
    stale_ws_seconds: float = 60.0
    # Socket-level private-WS force-reconnect bound. The private stream feeds the
    # real-time position/order/execution events that drive intrabar stops; if its socket
    # dies and pybit's own auto-reconnect fails, ws_risk silently degrades to REST-only
    # reconcile. We rebuild off pybit's is_connected() (true liveness, NOT data-silence,
    # which on a quiet account is ambiguous) only after the socket is continuously DOWN
    # past this bound (giving pybit's own reconnect first dibs), with the same value as a
    # cooldown so a persistent failure can't storm Bybit's auth connection limit. 0 = off.
    private_ws_reconnect_seconds: float = 180.0
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
    # adopt_short_strategy_id: legacy-ledger support only — the daily-short
    # sleeve was erased 2026-06-11; set explicitly to adopt old short rows.
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
    # Continuous-fade sleeve (3rd sleeve, also SHORT-direction). Same dual-side rationale: when
    # continuous_data_root is set this engine ALSO reads + routes that ledger so continuous positions
    # are recognised (not flattened as untracked, not mis-routed to the short ledger) on the shared
    # demo account. Set to "" to ignore the continuous sleeve.
    continuous_data_root: str = ""
    continuous_trades_dataset: str = "continuous_fade_demo_trades"
    continuous_orders_dataset: str = "continuous_fade_demo_orders"
    adopt_continuous_strategy_id: str = ""
    # Continuous add-on sleeve (sparse fresh_pop25 overlay). Uses the same
    # registered continuous dataset names in a separate root, but a distinct
    # sleeve tag / orderLinkId namespace (`lm-en-ca-*`) so fills and adoptions
    # cannot collide with the primary continuous sleeve.
    continuous_addon_data_root: str = ""
    continuous_addon_trades_dataset: str = "continuous_fade_demo_trades"
    continuous_addon_orders_dataset: str = "continuous_fade_demo_orders"
    adopt_continuous_addon_strategy_id: str = ""
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
    # ws-risk-5: a clock bumped ONLY by private-stream events (position / order /
    # execution) -- the ones that drive the prompt stop/close path. last_ws_event_
    # monotonic above is bumped by EVERY event including public 'ticker' traffic, so
    # it can't tell a dead private stream from a quiet one while tickers flow. The
    # stale-WS watchdog keys off THIS clock so private-stream silence forces a REST
    # reconcile even when the public ticker stream is healthy.
    last_private_ws_event_monotonic: float = field(default_factory=time.monotonic)
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
    # Cumulative count of CLOSED-trade order links pruned from the per-link maps
    # (orders / orders_by_link / executions_by_link / submitted_link_*). Those
    # maps grow one entry per order for the daemon's lifetime; left unbounded
    # they OOM-kill the long-lived risk daemon (orphaning a position) just like
    # the telemetry logs. _prune_closed_order_state evicts only links whose trade
    # is already closed (an OPEN trade's links are always retained for in-flight
    # reconciliation), beyond a large retention grace.
    orders_evicted: int = 0
    # Last error string from the most recent ``_safe_raw_positions`` call (or
    # empty when the snapshot was clean). Plumbed into the orphan reconciler
    # so a transient REST failure -- which leaves ``positions_by_symbol``
    # empty -- does not false-positive orphan-close every open trade.
    last_position_error: str = ""
    ws_order_unavailable: str = ""
    # Non-empty when a configured sibling-sleeve ledger READ raised this
    # reconcile pass (a torn/corrupt parquet or I/O error -- NOT a merely
    # empty/missing ledger, which stays fail-open). The position reconcilers
    # (exit_untracked_positions / adopt_untracked_positions) fail CLOSED while
    # set: a sleeve whose ledger we could not read drops out of open_trades, and
    # flattening/adopting positions we can no longer see would corrupt that
    # sibling sleeve on the netted account. Reset to "" at the top of
    # bootstrap/rest_reconcile; set by _note_ledger_read_error. Mirrors
    # last_position_error for the REST positions snapshot.
    ledger_read_error: str = ""
    # Cumulative count of cross-sleeve mis-attributions: rows whose `sleeve` tag
    # was non-empty but unowned (routed to short as a fallback) plus
    # un-recoverable short-side orphans adopted while a continuous sleeve is
    # configured (short vs continuous is ambiguous by venue side alone).
    # Surfaced in write_report so a recurring mis-attribution is visible.
    sleeve_misroutes: int = 0
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
        self.continuous_root: Path | None = (
            Path(self.risk.continuous_data_root).expanduser() if self.risk.continuous_data_root else None
        )
        if self.continuous_root is not None:
            self.continuous_root.mkdir(parents=True, exist_ok=True)
        self.continuous_addon_root: Path | None = (
            Path(self.risk.continuous_addon_data_root).expanduser()
            if self.risk.continuous_addon_data_root else None
        )
        if self.continuous_addon_root is not None:
            self.continuous_addon_root.mkdir(parents=True, exist_ok=True)
        self.private_client = private_client
        self.private_stream = private_stream
        self.public_stream = public_stream
        self.trade_client = trade_client
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.state = WebSocketRiskState()
        self.report_dir = self.root / "reports" / self.risk.data_name
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.state.telegram_keys_sent = set(_read_telegram_dedupe_keys(self.report_dir))
        # Last wallet equity observed by _refresh_cross_sleeve_account_state (one
        # REST read per reconcile pass) — reused by the cycle row/telegram so the
        # operator-facing message stops hardcoding equity=$0.00 (audit r3) without
        # adding a wallet call to the 10s heartbeat path.
        self._last_equity_usdt: float = 0.0
        # High-water marks for the notify REASON filter (round 4): the payload's
        # exits/stop_repairs/reconciliations slices are cumulative since-start
        # state, so after the first reconciliation every later alert computed
        # reason="position_reconciled" — a fresh stop_repair_failed or
        # untracked_position paged under the wrong header. The reason filter now
        # sees only rows added since the previous report (the message BODY and
        # persisted report keep the cumulative slices).
        self._reason_high_water: dict[str, int] = {}
        # cross-sleeve-2: trade_ids that were OPEN at the previous control-row
        # refresh, so the next pass can diff prior-vs-current open_trades and pass
        # the open->closed set as closed_trade_ids to write_account_state. That
        # frees a closed trade's symbol reservation immediately (faster symbol
        # turnover for a sibling sleeve once long/addon are live) instead of
        # waiting out the 180s TTL. The owned-side GC matches on reservation
        # trade_id; a trade reserved under a candidate trade_id that never matches
        # here just falls back to the existing TTL/release path, so wiring this can
        # only free reservations sooner, never strand a live one.
        self._cross_sleeve_open_trade_ids: set[str] = set()
        # Captured by run(): the one thread allowed to mutate self.state.
        self._consumer_thread_ident: int | None = None
        # Telegram notifications are sent on a background daemon thread so the
        # consumer thread never blocks on the HTTP round-trip — a slow Telegram
        # RTT would otherwise stall stop-enforcement event processing during a
        # cascade. Lazily started on first enqueue; drained + stopped in close().
        # Downside is bounded: a dropped/late notification, never an order or
        # state error (the dedupe + state mutation stay on the consumer thread).
        # Pre-rendered message STRINGS (not the live payload dict), so the background
        # sender never reads structures the consumer concurrently mutates (WS-R-001).
        self._telegram_queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._telegram_thread: threading.Thread | None = None
        # audit2c: the sender thread MUST NOT mutate self.state.telegram_keys_sent or
        # write the dedupe file (consumer-thread-only invariant + a file-write race
        # against maybe_notify). On a failed send it hands the key back here; the
        # consumer un-records it (drain in maybe_notify) so the alert can re-fire.
        self._telegram_failed_keys: queue.Queue[str] = queue.Queue()
        # Background reconcile-prefetcher (opt-in via reconcile_prefetch_enabled).
        # Holds the latest positions + open-orders REST snapshot so rest_reconcile
        # reads it non-blocking. Written by the prefetcher via atomic reference
        # swap; read by the consumer. Default off -> these stay None/idle.
        self._reconcile_prefetch: dict[str, Any] | None = None
        self._reconcile_prefetch_thread: threading.Thread | None = None
        self._reconcile_prefetch_stop = threading.Event()
        # Socket-level private-WS reconnect tracking (see _maybe_reconnect_private_stream).
        self._private_disconnected_since: float | None = None
        self._last_private_reconnect_monotonic = 0.0
        self._private_ws_reconnects = 0
        # ws-risk-1: a rebuild that fails mid-flight (after the old stream was
        # closed) leaves private_stream=None; this flag tells the next on_idle
        # pass that a replacement is still OWED so the None never latches the
        # daemon into REST-only mode permanently (start_streams runs only once).
        self._private_rebuild_pending = False
        # Public ticker stream health (see _maybe_reconnect_public_stream, round 4):
        # the public socket feeds price_by_symbol for the intrabar stop checks and
        # had NO reconnect treatment — pybit's permanent give-up froze ticker
        # prices for the daemon's lifetime (REST reconcile + venue stops bounded
        # the damage, but tick-latency stop enforcement silently degraded).
        self._last_ticker_event_monotonic = 0.0
        self._public_stream_built_monotonic = time.monotonic()
        self._last_public_reconnect_monotonic = 0.0
        self._public_ws_reconnects = 0
        # ws-risk-2: as for the private stream, a rebuild that fails after the old
        # socket was closed leaves public_stream=None AND subscribed_symbols cleared,
        # which would latch the daemon into REST-only ticker prices forever. This
        # flag + the saved symbol set let the next on_idle pass retry the build.
        self._public_rebuild_pending = False
        self._public_resubscribe: set[str] = set()

    def _private_stream_connected(self) -> bool | None:
        """Probe the private WS socket's TRUE liveness via pybit's ``is_connected()``
        (reads ``ws.sock.connected``). Returns True (socket up), False (socket down),
        or None when liveness is unknowable -- no stream, or an older pybit without
        the probe -- in which case callers must stay conservative and fall back to
        data-silence heuristics. Best-effort: a probe that raises reads as unknown."""
        stream = self.private_stream
        if stream is None:
            return None
        probe = getattr(stream, "is_connected", None)
        if not callable(probe):
            return None
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a flaky liveness probe must not break reconcile
            return None

    def _maybe_reconnect_private_stream(self, now: float) -> None:
        """Rebuild the private WS stream when its SOCKET is genuinely down (not merely
        a quiet account). The private stream feeds the position/order/execution events
        that drive ws_risk's intrabar stops; if its socket dies and pybit's own
        auto-reconnect fails, ws_risk silently degrades to REST-only reconcile. pybit's
        is_connected() reads ws.sock.connected (TRUE liveness), so we rebuild only after
        a continuously-down socket past private_ws_reconnect_seconds (pybit's reconnect
        goes first) + the same value as a cooldown (auth-limit safety). REST reconcile
        covers the gap, so a failed rebuild never loses protection. Best-effort: any
        failure is recorded and swallowed so the reconcile loop is never broken."""
        stream = self.private_stream
        bound = self.risk.private_ws_reconnect_seconds
        if bound <= 0.0:
            return
        # ws-risk-1: when a prior rebuild failed AFTER closing the old socket,
        # private_stream is None but a replacement is still OWED. Do NOT take the
        # stream-is-None early return in that case -- fall through to retry the
        # build so a single transient failure can't permanently disable the
        # positions/orders/executions feed (start_streams runs only once).
        if stream is None and not self._private_rebuild_pending:
            return
        if stream is not None:
            connected = self._private_stream_connected()
            if connected is not False:
                if connected is True:
                    self._private_disconnected_since = None
                return  # True (healthy) or None (unknown / older pybit) -> stay conservative
        if self._private_disconnected_since is None:
            self._private_disconnected_since = now
        down_for = now - self._private_disconnected_since
        if down_for <= bound or now - self._last_private_reconnect_monotonic <= bound:
            return
        self._last_private_reconnect_monotonic = now
        self._private_ws_reconnects += 1
        _logger.warning(
            "private WS socket down %.0fs > bound %.0fs; rebuilding the risk private stream",
            down_for, bound,
        )
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - close errors must not break reconcile
                pass
        # Build the replacement into a LOCAL and publish self.private_stream only
        # once it is in hand; on any failure leave _private_rebuild_pending set
        # and _private_disconnected_since armed so the next on_idle pass (after the
        # bound elapses again) retries instead of latching None forever.
        self.private_stream = None
        self._private_rebuild_pending = True
        new_stream, error = _call_with_timeout(
            "private websocket stream reconnect",
            lambda: _build_private_stream(self.config),
            timeout_seconds=self.risk.stream_start_timeout_seconds,
        )
        if error:
            self.state.errors.append(error)
            return
        self.private_stream = new_stream
        _, sub_error = _call_with_timeout(
            "private websocket re-subscribe",
            self._subscribe_private_stream,
            timeout_seconds=self.risk.stream_start_timeout_seconds,
        )
        if sub_error:
            # The socket built but the subscription did not land -> the stream is
            # live but feeds nothing; tear it back down and keep retrying so we
            # don't sit on a subscribe-less stream that looks healthy.
            self.state.errors.append(sub_error)
            try:
                new_stream.close()
            except Exception:  # noqa: BLE001 - close errors must not break reconcile
                pass
            self.private_stream = None
            self._private_disconnected_since = now
            return
        # Replacement is fully live and subscribed: clear the owed-rebuild latch.
        self._private_rebuild_pending = False
        self._private_disconnected_since = None

    def _maybe_reconnect_public_stream(self, now: float) -> None:
        """Rebuild the public TICKER stream when it has gone silent while symbols
        are subscribed (round 4). Bybit perp tickers tick near-continuously for a
        subscribed symbol, so a prolonged silence with live subscriptions means a
        dead socket (pybit's documented permanent give-up), not a quiet market.
        Mirrors the private-stream treatment: rebuild past the bound, cooldown
        against thrash, clear subscribed_symbols so re-subscription is real.
        REST reconcile + venue-side stops cover the gap; best-effort throughout."""
        bound = self.risk.private_ws_reconnect_seconds
        if bound <= 0.0:
            return
        # ws-risk-2: when a prior rebuild failed AFTER closing the old socket,
        # public_stream is None and subscribed_symbols was already cleared, so both
        # of the usual guards would make every later pass a no-op. The pending flag
        # tells us a replacement is still OWED so we fall through and retry instead
        # of latching the daemon onto REST-only (30s) ticker prices forever.
        if not self._public_rebuild_pending:
            if self.public_stream is None or not self.state.subscribed_symbols:
                return
        last_alive = max(self._last_ticker_event_monotonic, self._public_stream_built_monotonic)
        silence_bound = max(bound, 60.0)
        if now - last_alive <= silence_bound or now - self._last_public_reconnect_monotonic <= bound:
            return
        self._last_public_reconnect_monotonic = now
        self._public_ws_reconnects += 1
        _logger.warning(
            "public ticker WS silent %.0fs > bound %.0fs with %d subscribed symbols; rebuilding",
            now - last_alive, silence_bound, len(self.state.subscribed_symbols) or len(self._public_resubscribe),
        )
        if self.public_stream is not None:
            try:
                close = getattr(self.public_stream, "close", None)
                if callable(close):
                    close()
            except Exception:  # noqa: BLE001 - close errors must not break reconcile
                pass
        # Remember the symbols to re-subscribe ACROSS a failed attempt so a retry
        # still re-subscribes the right set (subscribed_symbols is cleared so a
        # rebuilt socket actually re-subscribes rather than thinking it already has).
        self._public_resubscribe |= set(self.state.subscribed_symbols)
        resubscribe = set(self._public_resubscribe)
        self.state.subscribed_symbols = set()
        self.public_stream = None
        self._public_rebuild_pending = True
        stream, error = _call_with_timeout(
            "public ticker websocket stream reconnect",
            lambda: BybitPublicTickerStream(
                category=self.config.exchange.category,
                testnet=self.config.exchange.testnet,
                demo=False,
            ),
            timeout_seconds=self.risk.stream_start_timeout_seconds,
        )
        if error:
            self.state.errors.append(error)
            return
        self.public_stream = stream
        self._public_stream_built_monotonic = now
        self._public_rebuild_pending = False
        self._public_resubscribe = set()
        self.subscribe_tickers(resubscribe | set(self.state.positions_by_symbol))

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
    def _sleeve_routes(self, *, trades: bool) -> dict[str, tuple[Path, str]]:
        """Map sleeve -> (root, dataset) for every ledger this engine owns. Short is always present;
        long/continuous only when their data_root is configured. Drives both reads and routed writes
        so a 4th sleeve is one entry, not N call-site edits."""
        routes: dict[str, tuple[Path, str]] = {
            "short": (self.root, "event_demo_trades" if trades else "event_demo_orders"),
        }
        if self.long_root is not None:
            routes["long"] = (self.long_root,
                              self.risk.long_trades_dataset if trades else self.risk.long_orders_dataset)
        if self.continuous_root is not None:
            routes["continuous"] = (self.continuous_root,
                                    self.risk.continuous_trades_dataset if trades else self.risk.continuous_orders_dataset)
        if self.continuous_addon_root is not None:
            routes["continuous_addon"] = (
                self.continuous_addon_root,
                self.risk.continuous_addon_trades_dataset if trades else self.risk.continuous_addon_orders_dataset,
            )
        return routes

    def _note_ledger_read_error(self, sleeve: str, dataset: str, exc: BaseException) -> None:
        """Record a RAISED (not merely empty) owned-ledger read/combine for this
        reconcile pass so the position reconcilers fail CLOSED -- see
        exit_untracked_positions / adopt_untracked_positions. Mirrors
        last_position_error for the REST positions snapshot."""
        self.state.ledger_read_error = f"{sleeve}:{dataset}: {type(exc).__name__}: {exc}"
        _logger.error("ws_risk: ledger read failed for sleeve=%s dataset=%s: %s", sleeve, dataset, exc)

    def _combine_sleeve_frames(self, frames: list[pl.DataFrame], *, trades: bool) -> pl.DataFrame:
        """diagonal_relaxed concat that ISOLATES a schema-incompatible sibling
        ledger so one corrupt sleeve can't abort reconcile for all three. A
        single corrupt ledger (e.g. a scalar column written as a list) would make
        a plain pl.concat raise SchemaError, propagate out of rest_reconcile and
        crash the shared reconcile loop for the short + long + continuous sleeves.
        Fold pairwise so a bad frame is dropped (logged + flagged) while the
        healthy sleeves still reconcile. frames[0] (short, the always-present
        root) seeds the fold and is never dropped; a dropped sibling sets
        ledger_read_error so exit/adopt fail closed (we can no longer see that
        sleeve's positions)."""
        try:
            return pl.concat(frames, how="diagonal_relaxed")
        except pl.exceptions.SchemaError as exc:
            _logger.error("ws_risk: combined %s concat failed (%s); isolating incompatible sleeve(s)",
                          "trades" if trades else "orders", exc)
            combined = frames[0]
            for frame in frames[1:]:
                try:
                    combined = pl.concat([combined, frame], how="diagonal_relaxed")
                except pl.exceptions.SchemaError as inner:
                    sleeve = str(frame["sleeve"][0]) if "sleeve" in frame.columns and frame.height else "?"
                    self._note_ledger_read_error(sleeve, "combined", inner)
            return combined

    # quality-dup-5: reconcile only needs OPEN trades + recently-touched orders.
    # Re-reading the whole-history ledger every ~60s pass scales with the daemon's
    # lifetime; the windowed read touches only the recent month buckets (+ the legacy
    # tail until migration drains it). months_back=6 is ~9x the longest sleeve hold
    # (~21d), so a live open trade is never outside the window; bootstrap() still does
    # the FULL read (months_back=0) so a cold start re-loads every open trade
    # regardless of age. A trade somehow stuck open past 6 months would fall out of the
    # steady-state window and be recovered on the next restart's full bootstrap read —
    # implausible given the hold horizon.
    _RECONCILE_MONTHS_BACK = 6

    def _read_combined(self, *, trades: bool, months_back: int = _RECONCILE_MONTHS_BACK) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for sleeve, (root, dataset) in self._sleeve_routes(trades=trades).items():
            try:
                df = (
                    read_dataset(root, dataset)
                    if months_back <= 0
                    else read_ledger_window(root, dataset, months_back=months_back)
                )
            except Exception as exc:  # noqa: BLE001 - one sleeve read must not abort the others
                # A MISSING/empty ledger is normal (fresh deploy) and stays
                # fail-open: read_dataset returns empty for a registered-but-absent
                # dataset (storage.read_dataset path.exists() guard). A RAISED read
                # (torn/corrupt parquet, I/O error, or an unregistered dataset
                # name) is dangerous -- silently dropping a configured sibling's
                # open trades here would let exit_untracked flatten / adopt
                # mis-route that sleeve's live positions. Flag it so those
                # reconcilers skip this pass; still return the healthy sleeves.
                self._note_ledger_read_error(sleeve, dataset, exc)
                df = pl.DataFrame()
            df = _ensure_sleeve_column(df, sleeve)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            return _ensure_sleeve_column(pl.DataFrame(), "short")
        if len(frames) == 1:
            return frames[0]
        return self._combine_sleeve_frames(frames, trades=trades)

    def _read_trades_combined(self, *, full: bool = False) -> pl.DataFrame:
        return self._read_combined(trades=True, months_back=0 if full else self._RECONCILE_MONTHS_BACK)

    def _read_orders_combined(self, *, full: bool = False) -> pl.DataFrame:
        return self._read_combined(trades=False, months_back=0 if full else self._RECONCILE_MONTHS_BACK)

    def _sleeve_of(self, row: dict[str, Any]) -> str:
        """The row's sleeve, but only if this engine actually owns that ledger; otherwise 'short'
        (the always-present root). An empty/missing tag legitimately defaults to short; a NON-empty
        tag we don't own is a possible mis-attribution (corrupt/misspelled tag, or a sleeve whose
        root is unconfigured) -- surface it (counter + warning) rather than silently mis-filing it
        into the short ledger, which would defeat sleeve independence on the netted account."""
        sleeve = str(row.get("sleeve") or "").lower()
        owned = (
            {"short"}
            | ({"long"} if self.long_root is not None else set())
            | ({"continuous"} if self.continuous_root is not None else set())
            | ({"continuous_addon"} if self.continuous_addon_root is not None else set())
        )
        if sleeve in owned:
            return sleeve
        if sleeve:
            self.state.sleeve_misroutes += 1
            _logger.warning(
                "ws_risk: row sleeve=%r not owned (owned=%s); routing to short -- possible misroute; "
                "trade_id=%s symbol=%s", sleeve, sorted(owned), row.get("trade_id"), row.get("symbol"),
            )
        return "short"

    def _write_rows_routed(self, rows: list[dict[str, Any]], *, trades: bool) -> None:
        if not rows:
            return
        routes = self._sleeve_routes(trades=trades)
        by_sleeve: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_sleeve.setdefault(self._sleeve_of(row), []).append(row)
        for sleeve, sleeve_rows in by_sleeve.items():
            root, dataset = routes[sleeve]
            # All sleeves write uniformly: _sleeve_routes already maps short ->
            # event_demo_trades/_orders, so the short path needs no special-case
            # (its event_demo wrappers were just write_dataset(..., partition_by=())).
            write_dataset(pl.DataFrame(sleeve_rows, infer_schema_length=None), root, dataset, partition_by=())

    def _write_trade_rows_routed(self, rows: list[dict[str, Any]]) -> None:
        self._write_rows_routed(rows, trades=True)

    def _write_order_rows_routed(self, rows: list[dict[str, Any]]) -> None:
        self._write_rows_routed(rows, trades=False)

    # --- cross-sleeve control-row OWNER (long-sleeve-5/6) ------------------
    # ws_risk is the only component that reads all three sleeve roots, so it owns the
    # one shared control row: each reconcile pass it recomputes aggregate IM-used,
    # GCs expired reservations, and rewrites the row (UNDER the dataset lock via
    # cross_sleeve.write_account_state, so a concurrent sleeve reservation claim is
    # never clobbered). The operator-set margin_budget_pct_by_sleeve is preserved, not
    # written, here. Every step is self-swallowing — a control-row fault must NEVER
    # break the reconcile loop (the sleeves just keep reading the last-good row).
    @property
    def account_key(self) -> str:
        return _cross_sleeve.account_key(account_type=self.risk.account_type, settle_coin=self.risk.settle_coin)

    def _sleeve_entry_leverage(self) -> dict[str, float]:
        # The short sleeve's leverage from ws_risk's own demo config; long/continuous
        # trades carry their own initial_margin_usdt / entry_leverage, which
        # compute_im_used prefers, so they need no entry here.
        demo = getattr(self.config, "demo", None)
        lev = float(getattr(demo, "entry_leverage", 0.0) or 0.0) if demo is not None else 0.0
        return {"short": lev} if lev > 0.0 else {}

    def _active_sleeves(self) -> list[str]:
        """Sleeves that are BOTH enabled (kill-switch toggle in sleeves.env, loaded as this
        unit's EnvironmentFile) AND owned by this engine (root configured). This is the
        denominator for the equal-split IM budget = 1/len(active): toggle a sleeve off and the
        remaining sleeves' shares grow on the next reconcile pass. The unset-defaults mirror
        deploy/lib_sleeves.sh EXACTLY (every sleeve fails safe to OFF since audit 2026-06-12
        round 3 — a missing sleeves.env must never resurrect an order-submitting sleeve) so
        this denominator can never drift from the kill-switch; in production the risk unit's
        EnvironmentFile always sets the toggles explicitly, so the default only matters
        off-VPS. The daily-short sleeve was ERASED 2026-06-11 — no toggle exists and it can
        never trade, so it must not claim a budget share (the legacy root stays read-only
        reconciled regardless)."""
        _defaults = {"SHORT_SLEEVE": "off", "LONG_SLEEVE": "off", "CONTINUOUS_SLEEVE": "off"}

        def _on(var: str) -> bool:
            return os.environ.get(var, _defaults[var]).strip().lower() in {"on", "1", "true", "yes"}
        active: list[str] = []
        if _on("SHORT_SLEEVE"):  # legacy escape hatch only — see docstring
            active.append("short")
        if self.long_root is not None and _on("LONG_SLEEVE"):
            active.append("long")
        if self.continuous_root is not None and _on("CONTINUOUS_SLEEVE"):
            active.append("continuous")
        if self.continuous_addon_root is not None and os.environ.get(
            "CONTINUOUS_ADDON_SLEEVE", "off"
        ).strip().lower() in {"on", "1", "true", "yes"}:
            active.append("continuous_addon")
        return active

    def _account_equity_usdt(self) -> float:
        client = self.private_client
        if client is None:
            return 0.0
        try:
            return wallet_equity_usdt(
                client.get_wallet_balance(account_type=self.risk.account_type, coin=self.risk.settle_coin)
            )
        except Exception as exc:  # noqa: BLE001 - wallet read must never break reconcile
            _logger.warning("ws_risk: cross-sleeve equity read failed: %s", exc)
            return 0.0

    def _adoption_equity_usdt(self) -> float:
        """Equity to stamp on an adopted orphan WITHOUT a synchronous wallet REST call
        (ws-risk-8). adopt_untracked_positions runs on the latency-critical consumer
        thread (the sole authority for stop-trigger events); a blocking
        get_wallet_balance per orphan would stall stop processing. bootstrap() and
        rest_reconcile() seed _last_equity_usdt before adopting, so the cache is the
        right source; we never fall back to a live fetch on this hot path."""
        return self._last_equity_usdt

    def _refresh_cross_sleeve_account_state(self) -> None:
        try:
            equity = self._account_equity_usdt()
            if equity > 0.0:
                self._last_equity_usdt = equity
            account_pct, im_by_sleeve = _cross_sleeve.compute_im_used(
                self.state.open_trades, equity_usdt=equity, sleeve_leverage=self._sleeve_entry_leverage()
            )
            # cross-sleeve-2: GC the symbol reservation of any trade that transitioned
            # open->closed since the previous pass. open_trades is the set of currently
            # OPEN/submitted rows (per-pass snapshot); a trade_id present last pass but
            # absent now has closed (or was flattened/adopted away), so its reservation
            # can be freed NOW instead of lingering to the 180s TTL — faster symbol
            # turnover for a sibling sleeve once long/addon are live. The owned-side
            # write_account_state matches closed_trade_ids against reservation trade_id;
            # an unmatched id is a harmless no-op (TTL/release still cover it).
            current_open_trade_ids = set(_column_values(self.state.open_trades, "trade_id"))
            closed_trade_ids = self._cross_sleeve_open_trade_ids - current_open_trade_ids
            # Margin budget is OFF (operator decision 2026-06-03): an EQUAL 1/n split would
            # STARVE the over-subscribed sleeves — long alone wants ~200% IM (10x lev x 10x
            # notional, 20%/position), short ~50%, continuous ~25%; the three combined want
            # ~275% of one netted account, so any <=100% budget throttles someone and an
            # equal third clamps long to ~2 of its 5-10 positions. The building blocks are
            # ready (`equal_split_budget(self._active_sleeves())`) but wiring a budget here
            # is gated on a deliberate, sleeve-WEIGHTED allocation choice. Until then ws_risk
            # writes ONLY IM/equity + GCs reservations; the clamp stays a no-op (budget None).
            _cross_sleeve.write_account_state(
                self.root,
                equity_usdt=equity,
                account_im_used_pct=account_pct,
                im_used_pct_by_sleeve=im_by_sleeve,
                now_ms=_now_ms(),
                closed_trade_ids=closed_trade_ids,
                account_key=self.account_key,
            )
            # Only advance the prior-open baseline AFTER a clean write so a write that
            # raised before completing re-tries the same close-GC next pass (the GC is
            # idempotent — re-dropping an already-freed reservation is a no-op).
            self._cross_sleeve_open_trade_ids = current_open_trade_ids
        except Exception as exc:  # noqa: BLE001 - owner write must never break reconcile
            _logger.error("ws_risk: cross-sleeve state refresh failed (non-fatal): %s", exc)

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
                try:
                    self.on_idle()
                except Exception as exc:  # noqa: BLE001 - isolate one bad idle/reconcile cycle
                    self._handle_consumer_error(exc, where="on_idle")
                continue
            try:
                self.handle_event(event_type, message)
            except Exception as exc:  # noqa: BLE001 - isolate one bad event; never kill the sole authority
                self._handle_consumer_error(exc, where=f"handle_event:{event_type}")

    def _handle_consumer_error(self, exc: BaseException, *, where: str) -> None:
        """Isolate a single bad event/idle cycle so one unexpected raise can't kill the
        SOLE reconcile/exit authority for all three sleeves. A crash here leaves every
        open position on its server-side disaster stop ONLY -- no intrabar TP/max-hold,
        no orphan close, no cross-sleeve control-row refresh -- until systemd restarts,
        and a DETERMINISTIC raise (e.g. a corrupt ledger re-read every bootstrap) would
        exhaust systemd's default start-limit and PARK the unit (permanent outage). So
        log + record + persist a handler_error report and continue. The off-thread
        state-mutation assertion stays a deliberate fail-fast (a programming bug, not a
        data event) and is re-raised; the report write is itself guarded so a failure
        while handling an error cannot re-crash the loop."""
        if isinstance(exc, RuntimeError) and "off the consumer thread" in str(exc):
            raise exc
        message = f"consumer-loop error in {where}: {exc}"
        _logger.exception(message)
        try:
            self.state.errors.append(message[:500])
        except Exception:  # noqa: BLE001
            pass
        try:
            self.write_report(reason="handler_error")
        except Exception:  # noqa: BLE001 - a report failure during error handling must not re-crash the loop
            _logger.exception("ws_risk handler_error report write failed")

    def bootstrap(self) -> None:
        self.private_client = self.private_client or _build_private_client(self.config)
        # Clear last pass's ledger-read fault before re-reading every owned
        # ledger; _read_combined re-sets it if a sibling read raises, gating the
        # untracked flatten/adopt below (fail closed on a degraded read).
        self.state.ledger_read_error = ""
        # Cold start: FULL read (months_back=0) so EVERY open trade is re-loaded
        # regardless of age — a windowed read could miss a long-held open position on
        # restart. The steady-state rest_reconcile loop uses the bounded window.
        self.state.all_trades = self._read_trades_combined(full=True)
        self.state.open_trades = _open_trades(self.state.all_trades)
        orders = self._read_orders_combined(full=True)
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
        self.reconcile_positions(write=True, require_evidence=True)
        self.evaluate_symbols(set(self.state.positions_by_symbol))
        self.repair_exchange_stops()
        # ws-risk-8: seed the equity cache with ONE wallet read BEFORE adoption so
        # _build_adopted_trade reads the cache instead of firing a blocking
        # get_wallet_balance per adopted orphan on the consumer thread. Cold start
        # is the only window where _last_equity_usdt is still 0.0; in steady state
        # the prior reconcile already populated it. Self-swallowing (returns 0.0 on
        # failure, leaving adopted rows with the documented zero-equity fallback).
        seed_equity = self._account_equity_usdt()
        if seed_equity > 0.0:
            self._last_equity_usdt = seed_equity
        self.adopt_untracked_positions()
        self.exit_untracked_positions()
        self.start_streams()
        self._refresh_cross_sleeve_account_state()  # OWNER: seed the control row at cold start (ls-5/6)
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
        # audit2c: anchor the public-WS health "built" timestamp to the ACTUAL stream
        # construction + first subscription here, not to __init__. The watchdog
        # (_maybe_reconnect_public_stream) is a no-op until symbols are subscribed, so
        # an __init__-time anchor only matters once the stream is live — and if the
        # build/subscribe lagged __init__ it could trip a spurious reconnect of a
        # healthy just-subscribed stream. Set it now (>= the __init__ value, so strictly
        # safer); rebuilds refresh it at _maybe_reconnect_public_stream.
        self._public_stream_built_monotonic = time.monotonic()
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

    def _assert_consumer_thread(self) -> None:
        """Fail-fast if a state mutator runs off the consumer thread. The documented
        invariant (WebSocketRiskState: every field mutated ONLY by the consumer thread)
        was enforced only at handle_event; calling this at the top of each public
        mutator too means a stray pybit callback wired directly to a mutator trips the
        assertion instead of silently racing (WS-R-002). The `is not None` gate keeps
        unit tests that call mutators directly (before run() sets the ident) green."""
        if self._consumer_thread_ident is not None and threading.get_ident() != self._consumer_thread_ident:
            raise RuntimeError(
                "WebSocketRiskState mutated off the consumer thread -- WS callbacks "
                "must enqueue onto self.events, never dispatch state changes directly."
            )

    def handle_event(self, event_type: str, message: dict[str, Any]) -> None:
        self._assert_consumer_thread()
        now_mono = time.monotonic()
        self.state.last_ws_event_monotonic = now_mono
        # ws-risk-5: bump the private-only clock for the events that come off the
        # PRIVATE socket (position / order / execution / ws order ack). Public
        # 'ticker' traffic deliberately does NOT touch it, so the stale-WS watchdog
        # can detect a dead private stream even while ticker prices keep flowing.
        if event_type in ("position", "order", "execution", "ws_order_ack"):
            self.state.last_private_ws_event_monotonic = now_mono
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
        self._assert_consumer_thread()
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
        # WS-R-002: every state mutator runs on the consumer thread. on_ticker_message
        # mutates price_by_symbol (the intrabar-stop price map) and was the lone on_*
        # handler missing this guard; restore it so a future miswiring of the public
        # ticker callback fails fast instead of silently racing (audit-iter1 ws-1).
        self._assert_consumer_thread()
        self._last_ticker_event_monotonic = time.monotonic()
        changed_symbols: set[str] = set()
        for row in _message_rows(message):
            symbol = str(row.get("symbol", ""))
            price = _first_price(row, ("markPrice", "lastPrice", "indexPrice"))
            if symbol and price > 0.0:
                self.state.price_by_symbol[symbol] = price
                changed_symbols.add(symbol)
        self.evaluate_symbols(changed_symbols)

    def on_order_message(self, message: dict[str, Any]) -> None:
        self._assert_consumer_thread()
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
                            fee_usdt=_float(row.get("cumExecFee") or row.get("cum_exec_fee")),
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
        self._assert_consumer_thread()
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
        self._assert_consumer_thread()
        for row in _message_rows(message):
            link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
            if not link:
                continue
            agg = self.state.executions_by_link.setdefault(link, {"filled_qty": 0.0, "value": 0.0, "fee": 0.0})
            agg["filled_qty"] += _float(row.get("execQty"))
            agg["value"] += _float(row.get("execValue")) or _float(row.get("execQty")) * _float(row.get("execPrice"))
            # Cumulative venue fee for the link — the WS close path stamps it as
            # exit_fee_usdt; without it a ws-stream close booked fee=0 (round 4).
            agg["fee"] = agg.get("fee", 0.0) + _float(row.get("execFee"))
            filled_qty = agg["filled_qty"]
            value = agg["value"]
            exit_price = value / filled_qty if filled_qty > 0.0 else 0.0
            if link in self.state.submitted_link_to_trade_id:
                self.record_tracked_exit_stream_fill(
                    order_link_id=link,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                    source="execution",
                    fee_usdt=agg.get("fee", 0.0),
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
        fee_usdt: float = 0.0,
    ) -> None:
        trade_id = self.state.submitted_link_to_trade_id.get(order_link_id, "")
        if not trade_id or self.state.all_trades.is_empty():
            return
        # reconcile-core-5: a tracked-exit fill arrives on the latency-critical WS
        # path; look the ONE trade up with a column filter instead of materializing
        # the entire ledger to a Python dict-of-dicts per fill. trade_id is unique,
        # so the filtered first row is the same row the full map would yield.
        match = self.state.all_trades.filter(pl.col("trade_id") == trade_id)
        trade = dict(match.to_dicts()[0]) if not match.is_empty() else {}
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
        current_trade_qty = _float(trade.get("qty"))
        remaining_qty = max(current_trade_qty - delta_qty, 0.0)
        # Close only when the POSITION is gone. The old order-fullness clause
        # (filled_qty >= order_target_qty) closed the WHOLE trade when a reduce
        # order that targets only PART of the position (a rebalance_reduce)
        # fully filled via the WS execution stream — the live remainder was
        # erased from the ledger. Same class as the round-3 pending-fill
        # reconciler fix; this is its WS-path twin (solo sweep 2026-06-12).
        # A plain full exit (target == position) still closes here because the
        # final delta drives remaining_qty to ~0.
        fully_filled = remaining_qty <= max(current_trade_qty * 1e-8, 1e-12)
        if delta_qty <= 0.0 and not fully_filled:
            return
        now_ms = _now_ms()
        exit_price = exit_price if exit_price > 0.0 else _float(order.get("avg_price")) or _float(trade.get("exit_price"))
        exit_reason = str(order.get("exit_reason") or trade.get("exit_reason") or f"{source}_confirmed")
        entry_price = _float(trade.get("entry_price"))
        # ws-risk-6: book the realized PnL of THIS fill's closed chunk so a
        # multi-leg close (a rebalance_reduce or a partially-filling stop) no
        # longer hides its crystallized gain/loss until the residual closes.
        # The closed delta's gross return is weighted by the delta's OWN entry
        # notional (delta_qty * entry_price) / equity — the same gross-of-cost
        # convention as the full-close net_return below. Accumulated into
        # rebalance_realized_return (the field continuous_demo's cycle path
        # already uses for exactly this) so the final close can fold in every
        # earlier leg. Prior realized legs are carried across the upsert.
        delta_gross_return = (
            _trade_return(entry_price, exit_price, side=str(trade.get("side") or "short"))
            if entry_price > 0.0 and exit_price > 0.0
            else 0.0
        )
        delta_weight = _ratio_or_zero(abs(delta_qty * entry_price), trade.get("equity_usdt"))
        prior_realized = _float(trade.get("rebalance_realized_return"))
        prior_realized_weight = _float(trade.get("rebalance_realized_weight"))
        current_fee_usdt = fee_usdt or _float(order.get("fee_usdt"))
        prior_exit_fee_usdt = _float(trade.get("rebalance_exit_fee_usdt")) or _float(trade.get("exit_fee_usdt"))
        if current_fee_usdt > 0.0:
            order["fee_usdt"] = current_fee_usdt
        if fully_filled:
            # A closed trade must carry gross_trade_return and net_return — the
            # ws-stream close is the STEADY-STATE close path under
            # ORDER_SUBMIT_MODE=ws_then_rest, and once status=closed lands the
            # pending-fill reconciler skips the row, so a missing PnL stamp here
            # was never backfilled (round 4). Same formula as the cycle exit /
            # pending-fill close / orphan backfill.
            gross_trade_return = delta_gross_return
            # net_return = the final delta's contribution PLUS every earlier
            # partial-reduce leg's realized return (ws-risk-6). On a plain
            # single-leg close prior_realized is 0.0 and this reduces to the
            # historical gross*weight, so a non-partial close is unchanged.
            final_realized = prior_realized + gross_trade_return * delta_weight
            final_realized_weight = prior_realized_weight + delta_weight
            if final_realized_weight > 0.0:
                gross_trade_return = final_realized / final_realized_weight
            exit_fee_usdt = prior_exit_fee_usdt + current_fee_usdt
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or now_ms,
                    "exit_price": exit_price,
                    "exit_fee_usdt": exit_fee_usdt,
                    "exit_exec_time_ms": now_ms,
                    "gross_trade_return": gross_trade_return,
                    "net_return": final_realized,
                    "rebalance_realized_return": final_realized,
                    "rebalance_realized_weight": final_realized_weight,
                    "rebalance_exit_fee_usdt": exit_fee_usdt,
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
            # ws-risk-6: persist the closed chunk's realized return on the
            # still-open row so it is never lost. NOTE (needs_integration):
            # _recent_adverse_exit_count in continuous_demo only sums status=='closed'
            # rows, so a loss-crystallizing partial reduce still does not increment
            # the entry-pause breaker until the residual fully closes — booking
            # rebalance_realized_return here makes that loss VISIBLE and feeds the
            # eventual closed row's net_return, but wiring the breaker to read open
            # partial rows must be done in continuous_demo (a file this engine does
            # not own).
            realized_so_far = prior_realized + delta_gross_return * delta_weight
            realized_weight_so_far = prior_realized_weight + delta_weight
            exit_fee_so_far = prior_exit_fee_usdt + current_fee_usdt
            trade.update(
                {
                    "status": "open",
                    "qty": _quantity_text(remaining_qty),
                    "notional_usdt": abs(entry_price * remaining_qty),
                    "rebalance_realized_return": realized_so_far,
                    "rebalance_realized_weight": realized_weight_so_far,
                    "rebalance_exit_fee_usdt": exit_fee_so_far,
                    "exit_fee_usdt": exit_fee_so_far,
                    "partial_exit_order_link_id": order_link_id,
                    "partial_exit_order_id": order.get("order_id", ""),
                    "partial_exit_price": exit_price,
                    "partial_exit_reason": exit_reason,
                    "partial_exit_qty": _quantity_text(filled_qty),
                    "partial_exit_gross_return": delta_gross_return,
                    "partial_exit_realized_return": delta_gross_return * delta_weight,
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
            # reconcile-core-2: shared netted account -> cap each sleeve's stop to its
            # own leg so it can't flatten a sibling sleeve's position on the same symbol.
            # ws-risk-4: key off the actual owned-ledger count (short is always present,
            # so >1 means a sibling sleeve is netted in) rather than naming long/continuous
            # explicitly -- the prior predicate omitted continuous_addon, so an addon-only
            # sibling config silently lost cross-sleeve isolation.
            cap_qty_to_trade=len(self._sleeve_routes(trades=True)) > 1,
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
        # reconcile-core-5: one .to_dicts() pass builds BOTH indexes (was two).
        trade_index: dict[str, str] = {}
        symbol_index: dict[str, str] = {}
        if not self.state.all_trades.is_empty():
            for row in self.state.all_trades.to_dicts():
                tid = str(row.get("trade_id") or "")
                sym = str(row.get("symbol") or "")
                sleeve = str(row.get("sleeve") or "")
                if tid:
                    trade_index[tid] = sleeve
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
        # reconcile-core-5: filter for the single target trade rather than
        # materializing every ledger row per exit. Preserve the KeyError-on-missing
        # contract (callers rely on the trade existing) by indexing [0].
        _wanted = str(exit_plan["trade_id"])
        _match = self.state.all_trades.filter(pl.col("trade_id") == _wanted)
        if _match.is_empty():
            raise KeyError(_wanted)
        trade = dict(_match.to_dicts()[0])
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
            # Use the shared 36-char-safe helper (truncates the base, never the
            # unique -s{idx} suffix) like the entry/exit paths — a raw f-string
            # would let two subs collide on a long base (audit 2026-06-02 #43).
            sub_link = base_link if len(sub_qty_strs) == 1 else _split_order_link_id(base_link, idx)
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

            if self.trade_client is None:  # ws_exit only runs when trade_client is set (submit_exit guard); explicit (-O-safe) guard
                raise RuntimeError("ws_exit requires a trade_client (submit_exit guard violated)")
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

    def reconcile_positions(self, *, write: bool, require_evidence: bool = False) -> list[dict[str, Any]]:
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
        # ``require_evidence`` is set by the bulk REST snapshot callers
        # (rest_reconcile / bootstrap): a successful-but-empty or partial snapshot
        # must not zero-PnL-close a trade that ``get_closed_pnl`` can't confirm.
        # The WS ``on_position_message`` path leaves it False (a WS size=0 is the
        # close evidence for that symbol).
        reconciled, rows = _risk_reconcile_missing_positions(
            self.state.open_trades,
            position_by_symbol=self.state.positions_by_symbol,
            now_ms=_now_ms(),
            enabled=self.risk.submit_orders and self.private_client is not None,
            position_error=self.state.last_position_error,
            trading_client=self.private_client,
            require_evidence=require_evidence,
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
        # Clear last pass's ledger-read fault; the combined reads below re-set it
        # if a sibling ledger read raises, so the untracked flatten/adopt fail
        # closed on a degraded read.
        self.state.ledger_read_error = ""
        prefetch = self._reconcile_prefetch if self.risk.reconcile_prefetch_enabled else None
        # Narrowed reference (a fresh snapshot dict or None) rather than a parallel
        # `prefetch_fresh` bool: carrying the value lets the type-checker see the dict is
        # non-None at every use below, so no assert and no index-ignore are needed.
        fresh_prefetch = (
            prefetch
            if prefetch is not None
            and time.monotonic() - float(prefetch["monotonic"]) <= max(self.risk.rest_reconcile_seconds, 1.0)
            else None
        )
        if fresh_prefetch is not None:
            raw_positions, error = fresh_prefetch["positions"], fresh_prefetch["positions_error"]
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
        if fresh_prefetch is not None:
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
            prefetched=(fresh_prefetch["open_orders"], fresh_prefetch["open_orders_error"]) if fresh_prefetch is not None else None
        )
        self.reconcile_pending_order_fills(orders)
        orders = self._read_orders_combined()
        self.load_pending_entry_orders(orders)
        self.load_pending_exit_orders(orders)
        if open_orders_ok:
            self.reconcile_flat_pending_exit_orders(orders)
            orders = self._read_orders_combined()
            self.terminalize_stale_pending_entry_orders(orders)
        self.reconcile_positions(write=True, require_evidence=True)
        self.evaluate_symbols(set(self.state.positions_by_symbol))
        self.repair_exchange_stops()
        self.reconcile_untracked_exit_orders()
        self.adopt_untracked_positions()
        self.exit_untracked_positions()
        # rest_reconcile is also the recovery path when reconcile_stale_websocket
        # fires after a WS silence: this call re-subscribes any tickers that the
        # public stream dropped. Don't move it out of rest_reconcile.
        self.subscribe_tickers(set(self.state.positions_by_symbol) | set(_column_values(self.state.open_trades, "symbol")))
        # OWNER: recompute IM-used + GC reservations + rewrite the ONE control row each
        # pass (long-sleeve-5/6), AFTER stop enforcement. Self-swallowing.
        self._refresh_cross_sleeve_account_state()
        self.state.last_reconcile_monotonic = time.monotonic()

    def adopt_untracked_positions(self) -> None:
        """Adopt exchange positions that have no ledger trade as tracked trades,
        so the normal stop-loss / take-profit / max-hold exit logic manages them.
        Leftover positions after a restart are taken over rather than flattened.
        Runs before exit_untracked_positions so an adopted position is no longer
        seen as untracked."""
        if not self.risk.adopt_untracked_positions:
            return
        if self.state.ledger_read_error:
            # See exit_untracked_positions: a degraded sibling-ledger read means we
            # cannot tell which positions are already tracked, so adopting now risks
            # mis-routing a sibling's position into the wrong ledger. Skip this pass.
            _logger.warning(
                "ws_risk: skipping adopt_untracked_positions -- a ledger read failed this pass (%s)",
                self.state.ledger_read_error,
            )
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
        # Route through _float first (like the sibling recovery path at ~line 2217): a
        # float-formatted venue ms string ("1.7e12", "...0") makes int(str) raise and
        # silently date the adopted trade to now_ms, skewing planned_exit by up to
        # adopt_hold_days. int(_float(...)) parses it; identical for integer strings
        # (audit-iter1 ws-2).
        opened_ms = int(_float(position.get("createdTime") or position.get("created_time"))) or now_ms
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
            link, strategy_id, signal_ts_ms, decoded_sleeve, reentry_seq, component_tag = recovered
            if decoded_sleeve == "continuous_addon":
                from .continuous_hedge_manager import (
                    HEDGE_SYMBOL,
                    HEDGE_SYMBOL_2,
                    ContinuousHedgeConfig,
                    build_hedge_tracking_row,
                )

                # BOTH 2f hedge legs (BTC + ETH) share the hedge link prefix and
                # the externally-managed safety contract (track, NEVER force-exit).
                # The old BTC-only check sent an orphaned ETH leg down the generic
                # recovered path, which stamps adopt stop/TP/3d-hold — ws_risk
                # would force-exit the leg and the next daily hedge run would
                # re-buy it, a silent churn loop (round 4).
                if symbol in (HEDGE_SYMBOL, HEDGE_SYMBOL_2) and side == "long":
                    return build_hedge_tracking_row(
                        ContinuousHedgeConfig(),
                        qty=_float(qty),
                        entry_price=entry_price,
                        opened_ms=opened_ms,
                        updated_ms=now_ms,
                        order_link_id=link,
                        order_id="",
                        signal_ts_ms=signal_ts_ms,
                        submit_mode="adopted_recovered",
                        symbol=symbol,
                    )
            # Rebuild the deterministic trade_id, carrying the continuous re-entry seq so a rebuilt
            # same-signal-window re-entry reconstructs its DISTINCT id (continuous-2). seq=0 (every
            # short/long link + a first continuous entry) reproduces the legacy form verbatim.
            trade_id = f"{strategy_id}-{symbol}-{signal_ts_ms}" + (f"-{reentry_seq}" if reentry_seq > 0 else "")
            # The live continuous trade_id carries the ensemble component ({base}-{component});
            # the deployed ensemble profiles emit ONLY component-tagged links, so a
            # component-less reconstruction matched NO paper-twin row — every post-rebuild
            # adoption broke reconciliation pairing (audit 2026-06-12). The sniper tag "s"
            # maps to the -snipe suffix; the BASE component is not in the link, but the
            # link's -x{crc} suffix lets us recover the exact live trade_id by enumerating
            # the known components (round 3) — falling back to the lossy {base}-snipe form
            # when no/ambiguous match.
            component_fields: dict[str, Any] = {}
            if decoded_sleeve == "continuous" and component_tag:
                if component_tag == "s":
                    from .continuous_demo import recover_snipe_trade_id_from_link

                    recovered_snipe = recover_snipe_trade_id_from_link(
                        link, strategy_id=strategy_id, symbol=symbol, signal_ts_ms=signal_ts_ms,
                    )
                    trade_id = recovered_snipe or (trade_id + "-snipe")
                else:
                    trade_id += f"-{component_tag}"
                    # Stamp the ensemble entry-sizing weight back onto the adopted
                    # row. Without it the daily rebalance defaults the missing
                    # weight to 1.0 and resizes a 0.10-0.40x component entry to
                    # FULL base notional — the round-3 CRITICAL re-entering via
                    # the adoption path (round 4). Unknown/ambiguous tags stay
                    # un-stamped; the rebalance planner now fail-safes by
                    # skipping such rows instead of defaulting.
                    from .continuous_demo import ensemble_component_weight_for_tag

                    component_weight = ensemble_component_weight_for_tag(component_tag)
                    if component_weight is not None:
                        component_fields = {
                            "component": component_tag,
                            "component_weight": component_weight,
                        }
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
                **component_fields,
                # Adopted positions carry zero fee/venue-time on the ledger by
                # default; the demo↔Bybit reconciliation will surface the real
                # fee as a pnl_gap on this trade, which is the correct semantic
                # ("we don't know what we paid; ask the venue"). A future
                # enhancement could query get_trade_history to backfill.
                "entry_fee_usdt": 0.0,
                "entry_exec_time_ms": opened_ms,
                "notional_usdt": abs(entry_price * _float(qty)),
                # Equity snapshot so per-row notional/equity consumers (the armed
                # hedge's book-state resolver) never see an un-stamped row — a
                # zero-equity row flips the WHOLE book to unknown and blocks the
                # hedge (snipe-fill twin, audit 2026-06-12 round 3 / solo sweep).
                # ws-risk-8: cache-only (bootstrap/rest_reconcile seed it first) so
                # adoption on the consumer thread never blocks on a wallet REST.
                "equity_usdt": self._adoption_equity_usdt(),
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
        # Link recovery failed (order-history window exhausted, or a hand-placed
        # position) -> fall back to the side-based sleeve tag. But short and
        # continuous are BOTH short-direction, so the side heuristic cannot tell
        # them apart: a continuous orphan here is tagged 'short' and lands in the
        # short ledger. Surface the ambiguity so the operator can reconcile.
        if side == "short" and self.continuous_root is not None:
            self.state.sleeve_misroutes += 1
            _logger.warning(
                "ws_risk: adopting un-recoverable SHORT-side orphan %s as sleeve='short' "
                "(entry-link recovery failed); short vs continuous is ambiguous on the netted "
                "account -- verify the sleeve manually (qty=%s entry_price=%s)",
                symbol, qty, entry_price,
            )
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
            # See the recovered path: never write an equity-less open row.
            # ws-risk-8: cache-only (seeded before adoption) — no wallet REST here.
            "equity_usdt": self._adoption_equity_usdt(),
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
    ) -> tuple[str, str, int, str, int, str] | None:  # (link, strategy_id, signal_ts_ms, sleeve, reentry_seq, component_tag)
        """Find the original bot-placed entry order for ``symbol`` and decode
        its orderLinkId into (link, strategy_id, signal_ts_ms, sleeve, seq, component_tag).
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
        # Pick the LATEST re-entry, not the first match: a same-signal-window cover-then-re-enter
        # leaves TWO decodable entry links for one symbol in history (seq=0 covered, seq=1 the live
        # position). Prefer the highest (reentry_seq, createdTime) so the rebuild adopts the LIVE id
        # — deterministic regardless of Bybit's get_order_history ordering (re-audit
        # scan-continuous-identity-1). seq=0-only history (the common case) is unaffected.
        best_key: tuple[int, int] | None = None
        best: tuple[str, str, int, str, int, str] | None = None
        for order in history:
            order_side = str(order.get("side") or "")
            if order_side != venue_side:
                continue
            link = str(order.get("orderLinkId") or order.get("order_link_id") or "")
            decoded = decode_entry_order_link_id(link)
            if decoded is None:
                continue
            decoded_sleeve, signal_ts_ms, reentry_seq, component_tag = decoded
            strategy_id = self._adopt_strategy_id_for_sleeve(decoded_sleeve)
            if not strategy_id:
                continue
            created_ts = int(_float(order.get("createdTime") or order.get("updatedTime") or 0))
            key = (reentry_seq, created_ts)
            if best_key is None or key > best_key:
                best_key = key
                best = (link, strategy_id, signal_ts_ms, decoded_sleeve, reentry_seq, component_tag)
        return best

    def _adopt_strategy_id_for_sleeve(self, sleeve: str) -> str:
        """Resolve the strategy_id used to reconstruct a deterministic
        trade_id for a recovered adoption. Falls back to canonical defaults
        when adopt_*_strategy_id was left empty in EventWebSocketRiskConfig."""
        if sleeve == "long":
            return self.risk.adopt_long_strategy_id or LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
        if sleeve == "continuous":
            from .continuous_demo import CONTINUOUS_STRATEGY_ID  # lazy: avoid heavy import at module load
            return self.risk.adopt_continuous_strategy_id or CONTINUOUS_STRATEGY_ID
        if sleeve == "continuous_addon":
            from .continuous_demo import CONTINUOUS_ADDON_STRATEGY_ID
            return self.risk.adopt_continuous_addon_strategy_id or CONTINUOUS_ADDON_STRATEGY_ID
        if sleeve == "short":
            # The daily-short sleeve was erased (operator order 2026-06-11). Legacy
            # ledger rows tagged sleeve="short" can still be adopted, but only with
            # an explicitly configured strategy_id — there is no canonical scenario
            # to derive any more.
            return self.risk.adopt_short_strategy_id or ""
        return ""

    def exit_untracked_positions(self) -> None:
        if not self.risk.exit_untracked_positions:
            return
        if self.state.ledger_read_error:
            # A configured sleeve's ledger read raised this pass -> its open
            # trades are missing from open_trades, so a live position of that
            # sleeve would look "untracked" and get flattened. Fail closed: never
            # flatten a position we merely failed to see. This janitor pauses; it
            # must not pretend venue stops exist for every sleeve/profile.
            _logger.warning(
                "ws_risk: skipping exit_untracked_positions -- a ledger read failed this pass (%s); "
                "refusing to flatten positions we may have failed to see", self.state.ledger_read_error,
            )
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
                        if self.private_client is None:  # explicit (-O-safe) guard; a None here is caught below as a failed submit
                            raise RuntimeError("submit_orders is set but private_client is unavailable")
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
                            if self.private_client is None:  # unreachable (submit above succeeded); explicit (-O-safe) re-narrow
                                raise RuntimeError("private_client became None after a successful submit")
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
            pnl_fields: dict[str, Any] = {}
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
                    # The backfill ALREADY computes the realized-PnL fields; they
                    # were previously extracted for price/time only and the PnL
                    # was discarded — the closed row carried no gross/net return
                    # (round 4).
                    prior_realized = _float(trade.get("rebalance_realized_return"))
                    prior_weight = _float(trade.get("rebalance_realized_weight"))
                    prior_fee = _float(trade.get("rebalance_exit_fee_usdt")) or _float(trade.get("exit_fee_usdt"))
                    backfill_gross = _float(backfill.get("gross_trade_return"))
                    backfill_net = _float(backfill.get("net_return"))
                    backfill_weight = (
                        abs(backfill_net / backfill_gross)
                        if abs(backfill_gross) > 1e-12
                        else _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
                    )
                    final_net = prior_realized + backfill_net
                    final_weight = prior_weight + backfill_weight
                    final_gross = final_net / final_weight if final_weight > 0.0 else backfill_gross
                    final_fee = prior_fee + _float(backfill.get("exit_fee_usdt"))
                    pnl_fields = {
                        "exit_fee_usdt": final_fee,
                        "exit_exec_time_ms": int(backfill.get("exit_exec_time_ms") or 0),
                        "gross_trade_return": final_gross,
                        "net_return": final_net,
                        "rebalance_realized_return": final_net,
                        "rebalance_realized_weight": final_weight,
                        "rebalance_exit_fee_usdt": final_fee,
                    }
            if not pnl_fields and close_exit_price > 0.0:
                # No backfill ran (the recovered order carried a usable
                # avg_price): book PnL from it directly — a closed trade must
                # carry both gross_trade_return and net_return (round 4).
                entry_price = _float(trade.get("entry_price"))
                gross_trade_return = (
                    _trade_return(entry_price, close_exit_price, side=str(trade.get("side") or "short"))
                    if entry_price > 0.0
                    else 0.0
                )
                current_weight = _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
                prior_realized = _float(trade.get("rebalance_realized_return"))
                prior_weight = _float(trade.get("rebalance_realized_weight"))
                prior_fee = _float(trade.get("rebalance_exit_fee_usdt")) or _float(trade.get("exit_fee_usdt"))
                net_return = prior_realized + gross_trade_return * current_weight
                realized_weight = prior_weight + current_weight
                if realized_weight > 0.0:
                    gross_trade_return = net_return / realized_weight
                pnl_fields = {
                    "exit_fee_usdt": prior_fee + _float(order.get("fee_usdt")),
                    "gross_trade_return": gross_trade_return,
                    "net_return": net_return,
                    "rebalance_realized_return": net_return,
                    "rebalance_realized_weight": realized_weight,
                    "rebalance_exit_fee_usdt": prior_fee + _float(order.get("fee_usdt")),
                }
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
                    **pnl_fields,
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
        self._maybe_reconnect_private_stream(now)
        self._maybe_reconnect_public_stream(now)
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
        if now - self.state.last_stale_reconcile_monotonic < self.risk.stale_ws_seconds:
            return
        ws_age = now - self.state.last_ws_event_monotonic
        # ws-risk-5: when we hold positions / open trades, the PRIVATE stream should
        # be delivering position/execution updates. Measure that stream's own silence
        # off last_private_ws_event_monotonic so a dead private socket forces a REST
        # reconcile even while the public ticker stream keeps last_ws_event fresh
        # (the old all-events clock could be kept warm by ticker traffic alone,
        # blinding the watchdog to the most safety-relevant stream's death).
        expects_private = bool(self.state.positions_by_symbol) or not self.state.open_trades.is_empty()
        # ws-risk-6: a private socket that is CONFIRMED live (is_connected() is True)
        # but merely quiet is NOT stale. Bybit only pushes private events on state
        # changes (fills, order/balance moves), so a healthy socket on an idle book
        # legitimately goes silent for hours. Counting that silence as staleness fired
        # a perpetual false "private-stream stale; forced REST reconcile" that surfaced
        # as position_report_error on EVERY heartbeat of a quiet-but-healthy account.
        # Socket liveness is the authority -- the same is_connected() signal
        # _maybe_reconnect_private_stream rebuilds off; fall back to data-silence only
        # when liveness is unknown (None, older pybit) or the socket is down (False),
        # where silence is genuinely meaningful. Mirrors the LONG daemon's
        # _check_ws_health fix (commit 4e224ed).
        if expects_private and self._private_stream_connected() is True:
            expects_private = False
        private_age = (now - self.state.last_private_ws_event_monotonic) if expects_private else 0.0
        stale_age = max(ws_age, private_age)
        if stale_age < self.risk.stale_ws_seconds:
            return
        reason = "private-stream" if private_age >= ws_age and expects_private else "websocket"
        self.state.errors.append(f"{reason} stale for {stale_age:.1f}s; forced REST reconcile")
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
            aged_out = ts_ms > 0 and max_age_ms > 0 and now_ms - ts_ms > max_age_ms
            if aged_out and not trade_id:
                continue
            if trade_id:
                # Keep the link->trade mapping even after the duplicate-submit
                # guard ages out. A delayed private-WS execution for an old
                # reduce-only exit can still arrive while the trade is open; if
                # we drop this mapping, record_tracked_exit_stream_fill treats
                # that fill as unrelated and silently loses the close.
                self.state.submitted_link_to_trade_id[link] = trade_id
            self.state.submitted_link_submit_mode[link] = str(row.get("submit_mode") or "submitted")
            if not aged_out:
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

    def _prune_closed_order_state(self) -> None:
        """Bound the per-order-link maps (the documented orphan-on-OOM failure
        mode, audit 2026-06-02 #14). Evicts ONLY links whose trade is already
        closed — an OPEN trade's entry/exit links are always retained so a
        late fill/cancel/reconcile never finds a missing order — and only the
        oldest beyond a ``retention``-order grace window (so a just-closed
        trade's late reconciliation still resolves). ``orders`` is
        append-ordered, so the tail is the most recent."""
        # ws-risk-7: live_trade_ids is derived from open_trades, which is KNOWN to
        # be incomplete on a pass where a sibling sleeve's ledger read raised
        # (_read_combined drops that sleeve and sets ledger_read_error). Pruning
        # then would judge a still-open sibling order's trade "closed" and evict
        # its order-link state, so a later fill for that link finds no order and is
        # silently dropped. Fail closed: never prune while the open-trade set is
        # degraded (the adopt/flatten reconcilers are already gated the same way).
        # The order maps re-bound on the next clean reconcile/bootstrap.
        if self.state.ledger_read_error:
            return
        retention = getattr(self.risk, "telemetry_log_retention", _LOG_RETENTION)
        orders = self.state.orders
        if len(orders) <= retention:
            return
        live_trade_ids = {tid for tid in _column_values(self.state.open_trades, "trade_id") if tid}
        grace_start = len(orders) - retention  # keep every order from here to the tail
        kept: list[dict[str, Any]] = []
        evicted_links: list[str] = []
        for idx, order in enumerate(orders):
            link = str(order.get("order_link_id") or "")
            trade_id = str(order.get("trade_id") or "")
            trade_open = trade_id != "" and trade_id in live_trade_ids
            # untracked_position exit rows carry trade_id="" by construction, so
            # the can't-prove-closed retention kept them FOREVER (and the
            # exit_untracked_positions attempt-counter scan grows with them —
            # audit 2026-06-12 round 3). A link-bearing TERMINAL untracked exit
            # beyond the grace window is provably done: evict it.
            untracked_terminal = (
                not trade_id
                and link
                and str(order.get("exit_reason") or "") == "untracked_position"
                and str(order.get("status") or "") in ("filled", "cancelled", "rejected")
            )
            if idx >= grace_start or trade_open or (not untracked_terminal and (not link or not trade_id)):
                kept.append(order)
            else:
                evicted_links.append(link)
        if not evicted_links:
            return
        self.state.orders = kept
        for link in evicted_links:
            self.state.orders_by_link.pop(link, None)
            self.state.executions_by_link.pop(link, None)
            self.state.submitted_link_to_trade_id.pop(link, None)
            self.state.submitted_link_submit_mode.pop(link, None)
        self.state.orders_evicted += len(evicted_links)

    def write_report(self, *, reason: str) -> dict[str, Any]:
        self._prune_state_logs()
        self._prune_closed_order_state()
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
            # Last equity seen by the reconcile pass (0.0 until the first wallet
            # read succeeds) — was hardcoded 0.0, so every operator-facing
            # ws_risk telegram said equity=$0.00 (audit 2026-06-12 round 3).
            "equity_usdt": self._last_equity_usdt,
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
            "ledger_read_error": self.state.ledger_read_error,
            "sleeve_misroutes": self.state.sleeve_misroutes,
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
            # Bound the history dir. Unlike the short/long cycles (json-only),
            # ws_risk writes a per-cycle .json AND .md and persists on EVERY
            # non-heartbeat reason (reconcile/exit/adopt/fill) -- thousands/day on an
            # active account -- so without pruning the dir grows unbounded until the
            # disk fills and a LEDGER write fails under the cycle lock (the
            # orphan-open-position mode). Prune BOTH extensions (the json-only default
            # would leak the .md). Amortized hourly via a sentinel; scan cost is nil.
            _prune_cycle_reports(
                self.report_dir,
                prefix="event_ws_risk_cycle_",
                keep_days=7,
                now_ms=now_ms,
                extensions=("json", "md"),
            )
        # Date-partitioned: append-only telemetry, see event_demo.py. partition_by=()
        # made every cycle read + rewrite the whole (unbounded) dataset.
        write_dataset(pl.DataFrame([cycle]), self.root, "event_demo_cycles", partition_by=("date",))
        latest_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        latest_md_path.write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
        self.state.last_report_monotonic = time.monotonic()
        return payload

    def _telegram_sender_loop(self) -> None:
        """Background daemon: drain (dedupe_key, pre-rendered text) pairs and do the
        blocking HTTP send. A None item is the shutdown sentinel. The string is frozen
        on the consumer thread (WS-R-001), so this thread touches no shared mutable
        payload state. On a FAILED send the dedupe key is un-recorded (disk + memory):
        the optimistic dedupe wrote the key before the HTTP round-trip, so a single
        timeout/429 on an UNPROTECTED/stop_repair_failed alert silently suppressed
        that exact alert for 24h (audit 2026-06-12). Un-recording lets the next
        material cycle re-fire it. The disk file is the authority (atomic tempfile
        helpers); a racing consumer re-add at worst restores today's suppress-once
        behavior — never a crash, never a spam loop."""
        while True:
            item = self._telegram_queue.get()
            if item is None:
                return
            key, text = item
            sent = False
            try:
                sent = send_telegram_message(text, enabled=True)
            except Exception as exc:  # noqa: BLE001 - a notification must never crash the daemon
                _logger.warning("background telegram send failed: %s", exc)
            if sent:
                continue
            if not telegram_configured():
                # "Not configured" is not a transport failure: un-recording here
                # made every material cycle re-render + rewrite the dedupe file at
                # heartbeat cadence and buried real send failures in the noise
                # (audit 2026-06-12 round 3). Keep the key; if creds appear later,
                # the next NEW material event notifies normally.
                _logger.warning("telegram not configured (TELEGRAM_* env missing); keeping dedupe key: %s", key)
                continue
            # audit2c: hand the failed key back to the CONSUMER thread to un-record
            # (discard from the set + rewrite the dedupe file) rather than mutating
            # consumer-only state and racing the dedupe file from this sender thread.
            _logger.warning("telegram send failed; handing dedupe key to the consumer to un-record so it can re-fire: %s", key)
            self._telegram_failed_keys.put(key)

    def _enqueue_telegram(self, key: str, text: str) -> None:
        if self._telegram_thread is None or not self._telegram_thread.is_alive():
            self._telegram_thread = threading.Thread(
                target=self._telegram_sender_loop, name="ws-risk-telegram", daemon=True
            )
            self._telegram_thread.start()
        self._telegram_queue.put((key, text))

    def _reason_payload_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Shallow payload clone whose cumulative state slices are reduced to the
        rows added since the previous report, for the notify REASON only (round
        4). Marks advance per report; on the rare un-recorded failed send the
        same rows no longer re-derive the reason (accepted fire-once tradeoff —
        the persisted report and ledger stay authoritative)."""
        view = dict(payload)
        for key, rows, evicted in (
            ("exits", self.state.exits, self.state.exits_evicted),
            ("exit_orders", self.state.orders, self.state.orders_evicted),
            ("stop_repairs", self.state.repairs, self.state.repairs_evicted),
            ("reconciliations", self.state.reconciliations, self.state.reconciliations_evicted),
            (
                "pending_fill_reconciliations",
                self.state.pending_fill_reconciliations,
                self.state.pending_fill_reconciliations_evicted,
            ),
        ):
            total = len(rows) + evicted
            fresh = max(total - self._reason_high_water.get(key, 0), 0)
            self._reason_high_water[key] = total
            view[key] = rows[-fresh:] if fresh > 0 else []
        return view

    def _drain_failed_telegram_keys(self) -> None:
        """Consumer-thread un-record of keys whose background send failed. The sender
        thread hands failed keys back via a thread-safe queue (it must not mutate
        consumer-only state or race the dedupe file); we discard them from the set and
        rewrite the dedupe file HERE, on the consumer thread, so the alert can re-fire."""
        drained = False
        while True:
            try:
                key = self._telegram_failed_keys.get_nowait()
            except queue.Empty:
                break
            self.state.telegram_keys_sent.discard(key)
            drained = True
        if drained:
            try:
                _write_telegram_dedupe_keys(self.report_dir, self.state.telegram_keys_sent)
            except Exception as exc:  # noqa: BLE001 - dedupe repair is best-effort telemetry
                _logger.warning("telegram dedupe un-record (consumer) failed: %s", exc)

    def maybe_notify(self, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.risk.telegram:
            return False, "disabled"
        # audit2c: un-record any keys whose background send failed (handed back by the
        # sender thread) before the dedupe check below, so a failed alert can re-fire.
        self._drain_failed_telegram_keys()
        reason = _telegram_notification_reason(self._reason_payload_view(payload))
        if not reason:
            return False, "quiet_no_material_event"
        key = _telegram_dedupe_key(reason, payload)
        if key in self.state.telegram_keys_sent:
            return False, "duplicate_material_event"
        # Render the message NOW, on the consumer thread, where the payload and its
        # shared order/position row-dicts are stable for this cycle — then enqueue only
        # the frozen string. The background sender thus never reads dicts the consumer
        # mutates in place on the next event (WS-R-001). Guard the render: now that it
        # runs on the consumer/reconcile thread, a formatting fault must be telemetry,
        # never an exception into the reconcile loop (same intent as EVE-2).
        try:
            text = format_telegram_status_message(payload)
        except Exception as exc:  # noqa: BLE001 - a telegram-format fault must not break reconcile
            return False, f"format_failed: {str(exc)[:200]}"
        # Optimistic dedupe + offload the blocking HTTP send: record the dedupe key on
        # the consumer thread and hand the network round-trip to the background sender,
        # returning immediately so a slow Telegram RTT can't stall stop-enforcement. A
        # failed send is logged, not retried — a notification, not an order.
        self.state.telegram_keys_sent.add(key)
        _write_telegram_dedupe_keys(self.report_dir, self.state.telegram_keys_sent)
        # Bound the in-memory dedupe set to the same 24h window the on-disk file keeps
        # (the write above just pruned it), so a long-lived daemon's set can't grow
        # without bound (WSR-5).
        self.state.telegram_keys_sent = set(_read_telegram_dedupe_keys(self.report_dir))
        self._enqueue_telegram(key, text)
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
    if config.exit_untracked_positions and (
        not config.long_data_root
        or not config.continuous_data_root
        or not config.continuous_addon_data_root
    ):
        # exit_untracked_positions flattens any Bybit position not found in this engine's ledger(s).
        # On the SHARED demo account this engine must read EVERY sleeve's ledger (short + long +
        # continuous + continuous add-on) or a sibling sleeve's open positions look
        # untracked and get force-closed. Warn per missing root; the launch script
        # hard-fails the shared-account combination.
        missing = [
            name for name, present in (
                ("long_data_root", config.long_data_root),
                ("continuous_data_root", config.continuous_data_root),
                ("continuous_addon_data_root", config.continuous_addon_data_root),
            )
            if not present
        ]
        _logger.warning(
            "exit_untracked_positions=ON with %s unset: this engine will FLATTEN any Bybit position "
            "absent from the ledgers it reads. If another sleeve shares this account its positions WILL "
            "be closed. Set the missing root(s) or disable exit_untracked_positions on a shared account.",
            " + ".join(missing),
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
        or data.get("orderLinkId")  # type: ignore[union-attr]  # data is a dict (or {}) per the isinstance ternary above
        or data.get("order_link_id")  # type: ignore[union-attr]  # data is a dict (or {}) per the isinstance ternary above
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
    # Thin alias over the shared _common.coerce_int (quality-dup-9); kept as a
    # name so the many module-internal call sites stay untouched.
    return coerce_int(value)


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
