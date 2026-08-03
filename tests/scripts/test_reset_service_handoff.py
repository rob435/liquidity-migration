"""Fail-closed handoff behavior of the demo ledger reset.

A reset that stopped the fleet but cannot restore it must end with every
managed unit verifiably stopped — a partial topology (producers without their
owner, or an owner mid-epoch) is worse than an outage.
"""

from __future__ import annotations

import pytest

from liquidity_migration.ops.demo_ledger_reset import (
    DOWNSTREAM_RESTART_UNITS,
    OWNER_RESTART_UNITS,
    RESTART_UNITS,
    STOP_UNITS,
    Execution,
    ResetOptions,
)
from tests.scripts.test_reset_runtime_contract import FakeSystemctl

_OWNER = OWNER_RESTART_UNITS[0]
_WORKER = DOWNSTREAM_RESTART_UNITS[0]


@pytest.mark.parametrize("failed_unit", [_OWNER, _WORKER])
def test_failed_post_clear_restart_fails_closed_to_all_inactive(failed_unit: str) -> None:
    systemctl = FakeSystemctl(fail_start={failed_unit})
    execution = Execution(
        systemctl=systemctl,
        options=ResetOptions(settle_seconds=0),
        active_before=RESTART_UNITS,
        services_stopped=True,
        failure_recovery_allowed=True,
    )
    execution.fail_closed_cleanup()
    # Recovery was attempted, failed at the parametrized unit, and the fleet
    # was then stopped whole — with nothing left active.
    stops = [unit for verb, unit in systemctl.calls if verb == "stop"]
    assert stops == list(STOP_UNITS)
    assert not systemctl.active
    if failed_unit == _OWNER:
        # A dead owner must keep every downstream producer un-started.
        starts = [unit for verb, unit in systemctl.calls if verb == "start"]
        assert starts == [_OWNER]


def test_failed_stop_during_fail_closed_is_reported_critical(
    capsys: pytest.CaptureFixture,
) -> None:
    stuck = DOWNSTREAM_RESTART_UNITS[1]
    systemctl = FakeSystemctl(fail_start={_OWNER}, fail_stop={stuck})
    systemctl.active.add(stuck)
    execution = Execution(
        systemctl=systemctl,
        options=ResetOptions(settle_seconds=0),
        active_before=RESTART_UNITS,
        services_stopped=True,
        failure_recovery_allowed=True,
    )
    execution.fail_closed_cleanup()
    captured = capsys.readouterr()
    assert "CRITICAL: at least one managed unit could not be stopped." in captured.err
    assert f"STILL ACTIVE after failed handoff: {stuck}" in captured.err


def test_successful_recovery_restores_the_previously_active_set() -> None:
    systemctl = FakeSystemctl()
    execution = Execution(
        systemctl=systemctl,
        options=ResetOptions(settle_seconds=0),
        active_before=(_OWNER, _WORKER),
        services_stopped=True,
        failure_recovery_allowed=True,
    )
    execution.fail_closed_cleanup()
    starts = [unit for verb, unit in systemctl.calls if verb == "start"]
    assert starts == [_OWNER, _WORKER]
    stops = [unit for verb, unit in systemctl.calls if verb == "stop"]
    assert stops == []
