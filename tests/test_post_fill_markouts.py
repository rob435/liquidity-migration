from __future__ import annotations

import json
from pathlib import Path

from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.market_capture import (
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
)
from liquidity_migration.post_fill_markouts import PostFillMarkoutObserver


def _kernel_with_fill(root: Path) -> tuple[AccountExecutionKernel, str]:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=1)
    kernel = AccountExecutionKernel(
        root,
        account_id="markout-account",
        clock=clock,
        id_seed="markout-test",
    )
    result = kernel.submit_targets(
        batch_id="batch-1",
        market_inputs=[
            MarketInputRef(
                input_key="book-1",
                symbol="BUSDT",
                exchange_ts_ns=1_800_000_000,
                local_receive_ts_ns=1_900_000_000,
                reference_price=10.0,
            )
        ],
        targets=[
            DesiredTarget(
                decision_key="decision-1",
                target_key="long/main/BUSDT",
                sleeve="long",
                strategy_id="long-v1",
                component_id="main",
                symbol="BUSDT",
                signed_qty=1.0,
                reference_price=10.0,
                leverage=2.0,
            )
        ],
        risk_snapshot=AccountRiskSnapshot(
            1_000.0,
            900.0,
            "wallet",
            1_950_000_000,
        ),
        risk_policy=AccountRiskPolicy(
            1_000.0,
            1_000.0,
            1_000.0,
            500.0,
            10.0,
        ),
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)
        },
    )
    command_id = result.commands[0].command_id
    kernel.record_ack(
        command_id=command_id,
        accepted=True,
        venue_order_id="venue-1",
        exchange_ts_ns=2_010_000_000,
        local_ack_ts_ns=2_020_000_000,
    )
    kernel.record_fill(
        command_id=command_id,
        execution_id="execution-1",
        signed_qty=1.0,
        price=10.1,
        fee_usdt=0.001,
        exchange_ts_ns=2_030_000_000,
        local_receive_ts_ns=2_040_000_000,
    )
    return kernel, command_id


def test_observer_defers_capture_io_and_deduplicates_canonical_fill(
    tmp_path: Path,
) -> None:
    kernel, command_id = _kernel_with_fill(tmp_path / "account")
    recorder = SequenceAwareMarketRecorder(
        tmp_path / "capture",
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=1,
            min_free_disk_bytes=1,
            persist_raw_market=False,
        ),
        clock=VirtualClock(current_wall_ns=2_050_000_000, current_monotonic_ns=0),
    )
    observer = PostFillMarkoutObserver(kernel=kernel, recorder=recorder)

    assert observer.notify("execution-1") is True
    assert observer.notify("execution-1") is True
    assert not list((tmp_path / "capture").rglob("segment-*.jsonl"))

    report = observer.drain()

    assert report.registered == 1
    assert report.capacity_rejected == 0
    assert report.duplicates == 1
    assert report.failed == 0
    assert recorder.pending_post_fill_symbols() == {"BUSDT"}
    rows = [
        json.loads(line)
        for path in (tmp_path / "capture").rglob("segment-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "post_fill_markout_schedule"
    assert rows[0]["execution_id"] == "execution-1"
    assert rows[0]["command_id"] == command_id
    assert rows[0]["target_horizons_ns"] == [
        1_000_000_000,
        15_000_000_000,
        60_000_000_000,
        300_000_000_000,
    ]
    recorder.close()


def test_observer_reports_unknown_execution_without_capture_write(
    tmp_path: Path,
) -> None:
    kernel, _command_id = _kernel_with_fill(tmp_path / "account")
    recorder = SequenceAwareMarketRecorder(
        tmp_path / "capture",
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=1,
            min_free_disk_bytes=1,
            persist_raw_market=False,
        ),
    )
    observer = PostFillMarkoutObserver(kernel=kernel, recorder=recorder)
    observer.notify("missing-execution")

    report = observer.drain()

    assert report.missing_executions == 1
    assert report.registered == 0
    assert not list((tmp_path / "capture").rglob("segment-*.jsonl"))
    recorder.close()
