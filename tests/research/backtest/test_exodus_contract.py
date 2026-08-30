from __future__ import annotations

import json
from pathlib import Path

from liquidity_migration.research.backtest.exodus_contract import (
    render_exodus_replay_report,
    replay_exodus_contract_file,
)


FIXTURE = Path(__file__).parents[2] / "fixtures" / "exodus_live_contract_replay_v1.json"


def test_replay_pins_exact_state_and_target_bytes_across_restart_and_cover() -> None:
    report = replay_exodus_contract_file(FIXTURE)
    steps = report["steps"]

    assert report["effective_config_sha256"] == ("a78841b88d81b2a76d65d98540b5829a43b6073c2b9c8daf0ac570082ad21a2c")
    assert [step["target_book_sha256"] for step in steps] == [
        "b66b1b91d003428101770f447ea0f5e89d3414e684953a0de973594cf72bcee0",
        "540fc82965888719dcd703c57071dba7c397e89b1bccecb4aabde804c7fd893f",
        "f7126dd7635356630e8211286b62d491f73a4f7c380826540069bad4d3114bd9",
        "58b9610a5ebaa3a31b32661ac329651e65734968c96ab3294e04bf7b2128428e",
    ]
    assert [step["staged_state_sha256"] for step in steps] == [
        "24d35c75bf94488b9b80fb758253adc6528aff0c89df9e998b7f5eaabfb72deb",
        "6c193cf409b8b3ad54c57c5f53c7a365efa7453fc48cf389594f5232a6b16883",
        "6c193cf409b8b3ad54c57c5f53c7a365efa7453fc48cf389594f5232a6b16883",
        "6c193cf409b8b3ad54c57c5f53c7a365efa7453fc48cf389594f5232a6b16883",
    ]
    assert [step["final_state_sha256"] for step in steps] == [
        "24d35c75bf94488b9b80fb758253adc6528aff0c89df9e998b7f5eaabfb72deb",
        "6c193cf409b8b3ad54c57c5f53c7a365efa7453fc48cf389594f5232a6b16883",
        "6c193cf409b8b3ad54c57c5f53c7a365efa7453fc48cf389594f5232a6b16883",
        "d24d85ada459bb791d68d2ecf4ed26252bb52066d6f0a0cc6ce38e9f1d5630c7",
    ]
    assert steps[0]["target_book_utf8"] == (
        "{\n"
        '  "decision_ts_ms": 1800000000000,\n'
        '  "source": "exodus_short",\n'
        '  "targets": [\n'
        "    {\n"
        '      "entry_valid_until_ms": 1800000900000,\n'
        '      "leverage": 5.0,\n'
        '      "notional_usdt": -32.5,\n'
        '      "stop_loss_fraction": 0.35,\n'
        '      "symbol": "AUSDT",\n'
        '      "target_qty": -3.25\n'
        "    }\n"
        "  ],\n"
        '  "valid_until_ms": 1800001800000,\n'
        '  "version": 2\n'
        "}\n"
    )
    assert steps[1]["prior_state_utf8"] == steps[0]["final_state_utf8"]
    assert steps[2]["prior_state_utf8"] == steps[1]["final_state_utf8"]
    assert steps[2]["staged_state_utf8"] == steps[2]["final_state_utf8"]
    assert steps[2]["covered_symbols"] == ["AUSDT"]
    assert steps[3]["staged_state_utf8"] == steps[2]["final_state_utf8"]
    assert json.loads(steps[3]["final_state_utf8"])["open"] == []
    assert steps[3]["covered_symbols"] == ["AUSDT"]


def test_replay_reports_live_application_order_and_honest_evidence_boundary() -> None:
    report = replay_exodus_contract_file(FIXTURE)

    assert report["application_order"] == [
        "persist_staged_state",
        "publish_target_book_bytes",
        "persist_final_state_after_conclusive_flat",
    ]
    assert all(step["application_order"] == report["application_order"] for step in report["steps"])
    boundary = report["evidence_boundary"]
    assert boundary["calls_live_reducer"] is True
    assert boundary["publishes_targets"] is False
    assert boundary["proves_venue_fills"] is False
    assert any("Minute klines cannot prove" in note for note in boundary["notes"])


def test_replay_report_serialization_is_canonical_and_repeatable() -> None:
    first = render_exodus_replay_report(replay_exodus_contract_file(FIXTURE))
    second = render_exodus_replay_report(replay_exodus_contract_file(FIXTURE))

    assert first == second
    assert first.endswith(b"\n")
    assert json.loads(first)["name"] == "exodus_live_contract_replay_v1"
