"""Structural tests for the outcome-blind strategy-overhaul schema registry."""

from __future__ import annotations

import json

import pytest

from liquidity_migration.strategy_overhaul_schemas import (
    ARTIFACT_SCHEMAS,
    CONTINUOUS_ENTRY_SCHEMA_ID,
    CONTINUOUS_LABEL_SCHEMA_ID,
    CONTINUOUS_SIGNAL_SCHEMA_ID,
    LONG_ENTRY_SCHEMA_ID,
    LONG_LABEL_SCHEMA_ID,
    LONG_SIGNAL_SCHEMA_ID,
    PROPOSED_SCHEMAS,
    REGISTRY_STATUS,
    SCHEMA_MISMATCHES,
    FieldSpec,
    mismatches_for,
    registry_payload,
    registry_sha256,
    schema_payload,
    schema_sha256,
    validate_registry,
)


EXPECTED = {
    CONTINUOUS_SIGNAL_SCHEMA_ID: ("continuous", "S02", False, 196),
    CONTINUOUS_ENTRY_SCHEMA_ID: ("continuous", "S03", True, 10),
    CONTINUOUS_LABEL_SCHEMA_ID: ("continuous", "S04", True, 34),
    LONG_SIGNAL_SCHEMA_ID: ("long", "S02", False, 138),
    LONG_ENTRY_SCHEMA_ID: ("long", "S03", True, 30),
    LONG_LABEL_SCHEMA_ID: ("long", "S04", True, 71),
}


def _names(schema_id: str) -> set[str]:
    return {field.name for field in ARTIFACT_SCHEMAS[schema_id].fields}


def _field_by_name(schema_id: str, name: str) -> FieldSpec:
    return next(field for field in ARTIFACT_SCHEMAS[schema_id].fields if field.name == name)


def test_registry_has_six_complete_stage_owned_schemas() -> None:
    assert validate_registry() == ()
    assert set(ARTIFACT_SCHEMAS) == set(EXPECTED)
    assert set(PROPOSED_SCHEMAS) == set(EXPECTED)

    for schema_id, (sleeve, stage, outcome_bearing, field_count) in EXPECTED.items():
        schema = ARTIFACT_SCHEMAS[schema_id]
        assert (schema.sleeve, schema.stage, schema.outcome_bearing) == (
            sleeve,
            stage,
            outcome_bearing,
        )
        assert len(schema.fields) == field_count
        assert PROPOSED_SCHEMAS[schema_id] is schema.fields
        assert set(schema.key_fields) <= _names(schema_id)


def test_every_field_has_explicit_dtype_unit_null_and_availability_metadata() -> None:
    for schema in ARTIFACT_SCHEMAS.values():
        names = [field.name for field in schema.fields]
        assert len(names) == len(set(names))
        for field in schema.fields:
            assert field.dtype.strip()
            assert field.unit.strip()
            assert isinstance(field.nullable, bool)
            assert field.null_semantics.strip()
            assert field.available_at.strip()
            assert field.contract_reference.strip()
            if field.implementation in {"missing", "semantic_mismatch"}:
                assert field.issue_id


def test_signal_schemas_do_not_contain_post_signal_entry_or_path_outcomes() -> None:
    continuous = _names(CONTINUOUS_SIGNAL_SCHEMA_ID)
    long = _names(LONG_SIGNAL_SCHEMA_ID)
    forbidden = (
        "entry_price",
        "entry_anchor",
        "point_return",
        "path_return",
        "short_directional_return",
        "_mfe",
        "_mae",
        "first_passage",
        "trade_pnl",
    )
    for names in (continuous, long):
        assert not {name for name in names if any(token in name for token in forbidden)}

    # Signal-time percentage geometry is explicitly permitted by LONG-A0.
    assert {"fc_exit_stop_pct", "fc_exit_take_profit_pct"} <= long


def test_registered_stage_boundaries_match_the_child_templates() -> None:
    continuous_entry = _names(CONTINUOUS_ENTRY_SCHEMA_ID)
    continuous_labels = _names(CONTINUOUS_LABEL_SCHEMA_ID)
    assert {
        "entry_bar_start_ts_ms",
        "entry_anchor_ts_ms",
        "entry_price",
        "entry_anchor_available",
        "missing_anchor_reason",
    } <= continuous_entry
    assert not {name for name in continuous_entry if name.startswith("path_")}
    assert {
        "path_1h_underlying_return",
        "path_1h_short_directional_return",
        "path_24h_short_mfe",
        "path_72h_short_mae",
    } <= continuous_labels
    assert "entry_price" not in continuous_labels

    long_entry = _names(LONG_ENTRY_SCHEMA_ID)
    long_labels = _names(LONG_LABEL_SCHEMA_ID)
    assert {
        "common_entry_price",
        "current_entry_price",
        "current_entry_scan_missing_hour_bitmask",
        "entry_price_improvement",
    } <= long_entry
    assert not {name for name in long_entry if "point_return" in name or "_mfe" in name}
    assert {
        "common_72h_point_return",
        "current_72h_point_return",
        "common_72h_adverse_magnitude",
        "current_stop_price",
        "current_same_bar_stop_tp_ambiguity",
    } <= long_labels
    assert "current_entry_price" not in long_labels


def test_builder_contract_gaps_are_explicit_and_blocking() -> None:
    issue_ids = {mismatch.issue_id for mismatch in SCHEMA_MISMATCHES}
    assert len(issue_ids) == len(SCHEMA_MISMATCHES)
    assert issue_ids == {
        "CONT-ADAPTER-IDENTITY",
        "A0-CONFIG-IDENTITY",
        "A0-POPULATION-COMPLETENESS",
        "LONG-ADAPTER-IDENTITY",
        "LONG-AVAILABILITY-TIMES",
        "LONG-REGIME-CONTEXT",
        "LONG-BTC-MONTH-REGIME",
    }
    assert all(item.severity == "blocking" for item in SCHEMA_MISMATCHES)
    assert {
        schema_id: {mismatch.issue_id for mismatch in mismatches_for(schema_id)}
        for schema_id in EXPECTED
    } == {
        CONTINUOUS_SIGNAL_SCHEMA_ID: {
            "A0-CONFIG-IDENTITY",
            "A0-POPULATION-COMPLETENESS",
            "CONT-ADAPTER-IDENTITY",
        },
        CONTINUOUS_ENTRY_SCHEMA_ID: {"A0-CONFIG-IDENTITY", "CONT-ADAPTER-IDENTITY"},
        CONTINUOUS_LABEL_SCHEMA_ID: {"A0-CONFIG-IDENTITY", "CONT-ADAPTER-IDENTITY"},
        LONG_SIGNAL_SCHEMA_ID: {
            "A0-CONFIG-IDENTITY",
            "A0-POPULATION-COMPLETENESS",
            "LONG-ADAPTER-IDENTITY",
            "LONG-AVAILABILITY-TIMES",
            "LONG-REGIME-CONTEXT",
            "LONG-BTC-MONTH-REGIME",
        },
        LONG_ENTRY_SCHEMA_ID: {"A0-CONFIG-IDENTITY", "LONG-ADAPTER-IDENTITY"},
        LONG_LABEL_SCHEMA_ID: {"A0-CONFIG-IDENTITY", "LONG-ADAPTER-IDENTITY"},
    }
    assert all(
        field.implementation not in {"missing", "semantic_mismatch"}
        for schema in ARTIFACT_SCHEMAS.values()
        for field in schema.fields
    )
    assert all(schema_payload(schema_id)["implementation_ready"] is False for schema_id in EXPECTED)


def test_long_regime_and_month_context_have_adapter_lineage_with_receipt_blockers() -> None:
    expected_issue = {
        "regime_on": "LONG-REGIME-CONTEXT",
        "eth_regime_on": "LONG-REGIME-CONTEXT",
        "btc_regime_available": "LONG-REGIME-CONTEXT",
        "eth_regime_available": "LONG-REGIME-CONTEXT",
        "btc_sma_dist": "LONG-REGIME-CONTEXT",
        "eth_sma_dist": "LONG-REGIME-CONTEXT",
        "btc_month_regime_value": "LONG-BTC-MONTH-REGIME",
        "btc_month_regime_available": "LONG-BTC-MONTH-REGIME",
        "btc_month_regime_pass": "LONG-BTC-MONTH-REGIME",
    }
    for name, issue_id in expected_issue.items():
        field = _field_by_name(LONG_SIGNAL_SCHEMA_ID, name)
        assert field.implementation == "adapter"
        assert field.source_columns == ()
        assert field.issue_id == issue_id


def test_retired_long_fields_record_current_builder_sources() -> None:
    signal_ts = _field_by_name(LONG_SIGNAL_SCHEMA_ID, "signal_ts_ms")
    assert (signal_ts.implementation, signal_ts.source_columns, signal_ts.issue_id) == (
        "builder",
        ("signal_ts_ms",),
        None,
    )
    assert _field_by_name(LONG_SIGNAL_SCHEMA_ID, "simple_return_1d").source_columns == ("log_return",)
    assert _field_by_name(LONG_SIGNAL_SCHEMA_ID, "simple_return_3d").source_columns == ("pump_3d_log",)
    assert _field_by_name(LONG_SIGNAL_SCHEMA_ID, "simple_return_7d").source_columns == ("pump_7d_log",)
    assert _field_by_name(LONG_SIGNAL_SCHEMA_ID, "trigger_strength_ratio").source_columns == (
        "ratio_1d",
        "ratio_3d",
        "ratio_7d",
    )
    assert _field_by_name(LONG_SIGNAL_SCHEMA_ID, "active_trigger_close_location").source_columns == (
        "close_location",
        "close_loc_3d",
        "close_loc_7d",
    )
    assert _field_by_name(LONG_SIGNAL_SCHEMA_ID, "classifier_eligible").source_columns == ("classifier_selected",)

    for name in (
        "current_entry_scan_missing_hour_bitmask",
        "current_entry_scan_prefix_complete",
        "current_entry_policy_available",
        "current_entry_missing_reason",
    ):
        field = _field_by_name(LONG_ENTRY_SCHEMA_ID, name)
        assert field.implementation == "builder"
        assert field.source_columns == (name,)
        assert field.issue_id is None

    for name in (
        "common_1h_endpoint_ts_ms",
        "common_1h_observed_bars",
        "common_1h_path_complete",
        "common_1h_missing_reason",
        "common_1h_hourly_extrema_interval_censored",
        "common_24h_mfe",
        "common_24h_signed_mae",
        "common_24h_adverse_magnitude",
        "common_stop_price",
        "common_take_profit_price",
    ):
        field = _field_by_name(LONG_LABEL_SCHEMA_ID, name)
        assert field.implementation == "builder"
        assert field.source_columns == (name,)
        assert field.issue_id is None

    assert ARTIFACT_SCHEMAS[LONG_ENTRY_SCHEMA_ID].builder_function == (
        "liquidity_migration.strategy_overhaul_long_stages.build_long_s03_entry_policy"
    )
    assert ARTIFACT_SCHEMAS[LONG_LABEL_SCHEMA_ID].builder_function == (
        "liquidity_migration.strategy_overhaul_long_stages.build_long_s04_path_labels"
    )


def test_continuous_exact_wrapper_owns_retired_adapter_surfaces() -> None:
    signal = ARTIFACT_SCHEMAS[CONTINUOUS_SIGNAL_SCHEMA_ID]
    assert signal.builder_function == ("liquidity_migration.strategy_overhaul_s02.build_continuous_s02_feature_tape")
    assert {field.issue_id for field in signal.fields if field.issue_id is not None} == set()
    for name in (
        "venue",
        "canonical_instrument_id",
        "rmom_tie_method",
        "btc_uptrend_known",
        "p3_static_first_rejection_reason",
        "rmom_data_available_ts_ms",
    ):
        field = _field_by_name(CONTINUOUS_SIGNAL_SCHEMA_ID, name)
        assert field.implementation in {"adapter", "projection"}
        assert field.issue_id is None


def test_long_exact_s02_wrapper_owns_rank_and_projection_surfaces() -> None:
    signal = ARTIFACT_SCHEMAS[LONG_SIGNAL_SCHEMA_ID]
    assert signal.builder_function == ("liquidity_migration.strategy_overhaul_long_s02.build_long_s02_feature_tape")
    assert {field.issue_id for field in signal.fields if field.issue_id is not None} == {
        "LONG-ADAPTER-IDENTITY",
        "LONG-AVAILABILITY-TIMES",
        "LONG-REGIME-CONTEXT",
        "LONG-BTC-MONTH-REGIME",
    }
    for prefix in ("today_volume_rank", "universe_rank"):
        for suffix in (
            "population_peer_count",
            "rankable_peer_count",
            "missing_peer_count",
            "tie_count",
            "tie_method",
            "denominator_rule",
        ):
            field = _field_by_name(LONG_SIGNAL_SCHEMA_ID, f"{prefix}_{suffix}")
            assert field.implementation == "adapter"
            assert field.issue_id is None


def test_payloads_and_hashes_are_stable_json_ready_receipts() -> None:
    payload = registry_payload()
    assert payload["registry_status"] == REGISTRY_STATUS
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    assert registry_sha256() == registry_sha256()
    assert len(registry_sha256()) == 64
    for schema_id in EXPECTED:
        assert schema_payload(schema_id)["calculated_in_phase0"] is False
        assert schema_sha256(schema_id) == schema_sha256(schema_id)
        assert len(schema_sha256(schema_id)) == 64


def test_nonimplemented_field_cannot_hide_without_an_issue() -> None:
    with pytest.raises(ValueError, match="requires an issue_id"):
        FieldSpec(
            name="future_return",
            dtype="float64",
            unit="fraction",
            nullable=True,
            null_semantics="null when unavailable",
            available_at="after signal",
            implementation="missing",
            source_columns=(),
            contract_reference="test",
        )


def test_integrated_adapter_or_projection_can_be_issue_free() -> None:
    adapter = FieldSpec(
        name="canonical_id",
        dtype="utf8",
        unit="identifier",
        nullable=False,
        null_semantics="never null",
        available_at="decision",
        implementation="adapter",
        source_columns=(),
        contract_reference="test",
    )
    projection = FieldSpec(
        name="rank_count",
        dtype="int64",
        unit="rows",
        nullable=False,
        null_semantics="never null",
        available_at="decision",
        implementation="projection",
        source_columns=("peer_count",),
        contract_reference="test",
    )

    assert adapter.issue_id is None
    assert projection.issue_id is None
