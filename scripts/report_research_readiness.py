from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any


DEFAULT_VOLUME_PROMOTION_GLOB = (
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/*/promotion/"
    "volume_promotion_report.json"
)
DEFAULT_CLOSE_FADE_PROMOTION = (
    "data/daily-close-fade-1m-3y-current-top160-20230503-20260503/reports/"
    "daily_close_fade_grid_splits/daily_close_fade_promotion_report.json"
)
DEFAULT_PIT_COVERAGE = "data/daily-close-fade-pit-20230503-20260503/reports/archive_pit_coverage_report.json"


def main() -> int:
    args = parse_args()
    volume_paths = _promotion_paths(args.volume_promotion, args.volume_promotion_glob)
    checks = [
        evaluate_pit_coverage(Path(args.pit_coverage)),
        *(
            [evaluate_promotion(path, name=f"volume_promotion:{_promotion_label(path)}") for path in volume_paths]
            if volume_paths
            else [_missing_check("volume_promotion", Path(args.volume_promotion_glob))]
        ),
        evaluate_promotion(Path(args.close_fade_promotion), name="close_fade_promotion"),
    ]
    payload = {
        "strict": args.strict,
        "overall_status": overall_status(checks, strict=args.strict),
        "checks": checks,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "research_readiness_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "research_readiness_report.md").write_text(format_readiness_report(payload), encoding="utf-8")
    print(
        f"research readiness status={payload['overall_status']} "
        f"path={output_dir / 'research_readiness_report.md'}"
    )
    return 0 if payload["overall_status"] == "pass" else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize alpha-research coverage and promotion-gate readiness.")
    parser.add_argument("--output-dir", default="data/research_reports/readiness")
    parser.add_argument("--pit-coverage", default=DEFAULT_PIT_COVERAGE)
    parser.add_argument(
        "--volume-promotion",
        action="append",
        help="Explicit volume promotion report JSON. Can be repeated. Overrides the default glob when provided.",
    )
    parser.add_argument("--volume-promotion-glob", default=DEFAULT_VOLUME_PROMOTION_GLOB)
    parser.add_argument("--close-fade-promotion", default=DEFAULT_CLOSE_FADE_PROMOTION)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat missing artifacts and failed gates as process failure.",
    )
    return parser.parse_args()


def evaluate_pit_coverage(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if payload is None:
        return _missing_check("pit_coverage", path)
    summary = payload.get("summary", {})
    coverage_rate = _number(summary.get("coverage_rate"), 0.0)
    usable_rate = _number(summary.get("usable_rate"), 0.0)
    min_coverage_rate = _number(payload.get("min_coverage_rate"), 0.0)
    min_usable_rate = _number(payload.get("min_usable_rate"), 0.0)
    passed = coverage_rate + 1e-12 >= min_coverage_rate and usable_rate + 1e-12 >= min_usable_rate
    return {
        "name": "pit_coverage",
        "path": str(path),
        "status": "pass" if passed else "fail",
        "rows": int(_number(payload.get("rows"), 0)),
        "coverage_rate": coverage_rate,
        "usable_rate": usable_rate,
        "min_coverage_rate": min_coverage_rate,
        "min_usable_rate": min_usable_rate,
        "message": (
            f"usable {usable_rate:.2%} vs gate {min_usable_rate:.2%}; "
            f"coverage {coverage_rate:.2%} vs gate {min_coverage_rate:.2%}"
        ),
    }


def evaluate_promotion(path: Path, *, name: str) -> dict[str, Any]:
    payload = _read_json(path)
    if payload is None:
        return _missing_check(name, path)
    rows = int(_number(payload.get("rows"), 0))
    promotable_rows = int(_number(payload.get("promotable_rows"), 0))
    passed = promotable_rows > 0
    return {
        "name": name,
        "path": str(path),
        "status": "pass" if passed else "fail",
        "rows": rows,
        "promotable_rows": promotable_rows,
        "message": f"{promotable_rows}/{rows} promotable rows",
    }


def overall_status(checks: list[dict[str, Any]], *, strict: bool) -> str:
    statuses = [str(check.get("status")) for check in checks]
    if strict:
        return "pass" if statuses and all(status == "pass" for status in statuses) else "fail"
    hard_failures = [status for status in statuses if status == "fail"]
    if hard_failures:
        return "fail"
    return "pass"


def _promotion_paths(explicit_paths: list[str] | None, pattern: str) -> list[Path]:
    if explicit_paths:
        return [Path(item) for item in explicit_paths]
    return [Path(item) for item in sorted(glob.glob(pattern))]


def _promotion_label(path: Path) -> str:
    if path.parent.name == "promotion":
        return path.parent.parent.name
    return path.stem


def format_readiness_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Research Readiness Report",
        "",
        f"Overall status: **{payload['overall_status']}**",
        f"Strict mode: {payload['strict']}",
        "",
        "| Check | Status | Key Message | Path |",
        "|---|---|---|---|",
    ]
    for check in payload.get("checks", []):
        lines.append(
            f"| {check.get('name', '')} | {check.get('status', '')} | "
            f"{check.get('message', '')} | `{check.get('path', '')}` |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `pass` means the artifact met its own configured gate.",
            "- `fail` means the artifact exists but rejected promotion/readiness.",
            "- `missing` means the required report has not been generated yet.",
            "- Use `--strict` in overnight jobs when missing artifacts should fail the run.",
            "",
        ]
    )
    return "\n".join(lines)


def _missing_check(name: str, path: Path) -> dict[str, Any]:
    return {
        "name": name,
        "path": str(path),
        "status": "missing",
        "message": "artifact missing",
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() or path.stat().st_size <= 0:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
