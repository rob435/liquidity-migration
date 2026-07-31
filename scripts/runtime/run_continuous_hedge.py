"""Periodic target-hedge runner for a bound continuous account route.

Computes the current two-leg hedge target from a commit-owned immutable model
prior and canonical account state. Dry-run (the default) prints the decision;
``--execute`` publishes an absolute target batch to the account owner. This
process never calls the venue private API and writes no sleeve-local ledger.

The CLI requires an explicit demo/paper route. The historical prior is not
extended with live returns and its age is not a freshness signal; missing,
malformed, future-dated, or estimator-inadequate prior data fails closed.

An armed run that is blocked or fails publication exits nonzero so the systemd
oneshot fails and liveness can alert. Dry runs and genuine no-action runs exit 0.
Exception: an armed run blocked only by unhealthy account-owner health exits 0
with its blocked status recorded, because the owner-health watchdog already pages
that root cause.

Usage:
    .venv/bin/python scripts/runtime/run_continuous_hedge.py --execution-environment demo
    .venv/bin/python scripts/runtime/run_continuous_hedge.py --execution-environment demo --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
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
from liquidity_migration.execution_environment import (  # noqa: E402
    account_id_for_environment,
)
from liquidity_migration.account_owner_health import (  # noqa: E402
    require_recent_account_owner_health,
)
from liquidity_migration.account_strategy_state import (  # noqa: E402
    CanonicalAccountProjection,
    canonical_account_projection,
    canonical_strategy_trade_rows,
    target_reservation_rows,
)
from liquidity_migration.continuous_hedge_manager import (  # noqa: E402
    HEDGE_MODEL_PRIOR_KIND,
    HEDGE_MODEL_PRIOR_LIMITATIONS,
    HEDGE_SYMBOL,
    HEDGE_SYMBOL_2,
    ContinuousHedgeConfig,
    HedgeDecision2F,
    HedgeModelPrior,
    compute_hedge_decision_2f,
    load_hedge_model_prior,
    require_usable_hedge_model_prior,
)
from liquidity_migration.operational_profile import load_operational_profile  # noqa: E402


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
    model_prior: HedgeModelPrior,
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
            **model_prior.provenance(),
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
    exits = [intent for intent in intents if float(intent.intent.signed_notional_usdt) == 0.0]
    nonzero = [intent for intent in intents if float(intent.intent.signed_notional_usdt) != 0.0]
    publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix=batch_id,
        exit_intents=exits,
        entry_intents=nonzero,
        created_ts_ns=now_ms * 1_000_000,
    )
    if publication.errors:
        details = "; ".join(
            f"{item.stage}:{item.target_key}:{item.error_type}:{item.message}" for item in publication.errors
        )
        raise OSError(f"hedge target publication failed: {details}")
    request_ids = [*publication.exit_request_ids, *publication.entry_request_ids]
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
    account_projection: CanonicalAccountProjection | None = None,
    symbol: str = HEDGE_SYMBOL,
) -> float:
    """Read the accepted hedge target, never a sleeve-local trade ledger."""

    rows = canonical_strategy_trade_rows(
        account_root,
        sleeve=SleeveAdapterKind.HEDGE.value,
        strategy_ids=(strategy_id,),
        account_projection=account_projection,
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
    account_projection: CanonicalAccountProjection | None = None,
) -> set[str]:
    """Accepted hedge targets that must not receive an unchanged refresh."""

    rows = canonical_strategy_trade_rows(
        account_root,
        sleeve=SleeveAdapterKind.HEDGE.value,
        strategy_ids=(strategy_id,),
        account_projection=account_projection,
    )
    if rows.is_empty() or "status" not in rows.columns or "symbol" not in rows.columns:
        return set()
    return {str(symbol).upper() for symbol in rows.filter(pl.col("status") == "target_pending")["symbol"].to_list()}


def _account_continuous_book_state(
    account_root: Path,
    *,
    equity_usdt: float,
    account_projection: CanonicalAccountProjection | None = None,
) -> LiveBookState:
    """Size hedge exposure from canonical CONTINUOUS targets, not a sleeve ledger."""
    rows = canonical_strategy_trade_rows(
        account_root,
        sleeve=SleeveAdapterKind.CONTINUOUS.value,
        account_projection=account_projection,
    )
    reserved = target_reservation_rows(rows)
    short_targets = reserved.filter(pl.col("side") == "short") if not reserved.is_empty() else reserved
    gross_notional = sum(abs(_float(row.get("notional_usdt"))) for row in short_targets.to_dicts())
    if equity_usdt <= 0.0:
        return LiveBookState(0.0, False, "account_target_equity_unavailable")
    return LiveBookState(
        gross_notional / equity_usdt,
        True,
        "account_kernel_desired_targets",
    )


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
    ap.add_argument(
        "--execution-environment",
        required=True,
        choices=("demo", "paper"),
        help="bound account-owner environment; there is no implicit default",
    )
    ap.add_argument("--primary-root", default="data/bybit-continuous-demo-event")
    ap.add_argument(
        "--model-prior",
        default=os.environ.get("HEDGE_MODEL_PRIOR", "deploy/hedge_warmstart/bybit_warmstart.csv"),
        help="strict immutable historical hedge model prior",
    )
    ap.add_argument(
        "--validate-model-prior-only",
        action="store_true",
        help="validate model-prior integrity and estimator sufficiency without reading account state",
    )
    ap.add_argument("--btc-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument("--eth-price", type=float, default=0.0, help="override; else read from kline store")
    ap.add_argument(
        "--equity-usdt",
        type=float,
        default=0.0,
        help="explicit override; otherwise use the latest canonical account observation",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="publish targets to the selected owner; omission is a dry run",
    )
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
        help="maximum age of the selected account owner's healthy capital observation",
    )
    ap.add_argument(
        "--operational-profile-file",
        default=os.environ.get("ACCOUNT_RISK_POLICY_FILE", ""),
        help="shared producer/account operational profile; required with --execute",
    )
    args = ap.parse_args()

    if args.account_health_max_age_seconds <= 0.0:
        ap.error("--account-health-max-age-seconds must be positive")
    operational_profile = None
    if args.operational_profile_file:
        operational_profile = load_operational_profile(args.operational_profile_file)
    if args.execute and operational_profile is None:
        ap.error("--execute requires --operational-profile-file")

    model_prior_path = Path(args.model_prior)
    if not model_prior_path.is_absolute():
        model_prior_path = REPO / model_prior_path
    as_of_date = datetime.now(timezone.utc).date()
    try:
        model_prior = require_usable_hedge_model_prior(
            load_hedge_model_prior(model_prior_path),
            as_of_date=as_of_date,
        )
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "model_prior_invalid",
                    "model_prior_kind": HEDGE_MODEL_PRIOR_KIND,
                    "error": str(exc)[:300],
                }
            )
        )
        return 1
    model_prior_age_days = (as_of_date - model_prior.data_through_date).days
    if args.validate_model_prior_only:
        print(
            json.dumps(
                {
                    "status": "model_prior_valid",
                    **model_prior.provenance(),
                    "model_prior_age_days_informational": model_prior_age_days,
                    "model_prior_limitations": HEDGE_MODEL_PRIOR_LIMITATIONS,
                }
            )
        )
        return 0

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

    cfg = ContinuousHedgeConfig()
    primary_root = REPO / args.primary_root
    account_root = Path(args.account_root).expanduser()
    if not account_root.is_absolute():
        account_root = REPO / account_root
    inbox_root = Path(args.account_inbox_root).expanduser()
    if not inbox_root.is_absolute():
        inbox_root = REPO / inbox_root
    account_route = require_account_route(
        account_id=account_id_for_environment(args.execution_environment),
        environment=args.execution_environment,
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_root = account_route.account_path

    unit = list(model_prior.unit_returns)
    btc = list(model_prior.btc_returns)
    eth = list(model_prior.eth_returns)

    btc_price = args.btc_price or _latest_close(primary_root, HEDGE_SYMBOL)
    eth_price = args.eth_price or _latest_close(primary_root, HEDGE_SYMBOL_2)
    # Equity comes from the owner's durable health record; this process never
    # opens a second private client.
    health_error = ""
    try:
        owner_health = require_recent_account_owner_health(
            account_root,
            environment=args.execution_environment,
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
    # One verified journal read shared by every read model below.
    account_projection = canonical_account_projection(account_root)
    live_book = _account_continuous_book_state(
        account_root, equity_usdt=equity, account_projection=account_projection
    )
    pending_hedge_symbols = _pending_account_hedge_symbols(
        account_root,
        strategy_id=cfg.strategy_id,
        account_projection=account_projection,
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    out: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "venue": "bybit",
        "execution_environment": args.execution_environment,
        "hedge_mode": "2f",
        "mode": "target_publish" if args.execute else "dry_run",
        "btc_price": btc_price,
        "eth_price": eth_price,
        "equity_usdt": equity,
        "equity_source": equity_source,
        "gross_short_frac": round(live_book.gross_short_frac, 4),
        "gross_short_frac_known": live_book.gross_short_frac_known,
        "gross_short_frac_source": live_book.gross_short_frac_source,
        "pending_hedge_symbols": sorted(pending_hedge_symbols),
        **model_prior.provenance(),
        "model_prior_age_days_informational": model_prior_age_days,
        "model_prior_limitations": HEDGE_MODEL_PRIOR_LIMITATIONS,
        "history_days": len(unit),
    }
    def read_current_hedge_qty(symbol: str = HEDGE_SYMBOL) -> float:
        return _current_account_hedge_qty(
            account_root,
            strategy_id=cfg.strategy_id,
            symbol=symbol,
        )

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
    if decision.fell_back_to_btc:
        out["hedge_mode"] = "btc_fallback"
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

    if health_error:
        out["status"] = "execute_blocked_account_owner_unhealthy" if args.execute else "dry_run_account_owner_unhealthy"
        out["error"] = health_error
    elif equity_source == "unavailable":
        out["status"] = "execute_blocked_equity_unavailable" if args.execute else "dry_run_equity_unavailable"
        out["error"] = "canonical account equity is unavailable"
    elif btc_price <= 0.0:
        # A dead BTC price forces plan=None, which without its own status reads
        # as a healthy no-op.
        out["status"] = "execute_blocked_btc_price_unavailable" if args.execute else "dry_run_btc_price_unavailable"
    elif args.execute and not live_book.gross_short_frac_known:
        # The 0.5 gross-short default is a sizing reference, not an observation:
        # sizing orders off it hedges a book nobody measured.
        out["status"] = "execute_blocked_book_state_unknown"
    elif args.execute and eth_price <= 0.0:
        # In 2f mode a dead ETH price would silently drop the ETH leg.
        out["status"] = "execute_blocked_eth_price_unavailable"
    elif args.execute:
        # Publish absolute targets, no-change ones included, so convergence never
        # depends on a sleeve-local ledger. But skip a target already pending with
        # no quantity change: that duplicate would advance the desire revision and
        # restart the owner's convergence generation/age.
        pending_unchanged_targets = [
            target
            for target in desired_targets
            if target.symbol.upper() in pending_hedge_symbols and target.plan is None
        ]
        refreshable_targets = [target for target in desired_targets if target not in pending_unchanged_targets]
        if pending_unchanged_targets:
            out["pending_target_refresh_skips"] = sorted(target.symbol.upper() for target in pending_unchanged_targets)
        submittable_targets = list(refreshable_targets)
        if submittable_targets:
            leverage = (
                operational_profile.hedge.entry_leverage
                if operational_profile is not None
                else 2.0
            )
            try:
                queued = _publish_hedge_target_batch(
                    submittable_targets,
                    cfg=cfg,
                    model_prior=model_prior,
                    route=account_route,
                    now_ms=now_ms,
                    leverage=leverage,
                )
            except Exception as exc:  # noqa: BLE001 - durable queue failure must page
                out["status"] = "target_publish_failed"
                out["publish_error"] = str(exc)[:300]
            else:
                out["queued"] = queued
                out["status"] = "target_queued"
        else:
            out["status"] = "execute_no_action"
    elif eth_price <= 0.0:
        out["status"] = "dry_run_eth_price_unavailable"
    else:
        out["status"] = "dry_run_ok"
    print(json.dumps(out))
    # A blocked or failed publish makes the oneshot fail so liveness can page.
    # execute_blocked_account_owner_unhealthy is deliberately absent: the
    # owner-health watchdog pages that root cause itself.
    failing_statuses = {
        "execute_blocked_equity_unavailable",
        "execute_blocked_btc_price_unavailable",
        "execute_blocked_book_state_unknown",
        "execute_blocked_eth_price_unavailable",
        "target_publish_failed",
    }
    return 1 if args.execute and out["status"] in failing_statuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
