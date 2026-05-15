from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import polars as pl

from . import demo_execution as demo_execution_module
from .config import DailyCloseFadeConfig, ForwardTestConfig, ResearchConfig
from .demo_execution import DemoSyncConfig
from .demo_fast_protection import DemoFastProtectionConfig, run_demo_fast_protection
from .forward_test import default_forward_sleeves, run_forward_sleeves


DEMO_CYCLE_SLEEVES = ("stage4_selected",)
DEMO_CYCLE_ORDER_PREFIXES = {
    "stage4_selected": "stg4",
}


@dataclass(frozen=True, slots=True)
class DemoCycleConfig:
    max_order_notional: float = 0.0
    max_new_orders: int = 5
    max_total_new_notional: float = 0.0
    use_wallet_balance: bool = False
    wallet_balance_fraction: float = 1.0
    max_order_notional_pct_equity: float = 0.10
    max_total_new_notional_pct_equity: float = 1.0
    cancel_stale_minutes: int = 5
    price_offset_bps: float = 2.0
    submit_orders: bool = False
    confirmed: bool = False
    allow_market_exit: bool = True
    send_telegram: bool = False
    entry_sleeves: tuple[str, ...] = ("stage4_selected",)
    entry_leverage: float = 1.0
    active_start_minute: int = 23 * 60 + 15
    active_end_minute: int = 6 * 60 + 30
    ignore_active_window: bool = False
    lock_stale_minutes: int = 30
    forward_mode: str = "open_from_scan"
    require_first_slice: bool = True
    require_contiguous_twap: bool = True
    fast_protection_seconds: float = 0.0
    fast_max_exit_submits_per_run: int = 5


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
    trade_stream: Any | None = None,
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
            trade_stream=trade_stream,
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
    trade_stream: Any | None,
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
    entry_sleeves = set(cycle.entry_sleeves)
    should_run_forward = (inside_window and not entries_paused and bool(entry_sleeves)) or existing_state["paper_open"] > 0
    pre_sync_fast_payloads = _run_pre_sync_fast_protection(
        base_root,
        config=config,
        fade_config=fade,
        cycle=cycle,
        now=now_dt,
        market_client=market_client,
        execution_client=execution_client,
        trade_stream=trade_stream,
        api_key=api_key,
        api_secret=api_secret,
    )

    if should_run_forward:
        if forward_runner is not None:
            forward_payload = forward_runner(
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
            forward_payload = run_forward_sleeves(
                base_root,
                config=config,
                fade_config=fade,
                forward_config=forward,
                now=now_dt,
                client=forward_client,
                send_telegram=False,
                sleeves=sleeve_configs,
                mode=cycle.forward_mode,
                require_first_slice=cycle.require_first_slice,
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
            sleeve_entries_paused = entries_paused or sleeve not in entry_sleeves
            sync_config = _sync_config_from_cycle(cycle, sleeve_prefix=order_prefix, entries_paused=sleeve_entries_paused)
            try:
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
                results.append(
                    _sleeve_result(
                        sleeve,
                        sleeve_root,
                        order_prefix,
                        sleeve_entries_paused,
                        sync_payload,
                        fast_payload=pre_sync_fast_payloads.get(sleeve),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - keep the cycle report useful if one sleeve fails
                results.append(_failed_sleeve_result(sleeve, sleeve_root, order_prefix, sleeve_entries_paused, str(exc)))

    summary = summarize_demo_cycle_results(results)
    payload = {
        "now": now_dt.isoformat(),
        "paused": pause,
        "active_window": {
            "inside": inside_window,
            "entries_paused": entries_paused,
            "entry_sleeves": list(cycle.entry_sleeves),
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
    if cycle.send_telegram or forward.send_telegram:
        payload["telegram"] = {
            "enabled": True,
            "sent": False,
            "reason": "cycle_summary_disabled_use_forward_audit",
        }
    _write_demo_cycle_outputs(base_root, payload)
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
        f"Entry sleeves: `{', '.join(payload.get('config', {}).get('entry_sleeves') or []) or 'none'}`",
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


def _write_demo_cycle_outputs(data_root: str | Path, payload: dict[str, Any]) -> None:
    output_dir = Path(data_root).expanduser() / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bybit_demo_cycle_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "bybit_demo_cycle_report.md").write_text(format_demo_cycle_report(payload), encoding="utf-8")


def _validate_cycle_config(config: DemoCycleConfig) -> None:
    if config.max_order_notional < 0.0:
        raise ValueError("max_order_notional must be non-negative")
    if config.max_new_orders < 0:
        raise ValueError("max_new_orders cannot be negative")
    if config.max_total_new_notional < 0.0:
        raise ValueError("max_total_new_notional must be non-negative")
    if not 0.0 < config.wallet_balance_fraction <= 1.0:
        raise ValueError("wallet_balance_fraction must be in (0, 1]")
    if config.max_order_notional_pct_equity < 0.0:
        raise ValueError("max_order_notional_pct_equity cannot be negative")
    if config.max_total_new_notional_pct_equity < 0.0:
        raise ValueError("max_total_new_notional_pct_equity cannot be negative")
    invalid_sleeves = sorted(set(config.entry_sleeves).difference(DEMO_CYCLE_SLEEVES))
    if invalid_sleeves:
        raise ValueError(f"unknown demo entry sleeve(s): {', '.join(invalid_sleeves)}")
    if config.entry_leverage < 0.0:
        raise ValueError("entry_leverage cannot be negative")
    if config.fast_protection_seconds < 0.0:
        raise ValueError("fast_protection_seconds cannot be negative")
    if config.fast_max_exit_submits_per_run < 0:
        raise ValueError("fast_max_exit_submits_per_run cannot be negative")
    if not 0 <= config.active_start_minute < 24 * 60:
        raise ValueError("active_start_minute must be inside one UTC day")
    if not 0 <= config.active_end_minute < 24 * 60:
        raise ValueError("active_end_minute must be inside one UTC day")
    if config.lock_stale_minutes < 1:
        raise ValueError("lock_stale_minutes must be positive")
    if config.forward_mode not in {"scan", "mark_only", "open_from_scan"}:
        raise ValueError("forward_mode must be one of: scan, mark_only, open_from_scan")


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
        owner_pid = _lock_owner_pid(lock_path)
        owner_dead = owner_pid is not None and not _pid_is_running(owner_pid)
        if owner_dead or age_seconds > stale_after_seconds:
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


def _lock_owner_pid(lock_path: Path) -> int | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        "entry_leverage": cycle.entry_leverage,
        "require_contiguous_twap": cycle.require_contiguous_twap,
    }
    kwargs["order_link_prefix"] = sleeve_prefix
    kwargs["pause_new_entries"] = entries_paused
    return DemoSyncConfig(**kwargs)


def _sleeve_result(
    sleeve: str,
    sleeve_root: Path,
    order_prefix: str,
    paused: bool,
    sync_payload: dict[str, Any],
    fast_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fast_failed = bool(fast_payload and str(fast_payload.get("status") or "") == "failed")
    return {
        "sleeve": sleeve,
        "status": "failed" if fast_failed else "ok",
        "data_root": str(sleeve_root),
        "order_link_prefix": order_prefix,
        "paused": paused,
        "rows": sync_payload.get("rows", {}),
        "summary": sync_payload.get("summary", {}),
        "sync": sync_payload,
        "fast_protection": fast_payload or {},
        "error": str((fast_payload or {}).get("error") or "") if fast_failed else "",
    }


def _run_pre_sync_fast_protection(
    base_root: Path,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig,
    cycle: DemoCycleConfig,
    now: datetime,
    market_client: Any | None,
    execution_client: Any | None,
    trade_stream: Any | None,
    api_key: str | None,
    api_secret: str | None,
) -> dict[str, dict[str, Any]]:
    if cycle.fast_protection_seconds <= 0.0 or not cycle.submit_orders or not cycle.confirmed:
        return {}
    payloads: dict[str, dict[str, Any]] = {}
    for sleeve in DEMO_CYCLE_SLEEVES:
        order_prefix = DEMO_CYCLE_ORDER_PREFIXES[sleeve]
        sleeve_root = base_root / "forward_sleeves" / sleeve
        try:
            payload = _run_fast_protection_for_sleeve(
                sleeve_root,
                config=config,
                fade_config=fade_config,
                cycle=cycle,
                now=now,
                order_prefix=order_prefix,
                market_client=market_client,
                execution_client=execution_client,
                trade_stream=trade_stream,
                api_key=api_key,
                api_secret=api_secret,
            )
        except Exception as exc:  # noqa: BLE001 - sync/reconcile should still run and report the failure
            payload = {
                "now": now.isoformat(),
                "status": "failed",
                "rows": {
                    "active_trades": 0,
                    "symbols": 0,
                    "trigger_events": 0,
                    "submitted_or_unknown": 0,
                    "duplicate_blocks": 0,
                    "submit_blocks": 0,
                    "callback_count": 0,
                },
                "summary": {},
                "error": str(exc),
            }
        if payload is not None:
            payloads[sleeve] = payload
    return payloads


def _run_fast_protection_for_sleeve(
    sleeve_root: Path,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig,
    cycle: DemoCycleConfig,
    now: datetime,
    order_prefix: str,
    market_client: Any | None,
    execution_client: Any | None,
    trade_stream: Any | None,
    api_key: str | None,
    api_secret: str | None,
) -> dict[str, Any] | None:
    if cycle.fast_protection_seconds <= 0.0 or not cycle.submit_orders or not cycle.confirmed:
        return None
    return run_demo_fast_protection(
        sleeve_root,
        config=config,
        fade_config=fade_config,
        protection_config=DemoFastProtectionConfig(
            runtime_seconds=cycle.fast_protection_seconds,
            submit_exits=True,
            confirmed=True,
            order_link_prefix=order_prefix,
            max_exit_submits_per_run=cycle.fast_max_exit_submits_per_run,
        ),
        now=now,
        market_client=market_client,
        execution_client=execution_client,
        trade_stream=trade_stream,
        api_key=api_key,
        api_secret=api_secret,
    )


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
