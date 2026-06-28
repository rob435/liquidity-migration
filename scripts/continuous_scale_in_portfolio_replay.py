from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any


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
    parser = argparse.ArgumentParser(description="Run continuous scale-in full-book portfolio replays.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument(
        "--variants",
        default="",
        help="Comma-separated subset of scale-in variants. Defaults to all preregistered variants.",
    )
    parser.add_argument("--summary-only", action="store_true", help="Do not run missing replay artifacts")
    parser.add_argument("--no-report", action="store_true", help="Only write the scale-in replay table")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root).expanduser()
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    variants = [variant.strip() for variant in args.variants.split(",") if variant.strip()] or None
    harness = _load_harness()
    artifacts = harness.write_scale_in_portfolio_replay(
        output_root,
        venues,
        run_replays=not args.summary_only,
        variant_names=variants,
    )
    if not args.no_report:
        report = harness.refresh_report_from_existing_artifacts(output_root, venues, artifacts)
        artifacts["final_research_report"] = str(report)
    print(artifacts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
