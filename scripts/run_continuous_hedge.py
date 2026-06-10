"""Daily BTC-beta hedge runner for the continuous demo book (WP3 live wiring).

Computes the day's hedge decision from the warm-start series + realized live book days
and either logs it (dry-run, default) or submits the resize through the REST private
client into the continuous-addon ledger root (adopted by the risk service).

Demo only. Dry-run is the safe default; --submit requires CONFIRM_DEMO_ORDERS=1 and
DEMO=true / REAL_MONEY!=true.

Usage:
    .venv/bin/python scripts/run_continuous_hedge.py --venue bybit            # dry-run
    SUBMIT_HEDGE=1 .venv/bin/python scripts/run_continuous_hedge.py --venue bybit --submit
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

from liquidity_migration.continuous_hedge_manager import (  # noqa: E402
    HEDGE_SYMBOL,
    ContinuousHedgeConfig,
    compute_hedge_decision,
    extend_with_live_days,
    load_warmstart,
)
from liquidity_migration.bybit import validate_order_submit_allowed  # noqa: E402
from liquidity_migration.storage import read_dataset  # noqa: E402

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


def _current_hedge_qty(data_root: Path, trades_dataset: str) -> float:
    try:
        trades = read_dataset(data_root, trades_dataset)
    except (FileNotFoundError, OSError):
        return 0.0
    if trades.is_empty() or "status" not in trades.columns or "symbol" not in trades.columns:
        return 0.0
    open_rows = trades.filter(
        (pl.col("status") == "open")
        & (pl.col("symbol") == HEDGE_SYMBOL)
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit", choices=["bybit", "binance"])
    ap.add_argument("--data-root", default="data/bybit-continuous-hedge-event")
    ap.add_argument("--primary-root", default="data/bybit-continuous-demo-event")
    ap.add_argument("--warmstart", default="")
    ap.add_argument("--btc-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument("--equity-usdt", type=float, default=0.0, help="override; else fallback")
    ap.add_argument("--submit", action="store_true")
    args = ap.parse_args()

    warmstart = args.warmstart or f"deploy/hedge_warmstart/{args.venue}_warmstart.csv"
    cfg = ContinuousHedgeConfig(
        data_root=args.data_root,
        warmstart_csv=warmstart,
        submit_orders=bool(args.submit),
        confirm_demo_orders=os.environ.get("CONFIRM_DEMO_ORDERS") == "1",
    )

    warmstart_path = REPO / warmstart if not Path(warmstart).is_absolute() else Path(warmstart)
    warm_unit, warm_btc = load_warmstart(warmstart_path)
    if len(warm_unit) < 60:
        print(json.dumps({"status": "no_warmstart", "rows": len(warm_unit)}))
        return 0
    warmstart_last = _warmstart_last_date(warmstart_path)
    warmstart_age_days = None if warmstart_last is None else (datetime.now(timezone.utc).date() - warmstart_last).days
    warmstart_stale = warmstart_age_days is None or warmstart_age_days > MAX_WARMSTART_STALE_DAYS

    live_book = _live_book_state(REPO / args.primary_root, "continuous_fade_demo_trades")
    live_btc_by_day: dict[str, float] = {}
    unit, btc = extend_with_live_days(warm_unit, warm_btc, live_book.live_unit_by_day, live_btc_by_day)

    btc_price = args.btc_price
    if btc_price <= 0.0:
        store = REPO / args.primary_root / ".cache" / "ws_klines" / "store.parquet"
        if store.exists():
            try:
                df = pl.read_parquet(store).filter(pl.col("symbol") == HEDGE_SYMBOL).sort("ts_ms")
                if not df.is_empty():
                    btc_price = float(df["close"][-1])
            except (OSError, pl.exceptions.PolarsError):
                pass
    equity = args.equity_usdt if args.equity_usdt > 0.0 else cfg.fallback_equity_usdt
    current_hedge_qty = _current_hedge_qty(REPO / args.data_root, cfg.trades_dataset)
    submit_guard_error = ""
    if args.submit:
        try:
            validate_order_submit_allowed(
                submit_orders=True,
                confirm_demo_orders=cfg.confirm_demo_orders,
            )
        except RuntimeError as exc:
            submit_guard_error = str(exc)

    decision = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=live_book.gross_short_frac,
        btc_price=btc_price, current_hedge_qty=current_hedge_qty, equity_usdt=equity,
    )
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "venue": args.venue,
        "mode": "submit" if args.submit else "dry_run",
        "hedge_ratio_equity_frac": round(decision.hedge_ratio_equity_frac, 5),
        "target_notional_usdt": round(decision.target_notional_usdt, 2),
        "current_hedge_qty": round(current_hedge_qty, 8),
        "current_notional_usdt": round(decision.current_notional_usdt, 2),
        "n_obs": decision.n_obs,
        "btc_price": btc_price,
        "equity_usdt": equity,
        "gross_short_frac": round(live_book.gross_short_frac, 4),
        "gross_short_frac_known": live_book.gross_short_frac_known,
        "gross_short_frac_source": live_book.gross_short_frac_source,
        "warmstart_last_date": None if warmstart_last is None else warmstart_last.isoformat(),
        "warmstart_age_days": warmstart_age_days,
        "warmstart_stale": warmstart_stale,
        "plan": None if decision.plan is None else {
            "side": decision.plan.side, "qty": round(decision.plan.qty, 6),
            "reduce_only": decision.plan.reduce_only, "reason": decision.plan.reason,
        },
        "history_days": decision.diagnostics["history_days"],
    }
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
    elif args.submit and decision.plan is not None:
        out["status"] = "submit_path_not_yet_enabled_pending_adoption_verify"
        # Order submission is gated behind a verified one-cycle dry-run + ws_risk
        # adoption-schema confirmation (R2-LIVE step). Until then this stays a
        # no-order evidence run by design — fail-safe: never a wrong order.
    elif args.submit:
        out["status"] = "submit_no_action"
    elif warmstart_stale:
        out["status"] = "dry_run_stale_warmstart"
    else:
        out["status"] = "dry_run_ok"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
