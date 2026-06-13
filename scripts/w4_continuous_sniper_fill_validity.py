#!/usr/bin/env python3
"""W4 Stage 2 sniper fill-validity harness for the frozen continuous book."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402
from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _pit_partition_gate  # noqa: E402


SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
STAGE1_TAG = "w4_continuous_stage1_stop_exit_2026-06-13"
SWEEP_TAG = "w4_continuous_stage2_sniper_fill_2026-06-13"
COMPONENTS = ("turn3p3", "turn4p3", "turn4p5", "age210tp14")
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
SNIPER_WICK_PCT = 0.08
SNIPER_SIZE_FRAC = 0.25
SNIPER_STOP_PCT = 0.25


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w4_continuous_sniper_fill_validity.py",
        REPO / "scripts" / "w4_continuous_stop_exit_realism.py",
        REPO / "liquidity_migration" / "continuous_demo.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dates(lo_ms: int, hi_ms: int) -> list[str]:
    d0 = dt.datetime.fromtimestamp(lo_ms / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(hi_ms / 1000, tz=dt.timezone.utc).date()
    out: list[str] = []
    day = d0
    while day <= d1:
        out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


@lru_cache(maxsize=300_000)
def _bars(venue: str, symbol: str, date: str) -> tuple[tuple[int, float, float], ...]:
    part = ROOTS[venue] / "klines_1h" / f"date={date}" / f"symbol={symbol}"
    if not part.exists():
        return ()
    df = pl.read_parquet(part, columns=["ts_ms", "high", "low"]).sort("ts_ms")
    return tuple((int(ts), float(high), float(low)) for ts, high, low in df.iter_rows())


@lru_cache(maxsize=300_000)
def _funding_events(venue: str, symbol: str, date: str) -> tuple[tuple[int, float], ...]:
    dataset = "funding" if venue == "bybit" else "binance_usdm_funding"
    part = ROOTS[venue] / dataset / f"date={date}" / f"symbol={symbol}"
    if not part.exists():
        return ()
    df = pl.read_parquet(part, columns=["ts_ms", "funding_rate"]).unique(["ts_ms"]).sort("ts_ms")
    return tuple((int(ts), float(rate)) for ts, rate in df.iter_rows())


def _base_trade_paths(root: Path) -> dict[str, Path]:
    return {
        component: root / "reports" / STAGE1_TAG / "00_frozen_no_stop" / component / "continuous_trades.csv"
        for component in COMPONENTS
    }


def _load_base_trades(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for component, path in _base_trade_paths(root).items():
        if not path.exists():
            raise FileNotFoundError(f"missing Stage 1 control trades: {path}")
        for row in pl.read_csv(path).to_dicts():
            row["component"] = component
            rows.append(row)
    return rows


def _sum_funding(venue: str, symbol: str, lo_ms: int, hi_ms: int) -> float:
    total = 0.0
    for date in _dates(lo_ms, hi_ms):
        for ts_ms, rate in _funding_events(venue, symbol, date):
            if lo_ms < ts_ms <= hi_ms:
                total += rate
    return total


def _simulate_sniper(venue: str, trade: dict[str, Any]) -> dict[str, Any]:
    symbol = str(trade["symbol"])
    entry_ts = int(trade["entry_ts_ms"])
    exit_ts = int(trade["exit_ts_ms"])
    entry_price = float(trade["entry_price"])
    base_exit_price = float(trade["exit_price"])
    notional_weight = abs(float(trade["notional_weight"]))
    cost_per_notional = float(trade["cost_return"]) / max(notional_weight, 1e-12)
    limit_price = entry_price * (1.0 + SNIPER_WICK_PCT)
    stop_price = limit_price * (1.0 + SNIPER_STOP_PCT)

    bars: list[tuple[int, float, float]] = []
    for date in _dates(entry_ts, exit_ts):
        bars.extend(_bars(venue, symbol, date))
    bars.sort(key=lambda item: item[0])

    fill_bar_ts: int | None = None
    for ts_ms, high, _low in bars:
        if ts_ms <= entry_ts or ts_ms >= exit_ts:
            continue
        if high >= limit_price:
            fill_bar_ts = ts_ms
            break

    out: dict[str, Any] = {
        "venue": venue,
        "component": trade["component"],
        "trade_id": trade["trade_id"],
        "symbol": symbol,
        "entry_ts_ms": entry_ts,
        "base_exit_ts_ms": exit_ts,
        "entry_price": entry_price,
        "base_exit_price": base_exit_price,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "notional_weight": notional_weight,
        "sniper_size_frac": SNIPER_SIZE_FRAC,
        "eligible": True,
        "filled": False,
        "fill_ts_ms": None,
        "sniper_exit_ts_ms": None,
        "sniper_exit_price": None,
        "sniper_exit_reason": "no_touch",
        "fill_delay_hours": None,
        "gross_return": 0.0,
        "funding_return_per_notional": 0.0,
        "cost_return_per_notional": cost_per_notional,
        "net_return_per_notional": 0.0,
        "sniper_return": 0.0,
        "max_adverse_pct": None,
        "max_favorable_pct": None,
    }
    if fill_bar_ts is None:
        return out

    fill_ts = fill_bar_ts + MS_PER_HOUR
    if fill_ts >= exit_ts:
        out["sniper_exit_reason"] = "touch_too_late"
        out["fill_ts_ms"] = fill_ts
        out["fill_delay_hours"] = (fill_ts - entry_ts) / MS_PER_HOUR
        return out

    exit_used_ts = exit_ts
    exit_used_price = base_exit_price
    exit_reason = "base_exit"
    path_highs: list[float] = []
    path_lows: list[float] = []
    for ts_ms, high, low in bars:
        if ts_ms < fill_ts or ts_ms >= exit_ts:
            continue
        path_highs.append(high)
        path_lows.append(low)
        if high >= stop_price:
            exit_used_ts = ts_ms + MS_PER_HOUR
            exit_used_price = stop_price
            exit_reason = "stop_loss"
            break

    gross = (limit_price - exit_used_price) / limit_price
    funding = _sum_funding(venue, symbol, fill_ts, exit_used_ts)
    net_per_notional = gross + funding + cost_per_notional
    sniper_return = SNIPER_SIZE_FRAC * notional_weight * net_per_notional
    out.update(
        {
            "filled": True,
            "fill_ts_ms": fill_ts,
            "sniper_exit_ts_ms": exit_used_ts,
            "sniper_exit_price": exit_used_price,
            "sniper_exit_reason": exit_reason,
            "fill_delay_hours": (fill_ts - entry_ts) / MS_PER_HOUR,
            "gross_return": gross,
            "funding_return_per_notional": funding,
            "net_return_per_notional": net_per_notional,
            "sniper_return": sniper_return,
            "max_adverse_pct": (max(path_highs) / limit_price - 1.0) if path_highs else None,
            "max_favorable_pct": ((limit_price - min(path_lows)) / limit_price) if path_lows else None,
        }
    )
    return out


def _ledger_with_equity(frame: pl.DataFrame) -> pl.DataFrame:
    rows = []
    equity = 1.0
    peak = 1.0
    for row in frame.sort("ts_ms").to_dicts():
        ret = float(row["basket_return"])
        equity *= 1.0 + ret
        peak = max(peak, equity)
        rows.append({**row, "equity": equity, "drawdown": equity / peak - 1.0})
    return pl.DataFrame(rows, infer_schema_length=None)


def _monthly_returns(ledger: pl.DataFrame) -> pl.DataFrame:
    if ledger.is_empty():
        return pl.DataFrame({"month": [], "strategy_return": []})
    return (
        ledger.select("ts_ms", "basket_return")
        .with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"))
        .sort("month")
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _arm_report(
    *,
    root: Path,
    venue: str,
    arm: str,
    ledger: pl.DataFrame,
    metrics: dict[str, Any],
    pit: dict[str, Any],
    extra: dict[str, Any],
) -> None:
    arm_dir = root / "reports" / SWEEP_TAG / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    _monthly_returns(ledger).write_csv(arm_dir / "volume_event_best_monthly.csv")
    report = {
        "preregistration": "docs/preregistration/2026-06-13-w4-continuous-stage2-sniper-fill-validity.md",
        "run_label": "exploratory",
        "date_range": {"start": extra["window"]["start"], "end": extra["window"]["end_exclusive"]},
        "best_scenario": {
            "total_return": metrics.get("total_return", 0.0),
            "max_drawdown": metrics.get("max_drawdown", 0.0),
            "sharpe_like": metrics.get("sharpe_like", 0.0),
            "trades": extra.get("eligible_orders", 0),
        },
        "pit_manifest": {"full_pit_universe_pass": bool(pit["full_pit_universe_pass"]), **pit},
        "data_root": str(root),
        "venue": venue,
        "arm": arm,
        **extra,
    }
    (arm_dir / "volume_event_research_report.json").write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = len(events)
    filled = [row for row in events if row["filled"]]
    stop_rows = [row for row in filled if row["sniper_exit_reason"] == "stop_loss"]
    non_stop = [row for row in filled if row["sniper_exit_reason"] != "stop_loss"]
    total_delta = sum(float(row["sniper_return"]) for row in filled)
    filled_net = [float(row["net_return_per_notional"]) for row in filled]
    stop_loss_sum = -sum(min(float(row["sniper_return"]), 0.0) for row in stop_rows)
    non_stop_profit = sum(max(float(row["sniper_return"]), 0.0) for row in non_stop)
    return {
        "eligible_orders": eligible,
        "filled_orders": len(filled),
        "fill_rate": len(filled) / max(eligible, 1),
        "stop_hits": len(stop_rows),
        "stop_hit_rate_filled": len(stop_rows) / max(len(filled), 1),
        "avg_net_bps_per_filled": (
            sum(filled_net) / len(filled_net) * 10_000.0 if filled_net else 0.0
        ),
        "total_sniper_delta_return": total_delta,
        "bps_per_eligible_order": total_delta / max(eligible, 1) * 10_000.0,
        "stop_loss_return_loss": stop_loss_sum,
        "non_stop_return_profit": non_stop_profit,
        "stop_loss_exceeds_non_stop_profit": stop_loss_sum > non_stop_profit,
    }


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W4 Continuous Stage 2 Sniper Fill Validity",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        "",
        "## Summary",
        "",
        "| Venue | Eligible | Filled | Fill Rate | Avg Filled Bps | Delta Return | Control MAR | Sniper MAR | MAR Delta | Stop Hit Rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["summary_rows"]:
        lines.append(
            f"| `{row['venue']}` | {row['eligible_orders']} | {row['filled_orders']} | "
            f"{row['fill_rate']:.2%} | {row['avg_net_bps_per_filled']:.2f} | "
            f"{row['total_sniper_delta_return']:.4f} | {row['control_mar']:.4f} | "
            f"{row['sniper_mar']:.4f} | {row['delta_mar_vs_control']:.4f} | "
            f"{row['stop_hit_rate_filled']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## Registered Falsifier",
            "",
            payload["registered_falsifier"],
            "",
            "This stage is historical mechanism evidence only. It is not forward-fill, paper-ready, or real-money evidence.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": "docs/preregistration/2026-06-13-w4-continuous-stage2-sniper-fill-validity.md",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out),
        "sweep_tag": SWEEP_TAG,
        "stage1_control_tag": STAGE1_TAG,
        "git_head": _git_head(),
        "code_hash": _code_hash(),
        "frozen_forward_config_hash": frozen_config_hash(),
        "sniper_config": {
            "wick_pct": SNIPER_WICK_PCT,
            "size_frac": SNIPER_SIZE_FRAC,
            "stop_pct": SNIPER_STOP_PCT,
            "fill_rule": "first hourly high >= limit strictly after base entry; live at end of touch bar",
            "exit_rule": "25% add-on stop else base lifecycle exit",
            "cost_rule": "base trade per-notional round-trip cost; no maker rebate credited",
        },
        "roots": {venue: str(ROOTS[venue]) for venue in venues},
        "pit_partition_gate": {},
        "summary_rows": [],
        "registered_falsifier": (
            "Reject this exact fixed sniper if either venue has non-positive average filled-add-on bps, "
            "adjusted return non-positive, R1 pooled MAR delta <= 0, any venue MAR delta <= -0.50, "
            "or stop-hit losses exceed non-stop profits. Fill rate below 5% is insufficient historical "
            "fill evidence, not alpha-positive evidence."
        ),
    }

    all_events: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        pit = _pit_partition_gate(root, start=args.start, end=args.end)
        payload["pit_partition_gate"][venue] = pit
        base_trades = _load_base_trades(root)
        events = [_simulate_sniper(venue, trade) for trade in base_trades]
        all_events.extend(events)
        venue_events_path = out / f"{venue}_sniper_events.csv"
        _write_csv(venue_events_path, events)

        base_ledger_path = root / "reports" / STAGE1_TAG / "00_frozen_no_stop" / "ensemble_hedged_ledger.csv"
        base_ledger = pl.read_csv(base_ledger_path).sort("ts_ms")
        control_ledger = _ledger_with_equity(base_ledger.with_columns(pl.lit(0.0).alias("sniper_return")))
        control_metrics = _daily_pnl_metrics(
            control_ledger.select("ts_ms", "equity", "drawdown", "basket_return")
        )
        delta_by_day: dict[int, float] = {}
        for row in events:
            if not row["filled"] or row["sniper_exit_ts_ms"] is None:
                continue
            day = (int(row["sniper_exit_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
            delta_by_day[day] = delta_by_day.get(day, 0.0) + float(row["sniper_return"])
        delta_frame = pl.DataFrame(
            {
                "ts_ms": list(delta_by_day.keys()),
                "sniper_return": list(delta_by_day.values()),
            },
            schema={"ts_ms": pl.Int64, "sniper_return": pl.Float64},
        )
        adjusted = base_ledger.join(delta_frame, on="ts_ms", how="left").with_columns(
            pl.col("sniper_return").fill_null(0.0)
        ).with_columns(
            (pl.col("basket_return") + pl.col("sniper_return")).alias("basket_return")
        )
        adjusted = _ledger_with_equity(adjusted)
        sniper_metrics = _daily_pnl_metrics(
            adjusted.select("ts_ms", "equity", "drawdown", "basket_return")
        )
        stats = _summarize_events(events)
        summary = {
            "venue": venue,
            "data_root": str(root),
            "event_rows": str(venue_events_path),
            "full_pit_universe_pass": bool(pit["full_pit_universe_pass"]),
            **stats,
            "control_total_return": float(control_metrics["total_return"]),
            "sniper_total_return": float(sniper_metrics["total_return"]),
            "delta_total_return_vs_control": float(sniper_metrics["total_return"] - control_metrics["total_return"]),
            "control_mar": float(control_metrics["mar"] or 0.0),
            "sniper_mar": float(sniper_metrics["mar"] or 0.0),
            "delta_mar_vs_control": float((sniper_metrics["mar"] or 0.0) - (control_metrics["mar"] or 0.0)),
            "control_max_drawdown": float(control_metrics["max_drawdown"]),
            "sniper_max_drawdown": float(sniper_metrics["max_drawdown"]),
            "delta_max_drawdown_vs_control": float(sniper_metrics["max_drawdown"] - control_metrics["max_drawdown"]),
        }
        payload["summary_rows"].append(summary)

        common_extra = {
            "window": payload["window"],
            "eligible_orders": stats["eligible_orders"],
            "code_hash": payload["code_hash"],
            "frozen_forward_config_hash": payload["frozen_forward_config_hash"],
            "sniper_config": payload["sniper_config"],
        }
        _arm_report(
            root=root,
            venue=venue,
            arm="00_control",
            ledger=control_ledger,
            metrics=control_metrics,
            pit=pit,
            extra={**common_extra, "sniper_enabled": False},
        )
        _arm_report(
            root=root,
            venue=venue,
            arm="01_fixed_x8_b25_base_exit",
            ledger=adjusted,
            metrics=sniper_metrics,
            pit=pit,
            extra={**common_extra, "sniper_enabled": True, "summary": summary},
        )

    _write_csv(out / "stage2_summary.csv", payload["summary_rows"])
    _write_csv(out / "stage2_sniper_events.csv", all_events)
    (out / "stage2_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage2_summary.md")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--out", default=str(SHARED / "w4_continuous_stage2_sniper_fill_2026-06-13"))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "summary_rows": payload["summary_rows"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
