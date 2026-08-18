"""The rollout flat gate, on the evidence that exists since the 2026-08-14 pivot.

The gate proves flatness from the venue directly, plus — for the phases where
the fleet is still running — the engine's own heartbeat. The bug this file
guards against re-introducing: a gate that reads a file whose writer is dead
(the deleted Python owner's journal and health file) can never clear, and it
blocked exactly the strict rollouts it existed to protect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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


OBSERVED_MS = 1_755_000_000_000
NOW_NS = (OBSERVED_MS + 1_000) * 1_000_000


def _heartbeat(tmp_path: Path, *, positions: list | None = None, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "account_equity_usdt": 1412.58,
        "account_available_usdt": 700.0,
        "account_observed_wall_ts_ms": OBSERVED_MS,
        "account_user_id": "555899665",
        "realm": "demo",
        "positions": positions if positions is not None else [],
    }
    payload.update(overrides)
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_running_phase_passes_on_fresh_empty_heartbeat_and_flat_venue(
    tmp_path: Path,
) -> None:
    client = _Client()
    result = readiness.require_rollout_readiness(
        head_binding="exact",
        client=client,
        now_ns=NOW_NS,
        heartbeat_path=_heartbeat(tmp_path),
    )
    assert result.engine_checked is True
    # Both venue order surfaces were read: regular and conditional.
    assert client.order_filters == [None, "StopOrder"]


def test_running_phase_blocks_when_the_engine_says_it_holds_something(
    tmp_path: Path,
) -> None:
    heartbeat = _heartbeat(tmp_path, positions=[{"symbol": "KAITOUSDT"}])
    with pytest.raises(RuntimeError, match="still holds.*KAITOUSDT"):
        readiness.require_rollout_readiness(
            head_binding="allow_behind",
            client=_Client(),
            now_ns=NOW_NS,
            heartbeat_path=heartbeat,
        )


def test_running_phase_blocks_on_a_missing_or_stale_heartbeat(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="engine account reading is not usable"):
        readiness.require_rollout_readiness(
            head_binding="exact",
            client=_Client(),
            now_ns=NOW_NS,
            heartbeat_path=tmp_path / "absent.json",
        )

    stale = _heartbeat(
        tmp_path,
        account_observed_wall_ts_ms=OBSERVED_MS
        - readiness.MAX_EVIDENCE_AGE_NS // 1_000_000,
    )
    with pytest.raises(RuntimeError, match="engine account reading is not usable"):
        readiness.require_rollout_readiness(
            head_binding="exact",
            client=_Client(),
            now_ns=NOW_NS,
            heartbeat_path=stale,
        )


def test_running_phase_blocks_when_the_engine_cannot_say_what_it_holds(
    tmp_path: Path,
) -> None:
    # positions=None is an engine too old to publish holdings: not the same
    # as holding nothing, and not enough to prove flat.
    heartbeat = _heartbeat(tmp_path, positions=None)
    heartbeat.write_text(
        heartbeat.read_text(encoding="utf-8").replace("[]", "null"), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="does not say what is held"):
        readiness.require_rollout_readiness(
            head_binding="exact",
            client=_Client(),
            now_ns=NOW_NS,
            heartbeat_path=heartbeat,
        )


def test_stopped_phases_skip_the_heartbeat_but_the_venue_still_binds(
    tmp_path: Path,
) -> None:
    # No heartbeat anywhere: a stopped engine's file is legitimately frozen,
    # so the stopped phases must not demand it.
    for binding in ("none", "stopped-maintenance"):
        result = readiness.require_rollout_readiness(
            head_binding=binding,
            client=_Client(),
            now_ns=NOW_NS,
            heartbeat_path=tmp_path / "absent.json",
        )
        assert result.engine_checked is False

    with pytest.raises(RuntimeError, match="authenticated Bybit positions are non-flat"):
        readiness.require_rollout_readiness(
            head_binding="none",
            client=_Client(
                positions=[{"symbol": "MIRAUSDT", "side": "Sell", "size": "2"}]
            ),
            now_ns=NOW_NS,
            heartbeat_path=tmp_path / "absent.json",
        )


def test_every_nonflat_venue_surface_is_reported(tmp_path: Path) -> None:
    client = _Client(
        positions=[{"symbol": "MIRAUSDT", "side": "Sell", "size": "2"}],
        all_orders=[{"symbol": "MIRAUSDT", "orderId": "regular-1"}],
        conditional_orders=[{"symbol": "MIRAUSDT", "orderId": "stop-1"}],
    )
    with pytest.raises(RuntimeError) as captured:
        readiness.require_rollout_readiness(
            head_binding="exact",
            client=client,
            now_ns=NOW_NS,
            heartbeat_path=_heartbeat(tmp_path, positions=[{"symbol": "MIRAUSDT"}]),
        )
    message = str(captured.value)
    for expected in (
        "the engine says the account still holds",
        "authenticated Bybit positions are non-flat",
        "authenticated Bybit open-order inventory is non-empty: count=2",
    ):
        assert expected in message


def test_a_client_whose_realm_contradicts_demo_is_refused(tmp_path: Path) -> None:
    # The proof is realm-checked: a client whose declared realm and transport
    # disagree would be proving the wrong book flat.
    contradictory = _Client()
    contradictory.demo = False
    with pytest.raises(ValueError, match="contradicts its demo realm"):
        readiness.require_rollout_readiness(
            head_binding="none",
            client=contradictory,
            now_ns=NOW_NS,
            heartbeat_path=_heartbeat(tmp_path),
        )


def test_an_unknown_head_binding_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="head binding must be"):
        readiness.require_rollout_readiness(
            head_binding="sideways",
            client=_Client(),
            now_ns=NOW_NS,
            heartbeat_path=_heartbeat(tmp_path),
        )
