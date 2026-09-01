from __future__ import annotations

from pathlib import Path

from liquidity_migration.research.backtest.native_directional_contract import (
    replay_native_fixture_file,
)


FIXTURES = Path(__file__).parents[2] / "fixtures"


def test_every_long_tape_envelope_calls_and_matches_the_rust_reducer() -> None:
    report = replay_native_fixture_file(
        FIXTURES / "long_native_replay_v1.json",
        sleeve="long",
    )

    assert report["calls_rust_reducer"] is True
    assert [case["name"] for case in report["cases"]] == [
        "flat_entry",
        "pending_entry_holds_original_target",
        "base_stop_exits_before_decay",
        "decayed_stop_exits_after_fill_clock",
        "time_exit_wins_at_fill_deadline",
    ]
    assert [case["output_sha256"] for case in report["cases"]] == [
        "1a3d780f504c0c7880b9749a4b986d73456367dd4029de78c87dc58c821b3a65",
        "172018df508202404a6bd90060fa692a4e231fc77de3b6a44c614b18e03c0998",
        "8dc3651d413e5da412471e1f094db0d08fa3307057eb29dab3a45245ddc19157",
        "3e354c61ac37d90155aec698037afce3d560e7d4999621894d07351b3580f5d6",
        "645983f4843e4827f454cccbc70b3216ff030409ea14fc132818cf05d1c456e9",
    ]


def test_carry_fixture_calls_rust_and_pins_event_before_state_before_orders() -> None:
    report = replay_native_fixture_file(
        FIXTURES / "carry_native_replay_v1.json",
        sleeve="carry",
    )

    assert report["calls_rust_reducer"] is True
    assert report["output_sha256"] == ("285c11c06b2d0aa6b39c8246bc41c72b326ffa7b2517c168eddaf0e59f2eb26a")
    assert report["effect_order"][:2] == ["append_carry_fire", "persist_checkpoint"]
    assert report["output"]["next_state"]["sizing_anchors"] == {
        "1800000000000": 1000.0,
        "1800086400000": 1300.0,
    }
