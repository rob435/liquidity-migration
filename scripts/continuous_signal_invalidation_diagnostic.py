from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any

import polars as pl


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "research" / "continuous_fade" / "runs" / "continuous_ensemble_v2_baseline_current"
HARNESS_PATH = REPO_ROOT / "research" / "continuous_fade" / "continuous_fade_research.py"


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("continuous_fade_research", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load research harness at {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh continuous signal-invalidation diagnostics.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--skip-state-panel", action="store_true", help="Skip the hourly state-coverage panel")
    parser.add_argument("--no-report", action="store_true", help="Only write signal-invalidation artifacts")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root).expanduser()
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    harness = _load_harness()
    trades = pl.read_parquet(output_root / "tables" / "trades_enriched.parquet")
    candidates = pl.read_parquet(output_root / "tables" / "signal_table.parquet")
    if venues:
        trades = trades.filter(pl.col("venue").is_in(venues))
        candidates = candidates.filter(pl.col("venue").is_in(venues))
    artifacts = harness.write_signal_invalidation_tables(output_root, trades, candidates)
    if not args.skip_state_panel:
        artifacts.update(harness.write_signal_invalidation_state_panel(output_root, trades, candidates, venues))
    if not args.no_report:
        report = harness.refresh_report_from_existing_artifacts(output_root, venues, artifacts)
        artifacts["final_research_report"] = str(report)
    print(artifacts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
