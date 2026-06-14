from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import polars as pl

from .bybit import BybitMarketData, BybitPrivateClient, resolve_private_credentials
from .config import ResearchConfig
from .downloaders import _normalize_instruments, _normalize_tickers
from .storage import exclusive_file_lock, read_dataset, write_dataset
from .telegram import send_telegram_message
from ._common import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, finite_float
from ._common import PENDING_ORDER_STATUSES  # noqa: F401  re-exported: tests + the pending-order guard constant below consume it via event_demo
# orderLinkId encode/decode live in order_link_id.py (cohesive home); re-exported here so
# existing `from .event_demo import _order_link_id` callers (3 sleeves, ws_risk, tests) are unaffected.
from .order_link_id import _base36, _order_link_id, _risk_order_link_id, _split_order_link_id, decode_entry_order_link_id, is_exit_link  # noqa: F401


_logger = logging.getLogger("liquidity_migration.event_demo")

PENDING_ORDER_GUARD_MS = 15 * MS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class EventDemoCycleConfig:
    lookback_days: int = 45
    # universe_rank_end / universe_max_symbols == 0 → match-the-backtest mode:
    # no ticker-turnover pre-filter, every active USDT-perp feeds into daily
    # aggregation, and the strategy's `universe_rank_max` applies on the
    # resulting daily-ranked features. This mirrors the backtest's PIT-manifest
    # behaviour so the same data + config produces the same entries on the
    # same dates. Set a positive value (e.g. 400) to revert to the legacy
    # narrow-universe demo — but the daemon and the backtest will then pick
    # different symbols on the same signal date because the rank denominators
    # differ (see commit 78df65a for the 2026-05-26 DRIFTUSDT divergence
    # reproduction).
    universe_rank_end: int = 0
    universe_max_symbols: int = 0
    universe_min_turnover_24h: float = 0.0
    workers: int = 8
    max_order_notional_pct_equity: float = 0.0
    wallet_balance_fraction: float = 1.0
    fallback_equity_usdt: float = 10_000.0
    max_entry_lag_minutes: int = 360  # 6h. Was 15 — too tight (feature pipeline builds 3-4h after bar close = 218min lag at first availability). 1440 was tried briefly to force a verification entry but degrades alpha — entries 16h late on the backtest's T+1h model trade away the edge. 360 fires entries within ~3-4h of ready_ts (acceptable decay), then skips truly stale signals.
    max_new_entries_per_cycle: int = 5
    entry_leverage: float = 2.0
    entry_order_type: str = "Market"
    exit_order_type: str = "Market"
    order_fill_confirm_seconds: float = 2.0
    order_fill_poll_interval_seconds: float = 0.2
    order_fill_fast_poll_interval_seconds: float = 0.05
    order_fill_fast_poll_seconds: float = 0.5
    max_concurrent_entries: int = 4
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-demo"
    strategy_profile: str = "promoted"
    # long-sleeve-5/-6: shared cross-sleeve control root (owned and written by ws_risk in
    # ITS configured root; the erased daily-short sleeve was the original authority root —
    # legacy roots still resolve). None => auto-resolve from this sleeve's root. NO-OP until
    # ws_risk writes it / until the operator sets a budget split (read-only, fail-open).
    cross_sleeve_account_root: str | None = None
    max_active_symbols: int = 0  # 0 = use the strategy profile's value; >0 overrides it
    # FAIL-CLOSED orphan invariant (default True): a ledger trade whose Bybit
    # position is absent is orphan-closed ONLY when there is POSITIVE evidence
    # of closure (a get_closed_pnl record since entry). Absence alone never
    # closes — a transient/empty positions read must not wipe a live position
    # from the ledger (the C1 false-orphan-close class). Set False to restore
    # the legacy close-on-absence behavior (zero-PnL close when no record).
    orphan_close_require_evidence: bool = True
    # WS-driven kline delivery. The daemon constructs a KlineStreamManager
    # when ws_klines_enabled, bootstraps lookback_days of history, then keeps
    # a hot in-memory store fed by Bybit's kline WS — cycle's
    # _download_recent_1h_klines reads from the store first and only REST-
    # fetches symbols not yet covered. Disable (env WS_KLINES_ENABLED=0) to
    # revert to the legacy REST-on-cycle path.
    ws_klines_enabled: bool = True
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 45
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


@dataclass(frozen=True, slots=True)
class EventRiskCycleConfig:
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-risk"
    repair_stops: bool = True
    exit_order_mode: str = "market"
    limit_chase_attempts: int = 3
    limit_chase_initial_bps: float = 2.0
    limit_chase_step_bps: float = 3.0
    limit_chase_max_bps: float = 15.0
    limit_chase_wait_seconds: float = 0.15
    limit_chase_fallback_market: bool = True
    stop_tolerance_bps: float = 1.0


def _resolve_cycle_universe(
    *,
    public: Any,
    demo: EventDemoCycleConfig,
    config: ResearchConfig,
    root: Path,
    cycle_now_ms: int,
    ticker_cache: Any | None,
    state_cache_stale_seconds: float,
) -> tuple[pl.DataFrame, list[str], pl.DataFrame, str]:
    """Resolve the per-cycle trading universe from fresh instruments x tickers.

    Returns ``(universe, symbols, tickers, ticker_source)``. Rebuilt every cycle
    so new listings enter and delistings leave within the instruments TTL. The
    shrink guard retries ONCE with a forced-fresh fetch when the universe
    collapses below ``_universe_shrink_floor`` — a partial Bybit ticker response
    (the 2026-05-24 signal-blackout incident). Raises if no tradable symbol
    survives the filters. Extracted from ``run_event_demo_cycle`` (the "universe"
    stage) to shrink that function and make the shrink guard independently testable."""
    instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
    raw_tickers, ticker_source = _resolve_ticker_snapshot(
        public, ticker_cache=ticker_cache, state_cache_stale_seconds=state_cache_stale_seconds,
    )
    tickers = _normalize_tickers(raw_tickers)
    universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
    shrink_floor = _universe_shrink_floor(demo)
    if universe.height < shrink_floor:
        _logger.warning(
            "universe shrink detected: got %d symbols (floor %d); busting instruments cache and retrying",
            universe.height, shrink_floor,
        )
        _bust_demo_instruments_cache(root)
        instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
        # Forced-fresh REST in case the cache itself is producing the shrunk view.
        tickers = _normalize_tickers(public.get_tickers())
        universe = _build_demo_universe(instruments, tickers, config=demo, snapshot_ts_ms=cycle_now_ms)
        if universe.height < shrink_floor:
            _logger.error(
                "universe shrink PERSISTS after cache bust: %d symbols (floor %d, instruments=%d, tickers=%d). "
                "Likely a partial Bybit ticker response — strategy cannot fire signals at this universe size.",
                universe.height, shrink_floor, instruments.height, tickers.height,
            )
    symbols = universe["symbol"].to_list() if not universe.is_empty() else []
    if not symbols:
        raise RuntimeError("Bybit demo event cycle found no current tradable symbols after universe filters")
    return universe, symbols, tickers, ticker_source


def _prune_cycle_reports(
    report_dir: Path,
    *,
    prefix: str,
    keep_days: int,
    now_ms: int,
    extensions: tuple[str, ...] = ("json",),
) -> None:
    """Drop per-cycle report files older than ``keep_days`` to keep the report
    directory bounded. The latest_*.json pointer and the partitioned cycle
    ledger preserve full history; per-cycle snapshots are only useful for
    inspecting a recent specific cycle. Best-effort: any unlink error is
    swallowed so a noisy filesystem can't break the cycle.

    ``extensions`` is the set of suffixes (no dot) to prune for this prefix.
    The short + long cycles write only a per-cycle ``.json``; ws_risk also
    writes a paired ``.md`` (event_ws_risk_cycle_*.md), so it passes
    ("json", "md") to avoid leaking the markdown half.

    Amortized: only does the full directory scan when the last prune was
    more than 1 hour ago. With 1500 cycles/day per daemon the directory
    grows by ~1 file/cycle; pruning every cycle = N stat calls every
    60s = wasted I/O. Hourly is plenty (files only need pruning when
    crossing the keep_days boundary, which moves on hour-scale).
    """
    if keep_days <= 0:
        return
    sentinel = report_dir / f".{prefix}prune_sentinel"
    try:
        sentinel_mtime_ms = int(sentinel.stat().st_mtime * 1000)
    except OSError:
        sentinel_mtime_ms = 0
    if sentinel_mtime_ms > 0 and now_ms - sentinel_mtime_ms < 3_600_000:
        return
    cutoff_ts = (now_ms / 1000.0) - keep_days * 86400.0
    try:
        for ext in extensions:
            for path in report_dir.glob(f"{prefix}*.{ext}"):
                try:
                    if path.stat().st_mtime < cutoff_ts:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        # Touch the sentinel so the next call's gate fires off this run.
        sentinel.touch()
    except OSError:
        return


def run_event_risk_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    risk_config: EventRiskCycleConfig | None = None,
    private_client: Any | None = None,
    market_client: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    risk = risk_config or EventRiskCycleConfig()
    _validate_risk_config(risk)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    report_dir = root / "reports" / risk.data_name
    report_dir.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = now_ms if now_ms is not None else _utc_now_ms()
    cycle_id = f"risk-{_yyyymmddhhmmss(cycle_now_ms)}-{int(time.time_ns())}"

    with exclusive_file_lock(root / ".locks" / "event_demo_ledger.lock", stale_seconds=900, poll_seconds=0.001):
        trading_client = private_client if private_client is not None else build_event_risk_private_client(config, risk)
        all_trades = read_dataset(root, "event_demo_trades")
        all_orders = read_dataset(root, "event_demo_orders")
        # Ledger rows are accumulated and flushed once at cycle end -- see the
        # event-demo cycle for the rationale (the cycle reads the ledgers once
        # and then works off the in-memory all_trades/all_orders).
        cycle_trade_rows: list[dict[str, Any]] = []
        cycle_order_rows: list[dict[str, Any]] = []
        open_trades = _open_trades(all_trades)
        raw_positions, position_error = _safe_raw_positions(trading_client, settle_coin=risk.settle_coin)
        if trading_client is None and (risk.telegram or risk.repair_stops):
            position_error = "Bybit private client unavailable; set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET"
        position_by_symbol = _active_position_by_symbol(raw_positions)
        position_snapshot = build_position_pnl_snapshot(raw_positions)
        price_by_symbol = _price_lookup_from_positions(position_by_symbol)
        tick_size_by_symbol = _risk_tick_size_lookup(
            open_trades,
            config=config,
            market_client=market_client,
            enabled=risk.exit_order_mode == "limit_chase",
        )

        reconciled_trades, reconcile_rows = _risk_reconcile_missing_positions(
            open_trades,
            position_by_symbol=position_by_symbol,
            now_ms=cycle_now_ms,
            enabled=risk.submit_orders and trading_client is not None,
            position_error=position_error,
            trading_client=trading_client,
            # Bulk REST snapshot: a successful-but-empty/partial get_positions must
            # not zero-PnL-orphan-close a trade get_closed_pnl can't confirm (C1).
            require_evidence=True,
        )
        if reconcile_rows:
            all_trades = _upsert_rows(all_trades, reconcile_rows, key="trade_id")
            cycle_trade_rows.extend(reconcile_rows)
            open_trades = _open_trades(all_trades)
            reconciled_trades = _open_trades(all_trades)

        # reconcile-core-2: this is the legacy single-account REST `event-risk` path — it owns
        # only ``data_root`` and has NO sibling-sleeve root awareness, so it does NOT pass
        # cap_qty_to_trade. The shared/netted demo account is managed exclusively by the WS risk
        # engine (event-risk-ws / ws_risk), which DOES cap each sleeve's stop to its own leg. Do
        # NOT run this REST path against the shared account; if shared-account support is ever
        # added here, wire cap_qty_to_trade the same way ws_risk does.
        exits = plan_risk_exits(
            reconciled_trades,
            position_by_symbol=position_by_symbol,
            price_by_symbol=price_by_symbol,
            now_ms=cycle_now_ms,
        )
        exit_symbols = {str(row["symbol"]) for row in exits}
        repairs = plan_stop_repairs(
            reconciled_trades,
            position_by_symbol=position_by_symbol,
            skip_symbols=exit_symbols,
            tolerance_bps=risk.stop_tolerance_bps,
        )
        repair_rows = _execute_stop_repairs(
            repairs,
            trading_client=trading_client,
            risk=risk,
            now_ms=cycle_now_ms,
        )
        if repair_rows:
            all_orders = _upsert_rows(all_orders, repair_rows, key="order_link_id")
            if risk.submit_orders or risk.record_dry_run:
                cycle_order_rows.extend(repair_rows)

        # Crash-durability preflight: write the order row to parquet BEFORE
        # place_order. A cycle crash between submission and the end-of-cycle
        # flush still leaves the order_link_id in the ledger for the next
        # wsrisk cycle / event-demo cycle's pending-fill reconciler to adopt.
        if risk.submit_orders:
            def _record_risk_preflight(row: dict[str, Any]) -> None:
                _write_order_rows(root, pl.DataFrame([row], infer_schema_length=None))
            risk_preflight_callback: Callable[[dict[str, Any]], None] | None = _record_risk_preflight
        else:
            risk_preflight_callback = None
        executed_exits, exit_order_rows = _execute_risk_exits(
            exits,
            all_trades,
            trading_client=trading_client,
            risk=risk,
            now_ms=cycle_now_ms,
            price_by_symbol=price_by_symbol,
            tick_size_by_symbol=tick_size_by_symbol,
            record_preflight=risk_preflight_callback,
        )
        if executed_exits:
            all_trades = _upsert_rows(all_trades, executed_exits, key="trade_id")
            if risk.submit_orders or risk.record_dry_run:
                cycle_trade_rows.extend(executed_exits)
        if exit_order_rows:
            all_orders = _upsert_rows(all_orders, exit_order_rows, key="order_link_id")
            if risk.submit_orders or risk.record_dry_run:
                cycle_order_rows.extend(exit_order_rows)

        # Orders before trades — see event-demo cycle for the rationale.
        if cycle_order_rows:
            _write_order_rows(root, pl.DataFrame(cycle_order_rows, infer_schema_length=None))
        if cycle_trade_rows:
            _write_trade_rows(root, pl.DataFrame(cycle_trade_rows, infer_schema_length=None))

        pending_exit_symbols = {
            str(row.get("symbol", ""))
            for row in exit_order_rows
            if str(row.get("submit_mode", "")) in {"dry_run", "submitted"} and str(row.get("symbol", ""))
        }
        open_symbols = set(_column_values(_open_trades(all_trades), "symbol"))
        untracked_positions = [
            row
            for row in position_snapshot
            if str(row.get("symbol", "")) and str(row.get("symbol", "")) not in open_symbols
            and str(row.get("symbol", "")) not in pending_exit_symbols
        ]
        bybit_position_summary = summarize_position_pnl(position_snapshot)
        # P1-3 alignment: prefer position-level markPrice over ticker mark for
        # ledger uPnL so it matches Bybit's own position uPnL.
        ledger_positions = build_ledger_position_pnl_snapshot(
            _open_trades(all_trades),
            price_by_symbol,
            position_by_symbol=position_by_symbol,
        )
        ledger_position_summary = summarize_position_pnl(ledger_positions)
        cycle_row = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "mode": "risk_submit" if risk.submit_orders else "risk_dry_run",
            "symbols": len(open_symbols),
            "kline_rows": 0,
            "feature_rows": 0,
            "latest_feature_ts_ms": 0,
            "entry_candidates": 0,
            "entries_executed": 0,
            "entries_parallel_workers": 1,
            "exit_candidates": len(exits),
            "exits_executed": len(executed_exits),
            "stop_repairs": len(repair_rows),
            "open_trades_before": open_trades.height,
            "open_trades_after": _open_trades(all_trades).height,
            "equity_usdt": 0.0,
            "order_notional_pct_equity": 0.0,
            "order_initial_margin_pct_equity": 0.0,
            "target_gross_exposure": 0.0,
            "target_initial_margin_pct_equity": 0.0,
            "entry_leverage": 0.0,
            "bybit_positions": bybit_position_summary["positions"],
            "bybit_position_value_usdt": bybit_position_summary["position_value_usdt"],
            "bybit_unrealized_pnl_usdt": bybit_position_summary["unrealized_pnl_usdt"],
            "bybit_position_pnl_pct": bybit_position_summary["pnl_pct"],
            "ledger_positions": ledger_position_summary["positions"],
            "ledger_position_value_usdt": ledger_position_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_position_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_position_summary["pnl_pct"],
            "position_report_error": position_error,
            "untracked_positions": len(untracked_positions),
            "telegram_sent": False,
            "telegram_error": "",
        }
        payload = {
            "cycle": cycle_row,
            "risk_config": asdict(risk),
            "exits": executed_exits,
            "exit_orders": exit_order_rows,
            "stop_repairs": repair_rows,
            "reconciliations": reconcile_rows,
            "untracked_positions": untracked_positions,
            "bybit_positions": position_snapshot,
            "bybit_position_summary": bybit_position_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_position_summary,
            "report_dir": str(report_dir),
        }
        # Partition by date: event_demo_cycles is append-only telemetry, never
        # read back inside a cycle. With partition_by=() the whole dataset was
        # read + rewritten every cycle, so the per-cycle write cost grew without
        # bound. Date partitioning caps each write to the current day's rows.
        # The cycle row is persisted with its telegram_sent=False/telegram_error=""
        # defaults; the notify runs OUTSIDE this lock (below) so a slow Telegram
        # RTT can never hold event_demo_ledger.lock against a co-located writer
        # (lock-held-I/O class, mirroring continuous_demo/long_native — audit
        # 2026-06-14, telegram-alert-2). The richer report JSON below carries the
        # post-notify telegram outcome; the cycles dataset keeps its schema.
        write_dataset(pl.DataFrame([cycle_row]), root, "event_demo_cycles", partition_by=("date",))

    # Telegram send is intentionally OUTSIDE the ledger lock: send_telegram_message
    # blocks on a urlopen up to its 10s timeout (plus a 429 retry sleep), and the
    # event_demo_ledger.lock is shared with other event_demo writers — holding it
    # through the round-trip would stall any co-located ledger writer.
    telegram_sent, telegram_error = _maybe_notify(payload, enabled=risk.telegram)
    cycle_row["telegram_sent"] = telegram_sent
    cycle_row["telegram_error"] = telegram_error
    payload["cycle"] = cycle_row
    report_path = report_dir / f"event_risk_cycle_{cycle_id}.json"
    report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (report_dir / "latest_event_risk_cycle.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (report_dir / "latest_event_risk_cycle.md").write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
    _prune_cycle_reports(report_dir, prefix="event_risk_cycle_", keep_days=7, now_ms=cycle_now_ms)
    return payload


def build_event_risk_private_client(config: ResearchConfig, risk: EventRiskCycleConfig) -> BybitPrivateClient | None:
    if risk.submit_orders:
        return _build_private_client(config)
    if _private_credentials_present() and (risk.telegram or risk.repair_stops):
        return _build_private_client(config)
    return None










def wallet_equity_usdt(wallet_payload: dict[str, Any]) -> float:
    rows = wallet_payload.get("list") or []
    if rows:
        first = rows[0]
        total_equity = _float(first.get("totalEquity"))
        if total_equity > 0.0:
            return total_equity
        for coin in first.get("coin") or []:
            if str(coin.get("coin", "")).upper() == "USDT":
                for key in ("equity", "walletBalance", "usdValue"):
                    value = _float(coin.get(key))
                    if value > 0.0:
                        return value
        for key in ("totalWalletBalance", "totalEquity"):
            value = _float(first.get(key))
            if value > 0.0:
                return value
    return 0.0


def build_position_pnl_snapshot(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position in positions:
        symbol = str(position.get("symbol", ""))
        size = _float(position.get("size"))
        if not symbol or size <= 0.0:
            continue
        side = _normalized_position_side(position.get("side"))
        avg_price = _first_float(position, ("avgPrice", "entryPrice", "sessionAvgPrice"))
        mark_price = _first_float(position, ("markPrice", "liqPrice"))
        position_value = _first_float(position, ("positionValue", "positionBalance"))
        if position_value <= 0.0 and mark_price > 0.0:
            position_value = size * mark_price
        unrealized_pnl = _first_float(position, ("unrealisedPnl", "unrealizedPnl"))
        pnl_pct = unrealized_pnl / position_value if position_value > 0.0 else 0.0
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": size,
                "avg_price": avg_price,
                "mark_price": mark_price,
                "position_value_usdt": position_value,
                "unrealized_pnl_usdt": unrealized_pnl,
                "pnl_pct": pnl_pct,
                "leverage": _first_float(position, ("leverage",)),
            }
        )
    return sorted(rows, key=lambda row: abs(float(row["unrealized_pnl_usdt"])), reverse=True)


def build_ledger_position_pnl_snapshot(
    open_trades: pl.DataFrame,
    price_by_symbol: dict[str, float],
    *,
    position_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compute uPnL per open ledger row.

    When ``position_by_symbol`` is provided, the per-symbol ``markPrice`` from
    the venue's position payload is preferred over ``price_by_symbol`` for
    that symbol. Without this, the ledger uPnL is computed from the ticker's
    ``mark_price`` (or ``last_price`` fallback) which can diverge from the
    venue's own position mark — observed live as a ~4% drift on illiquid
    alts like TRUSTUSDT where the WS-cache ticker mark trails the position
    payload mark across a thin orderbook. Aligning to position markPrice
    makes ledger uPnL match Bybit's own position uPnL by construction.
    """
    if open_trades.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for trade in open_trades.to_dicts():
        symbol = str(trade.get("symbol", ""))
        side = str(trade.get("side", ""))
        qty = _float(trade.get("qty"))
        entry_price = _float(trade.get("entry_price"))
        position_mark = 0.0
        if position_by_symbol is not None:
            position = position_by_symbol.get(symbol) or {}
            position_mark = _first_float(position, ("markPrice", "mark_price"))
        mark_price = position_mark if position_mark > 0.0 else price_by_symbol.get(symbol, 0.0)
        if not symbol or qty <= 0.0 or entry_price <= 0.0 or mark_price <= 0.0:
            continue
        if side == "short":
            unrealized_pnl = (entry_price - mark_price) * qty
        else:
            unrealized_pnl = (mark_price - entry_price) * qty
        position_value = mark_price * qty
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "avg_price": entry_price,
                "mark_price": mark_price,
                "position_value_usdt": position_value,
                "unrealized_pnl_usdt": unrealized_pnl,
                "pnl_pct": unrealized_pnl / position_value if position_value > 0.0 else 0.0,
                "leverage": 0.0,
            }
        )
    return sorted(rows, key=lambda row: abs(float(row["unrealized_pnl_usdt"])), reverse=True)


def summarize_position_pnl(rows: list[dict[str, Any]]) -> dict[str, Any]:
    position_value = sum(_float(row.get("position_value_usdt")) for row in rows)
    unrealized_pnl = sum(_float(row.get("unrealized_pnl_usdt")) for row in rows)
    return {
        "positions": len(rows),
        "position_value_usdt": position_value,
        "unrealized_pnl_usdt": unrealized_pnl,
        "pnl_pct": unrealized_pnl / position_value if position_value > 0.0 else 0.0,
    }


def order_quantity_for_notional(
    *,
    notional_usdt: float,
    price: float,
    qty_step: float,
    min_order_qty: float = 0.0,
    min_notional_value: float = 0.0,
    max_order_qty: float = 0.0,
) -> tuple[str, float] | None:
    """Convert a notional target into a Bybit-acceptable qty string.

    ``max_order_qty`` caps the result at Bybit's per-order maximum (use
    ``maxMktOrderQty`` for Market orders, ``maxOrderQty`` for limit). If
    the capped qty falls below ``min_order_qty`` (i.e. the gap between
    min and max would force a sub-min order), returns None so the
    caller skips the candidate rather than sending an order Bybit will
    reject. Observed 2026-05-25: SUPERUSDT entry at 26477 contracts vs
    Bybit's 21100 max → rejected; this cap prevents that.
    """
    if notional_usdt <= 0.0 or price <= 0.0:
        return None
    try:
        raw_qty = Decimal(str(notional_usdt)) / Decimal(str(price))
        step = Decimal(str(qty_step if qty_step > 0.0 else 0.001))
        qty = (raw_qty // step) * step
        min_qty = Decimal(str(min_order_qty if min_order_qty > 0.0 else 0.0))
        max_qty = Decimal(str(max_order_qty if max_order_qty > 0.0 else 0.0))
    except (InvalidOperation, ZeroDivisionError):
        return None
    if max_qty > 0 and qty > max_qty:
        # Floor to the step grid in case max_qty isn't already step-aligned.
        qty = (max_qty // step) * step
    if qty <= 0 or (min_qty > 0 and qty < min_qty):
        return None
    actual_notional = float(qty) * price
    if min_notional_value > 0.0 and actual_notional < min_notional_value:
        return None
    return _decimal_text(qty), actual_notional


# Below this many symbols the cycle treats the universe as anomalously shrunk
# and retries once with a forced-fresh fetch. In match-the-backtest mode
# (universe_max_symbols == universe_rank_end == 0) there is NO requested size,
# so the requested-size guard is inert; this absolute floor restores the
# protection. The live USDT-perp universe is ~560-750; a partial get_tickers()
# near UTC-midnight (the 2026-05-24 incident) collapsed it to ~168 and hid every
# signal for days. 300 sits well below a healthy universe and well above the
# incident size, so it never false-fires on a healthy snapshot.
_MATCH_BACKTEST_UNIVERSE_FLOOR = 300


def _universe_shrink_floor(demo: "EventDemoCycleConfig") -> int:
    """Universe size below which a cycle retries with a forced-fresh fetch.

    Narrow configs keep the historical ``0.75 * requested`` trigger; the
    match-the-backtest (0/0) production config — where the requested-size guard
    was dead code — falls back to the absolute :data:`_MATCH_BACKTEST_UNIVERSE_FLOOR`."""
    requested = demo.universe_max_symbols or demo.universe_rank_end
    return int(requested * 0.75) if requested > 0 else _MATCH_BACKTEST_UNIVERSE_FLOOR


def _validate_risk_config(config: EventRiskCycleConfig) -> None:
    from .bybit import validate_order_submit_allowed

    validate_order_submit_allowed(
        submit_orders=config.submit_orders,
        confirm_demo_orders=config.confirm_demo_orders,
    )
    if config.exit_order_mode not in {"market", "limit_chase"}:
        raise ValueError("exit_order_mode must be market or limit_chase")
    if config.limit_chase_attempts <= 0:
        raise ValueError("limit_chase_attempts must be positive")
    if config.limit_chase_initial_bps < 0.0 or config.limit_chase_step_bps < 0.0:
        raise ValueError("limit chase bps values must be non-negative")
    if config.limit_chase_max_bps < config.limit_chase_initial_bps:
        raise ValueError("limit_chase_max_bps must be >= limit_chase_initial_bps")
    if config.limit_chase_wait_seconds < 0.0:
        raise ValueError("limit_chase_wait_seconds must be non-negative")
    if config.stop_tolerance_bps < 0.0:
        raise ValueError("stop_tolerance_bps must be non-negative")


_DEMO_INSTRUMENTS_CACHE_TTL_MS = 60 * 60 * 1000


















































def _split_qty_for_max_order_size(
    *,
    target_qty: Decimal,
    max_qty_per_order: float,
    qty_step: float,
) -> list[Decimal]:
    """Split target_qty into N sub-quantities, each ≤ ``max_qty_per_order``.

    Bybit caps market-order qty per-order (``maxMktOrderQty``) but allows the
    same position to be built by N sequential orders each under the cap. By
    splitting at the strategy boundary rather than the venue boundary we
    capture the full target notional that the backtest assumed, instead of
    the cap-and-reduce behaviour that silently under-sized live trades vs
    backtest (observed live as REQUSDT entered at 53% of target notional).

    Each sub-qty is floored to ``qty_step``. The last sub absorbs any
    remainder from rounding so the total stays as close to ``target_qty`` as
    the step grid allows. Returns ``[target_qty]`` (no split) when the cap
    does not bind or is unknown (``max_qty_per_order <= 0``).
    """
    if max_qty_per_order <= 0.0:
        return [target_qty]
    cap = Decimal(str(max_qty_per_order))
    if target_qty <= cap:
        return [target_qty]
    step = Decimal(str(qty_step if qty_step > 0.0 else 0.001))
    # Split as evenly as the step grid allows, but distribute the rounding
    # remainder one step at a time across the FIRST few subs rather than dumping
    # all of it on the last one. The old "floor each, last absorbs the slack"
    # form let the last sub exceed the cap on a coarse step (audit 2026-06-02 #5:
    # target=29999 cap=10000 step=100 -> a 10100 final sub re-triggers the very
    # maxMktOrderQty rejection this function prevents). Working in whole step
    # units, every sub differs by at most one step and is provably <= cap.
    cap_units = int(cap // step)            # max whole steps that fit under the cap
    total_units = int(target_qty // step)   # whole steps of target (sub-step remainder dropped, as before)
    if cap_units <= 0 or total_units <= 0:
        # cap smaller than one step increment (pathological): cannot split safely.
        return [target_qty]
    n_subs = -(-total_units // cap_units)    # ceil: minimum orders so an even split fits under the cap
    base, rem = divmod(total_units, n_subs)  # `rem` subs get base+1 units, the rest base; base+1 <= cap_units
    return [step * (base + 1)] * rem + [step * base] * (n_subs - rem)








def _live_open_order_symbols(open_orders: list[dict[str, Any]], *, reduce_only: bool) -> set[str]:
    output: set[str] = set()
    for row in open_orders:
        if not _open_order_active(row):
            continue
        row_reduce_only = _bool(_first_non_empty(row.get("reduceOnly"), row.get("reduce_only")))
        if row_reduce_only != reduce_only:
            continue
        if reduce_only and not _is_own_exit_order(row):
            continue
        symbol = str(row.get("symbol") or "")
        if symbol:
            output.add(symbol)
    return output


def _open_order_active(row: dict[str, Any]) -> bool:
    status = str(row.get("orderStatus") or row.get("order_status") or "").strip().lower()
    if not status:
        return True
    return status not in {"filled", "cancelled", "canceled", "rejected", "deactivated"}


def _is_own_exit_order(row: dict[str, Any]) -> bool:
    link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
    return is_exit_link(link)












def _wallet_equity_usdt(trading_client: Any, *, demo: EventDemoCycleConfig) -> float:
    equity = wallet_equity_usdt(trading_client.get_wallet_balance(account_type=demo.account_type, coin=demo.settle_coin))
    if equity <= 0.0:
        raise RuntimeError("Bybit demo wallet equity could not be read or was zero")
    return equity


def _safe_wallet_equity_usdt(trading_client: Any, *, demo: EventDemoCycleConfig) -> tuple[float, str]:
    try:
        return _wallet_equity_usdt(trading_client, demo=demo), ""
    except Exception as exc:  # noqa: BLE001 - wallet outages must fail entries closed, not kill exits/reports
        return demo.fallback_equity_usdt, f"wallet equity unavailable: {exc}"[:500]


def _safe_raw_positions(trading_client: Any | None, *, settle_coin: str) -> tuple[list[dict[str, Any]], str]:
    if trading_client is None:
        return [], ""
    try:
        return trading_client.get_positions(settle_coin=settle_coin), ""
    except Exception as exc:  # noqa: BLE001 - private API failures should be reported, not hidden
        return [], str(exc)[:500]


def _safe_open_orders(trading_client: Any | None, *, settle_coin: str) -> tuple[list[dict[str, Any]], str]:
    if trading_client is None:
        return [], ""
    get_open_orders = getattr(trading_client, "get_open_orders", None)
    if not callable(get_open_orders):
        return [], ""
    try:
        return get_open_orders(settle_coin=settle_coin), ""
    except Exception as exc:  # noqa: BLE001 - open-order snapshot failures should be reported, not hidden
        return [], str(exc)[:500]


def _refresh_positions_and_orders(
    trading_client: Any | None, *, settle_coin: str
) -> tuple[tuple[list[dict[str, Any]], str], tuple[list[dict[str, Any]], str]]:
    """Concurrently refetch positions and open orders after a cycle placed
    orders. The two are independent read-only endpoints, so this costs one
    roundtrip instead of two; thread-safety is the same as
    _collect_private_snapshots. Returns ((positions, error), (orders, error))."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        positions_future = pool.submit(_safe_raw_positions, trading_client, settle_coin=settle_coin)
        orders_future = pool.submit(_safe_open_orders, trading_client, settle_coin=settle_coin)
        return positions_future.result(), orders_future.result()


def resolve_snapshot_equity(snapshot: dict[str, Any], *, fallback_equity_usdt: float) -> float:
    """Sizing equity from a private snapshot — fallback ONLY when the value is absent.

    A MISSING/None ``equity_usdt`` means the read path supplied nothing (no client,
    malformed cache row) -> use the fallback so paper/dry-run keeps working. A
    PRESENT zero (or negative) equity is REAL information — the wallet read
    SUCCEEDED and the account has no sizable capital; masking it with the fallback
    would size a whole book on phantom equity. (The REST error path never reaches
    here with 0: ``_safe_wallet_equity_usdt`` substitutes the fallback itself and
    sets ``wallet_error``.) Clamped at 0 so sizing degrades to no-entries."""
    raw = snapshot.get("equity_usdt")
    if raw is None:
        return fallback_equity_usdt
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback_equity_usdt
    return max(value, 0.0)


def _resolve_private_snapshot(
    trading_client: Any | None,
    demo: Any,
    *,
    private_state_cache: Any | None,
    state_cache_stale_seconds: float,
) -> tuple[dict[str, Any], str]:
    """Return the cycle's private snapshot, preferring the WS-fed cache.

    Returns ``(snapshot_dict, source)`` where ``source`` is either
    ``"ws_cache"`` (fast path) or ``"rest"`` (fallback). The cache is the
    fast path; if it is missing, not yet seeded, or stale, the REST path
    runs instead so the cycle never operates on stale state.
    """
    if private_state_cache is not None:
        try:
            if private_state_cache.is_seeded() and not private_state_cache.is_stale(
                stale_seconds=state_cache_stale_seconds,
            ):
                return private_state_cache.snapshot(), "ws_cache"
        except Exception as exc:  # noqa: BLE001 - cache must never break the cycle
            _logger.warning("private state cache snapshot failed; REST fallback: %s", exc)
    return _collect_private_snapshots(trading_client, demo), "rest"


def _resolve_ticker_snapshot(
    public: Any,
    *,
    ticker_cache: Any | None,
    state_cache_stale_seconds: float,
) -> tuple[list[dict[str, Any]], str]:
    """Bulk tickers from the WS cache when fresh, otherwise from REST.

    Returns ``(tickers_list, source)``. The returned list matches the shape
    ``BybitMarketData.get_tickers()`` returns — a per-symbol list of raw
    Bybit V5 ticker dicts, ready for ``_normalize_tickers``.
    """
    if ticker_cache is not None:
        try:
            if ticker_cache.is_seeded() and not ticker_cache.is_stale(
                stale_seconds=state_cache_stale_seconds,
            ):
                # ws-dataplane-1: per-symbol staleness filter — drop any symbol whose
                # OWN last update is older than the bound, so a stale per-symbol price
                # can't reach stop/exit pricing while another symbol keeps the global
                # cache 'fresh'. The 60s REST reconcile re-stamps all symbols (< 120s
                # bound), so a healthy-but-quiet symbol is never dropped.
                snap = ticker_cache.snapshot_list(max_age_seconds=state_cache_stale_seconds)
                if snap:
                    return snap, "ws_cache"
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ticker cache snapshot failed; REST fallback: %s", exc)
    return public.get_tickers(), "rest"


def _collect_private_snapshots(trading_client: Any | None, demo: Any) -> dict[str, Any]:
    """Fetch one cycle's three private REST snapshots — wallet equity, open
    orders, positions — concurrently.

    The three are independent endpoints, so the stage costs one roundtrip
    (max) instead of three (sum). BybitPrivateClient._call holds no mutable
    per-call state and pybit's HTTP wraps a requests.Session whose connection
    pool is thread-safe, so concurrent reads on one client are safe. Each call
    is wrapped by a _safe_* helper, so this never raises. run_event_demo_cycle
    runs this on a background thread so it also overlaps the public
    klines/features path; trading_client=None yields the same neutral snapshot
    the serial path produced when no client was present."""

    def _wallet() -> tuple[float, str]:
        if trading_client is None:
            return demo.fallback_equity_usdt, ""
        return _safe_wallet_equity_usdt(trading_client, demo=demo)

    with ThreadPoolExecutor(max_workers=3) as pool:
        wallet_future = pool.submit(_wallet)
        orders_future = pool.submit(_safe_open_orders, trading_client, settle_coin=demo.settle_coin)
        positions_future = pool.submit(_safe_raw_positions, trading_client, settle_coin=demo.settle_coin)
        equity_usdt, wallet_error = wallet_future.result()
        raw_open_orders, open_order_error = orders_future.result()
        raw_positions, position_error = positions_future.result()
    return {
        "equity_usdt": equity_usdt,
        "wallet_error": wallet_error,
        "raw_open_orders": raw_open_orders,
        "open_order_error": open_order_error,
        "raw_positions": raw_positions,
        "position_error": position_error,
    }


def _active_position_by_symbol(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for position in positions:
        symbol = str(position.get("symbol", ""))
        size = _float(position.get("size"))
        if symbol and size > 0.0:
            output[symbol] = position
    return output


def _price_lookup_from_positions(position_by_symbol: dict[str, dict[str, Any]]) -> dict[str, float]:
    output: dict[str, float] = {}
    for symbol, position in position_by_symbol.items():
        price = _first_float(position, ("markPrice", "mark_price", "lastPrice", "indexPrice", "avgPrice"))
        if price > 0.0:
            output[symbol] = price
    return output


def _risk_tick_size_lookup(
    open_trades: pl.DataFrame,
    *,
    config: ResearchConfig,
    market_client: Any | None,
    enabled: bool,
) -> dict[str, float]:
    output: dict[str, float] = {}
    if not open_trades.is_empty() and "tick_size" in open_trades.columns:
        for row in open_trades.select(["symbol", "tick_size"]).drop_nulls(["symbol"]).to_dicts():
            tick_size = _float(row.get("tick_size"))
            if tick_size > 0.0:
                output[str(row["symbol"])] = tick_size
    if not enabled:
        return output
    missing_symbols = set(_column_values(open_trades, "symbol")) - set(output)
    if not missing_symbols:
        return output
    try:
        client = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        instruments = _normalize_instruments(client.get_instruments_info())
    except Exception:
        return output
    if instruments.is_empty():
        return output
    for row in instruments.filter(pl.col("symbol").is_in(sorted(missing_symbols))).select(["symbol", "tick_size"]).to_dicts():
        tick_size = _float(row.get("tick_size"))
        if tick_size > 0.0:
            output[str(row["symbol"])] = tick_size
    return output





def _private_credentials_present() -> bool:
    from .bybit import resolve_private_credentials

    api_key, api_secret, _ = resolve_private_credentials()
    return bool(api_key and api_secret)


def _build_private_client(config: ResearchConfig) -> BybitPrivateClient:
    api_key, api_secret, demo = resolve_private_credentials()
    if not api_key or not api_secret:
        which = (
            "BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET" if demo
            else "BYBIT_REAL_API_KEY and BYBIT_REAL_API_SECRET"
        )
        raise RuntimeError(f"Set {which} before submitting orders")
    return BybitPrivateClient(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=demo,
        api_key=api_key,
        api_secret=api_secret,
    )


def _order_params(
    *,
    symbol: str,
    side: str,
    qty: str,
    order_type: str,
    order_link_id: str,
    reduce_only: bool,
    price: float | None = None,
    time_in_force: str | None = None,
    stop_loss: float | str | None = None,
    take_profit: float | str | None = None,
    tp_trigger_by: str = "MarkPrice",
    sl_trigger_by: str = "MarkPrice",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": qty,
        "orderLinkId": order_link_id,
        "reduceOnly": reduce_only,
    }
    if stop_loss is not None and _float(stop_loss) > 0.0:
        params["stopLoss"] = _decimal_text(Decimal(str(stop_loss)))
        params["slTriggerBy"] = sl_trigger_by
    if take_profit is not None and _float(take_profit) > 0.0:
        params["takeProfit"] = _decimal_text(Decimal(str(take_profit)))
        params["tpTriggerBy"] = tp_trigger_by
    if order_type.lower() != "market" and price is not None and price > 0.0:
        params["price"] = _decimal_text(Decimal(str(price)))
    if order_type.lower() != "market":
        params["timeInForce"] = time_in_force or "PostOnly"
    return params




def _fallback_tick_size(price: float) -> float:
    if price >= 1000.0:
        return 0.1
    if price >= 100.0:
        return 0.01
    if price >= 1.0:
        return 0.0001
    return 0.000001


def _execution_summary(executions: list[dict[str, Any]]) -> dict[str, Any]:
    qty = 0.0
    value = 0.0
    fee = 0.0
    # exec_time_ms = the latest venue-reported execTime across this order's
    # fills. For a single-fill order it IS the fill time; for a multi-fill
    # order it is the time the order fully completed. Capturing it lets the
    # ledger record when the *venue* filled the order rather than when our
    # daemon noticed (now_ms), which the reconciliation needs to measure
    # true fill-time skew between paper and demo (and between demo and Bybit).
    exec_time_ms = 0
    for execution in executions:
        exec_qty = _float(execution.get("execQty"))
        exec_price = _float(execution.get("execPrice"))
        exec_value = _float(execution.get("execValue"))
        qty += exec_qty
        value += exec_value if exec_value > 0.0 else exec_qty * exec_price
        fee += _float(execution.get("execFee"))
        ts_candidate = int(_float(execution.get("execTime") or 0))
        if ts_candidate > exec_time_ms:
            exec_time_ms = ts_candidate
    return {
        "qty": _decimal_text(Decimal(str(qty))) if qty > 0.0 else "",
        "avg_price": value / qty if qty > 0.0 else 0.0,
        "fee": fee,
        "exec_time_ms": exec_time_ms,
        "executions": len(executions),
    }


def _wait_for_execution_summary(
    trading_client: Any,
    *,
    symbol: str,
    order_link_id: str,
    poll_seconds: float,
    poll_interval_seconds: float,
    fast_poll_interval_seconds: float = 0.05,
    fast_poll_seconds: float = 0.5,
    execution_event_router: Any | None = None,
    target_qty: float = 0.0,
) -> dict[str, Any]:
    # `while True` is bounded: the deadline check below returns once
    # `time.monotonic() >= deadline`, so the loop runs at most `poll_seconds`
    # wall time regardless of what the venue returns. The sleep also clamps to
    # the time remaining before the deadline so we don't oversleep past it.
    #
    # Bybit demo fills typically land in 100-300ms. A uniform 200ms poll wastes
    # up to a full poll period per candidate on the average fill; a 50ms fast
    # window for the first 500ms catches most fills near optimally, then we
    # back off to the slower interval to limit get_trade_history hits.
    #
    # When an execution_event_router is provided, the WS private execution
    # stream is the fast path: each iteration first waits up to one slow_interval
    # for the router to deliver an event matching this orderLinkId; if WS
    # delivers, we return immediately without a REST call. REST polling remains
    # the safety net — if WS is down, slow, or events are lost, the existing
    # poll-deadline behavior is unchanged.
    start = time.monotonic()
    deadline = start + max(poll_seconds, 0.0)
    fast_deadline = start + max(fast_poll_seconds, 0.0)
    slow_interval = max(poll_interval_seconds, 0.01)
    fast_interval = max(fast_poll_interval_seconds, 0.005)
    while True:
        if execution_event_router is not None:
            now = time.monotonic()
            ws_wait = min(
                fast_interval if now < fast_deadline else slow_interval,
                max(deadline - now, 0.0),
            )
            if ws_wait > 0.0:
                ws_rows = execution_event_router.wait_for_fill_rows(order_link_id, ws_wait)
                if ws_rows:
                    summary = _execution_summary(ws_rows)
                    summary_qty = _float(summary.get("qty"))
                    # EXEC-6: multi-fill market orders deliver several WS execution
                    # rows for the SAME orderLinkId (book-walk). wait_for_fill_rows
                    # wakes on the FIRST row, so returning here would record a
                    # fully-filled order as `partial` from only the first leg. When
                    # target_qty is known, keep looping (rows accumulate in the
                    # router) until the cumulative WS qty reaches the target or the
                    # deadline passes; when unknown (0.0) preserve the legacy
                    # first-fill return.
                    if summary_qty > 0.0 and (
                        target_qty <= 0.0
                        or summary_qty + max(target_qty * 1e-8, 1e-12) >= target_qty
                        or time.monotonic() >= deadline
                    ):
                        return summary
        summary = _execution_summary(trading_client.get_trade_history(symbol=symbol, order_link_id=order_link_id, limit=50))
        rest_qty = _float(summary.get("qty"))
        # EXEC-6: gate the REST poll on target_qty too (mirrors the WS branch). A still-partial
        # REST view (only the first book-walk leg settled) must NOT short-circuit a multi-fill
        # order to 'partial'; wait for the cumulative qty to reach target. The deadline clause
        # still bounds a genuine partial (only one leg ever arrives) to the poll budget.
        if (
            rest_qty > 0.0
            and (target_qty <= 0.0 or rest_qty + max(target_qty * 1e-8, 1e-12) >= target_qty)
        ) or time.monotonic() >= deadline:
            return summary
        if execution_event_router is None:
            now = time.monotonic()
            interval = fast_interval if now < fast_deadline else slow_interval
            time.sleep(min(interval, max(deadline - now, 0.0)))


def _position_size_by_symbol_side(positions: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Position size keyed by (symbol, normalized_side).

    Side-awareness matters for orphan reconciliation: if a short closes and
    a long is opened on the same symbol (manual flip on Bybit, or two
    daemons sharing an account), Bybit's positions endpoint reports the
    new long with size > 0. Keying only by symbol would let the orphan
    reconciler keep the stale short trade as "still open" because some
    position exists for that symbol. (symbol, side) keying surfaces the
    flip as an orphan close on the short and leaves the new long
    unrelated.

    Sizes are aggregated by max() within each (symbol, side) bucket so a
    fragmented position (rare; would require hedge mode) still reports
    its real size."""
    output: dict[tuple[str, str], float] = {}
    for position in positions:
        symbol = str(position.get("symbol", ""))
        if not symbol:
            continue
        size = _float(position.get("size"))
        if size <= 0.0:
            continue
        side = _normalized_position_side(position.get("side"))
        if not side:
            continue
        key = (symbol, side)
        output[key] = max(output.get(key, 0.0), size)
    return output


def _normalized_position_side(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"sell", "short"}:
        return "short"
    if text in {"buy", "long"}:
        return "long"
    return text


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value != 0.0:
            return value
    return 0.0


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _combine_errors(*errors: str) -> str:
    output: list[str] = []
    seen: set[str] = set()
    for error in errors:
        if error and error not in seen:
            output.append(error)
            seen.add(error)
    return "; ".join(output)


def _prices_close(left: float, right: float, *, tolerance_bps: float) -> bool:
    if left <= 0.0 or right <= 0.0:
        return False
    return abs(left / right - 1.0) <= tolerance_bps / 10_000.0


def _open_trades(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty() or "status" not in trades.columns:
        return _empty_trades()
    return trades.filter(pl.col("status").is_in(["open", "submitted"]))


def _upsert_rows(existing: pl.DataFrame, rows: list[dict[str, Any]], *, key: str) -> pl.DataFrame:
    incoming = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if existing.is_empty():
        return incoming
    if incoming.is_empty():
        return existing
    return pl.concat([existing, incoming], how="diagonal_relaxed").unique(subset=[key], keep="last")


def _write_trade_rows(root: Path, rows: pl.DataFrame) -> None:
    if not rows.is_empty():
        write_dataset(rows, root, "event_demo_trades", partition_by=())


def _write_order_rows(root: Path, rows: pl.DataFrame) -> None:
    if not rows.is_empty():
        write_dataset(rows, root, "event_demo_orders", partition_by=())


def _price_lookup_from_tickers_and_klines(tickers: pl.DataFrame, klines: pl.DataFrame) -> dict[str, float]:
    output: dict[str, float] = {}
    if not klines.is_empty():
        for row in klines.sort(["symbol", "ts_ms"]).group_by("symbol").tail(1).to_dicts():
            price = _float(row.get("close"))
            if price > 0.0:
                output[str(row["symbol"])] = price
    if not tickers.is_empty():
        for row in tickers.to_dicts():
            symbol = str(row.get("symbol", ""))
            price = _float(row.get("mark_price")) or _float(row.get("last_price"))
            if symbol and price > 0.0:
                output[symbol] = price
    return output


def _contract_lookup(universe: pl.DataFrame) -> dict[str, dict[str, Any]]:
    if universe.is_empty():
        return {}
    return {str(row["symbol"]): row for row in universe.to_dicts()}


def _stop_price_for_entry(*, entry_price: float, side: str, stop_loss_pct: float, tick_size: float) -> float:
    price = Decimal(str(entry_price))
    pct = Decimal(str(stop_loss_pct))
    if side == "short":
        raw = price * (Decimal("1") + pct)
        return _round_price(raw, tick_size=tick_size, rounding=ROUND_CEILING)
    raw = price * (Decimal("1") - pct)
    return _round_price(raw, tick_size=tick_size, rounding=ROUND_FLOOR)


def _take_profit_price_for_entry(*, entry_price: float, side: str, take_profit_pct: float, tick_size: float) -> float:
    if take_profit_pct <= 0.0:
        return 0.0
    price = Decimal(str(entry_price))
    pct = Decimal(str(take_profit_pct))
    if side == "short":
        raw = price * (Decimal("1") - pct)
        return _round_price(raw, tick_size=tick_size, rounding=ROUND_FLOOR)
    raw = price * (Decimal("1") + pct)
    return _round_price(raw, tick_size=tick_size, rounding=ROUND_CEILING)


def _round_price(price: float | Decimal, *, tick_size: float, rounding: str) -> float:
    try:
        value = Decimal(str(price))
        tick = Decimal(str(tick_size if tick_size > 0.0 else 0.0001))
        units = (value / tick).to_integral_value(rounding=rounding)
        return float(units * tick)
    except (InvalidOperation, ZeroDivisionError):
        # Fallback: coerce the input to float (identity for a float input; honours the
        # -> float signature for a Decimal input instead of cast-lying about it).
        return float(price)


def _kline_window(now_ms: int, *, lookback_days: int) -> tuple[int, int]:
    end_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
    start_ms = end_ms - lookback_days * MS_PER_DAY
    return start_ms, end_ms


def _floor_hour_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MS_PER_HOUR)


def _utc_now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _iso_dt(ts_ms: Any) -> str:
    try:
        value = int(ts_ms)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def _yyyymmddhhmmss(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y%m%d%H%M%S")


def _column_values(frame: pl.DataFrame, column: str) -> list[str]:
    if frame.is_empty() or column not in frame.columns:
        return []
    return [str(item) for item in frame[column].to_list()]


def _max_int(frame: pl.DataFrame, column: str) -> int:
    if frame.is_empty() or column not in frame.columns:
        return 0
    value = frame[column].max()
    return int(value) if value is not None else 0  # type: ignore[arg-type]  # polars Series value


def _float(value: Any) -> float:
    # Delegates to the finite-guarded _common.finite_float (quality-dup / code-quality-5):
    # one NaN/inf coercion policy shared with reconciliation._float and ws_state_cache._float.
    return finite_float(value, default=0.0) or 0.0


def _ratio_or_zero(numerator: Any, denominator: Any) -> float:
    """numerator/denominator, returning 0.0 when the denominator is 0 (e.g. a
    notional/equity weight: zero equity => zero weight). Contrast
    volume_events._ratio_or_nan, which returns NaN on a non-positive denominator
    -- they were both once named _safe_ratio, a same-name/different-contract trap."""
    denom = _float(denominator)
    if denom == 0.0:
        return 0.0
    return _float(numerator) / denom


def _trade_return(entry_price: float, exit_price: float, *, side: str) -> float:
    if entry_price <= 0.0 or exit_price <= 0.0:
        return 0.0
    if side == "short":
        return (entry_price - exit_price) / entry_price
    if side == "long":
        return (exit_price - entry_price) / entry_price
    return 0.0




def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n", ""}:
        return False
    return bool(value)


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantity_text(value: float) -> str:
    return _decimal_text(Decimal(f"{max(value, 0.0):.12f}"))




def _empty_klines() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "symbol": pl.Series([], dtype=pl.String),
            "open": pl.Series([], dtype=pl.Float64),
            "high": pl.Series([], dtype=pl.Float64),
            "low": pl.Series([], dtype=pl.Float64),
            "close": pl.Series([], dtype=pl.Float64),
            "volume_base": pl.Series([], dtype=pl.Float64),
            "turnover_quote": pl.Series([], dtype=pl.Float64),
            "source": pl.Series([], dtype=pl.String),
        }
    )


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_id": pl.Series([], dtype=pl.String),
            "strategy_id": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "side": pl.Series([], dtype=pl.String),
            "status": pl.Series([], dtype=pl.String),
        }
    )








# ---------------------------------------------------------------------------
# Exit-execution back-import.
#
# Exit-execution code was extracted into a sibling module (event_demo_exits.py)
# to keep this file manageable. Re-importing the names here means external
# callers — `from liquidity_migration.event_demo import _execute_exits` — keep
# working without churn. The back-import sits at the END so event_demo.py has
# defined all its helpers (which event_demo_exits.py imports from us) before
# Python starts loading that module.
# ---------------------------------------------------------------------------
from .event_demo_exits import (  # noqa: E402, F401  (re-export surface)
    _execute_exits,
    _execute_risk_exits,
    _execute_stop_repairs,
    plan_risk_exits,
    plan_stop_repairs,
    _fetch_account_closed_pnl,
    _limit_chase_price,
    _orphan_close_pnl_backfill,
    _orphan_close_pnl_from_records,
    _orphan_close_trade_row,
    _partial_exit_trade_update,
    _preflight_exit_order_row,
    _reconcile_open_trades,
    _reconcile_pending_order_fills,
    _risk_order_row,
    _risk_preflight_order_row,
    _risk_reconcile_missing_positions,
    _submit_limit_chase_exit,
    _submit_reduce_only_exit,
    _terminalize_stale_pending_entry_orders,
)


# --- re-export extracted module (see top-of-file note) ---
from .event_demo_data import (  # noqa: E402, F401
    _build_demo_universe,
    _bust_demo_instruments_cache,
    _concat_recent_klines,
    _dedupe_recent_klines,
    _demo_feature_cache_fingerprint,
    _demo_feature_cache_paths,
    _demo_instruments,
    _demo_instruments_cache_paths,
    _demo_kline_compact_cache_paths,
    _demo_kline_compact_metadata,
    _demo_kline_fetch_ranges,
    _demo_private_rest_rate_limit_per_second,
    _demo_rest_rate_limit_per_second,
    _download_recent_1h_klines,
    _fetch_recent_1h_klines,
    _read_demo_feature_cache,
    _read_demo_instruments_cache,
    _read_demo_kline_cache,
    _read_demo_kline_compact_cache,
    _write_demo_feature_cache,
    _write_demo_instruments_cache,
    _write_demo_kline_compact_cache,
)


# --- re-export extracted module (see top-of-file note) ---
from .event_demo_reports import (  # noqa: E402, F401
    _position_markdown_row,
    _telegram_notification_reason,
    format_event_demo_cycle_report,
    format_event_risk_cycle_report,
    format_telegram_status_message,
)


def _maybe_notify(payload: dict[str, Any], *, enabled: bool) -> tuple[bool, str]:
    # Kept in the hub (not event_demo_reports) for test-patchability of
    # `event_demo.send_telegram_message` — see the note in event_demo_reports.py.
    if not enabled:
        return False, "disabled"
    if not _telegram_notification_reason(payload):
        return False, "quiet_no_material_event"
    try:
        # Formatting reads payload fields by direct subscript; a schema gap would raise.
        # Keep it INSIDE the guard so a telegram-formatting fault becomes cycle telemetry,
        # never an exception that could kill the (unguarded) event-risk-cycle --loop (EVE-2).
        text = format_telegram_status_message(payload)
        sent = send_telegram_message(text, enabled=True)
    except Exception as exc:  # noqa: BLE001 - notification failure is cycle telemetry
        return False, str(exc)[:500]
    if not sent:
        return False, "telegram env missing or Telegram API returned false"
    return True, ""


# --- re-export extracted module (see top-of-file note) ---
