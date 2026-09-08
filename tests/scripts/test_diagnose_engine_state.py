"""`mode=diagnose` must read the engine the funded alerts are about.

`may-open:`, `rolling-loss:` and `strategy-errors:` all come from an engine
heartbeat, and the diagnose recipe read every unit except the two engines: an
on-call session with no host access saw the watchdog's verdict and neither the
heartbeat that produced it nor the engine journal around it. Incident
`mainnet-ac90e31c207bc0da` paged `may-open:liquidity-migration-engine-mainnet.service`
every 30 s with no reading that separates a latched reconciliation halt from a
private stream that is merely down.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "vps-deploy.yml"


def _diagnose() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    return workflow[workflow.index("\n  diagnose:\n") : workflow.index("\n  vps:\n")]


def _digest_source() -> str:
    diagnose = _diagnose()
    end = diagnose.index('" "$engine_state/heartbeat.json"')
    start = diagnose.rindex('python3 -c "', 0, end) + len('python3 -c "')
    return diagnose[start:end].replace('\\"', '"')


def _digest(heartbeat: dict[str, object], tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(heartbeat), encoding="utf-8")
    result = subprocess.run(
        ["python3", "-c", _digest_source(), str(path)],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_both_engine_units_are_read() -> None:
    diagnose = _diagnose()

    assert "engine_unit=liquidity-migration-engine.service" in diagnose
    assert "engine_unit=liquidity-migration-engine-mainnet.service" in diagnose
    assert "engine_state=/var/lib/liquidity-migration-engine" in diagnose
    assert "engine_state=/var/lib/liquidity-migration-engine-mainnet" in diagnose
    assert '"$engine_state/heartbeat.json"' in diagnose


def test_the_engine_journal_reaches_the_run() -> None:
    diagnose = _diagnose()

    assert 'journalctl -u "$engine_unit" -n 40' in diagnose


def test_the_digest_carries_the_verdicts_the_watchdog_alerts_on(tmp_path: Path) -> None:
    digest = _digest(
        {
            "realm": "mainnet",
            "may_open": False,
            "rolling_loss_tripped": False,
            "rolling_loss_net_usdt": 12.5,
            "rolling_loss_limit_usdt": 162.7,
            "strategy_errors": [{"strategy": "CARRY", "error": "boom"}],
            "stream_resets": 7,
            "pid": 3272995,
            "uptime_s": 16800,
        },
        tmp_path,
    )

    assert digest["realm"] == "mainnet"
    assert digest["may_open"] is False
    assert digest["rolling_loss_tripped"] is False
    assert digest["rolling_loss_net_usdt"] == 12.5
    assert digest["strategy_errors"] == [{"strategy": "CARRY", "error": "boom"}]
    # The counter that separates a private stream still resetting from an
    # engine that has latched entries off and will not clear on its own.
    assert digest["stream_resets"] == 7


def test_the_digest_bounds_the_long_rows(tmp_path: Path) -> None:
    digest = _digest(
        {
            "entry_blockers": [
                {"strategy": "CARRY", "symbol": "ACEUSDT", "reason": "engine latched"},
                {"strategy": "CARRY", "symbol": "ARBUSDT", "reason": "engine latched"},
                {"strategy": "LONG", "symbol": "INJUSDT", "reason": "stream not ready"},
            ],
            "working_entries": [{"strategy": "LONG", "symbol": "INJUSDT"}],
            "positions": [{"symbol": "ACEUSDT"}, {"symbol": "ARBUSDT"}],
        },
        tmp_path,
    )

    assert digest["entry_blockers"] == 3
    assert digest["entry_blocker_reasons"] == ["engine latched", "stream not ready"]
    assert digest["working_entries"] == 1
    assert digest["positions"] == 2


def test_the_digest_never_prints_the_account_identity(tmp_path: Path) -> None:
    digest = _digest({"account_user_id": "1234567", "may_open": True}, tmp_path)

    assert "account_user_id" not in digest


def test_a_heartbeat_missing_fields_still_reports(tmp_path: Path) -> None:
    digest = _digest({}, tmp_path)

    assert digest["may_open"] is None
    assert digest["entry_blockers"] == 0
    assert digest["entry_blocker_reasons"] == []
