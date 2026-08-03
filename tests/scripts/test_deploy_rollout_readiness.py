from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from liquidity_migration.account.account_contracts import AccountEventType, AccountState
from scripts.vps import check_deploy_rollout_readiness as readiness


class _Client:
    demo = True

    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        all_orders: list[dict[str, Any]] | None = None,
        conditional_orders: list[dict[str, Any]] | None = None,
    ) -> None:
        self.positions = positions or []
        self.all_orders = all_orders or []
        self.conditional_orders = conditional_orders or []
        self.order_filters: list[str | None] = []

    def get_positions(self, *, settle_coin: str) -> list[dict[str, Any]]:
        assert settle_coin == "USDT"
        return self.positions

    def get_open_orders(
        self,
        *,
        settle_coin: str,
        order_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        assert settle_coin == "USDT"
        self.order_filters.append(order_filter)
        return self.conditional_orders if order_filter == "StopOrder" else self.all_orders


def _snapshot(now_ns: int, **overrides: Any) -> SimpleNamespace:
    payload: dict[str, Any] = {
        "local_receive_ts_ns": now_ns - 1_000_000,
        "healthy": True,
        "mismatches": [],
        "venue_positions": {},
        "reconstructed_positions": {},
        "metadata": {
            "venue_order_ownership": {
                "status": "verified",
                "unique_orders_observed": 0,
            }
        },
    }
    payload.update(overrides)
    return SimpleNamespace(
        event_type=AccountEventType.VENUE_SNAPSHOT.value,
        payload=payload,
        wall_ts_ns=now_ns - 1_000_000,
    )


def test_readiness_proves_local_health_and_both_bybit_order_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ns = 10_000_000_000
    state = AccountState(events_applied=42)
    health_calls: list[dict[str, Any]] = []
    client = _Client()
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [_snapshot(now_ns)])
    monkeypatch.setattr(readiness, "reduce_account_events", lambda _events: state)
    monkeypatch.setattr(
        readiness,
        "require_recent_account_owner_health",
        lambda *_args, **kwargs: health_calls.append(kwargs),
    )

    result = readiness.require_rollout_readiness(
        account_root=tmp_path,
        head_binding="exact",
        client=client,
        now_ns=now_ns,
    )

    assert result == readiness.RolloutReadiness(
        journal_sequence=42,
        local_positions=0,
        aggregate_targets=0,
        working_orders=0,
        venue_positions=0,
        venue_orders=0,
    )
    assert client.order_filters == [None, "StopOrder"]
    assert health_calls == [
        {
            "environment": "demo",
            "max_age_ns": readiness.MAX_EVIDENCE_AGE_NS,
            "max_venue_fact_age_ns": readiness.MAX_EVIDENCE_AGE_NS,
            "now_ns": now_ns,
            "head_binding": "exact",
        }
    ]


def test_runtime_clock_is_resampled_after_full_journal_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter((10_000_000_000, 30_000_000_000))
    health_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(readiness.time, "time_ns", lambda: next(times))
    monkeypatch.setattr(
        readiness,
        "read_account_journal",
        lambda *_args, **_kwargs: [_snapshot(30_000_000_000)],
    )
    monkeypatch.setattr(
        readiness,
        "reduce_account_events",
        lambda _events: AccountState(events_applied=1),
    )
    monkeypatch.setattr(
        readiness,
        "require_recent_account_owner_health",
        lambda *_args, **kwargs: health_calls.append(kwargs),
    )

    readiness.require_rollout_readiness(
        account_root=tmp_path,
        head_binding="allow_behind",
        client=_Client(),
    )

    assert health_calls[0]["now_ns"] is None


def test_readiness_reports_every_nonflat_or_unverified_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ns = 20_000_000_000
    state = SimpleNamespace(
        positions={"MIRAUSDT": SimpleNamespace(signed_qty=-2.0)},
        aggregate_targets={"MIRAUSDT": -2.0},
        working_symbols=lambda **_kwargs: {"MIRAUSDT"},
        events_applied=9,
    )
    client = _Client(
        positions=[{"symbol": "MIRAUSDT", "side": "Sell", "size": "2"}],
        all_orders=[{"symbol": "MIRAUSDT", "orderId": "regular-1"}],
        conditional_orders=[{"symbol": "MIRAUSDT", "orderId": "stop-1"}],
    )
    snapshot = _snapshot(
        now_ns,
        healthy=False,
        mismatches=["MIRAUSDT:mismatch"],
        venue_positions={"MIRAUSDT": -2.0},
        reconstructed_positions={"MIRAUSDT": -2.0},
        metadata={
            "venue_order_ownership": {
                "status": "unowned",
                "unique_orders_observed": 2,
            }
        },
    )
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [snapshot])
    monkeypatch.setattr(readiness, "reduce_account_events", lambda _events: state)
    monkeypatch.setattr(readiness, "require_recent_account_owner_health", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError) as captured:
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="allow_behind",
            client=client,
            now_ns=now_ns,
        )

    message = str(captured.value)
    for expected in (
        "canonical positions are non-flat",
        "canonical aggregate targets are non-flat",
        "canonical working orders remain",
        "venue reconciliation snapshot is unhealthy",
        "authenticated venue snapshot is non-flat",
        "venue snapshot reconstruction is non-flat",
        "venue order inventory is not empty and verified",
        "authenticated Bybit positions are non-flat",
        "authenticated Bybit open-order inventory is non-empty: count=2",
    ):
        assert expected in message


def test_readiness_skips_owner_health_only_after_owner_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ns = 30_000_000_000
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [_snapshot(now_ns)])
    monkeypatch.setattr(
        readiness,
        "reduce_account_events",
        lambda _events: AccountState(events_applied=12),
    )
    monkeypatch.setattr(
        readiness,
        "require_recent_account_owner_health",
        lambda *_args, **_kwargs: pytest.fail("stopped-owner check must not require live health"),
    )

    result = readiness.require_rollout_readiness(
        account_root=tmp_path,
        head_binding="none",
        client=_Client(),
        now_ns=now_ns,
    )

    assert result.journal_sequence == 12


def test_readiness_rejects_stale_evidence_and_non_demo_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ns = 100_000_000_000
    stale = _snapshot(now_ns)
    stale.payload["local_receive_ts_ns"] = now_ns - readiness.MAX_STOPPED_SNAPSHOT_AGE_NS - 1
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [stale])
    monkeypatch.setattr(
        readiness,
        "reduce_account_events",
        lambda _events: AccountState(events_applied=1),
    )

    with pytest.raises(RuntimeError, match="not fresh"):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="none",
            client=_Client(),
            now_ns=now_ns,
        )

    stopped = readiness.require_rollout_readiness(
        account_root=tmp_path,
        head_binding="stopped-maintenance",
        client=_Client(),
        now_ns=now_ns,
    )
    assert stopped.journal_sequence == 1

    # The readiness proof is realm-agnostic -- a funded account has to be
    # provable flat too -- but still refuses a client whose declared realm and
    # transport disagree, because it would then be proving the wrong book flat.
    contradictory = _Client()
    contradictory.demo = False
    with pytest.raises(ValueError, match="contradicts its demo realm"):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="none",
            client=contradictory,
            now_ns=now_ns,
        )

    # A coherent mainnet client gets past the realm check entirely: it now fails
    # on the evidence, exactly as the demo client above does, rather than on its
    # realm. A funded account has to be provable flat too.
    mainnet = _Client()
    mainnet.demo = False
    mainnet.realm = "mainnet"
    with pytest.raises(RuntimeError, match="not fresh"):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="none",
            client=mainnet,
            now_ns=now_ns,
        )


@pytest.mark.parametrize("size", ("nan", "invalid", -1))
def test_readiness_fails_closed_on_malformed_bybit_position_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: object,
) -> None:
    now_ns = 200_000_000_000
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [_snapshot(now_ns)])
    monkeypatch.setattr(
        readiness,
        "reduce_account_events",
        lambda _events: AccountState(events_applied=2),
    )

    with pytest.raises(ValueError, match="invalid size"):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="none",
            client=_Client(positions=[{"symbol": "BADUSDT", "size": size}]),
            now_ns=now_ns,
        )


def test_flat_book_between_checkpoints_still_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flat book journals nothing between ten-minute checkpoints."""

    now_ns = 3_000_000_000_000
    snapshot = _snapshot(now_ns)
    snapshot.payload["local_receive_ts_ns"] = now_ns - 9 * 60 * 1_000_000_000
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [snapshot])
    monkeypatch.setattr(
        readiness,
        "reduce_account_events",
        lambda _events: AccountState(events_applied=7),
    )

    result = readiness.require_rollout_readiness(
        account_root=tmp_path,
        head_binding="none",
        client=_Client(),
        now_ns=now_ns,
    )

    assert result.journal_sequence == 7


def test_stopped_journal_beyond_three_checkpoints_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ns = 3_000_000_000_000
    snapshot = _snapshot(now_ns)
    snapshot.payload["local_receive_ts_ns"] = now_ns - 31 * 60 * 1_000_000_000
    monkeypatch.setattr(readiness, "read_account_journal", lambda *_args, **_kwargs: [snapshot])
    monkeypatch.setattr(
        readiness,
        "reduce_account_events",
        lambda _events: AccountState(events_applied=7),
    )

    with pytest.raises(
        RuntimeError,
        match="latest venue reconciliation snapshot is not fresh",
    ):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="none",
            client=_Client(),
            now_ns=now_ns,
        )


def test_running_phase_requires_fresh_venue_facts_from_the_owner(tmp_path: Path) -> None:
    """Fresh health over a wedged venue loop must not clear a running rollout."""

    from liquidity_migration.account.account_kernel import AccountExecutionKernel
    from liquidity_migration.account.account_owner_health import (
        TEST_ACCOUNT_OWNER_INVOCATION_ID,
        AccountOwnerHealth,
        write_account_owner_health,
    )

    now_ns = 3_000_000_000_000
    kernel = AccountExecutionKernel(tmp_path, account_id="demo")
    kernel.record_venue_snapshot(
        snapshot_key="flat",
        venue_positions={},
        reconstructed_positions={},
        mismatches=(),
        exchange_ts_ns=0,
        local_receive_ts_ns=now_ns - 1_000_000,
        metadata={
            "venue_order_ownership": {"status": "verified", "unique_orders_observed": 0}
        },
    )
    state = kernel._state_ref()
    write_account_owner_health(
        tmp_path,
        AccountOwnerHealth(
            owner="account_execution",
            environment="demo",
            account_id="demo",
            status="healthy",
            observed_ts_ns=now_ns - 1_000_000,
            loop_sequence=1,
            journal_sequence=state.events_applied,
            journal_state_hash=state.rolling_state_hash,
            equity_usdt=10_000.0,
            available_margin_usdt=9_000.0,
            requested_symbols_ready=True,
            venue_facts_at_ns=now_ns - 90_000_000_000,
            venue_facts_healthy=True,
            invocation_id=TEST_ACCOUNT_OWNER_INVOCATION_ID,
        ),
    )

    with pytest.raises(RuntimeError, match="account-owner venue facts are stale"):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="exact",
            client=_Client(),
            now_ns=now_ns,
        )
