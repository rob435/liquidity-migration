from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


SIGNAL_KIND_ORDER = {
    "watchlist": 0,
    "emerging": 1,
    "entry_ready": 2,
    "legacy_confirmed": 3,
    "none": 4,
}

SIGNAL_KIND_LABELS = {
    "watchlist": "broad intrabar context",
    "emerging": "building intrabar setup",
    "entry_ready": "tradeable intrabar entry candidate",
    "legacy_confirmed": "historical confirmed-era row",
    "none": "no signal",
}

TRADE_EXPORT_COLUMNS = [
    "day",
    "timestamp",
    "ticker",
    "side",
    "entry_signal_kind",
    "exit_signal_kind",
    "exit_rank",
    "exit_composite_score",
    "exit_momentum_z",
    "exit_curvature",
    "exit_hurst",
    "exit_reason",
    "entry_price",
    "exit_price",
    "take_profit_price_at_exit",
    "stop_loss_price_at_exit",
    "profit_protection_adjustments",
    "quantity",
    "notional_usd",
    "pnl_pct",
    "pnl_usd",
    "holding_minutes",
    "minutes_to_first_profit_50bps",
    "minutes_to_first_profit_100bps",
    "bars_held",
    "mfe_pct",
    "mae_pct",
    "post_exit_best_pct",
    "post_exit_worst_pct",
    "volatility_pct",
    "notes",
]

PORTFOLIO_EXPORT_COLUMNS = [
    "timestamp",
    "day",
    "available_balance_usd",
    "equity_usd",
    "gross_notional_usd",
    "net_notional_usd",
    "open_positions",
    "long_positions",
    "short_positions",
    "estimated_trade_notional_usd",
    "estimated_additional_positions",
    "used_risk_usd",
    "remaining_risk_usd",
]


@dataclass(slots=True)
class TradeOverview:
    trade_count: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    total_pnl_pct: float
    total_pnl_usd: float
    avg_pnl_pct: float
    avg_pnl_usd: float
    avg_notional_usd: float | None
    avg_holding_minutes: float | None
    avg_minutes_to_first_profit_50bps: float | None
    avg_minutes_to_first_profit_100bps: float | None
    avg_mfe_pct: float | None
    avg_mae_pct: float | None
    avg_post_exit_best_pct: float | None
    avg_post_exit_worst_pct: float | None
    avg_volatility_pct: float | None
    stop_losses: int
    take_profits: int
    protected_profit_exits: int
    stale_winner_exits: int


@dataclass(slots=True)
class TradeDaySummary:
    day: str
    trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    total_pnl_pct: float
    total_pnl_usd: float
    avg_pnl_pct: float
    avg_pnl_usd: float
    avg_notional_usd: float | None
    avg_holding_minutes: float | None
    avg_minutes_to_first_profit_50bps: float | None
    avg_minutes_to_first_profit_100bps: float | None
    stop_losses: int
    take_profits: int
    protected_profit_exits: int
    stale_winner_exits: int
    avg_mfe_pct: float | None
    avg_mae_pct: float | None
    avg_post_exit_best_pct: float | None
    avg_post_exit_worst_pct: float | None
    avg_volatility_pct: float | None


@dataclass(slots=True)
class PortfolioOverview:
    snapshot_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    avg_open_positions: float | None
    max_open_positions: int | None
    avg_available_balance_usd: float | None
    min_available_balance_usd: float | None
    max_available_balance_usd: float | None
    avg_gross_notional_usd: float | None
    max_gross_notional_usd: float | None
    avg_estimated_additional_positions: float | None
    min_estimated_additional_positions: float | None
    max_estimated_additional_positions: float | None


@dataclass(slots=True)
class ReportSummary:
    total_rows: int
    alerted_rows: int
    first_timestamp: str | None
    last_timestamp: str | None
    stage_counts: list[tuple[str, int, int]]
    signal_kind_counts: list[tuple[str, int, int]]
    top_tickers: list[tuple[str, int, int, float, float]]
    trade_overview: TradeOverview | None = None
    trade_daily: list[TradeDaySummary] = field(default_factory=list)
    portfolio_overview: PortfolioOverview | None = None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _first_present(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp_to_day(value: Any) -> str | None:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        if value in (None, ""):
            return None
        text = str(value)
        return text[:10] if len(text) >= 10 else text
    return timestamp.date().isoformat()


def _normalize_signal_kind(value: Any) -> str:
    if value in (None, ""):
        return "legacy_confirmed"
    normalized = str(value).strip()
    if normalized in {"confirmed", "confirmed_strong"}:
        return "legacy_confirmed"
    return normalized


def _is_short_side(side: Any) -> bool:
    if side is None:
        return False
    normalized = str(side).strip().upper()
    return normalized in {"SELL", "SHORT"}


def _sanitize_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def _normalize_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = _first_present(
        row,
        (
            "closed_at",
            "exit_time",
            "updated_at",
            "timestamp",
            "opened_at",
            "entry_time",
        ),
    )
    opened_at = _first_present(row, ("opened_at", "entry_time", "created_at"))
    side = _first_present(row, ("side", "entry_side", "position_side", "direction", "trade_side"))
    ticker = _first_present(row, ("ticker", "symbol")) or "n/a"

    entry_price = _float_or_none(
        _first_present(row, ("entry_price", "avg_entry_price", "entry_fill_price"))
    )
    exit_price = _float_or_none(
        _first_present(row, ("exit_price", "avg_exit_price", "exit_fill_price"))
    )
    quantity = _float_or_none(_first_present(row, ("quantity", "qty", "size")))
    notional_usd = _float_or_none(
        _first_present(row, ("notional_usd", "requested_notional_usd", "gross_notional_usd"))
    )
    pnl_pct = _float_or_none(
        _first_present(row, ("realized_pnl_pct", "pnl_pct", "profit_pct"))
    )
    if pnl_pct is None and entry_price is not None and exit_price is not None and entry_price > 0:
        if _is_short_side(side):
            pnl_pct = (entry_price - exit_price) / entry_price
        else:
            pnl_pct = (exit_price - entry_price) / entry_price

    pnl_usd = _float_or_none(
        _first_present(row, ("realized_pnl_usd", "pnl_usd", "closed_pnl", "profit_usd"))
    )
    if pnl_usd is None and pnl_pct is not None and notional_usd is not None:
        pnl_usd = notional_usd * pnl_pct

    holding_minutes = _float_or_none(
        _first_present(row, ("holding_minutes", "hold_minutes", "duration_minutes"))
    )
    if holding_minutes is None:
        opened_dt = _parse_timestamp(opened_at)
        closed_dt = _parse_timestamp(timestamp)
        if opened_dt is not None and closed_dt is not None:
            holding_minutes = (closed_dt - opened_dt).total_seconds() / 60.0

    raw_exit_reason = _first_present(row, ("exit_reason", "reason", "notes"))
    exit_reason = _normalize_exit_reason(raw_exit_reason, entry_price, exit_price)

    return {
        "timestamp": str(timestamp) if timestamp is not None else None,
        "day": _timestamp_to_day(timestamp),
        "ticker": str(ticker),
        "side": str(side) if side is not None else None,
        "entry_signal_kind": _first_present(
            row, ("entry_signal_kind", "signal_kind", "signal", "entry_kind")
        ),
        "exit_signal_kind": _first_present(row, ("exit_signal_kind",)),
        "exit_rank": _int_or_none(_first_present(row, ("exit_rank",))),
        "exit_composite_score": _float_or_none(_first_present(row, ("exit_composite_score",))),
        "exit_momentum_z": _float_or_none(_first_present(row, ("exit_momentum_z",))),
        "exit_curvature": _float_or_none(_first_present(row, ("exit_curvature",))),
        "exit_hurst": _float_or_none(_first_present(row, ("exit_hurst",))),
        "exit_reason": exit_reason,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "take_profit_price_at_exit": _float_or_none(_first_present(row, ("take_profit_price_at_exit",))),
        "stop_loss_price_at_exit": _float_or_none(_first_present(row, ("stop_loss_price_at_exit",))),
        "profit_protection_adjustments": _int_or_none(
            _first_present(row, ("profit_protection_adjustments",))
        ),
        "quantity": quantity,
        "notional_usd": notional_usd,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "holding_minutes": holding_minutes,
        "minutes_to_first_profit_50bps": _float_or_none(
            _first_present(row, ("minutes_to_first_profit_50bps",))
        ),
        "minutes_to_first_profit_100bps": _float_or_none(
            _first_present(row, ("minutes_to_first_profit_100bps",))
        ),
        "bars_held": _int_or_none(_first_present(row, ("bars_held", "holding_bars"))),
        "mfe_pct": _float_or_none(_first_present(row, ("mfe_pct", "max_favorable_excursion_pct"))),
        "mae_pct": _float_or_none(_first_present(row, ("mae_pct", "max_adverse_excursion_pct"))),
        "post_exit_best_pct": _float_or_none(
            _first_present(row, ("post_exit_best_pct", "post_exit_best", "post_exit_high_pct"))
        ),
        "post_exit_worst_pct": _float_or_none(
            _first_present(row, ("post_exit_worst_pct", "post_exit_worst", "post_exit_low_pct"))
        ),
        "volatility_pct": _float_or_none(
            _first_present(
                row,
                (
                    "volatility_pct",
                    "realized_volatility_pct",
                    "post_exit_volatility_pct",
                ),
            )
        ),
        "notes": _first_present(row, ("notes",)),
    }


def _normalize_portfolio_row(row: dict[str, Any], avg_trade_notional_usd: float | None) -> dict[str, Any]:
    timestamp = _first_present(
        row, ("timestamp", "snapshot_at", "captured_at", "recorded_at", "updated_at")
    )
    available_balance = _float_or_none(
        _first_present(
            row,
            (
                "available_balance_usd",
                "wallet_balance_usd",
                "balance_usd",
                "available_balance",
            ),
        )
    )
    gross_notional = _float_or_none(
        _first_present(
            row,
            ("gross_notional_usd", "exposure_usd", "open_gross_notional_usd"),
        )
    )
    net_notional = _float_or_none(
        _first_present(row, ("net_notional_usd", "net_exposure_usd"))
    )
    open_positions = _int_or_none(_first_present(row, ("open_positions", "position_count")))
    long_positions = _int_or_none(_first_present(row, ("long_positions", "open_longs")))
    short_positions = _int_or_none(_first_present(row, ("short_positions", "open_shorts")))
    estimated_trade_notional = _float_or_none(
        _first_present(
            row,
            (
                "estimated_trade_notional_usd",
                "avg_trade_notional_usd",
                "target_notional_usd",
            ),
        )
    )
    if estimated_trade_notional is None:
        estimated_trade_notional = avg_trade_notional_usd
    estimated_additional_positions = None
    if (
        available_balance is not None
        and estimated_trade_notional is not None
        and estimated_trade_notional > 0
    ):
        estimated_additional_positions = available_balance / estimated_trade_notional
    used_risk = _float_or_none(_first_present(row, ("used_risk_usd", "risk_used_usd")))
    remaining_risk = _float_or_none(
        _first_present(row, ("remaining_risk_usd", "risk_remaining_usd"))
    )
    equity = _float_or_none(_first_present(row, ("equity_usd", "wallet_equity_usd")))

    return {
        "timestamp": str(timestamp) if timestamp is not None else None,
        "day": _timestamp_to_day(timestamp),
        "available_balance_usd": available_balance,
        "equity_usd": equity,
        "gross_notional_usd": gross_notional,
        "net_notional_usd": net_notional,
        "open_positions": open_positions,
        "long_positions": long_positions,
        "short_positions": short_positions,
        "estimated_trade_notional_usd": estimated_trade_notional,
        "estimated_additional_positions": estimated_additional_positions,
        "used_risk_usd": used_risk,
        "remaining_risk_usd": remaining_risk,
    }


def _load_signal_summary(connection: sqlite3.Connection, top_n: int) -> tuple[
    int,
    int,
    str | None,
    str | None,
    list[tuple[str, int, int]],
    list[tuple[str, int, int]],
    list[tuple[str, int, int, float, float]],
]:
    if not _table_exists(connection, "signals"):
        return 0, 0, None, None, [], [], []
    existing_columns = _table_columns(connection, "signals")
    total_rows, alerted_rows, first_timestamp, last_timestamp = connection.execute(
        """
        SELECT
            COUNT(*) AS total_rows,
            COALESCE(SUM(alerted), 0) AS alerted_rows,
            MIN(timestamp) AS first_timestamp,
            MAX(timestamp) AS last_timestamp
        FROM signals
        """
    ).fetchone()
    stage_counts = connection.execute(
        """
        SELECT
            COALESCE(stage, 'unknown') AS stage,
            COUNT(*) AS total_rows,
            COALESCE(SUM(alerted), 0) AS alerted_rows
        FROM signals
        GROUP BY COALESCE(stage, 'unknown')
        ORDER BY stage ASC
        """
    ).fetchall()
    if "signal_kind" in existing_columns:
        raw_signal_kind_counts = connection.execute(
            """
            SELECT
                signal_kind AS signal_kind,
                COUNT(*) AS total_rows,
                COALESCE(SUM(alerted), 0) AS alerted_rows
            FROM signals
            GROUP BY signal_kind
            ORDER BY signal_kind ASC
            """
        ).fetchall()
    else:
        raw_signal_kind_counts = connection.execute(
            """
            SELECT
                stage AS signal_kind,
                COUNT(*) AS total_rows,
                COALESCE(SUM(alerted), 0) AS alerted_rows
            FROM signals
            GROUP BY stage
            ORDER BY signal_kind ASC
            """
        ).fetchall()
    normalized_signal_kind_counts: dict[str, tuple[int, int]] = {}
    for signal_kind, kind_rows, kind_alerts in raw_signal_kind_counts:
        normalized = _normalize_signal_kind(signal_kind)
        prior_rows, prior_alerts = normalized_signal_kind_counts.get(normalized, (0, 0))
        normalized_signal_kind_counts[normalized] = (
            prior_rows + int(kind_rows),
            prior_alerts + int(kind_alerts),
        )
    top_tickers = connection.execute(
        """
        SELECT
            ticker,
            COUNT(*) AS total_rows,
            COALESCE(SUM(alerted), 0) AS alerted_rows,
            AVG(composite_score) AS avg_composite,
            MAX(composite_score) AS max_composite
        FROM signals
        GROUP BY ticker
        ORDER BY alerted_rows DESC, max_composite DESC, ticker ASC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    return (
        int(total_rows),
        int(alerted_rows),
        first_timestamp,
        last_timestamp,
        [
            (str(stage), int(stage_rows), int(stage_alerts))
            for stage, stage_rows, stage_alerts in stage_counts
        ],
        [
            (signal_kind, kind_rows, kind_alerts)
            for signal_kind, (kind_rows, kind_alerts) in sorted(
                normalized_signal_kind_counts.items(),
                key=lambda item: (SIGNAL_KIND_ORDER.get(item[0], 99), item[0]),
            )
        ],
        [
            (str(ticker), int(ticker_rows), int(ticker_alerts), float(avg_composite), float(max_composite))
            for ticker, ticker_rows, ticker_alerts, avg_composite, max_composite in top_tickers
        ],
    )


def _load_trade_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if _table_exists(connection, "trade_analytics"):
        return [
            _normalize_trade_row(_sanitize_row(row))
            for row in connection.execute("SELECT * FROM trade_analytics ORDER BY rowid")
        ]
    if not _table_exists(connection, "positions"):
        return []
    if _table_exists(connection, "orders"):
        rows = connection.execute(
            """
            SELECT p.*, o.side AS entry_side
            FROM positions p
            LEFT JOIN orders o ON o.id = p.entry_order_id
            WHERE p.status = 'closed'
            ORDER BY COALESCE(p.closed_at, p.updated_at, p.opened_at, p.id)
            """
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT p.*
            FROM positions p
            WHERE p.status = 'closed'
            ORDER BY COALESCE(p.closed_at, p.updated_at, p.opened_at, p.id)
            """
        ).fetchall()
    return [_normalize_trade_row(_sanitize_row(row)) for row in rows]


def _load_portfolio_rows(connection: sqlite3.Connection, avg_trade_notional_usd: float | None) -> list[dict[str, Any]]:
    if not _table_exists(connection, "portfolio_snapshots"):
        return []
    rows = connection.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY rowid"
    ).fetchall()
    return [
        _normalize_portfolio_row(_sanitize_row(row), avg_trade_notional_usd)
        for row in rows
    ]


def _count_reason(rows: list[dict[str, Any]], needle: str) -> int:
    needle = needle.lower()
    return sum(
        1
        for row in rows
        if needle in str(row.get("exit_reason") or "").lower()
        or needle in str(row.get("notes") or "").lower()
    )


def _normalize_exit_reason(
    raw_reason: Any,
    entry_price: float | None,
    exit_price: float | None,
) -> str | None:
    if raw_reason in (None, ""):
        return None
    text = str(raw_reason)
    lowered = text.lower()
    if "take_profit" in lowered or "stop_loss" in lowered:
        return text
    if exit_price is None:
        return text

    match = re.search(
        r"\bTP=([0-9]+(?:\.[0-9]+)?)\s+SL=([0-9]+(?:\.[0-9]+)?)(?:\b|[^0-9.])",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return text

    try:
        take_profit = float(match.group(1))
        stop_loss = float(match.group(2))
    except ValueError:
        return text

    distance_to_tp = abs(exit_price - take_profit)
    distance_to_sl = abs(exit_price - stop_loss)
    tolerance = max(
        abs(entry_price or 0.0) * 0.005,
        abs(take_profit) * 0.001,
        abs(stop_loss) * 0.001,
        1e-8,
    )
    if min(distance_to_tp, distance_to_sl) > tolerance:
        return text
    if distance_to_tp < distance_to_sl:
        return "take_profit_inferred"
    if distance_to_sl < distance_to_tp:
        return "stop_loss_inferred"
    return text


def _summarize_trades(rows: list[dict[str, Any]]) -> tuple[TradeOverview | None, list[TradeDaySummary]]:
    if not rows:
        return None, []
    pnl_values = [row["pnl_pct"] for row in rows if row["pnl_pct"] is not None]
    pnl_usd_values = [row["pnl_usd"] for row in rows if row["pnl_usd"] is not None]
    notional_values = [row["notional_usd"] for row in rows if row["notional_usd"] is not None]
    holding_values = [row["holding_minutes"] for row in rows if row["holding_minutes"] is not None]
    first_profit_50bps_values = [
        row["minutes_to_first_profit_50bps"]
        for row in rows
        if row["minutes_to_first_profit_50bps"] is not None
    ]
    first_profit_100bps_values = [
        row["minutes_to_first_profit_100bps"]
        for row in rows
        if row["minutes_to_first_profit_100bps"] is not None
    ]
    mfe_values = [row["mfe_pct"] for row in rows if row["mfe_pct"] is not None]
    mae_values = [row["mae_pct"] for row in rows if row["mae_pct"] is not None]
    post_exit_best_values = [
        row["post_exit_best_pct"] for row in rows if row["post_exit_best_pct"] is not None
    ]
    post_exit_worst_values = [
        row["post_exit_worst_pct"] for row in rows if row["post_exit_worst_pct"] is not None
    ]
    volatility_values = [row["volatility_pct"] for row in rows if row["volatility_pct"] is not None]

    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    breakeven = sum(1 for value in pnl_values if value == 0)
    trade_count = len(rows)
    stop_losses = _count_reason(rows, "stop_loss")
    take_profits = _count_reason(rows, "take_profit")
    protected_profit_exits = _count_reason(rows, "protected_profit")
    stale_winner_exits = _count_reason(rows, "stale_winner")

    daily_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = row.get("day") or "unknown"
        daily_groups[str(day)].append(row)

    daily_rows = [
        _summarize_trade_day(day, grouped_rows)
        for day, grouped_rows in sorted(daily_groups.items(), key=lambda item: item[0])
    ]

    overview = TradeOverview(
        trade_count=trade_count,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        win_rate=(wins / trade_count) if trade_count else 0.0,
        total_pnl_pct=sum(pnl_values) if pnl_values else 0.0,
        total_pnl_usd=sum(pnl_usd_values) if pnl_usd_values else 0.0,
        avg_pnl_pct=_mean(pnl_values) or 0.0,
        avg_pnl_usd=_mean(pnl_usd_values) or 0.0,
        avg_notional_usd=_mean(notional_values),
        avg_holding_minutes=_mean(holding_values),
        avg_minutes_to_first_profit_50bps=_mean(first_profit_50bps_values),
        avg_minutes_to_first_profit_100bps=_mean(first_profit_100bps_values),
        avg_mfe_pct=_mean(mfe_values),
        avg_mae_pct=_mean(mae_values),
        avg_post_exit_best_pct=_mean(post_exit_best_values),
        avg_post_exit_worst_pct=_mean(post_exit_worst_values),
        avg_volatility_pct=_mean(volatility_values),
        stop_losses=stop_losses,
        take_profits=take_profits,
        protected_profit_exits=protected_profit_exits,
        stale_winner_exits=stale_winner_exits,
    )
    return overview, daily_rows


def _summarize_trade_day(day: str, rows: list[dict[str, Any]]) -> TradeDaySummary:
    pnl_values = [row["pnl_pct"] for row in rows if row["pnl_pct"] is not None]
    pnl_usd_values = [row["pnl_usd"] for row in rows if row["pnl_usd"] is not None]
    notional_values = [row["notional_usd"] for row in rows if row["notional_usd"] is not None]
    holding_values = [row["holding_minutes"] for row in rows if row["holding_minutes"] is not None]
    first_profit_50bps_values = [
        row["minutes_to_first_profit_50bps"]
        for row in rows
        if row["minutes_to_first_profit_50bps"] is not None
    ]
    first_profit_100bps_values = [
        row["minutes_to_first_profit_100bps"]
        for row in rows
        if row["minutes_to_first_profit_100bps"] is not None
    ]
    mfe_values = [row["mfe_pct"] for row in rows if row["mfe_pct"] is not None]
    mae_values = [row["mae_pct"] for row in rows if row["mae_pct"] is not None]
    post_exit_best_values = [
        row["post_exit_best_pct"] for row in rows if row["post_exit_best_pct"] is not None
    ]
    post_exit_worst_values = [
        row["post_exit_worst_pct"] for row in rows if row["post_exit_worst_pct"] is not None
    ]
    volatility_values = [row["volatility_pct"] for row in rows if row["volatility_pct"] is not None]
    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    breakeven = sum(1 for value in pnl_values if value == 0)
    trades = len(rows)
    return TradeDaySummary(
        day=day,
        trades=trades,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        win_rate=(wins / trades) if trades else 0.0,
        total_pnl_pct=sum(pnl_values) if pnl_values else 0.0,
        total_pnl_usd=sum(pnl_usd_values) if pnl_usd_values else 0.0,
        avg_pnl_pct=_mean(pnl_values) or 0.0,
        avg_pnl_usd=_mean(pnl_usd_values) or 0.0,
        avg_notional_usd=_mean(notional_values),
        avg_holding_minutes=_mean(holding_values),
        avg_minutes_to_first_profit_50bps=_mean(first_profit_50bps_values),
        avg_minutes_to_first_profit_100bps=_mean(first_profit_100bps_values),
        stop_losses=_count_reason(rows, "stop_loss"),
        take_profits=_count_reason(rows, "take_profit"),
        protected_profit_exits=_count_reason(rows, "protected_profit"),
        stale_winner_exits=_count_reason(rows, "stale_winner"),
        avg_mfe_pct=_mean(mfe_values),
        avg_mae_pct=_mean(mae_values),
        avg_post_exit_best_pct=_mean(post_exit_best_values),
        avg_post_exit_worst_pct=_mean(post_exit_worst_values),
        avg_volatility_pct=_mean(volatility_values),
    )


def _summarize_portfolio(
    rows: list[dict[str, Any]],
    trade_overview: TradeOverview | None,
) -> PortfolioOverview | None:
    if not rows:
        return None
    open_positions = [row["open_positions"] for row in rows if row["open_positions"] is not None]
    available_balances = [row["available_balance_usd"] for row in rows if row["available_balance_usd"] is not None]
    gross_notional_values = [row["gross_notional_usd"] for row in rows if row["gross_notional_usd"] is not None]
    estimated_capacity_values = [
        row["estimated_additional_positions"]
        for row in rows
        if row["estimated_additional_positions"] is not None
    ]
    timestamps = [row["timestamp"] for row in rows if row["timestamp"] is not None]
    return PortfolioOverview(
        snapshot_count=len(rows),
        first_timestamp=min(timestamps) if timestamps else None,
        last_timestamp=max(timestamps) if timestamps else None,
        avg_open_positions=_mean([float(value) for value in open_positions]) if open_positions else None,
        max_open_positions=max(open_positions) if open_positions else None,
        avg_available_balance_usd=_mean(available_balances),
        min_available_balance_usd=min(available_balances) if available_balances else None,
        max_available_balance_usd=max(available_balances) if available_balances else None,
        avg_gross_notional_usd=_mean(gross_notional_values),
        max_gross_notional_usd=max(gross_notional_values) if gross_notional_values else None,
        avg_estimated_additional_positions=_mean(estimated_capacity_values),
        min_estimated_additional_positions=min(estimated_capacity_values)
        if estimated_capacity_values
        else None,
        max_estimated_additional_positions=max(estimated_capacity_values)
        if estimated_capacity_values
        else None,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _export_csvs(
    export_dir: str,
    signal_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    daily_rows: list[TradeDaySummary],
    portfolio_rows: list[dict[str, Any]],
    include_signals: bool,
) -> None:
    export_path = Path(export_dir).expanduser()
    export_path.mkdir(parents=True, exist_ok=True)
    if include_signals and signal_rows:
        signal_fields = list(signal_rows[0].keys())
        _write_csv(export_path / "signals.csv", signal_rows, signal_fields)
    if trade_rows:
        _write_csv(export_path / "trades.csv", trade_rows, TRADE_EXPORT_COLUMNS)
    if daily_rows:
        daily_dict_rows = [asdict(row) for row in daily_rows]
        _write_csv(
            export_path / "trade_daily_summary.csv",
            daily_dict_rows,
            list(daily_dict_rows[0].keys()),
        )
    if portfolio_rows:
        _write_csv(export_path / "portfolio_snapshots.csv", portfolio_rows, PORTFOLIO_EXPORT_COLUMNS)


def load_report_summary(
    database_path: str,
    top_n: int = 10,
    export_dir: str | None = None,
    include_signals_export: bool = False,
) -> ReportSummary:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        (
            total_rows,
            alerted_rows,
            first_timestamp,
            last_timestamp,
            stage_counts,
            signal_kind_counts,
            top_tickers,
        ) = _load_signal_summary(connection, top_n=top_n)
        trade_rows = _load_trade_rows(connection)
        trade_overview, trade_daily = _summarize_trades(trade_rows)
        portfolio_rows = _load_portfolio_rows(
            connection,
            avg_trade_notional_usd=trade_overview.avg_notional_usd if trade_overview else None,
        )
        portfolio_overview = _summarize_portfolio(portfolio_rows, trade_overview)
        if export_dir is not None:
            signal_rows = (
                [_sanitize_row(row) for row in connection.execute("SELECT * FROM signals ORDER BY rowid")]
                if _table_exists(connection, "signals")
                else []
            )
            _export_csvs(
                export_dir,
                signal_rows,
                trade_rows,
                trade_daily,
                portfolio_rows,
                include_signals=include_signals_export,
            )

    return ReportSummary(
        total_rows=total_rows,
        alerted_rows=alerted_rows,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        stage_counts=stage_counts,
        signal_kind_counts=signal_kind_counts,
        top_tickers=top_tickers,
        trade_overview=trade_overview,
        trade_daily=trade_daily,
        portfolio_overview=portfolio_overview,
    )


def _format_float(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def format_report(summary: ReportSummary) -> str:
    lines = [
        f"Rows: {summary.total_rows}",
        f"Alerted rows: {summary.alerted_rows}",
        f"First timestamp: {summary.first_timestamp or 'n/a'}",
        f"Last timestamp: {summary.last_timestamp or 'n/a'}",
    ]
    lines.append("Stage rows:")
    if not summary.stage_counts:
        lines.append("  none")
    else:
        for stage, total_rows, alerted_rows in summary.stage_counts:
            lines.append(f"  {stage}: rows={total_rows} alerts={alerted_rows}")
    lines.append("Signal kinds:")
    if not summary.signal_kind_counts:
        lines.append("  none")
    else:
        ordered_signal_kinds = sorted(
            summary.signal_kind_counts,
            key=lambda item: (SIGNAL_KIND_ORDER.get(item[0], 99), item[0]),
        )
        legend_rows = [
            f"    {signal_kind}: {SIGNAL_KIND_LABELS[signal_kind]}"
            for signal_kind, _, _ in ordered_signal_kinds
            if signal_kind in SIGNAL_KIND_LABELS
        ]
        if legend_rows:
            lines.append("  legend:")
            lines.extend(legend_rows)
        for signal_kind, total_rows, alerted_rows in ordered_signal_kinds:
            lines.append(f"  {signal_kind}: rows={total_rows} alerts={alerted_rows}")
    lines.append("Top tickers:")
    if not summary.top_tickers:
        lines.append("  none")
    else:
        for ticker, total_rows, alerted_rows, avg_composite, max_composite in summary.top_tickers:
            lines.append(
                f"  {ticker}: rows={total_rows} alerts={alerted_rows} "
                f"avg_composite={avg_composite:.3f} max_composite={max_composite:.3f}"
            )

    trade_overview = getattr(summary, "trade_overview", None)
    if trade_overview is not None:
        lines.append("Trade analytics:")
        lines.append(
            f"  trades={trade_overview.trade_count} wins={trade_overview.wins} "
            f"losses={trade_overview.losses} breakeven={trade_overview.breakeven} "
            f"win_rate={trade_overview.win_rate * 100:.1f}%"
        )
        lines.append(
            f"  realized_pnl_pct={trade_overview.total_pnl_pct:.3f} "
            f"avg_pnl_pct={trade_overview.avg_pnl_pct:.3f} "
            f"realized_pnl_usd={trade_overview.total_pnl_usd:.2f} "
            f"avg_pnl_usd={trade_overview.avg_pnl_usd:.2f}"
        )
        if trade_overview.avg_notional_usd is not None:
            lines.append(f"  avg_notional_usd={trade_overview.avg_notional_usd:.2f}")
        if trade_overview.avg_holding_minutes is not None:
            lines.append(f"  avg_holding_minutes={trade_overview.avg_holding_minutes:.1f}")
        if (
            trade_overview.avg_minutes_to_first_profit_50bps is not None
            or trade_overview.avg_minutes_to_first_profit_100bps is not None
        ):
            lines.append(
                f"  avg_minutes_to_first_profit_50bps={_format_float(trade_overview.avg_minutes_to_first_profit_50bps, 1)} "
                f"avg_minutes_to_first_profit_100bps={_format_float(trade_overview.avg_minutes_to_first_profit_100bps, 1)}"
            )
        if trade_overview.avg_mfe_pct is not None or trade_overview.avg_mae_pct is not None:
            lines.append(
                f"  avg_mfe_pct={_format_float(trade_overview.avg_mfe_pct)} "
                f"avg_mae_pct={_format_float(trade_overview.avg_mae_pct)}"
            )
        if (
            trade_overview.avg_post_exit_best_pct is not None
            or trade_overview.avg_post_exit_worst_pct is not None
            or trade_overview.avg_volatility_pct is not None
        ):
            lines.append(
                f"  avg_post_exit_best_pct={_format_float(trade_overview.avg_post_exit_best_pct)} "
                f"avg_post_exit_worst_pct={_format_float(trade_overview.avg_post_exit_worst_pct)} "
                f"avg_volatility_pct={_format_float(trade_overview.avg_volatility_pct)}"
            )
        lines.append(
            f"  stop_losses={trade_overview.stop_losses} "
            f"protected_profit_exits={trade_overview.protected_profit_exits} "
            f"stale_winner_exits={trade_overview.stale_winner_exits} "
            f"take_profits={trade_overview.take_profits}"
        )
        trade_daily = getattr(summary, "trade_daily", [])
        if trade_daily:
            lines.append("Daily trade results:")
            for row in trade_daily:
                lines.append(
                    f"  {row.day}: trades={row.trades} wins={row.wins} losses={row.losses} "
                    f"breakeven={row.breakeven} win_rate={row.win_rate * 100:.1f}% "
                    f"pnl_pct={row.total_pnl_pct:.3f} pnl_usd={row.total_pnl_usd:.2f} "
                    f"avg_notional_usd={_format_float(row.avg_notional_usd, 2)} "
                    f"avg_holding_minutes={_format_float(row.avg_holding_minutes, 1)} "
                    f"avg_minutes_to_first_profit_50bps={_format_float(row.avg_minutes_to_first_profit_50bps, 1)} "
                    f"avg_minutes_to_first_profit_100bps={_format_float(row.avg_minutes_to_first_profit_100bps, 1)} "
                    f"stop_losses={row.stop_losses} "
                    f"protected_profit_exits={row.protected_profit_exits} "
                    f"stale_winner_exits={row.stale_winner_exits} "
                    f"take_profits={row.take_profits}"
                )
        else:
            lines.append("Daily trade results:")
            lines.append("  none")

    portfolio_overview = getattr(summary, "portfolio_overview", None)
    if portfolio_overview is not None:
        lines.append("Portfolio snapshots:")
        lines.append(
            f"  snapshots={portfolio_overview.snapshot_count} "
            f"first={portfolio_overview.first_timestamp or 'n/a'} "
            f"last={portfolio_overview.last_timestamp or 'n/a'}"
        )
        lines.append(
            f"  open_positions avg={_format_float(portfolio_overview.avg_open_positions)} "
            f"max={portfolio_overview.max_open_positions if portfolio_overview.max_open_positions is not None else 'n/a'}"
        )
        lines.append(
            f"  available_balance_usd avg={_format_float(portfolio_overview.avg_available_balance_usd, 2)} "
            f"min={_format_float(portfolio_overview.min_available_balance_usd, 2)} "
            f"max={_format_float(portfolio_overview.max_available_balance_usd, 2)}"
        )
        lines.append(
            f"  gross_notional_usd avg={_format_float(portfolio_overview.avg_gross_notional_usd, 2)} "
            f"max={_format_float(portfolio_overview.max_gross_notional_usd, 2)}"
        )
        if (
            portfolio_overview.avg_estimated_additional_positions is not None
            or portfolio_overview.min_estimated_additional_positions is not None
            or portfolio_overview.max_estimated_additional_positions is not None
        ):
            lines.append(
                f"  estimated_positions_from_balance avg={_format_float(portfolio_overview.avg_estimated_additional_positions)} "
                f"min={_format_float(portfolio_overview.min_estimated_additional_positions)} "
                f"max={_format_float(portfolio_overview.max_estimated_additional_positions)}"
            )

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize the signal SQLite log.")
    parser.add_argument("--db", type=str, default="signals.sqlite3", help="Path to the SQLite database.")
    parser.add_argument("--top", type=int, default=10, help="Number of top tickers to show.")
    parser.add_argument(
        "--export-dir",
        type=str,
        default=None,
        help="Optional directory for CSV exports of trade analytics and portfolio data.",
    )
    parser.add_argument(
        "--include-signals-export",
        action="store_true",
        help="Also export raw signals.csv. Off by default because the file gets large quickly.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_path = str(Path(args.db).expanduser())
    summary = load_report_summary(
        database_path=database_path,
        top_n=args.top,
        export_dir=args.export_dir,
        include_signals_export=args.include_signals_export,
    )
    print(format_report(summary))


if __name__ == "__main__":
    main()
