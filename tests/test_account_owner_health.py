from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskSnapshot,
    GENESIS_HASH,
)
from liquidity_migration.account_owner_health import (
    ACCOUNT_OWNER_HEALTH_FILENAME,
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    account_owner_health_path,
    fold_convergence_health,
    format_convergence_health,
    read_account_owner_health,
    require_recent_account_owner_health,
    write_account_owner_health,
)
from liquidity_migration.account_reconcile import AccountReconciliationReport
from liquidity_migration.account_service import AccountConvergenceItem, AccountConvergenceReport
from liquidity_migration.account_paper_runner import publish_paper_owner_health
from liquidity_migration.account_service_runner import (
    notification_position_truth,
    owner_health_publish_decision,
    publish_demo_owner_health,
)
from liquidity_migration.deterministic_runtime import VirtualClock


def _health(*, loop_sequence: int = 1) -> AccountOwnerHealth:
    return AccountOwnerHealth(
        owner="account_execution",
        environment="paper",
        account_id="paper-account",
        status=AccountOwnerHealthStatus.HEALTHY,
        observed_ts_ns=10_000,
        loop_sequence=loop_sequence,
        journal_sequence=0,
        journal_state_hash=GENESIS_HASH,
        equity_usdt=10_000.0,
        available_margin_usdt=10_000.0,
        requested_symbols_ready=True,
    )


def test_convergence_health_is_stable_and_decision_useful() -> None:
    item = AccountConvergenceItem(
        symbol="BUSDT",
        generation="generation",
        target_signed_qty=-2.0,
        position_signed_qty=-0.6,
        working_signed_qty=0.0,
        working_order_count=0,
        projected_signed_qty=-0.6,
        residual_signed_qty=-1.4,
        desired_since_ns=1,
        age_ns=31_000_000_000,
        retry_attempts=2,
        retry_limit=3,
        next_retry_ts_ns=None,
        retryable=True,
        exhausted=False,
        reduce_only=False,
        status="retry_due",
    )
    report = AccountConvergenceReport(
        observed_ts_ns=32_000_000_000,
        grace_ns=30_000_000_000,
        items=(item,),
    )

    assert format_convergence_health(report) == (
        "target convergence unhealthy: "
        "BUSDT:retry_due:target=-2:position=-0.6:working=0:"
        "residual=-1.4:attempts=2/3"
    )
    assert format_convergence_health(AccountConvergenceReport(1, 1, ())) == ""
    assert fold_convergence_health(
        report,
        status=AccountOwnerHealthStatus.HEALTHY,
        detail="reconciliation healthy",
    ) == (
        AccountOwnerHealthStatus.BLOCKED,
        "reconciliation healthy; target convergence unhealthy: "
        "BUSDT:retry_due:target=-2:position=-0.6:working=0:"
        "residual=-1.4:attempts=2/3",
    )


def test_health_artifact_round_trips_strict_canonical_schema(tmp_path: Path) -> None:
    health = _health()

    path = write_account_owner_health(tmp_path, health)

    assert path == tmp_path / ACCOUNT_OWNER_HEALTH_FILENAME
    assert path == account_owner_health_path(tmp_path)
    assert path.read_bytes().endswith(b"\n")
    assert read_account_owner_health(tmp_path) == health
    assert json.loads(path.read_bytes()) == {
        "account_id": "paper-account",
        "available_margin_usdt": 10_000.0,
        "detail": "",
        "environment": "paper",
        "equity_usdt": 10_000.0,
        "journal_sequence": 0,
        "journal_state_hash": GENESIS_HASH,
        "last_batch_id": "",
        "loop_sequence": 1,
        "observed_ts_ns": 10_000,
        "owner": "account_execution",
        "requested_symbols_ready": True,
        "schema_version": 1,
        "status": "healthy",
    }


def test_paper_publisher_binds_fixed_capital_to_current_kernel_state(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=20_000, current_monotonic_ns=200)
    kernel = AccountExecutionKernel(
        tmp_path,
        account_id="paper-account",
        clock=clock,
        id_seed="paper-health-test",
    )
    kernel.record_venue_snapshot(
        snapshot_key="paper-fact-1",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=19_000,
        local_receive_ts_ns=19_100,
        metadata={"source": "paper_twin"},
    )
    state = kernel._state_ref()

    published = publish_paper_owner_health(
        kernel=kernel,
        account_root=tmp_path,
        account_id="paper-account",
        equity_usdt=12_345.0,
        status=AccountOwnerHealthStatus.BLOCKED,
        observed_ts_ns=20_000,
        loop_sequence=7,
        requested_symbols_ready=False,
        last_batch_id="batch-6",
        detail="targets lack demo-verified rules: NEWUSDT",
    )

    assert published.journal_sequence == state.events_applied == 1
    assert published.journal_state_hash == state.rolling_state_hash
    assert published.equity_usdt == 12_345.0
    assert published.available_margin_usdt == 12_345.0
    assert published.status == AccountOwnerHealthStatus.BLOCKED
    assert read_account_owner_health(tmp_path) == published


def test_demo_publisher_binds_wallet_capital_to_current_kernel_state(tmp_path: Path) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="demo-account")
    snapshot = AccountRiskSnapshot(
        equity_usdt=10_125.5,
        available_margin_usdt=8_250.0,
        snapshot_key="wallet-1",
        snapshot_ts_ns=30_000,
    )

    published = publish_demo_owner_health(
        kernel=kernel,
        account_root=tmp_path,
        account_id="demo-account",
        risk_snapshot=snapshot,
        status=AccountOwnerHealthStatus.HEALTHY,
        observed_ts_ns=31_000,
        loop_sequence=2,
        requested_symbols_ready=True,
        last_batch_id="batch-1",
    )

    assert published.environment == "demo"
    assert published.equity_usdt == 10_125.5
    assert published.available_margin_usdt == 8_250.0
    assert published.last_batch_id == "batch-1"
    assert read_account_owner_health(tmp_path) == published


def test_owner_health_republishes_each_journal_head_without_wallet_rest_burst() -> None:
    health_signature = ("healthy", "", True)
    journal_signature = (10, "a" * 64)
    common = {
        "receipt_completed": False,
        "health_signature": health_signature,
        "last_health_signature": health_signature,
        "journal_signature": journal_signature,
        "last_health_journal_signature": journal_signature,
        "now_monotonic": 12.0,
        "last_capital_refresh_monotonic": 10.0,
        "health_interval_seconds": 5.0,
    }

    assert owner_health_publish_decision(**common) == (False, False)
    assert owner_health_publish_decision(
        **{
            **common,
            "journal_signature": (11, "b" * 64),
        }
    ) == (True, False)
    assert owner_health_publish_decision(
        **{
            **common,
            "receipt_completed": True,
        }
    ) == (True, True)
    assert owner_health_publish_decision(
        **{
            **common,
            "health_signature": ("blocked", "reconcile mismatch", True),
        }
    ) == (True, True)
    assert owner_health_publish_decision(
        **{
            **common,
            "now_monotonic": 15.0,
        }
    ) == (True, True)

    with pytest.raises(ValueError, match="must be positive"):
        owner_health_publish_decision(
            **{
                **common,
                "health_interval_seconds": 0.0,
            }
        )


def test_journal_only_health_republish_restores_exact_head_binding(tmp_path: Path) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="demo-account")
    snapshot = AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 10_000)
    kernel.record_venue_snapshot(
        snapshot_key="first",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=10_000,
    )
    first = publish_demo_owner_health(
        kernel=kernel,
        account_root=tmp_path,
        account_id="demo-account",
        risk_snapshot=snapshot,
        status=AccountOwnerHealthStatus.HEALTHY,
        observed_ts_ns=11_000,
        loop_sequence=1,
        requested_symbols_ready=True,
    )
    kernel.record_venue_snapshot(
        snapshot_key="second",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=12_000,
    )
    with pytest.raises(RuntimeError, match="journal sequence mismatch"):
        require_recent_account_owner_health(
            tmp_path,
            environment="demo",
            max_age_ns=10_000,
            now_ns=12_500,
            expected_account_id="demo-account",
        )

    publish, refresh_capital = owner_health_publish_decision(
        receipt_completed=False,
        health_signature=("healthy", "", True),
        last_health_signature=("healthy", "", True),
        journal_signature=(
            kernel._state_ref().events_applied,
            kernel._state_ref().rolling_state_hash,
        ),
        last_health_journal_signature=(
            first.journal_sequence,
            first.journal_state_hash,
        ),
        now_monotonic=2.0,
        last_capital_refresh_monotonic=1.0,
        health_interval_seconds=5.0,
    )
    assert (publish, refresh_capital) == (True, False)
    rebound = publish_demo_owner_health(
        kernel=kernel,
        account_root=tmp_path,
        account_id="demo-account",
        risk_snapshot=snapshot,
        status=AccountOwnerHealthStatus.HEALTHY,
        observed_ts_ns=13_000,
        loop_sequence=2,
        requested_symbols_ready=True,
    )
    assert rebound.journal_sequence == 2
    assert require_recent_account_owner_health(
        tmp_path,
        environment="demo",
        max_age_ns=10_000,
        now_ns=13_500,
        expected_account_id="demo-account",
    ) == rebound


def test_notification_position_truth_is_independent_of_native_protection_health(
    tmp_path: Path,
) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="demo-account")
    report = AccountReconciliationReport(
        snapshot_key="snapshot",
        healthy=False,
        pending_orders_checked=0,
        execution_rows_observed=0,
        order_rows_observed=0,
        venue_positions={},
        reconstructed_positions={},
        mismatches=("native_protection:RuntimeError:missing stop",),
        observed_ts_ns=10_000,
    )

    class MatchingPositionTruth:
        checked_symbols: tuple[str, ...] = ()

        def require_recent_symbols_consistent(
            self,
            symbols: Sequence[str],
            *,
            max_age_ns: int,
        ) -> None:
            self.checked_symbols = tuple(symbols)
            assert max_age_ns == 2_000

    checker = MatchingPositionTruth()

    healthy, error = notification_position_truth(
        reconciler=checker,
        kernel=kernel,
        report=report,
        max_age_ns=2_000,
    )

    assert healthy is True
    assert error == ""
    assert checker.checked_symbols == ()


def test_notification_position_truth_requires_a_completed_reconciliation(
    tmp_path: Path,
) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="demo-account")

    class UnusedPositionTruth:
        def require_recent_symbols_consistent(
            self,
            symbols: Sequence[str],
            *,
            max_age_ns: int,
        ) -> None:
            raise AssertionError("must not be called")

    healthy, error = notification_position_truth(
        reconciler=UnusedPositionTruth(),
        kernel=kernel,
        report=None,
        max_age_ns=2_000,
    )

    assert healthy is False
    assert error == "account reconciliation has not completed"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("requested_symbols_ready", 1, "must be boolean"),
        ("equity_usdt", float("nan"), "finite and positive"),
        ("available_margin_usdt", -1.0, "finite and non-negative"),
        ("journal_state_hash", "not-a-hash", "lowercase SHA-256"),
    ],
)
def test_reader_rejects_invalid_health_values(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _health().to_dict()
    payload[field] = value
    account_owner_health_path(tmp_path).write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        read_account_owner_health(tmp_path)


def test_reader_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = _health().to_dict()
    payload["silent_extension"] = True
    account_owner_health_path(tmp_path).write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="unknown fields: silent_extension"):
        read_account_owner_health(tmp_path)


def test_failed_atomic_publish_preserves_last_good_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _health(loop_sequence=1)
    write_account_owner_health(tmp_path, first)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("liquidity_migration.account_owner_health.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_account_owner_health(tmp_path, _health(loop_sequence=2))

    assert read_account_owner_health(tmp_path) == first
    assert not list(tmp_path.glob(".*.tmp"))


def test_require_recent_health_checks_environment_status_and_age(tmp_path: Path) -> None:
    write_account_owner_health(tmp_path, _health())

    assert (
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=11_000,
        )
        == _health()
    )
    with pytest.raises(RuntimeError, match="environment"):
        require_recent_account_owner_health(
            tmp_path,
            environment="demo",
            max_age_ns=2_000,
            now_ns=11_000,
        )
    with pytest.raises(RuntimeError, match="stale"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=500,
            now_ns=11_000,
        )

    blocked = AccountOwnerHealth(
        **{
            **_health().to_dict(),
            "status": AccountOwnerHealthStatus.BLOCKED,
            "detail": "paper loop failed",
        }
    )
    write_account_owner_health(tmp_path, blocked)
    with pytest.raises(RuntimeError, match="paper loop failed"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=11_000,
        )


def test_require_recent_health_binds_exact_verified_journal_head(tmp_path: Path) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="paper-account")
    kernel.record_venue_snapshot(
        snapshot_key="paper-head",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=9_000,
        local_receive_ts_ns=9_100,
        metadata={"source": "test"},
    )
    state = kernel._state_ref()
    matching = AccountOwnerHealth(
        **{
            **_health().to_dict(),
            "journal_sequence": state.events_applied,
            "journal_state_hash": state.rolling_state_hash,
        }
    )
    write_account_owner_health(tmp_path, matching)

    assert require_recent_account_owner_health(
        tmp_path,
        environment="paper",
        expected_account_id="paper-account",
        max_age_ns=2_000,
        now_ns=11_000,
    ) == matching

    write_account_owner_health(tmp_path, _health())
    with pytest.raises(RuntimeError, match="journal sequence mismatch"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            expected_account_id="paper-account",
            max_age_ns=2_000,
            now_ns=11_000,
        )


def test_require_recent_health_rejects_wrong_expected_account_on_empty_journal(
    tmp_path: Path,
) -> None:
    write_account_owner_health(tmp_path, _health())

    with pytest.raises(RuntimeError, match="account_id"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            expected_account_id="another-paper-account",
            max_age_ns=2_000,
            now_ns=11_000,
        )
