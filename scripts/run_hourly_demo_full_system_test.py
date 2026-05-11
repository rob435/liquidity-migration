from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aggression_carry.config import load_config
from aggression_carry.demo_cycle import DemoCycleConfig, run_bybit_demo_cycle
from aggression_carry.forward_audit import run_forward_demo_audit
from aggression_carry.forward_test import default_forward_sleeves, run_forward_sleeves
from aggression_carry.telegram import send_telegram_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated full Bybit demo functional cycle on an arbitrary hour.")
    parser.add_argument("--config", default="configs/volume_alpha.default.yaml")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--signal-time", required=True, help="UTC HH:MM signal time for this isolated run.")
    parser.add_argument("--duration-minutes", type=int, default=265)
    parser.add_argument("--sleeves", default="stage4_selected")
    parser.add_argument("--max-order-notional-pct-equity", type=float, default=0.10)
    parser.add_argument("--max-total-new-notional-pct-equity", type=float, default=1.0)
    parser.add_argument("--entry-leverage", type=float, default=1.0)
    parser.add_argument("--fast-protection-seconds", type=float, default=55.0)
    parser.add_argument("--cycle-delay-seconds", type=float, default=2.0)
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument(
        "--i-understand-hourly-demo-submit",
        action="store_true",
        help="Required. This isolated harness submits Bybit demo orders and can pollute the supplied data root.",
    )
    args = parser.parse_args()
    if not args.i_understand_hourly_demo_submit:
        raise SystemExit("Refusing hourly demo submit harness without --i-understand-hourly-demo-submit")

    data_root = Path(args.data_root).expanduser()
    data_root.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    signal_minute = _signal_minute(args.signal_time)
    signal_dt = _signal_datetime(datetime.now(tz=UTC), signal_minute)
    fade = replace(config.daily_close_fade, signal_minute=signal_minute)
    sleeve_names = tuple(item.strip() for item in args.sleeves.split(",") if item.strip())
    sleeve_configs = {name: default_forward_sleeves(fade)[name] for name in sleeve_names}
    active_end_minute = (signal_minute + max(args.duration_minutes, 90)) % (24 * 60)
    cycle_config = DemoCycleConfig(
        submit_orders=True,
        confirmed=True,
        entry_sleeves=sleeve_names,
        max_order_notional=0.0,
        max_total_new_notional=0.0,
        use_wallet_balance=True,
        max_order_notional_pct_equity=args.max_order_notional_pct_equity,
        max_total_new_notional_pct_equity=args.max_total_new_notional_pct_equity,
        entry_leverage=args.entry_leverage,
        active_start_minute=signal_minute,
        active_end_minute=active_end_minute,
        forward_mode="open_from_scan",
        require_first_slice=True,
        require_contiguous_twap=True,
        fast_protection_seconds=args.fast_protection_seconds,
    )
    event_log = data_root / "reports" / "hourly_full_system_events.jsonl"
    event_log.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "started_at": datetime.now(tz=UTC).isoformat(),
        "data_root": str(data_root),
        "signal_time": args.signal_time,
        "signal_minute": signal_minute,
        "duration_minutes": args.duration_minutes,
        "sleeves": sleeve_names,
        "fast_protection_seconds": args.fast_protection_seconds,
        "cycle_delay_seconds": args.cycle_delay_seconds,
    }
    (data_root / "hourly_full_system_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if args.telegram:
        send_telegram_message(
            "MODEL050426 hourly full-system DEMO TEST starting "
            f"signal={args.signal_time} UTC root={data_root}",
            enabled=True,
        )

    _append_event(event_log, {"kind": "scan_start", "at": datetime.now(tz=UTC).isoformat()})
    scan_payload = run_forward_sleeves(
        data_root,
        config=config,
        fade_config=fade,
        forward_config=config.forward_test,
        now=datetime.now(tz=UTC),
        send_telegram=False,
        sleeves=sleeve_configs,
        mode="scan",
    )
    _append_event(event_log, {"kind": "scan_done", "at": datetime.now(tz=UTC).isoformat(), "payload": _compact(scan_payload)})
    if _candidate_count(scan_payload) <= 0:
        audit_payload = run_forward_demo_audit(data_root, send_telegram=args.telegram, now=datetime.now(tz=UTC))
        summary = {
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "status": "no_candidates",
            "failures": 0,
            "scan": _compact(scan_payload),
            "final_audit": _compact(audit_payload),
        }
        (data_root / "hourly_full_system_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        _append_event(event_log, {"kind": "done", **summary})
        if args.telegram:
            send_telegram_message(
                "MODEL050426 hourly full-system DEMO TEST finished without entries "
                f"signal={args.signal_time} UTC status=no_candidates root={data_root}",
                enabled=True,
            )
        return 0
    run_forward_demo_audit(data_root, send_telegram=args.telegram, now=datetime.now(tz=UTC))

    end_dt = signal_dt + timedelta(minutes=args.duration_minutes)
    failures = 0
    while datetime.now(tz=UTC) < end_dt:
        _sleep_to_next_minute(delay_seconds=args.cycle_delay_seconds)
        now = datetime.now(tz=UTC)
        try:
            cycle_payload = run_bybit_demo_cycle(
                data_root,
                config=config,
                cycle_config=cycle_config,
                fade_config=fade,
                forward_config=config.forward_test,
                now=now,
            )
            audit_payload = run_forward_demo_audit(data_root, send_telegram=args.telegram, now=datetime.now(tz=UTC))
            _append_event(
                event_log,
                {
                    "kind": "cycle",
                    "at": now.isoformat(),
                    "cycle": _compact(cycle_payload),
                    "audit": _compact(audit_payload),
                },
            )
            if (
                _all_paper_trades_closed(audit_payload)
                and _demo_execution_resolved(audit_payload)
                and now >= signal_dt + timedelta(minutes=80)
            ):
                break
        except Exception as exc:  # noqa: BLE001 - preserve the run log before returning failure
            failures += 1
            _append_event(event_log, {"kind": "cycle_error", "at": now.isoformat(), "error": str(exc)})
            if failures >= 3:
                break

    final_audit = run_forward_demo_audit(data_root, send_telegram=args.telegram, now=datetime.now(tz=UTC))
    summary = {
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "failures": failures,
        "demo_execution_resolved": _demo_execution_resolved(final_audit),
        "final_audit": _compact(final_audit),
    }
    (data_root / "hourly_full_system_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _append_event(event_log, {"kind": "done", **summary})
    if args.telegram:
        audit_summary = final_audit.get("summary", {})
        send_telegram_message(
            "MODEL050426 hourly full-system DEMO TEST finished "
            f"closed={audit_summary.get('paper_closed_trades')}/{audit_summary.get('paper_trades')} "
            f"demo_pnl={audit_summary.get('demo_realized_pnl_usdt')} root={data_root}",
            enabled=True,
        )
    return 1 if failures else 0


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    compacted = {
        "now": payload.get("now"),
        "rows": payload.get("rows"),
        "summary": payload.get("summary"),
        "telegram": payload.get("telegram"),
    }
    if payload.get("results"):
        compacted["results"] = [
            {
                key: row.get(key)
                for key in (
                    "sleeve",
                    "status",
                    "candidates",
                    "new_trades",
                    "open_trades",
                    "closed_trades",
                    "data_root",
                    "error",
                )
                if key in row
            }
            for row in payload.get("results", [])
            if isinstance(row, dict)
        ]
    if payload.get("sleeves"):
        compacted["sleeves"] = [_compact_sleeve(row) for row in payload.get("sleeves", []) if isinstance(row, dict)]
    return {key: value for key, value in compacted.items() if value not in (None, [], {})}


def _compact_sleeve(row: dict[str, Any]) -> dict[str, Any]:
    compacted = {
        "sleeve": row.get("sleeve"),
        "status": row.get("status"),
        "data_root": row.get("data_root"),
        "rows": row.get("rows"),
        "summary": row.get("summary"),
        "error": row.get("error"),
    }
    fast = row.get("fast_protection")
    if isinstance(fast, dict) and fast:
        compacted["fast_protection"] = {
            "now": fast.get("now"),
            "reason": fast.get("reason"),
            "symbols": fast.get("symbols"),
            "rows": fast.get("rows"),
            "latency_ms": fast.get("latency_ms"),
            "events": fast.get("events"),
        }
    return {key: value for key, value in compacted.items() if value not in (None, [], {})}


def _all_paper_trades_closed(payload: dict[str, Any]) -> bool:
    summary = payload.get("summary", {})
    paper_trades = int(summary.get("paper_trades") or 0)
    closed = int(summary.get("paper_closed_trades") or 0)
    return paper_trades > 0 and closed >= paper_trades


def _demo_execution_resolved(payload: dict[str, Any]) -> bool:
    summary = payload.get("summary", {})
    entry_fills = int(summary.get("demo_entries_filled") or 0)
    exit_fills = int(summary.get("demo_exits_filled") or 0)
    open_slices = int(summary.get("demo_slices_open") or 0)
    paper_trades = int(summary.get("paper_trades") or 0)
    paper_closed = int(summary.get("paper_closed_trades") or 0)
    if entry_fills <= 0:
        return open_slices == 0
    return paper_trades > 0 and paper_closed >= paper_trades and exit_fills >= entry_fills and open_slices == 0


def _candidate_count(payload: dict[str, Any]) -> int:
    return sum(int((row.get("candidates") or 0)) for row in payload.get("results", []))


def _append_event(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _sleep_to_next_minute(*, delay_seconds: float = 2.0) -> None:
    now = time.time()
    target = (int(now // 60) + 1) * 60.0 + max(delay_seconds, 0.0)
    time.sleep(max(target - now, 0.0))


def _signal_datetime(now: datetime, signal_minute: int) -> datetime:
    signal = now.replace(hour=signal_minute // 60, minute=signal_minute % 60, second=0, microsecond=0)
    if signal < now - timedelta(minutes=5):
        signal += timedelta(days=1)
    return signal


def _signal_minute(value: str) -> int:
    hour_text, minute_text = value.split(":", maxsplit=1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError(f"invalid signal time: {value}")
    return hour * 60 + minute


if __name__ == "__main__":
    raise SystemExit(main())
