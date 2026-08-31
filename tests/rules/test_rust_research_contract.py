"""Replay fences for Rust-owned CARRY scoring and LONG classification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.rules.carry_hold import CarryHoldConfig
from liquidity_migration.rules.long_config import resolve_strategy_config
from liquidity_migration.rules.long_native import long_v11a_profile
from liquidity_migration.rules.rust_strategy_contract import (
    RustCarryResearchScorer,
    RustLongResearchClassifier,
    RustStrategyContract,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_research_decisions_v1.json"


class RecordingContract:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        return self.response


def test_fixture_replays_both_decisions_over_one_process() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    with RustStrategyContract() as contract:
        assert contract.request(payload["carry"]["request"]) == payload["carry"]["expected"]
        assert contract.request(payload["long"]["request"]) == payload["long"]["expected"]


def test_long_wrapper_maps_nonfinite_features_to_json_null() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    contract = RecordingContract(fixture["long"]["expected"])
    classifier = RustLongResearchClassifier(
        contract,
        resolve_strategy_config("v11a", rule=long_v11a_profile()),
    )
    rows = fixture["long"]["request"]["rows"]
    rows[0]["sigma_daily_30d"] = float("nan")
    rows[1]["log_return"] = float("inf")

    output = classifier.classify(rows)

    assert len(output) == 2
    sent = contract.requests[0]["rows"]
    assert sent[0]["sigma_daily_30d"] is None
    assert sent[1]["log_return"] is None


def test_carry_wrapper_maps_nonfinite_conditioning_to_json_null() -> None:
    config = CarryHoldConfig.from_json(ROOT / "configs" / "lane2_carry_hold_v7.json")
    contract = RecordingContract({"schema_version": 1, "weights": []})
    scorer = RustCarryResearchScorer(contract)
    frame = pl.DataFrame(
        {
            "bar_ts_ms": [86_400_000],
            "symbol": ["ALPHAUSDT"],
            "by_funding": [-0.002],
            "trail_fund_24h": [float("nan")],
            "ret_3d": [None],
            "vol_30d_daily": [None],
            "dtrail_2d": [None],
            "crowd_persistence": [None],
            "turn_growth_3d": [None],
            "d_tt_ls_3d": [None],
        }
    )

    assert scorer.weights(frame, config).is_empty()
    assert contract.requests[0]["rows"][0]["trail_fund_24h"] is None
