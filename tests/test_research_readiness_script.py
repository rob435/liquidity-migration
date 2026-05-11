from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import report_research_readiness as readiness


def test_research_readiness_summarizes_pass_and_missing(tmp_path: Path) -> None:
    pit_path = tmp_path / "pit.json"
    volume_path = tmp_path / "volume.json"
    missing_path = tmp_path / "missing.json"
    pit_path.write_text(
        json.dumps(
            {
                "rows": 100,
                "min_coverage_rate": 0.95,
                "min_usable_rate": 0.90,
                "summary": {"coverage_rate": 0.97, "usable_rate": 0.92},
            }
        ),
        encoding="utf-8",
    )
    volume_path.write_text(json.dumps({"rows": 10, "promotable_rows": 2}), encoding="utf-8")

    checks = [
        readiness.evaluate_pit_coverage(pit_path),
        readiness.evaluate_promotion(volume_path, name="volume_promotion"),
        readiness.evaluate_close_fade_profit_protection(missing_path, name="close_fade_profit_protection"),
    ]

    assert checks[0]["status"] == "pass"
    assert checks[1]["status"] == "pass"
    assert checks[2]["status"] == "missing"
    assert readiness.overall_status(checks, strict=False) == "pass"
    assert readiness.overall_status(checks, strict=True) == "fail"


def test_research_readiness_fails_existing_bad_gate(tmp_path: Path) -> None:
    promotion_path = tmp_path / "promotion.json"
    promotion_path.write_text(json.dumps({"rows": 25, "promotable_rows": 0}), encoding="utf-8")

    check = readiness.evaluate_promotion(promotion_path, name="volume_promotion")

    assert check["status"] == "fail"
    assert readiness.overall_status([check], strict=False) == "fail"


def test_research_readiness_fails_close_fade_profit_protection_without_split_survival(tmp_path: Path) -> None:
    recheck_path = tmp_path / "corrected_profit_protection_summary.json"
    recheck_path.write_text(
        json.dumps(
            {
                "rows": 216,
                "positive_all_splits": 0,
                "best": {"min_split_return": -0.2287},
            }
        ),
        encoding="utf-8",
    )

    check = readiness.evaluate_close_fade_profit_protection(recheck_path, name="close_fade_profit_protection")

    assert check["status"] == "fail"
    assert check["positive_all_splits"] == 0
    assert "0/216" in check["message"]


def test_research_readiness_passes_close_fade_profit_protection_with_split_survival(tmp_path: Path) -> None:
    recheck_path = tmp_path / "corrected_profit_protection_summary.json"
    recheck_path.write_text(
        json.dumps(
            {
                "rows": 216,
                "positive_all_splits": 2,
                "best": {"min_split_return": 0.04},
            }
        ),
        encoding="utf-8",
    )

    check = readiness.evaluate_close_fade_profit_protection(recheck_path, name="close_fade_profit_protection")

    assert check["status"] == "pass"
    assert check["positive_all_splits"] == 2


def test_research_readiness_expands_volume_promotion_glob(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "tail" / "promotion" / "volume_promotion_report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"rows": 10, "promotable_rows": 1}), encoding="utf-8")

    paths = readiness._promotion_paths(None, str(tmp_path / "reports" / "*" / "promotion" / "*.json"))

    assert paths == [path]
    assert readiness._promotion_label(path) == "tail"
