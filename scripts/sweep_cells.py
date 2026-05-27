"""15-cell × 2-venue parameter sweep dispatcher.

Pre-reg: docs/preregistration/2026-05-28-liquidity-capacity-filter-and-filter-tweak-sweep.md

For each (cell, venue) combination, shells out to the `volume-events` CLI with
the cell's parameter overrides, parses the resulting
volume_event_research_report.json for headline metrics, and writes a single
aggregate sweep_summary.csv.

EXPLORATORY label: not promotion evidence. Decision rule (a priori): a cell
qualifies only if Sharpe Δ ≥ +0.5 vs baseline on BOTH venues AND max-DD Δ ≤
+5pp on both. See pre-reg doc for the full rule.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARED = Path.home() / "SHARED_DATA"
SWEEP_TAG = "sweep_2026-05-28"

# Window: 2025-01-01 → 2026-05-28 (~17 months). Trimmed from the 2023-26
# in-sample window because the sweep is exploratory + computationally
# bounded — each cell takes ~3-5 min on this window vs ~10 min on
# 2024-01-01+. Sample size of ~300+ trades is still enough to rank cells
# by Sharpe / DD. Conclusions here are EXPLORATORY only; promotion would
# need a full-window re-run.
START_DATE = "2025-01-01"
END_DATE = "2026-05-28"

VENUES = {
    "bybit":   SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}


@dataclass
class Cell:
    cell_id: str
    description: str
    overrides: dict[str, str] = field(default_factory=dict)


# Baseline = current production promoted profile (matches deploy/systemd
# bybit-demo + scripts/run_fullpit_volume_overnight.sh canonical cell)
BASELINE_PARAMS: dict[str, str] = {
    "--event-types": "liquidity_migration",
    "--thresholds": "0.4",
    "--hold-days": "3",
    "--sides": "reversal",
    "--stop-loss-pcts": "0.12",
    "--take-profit-pcts": "0.26",
    "--cost-multipliers": "3",
    "--gross-exposure": "1.0",
    "--entry-delay-hours": "1",
    "--entry-policy": "promoted_quality_squeeze",
    "--max-active-symbols": "3",
    "--cooldown-days": "5",
    "--rank-exit-threshold": "0.55",
    "--universe-rank-min": "31",
    "--universe-rank-max": "400",
    "--liquidity-migration-rank-improvement-min": "150",
    "--liquidity-migration-turnover-ratio-min": "6.0",
    "--liquidity-migration-event-rank-fraction-max": "0.90",
    "--liquidity-migration-day-return-min": "0.0",
    "--liquidity-migration-residual-return-min": "0.08",
    "--liquidity-migration-close-location-min": "0.30",
    "--liquidity-migration-pit-age-days-min": "90",
    "--liquidity-migration-crowding-filter": "union_pathology",
    "--stop-pressure-window-days": "10",
    "--stop-pressure-stop-count": "7",
    "--realized-loss-pressure-window-days": "5",
    "--realized-loss-pressure-loss-count": "6",
}


CELLS: list[Cell] = [
    Cell("00_baseline", "current promoted defaults (control)"),
    # Group A — liquidity capacity (the operator's hypothesis)
    # REQ on 2026-05-27 had turnover ~$5.7M; $5M floor barely keeps REQ in;
    # $10M floor excludes REQ-class names. $50M is the "majors only" extreme.
    Cell("A2_turnover_5M",  "min turnover $5M/day",   {"--universe-min-daily-turnover": "5000000"}),
    Cell("A3_turnover_10M", "min turnover $10M/day",  {"--universe-min-daily-turnover": "10000000"}),
    Cell("A4_turnover_50M", "min turnover $50M/day",  {"--universe-min-daily-turnover": "50000000"}),
    # Group B — rank-improvement tightening
    Cell("B1_rankimp_200", "rank_improvement_min 200", {"--liquidity-migration-rank-improvement-min": "200"}),
    # Group C — residual-return tightening
    Cell("C1_residret_12", "residual_return_min 0.12", {"--liquidity-migration-residual-return-min": "0.12"}),
    # Group D — hold period (confirm prior 2026-05-23 finding)
    Cell("D1_hold2", "hold_days 2", {"--hold-days": "2"}),
    # Group E — universe-rank tightening
    Cell("E1_rankmax_200", "universe_rank_max 200", {"--universe-rank-max": "200"}),
    # Group F — combos: best individual filter ideas stacked
    Cell("F1_turnover10M_hold2", "$10M + h=2",
         {"--universe-min-daily-turnover": "10000000", "--hold-days": "2"}),
    Cell("F3_turnover10M_hold2_residret12", "$10M + h=2 + resid 0.12",
         {"--universe-min-daily-turnover": "10000000", "--hold-days": "2",
          "--liquidity-migration-residual-return-min": "0.12"}),
]


def run_cell(cell: Cell, venue: str, data_root: Path) -> dict[str, str]:
    """Run one cell on one venue, return per-cell metrics dict."""
    report_dir = data_root / "reports" / SWEEP_TAG / cell.cell_id
    report_dir.mkdir(parents=True, exist_ok=True)
    params = dict(BASELINE_PARAMS)
    params.update(cell.overrides)
    cmd = [
        ".venv/bin/python", "-m", "liquidity_migration",
        "--data-root", str(data_root),
        "--config", "configs/volume_alpha.default.yaml",
        "volume-events",
        "--start", START_DATE,
        "--end", END_DATE,
        "--allow-partial-pit",  # pre-existing 2021 manifest gap
        "--report-dir", str(report_dir),
    ]
    for k, v in params.items():
        cmd.extend([k, v])

    start = time.monotonic()
    print(f"  [{venue}/{cell.cell_id}] {cell.description}  →  {report_dir}")
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        print(f"    FAILED (exit={proc.returncode}, {elapsed:.1f}s)")
        print(f"    stderr (last 500): {proc.stderr[-500:]}")
        return {
            "venue": venue,
            "cell_id": cell.cell_id,
            "description": cell.description,
            "status": "failed",
            "elapsed_seconds": f"{elapsed:.1f}",
            "error": proc.stderr[-500:].replace("\n", " | "),
        }

    # Parse the report JSON for headline metrics. Use best-scenario fields.
    report_json = report_dir / "volume_event_research_report.json"
    if not report_json.exists():
        return {
            "venue": venue, "cell_id": cell.cell_id, "description": cell.description,
            "status": "no_report", "elapsed_seconds": f"{elapsed:.1f}",
        }
    payload = json.loads(report_json.read_text())
    best = payload.get("best_scenario", {})
    row = {
        "venue": venue,
        "cell_id": cell.cell_id,
        "description": cell.description,
        "status": "ok",
        "elapsed_seconds": f"{elapsed:.1f}",
        "trades": str(best.get("trades", 0)),
        "total_return": f"{best.get('total_return', 0.0):.4f}",
        "max_drawdown": f"{best.get('max_drawdown', 0.0):.4f}",
        "avg_split_sharpe": f"{best.get('avg_split_sharpe', 0.0):.4f}",
        "sharpe_like": f"{best.get('sharpe_like', best.get('sharpe', 0.0)):.4f}",
        "promotable": str(best.get("promote", False)),
        "worst_90d": f"{best.get('worst_90d_return', 0.0):.4f}",
        "report_dir": str(report_dir),
    }
    print(f"    OK  ({elapsed:.1f}s)  trades={row['trades']}  ret={row['total_return']}  dd={row['max_drawdown']}  sharpe={row['sharpe_like']}")
    return row


def main() -> int:
    summary_path = SHARED / f"{SWEEP_TAG}_summary.csv"
    print(f"sweep summary → {summary_path}")
    print(f"window: {START_DATE} → {END_DATE}")
    print(f"cells: {len(CELLS)}  venues: {len(VENUES)}  total runs: {len(CELLS) * len(VENUES)}")
    print()

    rows: list[dict[str, str]] = []
    for venue, data_root in VENUES.items():
        if not data_root.exists():
            print(f"SKIP venue={venue}: data root not found at {data_root}")
            continue
        print(f"=== Venue: {venue} ({data_root}) ===")
        for cell in CELLS:
            row = run_cell(cell, venue, data_root)
            rows.append(row)
            # Flush partial summary after every cell so progress survives interruption
            if rows:
                fieldnames = sorted({k for r in rows for k in r.keys()})
                with open(summary_path, "w", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fieldnames)
                    writer.writeheader()
                    for r in rows:
                        writer.writerow(r)

    print(f"\nDONE. wrote {len(rows)} rows to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
