from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from liquidity_migration.account_contracts import AccountEventType, AccountState
from scripts import check_deploy_rollout_readiness as readiness


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
            "now_ns": now_ns,
            "head_binding": "exact",
        }
    ]


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
    stale.payload["local_receive_ts_ns"] = now_ns - readiness.MAX_EVIDENCE_AGE_NS - 1
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

    non_demo = _Client()
    non_demo.demo = False
    with pytest.raises(ValueError, match="non-demo"):
        readiness.require_rollout_readiness(
            account_root=tmp_path,
            head_binding="none",
            client=non_demo,
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
