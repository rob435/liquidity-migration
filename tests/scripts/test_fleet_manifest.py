from __future__ import annotations

import csv
import re
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
    depends_on: tuple[str, ...]
    health: str
    artifact: str | None
    timer_service: str | None
    first_delay_s: int | None
    cadence_s: int | None
    accuracy_s: int | None
    runtime_s: int | None
    input_artifact: str | None


def _optional_text(value: str) -> str | None:
    return None if value == "-" else value


def _optional_int(value: str) -> int | None:
    return None if value == "-" else int(value)


def _manifest() -> list[FleetUnit]:
    lines = [line for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
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
            depends_on=() if row[7] == "-" else tuple(row[7].split(",")),
            health=row[8],
            artifact=_optional_text(row[9]),
            timer_service=_optional_text(row[10]),
            first_delay_s=_optional_int(row[11]),
            cadence_s=_optional_int(row[12]),
            accuracy_s=_optional_int(row[13]),
            runtime_s=_optional_int(row[14]),
            input_artifact=_optional_text(row[15]),
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


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(s|min|h|d|week)", value)
    assert match is not None, value
    scale = {"s": 1, "min": 60, "h": 3_600, "d": 86_400, "week": 604_800}
    return int(match.group(1)) * scale[match.group(2)]


def _calendar_cadence_seconds(value: str) -> int:
    if re.fullmatch(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \*-\*-\* \d\d:\d\d:\d\d(?: UTC)?", value):
        return 604_800
    fixed_daily = re.fullmatch(r"\*-\*-\* \d\d:\d\d:\d\d(?: UTC)?", value)
    if fixed_daily is not None:
        return 86_400
    hourly = re.fullmatch(
        r"\*-\*-\* \*:(\d\d?)(?:/([1-9][0-9]*))?:\d\d(?: UTC)?",
        value,
    )
    assert hourly is not None, value
    return int(hourly.group(2) or 60) * 60


def test_manifest_is_strict_and_exactly_names_the_systemd_inventory() -> None:
    _helper("lm_validate_fleet_manifest; lm_verify_source_systemd_manifest")
    current = {row.unit for row in _manifest()}
    files = {path.name for path in SYSTEMD.glob("liquidity-migration-*.*") if path.suffix in {".service", ".timer"}}
    assert current == files


def test_every_current_service_is_guarded_and_has_one_runtime_dispatch() -> None:
    rows = _manifest()
    services = {row.unit for row in rows if row.kind == "service"}
    guarded = set(_helper("lm_guarded_units"))
    assert guarded == services
    assert {
        "liquidity-migration-engine.service",
        "liquidity-migration-engine-mainnet.service",
        "liquidity-migration-backup.service",
        "liquidity-migration-chaos-drill.service",
    } <= guarded

    wrapper = (ROOT / "scripts" / "run_authorized_runtime.sh").read_text(encoding="utf-8")
    dispatched = re.findall(r"(liquidity-migration-[\w-]+\.service):main", wrapper)
    assert set(dispatched) == services
    assert len(dispatched) == len(set(dispatched))


def test_rollout_order_and_dependencies_are_manifest_derived() -> None:
    rows = _manifest()
    expected_downstream = [
        row.unit
        for row in sorted(
            (row for row in rows if row.lifecycle == "downstream"),
            key=lambda row: row.stop_order,
        )
    ]
    expected_owners = [
        row.unit
        for row in sorted(
            (row for row in rows if row.lifecycle == "owner"),
            key=lambda row: row.stop_order,
        )
    ]
    assert _helper("lm_rollout_units downstream") == expected_downstream
    assert _helper("lm_rollout_units owner") == expected_owners
    assert expected_owners == [
        "liquidity-migration-engine.service",
        "liquidity-migration-engine-mainnet.service",
    ]

    by_name = {row.unit: row for row in rows}
    for row in rows:
        for dependency_name in row.depends_on:
            dependency = by_name[dependency_name]
            if row.lifecycle == dependency.lifecycle:
                assert row.stop_order < dependency.stop_order
            else:
                assert row.lifecycle == "downstream" and dependency.lifecycle == "owner"

    deploy = (ROOT / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    assert "done < <(lm_rollout_units downstream)" in deploy
    assert "done < <(lm_rollout_units owner)" in deploy
    assert "done < <(lm_realm_units mainnet)" in deploy


def test_directional_runtime_units_are_manifest_derived() -> None:
    assert _helper("lm_signal_worker_unit demo") == [
        "liquidity-migration-signal-worker-demo.service"
    ]
    assert _helper("lm_signal_worker_unit mainnet") == [
        "liquidity-migration-signal-worker-mainnet.service"
    ]
    assert _helper("lm_owner_unit demo") == ["liquidity-migration-engine.service"]
    assert _helper("lm_owner_unit mainnet") == ["liquidity-migration-engine-mainnet.service"]


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
    assert {
        "liquidity-migration-engine.service|demo|owner|-",
        "liquidity-migration-engine-mainnet.service|mainnet|owner|-",
        "liquidity-migration-signal-worker-demo.service|demo|signal|directional",
        "liquidity-migration-signal-worker-mainnet.service|mainnet|signal|directional",
    } <= set(actual)

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
    activate = deploy[deploy.index("activate_manifest_units()") : deploy.index("\nstart_required_engine()")]
    assert 'lm_activation_units "$realm" start' in activate
    assert 'lm_immediate_timer_jobs "$realm"' in activate
    demo = deploy[deploy.index("activate_mode()") : deploy.index("\n# The engine owns the funded account.")]
    mainnet = deploy[deploy.index("start_mainnet_fleet()") : deploy.index("\nresolve_fail_safe_python()")]
    assert "activate_manifest_units demo" in demo
    assert "activate_manifest_units mainnet" in mainnet
    literal_units = {
        row.unit
        for row in rows
        if row.lifecycle == "downstream"
        and row.activation in {"always", "mainnet", "job-now"}
        and row.artifact is None
    }
    for unit in literal_units:
        assert unit not in demo
        assert unit not in mainnet


def test_operator_activation_policy_is_read_from_the_manifest() -> None:
    rows = _manifest()
    for row in rows:
        assert _helper(f"lm_manifest_operator_policy {row.unit}") == [row.operator]

    ops = (ROOT / "scripts" / "ops.sh").read_text(encoding="utf-8")
    mutating = ops[ops.index("restart|stop|start)") : ops.index("\n  equity)")]
    assert 'operator_policy="$(lm_manifest_operator_policy "$unit"' in mutating
    assert "liquidity-migration-bybit-long-demo.service|" not in mutating


def test_timer_health_bounds_and_topology_rows_are_manifest_derived() -> None:
    rows = _manifest()
    timers = [row for row in rows if row.kind == "timer"]
    assert timers
    for timer in timers:
        assert timer.timer_service is not None
        assert timer.runtime_s is not None
        assert timer.first_delay_s is not None
        assert timer.cadence_s is not None
        assert timer.accuracy_s is not None
        timer_text = (SYSTEMD / timer.unit).read_text(encoding="utf-8")
        directives = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in timer_text.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        assert _duration_seconds(directives["AccuracySec"]) == timer.accuracy_s
        if "OnCalendar" in directives:
            assert "OnActiveSec" not in directives
            assert "OnUnitActiveSec" not in directives
            cadence = _calendar_cadence_seconds(directives["OnCalendar"])
            assert timer.first_delay_s == cadence
            assert timer.cadence_s == cadence
        else:
            assert "OnCalendar" not in directives
            assert _duration_seconds(directives["OnActiveSec"]) == timer.first_delay_s
            assert _duration_seconds(directives["OnUnitActiveSec"]) == timer.cadence_s
        service = (SYSTEMD / timer.timer_service).read_text(encoding="utf-8")
        assert f"TimeoutStartSec={timer.runtime_s}" in service

    health_rows = _helper("lm_fleet_health_rows on on on")
    parsed = {row.split("|", 1)[0]: row.split("|") for row in health_rows}
    health_units = {row.unit for row in rows if row.health != "none"}
    assert set(parsed) == health_units
    for timer in timers:
        fields = parsed[timer.unit]
        assert fields[1:] == [
            "on",
            "timer",
            "-",
            timer.timer_service,
            str(timer.first_delay_s),
            str(timer.cadence_s),
            str(timer.accuracy_s),
            str(timer.runtime_s),
        ]

    deploy = (ROOT / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    fleet = deploy[deploy.index("verify_fleet_units()") : deploy.index("\nverify_topology()")]
    assert "lm_fleet_health_rows" in fleet
    assert 'verify_timer_job "$unit" "$timer_service"' in fleet
    assert "verify_unit on liquidity-migration-backup.timer" not in fleet
    assert "verify_unit on liquidity-migration-chaos-drill.timer" not in fleet


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
        assert "/opt/liquidity-migration-engine/bin/run-authorized-runtime" in unit
