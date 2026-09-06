"""Recovery instructions must call modes the deployment entry point accepts."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts/deploy_vps_live.sh"


def _accepted_modes() -> set[str]:
    result = subprocess.run(
        ["bash", str(DEPLOY), "unknown-mode"], text=True, capture_output=True, check=False
    )
    assert result.returncode == 2
    match = re.search(r"usage: deploy_vps_live.sh \{([^}]+)\}", result.stderr)
    assert match is not None, result.stderr
    return set(match.group(1).split("|"))


def _assert_supported_commands(instructions: str) -> None:
    modes = []
    for line in instructions.splitlines():
        if line.startswith("SSH_TARGET=") and "scripts/deploy_vps_live.sh" in line:
            words = shlex.split(line)
            modes.append(words[words.index("scripts/deploy_vps_live.sh") + 1])
    assert modes, instructions
    assert set(modes) <= _accepted_modes()
    assert modes == ["verify", "deploy", "verify"]


def test_generated_recovery_commands_use_supported_deploy_modes() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/vps/print_vps_recovery_command.sh"), "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    _assert_supported_commands(result.stdout)


def test_rescue_completion_prints_supported_next_steps() -> None:
    source = (ROOT / "scripts/vps/vps_rescue_restore_ssh_access.sh").read_text(encoding="utf-8")
    instructions = source[source.index('echo "rescue-ssh-restore-ok"') :]
    result = subprocess.run(
        ["bash", "-c", instructions], text=True, capture_output=True, check=True
    )
    _assert_supported_commands(result.stdout)
