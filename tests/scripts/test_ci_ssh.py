"""The shared CI SSH setup preserves both identity and host-key pins."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _key(path: Path) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(path) + ".pub", "-E", "sha256"],
        text=True,
        capture_output=True,
        check=True,
    )
    return fingerprint.stdout.split()[1]


@pytest.mark.parametrize("mismatch", [None, "identity", "host"])
def test_shared_ssh_setup_requires_both_pinned_keys(tmp_path: Path, mismatch: str | None) -> None:
    identity = tmp_path / "identity"
    identity_pin = _key(identity)
    host_pin = _key(tmp_path / "host")
    host_row = "example.invalid " + (tmp_path / "host.pub").read_text(encoding="utf-8")
    fixture = tmp_path / "keyscan-output"
    fixture.write_text(host_row, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    keyscan = fake_bin / "ssh-keyscan"
    keyscan.write_text('#!/bin/sh\ncat "$KEYSCAN_FIXTURE"\n', encoding="utf-8")
    keyscan.chmod(0o755)
    directory = tmp_path / "ssh directory"
    helper = (ROOT / "scripts/vps/configure_ci_ssh.sh").read_text(encoding="utf-8")
    helper = helper.replace("~/.ssh", shlex.quote(str(directory)))
    result = subprocess.run(
        ["bash", "-c", helper],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "KEYSCAN_FIXTURE": str(fixture),
            "VPS_HOST": "example.invalid",
            "VPS_SSH_PRIVATE_KEY": identity.read_text(encoding="utf-8"),
            "GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT": "wrong" if mismatch == "identity" else identity_pin,
            "VPS_ED25519_FINGERPRINT": "wrong" if mismatch == "host" else host_pin,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    trusted = directory / "known_hosts"
    if mismatch:
        assert result.returncode != 0
        assert not trusted.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert trusted.read_text(encoding="utf-8") == host_row
        assert trusted.stat().st_mode & 0o777 == 0o600
        assert (directory / "vps_deploy_key").stat().st_mode & 0o777 == 0o600


def test_every_ci_vps_operation_uses_the_shared_ssh_setup() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/vps-deploy.yml").read_text(encoding="utf-8"))
    for name in ("disarm", "diagnose", "vps"):
        step = next(step for step in workflow["jobs"][name]["steps"] if step.get("name") == "Configure pinned SSH identity")
        assert step["run"] == "bash scripts/vps/configure_ci_ssh.sh"
        assert step["env"]["VPS_SSH_PRIVATE_KEY"] == "${{ secrets.VPS_SSH_PRIVATE_KEY }}"
