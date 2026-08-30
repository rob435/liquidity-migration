from __future__ import annotations

import json
from pathlib import Path

from scripts.research.replay_exodus_contract import main


REPO_ROOT = Path(__file__).parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exodus_live_contract_replay_v1.json"


def test_thin_research_entrypoint_writes_the_package_replay(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "exodus-replay.json"

    assert main(["--input", str(FIXTURE), "--output", str(output)]) == 0

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert summary["steps"] == 4
    assert summary["output"] == str(output.resolve())
    assert report["evidence_boundary"]["proves_venue_fills"] is False
    assert report["steps"][0]["target_book_sha256"] == (
        "b66b1b91d003428101770f447ea0f5e89d3414e684953a0de973594cf72bcee0"
    )
