from __future__ import annotations

import json
import time
from pathlib import Path

import scripts.run_active_runtime_comparator as runner


def test_structural_failure_capture_is_create_only_and_keeps_last_feedback(
    tmp_path: Path,
) -> None:
    work = tmp_path / "working"
    work.mkdir()
    trace = runner._ComparatorTraceWriter(work)
    trace.request({
        "request_ordinal": 7,
        "stage": "continuous_exit",
        "request_id": "failed-exit",
        "accepted": False,
        "target_committed": False,
        "rejection_keys": ["account-risk:failed-exit:test-rejection"],
    })
    context = runner._FailureContext(
        work=work,
        run_identity={"kind": "test-structural-failure"},
        started_at="2026-07-19T00:00:00Z",
        started_perf=time.perf_counter(),
        trace=trace,
        progress_hours=12,
        total_hours=100,
        last_boundary_ts_ms=123_000,
    )

    runner._capture_structural_failure(context, RuntimeError("exit rejected"))

    termination_path = work / "termination.json"
    first_bytes = termination_path.read_bytes()
    receipt = json.loads(first_bytes)
    assert receipt["status"] == "invalid_structural_failure"
    assert receipt["exception_type"] == "RuntimeError"
    assert receipt["exception_message"] == "exit rejected"
    assert receipt["progress_hours"] == 12
    assert receipt["monetary_outcomes_inspected"] is False
    assert receipt["last_request_feedback"]["request_id"] == "failed-exit"
    assert receipt["last_request_feedback"]["accepted"] is False
    expected_hash = receipt.pop("receipt_payload_sha256")
    assert runner.payload_sha256(receipt) == expected_hash

    runner._capture_structural_failure(context, ValueError("must not replace"))
    assert termination_path.read_bytes() == first_bytes
