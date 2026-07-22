from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import liquidity_migration.account_owner_health as owner_health_module
from liquidity_migration.account_kernel import (
    AccountEvent,
    AccountExecutionKernel,
    AccountRiskSnapshot,
    GENESIS_HASH,
)
from liquidity_migration.account_owner_health import (
    ACCOUNT_OWNER_HEALTH_FILENAME,
    ACCOUNT_OWNER_HEALTH_SCHEMA_VERSION,
    TEST_ACCOUNT_OWNER_INVOCATION_ID,
    AccountOwnerHealthHeadPending,
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    AccountOwnerMarketWarmupPending,
    account_owner_health_path,
    fold_convergence_health,
    format_convergence_health,
    read_account_owner_health,
    require_recent_account_owner_health,
    require_systemd_invocation_id,
    validate_systemd_invocation_id,
    write_account_owner_health,
)
from liquidity_migration.account_reconcile import (
    AccountPositionTruthMismatchError,
    AccountReconciliationReport,
    AccountReconciliationStaleError,
)
from liquidity_migration.account_service import AccountConvergenceItem, AccountConvergenceReport
from liquidity_migration.account_paper_runner import publish_paper_owner_health
from liquidity_migration.account_service_runner import (
    append_unique_notification_health_error,
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
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
    )


def test_notification_health_errors_dedupe_reconciliation_age_updates() -> None:
    errors = ["account reconciliation is stale: age_ns=61000000000"]

    append_unique_notification_health_error(
        errors,
        "account reconciliation is stale: age_ns=61000123456",
    )
    append_unique_notification_health_error(
        errors,
        "native protection missing: ONDOUSDT",
    )

    assert errors == [
        "account reconciliation is stale: age_ns=61000000000",
        "native protection missing: ONDOUSDT",
    ]


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

    persistent_reduction = replace(
        item,
        retry_limit=None,
        reduce_only=True,
    )
    assert "attempts=2/persistent" in format_convergence_health(
        AccountConvergenceReport(32_000_000_000, 30_000_000_000, (persistent_reduction,))
    )
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
        "invocation_id": TEST_ACCOUNT_OWNER_INVOCATION_ID,
        "last_batch_id": "",
        "loop_sequence": 1,
        "observed_ts_ns": 10_000,
        "owner": "account_execution",
        "requested_symbols_ready": True,
        "schema_version": ACCOUNT_OWNER_HEALTH_SCHEMA_VERSION,
        "status": "healthy",
    }


def test_health_reader_rejects_symbolic_and_hard_link_aliases(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = write_account_owner_health(source_root, _health())

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    account_owner_health_path(symlink_root).symlink_to(source)
    with pytest.raises(ValueError, match="symbolic link"):
        read_account_owner_health(symlink_root)

    hardlink_root = tmp_path / "hardlink"
    hardlink_root.mkdir()
    os.link(source, account_owner_health_path(hardlink_root))
    with pytest.raises(ValueError, match="hard-linked"):
        read_account_owner_health(hardlink_root)


def test_reader_rejects_pre_generation_health_schema(tmp_path: Path) -> None:
    old_payload = _health().to_dict()
    old_payload["schema_version"] = 1
    old_payload.pop("invocation_id")
    account_owner_health_path(tmp_path).write_text(json.dumps(old_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="missing fields: invocation_id"):
        read_account_owner_health(tmp_path)


@pytest.mark.parametrize(
    "value",
    ("", "0" * 32, "a" * 31, "A" * 32, "g" * 32, None),
)
def test_systemd_invocation_id_validation_is_strict(value: object) -> None:
    with pytest.raises(ValueError, match="invocation|INVOCATION"):
        validate_systemd_invocation_id(value)


def test_systemd_invocation_id_is_required_from_the_service_environment() -> None:
    invocation_id = "a1" * 16
    assert require_systemd_invocation_id({"INVOCATION_ID": invocation_id}) == invocation_id
    with pytest.raises(RuntimeError, match="INVOCATION_ID is required"):
        require_systemd_invocation_id({})
    with pytest.raises(RuntimeError, match="lowercase hexadecimal"):
        require_systemd_invocation_id({"INVOCATION_ID": "A" * 32})


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
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        last_batch_id="batch-6",
        detail="targets lack demo-verified rules: NEWUSDT",
    )

    assert published.journal_sequence == state.events_applied == 1
    assert published.journal_state_hash == state.rolling_state_hash
    assert published.equity_usdt == 12_345.0
    assert published.available_margin_usdt == 12_345.0
    assert published.invocation_id == TEST_ACCOUNT_OWNER_INVOCATION_ID
    assert published.status == AccountOwnerHealthStatus.BLOCKED
    assert published.detail.startswith(
        "execution_model_scope=integration_only_uncalibrated; "
    )
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
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        last_batch_id="batch-1",
    )

    assert published.environment == "demo"
    assert published.equity_usdt == 10_125.5
    assert published.available_margin_usdt == 8_250.0
    assert published.invocation_id == TEST_ACCOUNT_OWNER_INVOCATION_ID
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
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
    )
    kernel.record_venue_snapshot(
        snapshot_key="second",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=12_000,
    )
    with pytest.raises(AccountOwnerHealthHeadPending, match="journal sequence mismatch"):
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
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
    )
    assert rebound.journal_sequence == 2
    assert require_recent_account_owner_health(
        tmp_path,
        environment="demo",
        max_age_ns=10_000,
        now_ns=13_500,
        expected_account_id="demo-account",
    ) == rebound


def test_recent_health_retries_one_concurrent_projection_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _health(loop_sequence=1)
    second = _health(loop_sequence=2)
    reads = iter((first, second, second, second))
    monkeypatch.setattr(
        owner_health_module,
        "read_account_owner_health",
        lambda _root: next(reads),
    )

    assert require_recent_account_owner_health(
        tmp_path,
        environment="paper",
        max_age_ns=2_000,
        now_ns=11_000,
    ) == second


def test_recent_health_accepts_health_that_matches_the_observed_head_during_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="paper-account")
    first_head = kernel.record_venue_snapshot(
        snapshot_key="first-head",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=1,
    )[-1]
    second_head = kernel.record_venue_snapshot(
        snapshot_key="second-head",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=2,
    )[-1]

    def bound_health(loop_sequence: int, head: AccountEvent) -> AccountOwnerHealth:
        return AccountOwnerHealth(
            **{
                **_health(loop_sequence=loop_sequence).to_dict(),
                "journal_sequence": head.sequence,
                "journal_state_hash": head.state_hash,
            }
        )

    before = bound_health(1, first_head)
    after = bound_health(2, second_head)
    health_reads = iter((before, after))
    monkeypatch.setattr(
        owner_health_module,
        "read_account_owner_health",
        lambda _root: next(health_reads),
    )
    monkeypatch.setattr(
        owner_health_module,
        "read_account_journal_head",
        lambda _root: first_head,
    )

    assert require_recent_account_owner_health(
        tmp_path,
        environment="paper",
        max_age_ns=2_000,
        now_ns=11_000,
    ) == before


def test_recent_health_accepts_sustained_same_journal_heartbeat_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = iter(_health(loop_sequence=index) for index in range(1, 20))
    monkeypatch.setattr(
        owner_health_module,
        "read_account_owner_health",
        lambda _root: next(sequence),
    )

    result = require_recent_account_owner_health(
        tmp_path,
        environment="paper",
        max_age_ns=2_000,
        now_ns=11_000,
    )

    assert result.loop_sequence == 2


def test_recent_health_rejects_sustained_different_journal_head_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = iter(
        AccountOwnerHealth(
            **{
                **_health(loop_sequence=index).to_dict(),
                "journal_sequence": index,
                "journal_state_hash": f"{index:064x}",
            }
        )
        for index in range(1, 20)
    )
    monkeypatch.setattr(
        owner_health_module,
        "read_account_owner_health",
        lambda _root: next(sequence),
    )

    with pytest.raises(RuntimeError, match="changed while binding"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=11_000,
        )


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

    healthy, error, status = notification_position_truth(
        reconciler=checker,
        kernel=kernel,
        report=report,
        max_age_ns=2_000,
    )

    assert healthy is True
    assert error == ""
    assert status == "healthy"
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

    healthy, error, status = notification_position_truth(
        reconciler=UnusedPositionTruth(),
        kernel=kernel,
        report=None,
        max_age_ns=2_000,
    )

    assert healthy is False
    assert error == "account reconciliation has not completed"
    assert status == "unavailable"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            AccountReconciliationStaleError(
                "account reconciliation is stale: age_ns=3000"
            ),
            "stale",
        ),
        (
            AccountPositionTruthMismatchError(
                "requested venue position truth contradicts reduction: BUSDT"
            ),
            "mismatch",
        ),
        (RuntimeError("position truth provider unavailable"), "unavailable"),
    ],
)
def test_notification_position_truth_preserves_failure_classification(
    tmp_path: Path,
    error: RuntimeError,
    expected_status: str,
) -> None:
    kernel = AccountExecutionKernel(tmp_path, account_id="demo-account")
    report = AccountReconciliationReport(
        snapshot_key="snapshot",
        healthy=True,
        pending_orders_checked=0,
        execution_rows_observed=0,
        order_rows_observed=0,
        venue_positions={},
        reconstructed_positions={},
        mismatches=(),
        observed_ts_ns=10_000,
    )

    class FailingPositionTruth:
        def require_recent_symbols_consistent(
            self,
            symbols: Sequence[str],
            *,
            max_age_ns: int,
        ) -> None:
            del symbols, max_age_ns
            raise error

    healthy, detail, status = notification_position_truth(
        reconciler=FailingPositionTruth(),
        kernel=kernel,
        report=report,
        max_age_ns=2_000,
    )

    assert healthy is False
    assert detail == str(error)
    assert status == expected_status


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("requested_symbols_ready", 1, "must be boolean"),
        ("invocation_id", int("1" * 32), "lowercase hexadecimal"),
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


def test_atomic_publish_refuses_a_precreated_temporary_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "unrelated"
    target.write_text("preserve me", encoding="utf-8")
    monkeypatch.setattr("liquidity_migration.account_owner_health.time.time_ns", lambda: 123)
    monkeypatch.setattr("liquidity_migration.account_owner_health.threading.get_ident", lambda: 456)
    temporary = tmp_path / (
        f".{ACCOUNT_OWNER_HEALTH_FILENAME}.{os.getpid()}.456.123.tmp"
    )
    temporary.symlink_to(target)

    with pytest.raises(FileExistsError):
        write_account_owner_health(tmp_path, _health())

    assert target.read_text(encoding="utf-8") == "preserve me"
    assert temporary.is_symlink()


def test_require_recent_health_checks_environment_status_and_age(tmp_path: Path) -> None:
    write_account_owner_health(tmp_path, _health())

    assert (
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=11_000,
            expected_invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        )
        == _health()
    )
    with pytest.raises(RuntimeError, match="current systemd generation"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=11_000,
            expected_invocation_id="00000000000000000000000000000002",
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
    with pytest.raises(RuntimeError, match="stale"):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=13_000,
        )


def test_queue_head_warmup_is_transient_only_while_owner_health_is_fresh(
    tmp_path: Path,
) -> None:
    waiting = AccountOwnerHealth(
        **{
            **_health().to_dict(),
            "status": AccountOwnerHealthStatus.BLOCKED,
            "requested_symbols_ready": False,
            "detail": "waiting for queue-head market data: ETHUSDT:stale_book",
        }
    )
    write_account_owner_health(tmp_path, waiting)

    with pytest.raises(AccountOwnerMarketWarmupPending):
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=11_000,
        )
    with pytest.raises(RuntimeError, match="stale") as stale:
        require_recent_account_owner_health(
            tmp_path,
            environment="paper",
            max_age_ns=2_000,
            now_ns=13_000,
        )
    assert not isinstance(stale.value, AccountOwnerMarketWarmupPending)


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


def _behind_by_one_root(tmp_path: Path):
    """Health published at journal seq 1, then a fill-thread append moves the head to 2."""
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
    health = publish_demo_owner_health(
        kernel=kernel,
        account_root=tmp_path,
        account_id="demo-account",
        risk_snapshot=snapshot,
        status=AccountOwnerHealthStatus.HEALTHY,
        observed_ts_ns=11_000,
        loop_sequence=1,
        requested_symbols_ready=True,
        invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
    )
    kernel.record_venue_snapshot(
        snapshot_key="second",
        venue_positions={},
        reconstructed_positions={},
        mismatches=[],
        exchange_ts_ns=0,
        local_receive_ts_ns=12_000,
    )
    return kernel, health


def test_allow_behind_accepts_fresh_health_lagging_a_live_journal(tmp_path: Path) -> None:
    _kernel, health = _behind_by_one_root(tmp_path)
    bound = require_recent_account_owner_health(
        tmp_path,
        environment="demo",
        max_age_ns=10_000,
        now_ns=12_500,
        expected_account_id="demo-account",
        head_binding="allow_behind",
    )
    assert bound == health


def test_allow_behind_still_enforces_freshness(tmp_path: Path) -> None:
    _behind_by_one_root(tmp_path)
    with pytest.raises(RuntimeError, match="health is stale"):
        require_recent_account_owner_health(
            tmp_path,
            environment="demo",
            max_age_ns=10_000,
            now_ns=1_000_000,
            expected_account_id="demo-account",
            head_binding="allow_behind",
        )


def test_allow_behind_rejects_health_ahead_of_the_journal(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace

    from liquidity_migration.account_owner_health import write_account_owner_health

    _kernel, health = _behind_by_one_root(tmp_path)
    write_account_owner_health(tmp_path, dc_replace(health, journal_sequence=99))
    with pytest.raises(RuntimeError, match="journal sequence mismatch"):
        require_recent_account_owner_health(
            tmp_path,
            environment="demo",
            max_age_ns=10_000,
            now_ns=12_500,
            expected_account_id="demo-account",
            head_binding="allow_behind",
        )


def test_allow_behind_rejects_equal_sequence_hash_disagreement(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace

    from liquidity_migration.account_owner_health import write_account_owner_health

    kernel, health = _behind_by_one_root(tmp_path)
    doctored = dc_replace(
        health,
        journal_sequence=kernel._state_ref().events_applied,
        journal_state_hash="f" * 64,
    )
    write_account_owner_health(tmp_path, doctored)
    with pytest.raises(RuntimeError, match="state hash mismatch"):
        require_recent_account_owner_health(
            tmp_path,
            environment="demo",
            max_age_ns=10_000,
            now_ns=12_500,
            expected_account_id="demo-account",
            head_binding="allow_behind",
        )


def test_exact_binding_remains_the_default_for_sizing_consumers(tmp_path: Path) -> None:
    _behind_by_one_root(tmp_path)
    with pytest.raises(RuntimeError, match="journal sequence mismatch"):
        require_recent_account_owner_health(
            tmp_path,
            environment="demo",
            max_age_ns=10_000,
            now_ns=12_500,
            expected_account_id="demo-account",
        )
