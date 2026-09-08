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
        "verify-account-identity",
        "canary-order",
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


def test_curve_routes_the_selected_history_to_the_rust_companion(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("curve", "demo", "1440", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text()
    assert "REMOTE_ARGS=( demo 1440 )" in payload
    assert "exec /opt/liquidity-migration-engine/bin/engine-tools record-equity" in payload
    assert '--show "${REMOTE_ARGS[0]}" --samples "${REMOTE_ARGS[1]}"' in payload
    for realm, samples in (("unknown", "1"), ("demo", "0"), ("mainnet", "1;false")):
        assert _run("curve", realm, samples, env=environment).returncode == 2
    # The recorder is manifest-driven: a new realm needs no recorder change.
    assert _run("curve", "mexc", "60", env=environment).returncode == 0
    assert "REMOTE_ARGS=( mexc 60 )" in capture.read_text()


def test_deploy_allowlists_one_stop_and_disarm_mode_per_funded_realm(tmp_path: Path) -> None:
    result = _run("deploy", "definitely-not-a-mode")
    assert result.returncode == 2
    assert "deploy mode must be" in result.stderr
    _, environment = _ssh_capture(tmp_path)
    for mode in ("stop-mainnet", "disarm-mainnet", "stop-mexc", "disarm-mexc"):
        assert _run("deploy", mode, env=environment).returncode == 0, mode


def test_execution_study_reads_only_the_selected_report(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    for args, filename in (((), "latest.txt"), (("--json",), "latest.json")):
        result = _run("execution-study", *args, env=environment)
        assert result.returncode == 0, result.stderr
        payload = capture.read_text()
        assert f"REMOTE_ARGS=( {filename} )" in payload
        assert 'cat -- "/var/lib/liquidity-migration/execution-study/${REMOTE_ARGS[0]}"' in payload
    assert _run("execution-study", "--execute", env=environment).returncode == 2


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


def test_flatness_control_reaches_the_mexc_account_owner(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("attest-flat", "--environment", "mexc", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "/etc/liquidity-migration/mexc-mainnet.env" in payload
    assert "/etc/liquidity-migration/engine-mexc.env" in payload
    assert "runtime_user=liquidity-engine-mexc" in payload
    # The whole router travels; the realm picks its own arm. That arm unsets
    # every Bybit key and the arming switch for the read-only run.
    mexc_arm = payload.split("  mexc)", 1)[1].split(";;", 1)[0]
    assert "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET" in mexc_arm
    assert "REAL_MONEY" in mexc_arm
    assert "bybit" not in mexc_arm.replace("BYBIT_", "")


def test_flatness_control_reaches_the_hyperliquid_account_owner(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("attest-flat", "--environment", "hyperliquid", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "/etc/liquidity-migration/hyperliquid-mainnet.env" in payload
    assert "/etc/liquidity-migration/engine-hyperliquid.env" in payload
    assert "runtime_user=liquidity-engine-hyperliquid" in payload
    # This arm unsets every other venue's credential and the arming switch for
    # the read-only run, and reaches no other realm's files.
    arm = payload.split("  hyperliquid)", 1)[1].split(";;", 1)[0]
    assert "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET" in arm
    assert "MEXC_REAL_API_KEY MEXC_REAL_API_SECRET" in arm
    assert "REAL_MONEY" in arm
    assert "bybit" not in arm.replace("BYBIT_", "")
    assert "mexc" not in arm.replace("MEXC_", "")


def test_flatness_control_rejects_incomplete_arguments() -> None:
    assert _run("attest-flat").returncode == 2
    assert _run("attest-flat", "--environment", "prod").returncode == 2


def test_identity_check_routes_through_the_read_only_engine_control(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("verify-account-identity", "--environment", "mexc", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( mexc verify-account-identity )" in payload
    assert '"$engine_binary" "$mode"' in payload
    assert _run("verify-account-identity").returncode == 2
    assert _run("verify-account-identity", "--environment", "prod").returncode == 2


def test_canary_order_keeps_the_arming_switch_and_refuses_the_funded_bybit_account(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run(
        "canary-order", "--environment", "mexc", "--symbol", "BTCUSDT",
        "--expected-user-id", "key-0123456789abcdef", "--execute", env=environment,
    )
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert (
        "REMOTE_ARGS=( mexc canary-order --symbol BTCUSDT --expected-user-id key-0123456789abcdef --execute )"
        in payload
    )
    # The read-only modes drop REAL_MONEY; the canary is the one mode that
    # keeps it, because a live-canary gateway refuses to build unarmed.
    canary_arm = payload.split("  canary-order)", 1)[1].split(";;", 1)[0]
    assert "grep -vx REAL_MONEY" in canary_arm
    # The account lease is a kernel lock under the fleet lock root; the
    # read-only modes leave the sandbox read-only, the canary opens that root.
    assert "writable_paths=/run/lock/liquidity-migration" in canary_arm
    assert '--property="ReadWritePaths=$writable_paths"' in payload
    read_only_arms = payload.split("  attest-flat|verify-account-identity)", 1)[1].split(";;", 1)[0]
    assert "writable_paths=" not in read_only_arms
    assert 'liquidity-migration-${mode}-${realm}-$$' in payload

    dry = _run(
        "canary-order", "--environment", "demo", "--symbol", "XRPUSDT",
        "--expected-user-id", "579580669", env=environment,
    )
    assert dry.returncode == 0, dry.stderr
    assert "--execute" not in capture.read_text(encoding="utf-8").split("REMOTE_ARGS=(", 1)[1].split(")", 1)[0]

    hyperliquid = _run(
        "canary-order", "--environment", "hyperliquid", "--symbol", "BTC",
        "--expected-user-id", "0x" + "ab" * 20, "--execute", env=environment,
    )
    assert hyperliquid.returncode == 0, hyperliquid.stderr
    assert (
        "REMOTE_ARGS=( hyperliquid canary-order --symbol BTC --expected-user-id "
        + "0x" + "ab" * 20 + " --execute )"
    ) in capture.read_text(encoding="utf-8")

    for bad in (
        ("canary-order", "--environment", "mainnet", "--symbol", "BTCUSDT", "--expected-user-id", "1"),
        ("canary-order", "--environment", "mexc", "--symbol", "BTCUSDT"),
        ("canary-order", "--environment", "mexc", "--expected-user-id", "key-1"),
        ("canary-order", "--symbol", "BTCUSDT", "--expected-user-id", "key-1"),
    ):
        assert _run(*bad).returncode == 2, bad


def test_real_money_allowlist_covers_the_arming_subcommands(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)
    result = _run("real-money", "preflight", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "liquidity_migration.policy.real_money_arming" in payload
    assert "REMOTE_ARGS=( liquidity_migration.policy.real_money_arming preflight )" in payload
    assert _run("real-money", "set-real-money", env=environment).returncode == 2
    for subcommand in ("preflight-mexc", "preflight-hyperliquid"):
        result = _run("real-money", subcommand, env=environment)
        assert result.returncode == 0, result.stderr
        payload = capture.read_text(encoding="utf-8")
        assert (
            f"REMOTE_ARGS=( liquidity_migration.policy.real_money_arming {subcommand} )"
            in payload
        )
