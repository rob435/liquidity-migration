"""Static child-artifact schemas for the strategy-overhaul A0 scouts.

This module is deliberately declarative.  It does not read a data root, build a
feature, append a label, or decide that a child contract is executable.  The six
schemas below are the proposed, stage-separated artifact projections required by
the current CONTINUOUS-A0 and LONG-A0 templates.

``implementation`` and ``source_columns`` record which current boundary owns a
field.  A field marked ``missing`` or ``semantic_mismatch`` is not silently
treated as implemented; it points at a blocking mismatch record.  ``adapter``
and ``projection`` identify implemented transformation boundaries.  A retained
``issue_id`` on either means that the transformation exists but its upstream
identity/provenance is not yet receipt-bound.

The registry is suitable for deterministic JSON receipts.  It is not itself a
frozen canonical child contract and it authorizes no outcome run.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Implementation = Literal[
    "builder",
    "passthrough",
    "adapter",
    "projection",
    "missing",
    "semantic_mismatch",
]
Severity = Literal["blocking", "warning"]

REGISTRY_STATUS = "PROPOSED_STATIC_REGISTRY_NOT_CANONICAL"
REGISTRY_VERSION = "strategy_overhaul_child_schemas_v4"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One exact artifact field and its causal/schema metadata."""

    name: str
    dtype: str
    unit: str
    nullable: bool
    null_semantics: str
    available_at: str
    implementation: Implementation
    source_columns: tuple[str, ...]
    contract_reference: str
    issue_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("name", self.name),
            ("dtype", self.dtype),
            ("unit", self.unit),
            ("null_semantics", self.null_semantics),
            ("available_at", self.available_at),
            ("contract_reference", self.contract_reference),
        ):
            if not value.strip():
                raise ValueError(f"field {label} must be non-blank")
        if self.implementation in {"missing", "semantic_mismatch"}:
            if self.issue_id is None:
                raise ValueError(f"field {self.name} requires an issue_id for {self.implementation}")
        elif self.implementation in {"builder", "passthrough"} and self.issue_id is not None:
            raise ValueError(f"implemented field {self.name} must not carry an issue_id")
        if self.implementation in {"builder", "passthrough", "projection", "semantic_mismatch"}:
            if not self.source_columns or any(not source.strip() for source in self.source_columns):
                raise ValueError(f"field {self.name} requires non-blank source_columns")
        elif self.source_columns:
            raise ValueError(f"field {self.name} cannot claim source_columns {self.source_columns!r}")


@dataclass(frozen=True, slots=True)
class ArtifactSchema:
    """A separate keyed artifact; fields do not inherit prior-stage columns."""

    schema_id: str
    sleeve: Literal["continuous", "long"]
    stage: Literal["S02", "S03", "S04"]
    artifact_name: str
    schema_version: str
    key_fields: tuple[str, ...]
    outcome_bearing: bool
    builder_function: str
    fields: tuple[FieldSpec, ...]


@dataclass(frozen=True, slots=True)
class SchemaMismatch:
    """An explicit difference between the template target and current builder."""

    issue_id: str
    schema_ids: tuple[str, ...]
    severity: Severity
    contract_requirement: str
    builder_behavior: str
    required_resolution: str


def _field(
    name: str,
    dtype: str,
    unit: str,
    nullable: bool,
    null_semantics: str,
    available_at: str,
    *,
    implementation: Implementation,
    contract_reference: str,
    source_column: str | None = None,
    source_columns: tuple[str, ...] | None = None,
    issue_id: str | None = None,
) -> FieldSpec:
    if source_column is not None and source_columns is not None:
        raise ValueError(f"field {name} cannot declare both source_column and source_columns")
    if source_columns is None:
        source_columns = (source_column,) if source_column is not None else ()
    if implementation in {"builder", "passthrough", "projection", "semantic_mismatch"} and not source_columns:
        source_columns = (name,)
    return FieldSpec(
        name=name,
        dtype=dtype,
        unit=unit,
        nullable=nullable,
        null_semantics=null_semantics,
        available_at=available_at,
        implementation=implementation,
        source_columns=source_columns,
        contract_reference=contract_reference,
        issue_id=issue_id,
    )


def _many(
    names: tuple[str, ...],
    dtype: str,
    unit: str,
    nullable: bool,
    null_semantics: str,
    available_at: str,
    *,
    implementation: Implementation,
    contract_reference: str,
    issue_id: str | None = None,
) -> tuple[FieldSpec, ...]:
    return tuple(
        _field(
            name,
            dtype,
            unit,
            nullable,
            null_semantics,
            available_at,
            implementation=implementation,
            contract_reference=contract_reference,
            issue_id=issue_id,
        )
        for name in names
    )


_CONT_S02 = "continuous template: Frozen S02 Feature Set"
_CONT_S03 = "continuous template: S03 Common Entry-Anchor Labels"
_CONT_S04 = "continuous template: Frozen S03 And S04 Labels"
_LONG_S02 = "long template: Frozen S02 Feature Set"
_LONG_S03 = "long template: S03 Explicit Entry-Policy Labels"
_LONG_S04 = "long template: Frozen S04 Path Labels"


def _continuous_key_fields(*, stage: str) -> tuple[FieldSpec, ...]:
    availability = "decision_ts_ms" if stage == "S02" else "copied from frozen S02 key"
    return (
        _field(
            "venue",
            "utf8",
            "category",
            False,
            "never null",
            availability,
            implementation="adapter",
            contract_reference=_CONT_S02,
        ),
        _field(
            "symbol",
            "utf8",
            "venue_instrument",
            False,
            "never null",
            availability,
            implementation="builder",
            contract_reference=_CONT_S02,
        ),
        _field(
            "decision_ts_ms",
            "int64",
            "utc_epoch_ms",
            False,
            "never null",
            availability,
            implementation="builder",
            contract_reference=_CONT_S02,
        ),
    )


def _continuous_s02_fields() -> tuple[FieldSpec, ...]:
    fields: list[FieldSpec] = list(_continuous_key_fields(stage="S02"))
    fields.extend(
        (
            _field(
                "canonical_instrument_id",
                "utf8",
                "canonical_instrument",
                False,
                "never null after the receipt-bound venue-local map coverage gate",
                "instrument-map review no later than decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
            _field(
                "signal_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null; hourly source bar-open timestamp",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "signal_bar_close_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "feature_data_available_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "data_available_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null after all source-availability checks pass",
                "max of declared source availability timestamps",
                implementation="projection",
                source_columns=(
                    "feature_data_available_ts_ms",
                    "rmom_data_available_ts_ms",
                ),
                contract_reference=_CONT_S02,
            ),
        )
    )
    fields.extend(
        _many(
            ("manifest_date", "first_archive_observed_date"),
            "date32",
            "utc_date",
            True,
            "null means the manifest source did not establish this date",
            "manifest ingestion before decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("membership_source", "provenance_limitation", "coverage_state", "current_age_source"),
            "utf8",
            "category",
            True,
            "null means the root adapter could not establish the category",
            "root/manifest adapter before decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("membership_inferred", "current_age_source_available", "current_age_240_pass"),
            "bool",
            "flag",
            True,
            "null means the required provenance or age source is unknown",
            "root/manifest adapter before decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("reported_launch_time_ms", "root_first_bar_ts_ms"),
            "int64",
            "utc_epoch_ms",
            True,
            "null means the named source did not establish the timestamp",
            "root/manifest adapter before decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("age_days_reported_launch", "age_days_root_first_bar"),
            "float64",
            "calendar_days",
            True,
            "null means the corresponding age anchor is unavailable",
            "decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )

    fields.extend(
        _many(
            ("open", "high", "low", "close"),
            "float64",
            "quote_asset_per_base_asset",
            False,
            "never null after the closed-bar validity gate",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "turnover_quote",
            "float64",
            "quote_asset",
            True,
            "null means quote turnover was not published for the signal bar",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            (
                "ret1",
                "rv_168h",
                "max_ret168",
                "ret72",
                "ret168",
                "vov",
                "dist_low",
                "giveback_from_prior6_high",
                "turnover_spike_168h",
                "turnover_zscore_168h",
            ),
            "float64",
            "fraction",
            True,
            "null means insufficient causal history or an invalid denominator",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("min720", "max720", "prior6_close_max"),
            "float64",
            "quote_asset_per_base_asset",
            True,
            "null means insufficient causal history",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "prior6_ret1_max",
            "float64",
            "fraction",
            True,
            "null means no prior valid hourly return is available",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("prior168_turnover_mean", "prior168_turnover_std", "turnover_24h"),
            "float64",
            "quote_asset",
            True,
            "null means insufficient causal turnover history",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "prior_max_ret168_lag1",
            "float64",
            "fraction",
            True,
            "null means fewer than 48 prior valid hourly returns",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "turnover_quote_available",
            "bool",
            "flag",
            False,
            "never null",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )

    fields.extend(
        (
            _field(
                "rmom_source_day_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null; requested UTC source day, not proof of publication",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "rmom_data_available_ts_ms",
                "int64",
                "utc_epoch_ms",
                True,
                "null exactly when no stable RMOM row is available; historical operational publication time is not claimed",
                "conservative causal-computability time from the frozen shift-3 construction",
                implementation="projection",
                source_columns=(
                    "rmom_source_day_ts_ms",
                    "rmom_is_provisional",
                ),
                contract_reference=_CONT_S02,
            ),
            _field(
                "residual_momentum",
                "float64",
                "fraction",
                True,
                "null means no RMOM source value joined for the source day",
                "rmom_data_available_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
        )
    )
    fields.extend(
        _many(
            (
                "rmom_is_provisional",
                "rmom_source_row_present",
                "rmom_present",
                "rmom_provenance_declared",
                "rmom_stable_available",
            ),
            "bool",
            "flag",
            False,
            "never null; false distinguishes absence from a positive assertion",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            (
                "rmom_population_peer_count",
                "rmom_rankable_peer_count",
                "rmom_missing_peer_count",
            ),
            "int64",
            "rows",
            False,
            "never null",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        (
            _field(
                "rmom_rank_denominator_count",
                "int64",
                "rows",
                False,
                "never null; equals rankable stable finite peers",
                "decision_ts_ms",
                implementation="projection",
                source_column="rmom_rankable_peer_count",
                contract_reference=_CONT_S02,
            ),
            _field(
                "rmom_tie_method",
                "utf8",
                "category",
                False,
                "never null; average",
                "static rank rule",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
            _field(
                "rmom_rank_denominator_rule",
                "utf8",
                "category",
                False,
                "never null",
                "static rank rule",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
        )
    )
    fields.append(
        _field(
            "rmom_tie_count",
            "int64",
            "rows",
            True,
            "null when this row has no stable rankable RMOM value",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        (
            _field(
                "residual_momentum_rank",
                "float64",
                "fraction_0_1",
                True,
                "null when RMOM is absent, provisional, or non-rankable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "current_q25_pass",
                "bool",
                "flag",
                False,
                "never null; false includes missing/provisional RMOM",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "current_rmom_quantile_cutoff",
                "float64",
                "fraction_0_1",
                False,
                "never null; frozen at 0.25",
                "static config",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
        )
    )

    for prefix in ("full_population", "full_population_liquidity"):
        fields.extend(
            _many(
                (
                    f"{prefix}_rankable_peer_count",
                    f"{prefix}_missing_peer_count",
                    f"{prefix}_rank_denominator_count",
                ),
                "int64",
                "rows",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.append(
            _field(
                f"{prefix}_tie_count",
                "int64",
                "rows",
                True,
                "null when this row's ranked value is unavailable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.append(
            _field(
                f"{prefix}_population_peer_count",
                "uint32",
                "rows",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.extend(
            _many(
                (f"{prefix}_tie_method", f"{prefix}_rank_denominator_rule"),
                "utf8",
                "category",
                False,
                "never null",
                "static rank rule",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.extend(
            _many(
                (f"{prefix}_value_rank", f"{prefix}_score", f"{prefix}_score_rank"),
                "float64",
                "fraction_0_1",
                True,
                "null when the ranked value is unavailable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.append(
            _field(
                f"{prefix}_decile",
                "int64",
                "decile_0_9",
                True,
                "null when the score is unavailable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )

    fields.append(
        _field(
            "full_population_d9",
            "bool",
            "flag",
            False,
            "never null",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("current_q25_population_peer_count", "current_q25_rankable_peer_count", "current_q25_missing_peer_count"),
            "int64",
            "rows",
            False,
            "never null",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    for prefix in ("current_q25", "current_q25_liquidity"):
        fields.extend(
            _many(
                (f"{prefix}_value_rank", f"{prefix}_score", f"{prefix}_score_rank"),
                "float64",
                "fraction_0_1",
                True,
                "null outside stable RMOM q25 or when the ranked value is unavailable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.append(
            _field(
                f"{prefix}_decile",
                "int64",
                "decile_0_9",
                True,
                "null outside stable RMOM q25 or when the score is unavailable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.extend(
            _many(
                (f"{prefix}_tie_count", f"{prefix}_rank_denominator_count"),
                "int64",
                "rows",
                True,
                "null outside the q25 ranking population",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
        fields.extend(
            _many(
                (f"{prefix}_tie_method", f"{prefix}_rank_denominator_rule"),
                "utf8",
                "category",
                True,
                "null outside the q25 ranking population",
                "static rank rule",
                implementation="builder",
                contract_reference=_CONT_S02,
            )
        )
    fields.extend(
        _many(
            (
                "current_q25_liquidity_rankable_peer_count",
                "current_q25_liquidity_missing_peer_count",
            ),
            "int64",
            "rows",
            True,
            "null outside stable RMOM q25",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "current_q25_liquidity_population_peer_count",
            "uint32",
            "rows",
            True,
            "null outside stable RMOM q25",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        (
            _field(
                "liquidity_rank",
                "float64",
                "fraction_0_1",
                True,
                "null outside stable RMOM q25 or when turnover is unavailable",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "current_q25_d9",
                "bool",
                "flag",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
        )
    )

    fields.extend(
        _many(
            (
                "trigger_turn3_pop3",
                "trigger_turn4_pop3",
                "trigger_turn4_pop5",
                "trigger_any_current_component",
                "current_p3_component_membership",
                "current_p4p3_component_membership",
                "current_p4p5_component_membership",
                "raw_trigger_spell_head",
                "component_spell_head",
            ),
            "bool",
            "flag",
            False,
            "never null",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        _many(
            ("component_mask", "current_component_mask_before_liquidity"),
            "int8",
            "bitmask",
            False,
            "never null; valid values are 0, 1, 3, and 7",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "component_membership_count",
            "int8",
            "components",
            False,
            "never null",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        (
            _field(
                "component_tags",
                "utf8",
                "comma_separated_components",
                False,
                "never null; empty string means no component",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "implied_tier_weight",
                "float64",
                "fraction",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "unique_decision_id",
                "utf8",
                "identifier",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "simultaneous_trigger_decision_count",
                "int64",
                "decisions",
                False,
                "never null",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
            _field(
                "event_wave_id",
                "utf8",
                "identifier",
                True,
                "null for non-p3 rows",
                "decision_ts_ms after venue-level wave construction",
                implementation="builder",
                contract_reference=_CONT_S02,
            ),
        )
    )

    spell_prefixes = (
        "full_d9_spell",
        "current_q25_d9_spell",
        "trigger_spell",
        "p3_trigger_spell",
        "p4p3_trigger_spell",
        "p4p5_trigger_spell",
        "current_p3_component_spell",
        "current_p4p3_component_spell",
        "current_p4p5_component_spell",
    )
    for prefix in spell_prefixes:
        fields.extend(
            (
                _field(
                    f"{prefix}_id",
                    "utf8",
                    "identifier",
                    True,
                    "null outside the named membership spell",
                    "decision_ts_ms",
                    implementation="builder",
                    contract_reference=_CONT_S02,
                ),
                _field(
                    f"{prefix}_head",
                    "bool",
                    "flag",
                    False,
                    "never null",
                    "decision_ts_ms",
                    implementation="builder",
                    contract_reference=_CONT_S02,
                ),
                _field(
                    f"{prefix}_start_ts_ms",
                    "int64",
                    "utc_epoch_ms",
                    True,
                    "null outside the named membership spell",
                    "decision_ts_ms",
                    implementation="builder",
                    contract_reference=_CONT_S02,
                ),
                _field(
                    f"{prefix}_hour_index",
                    "int64",
                    "hours",
                    True,
                    "null outside the named membership spell",
                    "decision_ts_ms",
                    implementation="builder",
                    contract_reference=_CONT_S02,
                ),
            )
        )
    fields.append(
        _field(
            "pump_event_cluster_id",
            "utf8",
            "identifier",
            True,
            "null outside a raw trigger spell",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )

    fields.append(
        _field(
            "current_liquidity_500k_pass",
            "bool",
            "flag",
            False,
            "never null; false includes missing turnover",
            "decision_ts_ms",
            implementation="builder",
            contract_reference=_CONT_S02,
        )
    )
    fields.extend(
        (
            _field(
                "btc_uptrend_value",
                "float64",
                "fraction",
                True,
                "null means the causal BTC trend input is unknown",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
            _field(
                "btc_uptrend_known",
                "bool",
                "flag",
                False,
                "never null",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
            _field(
                "btc_uptrend_pass",
                "bool",
                "flag",
                False,
                "never null; false includes unknown, distinguished by btc_uptrend_unknown",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
            _field(
                "btc_uptrend_fail",
                "bool",
                "flag",
                False,
                "never null; true only when the causal value is known and non-positive",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
            _field(
                "btc_uptrend_unknown",
                "bool",
                "flag",
                False,
                "never null; true exactly when the causal value is unavailable",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            ),
        )
    )
    for asset in ("btc", "eth"):
        fields.extend(
            _many(
                (f"{asset}_ret1", f"{asset}_ret24", f"{asset}_ret168", f"{asset}_rv_168h"),
                "float64",
                "fraction",
                True,
                "null means insufficient causal market history",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            )
        )
    fields.extend(
        _many(
            ("alt_breadth_ret24_positive", "alt_breadth_ret1_ge_3pct"),
            "float64",
            "fraction_0_1",
            True,
            "null means no finite eligible alt peers",
            "decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )
    fields.append(
        _field(
            "xs_ret1_dispersion",
            "float64",
            "fraction",
            True,
            "null means fewer than two finite peers",
            "decision_ts_ms",
            implementation="adapter",
            contract_reference=_CONT_S02,
        )
    )
    for prefix in ("alt_breadth_ret24_positive", "alt_breadth_ret1_ge_3pct", "xs_ret1_dispersion"):
        fields.extend(
            _many(
                (f"{prefix}_peer_count", f"{prefix}_missing_peer_count", f"{prefix}_denominator_count"),
                "int64",
                "rows",
                False,
                "never null",
                "decision_ts_ms",
                implementation="adapter",
                contract_reference=_CONT_S02,
            )
        )
    for component in ("p3", "p4p3", "p4p5"):
        fields.extend(
            (
                _field(
                    f"{component}_static_candidate",
                    "bool",
                    "flag",
                    False,
                    "never null",
                    "decision_ts_ms",
                    implementation="adapter",
                    contract_reference=_CONT_S02,
                ),
                _field(
                    f"{component}_static_first_rejection_reason",
                    "utf8",
                    "category",
                    False,
                    "never null; selected rows use static_candidate",
                    "decision_ts_ms",
                    implementation="adapter",
                    contract_reference=_CONT_S02,
                ),
            )
        )
    return tuple(fields)


def _continuous_s03_fields() -> tuple[FieldSpec, ...]:
    fields: list[FieldSpec] = list(_continuous_key_fields(stage="S03"))
    fields.extend(
        (
            _field(
                "canonical_instrument_id",
                "utf8",
                "canonical_instrument",
                False,
                "never null",
                "copied from frozen S02 key",
                implementation="adapter",
                contract_reference=_CONT_S03,
            ),
            _field(
                "signal_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null",
                "copied from frozen S02 key",
                implementation="builder",
                contract_reference=_CONT_S03,
            ),
            _field(
                "entry_bar_start_ts_ms",
                "int64",
                "utc_epoch_ms",
                False,
                "never null; scheduled bar-open timestamp",
                "decision_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S03,
            ),
            _field(
                "entry_anchor_ts_ms",
                "int64",
                "utc_epoch_ms",
                True,
                "null when the following bar close is unavailable",
                "signal_ts_ms + 2h",
                implementation="builder",
                contract_reference=_CONT_S03,
            ),
            _field(
                "entry_price",
                "float64",
                "quote_asset_per_base_asset",
                True,
                "null when the following bar close is unavailable",
                "entry_anchor_ts_ms",
                implementation="builder",
                contract_reference=_CONT_S03,
            ),
            _field(
                "entry_anchor_available",
                "bool",
                "flag",
                False,
                "never null",
                "signal_ts_ms + 2h",
                implementation="builder",
                contract_reference=_CONT_S03,
            ),
            _field(
                "missing_anchor_reason",
                "utf8",
                "category",
                True,
                "null when the common anchor is available",
                "signal_ts_ms + 2h",
                implementation="builder",
                contract_reference=_CONT_S03,
            ),
        )
    )
    return tuple(fields)


def _continuous_s04_fields() -> tuple[FieldSpec, ...]:
    fields: list[FieldSpec] = list(_continuous_key_fields(stage="S04"))
    fields.append(
        _field(
            "canonical_instrument_id",
            "utf8",
            "canonical_instrument",
            False,
            "never null",
            "copied from frozen S02 key",
            implementation="adapter",
            contract_reference=_CONT_S04,
        )
    )
    for horizon in (1, 24, 72):
        endpoint = f"entry_anchor_ts_ms + {horizon}h"
        fields.extend(
            (
                _field(
                    f"path_{horizon}h_close_ts_ms",
                    "int64",
                    "utc_epoch_ms",
                    True,
                    "null when the S03 anchor is unavailable",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_observed_hours",
                    "int64",
                    "hours",
                    False,
                    "never null; zero when the anchor/path is unavailable",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_available",
                    "bool",
                    "flag",
                    False,
                    "never null",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_complete",
                    "bool",
                    "flag",
                    False,
                    "never null",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_missing_reason",
                    "utf8",
                    "category",
                    True,
                    "null when this horizon is complete",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_underlying_return",
                    "float64",
                    "fraction",
                    True,
                    "null unless the anchor and complete horizon endpoint are available",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_short_directional_return",
                    "float64",
                    "fraction",
                    True,
                    "null unless the matching underlying return is available",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
                _field(
                    f"path_{horizon}h_hourly_extrema_interval_censored",
                    "bool",
                    "flag",
                    False,
                    "never null; true for hourly OHLC extrema",
                    endpoint,
                    implementation="builder",
                    contract_reference=_CONT_S04,
                ),
            )
        )
        if horizon in (24, 72):
            fields.extend(
                (
                    _field(
                        f"path_{horizon}h_short_mfe",
                        "float64",
                        "fraction",
                        True,
                        "null unless every hourly high/low in the horizon is valid",
                        endpoint,
                        implementation="builder",
                        contract_reference=_CONT_S04,
                    ),
                    _field(
                        f"path_{horizon}h_short_mae",
                        "float64",
                        "fraction",
                        True,
                        "null unless every hourly high/low in the horizon is valid",
                        endpoint,
                        implementation="builder",
                        contract_reference=_CONT_S04,
                    ),
                )
            )
    fields.extend(
        (
            _field(
                "path_all_minimal_labels_complete",
                "bool",
                "flag",
                False,
                "never null",
                "entry_anchor_ts_ms + 72h",
                implementation="builder",
                contract_reference=_CONT_S04,
            ),
            _field(
                "missing_path_reason",
                "utf8",
                "category",
                True,
                "null when every minimal label is complete",
                "entry_anchor_ts_ms + 72h",
                implementation="builder",
                contract_reference=_CONT_S04,
            ),
        )
    )
    return tuple(fields)


def _long_key_fields(*, stage: str) -> tuple[FieldSpec, ...]:
    later = stage != "S02"
    availability = "signal_ts_ms" if not later else "copied from frozen S02 key"
    return (
        _field(
            "venue",
            "utf8",
            "category",
            False,
            "never null",
            availability,
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        ),
        _field(
            "symbol",
            "utf8",
            "venue_instrument",
            False,
            "never null",
            availability,
            implementation="passthrough" if not later else "builder",
            source_column="symbol",
            contract_reference=_LONG_S02,
        ),
        _field(
            "signal_ts_ms",
            "int64",
            "utc_epoch_ms",
            False,
            "never null; daily signal-bar close timestamp",
            availability,
            implementation="builder",
            source_column="signal_ts_ms",
            contract_reference=_LONG_S02,
        ),
    )


def _long_s02_fields() -> tuple[FieldSpec, ...]:
    fields: list[FieldSpec] = list(_long_key_fields(stage="S02"))
    fields.append(
        _field(
            "canonical_instrument_id",
            "utf8",
            "canonical_instrument",
            False,
            "never null after the receipt-bound venue-local map coverage gate",
            "instrument-map review no later than signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    fields.extend(
        _many(
            ("manifest_date", "first_archive_observed_date"),
            "date32",
            "utc_date",
            True,
            "null means the manifest source did not establish this date",
            "manifest ingestion before signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    fields.extend(
        _many(
            (
                "membership_source",
                "provenance_limitation",
                "coverage_state",
                "symbol_age_source",
            ),
            "utf8",
            "category",
            True,
            "null means the root adapter could not establish the category",
            "root/manifest adapter before signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    fields.append(
        _field(
            "membership_inferred",
            "bool",
            "flag",
            True,
            "null means observed-vs-inferred status is unknown",
            "manifest ingestion before signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    fields.extend(
        _many(
            ("reported_launch_time_ms", "root_first_bar_ts_ms"),
            "int64",
            "utc_epoch_ms",
            True,
            "null means the named source did not establish the timestamp",
            "root/manifest adapter before signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    fields.extend(
        _many(
            ("age_days_reported_launch", "age_days_root_first_bar"),
            "float64",
            "calendar_days",
            True,
            "null means the corresponding age anchor is unavailable",
            "signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    fields.extend(
        _many(
            (
                "signal_feature_available_ts_ms",
                "daily_bar_available_ts_ms",
                "btc_context_available_ts_ms",
                "eth_context_available_ts_ms",
                "btc_month_context_available_ts_ms",
            ),
            "int64",
            "utc_epoch_ms",
            True,
            "null means the adapter has not established declared causal source availability",
            "no later than signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-AVAILABILITY-TIMES",
        )
    )

    fields.extend(
        _many(
            ("open", "high", "low", "close"),
            "float64",
            "quote_asset_per_base_asset",
            False,
            "never null after the valid daily-bar gate",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "turnover_quote",
            "float64",
            "quote_asset",
            True,
            "null means daily quote turnover was unavailable",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "hourly_bars",
            "uint32",
            "bars",
            False,
            "never null; daily rows require at least 20",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("log_return", "pump_3d_log", "pump_7d_log", "intra_max_Nh_pump_log"),
            "float64",
            "log_fraction",
            True,
            "null means insufficient causal calendar/hourly history",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "intraday_feature_available",
            "bool",
            "flag",
            False,
            "never null; true exactly when intra_max_Nh_pump_log is finite",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    for name, source in (
        ("simple_return_1d", "log_return"),
        ("simple_return_3d", "pump_3d_log"),
        ("simple_return_7d", "pump_7d_log"),
    ):
        fields.append(
            _field(
                name,
                "float64",
                "fraction",
                True,
                "null when the matching log return is unavailable",
                "signal_ts_ms",
                implementation="builder",
                source_column=source,
                contract_reference=_LONG_S02,
            )
        )
    fields.extend(
        _many(
            ("close_location", "close_loc_3d", "close_loc_7d"),
            "float64",
            "fraction_0_1",
            True,
            "null means the required daily window is unavailable",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        (
            _field(
                "realized_vol",
                "float64",
                "fraction_sqrt_year",
                True,
                "null until 30 daily log returns are available",
                "signal_ts_ms",
                implementation="passthrough",
                contract_reference=_LONG_S02,
            ),
            _field(
                "sigma_daily_30d",
                "float64",
                "fraction",
                True,
                "null until realized_vol is available",
                "signal_ts_ms",
                implementation="passthrough",
                contract_reference=_LONG_S02,
            ),
        )
    )
    fields.extend(
        _many(
            ("turnover_median_90d", "turnover_median_30d"),
            "float64",
            "quote_asset",
            True,
            "null until the declared calendar-window minimum is met",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "vol_vs_30d_median",
            "float64",
            "ratio",
            True,
            "null when turnover or its 30d median is unavailable/non-positive",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("today_volume_rank", "universe_rank"),
            "uint32",
            "ordinal_rank",
            True,
            "null when the ranked input is unavailable",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    for prefix in ("today_volume_rank", "universe_rank"):
        fields.extend(
            _many(
                (
                    f"{prefix}_population_peer_count",
                    f"{prefix}_rankable_peer_count",
                    f"{prefix}_missing_peer_count",
                ),
                "int64",
                "rows",
                False,
                "never null",
                "signal_ts_ms",
                implementation="adapter",
                contract_reference=_LONG_S02,
            )
        )
        fields.append(
            _field(
                f"{prefix}_tie_count",
                "int64",
                "rows",
                True,
                "null when this row has no rankable input value",
                "signal_ts_ms",
                implementation="adapter",
                contract_reference=_LONG_S02,
            )
        )
        fields.extend(
            _many(
                (f"{prefix}_tie_method", f"{prefix}_denominator_rule"),
                "utf8",
                "category",
                False,
                "never null",
                "static rank rule",
                implementation="adapter",
                contract_reference=_LONG_S02,
            )
        )
    fields.append(
        _field(
            "symbol_age_days",
            "int64",
            "calendar_days",
            False,
            "never null for a daily population row",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "in_universe",
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "true_range",
            "float64",
            "quote_asset_per_base_asset",
            True,
            "null when prior-calendar close is unavailable",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("atr_14d_pct", "coin_30d_return", "coin_60d_return"),
            "float64",
            "fraction",
            True,
            "null means insufficient causal calendar history",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )

    fields.extend(
        _many(
            ("regime_on", "eth_regime_on"),
            "bool",
            "flag",
            False,
            "never null; false currently also covers unavailable upstream context",
            "signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-REGIME-CONTEXT",
        )
    )
    fields.extend(
        _many(
            ("btc_regime_available", "eth_regime_available"),
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-REGIME-CONTEXT",
        )
    )
    fields.append(
        _field(
            "btc_sma_dist",
            "float64",
            "fraction",
            True,
            "null means the exact 30-calendar-day BTC SMA is unavailable",
            "signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-REGIME-CONTEXT",
        )
    )
    fields.append(
        _field(
            "eth_sma_dist",
            "float64",
            "fraction",
            True,
            "null means the exact 30-calendar-day ETH SMA is unavailable",
            "signal_ts_ms",
            implementation="adapter",
            contract_reference=_LONG_S02,
            issue_id="LONG-REGIME-CONTEXT",
        )
    )
    fields.extend(
        (
            _field(
                "btc_month_regime_value",
                "float64",
                "fraction",
                True,
                "null means the configured BTC-month input is unavailable",
                "signal_ts_ms",
                implementation="adapter",
                contract_reference=_LONG_S02,
                issue_id="LONG-BTC-MONTH-REGIME",
            ),
            _field(
                "btc_month_regime_available",
                "bool",
                "flag",
                False,
                "never null",
                "signal_ts_ms",
                implementation="adapter",
                contract_reference=_LONG_S02,
                issue_id="LONG-BTC-MONTH-REGIME",
            ),
            _field(
                "btc_month_regime_pass",
                "bool",
                "flag",
                True,
                "null when the configured BTC-month input is unavailable",
                "signal_ts_ms",
                implementation="adapter",
                contract_reference=_LONG_S02,
                issue_id="LONG-BTC-MONTH-REGIME",
            ),
        )
    )

    fields.extend(
        _many(
            ("coin_fc_sma",),
            "float64",
            "quote_asset_per_base_asset",
            True,
            "null when the gate is disabled or history is insufficient",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("btc_high_proximity", "own_atr_quantile_90d"),
            "float64",
            "ratio",
            True,
            "null means the optional gate input is unavailable",
            "signal_ts_ms",
            implementation="passthrough",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "global_lsr",
            "float64",
            "ratio",
            True,
            "always null in A0 because positioning is Tier C and the frozen LSR gate is disabled",
            "signal_ts_ms",
            implementation="projection",
            source_columns=(),
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "oi_chg_7d",
            "float64",
            "fraction",
            True,
            "always null in A0 because open interest is Tier C and the frozen OI gate is disabled",
            "signal_ts_ms",
            implementation="projection",
            source_columns=(),
            contract_reference=_LONG_S02,
        )
    )

    fields.append(
        _field(
            "fc_sigma_threshold_available",
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("fc_threshold_1d_log", "fc_threshold_3d_log", "fc_threshold_7d_log"),
            "float64",
            "log_fraction",
            False,
            "never null because the fixed 15% fallback is defined",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    for horizon in (1, 3, 7):
        fields.append(
            _field(
                f"ratio_{horizon}d",
                "float64",
                "ratio",
                True,
                "null when the matching log return is unavailable",
                "signal_ts_ms",
                implementation="builder",
                source_column={1: "log_return", 3: "pump_3d_log", 7: "pump_7d_log"}[horizon],
                contract_reference=_LONG_S02,
            )
        )
    fields.extend(
        _many(
            ("fc_trigger_1d_input_complete", "fc_trigger_3d_input_complete", "fc_trigger_7d_input_complete"),
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            (
                "fc_trigger_1d",
                "fc_trigger_3d",
                "fc_trigger_7d",
                "fc_trigger_intraday",
                "fc_trigger_own_quantile",
                "fc_all_trigger",
            ),
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        (
            _field(
                "fc_trigger_identities",
                "list<utf8>",
                "trigger_families",
                False,
                "never null; empty list means no trigger",
                "signal_ts_ms",
                implementation="builder",
                contract_reference=_LONG_S02,
            ),
            _field(
                "fc_trigger_identity",
                "utf8",
                "category",
                True,
                "null means no trigger family fired",
                "signal_ts_ms",
                implementation="builder",
                contract_reference=_LONG_S02,
            ),
            _field(
                "fc_trigger_bitmask",
                "int8",
                "bitmask",
                False,
                "never null; 1d=1, 3d=2, 7d=4",
                "signal_ts_ms",
                implementation="builder",
                contract_reference=_LONG_S02,
            ),
            _field(
                "trigger_strength_ratio",
                "float64",
                "ratio",
                True,
                "null when no close-location-qualified trigger ratio is available",
                "signal_ts_ms",
                implementation="builder",
                source_columns=("ratio_1d", "ratio_3d", "ratio_7d"),
                contract_reference=_LONG_S02,
            ),
            _field(
                "active_trigger_close_location",
                "float64",
                "fraction_0_1",
                True,
                "null when no trigger family fired",
                "signal_ts_ms",
                implementation="builder",
                source_columns=("close_location", "close_loc_3d", "close_loc_7d"),
                contract_reference=_LONG_S02,
            ),
        )
    )

    gate_names = (
        "gate_btc_month_regime",
        "gate_fc_enabled",
        "gate_in_universe",
        "gate_btc_regime",
        "gate_eth_regime",
        "gate_volume_rank",
        "gate_log_return_available",
        "gate_any_trigger",
        "gate_coin_above_own_sma",
        "gate_coin_min_30d_return",
        "gate_btc_not_near_high",
        "gate_btc_must_be_near_high",
        "gate_atr_cap",
        "gate_own_atr_percentile",
        "gate_min_volume_confirmation",
        "gate_max_volume_confirmation",
        "gate_max_coin_60d_return",
        "gate_min_btc_sma_distance",
        "gate_max_btc_sma_distance",
        "gate_lsr",
        "gate_oi_rising",
        "fc_independent_gate_pass",
        "fc_classifier_gate_pass",
    )
    fields.extend(
        _many(
            gate_names,
            "bool",
            "flag",
            False,
            "never null; disabled gates emit their configured pass state",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "first_sequential_rejection_reason",
            "utf8",
            "category",
            False,
            "never null; selected is explicit",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("signal_bar_present", "signal_bar_complete"),
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "signal_close_hourly",
            "float64",
            "quote_asset_per_base_asset",
            True,
            "null when the exact hourly bar ending at signal_ts_ms is unavailable",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("fc_detector_selected", "classifier_selected"),
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "classifier_eligible",
            "bool",
            "flag",
            False,
            "never null; exact classifier selection, not stateful admission",
            "signal_ts_ms",
            implementation="builder",
            source_column="classifier_selected",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "classified_pattern",
            "utf8",
            "category",
            True,
            "null means no classifier pattern selected",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("classifier_stop_pct", "classifier_take_profit_pct"),
            "float64",
            "fraction",
            True,
            "null unless the classifier selected fomo_chase",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "classifier_max_hold_days",
            "int64",
            "calendar_days",
            True,
            "null unless the classifier selected fomo_chase",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("fc_exit_stop_pct", "fc_exit_take_profit_pct"),
            "float64",
            "fraction",
            False,
            "never null because the fixed fallback is defined",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.append(
        _field(
            "fc_exit_max_hold_hours",
            "int64",
            "hours",
            False,
            "never null; frozen at 72",
            "static config",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        _many(
            ("fc_atr_exit_available", "fc_atr_fallback_used"),
            "bool",
            "flag",
            False,
            "never null",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S02,
        )
    )
    fields.extend(
        (
            _field(
                "fc_exit_param_source",
                "utf8",
                "category",
                False,
                "never null",
                "signal_ts_ms",
                implementation="builder",
                contract_reference=_LONG_S02,
            ),
            _field(
                "long_feature_tape_schema_version",
                "utf8",
                "schema_id",
                False,
                "never null; expected long_a0_signal_feature_v3",
                "static builder version",
                implementation="builder",
                contract_reference=_LONG_S02,
            ),
        )
    )
    return tuple(fields)


def _long_s03_fields() -> tuple[FieldSpec, ...]:
    fields: list[FieldSpec] = list(_long_key_fields(stage="S03"))
    fields.append(
        _field(
            "canonical_instrument_id",
            "utf8",
            "canonical_instrument",
            False,
            "never null",
            "copied from frozen S02 key",
            implementation="adapter",
            contract_reference=_LONG_S03,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    for prefix in ("common", "current"):
        fields.extend(
            (
                _field(
                    f"{prefix}_entry_available",
                    "bool",
                    "flag",
                    False,
                    "never null",
                    "after the named entry-policy decision window",
                    implementation="builder",
                    source_column=f"{prefix}_entry_available",
                    contract_reference=_LONG_S03,
                ),
                _field(
                    f"{prefix}_entry_ts_ms",
                    "int64",
                    "utc_epoch_ms",
                    True,
                    "null when the named anchor is unavailable",
                    "after the named entry-policy decision window",
                    implementation="builder",
                    source_column=f"{prefix}_entry_ts_ms",
                    contract_reference=_LONG_S03,
                ),
                _field(
                    f"{prefix}_entry_hour",
                    "int64",
                    "hours_after_signal",
                    True,
                    "null when the named anchor is unavailable",
                    "after the named entry-policy decision window",
                    implementation="builder",
                    source_column=f"{prefix}_entry_hour",
                    contract_reference=_LONG_S03,
                ),
                _field(
                    f"{prefix}_entry_price",
                    "float64",
                    "quote_asset_per_base_asset",
                    True,
                    "null when the named anchor is unavailable",
                    "at the named entry timestamp",
                    implementation="builder",
                    source_column=f"{prefix}_entry_price",
                    contract_reference=_LONG_S03,
                ),
                _field(
                    f"{prefix}_entry_reason",
                    "utf8",
                    "category",
                    False,
                    "never null; unavailable reasons are explicit",
                    "after the named entry-policy decision window",
                    implementation="builder",
                    source_column=f"{prefix}_entry_reason",
                    contract_reference=_LONG_S03,
                ),
            )
        )
    fields.extend(
        _many(
            ("current_entry_retrace_pct",),
            "float64",
            "fraction",
            True,
            "null when the current policy cannot establish a retrace rule",
            "signal_ts_ms",
            implementation="builder",
            contract_reference=_LONG_S03,
        )
    )
    fields.append(
        _field(
            "current_entry_retrace_threshold",
            "float64",
            "quote_asset_per_base_asset",
            True,
            "null when signal close or the retrace rule is unavailable",
            "signal_ts_ms",
            implementation="builder",
            source_column="current_entry_retrace_threshold",
            contract_reference=_LONG_S03,
        )
    )
    fields.extend(
        _many(
            (
                "current_entry_scan_first_hour",
                "current_entry_scan_end_hour",
                "current_entry_close_trigger_first_hour",
                "current_entry_intrabar_low_first_hour_nonfill",
                "current_entry_intrabar_low_observed_first_hour_nonfill",
            ),
            "int64",
            "hours_after_signal",
            True,
            "null when no applicable scan/prefix event exists",
            "after the exact current-policy scan prefix",
            implementation="builder",
            contract_reference=_LONG_S03,
        )
    )
    fields.append(
        _field(
            "current_entry_scan_missing_hour_bitmask",
            "int8",
            "bitmask_h1_to_h6",
            False,
            "never null; zero means no missing close hour in the scanned prefix",
            "after the exact current-policy scan prefix",
            implementation="builder",
            contract_reference=_LONG_S03,
        )
    )
    fields.extend(
        _many(
            (
                "current_entry_scan_prefix_complete",
                "current_entry_close_triggered",
                "current_entry_intrabar_low_touch_nonfill",
                "current_entry_policy_available",
            ),
            "bool",
            "flag",
            True,
            "null only when the diagnostic itself is unobservable",
            "after the exact current-policy scan prefix",
            implementation="builder",
            contract_reference=_LONG_S03,
        )
    )
    fields.extend(
        (
            _field(
                "current_entry_missing_reason",
                "utf8",
                "category",
                True,
                "null when the current entry policy is available",
                "after the exact current-policy scan prefix",
                implementation="builder",
                source_column="current_entry_missing_reason",
                contract_reference=_LONG_S03,
            ),
            _field(
                "entry_price_improvement",
                "float64",
                "fraction",
                True,
                "null unless both common and current entry prices are available",
                "max(common_entry_ts_ms,current_entry_ts_ms)",
                implementation="builder",
                source_column="entry_price_improvement",
                contract_reference=_LONG_S03,
            ),
            _field(
                "entry_delay_hours_vs_common",
                "int64",
                "hours",
                True,
                "null unless both anchors are available",
                "max(common_entry_ts_ms,current_entry_ts_ms)",
                implementation="builder",
                source_column="entry_delay_hours_vs_common",
                contract_reference=_LONG_S03,
            ),
            _field(
                "long_entry_policy_schema_version",
                "utf8",
                "schema_id",
                False,
                "never null; expected long_a0_entry_policy_v1",
                "static builder version",
                implementation="builder",
                source_column="long_entry_policy_schema_version",
                contract_reference=_LONG_S03,
            ),
        )
    )
    return tuple(fields)


def _long_s04_fields() -> tuple[FieldSpec, ...]:
    fields: list[FieldSpec] = list(_long_key_fields(stage="S04"))
    fields.append(
        _field(
            "canonical_instrument_id",
            "utf8",
            "canonical_instrument",
            False,
            "never null",
            "copied from frozen S02 key",
            implementation="adapter",
            contract_reference=_LONG_S04,
            issue_id="LONG-ADAPTER-IDENTITY",
        )
    )
    for prefix in ("common", "current"):
        for horizon in (1, 24, 72):
            endpoint = f"{prefix}_entry_ts_ms + {horizon}h"
            fields.extend(
                (
                    _field(
                        f"{prefix}_{horizon}h_endpoint_ts_ms",
                        "int64",
                        "utc_epoch_ms",
                        True,
                        "null when the named anchor is unavailable",
                        endpoint,
                        implementation="builder",
                        contract_reference=_LONG_S04,
                    ),
                    _field(
                        f"{prefix}_{horizon}h_point_return",
                        "float64",
                        "fraction",
                        True,
                        "null when the anchor or endpoint close is unavailable",
                        endpoint,
                        implementation="builder",
                        source_column=f"{prefix}_{horizon}h_point_return",
                        contract_reference=_LONG_S04,
                    ),
                    _field(
                        f"{prefix}_{horizon}h_point_available",
                        "bool",
                        "flag",
                        False,
                        "never null",
                        endpoint,
                        implementation="builder",
                        source_column=f"{prefix}_{horizon}h_point_available",
                        contract_reference=_LONG_S04,
                    ),
                    _field(
                        f"{prefix}_{horizon}h_observed_bars",
                        "int64",
                        "bars",
                        False,
                        "never null; zero when the anchor/path is unavailable",
                        endpoint,
                        implementation="builder",
                        source_column=f"{prefix}_{horizon}h_observed_bars",
                        contract_reference=_LONG_S04,
                    ),
                    _field(
                        f"{prefix}_{horizon}h_path_complete",
                        "bool",
                        "flag",
                        False,
                        "never null",
                        endpoint,
                        implementation="builder",
                        source_column=f"{prefix}_{horizon}h_path_complete",
                        contract_reference=_LONG_S04,
                    ),
                    _field(
                        f"{prefix}_{horizon}h_missing_reason",
                        "utf8",
                        "category",
                        True,
                        "null when this horizon is complete",
                        endpoint,
                        implementation="builder",
                        contract_reference=_LONG_S04,
                    ),
                    _field(
                        f"{prefix}_{horizon}h_hourly_extrema_interval_censored",
                        "bool",
                        "flag",
                        False,
                        "never null; true for hourly OHLC extrema",
                        endpoint,
                        implementation="builder",
                        contract_reference=_LONG_S04,
                    ),
                )
            )
            if horizon in (24, 72):
                fields.extend(
                    (
                        _field(
                            f"{prefix}_{horizon}h_mfe",
                            "float64",
                            "fraction",
                            True,
                            "null unless the anchor and complete high path are available",
                            endpoint,
                            implementation="builder",
                            source_column=f"{prefix}_{horizon}h_mfe",
                            contract_reference=_LONG_S04,
                        ),
                        _field(
                            f"{prefix}_{horizon}h_signed_mae",
                            "float64",
                            "fraction",
                            True,
                            "null unless the anchor and complete low path are available",
                            endpoint,
                            implementation="builder",
                            source_column=f"{prefix}_{horizon}h_signed_mae",
                            contract_reference=_LONG_S04,
                        ),
                        _field(
                            f"{prefix}_{horizon}h_adverse_magnitude",
                            "float64",
                            "fraction",
                            True,
                            "null unless signed MAE is available",
                            endpoint,
                            implementation="builder",
                            contract_reference=_LONG_S04,
                        ),
                    )
                )
        fields.extend(
            (
                _field(
                    f"{prefix}_stop_price",
                    "float64",
                    "quote_asset_per_base_asset",
                    True,
                    "null when the anchor or signal-time stop percentage is unavailable",
                    "named entry timestamp",
                    implementation="builder",
                    contract_reference=_LONG_S04,
                ),
                _field(
                    f"{prefix}_take_profit_price",
                    "float64",
                    "quote_asset_per_base_asset",
                    True,
                    "null when the anchor or signal-time target percentage is unavailable",
                    "named entry timestamp",
                    implementation="builder",
                    contract_reference=_LONG_S04,
                ),
                _field(
                    f"{prefix}_same_bar_stop_tp_ambiguity",
                    "bool",
                    "flag",
                    True,
                    "true if observed; false only with complete 72h path and none; null otherwise",
                    f"{prefix}_entry_ts_ms + 72h",
                    implementation="builder",
                    source_column=f"{prefix}_same_bar_stop_tp_ambiguity",
                    contract_reference=_LONG_S04,
                ),
                _field(
                    f"{prefix}_label_complete",
                    "bool",
                    "flag",
                    False,
                    "never null",
                    f"{prefix}_entry_ts_ms + 72h",
                    implementation="builder",
                    source_column=f"{prefix}_label_complete",
                    contract_reference=_LONG_S04,
                ),
                _field(
                    f"{prefix}_missing_path_reason",
                    "utf8",
                    "category",
                    True,
                    "null when every frozen label is complete",
                    f"{prefix}_entry_ts_ms + 72h",
                    implementation="builder",
                    source_column=f"{prefix}_missing_path_reason",
                    contract_reference=_LONG_S04,
                ),
            )
        )
    fields.extend(
        (
            _field(
                "long_label_schema_version",
                "utf8",
                "schema_id",
                False,
                "never null; expected long_a0_minimal_labels_v1",
                "static builder version",
                implementation="builder",
                source_column="long_label_schema_version",
                contract_reference=_LONG_S04,
            ),
            _field(
                "long_label_point_horizons",
                "utf8",
                "pipe_separated_hours",
                False,
                "never null; expected 1|24|72",
                "static label config",
                implementation="builder",
                source_column="long_label_point_horizons",
                contract_reference=_LONG_S04,
            ),
            _field(
                "long_label_excursion_horizons",
                "utf8",
                "pipe_separated_hours",
                False,
                "never null; expected 24|72",
                "static label config",
                implementation="builder",
                source_column="long_label_excursion_horizons",
                contract_reference=_LONG_S04,
            ),
        )
    )
    return tuple(fields)


CONTINUOUS_SIGNAL_SCHEMA_ID = "continuous_a0_signal_features"
CONTINUOUS_ENTRY_SCHEMA_ID = "continuous_a0_entry_anchor"
CONTINUOUS_LABEL_SCHEMA_ID = "continuous_a0_path_labels"
LONG_SIGNAL_SCHEMA_ID = "long_a0_signal_features"
LONG_ENTRY_SCHEMA_ID = "long_a0_entry_policy"
LONG_LABEL_SCHEMA_ID = "long_a0_path_labels"


ARTIFACT_SCHEMAS = MappingProxyType(
    {
        CONTINUOUS_SIGNAL_SCHEMA_ID: ArtifactSchema(
            schema_id=CONTINUOUS_SIGNAL_SCHEMA_ID,
            sleeve="continuous",
            stage="S02",
            artifact_name="signal_feature_tape",
            schema_version="continuous_a0_signal_features_v1",
            key_fields=("venue", "symbol", "decision_ts_ms"),
            outcome_bearing=False,
            builder_function="liquidity_migration.strategy_overhaul_s02.build_continuous_s02_feature_tape",
            fields=_continuous_s02_fields(),
        ),
        CONTINUOUS_ENTRY_SCHEMA_ID: ArtifactSchema(
            schema_id=CONTINUOUS_ENTRY_SCHEMA_ID,
            sleeve="continuous",
            stage="S03",
            artifact_name="entry_anchor_labels",
            schema_version="continuous_a0_entry_anchor_v1",
            key_fields=("venue", "symbol", "decision_ts_ms"),
            outcome_bearing=True,
            builder_function="liquidity_migration.continuous_population_scout.build_continuous_entry_anchor",
            fields=_continuous_s03_fields(),
        ),
        CONTINUOUS_LABEL_SCHEMA_ID: ArtifactSchema(
            schema_id=CONTINUOUS_LABEL_SCHEMA_ID,
            sleeve="continuous",
            stage="S04",
            artifact_name="path_labels",
            schema_version="continuous_a0_minimal_path_labels_v1",
            key_fields=("venue", "symbol", "decision_ts_ms"),
            outcome_bearing=True,
            builder_function="liquidity_migration.continuous_population_scout.append_continuous_path_labels",
            fields=_continuous_s04_fields(),
        ),
        LONG_SIGNAL_SCHEMA_ID: ArtifactSchema(
            schema_id=LONG_SIGNAL_SCHEMA_ID,
            sleeve="long",
            stage="S02",
            artifact_name="signal_feature_tape",
            schema_version="long_a0_signal_feature_v3",
            key_fields=("venue", "symbol", "signal_ts_ms"),
            outcome_bearing=False,
            builder_function="liquidity_migration.strategy_overhaul_long_s02.build_long_s02_feature_tape",
            fields=_long_s02_fields(),
        ),
        LONG_ENTRY_SCHEMA_ID: ArtifactSchema(
            schema_id=LONG_ENTRY_SCHEMA_ID,
            sleeve="long",
            stage="S03",
            artifact_name="entry_policy_labels",
            schema_version="long_a0_entry_policy_v1",
            key_fields=("venue", "symbol", "signal_ts_ms"),
            outcome_bearing=True,
            builder_function="liquidity_migration.strategy_overhaul_long_stages.build_long_s03_entry_policy",
            fields=_long_s03_fields(),
        ),
        LONG_LABEL_SCHEMA_ID: ArtifactSchema(
            schema_id=LONG_LABEL_SCHEMA_ID,
            sleeve="long",
            stage="S04",
            artifact_name="path_labels",
            schema_version="long_a0_minimal_labels_v1",
            key_fields=("venue", "symbol", "signal_ts_ms"),
            outcome_bearing=True,
            builder_function="liquidity_migration.strategy_overhaul_long_stages.build_long_s04_path_labels",
            fields=_long_s04_fields(),
        ),
    }
)

PROPOSED_SCHEMAS = MappingProxyType({schema_id: schema.fields for schema_id, schema in ARTIFACT_SCHEMAS.items()})


def _mismatch(
    issue_id: str,
    schema_ids: tuple[str, ...],
    requirement: str,
    behavior: str,
    resolution: str,
    *,
    severity: Severity = "blocking",
) -> SchemaMismatch:
    return SchemaMismatch(
        issue_id=issue_id,
        schema_ids=schema_ids,
        severity=severity,
        contract_requirement=requirement,
        builder_behavior=behavior,
        required_resolution=resolution,
    )


SCHEMA_MISMATCHES = (
    _mismatch(
        "A0-CONFIG-IDENTITY",
        (
            CONTINUOUS_SIGNAL_SCHEMA_ID,
            CONTINUOUS_ENTRY_SCHEMA_ID,
            CONTINUOUS_LABEL_SCHEMA_ID,
            LONG_SIGNAL_SCHEMA_ID,
            LONG_ENTRY_SCHEMA_ID,
            LONG_LABEL_SCHEMA_ID,
        ),
        "Every versioned A0 artifact must be bound to the exact mechanically derived registered config and its hash.",
        "Exact canonical config/scope/component identity primitives and an S02 parity manifest exist, but they remain unwired to a complete child runner and are not carried across every CONTINUOUS and LONG stage.",
        "Bind the mechanically derived config identity artifacts in S01, have each registered wrapper verify them before S02, and recheck the same receipt chain through S04.",
    ),
    _mismatch(
        "A0-POPULATION-COMPLETENESS",
        (CONTINUOUS_SIGNAL_SCHEMA_ID, LONG_SIGNAL_SCHEMA_ID),
        "Cross-sectional ranks and breadth require proof that every expected PIT symbol/time row is present, not just validation of supplied rows.",
        "Outcome-blind source/signal population-key primitives now detect omissions within supplied root projections, but root completeness and their receipts are not wired into S01/S02.",
        "Build the expected population from receipt-bound complete root projections, bind its key artifact in S01, and fail S02 on any omission or extra.",
    ),
    _mismatch(
        "CONT-ADAPTER-IDENTITY",
        (CONTINUOUS_SIGNAL_SCHEMA_ID, CONTINUOUS_ENTRY_SCHEMA_ID, CONTINUOUS_LABEL_SCHEMA_ID),
        "Receipt-bound identity/PIT inputs and a venue-local map are required; cross-venue review is required only for portability claims.",
        "The exact wrapper validates PIT coverage state, map intervals, product identity, and same-venue aliases, but still accepts caller-supplied sidecars and an unverified nonblank map-version label.",
        "Load the normalized venue-local manifest/map from the registered receipt paths, verify their content hashes before S02, and carry that identity through S04.",
    ),
    _mismatch(
        "LONG-ADAPTER-IDENTITY",
        (LONG_SIGNAL_SCHEMA_ID, LONG_ENTRY_SCHEMA_ID, LONG_LABEL_SCHEMA_ID),
        "Receipt-bound venue-local identity, PIT provenance, coverage, and independent age anchors are mandatory; cross-venue review is a separate portability gate.",
        "The exact wrapper validates PIT/map state and independently supplied root-age parity, but those sidecars and the nonblank map-version label are not yet hash-bound to the Phase-0/S01 receipts.",
        "Load the normalized venue-local manifest/map/age inventory from registered receipt paths and verify their content hashes before S02.",
    ),
    _mismatch(
        "LONG-AVAILABILITY-TIMES",
        (LONG_SIGNAL_SCHEMA_ID,),
        "Every source field needs an explicit causal availability timestamp no later than signal_ts_ms.",
        "The exact adapter validates declared per-source causal times and their aggregate maximum, but the sidecar is not yet mechanically reconstructed from or hash-bound to registered source data.",
        "Register, hash, and reconstruct the availability sidecar from receipt-bound source-event metadata without claiming unobserved publication/ingestion latency.",
    ),
    _mismatch(
        "LONG-REGIME-CONTEXT",
        (LONG_SIGNAL_SCHEMA_ID,),
        "BTC and ETH exact-30d regime value, distance, and availability must remain distinguishable.",
        "The source-context adapter restores explicit availability/distances and parity-checks known states, but its timestamp-level BTC/ETH sidecar provenance is unbound.",
        "Build and hash the regime sidecar from the registered causal root, preserving unavailable state exactly.",
    ),
    _mismatch(
        "LONG-BTC-MONTH-REGIME",
        (LONG_SIGNAL_SCHEMA_ID,),
        "BTC-month value/availability/pass must be explicit even while the configured gate is off.",
        "The source-context adapter separates value, availability, nullable pass, and the independently true disabled gate, but its month sidecar provenance is unbound.",
        "Build and hash the configured daily-30d context from the registered causal root.",
    ),
)


def validate_registry() -> tuple[str, ...]:
    """Return deterministic validation errors; an empty tuple means structurally valid."""

    errors: list[str] = []
    expected_ids = {
        CONTINUOUS_SIGNAL_SCHEMA_ID,
        CONTINUOUS_ENTRY_SCHEMA_ID,
        CONTINUOUS_LABEL_SCHEMA_ID,
        LONG_SIGNAL_SCHEMA_ID,
        LONG_ENTRY_SCHEMA_ID,
        LONG_LABEL_SCHEMA_ID,
    }
    if set(ARTIFACT_SCHEMAS) != expected_ids:
        errors.append("registry must contain exactly the six A0 child artifact schemas")
    issue_ids = [item.issue_id for item in SCHEMA_MISMATCHES]
    if len(issue_ids) != len(set(issue_ids)):
        errors.append("schema mismatch issue_ids must be unique")
    known_issues = set(issue_ids)
    mismatch_by_id = {item.issue_id: item for item in SCHEMA_MISMATCHES}
    for mismatch in SCHEMA_MISMATCHES:
        if not mismatch.schema_ids or set(mismatch.schema_ids) - expected_ids:
            errors.append(f"{mismatch.issue_id}: invalid schema_ids")
        for value in (
            mismatch.contract_requirement,
            mismatch.builder_behavior,
            mismatch.required_resolution,
        ):
            if not value.strip():
                errors.append(f"{mismatch.issue_id}: blank mismatch text")
    for schema_id, schema in ARTIFACT_SCHEMAS.items():
        if schema.schema_id != schema_id:
            errors.append(f"{schema_id}: mapping key/schema_id mismatch")
        names = [field.name for field in schema.fields]
        if len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            errors.append(f"{schema_id}: duplicate fields {duplicates}")
        missing_keys = set(schema.key_fields) - set(names)
        if missing_keys:
            errors.append(f"{schema_id}: missing key fields {sorted(missing_keys)}")
        if schema.stage == "S02" and schema.outcome_bearing:
            errors.append(f"{schema_id}: S02 cannot be outcome-bearing")
        if schema.stage in {"S03", "S04"} and not schema.outcome_bearing:
            errors.append(f"{schema_id}: {schema.stage} must be outcome-bearing")
        for field in schema.fields:
            if field.issue_id is not None and field.issue_id not in known_issues:
                errors.append(f"{schema_id}.{field.name}: unknown issue_id {field.issue_id}")
                continue
            if field.issue_id is not None:
                mismatch = mismatch_by_id[field.issue_id]
                if schema_id not in mismatch.schema_ids:
                    errors.append(f"{schema_id}.{field.name}: issue {field.issue_id} does not cover schema")
    return tuple(errors)


def schema_payload(schema_id: str) -> dict[str, object]:
    """Return one canonical-JSON-ready schema payload."""

    schema = ARTIFACT_SCHEMAS[schema_id]
    blocking_issue_ids = sorted(
        {
            mismatch.issue_id
            for mismatch in SCHEMA_MISMATCHES
            if mismatch.severity == "blocking" and schema_id in mismatch.schema_ids
        }
    )
    return {
        "registry_status": REGISTRY_STATUS,
        "registry_version": REGISTRY_VERSION,
        "schema_id": schema.schema_id,
        "sleeve": schema.sleeve,
        "stage": schema.stage,
        "artifact_name": schema.artifact_name,
        "schema_version": schema.schema_version,
        "key_fields": list(schema.key_fields),
        "outcome_bearing": schema.outcome_bearing,
        "calculated_in_phase0": False,
        "builder_function": schema.builder_function,
        "implementation_ready": not blocking_issue_ids
        and all(
            field.implementation not in {"missing", "semantic_mismatch"} and field.issue_id is None
            for field in schema.fields
        ),
        "fields": [
            {
                **dataclasses.asdict(field),
                "source_columns": list(field.source_columns),
            }
            for field in schema.fields
        ],
        "blocking_issue_ids": blocking_issue_ids,
    }


def schema_sha256(schema_id: str) -> str:
    """Hash the exact stable JSON representation of one schema."""

    encoded = json.dumps(
        schema_payload(schema_id),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def registry_payload() -> dict[str, object]:
    """Return the complete registry and discrepancy ledger as JSON-ready data."""

    return {
        "registry_status": REGISTRY_STATUS,
        "registry_version": REGISTRY_VERSION,
        "schemas": {schema_id: schema_payload(schema_id) for schema_id in sorted(ARTIFACT_SCHEMAS)},
        "mismatches": [{**dataclasses.asdict(item), "schema_ids": list(item.schema_ids)} for item in SCHEMA_MISMATCHES],
    }


def registry_sha256() -> str:
    encoded = json.dumps(
        registry_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mismatches_for(schema_id: str) -> tuple[SchemaMismatch, ...]:
    if schema_id not in ARTIFACT_SCHEMAS:
        raise KeyError(schema_id)
    return tuple(item for item in SCHEMA_MISMATCHES if schema_id in item.schema_ids)


_validation_errors = validate_registry()
if _validation_errors:  # pragma: no cover - import-time guard exercised by focused tests
    raise RuntimeError("invalid strategy-overhaul schema registry: " + "; ".join(_validation_errors))


__all__ = [
    "ARTIFACT_SCHEMAS",
    "CONTINUOUS_ENTRY_SCHEMA_ID",
    "CONTINUOUS_LABEL_SCHEMA_ID",
    "CONTINUOUS_SIGNAL_SCHEMA_ID",
    "FieldSpec",
    "LONG_ENTRY_SCHEMA_ID",
    "LONG_LABEL_SCHEMA_ID",
    "LONG_SIGNAL_SCHEMA_ID",
    "PROPOSED_SCHEMAS",
    "REGISTRY_STATUS",
    "REGISTRY_VERSION",
    "SCHEMA_MISMATCHES",
    "ArtifactSchema",
    "SchemaMismatch",
    "mismatches_for",
    "registry_payload",
    "registry_sha256",
    "schema_payload",
    "schema_sha256",
    "validate_registry",
]
