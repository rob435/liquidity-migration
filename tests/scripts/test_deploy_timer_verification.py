from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
SYSTEMD = ROOT / "deploy" / "systemd"


def _helpers() -> str:
    body = DEPLOY.read_text(encoding="utf-8")
    start = body.index("systemd_property_value()")
    end = body.index("\nverify_topology()", start)
    return body[start:end]


HARNESS = r"""
set -euo pipefail

VERIFY_MISMATCHES=()
verify_note() {
    VERIFY_MISMATCHES+=("$1")
}

systemctl() {
    case "$1" in
        cat)
            [ "$SERVICE_PRESENT" = 1 ]
            ;;
        show)
            if [[ "$2" == *.timer ]]; then
                printf 'LoadState=%s\n' "$TIMER_LOAD_STATE"
                printf 'ActiveState=%s\n' "$TIMER_STATE"
                printf 'Result=%s\n' "$TIMER_RESULT"
                printf 'ActiveEnterTimestampMonotonic=%s\n' "$TIMER_ACTIVE_USEC"
            else
                printf 'LoadState=%s\n' "$SERVICE_LOAD_STATE"
                printf 'ActiveState=%s\n' "$SERVICE_STATE"
                printf 'InvocationID=%s\n' "$INVOCATION_ID"
                if [ "$OMIT_SERVICE_RESULT" = 0 ]; then
                    printf 'Result=%s\n' "$SERVICE_RESULT"
                fi
                printf 'ExecMainCode=%s\n' "$EXEC_MAIN_CODE"
                printf 'ExecMainStatus=%s\n' "$EXEC_MAIN_STATUS"
                printf 'StateChangeTimestampMonotonic=%s\n' "$SERVICE_CHANGED_USEC"
                printf 'ExecMainStartTimestampMonotonic=%s\n' "$START_USEC"
                printf 'ExecMainExitTimestampMonotonic=%s\n' "$EXIT_USEC"
            fi
            ;;
        *) return 64 ;;
    esac
}

timer_last_trigger_monotonic_usec() {
    [ "$LAST_TRIGGER_AVAILABLE" = 1 ] || return 1
    printf '%s\n' "$LAST_TRIGGER_USEC"
}

monotonic_now_usec() {
    printf '%s\n' "$NOW_USEC"
}

verify_timer_job \
    job.timer job.service \
    "$FIRST_DELAY_SECONDS" "$CADENCE_SECONDS" \
    "$ACCURACY_SECONDS" "$RUNTIME_SECONDS"

printf 'count=%s\n' "${#VERIFY_MISMATCHES[@]}"
for mismatch in "${VERIFY_MISMATCHES[@]}"; do
    printf 'message=%s\n' "$mismatch"
done
"""


DEFAULTS = {
    "SERVICE_PRESENT": "1",
    "TIMER_LOAD_STATE": "loaded",
    "TIMER_STATE": "active",
    "TIMER_RESULT": "success",
    "TIMER_ACTIVE_USEC": "9000000000",
    "LAST_TRIGGER_AVAILABLE": "1",
    "LAST_TRIGGER_USEC": "9900000000",
    "NOW_USEC": "10000000000",
    "SERVICE_LOAD_STATE": "loaded",
    "SERVICE_STATE": "inactive",
    "INVOCATION_ID": "a" * 32,
    "SERVICE_RESULT": "success",
    "OMIT_SERVICE_RESULT": "0",
    "EXEC_MAIN_CODE": "1",
    "EXEC_MAIN_STATUS": "0",
    "SERVICE_CHANGED_USEC": "9900200000",
    "START_USEC": "9900100000",
    "EXIT_USEC": "9900200000",
    "FIRST_DELAY_SECONDS": "60",
    "CADENCE_SECONDS": "180",
    "ACCURACY_SECONDS": "15",
    "RUNTIME_SECONDS": "120",
}


def _run_case(**overrides: str) -> list[str]:
    environment = os.environ.copy()
    environment.update(DEFAULTS)
    environment.update(overrides)
    completed = subprocess.run(
        ["bash", "-c", f"{_helpers()}\n{HARNESS}"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    rows = completed.stdout.splitlines()
    count = int(next(row.removeprefix("count=") for row in rows if row.startswith("count=")))
    messages = [row.removeprefix("message=") for row in rows if row.startswith("message=")]
    assert len(messages) == count
    return messages


def test_current_successful_completed_invocation_passes() -> None:
    assert _run_case() == []


def test_missing_service_fails() -> None:
    assert _run_case(SERVICE_PRESENT="0") == ["job.service is missing"]


def test_masked_or_unloaded_service_fails() -> None:
    assert _run_case(SERVICE_LOAD_STATE="masked") == ["job.service is not loaded"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "SERVICE_STATE": "failed",
                "SERVICE_RESULT": "exit-code",
                "EXEC_MAIN_STATUS": "1",
            },
            "job.service is failed",
        ),
        (
            {"SERVICE_RESULT": "exit-code", "EXEC_MAIN_STATUS": "1"},
            "latest completed invocation is not successful",
        ),
        (
            {"SERVICE_RESULT": "success", "EXEC_MAIN_CODE": "0"},
            "latest completed invocation is not successful",
        ),
        (
            {"OMIT_SERVICE_RESULT": "1"},
            "job.service status is ambiguous",
        ),
        (
            {"SERVICE_STATE": "active"},
            "job.service has ambiguous active state active",
        ),
    ],
)
def test_failed_or_ambiguous_completion_fails(overrides: dict[str, str], message: str) -> None:
    assert message in _run_case(**overrides)[0]


def test_reset_failed_result_cannot_masquerade_as_first_run() -> None:
    messages = _run_case(
        INVOCATION_ID="",
        EXEC_MAIN_CODE="0",
        START_USEC="0",
        EXIT_USEC="0",
        SERVICE_CHANGED_USEC="0",
    )
    assert messages == ["job.service has no current invocation for job.timer's latest trigger"]


def test_live_shaped_never_run_service_uses_first_run_grace() -> None:
    assert _run_case(
        TIMER_ACTIVE_USEC="9940000000",
        LAST_TRIGGER_USEC="0",
        INVOCATION_ID="",
        SERVICE_RESULT="success",
        EXEC_MAIN_CODE="0",
        EXEC_MAIN_STATUS="0",
        START_USEC="0",
        EXIT_USEC="0",
        SERVICE_CHANGED_USEC="9970000000",
    ) == []


def test_successful_invocation_before_timer_activation_uses_first_run_grace() -> None:
    assert _run_case(
        TIMER_ACTIVE_USEC="9940000000",
        LAST_TRIGGER_USEC="0",
        START_USEC="9900000000",
        EXIT_USEC="9910000000",
        SERVICE_CHANGED_USEC="9970000000",
    ) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"SERVICE_RESULT": "exit-code", "EXEC_MAIN_STATUS": "1"},
        {"EXEC_MAIN_CODE": "1"},
        {"INVOCATION_ID": "not-an-invocation"},
    ],
)
def test_malformed_never_run_evidence_cannot_use_first_run_grace(
    overrides: dict[str, str],
) -> None:
    messages = _run_case(
        **{
            "TIMER_ACTIVE_USEC": "9940000000",
            "LAST_TRIGGER_USEC": "0",
            "INVOCATION_ID": "",
            "EXEC_MAIN_CODE": "0",
            "START_USEC": "0",
            "EXIT_USEC": "0",
            "SERVICE_CHANGED_USEC": "9970000000",
            **overrides,
        }
    )
    assert messages == ["job.service has ambiguous first-run evidence for job.timer"]


def test_latest_timer_trigger_rejects_an_older_successful_invocation() -> None:
    messages = _run_case(
        LAST_TRIGGER_USEC="9950000000",
        START_USEC="9900100000",
        EXIT_USEC="9900200000",
    )
    assert messages == ["job.service has no current invocation for job.timer's latest trigger"]


def test_successful_invocation_becomes_stale_at_this_timers_cadence() -> None:
    messages = _run_case(
        LAST_TRIGGER_USEC="9600000000",
        START_USEC="9600100000",
        EXIT_USEC="9700000000",
    )
    assert messages == ["job.service latest successful invocation is stale for job.timer"]


def test_genuine_first_run_grace_is_tied_to_current_timer_activation() -> None:
    no_invocation = {
        "LAST_TRIGGER_USEC": "0",
        "INVOCATION_ID": "",
        "EXEC_MAIN_CODE": "0",
        "START_USEC": "0",
        "EXIT_USEC": "0",
        "SERVICE_CHANGED_USEC": "0",
    }
    assert _run_case(TIMER_ACTIVE_USEC="9940000000", **no_invocation) == []
    assert _run_case(TIMER_ACTIVE_USEC="9919000000", **no_invocation) == [
        "job.service has not completed its first run for job.timer"
    ]


def test_current_activation_in_progress_is_bounded_by_service_runtime() -> None:
    in_progress = {
        "SERVICE_STATE": "activating",
        "LAST_TRIGGER_USEC": "0",
        "TIMER_ACTIVE_USEC": "9900000000",
        "SERVICE_CHANGED_USEC": "9950000000",
        "START_USEC": "9950000000",
        "EXIT_USEC": "0",
    }
    assert _run_case(**in_progress) == []
    assert _run_case(
        **{
            **in_progress,
            "SERVICE_CHANGED_USEC": "9800000000",
            "START_USEC": "9800000000",
        }
    ) == ["job.service in-progress invocation is not current for job.timer"]
    assert _run_case(
        **{
            **in_progress,
            "TIMER_ACTIVE_USEC": "9800000000",
            "SERVICE_CHANGED_USEC": "9870000000",
            "START_USEC": "9870000000",
        }
    ) == ["job.service in-progress invocation is overdue"]


@pytest.mark.parametrize(
    ("name", "first_delay", "cadence", "accuracy", "runtime", "timer_lines"),
    [
        ("demo-liveness", 60, 180, 15, 120, ("OnActiveSec=1min", "OnUnitActiveSec=3min", "AccuracySec=15s")),
        ("mainnet-liveness", 60, 180, 15, 120, ("OnActiveSec=1min", "OnUnitActiveSec=3min", "AccuracySec=15s")),
        ("llm-ledger", 3600, 3600, 120, 600, ("OnCalendar=*-*-* *:05:00", "AccuracySec=2min")),
        ("trade-notify", 300, 300, 30, 120, ("OnCalendar=*-*-* *:0/5:30", "AccuracySec=30s")),
        ("backup", 86400, 86400, 300, 900, ("OnCalendar=*-*-* 03:17:00 UTC", "AccuracySec=5min")),
        ("chaos-drill", 604800, 604800, 600, 300, ("OnCalendar=Sun *-*-* 09:13:00 UTC", "AccuracySec=10min")),
        ("forward-upload", 3600, 3600, 60, 1800, ("OnCalendar=*-*-* *:23:00 UTC", "AccuracySec=1min")),
    ],
)
def test_each_job_policy_matches_its_current_timer_and_service(
    name: str,
    first_delay: int,
    cadence: int,
    accuracy: int,
    runtime: int,
    timer_lines: tuple[str, ...],
) -> None:
    deploy = " ".join(DEPLOY.read_text(encoding="utf-8").replace("\\\n", "").split())
    assert (
        f"verify_timer_job liquidity-migration-{name}.timer "
        f"liquidity-migration-{name}.service "
        f"{first_delay} {cadence} {accuracy} {runtime}"
    ) in deploy

    timer = (SYSTEMD / f"liquidity-migration-{name}.timer").read_text(encoding="utf-8")
    assert all(line in timer for line in timer_lines)
    service = (SYSTEMD / f"liquidity-migration-{name}.service").read_text(encoding="utf-8")
    assert f"TimeoutStartSec={runtime}" in service


def test_backup_health_is_its_successful_job_result_not_destination_configuration() -> None:
    helper = _helpers()
    assert 'systemctl show "$service" --all' in helper
    assert "backup.env" not in helper
    assert "BACKUP_" not in helper
