from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import write_research_manifest as manifest_script


def test_research_manifest_hashes_present_and_missing_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "report.json"
    artifact.write_text('{"ok": true}\n', encoding="utf-8")

    manifest = manifest_script.build_research_manifest(
        repo_root=tmp_path,
        artifact_paths=["report.json", "missing.json"],
        artifact_globs=(),
    )

    rows = {row["path"]: row for row in manifest["artifacts"]}

    assert rows["report.json"]["status"] == "present"
    assert rows["report.json"]["sha256"] == hashlib.sha256(b'{"ok": true}\n').hexdigest()
    assert rows["missing.json"]["status"] == "missing"


def test_research_manifest_expands_globs_once(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "reports" / "b.md").write_text("b", encoding="utf-8")

    manifest = manifest_script.build_research_manifest(
        repo_root=tmp_path,
        artifact_paths=["reports/a.md"],
        artifact_globs=("reports/*.md",),
    )

    assert [row["path"] for row in manifest["artifacts"]] == [
        "reports/a.md",
        "reports/b.md",
    ]
