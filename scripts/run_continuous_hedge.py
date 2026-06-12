"""Daily hedge runner for the continuous demo book — banked BTC+ETH two-factor form.

Computes the day's two-leg hedge decision (parity-tested live twin of the Stage-B
engine leg, receipts continuous-hedge-{upgrade,2f-engine}-2026-06-10.md) from the
warm-start series + realized live book days, and either logs it (dry-run, default)
or submits the per-leg resizes through the REST private client into the
continuous-addon ledger root (adopted by the risk service).

Demo only. Dry-run is the safe default; --submit requires CONFIRM_DEMO_ORDERS=1 and
demo credentials (the central order-submit guard hard-refuses REAL_MONEY=true), and
is additionally blocked from ADDING hedge exposure while the warm-start is stale
(all-reduce-only resizes still proceed — trimming a hedge is risk-reducing).
HEDGE_MODE=btc falls back to the single-leg WP3 form.

Exit-code contract (paging): an ARMED (--submit) run that is blocked or fails —
any submit_blocked_* status, submit_failed, or submit_partial — exits NONZERO, so
the daily systemd oneshot lands in `failed` and the liveness watchdog pages the
operator. Dry-run statuses and genuine no-action runs always exit 0. (Before
2026-06-12 a blocked armed run exited 0 and the book sat unhedged silently.)

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
from decimal import Decimal
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
    load_warmstart_2f,
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


def _live_book_state(
    primary_root: Path, primary_dataset: str, cycles_dataset: str | None = None
) -> LiveBookState:
    """Return live unit returns and current gross short exposure from the live ledger.

    Conservative when the live book is empty (fresh deploy): no live days, gross_short
    falls back to the 0.5 reference only when exposure is unknown. A present ledger
    with no open rows is a known flat book and sizes the hedge to zero.

    A trades dataset with NO rows ever is disambiguated via the sibling cycles
    dataset: a sleeve that writes cycle heartbeats but has never traded is
    demonstrably alive AND flat (gross_short=0.0, known). Without this, a
    rebuilt/never-traded book read as "unknown" 0.5 and the armed hedge would BUY
    against a flat book the day it unblocked (audit 2026-06-12).
    """
    if cycles_dataset is None:
        cycles_dataset = primary_dataset.replace("_trades", "_cycles")
    try:
        trades = read_dataset(primary_root, primary_dataset)
    except (FileNotFoundError, OSError):
        return LiveBookState({}, 0.5, False, "ledger_unavailable")
    if trades.is_empty() or "status" not in trades.columns:
        try:
            cycles = read_dataset(primary_root, cycles_dataset)
        except (FileNotFoundError, OSError):
            cycles = None
        if cycles is not None and not cycles.is_empty():
            return LiveBookState({}, 0.0, True, "ledger_present_no_rows")
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
        # Every row's equity_usdt is a snapshot of the SAME netted account at
        # entry, so a row missing it (legacy snipe fills / adopted rows written
        # before the round-3 stamping fixes) borrows the median of the known
        # ones — ONE un-stamped row previously flipped the whole book state to
        # unknown and blocked the armed hedge (solo sweep 2026-06-12 hardening;
        # rows with unknown NOTIONAL still block, that exposure is truly
        # unmeasured).
        known_equities = sorted(
            eq for eq in (_float(row.get("equity_usdt")) for row in rows) if eq > 0.0
        )
        fallback_equity = known_equities[len(known_equities) // 2] if known_equities else 0.0
        gross = 0.0
        valid = 0
        for row in rows:
            notional = abs(_float(row.get("notional_usdt")))
            equity = _float(row.get("equity_usdt")) or fallback_equity
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


# Per-run instrument-filter memo (at most 2 symbols/run; the daily oneshot exits
# after one pass, so this never goes stale).
_INSTRUMENT_FILTERS_CACHE: dict[str, dict[str, float]] = {}


def _instrument_filters(symbol: str, cache_root: Path) -> dict[str, float]:
    """qtyStep / minOrderQty / minNotionalValue for one symbol.

    Reuses the demo engine's cached instruments fetch (``_demo_instruments``:
    normalised Bybit get_instruments_info behind a 1h parquet TTL at
    ``cache_root/.cache/event_demo_instruments``). The continuous demo daemon keeps
    that cache warm at the primary root, so this is usually a local parquet read.
    """
    cached = _INSTRUMENT_FILTERS_CACHE.get(symbol)
    if cached is not None:
        return cached
    from liquidity_migration.bybit import BybitMarketData
    from liquidity_migration.event_demo_data import _demo_instruments

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    instruments = _demo_instruments(BybitMarketData(), cache_root=cache_root, now_ms=now_ms)
    rows = instruments.filter(pl.col("symbol") == symbol).to_dicts()
    row = rows[0] if rows else {}
    out = {
        "qty_step": _float(row.get("qty_step")),
        "min_order_qty": _float(row.get("min_order_qty")),
        "min_notional_value": _float(row.get("min_notional_value")),
    }
    _INSTRUMENT_FILTERS_CACHE[symbol] = out
    return out


def _submit_plan(plan, cfg: ContinuousHedgeConfig, data_root: Path, primary_root: Path, now_ms: int) -> dict:
    """Submit one leg's resize as a Market order and append the ledger rows.

    The central guard (``validate_order_submit_allowed``) has already passed by the
    time this runs; demo credentials are re-asserted here regardless.

    The planner emits a raw notional/price qty; the venue rejects anything off the
    qtyStep grid (planned 0.003348 BTC vs qtyStep 0.001 — audit 2026-06-12), so the
    qty is floored to the symbol's qtyStep first. A floored qty below minOrderQty —
    or, for non-reduce-only legs, a floored notional below minNotionalValue — is
    returned as a ``skipped`` leg instead of a guaranteed venue reject (reduce-only
    legs are exempt from minNotional per Bybit semantics).
    """
    from liquidity_migration.bybit import BybitPrivateClient, resolve_private_credentials
    from liquidity_migration.event_demo import _decimal_text, _order_params

    api_key, api_secret, demo_flag = resolve_private_credentials()
    if not api_key or not api_secret or not demo_flag:
        raise RuntimeError("hedge submit requires DEMO Bybit credentials in env")
    filters = _instrument_filters(plan.symbol, primary_root)
    step = Decimal(str(filters["qty_step"] if filters["qty_step"] > 0.0 else 0.001))
    qty_dec = (Decimal(str(plan.qty)) // step) * step
    qty = float(qty_dec)
    price = abs(plan.delta_notional_usdt) / max(plan.qty, 1e-12)
    skip_base = {"symbol": plan.symbol, "side": plan.side, "qty": qty,
                 "planned_qty": plan.qty, "reduce_only": plan.reduce_only}
    if qty <= 0.0 or (filters["min_order_qty"] > 0.0 and qty < filters["min_order_qty"]):
        return {**skip_base, "skipped": "below_min_qty"}
    if (
        not plan.reduce_only
        and filters["min_notional_value"] > 0.0
        and qty * price < filters["min_notional_value"]
    ):
        return {**skip_base, "skipped": "below_min_notional"}
    client = BybitPrivateClient(category="linear", demo=True, api_key=api_key, api_secret=api_secret)
    link = hedge_order_link_id(now_ms, symbol=plan.symbol)
    qty_text = _decimal_text(qty_dec)
    result = client.place_order(**_order_params(
        symbol=plan.symbol, side=plan.side, qty=qty_text, order_type="Market",
        order_link_id=link, reduce_only=plan.reduce_only,
    ))
    order_id = str(result.get("orderId", ""))
    order_row = {
        "order_link_id": link, "ts_ms": now_ms, "trade_id": f"hedge-{link}",
        "strategy_id": cfg.strategy_id, "symbol": plan.symbol, "side": plan.side,
        "order_type": "Market", "qty": qty, "reduce_only": plan.reduce_only,
        # status "filled" + filled_qty/target_qty: this runner books the market
        # fill itself (trade row below / reduce booking below), so the order row
        # must say so. ws_risk's pending-fill reconciler delta-adds
        # (venue cumulative − filled_qty) onto the trade row; an order row left
        # "submitted" with no filled_qty read previous=0 and re-added the FULL
        # fill — every armed BUY double-booked (audit 2026-06-11).
        "order_id": order_id, "submit_mode": "submitted", "status": "filled",
        "filled_qty": qty, "target_qty": qty,
        "trade_side": "long", "sleeve": "continuous_addon",
        "notional_usdt": abs(qty * price), "reason": plan.reason,
        "updated_at_ms": now_ms,
    }
    write_dataset(pl.DataFrame([order_row]), data_root, cfg.orders_dataset, append=True)
    if plan.side == "Buy" and not plan.reduce_only:
        trade_row = build_hedge_trade_row(
            cfg, qty=qty, entry_price=max(price, 0.0), now_ms=now_ms,
            order_link_id=link, order_id=order_id, symbol=plan.symbol,
        )
        write_dataset(pl.DataFrame([trade_row]), data_root, cfg.trades_dataset, append=True)
    elif plan.side == "Sell" and plan.reduce_only:
        _apply_hedge_reduce_to_trades(
            data_root, cfg, symbol=plan.symbol, sold_qty=qty,
            exit_price=max(price, 0.0), now_ms=now_ms,
        )
    return {"symbol": plan.symbol, "side": plan.side, "qty": qty,
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


def _live_wallet_equity_usdt() -> float:
    """Live demo wallet equity for ARMED sizing (round 4). The hedge previously
    sized every armed leg off the $10k ``fallback_equity_usdt`` constant — as the
    demo account drifts from $10k the armed hedge silently under/over-hedges.
    Raises on any credential/transport failure; the caller blocks the armed run
    with ``submit_blocked_equity_unavailable`` (nonzero exit -> watchdog pages)."""
    from liquidity_migration.bybit import BybitPrivateClient, resolve_private_credentials
    from liquidity_migration.event_demo import wallet_equity_usdt

    api_key, api_secret, demo_flag = resolve_private_credentials()
    if not api_key or not api_secret or not demo_flag:
        raise RuntimeError("hedge equity read requires DEMO Bybit credentials in env")
    client = BybitPrivateClient(category="linear", demo=True, api_key=api_key, api_secret=api_secret)
    return wallet_equity_usdt(client.get_wallet_balance())


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
    # Single loader for all three columns: unit/btc/eth rows are skipped together
    # (iff float(unit_ret) raises), so a malformed unit_ret can never desync the
    # eth column from the unit/btc pair (audit 2026-06-12).
    warm_unit, warm_btc, warm_eth = load_warmstart_2f(warmstart_path)
    if len(warm_unit) < 60:
        print(json.dumps({"status": "no_warmstart", "rows": len(warm_unit)}))
        # An ARMED run with no usable warm-start cannot hedge — fail the oneshot
        # so the watchdog pages; a dry-run stays a quiet no-op.
        return 1 if args.submit else 0
    warmstart_last = _warmstart_last_date(warmstart_path)
    warmstart_age_days = None if warmstart_last is None else (datetime.now(timezone.utc).date() - warmstart_last).days
    warmstart_stale = warmstart_age_days is None or warmstart_age_days > MAX_WARMSTART_STALE_DAYS

    primary_trades_dataset = "continuous_fade_demo_trades"
    live_book = _live_book_state(
        primary_root, primary_trades_dataset, primary_trades_dataset.replace("_trades", "_cycles")
    )
    unit, btc = extend_with_live_days(warm_unit, warm_btc, live_book.live_unit_by_day, {})
    eth: list[float | None] = list(warm_eth) + [None] * max(0, len(unit) - len(warm_eth))
    eth = eth[: len(unit)]

    btc_price = args.btc_price or _latest_close(primary_root, HEDGE_SYMBOL)
    eth_price = args.eth_price or _latest_close(primary_root, HEDGE_SYMBOL_2)
    # Sizing equity: explicit override > live wallet (ARMED runs only) > the
    # $10k fallback constant (dry-run reference). An armed run must never size
    # real legs off the constant (round 4) — a failed wallet read blocks below.
    equity = args.equity_usdt if args.equity_usdt > 0.0 else cfg.fallback_equity_usdt
    equity_source = "override" if args.equity_usdt > 0.0 else "fallback"
    equity_error = ""
    if args.submit and args.equity_usdt <= 0.0:
        try:
            live_equity = _live_wallet_equity_usdt()
        except Exception as exc:  # noqa: BLE001 — surfaced as a blocked status below
            equity_error = f"wallet equity read failed: {exc}"[:300]
        else:
            if live_equity > 0.0:
                equity, equity_source = live_equity, "wallet"
            else:
                equity_error = "wallet equity read returned <= 0"
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
        "equity_source": equity_source,
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
        # Single-leg mode never plans for the ETH leg — an ETH hedge position
        # opened by prior 2f runs would ride unmanaged forever after a mode flip
        # to HEDGE_MODE=btc (audit 2026-06-12 round 3). Surface it; an armed run
        # blocks below until the operator closes/transfers the leg.
        unmanaged_eth_qty = _current_hedge_qty(data_root, cfg.trades_dataset, HEDGE_SYMBOL_2)
        out.update({
            "hedge_ratio_equity_frac": round(single.hedge_ratio_equity_frac, 5),
            "target_notional_usdt": round(single.target_notional_usdt, 2),
            "current_hedge_qty": round(current_hedge_qty, 8),
            "current_notional_usdt": round(single.current_notional_usdt, 2),
            "n_obs": single.n_obs, "plan": _plan_json(single.plan),
            "unmanaged_eth_qty": round(unmanaged_eth_qty, 8),
        })
        plans = [single.plan] if single.plan is not None else []

    if submit_guard_error:
        out["status"] = "submit_blocked_order_submit_guard"
        out["error"] = submit_guard_error
    elif args.submit and equity_source == "fallback":
        # The wallet read failed and no override was passed: leg targets would be
        # sized off the $10k constant against a wallet of unknown size (round 4).
        out["status"] = "submit_blocked_equity_unavailable"
        out["error"] = equity_error or "live wallet equity unavailable"
    elif btc_price <= 0.0:
        # No BTC price (kline store missing/unreadable and no --btc-price): the plan
        # is necessarily None. Without an explicit status this read as a healthy
        # "dry_run_ok"/"submit_no_action" no-op — silently masking a dead input.
        out["status"] = (
            "submit_blocked_btc_price_unavailable" if args.submit else "dry_run_btc_price_unavailable"
        )
    elif args.submit and not live_book.gross_short_frac_known:
        # The 0.5 gross-short default is a sizing REFERENCE, not an observation —
        # never submit orders sized off it (it would buy a hedge against a book
        # whose exposure nobody measured).
        out["status"] = "submit_blocked_book_state_unknown"
    elif args.submit and use_2f and eth_price <= 0.0:
        # In 2f mode a dead ETH price silently drops the ETH leg; an armed run must
        # surface it instead of part-hedging.
        out["status"] = "submit_blocked_eth_price_unavailable"
    elif args.submit and not use_2f and float(out.get("unmanaged_eth_qty") or 0.0) > 0.0:
        # Mode flipped 2f -> single-leg while an ETH hedge leg is still open: the
        # single-leg path would never reduce/close it (round 3). Block + page.
        out["status"] = "submit_blocked_unmanaged_eth_leg"
    elif args.submit and warmstart_stale and any(not p.reduce_only for p in plans):
        # A stale beta window blocks only risk-INCREASING legs; trimming/closing a
        # hedge is risk-reducing and proceeds (all-reduce-only plans fall through).
        out["status"] = "submit_blocked_stale_warmstart"
        out["blocked_legs"] = [_plan_json(p) for p in plans if not p.reduce_only]
        out["would_run_legs"] = [_plan_json(p) for p in plans if p.reduce_only]
    elif args.submit and plans:
        submitted = []
        skipped = []
        errors = []
        for plan in plans:
            try:
                result = _submit_plan(plan, cfg, data_root, primary_root, now_ms)
            except Exception as exc:  # noqa: BLE001 — one leg failing must not kill the other's report
                errors.append({"symbol": plan.symbol, "error": str(exc)[:300]})
                continue
            (skipped if result.get("skipped") else submitted).append(result)
        out["submitted"] = submitted
        out["skipped"] = skipped
        out["submit_errors"] = errors
        if errors:
            out["status"] = "submit_partial" if submitted else "submit_failed"
        elif submitted:
            out["status"] = "submitted"
        else:
            # Every leg fell below the venue's qty-step/min filters: no order was
            # warranted at this size — a no-action run, not a failure.
            out["status"] = "submit_no_action"
    elif args.submit:
        out["status"] = "submit_no_action"
    elif use_2f and eth_price <= 0.0:
        out["status"] = "dry_run_eth_price_unavailable"
    elif warmstart_stale:
        out["status"] = "dry_run_stale_warmstart"
    else:
        out["status"] = "dry_run_ok"
    print(json.dumps(out))
    # SUCCESS notify (round 4): blocks/failures already page via the nonzero-exit
    # watchdog contract below, but real submitted hedge legs (live Market orders
    # on the demo account) were silent — the first-ever hedge submission is
    # exactly the event the operator needs to see. Best-effort and exception-
    # isolated; the send can never change the exit code.
    if out.get("status") in {"submitted", "submit_partial"}:
        try:
            from liquidity_migration.telegram import send_telegram_message

            legs = "; ".join(
                f"{r.get('side')} {r.get('qty')} {r.get('symbol')}" for r in out.get("submitted") or []
            )
            errors = out.get("submit_errors") or []
            message = (
                f"hedge {out['status']} ({out.get('hedge_mode')}): {legs or 'no legs'}"
                f" | equity ${equity:,.0f} ({equity_source})"
                + (f" | {len(errors)} leg error(s)" if errors else "")
            )
            send_telegram_message(message, enabled=True)
        except Exception as exc:  # noqa: BLE001 — notify must never mask the submit result
            print(f"hedge telegram notify failed: {exc}", file=sys.stderr)
    # Paging contract: a blocked or failed ARMED run exits nonzero so the systemd
    # oneshot lands `failed` and the liveness watchdog pages (see module docstring).
    failing_statuses = {
        "submit_blocked_order_submit_guard",
        "submit_blocked_equity_unavailable",
        "submit_blocked_btc_price_unavailable",
        "submit_blocked_book_state_unknown",
        "submit_blocked_eth_price_unavailable",
        "submit_blocked_unmanaged_eth_leg",
        "submit_blocked_stale_warmstart",
        "submit_failed",
        "submit_partial",
    }
    return 1 if args.submit and out["status"] in failing_statuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
