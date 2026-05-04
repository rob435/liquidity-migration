from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from hashlib import blake2b
from pathlib import Path
from typing import Any, Callable, Iterator

import polars as pl

from . import demo_execution as demo_execution_module
from .config import DailyCloseFadeConfig, ForwardTestConfig, ResearchConfig
from .demo_execution import DemoSyncConfig
from .forward_test import default_forward_sleeves, run_forward_sleeves
from .telegram import send_telegram_message


DEMO_CYCLE_SLEEVES = ("control_top_1_30", "core_31_150", "microcap_151_plus")
DEMO_CYCLE_ORDER_PREFIXES = {
    "control_top_1_30": "ctl",
    "core_31_150": "core",
    "microcap_151_plus": "micro",
}
RETRYABLE_DEMO_STATUSES = ("dry_run", "skipped", "place_failed", "pending_submit")


@dataclass(frozen=True, slots=True)
class DemoCycleConfig:
    max_order_notional: float = 10.0
    max_new_orders: int = 5
    max_total_new_notional: float = 50.0
    use_wallet_balance: bool = False
    wallet_balance_fraction: float = 1.0
    max_order_notional_pct_equity: float = 0.80
    max_total_new_notional_pct_equity: float = 1.0
    cancel_stale_minutes: int = 5
    price_offset_bps: float = 2.0
    submit_orders: bool = False
    confirmed: bool = False
    allow_market_exit: bool = True
    send_telegram: bool = False
    active_start_minute: int = 22 * 60 + 5
    active_end_minute: int = 2 * 60 + 30
    ignore_active_window: bool = False
    lock_stale_minutes: int = 30


ForwardSleevesRunner = Callable[..., dict[str, Any]]
DemoSyncRunner = Callable[..., dict[str, Any]]


def run_bybit_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    cycle_config: DemoCycleConfig | None = None,
    fade_config: DailyCloseFadeConfig | None = None,
    forward_config: ForwardTestConfig | None = None,
    now: datetime | None = None,
    forward_client: Any | None = None,
    market_client: Any | None = None,
    execution_client: Any | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    forward_runner: ForwardSleevesRunner | None = None,
    sync_runner: DemoSyncRunner | None = None,
) -> dict[str, Any]:
    cycle = cycle_config or DemoCycleConfig()
    _validate_cycle_config(cycle)
    if cycle.submit_orders and not cycle.confirmed:
        raise RuntimeError("Refusing demo cycle order submission without --i-understand-demo-sync")

    base_root = Path(data_root).expanduser()
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    with _demo_cycle_lock(base_root, now=now_dt, stale_minutes=cycle.lock_stale_minutes):
        return _run_bybit_demo_cycle_unlocked(
            base_root,
            config=config,
            cycle=cycle,
            fade_config=fade_config,
            forward_config=forward_config,
            now=now_dt,
            forward_client=forward_client,
            market_client=market_client,
            execution_client=execution_client,
            api_key=api_key,
            api_secret=api_secret,
            forward_runner=forward_runner,
            sync_runner=sync_runner,
        )


def _run_bybit_demo_cycle_unlocked(
    base_root: Path,
    *,
    config: ResearchConfig,
    cycle: DemoCycleConfig,
    fade_config: DailyCloseFadeConfig | None,
    forward_config: ForwardTestConfig | None,
    now: datetime,
    forward_client: Any | None,
    market_client: Any | None,
    execution_client: Any | None,
    api_key: str | None,
    api_secret: str | None,
    forward_runner: ForwardSleevesRunner | None,
    sync_runner: DemoSyncRunner | None,
) -> dict[str, Any]:
    now_dt = now
    fade = fade_config or config.daily_close_fade
    forward = forward_config or config.forward_test
    sleeve_configs = {name: default_forward_sleeves(fade)[name] for name in DEMO_CYCLE_SLEEVES}
    pause = read_demo_pause_state(base_root)
    inside_window = cycle.ignore_active_window or _is_active_window(
        now_dt,
        start_minute=cycle.active_start_minute,
        end_minute=cycle.active_end_minute,
    )
    existing_state = _existing_active_state(base_root)
    entries_paused = pause["paused"] or not inside_window

    if inside_window or existing_state["has_active_state"]:
        forward_payload = (forward_runner or run_forward_sleeves)(
            base_root,
            config=config,
            fade_config=fade,
            forward_config=forward,
            now=now_dt,
            client=forward_client,
            send_telegram=False,
            sleeves=sleeve_configs,
        )
    else:
        forward_payload = {
            "now": now_dt.isoformat(),
            "rows": {"sleeves": len(DEMO_CYCLE_SLEEVES), "skipped_inactive_window": True},
            "results": [
                {"sleeve": sleeve, "data_root": str(base_root / "forward_sleeves" / sleeve)}
                for sleeve in DEMO_CYCLE_SLEEVES
            ],
        }

    sleeve_roots = _sleeve_roots(base_root, forward_payload)
    results: list[dict[str, Any]] = []
    if not inside_window and not existing_state["has_active_state"]:
        for sleeve in DEMO_CYCLE_SLEEVES:
            results.append(
                _inactive_sleeve_result(
                    sleeve,
                    sleeve_roots[sleeve],
                    DEMO_CYCLE_ORDER_PREFIXES[sleeve],
                    entries_paused,
                )
            )
    else:
        for sleeve in DEMO_CYCLE_SLEEVES:
            sleeve_root = sleeve_roots[sleeve]
            order_prefix = DEMO_CYCLE_ORDER_PREFIXES[sleeve]
            sync_config = _sync_config_from_cycle(cycle, sleeve_prefix=order_prefix, entries_paused=entries_paused)
            try:
                with _demo_sync_compat_context(
                    sleeve_root=sleeve_root,
                    sleeve_prefix=order_prefix,
                    filter_paused_entries=entries_paused and not _demo_sync_supports_entry_pause(),
                ):
                    sync_payload = (sync_runner or demo_execution_module.run_bybit_demo_sync)(
                        sleeve_root,
                        config=config,
                        sync_config=sync_config,
                        now=now_dt,
                        market_client=market_client,
                        execution_client=execution_client,
                        api_key=api_key,
                        api_secret=api_secret,
                    )
                results.append(_sleeve_result(sleeve, sleeve_root, order_prefix, entries_paused, sync_payload))
            except Exception as exc:  # noqa: BLE001 - keep the cycle report useful if one sleeve fails
                results.append(_failed_sleeve_result(sleeve, sleeve_root, order_prefix, entries_paused, str(exc)))

    summary = summarize_demo_cycle_results(results)
    payload = {
        "now": now_dt.isoformat(),
        "paused": pause,
        "active_window": {
            "inside": inside_window,
            "entries_paused": entries_paused,
            "start_minute": cycle.active_start_minute,
            "end_minute": cycle.active_end_minute,
            "existing_active_state": existing_state,
        },
        "config": asdict(cycle),
        "forward": {
            "rows": forward_payload.get("rows", {}),
            "results": forward_payload.get("results", []),
        },
        "rows": {
            "sleeves": len(results),
            "failed_sleeves": summary["failed_sleeves"],
            "new_orders": summary["new_orders"],
            "ledger_orders": summary["ledger_orders"],
        },
        "summary": summary,
        "sleeves": results,
    }
    _write_demo_cycle_outputs(base_root, payload)

    telegram_enabled = cycle.send_telegram or forward.send_telegram
    send_telegram_message(format_demo_cycle_message(payload), enabled=telegram_enabled)
    return payload


def read_demo_pause_state(data_root: str | Path) -> dict[str, Any]:
    path = Path(data_root).expanduser() / "DEMO_PAUSED"
    reason = ""
    if path.exists():
        try:
            reason = path.read_text(encoding="utf-8").strip()[:500]
        except OSError as exc:
            reason = f"unreadable pause file: {exc}"
    return {"paused": path.exists(), "path": str(path), "reason": reason}


def summarize_demo_cycle_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "sleeves": len(results),
        "failed_sleeves": 0,
        "new_orders": 0,
        "ledger_orders": 0,
        "placed": 0,
        "accepted": 0,
        "dry_run": 0,
        "skipped": 0,
        "cancel_requested": 0,
        "place_failed": 0,
        "estimated_notional": 0.0,
    }
    for row in results:
        sleeve_summary = row.get("summary", {})
        sleeve_rows = row.get("rows", {})
        if row.get("status") == "failed":
            summary["failed_sleeves"] += 1
        summary["new_orders"] += int(sleeve_rows.get("new_orders", 0) or 0)
        summary["ledger_orders"] += int(sleeve_rows.get("ledger_orders", 0) or 0)
        for key in ("placed", "accepted", "dry_run", "skipped", "cancel_requested", "place_failed"):
            summary[key] += int(sleeve_summary.get(key, 0) or 0)
        summary["estimated_notional"] += float(sleeve_summary.get("estimated_notional", 0.0) or 0.0)
    return summary


def format_demo_cycle_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    pause = payload.get("paused", {})
    active = payload.get("active_window", {})
    lines = [
        "# Bybit Demo Cycle",
        "",
        f"Now: {payload.get('now')}",
        f"Paused: `{bool(pause.get('paused'))}`",
        f"Inside active window: `{bool(active.get('inside'))}`",
        f"Entries paused: `{bool(active.get('entries_paused'))}`",
        f"Submit orders: `{bool(payload.get('config', {}).get('submit_orders'))}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Sleeves | {summary.get('sleeves', 0)} |",
        f"| Failed sleeves | {summary.get('failed_sleeves', 0)} |",
        f"| New orders | {summary.get('new_orders', 0)} |",
        f"| Ledger orders | {summary.get('ledger_orders', 0)} |",
        f"| Placed | {summary.get('placed', 0)} |",
        f"| Accepted | {summary.get('accepted', 0)} |",
        f"| Dry run | {summary.get('dry_run', 0)} |",
        f"| Skipped | {summary.get('skipped', 0)} |",
        f"| Cancel requested | {summary.get('cancel_requested', 0)} |",
        f"| Place failed | {summary.get('place_failed', 0)} |",
        f"| Estimated notional | {summary.get('estimated_notional', 0.0):.2f} |",
        "",
        "## Sleeves",
        "",
        "| Sleeve | Status | Prefix | New | Ledger | Placed | Accepted | Dry Run | Skipped | Cancel | Notional | Root | Error |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in payload.get("sleeves", []):
        sleeve_summary = row.get("summary", {})
        sleeve_rows = row.get("rows", {})
        lines.append(
            f"| {row.get('sleeve')} | {row.get('status')} | {row.get('order_link_prefix')} | "
            f"{sleeve_rows.get('new_orders', 0)} | {sleeve_rows.get('ledger_orders', 0)} | "
            f"{sleeve_summary.get('placed', 0)} | {sleeve_summary.get('accepted', 0)} | "
            f"{sleeve_summary.get('dry_run', 0)} | "
            f"{sleeve_summary.get('skipped', 0)} | {sleeve_summary.get('cancel_requested', 0)} | "
            f"{float(sleeve_summary.get('estimated_notional', 0.0) or 0.0):.2f} | "
            f"{row.get('data_root')} | {str(row.get('error') or '')[:120]} |"
        )
    if pause.get("paused"):
        lines.extend(
            [
                "",
                "## Pause",
                "",
                f"Pause file: `{pause.get('path')}`",
                "",
                "New demo entries were blocked for this cycle; reduce-only exits and reconciliation were still allowed.",
                "",
            ]
        )
    else:
        lines.append("")
    return "\n".join(lines)


def format_demo_cycle_message(payload: dict[str, Any]) -> str:
    pause = payload.get("paused", {})
    summary = payload.get("summary", {})
    active = payload.get("active_window", {})
    lines = [
        "Bybit demo cycle",
        f"now: {payload.get('now')}",
        (
            f"paused: {bool(pause.get('paused'))} "
            f"window={bool(active.get('inside'))} "
            f"entries_paused={bool(active.get('entries_paused'))} "
            f"submit={bool(payload.get('config', {}).get('submit_orders'))}"
        ),
        (
            f"new={summary.get('new_orders', 0)} placed={summary.get('placed', 0)} "
            f"dry={summary.get('dry_run', 0)} skipped={summary.get('skipped', 0)} "
            f"failed_sleeves={summary.get('failed_sleeves', 0)}"
        ),
    ]
    for row in payload.get("sleeves", []):
        sleeve_summary = row.get("summary", {})
        sleeve_rows = row.get("rows", {})
        lines.append(
            f"{row.get('sleeve')}: {row.get('status')} new={sleeve_rows.get('new_orders', 0)} "
            f"placed={sleeve_summary.get('placed', 0)} dry={sleeve_summary.get('dry_run', 0)}"
        )
    return "\n".join(lines)


def _write_demo_cycle_outputs(data_root: str | Path, payload: dict[str, Any]) -> None:
    output_dir = Path(data_root).expanduser() / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bybit_demo_cycle_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "bybit_demo_cycle_report.md").write_text(format_demo_cycle_report(payload), encoding="utf-8")


def _validate_cycle_config(config: DemoCycleConfig) -> None:
    if config.max_order_notional < 0.0:
        raise ValueError("max_order_notional must be non-negative")
    if not config.use_wallet_balance and config.max_order_notional <= 0.0:
        raise ValueError("max_order_notional must be positive unless wallet balance sizing is enabled")
    if config.max_new_orders < 0:
        raise ValueError("max_new_orders cannot be negative")
    if config.max_total_new_notional < 0.0:
        raise ValueError("max_total_new_notional must be non-negative")
    if not config.use_wallet_balance and config.max_total_new_notional <= 0.0:
        raise ValueError("max_total_new_notional must be positive unless wallet balance sizing is enabled")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.max_order_notional_pct_equity < 0.0:
        raise ValueError("max_order_notional_pct_equity cannot be negative")
    if config.max_total_new_notional_pct_equity < 0.0:
        raise ValueError("max_total_new_notional_pct_equity cannot be negative")
    if not 0 <= config.active_start_minute < 24 * 60:
        raise ValueError("active_start_minute must be inside one UTC day")
    if not 0 <= config.active_end_minute < 24 * 60:
        raise ValueError("active_end_minute must be inside one UTC day")
    if config.lock_stale_minutes < 1:
        raise ValueError("lock_stale_minutes must be positive")


@contextmanager
def _demo_cycle_lock(data_root: Path, *, now: datetime, stale_minutes: int) -> Iterator[None]:
    data_root.mkdir(parents=True, exist_ok=True)
    lock_path = data_root / ".bybit_demo_cycle.lock"
    stale_after_seconds = max(stale_minutes, 1) * 60
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        try:
            age_seconds = max(0.0, now.timestamp() - lock_path.stat().st_mtime)
        except OSError:
            age_seconds = 0.0
        if age_seconds > stale_after_seconds:
            lock_path.unlink(missing_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise RuntimeError(f"Bybit demo cycle already running; lock exists at {lock_path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "started": now.isoformat()}) + "\n")
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _is_active_window(now: datetime, *, start_minute: int, end_minute: int) -> bool:
    minute = now.hour * 60 + now.minute
    if start_minute <= end_minute:
        return start_minute <= minute <= end_minute
    return minute >= start_minute or minute <= end_minute


def _existing_active_state(data_root: Path) -> dict[str, Any]:
    paper_open = 0
    demo_active = 0
    for sleeve in DEMO_CYCLE_SLEEVES:
        sleeve_root = data_root / "forward_sleeves" / sleeve
        trades = _safe_read_dataset(sleeve_root, "forward_paper_trades")
        if not trades.is_empty() and "status" in trades.columns:
            paper_open += int((trades["status"] == "open").sum())
        orders = _safe_read_dataset(sleeve_root, "demo_execution_orders")
        if not orders.is_empty() and "status" in orders.columns:
            demo_active += int(
                orders.filter(
                    pl.col("status").is_in(["accepted", "placed", "cancel_requested", "exit_submitted"])
                ).height
            )
    return {
        "paper_open": paper_open,
        "demo_active": demo_active,
        "has_active_state": bool(paper_open or demo_active),
    }


def _safe_read_dataset(data_root: Path, dataset: str) -> pl.DataFrame:
    try:
        return demo_execution_module.read_dataset(data_root, dataset)
    except Exception:  # noqa: BLE001 - stale/corrupt state should not block the cycle report
        return pl.DataFrame()


def _sync_config_from_cycle(
    cycle: DemoCycleConfig,
    *,
    sleeve_prefix: str,
    entries_paused: bool,
) -> DemoSyncConfig:
    kwargs: dict[str, Any] = {
        "max_order_notional": cycle.max_order_notional,
        "max_new_orders": cycle.max_new_orders,
        "max_total_new_notional": cycle.max_total_new_notional,
        "use_wallet_balance": cycle.use_wallet_balance,
        "wallet_balance_fraction": cycle.wallet_balance_fraction,
        "max_order_notional_pct_equity": cycle.max_order_notional_pct_equity,
        "max_total_new_notional_pct_equity": cycle.max_total_new_notional_pct_equity,
        "price_offset_bps": cycle.price_offset_bps,
        "cancel_stale_minutes": cycle.cancel_stale_minutes,
        "submit_orders": cycle.submit_orders,
        "confirmed": cycle.confirmed,
        "allow_market_exit": cycle.allow_market_exit,
    }
    known_fields = {field.name for field in fields(DemoSyncConfig)}
    for name in ("order_link_prefix", "order_link_id_prefix", "sleeve_prefix"):
        if name in known_fields:
            kwargs[name] = sleeve_prefix
    for name in ("entries_paused", "pause_new_entries", "new_entries_paused", "entry_pause"):
        if name in known_fields:
            kwargs[name] = entries_paused
    return DemoSyncConfig(**{key: value for key, value in kwargs.items() if key in known_fields})


def _demo_sync_supports_entry_pause() -> bool:
    known_fields = {field.name for field in fields(DemoSyncConfig)}
    return bool({"entries_paused", "pause_new_entries", "new_entries_paused", "entry_pause"} & known_fields)


@contextmanager
def _demo_sync_compat_context(
    *,
    sleeve_root: Path,
    sleeve_prefix: str,
    filter_paused_entries: bool,
) -> Iterator[None]:
    original_link = getattr(demo_execution_module, "_sync_order_link_id", None)
    original_read = getattr(demo_execution_module, "read_dataset", None)

    def scoped_order_link_id(trade_id: str, action: str, *, prefix: str = "") -> str:
        return _scoped_order_link_id(prefix or sleeve_prefix, trade_id, action)

    def read_dataset_compat(data_root: str | Path, dataset: str) -> pl.DataFrame:
        frame = original_read(data_root, dataset)
        if not _same_path(data_root, sleeve_root):
            return frame
        if dataset == "forward_paper_trades" and filter_paused_entries:
            return _exit_only_forward_trades(frame)
        if dataset == "demo_execution_orders":
            return _without_retryable_demo_orders(frame)
        return frame

    if original_link is not None:
        setattr(demo_execution_module, "_sync_order_link_id", scoped_order_link_id)
    if original_read is not None:
        setattr(demo_execution_module, "read_dataset", read_dataset_compat)
    try:
        yield
    finally:
        if original_link is not None:
            setattr(demo_execution_module, "_sync_order_link_id", original_link)
        if original_read is not None:
            setattr(demo_execution_module, "read_dataset", original_read)


def _sleeve_result(
    sleeve: str,
    sleeve_root: Path,
    order_prefix: str,
    paused: bool,
    sync_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "sleeve": sleeve,
        "status": "ok",
        "data_root": str(sleeve_root),
        "order_link_prefix": order_prefix,
        "paused": paused,
        "rows": sync_payload.get("rows", {}),
        "summary": sync_payload.get("summary", {}),
        "sync": sync_payload,
        "error": "",
    }


def _failed_sleeve_result(
    sleeve: str,
    sleeve_root: Path,
    order_prefix: str,
    paused: bool,
    error: str,
) -> dict[str, Any]:
    return {
        "sleeve": sleeve,
        "status": "failed",
        "data_root": str(sleeve_root),
        "order_link_prefix": order_prefix,
        "paused": paused,
        "rows": {"paper_trades": 0, "existing_orders": 0, "new_orders": 0, "ledger_orders": 0},
        "summary": {
            "orders": 0,
            "placed": 0,
            "accepted": 0,
            "dry_run": 0,
            "skipped": 0,
            "cancel_requested": 0,
            "place_failed": 0,
            "estimated_notional": 0.0,
        },
        "sync": {},
        "error": error,
    }


def _inactive_sleeve_result(
    sleeve: str,
    sleeve_root: Path,
    order_prefix: str,
    entries_paused: bool,
) -> dict[str, Any]:
    return {
        "sleeve": sleeve,
        "status": "inactive_window",
        "data_root": str(sleeve_root),
        "order_link_prefix": order_prefix,
        "paused": entries_paused,
        "rows": {"paper_trades": 0, "existing_orders": 0, "new_orders": 0, "ledger_orders": 0},
        "summary": {
            "orders": 0,
            "placed": 0,
            "accepted": 0,
            "dry_run": 0,
            "skipped": 0,
            "cancel_requested": 0,
            "place_failed": 0,
            "estimated_notional": 0.0,
        },
        "sync": {},
        "error": "",
    }


def _sleeve_roots(data_root: Path, forward_payload: dict[str, Any]) -> dict[str, Path]:
    roots = {sleeve: data_root / "forward_sleeves" / sleeve for sleeve in DEMO_CYCLE_SLEEVES}
    for row in forward_payload.get("results", []):
        sleeve = str(row.get("sleeve") or "")
        root = row.get("data_root")
        if sleeve in roots and root:
            roots[sleeve] = Path(str(root)).expanduser()
    return roots


def _scoped_order_link_id(prefix: str, trade_id: str, action: str) -> str:
    clean_prefix = _compact_token(prefix, max_len=8) or "s"
    action_key = _compact_token(action, max_len=1) or "x"
    digest = blake2b(f"{prefix}:{trade_id}:{action}".encode("utf-8"), digest_size=8).hexdigest()
    return f"agc{clean_prefix}{action_key}{digest}"[:36]


def _compact_token(value: str, *, max_len: int) -> str:
    return "".join(char for char in value.lower() if char.isalnum())[:max_len]


def _without_retryable_demo_orders(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty() or "status" not in frame.columns:
        return frame
    return frame.filter(~pl.col("status").is_in(RETRYABLE_DEMO_STATUSES))


def _exit_only_forward_trades(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty() or "status" not in frame.columns:
        return frame
    return frame.filter(pl.col("status") != "open")


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser() == Path(right).expanduser()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
