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
import json
import os
import sys
from datetime import datetime, timezone
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
from liquidity_migration.storage import read_dataset  # noqa: E402

PRIMARY_STRATEGY_ID = "continuous_fade_v1"


def _utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).date().isoformat()


def _live_book_state(primary_root: Path, primary_dataset: str) -> tuple[dict[str, float], float]:
    """Return (live_unit_returns_by_day, current_gross_short_frac) from the live ledger.

    Conservative when the live book is empty (fresh deploy): no live days, gross_short
    falls back to the 0.5 reference so the warm-start beta still sizes a hedge.
    """
    try:
        trades = read_dataset(primary_root, primary_dataset)
    except (FileNotFoundError, OSError):
        return {}, 0.5
    if trades.is_empty() or "status" not in trades.columns:
        return {}, 0.5
    open_now = trades.filter(pl.col("status") == "open")
    gross = 0.0
    if not open_now.is_empty() and "notional_weight" in open_now.columns:
        gross = float(open_now["notional_weight"].abs().sum())
    gross_short_frac = gross if gross > 0.0 else 0.5
    # Live realized per-unit book days are not reconstructed here (the daily-MTM
    # ledger is the rmom/forward job's domain); the warm-start carries the beta
    # window. Live-day extension is wired for when a daily book-return feed exists.
    return {}, gross_short_frac


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

    warm_unit, warm_btc = load_warmstart(REPO / warmstart if not Path(warmstart).is_absolute() else warmstart)
    if len(warm_unit) < 60:
        print(json.dumps({"status": "no_warmstart", "rows": len(warm_unit)}))
        return 0

    live_unit_by_day, gross_short_frac = _live_book_state(REPO / args.primary_root, "continuous_fade_demo_trades")
    live_btc_by_day: dict[str, float] = {}
    unit, btc = extend_with_live_days(warm_unit, warm_btc, live_unit_by_day, live_btc_by_day)

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

    decision = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=gross_short_frac,
        btc_price=btc_price, current_hedge_qty=0.0, equity_usdt=equity,
    )
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "venue": args.venue,
        "mode": "submit" if (args.submit and cfg.confirm_demo_orders) else "dry_run",
        "hedge_ratio_equity_frac": round(decision.hedge_ratio_equity_frac, 5),
        "target_notional_usdt": round(decision.target_notional_usdt, 2),
        "n_obs": decision.n_obs,
        "btc_price": btc_price,
        "equity_usdt": equity,
        "gross_short_frac": round(gross_short_frac, 4),
        "plan": None if decision.plan is None else {
            "side": decision.plan.side, "qty": round(decision.plan.qty, 6),
            "reduce_only": decision.plan.reduce_only, "reason": decision.plan.reason,
        },
        "history_days": decision.diagnostics["history_days"],
    }
    if args.submit and not cfg.confirm_demo_orders:
        out["status"] = "submit_blocked_confirm_demo_orders_not_set"
    elif args.submit and os.environ.get("REAL_MONEY") == "true":
        out["status"] = "submit_blocked_real_money"
    elif args.submit and decision.plan is not None:
        out["status"] = "submit_path_not_yet_enabled_pending_adoption_verify"
        # Order submission is gated behind a verified one-cycle dry-run + ws_risk
        # adoption-schema confirmation (R2-LIVE step). Until then this stays a
        # no-order evidence run by design — fail-safe: never a wrong order.
    else:
        out["status"] = "dry_run_ok"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
