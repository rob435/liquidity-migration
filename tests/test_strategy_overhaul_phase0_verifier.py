"""Integration tests for strict Phase-0 semantic verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from liquidity_migration import strategy_overhaul_phase0 as phase0
from liquidity_migration import strategy_overhaul_phase0_verifier as verifier
from liquidity_migration.strategy_overhaul_phase0 import SleeveWindow
from liquidity_migration.strategy_overhaul_phase0_verifier import (
    Phase0BundleVerificationError,
    verify_phase0_bundle,
)
from liquidity_migration.strategy_overhaul_root_snapshot import (
    RootSnapshotError,
    RootSnapshotWindow,
    build_root_snapshot,
)
from scripts import strategy_overhaul_scout_2026_07_10 as scout


START = "2026-01-01"
SIGNAL_END = "2026-01-03"
LABEL_END = "2026-01-04"
WINDOWS = (
    SleeveWindow("continuous", START, START, SIGNAL_END),
    SleeveWindow("long", START, "2026-01-02", SIGNAL_END),
)
ROOT_WINDOW = RootSnapshotWindow(START, START, SIGNAL_END, LABEL_END)


def _write_root(root: Path, *, venue: str) -> Path:
    source = "bybit_public_trading_archive" if venue == "bybit" else "binance_public_data_archive"
    rmom_rows: list[dict[str, Any]] = []
    for day_ordinal, day in enumerate(("2026-01-01", "2026-01-02", "2026-01-03")):
        start_ms = 1_767_225_600_000 + day_ordinal * 86_400_000
        kline = root / "klines_1h" / f"date={day}" / "symbol=AAAUSDT" / "part.parquet"
        kline.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["AAAUSDT"] * 24,
                "ts_ms": [start_ms + hour * 3_600_000 for hour in range(24)],
                "open": [100.0 + hour for hour in range(24)],
                "high": [101.0 + hour for hour in range(24)],
                "low": [99.0 + hour for hour in range(24)],
                "close": [100.5 + hour for hour in range(24)],
                "volume_base": [1_000.0 + hour for hour in range(24)],
                "turnover_quote": [100_000.0 + hour for hour in range(24)],
                "source": [source] * 24,
            }
        ).write_parquet(kline)
        rmom_rows.extend(
            {
                "symbol": "AAAUSDT",
                "ts_ms": start_ms + hour * 3_600_000,
                "residual_momentum": float(hour) / 100.0,
                "is_provisional": False,
                "source": source,
            }
            for hour in range(24)
        )
        if day < SIGNAL_END:
            manifest = root / "archive_trade_manifest" / f"date={day}" / "part.parquet"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame(
                {
                    "symbol": ["AAAUSDT"],
                    "date": [day],
                    "url": [f"fixture://{venue}/{day}/AAAUSDT"],
                    "source": [source],
                    "membership_source": [source],
                    "membership_inferred": [False],
                    "first_archive_observed_date": [day],
                }
            ).write_parquet(manifest)
    pl.DataFrame(rmom_rows).write_parquet(root / "residual_momentum.parquet")
    return root


@pytest.fixture(scope="module")
def ready_phase0(tmp_path_factory: pytest.TempPathFactory):
    tmp_path = tmp_path_factory.mktemp("strict-phase0")
    roots = {venue: _write_root(tmp_path / venue, venue=venue) for venue in ("bybit", "binance")}
    patch = pytest.MonkeyPatch()
    # Other metadata-focused tests can make the editable project distribution
    # visible through duplicate finders in the same pytest process.  Freeze one
    # exact object per identical normalized-name/version/location identity here;
    # production still fails closed on genuinely conflicting duplicates.
    installed: dict[tuple[str, str, str], Any] = {}
    for distribution in scout.importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or getattr(distribution, "name", None)
        if name:
            key = (
                scout._normalized_distribution_name(str(name)),
                str(distribution.version),
                str(Path(distribution.locate_file("")).resolve()),
            )
            installed.setdefault(key, distribution)
    stable_distributions = list(installed.values())
    patch.setattr(scout.importlib.metadata, "distributions", lambda: list(stable_distributions))
    patch.setattr(phase0, "REGISTERED_SLEEVE_WINDOWS", WINDOWS)
    patch.setattr(verifier, "REGISTERED_SLEEVE_WINDOWS", WINDOWS)
    patch.setattr(scout, "ROOT_START_DATE", START)
    patch.setattr(scout, "CONTINUOUS_READ_START_DATE", START)
    patch.setattr(scout, "CONTINUOUS_START_DATE", START)
    patch.setattr(scout, "LONG_READ_START_DATE", START)
    patch.setattr(scout, "LONG_START_DATE", "2026-01-02")
    patch.setattr(scout, "SIGNAL_END_DATE", SIGNAL_END)
    patch.setattr(scout, "LABEL_END_DATE", LABEL_END)
    output = tmp_path / "phase0-output"
    supplied_roots = dict(roots)
    for venue, root in roots.items():
        raw = str(root)
        alias = Path(raw.removeprefix("/private")) if raw.startswith("/private/var/") else root
        if alias.exists() and alias.resolve() == root.resolve():
            supplied_roots[venue] = alias
    args = argparse.Namespace(
        deep_root_hash=False,
        write_plan=None,
        output_root=output,
        bybit_root=supplied_roots["bybit"],
        binance_root=supplied_roots["binance"],
        instrument_map=None,
        instrument_map_version=None,
        batch_size=16,
    )
    assert scout.run_phase0_inventory(args) == 0
    receipts = list(output.glob("*/receipt.json"))
    assert len(receipts) == 1
    try:
        yield receipts[0], roots, tmp_path
    finally:
        patch.undo()


def _rewrite_json_artifact(receipt_path: Path, name: str, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    (receipt_path.parent / name).write_bytes(data)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    row = next(row for row in receipt["files"] if row["path"] == name)
    row["sha256"] = hashlib.sha256(data).hexdigest()
    row["bytes"] = len(data)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_strict_verifier_recomputes_ready_bundle_and_root_scope(ready_phase0) -> None:
    receipt, roots, _tmp_path = ready_phase0

    result = verify_phase0_bundle(
        receipt,
        expected_venue="bybit",
        expected_root=roots["bybit"],
        expected_window={
            "identity_history_start_date": START,
            "causal_read_start_date": START,
            "signal_end_date_exclusive": SIGNAL_END,
            "label_end_date_exclusive": LABEL_END,
        },
    )

    assert result.phase0_internal_reexecution_verified is True
    assert result.phase0_semantics_fully_verified is False
    assert result.source_authenticity_proven is False
    assert result.full_process_environment_identity_proven is False
    assert result.upstream_root_lineage_proven is False
    assert result.outcome_values_read is False
    assert result.outcome_run_authorized is False
    assert result.receipt["readiness_status"] == "READY"
    for venue, root in roots.items():
        plan_root = result.command_plan["roots"][venue]
        assert plan_root["root"] == str(root.resolve())
        assert plan_root["residual_momentum"]["path"] == str(root.resolve() / "residual_momentum.parquet")
    snapshot = build_root_snapshot(
        roots["bybit"],
        venue="bybit",
        window=ROOT_WINDOW,
        phase0_bundle_receipt=receipt,
    )
    assert snapshot.receipt["status"] == "BYTE_SNAPSHOT_ONLY"
    assert snapshot.receipt["phase0_internal_reexecution_verified"] is True
    assert snapshot.receipt["phase0_semantics_fully_verified"] is False
    assert snapshot.receipt["registered_scope_verified"] is False
    assert snapshot.receipt["earliest_root_history_proven"] is False
    assert snapshot.receipt["registered_s01_ready"] is False


def test_writer_blocks_swapped_venue_contents_and_same_physical_roots(ready_phase0) -> None:
    _receipt, roots, tmp_path = ready_phase0
    swapped_output = tmp_path / "swapped-output"
    swapped_args = argparse.Namespace(
        deep_root_hash=False,
        write_plan=None,
        output_root=swapped_output,
        bybit_root=roots["binance"],
        binance_root=roots["bybit"],
        instrument_map=None,
        instrument_map_version=None,
        batch_size=16,
    )
    assert scout.run_phase0_inventory(swapped_args) == 2
    swapped_receipt = next(swapped_output.glob("*/receipt.json"))
    swapped_inventory = json.loads((swapped_receipt.parent / "phase0_inventory.json").read_text(encoding="utf-8"))
    assert swapped_inventory["readiness"]["status"] == "NOT_READY"
    assert swapped_inventory["root_lineage"]["all_venue_source_labels_compatible"] is False
    with pytest.raises(Phase0BundleVerificationError, match="readiness_status=READY"):
        verify_phase0_bundle(swapped_receipt)

    external_map = tmp_path / "counterfeit-map.json"
    external_map.write_text(json.dumps({"version": "counterfeit", "entries": []}), encoding="utf-8")
    same_root_values = vars(swapped_args).copy()
    same_root_values.update(
        output_root=tmp_path / "same-root-output",
        bybit_root=roots["bybit"],
        binance_root=roots["bybit"],
        instrument_map=external_map,
    )
    with pytest.raises(phase0.Phase0IntegrityError, match="same physical directory"):
        scout.run_phase0_inventory(argparse.Namespace(**same_root_values))


def test_external_map_cannot_self_assert_product_or_portability_trust(ready_phase0) -> None:
    _receipt, roots, _tmp_path = ready_phase0
    entries = [
        {
            "canonical_instrument": "COUNTERFEIT::NOT_AAA",
            "venue": venue,
            "symbol": "AAAUSDT",
            "valid_from_date": START,
            "base_asset": "ZZZ",
            "quote_asset": "EUR",
            "settlement_asset": "GOLD",
            "contract_type": "invented",
            "contract_multiplier": 999.0,
            "mapping_source": "self_asserted_no_receipt",
            "review_status": "reviewed",
        }
        for venue in ("bybit", "binance")
    ]
    artifact = phase0.build_phase0_artifacts(
        roots,
        start_date=START,
        end_date_exclusive=SIGNAL_END,
        instrument_map=entries,
        instrument_map_version="counterfeit-v1",
        instrument_map_authority="external_untrusted",
        sleeve_windows=WINDOWS,
    )
    coverage = artifact["instrument_map_coverage"]
    assert coverage["status"] == "diagnostic_untrusted"
    assert coverage["self_asserted_all_entries_reviewed"] is True
    assert coverage["external_review_status_trusted"] is False
    assert coverage["venue_local_identity_ready"] is False
    assert coverage["portable_matching_ready"] is False
    assert coverage["trusted_reviewer_bound_receipt_present"] is False


def test_label_tail_bytes_do_not_become_registered_scope_evidence(ready_phase0) -> None:
    receipt, roots, _tmp_path = ready_phase0
    tail = roots["bybit"] / "klines_1h" / "date=2026-01-03" / "symbol=AAAUSDT" / "part.parquet"
    original = tail.read_bytes()
    tail.write_bytes(b"arbitrary non-parquet label-tail bytes")
    try:
        snapshot = build_root_snapshot(
            roots["bybit"],
            venue="bybit",
            window=ROOT_WINDOW,
            phase0_bundle_receipt=receipt,
        )
        assert snapshot.receipt["status"] == "BYTE_SNAPSHOT_ONLY"
        assert snapshot.receipt["registered_scope_verified"] is False
        assert snapshot.receipt["registered_s01_ready"] is False
    finally:
        tail.write_bytes(original)


def test_strict_verifier_rejects_unrelated_root_and_window(ready_phase0) -> None:
    receipt, roots, _tmp_path = ready_phase0

    with pytest.raises(Phase0BundleVerificationError, match="root mismatch"):
        verify_phase0_bundle(
            receipt,
            expected_venue="bybit",
            expected_root=roots["binance"],
            expected_window={
                "identity_history_start_date": START,
                "causal_read_start_date": START,
                "signal_end_date_exclusive": SIGNAL_END,
                "label_end_date_exclusive": LABEL_END,
            },
        )
    with pytest.raises(Phase0BundleVerificationError, match="outside the exact registered"):
        verify_phase0_bundle(
            receipt,
            expected_venue="bybit",
            expected_root=roots["bybit"],
            expected_window={
                "identity_history_start_date": START,
                "causal_read_start_date": START,
                "signal_end_date_exclusive": "2026-01-02",
                "label_end_date_exclusive": LABEL_END,
            },
        )


def test_strict_verifier_rejects_root_drift_even_with_untouched_bundle(ready_phase0) -> None:
    receipt, roots, _tmp_path = ready_phase0
    manifest = roots["bybit"] / "archive_trade_manifest" / "date=2026-01-02" / "part.parquet"
    original = manifest.read_bytes()
    frame = pl.read_parquet(manifest).with_columns(pl.lit("BBBUSDT").alias("symbol"))
    frame.write_parquet(manifest)
    try:
        with pytest.raises(Phase0BundleVerificationError, match="instrument_map_input|fresh outcome-blind scan"):
            verify_phase0_bundle(receipt)
    finally:
        manifest.write_bytes(original)


def test_strict_verifier_rejects_rehashed_environment_fabrication(ready_phase0) -> None:
    receipt, _roots, tmp_path = ready_phase0
    copied = tmp_path / "fabricated-environment" / receipt.parent.name
    shutil.copytree(receipt.parent, copied)
    copied_receipt = copied / "receipt.json"
    environment = json.loads((copied / "environment_manifest.json").read_text(encoding="utf-8"))
    environment["platform"]["machine"] = "fabricated-machine"
    _rewrite_json_artifact(copied_receipt, "environment_manifest.json", environment)

    with pytest.raises(Phase0BundleVerificationError, match="environment does not equal"):
        verify_phase0_bundle(copied_receipt)


def test_root_snapshot_rejects_truncated_local_identity_history(ready_phase0) -> None:
    receipt, roots, _tmp_path = ready_phase0
    added: list[Path] = []
    for dataset in ("klines_1h", "archive_trade_manifest"):
        source = next((roots["bybit"] / dataset / "date=2026-01-01").rglob("*.parquet"))
        target = roots["bybit"] / dataset / "date=2025-12-31" / "part.parquet"
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())
        added.append(target)
    try:
        with pytest.raises(RootSnapshotError, match="identity_history_start_date must equal the earliest local"):
            build_root_snapshot(
                roots["bybit"],
                venue="bybit",
                window=ROOT_WINDOW,
                phase0_bundle_receipt=receipt,
            )
    finally:
        for path in added:
            path.unlink()
            path.parent.rmdir()
