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
    parser = argparse.ArgumentParser(description="Run or refresh continuous skip-rule portfolio replays.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument(
        "--variants",
        default="",
        help="Comma-separated skip replay variants to run; table still refreshes from all existing summaries.",
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="Only rebuild skip_portfolio_replay.csv from existing summaries",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    variants = [variant.strip() for variant in args.variants.split(",") if variant.strip()] or None
    harness = _load_harness()
    artifacts = harness.write_skip_portfolio_replay(
        Path(args.output_root).expanduser(),
        venues,
        run_replays=not args.no_run,
        variant_names=variants,
    )
    if venues != list(harness.VENUES):
        artifacts = harness.write_skip_portfolio_replay(
            Path(args.output_root).expanduser(),
            list(harness.VENUES),
            run_replays=False,
        )
    print(artifacts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
