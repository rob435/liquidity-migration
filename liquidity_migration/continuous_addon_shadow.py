from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Any

import polars as pl

from ._common import MS_PER_DAY, coerce_int, finite_float
from .storage import read_dataset


@dataclass(frozen=True)
class ContinuousAddonShadowAuditConfig:
    primary_data_root: str
    addon_data_root: str
    historical_blended_trades_csv: str = ""
    primary_trades_dataset: str = "continuous_fade_paper_trades"
    addon_trades_dataset: str = "continuous_fade_paper_trades"
    primary_orders_dataset: str = "continuous_fade_paper_orders"
    addon_orders_dataset: str = "continuous_fade_paper_orders"
    addon_cycles_dataset: str = "continuous_fade_paper_cycles"
    expected_primary_strategy_id: str = ""
    expected_addon_strategy_id: str = ""
    output_dir: str = ""
    report_name: str = "continuous_addon_shadow_audit"
    min_addon_trades: int = 0
    min_matched_addon_keys: int = 0
    max_missing_addon_keys: int = -1
    max_extra_addon_keys: int = -1
    max_missing_addon_key_fraction: float = -1.0
    min_addon_to_primary_ratio: float = -1.0
    max_addon_to_primary_ratio: float = -1.0
    min_active_same_symbol_overlap_fraction: float = -1.0
    max_active_same_symbol_overlap_fraction: float = -1.0
    min_exact_same_entry_fraction: float = -1.0
    max_exact_same_entry_fraction: float = -1.0
    max_historical_anatomy_drift: float = -1.0
    max_addon_top1_weight_share: float = -1.0
    max_addon_top5_weight_share: float = -1.0
    max_addon_top10_weight_share: float = -1.0
    max_historical_concentration_drift: float = -1.0
    max_active_addon_weight: float = -1.0
    max_active_combined_weight: float = -1.0
    max_unit_weight_rows: int = -1
    max_primary_trades_per_day: int = -1
    max_addon_trades_per_day: int = -1
    max_combined_trades_per_day: int = -1
    max_primary_entry_order_attempts_per_day: int = -1
    max_addon_entry_order_attempts_per_day: int = -1
    max_combined_entry_order_attempts_per_day: int = -1
    max_primary_trades_per_symbol_day: int = -1
    max_addon_trades_per_symbol_day: int = -1
    max_combined_trades_per_symbol_day: int = -1
    max_primary_entry_order_attempts_per_symbol_day: int = -1
    max_addon_entry_order_attempts_per_symbol_day: int = -1
    max_combined_entry_order_attempts_per_symbol_day: int = -1
    min_primary_same_symbol_trade_gap_minutes: float = -1.0
    min_addon_same_symbol_trade_gap_minutes: float = -1.0
    min_combined_same_symbol_trade_gap_minutes: float = -1.0
    min_primary_same_symbol_entry_order_gap_minutes: float = -1.0
    min_addon_same_symbol_entry_order_gap_minutes: float = -1.0
    min_combined_same_symbol_entry_order_gap_minutes: float = -1.0
    simulate_addon_same_symbol_trade_cooldown_minutes: float = -1.0
    simulate_addon_same_symbol_entry_order_cooldown_minutes: float = -1.0
    max_primary_unexpected_strategy_rows: int = -1
    max_addon_unexpected_strategy_rows: int = -1
    expected_primary_entry_order_prefix: str = ""
    expected_addon_entry_order_prefix: str = ""
    max_primary_unexpected_entry_order_prefix_rows: int = -1
    max_addon_unexpected_entry_order_prefix_rows: int = -1
    max_primary_repeated_entry_rows: int = -1
    max_addon_repeated_entry_rows: int = -1
    max_primary_repeated_entry_order_rows: int = -1
    max_addon_repeated_entry_order_rows: int = -1
    max_primary_problem_entry_order_attempts: int = -1
    max_addon_problem_entry_order_attempts: int = -1
    max_primary_unmatched_entry_order_attempts: int = -1
    max_addon_unmatched_entry_order_attempts: int = -1
    max_primary_unmatched_live_entry_order_attempts: int = -1
    max_addon_unmatched_live_entry_order_attempts: int = -1
    max_primary_unmatched_entry_order_age_minutes: float = -1.0
    max_addon_unmatched_entry_order_age_minutes: float = -1.0
    max_primary_unmatched_live_entry_order_age_minutes: float = -1.0
    max_addon_unmatched_live_entry_order_age_minutes: float = -1.0
    min_cycle_entry_acceptance_fraction: float = -1.0
    max_cycle_same_signal_reentry_skip_fraction: float = -1.0
    max_cycle_addon_primary_pnl_gate_skip_fraction: float = -1.0
    max_cycle_candidate_pressure: int = -1
    min_worst_cycle_entry_acceptance_fraction: float = -1.0
    max_worst_cycle_same_signal_reentry_skip_fraction: float = -1.0
    max_worst_cycle_addon_primary_pnl_gate_skip_fraction: float = -1.0
    min_addon_cycles: int = 0
    max_latest_cycle_age_minutes: float = -1.0
    max_cycle_gap_minutes: float = -1.0
    audit_now_ms: int = 0


def _str(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "")


def _int(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = coerce_int(row.get(key))
        if value > 0:
            return value
    return 0


def _float(value: Any) -> float:
    # Delegates to the finite-guarded _common.finite_float (code-quality-5): drops the
    # divergent `float(value or 0.0)` idiom for the one NaN/inf coercion policy shared with
    # reconciliation._float / ws_state_cache._float. Research-only file.
    return finite_float(value, default=0.0) or 0.0


def _optional_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            out = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(out):
            return out
    return None


def _cost_corrected_return(row: dict[str, Any]) -> float | None:
    # audit2c: live `net_return` is gross-of-cost (gtr*notional_weight; fees in separate
    # entry_fee_usdt/exit_fee_usdt, funding uncosted -- see continuous_demo DEPLOY NOTE
    # ~L1398), so the cooldown skipped-return aggregation overstated saved/forfeited PnL
    # by round-trip fees (+funding) and could be wrong-signed for marginal trades. Mirror
    # the breaker `net_of_fees` adjustment: subtract realised round-trip fees as a fraction
    # of trade equity. Backtest rows are already NET (net=gross+cost+funding) and carry no
    # entry_fee_usdt/exit_fee_usdt, so no fee is subtracted -- no double-subtraction. A
    # true `funding_return` field, if ever stored live, is folded in with the engine's
    # additive sign (net = gross + funding_return).
    gross = _optional_float(row, "net_return", "return_pct", "return", "gross_return", "gtr")
    if gross is None:
        return None
    net = gross
    entry_fee = row.get("entry_fee_usdt")
    exit_fee = row.get("exit_fee_usdt")
    if entry_fee is not None or exit_fee is not None:
        equity = _optional_float(row, "equity_usdt")
        if equity is not None and equity > 0.0:
            net -= (_float(entry_fee) + _float(exit_fee)) / equity
    funding = _optional_float(row, "funding_return")
    if funding is not None:
        net += funding
    return net


def _entry_ts(row: dict[str, Any]) -> int:
    return _int(row, "entry_ts_ms", "entry_exec_time_ms", "signal_ts_ms", "entry_signal_ts_ms")


def _signal_ts(row: dict[str, Any]) -> int:
    return _int(row, "signal_ts_ms", "entry_signal_ts_ms", "entry_ts_ms")


def _reconciliation_signal_ts(row: dict[str, Any]) -> int:
    """Signal timestamp used to key trade<->order reconciliation.

    Deliberately does NOT fall back to entry_ts_ms (unlike _signal_ts). Orders
    key off their signal_ts_ms, while entry occurs (1 + entry_confirm_delay) bars
    later; if a trade row that dropped signal_ts_ms fell back to entry_ts_ms, its
    key would land on a different timestamp than its order's, producing a spurious
    'unmatched entry order attempt' (shadows-4). Using a single signal-only basis
    keeps trade and order keys consistent on healthy data (which always carries
    signal_ts_ms) and excludes a malformed signal-less trade from the signal key
    set rather than mis-keying it; trade_id reconciliation below is the backstop
    that still matches the order in that degenerate case.
    """
    return _int(row, "signal_ts_ms", "entry_signal_ts_ms")


def _trade_id(row: dict[str, Any]) -> str:
    return _str(row, "trade_id")


def _exit_ts(row: dict[str, Any]) -> int:
    return _int(row, "exit_ts_ms", "exit_exec_time_ms")


def _entry_key(row: dict[str, Any]) -> tuple[str, int]:
    return (_str(row, "symbol"), _reconciliation_signal_ts(row))


def _trade_day(ts_ms: int) -> int:
    return ts_ms // MS_PER_DAY if ts_ms > 0 else 0


def _rows(frame: pl.DataFrame, *, source: str | None = None) -> list[dict[str, Any]]:
    if frame.is_empty():
        return []
    rows = frame.to_dicts()
    if source is None:
        return rows
    if "blend_source" not in frame.columns:
        # audit2: a primary/addon split was explicitly requested but the frame has no
        # blend_source discriminator. The old guard returned the FULL row set for BOTH
        # requests, silently counting every historical trade as BOTH the primary and the
        # add-on leg (addon/primary ratio pinned to 1.0) and feeding the historical
        # anatomy/concentration/key-diff drift gates garbage with no warning. The
        # historical comparison REQUIRES the discriminator -> fail loud instead.
        raise ValueError(
            "blended trade frame is missing the required 'blend_source' column; "
            f"cannot split by source={source!r} (historical drift gate would compare against garbage)"
        )
    return [row for row in rows if _str(row, "blend_source").lower() == source]


def _clean_trade_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _str(row, "symbol"):
            continue
        if _entry_ts(row) <= 0 and _signal_ts(row) <= 0:
            continue
        out.append(row)
    return out


def _is_reduce_only(row: dict[str, Any]) -> bool:
    value = row.get("reduce_only")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _entry_order_key(row: dict[str, Any]) -> tuple[str, int]:
    return (_str(row, "symbol"), _reconciliation_signal_ts(row))


def _entry_order_attempt_ts(row: dict[str, Any]) -> int:
    return _int(row, "ts_ms", "created_ts_ms", "updated_at_ms", "signal_ts_ms", "entry_signal_ts_ms")


def _entry_order_status_rank(row: dict[str, Any]) -> int:
    status = _str(row, "status").lower()
    if status in {"error", "failed", "submitted_unconfirmed"}:
        return 4
    if status in {"submitted", "partial"}:
        return 3
    if status == "filled":
        return 2
    if status == "planned":
        return 1
    return 0


def _is_problem_entry_order_status(row: dict[str, Any]) -> bool:
    return _str(row, "status").lower() in {"error", "failed", "submitted_unconfirmed"}


def _is_live_entry_order_status(row: dict[str, Any]) -> bool:
    return _str(row, "status").lower() in {"filled", "partial", "submitted", "submitted_unconfirmed"}


def _clean_entry_order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if _is_reduce_only(row) or not _str(row, "symbol"):
            continue
        if _signal_ts(row) <= 0:
            continue
        order_id = _str(row, "order_link_id") or _str(row, "trade_id")
        dedupe_key = (_str(row, "symbol"), order_id)
        if order_id:
            existing = deduped.get(dedupe_key)
            if existing is None or _entry_order_status_rank(row) > _entry_order_status_rank(existing):
                deduped[dedupe_key] = row
            continue
        out.append(row)
    out.extend(deduped.values())
    return out


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = _str(row, "status").lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _filter_by_strategy_id(rows: list[dict[str, Any]], strategy_id: str) -> list[dict[str, Any]]:
    """Keep only rows whose strategy_id matches (used for the same-root split)."""
    return [row for row in rows if _str(row, "strategy_id") == strategy_id]


def _strategy_id_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _clean_trade_rows(rows):
        strategy_id = _str(row, "strategy_id") or "missing"
        counts[strategy_id] = counts.get(strategy_id, 0) + 1
    return counts


def _strategy_identity_summary(rows: list[dict[str, Any]], *, expected_strategy_id: str) -> dict[str, Any]:
    counts = _strategy_id_counts(rows)
    total = sum(counts.values())
    expected = counts.get(expected_strategy_id, 0) if expected_strategy_id else 0
    return {
        "expected_strategy_id": expected_strategy_id,
        "strategy_counts": counts,
        "expected_strategy_rows": expected,
        "unexpected_strategy_rows": total - expected if expected_strategy_id else 0,
        "missing_strategy_rows": counts.get("missing", 0),
    }


def _entry_order_link_prefix(row: dict[str, Any]) -> str:
    link = _str(row, "order_link_id")
    if not link:
        return "missing"
    parts = link.split("-")
    if len(parts) >= 4 and parts[0] == "lm" and parts[1] == "en" and parts[2] == "ca":
        return "lm-en-ca"
    if len(parts) >= 3 and parts[0] == "lm" and parts[1] == "en":
        return "-".join(parts[:3])
    return "unknown"


def _entry_order_link_prefix_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _clean_entry_order_rows(rows):
        prefix = _entry_order_link_prefix(row)
        counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def _entry_order_prefix_summary(rows: list[dict[str, Any]], *, expected_prefix: str) -> dict[str, Any]:
    counts = _entry_order_link_prefix_counts(rows)
    total = sum(counts.values())
    expected = counts.get(expected_prefix, 0) if expected_prefix else 0
    return {
        "expected_entry_order_prefix": expected_prefix,
        "entry_order_prefix_counts": counts,
        "expected_entry_order_prefix_rows": expected,
        "unexpected_entry_order_prefix_rows": total - expected if expected_prefix else 0,
        "missing_entry_order_prefix_rows": counts.get("missing", 0),
    }


def _key_repeat_summary(rows: list[dict[str, Any]], key_fn) -> dict[str, int]:
    counts: dict[tuple[str, int], int] = {}
    for row in rows:
        key = key_fn(row)
        if not key[0] or key[1] <= 0:
            continue
        counts[key] = counts.get(key, 0) + 1
    repeated = [count for count in counts.values() if count > 1]
    return {
        "repeated_entry_keys": len(repeated),
        "repeated_entry_rows": sum(count - 1 for count in repeated),
        "max_entries_per_entry_key": max(counts.values(), default=0),
    }


def _entry_key_repeat_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return _key_repeat_summary(_clean_trade_rows(rows), _entry_key)


def _trade_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _clean_trade_rows(rows)
    symbols = {_str(row, "symbol") for row in rows if _str(row, "symbol")}
    days = {_trade_day(_entry_ts(row)) for row in rows if _trade_day(_entry_ts(row)) > 0}
    summary = {
        "trades": len(rows),
        "unique_symbols": len(symbols),
        "trade_days": len(days),
        "avg_trades_per_trade_day": len(rows) / len(days) if days else 0.0,
        "max_trades_per_day": max((sum(1 for row in rows if _trade_day(_entry_ts(row)) == day) for day in days), default=0),
        "status_counts": _status_counts(rows),
    }
    summary.update(_entry_key_repeat_summary(rows))
    return summary


def _entry_order_attempt_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _clean_entry_order_rows(rows)
    symbols = {_str(row, "symbol") for row in rows if _str(row, "symbol")}
    summary = {
        "entry_order_attempts": len(rows),
        "unique_symbols": len(symbols),
        "problem_entry_order_attempts": sum(1 for row in rows if _is_problem_entry_order_status(row)),
        "status_counts": _status_counts(rows),
    }
    summary.update(_key_repeat_summary(rows, _entry_order_key))
    return summary


def _trade_key_set(rows: list[dict[str, Any]]) -> set[tuple[str, int]]:
    return {
        _entry_key(row)
        for row in _clean_trade_rows(rows)
        if _entry_key(row)[0] and _entry_key(row)[1] > 0
    }


def _trade_id_set(rows: list[dict[str, Any]]) -> set[str]:
    """Non-empty trade_ids of recorded trades, for trade_id-based reconciliation.

    The order's trade_id is the authoritative link to its trade, independent of
    timestamps. Matching on it as well as the signal key means a trade that
    dropped signal_ts_ms (so its signal key is excluded) still reconciles its
    order instead of producing a spurious unmatched-attempt flag (shadows-4).
    """
    return {tid for row in _clean_trade_rows(rows) if (tid := _trade_id(row))}


def _entry_order_trade_reconciliation_summary(
    *,
    order_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    now_ms: int = 0,
) -> dict[str, Any]:
    trade_keys = _trade_key_set(trade_rows)
    trade_ids = _trade_id_set(trade_rows)
    unmatched = [
        row
        for row in _clean_entry_order_rows(order_rows)
        if _entry_order_key(row)[0]
        and _entry_order_key(row)[1] > 0
        and _entry_order_key(row) not in trade_keys
        and not (_trade_id(row) and _trade_id(row) in trade_ids)
    ]
    unmatched_live = [row for row in unmatched if _is_live_entry_order_status(row)]
    return {
        "entry_order_attempts_without_trade_key": len(unmatched),
        "live_entry_order_attempts_without_trade_key": len(unmatched_live),
        "max_unmatched_entry_order_age_minutes": max(
            (_entry_order_age_minutes(row, now_ms=now_ms) for row in unmatched),
            default=0.0,
        ),
        "max_unmatched_live_entry_order_age_minutes": max(
            (_entry_order_age_minutes(row, now_ms=now_ms) for row in unmatched_live),
            default=0.0,
        ),
    }


def _entry_order_age_minutes(row: dict[str, Any], *, now_ms: int) -> float:
    ts_ms = _entry_order_attempt_ts(row)
    if now_ms <= 0 or ts_ms <= 0:
        return 0.0
    return max(0.0, (now_ms - ts_ms) / 60_000)


def _unmatched_entry_order_attempt_rows(
    *,
    source: str,
    order_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    now_ms: int = 0,
) -> list[dict[str, Any]]:
    trade_keys = _trade_key_set(trade_rows)
    # audit2: mirror the shadows-4 trade_id backstop the SUMMARY path uses
    # (_entry_order_trade_reconciliation_summary). Without it, a trade that dropped
    # signal_ts_ms but kept its authoritative trade_id (the crash-recovery/schema-drift
    # case) is excluded from the summary's unmatched count (0) yet still emitted here as
    # a phantom orphaned add-on order in the *_unmatched_entry_order_attempts.csv the
    # operator audits — same audit, contradictory artifacts.
    trade_ids = _trade_id_set(trade_rows)
    out: list[dict[str, Any]] = []
    for row in _clean_entry_order_rows(order_rows):
        key = _entry_order_key(row)
        if not key[0] or key[1] <= 0 or key in trade_keys:
            continue
        if _trade_id(row) and _trade_id(row) in trade_ids:
            continue
        out.append(
            {
                "source": source,
                "symbol": _str(row, "symbol"),
                "signal_ts_ms": _signal_ts(row),
                "order_link_id": _str(row, "order_link_id"),
                "trade_id": _str(row, "trade_id"),
                "status": _str(row, "status").lower() or "unknown",
                "ts_ms": _entry_order_attempt_ts(row),
                "submit_mode": _str(row, "submit_mode"),
                "entry_order_prefix": _entry_order_link_prefix(row),
                "live_order_status": _is_live_entry_order_status(row),
                "age_minutes": _entry_order_age_minutes(row, now_ms=now_ms),
            }
        )
    out.sort(
        key=lambda item: (
            str(item["source"]),
            str(item["symbol"]),
            int(item["signal_ts_ms"]),
            str(item["order_link_id"]),
            str(item["trade_id"]),
        )
    )
    return out


def _entry_order_attempt_rows(*, source: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = _clean_entry_order_rows(rows)
    key_counts: dict[tuple[str, int], int] = {}
    for row in cleaned:
        key = _entry_order_key(row)
        key_counts[key] = key_counts.get(key, 0) + 1
    out: list[dict[str, Any]] = [
        {
            "source": source,
            "symbol": _str(row, "symbol"),
            "signal_ts_ms": _signal_ts(row),
            "entry_order_attempts_for_key": key_counts.get(_entry_order_key(row), 0),
            "order_link_id": _str(row, "order_link_id"),
            "trade_id": _str(row, "trade_id"),
            "status": _str(row, "status").lower() or "unknown",
            # Use the same fallback chain as bucketing (and the sibling unmatched-attempts
            # CSV at ~line 489) so per-row ts_ms reconciles against the daily/symbol-day
            # counts instead of emitting 0 for rows keyed by signal_ts (audit-iter3 backlog).
            "ts_ms": _entry_order_attempt_ts(row),
            "submit_mode": _str(row, "submit_mode"),
            "error": _str(row, "error"),
        }
        for row in cleaned
    ]
    out.sort(
        key=lambda item: (
            str(item["source"]),
            str(item["symbol"]),
            int(item["signal_ts_ms"]),
            str(item["order_link_id"]),
            str(item["trade_id"]),
        )
    )
    return out


def _count_by_trade_day(rows: list[dict[str, Any]], ts_fn) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in rows:
        ts_ms = ts_fn(row)
        if ts_ms <= 0:
            continue
        day = _trade_day(ts_ms)
        counts[day] = counts.get(day, 0) + 1
    return counts


def _daily_ticket_rows(
    *,
    primary_rows: list[dict[str, Any]],
    addon_rows: list[dict[str, Any]],
    primary_order_rows: list[dict[str, Any]],
    addon_order_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    primary_trades = _count_by_trade_day(_clean_trade_rows(primary_rows), _entry_ts)
    addon_trades = _count_by_trade_day(_clean_trade_rows(addon_rows), _entry_ts)
    primary_orders = _count_by_trade_day(_clean_entry_order_rows(primary_order_rows), _entry_order_attempt_ts)
    addon_orders = _count_by_trade_day(_clean_entry_order_rows(addon_order_rows), _entry_order_attempt_ts)
    days = sorted(set(primary_trades) | set(addon_trades) | set(primary_orders) | set(addon_orders))
    out: list[dict[str, Any]] = []
    for day in days:
        primary_trade_count = primary_trades.get(day, 0)
        addon_trade_count = addon_trades.get(day, 0)
        primary_order_count = primary_orders.get(day, 0)
        addon_order_count = addon_orders.get(day, 0)
        out.append(
            {
                "trade_day": day,
                "day_start_ts_ms": day * MS_PER_DAY,
                "primary_trades": primary_trade_count,
                "addon_trades": addon_trade_count,
                "combined_trades": primary_trade_count + addon_trade_count,
                "primary_entry_order_attempts": primary_order_count,
                "addon_entry_order_attempts": addon_order_count,
                "combined_entry_order_attempts": primary_order_count + addon_order_count,
            }
        )
    return out


def _daily_ticket_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    peak_trade = max(rows, key=lambda row: int(row["combined_trades"]), default=None)
    peak_order = max(rows, key=lambda row: int(row["combined_entry_order_attempts"]), default=None)
    return {
        "days": len(rows),
        "max_primary_trades_per_day": max((int(row["primary_trades"]) for row in rows), default=0),
        "max_addon_trades_per_day": max((int(row["addon_trades"]) for row in rows), default=0),
        "max_combined_trades_per_day": max((int(row["combined_trades"]) for row in rows), default=0),
        "max_combined_trades_day": int(peak_trade["trade_day"]) if peak_trade else 0,
        "max_primary_entry_order_attempts_per_day": max(
            (int(row["primary_entry_order_attempts"]) for row in rows),
            default=0,
        ),
        "max_addon_entry_order_attempts_per_day": max(
            (int(row["addon_entry_order_attempts"]) for row in rows),
            default=0,
        ),
        "max_combined_entry_order_attempts_per_day": max(
            (int(row["combined_entry_order_attempts"]) for row in rows),
            default=0,
        ),
        "max_combined_entry_order_attempts_day": int(peak_order["trade_day"]) if peak_order else 0,
    }


def _count_by_symbol_trade_day(rows: list[dict[str, Any]], ts_fn) -> dict[tuple[str, int], int]:
    counts: dict[tuple[str, int], int] = {}
    for row in rows:
        symbol = _str(row, "symbol")
        ts_ms = ts_fn(row)
        if not symbol or ts_ms <= 0:
            continue
        key = (symbol, _trade_day(ts_ms))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _symbol_day_ticket_rows(
    *,
    primary_rows: list[dict[str, Any]],
    addon_rows: list[dict[str, Any]],
    primary_order_rows: list[dict[str, Any]],
    addon_order_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    primary_trades = _count_by_symbol_trade_day(_clean_trade_rows(primary_rows), _entry_ts)
    addon_trades = _count_by_symbol_trade_day(_clean_trade_rows(addon_rows), _entry_ts)
    primary_orders = _count_by_symbol_trade_day(_clean_entry_order_rows(primary_order_rows), _entry_order_attempt_ts)
    addon_orders = _count_by_symbol_trade_day(_clean_entry_order_rows(addon_order_rows), _entry_order_attempt_ts)
    keys = sorted(set(primary_trades) | set(addon_trades) | set(primary_orders) | set(addon_orders))
    out: list[dict[str, Any]] = []
    for symbol, day in keys:
        primary_trade_count = primary_trades.get((symbol, day), 0)
        addon_trade_count = addon_trades.get((symbol, day), 0)
        primary_order_count = primary_orders.get((symbol, day), 0)
        addon_order_count = addon_orders.get((symbol, day), 0)
        out.append(
            {
                "trade_day": day,
                "day_start_ts_ms": day * MS_PER_DAY,
                "symbol": symbol,
                "primary_trades": primary_trade_count,
                "addon_trades": addon_trade_count,
                "combined_trades": primary_trade_count + addon_trade_count,
                "primary_entry_order_attempts": primary_order_count,
                "addon_entry_order_attempts": addon_order_count,
                "combined_entry_order_attempts": primary_order_count + addon_order_count,
            }
        )
    return out


def _symbol_day_ticket_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    peak_trade = max(rows, key=lambda row: int(row["combined_trades"]), default=None)
    peak_order = max(rows, key=lambda row: int(row["combined_entry_order_attempts"]), default=None)
    return {
        "symbol_days": len(rows),
        "max_primary_trades_per_symbol_day": max((int(row["primary_trades"]) for row in rows), default=0),
        "max_addon_trades_per_symbol_day": max((int(row["addon_trades"]) for row in rows), default=0),
        "max_combined_trades_per_symbol_day": max((int(row["combined_trades"]) for row in rows), default=0),
        "max_combined_trades_symbol": str(peak_trade["symbol"]) if peak_trade else "",
        "max_combined_trades_day": int(peak_trade["trade_day"]) if peak_trade else 0,
        "max_primary_entry_order_attempts_per_symbol_day": max(
            (int(row["primary_entry_order_attempts"]) for row in rows),
            default=0,
        ),
        "max_addon_entry_order_attempts_per_symbol_day": max(
            (int(row["addon_entry_order_attempts"]) for row in rows),
            default=0,
        ),
        "max_combined_entry_order_attempts_per_symbol_day": max(
            (int(row["combined_entry_order_attempts"]) for row in rows),
            default=0,
        ),
        "max_combined_entry_order_attempts_symbol": str(peak_order["symbol"]) if peak_order else "",
        "max_combined_entry_order_attempts_day": int(peak_order["trade_day"]) if peak_order else 0,
    }


def _trade_gap_events(source: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in _clean_trade_rows(rows):
        ts_ms = _entry_ts(row)
        if ts_ms <= 0:
            continue
        events.append(
            {
                "event_source": source,
                "symbol": _str(row, "symbol"),
                "ts_ms": ts_ms,
                "trade_id": _str(row, "trade_id"),
                "order_link_id": "",
                "signal_ts_ms": _signal_ts(row),
                "status": _str(row, "status").lower() or "unknown",
                # audit2c: cost-correct gross-of-fee live net_return before the cooldown
                # skipped-return aggregation (mirrors the breaker net_of_fees adjustment).
                "return_value": _cost_corrected_return(row),
            }
        )
    return events


def _entry_order_gap_events(source: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in _clean_entry_order_rows(rows):
        ts_ms = _entry_order_attempt_ts(row)
        if ts_ms <= 0:
            continue
        events.append(
            {
                "event_source": source,
                "symbol": _str(row, "symbol"),
                "ts_ms": ts_ms,
                "trade_id": _str(row, "trade_id"),
                "order_link_id": _str(row, "order_link_id"),
                "signal_ts_ms": _signal_ts(row),
                "status": _str(row, "status").lower() or "unknown",
                "return_value": None,
            }
        )
    return events


def _same_symbol_entry_gap_rows(
    *,
    primary_rows: list[dict[str, Any]],
    addon_rows: list[dict[str, Any]],
    primary_order_rows: list[dict[str, Any]],
    addon_order_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    primary_trade_events = _trade_gap_events("primary", primary_rows)
    addon_trade_events = _trade_gap_events("addon", addon_rows)
    primary_order_events = _entry_order_gap_events("primary", primary_order_rows)
    addon_order_events = _entry_order_gap_events("addon", addon_order_rows)
    out: list[dict[str, Any]] = []
    for source, record_type, events in (
        ("primary", "trade", primary_trade_events),
        ("addon", "trade", addon_trade_events),
        ("combined", "trade", [*primary_trade_events, *addon_trade_events]),
        ("primary", "entry_order_attempt", primary_order_events),
        ("addon", "entry_order_attempt", addon_order_events),
        ("combined", "entry_order_attempt", [*primary_order_events, *addon_order_events]),
    ):
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_symbol.setdefault(str(event["symbol"]), []).append(event)
        for symbol, symbol_events in by_symbol.items():
            if not symbol:
                continue
            symbol_events.sort(
                key=lambda event: (
                    int(event["ts_ms"]),
                    str(event["event_source"]),
                    str(event["trade_id"]),
                    str(event["order_link_id"]),
                )
            )
            for previous, current in zip(symbol_events, symbol_events[1:], strict=False):
                gap_minutes = max(0.0, (int(current["ts_ms"]) - int(previous["ts_ms"])) / 60_000)
                out.append(
                    {
                        "source": source,
                        "record_type": record_type,
                        "symbol": symbol,
                        "previous_source": str(previous["event_source"]),
                        "current_source": str(current["event_source"]),
                        "previous_ts_ms": int(previous["ts_ms"]),
                        "ts_ms": int(current["ts_ms"]),
                        "gap_minutes": gap_minutes,
                        "previous_signal_ts_ms": int(previous["signal_ts_ms"]),
                        "signal_ts_ms": int(current["signal_ts_ms"]),
                        "previous_trade_id": str(previous["trade_id"]),
                        "trade_id": str(current["trade_id"]),
                        "previous_order_link_id": str(previous["order_link_id"]),
                        "order_link_id": str(current["order_link_id"]),
                        "previous_status": str(previous["status"]),
                        "status": str(current["status"]),
                    }
                )
    out.sort(key=lambda row: (str(row["record_type"]), str(row["source"]), float(row["gap_minutes"]), str(row["symbol"])))
    return out


def _same_symbol_entry_gap_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def min_gap(source: str, record_type: str) -> float:
        return min(
            (
                float(row["gap_minutes"])
                for row in rows
                if str(row["source"]) == source and str(row["record_type"]) == record_type
            ),
            default=math.inf,
        )

    return {
        "gap_rows": len(rows),
        "min_primary_same_symbol_trade_gap_minutes": min_gap("primary", "trade"),
        "min_addon_same_symbol_trade_gap_minutes": min_gap("addon", "trade"),
        "min_combined_same_symbol_trade_gap_minutes": min_gap("combined", "trade"),
        "min_primary_same_symbol_entry_order_gap_minutes": min_gap("primary", "entry_order_attempt"),
        "min_addon_same_symbol_entry_order_gap_minutes": min_gap("addon", "entry_order_attempt"),
        "min_combined_same_symbol_entry_order_gap_minutes": min_gap("combined", "entry_order_attempt"),
    }


def _same_symbol_cooldown_simulation_rows(
    *,
    source: str,
    record_type: str,
    events: list[dict[str, Any]],
    cooldown_minutes: float,
) -> list[dict[str, Any]]:
    if cooldown_minutes < 0.0:
        return []
    cooldown_ms = int(cooldown_minutes * 60_000)
    last_accepted_by_symbol: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for event in sorted(
        events,
        key=lambda item: (
            str(item["symbol"]),
            int(item["ts_ms"]),
            str(item["trade_id"]),
            str(item["order_link_id"]),
        ),
    ):
        symbol = str(event["symbol"])
        if not symbol:
            continue
        previous = last_accepted_by_symbol.get(symbol)
        if previous is None:
            last_accepted_by_symbol[symbol] = event
            continue
        gap_ms = int(event["ts_ms"]) - int(previous["ts_ms"])
        if gap_ms >= cooldown_ms:
            last_accepted_by_symbol[symbol] = event
            continue
        out.append(
            {
                "source": source,
                "record_type": record_type,
                "symbol": symbol,
                "cooldown_minutes": cooldown_minutes,
                "gap_minutes": max(0.0, gap_ms / 60_000),
                "previous_ts_ms": int(previous["ts_ms"]),
                "ts_ms": int(event["ts_ms"]),
                "previous_signal_ts_ms": int(previous["signal_ts_ms"]),
                "signal_ts_ms": int(event["signal_ts_ms"]),
                "previous_trade_id": str(previous["trade_id"]),
                "trade_id": str(event["trade_id"]),
                "previous_order_link_id": str(previous["order_link_id"]),
                "order_link_id": str(event["order_link_id"]),
                "previous_status": str(previous["status"]),
                "status": str(event["status"]),
                "previous_return_value": previous.get("return_value"),
                "return_value": event.get("return_value"),
            }
        )
    out.sort(key=lambda row: (str(row["record_type"]), str(row["symbol"]), int(row["ts_ms"])))
    return out


def _cooldown_simulation_summary(
    *,
    trade_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    trade_cooldown_minutes: float,
    entry_order_cooldown_minutes: float,
) -> dict[str, Any]:
    trade_candidates = len(_trade_gap_events("addon", trade_rows))
    order_candidates = len(_entry_order_gap_events("addon", order_rows))
    skipped_trade_rows = _same_symbol_cooldown_simulation_rows(
        source="addon",
        record_type="trade",
        events=_trade_gap_events("addon", trade_rows),
        cooldown_minutes=trade_cooldown_minutes,
    )
    skipped_order_rows = _same_symbol_cooldown_simulation_rows(
        source="addon",
        record_type="entry_order_attempt",
        events=_entry_order_gap_events("addon", order_rows),
        cooldown_minutes=entry_order_cooldown_minutes,
    )
    skipped_symbols: dict[str, int] = {}
    for row in [*skipped_trade_rows, *skipped_order_rows]:
        symbol = str(row["symbol"])
        skipped_symbols[symbol] = skipped_symbols.get(symbol, 0) + 1
    skipped_trade_returns = [
        float(row["return_value"])
        for row in skipped_trade_rows
        if row.get("return_value") is not None
    ]
    return {
        "trade_cooldown_minutes": trade_cooldown_minutes,
        "entry_order_cooldown_minutes": entry_order_cooldown_minutes,
        "trade_candidates": trade_candidates,
        "entry_order_candidates": order_candidates,
        "skipped_trades": len(skipped_trade_rows),
        "skipped_entry_order_attempts": len(skipped_order_rows),
        "accepted_trades": trade_candidates - len(skipped_trade_rows),
        "accepted_entry_order_attempts": order_candidates - len(skipped_order_rows),
        "trade_suppression_fraction": _ratio(len(skipped_trade_rows), trade_candidates),
        "entry_order_suppression_fraction": _ratio(len(skipped_order_rows), order_candidates),
        "skipped_symbols": len(skipped_symbols),
        "max_skips_per_symbol": max(skipped_symbols.values(), default=0),
        "skipped_trade_return_observations": len(skipped_trade_returns),
        "skipped_trade_return_sum": sum(skipped_trade_returns),
        "skipped_trade_return_mean": (
            sum(skipped_trade_returns) / len(skipped_trade_returns) if skipped_trade_returns else 0.0
        ),
        "skipped_trade_positive_return_fraction": _ratio(
            sum(1 for value in skipped_trade_returns if value > 0.0),
            len(skipped_trade_returns),
        ),
    }


def _cooldown_simulation_rows(
    *,
    addon_rows: list[dict[str, Any]],
    addon_order_rows: list[dict[str, Any]],
    trade_cooldown_minutes: float,
    entry_order_cooldown_minutes: float,
) -> list[dict[str, Any]]:
    return [
        *_same_symbol_cooldown_simulation_rows(
            source="addon",
            record_type="trade",
            events=_trade_gap_events("addon", addon_rows),
            cooldown_minutes=trade_cooldown_minutes,
        ),
        *_same_symbol_cooldown_simulation_rows(
            source="addon",
            record_type="entry_order_attempt",
            events=_entry_order_gap_events("addon", addon_order_rows),
            cooldown_minutes=entry_order_cooldown_minutes,
        ),
    ]


def _daily_ticket_contributor_rows(
    *,
    primary_rows: list[dict[str, Any]],
    addon_rows: list[dict[str, Any]],
    primary_order_rows: list[dict[str, Any]],
    addon_order_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source, rows in (("primary", _clean_trade_rows(primary_rows)), ("addon", _clean_trade_rows(addon_rows))):
        for row in rows:
            ts_ms = _entry_ts(row)
            if ts_ms <= 0:
                continue
            out.append(
                {
                    "trade_day": _trade_day(ts_ms),
                    "day_start_ts_ms": _trade_day(ts_ms) * MS_PER_DAY,
                    "record_type": "trade",
                    "source": source,
                    "symbol": _str(row, "symbol"),
                    "signal_ts_ms": _signal_ts(row),
                    "ts_ms": ts_ms,
                    "entry_ts_ms": _entry_ts(row),
                    "exit_ts_ms": _exit_ts(row),
                    "trade_id": _str(row, "trade_id"),
                    "order_link_id": "",
                    "entry_order_prefix": "",
                    "status": _str(row, "status").lower() or "unknown",
                }
            )
    for source, rows in (
        ("primary", _clean_entry_order_rows(primary_order_rows)),
        ("addon", _clean_entry_order_rows(addon_order_rows)),
    ):
        for row in rows:
            ts_ms = _entry_order_attempt_ts(row)
            if ts_ms <= 0:
                continue
            out.append(
                {
                    "trade_day": _trade_day(ts_ms),
                    "day_start_ts_ms": _trade_day(ts_ms) * MS_PER_DAY,
                    "record_type": "entry_order_attempt",
                    "source": source,
                    "symbol": _str(row, "symbol"),
                    "signal_ts_ms": _signal_ts(row),
                    "ts_ms": ts_ms,
                    "entry_ts_ms": 0,
                    "exit_ts_ms": 0,
                    "trade_id": _str(row, "trade_id"),
                    "order_link_id": _str(row, "order_link_id"),
                    "entry_order_prefix": _entry_order_link_prefix(row),
                    "status": _str(row, "status").lower() or "unknown",
                }
            )
    out.sort(
        key=lambda item: (
            int(item["trade_day"]),
            int(item["ts_ms"]),
            str(item["record_type"]),
            str(item["source"]),
            str(item["symbol"]),
            str(item["trade_id"]),
            str(item["order_link_id"]),
        )
    )
    return out


def _primary_intervals(primary_rows: list[dict[str, Any]]) -> dict[str, list[tuple[int, int]]]:
    intervals: dict[str, list[tuple[int, int]]] = {}
    for row in _clean_trade_rows(primary_rows):
        symbol = _str(row, "symbol")
        start = _entry_ts(row)
        if not symbol or start <= 0:
            continue
        end = _exit_ts(row)
        status = _str(row, "status").lower()
        if end <= 0 or status == "open":
            end = 9_223_372_036_854_775_807
        intervals.setdefault(symbol, []).append((start, end))
    for vals in intervals.values():
        vals.sort()
    return intervals


def _overlap_summary(primary_rows: list[dict[str, Any]], addon_rows: list[dict[str, Any]]) -> dict[str, int]:
    intervals = _primary_intervals(primary_rows)
    primary_entry_by_symbol = {
        (_str(row, "symbol"), _entry_ts(row))
        for row in _clean_trade_rows(primary_rows)
        if _str(row, "symbol") and _entry_ts(row) > 0
    }
    overlap = 0
    exact_entry = 0
    for row in _clean_trade_rows(addon_rows):
        symbol = _str(row, "symbol")
        ts = _entry_ts(row)
        if not symbol or ts <= 0:
            continue
        if any(start <= ts < end for start, end in intervals.get(symbol, [])):
            overlap += 1
        if (symbol, ts) in primary_entry_by_symbol:
            exact_entry += 1
    return {
        "active_same_symbol_primary": overlap,
        "exact_same_entry_primary": exact_entry,
    }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator > 0:
        return numerator / denominator
    return math.inf if numerator > 0 else 0.0


def _anatomy_summary(
    *,
    primary_summary: dict[str, Any],
    addon_summary: dict[str, Any],
    overlap_summary: dict[str, int],
) -> dict[str, float]:
    primary_trades = int(primary_summary["trades"])
    addon_trades = int(addon_summary["trades"])
    return {
        "addon_to_primary_ratio": _ratio(addon_trades, primary_trades),
        "active_same_symbol_overlap_fraction": _ratio(
            int(overlap_summary["active_same_symbol_primary"]), addon_trades
        ),
        "exact_same_entry_fraction": _ratio(int(overlap_summary["exact_same_entry_primary"]), addon_trades),
    }


def _anatomy_drift(shadow: dict[str, float], historical: dict[str, float]) -> dict[str, float]:
    return {
        key: abs(float(shadow[key]) - float(historical[key]))
        for key in (
            "addon_to_primary_ratio",
            "active_same_symbol_overlap_fraction",
            "exact_same_entry_fraction",
        )
    }


def _row_weight(row: dict[str, Any]) -> float:
    return _row_weight_with_source(row)[0]


def _row_weight_source(row: dict[str, Any]) -> str:
    return _row_weight_with_source(row)[1]


def _row_weight_with_source(row: dict[str, Any]) -> tuple[float, str]:
    for key in ("notional_usdt", "filled_notional", "entry_notional_usdt", "accepted_notional", "notional_weight"):
        value = abs(_float(row.get(key)))
        if value > 0.0:
            return value, key
    entry_price = abs(_float(row.get("entry_price")))
    qty = abs(_float(row.get("qty")))
    if entry_price > 0.0 and qty > 0.0:
        return entry_price * qty, "entry_price_qty"
    return 1.0, "unit"


def _weight_source_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _clean_trade_rows(rows):
        source = _row_weight_source(row)
        counts[source] = counts.get(source, 0) + 1
    return counts


def _weight_source_summary(primary_rows: list[dict[str, Any]], addon_rows: list[dict[str, Any]]) -> dict[str, Any]:
    primary_counts = _weight_source_counts(primary_rows)
    addon_counts = _weight_source_counts(addon_rows)
    return {
        "primary": primary_counts,
        "addon": addon_counts,
        "primary_unit_weight_rows": int(primary_counts.get("unit", 0)),
        "addon_unit_weight_rows": int(addon_counts.get("unit", 0)),
        "combined_unit_weight_rows": int(primary_counts.get("unit", 0)) + int(addon_counts.get("unit", 0)),
    }


def _symbol_concentration_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weights: dict[str, float] = {}
    trade_counts: dict[str, int] = {}
    for row in _clean_trade_rows(rows):
        symbol = _str(row, "symbol")
        if not symbol:
            continue
        weights[symbol] = weights.get(symbol, 0.0) + _row_weight(row)
        trade_counts[symbol] = trade_counts.get(symbol, 0) + 1
    total = sum(weights.values())
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    out: list[dict[str, Any]] = []
    cumulative = 0.0
    for rank, (symbol, weight) in enumerate(ranked, start=1):
        share = weight / total if total > 0.0 else 0.0
        cumulative += share
        out.append(
            {
                "rank": rank,
                "symbol": symbol,
                "trades": trade_counts.get(symbol, 0),
                "weight": weight,
                "weight_share": share,
                "cumulative_weight_share": cumulative,
            }
        )
    return out


def _symbol_concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    concentration_rows = _symbol_concentration_rows(rows)
    total = sum(float(row["weight"]) for row in concentration_rows)

    def top_share(n: int) -> float:
        return sum(float(row["weight"]) for row in concentration_rows[:n]) / total if total > 0.0 else 0.0

    return {
        "symbols": len(concentration_rows),
        "total_weight": total,
        "top1_weight_share": top_share(1),
        "top5_weight_share": top_share(5),
        "top10_weight_share": top_share(10),
        "top1_symbol": str(concentration_rows[0]["symbol"]) if concentration_rows else "",
    }


def _trade_active_at(row: dict[str, Any], ts_ms: int) -> bool:
    start = _entry_ts(row)
    if start <= 0 or ts_ms < start:
        return False
    end = _exit_ts(row)
    status = _str(row, "status").lower()
    return end <= 0 or status == "open" or ts_ms < end


def _current_open_trade_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _clean_trade_rows(rows):
        status = _str(row, "status").lower()
        if status == "open" or _exit_ts(row) <= 0:
            out.append(row)
    return out


def _active_trade_rows_at(rows: list[dict[str, Any]], ts_ms: int) -> list[dict[str, Any]]:
    return [row for row in _clean_trade_rows(rows) if _trade_active_at(row, ts_ms)]


def _weight_sum(rows: list[dict[str, Any]]) -> float:
    return sum(_row_weight(row) for row in rows)


def _active_exposure_rows(primary_rows: list[dict[str, Any]], addon_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary_clean = _clean_trade_rows(primary_rows)
    addon_clean = _clean_trade_rows(addon_rows)
    timestamps = sorted(
        {
            _entry_ts(row)
            for row in [*primary_clean, *addon_clean]
            if _entry_ts(row) > 0
        }
    )
    out: list[dict[str, Any]] = []
    for ts_ms in timestamps:
        active_primary = _active_trade_rows_at(primary_clean, ts_ms)
        active_addon = _active_trade_rows_at(addon_clean, ts_ms)
        primary_weight = _weight_sum(active_primary)
        addon_weight = _weight_sum(active_addon)
        out.append(
            {
                "ts_ms": ts_ms,
                "active_primary_trades": len(active_primary),
                "active_addon_trades": len(active_addon),
                "active_combined_trades": len(active_primary) + len(active_addon),
                "active_primary_weight": primary_weight,
                "active_addon_weight": addon_weight,
                "active_combined_weight": primary_weight + addon_weight,
            }
        )
    return out


def _active_exposure_contributor_rows(primary_rows: list[dict[str, Any]], addon_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary_clean = _clean_trade_rows(primary_rows)
    addon_clean = _clean_trade_rows(addon_rows)
    out: list[dict[str, Any]] = []
    for exposure in _active_exposure_rows(primary_clean, addon_clean):
        ts_ms = int(exposure["ts_ms"])
        active_by_source = {
            "primary": _active_trade_rows_at(primary_clean, ts_ms),
            "addon": _active_trade_rows_at(addon_clean, ts_ms),
        }
        for source, active_rows in active_by_source.items():
            for row in active_rows:
                out.append(
                    {
                        "ts_ms": ts_ms,
                        "source": source,
                        "symbol": _str(row, "symbol"),
                        "trade_id": _str(row, "trade_id"),
                        "signal_ts_ms": _signal_ts(row),
                        "entry_ts_ms": _entry_ts(row),
                        "exit_ts_ms": _exit_ts(row),
                        "status": _str(row, "status").lower() or "unknown",
                        "weight": _row_weight(row),
                        "weight_source": _row_weight_source(row),
                        "active_primary_weight": float(exposure["active_primary_weight"]),
                        "active_addon_weight": float(exposure["active_addon_weight"]),
                        "active_combined_weight": float(exposure["active_combined_weight"]),
                    }
                )
    out.sort(
        key=lambda item: (
            int(item["ts_ms"]),
            str(item["source"]),
            str(item["symbol"]),
            str(item["trade_id"]),
        )
    )
    return out


def _active_exposure_summary(primary_rows: list[dict[str, Any]], addon_rows: list[dict[str, Any]]) -> dict[str, Any]:
    exposure_rows = _active_exposure_rows(primary_rows, addon_rows)
    current_primary = _current_open_trade_rows(primary_rows)
    current_addon = _current_open_trade_rows(addon_rows)
    current_primary_weight = _weight_sum(current_primary)
    current_addon_weight = _weight_sum(current_addon)
    return {
        "max_active_addon_trades": max((int(row["active_addon_trades"]) for row in exposure_rows), default=0),
        "max_active_combined_trades": max((int(row["active_combined_trades"]) for row in exposure_rows), default=0),
        "max_active_addon_weight": max((float(row["active_addon_weight"]) for row in exposure_rows), default=0.0),
        "max_active_combined_weight": max((float(row["active_combined_weight"]) for row in exposure_rows), default=0.0),
        "current_open_primary_trades": len(current_primary),
        "current_open_addon_trades": len(current_addon),
        "current_open_combined_trades": len(current_primary) + len(current_addon),
        "current_open_primary_weight": current_primary_weight,
        "current_open_addon_weight": current_addon_weight,
        "current_open_combined_weight": current_primary_weight + current_addon_weight,
    }


def _concentration_drift(shadow: dict[str, Any], historical: dict[str, Any]) -> dict[str, float]:
    return {
        key: abs(float(shadow[key]) - float(historical[key]))
        for key in ("top1_weight_share", "top5_weight_share", "top10_weight_share")
    }


def _key_diff(
    *,
    shadow_addon_rows: list[dict[str, Any]],
    historical_addon_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    shadow_keys = {_entry_key(row) for row in _clean_trade_rows(shadow_addon_rows) if _entry_key(row)[0] and _entry_key(row)[1] > 0}
    historical_keys = {
        _entry_key(row) for row in _clean_trade_rows(historical_addon_rows) if _entry_key(row)[0] and _entry_key(row)[1] > 0
    }
    missing = sorted(historical_keys - shadow_keys)
    extra = sorted(shadow_keys - historical_keys)
    rows = [
        {"diff_type": "missing_from_shadow", "symbol": symbol, "signal_ts_ms": signal_ts}
        for symbol, signal_ts in missing
    ]
    rows.extend(
        {"diff_type": "extra_shadow", "symbol": symbol, "signal_ts_ms": signal_ts}
        for symbol, signal_ts in extra
    )
    return rows, {
        "matched_addon_keys": len(shadow_keys & historical_keys),
        "missing_from_shadow": len(missing),
        "extra_shadow": len(extra),
    }


def _cycle_summary(cycles: pl.DataFrame, *, now_ms: int = 0) -> dict[str, Any]:
    if cycles.is_empty():
        return {
            "cycles": 0,
            "latest_cycle_ts_ms": 0,
            "latest_cycle_age_minutes": math.inf if now_ms > 0 else 0.0,
            "max_cycle_gap_minutes": math.inf,
            "addon_primary_pnl_gate_skips": 0,
            "skipped_same_signal_reentry": 0,
            "entry_candidates": 0,
            "entries": 0,
            "candidate_pressure": 0,
            "entry_acceptance_fraction": 0.0,
            "same_signal_reentry_skip_fraction": 0.0,
            "addon_primary_pnl_gate_skip_fraction": 0.0,
            "max_candidate_pressure": 0,
            "worst_entry_acceptance_fraction": 0.0,
            "worst_same_signal_reentry_skip_fraction": 0.0,
            "worst_addon_primary_pnl_gate_skip_fraction": 0.0,
        }
    rows = _cycle_pressure_rows(cycles)
    addon_primary_pnl_gate_skips = sum(int(row["addon_primary_pnl_gate_skips"]) for row in rows)
    same_signal_reentry_skips = sum(int(row["skipped_same_signal_reentry"]) for row in rows)
    entry_candidates = sum(int(row["entry_candidates"]) for row in rows)
    entries = sum(int(row["entries"]) for row in rows)
    candidate_pressure = sum(int(row["candidate_pressure"]) for row in rows)
    latest_cycle_ts_ms = max((int(row["ts_ms"]) for row in rows), default=0)
    cycle_ts_values = sorted(int(row["ts_ms"]) for row in rows if int(row["ts_ms"]) > 0)
    max_cycle_gap_minutes = (
        max((right - left) / 60_000 for left, right in zip(cycle_ts_values, cycle_ts_values[1:]))
        if len(cycle_ts_values) >= 2
        else math.inf
    )
    if now_ms > 0 and latest_cycle_ts_ms > 0:
        latest_cycle_age_minutes = max(0.0, (now_ms - latest_cycle_ts_ms) / 60_000)
    elif now_ms > 0:
        latest_cycle_age_minutes = math.inf
    else:
        latest_cycle_age_minutes = 0.0
    return {
        "cycles": len(rows),
        "latest_cycle_ts_ms": latest_cycle_ts_ms,
        "latest_cycle_age_minutes": latest_cycle_age_minutes,
        "max_cycle_gap_minutes": max_cycle_gap_minutes,
        "addon_primary_pnl_gate_skips": addon_primary_pnl_gate_skips,
        "skipped_same_signal_reentry": same_signal_reentry_skips,
        "entry_candidates": entry_candidates,
        "entries": entries,
        "candidate_pressure": candidate_pressure,
        "entry_acceptance_fraction": _ratio(entries, candidate_pressure),
        "same_signal_reentry_skip_fraction": _ratio(same_signal_reentry_skips, candidate_pressure),
        "addon_primary_pnl_gate_skip_fraction": _ratio(addon_primary_pnl_gate_skips, candidate_pressure),
        "max_candidate_pressure": max((int(row["candidate_pressure"]) for row in rows), default=0),
        # Exclude idle (zero-candidate-pressure) cycles: an idle cycle has acceptance
        # 0.0 but rejected nothing, so counting it would spuriously fail the worst-cycle
        # acceptance gate. default=1.0 = "no cycle had candidates to reject" (audit-iter3).
        "worst_entry_acceptance_fraction": min(
            (
                float(row["entry_acceptance_fraction"])
                for row in rows
                if int(row["candidate_pressure"]) > 0
            ),
            default=1.0,
        ),
        "worst_same_signal_reentry_skip_fraction": max(
            (float(row["same_signal_reentry_skip_fraction"]) for row in rows),
            default=0.0,
        ),
        "worst_addon_primary_pnl_gate_skip_fraction": max(
            (float(row["addon_primary_pnl_gate_skip_fraction"]) for row in rows),
            default=0.0,
        ),
    }


def _cycle_int(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in row and row.get(key) is not None:
            return coerce_int(row.get(key))
    return 0


def _cycle_candidate_pressure(row: dict[str, Any]) -> int:
    if "candidates" in row and row.get("candidates") is not None:
        return (
            coerce_int(row.get("candidates"))
            + coerce_int(row.get("addon_primary_pnl_gate_skips"))
            + coerce_int(row.get("skipped_same_signal_reentry"))
        )
    return coerce_int(row.get("entry_candidates"))


def _cycle_pressure_rows(cycles: pl.DataFrame) -> list[dict[str, Any]]:
    if cycles.is_empty():
        return []
    out: list[dict[str, Any]] = []
    for row in cycles.to_dicts():
        candidate_pressure = _cycle_candidate_pressure(row)
        entries = _cycle_int(row, "entries", "entries_executed")
        pnl_gate_skips = coerce_int(row.get("addon_primary_pnl_gate_skips"))
        same_signal_skips = coerce_int(row.get("skipped_same_signal_reentry"))
        out.append(
            {
                "cycle_id": _str(row, "cycle_id"),
                "ts_ms": _cycle_int(row, "ts_ms", "cycle_ts_ms", "now_ms"),
                "entry_candidates": _cycle_int(row, "entry_candidates", "candidates"),
                "entries": entries,
                "candidate_pressure": candidate_pressure,
                "addon_primary_pnl_gate_skips": pnl_gate_skips,
                "skipped_same_signal_reentry": same_signal_skips,
                "entry_acceptance_fraction": _ratio(entries, candidate_pressure),
                "addon_primary_pnl_gate_skip_fraction": _ratio(pnl_gate_skips, candidate_pressure),
                "same_signal_reentry_skip_fraction": _ratio(same_signal_skips, candidate_pressure),
            }
        )
    out.sort(key=lambda item: (str(item["cycle_id"]), int(item["ts_ms"])))
    return out


def _fmt_count_map(values: dict[str, int]) -> str:
    return "; ".join(f"{key}={value}" for key, value in sorted(values.items())) or "-"


def _historical_key_gate_enabled(config: ContinuousAddonShadowAuditConfig) -> bool:
    return (
        config.min_matched_addon_keys > 0
        or config.max_missing_addon_keys >= 0
        or config.max_extra_addon_keys >= 0
        or config.max_missing_addon_key_fraction >= 0.0
    )


def _historical_anatomy_drift_gate_enabled(config: ContinuousAddonShadowAuditConfig) -> bool:
    return config.max_historical_anatomy_drift >= 0.0


def _historical_concentration_drift_gate_enabled(config: ContinuousAddonShadowAuditConfig) -> bool:
    return config.max_historical_concentration_drift >= 0.0


def _cycle_summary_now_ms(config: ContinuousAddonShadowAuditConfig) -> int:
    if config.audit_now_ms > 0:
        return int(config.audit_now_ms)
    if config.max_latest_cycle_age_minutes >= 0.0:
        return int(time.time() * 1000)
    return 0


def _order_reconciliation_now_ms(config: ContinuousAddonShadowAuditConfig) -> int:
    if config.audit_now_ms > 0:
        return int(config.audit_now_ms)
    if (
        config.max_primary_unmatched_entry_order_age_minutes >= 0.0
        or config.max_addon_unmatched_entry_order_age_minutes >= 0.0
        or config.max_primary_unmatched_live_entry_order_age_minutes >= 0.0
        or config.max_addon_unmatched_live_entry_order_age_minutes >= 0.0
    ):
        return int(time.time() * 1000)
    return 0


def _gate_summary(summary: dict[str, Any], config: ContinuousAddonShadowAuditConfig) -> dict[str, Any]:
    failures: list[str] = []
    addon_trades = int(summary["addon"]["trades"])
    if config.min_addon_trades > 0 and addon_trades < config.min_addon_trades:
        failures.append(f"addon trades {addon_trades} < min_addon_trades {config.min_addon_trades}")
    primary_strategy = summary["primary_strategy"]
    primary_unexpected_strategy_rows = int(primary_strategy["unexpected_strategy_rows"])
    if (
        config.max_primary_unexpected_strategy_rows >= 0
        and primary_unexpected_strategy_rows > config.max_primary_unexpected_strategy_rows
    ):
        failures.append(
            f"primary unexpected strategy rows {primary_unexpected_strategy_rows} > "
            f"max_primary_unexpected_strategy_rows {config.max_primary_unexpected_strategy_rows}"
        )
    addon_strategy = summary["addon_strategy"]
    addon_unexpected_strategy_rows = int(addon_strategy["unexpected_strategy_rows"])
    if config.max_addon_unexpected_strategy_rows >= 0 and addon_unexpected_strategy_rows > config.max_addon_unexpected_strategy_rows:
        failures.append(
            f"add-on unexpected strategy rows {addon_unexpected_strategy_rows} > "
            f"max_addon_unexpected_strategy_rows {config.max_addon_unexpected_strategy_rows}"
        )
    primary_order_prefix = summary["primary_order_prefix"]
    primary_unexpected_order_prefix_rows = int(primary_order_prefix["unexpected_entry_order_prefix_rows"])
    if (
        config.max_primary_unexpected_entry_order_prefix_rows >= 0
        and primary_unexpected_order_prefix_rows > config.max_primary_unexpected_entry_order_prefix_rows
    ):
        failures.append(
            f"primary unexpected entry order prefix rows {primary_unexpected_order_prefix_rows} > "
            "max_primary_unexpected_entry_order_prefix_rows "
            f"{config.max_primary_unexpected_entry_order_prefix_rows}"
        )
    addon_order_prefix = summary["addon_order_prefix"]
    addon_unexpected_order_prefix_rows = int(addon_order_prefix["unexpected_entry_order_prefix_rows"])
    if (
        config.max_addon_unexpected_entry_order_prefix_rows >= 0
        and addon_unexpected_order_prefix_rows > config.max_addon_unexpected_entry_order_prefix_rows
    ):
        failures.append(
            f"add-on unexpected entry order prefix rows {addon_unexpected_order_prefix_rows} > "
            "max_addon_unexpected_entry_order_prefix_rows "
            f"{config.max_addon_unexpected_entry_order_prefix_rows}"
        )
    primary_repeats = int(summary["primary"]["repeated_entry_rows"])
    if config.max_primary_repeated_entry_rows >= 0 and primary_repeats > config.max_primary_repeated_entry_rows:
        failures.append(
            f"primary repeated entry rows {primary_repeats} > "
            f"max_primary_repeated_entry_rows {config.max_primary_repeated_entry_rows}"
        )
    addon_repeats = int(summary["addon"]["repeated_entry_rows"])
    if config.max_addon_repeated_entry_rows >= 0 and addon_repeats > config.max_addon_repeated_entry_rows:
        failures.append(
            f"add-on repeated entry rows {addon_repeats} > "
            f"max_addon_repeated_entry_rows {config.max_addon_repeated_entry_rows}"
        )
    primary_order_repeats = int(summary["primary_orders"]["repeated_entry_rows"])
    if (
        config.max_primary_repeated_entry_order_rows >= 0
        and primary_order_repeats > config.max_primary_repeated_entry_order_rows
    ):
        failures.append(
            f"primary repeated entry order rows {primary_order_repeats} > "
            f"max_primary_repeated_entry_order_rows {config.max_primary_repeated_entry_order_rows}"
        )
    addon_order_repeats = int(summary["addon_orders"]["repeated_entry_rows"])
    if config.max_addon_repeated_entry_order_rows >= 0 and addon_order_repeats > config.max_addon_repeated_entry_order_rows:
        failures.append(
            f"add-on repeated entry order rows {addon_order_repeats} > "
            f"max_addon_repeated_entry_order_rows {config.max_addon_repeated_entry_order_rows}"
        )
    primary_problem_orders = int(summary["primary_orders"]["problem_entry_order_attempts"])
    if (
        config.max_primary_problem_entry_order_attempts >= 0
        and primary_problem_orders > config.max_primary_problem_entry_order_attempts
    ):
        failures.append(
            f"primary problem entry order attempts {primary_problem_orders} > "
            f"max_primary_problem_entry_order_attempts {config.max_primary_problem_entry_order_attempts}"
        )
    addon_problem_orders = int(summary["addon_orders"]["problem_entry_order_attempts"])
    if config.max_addon_problem_entry_order_attempts >= 0 and addon_problem_orders > config.max_addon_problem_entry_order_attempts:
        failures.append(
            f"add-on problem entry order attempts {addon_problem_orders} > "
            f"max_addon_problem_entry_order_attempts {config.max_addon_problem_entry_order_attempts}"
        )
    primary_order_reconciliation = summary["primary_order_trade_reconciliation"]
    primary_unmatched_orders = int(primary_order_reconciliation["entry_order_attempts_without_trade_key"])
    if (
        config.max_primary_unmatched_entry_order_attempts >= 0
        and primary_unmatched_orders > config.max_primary_unmatched_entry_order_attempts
    ):
        failures.append(
            f"primary unmatched entry order attempts {primary_unmatched_orders} > "
            f"max_primary_unmatched_entry_order_attempts {config.max_primary_unmatched_entry_order_attempts}"
        )
    addon_order_reconciliation = summary["addon_order_trade_reconciliation"]
    addon_unmatched_orders = int(addon_order_reconciliation["entry_order_attempts_without_trade_key"])
    if config.max_addon_unmatched_entry_order_attempts >= 0 and addon_unmatched_orders > config.max_addon_unmatched_entry_order_attempts:
        failures.append(
            f"add-on unmatched entry order attempts {addon_unmatched_orders} > "
            f"max_addon_unmatched_entry_order_attempts {config.max_addon_unmatched_entry_order_attempts}"
        )
    primary_live_unmatched_orders = int(primary_order_reconciliation["live_entry_order_attempts_without_trade_key"])
    if (
        config.max_primary_unmatched_live_entry_order_attempts >= 0
        and primary_live_unmatched_orders > config.max_primary_unmatched_live_entry_order_attempts
    ):
        failures.append(
            f"primary unmatched live entry order attempts {primary_live_unmatched_orders} > "
            f"max_primary_unmatched_live_entry_order_attempts {config.max_primary_unmatched_live_entry_order_attempts}"
        )
    addon_live_unmatched_orders = int(addon_order_reconciliation["live_entry_order_attempts_without_trade_key"])
    if (
        config.max_addon_unmatched_live_entry_order_attempts >= 0
        and addon_live_unmatched_orders > config.max_addon_unmatched_live_entry_order_attempts
    ):
        failures.append(
            f"add-on unmatched live entry order attempts {addon_live_unmatched_orders} > "
            f"max_addon_unmatched_live_entry_order_attempts {config.max_addon_unmatched_live_entry_order_attempts}"
        )
    _append_ratio_failure(
        failures,
        value=float(primary_order_reconciliation["max_unmatched_entry_order_age_minutes"]),
        min_value=-1.0,
        max_value=config.max_primary_unmatched_entry_order_age_minutes,
        label="primary unmatched entry order age minutes",
        flag="primary_unmatched_entry_order_age_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(addon_order_reconciliation["max_unmatched_entry_order_age_minutes"]),
        min_value=-1.0,
        max_value=config.max_addon_unmatched_entry_order_age_minutes,
        label="add-on unmatched entry order age minutes",
        flag="addon_unmatched_entry_order_age_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(primary_order_reconciliation["max_unmatched_live_entry_order_age_minutes"]),
        min_value=-1.0,
        max_value=config.max_primary_unmatched_live_entry_order_age_minutes,
        label="primary unmatched live entry order age minutes",
        flag="primary_unmatched_live_entry_order_age_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(addon_order_reconciliation["max_unmatched_live_entry_order_age_minutes"]),
        min_value=-1.0,
        max_value=config.max_addon_unmatched_live_entry_order_age_minutes,
        label="add-on unmatched live entry order age minutes",
        flag="addon_unmatched_live_entry_order_age_minutes",
    )
    ticket_rate = summary["daily_ticket_rate"]
    _append_int_failure(
        failures,
        value=int(ticket_rate["max_primary_trades_per_day"]),
        max_value=config.max_primary_trades_per_day,
        label="max primary trades per day",
        flag="max_primary_trades_per_day",
    )
    _append_int_failure(
        failures,
        value=int(ticket_rate["max_addon_trades_per_day"]),
        max_value=config.max_addon_trades_per_day,
        label="max add-on trades per day",
        flag="max_addon_trades_per_day",
    )
    _append_int_failure(
        failures,
        value=int(ticket_rate["max_combined_trades_per_day"]),
        max_value=config.max_combined_trades_per_day,
        label="max combined trades per day",
        flag="max_combined_trades_per_day",
    )
    _append_int_failure(
        failures,
        value=int(ticket_rate["max_primary_entry_order_attempts_per_day"]),
        max_value=config.max_primary_entry_order_attempts_per_day,
        label="max primary entry order attempts per day",
        flag="max_primary_entry_order_attempts_per_day",
    )
    _append_int_failure(
        failures,
        value=int(ticket_rate["max_addon_entry_order_attempts_per_day"]),
        max_value=config.max_addon_entry_order_attempts_per_day,
        label="max add-on entry order attempts per day",
        flag="max_addon_entry_order_attempts_per_day",
    )
    _append_int_failure(
        failures,
        value=int(ticket_rate["max_combined_entry_order_attempts_per_day"]),
        max_value=config.max_combined_entry_order_attempts_per_day,
        label="max combined entry order attempts per day",
        flag="max_combined_entry_order_attempts_per_day",
    )
    symbol_day_ticket_rate = summary["symbol_day_ticket_rate"]
    _append_int_failure(
        failures,
        value=int(symbol_day_ticket_rate["max_primary_trades_per_symbol_day"]),
        max_value=config.max_primary_trades_per_symbol_day,
        label="max primary trades per symbol-day",
        flag="max_primary_trades_per_symbol_day",
    )
    _append_int_failure(
        failures,
        value=int(symbol_day_ticket_rate["max_addon_trades_per_symbol_day"]),
        max_value=config.max_addon_trades_per_symbol_day,
        label="max add-on trades per symbol-day",
        flag="max_addon_trades_per_symbol_day",
    )
    _append_int_failure(
        failures,
        value=int(symbol_day_ticket_rate["max_combined_trades_per_symbol_day"]),
        max_value=config.max_combined_trades_per_symbol_day,
        label="max combined trades per symbol-day",
        flag="max_combined_trades_per_symbol_day",
    )
    _append_int_failure(
        failures,
        value=int(symbol_day_ticket_rate["max_primary_entry_order_attempts_per_symbol_day"]),
        max_value=config.max_primary_entry_order_attempts_per_symbol_day,
        label="max primary entry order attempts per symbol-day",
        flag="max_primary_entry_order_attempts_per_symbol_day",
    )
    _append_int_failure(
        failures,
        value=int(symbol_day_ticket_rate["max_addon_entry_order_attempts_per_symbol_day"]),
        max_value=config.max_addon_entry_order_attempts_per_symbol_day,
        label="max add-on entry order attempts per symbol-day",
        flag="max_addon_entry_order_attempts_per_symbol_day",
    )
    _append_int_failure(
        failures,
        value=int(symbol_day_ticket_rate["max_combined_entry_order_attempts_per_symbol_day"]),
        max_value=config.max_combined_entry_order_attempts_per_symbol_day,
        label="max combined entry order attempts per symbol-day",
        flag="max_combined_entry_order_attempts_per_symbol_day",
    )
    same_symbol_gaps = summary["same_symbol_entry_gaps"]
    _append_ratio_failure(
        failures,
        value=float(same_symbol_gaps["min_primary_same_symbol_trade_gap_minutes"]),
        min_value=config.min_primary_same_symbol_trade_gap_minutes,
        max_value=-1.0,
        label="primary same-symbol trade gap minutes",
        flag="primary_same_symbol_trade_gap_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(same_symbol_gaps["min_addon_same_symbol_trade_gap_minutes"]),
        min_value=config.min_addon_same_symbol_trade_gap_minutes,
        max_value=-1.0,
        label="add-on same-symbol trade gap minutes",
        flag="addon_same_symbol_trade_gap_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(same_symbol_gaps["min_combined_same_symbol_trade_gap_minutes"]),
        min_value=config.min_combined_same_symbol_trade_gap_minutes,
        max_value=-1.0,
        label="combined same-symbol trade gap minutes",
        flag="combined_same_symbol_trade_gap_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(same_symbol_gaps["min_primary_same_symbol_entry_order_gap_minutes"]),
        min_value=config.min_primary_same_symbol_entry_order_gap_minutes,
        max_value=-1.0,
        label="primary same-symbol entry order gap minutes",
        flag="primary_same_symbol_entry_order_gap_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(same_symbol_gaps["min_addon_same_symbol_entry_order_gap_minutes"]),
        min_value=config.min_addon_same_symbol_entry_order_gap_minutes,
        max_value=-1.0,
        label="add-on same-symbol entry order gap minutes",
        flag="addon_same_symbol_entry_order_gap_minutes",
    )
    _append_ratio_failure(
        failures,
        value=float(same_symbol_gaps["min_combined_same_symbol_entry_order_gap_minutes"]),
        min_value=config.min_combined_same_symbol_entry_order_gap_minutes,
        max_value=-1.0,
        label="combined same-symbol entry order gap minutes",
        flag="combined_same_symbol_entry_order_gap_minutes",
    )
    cycles = summary["addon_cycles"]
    cycle_count = int(cycles["cycles"])
    if config.min_addon_cycles > 0 and cycle_count < config.min_addon_cycles:
        failures.append(f"add-on cycles {cycle_count} < min_addon_cycles {config.min_addon_cycles}")
    latest_cycle_age_minutes = float(cycles["latest_cycle_age_minutes"])
    if config.max_latest_cycle_age_minutes >= 0.0 and latest_cycle_age_minutes > config.max_latest_cycle_age_minutes:
        failures.append(
            f"latest add-on cycle age minutes {_fmt_ratio(latest_cycle_age_minutes)} > "
            f"max_latest_cycle_age_minutes {_fmt_ratio(config.max_latest_cycle_age_minutes)}"
        )
    max_cycle_gap_minutes = float(cycles["max_cycle_gap_minutes"])
    if config.max_cycle_gap_minutes >= 0.0 and max_cycle_gap_minutes > config.max_cycle_gap_minutes:
        failures.append(
            f"max add-on cycle gap minutes {_fmt_ratio(max_cycle_gap_minutes)} > "
            f"max_cycle_gap_minutes {_fmt_ratio(config.max_cycle_gap_minutes)}"
        )
    _append_ratio_failure(
        failures,
        value=float(cycles["entry_acceptance_fraction"]),
        min_value=config.min_cycle_entry_acceptance_fraction,
        max_value=-1.0,
        label="cycle entry acceptance fraction",
        flag="cycle_entry_acceptance_fraction",
    )
    _append_ratio_failure(
        failures,
        value=float(cycles["same_signal_reentry_skip_fraction"]),
        min_value=-1.0,
        max_value=config.max_cycle_same_signal_reentry_skip_fraction,
        label="cycle same-signal re-entry skip fraction",
        flag="cycle_same_signal_reentry_skip_fraction",
    )
    _append_ratio_failure(
        failures,
        value=float(cycles["addon_primary_pnl_gate_skip_fraction"]),
        min_value=-1.0,
        max_value=config.max_cycle_addon_primary_pnl_gate_skip_fraction,
        label="cycle add-on primary-PnL gate skip fraction",
        flag="cycle_addon_primary_pnl_gate_skip_fraction",
    )
    max_cycle_pressure = int(cycles["max_candidate_pressure"])
    if config.max_cycle_candidate_pressure >= 0 and max_cycle_pressure > config.max_cycle_candidate_pressure:
        failures.append(
            f"max cycle candidate pressure {max_cycle_pressure} > "
            f"max_cycle_candidate_pressure {config.max_cycle_candidate_pressure}"
        )
    _append_ratio_failure(
        failures,
        value=float(cycles["worst_entry_acceptance_fraction"]),
        min_value=config.min_worst_cycle_entry_acceptance_fraction,
        max_value=-1.0,
        label="worst-cycle entry acceptance fraction",
        flag="worst_cycle_entry_acceptance_fraction",
    )
    _append_ratio_failure(
        failures,
        value=float(cycles["worst_same_signal_reentry_skip_fraction"]),
        min_value=-1.0,
        max_value=config.max_worst_cycle_same_signal_reentry_skip_fraction,
        label="worst-cycle same-signal re-entry skip fraction",
        flag="worst_cycle_same_signal_reentry_skip_fraction",
    )
    _append_ratio_failure(
        failures,
        value=float(cycles["worst_addon_primary_pnl_gate_skip_fraction"]),
        min_value=-1.0,
        max_value=config.max_worst_cycle_addon_primary_pnl_gate_skip_fraction,
        label="worst-cycle add-on primary-PnL gate skip fraction",
        flag="worst_cycle_addon_primary_pnl_gate_skip_fraction",
    )
    anatomy = summary["shadow_anatomy"]
    _append_ratio_failure(
        failures,
        value=float(anatomy["addon_to_primary_ratio"]),
        min_value=config.min_addon_to_primary_ratio,
        max_value=config.max_addon_to_primary_ratio,
        label="add-on / primary trade ratio",
        flag="addon_to_primary_ratio",
    )
    _append_ratio_failure(
        failures,
        value=float(anatomy["active_same_symbol_overlap_fraction"]),
        min_value=config.min_active_same_symbol_overlap_fraction,
        max_value=config.max_active_same_symbol_overlap_fraction,
        label="active same-symbol overlap fraction",
        flag="active_same_symbol_overlap_fraction",
    )
    _append_ratio_failure(
        failures,
        value=float(anatomy["exact_same_entry_fraction"]),
        min_value=config.min_exact_same_entry_fraction,
        max_value=config.max_exact_same_entry_fraction,
        label="exact same-entry fraction",
        flag="exact_same_entry_fraction",
    )
    concentration = summary["addon_concentration"]
    _append_ratio_failure(
        failures,
        value=float(concentration["top1_weight_share"]),
        min_value=-1.0,
        max_value=config.max_addon_top1_weight_share,
        label="add-on top1 symbol weight share",
        flag="addon_top1_weight_share",
    )
    _append_ratio_failure(
        failures,
        value=float(concentration["top5_weight_share"]),
        min_value=-1.0,
        max_value=config.max_addon_top5_weight_share,
        label="add-on top5 symbol weight share",
        flag="addon_top5_weight_share",
    )
    _append_ratio_failure(
        failures,
        value=float(concentration["top10_weight_share"]),
        min_value=-1.0,
        max_value=config.max_addon_top10_weight_share,
        label="add-on top10 symbol weight share",
        flag="addon_top10_weight_share",
    )
    exposure = summary["active_exposure"]
    weight_sources = summary["weight_sources"]
    unit_weight_rows = int(weight_sources["combined_unit_weight_rows"])
    if config.max_unit_weight_rows >= 0 and unit_weight_rows > config.max_unit_weight_rows:
        failures.append(f"unit-weight trade rows {unit_weight_rows} > max_unit_weight_rows {config.max_unit_weight_rows}")
    _append_ratio_failure(
        failures,
        value=float(exposure["max_active_addon_weight"]),
        min_value=-1.0,
        max_value=config.max_active_addon_weight,
        label="max active add-on weight",
        flag="active_addon_weight",
    )
    _append_ratio_failure(
        failures,
        value=float(exposure["max_active_combined_weight"]),
        min_value=-1.0,
        max_value=config.max_active_combined_weight,
        label="max active combined weight",
        flag="active_combined_weight",
    )

    historical = summary.get("historical")
    historical_gate_requested = (
        _historical_key_gate_enabled(config)
        or _historical_anatomy_drift_gate_enabled(config)
        or _historical_concentration_drift_gate_enabled(config)
    )
    if historical_gate_requested and not historical:
        failures.append("historical_blended_trades_csv is required for historical add-on key/anatomy/concentration gates")
    elif historical:
        key_diff = historical["key_diff"]
        matched = int(key_diff["matched_addon_keys"])
        missing = int(key_diff["missing_from_shadow"])
        extra = int(key_diff["extra_shadow"])
        historical_key_count = matched + missing
        missing_fraction = missing / historical_key_count if historical_key_count else 0.0
        if config.min_matched_addon_keys > 0 and matched < config.min_matched_addon_keys:
            failures.append(f"matched add-on keys {matched} < min_matched_addon_keys {config.min_matched_addon_keys}")
        if config.max_missing_addon_keys >= 0 and missing > config.max_missing_addon_keys:
            failures.append(f"missing historical add-on keys {missing} > max_missing_addon_keys {config.max_missing_addon_keys}")
        if config.max_extra_addon_keys >= 0 and extra > config.max_extra_addon_keys:
            failures.append(f"extra shadow add-on keys {extra} > max_extra_addon_keys {config.max_extra_addon_keys}")
        if config.max_missing_addon_key_fraction >= 0.0 and missing_fraction > config.max_missing_addon_key_fraction:
            failures.append(
                "missing historical add-on key fraction "
                f"{missing_fraction:.4f} > max_missing_addon_key_fraction {config.max_missing_addon_key_fraction:.4f}"
            )
        if config.max_historical_anatomy_drift >= 0.0:
            drift = summary["historical_anatomy_drift"]
            for key, value in drift.items():
                if float(value) > config.max_historical_anatomy_drift:
                    failures.append(
                        f"historical anatomy drift {key} {_fmt_ratio(float(value))} "
                        f"> max_historical_anatomy_drift {_fmt_ratio(config.max_historical_anatomy_drift)}"
                    )
        if config.max_historical_concentration_drift >= 0.0:
            drift = summary["historical_concentration_drift"]
            for key, value in drift.items():
                if float(value) > config.max_historical_concentration_drift:
                    failures.append(
                        f"historical concentration drift {key} {_fmt_ratio(float(value))} "
                        f"> max_historical_concentration_drift {_fmt_ratio(config.max_historical_concentration_drift)}"
                    )
    return {"passed": not failures, "failures": failures}


def _append_ratio_failure(
    failures: list[str],
    *,
    value: float,
    min_value: float,
    max_value: float,
    label: str,
    flag: str,
) -> None:
    if min_value >= 0.0 and value < min_value:
        failures.append(f"{label} {_fmt_ratio(value)} < min_{flag} {_fmt_ratio(min_value)}")
    if max_value >= 0.0 and value > max_value:
        failures.append(f"{label} {_fmt_ratio(value)} > max_{flag} {_fmt_ratio(max_value)}")


def _append_int_failure(
    failures: list[str],
    *,
    value: int,
    max_value: int,
    label: str,
    flag: str,
) -> None:
    if max_value >= 0 and value > max_value:
        failures.append(f"{label} {value} > {flag} {max_value}")


def _fmt_ratio(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.4f}"


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Continuous Add-on Shadow Audit",
        "",
        "Paper-only audit. Reads local ledgers and optional historical blend CSV; submits no orders.",
        "",
        "## Inputs",
        "",
        f"- Primary root: `{payload['primary_data_root']}`",
        f"- Add-on root: `{payload['addon_data_root']}`",
        f"- Historical blend CSV: `{payload.get('historical_blended_trades_csv') or '-'}`",
        "",
        "## Shadow Ledger Summary",
        "",
        "| Sleeve | Trades | Unique symbols | Trade days | Avg trades/day | Max trades/day | Status counts |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for label, key in (("Primary", "primary"), ("Add-on", "addon")):
        item = summary[key]
        lines.append(
            f"| {label} | {item['trades']} | {item['unique_symbols']} | {item['trade_days']} | "
            f"{item['avg_trades_per_trade_day']:.2f} | {item['max_trades_per_day']} | "
            f"{_fmt_count_map(item['status_counts'])} |"
        )
    overlap = summary["shadow_overlap"]
    anatomy = summary["shadow_anatomy"]
    concentration = summary["addon_concentration"]
    exposure = summary["active_exposure"]
    weight_sources = summary["weight_sources"]
    daily_ticket_rate = summary["daily_ticket_rate"]
    symbol_day_ticket_rate = summary["symbol_day_ticket_rate"]
    same_symbol_gaps = summary["same_symbol_entry_gaps"]
    cooldown = summary["addon_cooldown_simulation"]
    cycles = summary["addon_cycles"]
    primary_repeats = summary["primary"]
    addon_repeats = summary["addon"]
    primary_strategy = summary["primary_strategy"]
    addon_strategy = summary["addon_strategy"]
    primary_order_prefix = summary["primary_order_prefix"]
    addon_order_prefix = summary["addon_order_prefix"]
    primary_orders = summary["primary_orders"]
    addon_orders = summary["addon_orders"]
    primary_order_reconciliation = summary["primary_order_trade_reconciliation"]
    addon_order_reconciliation = summary["addon_order_trade_reconciliation"]
    lines.extend(
        [
            "",
            "## Churn Fingerprint",
            "",
            f"- Primary trade strategy ids: `{_fmt_count_map(primary_strategy['strategy_counts'])}`",
            f"- Primary unexpected strategy rows: `{primary_strategy['unexpected_strategy_rows']}`",
            f"- Add-on trade strategy ids: `{_fmt_count_map(addon_strategy['strategy_counts'])}`",
            f"- Add-on unexpected strategy rows: `{addon_strategy['unexpected_strategy_rows']}`",
            f"- Primary entry order prefixes: `{_fmt_count_map(primary_order_prefix['entry_order_prefix_counts'])}`",
            "- Primary unexpected entry order prefix rows: "
            f"`{primary_order_prefix['unexpected_entry_order_prefix_rows']}`",
            f"- Add-on entry order prefixes: `{_fmt_count_map(addon_order_prefix['entry_order_prefix_counts'])}`",
            "- Add-on unexpected entry order prefix rows: "
            f"`{addon_order_prefix['unexpected_entry_order_prefix_rows']}`",
            f"- Primary repeated entry keys: `{primary_repeats['repeated_entry_keys']}` "
            f"(`{primary_repeats['repeated_entry_rows']}` repeat rows, "
            f"max `{primary_repeats['max_entries_per_entry_key']}` per key)",
            f"- Add-on repeated entry keys: `{addon_repeats['repeated_entry_keys']}` "
            f"(`{addon_repeats['repeated_entry_rows']}` repeat rows, "
            f"max `{addon_repeats['max_entries_per_entry_key']}` per key)",
            f"- Primary entry order attempts: `{primary_orders['entry_order_attempts']}` "
            f"(`{primary_orders['repeated_entry_rows']}` repeat rows, "
            f"max `{primary_orders['max_entries_per_entry_key']}` per key)",
            f"- Primary problem entry order attempts: `{primary_orders['problem_entry_order_attempts']}`",
            f"- Primary entry order statuses: `{_fmt_count_map(primary_orders['status_counts'])}`",
            "- Primary unmatched entry order attempts: "
            f"`{primary_order_reconciliation['entry_order_attempts_without_trade_key']}` "
            f"(live `{primary_order_reconciliation['live_entry_order_attempts_without_trade_key']}`, "
            "max age "
            f"`{_fmt_ratio(primary_order_reconciliation['max_unmatched_entry_order_age_minutes'])}`, "
            "live max age "
            f"`{_fmt_ratio(primary_order_reconciliation['max_unmatched_live_entry_order_age_minutes'])}`)",
            f"- Add-on entry order attempts: `{addon_orders['entry_order_attempts']}` "
            f"(`{addon_orders['repeated_entry_rows']}` repeat rows, "
            f"max `{addon_orders['max_entries_per_entry_key']}` per key)",
            f"- Add-on problem entry order attempts: `{addon_orders['problem_entry_order_attempts']}`",
            f"- Add-on entry order statuses: `{_fmt_count_map(addon_orders['status_counts'])}`",
            "- Add-on unmatched entry order attempts: "
            f"`{addon_order_reconciliation['entry_order_attempts_without_trade_key']}` "
            f"(live `{addon_order_reconciliation['live_entry_order_attempts_without_trade_key']}`, "
            "max age "
            f"`{_fmt_ratio(addon_order_reconciliation['max_unmatched_entry_order_age_minutes'])}`, "
            "live max age "
            f"`{_fmt_ratio(addon_order_reconciliation['max_unmatched_live_entry_order_age_minutes'])}`)",
            f"- Max combined trades/day: `{daily_ticket_rate['max_combined_trades_per_day']}` "
            f"(day `{daily_ticket_rate['max_combined_trades_day']}`)",
            f"- Max primary trades/day: `{daily_ticket_rate['max_primary_trades_per_day']}`",
            f"- Max add-on trades/day: `{daily_ticket_rate['max_addon_trades_per_day']}`",
            "- Max combined entry order attempts/day: "
            f"`{daily_ticket_rate['max_combined_entry_order_attempts_per_day']}` "
            f"(day `{daily_ticket_rate['max_combined_entry_order_attempts_day']}`)",
            "- Max primary entry order attempts/day: "
            f"`{daily_ticket_rate['max_primary_entry_order_attempts_per_day']}`",
            "- Max add-on entry order attempts/day: "
            f"`{daily_ticket_rate['max_addon_entry_order_attempts_per_day']}`",
            f"- Daily ticket-rate CSV: `{payload.get('daily_ticket_rate_csv_path') or '-'}`",
            f"- Daily ticket contributors CSV: `{payload.get('daily_ticket_contributors_csv_path') or '-'}`",
            "- Max combined trades/symbol-day: "
            f"`{symbol_day_ticket_rate['max_combined_trades_per_symbol_day']}` "
            f"(`{symbol_day_ticket_rate['max_combined_trades_symbol'] or '-'}`, "
            f"day `{symbol_day_ticket_rate['max_combined_trades_day']}`)",
            "- Max primary trades/symbol-day: "
            f"`{symbol_day_ticket_rate['max_primary_trades_per_symbol_day']}`",
            "- Max add-on trades/symbol-day: "
            f"`{symbol_day_ticket_rate['max_addon_trades_per_symbol_day']}`",
            "- Max combined entry order attempts/symbol-day: "
            f"`{symbol_day_ticket_rate['max_combined_entry_order_attempts_per_symbol_day']}` "
            f"(`{symbol_day_ticket_rate['max_combined_entry_order_attempts_symbol'] or '-'}`, "
            f"day `{symbol_day_ticket_rate['max_combined_entry_order_attempts_day']}`)",
            "- Max primary entry order attempts/symbol-day: "
            f"`{symbol_day_ticket_rate['max_primary_entry_order_attempts_per_symbol_day']}`",
            "- Max add-on entry order attempts/symbol-day: "
            f"`{symbol_day_ticket_rate['max_addon_entry_order_attempts_per_symbol_day']}`",
            f"- Symbol-day ticket-rate CSV: `{payload.get('symbol_day_ticket_rate_csv_path') or '-'}`",
            "- Min add-on same-symbol trade gap minutes: "
            f"`{_fmt_ratio(same_symbol_gaps['min_addon_same_symbol_trade_gap_minutes'])}`",
            "- Min combined same-symbol trade gap minutes: "
            f"`{_fmt_ratio(same_symbol_gaps['min_combined_same_symbol_trade_gap_minutes'])}`",
            "- Min add-on same-symbol entry order gap minutes: "
            f"`{_fmt_ratio(same_symbol_gaps['min_addon_same_symbol_entry_order_gap_minutes'])}`",
            "- Min combined same-symbol entry order gap minutes: "
            f"`{_fmt_ratio(same_symbol_gaps['min_combined_same_symbol_entry_order_gap_minutes'])}`",
            f"- Same-symbol entry gap CSV: `{payload.get('same_symbol_entry_gap_csv_path') or '-'}`",
            "- Add-on trade cooldown simulation: "
            f"`{cooldown['skipped_trades']}` skipped of `{cooldown['trade_candidates']}` "
            f"(`{_fmt_ratio(cooldown['trade_suppression_fraction'])}`)",
            "- Add-on cooldown skipped-trade return sum: "
            f"`{_fmt_ratio(cooldown['skipped_trade_return_sum'])}` "
            f"(`{cooldown['skipped_trade_return_observations']}` observations)",
            "- Add-on cooldown skipped-trade mean return: "
            f"`{_fmt_ratio(cooldown['skipped_trade_return_mean'])}` "
            f"(positive `{_fmt_ratio(cooldown['skipped_trade_positive_return_fraction'])}`)",
            "- Add-on entry-order cooldown simulation: "
            f"`{cooldown['skipped_entry_order_attempts']}` skipped of `{cooldown['entry_order_candidates']}` "
            f"(`{_fmt_ratio(cooldown['entry_order_suppression_fraction'])}`)",
            f"- Cooldown simulation CSV: `{payload.get('cooldown_simulation_csv_path') or '-'}`",
            f"- Entry order attempt CSV: `{payload.get('entry_order_attempts_csv_path') or '-'}`",
            f"- Unmatched entry order attempt CSV: `{payload.get('unmatched_entry_order_attempts_csv_path') or '-'}`",
            f"- Add-ons with active same-symbol primary: `{overlap['active_same_symbol_primary']}`",
            f"- Add-ons with exact same-entry primary: `{overlap['exact_same_entry_primary']}`",
            f"- Add-on / primary trade ratio: `{_fmt_ratio(anatomy['addon_to_primary_ratio'])}`",
            f"- Active same-symbol overlap fraction: `{_fmt_ratio(anatomy['active_same_symbol_overlap_fraction'])}`",
            f"- Exact same-entry fraction: `{_fmt_ratio(anatomy['exact_same_entry_fraction'])}`",
            f"- Add-on top1 symbol weight share: `{_fmt_ratio(concentration['top1_weight_share'])}`"
            f" (`{concentration['top1_symbol'] or '-'}`)",
            f"- Add-on top5 symbol weight share: `{_fmt_ratio(concentration['top5_weight_share'])}`",
            f"- Add-on top10 symbol weight share: `{_fmt_ratio(concentration['top10_weight_share'])}`",
            f"- Add-on concentration CSV: `{payload.get('addon_concentration_csv_path') or '-'}`",
            f"- Current open add-on weight: `{_fmt_ratio(exposure['current_open_addon_weight'])}`",
            f"- Current open combined weight: `{_fmt_ratio(exposure['current_open_combined_weight'])}`",
            f"- Max active add-on weight: `{_fmt_ratio(exposure['max_active_addon_weight'])}`",
            f"- Max active combined weight: `{_fmt_ratio(exposure['max_active_combined_weight'])}`",
            f"- Primary weight sources: `{_fmt_count_map(weight_sources['primary'])}`",
            f"- Add-on weight sources: `{_fmt_count_map(weight_sources['addon'])}`",
            f"- Unit-weight trade rows: `{weight_sources['combined_unit_weight_rows']}`",
            f"- Active exposure CSV: `{payload.get('active_exposure_csv_path') or '-'}`",
            f"- Active exposure contributors CSV: `{payload.get('active_exposure_contributors_csv_path') or '-'}`",
            f"- Add-on cycles read: `{cycles['cycles']}`",
            f"- Add-on latest cycle ts_ms: `{cycles['latest_cycle_ts_ms']}`",
            f"- Add-on latest cycle age minutes: `{_fmt_ratio(cycles['latest_cycle_age_minutes'])}`",
            f"- Add-on max cycle gap minutes: `{_fmt_ratio(cycles['max_cycle_gap_minutes'])}`",
            f"- Add-on cycle candidate pressure: `{cycles['candidate_pressure']}`",
            f"- Add-on cycle entry candidates recorded: `{cycles['entry_candidates']}`",
            f"- Add-on cycle entries recorded: `{cycles['entries']}`",
            f"- Add-on cycle entry acceptance fraction: `{_fmt_ratio(cycles['entry_acceptance_fraction'])}`",
            f"- Add-on primary-PnL gate skips recorded: `{cycles['addon_primary_pnl_gate_skips']}`",
            f"- Add-on primary-PnL gate skip fraction: `{_fmt_ratio(cycles['addon_primary_pnl_gate_skip_fraction'])}`",
            f"- Add-on same-signal re-entry skips recorded: `{cycles['skipped_same_signal_reentry']}`",
            f"- Add-on same-signal re-entry skip fraction: `{_fmt_ratio(cycles['same_signal_reentry_skip_fraction'])}`",
            f"- Add-on max cycle candidate pressure: `{cycles['max_candidate_pressure']}`",
            f"- Add-on worst-cycle entry acceptance fraction: `{_fmt_ratio(cycles['worst_entry_acceptance_fraction'])}`",
            f"- Add-on worst-cycle primary-PnL gate skip fraction: `{_fmt_ratio(cycles['worst_addon_primary_pnl_gate_skip_fraction'])}`",
            f"- Add-on worst-cycle same-signal re-entry skip fraction: `{_fmt_ratio(cycles['worst_same_signal_reentry_skip_fraction'])}`",
            f"- Add-on cycle pressure CSV: `{payload.get('cycle_pressure_csv_path') or '-'}`",
            "",
        ]
    )
    gate = summary.get("gate", {})
    if gate:
        lines.extend(
            [
                "## Gate Verdict",
                "",
                f"- Passed: `{bool(gate['passed'])}`",
            ]
        )
        failures = gate.get("failures") or []
        if failures:
            lines.append("- Failures:")
            lines.extend(f"  - {failure}" for failure in failures)
        lines.append("")
    hist = summary.get("historical")
    if hist:
        hist_overlap = hist["overlap"]
        hist_anatomy = hist["anatomy"]
        hist_concentration = hist["concentration"]
        hist_drift = summary["historical_anatomy_drift"]
        hist_concentration_drift = summary["historical_concentration_drift"]
        key_diff = hist["key_diff"]
        hist_primary_repeats = hist["primary"]
        hist_addon_repeats = hist["addon"]
        lines.extend(
            [
                "## Historical Blend Comparison",
                "",
                "| Source | Trades | Unique symbols | Trade days | Avg trades/day | Max trades/day |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for label, key in (("Historical primary", "primary"), ("Historical add-on", "addon")):
            item = hist[key]
            lines.append(
                f"| {label} | {item['trades']} | {item['unique_symbols']} | {item['trade_days']} | "
                f"{item['avg_trades_per_trade_day']:.2f} | {item['max_trades_per_day']} |"
            )
        lines.extend(
            [
                "",
                f"- Historical primary repeated entry keys: `{hist_primary_repeats['repeated_entry_keys']}` "
                f"(`{hist_primary_repeats['repeated_entry_rows']}` repeat rows, "
                f"max `{hist_primary_repeats['max_entries_per_entry_key']}` per key)",
                f"- Historical add-on repeated entry keys: `{hist_addon_repeats['repeated_entry_keys']}` "
                f"(`{hist_addon_repeats['repeated_entry_rows']}` repeat rows, "
                f"max `{hist_addon_repeats['max_entries_per_entry_key']}` per key)",
                f"- Historical add-ons with active same-symbol primary: `{hist_overlap['active_same_symbol_primary']}`",
                f"- Historical add-ons with exact same-entry primary: `{hist_overlap['exact_same_entry_primary']}`",
                f"- Historical add-on / primary trade ratio: `{_fmt_ratio(hist_anatomy['addon_to_primary_ratio'])}`",
                f"- Historical active same-symbol overlap fraction: `{_fmt_ratio(hist_anatomy['active_same_symbol_overlap_fraction'])}`",
                f"- Historical exact same-entry fraction: `{_fmt_ratio(hist_anatomy['exact_same_entry_fraction'])}`",
                f"- Add-on / primary ratio drift: `{_fmt_ratio(hist_drift['addon_to_primary_ratio'])}`",
                f"- Active same-symbol overlap drift: `{_fmt_ratio(hist_drift['active_same_symbol_overlap_fraction'])}`",
                f"- Exact same-entry drift: `{_fmt_ratio(hist_drift['exact_same_entry_fraction'])}`",
                f"- Historical add-on top1 symbol weight share: `{_fmt_ratio(hist_concentration['top1_weight_share'])}`"
                f" (`{hist_concentration['top1_symbol'] or '-'}`)",
                f"- Historical add-on top5 symbol weight share: `{_fmt_ratio(hist_concentration['top5_weight_share'])}`",
                f"- Historical add-on top10 symbol weight share: `{_fmt_ratio(hist_concentration['top10_weight_share'])}`",
                f"- Historical add-on concentration CSV: `{payload.get('historical_addon_concentration_csv_path') or '-'}`",
                f"- Add-on top1 concentration drift: `{_fmt_ratio(hist_concentration_drift['top1_weight_share'])}`",
                f"- Add-on top5 concentration drift: `{_fmt_ratio(hist_concentration_drift['top5_weight_share'])}`",
                f"- Add-on top10 concentration drift: `{_fmt_ratio(hist_concentration_drift['top10_weight_share'])}`",
                f"- Matched add-on keys: `{key_diff['matched_addon_keys']}`",
                f"- Missing historical add-on keys from shadow: `{key_diff['missing_from_shadow']}`",
                f"- Extra shadow add-on keys: `{key_diff['extra_shadow']}`",
            ]
        )
        if payload.get("key_diff_csv_path"):
            lines.append(f"- Key diff CSV: `{payload['key_diff_csv_path']}`")
    return "\n".join(lines) + "\n"


def run_continuous_addon_shadow_audit(config: ContinuousAddonShadowAuditConfig) -> dict[str, Any]:
    primary_root = Path(config.primary_data_root).expanduser()
    addon_root = Path(config.addon_data_root).expanduser()
    # Guard against silently auditing the SAME source as both sides. The primary
    # and addon sides are read with no source filter, so if both (root, dataset)
    # pairs resolve to the same path the identical trades load as BOTH primary and
    # addon: addon_to_primary_ratio->1.0 and every combined metric (combined
    # trades/symbol-day, active combined weight) double-counts the true exposure,
    # silently producing a materially wrong audit with no warning. Require either
    # distinct sources, or — for a deliberate single-root, strategy-split audit —
    # distinct expected_*_strategy_id values that are then applied to each side.
    # (shadows-3)
    same_trades_source = (primary_root.resolve(), config.primary_trades_dataset) == (
        addon_root.resolve(),
        config.addon_trades_dataset,
    )
    # audit-iter3: the ORDERS datasets are also read for both sides (daily/symbol-day
    # ticket-rate gates), so an orders-source collision double-counts those metrics
    # just like trades. Guard it on the same condition.
    same_orders_source = (primary_root.resolve(), config.primary_orders_dataset) == (
        addon_root.resolve(),
        config.addon_orders_dataset,
    )
    same_any_source = same_trades_source or same_orders_source
    distinct_strategy_split = bool(
        config.expected_primary_strategy_id
        and config.expected_addon_strategy_id
        and config.expected_primary_strategy_id != config.expected_addon_strategy_id
    )
    if same_any_source and not distinct_strategy_split:
        which = "trades" if same_trades_source else "orders"
        raise ValueError(
            f"Add-on shadow audit primary and add-on resolve to the same {which} source "
            f"({primary_root.resolve()}); this double-counts combined metrics. Point "
            "--primary-data-root and --addon-data-root at distinct roots (or distinct "
            "datasets), or supply distinct --expected-primary-strategy-id and "
            "--expected-addon-strategy-id to split one root by strategy."
        )

    primary = read_dataset(primary_root, config.primary_trades_dataset)
    addon = read_dataset(addon_root, config.addon_trades_dataset)
    primary_orders = read_dataset(primary_root, config.primary_orders_dataset)
    addon_orders = read_dataset(addon_root, config.addon_orders_dataset)
    addon_cycles = read_dataset(addon_root, config.addon_cycles_dataset)

    primary_rows = _clean_trade_rows(_rows(primary))
    addon_rows = _clean_trade_rows(_rows(addon))
    primary_order_rows = _rows(primary_orders)
    addon_order_rows = _rows(addon_orders)
    if same_any_source and distinct_strategy_split:
        # Single-root strategy-split mode: keep only each side's strategy so the
        # two sides are genuinely disjoint rather than the same rows twice.
        primary_rows = _filter_by_strategy_id(primary_rows, config.expected_primary_strategy_id)
        addon_rows = _filter_by_strategy_id(addon_rows, config.expected_addon_strategy_id)
        primary_order_rows = _filter_by_strategy_id(primary_order_rows, config.expected_primary_strategy_id)
        addon_order_rows = _filter_by_strategy_id(addon_order_rows, config.expected_addon_strategy_id)
        # addon_cycles is a pl.DataFrame (not list[dict]); filter it too so the
        # cycle-based gates see ONLY the add-on strategy's cycles, not both strategies'
        # telemetry mixed together (audit-iter3 deep-continuous_addon_shadow). Guard on
        # the column: older/empty cycle datasets may not carry strategy_id.
        if "strategy_id" in addon_cycles.columns:
            addon_cycles = addon_cycles.filter(
                pl.col("strategy_id") == config.expected_addon_strategy_id
            )
    primary_summary = _trade_summary(primary_rows)
    addon_summary = _trade_summary(addon_rows)
    shadow_overlap = _overlap_summary(primary_rows, addon_rows)
    order_reconciliation_now_ms = _order_reconciliation_now_ms(config)
    daily_ticket_rows = _daily_ticket_rows(
        primary_rows=primary_rows,
        addon_rows=addon_rows,
        primary_order_rows=primary_order_rows,
        addon_order_rows=addon_order_rows,
    )
    symbol_day_ticket_rows = _symbol_day_ticket_rows(
        primary_rows=primary_rows,
        addon_rows=addon_rows,
        primary_order_rows=primary_order_rows,
        addon_order_rows=addon_order_rows,
    )
    same_symbol_entry_gap_rows = _same_symbol_entry_gap_rows(
        primary_rows=primary_rows,
        addon_rows=addon_rows,
        primary_order_rows=primary_order_rows,
        addon_order_rows=addon_order_rows,
    )
    cooldown_simulation_rows = _cooldown_simulation_rows(
        addon_rows=addon_rows,
        addon_order_rows=addon_order_rows,
        trade_cooldown_minutes=config.simulate_addon_same_symbol_trade_cooldown_minutes,
        entry_order_cooldown_minutes=config.simulate_addon_same_symbol_entry_order_cooldown_minutes,
    )
    summary: dict[str, Any] = {
        "primary": primary_summary,
        "addon": addon_summary,
        "shadow_overlap": shadow_overlap,
        "primary_strategy": _strategy_identity_summary(primary_rows, expected_strategy_id=config.expected_primary_strategy_id),
        "addon_strategy": _strategy_identity_summary(addon_rows, expected_strategy_id=config.expected_addon_strategy_id),
        "primary_order_prefix": _entry_order_prefix_summary(
            primary_order_rows,
            expected_prefix=config.expected_primary_entry_order_prefix,
        ),
        "addon_order_prefix": _entry_order_prefix_summary(
            addon_order_rows,
            expected_prefix=config.expected_addon_entry_order_prefix,
        ),
        "primary_orders": _entry_order_attempt_summary(primary_order_rows),
        "addon_orders": _entry_order_attempt_summary(addon_order_rows),
        "primary_order_trade_reconciliation": _entry_order_trade_reconciliation_summary(
            order_rows=primary_order_rows,
            trade_rows=primary_rows,
            now_ms=order_reconciliation_now_ms,
        ),
        "addon_order_trade_reconciliation": _entry_order_trade_reconciliation_summary(
            order_rows=addon_order_rows,
            trade_rows=addon_rows,
            now_ms=order_reconciliation_now_ms,
        ),
        "shadow_anatomy": _anatomy_summary(
            primary_summary=primary_summary,
            addon_summary=addon_summary,
            overlap_summary=shadow_overlap,
        ),
        "addon_concentration": _symbol_concentration(addon_rows),
        "active_exposure": _active_exposure_summary(primary_rows, addon_rows),
        "weight_sources": _weight_source_summary(primary_rows, addon_rows),
        "daily_ticket_rate": _daily_ticket_summary(daily_ticket_rows),
        "symbol_day_ticket_rate": _symbol_day_ticket_summary(symbol_day_ticket_rows),
        "same_symbol_entry_gaps": _same_symbol_entry_gap_summary(same_symbol_entry_gap_rows),
        "addon_cooldown_simulation": _cooldown_simulation_summary(
            trade_rows=addon_rows,
            order_rows=addon_order_rows,
            trade_cooldown_minutes=config.simulate_addon_same_symbol_trade_cooldown_minutes,
            entry_order_cooldown_minutes=config.simulate_addon_same_symbol_entry_order_cooldown_minutes,
        ),
        "addon_cycles": _cycle_summary(addon_cycles, now_ms=_cycle_summary_now_ms(config)),
    }

    historical_csv = Path(config.historical_blended_trades_csv).expanduser() if config.historical_blended_trades_csv else None
    diff_rows: list[dict[str, Any]] = []
    historical_addon_rows: list[dict[str, Any]] = []
    if historical_csv:
        historical = pl.read_csv(historical_csv)
        historical_primary_rows = _clean_trade_rows(_rows(historical, source="primary"))
        historical_addon_rows = _clean_trade_rows(_rows(historical, source="addon"))
        historical_primary_summary = _trade_summary(historical_primary_rows)
        historical_addon_summary = _trade_summary(historical_addon_rows)
        historical_overlap = _overlap_summary(historical_primary_rows, historical_addon_rows)
        diff_rows, diff_summary = _key_diff(
            shadow_addon_rows=addon_rows,
            historical_addon_rows=historical_addon_rows,
        )
        summary["historical"] = {
            "primary": historical_primary_summary,
            "addon": historical_addon_summary,
            "overlap": historical_overlap,
            "anatomy": _anatomy_summary(
                primary_summary=historical_primary_summary,
                addon_summary=historical_addon_summary,
                overlap_summary=historical_overlap,
            ),
            "concentration": _symbol_concentration(historical_addon_rows),
            "key_diff": diff_summary,
        }
        summary["historical_anatomy_drift"] = _anatomy_drift(
            summary["shadow_anatomy"],
            summary["historical"]["anatomy"],
        )
        summary["historical_concentration_drift"] = _concentration_drift(
            summary["addon_concentration"],
            summary["historical"]["concentration"],
        )
    summary["gate"] = _gate_summary(summary, config)

    output_dir = Path(config.output_dir).expanduser() if config.output_dir else addon_root / "reports" / config.report_name
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{config.report_name}.md"
    payload: dict[str, Any] = {
        "primary_data_root": str(primary_root),
        "addon_data_root": str(addon_root),
        "historical_blended_trades_csv": str(historical_csv) if historical_csv else "",
        "summary": summary,
        "report_path": str(report_path),
        "key_diff_csv_path": None,
        "addon_concentration_csv_path": None,
        "historical_addon_concentration_csv_path": None,
        "active_exposure_csv_path": None,
        "active_exposure_contributors_csv_path": None,
        "daily_ticket_rate_csv_path": None,
        "daily_ticket_contributors_csv_path": None,
        "symbol_day_ticket_rate_csv_path": None,
        "same_symbol_entry_gap_csv_path": None,
        "cooldown_simulation_csv_path": None,
        "unmatched_entry_order_attempts_csv_path": None,
        "cycle_pressure_csv_path": None,
        "entry_order_attempts_csv_path": None,
    }
    addon_concentration_rows = _symbol_concentration_rows(addon_rows)
    if addon_concentration_rows:
        addon_concentration_csv = output_dir / f"{config.report_name}_addon_symbol_concentration.csv"
        pl.DataFrame(addon_concentration_rows).write_csv(addon_concentration_csv)
        payload["addon_concentration_csv_path"] = str(addon_concentration_csv)
    active_exposure_rows = _active_exposure_rows(primary_rows, addon_rows)
    if active_exposure_rows:
        active_exposure_csv = output_dir / f"{config.report_name}_active_exposure.csv"
        pl.DataFrame(active_exposure_rows).write_csv(active_exposure_csv)
        payload["active_exposure_csv_path"] = str(active_exposure_csv)
        active_exposure_contributors_csv = output_dir / f"{config.report_name}_active_exposure_contributors.csv"
        pl.DataFrame(_active_exposure_contributor_rows(primary_rows, addon_rows)).write_csv(active_exposure_contributors_csv)
        payload["active_exposure_contributors_csv_path"] = str(active_exposure_contributors_csv)
    if daily_ticket_rows:
        daily_ticket_csv = output_dir / f"{config.report_name}_daily_ticket_rate.csv"
        pl.DataFrame(daily_ticket_rows).write_csv(daily_ticket_csv)
        payload["daily_ticket_rate_csv_path"] = str(daily_ticket_csv)
        daily_ticket_contributors_csv = output_dir / f"{config.report_name}_daily_ticket_contributors.csv"
        pl.DataFrame(
            _daily_ticket_contributor_rows(
                primary_rows=primary_rows,
                addon_rows=addon_rows,
                primary_order_rows=primary_order_rows,
                addon_order_rows=addon_order_rows,
            )
        ).write_csv(daily_ticket_contributors_csv)
        payload["daily_ticket_contributors_csv_path"] = str(daily_ticket_contributors_csv)
    if symbol_day_ticket_rows:
        symbol_day_ticket_csv = output_dir / f"{config.report_name}_symbol_day_ticket_rate.csv"
        pl.DataFrame(symbol_day_ticket_rows).write_csv(symbol_day_ticket_csv)
        payload["symbol_day_ticket_rate_csv_path"] = str(symbol_day_ticket_csv)
    if same_symbol_entry_gap_rows:
        same_symbol_entry_gap_csv = output_dir / f"{config.report_name}_same_symbol_entry_gaps.csv"
        pl.DataFrame(same_symbol_entry_gap_rows).write_csv(same_symbol_entry_gap_csv)
        payload["same_symbol_entry_gap_csv_path"] = str(same_symbol_entry_gap_csv)
    if cooldown_simulation_rows:
        cooldown_simulation_csv = output_dir / f"{config.report_name}_cooldown_simulation.csv"
        pl.DataFrame(cooldown_simulation_rows).write_csv(cooldown_simulation_csv)
        payload["cooldown_simulation_csv_path"] = str(cooldown_simulation_csv)
    entry_order_attempt_rows = [
        *_entry_order_attempt_rows(source="primary", rows=primary_order_rows),
        *_entry_order_attempt_rows(source="addon", rows=addon_order_rows),
    ]
    if entry_order_attempt_rows:
        entry_order_attempts_csv = output_dir / f"{config.report_name}_entry_order_attempts.csv"
        pl.DataFrame(entry_order_attempt_rows).write_csv(entry_order_attempts_csv)
        payload["entry_order_attempts_csv_path"] = str(entry_order_attempts_csv)
    unmatched_entry_order_attempt_rows = [
        *_unmatched_entry_order_attempt_rows(
            source="primary",
            order_rows=primary_order_rows,
            trade_rows=primary_rows,
            now_ms=order_reconciliation_now_ms,
        ),
        *_unmatched_entry_order_attempt_rows(
            source="addon",
            order_rows=addon_order_rows,
            trade_rows=addon_rows,
            now_ms=order_reconciliation_now_ms,
        ),
    ]
    if unmatched_entry_order_attempt_rows:
        unmatched_entry_order_attempts_csv = output_dir / f"{config.report_name}_unmatched_entry_order_attempts.csv"
        pl.DataFrame(unmatched_entry_order_attempt_rows).write_csv(unmatched_entry_order_attempts_csv)
        payload["unmatched_entry_order_attempts_csv_path"] = str(unmatched_entry_order_attempts_csv)
    cycle_pressure_rows = _cycle_pressure_rows(addon_cycles)
    if cycle_pressure_rows:
        cycle_pressure_csv = output_dir / f"{config.report_name}_cycle_pressure.csv"
        pl.DataFrame(cycle_pressure_rows).write_csv(cycle_pressure_csv)
        payload["cycle_pressure_csv_path"] = str(cycle_pressure_csv)
    if historical_csv:
        historical_concentration_rows = _symbol_concentration_rows(historical_addon_rows)
        if historical_concentration_rows:
            historical_concentration_csv = output_dir / f"{config.report_name}_historical_addon_symbol_concentration.csv"
            pl.DataFrame(historical_concentration_rows).write_csv(historical_concentration_csv)
            payload["historical_addon_concentration_csv_path"] = str(historical_concentration_csv)
        if diff_rows:
            key_diff_csv = output_dir / f"{config.report_name}_key_diff.csv"
            pl.DataFrame(diff_rows).write_csv(key_diff_csv)
            payload["key_diff_csv_path"] = str(key_diff_csv)
    report_path.write_text(_markdown(payload), encoding="utf-8")
    return payload
