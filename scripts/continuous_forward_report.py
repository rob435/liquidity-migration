#!/usr/bin/env python3
"""Run the continuous-vs-daily forward gate and optionally send a Telegram receipt."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.reconciliation import run_continuous_vs_daily_forward_comparison  # noqa: E402
from liquidity_migration.telegram import send_telegram_message  # noqa: E402

MS_PER_DAY = 86_400_000


def _fmt(value: Any, *, pct: bool = False) -> str:
    if value is None:
        return "NA"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{100.0 * x:.2f}%" if pct else f"{x:.2f}"


def _coverage_gap_days(summary: dict[str, Any]) -> int:
    daily = int(summary.get("latest_daily_day_ts") or 0)
    continuous = int(summary.get("latest_continuous_day_ts") or 0)
    if daily <= 0 or continuous <= 0:
        return 0
    return abs(daily - continuous) // MS_PER_DAY


def _lagging_side(summary: dict[str, Any]) -> str:
    daily = int(summary.get("latest_daily_day_ts") or 0)
    continuous = int(summary.get("latest_continuous_day_ts") or 0)
    if daily <= 0 or continuous <= 0 or daily == continuous:
        return ""
    return "daily" if daily < continuous else "continuous"


def _continuous_age_days(summary: dict[str, Any]) -> int:
    """Days the CONTINUOUS ledger lags behind today (UTC) — the real staleness signal."""
    latest = int(summary.get("latest_continuous_day_ts") or 0)
    if latest <= 0:
        return 0
    from datetime import datetime, timezone

    today = (int(datetime.now(timezone.utc).timestamp() * 1000) // MS_PER_DAY) * MS_PER_DAY
    return max(today - latest, 0) // MS_PER_DAY


def _status(payload: dict[str, Any], *, stale_coverage_gap_days: int = 2) -> str:
    """Continuous-first status (2026-06-11 operator decision: no dependence on the
    short sleeve). STALE = OUR continuous ledger stopped ticking; the daily leg
    lagging or being off is CONTINUOUS-ONLY, never an alarm."""
    summary = payload.get("summary", {})
    common_days = int(summary.get("common_days") or 0)
    min_common_days = int(summary.get("min_common_days") or 30)
    if _continuous_age_days(summary) >= max(int(stale_coverage_gap_days), 1) + 1:
        return "STALE"
    daily_dead = (
        int(summary.get("daily_observed_days") or 0) == 0
        or _coverage_gap_days(summary) >= max(int(stale_coverage_gap_days), 1)
    )
    if daily_dead:
        return "CONTINUOUS-ONLY"
    if common_days < min_common_days:
        return "COLLECTING"
    return "PASS" if payload.get("ok") else "FAIL"


def format_message(payload: dict[str, Any], *, stale_coverage_gap_days: int = 2) -> str:
    summary = payload.get("summary", {})
    daily = payload.get("daily", {})
    continuous = payload.get("continuous", {})
    status = _status(payload, stale_coverage_gap_days=stale_coverage_gap_days)
    gap_days = _coverage_gap_days(summary)
    lagging = _lagging_side(summary)
    lines = [
        f"continuous-vs-daily forward: {status}",
        f"common_days={summary.get('common_days', 0)}/{summary.get('min_common_days', 30)}",
        f"maturity_day_ts={summary.get('maturity_day_ts', 0)}",
        (
            f"coverage daily={summary.get('daily_observed_days', 0)}d "
            f"latest={summary.get('latest_daily_day_ts', 0)}; "
            f"continuous={summary.get('continuous_observed_days', 0)}d "
            f"latest={summary.get('latest_continuous_day_ts', 0)}"
        ),
        f"coverage_gap_days={gap_days} lagging={lagging or 'none'}",
        (
            f"continuous OWN-WINDOW: {summary.get('continuous_full_days', 0)}d "
            f"return={_fmt(summary.get('continuous_full_total_return'), pct=True)} "
            f"MAR={_fmt(summary.get('continuous_full_mar'))}"
        ),
        f"continuous (common window) return={_fmt(continuous.get('total_return'), pct=True)} MAR={_fmt(continuous.get('mar'))}",
        f"daily return={_fmt(daily.get('total_return'), pct=True)} MAR={_fmt(daily.get('mar'))}",
        f"report={payload.get('report_path', '')}",
        f"json={payload.get('json_path', '')}",
    ]
    issues = payload.get("issues") or []
    if issues:
        lines.append("issues: " + "; ".join(str(x) for x in issues[:3]))
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--daily-data-root", default="data/bybit-paper-event")
    p.add_argument("--continuous-data-root", default="data/bybit-continuous-paper-event")
    p.add_argument("--daily-trades-dataset", default="event_demo_trades")
    p.add_argument("--daily-cycles-dataset", default="event_demo_cycles")
    p.add_argument("--continuous-cycles-dataset", default="continuous_fade_paper_cycles")
    p.add_argument("--continuous-trades-dataset", default="continuous_fade_paper_trades")
    p.add_argument("--min-common-days", type=int, default=30)
    p.add_argument(
        "--stale-coverage-gap-days",
        type=int,
        default=2,
        help="Before the common-window floor matures, send Telegram if one ledger lags the other by this many days.",
    )
    p.add_argument("--output-dir", default=None)
    p.add_argument("--telegram", action="store_true")
    p.add_argument(
        "--telegram-before-min-common-days",
        action="store_true",
        help="Send Telegram even while the common forward window is still below --min-common-days.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_continuous_vs_daily_forward_comparison(
        args.daily_data_root,
        args.continuous_data_root,
        daily_trades_dataset=args.daily_trades_dataset,
        daily_cycles_dataset=args.daily_cycles_dataset,
        continuous_cycles_dataset=args.continuous_cycles_dataset,
        continuous_trades_dataset=args.continuous_trades_dataset,
        min_common_days=args.min_common_days,
        output_dir=args.output_dir,
    )
    message = format_message(payload, stale_coverage_gap_days=args.stale_coverage_gap_days)
    print(message)
    status = _status(payload, stale_coverage_gap_days=args.stale_coverage_gap_days)
    sent = False
    quiet = status in ("COLLECTING", "CONTINUOUS-ONLY")  # routine accumulation — no Telegram noise
    if args.telegram and (args.telegram_before_min_common_days or not quiet):
        try:
            sent = send_telegram_message(message, enabled=True)
        except Exception as exc:  # noqa: BLE001 - report must not fail because Telegram is down
            print(f"telegram_send_error={type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"telegram_sent={sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
