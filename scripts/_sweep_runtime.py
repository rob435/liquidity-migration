"""Shared parallel-sweep runtime for the multi-phase research program.

Used by the Round-2 sweep dispatchers ``scripts/r1_filter_audit_sweep.py`` and
``scripts/r13_exit_rule_sweep.py``; future phase orchestrators (R3 bearish
stack, R9 assembly, etc.) import the same primitives:

  Cell                — (cell_id, description, overrides)
  run_cell()          — invoke `python -m liquidity_migration volume-events`
                        for one (cell, venue), parse the report JSON,
                        return a metrics dict
  run_sweep()         — ThreadPoolExecutor dispatch + locked summary.csv
                        write + atomic-print stdout serialisation

The runtime honours two env vars set by the operator (matching the
research-phase-runner skill):
  SWEEP_MAX_WORKERS    cells run in parallel (default 8)
  POLARS_MAX_THREADS   threads per cell's polars/rayon runtime (default 4)

8 × 4 = 32 = full 5950X SMT occupancy.
"""
from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Mapping


def _compute_window_days(start_date: str, end_date: str) -> int:
    """Days between ``start_date`` (inclusive) and ``end_date`` (exclusive),
    matching the volume-events backtest's end-exclusive window convention.

    Both dates are ``YYYY-MM-DD``. Returns a non-negative int.
    """
    s = datetime.strptime(start_date, "%Y-%m-%d").date() if not isinstance(start_date, date) else start_date
    e = datetime.strptime(end_date, "%Y-%m-%d").date() if not isinstance(end_date, date) else end_date
    return max(0, (e - s).days)

REPO = Path(__file__).resolve().parent.parent
SHARED = Path.home() / "SHARED_DATA"


# Force line-buffered + UTF-8 stdout so atomic_print events surface in
# real-time when the orchestrator's stdout is captured to a file (default
# Windows cp1252 + block-buffered pipe both bite us; observed wedging the
# Phase 0 dispatch even though cells were actually completing). Belt and
# suspenders — invoking with `python -u` is the other half of this.
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    # Pre-3.7 Python or already-detached streams; falls back to default.
    pass

MAX_WORKERS = max(1, int(os.environ.get("SWEEP_MAX_WORKERS", "8")))
PER_CELL_POLARS_THREADS = max(1, int(os.environ.get("POLARS_MAX_THREADS", "4")))


@dataclass
class Cell:
    cell_id: str
    description: str
    overrides: dict[str, str] = field(default_factory=dict)
    # Optional per-cell data-root override. When set, this cell runs against
    # the given data root regardless of the orchestrator's per-venue default.
    # Used by Phase 1 to route 474-archive-only cells to the side-copy at
    # ~/SHARED_DATA/bybit_full_pit_archive_only while 764 cells stay on the
    # full root. Stays None for Phase 0 / sweep_cells / future single-root
    # phases.
    data_root_override: Path | None = None


_PRINT_LOCK = threading.Lock()


def _atomic_print(*lines: str) -> None:
    with _PRINT_LOCK:
        for line in lines:
            print(line)
        sys.stdout.flush()


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["POLARS_MAX_THREADS"] = str(PER_CELL_POLARS_THREADS)
    env["RAYON_NUM_THREADS"] = str(PER_CELL_POLARS_THREADS)
    return env


def run_cell(
    cell: Cell,
    venue: str,
    data_root: Path,
    *,
    baseline_params: Mapping[str, str],
    start_date: str,
    end_date: str,
    sweep_tag: str,
    config_path: str = "configs/volume_alpha.default.yaml",
) -> dict[str, str]:
    """Run one cell on one venue, return per-cell metrics dict.

    Shells out to ``python -m liquidity_migration volume-events`` with the
    baseline params + cell overrides + the standard window / report-dir
    flags. Parses the resulting ``volume_event_research_report.json`` for
    headline metrics; on failure, returns a row with status='failed' so
    the orchestrator can record what went wrong.
    """
    report_dir = data_root / "reports" / sweep_tag / cell.cell_id
    report_dir.mkdir(parents=True, exist_ok=True)
    params = dict(baseline_params)
    params.update(cell.overrides)
    cmd = [
        sys.executable, "-m", "liquidity_migration",
        "--data-root", str(data_root),
        "--config", config_path,
        "volume-events",
        "--start", start_date,
        "--end", end_date,
        "--allow-partial-pit",
        "--report-dir", str(report_dir),
    ]
    for k, v in params.items():
        cmd.extend([k, v])

    start = time.monotonic()
    _atomic_print(f"  [{venue}/{cell.cell_id}] START  {cell.description}  ->  {report_dir}")
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=_subprocess_env())
    elapsed = time.monotonic() - start
    window_days = _compute_window_days(start_date, end_date)
    if proc.returncode != 0:
        _atomic_print(
            f"  [{venue}/{cell.cell_id}] FAILED (exit={proc.returncode}, {elapsed:.1f}s)",
            f"    stderr (last 500): {proc.stderr[-500:]}",
        )
        return {
            "venue": venue,
            "cell_id": cell.cell_id,
            "description": cell.description,
            "status": "failed",
            "elapsed_seconds": f"{elapsed:.1f}",
            "start_date": start_date,
            "end_date": end_date,
            "window_days": str(window_days),
            "error": proc.stderr[-500:].replace("\n", " | "),
        }

    report_json = report_dir / "volume_event_research_report.json"
    if not report_json.exists():
        _atomic_print(f"  [{venue}/{cell.cell_id}] NO_REPORT ({elapsed:.1f}s) — expected {report_json}")
        return {
            "venue": venue, "cell_id": cell.cell_id, "description": cell.description,
            "status": "no_report", "elapsed_seconds": f"{elapsed:.1f}",
            "start_date": start_date, "end_date": end_date,
            "window_days": str(window_days),
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
        "start_date": start_date,
        "end_date": end_date,
        "window_days": str(window_days),
    }
    _atomic_print(
        f"  [{venue}/{cell.cell_id}] OK ({elapsed:.1f}s)  "
        f"trades={row['trades']}  ret={row['total_return']}  dd={row['max_drawdown']}  sharpe={row['sharpe_like']}"
    )
    return row


def _write_summary(summary_path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(summary_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def run_sweep(
    cells: list[Cell],
    venues: Mapping[str, Path],
    *,
    baseline_params: Mapping[str, str],
    start_date: str,
    end_date: str,
    sweep_tag: str,
    summary_path: Path,
    config_path: str = "configs/volume_alpha.default.yaml",
) -> int:
    """Dispatch (cell × venue) work to ThreadPoolExecutor; flush summary.csv
    after every completion under a lock. Returns 0 on completion."""
    work: list[tuple[Cell, str, Path]] = []
    for venue, data_root in venues.items():
        if not data_root.exists():
            print(f"SKIP venue={venue}: data root not found at {data_root}")
            continue
        for cell in cells:
            # Honour per-cell data_root_override so Phase 1 can route a
            # mix of 474-archive-only and 764-full cells through the same
            # orchestrator pass.
            effective_root = cell.data_root_override if cell.data_root_override is not None else data_root
            if not effective_root.exists():
                print(f"SKIP cell={cell.cell_id} venue={venue}: data root not found at {effective_root}")
                continue
            work.append((cell, venue, effective_root))

    print(f"sweep summary -> {summary_path}")
    print(f"window: {start_date} -> {end_date}")
    print(f"sweep tag: {sweep_tag}")
    print(
        f"cells: {len(cells)}  venues: {len(venues)}  total runs: {len(work)}  "
        f"parallel: SWEEP_MAX_WORKERS={MAX_WORKERS}  POLARS_MAX_THREADS={PER_CELL_POLARS_THREADS}  "
        f"(thread budget = {MAX_WORKERS * PER_CELL_POLARS_THREADS})"
    )
    print()

    rows: list[dict[str, str]] = []
    rows_lock = threading.Lock()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    sweep_start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(
                run_cell,
                cell,
                venue,
                data_root,
                baseline_params=baseline_params,
                start_date=start_date,
                end_date=end_date,
                sweep_tag=sweep_tag,
                config_path=config_path,
            ): (cell, venue)
            for cell, venue, data_root in work
        }
        for fut in concurrent.futures.as_completed(futures):
            cell, venue = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {
                    "venue": venue,
                    "cell_id": cell.cell_id,
                    "description": cell.description,
                    "status": "exception",
                    "elapsed_seconds": "0.0",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                _atomic_print(f"  [{venue}/{cell.cell_id}] EXCEPTION: {type(exc).__name__}: {exc}")
            with rows_lock:
                rows.append(row)
                _write_summary(summary_path, rows)

    elapsed_total = time.monotonic() - sweep_start
    print(f"\nDONE. wrote {len(rows)} rows to {summary_path} in {elapsed_total/60:.1f} min")
    return 0
