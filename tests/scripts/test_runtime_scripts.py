"""The deploy script, the fleet's unit files, and their load-bearing wiring."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
SYSTEMD = ROOT / "deploy" / "systemd"


def _remote_script() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index("cat <<'REMOTE_SCRIPT'\n") + len("cat <<'REMOTE_SCRIPT'\n")
    end = text.index("\nREMOTE_SCRIPT\n")
    return text[start:end]


def _bash_ok(script: str) -> None:
    subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=True)


def test_deploy_local_and_remote_scripts_parse() -> None:
    subprocess.run(["bash", "-n", str(DEPLOY)], check=True)
    _bash_ok(_remote_script())


def test_deployed_shell_entrypoints_are_executable() -> None:
    for relative in (
        "scripts/ops.sh",
        "scripts/deploy_vps_live.sh",
        "deploy/telegram_control_helper.sh",
        "scripts/runtime/backup_state.sh",
        "scripts/runtime/chaos_drill.sh",
        "scripts/runtime/upload_forward_capture.sh",
        "scripts/vps/flatten_account.sh",
    ):
        path = ROOT / relative
        assert path.exists(), relative
        assert os.access(path, os.X_OK), f"{relative} is not executable"


def test_deploy_modes_are_exactly_the_four_operations() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    match = re.search(r"case \"\$MODE\" in\n\s+(\S+)\)", text)
    assert match is not None
    assert "deploy|verify|stop-mainnet|disarm-mainnet" in text
    for retired in ("install)", "activate)", "staged)", "rollout)", "--profile"):
        assert retired not in text


def test_deploy_refuses_a_malformed_expected_commit(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(DEPLOY), "deploy"],
        env={**os.environ, "EXPECTED_COMMIT": "not-a-commit"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "40-character commit" in result.stderr


def test_remote_deploy_only_ships_commits_from_the_pushed_branch() -> None:
    remote = _remote_script()
    assert "merge-base --is-ancestor" in remote
    assert 'checkout -B "$BRANCH" "$EXPECTED_COMMIT"' in remote


def test_deploy_removes_the_retired_generation_gate_artifacts() -> None:
    remote = _remote_script()
    assert "rm -f /opt/liquidity-migration-engine/bin/run-authorized-runtime" in remote
    assert not (ROOT / "deploy" / "run_authorized_runtime_trusted.sh").exists()
    assert not (ROOT / "scripts" / "run_authorized_runtime.sh").exists()


def test_disarm_rewrite_sets_real_money_false_and_keeps_other_keys(tmp_path: Path) -> None:
    remote = _remote_script()
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY\n", remote, re.DOTALL)
    disarm = next(block for block in blocks if 'values["REAL_MONEY"] = "false"' in block)
    credential = tmp_path / "bybit-mainnet.env"
    credential.write_text(
        "REAL_MONEY=true\nBYBIT_REAL_API_KEY=abc\nTELEGRAM_CHAT_ID=42\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["python3", "-I", "-", str(credential)],
        input=disarm,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    text = credential.read_text(encoding="utf-8")
    assert "REAL_MONEY=false" in text
    assert "BYBIT_REAL_API_KEY=abc" in text
    assert "TELEGRAM_CHAT_ID=42" in text


def test_disarm_rewrite_refuses_an_ambiguous_credential(tmp_path: Path) -> None:
    remote = _remote_script()
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY\n", remote, re.DOTALL)
    disarm = next(block for block in blocks if 'values["REAL_MONEY"] = "false"' in block)
    credential = tmp_path / "bybit-mainnet.env"
    credential.write_text("REAL_MONEY=true\nREAL_MONEY=false\n", encoding="utf-8")
    completed = subprocess.run(
        ["python3", "-I", "-", str(credential)],
        input=disarm,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "REAL_MONEY=true" in credential.read_text(encoding="utf-8")


def test_deploy_starts_the_funded_realm_only_when_armed() -> None:
    remote = _remote_script()
    deploy_body = remote[remote.index("deploy_mode()") :]
    assert "if mainnet_armed; then" in deploy_body
    assert "start_realm mainnet" in deploy_body
    assert "real-money off: funded units stay stopped" in deploy_body


def test_every_service_execstart_is_an_absolute_committed_command() -> None:
    for path in sorted(SYSTEMD.glob("*.service")):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"^ExecStart=(\S+)", text, re.MULTILINE)
        assert match is not None, path.name
        assert match.group(1).startswith("/"), path.name
        assert "run-authorized-runtime" not in text, path.name
        assert "RestartPreventExitStatus" not in text, path.name


def test_engine_units_unset_credentials_they_must_not_see() -> None:
    demo = (SYSTEMD / "liquidity-migration-engine.service").read_text(encoding="utf-8")
    assert "UnsetEnvironment=" in demo
    for secret in ("BYBIT_REAL_API_KEY", "REAL_MONEY", "TELEGRAM_BOT_TOKEN"):
        assert secret in demo
    for realm in ("demo", "mainnet"):
        worker = (
            SYSTEMD / f"liquidity-migration-signal-worker-{realm}.service"
        ).read_text(encoding="utf-8")
        assert "bybit-demo.env" not in worker
        assert "bybit-mainnet.env" not in worker


def test_control_helper_parses_and_keeps_the_fixed_action_surface() -> None:
    helper = ROOT / "deploy" / "telegram_control_helper.sh"
    subprocess.run(["bash", "-n", str(helper)], check=True)
    text = helper.read_text(encoding="utf-8")
    for action in ("pause-demo", "resume-demo", "pause-mainnet", "resume-mainnet", "status-fleet"):
        assert action in text
    assert "engine.release" not in text
    assert "activation.complete" not in text


def test_ci_workflow_dispatch_covers_deploy_verify_and_disarm() -> None:
    workflow = (ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    assert "options: [deploy, verify, disarm-mainnet]" in workflow
    assert "disarm-mainnet" in workflow
    assert "rollout" not in workflow.replace("# pending slot", "")
