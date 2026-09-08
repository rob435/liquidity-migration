"""`mode=diagnose` must say why a failed unit failed, not only that one did.

`verify_mode` reads journals for the units it expects to be running, so a unit
outside that list reaches an on-call session with no host access as a name
under `systemctl --failed` and nothing else. Incident
`host-ecbac293ecc90d5e` ended with `liquidity-migration-backup.service` and
`liquidity-migration-execution-study.service` failed and unnameable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "scripts/vps/deploy_remote.sh"


def _function(name: str) -> str:
    source = REMOTE.read_text(encoding="utf-8")
    body = source[source.index(f"{name}() {{") :]
    return body[: body.index("\n}\n") + 3]


def _report(failed: list[str], *, journal_lines: int = 20, units: int = 5) -> list[str]:
    listed = " ".join(repr(f"{unit} loaded failed failed a description") for unit in failed)
    script = f"""
set -euo pipefail
FAILED_UNIT_JOURNAL_LINES={journal_lines}
FAILED_UNIT_REPORT_MAX={units}
systemctl() {{
    case "$1" in
        list-units) [ -z "{listed}" ] || printf '%s\\n' {listed} ;;
        show) echo "Id=$2"; echo "Result=exit-code"; echo "ExecMainStatus=2" ;;
    esac
}}
journalctl() {{
    local unit="$2" count="$4" index=1
    while [ "$index" -le "$count" ]; do
        echo "2026-09-08T01:00:00+00:00 host $unit[1]: line $index"
        index=$((index + 1))
    done
}}
{_function("report_failed_units")}
report_failed_units
"""
    result = subprocess.run(["bash"], input=script, text=True, capture_output=True, check=True)
    return result.stdout.splitlines()


def test_each_failed_unit_is_named_with_its_result_and_journal() -> None:
    lines = _report(["liquidity-migration-backup.service"])

    assert "failed-unit liquidity-migration-backup.service" in lines, lines
    assert "Id=liquidity-migration-backup.service" in lines, lines
    assert "Result=exit-code" in lines, lines
    assert any("liquidity-migration-backup.service[1]: line 1" in line for line in lines), lines


def test_every_failed_unit_reaches_the_report() -> None:
    lines = _report(
        [
            "liquidity-migration-backup.service",
            "liquidity-migration-execution-study.service",
        ]
    )

    named = [line for line in lines if line.startswith("failed-unit ")]
    assert named == [
        "failed-unit liquidity-migration-backup.service",
        "failed-unit liquidity-migration-execution-study.service",
    ], lines


def test_a_healthy_host_prints_nothing() -> None:
    assert _report([]) == []


def test_the_journal_tail_is_bounded_per_unit() -> None:
    lines = _report(["liquidity-migration-backup.service"], journal_lines=3)

    assert sum(1 for line in lines if "line " in line) == 3, lines


def test_the_report_is_bounded_so_one_host_cannot_flood_the_run() -> None:
    lines = _report([f"liquidity-migration-unit-{index}.service" for index in range(9)], units=2)

    named = [line for line in lines if line.startswith("failed-unit ")]
    assert len(named) == 2, lines
    assert "failed-unit-report truncated at 2 units" in lines, lines


def test_verify_mode_takes_the_reading() -> None:
    assert "report_failed_units" in _function("verify_mode")
