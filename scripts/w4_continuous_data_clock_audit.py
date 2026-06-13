#!/usr/bin/env python3
"""W4 Stage 0 data/PIT/forward-clock audit for the continuous book."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from liquidity_migration.data_layer import DataLayerAuditConfig, run_data_layer_audit
from liquidity_migration.pit_coverage import coverage_status
from liquidity_migration.continuous_forward_replay import forward_readiness_summary


SHARED = Path.home() / "SHARED_DATA"
ROOTS = {
    "bybit": SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}
DATASETS = (
    "klines_1h",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    "binance_usdm_klines_1h",
    "binance_usdm_funding",
    "binance_usdm_open_interest",
    "binance_usdm_mark_price_1h",
    "binance_usdm_index_price_1h",
    "binance_usdm_premium_index_1h",
    "binance_usdm_taker_flow_1h",
)


def _iso_ms(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).isoformat()


def _latest_jsonl(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"exists": False, "files": 0, "latest_mtime_utc": None, "latest_path": None}
    files = sorted(root.glob("*/*.jsonl"))
    if not files:
        return {"exists": True, "files": 0, "latest_mtime_utc": None, "latest_path": None}
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return {
        "exists": True,
        "files": len(files),
        "latest_mtime_utc": dt.datetime.fromtimestamp(
            latest.stat().st_mtime, tz=dt.timezone.utc
        ).isoformat(),
        "latest_path": str(latest),
    }


def _read_forward_state(state_dir: Path, venue: str, forward_start: str) -> dict[str, Any]:
    start_ms = int(
        dt.datetime.fromisoformat(forward_start).replace(tzinfo=dt.timezone.utc).timestamp()
        * 1000
    )
    summary = forward_readiness_summary(state_dir, venue, forward_start_ms=start_ms)
    ledger = state_dir / venue / "forward_ledger.csv"
    return {
        "state_dir": str(state_dir),
        "ledger_exists": ledger.exists(),
        "ledger_path": str(ledger),
        "readiness": summary,
    }


def _coverage_dict(root: Path, today: dt.date) -> dict[str, Any]:
    status = coverage_status(root, today=today)
    return {
        "data_root": status.data_root,
        "manifest_end": status.manifest_end.isoformat() if status.manifest_end else None,
        "kline_end": status.kline_end.isoformat() if status.kline_end else None,
        "latest_signal_trading_day": status.latest_signal_trading_day.isoformat(),
        "manifest_margin_days": status.manifest_margin_days,
        "manifest_lag_vs_klines_days": status.manifest_lag_vs_klines_days,
        "manifest_covers_latest_signal": status.manifest_covers_latest_signal,
        "is_stale": status.is_stale,
    }


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W4 Continuous Stage 0 Data / Clock Audit",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Forward start: `{payload['forward_start']}`",
        f"- Output root: `{payload['output_dir']}`",
        "",
        "## Venue Status",
        "",
        "| Venue | Root Exists | Klines End | Manifest End | Manifest Covers Latest Signal | Forward Days | Notes |",
        "|---|---:|---|---|---:|---:|---|",
    ]
    for venue, row in payload["venues"].items():
        cov = row["pit_coverage"]
        fwd = row["forward_state"]["readiness"]
        notes = []
        if not row["root_exists"]:
            notes.append("missing root")
        if cov.get("is_stale"):
            notes.append("PIT stale for latest signal")
        if row.get("data_layer_error"):
            notes.append("data-layer audit error")
        lines.append(
            f"| `{venue}` | {row['root_exists']} | {cov.get('kline_end')} | "
            f"{cov.get('manifest_end')} | {cov.get('manifest_covers_latest_signal')} | "
            f"{fwd.get('forward_days', 0)} | {'; '.join(notes)} |"
        )
    lines.extend(
        [
            "",
            "## Forward Capture Local Roots",
            "",
            f"- Liquidations: `{json.dumps(payload['forward_capture']['liquidations'])}`",
            f"- Depth: `{json.dumps(payload['forward_capture']['depth'])}`",
            "",
            "## Interpretation",
            "",
            "Stage 0 is an operational prerequisite audit. It is not alpha evidence.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--out", default=str(SHARED / "w4_continuous_stage0_data_clock_2026-06-13"))
    parser.add_argument("--forward-state-dir", default=str(SHARED / "continuous_forward_replay"))
    parser.add_argument("--forward-start", default="2026-06-10")
    args = parser.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    venues = [item.strip() for item in args.venues.split(",") if item.strip()]

    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "output_dir": str(out),
        "forward_start": args.forward_start,
        "venues": {},
        "forward_capture": {
            "liquidations": _latest_jsonl(Path("data/liquidations")),
            "depth": _latest_jsonl(Path("data/depth")),
        },
    }
    for venue in venues:
        root = ROOTS[venue]
        venue_out = out / venue
        venue_out.mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "root": str(root),
            "root_exists": root.exists(),
            "pit_coverage": _coverage_dict(root, today),
            "forward_state": _read_forward_state(Path(args.forward_state_dir).expanduser(), venue, args.forward_start),
        }
        try:
            audit = run_data_layer_audit(
                root,
                config=DataLayerAuditConfig(
                    name=f"w4_stage0_{venue}_2026_06_13",
                    datasets=DATASETS,
                    output_dir=venue_out / "data_layer",
                ),
            )
            row["data_layer"] = audit
        except Exception as exc:  # noqa: BLE001 - audit should record errors per venue.
            row["data_layer_error"] = f"{type(exc).__name__}: {exc}"
        payload["venues"][venue] = row

    (out / "stage0_data_clock_audit.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage0_data_clock_audit.md")
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
