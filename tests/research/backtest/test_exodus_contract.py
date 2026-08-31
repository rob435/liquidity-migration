from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.research.backtest.exodus_contract import (
    render_exodus_replay_report,
    replay_exodus_contract,
    replay_exodus_contract_file,
)


FIXTURE = Path(__file__).parents[2] / "fixtures" / "exodus_live_contract_replay_v1.json"


def test_replay_pins_exact_rust_checkpoint_and_output_bytes_across_restart_and_cover() -> None:
    report = replay_exodus_contract_file(FIXTURE)
    steps = report["steps"]

    assert report["decision_config_sha256"] == ("570b799bbf1817fdf9dfaa25eba7984fa9f146aa02913328f0c84c25e3d9fb65")
    assert [step["checkpoint_sha256"] for step in steps] == [
        "1e7b42ccdf085068dc71f44efd362e67b069f6893d8a57e0bfe62e16dc88e2d1",
        "0ff9943dfd3b877824f99ca4706f466f809e5b6db464a8da08912a0d53f601ed",
        "0ff9943dfd3b877824f99ca4706f466f809e5b6db464a8da08912a0d53f601ed",
        "e418f0e5545cff6a5e1f01c98f8ac232a1ce83d6b76de6fafdd78bfeff72cfe2",
    ]
    assert [step["reducer_output_sha256"] for step in steps] == [
        "b98ce819eee937b9773fe9b6378da8c1da0133e541be99018d1c8314565cf39d",
        "fcf91118bf150342e08a80efd0b5b342e18993ea7d707ef396f5880ef32024b6",
        "9a8c87e38c2a16cc28f3752664fc3553c8d145e6b9cefff228a0fa416c403550",
        "0bf3157c580d47024e0fc3cd0f5a890b8b4ebdf1a04dcd24d04ff2227912266b",
    ]
    assert json.loads(steps[0]["checkpoint_utf8"])["open"]["AUSDT"]["target_qty"] == 3.25
    assert json.loads(steps[1]["prior_checkpoint_utf8"]) == json.loads(steps[0]["checkpoint_utf8"])
    assert json.loads(steps[2]["prior_checkpoint_utf8"]) == json.loads(steps[1]["checkpoint_utf8"])
    assert steps[2]["covered_symbols"] == ["AUSDT"]
    assert json.loads(steps[3]["checkpoint_utf8"])["open"] == {}
    assert steps[3]["retired_symbols"] == ["AUSDT"]


def test_replay_reports_live_application_order_and_honest_evidence_boundary() -> None:
    report = replay_exodus_contract_file(FIXTURE)

    assert report["application_order"] == [
        "persist_checkpoint",
        "consume_carry_fire",
        "order_effects",
    ]
    assert all(step["application_order"] == report["application_order"] for step in report["steps"])
    assert report["steps"][0]["effect_order"] == [
        "persist_checkpoint",
        "consume_carry_fire",
        "order",
    ]
    assert report["steps"][2]["effect_order"] == [
        "persist_checkpoint",
        "consume_carry_fire",
        "order",
    ]
    boundary = report["evidence_boundary"]
    assert boundary["calls_live_reducer"] is True
    assert boundary["calls_rust_reducer"] is True
    assert boundary["publishes_targets"] is False
    assert boundary["proves_venue_fills"] is False
    assert any("Minute klines cannot prove" in note for note in boundary["notes"])


def test_replay_report_serialization_is_canonical_and_repeatable() -> None:
    first = render_exodus_replay_report(replay_exodus_contract_file(FIXTURE))
    second = render_exodus_replay_report(replay_exodus_contract_file(FIXTURE))

    assert first == second
    assert first.endswith(b"\n")
    assert json.loads(first)["name"] == "exodus_live_contract_replay_v1"


def test_replay_rejects_bool_schema_and_leverage() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    with pytest.raises(ValueError, match="schema"):
        replay_exodus_contract(payload)

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["effective_config"]["entry_leverage"] = True
    with pytest.raises(ValueError, match="leverage"):
        replay_exodus_contract(payload)
