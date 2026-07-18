#!/usr/bin/env python3
"""Analyze the registered V2 discovery tape and build its barebones portfolio."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import bisect
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, cast

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import scripts.build_candidate_tape as candidate
from liquidity_migration import account_kernel as account_kernel_module
from liquidity_migration import historical_account_replay as replay_module
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms
from liquidity_migration.account_kernel import (
    AccountEventType,
    AccountRiskPolicy,
    read_account_journal,
    verify_account_journal,
)
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.config import CostConfig, TradeLifecycleConfig, load_config
from liquidity_migration.continuous_events import ContinuousEventConfig, _round_trip_bps
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.execution_adapters import ExecutionTwinConfig, LatencyProfile
from liquidity_migration.historical_account_replay import (
    HistoricalAccountSession,
    HistoricalTargetDecision,
    historical_submission_feedback,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.strategy_event_clock import MemoryStrategyEventTape
from liquidity_migration.strategy_funnel import (
    canonical_payload,
    payload_sha256,
    validate_decision_funnel,
    validate_path_labels,
)
from liquidity_migration.strategy_targets import component_target_intent
from liquidity_migration.symbol_codec import encode_symbol_partition
from liquidity_migration.trade_lifecycle import (
    _IndexedTradeState,
    _funding_lookup,
    _perp_funding_return,
)


DISCOVERY_START = dt.date(2021, 5, 1)
DISCOVERY_END = dt.date(2024, 12, 1)
EMBARGO_END = dt.date(2025, 1, 1)
BOOTSTRAP_SEED = 20260717
BOOTSTRAP_REPLICATES = 10_000
CAPITAL_USD = 1_000_000.0
NOTIONAL_USD = 10_000.0
NOTIONAL_WEIGHT = NOTIONAL_USD / CAPITAL_USD
COMPLETION_CONTRACT = REPO / "docs/preregistration/strategy_overhaul_v2_completion_cycle_2026-07-17.md"
BASE_CONTRACT = REPO / "docs/preregistration/strategy_overhaul_v2_diagnostic_epoch_2026-07-17.md"
RECOVERY_CONTRACT = REPO / "docs/preregistration/strategy_overhaul_v2_phase3_replay_recovery_2026-07-18.md"
BUFFERED_RECOVERY_CONTRACT = (
    REPO / "docs/preregistration/strategy_overhaul_v2_phase3_buffered_replay_recovery_2026-07-18.md"
)
EXPECTED_COMPLETION_CONTRACT_SHA256 = "702ab2e84e0c6acdc5c14acd251a60a63f8fdca68928b0109b2d440999876cc8"
EXPECTED_BASE_CONTRACT_SHA256 = "9b522bb09bc08e36eb8cdddcbc47d915fc580499895879c2d10070b4fe090879"
EXPECTED_RECOVERY_CONTRACT_SHA256 = "d572818f7098a4ffda52c325881a98e49ed952b01b626c4e478c5288cb580095"
EXPECTED_BUFFERED_RECOVERY_CONTRACT_SHA256 = "b9e3892d96daaa60617e0ea9b5dbde68a78bae9b55c619eae5d1cd52d3f282e6"
EXPECTED_CANDIDATE_COMMIT = "fefb7b5c4fdd225c45540760488e38c94ec111a7"
HORIZONS = (1, 6, 24, 72)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _month_specs() -> list[tuple[str, dt.date, dt.date]]:
    output: list[tuple[str, dt.date, dt.date]] = []
    cursor = DISCOVERY_START
    while cursor < DISCOVERY_END:
        following = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        output.append((cursor.strftime("%Y-%m"), cursor, following))
        cursor = following
    if len(output) != 43:
        raise RuntimeError("registered discovery calendar must contain 43 months")
    return output


def _manifest_path(candidate_root: Path, month: str) -> Path:
    return candidate_root / f"month={month}" / "manifest.json"


def _load_candidate_manifests(candidate_root: Path) -> list[dict[str, Any]]:
    expected_dirs = [f"month={month}" for month, _start, _end in _month_specs()]
    actual_dirs = sorted(path.name for path in candidate_root.glob("month=*") if path.is_dir())
    if actual_dirs != expected_dirs:
        raise RuntimeError(
            "candidate discovery directories differ from the frozen calendar: "
            f"missing={sorted(set(expected_dirs) - set(actual_dirs))} "
            f"extra={sorted(set(actual_dirs) - set(expected_dirs))}"
        )
    manifests: list[dict[str, Any]] = []
    for month, start, end in _month_specs():
        partition = candidate_root / f"month={month}"
        files = sorted(path.name for path in partition.iterdir() if path.is_file())
        expected_files = ["decision_funnel.parquet", "manifest.json", "path_labels.parquet"]
        if files != expected_files:
            raise RuntimeError(f"{month} payload set differs from the contract: {files}")
        manifest = json.loads(_manifest_path(candidate_root, month).read_text(encoding="utf-8"))
        identity = manifest["run_identity"]
        if identity["source_window"] != {
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
        }:
            raise RuntimeError(f"{month} source window differs from the registered month")
        checks = {
            "candidate code": identity.get("code_commit") == EXPECTED_CANDIDATE_COMMIT,
            "completion contract": identity.get("contract_sha256") == EXPECTED_COMPLETION_CONTRACT_SHA256,
            "base contract": identity.get("base_contract_sha256") == EXPECTED_BASE_CONTRACT_SHA256,
            "clean generation": identity.get("git_dirty") is False,
            "outcome boundary": manifest.get("outcomes_inspected") is False,
        }
        failed = [label for label, passed in checks.items() if not passed]
        if failed:
            raise RuntimeError(f"{month} candidate identity failed: {failed}")
        for name, expected in manifest["files"].items():
            path = partition / name
            if not path.is_file() or _sha256(path) != expected["sha256"]:
                raise RuntimeError(f"{month}/{name} failed its recorded identity")
        manifests.append(manifest)
    return manifests


def _structural_frames(
    candidate_root: Path,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:
    funnels: list[pl.DataFrame] = []
    labels: list[pl.DataFrame] = []
    structural_label_columns = [
        "source_key",
        "sleeve",
        "venue",
        "symbol",
        "signal_ts_ms",
        "entry_ts_ms",
        *[f"target_{hours}h_ts_ms" for hours in HORIZONS],
        *[f"actual_{hours}h_ts_ms" for hours in HORIZONS],
        *[f"missing_{hours}h_reason" for hours in HORIZONS],
        "path_missing_reason",
    ]
    for month, _start, _end in _month_specs():
        partition = candidate_root / f"month={month}"
        funnels.append(pl.read_parquet(partition / "decision_funnel.parquet"))
        path = partition / "path_labels.parquet"
        schema = pl.read_parquet_schema(path)
        missing = sorted(set(structural_label_columns) - set(schema))
        if missing:
            raise RuntimeError(f"{month} structural label fields are missing: {missing}")
        frame = pl.read_parquet(path, columns=structural_label_columns).with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(f"return_{hours}h") for hours in HORIZONS]
        )
        labels.append(frame)
    funnel = pl.concat(funnels, how="diagonal_relaxed", rechunk=True)
    label_structure = pl.concat(labels, how="diagonal_relaxed", rechunk=True)
    funnel_result = validate_decision_funnel(
        funnel,
        required_gate_orders=candidate.REQUIRED_GATE_ORDERS,
    )
    label_result = validate_path_labels(label_structure, funnel)
    start_ms = candidate._date_ms(DISCOVERY_START)
    end_ms = candidate._date_ms(DISCOVERY_END)
    if funnel.filter((pl.col("signal_ts_ms") < start_ms) | (pl.col("signal_ts_ms") >= end_ms)).height:
        raise RuntimeError("candidate source escaped the discovery calendar")
    if funnel["source_key"].n_unique() != funnel.height:
        raise RuntimeError("candidate union contains duplicate source keys")
    return (
        funnel,
        label_structure,
        {
            "months": len(_month_specs()),
            "funnel": funnel_result,
            "labels": label_result,
            "source_dates": funnel.select(pl.from_epoch("signal_ts_ms", time_unit="ms").dt.date().n_unique()).item(),
            "embargo_clean": True,
            "outcome_columns_read": [],
        },
    )


def _aggregate_from_cached_hashes(
    paths: Sequence[Path],
    *,
    relative_to: Path,
    hashes: Mapping[Path, str],
) -> dict[str, Any]:
    digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(set(paths)):
        size = path.stat().st_size
        digest.update(
            canonical_payload(
                {
                    "path": path.relative_to(relative_to).as_posix(),
                    "bytes": size,
                    "sha256": hashes[path],
                }
            )
        )
        digest.update(b"\n")
        total_bytes += size
    return {
        "algorithm": "sha256(sorted canonical {path,bytes,sha256})",
        "file_count": len(set(paths)),
        "bytes": total_bytes,
        "aggregate_sha256": digest.hexdigest(),
    }


def _verify_raw_candidate_inputs(
    root: Path,
    manifests: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    monthly: list[tuple[str, list[Path], list[Path], dict[str, Any]]] = []
    all_paths: set[Path] = set()
    for manifest in manifests:
        identity = manifest["run_identity"]
        start = dt.date.fromisoformat(identity["source_window"]["start"])
        end = dt.date.fromisoformat(identity["source_window"]["end_exclusive"])
        read_start = dt.date.fromisoformat(identity["read_window"]["start"])
        read_end = dt.date.fromisoformat(identity["read_window"]["end_exclusive"])
        kline_paths = candidate._dataset_files(
            root,
            "klines_1h",
            candidate._date_range(read_start, read_end),
        )
        membership_dates = sorted({start - dt.timedelta(days=1), *candidate._date_range(start, end)})
        manifest_paths = candidate._dataset_files(
            root,
            "archive_trade_manifest",
            membership_dates,
        )
        all_paths.update(kline_paths)
        all_paths.update(manifest_paths)
        monthly.append((start.strftime("%Y-%m"), kline_paths, manifest_paths, identity))
    hashes = {path: _sha256(path) for path in sorted(all_paths)}
    for month, klines, archive, identity in monthly:
        actual_kline = _aggregate_from_cached_hashes(klines, relative_to=root, hashes=hashes)
        actual_archive = _aggregate_from_cached_hashes(archive, relative_to=root, hashes=hashes)
        expected = identity["input_identity"]
        if actual_kline != expected["klines_1h"]:
            raise RuntimeError(f"{month} raw kline identity changed after candidate generation")
        if actual_archive != expected["archive_trade_manifest"]:
            raise RuntimeError(f"{month} PIT manifest identity changed after candidate generation")
    rmom = root / "residual_momentum.parquet"
    expected_rmom = {
        identity["input_identity"]["residual_momentum"].get("sha256") for _month, _klines, _archive, identity in monthly
    }
    if expected_rmom != {_sha256(rmom)}:
        raise RuntimeError("rejected residual-momentum input identity changed")
    return {
        "unique_files": len(all_paths),
        "verified_months": len(monthly),
        "all_monthly_aggregates_match": True,
    }


def _load_outcomes(candidate_root: Path, funnel: pl.DataFrame) -> pl.DataFrame:
    labels = pl.concat(
        [
            pl.read_parquet(candidate_root / f"month={month}" / "path_labels.parquet")
            for month, _start, _end in _month_specs()
        ],
        how="diagonal_relaxed",
        rechunk=True,
    )
    validate_path_labels(labels, funnel)
    joined = funnel.filter(pl.col("barebones_accepted")).join(
        labels,
        on="source_key",
        how="inner",
        suffix="_label",
        validate="1:1",
    )
    for column in ("sleeve", "venue", "symbol", "signal_ts_ms", "entry_ts_ms"):
        other = f"{column}_label"
        if other in joined.columns and joined.filter(pl.col(column) != pl.col(other)).height:
            raise RuntimeError(f"funnel/label {column} identity differs")
    return joined.with_columns(
        pl.from_epoch("signal_ts_ms", time_unit="ms").dt.strftime("%Y-%m-%d").alias("signal_date")
    )


def _daily_wave_values(frame: pl.DataFrame, outcome: str) -> dict[str, float]:
    finite = frame.filter(pl.col(outcome).is_not_null() & pl.col(outcome).is_finite())
    if finite.is_empty():
        return {}
    daily = (
        finite.group_by(["signal_date", "decision_ts_ms"])
        .agg(pl.col(outcome).mean().alias("wave_value"))
        .group_by("signal_date")
        .agg(pl.col("wave_value").mean().alias("date_value"))
        .sort("signal_date")
    )
    return {str(day): float(value) for day, value in daily.iter_rows()}


def _bootstrap_mean(values: np.ndarray) -> tuple[float, float]:
    if values.size == 0:
        return math.nan, math.nan
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    output: np.ndarray[Any, np.dtype[np.float64]] = np.empty(BOOTSTRAP_REPLICATES, dtype=float)
    chunk = 250
    for offset in range(0, BOOTSTRAP_REPLICATES, chunk):
        count = min(chunk, BOOTSTRAP_REPLICATES - offset)
        indices = rng.integers(0, values.size, size=(count, values.size), dtype=np.int32)
        output[offset : offset + count] = np.nanmean(values[indices], axis=1)
    return float(np.nanquantile(output, 0.025)), float(np.nanquantile(output, 0.975))


def _path_estimate(frame: pl.DataFrame, outcome: str) -> dict[str, Any]:
    finite = frame.filter(pl.col(outcome).is_not_null() & pl.col(outcome).is_finite())
    daily = _daily_wave_values(frame, outcome)
    values = np.asarray(list(daily.values()), dtype=float)
    low, high = _bootstrap_mean(values)
    return {
        "outcome": outcome,
        "sources": frame.height,
        "finite_sources": finite.height,
        "missing_sources": frame.height - finite.height,
        "waves": finite.select("decision_ts_ms").n_unique() if not finite.is_empty() else 0,
        "dates": len(daily),
        "date_wave_mean": None if not daily else float(values.mean()),
        "block_ci_95": [None if math.isnan(low) else low, None if math.isnan(high) else high],
        "candidate_mean": None if finite.is_empty() else float(cast(float, finite[outcome].mean())),
        "candidate_median": None if finite.is_empty() else float(cast(float, finite[outcome].median())),
        "candidate_win_fraction": (
            None if finite.is_empty() else float(finite.select((pl.col(outcome) > 0.0).mean()).item())
        ),
    }


def _contrast_estimate(
    low_frame: pl.DataFrame,
    high_frame: pl.DataFrame,
    *,
    outcome: str,
) -> dict[str, Any]:
    low_daily = _daily_wave_values(low_frame, outcome)
    high_daily = _daily_wave_values(high_frame, outcome)

    def support(part: pl.DataFrame) -> dict[str, int]:
        part = part.filter(pl.col(outcome).is_not_null() & pl.col(outcome).is_finite())
        return {
            "sources": part.height,
            "waves": part.select("decision_ts_ms").n_unique(),
            "dates": part.select("signal_date").n_unique(),
        }

    low_support, high_support = support(low_frame), support(high_frame)
    if not low_daily or not high_daily:
        return {
            "outcome": outcome,
            "effect_high_minus_low": None,
            "block_ci_95": [None, None],
            "low_support": low_support,
            "high_support": high_support,
            "status": "sparse",
        }
    dates = sorted(set(low_daily) | set(high_daily))
    low_values = np.asarray([low_daily.get(day, np.nan) for day in dates], dtype=float)
    high_values = np.asarray([high_daily.get(day, np.nan) for day in dates], dtype=float)
    point = float(np.nanmean(high_values) - np.nanmean(low_values))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    output: np.ndarray[Any, np.dtype[np.float64]] = np.empty(BOOTSTRAP_REPLICATES, dtype=float)
    chunk = 250
    for offset in range(0, BOOTSTRAP_REPLICATES, chunk):
        count = min(chunk, BOOTSTRAP_REPLICATES - offset)
        indices = rng.integers(0, len(dates), size=(count, len(dates)), dtype=np.int32)
        output[offset : offset + count] = np.nanmean(high_values[indices], axis=1) - np.nanmean(
            low_values[indices], axis=1
        )

    return {
        "outcome": outcome,
        "effect_high_minus_low": point,
        "block_ci_95": [float(np.nanquantile(output, 0.025)), float(np.nanquantile(output, 0.975))],
        "low_support": low_support,
        "high_support": high_support,
        "status": "estimated",
    }


def _long_derived(frame: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in frame.select(
        "source_key",
        "log_return",
        "pump_3d_log",
        "pump_7d_log",
        "pump_threshold_1d",
        "pump_threshold_3d",
        "pump_threshold_7d",
        "pump_trigger_1d",
        "pump_trigger_3d",
        "pump_trigger_7d",
        "close_location",
        "close_loc_3d",
        "close_loc_7d",
    ).iter_rows(named=True):
        candidates_for_row: list[tuple[float, int, float | None]] = []
        for horizon, value_key, threshold_key, trigger_key, close_key in (
            (1, "log_return", "pump_threshold_1d", "pump_trigger_1d", "close_location"),
            (3, "pump_3d_log", "pump_threshold_3d", "pump_trigger_3d", "close_loc_3d"),
            (7, "pump_7d_log", "pump_threshold_7d", "pump_trigger_7d", "close_loc_7d"),
        ):
            value = row[value_key]
            threshold = row[threshold_key]
            if row[trigger_key] and value is not None and threshold is not None and threshold > 0:
                candidates_for_row.append((float(value) / float(threshold), horizon, row[close_key]))
        candidates_for_row.sort(key=lambda item: (-item[0], item[1]))
        strongest = candidates_for_row[0] if candidates_for_row else (math.nan, 0, None)
        rows.append(
            {
                "source_key": row["source_key"],
                "dominant_pump": f"{strongest[1]}d" if strongest[1] else "missing",
                "dominant_close_location": strongest[2],
            }
        )
    return frame.join(pl.from_dicts(rows), on="source_key", how="left", validate="1:1")


def _decorate_characteristics(frame: pl.DataFrame, continuous_config: ContinuousEventConfig) -> pl.DataFrame:
    long = _long_derived(frame.filter(pl.col("sleeve") == "long")).with_columns(
        pl.col("turnover_median_90d").log1p().alias("analysis_turnover"),
        pl.when((pl.col("gate_active_btc_regime") == "pass") & (pl.col("gate_active_eth_regime") == "pass"))
        .then(pl.lit("11"))
        .otherwise(pl.lit("any_off"))
        .alias("analysis_regime"),
        pl.lit(None, dtype=pl.Float64).alias("modeled_cost_bps"),
    )
    continuous = frame.filter(pl.col("sleeve") == "continuous").with_columns(
        pl.col("turnover_quote").log1p().alias("analysis_turnover"),
        pl.when(pl.col("gate_active_btc_trend") == "pass")
        .then(pl.lit("pass"))
        .when(pl.col("gate_active_btc_trend") == "fail")
        .then(pl.lit("fail"))
        .otherwise(pl.lit("missing"))
        .alias("analysis_regime"),
        pl.struct("turnover_quote")
        .map_elements(
            lambda row: _round_trip_bps(
                continuous_config,
                float(row["turnover_quote"]),
                notional_weight=NOTIONAL_WEIGHT,
            ),
            return_dtype=pl.Float64,
        )
        .alias("modeled_cost_bps"),
        pl.when(
            pl.all_horizontal(
                pl.col("active_turn4_pop5").is_null(),
                pl.col("active_turn4_pop3").is_null(),
                pl.col("active_turn3_pop3").is_null(),
            )
        )
        .then(pl.lit("missing"))
        .when(pl.col("active_turn4_pop5") == True)  # noqa: E712
        .then(pl.lit("turn4p5"))
        .when(pl.col("active_turn4_pop3") == True)  # noqa: E712
        .then(pl.lit("turn4p3"))
        .when(pl.col("active_turn3_pop3") == True)  # noqa: E712
        .then(pl.lit("turn3p3"))
        .otherwise(pl.lit("none"))
        .alias("dominant_pump"),
        pl.col("dist_low").alias("dominant_close_location"),
    )
    return pl.concat([long, continuous], how="diagonal_relaxed", rechunk=True)


def _quartile_contrast(
    frame: pl.DataFrame,
    *,
    family: str,
    field: str,
) -> dict[str, Any]:
    finite = frame.filter(pl.col(field).is_not_null() & pl.col(field).is_finite())
    if finite.is_empty():
        return {"family": family, "field": field, "status": "missing"}
    q25 = float(cast(float, finite[field].quantile(0.25, interpolation="linear")))
    q75 = float(cast(float, finite[field].quantile(0.75, interpolation="linear")))
    low = finite.filter(pl.col(field) <= q25)
    high = finite.filter(pl.col(field) >= q75)
    output = {
        "family": family,
        "field": field,
        "status": "estimated",
        "q25": q25,
        "q75": q75,
        "missing_sources": frame.height - finite.height,
        "return_24h": _contrast_estimate(low, high, outcome="return_24h"),
        "return_72h": _contrast_estimate(low, high, outcome="return_72h"),
    }
    for name, start, end in (
        ("early", "2021-05-01", "2023-01-01"),
        ("late", "2023-01-01", "2024-12-01"),
    ):
        output[f"{name}_return_24h"] = _contrast_estimate(
            low.filter((pl.col("signal_date") >= start) & (pl.col("signal_date") < end)),
            high.filter((pl.col("signal_date") >= start) & (pl.col("signal_date") < end)),
            outcome="return_24h",
        )
    return output


def _regime_contrast(frame: pl.DataFrame, sleeve: str) -> dict[str, Any]:
    low_label, high_label = ("any_off", "11") if sleeve == "long" else ("fail", "pass")
    low = frame.filter(pl.col("analysis_regime") == low_label)
    high = frame.filter(pl.col("analysis_regime") == high_label)
    output: dict[str, Any] = {
        "family": "regime",
        "field": "analysis_regime",
        "low": low_label,
        "high": high_label,
        "status": "estimated",
        "return_24h": _contrast_estimate(low, high, outcome="return_24h"),
        "return_72h": _contrast_estimate(low, high, outcome="return_72h"),
    }
    for name, start, end in (
        ("early", "2021-05-01", "2023-01-01"),
        ("late", "2023-01-01", "2024-12-01"),
    ):
        output[f"{name}_return_24h"] = _contrast_estimate(
            low.filter((pl.col("signal_date") >= start) & (pl.col("signal_date") < end)),
            high.filter((pl.col("signal_date") >= start) & (pl.col("signal_date") < end)),
            outcome="return_24h",
        )
    return output


def _funnel_diagnostics(funnel: pl.DataFrame) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for sleeve, gate_order in candidate.REQUIRED_GATE_ORDERS.items():
        part = funnel.filter(pl.col("sleeve") == sleeve)
        attrition: list[dict[str, Any]] = []
        survivors = part
        for gate in gate_order:
            column = f"gate_{gate}"
            counts = {str(state): int(count) for state, count in survivors.group_by(column).len().iter_rows()}
            passed = survivors.filter(pl.col(column) == "pass")
            attrition.append(
                {
                    "gate": gate,
                    "arriving": survivors.height,
                    "states": counts,
                    "surviving": passed.height,
                }
            )
            survivors = passed
        first_rejection = {
            str(value if value is not None else "accepted"): int(count)
            for value, count in part.group_by("first_rejection").len().iter_rows()
        }
        wave_sizes = part.group_by("decision_ts_ms").len()["len"].to_numpy()
        symbol_counts = part.group_by("symbol").len().sort("len", descending=True)
        output[sleeve] = {
            "sources": part.height,
            "accepted": part.filter(pl.col("barebones_accepted")).height,
            "waves": part.select("decision_ts_ms").n_unique(),
            "dates": part.select(pl.from_epoch("signal_ts_ms", time_unit="ms").dt.date().n_unique()).item(),
            "symbols": part.select("symbol").n_unique(),
            "attrition": attrition,
            "first_rejection": first_rejection,
            "wave_size": {
                "median": float(np.median(wave_sizes)),
                "p95": float(np.quantile(wave_sizes, 0.95)),
                "max": int(wave_sizes.max()),
            },
            "top_symbol_source_fraction": (
                0.0 if symbol_counts.is_empty() else float(symbol_counts["len"][0] / part.height)
            ),
        }
    return output


def _characteristic_diagnostics(
    frame: pl.DataFrame,
    continuous_config: ContinuousEventConfig,
) -> dict[str, Any]:
    decorated = _decorate_characteristics(frame, continuous_config)
    field_map = {
        "long": {
            "signal_strength": "source_strength",
            "close_location": "dominant_close_location",
            "volatility_atr": "atr_14d_pct",
            "turnover_liquidity": "analysis_turnover",
            "listing_age": "symbol_age_days",
        },
        "continuous": {
            "signal_strength": "source_composite",
            "close_location": "dominant_close_location",
            "volatility_atr": "rv_168h",
            "turnover_liquidity": "analysis_turnover",
            "listing_age": "archive_age_lower_bound_days",
            "modeled_execution_cost": "modeled_cost_bps",
        },
    }
    output: dict[str, Any] = {}
    for sleeve, fields in field_map.items():
        part = decorated.filter(pl.col("sleeve") == sleeve)
        contrasts = [_quartile_contrast(part, family=family, field=field) for family, field in fields.items()]
        contrasts.append(_regime_contrast(part, sleeve))
        event_levels: dict[str, Any] = {}
        for level in sorted(str(value) for value in part["dominant_pump"].drop_nulls().unique()):
            level_frame = part.filter(pl.col("dominant_pump") == level)
            event_levels[level] = {
                "return_24h": _path_estimate(level_frame, "return_24h"),
                "return_72h": _path_estimate(level_frame, "return_72h"),
            }
        output[sleeve] = {
            "contrasts": contrasts,
            "event_subtype_levels": event_levels,
            "residual_momentum": (
                {"status": "not_applicable"}
                if sleeve == "long"
                else {
                    "status": "provenance_invalid_missing_only",
                    "missing_sources": part.filter(pl.col("residual_momentum").is_null()).height,
                }
            ),
        }
    output["decorated_frame"] = decorated
    return output


@dataclass(slots=True)
class _OpenPosition:
    source_key: str
    sleeve: str
    state: _IndexedTradeState
    last_mark_price: float
    last_mark_ts_ms: int
    boundary_ts_ms: int


def _daily_bucket(
    buckets: dict[tuple[str, int], dict[str, float]],
    sleeve: str,
    ts_ms: int,
) -> dict[str, float]:
    day = (int(ts_ms) // MS_PER_DAY) * MS_PER_DAY
    return buckets.setdefault(
        (sleeve, day),
        {"gross_return": 0.0, "cost_return": 0.0, "funding_return": 0.0},
    )


def _read_kline_day(root: Path, day: dt.date) -> tuple[pl.DataFrame, list[Path]]:
    files = candidate._dataset_files(root, "klines_1h", [day])
    if not files:
        return pl.DataFrame(), []
    frame = pl.read_parquet(
        files,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
        rechunk=True,
    ).filter(candidate._valid_ohlc())
    return frame.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms")), files


def _symbol_bar_ends(
    root: Path,
    *,
    symbol: str,
    day: dt.date,
    cache: dict[tuple[str, dt.date], list[int]],
) -> list[int]:
    key = (symbol, day)
    if key in cache:
        return cache[key]
    partition = root / "klines_1h" / f"date={day.isoformat()}" / f"symbol={encode_symbol_partition(symbol)}"
    files = sorted(partition.rglob("*.parquet")) if partition.is_dir() else []
    if not files:
        cache[key] = []
        return []
    frame = pl.read_parquet(
        files,
        columns=["ts_ms", "open", "high", "low", "close"],
        rechunk=True,
    ).filter(candidate._valid_ohlc())
    cache[key] = sorted(int(value) + MS_PER_HOUR for value in frame["ts_ms"].to_list())
    return cache[key]


def _position_boundary(
    root: Path,
    *,
    sleeve: str,
    symbol: str,
    entry_ts_ms: int,
    planned_exit_ts_ms: int,
    cache: dict[tuple[str, dt.date], list[int]],
) -> int | None:
    start = dt.datetime.fromtimestamp(entry_ts_ms / 1000, tz=dt.timezone.utc).date()
    stop = dt.datetime.fromtimestamp(planned_exit_ts_ms / 1000, tz=dt.timezone.utc).date()
    if sleeve == "long":
        stop += dt.timedelta(days=1)
    ends: list[int] = []
    cursor = start
    while cursor <= stop:
        ends.extend(_symbol_bar_ends(root, symbol=symbol, day=cursor, cache=cache))
        cursor += dt.timedelta(days=1)
    eligible = [value for value in ends if value > entry_ts_ms]
    if not eligible:
        return None
    if sleeve == "long":
        at_or_after = [value for value in eligible if value >= planned_exit_ts_ms]
        return min(at_or_after) if at_or_after else max(eligible)
    at_or_before = [value for value in eligible if value <= planned_exit_ts_ms]
    return max(at_or_before) if at_or_before else None


def _simulate_portfolio(
    funnel: pl.DataFrame,
    *,
    root: Path,
    long_costs: CostConfig,
    continuous_config: ContinuousEventConfig,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], dict[str, float]], dict[str, Any], list[Path]]:
    accepted = funnel.filter(pl.col("barebones_accepted")).sort(
        ["entry_ts_ms", "source_strength", "symbol"],
        descending=[False, True, False],
    )
    entries: dict[int, list[dict[str, Any]]] = {}
    for row in accepted.to_dicts():
        if row.get("entry_ts_ms") is None or row.get("entry_price") is None:
            raise RuntimeError("accepted source lacks its executable entry anchor")
        entries.setdefault(int(row["entry_ts_ms"]), []).append(row)
    open_positions: dict[str, dict[str, _OpenPosition]] = {"long": {}, "continuous": {}}
    trades: list[dict[str, Any]] = []
    daily: dict[tuple[str, int], dict[str, float]] = {}
    processed: set[str] = set()
    files_read: list[Path] = []
    stats: dict[str, dict[str, Any]] = {
        sleeve: {
            "accepted_sources": accepted.filter(pl.col("sleeve") == sleeve).height,
            "admitted": 0,
            "skipped_capacity": 0,
            "skipped_open_symbol": 0,
            "missing_exit_geometry": 0,
            "missing_exit_path": 0,
            "max_open": 0,
            "open_samples": 0,
            "open_sum": 0,
        }
        for sleeve in ("long", "continuous")
    }
    long_round_trip_bps = long_costs.base_entry_exit_cost_bps * 3.0
    lifecycle = {
        "long": TradeLifecycleConfig(hold_days=3, side_mode="long_high_short_low"),
        "continuous": TradeLifecycleConfig(
            hold_days=1,
            take_profit_pct=0.12,
            side_mode="long_low_short_high",
        ),
    }
    symbol_bar_cache: dict[tuple[str, dt.date], list[int]] = {}

    def finalize(position: _OpenPosition) -> None:
        trade = position.state.to_trade()
        trade.update({"source_key": position.source_key, "sleeve": position.sleeve})
        trades.append(trade)

    minimum_entry = min(entries)
    maximum_entry = max(entries)
    first_day = dt.datetime.fromtimestamp(minimum_entry / 1000, tz=dt.timezone.utc).date()
    final_day = dt.datetime.fromtimestamp(maximum_entry / 1000, tz=dt.timezone.utc).date() + dt.timedelta(days=4)
    cursor = first_day
    while cursor <= final_day:
        frame, day_files = _read_kline_day(root, cursor)
        files_read.extend(day_files)
        if frame.is_empty():
            cursor += dt.timedelta(days=1)
            continue
        for part in frame.sort(["bar_end_ts_ms", "symbol"]).partition_by("bar_end_ts_ms", maintain_order=True):
            ts_ms = int(part["bar_end_ts_ms"][0])
            bars = {str(row["symbol"]): row for row in part.to_dicts()}
            for sleeve in ("long", "continuous"):
                closed: list[str] = []
                for symbol, position in sorted(open_positions[sleeve].items()):
                    bar = bars.get(symbol)
                    if bar is None:
                        continue
                    if ts_ms <= position.state.entry_ts_ms:
                        continue
                    did_close = position.state.on_bar(
                        high=float(bar["high"]),
                        low=float(bar["low"]),
                        close=float(bar["close"]),
                        bar_end_ts_ms=ts_ms,
                    )
                    if not did_close and ts_ms >= position.boundary_ts_ms:
                        position.state.close_at_boundary(
                            close=float(bar["close"]),
                            bar_end_ts_ms=ts_ms,
                        )
                        did_close = True
                    mark = float(cast(float, position.state.exit_price)) if did_close else float(bar["close"])
                    sign = 1.0 if position.state.side == "long" else -1.0
                    _daily_bucket(daily, sleeve, ts_ms)["gross_return"] += (
                        NOTIONAL_WEIGHT * sign * (mark - position.last_mark_price) / position.state.entry_price
                    )
                    position.last_mark_price = mark
                    position.last_mark_ts_ms = ts_ms
                    if did_close:
                        finalize(position)
                        closed.append(symbol)
                for symbol in closed:
                    del open_positions[sleeve][symbol]

            candidates_at_ts = entries.get(ts_ms, [])
            for row in sorted(
                candidates_at_ts,
                key=lambda item: (-float(item["source_strength"]), str(item["symbol"])),
            ):
                source_key = str(row["source_key"])
                sleeve = str(row["sleeve"])
                symbol = str(row["symbol"])
                processed.add(source_key)
                if symbol in open_positions[sleeve]:
                    stats[sleeve]["skipped_open_symbol"] += 1
                    continue
                capacity = 10 if sleeve == "long" else 25
                if len(open_positions[sleeve]) >= capacity:
                    stats[sleeve]["skipped_capacity"] += 1
                    continue
                bar = bars.get(symbol)
                if bar is None:
                    raise RuntimeError(f"{source_key} entry bar is absent from the raw tape")
                entry_price = float(row["entry_price"])
                if not math.isclose(
                    float(bar["close"]),
                    entry_price,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ):
                    raise RuntimeError(f"{source_key} entry price differs from the raw close")
                if sleeve == "long":
                    atr = row.get("atr_14d_pct")
                    if atr is None or not math.isfinite(float(atr)) or float(atr) <= 0.0:
                        stats[sleeve]["missing_exit_geometry"] += 1
                        continue
                    stop_price = entry_price * (1.0 - 1.5 * float(atr))
                    target_price = entry_price * (1.0 + 4.0 * float(atr))
                    planned_exit = ts_ms + exact_duration_ms(hours=72)
                    round_trip_bps = long_round_trip_bps
                    side = "long"
                else:
                    stop_price = None
                    target_price = entry_price * (1.0 - 0.12)
                    planned_exit = ts_ms + exact_duration_ms(hours=24)
                    round_trip_bps = _round_trip_bps(
                        continuous_config,
                        float(row["turnover_quote"]),
                        notional_weight=NOTIONAL_WEIGHT,
                    )
                    side = "short"
                boundary_ts_ms = _position_boundary(
                    root,
                    sleeve=sleeve,
                    symbol=symbol,
                    entry_ts_ms=ts_ms,
                    planned_exit_ts_ms=planned_exit,
                    cache=symbol_bar_cache,
                )
                if boundary_ts_ms is None:
                    stats[sleeve]["missing_exit_path"] += 1
                    continue
                state = _IndexedTradeState(
                    symbol=symbol,
                    side=side,
                    score=float(row["source_strength"]),
                    rank=9,
                    basket_id=source_key,
                    signal_ts_ms=int(row["signal_ts_ms"]),
                    entry_ts_ms=ts_ms,
                    entry_price=entry_price,
                    planned_exit_ts_ms=planned_exit,
                    notional_weight=NOTIONAL_WEIGHT,
                    position_weight=1.0,
                    config=lifecycle[sleeve],
                    round_trip_cost_bps=round_trip_bps,
                    stop_price=stop_price,
                    take_profit_price=target_price,
                    rank_lookup={},
                    event_decay_threshold=0.0,
                    funding_lookup=None,
                    stop_fill_mode="bar_extreme_capped",
                    stop_slippage_cap_pct=0.10,
                )
                open_positions[sleeve][symbol] = _OpenPosition(
                    source_key=source_key,
                    sleeve=sleeve,
                    state=state,
                    last_mark_price=entry_price,
                    last_mark_ts_ms=ts_ms,
                    boundary_ts_ms=boundary_ts_ms,
                )
                _daily_bucket(daily, sleeve, ts_ms)["cost_return"] -= NOTIONAL_WEIGHT * round_trip_bps / 10_000.0
                stats[sleeve]["admitted"] += 1
            for sleeve in ("long", "continuous"):
                count = len(open_positions[sleeve])
                stats[sleeve]["max_open"] = max(stats[sleeve]["max_open"], count)
                stats[sleeve]["open_samples"] += 1
                stats[sleeve]["open_sum"] += count
        cursor += dt.timedelta(days=1)
    if any(open_positions[sleeve] for sleeve in open_positions):
        raise RuntimeError("portfolio replay ended with open positions")
    accepted_keys = set(accepted["source_key"].to_list())
    if processed != accepted_keys:
        raise RuntimeError(
            "portfolio replay did not evaluate every accepted source: "
            f"missing={len(accepted_keys - processed)} extra={len(processed - accepted_keys)}"
        )
    for sleeve in stats:
        samples = int(stats[sleeve]["open_samples"])
        stats[sleeve]["mean_open"] = float(stats[sleeve]["open_sum"] / samples) if samples else 0.0
    return trades, daily, stats, sorted(set(files_read))


def _read_funding(
    root: Path,
    *,
    start: dt.date,
    end: dt.date,
) -> tuple[pl.DataFrame, list[Path]]:
    frames: list[pl.DataFrame] = []
    paths: list[Path] = []
    cursor = start
    while cursor < end:
        files = candidate._dataset_files(root, "funding", [cursor])
        if files:
            paths.extend(files)
            frames.append(pl.read_parquet(files, rechunk=True))
        cursor += dt.timedelta(days=1)
    return (
        pl.concat(frames, how="diagonal_relaxed", rechunk=True) if frames else pl.DataFrame(),
        sorted(set(paths)),
    )


def _apply_funding(
    trades: list[dict[str, Any]],
    daily: dict[tuple[str, int], dict[str, float]],
    lookup: dict[str, dict[str, Any]] | None,
) -> None:
    for trade in trades:
        raw, mode, count = _perp_funding_return(
            lookup,
            symbol=str(trade["symbol"]),
            side=str(trade["side"]),
            entry_ts_ms=int(trade["entry_ts_ms"]),
            exit_ts_ms=int(trade["exit_ts_ms"]),
        )
        funding_return = NOTIONAL_WEIGHT * raw
        trade["funding_return"] = funding_return
        trade["funding_mode"] = mode
        trade["funding_event_count"] = count
        trade["net_return"] = float(trade["gross_return"]) + float(trade["cost_return"]) + funding_return
        if lookup is None or str(trade["symbol"]) not in lookup:
            continue
        series = lookup[str(trade["symbol"])]
        timestamps = series["events_ts"]
        lo = bisect.bisect_right(timestamps, int(trade["entry_ts_ms"]))
        hi = bisect.bisect_right(timestamps, int(trade["exit_ts_ms"]))
        sign = -1.0 if trade["side"] == "long" else 1.0
        for ts_ms, rate in zip(timestamps[lo:hi], series["events_rate"][lo:hi]):
            _daily_bucket(daily, str(trade["sleeve"]), int(ts_ms))["funding_return"] += (
                NOTIONAL_WEIGHT * sign * float(rate)
            )


_PORTABLE_ACCOUNT_MUTEX = threading.RLock()
_ORIGINAL_ACCOUNT_WRITE_TRANSACTION = account_kernel_module._write_transaction
_PORTABLE_TRANSACTION_BUFFER: list[tuple[Path, tuple[Any, ...]]] = []


@contextlib.contextmanager
def _portable_account_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
    with _PORTABLE_ACCOUNT_MUTEX:
        yield


def _portable_atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.write_bytes(data) != len(data):
        raise OSError("portable account write made no progress")


def _portable_buffer_transaction(root: str | Path, events: Sequence[Any]) -> Path:
    if not events:
        raise ValueError("cannot buffer an empty account transaction")
    directory = account_kernel_module.account_transactions_path(root)
    directory.mkdir(parents=True, exist_ok=True)
    batch = tuple(events)
    _PORTABLE_TRANSACTION_BUFFER.append((Path(root), batch))
    return directory / f"{batch[0].sequence:020d}-{batch[-1].sequence:020d}.buffered"


def _enable_portable_account_io() -> None:
    setattr(account_kernel_module, "exclusive_file_lock", _portable_account_lock)
    setattr(account_kernel_module, "_atomic_replace", _portable_atomic_replace)
    setattr(account_kernel_module, "_write_transaction", _portable_buffer_transaction)
    setattr(account_kernel_module, "_append_jsonl_projection", lambda *_args, **_kwargs: None)
    setattr(replay_module, "JsonlStrategyEventTape", lambda _path: MemoryStrategyEventTape())


def _account_sample(ledger: pl.DataFrame) -> pl.DataFrame:
    selected: list[pl.DataFrame] = []
    for sleeve in ("long", "continuous"):
        part = ledger.filter(pl.col("sleeve") == sleeve)
        keys = sorted(
            (str(value) for value in part["source_key"]),
            key=lambda key: (hashlib.sha256(key.encode("utf-8")).hexdigest(), key),
        )[:100]
        selected.append(part.filter(pl.col("source_key").is_in(keys)))
    return pl.concat(selected, how="vertical", rechunk=True)


def _replay_account(
    ledger: pl.DataFrame,
    *,
    work_root: Path,
    long_costs: CostConfig,
) -> dict[str, Any]:
    _enable_portable_account_io()
    if _PORTABLE_TRANSACTION_BUFFER:
        raise RuntimeError("portable transaction buffer was not empty at replay start")
    output: dict[str, Any] = {}
    for sleeve in ("long", "continuous"):
        part = ledger.filter(pl.col("sleeve") == sleeve).sort(["entry_ts_ms", "symbol"])
        symbols = sorted(str(value) for value in part["symbol"].unique())
        account_root = work_root / f"account-{sleeve}"
        strategy_id = f"strategy-overhaul-v2-barebones-{sleeve}"
        fee_bps = long_costs.base_entry_exit_cost_bps * 3.0 / 2.0 if sleeve == "long" else 5.5
        policy = AccountRiskPolicy(
            max_component_gross_notional_usdt=CAPITAL_USD * 10.0,
            max_account_gross_notional_usdt=CAPITAL_USD * 100.0,
            max_symbol_notional_usdt=CAPITAL_USD * 10.0,
            max_initial_margin_usdt=CAPITAL_USD * 100.0,
            max_leverage=1.0,
        )
        observed_ts_ns = max(1, int(cast(int, part["entry_ts_ms"].min())) * 1_000_000)
        session = HistoricalAccountSession(
            account_root,
            account_id=strategy_id,
            risk_policy=policy,
            instrument_rules=synthetic_historical_rules_for_symbols(
                symbols,
                max_leverage=1.0,
                observed_ts_ns=observed_ts_ns,
            ),
            execution_config=ExecutionTwinConfig(
                fee_bps=fee_bps,
                latency=LatencyProfile(0, 0, 0),
                max_decision_age_ns=0,
            ),
            id_seed=f"{strategy_id}:historical",
        )
        schedule: dict[int, dict[str, list[HistoricalTargetDecision]]] = {}
        for row in part.to_dicts():
            component_id = str(row["source_key"])
            symbol = str(row["symbol"])
            sign = 1.0 if sleeve == "long" else -1.0
            for action, ts_field, price_field, notional, reason in (
                ("entry", "entry_ts_ms", "entry_price", sign * NOTIONAL_USD, "barebones_entry"),
                ("exit", "exit_ts_ms", "exit_price", 0.0, str(row["exit_reason"])),
            ):
                ts_ms = int(row[ts_field])
                decision = HistoricalTargetDecision(
                    wall_ts_ns=ts_ms * 1_000_000,
                    reference_price=float(row[price_field]),
                    intent=component_target_intent(
                        adapter_kind=(SleeveAdapterKind.LONG if sleeve == "long" else SleeveAdapterKind.CONTINUOUS),
                        action=action,
                        decision_ts_ms=ts_ms,
                        strategy_id=strategy_id,
                        component_id=component_id,
                        symbol=symbol,
                        signed_notional_usdt=notional,
                        leverage=1.0,
                        reason=reason,
                        metadata={
                            "source": "strategy_overhaul_v2_barebones_replay",
                            "signal_ts_ms": int(row.get("entry_signal_ts_ms") or row["entry_ts_ms"]),
                            "signal_valid_until_ms": int(row["entry_ts_ms"]) + MS_PER_HOUR,
                        },
                    ),
                )
                schedule.setdefault(ts_ms, {"exit": [], "entry": []})[action].append(decision)
        prices: dict[str, float] = {}
        accepted_decisions = 0
        for ts_ms in sorted(schedule):
            for action in ("exit", "entry"):
                decisions = schedule[ts_ms][action]
                if not decisions:
                    continue
                for decision in decisions:
                    prices[decision.intent.intent.symbol.upper()] = decision.reference_price
                results = session.submit_decisions(
                    decisions,
                    equity_usdt=CAPITAL_USD,
                    batch_prefix=f"v2-{sleeve}-{action}",
                    market_prices=prices,
                )
                feedback = historical_submission_feedback(results)
                if not feedback.accepted:
                    raise RuntimeError(f"{sleeve} account replay rejected decisions: {feedback.rejection_keys}")
                accepted_decisions += len(decisions)
                if action == "exit":
                    for decision in decisions:
                        prices.pop(decision.intent.intent.symbol.upper(), None)
        if session.kernel is None:
            raise RuntimeError(f"{sleeve} account replay never started")
        state = session.kernel._state_ref()
        nonflat = {
            symbol: position.signed_qty
            for symbol, position in state.positions.items()
            if abs(position.signed_qty) > 1e-12
        }
        if nonflat:
            raise RuntimeError(f"{sleeve} account replay ended non-flat: {nonflat}")
        for transaction_root, buffered_events in _PORTABLE_TRANSACTION_BUFFER:
            if transaction_root != account_root:
                raise RuntimeError("portable transaction buffer crossed account roots")
            _ORIGINAL_ACCOUNT_WRITE_TRANSACTION(account_root, buffered_events)
        _PORTABLE_TRANSACTION_BUFFER.clear()
        events = read_account_journal(account_root, verify=True)
        _portable_atomic_replace(
            account_kernel_module.account_journal_path(account_root),
            b"".join(canonical_json(event.to_dict()) + b"\n" for event in events),
        )
        event_counts: dict[str, int] = {}
        for event in events:
            event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
        expected_fills = part.height * 2
        actual_fills = event_counts.get(AccountEventType.FILL.value, 0)
        if actual_fills != expected_fills:
            raise RuntimeError(f"{sleeve} account replay fill count differs: {actual_fills} != {expected_fills}")
        receipt = verify_account_journal(account_root)
        recorder = session.event_clock.recorder if session.event_clock is not None else None
        output[sleeve] = {
            **receipt,
            "decisions": accepted_decisions,
            "expected_fills": expected_fills,
            "event_counts": event_counts,
            "strategy_event_tape_hash": None if recorder is None else recorder.tape_hash,
            "portable_boundary": "single_process_buffered_direct_materialization_no_durability",
            "final_flat": True,
        }
    return output


def _build_curve(
    ledger: pl.DataFrame,
    daily: Mapping[tuple[str, int], Mapping[str, float]],
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    start = min(day for _sleeve, day in daily)
    end = max(day for _sleeve, day in daily)
    for sleeve in ("long", "continuous"):
        equity = 1.0
        peak = 1.0
        day = start
        while day <= end:
            bucket = daily.get(
                (sleeve, day),
                {"gross_return": 0.0, "cost_return": 0.0, "funding_return": 0.0},
            )
            net = sum(float(bucket[key]) for key in ("gross_return", "cost_return", "funding_return"))
            equity += net
            peak = max(peak, equity)
            rows.append(
                {
                    "sleeve": sleeve,
                    "ts_ms": day,
                    "date": dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat(),
                    "gross_return": float(bucket["gross_return"]),
                    "cost_return": float(bucket["cost_return"]),
                    "funding_return": float(bucket["funding_return"]),
                    "net_return": net,
                    "equity": equity,
                    "drawdown": equity - peak,
                }
            )
            day += MS_PER_DAY
    curve = pl.from_dicts(rows).sort(["sleeve", "ts_ms"])
    for sleeve in ("long", "continuous"):
        trade_net = float(ledger.filter(pl.col("sleeve") == sleeve)["net_return"].sum())
        curve_net = float(curve.filter(pl.col("sleeve") == sleeve)["net_return"].sum())
        if not math.isclose(trade_net, curve_net, rel_tol=1e-10, abs_tol=1e-12):
            raise RuntimeError(f"{sleeve} ledger and MTM curve do not reconcile")
    return curve


def _portfolio_summary(
    ledger: pl.DataFrame,
    curve: pl.DataFrame,
    stats: Mapping[str, Any],
    account: Mapping[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for sleeve in ("long", "continuous"):
        trades = ledger.filter(pl.col("sleeve") == sleeve)
        daily = curve.filter(pl.col("sleeve") == sleeve)
        by_symbol = trades.group_by("symbol").agg(pl.col("net_return").sum()).sort("net_return")
        by_day = daily.sort("net_return")
        exit_reasons = {str(reason): int(count) for reason, count in trades.group_by("exit_reason").len().iter_rows()}
        funding_modes = {str(mode): int(count) for mode, count in trades.group_by("funding_mode").len().iter_rows()}
        output[sleeve] = {
            "trades": trades.height,
            "symbols": trades.select("symbol").n_unique(),
            "net_return": float(trades["net_return"].sum()),
            "gross_return": float(trades["gross_return"].sum()),
            "cost_return": float(trades["cost_return"].sum()),
            "funding_return": float(trades["funding_return"].sum()),
            "max_drawdown": float(cast(float, daily["drawdown"].min())),
            "worst_day_return": float(cast(float, daily["net_return"].min())),
            "worst_day": str(by_day["date"][0]),
            "worst_symbol": None if by_symbol.is_empty() else str(by_symbol["symbol"][0]),
            "worst_symbol_return": (None if by_symbol.is_empty() else float(by_symbol["net_return"][0])),
            "exit_reasons": exit_reasons,
            "funding_modes": funding_modes,
            "selection": stats[sleeve],
            "account": account[sleeve],
        }
    long_entries = ledger.filter(pl.col("sleeve") == "long").select("symbol", "entry_ts_ms")
    continuous_entries = ledger.filter(pl.col("sleeve") == "continuous").select("symbol", "entry_ts_ms")
    output["cross_sleeve"] = {
        "same_symbol_entry_timestamp": long_entries.join(
            continuous_entries,
            on=["symbol", "entry_ts_ms"],
            how="inner",
        ).height,
        "worst_combined_day_return": float(
            cast(float, curve.group_by("date").agg(pl.col("net_return").sum())["net_return"].min())
        ),
    }
    return output


def _candidate_scores(
    characteristics: Mapping[str, Any],
    *,
    long_cost_return: float,
    continuous_cost_return: float,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for sleeve in ("long", "continuous"):
        rows: list[dict[str, Any]] = []
        cost = long_cost_return if sleeve == "long" else continuous_cost_return
        for item in characteristics[sleeve]["contrasts"]:
            if item.get("status") != "estimated":
                continue
            primary = item["return_24h"]
            if primary["effect_high_minus_low"] is None:
                continue
            effect = float(primary["effect_high_minus_low"])
            ci_low, ci_high = (float(value) for value in primary["block_ci_95"])
            supports = [primary["low_support"], primary["high_support"]]
            minimum_sources = min(int(value["sources"]) for value in supports)
            minimum_waves = min(int(value["waves"]) for value in supports)
            minimum_dates = min(int(value["dates"]) for value in supports)
            support_score = (
                2
                if minimum_sources >= 365 and minimum_waves >= 100 and minimum_dates >= 100
                else 1
                if minimum_sources >= 100 and minimum_waves >= 30 and minimum_dates >= 30
                else 0
            )
            keeps_sign = ci_low > 0.0 or ci_high < 0.0
            economic_score = 2 if abs(effect) > cost and keeps_sign else 1 if abs(effect) > cost else 0
            early = item["early_return_24h"]["effect_high_minus_low"]
            late = item["late_return_24h"]["effect_high_minus_low"]
            same_sign = early is not None and late is not None and np.sign(early) == np.sign(effect) == np.sign(late)
            half_magnitude = (
                early is not None
                and late is not None
                and abs(early) >= 0.5 * abs(effect)
                and abs(late) >= 0.5 * abs(effect)
            )
            temporal_score = 2 if same_sign and half_magnitude else 1 if same_sign else 0
            prior_penalty = -2 if sleeve == "continuous" and item["family"] == "regime" else 0
            regime_no_change = sleeve == "long" and item["family"] == "regime" and effect >= 0.0
            total = 2 + support_score + economic_score + temporal_score + 1 + prior_penalty
            selection_blocked = economic_score == 0 or regime_no_change or sleeve == "continuous"
            eligible = total >= 6 and support_score >= 1 and not selection_blocked
            rows.append(
                {
                    "family": item["family"],
                    "field": item["field"],
                    "effect_24h": effect,
                    "effect_cost_ratio": abs(effect) / cost if cost > 0 else None,
                    "identification_score": 2,
                    "support_score": support_score,
                    "economic_score": economic_score,
                    "temporal_score": temporal_score,
                    "implementation_score": 1,
                    "prior_penalty": prior_penalty,
                    "total_score": total,
                    "eligible": eligible,
                    "ineligible_reason": (
                        "current profile comparator unavailable: invalid RMOM provenance"
                        if sleeve == "continuous"
                        else "effect does not exceed modeled cost"
                        if economic_score == 0
                        else "no profile change when the active regime side is favored"
                        if regime_no_change
                        else None
                    ),
                    "discovery_constants": {key: item[key] for key in ("q25", "q75", "low", "high") if key in item},
                }
            )
        eligible_rows = [row for row in rows if row["eligible"]]
        eligible_rows.sort(
            key=lambda row: (
                -int(row["total_score"]),
                -float(row["effect_cost_ratio"] or 0.0),
                [
                    "signal_strength",
                    "close_location",
                    "volatility_atr",
                    "turnover_liquidity",
                    "listing_age",
                    "regime",
                    "modeled_execution_cost",
                ].index(str(row["family"])),
            )
        )
        output[sleeve] = {
            "considered": rows,
            "selected_mechanical_candidate": eligible_rows[0] if eligible_rows else None,
            "comparator_available": sleeve == "long",
        }
    return output


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items() if key != "decorated_frame"}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_bytes(canonical_payload(_json_ready(dict(payload))) + b"\n")


def _validate_existing_output(out: Path) -> dict[str, Any]:
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        raise FileExistsError(f"analysis output exists without a manifest: {out}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, identity in manifest.get("files", {}).items():
        path = out / name
        if not path.is_file() or _sha256(path) != identity.get("sha256"):
            raise RuntimeError(f"existing analysis output failed identity check: {path}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=COMPLETION_CONTRACT)
    parser.add_argument("--base-contract", type=Path, default=BASE_CONTRACT)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve(strict=True)
    candidate_root = args.candidate_root.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    contract = args.contract.expanduser().resolve(strict=True)
    base_contract = args.base_contract.expanduser().resolve(strict=True)
    recovery_contract = RECOVERY_CONTRACT.resolve(strict=True)
    buffered_recovery_contract = BUFFERED_RECOVERY_CONTRACT.resolve(strict=True)
    if _sha256(contract) != EXPECTED_COMPLETION_CONTRACT_SHA256:
        raise RuntimeError("completion contract identity differs from the registered bytes")
    if _sha256(base_contract) != EXPECTED_BASE_CONTRACT_SHA256:
        raise RuntimeError("base diagnostic contract identity differs from the registered bytes")
    if _sha256(recovery_contract) != EXPECTED_RECOVERY_CONTRACT_SHA256:
        raise RuntimeError("recovery contract identity differs from the registered bytes")
    if _sha256(buffered_recovery_contract) != EXPECTED_BUFFERED_RECOVERY_CONTRACT_SHA256:
        raise RuntimeError("buffered recovery contract identity differs from the registered bytes")
    started = time.perf_counter()
    manifests = _load_candidate_manifests(candidate_root)
    funnel, _label_structure, structural = _structural_frames(candidate_root)
    preflight = {
        "schema_version": 1,
        "mode": "preflight",
        "root": str(root),
        "candidate_root": str(candidate_root),
        "out": str(out),
        "contract": str(contract),
        "contract_sha256": _sha256(contract),
        "base_contract": str(base_contract),
        "base_contract_sha256": _sha256(base_contract),
        "recovery_contract": str(recovery_contract),
        "recovery_contract_sha256": _sha256(recovery_contract),
        "buffered_recovery_contract": str(buffered_recovery_contract),
        "buffered_recovery_contract_sha256": _sha256(buffered_recovery_contract),
        "candidate_manifests": len(manifests),
        "candidate_manifest_file_sha256": [
            _sha256(_manifest_path(candidate_root, month)) for month, _start, _end in _month_specs()
        ],
        "structural": structural,
        "outcome_columns_read": [],
        "holdout_touched": False,
        "runs_active_backtest": False,
        "writes_data_root": False,
    }
    if args.preflight:
        print(json.dumps(preflight, sort_keys=True))
        return 0
    dirty = bool(_git("status", "--porcelain=v1"))
    if dirty and not args.allow_dirty:
        raise RuntimeError("Phase-3 analysis requires clean code")
    if out.exists():
        print(json.dumps(_validate_existing_output(out), sort_keys=True))
        return 0

    raw_identity = _verify_raw_candidate_inputs(root, manifests)
    funding, funding_files = _read_funding(
        root,
        start=DISCOVERY_START - dt.timedelta(days=10),
        end=DISCOVERY_END + dt.timedelta(days=14),
    )
    funding_identity = candidate._aggregate_file_identity(funding_files, relative_to=root)
    funding_lookup = _funding_lookup(funding)
    outcomes_opened_at = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    joined = _load_outcomes(candidate_root, funnel)

    config = load_config(REPO / "configs/volume_alpha.default.yaml")
    long_costs = config.costs
    continuous_config = ContinuousEventConfig(
        component_key="strategy_overhaul_v2_barebones",
        start_date=DISCOVERY_START.isoformat(),
        end_date=DISCOVERY_END.isoformat(),
        side="short",
        entry_delay_hours=1,
        hold_hours=24,
        take_profit_pct=0.12,
        gross_exposure=0.25,
        max_active=25,
        taker_fee_bps=5.5,
        spread_bps=2.5,
        impact_coef_bps=50.0,
        impact_exponent=0.5,
        deploy_capital_usd=CAPITAL_USD,
        sizing_mode="flat",
        age_days_min=0,
        btc_trend_gate="off",
        entry_crowding_max_fresh=0,
        use_funding=True,
    )
    characteristics = _characteristic_diagnostics(joined, continuous_config)
    path = {
        sleeve: {
            **{
                f"return_{hours}h": _path_estimate(
                    joined.filter(pl.col("sleeve") == sleeve),
                    f"return_{hours}h",
                )
                for hours in HORIZONS
            },
            "mae_72h": _path_estimate(joined.filter(pl.col("sleeve") == sleeve), "mae_72h"),
            "mfe_72h": _path_estimate(joined.filter(pl.col("sleeve") == sleeve), "mfe_72h"),
        }
        for sleeve in ("long", "continuous")
    }
    trade_rows, daily, portfolio_stats, kline_files = _simulate_portfolio(
        funnel,
        root=root,
        long_costs=long_costs,
        continuous_config=continuous_config,
    )
    _apply_funding(trade_rows, daily, funding_lookup)
    ledger = pl.from_dicts(trade_rows, infer_schema_length=None).sort(["sleeve", "entry_ts_ms", "symbol"])
    curve = _build_curve(ledger, daily)
    work = out.with_name(f".{out.name}.working")
    if work.exists():
        raise FileExistsError(f"analysis working directory already exists: {work}")
    work.mkdir(parents=True)
    account_sample = _account_sample(ledger)
    account = _replay_account(
        account_sample,
        work_root=work,
        long_costs=long_costs,
    )
    for sleeve in ("long", "continuous"):
        full_part = ledger.filter(pl.col("sleeve") == sleeve)
        sample_part = account_sample.filter(pl.col("sleeve") == sleeve)
        sample_identity = hashlib.sha256(
            canonical_json({"source_keys": sorted(str(value) for value in sample_part["source_key"])})
        ).hexdigest()
        account[sleeve].update(
            scope="bounded_key_sample_100_per_sleeve",
            full_ledger_trades=full_part.height,
            sampled_trades=sample_part.height,
            sample_source_keys_sha256=sample_identity,
        )
    portfolio = _portfolio_summary(ledger, curve, portfolio_stats, account)
    median_continuous_cost = float(
        characteristics["decorated_frame"].filter(pl.col("sleeve") == "continuous")["modeled_cost_bps"].median()
        / 10_000.0
    )
    selection = _candidate_scores(
        characteristics,
        long_cost_return=long_costs.base_entry_exit_cost_bps * 3.0 / 10_000.0,
        continuous_cost_return=median_continuous_cost,
    )
    diagnostics = _json_ready(
        {
            "schema_version": 1,
            "kind": "strategy_overhaul_v2_phase3_diagnostics",
            "study_mode": "exploratory",
            "integrity": {
                **structural,
                "raw_candidate_inputs": raw_identity,
                "baseline_comparison_enabled": False,
                "baseline_missing_files": 23,
            },
            "funnel": _funnel_diagnostics(funnel),
            "path": path,
            "characteristics": characteristics,
            "portfolio": portfolio,
            "phase4_selection": selection,
            "tested_variants": {
                "horizons": list(HORIZONS),
                "characteristic_families": [
                    "signal_strength",
                    "close_location",
                    "volatility_atr",
                    "turnover_liquidity",
                    "listing_age",
                    "regime",
                    "residual_momentum_missingness",
                    "event_subtype_levels",
                    "modeled_execution_cost",
                ],
                "bootstrap_replicates": BOOTSTRAP_REPLICATES,
                "bootstrap_seed": BOOTSTRAP_SEED,
                "unregistered_variants": [],
            },
            "explicit_non_conclusions": [
                "exploratory discovery does not establish alpha",
                "active artifact comparison remains disabled because all 23 payloads are absent",
                "CONTINUOUS current-profile thesis selection is disabled by invalid RMOM provenance",
                "historical costs are modeled, not calibrated execution TCA",
                "portable account I/O proves neither crash/POSIX durability nor deployment parity",
                "full ledger/curve values are not exhaustively account-replayed; account proof is a key-only sample",
                "no demo, mainnet, size, capital, or real-money authority",
            ],
        }
    )

    staging = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    staging.mkdir(parents=True)
    try:
        diagnostics_path = staging / "diagnostics.json"
        ledger_path = staging / "barebones_ledger.parquet"
        curve_path = staging / "barebones_curve.parquet"
        _write_json(diagnostics_path, diagnostics)
        ledger.write_parquet(ledger_path, compression="zstd", statistics=True)
        curve.write_parquet(curve_path, compression="zstd", statistics=True)
        account_transactions = sorted(work.rglob("*.json"))
        account_identity = candidate._aggregate_file_identity(
            account_transactions,
            relative_to=work,
        )
        run_identity = {
            **preflight,
            "mode": "build",
            "code_commit": _git("rev-parse", "HEAD"),
            "git_dirty": dirty,
            "cost_config": {
                "path": "configs/volume_alpha.default.yaml",
                "sha256": _sha256(REPO / "configs/volume_alpha.default.yaml"),
                "long_round_trip_bps": long_costs.base_entry_exit_cost_bps * 3.0,
                "continuous_taker_bps_per_side": 5.5,
                "continuous_spread_bps_per_side": 2.5,
                "continuous_impact_formula": "50 * sqrt(10000 / signal_turnover) per side",
            },
            "funding_identity": funding_identity,
            "portfolio_kline_files_read": len(kline_files),
            "portfolio_kline_identity_covered_by_candidate_manifests": True,
            "account_transaction_identity": account_identity,
            "account_scope": "bounded_key_sample_100_per_sleeve",
            "account_sample_source_keys_sha256": {
                sleeve: account[sleeve]["sample_source_keys_sha256"] for sleeve in ("long", "continuous")
            },
            "portable_account_boundary": "single_process_buffered_direct_materialization_no_durability",
            "crash_durability_claim": False,
        }
        manifest_payload: dict[str, Any] = {
            "schema_version": 1,
            "kind": "strategy_overhaul_v2_phase3_analysis",
            "study_mode": "exploratory",
            "run_identity": run_identity,
            "run_identity_sha256": payload_sha256(run_identity),
            "outcomes_inspected": True,
            "outcomes_first_opened_at_utc": outcomes_opened_at,
            "holdout_touched": False,
            "elapsed_seconds": time.perf_counter() - started,
            "counts": {
                "funnel_rows": funnel.height,
                "path_rows": joined.height,
                "portfolio_trades": ledger.height,
                "curve_rows": curve.height,
            },
            "files": {
                "diagnostics.json": {
                    "bytes": diagnostics_path.stat().st_size,
                    "sha256": _sha256(diagnostics_path),
                },
                "barebones_ledger.parquet": {
                    "bytes": ledger_path.stat().st_size,
                    "sha256": _sha256(ledger_path),
                },
                "barebones_curve.parquet": {
                    "bytes": curve_path.stat().st_size,
                    "sha256": _sha256(curve_path),
                },
            },
            "explicit_non_conclusions": diagnostics["explicit_non_conclusions"],
        }
        manifest_payload["manifest_payload_sha256"] = payload_sha256(manifest_payload)
        _write_json(staging / "manifest.json", manifest_payload)
        out.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(manifest_payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
