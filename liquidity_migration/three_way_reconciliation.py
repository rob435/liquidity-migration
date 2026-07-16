"""Journal-native demo/paper/backtest structural reconciliation.

The three evidence planes do not share fill semantics.  Demo and paper expose
account-kernel target, risk, command, and fill facts; a historical run exposes
modeled trades produced from a different market tape and execution model.  This
module therefore compares the narrow invariant they can honestly share:

``(sleeve, active component, symbol, causal signal timestamp)``.

Execution state and backtest performance remain separate evidence.  Exact key
agreement is not execution calibration, PnL reconciliation, runtime parity, or
deployment authority.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_kernel import (
    AccountEvent,
    AccountEventType,
    read_account_journal,
    verify_account_journal,
)
from .continuous_profile import ACTIVE_CONTINUOUS_COMPONENTS
from .deterministic_serialization import canonical_json


_CONTINUOUS_COMPONENT_ALIASES = {
    "p3": "turn3p3",
    "p4p3": "turn4p3",
    "p4p5": "turn4p5",
    "turn3p3": "turn3p3",
    "turn4p3": "turn4p3",
    "turn4p5": "turn4p5",
}
_COMMIT_KEYS = {
    "code_commit",
    "commit",
    "expected_commit",
    "git_commit",
    "installed_commit",
    "repo_commit",
}


@dataclass(frozen=True, order=True, slots=True)
class EntryKey:
    sleeve: str
    component: str
    symbol: str
    signal_ts_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sleeve": self.sleeve,
            "component": self.component,
            "symbol": self.symbol,
            "signal_ts_ms": self.signal_ts_ms,
        }


@dataclass(frozen=True, slots=True)
class AccountEntryRecord:
    key: EntryKey
    environment: str
    account_id: str
    batch_id: str
    target_key: str
    decision_key: str
    wall_ts_ns: int
    signed_qty: float
    reference_price: float
    accepted: bool | None
    execution_state: str

    def to_dict(self) -> dict[str, object]:
        return {
            **self.key.to_dict(),
            "environment": self.environment,
            "account_id": self.account_id,
            "batch_id": self.batch_id,
            "target_key": self.target_key,
            "decision_key": self.decision_key,
            "wall_ts_ns": self.wall_ts_ns,
            "signed_qty": self.signed_qty,
            "reference_price": self.reference_price,
            "accepted": self.accepted,
            "execution_state": self.execution_state,
        }


@dataclass(frozen=True, slots=True)
class BacktestEntryRecord:
    key: EntryKey
    venue: str
    source_path: str
    entry_ts_ms: int
    exit_ts_ms: int | None
    side: str

    def to_dict(self) -> dict[str, object]:
        return {
            **self.key.to_dict(),
            "venue": self.venue,
            "source_path": self.source_path,
            "entry_ts_ms": self.entry_ts_ms,
            "exit_ts_ms": self.exit_ts_ms,
            "side": self.side,
        }


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    root: str
    environment: str
    records: tuple[AccountEntryRecord, ...]
    journal_receipt: Mapping[str, Any]
    event_count: int
    first_wall_ts_ms: int | None
    last_wall_ts_ms: int | None
    account_ids: tuple[str, ...]
    strategy_ids: tuple[str, ...]
    commit_candidates: tuple[str, ...]


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_component(sleeve: str, component_id: object) -> str:
    """Map live component identities to the active historical component keys."""

    normalized_sleeve = str(sleeve).strip().lower()
    if normalized_sleeve == "long":
        return "long"
    raw = str(component_id or "").strip()
    if normalized_sleeve != "continuous":
        return raw or "unknown"
    direct = _CONTINUOUS_COMPONENT_ALIASES.get(raw)
    if direct:
        return direct
    suffix = raw.rsplit("-", 1)[-1] if raw else ""
    return _CONTINUOUS_COMPONENT_ALIASES.get(suffix, f"unknown:{raw or 'missing'}")


def _command_id(event: AccountEvent) -> str:
    return str(event.payload.get("command_id") or "")


def extract_account_entry_records(
    events: Sequence[AccountEvent],
    *,
    environment: str,
) -> tuple[AccountEntryRecord, ...]:
    """Extract proposed entry targets and their account-level execution state."""

    normalized_environment = str(environment).strip().lower()
    if normalized_environment not in {"demo", "paper"}:
        raise ValueError("account evidence environment must be demo or paper")

    risk_by_batch: dict[str, bool] = {}
    commands_by_batch_symbol: dict[tuple[str, str], list[str]] = defaultdict(list)
    ack_by_command: dict[str, bool] = {}
    filled_commands: set[str] = set()

    for event in events:
        if event.event_type == AccountEventType.RISK_DECISION.value:
            batch_id = str(event.payload.get("batch_id") or event.correlation_id)
            if batch_id:
                risk_by_batch[batch_id] = bool(event.payload.get("accepted"))
        elif event.event_type == AccountEventType.ORDER_COMMAND.value:
            command_id = _command_id(event)
            batch_id = str(event.payload.get("batch_id") or event.correlation_id)
            if command_id and batch_id:
                commands_by_batch_symbol[(batch_id, event.symbol.upper())].append(command_id)
        elif event.event_type == AccountEventType.ACK.value:
            command_id = _command_id(event)
            if command_id:
                ack_by_command[command_id] = bool(event.payload.get("accepted"))
        elif event.event_type == AccountEventType.FILL.value:
            command_id = _command_id(event)
            if command_id:
                filled_commands.add(command_id)

    records: list[AccountEntryRecord] = []
    for event in events:
        if event.event_type != AccountEventType.TARGET.value:
            continue
        payload = event.payload
        metadata = _mapping(payload.get("metadata"))
        if bool(metadata.get("account_convergence_retry")):
            continue
        signal_ts_ms = _int_or_none(metadata.get("signal_ts_ms"))
        signed_qty = _finite_or_none(payload.get("signed_qty"))
        reference_price = _finite_or_none(payload.get("reference_price"))
        sleeve = str(payload.get("sleeve") or event.sleeve).strip().lower()
        if (
            sleeve not in {"long", "continuous"}
            or signal_ts_ms is None
            or signal_ts_ms <= 0
            or signed_qty is None
            or abs(signed_qty) <= 1e-15
            or reference_price is None
            or reference_price <= 0.0
        ):
            continue

        batch_id = str(payload.get("batch_id") or event.correlation_id)
        accepted = risk_by_batch.get(batch_id)
        command_ids = commands_by_batch_symbol.get((batch_id, event.symbol.upper()), [])
        if accepted is None:
            execution_state = "proposal_without_risk_decision"
        elif not accepted:
            execution_state = "risk_rejected"
        elif any(command_id in filled_commands for command_id in command_ids):
            # Commands are net-symbol facts. This proves that the accepted batch
            # changed the symbol and received a fill, not that a component had a
            # separately identifiable venue fill.
            execution_state = "accepted_batch_symbol_filled"
        elif command_ids and all(ack_by_command.get(command_id) is False for command_id in command_ids):
            execution_state = "accepted_target_command_rejected"
        elif command_ids:
            execution_state = "accepted_target_commanded_without_fill"
        else:
            execution_state = "accepted_target_no_net_command"

        records.append(
            AccountEntryRecord(
                key=EntryKey(
                    sleeve=sleeve,
                    component=normalize_component(sleeve, payload.get("component_id")),
                    symbol=event.symbol.upper(),
                    signal_ts_ms=signal_ts_ms,
                ),
                environment=normalized_environment,
                account_id=event.account_id,
                batch_id=batch_id,
                target_key=str(payload.get("target_key") or ""),
                decision_key=str(payload.get("decision_key") or event.causation_id),
                wall_ts_ns=int(event.wall_ts_ns),
                signed_qty=signed_qty,
                reference_price=reference_price,
                accepted=accepted,
                execution_state=execution_state,
            )
        )
    return tuple(records)


def _commit_candidates(value: object) -> set[str]:
    candidates: set[str] = set()

    def visit(node: object, key: str = "") -> None:
        if isinstance(node, Mapping):
            for child_key, child in node.items():
                visit(child, str(child_key).lower())
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child, key)
        elif key in _COMMIT_KEYS and isinstance(node, str):
            normalized = node.strip().lower()
            if len(normalized) == 40 and all(character in "0123456789abcdef" for character in normalized):
                candidates.add(normalized)

    visit(value)
    return candidates


def load_account_evidence(root: str | Path, *, environment: str) -> AccountEvidence:
    resolved = Path(root).expanduser().resolve(strict=True)
    events = read_account_journal(resolved, verify=True)
    receipt = verify_account_journal(resolved)
    wall_ms = [int(event.wall_ts_ns // 1_000_000) for event in events]
    strategies = sorted(
        {
            str(event.payload.get("strategy_id") or "")
            for event in events
            if event.event_type == AccountEventType.TARGET.value
            and str(event.payload.get("strategy_id") or "")
        }
    )
    commits: set[str] = set()
    for event in events:
        commits.update(_commit_candidates(event.payload))
    return AccountEvidence(
        root=str(resolved),
        environment=environment,
        records=extract_account_entry_records(events, environment=environment),
        journal_receipt=receipt,
        event_count=len(events),
        first_wall_ts_ms=min(wall_ms) if wall_ms else None,
        last_wall_ts_ms=max(wall_ms) if wall_ms else None,
        account_ids=tuple(sorted({event.account_id for event in events})),
        strategy_ids=tuple(strategies),
        commit_candidates=tuple(sorted(commits)),
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _backtest_row(
    row: Mapping[str, str],
    *,
    sleeve: str,
    component: str,
    venue: str,
    path: Path,
) -> BacktestEntryRecord | None:
    signal_ts_ms = _int_or_none(row.get("entry_signal_ts_ms") or row.get("signal_ts_ms"))
    entry_ts_ms = _int_or_none(row.get("entry_ts_ms"))
    exit_ts_ms = _int_or_none(row.get("exit_ts_ms"))
    symbol = str(row.get("symbol") or "").strip().upper()
    side = str(row.get("side") or "").strip().lower()
    if signal_ts_ms is None or signal_ts_ms <= 0 or entry_ts_ms is None or entry_ts_ms <= 0 or not symbol:
        return None
    return BacktestEntryRecord(
        key=EntryKey(
            sleeve=sleeve,
            component=component,
            symbol=symbol,
            signal_ts_ms=signal_ts_ms,
        ),
        venue=venue,
        source_path=str(path.resolve()),
        entry_ts_ms=entry_ts_ms,
        exit_ts_ms=exit_ts_ms,
        side=side,
    )


def load_backtest_entry_records(
    report_root: str | Path,
    *,
    venue: str,
    sleeves: Sequence[str] = ("long", "continuous"),
) -> tuple[tuple[BacktestEntryRecord, ...], tuple[str, ...], dict[str, Any]]:
    """Load entry identities from one standard equity-curve report root."""

    root = Path(report_root).expanduser().resolve()
    selected = {str(sleeve).strip().lower() for sleeve in sleeves}
    records: list[BacktestEntryRecord] = []
    warnings: list[str] = []
    metadata: dict[str, Any] = {"report_root": str(root), "venue": venue, "reports": {}}

    if "long" in selected:
        trades_path = root / "long" / "long_native_trades.csv"
        report_path = root / "long" / "long_native_research_report.json"
        if not trades_path.is_file():
            warnings.append(f"missing LONG backtest trades: {trades_path}")
        else:
            for row in _read_csv(trades_path):
                record = _backtest_row(
                    row,
                    sleeve="long",
                    component="long",
                    venue=venue,
                    path=trades_path,
                )
                if record is not None:
                    records.append(record)
        if report_path.is_file():
            metadata["reports"]["long"] = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            warnings.append(f"missing LONG backtest report: {report_path}")

    if "continuous" in selected:
        component_reports: dict[str, Any] = {}
        for profile in ACTIVE_CONTINUOUS_COMPONENTS:
            trades_path = root / "continuous" / "components" / venue / profile.artifact_cell / "continuous_trades.csv"
            report_path = root / "continuous" / "components" / venue / profile.artifact_cell / "continuous_report.json"
            if not trades_path.is_file():
                warnings.append(f"missing CONTINUOUS {profile.key} trades: {trades_path}")
            else:
                for row in _read_csv(trades_path):
                    record = _backtest_row(
                        row,
                        sleeve="continuous",
                        component=profile.key,
                        venue=venue,
                        path=trades_path,
                    )
                    if record is not None:
                        records.append(record)
            if report_path.is_file():
                component_reports[profile.key] = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                warnings.append(f"missing CONTINUOUS {profile.key} report: {report_path}")
        metadata["reports"]["continuous_components"] = component_reports

    return tuple(records), tuple(warnings), metadata


def _within(key: EntryKey, *, start_ms: int, end_ms: int) -> bool:
    return start_ms <= key.signal_ts_ms < end_ms


def _pairwise(left: set[EntryKey], right: set[EntryKey]) -> dict[str, object]:
    union = left | right
    return {
        "left": len(left),
        "right": len(right),
        "overlap": len(left & right),
        "left_only": len(left - right),
        "right_only": len(right - left),
        "exact": left == right,
        "jaccard": (len(left & right) / len(union)) if union else 1.0,
    }


def _states_by_key(records: Sequence[AccountEntryRecord]) -> dict[EntryKey, tuple[str, ...]]:
    states: dict[EntryKey, set[str]] = defaultdict(set)
    for record in records:
        states[record.key].add(record.execution_state)
    return {key: tuple(sorted(values)) for key, values in states.items()}


def compare_three_way_entries(
    *,
    demo: AccountEvidence,
    paper: AccountEvidence,
    backtest_records: Sequence[BacktestEntryRecord],
    backtest_metadata: Mapping[str, Any],
    backtest_warnings: Sequence[str],
    start_ms: int,
    end_ms: int,
    start_source: str,
    code_commit: str,
    account_snapshot_commit: str | None = None,
) -> dict[str, Any]:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("three-way reconciliation requires a non-empty half-open window")

    demo_window = [record for record in demo.records if _within(record.key, start_ms=start_ms, end_ms=end_ms)]
    paper_window = [record for record in paper.records if _within(record.key, start_ms=start_ms, end_ms=end_ms)]
    backtest_window = [record for record in backtest_records if _within(record.key, start_ms=start_ms, end_ms=end_ms)]

    demo_proposed = {record.key for record in demo_window}
    paper_proposed = {record.key for record in paper_window}
    demo_accepted = {record.key for record in demo_window if record.accepted is True}
    paper_accepted = {record.key for record in paper_window if record.accepted is True}
    model = {record.key for record in backtest_window}
    all_keys = sorted(demo_proposed | paper_proposed | model)
    demo_states = _states_by_key(demo_window)
    paper_states = _states_by_key(paper_window)

    warnings = list(backtest_warnings)
    unknown_components = sorted(
        {
            key.component
            for key in demo_proposed | paper_proposed
            if key.component.startswith("unknown:")
        }
    )
    if unknown_components:
        warnings.append(f"unmapped live continuous component identities: {unknown_components}")
    if not demo.account_ids:
        warnings.append("demo account journal is empty")
    if not paper.account_ids:
        warnings.append("paper account journal is empty")
    if len(demo.account_ids) > 1 or len(paper.account_ids) > 1:
        warnings.append("an account journal contains multiple account IDs")

    observed_commits = sorted(set(demo.commit_candidates) | set(paper.commit_candidates))
    identity_status = "unverified"
    if account_snapshot_commit:
        normalized_snapshot_commit = account_snapshot_commit.strip().lower()
        if len(normalized_snapshot_commit) != 40 or any(
            character not in "0123456789abcdef" for character in normalized_snapshot_commit
        ):
            raise ValueError("account snapshot commit must be a full lowercase Git commit")
        identity_status = "matches_backtest" if normalized_snapshot_commit == code_commit else "mismatch"
        if observed_commits and observed_commits != [normalized_snapshot_commit]:
            warnings.append(
                "explicit account snapshot commit conflicts with commit candidates embedded in journal payloads"
            )
    elif len(observed_commits) == 1:
        identity_status = "matches_backtest" if observed_commits[0] == code_commit else "mismatch"
    if identity_status == "unverified":
        warnings.append("demo/paper code identity is not proven by the supplied account snapshots")
    elif identity_status == "mismatch":
        warnings.append("demo/paper account snapshot commit does not match the backtest commit")

    sleeve_summaries: dict[str, Any] = {}
    for sleeve in ("long", "continuous"):
        d = {key for key in demo_accepted if key.sleeve == sleeve}
        p = {key for key in paper_accepted if key.sleeve == sleeve}
        b = {key for key in model if key.sleeve == sleeve}
        sleeve_summaries[sleeve] = {
            "demo_accepted": len(d),
            "paper_accepted": len(p),
            "backtest_modeled": len(b),
            "three_way_overlap": len(d & p & b),
            "three_way_exact": d == p == b,
            "vacuous": not (d or p or b),
            "demo_vs_paper": _pairwise(d, p),
            "demo_vs_backtest": _pairwise(d, b),
            "paper_vs_backtest": _pairwise(p, b),
        }

    rows = [
        {
            **key.to_dict(),
            "demo_proposed": key in demo_proposed,
            "demo_accepted": key in demo_accepted,
            "demo_execution_states": list(demo_states.get(key, ())),
            "paper_proposed": key in paper_proposed,
            "paper_accepted": key in paper_accepted,
            "paper_execution_states": list(paper_states.get(key, ())),
            "backtest_modeled": key in model,
        }
        for key in all_keys
    ]

    source_identity = {
        "demo": {
            "root": demo.root,
            "journal_receipt": dict(demo.journal_receipt),
            "event_count": demo.event_count,
            "account_ids": list(demo.account_ids),
            "strategy_ids": list(demo.strategy_ids),
            "commit_candidates": list(demo.commit_candidates),
            "first_wall_ts_ms": demo.first_wall_ts_ms,
            "last_wall_ts_ms": demo.last_wall_ts_ms,
        },
        "paper": {
            "root": paper.root,
            "journal_receipt": dict(paper.journal_receipt),
            "event_count": paper.event_count,
            "account_ids": list(paper.account_ids),
            "strategy_ids": list(paper.strategy_ids),
            "commit_candidates": list(paper.commit_candidates),
            "first_wall_ts_ms": paper.first_wall_ts_ms,
            "last_wall_ts_ms": paper.last_wall_ts_ms,
        },
        "backtest": dict(backtest_metadata),
        "code_commit": code_commit,
        "account_snapshot_commit": account_snapshot_commit,
        "code_identity_status": identity_status,
    }
    snapshot_material = {
        "window": [start_ms, end_ms],
        "source_identity": source_identity,
        "rows": rows,
    }
    snapshot_id = hashlib.sha256(canonical_json(snapshot_material)).hexdigest()
    return {
        "schema_version": 1,
        "kind": "demo_paper_backtest_entry_reconciliation",
        "snapshot_id": snapshot_id,
        "claim_scope": (
            "accepted entry-key structural agreement only; execution quality, fill attribution, "
            "account PnL, backtest performance, and runtime parity remain separate"
        ),
        "window": {
            "start_ts_ms": start_ms,
            "end_ts_ms_exclusive": end_ms,
            "start_source": start_source,
        },
        "source_identity": source_identity,
        "counts": {
            "demo_proposed": len(demo_proposed),
            "demo_accepted": len(demo_accepted),
            "paper_proposed": len(paper_proposed),
            "paper_accepted": len(paper_accepted),
            "backtest_modeled": len(model),
            "three_way_overlap": len(demo_accepted & paper_accepted & model),
        },
        "sleeves": sleeve_summaries,
        "warnings": warnings,
        "entry_rows": rows,
    }


def reconcile_account_roots_to_backtest(
    *,
    demo_account_root: str | Path,
    paper_account_root: str | Path,
    backtest_report_root: str | Path,
    venue: str,
    end_ms: int,
    code_commit: str,
    start_ms: int | None = None,
    account_snapshot_commit: str | None = None,
    sleeves: Sequence[str] = ("long", "continuous"),
) -> dict[str, Any]:
    demo = load_account_evidence(demo_account_root, environment="demo")
    paper = load_account_evidence(paper_account_root, environment="paper")
    if start_ms is None:
        starts = [value for value in (demo.first_wall_ts_ms, paper.first_wall_ts_ms) if value is not None]
        if len(starts) != 2:
            raise RuntimeError(
                "cannot derive a common account epoch from empty journal evidence; pass --reconcile-start"
            )
        start_ms = max(starts)
        start_source = "latest_demo_paper_journal_epoch_start"
    else:
        start_source = "explicit"
    records, backtest_warnings, metadata = load_backtest_entry_records(
        backtest_report_root,
        venue=venue,
        sleeves=sleeves,
    )
    return compare_three_way_entries(
        demo=demo,
        paper=paper,
        backtest_records=records,
        backtest_metadata=metadata,
        backtest_warnings=backtest_warnings,
        start_ms=int(start_ms),
        end_ms=int(end_ms),
        start_source=start_source,
        code_commit=code_commit,
        account_snapshot_commit=account_snapshot_commit,
    )


def format_three_way_markdown(report: Mapping[str, Any]) -> str:
    window = _mapping(report.get("window"))
    identity = _mapping(report.get("source_identity"))
    lines = [
        "# Demo / Paper / Backtest Reconciliation",
        "",
        f"- snapshot: `{report.get('snapshot_id', '')}`",
        (
            f"- window: `[{window.get('start_ts_ms')}, "
            f"{window.get('end_ts_ms_exclusive')})` ms ({window.get('start_source')})"
        ),
        f"- code identity: `{identity.get('code_identity_status', 'unverified')}`",
        f"- claim scope: {report.get('claim_scope', '')}",
        "",
        "## Accepted entry agreement",
        "",
        "| sleeve | demo | paper | backtest | all three | exact | vacuous |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for sleeve in ("long", "continuous"):
        summary = _mapping(_mapping(report.get("sleeves")).get(sleeve))
        lines.append(
            "| {sleeve} | {demo} | {paper} | {backtest} | {overlap} | {exact} | {vacuous} |".format(
                sleeve=sleeve,
                demo=summary.get("demo_accepted", 0),
                paper=summary.get("paper_accepted", 0),
                backtest=summary.get("backtest_modeled", 0),
                overlap=summary.get("three_way_overlap", 0),
                exact="yes" if summary.get("three_way_exact") else "no",
                vacuous="yes" if summary.get("vacuous") else "no",
            )
        )
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "## Limits and warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(
        [
            "",
            "The CSV contains one row per unioned entry key. Demo/paper execution states are account-level ",
            "net-symbol facts and must not be read as separately attributable component fills.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"immutable reconciliation artifact changed: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_three_way_artifacts(report: Mapping[str, Any], out_dir: str | Path) -> dict[str, str]:
    """Write deterministic, idempotent artifacts; never overwrite changed evidence."""

    out = Path(out_dir).expanduser().resolve()
    json_path = out / "three_way_reconciliation.json"
    md_path = out / "three_way_reconciliation.md"
    csv_path = out / "entry_agreement.csv"
    json_bytes = canonical_json(dict(report)) + b"\n"
    md_bytes = format_three_way_markdown(report).encode("utf-8")

    fieldnames = [
        "sleeve",
        "component",
        "symbol",
        "signal_ts_ms",
        "demo_proposed",
        "demo_accepted",
        "demo_execution_states",
        "paper_proposed",
        "paper_accepted",
        "paper_execution_states",
        "backtest_modeled",
    ]
    rows = report.get("entry_rows")
    csv_lines: list[str] = []
    if not isinstance(rows, list):
        rows = []
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = {name: raw.get(name) for name in fieldnames}
        row["demo_execution_states"] = json.dumps(row["demo_execution_states"], separators=(",", ":"))
        row["paper_execution_states"] = json.dumps(row["paper_execution_states"], separators=(",", ":"))
        writer.writerow(row)
    csv_lines.append(buffer.getvalue())
    csv_bytes = "".join(csv_lines).encode("utf-8")

    _write_immutable(json_path, json_bytes)
    _write_immutable(md_path, md_bytes)
    _write_immutable(csv_path, csv_bytes)
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "csv": str(csv_path),
    }
