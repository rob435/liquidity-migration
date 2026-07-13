"""Periodic target-hedge runner for the continuous demo book — BTC+ETH two-factor form.

Computes the current two-leg hedge target from the warm-start series and the
canonical account state. Dry-run prints the decision; ``--submit`` publishes an
absolute target batch to the single account owner. This process never calls the
venue private API and never writes a compatibility trade ledger.

Demo only. Dry-run is the safe default. Publishing is blocked from increasing
hedge exposure while the warm-start is stale; risk-reducing targets still proceed.
HEDGE_MODE=btc falls back to the single-leg WP3 form.

Exit-code contract (paging): an ARMED (``--submit``) run that is blocked or whose
target publication fails exits NONZERO, so
the systemd oneshot lands in `failed` and the liveness watchdog pages the
operator. Dry-run statuses and genuine no-action runs always exit 0. (Before
2026-06-12 a blocked armed run exited 0 and the book sat unhedged silently.)

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

from liquidity_migration.account_intent_client import (  # noqa: E402
    AccountTargetPublisher,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account_service import SleeveAdapterKind  # noqa: E402
from liquidity_migration.account_route import AccountRoute, require_account_route  # noqa: E402
from liquidity_migration.account_owner_health import (  # noqa: E402
    require_recent_account_owner_health,
)
from liquidity_migration.account_strategy_state import (  # noqa: E402
    canonical_strategy_trade_rows,
    target_reservation_rows,
)
from liquidity_migration.continuous_hedge_manager import (  # noqa: E402
    HEDGE_SYMBOL,
    HEDGE_SYMBOL_2,
    ContinuousHedgeConfig,
    HedgeDecision2F,
    compute_hedge_decision,
    compute_hedge_decision_2f,
    load_warmstart_2f,
)

MAX_WARMSTART_STALE_DAYS = 3


@dataclass(frozen=True, slots=True)
class LiveBookState:
    gross_short_frac: float
    gross_short_frac_known: bool
    gross_short_frac_source: str


@dataclass(frozen=True, slots=True)
class HedgeDesiredTarget:
    """Absolute account-kernel target; never an order-side instruction."""

    symbol: str
    target_notional_usdt: float
    reason: str
    plan: object | None = None


def _publish_hedge_target_batch(
    targets: list[HedgeDesiredTarget],
    *,
    cfg: ContinuousHedgeConfig,
    route: AccountRoute,
    now_ms: int,
    leverage: float,
) -> dict[str, object]:
    """Publish all hedge legs atomically to the single account owner."""

    batch_id = f"hedge-target/{cfg.strategy_id}/{now_ms}"
    intents = []
    queued_targets: list[dict[str, object]] = []
    for target in sorted(targets, key=lambda item: item.symbol):
        symbol = target.symbol.upper()
        component_id = symbol.removesuffix("USDT").lower()
        target_key = component_target_key(
            sleeve=SleeveAdapterKind.HEDGE,
            strategy_id=cfg.strategy_id,
            component_id=component_id,
            symbol=symbol,
        )
        plan = target.plan
        metadata = {
            "source": "continuous_hedge_target_runner",
            "target_notional_usdt": float(target.target_notional_usdt),
        }
        if plan is not None:
            metadata.update(
                {
                    "planner_side": str(getattr(plan, "side", "")),
                    "planner_reduce_only": bool(getattr(plan, "reduce_only", False)),
                    "planner_delta_notional_usdt": float(getattr(plan, "delta_notional_usdt", 0.0)),
                }
            )
        intents.append(
            requested_target(
                adapter_kind=SleeveAdapterKind.HEDGE,
                decision_key=f"{batch_id}/{symbol}",
                target_key=target_key,
                strategy_id=cfg.strategy_id,
                component_id=component_id,
                symbol=symbol,
                signed_notional_usdt=max(float(target.target_notional_usdt), 0.0),
                leverage=leverage,
                reason=target.reason,
                metadata=metadata,
            )
        )
        queued_targets.append(
            {
                "symbol": symbol,
                "target_notional_usdt": max(float(target.target_notional_usdt), 0.0),
                "target_key": target_key,
            }
        )
    publisher = AccountTargetPublisher(route)
    exits = [
        intent
        for intent in intents
        if float(intent.intent.signed_notional_usdt) == 0.0
    ]
    nonzero = [
        intent
        for intent in intents
        if float(intent.intent.signed_notional_usdt) != 0.0
    ]
    publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix=batch_id,
        exit_intents=exits,
        entry_intents=nonzero,
        created_ts_ns=now_ms * 1_000_000,
    )
    if publication.errors:
        details = "; ".join(
            f"{item.stage}:{item.target_key}:{item.error_type}:{item.message}"
            for item in publication.errors
        )
        raise OSError(f"hedge target publication failed: {details}")
    request_ids = [*publication.exit_request_ids]
    if publication.entry_request_id:
        request_ids.append(publication.entry_request_id)
    if not request_ids:
        raise RuntimeError("hedge target publication produced no durable request")
    return {
        "request_id": request_ids[-1],
        "request_ids": request_ids,
        "batch_id": batch_id,
        "target_count": len(intents),
        "targets": queued_targets,
    }


def _float(value: object, default: float = 0.0) -> float:
    try:
        out = float(str(value))
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def _current_account_hedge_qty(
    account_root: Path,
    *,
    strategy_id: str,
    symbol: str = HEDGE_SYMBOL,
) -> float:
    """Read the accepted hedge target, never a sleeve compatibility ledger."""

    rows = canonical_strategy_trade_rows(
        account_root,
        sleeve=SleeveAdapterKind.HEDGE.value,
        strategy_ids=(strategy_id,),
    )
    reserved = target_reservation_rows(rows)
    if reserved.is_empty():
        return 0.0
    qty = 0.0
    for row in reserved.filter(pl.col("symbol") == symbol).to_dicts():
        qty += max(_float(row.get("signed_qty")), 0.0)
    return qty


def _pending_account_hedge_symbols(
    account_root: Path,
    *,
    strategy_id: str,
) -> set[str]:
    """Accepted hedge targets that must not receive an unchanged refresh."""

    rows = canonical_strategy_trade_rows(
        account_root,
        sleeve=SleeveAdapterKind.HEDGE.value,
        strategy_ids=(strategy_id,),
    )
    if rows.is_empty() or "status" not in rows.columns or "symbol" not in rows.columns:
        return set()
    return {
        str(symbol).upper()
        for symbol in rows.filter(pl.col("status") == "target_pending")["symbol"].to_list()
    }


def _account_continuous_book_state(
    account_root: Path,
    *,
    equity_usdt: float,
) -> LiveBookState:
    """Size hedge exposure from canonical CONTINUOUS targets.

    Realized daily return extension remains a research-data concern, but open
    gross cannot come from the disabled sleeve ledger during account cutover.
    """

    rows = canonical_strategy_trade_rows(
        account_root,
        sleeve=SleeveAdapterKind.CONTINUOUS.value,
    )
    reserved = target_reservation_rows(rows)
    short_targets = (
        reserved.filter(pl.col("side") == "short")
        if not reserved.is_empty()
        else reserved
    )
    gross_notional = sum(
        abs(_float(row.get("notional_usdt")))
        for row in short_targets.to_dicts()
    )
    if equity_usdt <= 0.0:
        return LiveBookState(0.0, False, "account_target_equity_unavailable")
    return LiveBookState(
        gross_notional / equity_usdt,
        True,
        "account_kernel_desired_targets",
    )


def _warmstart_last_date(path: Path) -> date | None:
    if not path.exists():
        return None
    last_observation: date | None = None
    data_through: date | None = None
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            for key, current in (("date", last_observation), ("data_through_date", data_through)):
                raw = row.get(key)
                if not raw:
                    continue
                try:
                    parsed = date.fromisoformat(raw)
                except ValueError:
                    continue
                if current is None or parsed > current:
                    if key == "date":
                        last_observation = parsed
                    else:
                        data_through = parsed
    # New tapes carry the validated signal-data boundary.  Fall back to the
    # latest unit observation for legacy four-column CSVs.
    return data_through or last_observation


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
    return {
        "symbol": plan.symbol,
        "side": plan.side,
        "qty": round(plan.qty, 6),
        "reduce_only": plan.reduce_only,
        "reason": plan.reason,
        "current_notional_usdt": round(plan.current_notional_usdt, 2),
        "target_notional_usdt": round(plan.target_notional_usdt, 2),
        "delta_notional_usdt": round(plan.delta_notional_usdt, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="bybit", choices=["bybit", "binance"])
    ap.add_argument("--primary-root", default="data/bybit-continuous-demo-event")
    ap.add_argument("--warmstart", default="")
    ap.add_argument("--btc-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument("--eth-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument(
        "--equity-usdt",
        type=float,
        default=0.0,
        help="explicit override; otherwise use the latest canonical account observation",
    )
    ap.add_argument("--submit", action="store_true")
    ap.add_argument(
        "--account-inbox-root",
        default=os.environ.get("ACCOUNT_INTENT_INBOX_ROOT", ""),
        help="canonical account-owner target inbox",
    )
    ap.add_argument(
        "--account-root",
        default=os.environ.get("ACCOUNT_EXECUTION_ROOT", ""),
        help="canonical account journal used for hedge and primary-book planning state",
    )
    ap.add_argument(
        "--account-health-max-age-seconds",
        type=float,
        default=30.0,
        help="maximum age of the demo account owner's healthy capital observation",
    )
    args = ap.parse_args()

    if args.account_health_max_age_seconds <= 0.0:
        ap.error("--account-health-max-age-seconds must be positive")

    if not args.account_root or not args.account_inbox_root:
        print(
            json.dumps(
                {
                    "status": "account_route_config_missing",
                    "error": "--account-root and --account-inbox-root are required",
                }
            )
        )
        return 1

    warmstart = args.warmstart or f"deploy/hedge_warmstart/{args.venue}_warmstart.csv"
    hedge_mode = os.environ.get("HEDGE_MODE", "2f")
    cfg = ContinuousHedgeConfig(
        warmstart_csv=warmstart,
        hedge_mode=hedge_mode,
    )
    primary_root = REPO / args.primary_root
    account_root = Path(args.account_root).expanduser()
    if not account_root.is_absolute():
        account_root = REPO / account_root
    inbox_root = Path(args.account_inbox_root).expanduser()
    if not inbox_root.is_absolute():
        inbox_root = REPO / inbox_root
    account_route = require_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_root = account_route.account_path

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

    unit = list(warm_unit)
    btc = list(warm_btc)
    eth: list[float | None] = list(warm_eth) + [None] * max(0, len(unit) - len(warm_eth))
    eth = eth[: len(unit)]

    btc_price = args.btc_price or _latest_close(primary_root, HEDGE_SYMBOL)
    eth_price = args.eth_price or _latest_close(primary_root, HEDGE_SYMBOL_2)
    # Only the account owner may observe private wallet state. The publisher
    # consumes that durable observation; it never opens a second private client.
    health_error = ""
    try:
        owner_health = require_recent_account_owner_health(
            account_root,
            environment="demo",
            max_age_ns=int(args.account_health_max_age_seconds * 1_000_000_000),
            expected_account_id=account_route.account_id,
        )
    except (RuntimeError, ValueError) as exc:
        account_equity = 0.0
        health_error = str(exc)[:300]
    else:
        account_equity = owner_health.equity_usdt
    equity = args.equity_usdt if args.equity_usdt > 0.0 else account_equity
    equity_source = (
        "override" if args.equity_usdt > 0.0 else "account_owner_health" if account_equity > 0.0 else "unavailable"
    )
    live_book = _account_continuous_book_state(account_root, equity_usdt=equity)
    pending_hedge_symbols = _pending_account_hedge_symbols(
        account_root,
        strategy_id=cfg.strategy_id,
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    n_eth = sum(1 for e in warm_eth if e is not None)
    use_2f = hedge_mode == "2f" and n_eth >= 60
    out: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "venue": args.venue,
        "hedge_mode": "2f" if use_2f else "btc",
        "mode": "publish" if args.submit else "dry_run",
        "btc_price": btc_price,
        "eth_price": eth_price,
        "equity_usdt": equity,
        "equity_source": equity_source,
        "gross_short_frac": round(live_book.gross_short_frac, 4),
        "gross_short_frac_known": live_book.gross_short_frac_known,
        "gross_short_frac_source": live_book.gross_short_frac_source,
        "pending_hedge_symbols": sorted(pending_hedge_symbols),
        "warmstart_last_date": None if warmstart_last is None else warmstart_last.isoformat(),
        "warmstart_data_through_date": None if warmstart_last is None else warmstart_last.isoformat(),
        "warmstart_age_days": warmstart_age_days,
        "warmstart_stale": warmstart_stale,
        "history_days": len(unit),
    }
    plans = []
    desired_targets: list[HedgeDesiredTarget] = []

    def read_current_hedge_qty(symbol: str = HEDGE_SYMBOL) -> float:
        return _current_account_hedge_qty(
            account_root,
            strategy_id=cfg.strategy_id,
            symbol=symbol,
        )

    if use_2f:
        decision: HedgeDecision2F = compute_hedge_decision_2f(
            cfg,
            unit_returns=unit,
            btc_returns=btc,
            eth_returns=eth,
            live_gross_short_frac=live_book.gross_short_frac,
            btc_price=btc_price,
            eth_price=eth_price,
            current_btc_qty=read_current_hedge_qty(),
            current_eth_qty=read_current_hedge_qty(HEDGE_SYMBOL_2),
            equity_usdt=equity,
        )
        out.update(
            {
                "ratio_btc": round(decision.ratio_btc, 5),
                "ratio_eth": round(decision.ratio_eth, 5),
                "target_btc_usdt": round(decision.target_btc_usdt, 2),
                "target_eth_usdt": round(decision.target_eth_usdt, 2),
                "n_obs_joint": decision.n_obs_joint,
                "fell_back_to_btc": decision.fell_back_to_btc,
                "plan_btc": _plan_json(decision.plan_btc),
                "plan_eth": _plan_json(decision.plan_eth),
            }
        )
        # audit2c: report the EFFECTIVE hedge mode. The use_2f gate is a coarse
        # full-series ETH pre-filter; the engine measures joint obs over the trailing
        # beta window and falls back to a single-leg BTC hedge when that window is
        # ETH-thin. Reflect that fallback here so hedge_mode is not misleadingly "2f"
        # when the book was actually hedged BTC-only (fell_back_to_btc carries the detail).
        if decision.fell_back_to_btc:
            out["hedge_mode"] = "btc"
        plans = [p for p in (decision.plan_btc, decision.plan_eth) if p is not None]
        desired_targets = [
            HedgeDesiredTarget(
                symbol=HEDGE_SYMBOL,
                target_notional_usdt=decision.target_btc_usdt,
                reason="scheduled_hedge_target_refresh",
                plan=decision.plan_btc,
            ),
            HedgeDesiredTarget(
                symbol=HEDGE_SYMBOL_2,
                target_notional_usdt=decision.target_eth_usdt,
                reason="scheduled_hedge_target_refresh",
                plan=decision.plan_eth,
            ),
        ]
    else:
        current_hedge_qty = read_current_hedge_qty()
        single = compute_hedge_decision(
            cfg,
            unit_returns=unit,
            btc_returns=btc,
            live_gross_short_frac=live_book.gross_short_frac,
            btc_price=btc_price,
            current_hedge_qty=current_hedge_qty,
            equity_usdt=equity,
        )
        # A mode flip to BTC-only explicitly targets ETH to zero. The account
        # owner performs the close and attributes its confirmed fill.
        unmanaged_eth_qty = read_current_hedge_qty(HEDGE_SYMBOL_2)
        out.update(
            {
                "hedge_ratio_equity_frac": round(single.hedge_ratio_equity_frac, 5),
                "target_notional_usdt": round(single.target_notional_usdt, 2),
                "current_hedge_qty": round(current_hedge_qty, 8),
                "current_notional_usdt": round(single.current_notional_usdt, 2),
                "n_obs": single.n_obs,
                "plan": _plan_json(single.plan),
                "unmanaged_eth_qty": round(unmanaged_eth_qty, 8),
            }
        )
        plans = [single.plan] if single.plan is not None else []
        desired_targets = [
            HedgeDesiredTarget(
                symbol=HEDGE_SYMBOL,
                target_notional_usdt=single.target_notional_usdt,
                reason="scheduled_hedge_target_refresh",
                plan=single.plan,
            ),
            HedgeDesiredTarget(
                symbol=HEDGE_SYMBOL_2,
                target_notional_usdt=0.0,
                reason="btc_only_mode_close_eth",
            ),
        ]

    if health_error:
        out["status"] = "submit_blocked_account_owner_unhealthy" if args.submit else "dry_run_account_owner_unhealthy"
        out["error"] = health_error
    elif equity_source == "unavailable":
        out["status"] = "submit_blocked_equity_unavailable" if args.submit else "dry_run_equity_unavailable"
        out["error"] = "canonical account equity is unavailable"
    elif btc_price <= 0.0:
        # No BTC price (kline store missing/unreadable and no --btc-price): the plan
        # is necessarily None. Without an explicit status this read as a healthy
        # "dry_run_ok"/"submit_no_action" no-op — silently masking a dead input.
        out["status"] = "submit_blocked_btc_price_unavailable" if args.submit else "dry_run_btc_price_unavailable"
    elif args.submit and not live_book.gross_short_frac_known:
        # The 0.5 gross-short default is a sizing REFERENCE, not an observation —
        # never submit orders sized off it (it would buy a hedge against a book
        # whose exposure nobody measured).
        out["status"] = "submit_blocked_book_state_unknown"
    elif args.submit and use_2f and eth_price <= 0.0:
        # In 2f mode a dead ETH price silently drops the ETH leg; an armed run must
        # surface it instead of part-hedging.
        out["status"] = "submit_blocked_eth_price_unavailable"
    elif args.submit:
        # Kernel route: publish absolute targets, including no-change targets, so
        # convergence never depends on the sleeve's compatibility ledger. Do
        # not refresh a target that is already pending when the planner sees no
        # quantity change: accepting that duplicate would advance the desire
        # revision and restart the owner's convergence generation/age. A stale
        # beta estimate may only lower exposure: omit target legs whose local
        # plan would increase risk, while still atomically publishing any
        # risk-reducing siblings.
        plan_by_symbol = {plan.symbol.upper(): plan for plan in plans}
        pending_unchanged_targets = [
            target
            for target in desired_targets
            if target.symbol.upper() in pending_hedge_symbols
            and target.plan is None
        ]
        refreshable_targets = [
            target
            for target in desired_targets
            if target not in pending_unchanged_targets
        ]
        if pending_unchanged_targets:
            out["pending_target_refresh_skips"] = sorted(
                target.symbol.upper() for target in pending_unchanged_targets
            )
        if warmstart_stale:
            blocked_add_legs = [plan for plan in plans if not plan.reduce_only]
            submittable_targets = [
                target
                for target in refreshable_targets
                if not ((plan := plan_by_symbol.get(target.symbol.upper())) is not None and not plan.reduce_only)
            ]
        else:
            blocked_add_legs = []
            submittable_targets = list(refreshable_targets)
        if blocked_add_legs:
            out["blocked_legs"] = [_plan_json(plan) for plan in blocked_add_legs]
        if submittable_targets:
            leverage = float(os.environ.get("HEDGE_ENTRY_LEVERAGE") or os.environ.get("ENTRY_LEVERAGE") or 10.0)
            try:
                queued = _publish_hedge_target_batch(
                    submittable_targets,
                    cfg=cfg,
                    route=account_route,
                    now_ms=now_ms,
                    leverage=leverage,
                )
            except Exception as exc:  # noqa: BLE001 - durable queue failure must page
                out["status"] = "target_publish_failed"
                out["publish_error"] = str(exc)[:300]
            else:
                out["queued"] = queued
                out["status"] = "target_queued_partial_blocked_stale_warmstart" if blocked_add_legs else "target_queued"
        else:
            out["status"] = (
                "submit_blocked_stale_warmstart" if blocked_add_legs or warmstart_stale else "submit_no_action"
            )
    elif use_2f and eth_price <= 0.0:
        out["status"] = "dry_run_eth_price_unavailable"
    elif warmstart_stale:
        out["status"] = "dry_run_stale_warmstart"
    else:
        out["status"] = "dry_run_ok"
    print(json.dumps(out))
    # A blocked or failed publish makes the oneshot fail so liveness can page.
    failing_statuses = {
        "submit_blocked_equity_unavailable",
        "submit_blocked_account_owner_unhealthy",
        "submit_blocked_btc_price_unavailable",
        "submit_blocked_book_state_unknown",
        "submit_blocked_eth_price_unavailable",
        "submit_blocked_stale_warmstart",
        "target_queued_partial_blocked_stale_warmstart",
        "target_publish_failed",
    }
    return 1 if args.submit and out["status"] in failing_statuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
