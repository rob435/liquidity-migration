from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    CONTINUOUS_LABEL_SCHEMA_ID,
    CONTINUOUS_SIGNAL_SCHEMA_ID,
    LONG_ENTRY_SCHEMA_ID,
    LONG_LABEL_SCHEMA_ID,
    LONG_SIGNAL_SCHEMA_ID,
)


REPO = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "continuous": REPO / "docs" / "preregistration" / "strategy-overhaul-continuous-a0.analysis.template.json",
    "long": REPO / "docs" / "preregistration" / "strategy-overhaul-long-a0.analysis.template.json",
}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


@pytest.mark.parametrize("sleeve", ["continuous", "long"])
def test_child_analysis_template_is_finite_non_executable_and_stage_separated(
    sleeve: str,
) -> None:
    path = TEMPLATES[sleeve]
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )

    assert payload["sleeve"] == sleeve.upper()
    assert payload["template_status"] == "NON_EXECUTABLE_TEMPLATE"
    assert payload["execution_permitted"] is False
    assert payload["canonical_child_filename_created"] is False
    assert len(payload["hypotheses"]) == 2
    assert len({row["id"] for row in payload["hypotheses"]}) == 2
    assert payload["stages"]["S02"]["outcome_bearing"] is False
    assert payload["stages"]["S03"]["outcome_bearing"] is True
    assert payload["stages"]["S04"]["outcome_bearing"] is True

    substitutions = payload["required_phase0_substitutions"]
    assert substitutions
    assert set(substitutions.values()) == {"REQUIRED_PHASE0_SUBSTITUTION"}
    assert "canonical_contract_sha256" not in substitutions
    assert "canonical_analysis_manifest_sha256" not in substitutions

    observed = payload["support"]["archive_observed_sensitivity"]
    assert observed["require_membership_observation_status"] == "archive_observed"
    assert observed["excluded_statuses"] == ["inferred", "unknown"]
    assert observed["evaluated_per_venue"] is True
    assert observed["same_sign_required_only_for_portability_status"] is True

    dependence = payload["dependence"]
    assert dependence["confidence_interval"] == {
        "method": "percentile",
        "coverage": 0.9875,
        "lower_quantile": 0.00625,
        "upper_quantile": 0.99375,
    }
    assert dependence["multiplicity"]["family_size"] == 4
    assert dependence["multiplicity"]["family_alpha"] == 0.05

    rule = payload["decision_rule"]
    assert rule["label_grain"] == ["hypothesis_id", "venue"]
    assert rule["maximum_advanced_hypotheses"] == 2
    assert rule["maximum_primary_venue_tests"] == 4
    assert rule["one_venue_only"] == ("venue_scoped_label_allowed_cross_venue_portability_unidentified")
    assert rule["venue_disagreement"] == ("preserve_venue_scoped_labels_and_set_portability_status_heterogeneous")


def test_long_template_keeps_post_signal_entry_policy_out_of_s02() -> None:
    payload = json.loads(TEMPLATES["long"].read_text(encoding="utf-8"))
    forbidden = set(payload["stages"]["S02"]["forbidden_columns"])

    assert {
        "h1_h6_close",
        "h1_h6_low",
        "common_entry_anchor",
        "current_entry_anchor",
        "retrace_result",
        "forward_return",
        "mfe",
        "mae",
    } <= forbidden
    assert payload["entry_policy_labels"]["artifact_stage"] == "S03"
    assert payload["path_labels"]["artifact_stage"] == "S04"
    assert payload["entry_policy_labels"]["raw_scan_arrays_written"] is False


def test_literal_child_vocabulary_is_registered_exactly() -> None:
    continuous = json.loads(TEMPLATES["continuous"].read_text(encoding="utf-8"))
    long = json.loads(TEMPLATES["long"].read_text(encoding="utf-8"))
    continuous_s02 = {field.name for field in ARTIFACT_SCHEMAS[CONTINUOUS_SIGNAL_SCHEMA_ID].fields}
    continuous_s04 = {field.name for field in ARTIFACT_SCHEMAS[CONTINUOUS_LABEL_SCHEMA_ID].fields}
    long_s02 = {field.name for field in ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID].fields}
    long_s03 = {field.name for field in ARTIFACT_SCHEMAS[LONG_ENTRY_SCHEMA_ID].fields}
    long_s04 = {field.name for field in ARTIFACT_SCHEMAS[LONG_LABEL_SCHEMA_ID].fields}

    for values in (
        continuous["feature_manifest"]["identity_timing_provenance"],
        continuous["feature_manifest"]["rmom"]["fields"],
        continuous["feature_manifest"]["static_gates"],
    ):
        assert set(values) <= continuous_s02
    for hypothesis in continuous["hypotheses"]:
        assert hypothesis["primary_outcome"] in continuous_s04
        assert set(hypothesis.get("diagnostic_only_outcomes", ())) <= continuous_s04

    for values in (
        long["feature_manifest"]["identity_timing_provenance"],
        long["feature_manifest"]["raw_daily"],
        long["feature_manifest"]["causal_daily_features"],
        long["feature_manifest"]["classifier_fields"],
    ):
        assert set(values) <= long_s02
    assert set(long["entry_policy_labels"]["output_fields"]) <= long_s03
    for hypothesis in long["hypotheses"]:
        if hypothesis["primary_outcome"] != "delta72":
            assert hypothesis["primary_outcome"] in long_s04
        assert set(hypothesis.get("diagnostic_only_outcomes", ())) <= long_s04

    serialized = json.dumps({"continuous": continuous, "long": long}, sort_keys=True)
    for stale in (
        '"reported_launch_time"',
        '"btc_trend_gate_pass"',
        '"short_directional_return_24h"',
        '"source_ordered_first_rejection"',
        '"common_path_72h_complete',
        '"common_underlying_return_72h"',
    ):
        assert stale not in serialized
