from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import write_research_run_record as run_record


def test_research_run_record_captures_context_and_gate_rows(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("alpha: test\n", encoding="utf-8")
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    report_dir = tmp_path / "reports" / "tail" / "promotion"
    report_dir.mkdir(parents=True)
    promotion = report_dir / "volume_promotion_report.json"
    promotion.write_text(json.dumps({"rows": 12, "promotable_rows": 2}), encoding="utf-8")
    target_dir = tmp_path / "reports" / "daily-target"
    target_dir.mkdir(parents=True)
    target = target_dir / "daily_close_fade_sharpe_target.json"
    target.write_text(
        json.dumps({"status": "target_hit", "rows": {"grid": 144}, "candidates": [{"grid_id": "ok"}]}),
        encoding="utf-8",
    )
    candidate_dir = tmp_path / "reports" / "candidate"
    candidate_dir.mkdir(parents=True)
    candidate = candidate_dir / "daily_close_fade_report.json"
    candidate.write_text(
        json.dumps(
            {
                "rows": {"trades": 63},
                "backtest_validity": {"label": "candidate", "can_support_promotion": False},
            }
        ),
        encoding="utf-8",
    )

    record = run_record.build_research_run_record(
        repo_root=tmp_path,
        name="Tail Volume Sweep",
        strategy="volume",
        status="candidate",
        bias="current_universe_biased",
        intent="Check whether tail volume survives splits.",
        constraints=("No headline-return promotion.",),
        decision="Review gates before promotion.",
        next_step="Run PIT validation.",
        notes=("test note",),
        tags=("overnight",),
        data_roots=("data-root",),
        config_paths=("config.yaml",),
        artifact_paths=(),
        artifact_globs=("reports/**/*.json",),
    )

    assert record["run_id"].endswith("tail-volume-sweep")
    assert record["configs"][0]["status"] == "present"
    assert record["data_roots"][0]["status"] == "present"
    assert any(row["path"] == "reports/tail/promotion/volume_promotion_report.json" for row in record["artifacts"])
    assert any(row["name"] == "tail" and row["status"] == "pass" for row in record["gates"])
    assert any(row["name"] == "daily_close_fade_sharpe_target" and row["promotable_rows"] == 1 for row in record["gates"])
    assert any(row["name"] == "candidate" and row["promotable_rows"] == 0 for row in record["gates"])

    output_dir = tmp_path / "log"
    run_record.write_research_run_record(record, output_dir=output_dir)

    assert (output_dir / "research_log.jsonl").exists()
    assert (output_dir / "research_log.md").read_text(encoding="utf-8").startswith("# Research Log")
    assert (output_dir / "runs" / f"{record['run_id']}.md").exists()


def test_research_run_record_formats_missing_artifacts(tmp_path: Path) -> None:
    record = run_record.build_research_run_record(
        repo_root=tmp_path,
        name="Missing Artifact",
        strategy="daily-close",
        status="blocked",
        bias="point_in_time",
        intent="Check missing handling.",
        constraints=(),
        decision="n/a",
        next_step="n/a",
        notes=(),
        tags=(),
        data_roots=("missing-data",),
        config_paths=("missing.yaml",),
        artifact_paths=("missing.json",),
        artifact_globs=(),
    )

    report = run_record.format_research_run_record(record)

    assert "Artifacts: 0 present, 1 missing" in report
    assert "| missing | `missing-data` |" in report
    assert "| missing |  | `` | `missing.json` |" in report
