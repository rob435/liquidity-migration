#!/usr/bin/env python3
"""Run the registered V2 comparator provenance/accounting repair gates.

This command is deliberately outcome-blind.  It rebuilds the single registered
RMOM artifact and validates it against the legacy spent-window values, or it
replays the already-published barebones ledger through the production account
kernel.  It never reads or creates a V2 holdout payload.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, cast

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Importing the candidate/analyzer module first installs its fail-closed fcntl
# import guard on Windows.  The account phase later installs the analyzer's
# explicit single-process research adapter before it touches a journal.
import scripts.analyze_strategy_overhaul_v2 as phase3  # noqa: E402
import scripts.build_candidate_tape as candidate  # noqa: E402
import scripts.precompute_residual_momentum as rmom_owner  # noqa: E402
from liquidity_migration import storage  # noqa: E402
from liquidity_migration.account_kernel import (  # noqa: E402
    AccountEventType,
    read_account_journal,
)
from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    require_stable_residual_momentum,
)
from liquidity_migration.daily_feature_panel import (  # noqa: E402
    _autodetect_dataset_names,
)
from liquidity_migration.strategy_funnel import payload_sha256  # noqa: E402


REPAIR_ID = "strategy-overhaul-v2-comparator-accounting-repair-2026-07-18"
CONTRACT = REPO / "docs/preregistration/strategy_overhaul_v2_comparator_accounting_repair_2026-07-18.md"
EXPECTED_CONTRACT_SHA256 = "9c6fb8383d09ad143c86784623f23000b932432b834eea024a85da71f89ec191"
PHASE3_ROOT = REPO / "reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/phase3-analysis"
PHASE3_MANIFEST = PHASE3_ROOT / "manifest.json"
PHASE3_DIAGNOSTICS = PHASE3_ROOT / "diagnostics.json"
BAREBONES_LEDGER = PHASE3_ROOT / "barebones_ledger.parquet"
EXPECTED_PHASE3_MANIFEST_SHA256 = "48c34b7612eb7a0d3e8603908df0633b8640705f4cd571c483577fbab2465269"
EXPECTED_PHASE3_DIAGNOSTICS_SHA256 = "5fbcf06904454ca39ad3138bfc0cc80acb4f59658ccd58293f38783e87910274"
EXPECTED_BAREBONES_LEDGER_SHA256 = "368a7c04640dd362179d4c00897948d036ce38dc6136da12eedd47b4b6c64ddd"
EXPECTED_LEGACY_RMOM_SHA256 = "547259477d4d33d70e904a0226366338695d503794e7856aa72fb9c6079d9f6f"
EXPECTED_TRADE_COUNTS = {"long": 1_899, "continuous": 16_745}
EXPECTED_SAMPLE = {
    "long": {
        "events": 1_858,
        "fills": 200,
        "final_state_hash": "d826ef2bcca5490dd01c0543a289db4b45b3463ee5fce096052a04bac1c9a717",
        "last_event_hash": "970b5c64d5f8428c430b4c07a5570c1ad005a9400c59a6d88f6c78f0e06390a9",
        "strategy_event_tape_hash": "abfa7cb3b33ca5f2629b32ced160661b6a825bbc6c019223754c7ddaa4dee1c9",
        "original_kernel_transactions": 685,
        "source_keys_sha256": "7bb36dfd0132162052e5de2bddbb26284dc1620ac2811052b765c296737c27a9",
    },
    "continuous": {
        "events": 1_824,
        "fills": 200,
        "final_state_hash": "bf56c2b99be20a1bf88816e9941cbd38603dca66a05a01ebc1f2df4a5556c876",
        "last_event_hash": "db14cab13fffc53aa2a212fa157de54818c7fb51bc8b682a6dce549e363d98cb",
        "strategy_event_tape_hash": "c4b18b318e0f62b328ff61c93bff36dee9b5b21515d22ce9afcfb5c2067adf75",
        "original_kernel_transactions": 694,
        "source_keys_sha256": "8e8bb7dba15cab08cb341add272cbaf44e12a5979e406fc547566f992bb02032",
    },
}
RMOM_START = dt.date(2023, 3, 1)
RMOM_END = dt.date(2024, 12, 5)
COMPARATOR_START = dt.date(2023, 7, 17)
COMPARATOR_END = dt.date(2024, 12, 1)
DEFAULT_DATA_ROOT = Path.home() / "SHARED_DATA/bybit_full_pit"
DEFAULT_OUT = REPO / "reports/strategy-overhaul-v2/comparator-accounting-repair-2026-07-18"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _date_ms(value: dt.date) -> int:
    return int(dt.datetime.combine(value, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)


def _assert_registered_inputs(data_root: Path) -> dict[str, Any]:
    identities = {
        "contract": (CONTRACT, EXPECTED_CONTRACT_SHA256),
        "phase3_manifest": (PHASE3_MANIFEST, EXPECTED_PHASE3_MANIFEST_SHA256),
        "phase3_diagnostics": (PHASE3_DIAGNOSTICS, EXPECTED_PHASE3_DIAGNOSTICS_SHA256),
        "barebones_ledger": (BAREBONES_LEDGER, EXPECTED_BAREBONES_LEDGER_SHA256),
        "legacy_rmom": (data_root / "residual_momentum.parquet", EXPECTED_LEGACY_RMOM_SHA256),
    }
    receipt: dict[str, Any] = {}
    for name, (path, expected) in identities.items():
        if not path.is_file():
            raise FileNotFoundError(f"registered {name} input is absent: {path}")
        actual = _sha256(path)
        if actual != expected:
            raise RuntimeError(f"registered {name} identity changed: {actual} != {expected}")
        receipt[name] = {"path": str(path), "bytes": path.stat().st_size, "sha256": actual}
    return receipt


def _assert_holdout_absent() -> Path:
    holdout = PHASE3_ROOT.parent / "bybit/holdout"
    if holdout.exists():
        raise RuntimeError(f"registered holdout boundary is not absent: {holdout}")
    return holdout


def _base_identity(data_root: Path, out: Path) -> dict[str, Any]:
    holdout = _assert_holdout_absent()
    return {
        "schema_version": 1,
        "repair_id": REPAIR_ID,
        "code_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain=v1")),
        "data_root": str(data_root),
        "out": str(out),
        "registered_inputs": _assert_registered_inputs(data_root),
        "holdout_path_checked_absent": str(holdout),
        "holdout_touched": False,
        "writes_data_root": False,
    }


@contextlib.contextmanager
def _portable_readonly_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
    """Windows-only adapter for a frozen historical root.

    The influencing file hashes are captured, and the legacy shared feature is
    re-hashed after the build.  This grants no concurrent-reader/writer claim.
    """

    yield


def _install_windows_readonly_adapter() -> str:
    if os.name != "nt":
        return "native_posix_dataset_read_lock"
    storage.exclusive_file_lock = _portable_readonly_lock
    # daily_feature_panel imported the function object directly.
    import liquidity_migration.daily_feature_panel as daily_panel

    daily_panel.exclusive_file_lock = _portable_readonly_lock  # type: ignore[attr-defined]
    return "single_process_windows_readonly_no_flock_input_hash_guard"


def _rmom_logical_input_identities(data_root: Path) -> dict[str, Any]:
    """Hash the date partitions that can influence the registered RMOM output."""

    names = _autodetect_dataset_names(data_root)
    # BTC beta reads 90 days of kline warm-up.  The common feature builder
    # reads 60 days of warm-up and three forward days for fwd_ret_1d.
    kline_dates = candidate._date_range(RMOM_START - dt.timedelta(days=90), RMOM_END + dt.timedelta(days=3))
    feature_dates = candidate._date_range(RMOM_START - dt.timedelta(days=60), RMOM_END + dt.timedelta(days=3))
    requested = {
        names["klines_dataset"]: kline_dates,
        names["funding_dataset"]: feature_dates,
        names["open_interest_dataset"]: feature_dates,
        names["premium_dataset"]: feature_dates,
    }
    identities: dict[str, Any] = {}
    for dataset, dates in requested.items():
        files = candidate._dataset_files(data_root, dataset, dates)
        identities[dataset] = {
            **candidate._aggregate_file_identity(files, relative_to=data_root),
            "logical_start": dates[0].isoformat(),
            "logical_end_exclusive": (dates[-1] + dt.timedelta(days=1)).isoformat(),
        }
    return identities


def _compare_rmom_to_legacy(
    rebuilt: pl.DataFrame,
    legacy: pl.DataFrame,
) -> dict[str, Any]:
    stable = require_stable_residual_momentum(rebuilt, source="rebuilt run-scoped RMOM")
    start_ms = _date_ms(COMPARATOR_START)
    end_ms = _date_ms(COMPARATOR_END)
    rebuilt_window = (
        stable.filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
        .select("symbol", "ts_ms", pl.col("residual_momentum").alias("rebuilt_value"))
        .sort(["symbol", "ts_ms"])
    )
    required_legacy = {"symbol", "ts_ms", "residual_momentum"}
    missing = sorted(required_legacy - set(legacy.columns))
    if missing:
        raise RuntimeError(f"legacy RMOM is missing comparison columns: {missing}")
    legacy_window = (
        legacy.filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
        .select("symbol", "ts_ms", pl.col("residual_momentum").alias("legacy_value"))
        .sort(["symbol", "ts_ms"])
    )
    if rebuilt_window.is_empty() or legacy_window.is_empty():
        raise RuntimeError("RMOM comparison window is empty")
    rebuilt_only = rebuilt_window.join(legacy_window, on=["symbol", "ts_ms"], how="anti")
    legacy_only = legacy_window.join(rebuilt_window, on=["symbol", "ts_ms"], how="anti")
    if not rebuilt_only.is_empty() or not legacy_only.is_empty():
        raise RuntimeError(
            "RMOM stable-key sets differ in the comparator window: "
            f"rebuilt_only={rebuilt_only.height}, legacy_only={legacy_only.height}"
        )
    joined = rebuilt_window.join(legacy_window, on=["symbol", "ts_ms"], how="inner").sort(
        ["symbol", "ts_ms"]
    )
    rebuilt_values = joined["rebuilt_value"].to_numpy()
    legacy_values = joined["legacy_value"].to_numpy()
    rebuilt_nonfinite = ~np.isfinite(rebuilt_values)
    legacy_nonfinite = ~np.isfinite(legacy_values)
    if not np.array_equal(rebuilt_nonfinite, legacy_nonfinite):
        raise RuntimeError("RMOM legacy/rebuilt non-finite positions differ")
    finite = ~rebuilt_nonfinite
    if finite.any() and not np.allclose(
        rebuilt_values[finite], legacy_values[finite], rtol=1e-10, atol=1e-12
    ):
        observed_max_abs_diff = float(
            np.max(np.abs(rebuilt_values[finite] - legacy_values[finite]))
        )
        raise RuntimeError(
            f"RMOM legacy/rebuilt values differ: max_abs_diff={observed_max_abs_diff:.12g}"
        )
    max_abs_diff: float | None = (
        float(np.max(np.abs(rebuilt_values[finite] - legacy_values[finite])))
        if finite.any()
        else None
    )
    return {
        "status": "pass",
        "stable_rows_total": stable.height,
        "rebuilt_rows_in_comparator_window": rebuilt_window.height,
        "legacy_rows_in_comparator_window": legacy_window.height,
        "matching_keys": joined.height,
        "nonfinite_positions": int(rebuilt_nonfinite.sum()),
        "rtol": 1e-10,
        "atol": 1e-12,
        "max_abs_diff": max_abs_diff,
    }


def _validate_existing_receipt(path: Path, *, artifact: Path) -> dict[str, Any]:
    payload = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    recorded = payload.get("artifact") or {}
    if not artifact.is_file() or recorded.get("sha256") != _sha256(artifact):
        raise RuntimeError(f"existing receipt does not validate its artifact: {path}")
    return payload


def run_rmom(data_root: Path, out: Path) -> dict[str, Any]:
    identity = _base_identity(data_root, out)
    if identity["git_dirty"]:
        raise RuntimeError("RMOM repair requires a clean code commit")
    rmom_dir = out / "rmom"
    artifact = rmom_dir / "residual_momentum.parquet"
    receipt_path = rmom_dir / "receipt.json"
    if receipt_path.exists():
        return _validate_existing_receipt(receipt_path, artifact=artifact)

    rmom_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    built_now = False
    if not artifact.exists():
        adapter = _install_windows_readonly_adapter()
        rmom_owner.precompute(
            data_root,
            start=RMOM_START.isoformat(),
            end=RMOM_END.isoformat(),
            klines_dataset="klines_1h",
            append=False,
            output_path=artifact,
        )
        built_now = True
    else:
        adapter = "resume_validation_of_preserved_single_build_artifact"

    rebuilt = pl.read_parquet(artifact)
    legacy_path = data_root / "residual_momentum.parquet"
    comparison = _compare_rmom_to_legacy(rebuilt, pl.read_parquet(legacy_path))
    if _sha256(legacy_path) != EXPECTED_LEGACY_RMOM_SHA256:
        raise RuntimeError("shared-root legacy RMOM changed during the run-scoped rebuild")
    raw_inputs = _rmom_logical_input_identities(data_root)
    artifact_identity = {
        "path": str(artifact),
        "bytes": artifact.stat().st_size,
        "sha256": _sha256(artifact),
        "rows": rebuilt.height,
        "schema": {name: str(dtype) for name, dtype in rebuilt.schema.items()},
    }
    receipt: dict[str, Any] = {
        **identity,
        "kind": "strategy_overhaul_v2_rmom_repair",
        "study_mode": "outcome_blind_provenance_validation",
        "command_phase": "rmom",
        "built_now": built_now,
        "formula": {
            "start": RMOM_START.isoformat(),
            "end_exclusive": RMOM_END.isoformat(),
            "factor_columns": list(rmom_owner.COMMON4),
            "rolling_window_days": rmom_owner.RMOM_WINDOW,
            "minimum_observations": rmom_owner.RMOM_MIN_SAMPLES,
            "causal_shift_days": rmom_owner.RMOM_CAUSAL_SHIFT,
        },
        "read_adapter": adapter,
        "raw_logical_input_identities": raw_inputs,
        "comparison": comparison,
        "artifact": artifact_identity,
        "elapsed_seconds": time.perf_counter() - started,
        "outcomes_opened": False,
        "explicit_non_conclusions": [
            "legacy agreement validates spent-window values but cannot repair legacy provenance",
            "this artifact alone does not establish an exact active comparator",
            "no alpha, deployment, execution-calibration, mainnet, or real-money claim",
        ],
    }
    receipt["receipt_payload_sha256"] = payload_sha256(receipt)
    _write_json(receipt_path, receipt)
    return receipt


def _load_and_validate_ledger() -> pl.DataFrame:
    ledger = pl.read_parquet(BAREBONES_LEDGER).sort(["sleeve", "entry_ts_ms", "symbol"])
    actual_counts = {
        sleeve: ledger.filter(pl.col("sleeve") == sleeve).height
        for sleeve in EXPECTED_TRADE_COUNTS
    }
    if actual_counts != EXPECTED_TRADE_COUNTS:
        raise RuntimeError(f"barebones ledger counts changed: {actual_counts}")
    for sleeve, expected in EXPECTED_TRADE_COUNTS.items():
        part = ledger.filter(pl.col("sleeve") == sleeve)
        if part["source_key"].n_unique() != expected or part["trade_id"].n_unique() != expected:
            raise RuntimeError(f"{sleeve} ledger keys are not one-to-one")
    net_error = ledger.select(
        (
            pl.col("net_return")
            - pl.col("gross_return")
            - pl.col("cost_return")
            - pl.col("funding_return")
        )
        .abs()
        .max()
    ).item()
    gross_error = ledger.select(
        (
            pl.col("gross_return")
            - pl.col("gross_trade_return")
            * pl.col("notional_weight")
            * pl.col("position_weight")
        )
        .abs()
        .max()
    ).item()
    if float(net_error or 0.0) > 1e-12 or float(gross_error or 0.0) > 1e-12:
        raise RuntimeError(
            f"barebones ledger P&L identities failed: net={net_error}, gross={gross_error}"
        )
    return ledger


def _source_key_sha256(part: pl.DataFrame) -> str:
    return hashlib.sha256(
        phase3.canonical_json({"source_keys": sorted(str(value) for value in part["source_key"])})
    ).hexdigest()


def _validate_event_coverage(
    ledger: pl.DataFrame,
    *,
    account_root: Path,
    sleeve: str,
) -> dict[str, Any]:
    part = ledger.filter(pl.col("sleeve") == sleeve)
    expected_keys = {str(value) for value in part["source_key"]}
    expected_gross = {
        str(row["source_key"]): float(row["gross_return"]) * phase3.CAPITAL_USD
        for row in part.select("source_key", "gross_return").to_dicts()
    }
    events = read_account_journal(account_root, verify=True)
    decisions: Counter[str] = Counter()
    pnl_counts: Counter[str] = Counter()
    actual_gross: dict[str, float] = {}
    for event in events:
        if event.event_type == AccountEventType.DECISION.value:
            component_id = str(event.payload.get("component_id") or "")
            if component_id:
                decisions[component_id] += 1
        elif event.event_type == AccountEventType.PNL.value:
            metadata = event.payload.get("metadata") or {}
            component_ids = metadata.get("component_ids") or () if isinstance(metadata, Mapping) else ()
            if not isinstance(component_ids, Sequence) or isinstance(component_ids, (str, bytes)):
                raise RuntimeError(f"{sleeve} P&L event lacks component attribution")
            ids = [str(value) for value in component_ids]
            for component_id in ids:
                pnl_counts[component_id] += 1
            if ids:
                per_component_expected = sum(expected_gross[component_id] for component_id in ids)
                observed = float(event.payload["gross_pnl_usdt"])
                if not np.isclose(observed, per_component_expected, rtol=1e-12, atol=1e-12):
                    raise RuntimeError(
                        f"{sleeve} account/ledger gross P&L differs for {ids}: "
                        f"{observed} != {per_component_expected}"
                    )
                for component_id in ids:
                    actual_gross[component_id] = expected_gross[component_id]
    if set(decisions) != expected_keys or any(decisions[key] != 2 for key in expected_keys):
        raise RuntimeError(f"{sleeve} account decision/source-key coverage differs")
    if set(pnl_counts) != expected_keys or any(pnl_counts[key] != 1 for key in expected_keys):
        raise RuntimeError(f"{sleeve} account P&L/source-key coverage differs")
    if set(actual_gross) != expected_keys:
        raise RuntimeError(f"{sleeve} account gross-P&L coverage differs")
    return {
        "source_keys": len(expected_keys),
        "decision_events": sum(decisions.values()),
        "pnl_attributions": sum(pnl_counts.values()),
        "account_ledger_gross_pnl_rtol": 1e-12,
        "account_ledger_gross_pnl_atol": 1e-12,
        "status": "pass",
    }


def _compare_sample_receipts(
    sample: pl.DataFrame,
    receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for sleeve, expected in EXPECTED_SAMPLE.items():
        receipt = receipts[sleeve]
        part = sample.filter(pl.col("sleeve") == sleeve)
        actual = {
            "events": receipt["events"],
            "fills": receipt["event_counts"][AccountEventType.FILL.value],
            "final_state_hash": receipt["final_state_hash"],
            "last_event_hash": receipt["last_event_hash"],
            "strategy_event_tape_hash": receipt["strategy_event_tape_hash"],
            "original_kernel_transactions": receipt["original_kernel_transactions"],
            "source_keys_sha256": _source_key_sha256(part),
        }
        differences = {
            key: {"actual": actual.get(key), "expected": value}
            for key, value in expected.items()
            if actual.get(key) != value
        }
        if differences:
            raise RuntimeError(f"{sleeve} frozen sample regression differs: {differences}")
        result[sleeve] = {**actual, "status": "pass"}
    return result


def _account_archive_identity(root: Path) -> dict[str, Any]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    return candidate._aggregate_file_identity(files, relative_to=root)


def run_account(data_root: Path, out: Path) -> dict[str, Any]:
    identity = _base_identity(data_root, out)
    if identity["git_dirty"]:
        raise RuntimeError("account repair requires a clean code commit")
    ledger = _load_and_validate_ledger()
    config = load_config(REPO / "configs/volume_alpha.default.yaml")

    sample_final = out / "account-sample-regression"
    sample_receipt_path = sample_final / "receipt.json"
    if not sample_receipt_path.exists():
        if sample_final.exists():
            raise FileExistsError(f"preserved incomplete sample root requires inspection: {sample_final}")
        sample_work = out / ".account-sample-regression.working"
        if sample_work.exists():
            raise FileExistsError(f"preserved incomplete sample work requires inspection: {sample_work}")
        sample_work.mkdir(parents=True)
        sample = phase3._account_sample(ledger)
        sample_receipts = phase3._replay_account(
            sample,
            work_root=sample_work,
            long_costs=config.costs,
        )
        sample_regression = _compare_sample_receipts(sample, sample_receipts)
        sample_coverage = {
            sleeve: _validate_event_coverage(
                sample,
                account_root=sample_work / f"account-{sleeve}",
                sleeve=sleeve,
            )
            for sleeve in EXPECTED_TRADE_COUNTS
        }
        sample_payload: dict[str, Any] = {
            "kind": "strategy_overhaul_v2_frozen_account_sample_regression",
            "code_commit": identity["code_commit"],
            "sample_regression": sample_regression,
            "coverage": sample_coverage,
            "archive_identity_before_receipt": _account_archive_identity(sample_work),
            "holdout_touched": False,
        }
        sample_payload["receipt_payload_sha256"] = payload_sha256(sample_payload)
        _write_json(sample_work / "receipt.json", sample_payload)
        sample_work.replace(sample_final)
    sample_payload = cast(dict[str, Any], json.loads(sample_receipt_path.read_text(encoding="utf-8")))

    full_final = out / "full-account-replay"
    full_receipt_path = full_final / "receipt.json"
    if full_receipt_path.exists():
        return cast(dict[str, Any], json.loads(full_receipt_path.read_text(encoding="utf-8")))
    if full_final.exists():
        raise FileExistsError(f"preserved incomplete full root requires inspection: {full_final}")
    full_work = out / ".full-account-replay.working"
    if full_work.exists():
        raise FileExistsError(
            "a full replay attempt already exists; the registered one-attempt boundary forbids retry "
            f"without a new outcome-blind repair contract: {full_work}"
        )
    full_work.mkdir(parents=True)
    attempt_started_utc = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    _write_json(
        full_work / "attempt.json",
        {
            "repair_id": REPAIR_ID,
            "code_commit": identity["code_commit"],
            "started_at_utc": attempt_started_utc,
            "maximum_measured_seconds": 7_200,
            "attempt_number": 1,
            "holdout_touched": False,
        },
    )
    started = time.perf_counter()
    receipts = phase3._replay_account(
        ledger,
        work_root=full_work,
        long_costs=config.costs,
    )
    elapsed = time.perf_counter() - started
    if elapsed > 7_200:
        raise RuntimeError(f"full account replay exceeded its registered two-hour cap: {elapsed:.3f}s")
    coverage = {
        sleeve: _validate_event_coverage(
            ledger,
            account_root=full_work / f"account-{sleeve}",
            sleeve=sleeve,
        )
        for sleeve in EXPECTED_TRADE_COUNTS
    }
    for sleeve, trade_count in EXPECTED_TRADE_COUNTS.items():
        receipt = receipts[sleeve]
        required = {
            "decisions": trade_count * 2,
            "expected_fills": trade_count * 2,
            "final_flat": True,
        }
        differences = {
            key: {"actual": receipt.get(key), "expected": value}
            for key, value in required.items()
            if receipt.get(key) != value
        }
        if differences:
            raise RuntimeError(f"{sleeve} full account replay differs: {differences}")
        journal_path = Path(str(receipt.pop("journal_path")))
        receipt["journal_path_relative_to_archive"] = journal_path.relative_to(full_work).as_posix()
    payload: dict[str, Any] = {
        **identity,
        "kind": "strategy_overhaul_v2_full_account_replay",
        "study_mode": "outcome_blind_account_integrity_validation",
        "command_phase": "account",
        "attempt_started_at_utc": attempt_started_utc,
        "attempts": 1,
        "elapsed_seconds": elapsed,
        "maximum_measured_seconds": 7_200,
        "ledger_pnl_identity": {
            "net_equals_gross_plus_cost_plus_funding": True,
            "gross_equals_trade_return_times_weights": True,
            "rtol": 1e-12,
            "atol": 1e-12,
        },
        "frozen_sample_receipt_sha256": _sha256(sample_receipt_path),
        "frozen_sample_receipt_payload_sha256": sample_payload["receipt_payload_sha256"],
        "account_receipts": receipts,
        "coverage": coverage,
        "archive_identity_before_receipt": _account_archive_identity(full_work),
        "outcomes_opened": False,
        "explicit_non_conclusions": [
            "portable compaction proves no POSIX crash durability or concurrent-writer behavior",
            "gross P&L is reconciled to the published ledger; modeled cost and funding remain separate ledger fields",
            "this replay does not establish an exact active comparator or calibrated venue execution",
            "no alpha, deployment, mainnet, or real-money claim",
        ],
    }
    payload["receipt_payload_sha256"] = payload_sha256(payload)
    _write_json(full_work / "receipt.json", payload)
    full_work.replace(full_final)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("preflight", "rmom", "account"), required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = args.root.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    identity = _base_identity(data_root, out)
    if args.phase == "preflight":
        print(json.dumps({**identity, "phase": "preflight"}, sort_keys=True))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    payload = run_rmom(data_root, out) if args.phase == "rmom" else run_account(data_root, out)
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
