"""Acceptance gate for historical/paper/demo account-kernel parity."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_kernel import AccountEvent, AccountEventType, AccountState, apply_account_event, read_account_journal
from .deterministic_serialization import canonical_json


UNVERIFIED_EXTERNAL_GATES = (
    "actual_market_tape_provenance",
    "strategy_scheduler_identity_across_environments",
    "fresh_demo_venue_rules",
    "credentialed_demo_execution",
    "owner_first_host_topology",
    "venue_closed_pnl_and_funding_reconciliation",
)


@dataclass(frozen=True, slots=True)
class KernelParityReport:
    passed: bool
    quantity_tolerance: float
    compared_environments: tuple[str, ...]
    decision_keys_identical: bool
    rejection_keys_identical: bool
    target_quantities_within_tolerance: bool
    event_types_identical: bool
    state_hashes_identical_by_sequence: bool
    mismatches: tuple[str, ...]

    def require_passed(self) -> None:
        if not self.passed:
            raise RuntimeError("account-kernel parity failed: " + "; ".join(self.mismatches))


def _decision_keys(events: Sequence[AccountEvent]) -> tuple[str, ...]:
    return tuple(
        str(event.payload.get("decision_key") or "")
        for event in events
        if event.event_type == AccountEventType.DECISION.value
    )


def _rejection_keys(events: Sequence[AccountEvent]) -> tuple[str, ...]:
    keys: list[str] = []
    for event in events:
        if event.event_type == AccountEventType.RISK_DECISION.value:
            keys.extend(str(key) for key in event.payload.get("rejection_keys") or ())
        elif event.event_type == AccountEventType.ACK.value and event.payload.get("accepted") is False:
            key = str(event.payload.get("rejection_key") or "")
            if key:
                keys.append(key)
        elif event.event_type == AccountEventType.ORDER_STATUS.value:
            key = str(event.payload.get("rejection_key") or "")
            if key:
                keys.append(key)
    return tuple(keys)


def _targets(events: Sequence[AccountEvent]) -> dict[tuple[str, str], float]:
    return {
        (event.correlation_id, str(event.payload.get("target_key") or "")): float(event.payload["signed_qty"])
        for event in events
        if event.event_type == AccountEventType.TARGET.value
    }


_SUPPLEMENTAL_PARITY_EVENTS = {
    AccountEventType.ACK_OBSERVATION.value,
    AccountEventType.VENUE_SNAPSHOT.value,
}


def _parity_events(events: Sequence[AccountEvent]) -> tuple[AccountEvent, ...]:
    return tuple(event for event in events if event.event_type not in _SUPPLEMENTAL_PARITY_EVENTS)


def _order_position_payload(state: AccountState) -> dict[str, object]:
    executions = sorted(
        (
            str(row.get("command_id") or ""),
            float(row.get("signed_qty") or 0.0),
            float(row.get("price") or 0.0),
            float(row.get("fee_usdt") or 0.0),
        )
        for row in state.executions.values()
    )
    return {
        "component_targets": {
            key: {
                "symbol": value.get("symbol"),
                "signed_qty": value.get("signed_qty"),
                "leverage": value.get("leverage"),
            }
            for key, value in sorted(state.component_targets.items())
        },
        "aggregate_targets": state.aggregate_targets,
        "orders": {
            key: {
                "command_id": value.command_id,
                "batch_id": value.batch_id,
                "symbol": value.symbol,
                "signed_qty": value.signed_qty,
                "reduce_only": value.reduce_only,
                "status": value.status,
                "filled_signed_qty": value.filled_signed_qty,
                "rejection_key": value.rejection_key,
            }
            for key, value in sorted(state.orders.items())
        },
        "positions": {key: asdict(value) for key, value in sorted(state.positions.items())},
        "executions": executions,
        "protections": {
            key: {
                "status": value.get("status"),
                "stop_price": value.get("stop_price"),
                "take_profit_price": value.get("take_profit_price"),
            }
            for key, value in sorted(state.protections.items())
        },
        "closes": {
            key: {"reason": value.get("reason"), "venue_flat": value.get("venue_flat")}
            for key, value in sorted(state.closes.items())
        },
        "pnl": {
            key: {
                "gross_pnl_usdt": value.get("gross_pnl_usdt"),
                "fee_usdt": value.get("fee_usdt"),
                "funding_usdt": value.get("funding_usdt"),
                "net_pnl_usdt": value.get("net_pnl_usdt"),
            }
            for key, value in sorted(state.pnl.items())
        },
    }


def _order_position_hashes(events: Sequence[AccountEvent]) -> tuple[str, ...]:
    state = AccountState()
    output: list[str] = []
    for event in events:
        apply_account_event(state, event)
        if event.event_type in _SUPPLEMENTAL_PARITY_EVENTS:
            continue
        output.append(hashlib.sha256(canonical_json(_order_position_payload(state))).hexdigest())
    return tuple(output)


def compare_kernel_journals(
    environments: Mapping[str, str | Path | Sequence[AccountEvent]],
    *,
    quantity_tolerance: float,
) -> KernelParityReport:
    if quantity_tolerance < 0.0 or not math.isfinite(quantity_tolerance):
        raise ValueError("quantity_tolerance must be finite and non-negative")
    if len(environments) < 2:
        raise ValueError("parity requires at least two environments")
    loaded: dict[str, Sequence[AccountEvent]] = {}
    for name, source in environments.items():
        if isinstance(source, (str, Path)):
            loaded[name] = read_account_journal(source)
        else:
            loaded[name] = source
        if not loaded[name]:
            raise ValueError(f"parity source {name!r} has no account events")
    names = tuple(loaded)
    baseline_name = names[0]
    baseline_all = loaded[baseline_name]
    baseline = _parity_events(baseline_all)
    base_decisions = _decision_keys(baseline)
    base_rejections = _rejection_keys(baseline)
    base_targets = _targets(baseline)
    base_types = tuple(event.event_type for event in baseline)
    base_hashes = _order_position_hashes(baseline_all)

    decisions_ok = rejections_ok = targets_ok = types_ok = hashes_ok = True
    mismatches: list[str] = []
    for name in names[1:]:
        all_events = loaded[name]
        events = _parity_events(all_events)
        if _decision_keys(events) != base_decisions:
            decisions_ok = False
            mismatches.append(f"decision keys differ: {baseline_name} vs {name}")
        if _rejection_keys(events) != base_rejections:
            rejections_ok = False
            mismatches.append(f"rejection keys differ: {baseline_name} vs {name}")
        targets = _targets(events)
        if set(targets) != set(base_targets):
            targets_ok = False
            mismatches.append(f"target keys differ: {baseline_name} vs {name}")
        else:
            bad = [key for key in base_targets if abs(base_targets[key] - targets[key]) > quantity_tolerance]
            if bad:
                targets_ok = False
                mismatches.append(f"target quantities differ: {baseline_name} vs {name}: {bad[:5]}")
        if tuple(event.event_type for event in events) != base_types:
            types_ok = False
            mismatches.append(f"event types differ by sequence: {baseline_name} vs {name}")
        if _order_position_hashes(all_events) != base_hashes:
            hashes_ok = False
            mismatches.append(f"state hashes differ by sequence: {baseline_name} vs {name}")
    passed = decisions_ok and rejections_ok and targets_ok and types_ok and hashes_ok
    return KernelParityReport(
        passed=passed,
        quantity_tolerance=quantity_tolerance,
        compared_environments=names,
        decision_keys_identical=decisions_ok,
        rejection_keys_identical=rejections_ok,
        target_quantities_within_tolerance=targets_ok,
        event_types_identical=types_ok,
        state_hashes_identical_by_sequence=hashes_ok,
        mismatches=tuple(mismatches),
    )


def build_kernel_parity_receipt(
    environments: Mapping[str, str | Path],
    *,
    quantity_tolerance: float,
) -> dict[str, Any]:
    """Build a deterministic, source-bound structural parity receipt.

    This deliberately does not claim market-tape provenance or venue-rule
    parity. Those are separate deployment evidence and cannot be inferred from
    three journals that happen to have environment-like directory names.
    """
    report = compare_kernel_journals(
        environments,
        quantity_tolerance=quantity_tolerance,
    )
    sources: dict[str, dict[str, Any]] = {}
    for name, root in environments.items():
        events = read_account_journal(root)
        normalized = canonical_json({"events": [event.to_dict() for event in events]})
        sources[name] = {
            "root": str(Path(root).expanduser().resolve()),
            "event_count": len(events),
            "normalized_journal_sha256": hashlib.sha256(normalized).hexdigest(),
        }
    report_payload = asdict(report)
    report_payload["compared_environments"] = list(report.compared_environments)
    report_payload["mismatches"] = list(report.mismatches)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "evidence_scope": "account_journal_structural_parity",
        "journal_parity_passed": report.passed,
        "full_cross_environment_acceptance_passed": False,
        "sources": sources,
        "report": report_payload,
        "unverified_external_gates": list(UNVERIFIED_EXTERNAL_GATES),
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    return receipt


def verify_kernel_parity_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Verify integrity and internal claims without widening evidence scope."""

    payload = dict(receipt)
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("unsupported account-kernel parity receipt schema")
    if payload.get("evidence_scope") != "account_journal_structural_parity":
        raise ValueError("account-kernel parity receipt has the wrong evidence scope")
    observed_hash = str(payload.get("artifact_sha256") or "")
    unhashed = {**payload, "artifact_sha256": ""}
    expected_hash = hashlib.sha256(canonical_json(unhashed)).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("account-kernel parity receipt hash mismatch")
    report = payload.get("report")
    sources = payload.get("sources")
    if not isinstance(report, Mapping) or not isinstance(sources, Mapping):
        raise ValueError("account-kernel parity receipt lacks report or sources")
    if set(sources) != {"historical", "paper", "demo"}:
        raise ValueError("account-kernel parity receipt requires historical, paper, and demo")
    source_hashes: list[str] = []
    for name, source in sources.items():
        if not isinstance(source, Mapping):
            raise ValueError(f"account-kernel parity source {name!r} must be an object")
        if int(source.get("event_count") or 0) <= 0:
            raise ValueError(f"account-kernel parity source {name!r} is empty")
        digest = str(source.get("normalized_journal_sha256") or "")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"account-kernel parity source {name!r} has an invalid hash")
        source_hashes.append(digest)
    passed = payload.get("journal_parity_passed")
    if not isinstance(passed, bool) or report.get("passed") is not passed:
        raise ValueError("account-kernel parity aggregate gate is inconsistent")
    if payload.get("full_cross_environment_acceptance_passed") is not False:
        raise ValueError("structural parity cannot claim full cross-environment acceptance")
    unverified = payload.get("unverified_external_gates")
    if unverified != list(UNVERIFIED_EXTERNAL_GATES):
        raise ValueError("account-kernel parity receipt must retain its unverified external gates")
    return payload


def _atomic_write_receipt(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            offset = 0
            view = memoryview(data)
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("account-kernel parity receipt write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_kernel_parity_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    output = Path(path).expanduser()
    payload = verify_kernel_parity_receipt(receipt)
    _atomic_write_receipt(output, json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n")
    return output


def load_kernel_parity_receipt(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().read_text())
    if not isinstance(value, Mapping):
        raise ValueError("account-kernel parity receipt must be an object")
    return verify_kernel_parity_receipt(value)


def _environment_arg(raw: str) -> tuple[str, Path]:
    name, separator, raw_path = raw.partition("=")
    name = name.strip()
    raw_path = raw_path.strip()
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("environment must be NAME=ACCOUNT_ROOT")
    return name, Path(raw_path).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare historical, paper, and demo account journals and write a source-bound structural parity receipt."
        )
    )
    parser.add_argument(
        "--environment",
        action="append",
        required=True,
        type=_environment_arg,
        metavar="NAME=ACCOUNT_ROOT",
        help="Repeat exactly once for historical, paper, and demo.",
    )
    parser.add_argument("--quantity-tolerance", type=float, default=1e-12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    environments: dict[str, Path] = {}
    for name, root in args.environment:
        if name in environments:
            parser.error(f"duplicate environment name: {name}")
        environments[name] = root
    required = {"historical", "paper", "demo"}
    if set(environments) != required:
        parser.error("environments must be exactly historical, paper, and demo; got " + ", ".join(sorted(environments)))

    receipt = build_kernel_parity_receipt(
        environments,
        quantity_tolerance=args.quantity_tolerance,
    )
    write_kernel_parity_receipt(args.output, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["journal_parity_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
