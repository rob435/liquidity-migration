from __future__ import annotations

import copy
from pathlib import Path

import pytest

from liquidity_migration.account.account_kernel import AccountEvent, AccountEventType
from liquidity_migration.research.backtest.continuous_profile import ACTIVE_CONTINUOUS_COMPONENTS
from liquidity_migration.research.execution.three_way_reconciliation import (
    AccountEntryRecord,
    AccountEvidence,
    BacktestEntryRecord,
    EntryKey,
    compare_three_way_entries,
    extract_account_entry_records,
    load_backtest_entry_records,
    normalize_component,
    write_three_way_artifacts,
)


def _event(
    sequence: int,
    event_type: AccountEventType,
    *,
    batch: str,
    sleeve: str = "continuous",
    symbol: str = "BTCUSDT",
    payload: dict[str, object] | None = None,
) -> AccountEvent:
    return AccountEvent(
        schema_version=1,
        event_id=f"event-{sequence}",
        sequence=sequence,
        event_type=event_type.value,
        correlation_id=batch,
        causation_id=f"cause-{sequence}",
        account_id="demo-account",
        sleeve=sleeve,
        symbol=symbol,
        wall_ts_ns=1_000_000_000 + sequence,
        monotonic_ns=sequence,
        payload=dict(payload or {}),
        prev_event_hash="0" * 64,
        state_hash="1" * 64,
        event_hash="2" * 64,
    )


def test_normalize_component_maps_live_runtime_tags() -> None:
    assert normalize_component("long", "long-BTCUSDT-123") == "long"
    assert normalize_component("continuous", "continuous_fade_v2-BTCUSDT-123-p3") == "turn3p3"
    assert normalize_component("continuous", "p4p5") == "turn4p5"
    assert normalize_component("continuous", "mystery") == "unknown:mystery"


def test_extract_account_entries_keeps_risk_and_net_symbol_execution_separate() -> None:
    batch = "batch-1"
    target_payload = {
        "batch_id": batch,
        "decision_key": "decision-1",
        "target_key": "continuous/strategy/component/BTCUSDT",
        "sleeve": "continuous",
        "strategy_id": "continuous_fade_v2",
        "component_id": "continuous_fade_v2-BTCUSDT-1000-p4p3",
        "signed_qty": -2.0,
        "reference_price": 100.0,
        "metadata": {"signal_ts_ms": 1_000, "signal_valid_until_ms": 2_000},
    }
    events = [
        _event(1, AccountEventType.TARGET, batch=batch, payload=target_payload),
        _event(
            2,
            AccountEventType.RISK_DECISION,
            batch=batch,
            sleeve="account_risk",
            symbol="",
            payload={"batch_id": batch, "accepted": True},
        ),
        _event(
            3,
            AccountEventType.ORDER_COMMAND,
            batch=batch,
            sleeve="account_execution",
            payload={"batch_id": batch, "command_id": "command-1"},
        ),
        _event(
            4,
            AccountEventType.ACK,
            batch="command-1",
            sleeve="account_execution",
            payload={"command_id": "command-1", "accepted": True},
        ),
        _event(
            5,
            AccountEventType.FILL,
            batch="command-1",
            sleeve="account_execution",
            payload={"command_id": "command-1"},
        ),
    ]

    records = extract_account_entry_records(events, environment="demo")

    assert len(records) == 1
    assert records[0].key == EntryKey("continuous", "turn4p3", "BTCUSDT", 1_000)
    assert records[0].accepted is True
    assert records[0].execution_state == "accepted_batch_symbol_filled"


def test_extract_account_entries_excludes_zero_and_convergence_targets() -> None:
    base = {
        "batch_id": "batch",
        "decision_key": "decision",
        "target_key": "long/strategy/component/BTCUSDT",
        "sleeve": "long",
        "strategy_id": "long_native_v11a_div_weekend_vol",
        "component_id": "long-BTCUSDT-1000",
        "reference_price": 100.0,
        "metadata": {"signal_ts_ms": 1_000, "signal_valid_until_ms": 2_000},
    }
    zero = _event(
        1,
        AccountEventType.TARGET,
        batch="batch",
        sleeve="long",
        payload={**base, "signed_qty": 0.0},
    )
    convergence = _event(
        2,
        AccountEventType.TARGET,
        batch="batch-2",
        sleeve="long",
        payload={
            **base,
            "batch_id": "batch-2",
            "signed_qty": 1.0,
            "metadata": {**base["metadata"], "account_convergence_retry": True},
        },
    )

    assert extract_account_entry_records([zero, convergence], environment="demo") == ()


def _account_evidence(
    environment: str,
    records: tuple[AccountEntryRecord, ...],
) -> AccountEvidence:
    return AccountEvidence(
        root=f"/{environment}",
        environment=environment,
        records=records,
        journal_receipt={"events": len(records), "last_event_hash": environment},
        event_count=len(records),
        first_wall_ts_ms=500,
        last_wall_ts_ms=2_000,
        account_ids=(f"{environment}-account",),
        strategy_ids=("strategy",),
        commit_candidates=(),
    )


def _account_record(key: EntryKey, environment: str, *, accepted: bool) -> AccountEntryRecord:
    return AccountEntryRecord(
        key=key,
        environment=environment,
        account_id=f"{environment}-account",
        batch_id=f"{environment}-batch",
        target_key=f"{environment}-target",
        decision_key=f"{environment}-decision",
        wall_ts_ns=1_000_000_000,
        signed_qty=1.0,
        reference_price=100.0,
        accepted=accepted,
        execution_state="accepted_batch_symbol_filled" if accepted else "risk_rejected",
    )


def test_compare_uses_accepted_keys_and_labels_vacuous_sleeves() -> None:
    matched = EntryKey("long", "long", "BTCUSDT", 1_000)
    rejected = EntryKey("long", "long", "ETHUSDT", 1_100)
    demo = _account_evidence(
        "demo",
        (_account_record(matched, "demo", accepted=True), _account_record(rejected, "demo", accepted=False)),
    )
    paper = _account_evidence("paper", (_account_record(matched, "paper", accepted=True),))
    backtest = (
        BacktestEntryRecord(
            key=matched,
            venue="bybit",
            source_path="/backtest.csv",
            entry_ts_ms=1_100,
            exit_ts_ms=1_500,
            side="long",
        ),
    )

    report = compare_three_way_entries(
        demo=demo,
        paper=paper,
        backtest_records=backtest,
        backtest_metadata={"venue": "bybit"},
        backtest_warnings=(),
        start_ms=900,
        end_ms=2_000,
        start_source="test",
        code_commit="a" * 40,
    )

    assert report["counts"] == {
        "demo_proposed": 2,
        "demo_accepted": 1,
        "paper_proposed": 1,
        "paper_accepted": 1,
        "backtest_modeled": 1,
        "three_way_overlap": 1,
    }
    assert report["sleeves"]["long"]["three_way_exact"] is True
    assert report["sleeves"]["continuous"]["vacuous"] is True
    rejected_row = next(row for row in report["entry_rows"] if row["symbol"] == "ETHUSDT")
    assert rejected_row["demo_proposed"] is True
    assert rejected_row["demo_accepted"] is False
    assert "not proven" in " ".join(report["warnings"])


def test_backtest_loader_attaches_active_continuous_component(tmp_path: Path) -> None:
    long_root = tmp_path / "long"
    long_root.mkdir()
    (long_root / "long_native_trades.csv").write_text(
        "symbol,side,entry_signal_ts_ms,entry_ts_ms,exit_ts_ms\n"
        "BTCUSDT,long,1000,1100,1500\n",
        encoding="utf-8",
    )
    (long_root / "long_native_research_report.json").write_text("{}\n", encoding="utf-8")

    for index, profile in enumerate(ACTIVE_CONTINUOUS_COMPONENTS):
        component = tmp_path / "continuous" / "components" / "bybit" / profile.artifact_cell
        component.mkdir(parents=True)
        rows = ""
        if index == 0:
            rows = "ETHUSDT,short,1200,1300,1600\n"
        (component / "continuous_trades.csv").write_text(
            "symbol,side,entry_signal_ts_ms,entry_ts_ms,exit_ts_ms\n" + rows,
            encoding="utf-8",
        )
        (component / "continuous_report.json").write_text("{}\n", encoding="utf-8")

    records, warnings, _metadata = load_backtest_entry_records(
        tmp_path,
        venue="bybit",
        sleeves=("long", "continuous"),
    )

    assert warnings == ()
    assert {record.key for record in records} == {
        EntryKey("long", "long", "BTCUSDT", 1_000),
        EntryKey("continuous", "turn3p3", "ETHUSDT", 1_200),
    }

    # The default selection now includes carry; an absent carry model book is
    # a loud warning (no daemon-replay trades export exists yet), never a
    # silently-empty model side.
    _records, default_warnings, _meta = load_backtest_entry_records(tmp_path, venue="bybit")
    assert any("missing CARRY backtest trades" in warning for warning in default_warnings)


def test_artifacts_are_idempotent_but_never_overwritten(tmp_path: Path) -> None:
    key = EntryKey("long", "long", "BTCUSDT", 1_000)
    demo = _account_evidence("demo", (_account_record(key, "demo", accepted=True),))
    paper = _account_evidence("paper", (_account_record(key, "paper", accepted=True),))
    report = compare_three_way_entries(
        demo=demo,
        paper=paper,
        backtest_records=(
            BacktestEntryRecord(key, "bybit", "/backtest.csv", 1_100, 1_500, "long"),
        ),
        backtest_metadata={"venue": "bybit"},
        backtest_warnings=(),
        start_ms=900,
        end_ms=2_000,
        start_source="test",
        code_commit="a" * 40,
    )

    first = write_three_way_artifacts(report, tmp_path)
    second = write_three_way_artifacts(report, tmp_path)

    assert first == second
    assert Path(first["json"]).is_file()
    changed = copy.deepcopy(report)
    changed["warnings"].append("changed evidence")
    with pytest.raises(FileExistsError, match="immutable reconciliation artifact changed"):
        write_three_way_artifacts(changed, tmp_path)
