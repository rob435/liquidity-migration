from __future__ import annotations

import json
import os
import shutil
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import blake2b
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitMarketData, BybitPrivateClient, BybitPublicTradeStream
from .config import DailyCloseFadeConfig, ResearchConfig
from .demo_execution import DemoSyncConfig, submit_demo_exit_for_trade
from .storage import dataset_lock_path, dataset_path, exclusive_file_lock, read_dataset, write_dataset


MS_PER_MINUTE = 60_000
_BLOCKING_FAST_EXIT_STATUSES = {
    "accepted",
    "blocked_duplicate_exit",
    "exit_submitted",
    "placed",
    "submitted",
    "submit_unknown",
}


@dataclass(frozen=True, slots=True)
class DemoFastProtectionConfig:
    runtime_seconds: float = 0.0
    submit_exits: bool = False
    confirmed: bool = False
    order_link_prefix: str = ""
    min_submit_interval_ms: int = 0
    max_exit_submits_per_run: int = 5
    whole_symbol_exit: bool = True


@dataclass(slots=True)
class _TradeState:
    trade: dict[str, Any]
    symbol: str
    trade_id: str
    entry_price: float
    best_price: float
    realized_vol: float
    vol_trailing_stop_pct: float
    vol_trailing_activation_pct: float
    mfe_giveback_activation_pct: float
    mfe_giveback_pct: float
    profit_active_ts_ms: int
    exit_eligible_ts_ms: int
    vol_trailing_active: bool = False
    mfe_giveback_active: bool = False
    triggered: bool = False
    trigger_ts_ms: int | None = None
    trigger_price: float | None = None
    trigger_model_exit_price: float | None = None
    trigger_reason: str = ""
    exit_order_link_id: str = ""
    exit_status: str = ""
    has_active_observation: bool = False


@dataclass(frozen=True, slots=True)
class _TriggerDecision:
    reason: str
    model_exit_price: float


def run_demo_fast_protection(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig,
    protection_config: DemoFastProtectionConfig,
    now: datetime | None = None,
    execution_client: Any | None = None,
    market_client: Any | None = None,
    trade_stream: Any | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    if protection_config.submit_exits and not protection_config.confirmed:
        raise RuntimeError("Refusing fast demo protection submission without demo sync confirmation")

    root = Path(data_root).expanduser()
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    now_ms = int(now_dt.timestamp() * 1000)
    trades = read_dataset(root, "forward_paper_trades")
    with exclusive_file_lock(dataset_lock_path(root, "demo_execution_orders")):
        orders = read_dataset(root, "demo_execution_orders")
    previous_state = read_dataset(root, "demo_fast_protection_state")
    states = _active_trade_states(
        trades,
        orders,
        previous_state,
        fade_config=fade_config,
        now_ms=now_ms,
    )
    if not states:
        _clear_state(root)
        payload = _payload(
            now_dt,
            protection_config,
            symbols=[],
            states=[],
            events=[],
            latencies_ms=[],
            reason="no_active_profit_protected_trades",
        )
        _write_report(root, payload)
        return payload

    executor = execution_client
    market = market_client
    if protection_config.submit_exits and executor is None:
        key = api_key or os.environ.get("BYBIT_DEMO_API_KEY")
        secret = api_secret or os.environ.get("BYBIT_DEMO_API_SECRET")
        executor = BybitPrivateClient(
            category=config.exchange.category,
            testnet=config.exchange.testnet,
            demo=True,
            api_key=key,
            api_secret=secret,
        )
    if protection_config.submit_exits and market is None and execution_client is None:
        market = BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    instruments = (
        {str(row.get("symbol", "")).upper(): row for row in market.get_instruments_info()}
        if protection_config.submit_exits and market is not None
        else {}
    )

    stream = trade_stream or BybitPublicTradeStream(category=config.exchange.category, testnet=config.exchange.testnet)
    symbols = sorted({state.symbol for state in states})
    state_by_symbol: dict[str, list[_TradeState]] = {}
    for state in states:
        state_by_symbol.setdefault(state.symbol, []).append(state)
    events: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    stop_event = threading.Event()
    submit_count = 0
    duplicate_blocks = 0
    submit_blocks = 0
    last_submit_ms = 0

    def callback(message: Any) -> None:
        nonlocal duplicate_blocks, last_submit_ms, submit_blocks, submit_count
        started = time.perf_counter()
        try:
            for event in _public_trade_events(message):
                symbol = event["symbol"]
                price = event["price"]
                event_ts_ms = event["ts_ms"] or int(time.time() * 1000)
                for state in state_by_symbol.get(symbol, []):
                    if state.triggered:
                        duplicate_blocks += 1
                        continue
                    trigger = _evaluate_trade_event(state, price=price, event_ts_ms=event_ts_ms)
                    if trigger is None:
                        continue
                    if submit_count >= protection_config.max_exit_submits_per_run:
                        submit_blocks += 1
                        events.append(_event_row(root, state, event_ts_ms, price, trigger, "blocked_run_cap", {}))
                        continue
                    if (
                        protection_config.min_submit_interval_ms > 0
                        and last_submit_ms > 0
                        and event_ts_ms - last_submit_ms < protection_config.min_submit_interval_ms
                    ):
                        submit_blocks += 1
                        events.append(_event_row(root, state, event_ts_ms, price, trigger, "blocked_submit_interval", {}))
                        continue
                    state.triggered = True
                    state.trigger_ts_ms = event_ts_ms
                    state.trigger_price = price
                    state.trigger_model_exit_price = trigger.model_exit_price
                    state.trigger_reason = trigger.reason
                    result: dict[str, Any] = {"status": "observe_only"}
                    if protection_config.submit_exits:
                        if executor is None:
                            result = {"status": "blocked_missing_executor"}
                            submit_blocks += 1
                        else:
                            try:
                                trade = dict(state.trade)
                                trade.update(
                                    {
                                        "status": "closed",
                                        "exit_ts_ms": event_ts_ms,
                                        "exit_time": _dt_from_ms(event_ts_ms).isoformat(),
                                        "exit_price": trigger.model_exit_price,
                                        "model_exit_price": trigger.model_exit_price,
                                        "trigger_price": price,
                                        "fast_trigger_price": price,
                                        "execution_exit_price": price,
                                        "exit_reason": f"fast_{trigger.reason}",
                                        "mark_ts_ms": event_ts_ms,
                                        "mark_time": _dt_from_ms(event_ts_ms).isoformat(),
                                        "mark_price": price,
                                    }
                                )
                                result = submit_demo_exit_for_trade(
                                    root,
                                    trade,
                                    sync_config=DemoSyncConfig(
                                        submit_orders=True,
                                        confirmed=True,
                                        allow_market_exit=True,
                                        order_link_prefix=protection_config.order_link_prefix,
                                    ),
                                    now=_dt_from_ms(event_ts_ms),
                                    execution_client=executor,
                                    whole_position=protection_config.whole_symbol_exit,
                                    instrument=instruments.get(state.symbol, {}),
                                )
                            except Exception as exc:  # noqa: BLE001 - pre-submit/reconcile failures must be retryable
                                result = {"status": "submit_exception", "error": str(exc)}
                                submit_blocks += 1
                            result_status = str(result.get("status") or "")
                            if result_status in _BLOCKING_FAST_EXIT_STATUSES:
                                if result_status in {"accepted", "submit_unknown"}:
                                    submit_count += 1
                                    last_submit_ms = event_ts_ms
                                state.exit_order_link_id = str(result.get("order_link_id") or "")
                                state.exit_status = result_status
                            else:
                                state.triggered = False
                                state.trigger_ts_ms = None
                                state.trigger_price = None
                                state.trigger_model_exit_price = None
                                state.trigger_reason = ""
                    events.append(_event_row(root, state, event_ts_ms, price, trigger, str(result.get("status") or ""), result))
                    if submit_count >= protection_config.max_exit_submits_per_run:
                        stop_event.set()
        finally:
            latencies_ms.append((time.perf_counter() - started) * 1000.0)

    stream.subscribe_public_trades(symbols, callback)
    runtime = max(float(protection_config.runtime_seconds), 0.0)
    if runtime > 0.0 and not stop_event.is_set():
        stop_event.wait(runtime)
    closer = getattr(stream, "close", None)
    if callable(closer):
        closer()

    _write_state(root, states)
    if events:
        _write_events(root, events)
    payload = _payload(
        now_dt,
        protection_config,
        symbols=symbols,
        states=states,
        events=events,
        latencies_ms=latencies_ms,
        reason="completed",
        duplicate_blocks=duplicate_blocks,
        submit_blocks=submit_blocks,
    )
    _write_report(root, payload)
    return payload


def _active_trade_states(
    trades: pl.DataFrame,
    orders: pl.DataFrame,
    previous_state: pl.DataFrame,
    *,
    fade_config: DailyCloseFadeConfig,
    now_ms: int,
) -> list[_TradeState]:
    if trades.is_empty() or orders.is_empty():
        return []
    previous = _previous_state(previous_state)
    order_trade_ids = _entry_trade_ids_with_exposure(orders)
    states: list[_TradeState] = []
    for trade in trades.to_dicts():
        trade_id = str(trade.get("trade_id") or "")
        symbol = str(trade.get("symbol") or "").upper()
        if not trade_id or not symbol or trade_id not in order_trade_ids:
            continue
        if str(trade.get("status") or "") != "open":
            continue
        profit_active_ts_ms = int(_num(trade.get("profit_protection_active_ts_ms")))
        if profit_active_ts_ms <= 0:
            entry_complete = int(_num(trade.get("entry_complete_ts_ms")) or _num(trade.get("entry_ts_ms")))
            profit_active_ts_ms = entry_complete + int(_num(trade.get("profit_protection_delay_minutes")) or 15) * 60_000
        if now_ms < profit_active_ts_ms:
            continue
        entry_price = _num(trade.get("avg_entry_price")) or _num(trade.get("entry_price"))
        if entry_price <= 0.0:
            continue
        realized_vol = max(_num(trade.get("realized_vol")), 0.0)
        vol_stop_mult = _trade_or_default(trade, "vol_trailing_stop_mult", float(fade_config.vol_trailing_stop_mult))
        vol_activation_mult = _trade_or_default(
            trade,
            "vol_trailing_activation_mult",
            float(fade_config.vol_trailing_activation_mult),
        )
        mfe_activation = _trade_or_default(
            trade,
            "mfe_giveback_activation_pct",
            float(fade_config.mfe_giveback_activation_pct),
        )
        mfe_giveback = _trade_or_default(trade, "mfe_giveback_pct", float(fade_config.mfe_giveback_pct))
        prior = previous.get(trade_id, {})
        mark_price = _num(trade.get("mark_price")) or entry_price
        prior_updated_ts_ms = int(_num(prior.get("updated_ts_ms")))
        prior_has_active_observation = _truthy(prior.get("has_active_observation")) or (
            prior_updated_ts_ms >= profit_active_ts_ms and _num(prior.get("best_price")) > 0.0
        )
        best_price = (
            _num(prior.get("best_price"))
            if prior_has_active_observation
            else _seed_best_price(trade, entry_price=entry_price, mark_price=mark_price)
        )
        if best_price <= 0.0:
            best_price = entry_price
        best_return = _short_return(entry_price, best_price)
        exit_order_state = _exit_order_state(orders, trade_id)
        prior_exit_status = str(prior.get("exit_status") or "").strip().lower()
        prior_exit_seen = exit_order_state == "blocking" or (
            exit_order_state == "" and prior_exit_status in _BLOCKING_FAST_EXIT_STATUSES
        )
        state = _TradeState(
            trade=trade,
            symbol=symbol,
            trade_id=trade_id,
            entry_price=entry_price,
            best_price=best_price,
            realized_vol=realized_vol,
            vol_trailing_stop_pct=realized_vol * vol_stop_mult if vol_stop_mult > 0.0 else 0.0,
            vol_trailing_activation_pct=realized_vol * vol_activation_mult if vol_stop_mult > 0.0 else 0.0,
            mfe_giveback_activation_pct=mfe_activation,
            mfe_giveback_pct=mfe_giveback,
            profit_active_ts_ms=profit_active_ts_ms,
            exit_eligible_ts_ms=profit_active_ts_ms + MS_PER_MINUTE,
            vol_trailing_active=prior_has_active_observation
            and (_truthy(prior.get("vol_trailing_active")) or best_return >= realized_vol * vol_activation_mult),
            mfe_giveback_active=prior_has_active_observation
            and (_truthy(prior.get("mfe_giveback_active")) or best_return >= mfe_activation),
            triggered=prior_exit_seen,
            trigger_ts_ms=int(_num(prior.get("trigger_ts_ms"))) or None,
            trigger_price=_num(prior.get("trigger_price")) or None,
            trigger_model_exit_price=_num(prior.get("trigger_model_exit_price")) or None,
            trigger_reason=str(prior.get("trigger_reason") or "") if prior_exit_seen else "",
            exit_order_link_id=str(prior.get("exit_order_link_id") or ""),
            exit_status=str(prior.get("exit_status") or ""),
            has_active_observation=prior_has_active_observation,
        )
        states.append(state)
    return states


def _evaluate_trade_event(state: _TradeState, *, price: float, event_ts_ms: int) -> _TriggerDecision | None:
    if price <= 0.0 or event_ts_ms < state.profit_active_ts_ms:
        return None
    if not state.has_active_observation:
        if price < state.best_price:
            state.best_price = price
        state.has_active_observation = True
        _arm_profit_protection(state)
        return None
    if price < state.best_price:
        state.best_price = price
    _arm_profit_protection(state)
    if event_ts_ms < state.exit_eligible_ts_ms:
        return None
    protective_exits: list[tuple[float, str]] = []
    if state.vol_trailing_active and state.vol_trailing_stop_pct > 0.0:
        stop_price = state.best_price * (1.0 + state.vol_trailing_stop_pct)
        if price >= stop_price:
            protective_exits.append((stop_price, "vol_trailing_stop"))
    if state.mfe_giveback_active and state.mfe_giveback_pct > 0.0:
        stop_price = _mfe_giveback_stop_price(state.entry_price, state.best_price, state.mfe_giveback_pct)
        if price >= stop_price:
            protective_exits.append((stop_price, "mfe_giveback"))
    if not protective_exits:
        return None
    stop_price, reason = max(protective_exits, key=lambda item: item[0])
    return _TriggerDecision(reason=reason, model_exit_price=stop_price)


def _arm_profit_protection(state: _TradeState) -> None:
    best_return = _short_return(state.entry_price, state.best_price)
    if state.vol_trailing_stop_pct > 0.0 and best_return >= state.vol_trailing_activation_pct:
        state.vol_trailing_active = True
    if state.mfe_giveback_pct > 0.0 and best_return >= state.mfe_giveback_activation_pct:
        state.mfe_giveback_active = True


def _seed_best_price(trade: dict[str, Any], *, entry_price: float, mark_price: float) -> float:
    candidates = [entry_price]
    if mark_price > 0.0:
        candidates.append(mark_price)
    mfe = max(_num(trade.get("mfe")), 0.0)
    if mfe > 0.0:
        candidates.append(entry_price * max(0.0, 1.0 - mfe))
    return min(price for price in candidates if price > 0.0)


def _public_trade_events(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, dict):
        raw = message.get("data", message)
    else:
        raw = message
    rows = raw if isinstance(raw, list) else [raw]
    output: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("s") or row.get("symbol") or "").upper()
        price = _num(row.get("p", row.get("price", row.get("execPrice"))))
        ts_ms = int(_num(row.get("T", row.get("ts", row.get("timestamp")))))
        if symbol and price > 0.0:
            output.append({"symbol": symbol, "price": price, "ts_ms": ts_ms})
    return output


def _entry_trade_ids_with_exposure(orders: pl.DataFrame) -> set[str]:
    if orders.is_empty() or {"paper_trade_id", "action"}.difference(set(orders.columns)):
        return set()
    output: set[str] = set()
    filled_reconciled_statuses = {"filled", "filled_pending_execs", "partial", "position_detected"}
    for row in orders.filter(pl.col("action") == "entry").to_dicts():
        reconciled = str(row.get("reconciled_status") or "").lower()
        if (
            reconciled in filled_reconciled_statuses
            or _num(row.get("filled_qty")) > 0.0
            or _num(row.get("position_size")) > 0.0
        ):
            output.add(str(row.get("paper_trade_id") or ""))
    return output


def _exit_order_state(orders: pl.DataFrame, trade_id: str) -> str:
    if orders.is_empty() or {"paper_trade_id", "action"}.difference(set(orders.columns)):
        return ""
    rows = orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == "exit")).to_dicts()
    if not rows:
        return ""
    blocking_statuses = {
        "accepted",
        "placed",
        "submitted",
        "submit_unknown",
        "exit_submitted",
        "open_order_seen",
        "position_detected",
        "partial",
        "filled",
        "filled_pending_execs",
    }
    terminal_statuses = {"cancelled", "missed_entry", "submit_not_found", "rejected", "partial_cancelled"}
    has_terminal = False
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        reconciled = str(row.get("reconciled_status") or "").strip().lower()
        if reconciled in terminal_statuses:
            has_terminal = True
            continue
        if reconciled in blocking_statuses or status in blocking_statuses:
            return "blocking"
        if status in terminal_statuses:
            has_terminal = True
    return "terminal" if has_terminal else ""


def _previous_state(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
    if frame.is_empty() or "paper_trade_id" not in frame.columns:
        return {}
    return {str(row.get("paper_trade_id") or ""): row for row in frame.to_dicts()}


def _write_state(root: Path, states: list[_TradeState]) -> None:
    if not states:
        return
    now_ms = int(time.time() * 1000)
    rows = []
    for state in states:
        mfe = _short_return(state.entry_price, state.best_price)
        rows.append(
            {
                "paper_trade_id": state.trade_id,
                "symbol": state.symbol,
                "date": str(state.trade.get("date") or _dt_from_ms(now_ms).date().isoformat()),
                "updated_ts_ms": now_ms,
                "updated_time": _dt_from_ms(now_ms).isoformat(),
                "entry_price": state.entry_price,
                "profit_active_ts_ms": state.profit_active_ts_ms,
                "profit_active_time": _dt_from_ms(state.profit_active_ts_ms).isoformat(),
                "exit_eligible_ts_ms": state.exit_eligible_ts_ms,
                "exit_eligible_time": _dt_from_ms(state.exit_eligible_ts_ms).isoformat(),
                "best_price": state.best_price,
                "mfe": mfe,
                "vol_trailing_active": state.vol_trailing_active,
                "mfe_giveback_active": state.mfe_giveback_active,
                "trigger_ts_ms": state.trigger_ts_ms,
                "trigger_time": _dt_from_ms(state.trigger_ts_ms).isoformat() if state.trigger_ts_ms else "",
                "trigger_price": state.trigger_price,
                "trigger_model_exit_price": state.trigger_model_exit_price,
                "trigger_reason": state.trigger_reason,
                "exit_order_link_id": state.exit_order_link_id,
                "exit_status": state.exit_status,
                "has_active_observation": state.has_active_observation,
            }
        )
    with exclusive_file_lock(dataset_lock_path(root, "demo_fast_protection_state")):
        write_dataset(pl.DataFrame(rows, infer_schema_length=None), root, "demo_fast_protection_state")


def _clear_state(root: Path) -> None:
    with exclusive_file_lock(dataset_lock_path(root, "demo_fast_protection_state")):
        path = dataset_path(root, "demo_fast_protection_state")
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def _write_events(root: Path, events: list[dict[str, Any]]) -> None:
    with exclusive_file_lock(dataset_lock_path(root, "demo_fast_protection_events")):
        write_dataset(pl.DataFrame(events, infer_schema_length=None), root, "demo_fast_protection_events")


def _event_row(
    root: Path,
    state: _TradeState,
    ts_ms: int,
    price: float,
    trigger: _TriggerDecision | str,
    decision: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(trigger, _TriggerDecision):
        reason = trigger.reason
        model_exit_price = trigger.model_exit_price
    else:
        reason = str(trigger)
        model_exit_price = state.trigger_model_exit_price
    token = f"{state.trade_id}:{state.symbol}:{ts_ms}:{price}:{reason}:{decision}"
    return {
        "event_id": blake2b(token.encode("utf-8"), digest_size=12).hexdigest(),
        "paper_trade_id": state.trade_id,
        "symbol": state.symbol,
        "date": str(state.trade.get("date") or _dt_from_ms(ts_ms).date().isoformat()),
        "ts_ms": ts_ms,
        "time": _dt_from_ms(ts_ms).isoformat(),
        "price": price,
        "trigger_price": price,
        "model_exit_price": model_exit_price,
        "reason": reason,
        "decision": decision,
        "order_link_id": str(result.get("order_link_id") or ""),
        "result_status": str(result.get("status") or ""),
        "data_root": str(root),
    }


def _payload(
    now_dt: datetime,
    protection_config: DemoFastProtectionConfig,
    *,
    symbols: list[str],
    states: list[_TradeState],
    events: list[dict[str, Any]],
    latencies_ms: list[float],
    reason: str,
    duplicate_blocks: int = 0,
    submit_blocks: int = 0,
) -> dict[str, Any]:
    submitted = sum(1 for event in events if event.get("result_status") in {"accepted", "submit_unknown"})
    return {
        "now": now_dt.isoformat(),
        "reason": reason,
        "config": asdict(protection_config),
        "symbols": symbols,
        "rows": {
            "active_trades": len(states),
            "symbols": len(symbols),
            "trigger_events": len(events),
            "submitted_or_unknown": submitted,
            "duplicate_blocks": duplicate_blocks,
            "submit_blocks": submit_blocks,
            "callback_count": len(latencies_ms),
        },
        "latency_ms": {
            "p50": _quantile(latencies_ms, 0.50),
            "p95": _quantile(latencies_ms, 0.95),
            "p99": _quantile(latencies_ms, 0.99),
        },
        "events": events[-25:],
    }


def _write_report(root: Path, payload: dict[str, Any]) -> None:
    output_dir = root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "demo_fast_protection_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "demo_fast_protection_report.md").write_text(_format_report(payload), encoding="utf-8")


def _format_report(payload: dict[str, Any]) -> str:
    rows = payload.get("rows", {})
    lines = [
        "# Demo Fast Protection",
        "",
        f"Now: {payload.get('now')}",
        f"Reason: `{payload.get('reason')}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Active trades | {rows.get('active_trades', 0)} |",
        f"| Symbols | {rows.get('symbols', 0)} |",
        f"| Trigger events | {rows.get('trigger_events', 0)} |",
        f"| Submitted or unknown | {rows.get('submitted_or_unknown', 0)} |",
        f"| Duplicate blocks | {rows.get('duplicate_blocks', 0)} |",
        f"| Submit blocks | {rows.get('submit_blocks', 0)} |",
        "",
    ]
    return "\n".join(lines)


def _short_return(entry_price: float, exit_price: float) -> float:
    return (entry_price - exit_price) / max(entry_price, 1e-12)


def _mfe_giveback_stop_price(entry_price: float, best_price: float, giveback_pct: float) -> float:
    max_favorable = max(_short_return(entry_price, best_price), 0.0)
    retained_return = max_favorable * max(0.0, 1.0 - giveback_pct)
    return entry_price * (1.0 - retained_return)


def _quantile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index] if ordered else statistics.median(values)


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _trade_or_default(row: dict[str, Any], key: str, default: float) -> float:
    value = row.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dt_from_ms(ts_ms: int | None) -> datetime:
    return datetime.fromtimestamp(int(ts_ms or 0) / 1000, tz=UTC)
