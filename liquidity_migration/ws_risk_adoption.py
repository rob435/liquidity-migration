from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from ._common import MS_PER_DAY
from .event_demo import (
    _float,
    _normalized_position_side,
    _stop_price_for_entry,
    _take_profit_price_for_entry,
    decode_entry_order_link_id,
)
from .long_native_event_demo import LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID


class RecoveredEntryLinkMetadata(NamedTuple):
    link: str
    strategy_id: str
    signal_ts_ms: int
    sleeve: str
    reentry_seq: int
    component_tag: str


@dataclass(frozen=True, slots=True)
class AdoptedTradeBuildResult:
    row: dict[str, Any] | None
    ambiguous_short: bool = False
    ambiguous_symbol: str = ""
    ambiguous_qty: str = ""
    ambiguous_entry_price: float = 0.0


def first_price(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value > 0.0:
            return value
    return 0.0


def adopt_strategy_id_for_sleeve(risk: Any, sleeve: str) -> str:
    """Resolve the strategy_id used to reconstruct deterministic adopted trade IDs."""
    if sleeve == "long":
        return risk.adopt_long_strategy_id or LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    if sleeve == "continuous":
        from .continuous_demo import CONTINUOUS_STRATEGY_ID

        return risk.adopt_continuous_strategy_id or CONTINUOUS_STRATEGY_ID
    if sleeve == "continuous_addon":
        from .continuous_demo import CONTINUOUS_ADDON_STRATEGY_ID

        return risk.adopt_continuous_addon_strategy_id or CONTINUOUS_ADDON_STRATEGY_ID
    if sleeve == "short":
        # Compatibility rows can still be recovered, but only with an explicitly
        # configured ID.
        return risk.adopt_short_strategy_id or ""
    return ""


def select_recovered_entry_link_metadata(
    history: list[dict[str, Any]],
    risk: Any,
    *,
    side: str,
) -> RecoveredEntryLinkMetadata | None:
    """Pick the best decodable entry order from Bybit order history.

    Same-signal continuous re-entries can leave multiple live-looking links in
    recent order history. Prefer highest reentry_seq, then latest venue timestamp.
    """
    venue_side = "Buy" if side == "long" else "Sell"
    best_key: tuple[int, int] | None = None
    best: RecoveredEntryLinkMetadata | None = None
    for order in history:
        order_side = str(order.get("side") or "")
        if order_side != venue_side:
            continue
        link = str(order.get("orderLinkId") or order.get("order_link_id") or "")
        decoded = decode_entry_order_link_id(link)
        if decoded is None:
            continue
        decoded_sleeve, signal_ts_ms, reentry_seq, component_tag = decoded
        strategy_id = adopt_strategy_id_for_sleeve(risk, decoded_sleeve)
        if not strategy_id:
            continue
        created_ts = int(_float(order.get("createdTime") or order.get("updatedTime") or 0))
        key = (reentry_seq, created_ts)
        if best_key is None or key > best_key:
            best_key = key
            best = RecoveredEntryLinkMetadata(
                link=link,
                strategy_id=strategy_id,
                signal_ts_ms=signal_ts_ms,
                sleeve=decoded_sleeve,
                reentry_seq=reentry_seq,
                component_tag=component_tag,
            )
    return best


def build_adopted_trade_row(
    position: dict[str, Any],
    *,
    now_ms: int,
    risk: Any,
    recover_entry_link_metadata: Callable[[str, str], RecoveredEntryLinkMetadata | None],
    adoption_equity_usdt: Callable[[], float],
    continuous_root_configured: bool,
) -> AdoptedTradeBuildResult:
    symbol = str(position.get("symbol", ""))
    qty = str(position.get("size") or "")
    entry_price = first_price(position, ("avgPrice", "avg_price", "entryPrice", "entry_price"))
    side = _normalized_position_side(position.get("side"))
    if not symbol or _float(qty) <= 0.0 or entry_price <= 0.0 or side not in {"long", "short"}:
        return AdoptedTradeBuildResult(row=None)

    # Route through _float first: float-formatted venue ms strings should not
    # silently date adopted trades to now_ms.
    opened_ms = int(_float(position.get("createdTime") or position.get("created_time"))) or now_ms
    stop_loss_pct = max(risk.adopt_stop_loss_pct, 0.0)
    take_profit_pct = max(risk.adopt_take_profit_pct, 0.0)
    tick_size = _float(position.get("tickSize") or position.get("tick_size"))
    stop_price = (
        _stop_price_for_entry(entry_price=entry_price, side=side, stop_loss_pct=stop_loss_pct, tick_size=tick_size)
        if stop_loss_pct > 0.0
        else 0.0
    )
    take_profit_price = _take_profit_price_for_entry(
        entry_price=entry_price,
        side=side,
        take_profit_pct=take_profit_pct,
        tick_size=tick_size,
    )
    planned_exit_ts_ms = opened_ms + int(max(risk.adopt_hold_days, 0.0) * MS_PER_DAY)
    sleeve = "long" if side == "long" else "short"

    recovered = recover_entry_link_metadata(symbol, side)
    if recovered is not None:
        link, strategy_id, signal_ts_ms, decoded_sleeve, reentry_seq, component_tag = recovered
        if decoded_sleeve == "continuous_addon":
            from .continuous_hedge_manager import (
                HEDGE_SYMBOL,
                HEDGE_SYMBOL_2,
                ContinuousHedgeConfig,
                build_hedge_tracking_row,
            )

            if symbol in (HEDGE_SYMBOL, HEDGE_SYMBOL_2) and side == "long":
                return AdoptedTradeBuildResult(
                    row=build_hedge_tracking_row(
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
                )

        trade_id = f"{strategy_id}-{symbol}-{signal_ts_ms}" + (f"-{reentry_seq}" if reentry_seq > 0 else "")
        component_fields: dict[str, Any] = {}
        if decoded_sleeve == "continuous" and component_tag:
            if component_tag == "s":
                from .continuous_demo import recover_snipe_trade_id_from_link

                recovered_snipe = recover_snipe_trade_id_from_link(
                    link,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    signal_ts_ms=signal_ts_ms,
                )
                trade_id = recovered_snipe or (trade_id + "-snipe")
            else:
                trade_id += f"-{component_tag}"
                from .continuous_demo import ensemble_component_weight_for_tag

                component_weight = ensemble_component_weight_for_tag(component_tag)
                if component_weight is not None:
                    component_fields = {
                        "component": component_tag,
                        "component_weight": component_weight,
                    }

        return AdoptedTradeBuildResult(
            row={
                "trade_id": trade_id,
                "sleeve": decoded_sleeve,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": side,
                "status": "open",
                "qty": qty,
                "entry_price": entry_price,
                **component_fields,
                "entry_fee_usdt": 0.0,
                "entry_exec_time_ms": opened_ms,
                "notional_usdt": abs(entry_price * _float(qty)),
                "equity_usdt": adoption_equity_usdt(),
                "ts_ms": now_ms,
                "entry_ts_ms": opened_ms,
                "opened_at_ms": opened_ms,
                "updated_at_ms": now_ms,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "planned_exit_ts_ms": planned_exit_ts_ms,
                "entry_order_link_id": link,
                "entry_order_id": "",
                "signal_ts_ms": signal_ts_ms,
                "submit_mode": "adopted_recovered",
            }
        )

    ambiguous_short = side == "short" and continuous_root_configured
    return AdoptedTradeBuildResult(
        row={
            "trade_id": f"adopted-{symbol}-{opened_ms}",
            "sleeve": sleeve,
            "strategy_id": "adopted",
            "symbol": symbol,
            "side": side,
            "status": "open",
            "qty": qty,
            "entry_price": entry_price,
            "entry_fee_usdt": 0.0,
            "entry_exec_time_ms": opened_ms,
            "notional_usdt": abs(entry_price * _float(qty)),
            "equity_usdt": adoption_equity_usdt(),
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
            "signal_ts_ms": 0,
            "submit_mode": "adopted",
        },
        ambiguous_short=ambiguous_short,
        ambiguous_symbol=symbol,
        ambiguous_qty=qty,
        ambiguous_entry_price=entry_price,
    )


def validate_trade_row_invariants(row: dict[str, Any]) -> tuple[bool, str]:
    """Cheap defensive check before writing a trade row to the ledger."""
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
