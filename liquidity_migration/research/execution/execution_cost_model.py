"""Measured per-fill execution costs from journal fills, decision books, and markouts.

`trade_diagnostics` projects one row per order command with quantity-weighted
markouts; this module keeps the *fill* grain and anchors every quantity to the
decision-time midpoint ``M0`` so the classical decomposition holds exactly:

    effective_spread  = 2·s·(P_fill − M0)/M0
    price_impact_h    = 2·s·(M_h    − M0)/M0
    realized_spread_h = effective_spread − price_impact_h
                      = 2·s·(P_fill − M_h)/M0

with ``s`` = +1 for buys and −1 for sells, ``M_h`` the captured L2 midpoint at
horizon ``h``. Effective spread is what the taker paid at arrival; price impact
is the adverse-selection component that persisted to ``h``; realized spread is
what a passive counterparty would have earned.

Inputs are the journal events, the decision `book_context` records (keyed by
market-input key), and the per-``(execution_id, horizon)`` markout observations
that `trade_diagnostics` already loads. Nothing here feeds sizing or execution.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import polars as pl

from liquidity_migration.account.account_contracts import AccountEvent, AccountEventType
from liquidity_migration.research.execution.trade_diagnostics import TradeDiagnosticError

MARKOUT_HORIZONS_NS: tuple[tuple[str, int], ...] = (
    ("1s", 1_000_000_000),
    ("15s", 15_000_000_000),
    ("1m", 60_000_000_000),
    ("5m", 300_000_000_000),
)

FILL_COST_SCHEMA = pl.Schema(
    {
        "execution_id": pl.String,
        "command_id": pl.String,
        "batch_id": pl.String,
        "symbol": pl.String,
        "side": pl.String,
        "fill_qty": pl.Float64,
        "fill_price": pl.Float64,
        "fill_notional_usdt": pl.Float64,
        "fill_exchange_ts_ns": pl.Int64,
        "decision_mid": pl.Float64,
        "quoted_spread_bps": pl.Float64,
        "effective_spread_bps": pl.Float64,
        **{
            key: dtype
            for label, _ in MARKOUT_HORIZONS_NS
            for key, dtype in (
                (f"markout_mid_{label}", pl.Float64),
                (f"price_impact_{label}_bps", pl.Float64),
                (f"realized_spread_{label}_bps", pl.Float64),
            )
        },
    }
)


def _top_of_book_mid(context: Mapping[str, Any], *, key: str) -> tuple[float, float]:
    """Return (mid, quoted_spread_bps) from one decision book context."""

    bids = context.get("bids") or []
    asks = context.get("asks") or []
    if not bids or not asks:
        raise TradeDiagnosticError(f"decision book context {key!r} has an empty side")
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    if not (math.isfinite(best_bid) and math.isfinite(best_ask)) or best_bid <= 0.0 or best_ask <= 0.0:
        raise TradeDiagnosticError(f"decision book context {key!r} has invalid touch prices")
    if best_ask < best_bid:
        raise TradeDiagnosticError(f"decision book context {key!r} is crossed")
    mid = (best_bid + best_ask) / 2.0
    return mid, (best_ask - best_bid) / mid * 10_000.0


def build_fill_cost_rows(
    events: Sequence[AccountEvent],
    book_contexts: Mapping[str, Mapping[str, Any]],
    markout_observations: Mapping[tuple[str, int], Mapping[str, Any]],
) -> pl.DataFrame:
    """One row per journal fill with M0-anchored cost decomposition.

    Fills whose decision book context is missing are excluded (counted by the
    summary as uncovered rather than silently averaged); markout horizons that
    were not observed healthy stay null.
    """

    market_key_by_batch_symbol: dict[tuple[str, str], str] = {}
    command_meta: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.event_type == AccountEventType.MARKET_INPUT_REF.value:
            input_key = str(event.payload.get("input_key") or "")
            if input_key:
                market_key_by_batch_symbol[(event.correlation_id, event.symbol)] = input_key
        elif event.event_type == AccountEventType.ORDER_COMMAND.value:
            command_id = str(event.payload.get("command_id") or "")
            if command_id:
                command_meta[command_id] = (event.correlation_id, event.symbol)

    rows: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != AccountEventType.FILL.value:
            continue
        payload = event.payload
        execution_id = str(payload.get("execution_id") or "")
        command_id = str(payload.get("command_id") or "")
        if not execution_id or command_id not in command_meta:
            continue
        batch_id, symbol = command_meta[command_id]
        market_key = market_key_by_batch_symbol.get((batch_id, symbol), "")
        context = book_contexts.get(market_key)
        if context is None:
            continue
        decision_mid, quoted_spread_bps = _top_of_book_mid(context, key=market_key)
        signed_qty = float(payload.get("signed_qty") or 0.0)
        price = float(payload.get("price") or 0.0)
        if signed_qty == 0.0 or price <= 0.0 or not math.isfinite(price):
            raise TradeDiagnosticError(f"fill {execution_id} has invalid quantity/price")
        sign = 1.0 if signed_qty > 0.0 else -1.0
        row: dict[str, Any] = {
            "execution_id": execution_id,
            "command_id": command_id,
            "batch_id": batch_id,
            "symbol": symbol,
            "side": "buy" if sign > 0 else "sell",
            "fill_qty": abs(signed_qty),
            "fill_price": price,
            "fill_notional_usdt": abs(signed_qty) * price,
            "fill_exchange_ts_ns": int(payload.get("exchange_ts_ns") or 0),
            "decision_mid": decision_mid,
            "quoted_spread_bps": quoted_spread_bps,
            "effective_spread_bps": 2.0 * sign * (price - decision_mid) / decision_mid * 10_000.0,
        }
        for label, horizon_ns in MARKOUT_HORIZONS_NS:
            observation = markout_observations.get((execution_id, horizon_ns))
            mid_h: float | None = None
            if observation is not None and observation.get("markout_status") == "observed_healthy":
                candidate = observation.get("markout_midpoint")
                if candidate is not None and math.isfinite(float(candidate)) and float(candidate) > 0.0:
                    mid_h = float(candidate)
            row[f"markout_mid_{label}"] = mid_h
            if mid_h is None:
                row[f"price_impact_{label}_bps"] = None
                row[f"realized_spread_{label}_bps"] = None
            else:
                impact = 2.0 * sign * (mid_h - decision_mid) / decision_mid * 10_000.0
                row[f"price_impact_{label}_bps"] = impact
                row[f"realized_spread_{label}_bps"] = row["effective_spread_bps"] - impact
        rows.append(row)

    if not rows:
        return pl.DataFrame(schema=FILL_COST_SCHEMA)
    return pl.DataFrame(rows, schema=FILL_COST_SCHEMA)


def cost_model_summary(rows: pl.DataFrame, *, total_fill_events: int) -> dict[str, Any]:
    """Notional-weighted measured cost report over the per-fill rows."""

    if rows.is_empty():
        return {
            "fills_measured": 0,
            "fills_total": int(total_fill_events),
            "coverage": 0.0,
        }
    weight = rows["fill_notional_usdt"]
    total_notional = float(weight.sum())

    def _weighted(column: str) -> float | None:
        present = rows.filter(pl.col(column).is_not_null())
        if present.is_empty():
            return None
        denominator = float(present["fill_notional_usdt"].sum())
        if denominator <= 0.0:
            return None
        weighted_total = float((present[column] * present["fill_notional_usdt"]).sum())
        return weighted_total / denominator

    summary: dict[str, Any] = {
        "fills_measured": rows.height,
        "fills_total": int(total_fill_events),
        "coverage": rows.height / max(total_fill_events, 1),
        "total_notional_usdt": total_notional,
        "quoted_spread_bps_weighted": _weighted("quoted_spread_bps"),
        "effective_spread_bps_weighted": _weighted("effective_spread_bps"),
    }
    for label, _ in MARKOUT_HORIZONS_NS:
        summary[f"price_impact_{label}_bps_weighted"] = _weighted(f"price_impact_{label}_bps")
        summary[f"realized_spread_{label}_bps_weighted"] = _weighted(f"realized_spread_{label}_bps")
        summary[f"observed_{label}"] = int(
            rows.filter(pl.col(f"price_impact_{label}_bps").is_not_null()).height
        )
    return summary
