from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "scripts" / "ops.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(OPS), *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def _ssh_capture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text('#!/usr/bin/env bash\ncat > "$CAPTURE"\n', encoding="utf-8")
    ssh.chmod(0o700)
    return capture, {"PATH": f"{tmp_path}:{os.environ['PATH']}", "CAPTURE": str(capture)}


def test_help_lists_only_current_operator_routes() -> None:
    result = _run("help")
    assert result.returncode == 0
    for route in (
        "status",
        "units",
        "logs",
        "restart",
        "equity",
        "flatten",
        "attest-flat",
        "real-money",
        "deploy",
    ):
        assert route in result.stdout
    for retired in ("rollout", "staged", "install|activate"):
        assert retired not in result.stdout


def test_unknown_command_fails_with_usage() -> None:
    result = _run("definitely-not-a-command")
    assert result.returncode == 2
    assert "unknown command" in result.stderr


def test_deploy_allowlists_the_four_modes() -> None:
    result = _run("deploy", "definitely-not-a-mode")
    assert result.returncode == 2
    assert "deploy mode must be" in result.stderr


def test_unit_verbs_reach_systemd_and_qualify_short_names(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)

    assert _run("logs", "signal-worker-demo.service", env=environment).returncode == 0
    payload = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( liquidity-migration-signal-worker-demo.service 100 )" in payload
    assert "journalctl -u" in payload

    for verb in ("restart", "stop", "start"):
        assert _run(verb, "signal-worker-demo.service", env=environment).returncode == 0
        payload = capture.read_text(encoding="utf-8")
        assert f'exec systemctl {verb} "${{REMOTE_ARGS[@]}}"' in payload
        assert "REMOTE_ARGS=( liquidity-migration-signal-worker-demo.service )" in payload
        assert _run(verb, env=environment).returncode == 2


def test_funded_units_take_ordinary_unit_verbs(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    for verb in ("restart", "stop", "start"):
        result = _run(verb, "engine-mainnet.service", env=environment)
        assert result.returncode == 0, result.stderr
        payload = capture.read_text(encoding="utf-8")
        assert "REMOTE_ARGS=( liquidity-migration-engine-mainnet.service )" in payload


def test_mutating_unit_verbs_reject_non_unit_syntax(tmp_path: Path) -> None:
    _, environment = _ssh_capture(tmp_path)
    result = _run("restart", "engine.service; rm -rf /", env=environment)
    assert result.returncode == 2
    assert "invalid systemd unit name" in result.stderr


def test_flatten_payload_hands_its_arguments_to_the_remote_script(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("flatten", "--environment", "demo", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( --dry-run --environment demo )" in payload
    assert 'flatten_account.sh" "${REMOTE_ARGS[@]}"' in payload

    execute = _run("flatten", "--environment", "demo", "--execute", env=environment)
    assert execute.returncode == 0, execute.stderr
    executed = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( --environment demo --execute )" in executed
    assert "--dry-run" not in executed


def test_flatness_control_uses_the_installed_rust_engine(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("attest-flat", "--environment", "demo", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "/opt/liquidity-migration-engine/bin/engine" in payload
    assert "attest-flat" in payload
    assert "systemd-run" in payload
    # The funded credential set prefers the read-only attestor when present.
    result = _run("attest-flat", "--environment", "mainnet", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "bybit-mainnet-attestor.env" in payload


def test_flatness_control_rejects_incomplete_arguments() -> None:
    assert _run("attest-flat").returncode == 2
    assert _run("attest-flat", "--environment", "prod").returncode == 2


def test_real_money_allowlist_covers_the_arming_subcommands(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("real-money", "preflight", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "liquidity_migration.policy.real_money_arming" in payload
    assert "REMOTE_ARGS=( liquidity_migration.policy.real_money_arming preflight )" in payload
    assert _run("real-money", "set-real-money", env=environment).returncode == 2
