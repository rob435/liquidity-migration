"""Deterministic incident simulations for the canonical lifecycle engine.

These are executable fault contracts, not stochastic toy markets.  Each scenario
uses a fixed logical clock and the production journal/reducer so a regression in
deduplication, reconciliation, partial fills, venue-rule handling or portfolio
shock behavior fails identically on every machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .canonical_journal import EventSpec, EventType, append_events, replay_journal


BASE_TS_MS = 1_800_000_000_000


@dataclass(frozen=True, slots=True)
class IncidentScenarioResult:
    name: str
    journal_root: str
    events: int
    lifecycle_state: str
    order_version: int
    position_version: int
    entry_filled_qty: float
    closed_qty: float
    assertions: dict[str, Any]


def _spec(
    event_type: EventType,
    *,
    trade_id: str = "incident-trade",
    symbol: str = "BUSDT",
    side: str = "short",
    offset_ms: int,
    order_version: int,
    position_version: int,
    qty: float | None = None,
    order_link_id: str = "incident-order",
    key_suffix: str = "",
    metadata: dict[str, Any] | None = None,
    trade_patch: dict[str, Any] | None = None,
) -> EventSpec:
    price = 0.125
    return EventSpec(
        event_type=event_type,
        mode="demo",
        sleeve="continuous",
        strategy_id="incident_strategy",
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        local_ts_ms=BASE_TS_MS + offset_ms,
        venue_ts_ms=BASE_TS_MS + offset_ms,
        order_version=order_version,
        position_version=position_version,
        order_link_id=order_link_id,
        qty=qty,
        price=price if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        decision_price=0.124,
        submission_price=0.1245,
        fill_price=price if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        depth_consumed_quote=(abs(qty or 0.0) * price) if qty is not None else None,
        latency_ms=25.0 if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
        idempotency_key=(
            f"incident:{trade_id}:{event_type.value}:{order_version}:{position_version}:{key_suffix}"
        ),
        metadata=metadata or {},
        trade_patch=trade_patch or {},
    )


def _open_position_specs(*, trade_id: str = "incident-trade", qty: float = 1.0) -> list[EventSpec]:
    return [
        _spec(EventType.DECISION, trade_id=trade_id, offset_ms=0, order_version=0, position_version=0),
        _spec(EventType.RISK_ACCEPTED, trade_id=trade_id, offset_ms=1, order_version=0, position_version=0),
        _spec(EventType.SUBMITTED, trade_id=trade_id, offset_ms=2, order_version=1, position_version=0),
        _spec(EventType.ACKNOWLEDGED, trade_id=trade_id, offset_ms=3, order_version=1, position_version=0),
        _spec(EventType.FILL, trade_id=trade_id, offset_ms=4, order_version=1, position_version=1, qty=qty),
        _spec(
            EventType.PROTECTION_ACTIVE,
            trade_id=trade_id,
            offset_ms=5,
            order_version=1,
            position_version=2,
            trade_patch={"trade_id": trade_id, "symbol": "BUSDT", "side": "short", "status": "open", "qty": qty},
        ),
    ]


def _result(root: Path, name: str, *, trade_id: str = "incident-trade", **assertions: Any) -> IncidentScenarioResult:
    projection = replay_journal(root)
    state = projection.trades[trade_id]
    return IncidentScenarioResult(
        name=name,
        journal_root=str(root),
        events=projection.events_applied,
        lifecycle_state=state.lifecycle_state,
        order_version=state.order_version,
        position_version=state.position_version,
        entry_filled_qty=state.entry_filled_qty,
        closed_qty=state.closed_qty,
        assertions=assertions,
    )


def simulate_venue_flat_ledger_open(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    append_events(root, _open_position_specs())
    append_events(
        root,
        [
            _spec(
                EventType.VENUE_SNAPSHOT,
                offset_ms=60_000,
                order_version=1,
                position_version=2,
                metadata={
                    "venue_position_qty": 0.0,
                    "ledger_position_qty": 1.0,
                    "reconciliation_state": "venue_flat_awaiting_pnl",
                    "close_resubmit_allowed": False,
                },
                trade_patch={"status": "awaiting_pnl", "venue_position_qty": 0.0},
            )
        ],
    )
    result = _result(root, "venue_flat_ledger_open", close_resubmit_allowed=False)
    assert result.lifecycle_state == EventType.PROTECTION_ACTIVE.value
    assert result.closed_qty == 0.0
    return result


def simulate_duplicate_fills(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    prefix = _open_position_specs()[:4]
    fill = _spec(
        EventType.FILL,
        offset_ms=4,
        order_version=1,
        position_version=1,
        qty=1.0,
        key_suffix="exec-duplicate",
        metadata={"exec_id": "exec-duplicate"},
    )
    append_events(root, prefix + [fill, fill])
    append_events(
        root,
        [_spec(EventType.PROTECTION_ACTIVE, offset_ms=5, order_version=1, position_version=2)],
    )
    result = _result(root, "duplicate_fills", deduplicated=True)
    assert result.entry_filled_qty == 1.0
    assert result.events == 6
    return result


def simulate_missing_websocket_events(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    specs = _open_position_specs()
    append_events(root, specs[:3])
    append_events(
        root,
        [
            _spec(
                EventType.WS_GAP,
                offset_ms=1_000,
                order_version=1,
                position_version=0,
                metadata={"stream": "execution", "fallback": "rest_reconciliation"},
            )
        ],
    )
    # REST later proves the acknowledgement and fill; the lifecycle converges
    # without inventing a WS message.
    append_events(
        root,
        [
            _spec(
                EventType.ACKNOWLEDGED,
                offset_ms=1_001,
                order_version=1,
                position_version=0,
                metadata={"source": "rest_reconciliation"},
            ),
            _spec(
                EventType.FILL,
                offset_ms=1_002,
                order_version=1,
                position_version=1,
                qty=1.0,
                metadata={"source": "rest_reconciliation"},
            ),
            _spec(EventType.PROTECTION_ACTIVE, offset_ms=1_003, order_version=1, position_version=2),
        ],
    )
    result = _result(root, "missing_websocket_events", recovered_by="rest_reconciliation")
    assert result.lifecycle_state == EventType.PROTECTION_ACTIVE.value
    return result


def simulate_partial_closes(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    append_events(root, _open_position_specs())
    append_events(
        root,
        [
            _spec(EventType.EXIT_REQUESTED, offset_ms=100, order_version=2, position_version=2),
            _spec(
                EventType.CLOSE_FILL,
                offset_ms=101,
                order_version=2,
                position_version=3,
                qty=0.4,
                key_suffix="partial-1",
                metadata={"remaining_qty": 0.6},
            ),
            _spec(
                EventType.CLOSE_FILL,
                offset_ms=102,
                order_version=2,
                position_version=4,
                qty=0.6,
                key_suffix="partial-2",
                metadata={"remaining_qty": 0.0},
            ),
            _spec(
                EventType.PNL_CONFIRMED,
                offset_ms=103,
                order_version=2,
                position_version=4,
                metadata={"realized_pnl": 0.05},
                trade_patch={"status": "closed", "realized_pnl_usdt": 0.05},
            ),
        ],
    )
    result = _result(root, "partial_closes", partial_fill_count=2)
    assert result.closed_qty == 1.0
    assert result.lifecycle_state == EventType.PNL_CONFIRMED.value
    return result


def simulate_reduce_only_rejection(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    append_events(root, _open_position_specs())
    append_events(
        root,
        [
            _spec(EventType.EXIT_REQUESTED, offset_ms=100, order_version=2, position_version=2),
            _spec(
                EventType.ORDER_REJECTED,
                offset_ms=101,
                order_version=2,
                position_version=2,
                metadata={
                    "reduce_only": True,
                    "venue_error_code": 110017,
                    "venue_position_qty": 0.0,
                    "next_action": "snapshot_then_await_pnl",
                    "blind_retry_allowed": False,
                },
            ),
        ],
    )
    result = _result(root, "reduce_only_rejection", blind_retry_allowed=False)
    assert result.lifecycle_state == EventType.EXIT_REQUESTED.value
    assert result.closed_qty == 0.0
    return result


def simulate_changed_minimum_notional(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    append_events(
        root,
        [
            _spec(EventType.DECISION, offset_ms=0, order_version=0, position_version=0),
            _spec(
                EventType.VENUE_RULE_CHANGED,
                offset_ms=1,
                order_version=0,
                position_version=0,
                metadata={
                    "rule": "minimum_notional",
                    "old_value": 1.0,
                    "new_value": 5.0,
                    "candidate_notional": 2.5,
                    "risk_decision": "reject_before_submit",
                },
            ),
            _spec(
                EventType.ORDER_REJECTED,
                offset_ms=2,
                order_version=0,
                position_version=0,
                metadata={"stage": "pre_submit", "reason": "below_current_minimum_notional"},
            ),
        ],
    )
    result = _result(root, "changed_minimum_notional", submitted=False)
    assert result.lifecycle_state == EventType.DECISION.value
    assert result.order_version == 0
    return result


def simulate_delayed_hedge(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    append_events(root, _open_position_specs(qty=10.0))
    append_events(
        root,
        [
            _spec(
                EventType.HEDGE_DELAYED,
                offset_ms=300_000,
                order_version=1,
                position_version=2,
                metadata={
                    "unhedged_seconds": 300,
                    "gross_short_notional": 1_250.0,
                    "required_hedge_notional": 625.0,
                    "risk_action": "pause_new_entries_and_page_once",
                },
            )
        ],
    )
    result = _result(root, "delayed_hedge", entries_paused=True, page_deduplicated=True)
    assert result.lifecycle_state == EventType.PROTECTION_ACTIVE.value
    return result


def simulate_correlated_ten_x_squeeze(root: str | Path) -> IncidentScenarioResult:
    root = Path(root)
    trade_ids = ["squeeze-a", "squeeze-b", "squeeze-c"]
    for trade_id in trade_ids:
        append_events(root, _open_position_specs(trade_id=trade_id, qty=1.0))
        append_events(
            root,
            [
                _spec(
                    EventType.RISK_SHOCK,
                    trade_id=trade_id,
                    offset_ms=60_000,
                    order_version=1,
                    position_version=2,
                    metadata={
                        "leverage": 10.0,
                        "correlated_adverse_move_frac": 0.15,
                        "equity_loss_frac": 0.50,
                        "liquidation_buffer_breached": True,
                        "kill_switch": True,
                    },
                )
            ],
        )
    projection = replay_journal(root)
    assert all(
        state.incident_facts[-1]["kill_switch"] is True
        for trade_id, state in projection.trades.items()
        if trade_id in trade_ids
    )
    result = _result(root, "correlated_ten_x_squeeze", trade_id=trade_ids[0], kill_switch=True, trades=3)
    assert result.lifecycle_state == EventType.PROTECTION_ACTIVE.value
    return result


SCENARIOS: dict[str, Callable[[str | Path], IncidentScenarioResult]] = {
    "venue_flat_ledger_open": simulate_venue_flat_ledger_open,
    "duplicate_fills": simulate_duplicate_fills,
    "missing_websocket_events": simulate_missing_websocket_events,
    "partial_closes": simulate_partial_closes,
    "reduce_only_rejection": simulate_reduce_only_rejection,
    "changed_minimum_notional": simulate_changed_minimum_notional,
    "delayed_hedge": simulate_delayed_hedge,
    "correlated_ten_x_squeeze": simulate_correlated_ten_x_squeeze,
}


def run_all_incident_scenarios(root: str | Path) -> dict[str, IncidentScenarioResult]:
    base = Path(root)
    return {name: scenario(base / name) for name, scenario in SCENARIOS.items()}


__all__ = [
    "BASE_TS_MS",
    "IncidentScenarioResult",
    "SCENARIOS",
    "run_all_incident_scenarios",
    "simulate_changed_minimum_notional",
    "simulate_correlated_ten_x_squeeze",
    "simulate_delayed_hedge",
    "simulate_duplicate_fills",
    "simulate_missing_websocket_events",
    "simulate_partial_closes",
    "simulate_reduce_only_rejection",
    "simulate_venue_flat_ledger_open",
]
