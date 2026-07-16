from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESET_SCRIPT = ROOT / "scripts" / "reset_demo_paper_ledgers.sh"


def _function_source(name: str) -> str:
    text = RESET_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text)
    assert match is not None, f"missing shell function {name}"
    return match.group(0)


@pytest.mark.parametrize("failed_unit", ["owner-b", "worker-b"])
def test_failed_post_clear_restart_fails_closed_to_all_inactive(
    tmp_path: Path,
    failed_unit: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
command="$1"
shift
if [[ "${1:-}" == "--quiet" ]]; then
  shift
fi
unit="$1"
case "$command" in
  start)
    [[ "$unit" != "$FAILED_UNIT" ]] || exit 1
    : > "$STATE/$unit.active"
    ;;
  stop)
    rm -f -- "$STATE/$unit.active"
    printf '%s\\n' "$unit" >> "$STATE/stops"
    ;;
  is-active)
    [[ -e "$STATE/$unit.active" ]]
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o700)

    functions = "\n".join(
        _function_source(name)
        for name in (
            "restart_previously_active",
            "stop_all_managed_units_after_failed_handoff",
            "cleanup",
        )
    )
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
STOP_UNITS=(worker-a worker-b owner-a owner-b)
OWNER_RESTART_UNITS=(owner-a owner-b)
DOWNSTREAM_RESTART_UNITS=(worker-a worker-b)
ACTIVE_BEFORE=(worker-a worker-b owner-a owner-b)
SYSTEMCTL_BIN={fake_systemctl!s}
SETTLE_SECONDS=0
SERVICES_STOPPED=1
RESTART_COMPLETE=0
FAILURE_RECOVERY_ALLOWED=0
LEAVE_STOPPED=0
MANIFEST_DIR=""
DEMO_ACCOUNT_LEASE_HELD=0
PAPER_ACCOUNT_LEASE_HELD=0
was_active() {{
  local needle="$1" unit
  for unit in "${{ACTIVE_BEFORE[@]}}"; do
    [[ "$unit" == "$needle" ]] && return 0
  done
  return 1
}}
release_paper_account_lease() {{ PAPER_ACCOUNT_LEASE_HELD=0; }}
release_demo_account_lease() {{ DEMO_ACCOUNT_LEASE_HELD=0; }}
{functions}
trap cleanup EXIT
restart_previously_active "normal completion"
RESTART_COMPLETE=1
""",
        encoding="utf-8",
    )
    harness.chmod(0o700)

    environment = os.environ.copy()
    environment.update(STATE=str(state), FAILED_UNIT=failed_unit)
    result = subprocess.run(
        [str(harness)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode != 0
    assert not list(state.glob("*.active")), result.stderr
    assert (state / "stops").read_text(encoding="utf-8").splitlines() == [
        "worker-a",
        "worker-b",
        "owner-a",
        "owner-b",
    ]
