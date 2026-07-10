from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import strategy_overhaul_phase0 as phase0
from liquidity_migration.strategy_overhaul_phase0 import (
    DEFAULT_PROPOSED_SCHEMAS,
    DatasetSpec,
    InstrumentMapEntry,
    Phase0IntegrityError,
    ProposedField,
    SleeveWindow,
    build_phase0_artifacts,
    canonicalize_phase0_roots,
)


DAY_MS = 86_400_000
HOUR_MS = 3_600_000
JAN31 = 1_769_817_600_000
FEB01 = JAN31 + DAY_MS


def _map_entry(
    canonical: str,
    venue: str,
    symbol: str,
    start: str = "2026-01-01",
    end: str | None = None,
    *,
    multiplier: float = 1.0,
) -> InstrumentMapEntry:
    return InstrumentMapEntry(
        canonical_instrument=canonical,
        venue=venue,
        symbol=symbol,
        valid_from_date=start,
        base_asset=canonical.upper(),
        quote_asset="USDT",
        settlement_asset="USDT",
        contract_type="linear_perpetual",
        contract_multiplier=multiplier,
        mapping_source="fixture://reviewed-map",
        review_status="reviewed",
        valid_to_date_exclusive=end,
    )


def _kline_rows(date_ms: int, symbols: tuple[str, ...], *, source: str = "fixture") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for symbol_index, symbol in enumerate(symbols):
        for hour in (0, 1):
            price = 10.0 + symbol_index + hour / 10
            rows.append(
                {
                    "symbol": symbol,
                    "ts_ms": date_ms + hour * HOUR_MS,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + 0.25,
                    "volume_base": 100.0 + hour,
                    "turnover_quote": 1_000.0 + hour,
                    "source": source,
                }
            )
    return rows


def _write_day(
    root: Path,
    *,
    date: str,
    date_ms: int,
    symbols: tuple[str, ...],
    manifest_rows: list[dict[str, object]] | None = None,
    kline_source: str = "fixture",
) -> None:
    kline_path = root / "klines_1h" / f"date={date}" / "part.parquet"
    kline_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(_kline_rows(date_ms, symbols, source=kline_source)).write_parquet(kline_path)

    manifest_path = root / "archive_trade_manifest" / f"date={date}" / "part.parquet"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_rows is None:
        manifest_rows = [{"symbol": symbol, "date": date, "url": f"source:{symbol}:{date}"} for symbol in symbols]
    pl.DataFrame(manifest_rows).write_parquet(manifest_path)


def _write_rmom(root: Path, *, symbols: tuple[str, ...]) -> None:
    rows = [
        {
            "symbol": symbol,
            "ts_ms": timestamp,
            "residual_momentum": 0.1 + index,
            "is_provisional": False,
            "source": "fixture_rmom",
        }
        for index, (symbol, timestamp) in enumerate(
            (symbol, timestamp) for timestamp in (JAN31, FEB01) for symbol in symbols
        )
    ]
    pl.DataFrame(rows).write_parquet(root / "residual_momentum.parquet")


def _complete_roots(tmp_path: Path) -> tuple[Path, Path]:
    bybit = tmp_path / "bybit"
    binance = tmp_path / "binance"
    _write_day(
        bybit,
        date="2026-01-31",
        date_ms=JAN31,
        symbols=("AAAUSDT", "BBBUSDT"),
        kline_source="bybit_v5_market_kline",
        manifest_rows=[
            {
                "symbol": "AAAUSDT",
                "date": "2026-01-31",
                "url": "https://archive/AAA-1",
                "source": "bybit_public_trading_archive",
            },
            {
                "symbol": "AAAUSDT",
                "date": "2026-01-31",
                "url": "bybit_v5_listing",
                "source": "bybit_v5_listing",
            },
            {
                "symbol": "BBBUSDT",
                "date": "2026-01-31",
                "url": "bybit_v5_listing",
                "source": "bybit_v5_listing",
            },
        ],
    )
    _write_day(
        bybit,
        date="2026-02-01",
        date_ms=FEB01,
        symbols=("AAAUSDT", "BBBUSDT"),
        kline_source="bybit_v5_market_kline",
        manifest_rows=[
            {
                "symbol": "AAAUSDT",
                "date": "2026-02-01",
                "url": "https://archive/AAA-2",
                "source": "bybit_public_trading_archive",
            },
            {
                "symbol": "BBBUSDT",
                "date": "2026-02-01",
                "url": "bybit_v5_listing",
                "source": "bybit_v5_listing",
            },
        ],
    )
    _write_rmom(bybit, symbols=("AAAUSDT", "BBBUSDT"))

    # Binance intentionally has no persisted source/provenance columns.  The
    # inventory must retain that as unknown rather than infer archive history.
    for date, date_ms in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        _write_day(
            binance,
            date=date,
            date_ms=date_ms,
            symbols=("AAAUSDT", "CCCUSDT"),
            kline_source="binance_vision_um_1h",
            manifest_rows=[
                {"symbol": symbol, "date": date, "url": "kline_coverage"} for symbol in ("AAAUSDT", "CCCUSDT")
            ],
        )
    _write_rmom(binance, symbols=("AAAUSDT", "CCCUSDT"))
    return bybit, binance


def _rewrite_kline_source(root: Path, source: str | None) -> None:
    for path in sorted((root / "klines_1h").rglob("*.parquet")):
        pl.read_parquet(path).with_columns(pl.lit(source, dtype=pl.String).alias("source")).write_parquet(path)


def _build(
    bybit: Path,
    binance: Path | None = None,
    **kwargs: object,
) -> dict[str, object]:
    roots = {"bybit": bybit}
    if binance is not None:
        roots["binance"] = binance
    return build_phase0_artifacts(
        roots,
        start_date="2026-01-31",
        end_date_exclusive="2026-02-02",
        sleeve_windows=(
            SleeveWindow(
                sleeve="test",
                causal_read_start_date="2026-01-31",
                signal_start_date="2026-02-01",
                signal_end_date_exclusive="2026-02-02",
            ),
        ),
        **kwargs,
    )


def test_date_partition_discovery_does_not_walk_the_full_dataset_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "klines_1h"
    selected = base / "date=2026-02-01" / "symbol=AAAUSDT" / "part.parquet"
    outside = base / "date=2024-01-01" / "symbol=STALEUSDT" / "part.parquet"
    selected.parent.mkdir(parents=True)
    outside.parent.mkdir(parents=True)
    selected.touch()
    outside.touch()

    original_rglob = Path.rglob

    def guarded_rglob(path: Path, pattern: str):
        if path == base:
            raise AssertionError("full dataset-root recursion is forbidden for date partitions")
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", guarded_rglob)
    files = phase0._discover_files(  # noqa: SLF001 - focused discovery regression
        base,
        start=dt.date(2026, 2, 1),
        end=dt.date(2026, 2, 2),
    )

    assert files == [selected]


def test_phase0_is_json_ready_deterministic_and_preserves_month_counts(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)

    first = _build(bybit, binance)
    second = _build(bybit, binance)

    assert first == second
    json.dumps(first, allow_nan=False)
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert first["readiness"]["status"] == "PARTIAL"
    assert first["readiness"]["data_status"] == "PARTIAL"
    assert first["readiness"]["registered_contract_scope_ready"] is False
    assert first["readiness"]["outcome_run_authorized"] is False
    assert first["window"]["sleeve_windows"][0]["causal_warmup_days"] == 1

    kline = first["field_availability"]["bybit"]["klines_1h"]
    assert kline["row_count"] == 8
    assert kline["monthly_counts"] == [
        {"month": "2026-01", "row_count": 4, "symbol_count": 2},
        {"month": "2026-02", "row_count": 4, "symbol_count": 2},
    ]
    assert kline["key_audit"]["status"] == "passed"
    assert kline["grid_integrity"]["off_grid_timestamp_count"] == 0
    assert "close" not in kline["value_columns_read"]
    assert "turnover_quote" not in kline["value_columns_read"]

    pit = first["pit_provenance"]["venues"]
    assert pit["bybit"]["storage_row_count"] == 5
    assert pit["bybit"]["membership_pair_count"] == 4
    assert pit["bybit"]["collapsed_duplicate_source_row_count"] == 1
    assert pit["bybit"]["mixed_observed_inferred_membership_pair_count"] == 1
    assert pit["bybit"]["contradictory_provenance_membership_pair_count"] == 0
    assert pit["bybit"]["observed_wins_collapse_policy"] is True
    assert pit["bybit"]["first_archive_observed"]["left_censoring_possible"] is True
    assert pit["bybit"]["first_archive_observed"]["inventory_start_date"] == "2026-01-31"
    assert pit["bybit"]["observation_status_counts"] == [
        {"status": "archive_observed", "membership_pair_count": 2},
        {"status": "inferred", "membership_pair_count": 2},
        {"status": "unknown", "membership_pair_count": 0},
    ]
    assert pit["binance"]["observation_status_counts"][-1] == {
        "status": "unknown",
        "membership_pair_count": 4,
    }
    assert first["readiness"]["venues"]["bybit"]["pit_provenance_ready"] is True
    assert first["readiness"]["venues"]["binance"]["pit_provenance_ready"] is False
    assert first["readiness"]["venues"]["binance"]["unknown_observation_provenance_membership_pair_count"] == 4
    assert first["root_lineage"]["venues"]["bybit"]["root_build_receipt"]["status"] == "ABSENT"
    assert first["root_lineage"]["all_upstream_authenticity_proven"] is False
    assert first["root_lineage"]["canonical_s01_root_lineage_ready"] is False
    assert first["manifest_kline_coverage"]["all_venues_complete"] is True

    rmom_coverage = first["rmom_population_coverage"]
    assert rmom_coverage["numeric_values_read"] is False
    assert rmom_coverage["s02_stable_value_readiness"] == "DEFERRED"
    assert rmom_coverage["venues"]["bybit"]["population_symbol_day_count"] == 4
    assert rmom_coverage["venues"]["bybit"]["declared_non_provisional_only_symbol_day_count"] == 4

    no_map = first["instrument_map_coverage"]
    assert no_map["status"] == "not_provided"
    assert no_map["portable_matching_ready"] is False
    assert no_map["raw_ticker_candidates"]["symbols"] == ["AAAUSDT"]
    assert no_map["raw_ticker_candidates"]["authoritative"] is False

    audit = first["outcome_blind_audit"]
    assert audit["wall_clock_fields_emitted"] is False
    assert audit["outcome_values_read"] is False
    assert audit["returns_calculated"] is False
    assert audit["mfe_calculated"] is False
    assert audit["mae_calculated"] is False
    assert audit["pnl_calculated"] is False
    assert audit["ranks_calculated"] is False
    assert set(first["proposed_schemas"]) == set(DEFAULT_PROPOSED_SCHEMAS)
    assert len(first["proposed_schemas"]) == 6
    assert first["proposed_schemas"]["continuous_a0_signal_features"]["outcome_bearing_artifact"] is False
    assert first["proposed_schemas"]["long_a0_path_labels"]["outcome_bearing_artifact"] is True
    assert all(not schema["outcome_values_calculated_in_phase0"] for schema in first["proposed_schemas"].values())
    assert first["child_schema_registry"]["artifact_sha256"]
    assert first["child_schema_registry"]["mismatches"]
    assert all(not schema["implementation_ready"] for schema in first["child_schema_registry"]["schemas"].values())
    assert first["dataset_specs"]["sha256"]

    resources = first["resource_estimate"]["totals"]
    assert resources["audit_parquet_files"] == 10
    assert resources["estimated_row_scan_seconds"] == 1
    assert resources["estimated_parquet_file_overhead_seconds"] == 1
    assert resources["estimated_audit_runtime_seconds"] == 2
    assert resources["stress_audit_runtime_seconds"] == 2
    assert (
        first["resource_estimate"]["calibration_reference"][
            "cold_cache_or_big_pc_measurement_required_before_feasibility_claim"
        ]
        is True
    )
    assert first["resource_estimate"]["concurrency_plan"]["worker_processes"] == 1


@pytest.mark.parametrize("source_label", ("bybit_public_trades", "bybit_rest"))
def test_production_bybit_kline_source_labels_pass_only_venue_sanity(
    tmp_path: Path,
    source_label: str,
) -> None:
    bybit, binance = _complete_roots(tmp_path)
    _rewrite_kline_source(bybit, source_label)

    artifact = _build(bybit, binance)
    lineage = artifact["root_lineage"]["venues"]["bybit"]
    klines = lineage["datasets"]["klines_1h"]

    assert klines["observed_source_labels"] == [source_label]
    assert klines["compatible_source_labels"] == [source_label]
    assert klines["incompatible_source_labels"] == []
    assert klines["registered_compatible_labels"] == [
        "bybit_public_trades",
        "bybit_public_trading_archive",
        "bybit_rest",
        "bybit_v5_market_kline",
    ]
    assert klines["source_field_present_for_all_rows"] is True
    assert klines["source_value_present_for_all_rows"] is True
    assert klines["source_label_failure_observation_count"] == 0
    assert klines["source_label_failure_samples"] == []
    assert klines["source_label_compatibility_ready"] is True
    assert lineage["source_label_compatibility_status"] == "COMPATIBLE_SELF_REPORTED"
    assert lineage["source_labels_are_authentication"] is False
    assert lineage["upstream_authenticity_proven"] is False
    assert lineage["canonical_s01_root_lineage_ready"] is False


@pytest.mark.parametrize("source_label", ("binance_vision_um_1h", "unknown_bybit_kline_source"))
def test_binance_and_unknown_kline_source_labels_fail_bybit_sanity_with_bounded_samples(
    tmp_path: Path,
    source_label: str,
) -> None:
    bybit, binance = _complete_roots(tmp_path)
    _rewrite_kline_source(bybit, source_label)

    artifact = _build(bybit, binance)
    lineage = artifact["root_lineage"]["venues"]["bybit"]
    klines = lineage["datasets"]["klines_1h"]

    assert klines["compatible_source_labels"] == []
    assert klines["incompatible_source_labels"] == [source_label]
    assert klines["source_label_compatibility_ready"] is False
    assert lineage["source_label_compatibility_status"] == "INCOMPATIBLE"
    assert klines["source_label_failure_observation_count"] == 8
    assert klines["source_label_failure_file_count"] == 2
    assert klines["source_label_failure_file_sample_limit"] == 20
    assert klines["source_label_failure_relative_file_sample"] == [
        "klines_1h/date=2026-01-31/part.parquet",
        "klines_1h/date=2026-02-01/part.parquet",
    ]
    assert klines["source_label_failure_reason_counts"] == [
        {"reason": "incompatible_label", "observation_count": 8}
    ]
    assert klines["source_label_failure_sample_limit_per_reason"] == 5
    assert len(klines["source_label_failure_samples"]) == 5
    assert all(
        set(sample) == {"column", "key", "observed_value", "reason", "relative_file"}
        and set(sample["key"]) == {"symbol", "ts_ms"}
        and sample["column"] == "source"
        and sample["observed_value"] == source_label
        and sample["reason"] == "incompatible_label"
        and sample["relative_file"].startswith("klines_1h/date=")
        for sample in klines["source_label_failure_samples"]
    )
    assert lineage["source_labels_are_authentication"] is False
    assert lineage["upstream_authenticity_proven"] is False
    assert lineage["canonical_s01_root_lineage_ready"] is False


def test_null_and_missing_kline_source_rows_fail_closed_and_identify_keys_and_files(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    paths = sorted((bybit / "klines_1h").rglob("*.parquet"))
    first = pl.read_parquet(paths[0])
    first_sources = first["source"].to_list()
    first_sources[0] = None
    first.with_columns(pl.Series("source", first_sources, dtype=pl.String)).write_parquet(paths[0])
    pl.read_parquet(paths[1]).drop("source").write_parquet(paths[1])

    artifact = _build(bybit, binance)
    lineage = artifact["root_lineage"]["venues"]["bybit"]
    klines = lineage["datasets"]["klines_1h"]
    scan_sanity = artifact["field_availability"]["bybit"]["klines_1h"]["source_label_sanity"]

    assert klines["observed_source_labels"] == ["bybit_v5_market_kline"]
    assert klines["compatible_source_labels"] == ["bybit_v5_market_kline"]
    assert klines["incompatible_source_labels"] == []
    assert klines["source_field_present_for_all_rows"] is False
    assert klines["source_value_present_for_all_rows"] is False
    assert klines["source_present_for_all_rows"] is False
    assert klines["source_label_compatibility_ready"] is False
    assert klines["source_label_failure_observation_count"] == 5
    assert klines["source_label_failure_file_count"] == 2
    assert klines["source_label_failure_file_sample_limit"] == 20
    assert klines["source_label_failure_relative_file_sample"] == [
        "klines_1h/date=2026-01-31/part.parquet",
        "klines_1h/date=2026-02-01/part.parquet",
    ]
    assert klines["source_label_failure_reason_counts"] == [
        {"reason": "source_field_missing", "observation_count": 4},
        {"reason": "source_value_null", "observation_count": 1},
    ]
    assert scan_sanity["failure_reason_counts"] == klines["source_label_failure_reason_counts"]
    assert scan_sanity["source_labels_are_authentication"] is False
    samples = klines["source_label_failure_samples"]
    assert len(samples) == 5
    assert {sample["reason"] for sample in samples} == {"source_field_missing", "source_value_null"}
    assert all(set(sample["key"]) == {"symbol", "ts_ms"} for sample in samples)
    assert all(sample["relative_file"].startswith("klines_1h/date=") for sample in samples)
    assert all("open" not in sample and "close" not in sample for sample in samples)
    assert lineage["source_labels_are_authentication"] is False
    assert lineage["upstream_authenticity_proven"] is False
    assert lineage["canonical_s01_root_lineage_ready"] is False


def test_venue_swap_and_missing_required_dataset_source_labels_fail_closed(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)

    swapped = _build(binance, bybit)
    assert swapped["readiness"]["status"] == "NOT_READY"
    assert swapped["readiness"]["venues"]["bybit"]["source_label_compatibility_ready"] is False
    assert swapped["readiness"]["venues"]["binance"]["source_label_compatibility_ready"] is False
    assert swapped["root_lineage"]["venues"]["bybit"]["datasets"]["klines_1h"][
        "incompatible_source_labels"
    ] == ["binance_vision_um_1h"]
    assert swapped["root_lineage"]["venues"]["binance"]["datasets"]["klines_1h"][
        "incompatible_source_labels"
    ] == ["bybit_v5_market_kline"]
    assert swapped["root_lineage"]["all_upstream_authenticity_proven"] is False
    assert swapped["root_lineage"]["canonical_s01_root_lineage_ready"] is False

    for path in (bybit / "klines_1h").rglob("*.parquet"):
        pl.read_parquet(path).drop("source").write_parquet(path)
    missing_kline_lineage = _build(bybit, binance)
    bybit_lineage = missing_kline_lineage["root_lineage"]["venues"]["bybit"]
    assert bybit_lineage["datasets"]["archive_trade_manifest"]["source_label_compatibility_ready"] is True
    assert bybit_lineage["datasets"]["klines_1h"]["source_label_compatibility_ready"] is False
    assert bybit_lineage["source_label_compatibility_ready"] is False
    assert missing_kline_lineage["readiness"]["venues"]["bybit"]["status"] == "NOT_READY"


def test_false_inference_flag_cannot_fabricate_archive_provenance(tmp_path: Path) -> None:
    root = tmp_path / "bybit"
    _write_day(
        root,
        date="2026-01-31",
        date_ms=JAN31,
        symbols=("AAAUSDT",),
        kline_source="bybit_v5_market_kline",
        manifest_rows=[
            {
                "symbol": "AAAUSDT",
                "date": "2026-01-31",
                "url": "fixture://self-asserted",
                "source": "fabricated_self_assertion",
                "membership_source": "fabricated_self_assertion",
                "membership_inferred": False,
            }
        ],
    )
    artifact = build_phase0_artifacts(
        {"bybit": root},
        start_date="2026-01-31",
        end_date_exclusive="2026-02-01",
        sleeve_windows=(SleeveWindow("test", "2026-01-31", "2026-01-31", "2026-02-01"),),
    )

    assert artifact["pit_provenance"]["venues"]["bybit"]["observation_status_counts"] == [
        {"status": "archive_observed", "membership_pair_count": 0},
        {"status": "inferred", "membership_pair_count": 0},
        {"status": "unknown", "membership_pair_count": 1},
    ]
    assert artifact["readiness"]["venues"]["bybit"]["pit_provenance_ready"] is False
    assert artifact["root_lineage"]["venues"]["bybit"]["source_label_compatibility_ready"] is False


def test_phase0_rejects_same_physical_or_overlapping_venue_roots(tmp_path: Path) -> None:
    bybit, _binance = _complete_roots(tmp_path)
    with pytest.raises(Phase0IntegrityError, match="same physical directory"):
        build_phase0_artifacts(
            {"bybit": bybit, "binance": bybit},
            start_date="2026-01-31",
            end_date_exclusive="2026-02-02",
        )

    nested = bybit / "nested-root"
    nested.mkdir()
    with pytest.raises(Phase0IntegrityError, match="must not overlap"):
        canonicalize_phase0_roots({"bybit": bybit, "binance": nested}, require_registered_venues=True)


def test_phase0_is_invariant_to_ohlcv_turnover_and_rmom_value_mutations(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    before = _build(bybit, binance)

    for root in (bybit, binance):
        for path in sorted((root / "klines_1h").rglob("*.parquet")):
            frame = pl.read_parquet(path).with_columns(
                (pl.col("open") * 17.0).alias("open"),
                (pl.col("high") * 19.0).alias("high"),
                (pl.col("low") * 11.0).alias("low"),
                (pl.col("close") * 23.0).alias("close"),
                (pl.col("volume_base") * 29.0).alias("volume_base"),
                (pl.col("turnover_quote") * 31.0).alias("turnover_quote"),
            )
            frame.write_parquet(path)
        rmom_path = root / "residual_momentum.parquet"
        pl.read_parquet(rmom_path).with_columns(
            (pl.col("residual_momentum") * -999.0).alias("residual_momentum")
        ).write_parquet(rmom_path)

    after = _build(bybit, binance)
    assert after == before
    assert after["artifact_sha256"] == before["artifact_sha256"]


def test_rmom_population_coverage_retains_provisional_unknown_mixed_and_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    for date, timestamp in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        _write_day(root, date=date, date_ms=timestamp, symbols=symbols)
    pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "ts_ms": JAN31,
                "residual_momentum": 0.1,
                "is_provisional": False,
                "source": "fixture",
            },
            {
                "symbol": "AAAUSDT",
                "ts_ms": JAN31 + HOUR_MS,
                "residual_momentum": 0.2,
                "is_provisional": True,
                "source": "fixture",
            },
            {
                "symbol": "BBBUSDT",
                "ts_ms": JAN31,
                "residual_momentum": 0.3,
                "is_provisional": True,
                "source": "fixture",
            },
            {
                "symbol": "CCCUSDT",
                "ts_ms": JAN31,
                "residual_momentum": 0.4,
                "is_provisional": None,
                "source": "fixture",
            },
            {
                "symbol": "AAAUSDT",
                "ts_ms": FEB01,
                "residual_momentum": 0.5,
                "is_provisional": False,
                "source": "fixture",
            },
        ]
    ).write_parquet(root / "residual_momentum.parquet")

    report = _build(root)["rmom_population_coverage"]["venues"]["bybit"]

    assert report["population_symbol_day_count"] == 6
    assert report["declared_non_provisional_only_symbol_day_count"] == 1
    assert report["declared_provisional_only_symbol_day_count"] == 1
    assert report["provisional_status_unknown_only_symbol_day_count"] == 1
    assert report["mixed_provisional_status_symbol_day_count"] == 1
    assert report["missing_rmom_identity_symbol_day_count"] == 2
    assert report["numeric_residual_momentum_validity"] == "DEFERRED_TO_S02"


def test_legacy_rmom_without_provisional_column_is_unknown_and_not_ready(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    for date, timestamp in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        _write_day(root, date=date, date_ms=timestamp, symbols=("AAAUSDT",))
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT", "AAAUSDT"],
            "ts_ms": [JAN31, FEB01],
            "residual_momentum": [0.1, 0.2],
        }
    ).write_parquet(root / "residual_momentum.parquet")

    artifact = _build(root)
    dataset = artifact["field_availability"]["bybit"]["residual_momentum"]
    coverage = artifact["rmom_population_coverage"]["venues"]["bybit"]

    assert dataset["ready"] is False
    assert "is_provisional" in " ".join(dataset["failure_reasons"])
    assert coverage["provisional_status_unknown_only_symbol_day_count"] == 2
    assert coverage["declared_non_provisional_only_symbol_day_count"] == 0


def test_identity_projection_hash_detects_same_count_manifest_change(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    before = _build(bybit, binance)
    path = bybit / "archive_trade_manifest" / "date=2026-01-31" / "part.parquet"
    frame = pl.read_parquet(path).with_columns(
        pl.when(pl.col("url") == "https://archive/AAA-1")
        .then(pl.lit("https://archive/AAA-renamed"))
        .otherwise(pl.col("url"))
        .alias("url")
    )
    frame.write_parquet(path)

    after = _build(bybit, binance)
    before_hash = before["field_availability"]["bybit"]["archive_trade_manifest"]["key_provenance_projection_sha256"]
    after_hash = after["field_availability"]["bybit"]["archive_trade_manifest"]["key_provenance_projection_sha256"]
    assert after_hash != before_hash
    assert after["pit_provenance"]["venues"]["bybit"]["membership_pair_count"] == 4


def test_exact_manifest_storage_key_duplicates_fail_but_source_rows_do_not(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for date, timestamp in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        duplicate_rows = [
            {"symbol": "AAAUSDT", "date": date, "url": "same", "source": "source_a"},
            {"symbol": "AAAUSDT", "date": date, "url": "same", "source": "source_b"},
        ]
        _write_day(
            root,
            date=date,
            date_ms=timestamp,
            symbols=("AAAUSDT",),
            manifest_rows=duplicate_rows,
        )

    with pytest.raises(Phase0IntegrityError, match="duplicate key"):
        _build(root)


def test_manifest_reports_internal_provenance_contradiction(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for date, timestamp in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        _write_day(
            root,
            date=date,
            date_ms=timestamp,
            symbols=("AAAUSDT",),
            manifest_rows=[
                {
                    "symbol": "AAAUSDT",
                    "date": date,
                    "url": f"https://archive/{date}",
                    "source": "bybit_public_trading_archive",
                    "membership_inferred": date == "2026-01-31",
                }
            ],
        )
    _write_rmom(root, symbols=("AAAUSDT",))

    pit = _build(root)["pit_provenance"]["venues"]["bybit"]

    assert pit["contradictory_provenance_membership_pair_count"] == 1
    assert pit["internally_conflicting_storage_row_count"] == 1


@pytest.mark.parametrize("malformation", ["duplicate", "null", "off_grid"])
def test_kline_key_and_grid_integrity_fail_closed(tmp_path: Path, malformation: str) -> None:
    root = tmp_path / "root"
    for date, timestamp in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        _write_day(root, date=date, date_ms=timestamp, symbols=("AAAUSDT",))
    path = root / "klines_1h" / "date=2026-01-31" / "part.parquet"
    frame = pl.read_parquet(path)
    if malformation == "duplicate":
        frame = pl.concat([frame, frame.head(1)])
    elif malformation == "null":
        frame = frame.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(None, dtype=pl.String))
            .otherwise(pl.col("symbol"))
            .alias("symbol")
        )
    else:
        frame = frame.with_columns(
            pl.when(pl.int_range(pl.len()) == 0).then(pl.col("ts_ms") + 1).otherwise(pl.col("ts_ms")).alias("ts_ms")
        )
    frame.write_parquet(path)

    if malformation in {"duplicate", "null"}:
        with pytest.raises(Phase0IntegrityError):
            _build(root)
    else:
        artifact = _build(root)
        report = artifact["field_availability"]["bybit"]["klines_1h"]
        assert report["ready"] is False
        assert report["grid_integrity"]["off_grid_timestamp_count"] == 1


def test_external_map_reports_coverage_but_cannot_self_assert_trust(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    entries = (
        _map_entry("asset_a", "bybit", "AAAUSDT"),
        _map_entry("asset_b", "bybit", "BBBUSDT"),
        _map_entry("asset_a", "binance", "AAAUSDT"),
        _map_entry("asset_c", "binance", "CCCUSDT"),
    )

    artifact = _build(
        bybit,
        binance,
        instrument_map=entries,
        instrument_map_version="fixture-v1",
    )

    coverage = artifact["instrument_map_coverage"]
    assert coverage["status"] == "diagnostic_untrusted"
    assert coverage["venue_local_identity_ready"] is False
    assert coverage["portable_matching_ready"] is False
    assert coverage["self_asserted_all_entries_reviewed"] is True
    assert coverage["external_review_status_trusted"] is False
    assert coverage["trusted_reviewer_bound_receipt_present"] is False
    assert coverage["map_version"] == "fixture-v1"
    assert coverage["venues"]["bybit"]["mapped_membership_pair_count"] == 4
    assert coverage["cross_venue"]["all_venue_matched_canonical_instrument_days"] == 2
    assert coverage["cross_venue"]["pairwise"] == [
        {
            "left_venue": "binance",
            "right_venue": "bybit",
            "matched_canonical_instrument_days": 2,
            "union_canonical_instrument_days": 6,
        }
    ]


def test_all_mapped_but_disjoint_canonical_sets_are_not_portable(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    entries = (
        _map_entry("bybit_a", "bybit", "AAAUSDT"),
        _map_entry("bybit_b", "bybit", "BBBUSDT"),
        _map_entry("binance_a", "binance", "AAAUSDT"),
        _map_entry("binance_c", "binance", "CCCUSDT"),
    )

    coverage = _build(
        bybit,
        binance,
        instrument_map=entries,
        instrument_map_version="disjoint-v1",
    )["instrument_map_coverage"]

    assert coverage["status"] == "diagnostic_untrusted"
    assert coverage["portable_matching_ready"] is False
    assert coverage["cross_venue"]["all_venue_matched_canonical_instrument_days"] == 0
    assert "no canonical instrument-day is jointly present across all venues" in coverage[
        "portable_matching_unready_reasons"
    ]
    assert any("trusted reviewer-bound receipt" in reason for reason in coverage["portable_matching_unready_reasons"])


def test_same_venue_alias_collision_blocks_portable_matching(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    entries = (
        _map_entry("asset_a", "bybit", "AAAUSDT"),
        _map_entry("asset_a", "bybit", "BBBUSDT"),
        _map_entry("asset_a", "binance", "AAAUSDT"),
        _map_entry("asset_c", "binance", "CCCUSDT"),
    )

    coverage = _build(
        bybit,
        binance,
        instrument_map=entries,
        instrument_map_version="alias-v1",
    )["instrument_map_coverage"]

    assert coverage["status"] == "diagnostic_untrusted"
    assert coverage["portable_matching_ready"] is False
    assert coverage["venues"]["bybit"]["same_venue_canonical_day_alias_collision_count"] == 2
    assert "same-venue aliases" in " ".join(coverage["portable_matching_unready_reasons"])


def test_dataset_spec_cannot_escape_declared_root() -> None:
    with pytest.raises(ValueError, match="inside the declared venue root"):
        DatasetSpec(
            name="escape",
            relative_path="../other-root/data.parquet",
            key_columns=("symbol", "ts_ms"),
            temporal_column="ts_ms",
            temporal_kind="epoch_ms",
        )


def test_instrument_map_rejects_null_duplicate_and_overlapping_keys(tmp_path: Path) -> None:
    bybit, _binance = _complete_roots(tmp_path)
    with pytest.raises(Phase0IntegrityError, match="null or blank"):
        _build(
            bybit,
            instrument_map=(
                {
                    "canonical_instrument": None,
                    "venue": "bybit",
                    "symbol": "AAAUSDT",
                    "valid_from_date": "2026-01-01",
                },
            ),
            instrument_map_version="bad",
        )
    duplicate = (
        _map_entry("a", "bybit", "AAAUSDT"),
        _map_entry("a", "bybit", "AAAUSDT"),
    )
    with pytest.raises(Phase0IntegrityError, match="duplicate instrument-map key"):
        _build(bybit, instrument_map=duplicate, instrument_map_version="bad")
    overlap = (
        _map_entry("a", "bybit", "AAAUSDT", end="2026-02-02"),
        _map_entry("a", "bybit", "AAAUSDT", start="2026-02-01"),
    )
    with pytest.raises(Phase0IntegrityError, match="overlapping"):
        _build(bybit, instrument_map=overlap, instrument_map_version="bad")


def test_one_stale_root_retains_ready_venue_and_marks_overall_partial(tmp_path: Path) -> None:
    bybit, binance = _complete_roots(tmp_path)
    for dataset in ("klines_1h", "archive_trade_manifest"):
        path = binance / dataset / "date=2026-02-01" / "part.parquet"
        path.unlink()

    artifact = _build(bybit, binance)

    assert artifact["readiness"]["status"] == "PARTIAL"
    assert artifact["readiness"]["venues"]["bybit"]["status"] == "READY"
    assert artifact["readiness"]["venues"]["binance"]["status"] == "NOT_READY"
    missing = artifact["field_availability"]["binance"]["klines_1h"]["partition_coverage"]
    assert missing["missing_date_sample"] == ["2026-02-01"]
    assert artifact["field_availability"]["bybit"]["klines_1h"]["row_count"] == 8


def test_manifest_kline_gap_is_counted_without_reading_prices(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for date, timestamp in (("2026-01-31", JAN31), ("2026-02-01", FEB01)):
        _write_day(
            root,
            date=date,
            date_ms=timestamp,
            symbols=("AAAUSDT",),
            manifest_rows=[
                {"symbol": "AAAUSDT", "date": date, "url": "a"},
                {"symbol": "MISSINGUSDT", "date": date, "url": "missing"},
            ],
        )

    artifact = _build(root)
    coverage = artifact["manifest_kline_coverage"]["venues"]["bybit"]
    assert coverage["status"] == "partial"
    assert coverage["membership_without_kline_count"] == 2
    assert coverage["membership_coverage_fraction"] == pytest.approx(0.5)
    assert artifact["readiness"]["venues"]["bybit"]["status"] == "NOT_READY"


def test_proposed_schema_is_a_static_allowlist(tmp_path: Path) -> None:
    bybit, _binance = _complete_roots(tmp_path)
    injected = copy.deepcopy(dict(DEFAULT_PROPOSED_SCHEMAS))
    injected["continuous_a0_signal_features"] = (
        *injected["continuous_a0_signal_features"],
        ProposedField(
            "forward_return_72h",
            "float64",
            "fraction",
            True,
            "outcome",
            "future",
        ),
    )

    with pytest.raises(Phase0IntegrityError, match="static Phase-0 field allowlist"):
        _build(bybit, proposed_schemas=injected)
    with pytest.raises(ValueError, match="forbidden"):
        DatasetSpec(
            name="bad",
            relative_path="bad",
            key_columns=("symbol", "ts_ms"),
            temporal_column="ts_ms",
            temporal_kind="epoch_ms",
            provenance_columns=("close",),
        )
