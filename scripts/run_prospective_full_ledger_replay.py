#!/usr/bin/env python3
"""Run the registered complete account-ledger replay with unit-aware checks."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _install_import_only_windows_fcntl_guard() -> None:
    if os.name != "nt" or "fcntl" in sys.modules:
        return
    module = types.ModuleType("fcntl")
    module.LOCK_SH = 1  # type: ignore[attr-defined]
    module.LOCK_EX = 2  # type: ignore[attr-defined]
    module.LOCK_NB = 4  # type: ignore[attr-defined]
    module.LOCK_UN = 8  # type: ignore[attr-defined]

    def _forbidden_flock(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("prospective historical replay must use its explicit portable adapter")

    module.flock = _forbidden_flock  # type: ignore[attr-defined]
    sys.modules["fcntl"] = module


_install_import_only_windows_fcntl_guard()

import scripts.analyze_strategy_overhaul_v2 as phase3  # noqa: E402
from liquidity_migration.account_kernel import (  # noqa: E402
    AccountEvent,
    AccountEventType,
    read_account_journal,
)
from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.strategy_funnel import payload_sha256  # noqa: E402
from liquidity_migration.unit_numeric_comparison import (  # noqa: E402
    NumericComparison,
    NumericUnit,
    compare_numeric,
    summarize_comparisons,
)

EPOCH_ROOT = REPO / "reports/prospective-runtime-parity-execution-epoch-2026-07-18"
BASE_CONTRACT = REPO / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18.md"
AMENDMENTS = REPO / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_amendments.md"
BAREBONES_LEDGER = (
    REPO
    / "reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/phase3-analysis/barebones_ledger.parquet"
)
PHASE3_MANIFEST = BAREBONES_LEDGER.with_name("manifest.json")
PHASE3_DIAGNOSTICS = BAREBONES_LEDGER.with_name("diagnostics.json")
FEATURE_RECEIPT = EPOCH_ROOT / "features/bybit-baseline/feature_receipt.json"
ACCOUNT_REPLAY_CONFIG = REPO / "configs/volume_alpha.default.yaml"
DEFAULT_OUT = EPOCH_ROOT / "ledger-parity/full-account-replay"

EXPECTED_BASE_CONTRACT_SHA256 = "15edc498adf2bd068c33ff2f791fa3e46f161196db673a839adcf317aba35a31"
EXPECTED_BAREBONES_LEDGER_SHA256 = "368a7c04640dd362179d4c00897948d036ce38dc6136da12eedd47b4b6c64ddd"
EXPECTED_PHASE3_MANIFEST_SHA256 = "48c34b7612eb7a0d3e8603908df0633b8640705f4cd571c483577fbab2465269"
EXPECTED_PHASE3_DIAGNOSTICS_SHA256 = "5fbcf06904454ca39ad3138bfc0cc80acb4f59658ccd58293f38783e87910274"
EXPECTED_FEATURE_RECEIPT_SHA256 = "1d50aeb731e0cc82a1963d57576f032228df5b375dbdb20375c01541d397af31"
EXPECTED_AMENDMENTS_SHA256 = "3494faba073e069ee4b69ebbace657f4211ce73437df2ad34210e5847b68cf98"
EXPECTED_ACCOUNT_REPLAY_CONFIG_SHA256 = "cc0cd0c651c207c45bc9856691021167634c91b20c40fc67a2987a7a1f8dd24c"
EXPECTED_TRADE_COUNTS = {"long": 1_899, "continuous": 16_745}
CALIBRATION_KEYS_PER_SLEEVE = 200
CAPITAL_USDT = 1_000_000.0
ENTRY_NOTIONAL_USDT = 10_000.0


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
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


def _write_json_create(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"create-only replay artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _write_parquet_create(path: Path, frame: pl.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(f"create-only replay artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _registered_inputs() -> dict[str, dict[str, Any]]:
    expected = {
        "base_contract": (BASE_CONTRACT, EXPECTED_BASE_CONTRACT_SHA256),
        "barebones_ledger": (BAREBONES_LEDGER, EXPECTED_BAREBONES_LEDGER_SHA256),
        "phase3_manifest": (PHASE3_MANIFEST, EXPECTED_PHASE3_MANIFEST_SHA256),
        "phase3_diagnostics": (PHASE3_DIAGNOSTICS, EXPECTED_PHASE3_DIAGNOSTICS_SHA256),
        "feature_receipt": (FEATURE_RECEIPT, EXPECTED_FEATURE_RECEIPT_SHA256),
        "account_replay_config": (
            ACCOUNT_REPLAY_CONFIG,
            EXPECTED_ACCOUNT_REPLAY_CONFIG_SHA256,
        ),
    }
    output: dict[str, dict[str, Any]] = {}
    for name, (path, expected_sha) in expected.items():
        resolved = path.resolve(strict=True)
        actual = _sha256(resolved)
        if actual != expected_sha:
            raise RuntimeError(f"registered {name} identity changed: {actual} != {expected_sha}")
        output[name] = {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": actual}
    amendment = AMENDMENTS.resolve(strict=True)
    amendment_sha = _sha256(amendment)
    if amendment_sha != EXPECTED_AMENDMENTS_SHA256:
        raise RuntimeError(
            "registered amendments identity changed: "
            f"{amendment_sha} != {EXPECTED_AMENDMENTS_SHA256}"
        )
    output["amendments"] = {
        "path": str(amendment),
        "bytes": amendment.stat().st_size,
        "sha256": amendment_sha,
    }
    return output


def _load_ledger() -> pl.DataFrame:
    ledger = pl.read_parquet(BAREBONES_LEDGER).sort(["sleeve", "entry_ts_ms", "symbol"])
    actual_counts = {
        sleeve: ledger.filter(pl.col("sleeve") == sleeve).height
        for sleeve in EXPECTED_TRADE_COUNTS
    }
    if actual_counts != EXPECTED_TRADE_COUNTS:
        raise RuntimeError(f"barebones ledger counts changed: {actual_counts}")
    required = {
        "sleeve",
        "source_key",
        "trade_id",
        "symbol",
        "entry_ts_ms",
        "exit_ts_ms",
        "entry_price",
        "exit_price",
        "gross_return",
    }
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise RuntimeError(f"barebones ledger lacks replay columns: {missing}")
    for sleeve, expected_count in EXPECTED_TRADE_COUNTS.items():
        part = ledger.filter(pl.col("sleeve") == sleeve)
        if part["source_key"].n_unique() != expected_count or part["trade_id"].n_unique() != expected_count:
            raise RuntimeError(f"{sleeve} ledger keys are not one-to-one")
    return ledger


def _calibration_keys(ledger: pl.DataFrame) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    for sleeve in EXPECTED_TRADE_COUNTS:
        keys = [str(value) for value in ledger.filter(pl.col("sleeve") == sleeve)["source_key"]]
        output[sleeve] = set(
            sorted(
                keys,
                key=lambda key: (hashlib.sha256(key.encode("utf-8")).hexdigest(), key),
            )[:CALIBRATION_KEYS_PER_SLEEVE]
        )
    return output


def _events_by_type(events: Sequence[AccountEvent]) -> dict[str, list[AccountEvent]]:
    grouped: dict[str, list[AccountEvent]] = collections.defaultdict(list)
    for event in events:
        grouped[event.event_type].append(event)
    return dict(grouped)


def _component_ids(event: AccountEvent) -> tuple[str, ...]:
    metadata = event.payload.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise RuntimeError(f"{event.event_type} event metadata is not a mapping")
    values = metadata.get("component_ids") or ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise RuntimeError(f"{event.event_type} event component_ids are invalid")
    return tuple(str(value) for value in values)


class _ComparisonBook:
    def __init__(self) -> None:
        self.by_field: dict[str, list[NumericComparison]] = collections.defaultdict(list)
        self.by_key: dict[str, list[NumericComparison]] = collections.defaultdict(list)
        self.failures: list[dict[str, Any]] = []

    def add(
        self,
        *,
        key: str,
        field: str,
        left: float,
        right: float,
        unit: NumericUnit,
    ) -> None:
        result = compare_numeric(left, right, unit=unit)
        self.by_field[field].append(result)
        self.by_key[key].append(result)
        if not result.passed:
            self.failures.append({"source_key": key, "field": field, **result.to_dict()})

    def summary(self) -> dict[str, Any]:
        return {
            "fields": {
                field: summarize_comparisons(rows) for field, rows in sorted(self.by_field.items())
            },
            "all": summarize_comparisons(
                row for rows in self.by_field.values() for row in rows
            ),
        }


def _validate_sleeve(
    ledger: pl.DataFrame,
    *,
    sleeve: str,
    account_root: Path,
    calibration_keys: set[str],
    replay_receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], pl.DataFrame, list[dict[str, Any]]]:
    part = ledger.filter(pl.col("sleeve") == sleeve)
    rows = {str(row["source_key"]): row for row in part.to_dicts()}
    expected_keys = set(rows)
    events = read_account_journal(account_root, verify=True)
    grouped = _events_by_type(events)
    decisions: dict[str, list[AccountEvent]] = collections.defaultdict(list)
    targets: dict[str, list[AccountEvent]] = collections.defaultdict(list)
    target_components: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for event in grouped.get(AccountEventType.DECISION.value, []):
        component = str(event.payload.get("component_id") or "")
        if component:
            decisions[component].append(event)
    for event in grouped.get(AccountEventType.TARGET.value, []):
        component = str(event.payload.get("component_id") or "")
        if component:
            targets[component].append(event)
            target_components[
                (str(event.payload.get("batch_id") or ""), str(event.payload.get("symbol") or ""))
            ].add(component)

    command_events = grouped.get(AccountEventType.ORDER_COMMAND.value, [])
    commands = {str(event.payload["command_id"]): event for event in command_events}
    duplicate_command_ids = len(command_events) - len(commands)
    fills_by_command: dict[str, list[AccountEvent]] = collections.defaultdict(list)
    for event in grouped.get(AccountEventType.FILL.value, []):
        fills_by_command[str(event.payload.get("command_id") or "")].append(event)
    acks_by_command: collections.Counter[str] = collections.Counter(
        str(event.payload.get("command_id") or "")
        for event in grouped.get(AccountEventType.ACK.value, [])
    )
    statuses_by_command: collections.Counter[str] = collections.Counter(
        str(event.payload.get("command_id") or "")
        for event in grouped.get(AccountEventType.ORDER_STATUS.value, [])
    )
    command_by_component: dict[str, list[tuple[AccountEvent, AccountEvent]]] = collections.defaultdict(list)
    for command_id, command in commands.items():
        command_key = (
            str(command.payload.get("batch_id") or ""),
            str(command.payload.get("symbol") or ""),
        )
        components = target_components.get(command_key, set())
        if len(components) != 1:
            raise RuntimeError(
                f"{sleeve} command {command_id} maps to {len(components)} components"
            )
        fills = fills_by_command.get(command_id, [])
        if len(fills) != 1:
            raise RuntimeError(f"{sleeve} command {command_id} has {len(fills)} fills")
        command_by_component[next(iter(components))].append((command, fills[0]))

    closes: dict[str, list[AccountEvent]] = collections.defaultdict(list)
    pnls: dict[str, list[AccountEvent]] = collections.defaultdict(list)
    for event in grouped.get(AccountEventType.CLOSE.value, []):
        for component in _component_ids(event):
            closes[component].append(event)
    for event in grouped.get(AccountEventType.PNL.value, []):
        for component in _component_ids(event):
            pnls[component].append(event)

    discrete_failures: list[dict[str, Any]] = []
    book = _ComparisonBook()
    key_rows: list[dict[str, Any]] = []
    for key in sorted(expected_keys):
        expected = rows[key]
        key_discrete: list[str] = []

        def require(label: str, condition: bool) -> None:
            if not condition:
                key_discrete.append(label)

        key_decisions = sorted(decisions.get(key, []), key=lambda event: event.sequence)
        key_targets = sorted(targets.get(key, []), key=lambda event: event.sequence)
        key_commands = sorted(command_by_component.get(key, []), key=lambda item: item[0].sequence)
        key_closes = closes.get(key, [])
        key_pnls = pnls.get(key, [])
        require("decision_count", len(key_decisions) == 2)
        require("target_count", len(key_targets) == 2)
        require("command_fill_count", len(key_commands) == 2)
        require("close_count", len(key_closes) == 1)
        require("pnl_count", len(key_pnls) == 1)

        entry_target = next(
            (event for event in key_targets if float(event.payload.get("signed_qty") or 0.0) != 0.0),
            None,
        )
        exit_target = next(
            (event for event in key_targets if float(event.payload.get("signed_qty") or 0.0) == 0.0),
            None,
        )
        require("entry_target", entry_target is not None)
        require("exit_target", exit_target is not None)
        if entry_target is not None:
            book.add(
                key=key,
                field="entry_reference_price",
                left=float(entry_target.payload["reference_price"]),
                right=float(expected["entry_price"]),
                unit=NumericUnit.NATIVE_PRICE_OR_QUANTITY,
            )
            metadata = entry_target.payload.get("metadata") or {}
            book.add(
                key=key,
                field="entry_signed_notional_usdt",
                left=float(metadata.get("signed_notional_usdt") or 0.0),
                right=ENTRY_NOTIONAL_USDT if sleeve == "long" else -ENTRY_NOTIONAL_USDT,
                unit=NumericUnit.USDT,
            )
            expected_raw_qty = (
                (1.0 if sleeve == "long" else -1.0)
                * ENTRY_NOTIONAL_USDT
                / float(expected["entry_price"])
            )
            raw_signed_qty = metadata.get("raw_signed_qty")
            require("entry_raw_signed_qty_present", raw_signed_qty is not None)
            if raw_signed_qty is not None:
                book.add(
                    key=key,
                    field="entry_raw_signed_qty",
                    left=float(raw_signed_qty),
                    right=expected_raw_qty,
                    unit=NumericUnit.NATIVE_PRICE_OR_QUANTITY,
                )
        if exit_target is not None:
            book.add(
                key=key,
                field="exit_reference_price",
                left=float(exit_target.payload["reference_price"]),
                right=float(expected["exit_price"]),
                unit=NumericUnit.NATIVE_PRICE_OR_QUANTITY,
            )
            book.add(
                key=key,
                field="exit_target_quantity",
                left=float(exit_target.payload["signed_qty"]),
                right=0.0,
                unit=NumericUnit.VENUE_DISCRETIZED,
            )

        fill_fees: list[float] = []
        for command, fill in key_commands:
            command_id = str(command.payload["command_id"])
            require(f"ack_count:{command_id}", acks_by_command[command_id] == 1)
            require(f"status_count:{command_id}", statuses_by_command[command_id] == 1)
            book.add(
                key=key,
                field="fill_price_vs_command",
                left=float(fill.payload["price"]),
                right=float(command.payload["reference_price"]),
                unit=NumericUnit.NATIVE_PRICE_OR_QUANTITY,
            )
            book.add(
                key=key,
                field="fill_qty_vs_command",
                left=float(fill.payload["signed_qty"]),
                right=float(command.payload["signed_qty"]),
                unit=NumericUnit.VENUE_DISCRETIZED,
            )
            fill_fees.append(float(fill.payload.get("fee_usdt") or 0.0))

        if key_closes:
            close = key_closes[0]
            close_metadata = close.payload.get("metadata") or {}
            require("close_reconstructed_flat", close_metadata.get("reconstructed_flat") is True)
            require("close_venue_flat_is_transitional", close.payload.get("venue_flat") is False)
            require(
                "close_venue_position_status",
                close_metadata.get("venue_position_status") == "pending_reconciliation",
            )
        if key_pnls:
            pnl = key_pnls[0]
            gross = float(pnl.payload["gross_pnl_usdt"])
            fee = float(pnl.payload["fee_usdt"])
            funding = float(pnl.payload["funding_usdt"])
            net = float(pnl.payload["net_pnl_usdt"])
            book.add(
                key=key,
                field="gross_pnl_usdt",
                left=gross,
                right=float(expected["gross_return"]) * CAPITAL_USDT,
                unit=NumericUnit.USDT,
            )
            book.add(
                key=key,
                field="fee_usdt_vs_fills",
                left=fee,
                right=sum(fill_fees),
                unit=NumericUnit.USDT,
            )
            book.add(
                key=key,
                field="funding_usdt_modeled_separately",
                left=funding,
                right=0.0,
                unit=NumericUnit.USDT,
            )
            book.add(
                key=key,
                field="net_pnl_identity_usdt",
                left=net,
                right=gross - fee + funding,
                unit=NumericUnit.USDT,
            )
            metadata = pnl.payload.get("metadata") or {}
            require("funding_status", metadata.get("funding_status") == "modeled_separately")
            require(
                "pnl_finalization_status",
                metadata.get("pnl_finalization_status") == "modeled_execution_twin",
            )

        numeric_rows = book.by_key.get(key, [])
        numeric_pass = all(row.passed for row in numeric_rows)
        if key_discrete:
            discrete_failures.append({"source_key": key, "failures": key_discrete})
        key_rows.append(
            {
                "sleeve": sleeve,
                "source_key": key,
                "population": "calibration" if key in calibration_keys else "validation",
                "decision_events": len(key_decisions),
                "target_events": len(key_targets),
                "command_fill_pairs": len(key_commands),
                "close_events": len(key_closes),
                "pnl_events": len(key_pnls),
                "numeric_comparisons": len(numeric_rows),
                "numeric_pass": numeric_pass,
                "discrete_pass": not key_discrete,
                "status": "pass" if numeric_pass and not key_discrete else "fail",
            }
        )

    unexpected = {
        "decision": sorted(set(decisions) - expected_keys),
        "target": sorted(set(targets) - expected_keys),
        "command": sorted(set(command_by_component) - expected_keys),
        "close": sorted(set(closes) - expected_keys),
        "pnl": sorted(set(pnls) - expected_keys),
    }
    risk_events = grouped.get(AccountEventType.RISK_DECISION.value, [])
    rejected_risk = sum(not bool(event.payload.get("accepted")) for event in risk_events)
    expected_event_counts = {
        AccountEventType.DECISION.value: len(expected_keys) * 2,
        AccountEventType.TARGET.value: len(expected_keys) * 2,
        AccountEventType.ORDER_COMMAND.value: len(expected_keys) * 2,
        AccountEventType.ACK.value: len(expected_keys) * 2,
        AccountEventType.FILL.value: len(expected_keys) * 2,
        AccountEventType.ORDER_STATUS.value: len(expected_keys) * 2,
        AccountEventType.CLOSE.value: len(expected_keys),
        AccountEventType.PNL.value: len(expected_keys),
    }
    event_count_mismatches = {
        event_type: {
            "expected": expected,
            "actual": len(grouped.get(event_type, [])),
        }
        for event_type, expected in expected_event_counts.items()
        if len(grouped.get(event_type, [])) != expected
    }
    unknown_command_references = {
        "fill": sorted(set(fills_by_command) - set(commands)),
        "ack": sorted(set(acks_by_command) - set(commands)),
        "order_status": sorted(set(statuses_by_command) - set(commands)),
    }
    journal_receipt_complete = bool(
        replay_receipt.get("final_state_hash")
        and replay_receipt.get("last_event_hash")
        and int(replay_receipt.get("events") or -1) == len(events)
        and int(replay_receipt.get("last_sequence") or -1) == len(events)
    )
    key_frame = pl.from_dicts(key_rows, infer_schema_length=None).sort("source_key")
    calibration_frame = key_frame.filter(pl.col("population") == "calibration")
    validation_frame = key_frame.filter(pl.col("population") == "validation")
    summary: dict[str, Any] = {
        "source_keys": len(expected_keys),
        "calibration_keys": calibration_frame.height,
        "validation_keys": validation_frame.height,
        "calibration_failures": calibration_frame.filter(pl.col("status") != "pass").height,
        "validation_failures": validation_frame.filter(pl.col("status") != "pass").height,
        "decision_events": len(grouped.get(AccountEventType.DECISION.value, [])),
        "target_events": len(grouped.get(AccountEventType.TARGET.value, [])),
        "fill_events": len(grouped.get(AccountEventType.FILL.value, [])),
        "close_events": len(grouped.get(AccountEventType.CLOSE.value, [])),
        "pnl_events": len(grouped.get(AccountEventType.PNL.value, [])),
        "risk_decisions": len(risk_events),
        "rejected_risk_decisions": rejected_risk,
        "unexpected_component_counts": {name: len(values) for name, values in unexpected.items()},
        "event_count_mismatches": event_count_mismatches,
        "duplicate_command_ids": duplicate_command_ids,
        "unknown_command_reference_counts": {
            name: len(values) for name, values in unknown_command_references.items()
        },
        "numeric": book.summary(),
        "numeric_failures": len(book.failures),
        "discrete_failures": len(discrete_failures),
        "journal_events": len(events),
        "journal_verified": True,
        "journal_receipt_complete": journal_receipt_complete,
        "final_state_hash": replay_receipt.get("final_state_hash"),
        "last_event_hash": replay_receipt.get("last_event_hash"),
        "final_flat": bool(replay_receipt.get("final_flat")),
        "status": "pass",
    }
    if (
        summary["validation_failures"]
        or summary["calibration_failures"]
        or summary["numeric_failures"]
        or summary["discrete_failures"]
        or summary["rejected_risk_decisions"]
        or any(summary["unexpected_component_counts"].values())
        or summary["event_count_mismatches"]
        or summary["duplicate_command_ids"]
        or any(summary["unknown_command_reference_counts"].values())
        or not summary["journal_receipt_complete"]
        or not summary["final_flat"]
    ):
        summary["status"] = "fail"
    global_failures: list[dict[str, Any]] = []
    if event_count_mismatches:
        global_failures.append({"scope": "sleeve", "event_count_mismatches": event_count_mismatches})
    if duplicate_command_ids:
        global_failures.append({"scope": "sleeve", "duplicate_command_ids": duplicate_command_ids})
    if any(unknown_command_references.values()):
        global_failures.append(
            {"scope": "sleeve", "unknown_command_references": unknown_command_references}
        )
    if not journal_receipt_complete:
        global_failures.append({"scope": "sleeve", "journal_receipt_complete": False})
    if not replay_receipt.get("final_flat"):
        global_failures.append({"scope": "sleeve", "final_flat": False})
    failures = [*book.failures, *discrete_failures, *global_failures]
    return summary, key_frame, failures


def _artifact_identities(root: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("**/*")):
        if not path.is_file() or path.name == "receipt.json":
            continue
        relative = path.relative_to(root).as_posix()
        output[relative] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    return output


def _validate_existing(output: Path) -> dict[str, Any]:
    receipt_path = output / "receipt.json"
    if not receipt_path.is_file():
        raise FileExistsError(f"replay output exists without receipt: {output}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for relative, identity in receipt.get("files", {}).items():
        path = output.joinpath(*relative.split("/"))
        if not path.is_file() or path.stat().st_size != identity["bytes"] or _sha256(path) != identity["sha256"]:
            raise RuntimeError(f"existing replay artifact identity failed: {path}")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--preflight", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.out.expanduser().resolve()
    inputs = _registered_inputs()
    if float(phase3.CAPITAL_USD) != CAPITAL_USDT:
        raise RuntimeError("phase3 account capital changed from the registered replay value")
    if float(phase3.NOTIONAL_USD) != ENTRY_NOTIONAL_USDT:
        raise RuntimeError("phase3 entry notional changed from the registered replay value")
    head = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain=v1"))
    ledger = _load_ledger()
    calibration = _calibration_keys(ledger)
    run_identity = {
        "schema_version": 1,
        "kind": "prospective_complete_account_ledger_replay",
        "code_commit": head,
        "git_dirty": dirty,
        "registered_inputs": inputs,
        "trade_counts": EXPECTED_TRADE_COUNTS,
        "calibration_keys_per_sleeve": CALIBRATION_KEYS_PER_SLEEVE,
        "validation_keys": {
            sleeve: count - CALIBRATION_KEYS_PER_SLEEVE
            for sleeve, count in EXPECTED_TRADE_COUNTS.items()
        },
        "command": [str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
        "strategy_outcomes_inspected": False,
        "accounting_identities_inspected": True,
    }
    if args.preflight:
        print(json.dumps({**run_identity, "mode": "preflight"}, sort_keys=True))
        return 0
    if dirty:
        raise RuntimeError("complete ledger replay requires a clean code commit")
    if output.exists():
        existing = _validate_existing(output)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "status": existing["status"],
                    "receipt_sha256": _sha256(output / "receipt.json"),
                },
                sort_keys=True,
            )
        )
        return 0
    work = output.with_name(f".{output.name}.working-{head[:12]}")
    if work.exists():
        raise FileExistsError(f"preserved replay attempt already exists: {work}")
    work.mkdir(parents=True)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    _write_json_create(
        work / "attempt.json",
        {
            **run_identity,
            "started_at": started_at,
            "attempt_number": 1,
            "maximum_elapsed_seconds": 7_200,
        },
    )
    started = time.perf_counter()
    config = load_config(ACCOUNT_REPLAY_CONFIG)
    replay_receipts = phase3._replay_account(
        ledger,
        work_root=work,
        long_costs=config.costs,
    )
    elapsed = time.perf_counter() - started
    if elapsed > 7_200:
        raise RuntimeError(f"complete replay exceeded the registered two-hour cap: {elapsed:.3f}s")

    sleeve_summaries: dict[str, Any] = {}
    key_frames: list[pl.DataFrame] = []
    failures: list[dict[str, Any]] = []
    for sleeve in EXPECTED_TRADE_COUNTS:
        summary, key_frame, sleeve_failures = _validate_sleeve(
            ledger,
            sleeve=sleeve,
            account_root=work / f"account-{sleeve}",
            calibration_keys=calibration[sleeve],
            replay_receipt=replay_receipts[sleeve],
        )
        sleeve_summaries[sleeve] = summary
        key_frames.append(key_frame)
        failures.extend({"sleeve": sleeve, **failure} for failure in sleeve_failures)
    key_reconciliation = pl.concat(key_frames, how="vertical_relaxed").sort(
        ["sleeve", "source_key"]
    )
    _write_parquet_create(work / "key_reconciliation.parquet", key_reconciliation)
    _write_json_create(work / "failures.json", {"failures": failures})
    status = "pass" if not failures and all(
        summary["status"] == "pass" for summary in sleeve_summaries.values()
    ) else "fail"
    files = _artifact_identities(work)
    receipt: dict[str, Any] = {
        **run_identity,
        "started_at": started_at,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "elapsed_seconds": elapsed,
        "status": status,
        "sleeves": sleeve_summaries,
        "key_reconciliation": {
            "rows": key_reconciliation.height,
            "passing_rows": key_reconciliation.filter(pl.col("status") == "pass").height,
            "failing_rows": key_reconciliation.filter(pl.col("status") != "pass").height,
            "calibration_rows": key_reconciliation.filter(pl.col("population") == "calibration").height,
            "validation_rows": key_reconciliation.filter(pl.col("population") == "validation").height,
        },
        "funding_scope": "account execution twin records zero with modeled_separately provenance; historical ledger funding is not injected or compared",
        "cost_scope": "account modeled fill fees reconcile internally; broader legacy spread/impact cost_return is not an account-journal field",
        "portable_boundary": "single_process_buffered_direct_materialization_no_crash_or_concurrent_writer_claim",
        "files": files,
        "strategy_outcomes_inspected": False,
        "accounting_identities_inspected": True,
        "explicit_non_conclusions": [
            "no alpha or return conclusion",
            "no calibrated venue execution claim",
            "no active-runtime target parity claim from this ledger fixture",
            "no deployment, mainnet, or real-money authority",
        ],
    }
    receipt["receipt_payload_sha256"] = payload_sha256(receipt)
    _write_json_create(work / "receipt.json", receipt)
    os.replace(work, output)
    print(
        json.dumps(
            {
                "output": str(output),
                "status": status,
                "receipt_sha256": _sha256(output / "receipt.json"),
                "key_rows": key_reconciliation.height,
                "failing_key_rows": receipt["key_reconciliation"]["failing_rows"],
                "strategy_outcomes_inspected": False,
            },
            sort_keys=True,
        )
    )
    if status != "pass":
        raise RuntimeError("complete account-ledger replay failed its registered checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
