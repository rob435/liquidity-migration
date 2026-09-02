from __future__ import annotations

import csv
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy" / "fleet_manifest.tsv"
SYSTEMD = ROOT / "deploy" / "systemd"


@dataclass(frozen=True)
class FleetUnit:
    unit: str
    kind: str
    realm: str
    lifecycle: str
    stop_order: int
    activation: str
    operator: str
    health: str
    artifact: str | None


def _optional_text(value: str) -> str | None:
    return None if value == "-" else value


def _manifest() -> list[FleetUnit]:
    lines = [
        line
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    rows = list(csv.reader(lines, delimiter="|"))
    assert rows and all(len(row) == 16 for row in rows)
    return [
        FleetUnit(
            unit=row[0],
            kind=row[1],
            realm=row[2],
            lifecycle=row[3],
            stop_order=int(row[4]),
            activation=row[5],
            operator=row[6],
            health=row[8],
            artifact=_optional_text(row[9]),
        )
        for row in rows
    ]


def _helper(command: str) -> list[str]:
    completed = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; . deploy/lib_sleeves.sh; {command}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.splitlines()


def test_manifest_is_strict_and_exactly_names_the_systemd_inventory() -> None:
    _helper("lm_validate_fleet_manifest")
    current = {row.unit for row in _manifest()}
    files = {
        path.name
        for path in SYSTEMD.glob("liquidity-migration-*.*")
        if path.suffix in {".service", ".timer"}
    }
    assert current == files


def test_directional_runtime_units_are_manifest_derived() -> None:
    assert _helper("lm_signal_worker_unit demo") == [
        "liquidity-migration-signal-worker-demo.service"
    ]
    assert _helper("lm_signal_worker_unit mainnet") == [
        "liquidity-migration-signal-worker-mainnet.service"
    ]
    assert _helper("lm_owner_unit demo") == ["liquidity-migration-engine.service"]
    assert _helper("lm_owner_unit mainnet") == ["liquidity-migration-engine-mainnet.service"]


def test_heartbeat_artifacts_are_manifest_derived() -> None:
    for row in _manifest():
        if row.artifact is None:
            continue
        assert _helper(f"lm_output_artifact_for_unit {row.unit}") == [row.artifact]


def test_operator_status_inventory_is_exactly_manifest_derived() -> None:
    rows = _manifest()
    expected: list[str] = []
    for row in sorted(
        (
            row
            for row in rows
            if row.kind == "service"
            and (
                row.lifecycle == "owner"
                or row.unit == f"liquidity-migration-signal-worker-{row.realm}.service"
            )
        ),
        key=lambda row: (
            0 if row.realm == "demo" else 1,
            0 if row.lifecycle == "owner" else 1,
            "-" if row.lifecycle == "owner" else "directional",
            row.unit,
        ),
    ):
        role = "owner" if row.lifecycle == "owner" else "signal"
        sleeve = "-" if role == "owner" else "directional"
        expected.append(f"{row.unit}|{row.realm}|{role}|{sleeve}")

    actual = _helper("lm_operator_status_rows")
    assert actual == expected

    helper = (ROOT / "deploy" / "telegram_control_helper.sh").read_text(encoding="utf-8")
    assert "lm_operator_status_rows" in helper
    assert "status-fleet) status_fleet" in helper


def test_activation_unit_sets_and_immediate_jobs_are_manifest_derived() -> None:
    rows = _manifest()

    def expected_activation(realm: str) -> list[str]:
        activation = "always" if realm == "demo" else "mainnet"
        realms = {"demo", "shared"} if realm == "demo" else {"mainnet"}
        return [
            row.unit
            for row in sorted(rows, key=lambda row: row.stop_order, reverse=True)
            if row.lifecycle == "downstream"
            and (
                (row.activation == activation and row.realm in realms)
                or (row.activation == "job-now" and row.realm == realm)
            )
            and row.artifact is None
        ]

    for realm in ("demo", "mainnet"):
        expected = expected_activation(realm)
        assert _helper(f"lm_activation_units {realm} start") == expected
        assert _helper(f"lm_activation_units {realm} stop") == list(reversed(expected))
        assert _helper(f"lm_immediate_timer_jobs {realm}") == [
            row.unit
            for row in sorted(rows, key=lambda row: row.stop_order, reverse=True)
            if row.kind == "service"
            and row.realm == realm
            and row.lifecycle == "downstream"
            and row.activation == "job-now"
        ]

    deploy = (ROOT / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    start_realm = deploy[deploy.index("start_realm()") : deploy.index("\nverify_mode()")]
    assert 'lm_activation_units "$realm" start' in start_realm
    assert 'lm_immediate_timer_jobs "$realm"' in start_realm


def test_realm_units_cover_the_funded_stop_surface() -> None:
    rows = _manifest()
    mainnet_units = set(_helper("lm_realm_units mainnet"))
    assert mainnet_units == {row.unit for row in rows if row.realm == "mainnet"}
    deploy = (ROOT / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    assert "lm_realm_units mainnet" in deploy


def test_each_realm_has_one_credential_free_signal_worker() -> None:
    rows = _manifest()
    workers = [
        row
        for row in rows
        if row.kind == "service"
        and row.unit.startswith("liquidity-migration-signal-worker-")
    ]
    assert {row.realm for row in workers} == {"demo", "mainnet"}
    for row in workers:
        assert row.health == "active"
        assert row.artifact == (
            f"/var/lib/liquidity-migration-signal-worker-{row.realm}/heartbeat.json"
        )
        unit = (SYSTEMD / row.unit).read_text(encoding="utf-8")
        assert "UnsetEnvironment=" in unit
        assert "REAL_MONEY" in unit
        assert "BYBIT_REAL_API_KEY" in unit
        assert (
            "ExecStart=/opt/liquidity-migration-engine/bin/signal-worker live" in unit
        )


def test_independent_units_are_shared_never_stopped_by_a_realm_and_recorder_first() -> None:
    rows = _manifest()
    independent = [row for row in rows if row.lifecycle == "independent"]
    assert {row.realm for row in independent} == {"shared"}
    assert {row.unit for row in independent} == {
        "liquidity-migration-forward-capture.service",
        "liquidity-migration-forward-capture-binance.service",
        "liquidity-migration-market-tape-upload.timer",
        "liquidity-migration-market-tape-upload.service",
        "liquidity-migration-backup.timer",
        "liquidity-migration-backup.service",
        "liquidity-migration-host-liveness.timer",
        "liquidity-migration-host-liveness.service",
    }
    ordered = _helper("lm_independent_units")
    assert ordered[0] == "liquidity-migration-forward-capture.service"
    assert ordered == [
        row.unit for row in sorted(independent, key=lambda row: row.stop_order, reverse=True)
    ]
    for realm in ("demo", "mainnet"):
        assert not set(_helper(f"lm_activation_units {realm} start")) & set(ordered)
        assert not set(_helper(f"lm_realm_units {realm}")) & set(ordered)
