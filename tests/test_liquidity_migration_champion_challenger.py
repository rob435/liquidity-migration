from __future__ import annotations

from pathlib import Path

from liquidity_migration.champion_challenger import champion_challenger_specs, run_champion_challenger_audit


def test_champion_challenger_audit_allows_only_demo_relaxed_submitter(tmp_path: Path) -> None:
    payload = run_champion_challenger_audit(tmp_path)
    submitters = [spec for spec in payload["specs"] if spec["submits_orders"]]

    assert payload["status"] == "PASS"
    assert len(submitters) == 1
    assert submitters[0]["challenger_id"] == "champion_demo_relaxed_submit"
    assert "STRATEGY_PROFILE=demo_relaxed" in submitters[0]["command"]
    assert Path(payload["output_files"]["markdown"]).exists()
    assert Path(payload["output_files"]["json"]).exists()


def test_shadow_challenger_commands_never_submit_orders() -> None:
    for spec in champion_challenger_specs():
        if spec.role != "shadow_challenger":
            continue
        assert spec.submits_orders is False
        assert "--submit-orders" not in spec.command
        assert "SUBMIT_ORDERS=1" not in spec.command
