from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple


class RoutedSleeve(NamedTuple):
    sleeve: str
    requested: str
    owned: tuple[str, ...]
    misroute: bool


def build_sleeve_routes(
    root: Path,
    risk: Any,
    *,
    long_root: Path | None,
    continuous_root: Path | None,
    continuous_addon_root: Path | None,
    trades: bool,
) -> dict[str, tuple[Path, str]]:
    routes: dict[str, tuple[Path, str]] = {
        "short": (root, "event_demo_trades" if trades else "event_demo_orders"),
    }
    if long_root is not None:
        routes["long"] = (
            long_root,
            risk.long_trades_dataset if trades else risk.long_orders_dataset,
        )
    if continuous_root is not None:
        routes["continuous"] = (
            continuous_root,
            risk.continuous_trades_dataset if trades else risk.continuous_orders_dataset,
        )
    if continuous_addon_root is not None:
        routes["continuous_addon"] = (
            continuous_addon_root,
            risk.continuous_addon_trades_dataset if trades else risk.continuous_addon_orders_dataset,
        )
    return routes


def owned_sleeves(
    *,
    long_root: Path | None,
    continuous_root: Path | None,
    continuous_addon_root: Path | None,
) -> set[str]:
    return (
        {"short"}
        | ({"long"} if long_root is not None else set())
        | ({"continuous"} if continuous_root is not None else set())
        | ({"continuous_addon"} if continuous_addon_root is not None else set())
    )


def resolve_sleeve(row: dict[str, Any], owned: set[str]) -> RoutedSleeve:
    requested = str(row.get("sleeve") or "").lower()
    sorted_owned = tuple(sorted(owned))
    if requested in owned:
        return RoutedSleeve(sleeve=requested, requested=requested, owned=sorted_owned, misroute=False)
    return RoutedSleeve(sleeve="short", requested=requested, owned=sorted_owned, misroute=bool(requested))


def tag_sleeve_from_trades(
    trade_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    *,
    all_trades: Any,
    fallback_symbol: str = "",
) -> None:
    """Fill missing sleeve tags on event_demo trade/order rows from the combined ledger."""
    if not trade_rows and not order_rows:
        return
    trade_index: dict[str, str] = {}
    symbol_index: dict[str, str] = {}
    if not all_trades.is_empty():
        for row in all_trades.to_dicts():
            tid = str(row.get("trade_id") or "")
            sym = str(row.get("symbol") or "")
            sleeve = str(row.get("sleeve") or "")
            if tid:
                trade_index[tid] = sleeve
            if sym and sleeve and sym not in symbol_index:
                symbol_index[sym] = sleeve

    def _resolve(row: dict[str, Any]) -> str:
        existing = str(row.get("sleeve") or "")
        if existing:
            return existing
        tid = str(row.get("trade_id") or "")
        sleeve = trade_index.get(tid, "")
        if sleeve:
            return sleeve
        sym = str(row.get("symbol") or fallback_symbol)
        return symbol_index.get(sym, "short")

    for row in trade_rows:
        row["sleeve"] = _resolve(row)
    for order in order_rows:
        order["sleeve"] = _resolve(order)
