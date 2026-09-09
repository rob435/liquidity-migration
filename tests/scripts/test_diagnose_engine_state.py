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
MANIFEST = ROOT / "deploy" / "fleet_manifest.tsv"


def _diagnose() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    return workflow[workflow.index("\n  diagnose:\n") : workflow.index("\n  vps:\n")]


def _digest_source() -> str:
    diagnose = _diagnose()
    end = diagnose.index('" "$engine_state/heartbeat.json"')
    start = diagnose.rindex('python3 -c "', 0, end) + len('python3 -c "')
    return diagnose[start:end].replace('\\"', '"')


def _worker_digest_source() -> str:
    diagnose = _diagnose()
    end = diagnose.index('" "/var/lib/liquidity-migration-signal-worker-$realm/heartbeat.json"')
    start = diagnose.rindex('python3 -c "', 0, end) + len('python3 -c "')
    return diagnose[start:end].replace('\\"', '"')


def _run_digest(source: str, heartbeat: dict[str, object], tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(heartbeat), encoding="utf-8")
    result = subprocess.run(
        ["python3", "-c", source, str(path)],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _digest(heartbeat: dict[str, object], tmp_path: Path) -> dict[str, object]:
    return _run_digest(_digest_source(), heartbeat, tmp_path)


def _worker_digest(heartbeat: dict[str, object], tmp_path: Path) -> dict[str, object]:
    return _run_digest(_worker_digest_source(), heartbeat, tmp_path)


def _manifest_realms() -> list[str]:
    """Every realm that owns an engine, from the manifest rather than a list here."""
    realms = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        unit, _kind, realm = line.split("|")[:3]
        if unit.startswith("liquidity-migration-engine") and unit.endswith(".service"):
            realms.append(realm)
    return sorted(set(realms))


def test_every_engine_unit_is_read() -> None:
    diagnose = _diagnose()

    # This is the only host reading the incident routine may take, so a realm
    # missing here is a realm the on-call engineer can neither diagnose nor
    # verify a deploy against. The realms come from the manifest so that adding
    # one to the fleet and forgetting it here fails rather than going unread.
    for realm in _manifest_realms():
        assert f" {realm}" in diagnose[: diagnose.index("do case")], realm
    assert "engine_unit=liquidity-migration-engine.service" in diagnose
    assert 'engine_unit="liquidity-migration-engine-$realm.service"' in diagnose
    assert "engine_state=/var/lib/liquidity-migration-engine" in diagnose
    assert 'engine_state="/var/lib/liquidity-migration-engine-$realm"' in diagnose
    assert '"$engine_state/heartbeat.json"' in diagnose


def test_every_realm_watchdog_and_worker_is_read() -> None:
    diagnose = _diagnose()

    assert "liquidity-migration-host-liveness.service" in diagnose
    for realm in _manifest_realms():
        assert f"liquidity-migration-{realm}-liveness.service" in diagnose, realm
        assert f"liquidity-migration-signal-worker-{realm}.service" in diagnose, realm
    for realm in _manifest_realms():
        assert f" {realm}" in diagnose[: diagnose.index('do printf "signal-worker')], realm


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
            "private_stream_ready": False,
            "private_stream_unready_ms": 240_000,
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
    # `may_open` is the latch alone and `private-stream:` is its own alert, so
    # the reading that tells a stuck stream from a sweep in progress is the age
    # the watchdog thresholds on. Without it the digest shows a healthy latch
    # and no sign of the fault the page names.
    assert digest["private_stream_ready"] is False
    assert digest["private_stream_unready_ms"] == 240_000


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


def test_the_worker_digest_carries_the_capped_spool_class_it_pages_on(tmp_path: Path) -> None:
    # Incident `host-22826ce0bb838311`. A spool class at its cap refuses that
    # class's new files, so the lane whose output lands in it stops advancing
    # its cycle and the page names the class. The class name alone does not say
    # whether the block is files or bytes, how far past which cap, or whether
    # the engine is retiring anything — and this is the only host reading the
    # incident routine may take. `long_cycle_cadence_ms` is the limit the LONG
    # page thresholds on at three cadences, so without it the verdict in the
    # page cannot be reproduced from the reading either.
    digest = _worker_digest(
        {
            "status": "degraded",
            "updated_at_ms": 1_788_937_863_247,
            "last_long_cycle_completed_wall_ts_ms": 1_788_937_493_362,
            "long_cycle_cadence_ms": 60_000,
            "carry_cycle_not_before_wall_ts_ms": 1_788_913_200_000,
            "spool_backpressured": False,
            "spool_backpressured_classes": ["current"],
            "spool_files": 11,
            "spool_bytes": 52_118,
            "spool_class_files": {"current": 8, "lifecycle": 1},
            "spool_class_file_caps": {"current": 8, "lifecycle": 512},
            "spool_class_bytes": {"current": 41_234},
            "spool_class_byte_caps": {"current": 536_346_624},
            "spool_class_byte_soft_thresholds": {"current": 536_215_552},
            "replaceable_outputs_coalesced": 4_312,
            "account_user_id": "1234567",
        },
        tmp_path,
    )

    assert digest["spool_backpressured_classes"] == ["current"]
    assert digest["spool_class_files"] == {"current": 8, "lifecycle": 1}
    assert digest["spool_class_file_caps"] == {"current": 8, "lifecycle": 512}
    assert digest["spool_class_bytes"] == {"current": 41_234}
    assert digest["spool_class_byte_caps"] == {"current": 536_346_624}
    assert digest["spool_class_byte_soft_thresholds"] == {"current": 536_215_552}
    assert digest["spool_files"] == 11
    assert digest["spool_bytes"] == 52_118
    assert digest["replaceable_outputs_coalesced"] == 4_312
    assert digest["long_cycle_cadence_ms"] == 60_000
    assert digest["carry_cycle_not_before_wall_ts_ms"] == 1_788_913_200_000
    assert "account_user_id" not in digest


def test_the_digest_never_prints_the_account_identity(tmp_path: Path) -> None:
    digest = _digest({"account_user_id": "1234567", "may_open": True}, tmp_path)

    assert "account_user_id" not in digest


def test_a_heartbeat_missing_fields_still_reports(tmp_path: Path) -> None:
    digest = _digest({}, tmp_path)

    assert digest["may_open"] is None
    assert digest["entry_blockers"] == 0
    assert digest["entry_blocker_reasons"] == []
