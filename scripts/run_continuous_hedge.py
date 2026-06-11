"""Daily hedge runner for the continuous demo book — banked BTC+ETH two-factor form.

Computes the day's two-leg hedge decision (parity-tested live twin of the Stage-B
engine leg, receipts continuous-hedge-{upgrade,2f-engine}-2026-06-10.md) from the
warm-start series + realized live book days, and either logs it (dry-run, default)
or submits the per-leg resizes through the REST private client into the
continuous-addon ledger root (adopted by the risk service).

Demo only. Dry-run is the safe default; --submit requires CONFIRM_DEMO_ORDERS=1 and
demo credentials (the central order-submit guard hard-refuses REAL_MONEY=true), and
is additionally blocked while the warm-start is stale. HEDGE_MODE=btc falls back to
the single-leg WP3 form.

Usage:
    .venv/bin/python scripts/run_continuous_hedge.py --venue bybit            # dry-run
    SUBMIT_HEDGE=1 CONFIRM_DEMO_ORDERS=1 .venv/bin/python scripts/run_continuous_hedge.py --venue bybit --submit
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.bybit import validate_order_submit_allowed  # noqa: E402
from liquidity_migration.continuous_hedge_manager import (  # noqa: E402
    HEDGE_SYMBOL,
    HEDGE_SYMBOL_2,
    ContinuousHedgeConfig,
    HedgeDecision2F,
    build_hedge_trade_row,
    compute_hedge_decision,
    compute_hedge_decision_2f,
    extend_with_live_days,
    hedge_order_link_id,
    load_warmstart,
)
from liquidity_migration.storage import read_dataset, write_dataset  # noqa: E402

PRIMARY_STRATEGY_ID = "continuous_fade_v1"
MAX_WARMSTART_STALE_DAYS = 3


@dataclass(frozen=True, slots=True)
class LiveBookState:
    live_unit_by_day: dict[str, float]
    gross_short_frac: float
    gross_short_frac_known: bool
    gross_short_frac_source: str


def _utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).date().isoformat()


def _float(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def _is_short_trade(row: dict[str, object]) -> bool:
    side = str(row.get("side") or row.get("trade_side") or "").strip().lower()
    if side in {"long", "buy"}:
        return False
    return side in {"", "short", "sell"}


def _is_long_hedge_trade(row: dict[str, object]) -> bool:
    side = str(row.get("side") or row.get("trade_side") or "").strip().lower()
    return side in {"long", "buy"}


def _live_book_state(primary_root: Path, primary_dataset: str) -> LiveBookState:
    """Return live unit returns and current gross short exposure from the live ledger.

    Conservative when the live book is empty (fresh deploy): no live days, gross_short
    falls back to the 0.5 reference only when exposure is unknown. A present ledger
    with no open rows is a known flat book and sizes the hedge to zero.
    """
    try:
        trades = read_dataset(primary_root, primary_dataset)
    except (FileNotFoundError, OSError):
        return LiveBookState({}, 0.5, False, "ledger_unavailable")
    if trades.is_empty() or "status" not in trades.columns:
        return LiveBookState({}, 0.5, False, "ledger_empty_or_missing_status")
    open_now = trades.filter(pl.col("status") == "open")
    if open_now.is_empty():
        return LiveBookState({}, 0.0, True, "flat")
    rows = [row for row in open_now.to_dicts() if _is_short_trade(row)]
    if not rows:
        return LiveBookState({}, 0.0, True, "no_open_shorts")
    if "notional_weight" in open_now.columns:
        gross = sum(abs(_float(row.get("notional_weight"))) for row in rows)
        if gross > 0.0:
            return LiveBookState({}, gross, True, "notional_weight")
    if "notional_usdt" in open_now.columns and "equity_usdt" in open_now.columns:
        gross = 0.0
        valid = 0
        for row in rows:
            notional = abs(_float(row.get("notional_usdt")))
            equity = _float(row.get("equity_usdt"))
            if notional <= 0.0 or equity <= 0.0:
                continue
            gross += notional / equity
            valid += 1
        if valid == len(rows):
            return LiveBookState({}, gross, True, "notional_over_equity")
        if valid > 0:
            return LiveBookState({}, 0.5, False, "partial_notional_over_equity")
    gross_short_frac = 0.5
    # Live realized per-unit book days are not reconstructed here (the daily-MTM
    # ledger is the rmom/forward job's domain); the warm-start carries the beta
    # window. Live-day extension is wired for when a daily book-return feed exists.
    return LiveBookState({}, gross_short_frac, False, "unknown")


def _current_hedge_qty(data_root: Path, trades_dataset: str, symbol: str = HEDGE_SYMBOL) -> float:
    try:
        trades = read_dataset(data_root, trades_dataset)
    except (FileNotFoundError, OSError):
        return 0.0
    if trades.is_empty() or "status" not in trades.columns or "symbol" not in trades.columns:
        return 0.0
    open_rows = trades.filter(
        (pl.col("status") == "open")
        & (pl.col("symbol") == symbol)
    )
    qty = 0.0
    for row in open_rows.to_dicts():
        if _is_long_hedge_trade(row):
            qty += abs(_float(row.get("qty")))
    return qty


def _warmstart_last_date(path: Path) -> date | None:
    if not path.exists():
        return None
    last: date | None = None
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = row.get("date")
            if not raw:
                continue
            try:
                last = date.fromisoformat(raw)
            except ValueError:
                continue
    return last


def _load_warmstart_eth(path: Path) -> list[float | None]:
    """The optional eth_ret column, aligned to the warm-start rows (None when absent).

    Kept separate from ``load_warmstart`` so the (unit, btc) loading path — and its
    test seams — are unchanged; a warm-start without eth_ret simply yields no joint
    observations and the runner falls back to the single-leg BTC form.
    """
    out: list[float | None] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("unit_ret") in (None, ""):
                continue
            v = row.get("eth_ret")
            try:
                out.append(float(v) if v not in (None, "") else None)
            except (TypeError, ValueError):
                out.append(None)
    return out


def _latest_close(primary_root: Path, symbol: str) -> float:
    store = primary_root / ".cache" / "ws_klines" / "store.parquet"
    if not store.exists():
        return 0.0
    try:
        df = pl.read_parquet(store).filter(pl.col("symbol") == symbol).sort("ts_ms")
        return float(df["close"][-1]) if not df.is_empty() else 0.0
    except (OSError, pl.exceptions.PolarsError):
        return 0.0


def _plan_json(plan) -> dict | None:
    if plan is None:
        return None
    return {"symbol": plan.symbol, "side": plan.side, "qty": round(plan.qty, 6),
            "reduce_only": plan.reduce_only, "reason": plan.reason,
            "delta_notional_usdt": round(plan.delta_notional_usdt, 2)}


def _submit_plan(plan, cfg: ContinuousHedgeConfig, data_root: Path, now_ms: int) -> dict:
    """Submit one leg's resize as a Market order and append the ledger rows.

    The central guard (``validate_order_submit_allowed``) has already passed by the
    time this runs; demo credentials are re-asserted here regardless.
    """
    from liquidity_migration.bybit import BybitPrivateClient, resolve_private_credentials
    from liquidity_migration.event_demo import _order_params

    api_key, api_secret, demo_flag = resolve_private_credentials()
    if not api_key or not api_secret or not demo_flag:
        raise RuntimeError("hedge submit requires DEMO Bybit credentials in env")
    client = BybitPrivateClient(category="linear", demo=True, api_key=api_key, api_secret=api_secret)
    link = hedge_order_link_id(now_ms, symbol=plan.symbol)
    qty_text = f"{plan.qty:.6f}".rstrip("0").rstrip(".")
    result = client.place_order(**_order_params(
        symbol=plan.symbol, side=plan.side, qty=qty_text, order_type="Market",
        order_link_id=link, reduce_only=plan.reduce_only,
    ))
    order_id = str(result.get("orderId", ""))
    order_row = {
        "order_link_id": link, "ts_ms": now_ms, "trade_id": f"hedge-{link}",
        "strategy_id": cfg.strategy_id, "symbol": plan.symbol, "side": plan.side,
        "order_type": "Market", "qty": plan.qty, "reduce_only": plan.reduce_only,
        # status "filled" + filled_qty/target_qty: this runner books the market
        # fill itself (trade row below / reduce booking below), so the order row
        # must say so. ws_risk's pending-fill reconciler delta-adds
        # (venue cumulative − filled_qty) onto the trade row; an order row left
        # "submitted" with no filled_qty read previous=0 and re-added the FULL
        # fill — every armed BUY double-booked (audit 2026-06-11).
        "order_id": order_id, "submit_mode": "submitted", "status": "filled",
        "filled_qty": plan.qty, "target_qty": plan.qty,
        "trade_side": "long", "sleeve": "continuous_addon",
        "notional_usdt": abs(plan.delta_notional_usdt), "reason": plan.reason,
        "updated_at_ms": now_ms,
    }
    write_dataset(pl.DataFrame([order_row]), data_root, cfg.orders_dataset, append=True)
    price = abs(plan.delta_notional_usdt) / max(plan.qty, 1e-12)
    if plan.side == "Buy" and not plan.reduce_only:
        trade_row = build_hedge_trade_row(
            cfg, qty=plan.qty, entry_price=max(price, 0.0), now_ms=now_ms,
            order_link_id=link, order_id=order_id, symbol=plan.symbol,
        )
        write_dataset(pl.DataFrame([trade_row]), data_root, cfg.trades_dataset, append=True)
    elif plan.side == "Sell" and plan.reduce_only:
        _apply_hedge_reduce_to_trades(
            data_root, cfg, symbol=plan.symbol, sold_qty=plan.qty,
            exit_price=max(price, 0.0), now_ms=now_ms,
        )
    return {"symbol": plan.symbol, "side": plan.side, "qty": plan.qty,
            "reduce_only": plan.reduce_only, "order_id": order_id, "link": link}


def _apply_hedge_reduce_to_trades(
    data_root: Path, cfg: ContinuousHedgeConfig, *, symbol: str,
    sold_qty: float, exit_price: float, now_ms: int,
) -> None:
    """Book a reduce-only Sell against the open hedge trade rows, oldest-first.

    Until 2026-06-11 reduces never touched the trade rows at all (the Sell's order
    row keys trade_id 'hedge-{sell-link}', matching no trade row, so ws_risk's
    reduce branch no-ops): _current_hedge_qty then overstated the live hedge and
    the planner re-sold the phantom excess daily until the venue went flat —
    leaving the book unhedged while the ledger showed an open hedge."""
    try:
        trades = read_dataset(data_root, cfg.trades_dataset)
    except (FileNotFoundError, OSError):
        return
    if trades.is_empty() or "status" not in trades.columns or "symbol" not in trades.columns:
        return
    open_rows = [
        row
        for row in (
            trades.filter((pl.col("status") == "open") & (pl.col("symbol") == symbol))
            .sort("entry_ts_ms")
            .to_dicts()
        )
        if _is_long_hedge_trade(row)
    ]
    remaining = float(sold_qty)
    updates: list[dict] = []
    for row in open_rows:
        if remaining <= 1e-12:
            break
        row_qty = abs(_float(row.get("qty")))
        take = min(row_qty, remaining)
        remaining -= take
        upd = dict(row)
        upd["updated_at_ms"] = now_ms
        if take >= row_qty - 1e-12:
            upd.update({
                "status": "closed", "exit_price": float(exit_price),
                "exit_ts_ms": now_ms, "exit_reason": "hedge_reduce",
            })
        else:
            upd["qty"] = row_qty - take
        updates.append(upd)
    if remaining > 1e-9:
        print(f"WARN: hedge reduce sold {sold_qty:.6f} {symbol} but the ledger held "
              f"only {sold_qty - remaining:.6f} open — venue/ledger drift, inspect the addon ledger.")
    if updates:
        write_dataset(pl.DataFrame(updates, infer_schema_length=None), data_root, cfg.trades_dataset, append=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit", choices=["bybit", "binance"])
    ap.add_argument("--data-root", default="data/bybit-continuous-hedge-event")
    ap.add_argument("--primary-root", default="data/bybit-continuous-demo-event")
    ap.add_argument("--warmstart", default="")
    ap.add_argument("--btc-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument("--eth-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument("--equity-usdt", type=float, default=0.0, help="override; else fallback")
    ap.add_argument("--submit", action="store_true")
    args = ap.parse_args()

    warmstart = args.warmstart or f"deploy/hedge_warmstart/{args.venue}_warmstart.csv"
    hedge_mode = os.environ.get("HEDGE_MODE", "2f")
    cfg = ContinuousHedgeConfig(
        data_root=args.data_root,
        warmstart_csv=warmstart,
        hedge_mode=hedge_mode,
        submit_orders=bool(args.submit),
        confirm_demo_orders=os.environ.get("CONFIRM_DEMO_ORDERS") == "1",
    )
    data_root = REPO / args.data_root
    primary_root = REPO / args.primary_root

    warmstart_path = REPO / warmstart if not Path(warmstart).is_absolute() else Path(warmstart)
    warm_unit, warm_btc = load_warmstart(warmstart_path)
    if len(warm_unit) < 60:
        print(json.dumps({"status": "no_warmstart", "rows": len(warm_unit)}))
        return 0
    warmstart_last = _warmstart_last_date(warmstart_path)
    warmstart_age_days = None if warmstart_last is None else (datetime.now(timezone.utc).date() - warmstart_last).days
    warmstart_stale = warmstart_age_days is None or warmstart_age_days > MAX_WARMSTART_STALE_DAYS

    live_book = _live_book_state(primary_root, "continuous_fade_demo_trades")
    unit, btc = extend_with_live_days(warm_unit, warm_btc, live_book.live_unit_by_day, {})
    warm_eth = _load_warmstart_eth(warmstart_path)
    eth: list[float | None] = list(warm_eth) + [None] * max(0, len(unit) - len(warm_eth))
    eth = eth[: len(unit)]

    btc_price = args.btc_price or _latest_close(primary_root, HEDGE_SYMBOL)
    eth_price = args.eth_price or _latest_close(primary_root, HEDGE_SYMBOL_2)
    equity = args.equity_usdt if args.equity_usdt > 0.0 else cfg.fallback_equity_usdt
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    submit_guard_error = ""
    if args.submit:
        try:
            validate_order_submit_allowed(
                submit_orders=True,
                confirm_demo_orders=cfg.confirm_demo_orders,
            )
        except RuntimeError as exc:
            submit_guard_error = str(exc)

    n_eth = sum(1 for e in warm_eth if e is not None)
    use_2f = hedge_mode == "2f" and n_eth >= 60
    out: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "venue": args.venue,
        "hedge_mode": "2f" if use_2f else "btc",
        "mode": "submit" if args.submit else "dry_run",
        "btc_price": btc_price, "eth_price": eth_price, "equity_usdt": equity,
        "gross_short_frac": round(live_book.gross_short_frac, 4),
        "gross_short_frac_known": live_book.gross_short_frac_known,
        "gross_short_frac_source": live_book.gross_short_frac_source,
        "warmstart_last_date": None if warmstart_last is None else warmstart_last.isoformat(),
        "warmstart_age_days": warmstart_age_days,
        "warmstart_stale": warmstart_stale,
        "history_days": len(unit),
    }
    plans = []
    if use_2f:
        decision: HedgeDecision2F = compute_hedge_decision_2f(
            cfg, unit_returns=unit, btc_returns=btc, eth_returns=eth,
            live_gross_short_frac=live_book.gross_short_frac, btc_price=btc_price, eth_price=eth_price,
            current_btc_qty=_current_hedge_qty(data_root, cfg.trades_dataset),
            current_eth_qty=_current_hedge_qty(data_root, cfg.trades_dataset, HEDGE_SYMBOL_2),
            equity_usdt=equity,
        )
        out.update({
            "ratio_btc": round(decision.ratio_btc, 5), "ratio_eth": round(decision.ratio_eth, 5),
            "target_btc_usdt": round(decision.target_btc_usdt, 2),
            "target_eth_usdt": round(decision.target_eth_usdt, 2),
            "n_obs_joint": decision.n_obs_joint, "fell_back_to_btc": decision.fell_back_to_btc,
            "plan_btc": _plan_json(decision.plan_btc), "plan_eth": _plan_json(decision.plan_eth),
        })
        plans = [p for p in (decision.plan_btc, decision.plan_eth) if p is not None]
    else:
        current_hedge_qty = _current_hedge_qty(data_root, cfg.trades_dataset)
        single = compute_hedge_decision(
            cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=live_book.gross_short_frac,
            btc_price=btc_price, current_hedge_qty=current_hedge_qty, equity_usdt=equity,
        )
        out.update({
            "hedge_ratio_equity_frac": round(single.hedge_ratio_equity_frac, 5),
            "target_notional_usdt": round(single.target_notional_usdt, 2),
            "current_hedge_qty": round(current_hedge_qty, 8),
            "current_notional_usdt": round(single.current_notional_usdt, 2),
            "n_obs": single.n_obs, "plan": _plan_json(single.plan),
        })
        plans = [single.plan] if single.plan is not None else []

    if submit_guard_error:
        out["status"] = "submit_blocked_order_submit_guard"
        out["error"] = submit_guard_error
    elif btc_price <= 0.0:
        # No BTC price (kline store missing/unreadable and no --btc-price): the plan
        # is necessarily None. Without an explicit status this read as a healthy
        # "dry_run_ok"/"submit_no_action" no-op — silently masking a dead input.
        out["status"] = (
            "submit_blocked_btc_price_unavailable" if args.submit else "dry_run_btc_price_unavailable"
        )
    elif args.submit and warmstart_stale:
        out["status"] = "submit_blocked_stale_warmstart"
    elif args.submit and plans:
        submitted = []
        errors = []
        for plan in plans:
            try:
                submitted.append(_submit_plan(plan, cfg, data_root, now_ms))
            except Exception as exc:  # noqa: BLE001 — one leg failing must not kill the other's report
                errors.append({"symbol": plan.symbol, "error": str(exc)[:300]})
        out["submitted"] = submitted
        out["submit_errors"] = errors
        out["status"] = "submitted" if submitted and not errors else ("submit_partial" if submitted else "submit_failed")
    elif args.submit:
        out["status"] = "submit_no_action"
    elif btc_price <= 0.0:
        # No BTC price (kline store missing/unreadable and no --btc-price): the plan
        # is None for a dead-input reason — surface it, never a healthy-looking no-op.
        out["status"] = "dry_run_btc_price_unavailable"
    elif use_2f and eth_price <= 0.0:
        out["status"] = "dry_run_eth_price_unavailable"
    elif warmstart_stale:
        out["status"] = "dry_run_stale_warmstart"
    else:
        out["status"] = "dry_run_ok"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
