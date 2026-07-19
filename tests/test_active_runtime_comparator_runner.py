from __future__ import annotations

import json
import time
from pathlib import Path

import polars as pl
import pytest

import scripts.run_active_runtime_comparator as runner


def _prefix_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["XRPUSDT", "XRPUSDT", "BTCUSDT"],
            "gate_existing_exposure": ["fail", "fail", "pass"],
            "gate_cooldown": ["pass", "pass", "pass"],
            "gate_state_sha256": ["old-a", "old-b", "same-c"],
            "first_rejection": ["entry_anchor", "signal_freshness", "account_risk"],
            "barebones_accepted": [False, False, False],
            "source_key": ["a", "b", "c"],
        }
    )


def _write_prefix(
    root: Path,
    relative: str,
    frame: pl.DataFrame,
) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return path


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


def test_repair_aware_prefix_allows_only_registered_xrp_state_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = "traces/long_funnel/part-00000.parquet"
    baseline_root = tmp_path / "baseline"
    work = tmp_path / "work"
    baseline = _prefix_frame()
    actual = baseline.with_columns(
        pl.Series("gate_existing_exposure", ["pass", "pass", "pass"]),
        pl.Series("gate_cooldown", ["pass", "fail", "pass"]),
        pl.Series("gate_state_sha256", ["new-a", "new-b", "same-c"]),
    )
    baseline_path = _write_prefix(baseline_root, relative, baseline)
    _write_prefix(work, relative, actual)
    monkeypatch.setattr(runner, "PREFIX_BASELINE_ROOT", baseline_root)
    monkeypatch.setattr(
        runner,
        "EXPECTED_PREFIX_IDENTITIES",
        {relative: runner._sha256(baseline_path)},
    )

    receipt = runner._prefix_equivalence(work)

    assert receipt["status"] == "pass"
    assert receipt["barebones_accepted_exact"] is True
    assert receipt["first_rejection_exact"] is True
    assert receipt["totals"] == {
        "files": 1,
        "byte_identical_files": 0,
        "semantic_only_files": 1,
        "changed_rows": 2,
        "gate_existing_exposure_fail_to_pass": 2,
        "gate_cooldown_pass_to_fail": 1,
        "gate_state_sha256_changes": 2,
    }


def test_repair_aware_prefix_rejects_transition_on_non_xrp_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = "traces/long_funnel/part-00000.parquet"
    baseline_root = tmp_path / "baseline"
    work = tmp_path / "work"
    baseline = _prefix_frame()
    actual = baseline.with_columns(
        pl.Series("gate_existing_exposure", ["fail", "fail", "pass"]),
        pl.Series("gate_cooldown", ["pass", "pass", "fail"]),
        pl.Series("gate_state_sha256", ["old-a", "old-b", "new-c"]),
    )
    baseline_path = _write_prefix(baseline_root, relative, baseline)
    _write_prefix(work, relative, actual)
    monkeypatch.setattr(runner, "PREFIX_BASELINE_ROOT", baseline_root)
    monkeypatch.setattr(
        runner,
        "EXPECTED_PREFIX_IDENTITIES",
        {relative: runner._sha256(baseline_path)},
    )

    with pytest.raises(RuntimeError, match="not XRPUSDT"):
        runner._prefix_equivalence(work)


def test_repair_aware_prefix_requires_derived_hash_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = "traces/long_funnel/part-00000.parquet"
    baseline_root = tmp_path / "baseline"
    work = tmp_path / "work"
    baseline = _prefix_frame()
    actual = baseline.with_columns(
        pl.Series("gate_existing_exposure", ["pass", "fail", "pass"]),
    )
    baseline_path = _write_prefix(baseline_root, relative, baseline)
    _write_prefix(work, relative, actual)
    monkeypatch.setattr(runner, "PREFIX_BASELINE_ROOT", baseline_root)
    monkeypatch.setattr(
        runner,
        "EXPECTED_PREFIX_IDENTITIES",
        {relative: runner._sha256(baseline_path)},
    )

    with pytest.raises(RuntimeError, match="derived gate hash"):
        runner._prefix_equivalence(work)


@pytest.mark.parametrize(
    ("column", "values"),
    (
        ("barebones_accepted", [True, False, False]),
        ("first_rejection", ["changed", "signal_freshness", "account_risk"]),
    ),
)
def test_repair_aware_prefix_keeps_decision_outcomes_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    values: list[object],
) -> None:
    relative = "traces/long_funnel/part-00000.parquet"
    baseline_root = tmp_path / "baseline"
    work = tmp_path / "work"
    baseline = _prefix_frame()
    actual = baseline.with_columns(pl.Series(column, values))
    baseline_path = _write_prefix(baseline_root, relative, baseline)
    _write_prefix(work, relative, actual)
    monkeypatch.setattr(runner, "PREFIX_BASELINE_ROOT", baseline_root)
    monkeypatch.setattr(
        runner,
        "EXPECTED_PREFIX_IDENTITIES",
        {relative: runner._sha256(baseline_path)},
    )

    with pytest.raises(RuntimeError, match=f"unregistered prefix field.*{column}"):
        runner._prefix_equivalence(work)
