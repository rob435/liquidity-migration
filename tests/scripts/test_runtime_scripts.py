"""The deploy script, the fleet's unit files, and their load-bearing wiring."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
SYSTEMD = ROOT / "deploy" / "systemd"
WORKFLOW = ROOT / ".github" / "workflows" / "vps-deploy.yml"


def _remote_script() -> str:
    return (ROOT / "scripts/vps/deploy_remote.sh").read_text(encoding="utf-8")


def _bash_ok(script: str) -> None:
    subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=True)


def test_deploy_local_and_remote_scripts_parse() -> None:
    subprocess.run(["bash", "-n", str(DEPLOY)], check=True)
    _bash_ok(_remote_script())


def test_deploy_launcher_transmits_remote_file_and_literal_variables(tmp_path: Path) -> None:
    ssh = tmp_path / "ssh"
    captured = tmp_path / "remote.sh"
    ssh.write_text('#!/bin/sh\ncat > "$REMOTE_CAPTURE"\n', encoding="utf-8")
    ssh.chmod(0o755)
    value = "literal ' space $HOME $(false); `false`"
    result = subprocess.run(
        ["bash", str(DEPLOY), "verify"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "REMOTE_CAPTURE": str(captured),
            "REPO_DIR": value,
            "GITHUB_TOKEN": "",
            "EXPECTED_COMMIT": "a" * 40,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    transmitted = captured.read_text(encoding="utf-8")
    remote = _remote_script()
    assert transmitted.endswith(remote)
    _bash_ok(transmitted)
    variables = transmitted[: -len(remote)]
    decoded = subprocess.run(
        ["bash", "-c", variables + 'printf "%s" "$REPO_DIR"'],
        text=True,
        capture_output=True,
        check=True,
    )
    assert decoded.stdout == value


def test_deployed_shell_entrypoints_are_executable() -> None:
    for relative in (
        "scripts/ops.sh",
        "scripts/deploy_vps_live.sh",
        "deploy/telegram_control_helper.sh",
        "scripts/runtime/backup_state.sh",
        "scripts/runtime/chaos_drill.sh",
        "scripts/vps/flatten_account.sh",
    ):
        path = ROOT / relative
        assert path.exists(), relative
        assert os.access(path, os.X_OK), f"{relative} is not executable"


def test_deploy_modes_are_exactly_the_five_operations() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    match = re.search(r"case \"\$MODE\" in\n\s+(\S+)\)", text)
    assert match is not None
    assert "deploy|rollback|verify|stop-mainnet|disarm-mainnet" in text
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
    assert "handover_realm mainnet" in deploy_body
    assert "real-money off: funded units stay stopped" in deploy_body


def test_deploy_prepares_oncall_routes_before_starting_independent_watchdog() -> None:
    remote = _remote_script()
    deploy_body = remote[remote.index("deploy_mode()") :]
    assert "liquidity_migration.policy.oncall_environment" in remote
    assert deploy_body.index("prepare_oncall_inputs") < deploy_body.index("start_independent_units")


def test_deploy_waits_for_a_received_frame_before_releasing_a_restarted_recorder() -> None:
    remote = _remote_script()
    ready = remote[remote.index("capture_status_ready()") : remote.index("start_unit()")]
    independent = _function_body(remote, "start_independent_units")

    assert 'payload.get("last_receive_ns")' in ready
    assert 'payload.get("pid") == expected_pid' in ready
    assert 'systemctl show --property=MainPID --value "$unit"' in ready
    assert 'shard.get("connected") is True' in ready
    assert 'wait_capture_ready "$unit" "$CAPTURE_STATUS" "$since"' in independent
    assert 'wait_fresh_heartbeat "$unit" "$CAPTURE_STATUS" "$since"' not in independent


def test_observers_load_dedicated_notification_files_not_venue_credentials() -> None:
    units = [
        "liquidity-migration-demo-liveness.service",
        "liquidity-migration-mainnet-liveness.service",
        "liquidity-migration-host-liveness.service",
        "liquidity-migration-trade-notify.service",
        "liquidity-migration-telegram-controls.service",
    ]
    for name in units:
        text = (SYSTEMD / name).read_text(encoding="utf-8")
        assert "EnvironmentFile=/etc/liquidity-migration/notifications.env" in text
        assert "EnvironmentFile=/etc/liquidity-migration/bybit-demo.env" not in text
        assert "EnvironmentFile=/etc/liquidity-migration/bybit-mainnet.env" not in text
    for realm in ("demo", "mainnet", "host"):
        text = (SYSTEMD / f"liquidity-migration-{realm}-liveness.service").read_text(encoding="utf-8")
        assert "EnvironmentFile=/etc/liquidity-migration/oncall.env" in text
        assert "--require-oncall" in text


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
        worker = (SYSTEMD / f"liquidity-migration-signal-worker-{realm}.service").read_text(encoding="utf-8")
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


def test_ci_workflow_dispatch_covers_operations_and_fast_diagnostics() -> None:
    workflow = (ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    assert "options: [deploy, qualify, rollback, verify, diagnose, disarm-mainnet]" in workflow
    assert "deploy|rollback|verify) ;;" in workflow
    assert "disarm-mainnet" in workflow
    diagnose = workflow[workflow.index("\n  diagnose:\n") : workflow.index("\n  vps:\n")]
    assert "scripts/deploy_vps_live.sh verify" in diagnose
    assert "journalctl" in diagnose
    assert "systemctl --failed" in diagnose
    assert "rollout" not in workflow.replace("# pending slot", "")


def test_the_ci_deploy_hands_the_host_a_token_for_the_private_fetch() -> None:
    # The host fetches the exact commit itself. This repository is private and
    # nothing provisions a host credential, so the run's own token has to
    # travel with the deploy or the fetch asks for a username it cannot get.
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"]["contents"] == "read"
    steps = workflow["jobs"]["vps"]["steps"]
    runner = next(step for step in steps if "scripts/deploy_vps_live.sh" in (step.get("run") or ""))
    assert runner["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    # It is only a fix because the remote body spends it on that fetch.
    remote = _remote_script()
    assert 'if [ -n "$GITHUB_TOKEN" ] && [[ "$REPO_URL" == https://github.com/* ]]; then' in remote
    assert "git_authorized fetch --no-tags" in remote
    assert "printf 'GITHUB_TOKEN=%q\\n' \"$GITHUB_TOKEN\"" in DEPLOY.read_text(encoding="utf-8")


def test_a_realm_that_does_not_come_up_rolls_back_to_the_last_finished_deploy() -> None:
    remote = _remote_script()
    deploy_body = remote[remote.index("deploy_mode()") : remote.index("rollback_mode()")]
    assert "seed_generation_record" in deploy_body
    assert "handover_realm demo" in deploy_body
    assert "handover_realm mainnet" in deploy_body
    assert "record_generation" in deploy_body
    handover = _function_body(remote, "handover_realm")
    assert 'stop_realm_units "$realm"' in handover
    assert 'ensure_native_strategy_state "$realm"' in handover
    assert 'start_realm "$realm"' in handover
    assert 'rollback_after_failure "$realm"' in handover
    assert handover.index('rollback_after_failure "$realm"') < handover.index("return 1")
    assert handover.index("return 1") < handover.index('record_realm_fingerprint "$realm"')
    rollback = _function_body(remote, "rollback_after_failure")
    # A rolled-back generation that also fails stops the fleet instead of looping.
    assert 'if [ "${AUTO_ROLLBACK:-0}" = 1 ]; then' in rollback
    assert 'AUTO_ROLLBACK=1 EXPECTED_COMMIT="$target" deploy_mode' in rollback
    assert "rollback) rollback_mode ;;" in remote


def test_a_realm_whose_inputs_did_not_change_is_left_running() -> None:
    """A deploy that changes nothing a realm runs from — the recorder's config,
    a doc, the deploy script itself — must not restart the funded engine."""

    remote = _remote_script()
    fingerprint = _function_body(remote, "realm_fingerprint")
    # The engine source tree, not the binary: the binary embeds the commit.
    assert 'rev-parse "$commit:engine"' in fingerprint
    assert '"$commit:configs/signal-worker.$realm.json"' in fingerprint
    assert "$ENGINE_MAINNET_CONFIG" in fingerprint and "$ENGINE_DEMO_CONFIG" in fingerprint
    unchanged = _function_body(remote, "realm_unchanged")
    assert 'systemctl is-active --quiet "$worker_unit" && systemctl is-active --quiet "$owner_unit"' in unchanged

    deploy_body = remote[remote.index("deploy_mode()") : remote.index("rollback_mode()")]
    assert "if realm_unchanged demo && demo_candidate_running; then" in deploy_body
    assert "if realm_unchanged mainnet; then" in deploy_body
    for realm in ("demo", "mainnet"):
        assert f'echo "{realm}-ok result=unchanged-left-running"' in deploy_body
        # The handover, when it runs, records what it started so the next deploy can compare.
        assert f"handover_realm {realm}" in deploy_body
    assert deploy_body.index("stage_demo_candidate") < deploy_body.index("handover_realm demo")
    assert deploy_body.index("wait_demo_soak") < deploy_body.index("install_release")
    # The first gated deploy seeds the record from the commit that started the realm,
    # before anything is rendered, so it compares against what actually runs.
    assert deploy_body.index("seed_realm_fingerprints") < deploy_body.index("install_release")
    seed = _function_body(remote, "seed_realm_fingerprints")
    assert 'realm_fingerprint "$realm" "$deployed"' in seed and "$DEPLOYED_COMMIT_FILE" in seed
    assert "handover_realm demo" not in deploy_body[: deploy_body.index("prepare_demo_inputs")]


def test_ci_checks_main_pushes_and_keeps_release_work_explicit() -> None:
    workflow = (ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    triggers = workflow[workflow.index("\non:\n") : workflow.index("\npermissions:\n")]
    assert "pull_request:" in triggers
    assert "push:" in triggers
    assert "branches: [main]" in triggers
    push = triggers[triggers.index("  push:") : triggers.index("  pull_request:")]
    assert "paths-ignore" not in push
    assert '"**/*.md"' in triggers and '"docs/**"' in triggers

    ci = workflow[workflow.index("\n  ci:\n") : workflow.index("\n  rust:\n")]
    assert "github.event_name == 'pull_request'" in ci
    assert "github.event_name == 'push'" in ci
    assert "inputs.mode == 'deploy'" in ci

    rust = workflow[workflow.index("\n  rust:\n") : workflow.index("\n  rust-artifact:\n")]
    assert "github.event_name == 'push'" in rust
    assert "cargo test --workspace --all-targets --locked" in rust
    assert "--release" not in rust and "--profile" not in rust
    assert "inputs.mode == 'deploy' || inputs.mode == 'qualify'" in rust

    artifact = workflow[workflow.index("\n  rust-artifact:\n") : workflow.index("\n  rust-qualify:\n")]
    assert "inputs.mode == 'deploy'" in artifact
    assert "cargo build --release --locked --workspace --bins" in artifact
    assert "cargo test" not in artifact and "release_artifact.py qualify" not in artifact
    assert "retention-days: 2" in artifact

    qualify = workflow[workflow.index("\n  rust-qualify:\n") : workflow.index("\n  disarm:\n")]
    assert "inputs.mode == 'qualify'" in qualify
    assert "inputs.mode == 'deploy'" not in qualify
    assert "scripts/release_artifact.py qualify" in qualify
    assert "rust-soak-bench:" not in workflow

    vps = workflow[workflow.index("\n  vps:\n") :]
    assert "needs: [ci, rust, rust-artifact]" in vps
    assert "always()" in vps
    for result in ("needs.ci.result", "needs.rust.result", "needs.rust-artifact.result"):
        assert f"{result} == 'success'" in vps

    concurrency = workflow[workflow.index("concurrency:") : workflow.index("\njobs:\n")]
    assert "format('liquidity-migration-pr-{0}', github.event.pull_request.number)" in concurrency
    assert "format('liquidity-migration-checks-{0}', github.ref)" in concurrency
    assert "format('liquidity-migration-qualify-{0}', github.ref)" in concurrency
    assert "format('liquidity-migration-diagnose-{0}', github.run_id)" in concurrency
    assert "format('liquidity-migration-vps-{0}', github.ref)" in concurrency
    # The host's fetch of a private repository needs the run's token (issue #18).
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in vps[vps.index("Run VPS mode") :]


def test_deploy_preserves_unchanged_independent_units() -> None:
    remote = _remote_script()
    deploy_body = remote[remote.index("deploy_mode()") : remote.index("rollback_mode()")]
    assert "start_independent_units" in deploy_body
    independent = remote[
        remote.index("start_independent_units()") : remote.index(
            "# ------------------------------------------------------------ realm inputs"
        )
    ]
    assert 'wait_capture_ready "$unit" "$CAPTURE_STATUS" "$since"' in independent
    assert "result=unchanged-left-running" in independent


def _function_body(remote: str, name: str) -> str:
    body = remote[remote.index(f"{name}() {{") :]
    return body[: body.index("\n}\n") + 3]


def test_remote_deploy_enters_the_checkout_before_it_imports_the_package() -> None:
    # The remote body runs from the ssh login directory, and the venv installs
    # the runtime lock without the project, so every
    # `python -m liquidity_migration.*` resolves from the working directory.
    remote = _remote_script()
    assert 'cd "$REPO_DIR"' in _function_body(remote, "fetch_exact_commit")
    order = _function_body(remote, "deploy_mode")
    assert order.index("fetch_exact_commit") < order.index("install_python_environment")


@pytest.mark.parametrize("runtime_lock", [True, False])
def test_deploy_selects_host_dependencies_and_supports_older_checkouts(
    tmp_path: Path, runtime_lock: bool
) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / "requirements.lock").write_text("pytest==1\n", encoding="utf-8")
    if runtime_lock:
        (tmp_path / "requirements-runtime.lock").write_text("websocket-client==1\n", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    remote = _remote_script()
    result = subprocess.run(
        ["bash", "-c", "\n".join([
            "set -euo pipefail",
            _function_body(remote, "python_requirements_path"),
            _function_body(remote, "install_python_environment"),
            "install_python_environment",
        ])],
        env={**os.environ, "REPO_DIR": str(tmp_path), "PYTHON": str(python)},
        text=True,
        capture_output=True,
        check=True,
    )
    arguments = result.stdout.splitlines()
    expected = "requirements-runtime.lock" if runtime_lock else "requirements.lock"
    assert arguments[arguments.index("-r") + 1] == str(tmp_path / expected)
    assert "--no-deps" in arguments


def test_mainnet_takeover_reloads_the_owner_arming_switch() -> None:
    # The takeover unsets REAL_MONEY, so its allowlist must name it back or the
    # engine refuses every funded import. The gateway still reads
    # BYBIT_INVENTORY_CREDENTIAL_SET.
    body = _function_body(_remote_script(), "run_engine_takeover_command")
    subshell = body[body.index("unset BYBIT_DEMO_API_KEY") :]
    mainnet = subshell[subshell.index("mainnet)") :]
    mainnet = mainnet[: mainnet.index(";;")]
    assert "REAL_MONEY" in mainnet
    assert "BYBIT_INVENTORY_CREDENTIAL_SET" in mainnet


def test_the_systemd_unit_runs_the_packer_over_every_tape_and_receipts_it() -> None:
    unit = (SYSTEMD / "liquidity-migration-market-tape-upload.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/liquidity-migration/.venv/bin/python -m market_tape pack" in unit
    assert "--tape bybit-linear=/var/lib/liquidity-migration/forward-market" in unit
    assert "--remote-base gdrive:LiquidityMigration/market-tape" in unit
    assert "--state-dir /var/lib/liquidity-migration/market-tape-upload" in unit
    assert "--stamp-file /var/lib/liquidity-migration/receipts/market-tape-upload.last-success" in unit
    assert "Environment=RCLONE_CONFIG=/var/lib/liquidity-migration/market-tape-upload/rclone.conf" in unit
    assert "Environment=RCLONE_CONFIG_SEED=/etc/liquidity-migration/rclone.conf" in unit
    named = re.findall(r"--tape (\S+)", unit)
    assert named
    for text in named:
        name, separator, root = text.partition("=")
        assert separator and name and "/" not in name
        assert Path(root).is_absolute()
    timer = (SYSTEMD / "liquidity-migration-market-tape-upload.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start) + len("\n}\n")]


def _trace_start_realm(realm: str, tmp_path: Path) -> list[str]:
    """Every systemctl call `start_realm <realm>` makes, in order."""

    bin_dir = tmp_path / realm / "bin"
    bin_dir.mkdir(parents=True)
    trace = tmp_path / realm / "systemctl.trace"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SYSTEMCTL_TRACE"
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    deploy = _remote_script()
    harness = "\n".join(
        [
            "set -euo pipefail",
            'source "$LM_REPOSITORY_ROOT/deploy/lib_sleeves.sh"',
            'fail() { echo "$*" >&2; exit 1; }',
            # The heartbeat wait needs live units; the start order does not.
            "wait_fresh_heartbeat() { :; }",
            _function(deploy, "start_unit"),
            _function(deploy, "start_realm"),
            f'start_realm "{realm}"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SYSTEMCTL_TRACE": str(trace),
            "LM_REPOSITORY_ROOT": str(ROOT),
        },
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return trace.read_text(encoding="utf-8").splitlines()


def _trace_stop_realm(realm: str, tmp_path: Path) -> list[str]:
    bin_dir = tmp_path / f"stop-{realm}" / "bin"
    bin_dir.mkdir(parents=True)
    trace = tmp_path / f"stop-{realm}" / "systemctl.trace"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SYSTEMCTL_TRACE"
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    deploy = _remote_script()
    harness = "\n".join(
        [
            "set -euo pipefail",
            'source "$LM_REPOSITORY_ROOT/deploy/lib_sleeves.sh"',
            'fail() { echo "$*" >&2; exit 1; }',
            _function(deploy, "stop_realm_units"),
            f'stop_realm_units "{realm}"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SYSTEMCTL_TRACE": str(trace),
            "LM_REPOSITORY_ROOT": str(ROOT),
        },
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return trace.read_text(encoding="utf-8").splitlines()


def _native_state_paths(tmp_path: Path, realm: str) -> tuple[Path, list[Path]]:
    runtime = "liquidity-migration-engine" + ("-mainnet" if realm == "mainnet" else "")
    targets = tmp_path / "liquidity-migration/targets"
    return tmp_path / runtime / "engine.wal", [
        targets / f"long-{realm}-state.json",
        tmp_path / f"carry-{realm}/.cache/carry_sizing_anchors.json",
        targets / f"carry-{realm}.json",
        tmp_path / f"exodus-{realm}/exodus_state_identity.json",
        tmp_path / f"exodus-{realm}/exodus_state.json",
    ]


def _ensure_native_state(
    tmp_path: Path, realm: str, *, verify_status: int = 1,
    initialize_status: int = 0, final_verify_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    helper = _function(_remote_script(), "ensure_native_strategy_state")
    # Relocate the host's absolute state paths; the function's control flow is unchanged.
    helper = helper.replace("/var/lib/", f"{tmp_path}/")
    trace = tmp_path / "native-state.trace"
    trace.unlink(missing_ok=True)
    harness = "\n".join(
        [
            "set -uo pipefail",
            "ENGINE_DEMO_CONFIG=demo.toml",
            "ENGINE_MAINNET_CONFIG=mainnet.toml",
            'CARRY_DEMO_ROOT="$STATE_ROOT/carry-demo"',
            'CARRY_MAINNET_ROOT="$STATE_ROOT/carry-mainnet"',
            'EXODUS_DEMO_ROOT="$STATE_ROOT/exodus-demo"',
            'EXODUS_MAINNET_ROOT="$STATE_ROOT/exodus-mainnet"',
            "fail() { printf '%s\\n' \"$*\" >&2; exit 1; }",
            "verify_count=0",
            "run_engine_takeover_command() {",
            "  printf '%s %s %s\\n' \"$1\" \"$2\" \"$3\" >> \"$STATE_TRACE\"",
            '  if [ "$3" = initialize-native-strategy-state ]; then',
            '    return "$INITIALIZE_STATUS"',
            "  fi",
            "  verify_count=$((verify_count + 1))",
            '  if [ "$verify_count" -eq 1 ]; then return "$VERIFY_STATUS"; fi',
            '  return "$FINAL_VERIFY_STATUS"',
            "}",
            helper,
            f"ensure_native_strategy_state {realm}",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        env={
            **os.environ,
            "STATE_ROOT": str(tmp_path),
            "STATE_TRACE": str(trace),
            "VERIFY_STATUS": str(verify_status),
            "INITIALIZE_STATUS": str(initialize_status),
            "FINAL_VERIFY_STATUS": str(final_verify_status),
        },
        text=True, capture_output=True, check=False, timeout=10,
    )
    return result, trace.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
def test_verified_native_state_keeps_existing_wal_and_legacy_sources(tmp_path: Path, realm: str) -> None:
    wal, sources = _native_state_paths(tmp_path, realm)
    originals = {wal: b"canonical WAL", **{source: b"retained source" for source in sources}}
    for path, contents in originals.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    result, calls = _ensure_native_state(tmp_path, realm, verify_status=0)
    assert result.returncode == 0, result.stderr
    assert calls == [f"{realm} {realm}.toml verify-native-strategy-state"]
    assert "result=already-complete" in result.stdout
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
@pytest.mark.parametrize("wal_exists", [False, True])
def test_native_initialization_requires_empty_wal_and_no_legacy_sources(
    tmp_path: Path, realm: str, wal_exists: bool,
) -> None:
    wal, _ = _native_state_paths(tmp_path, realm)
    if wal_exists:
        wal.parent.mkdir(parents=True)
        wal.touch()
    result, calls = _ensure_native_state(tmp_path, realm)
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"{realm} {realm}.toml {command}" for command in
        ("verify-native-strategy-state", "initialize-native-strategy-state", "verify-native-strategy-state")
    ]
    assert "result=initialized-empty" in result.stdout


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
@pytest.mark.parametrize("existing", ["wal", "long", "carry-checkpoint", "carry-book", "exodus-identity", "exodus-state", "all-legacy"])
def test_unverified_native_state_never_initializes_over_retained_state(
    tmp_path: Path, realm: str, existing: str,
) -> None:
    wal, sources = _native_state_paths(tmp_path, realm)
    paths = dict(zip(
        ("wal", "long", "carry-checkpoint", "carry-book", "exodus-identity", "exodus-state"),
        (wal, *sources), strict=True,
    ))
    retained = sources if existing == "all-legacy" else [paths[existing]]
    for path in retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained state")
    result, calls = _ensure_native_state(tmp_path, realm)
    assert result.returncode != 0
    assert "compatible retained release" in result.stderr
    assert calls == [f"{realm} {realm}.toml verify-native-strategy-state"]
    assert all(path.read_bytes() == b"retained state" for path in retained)


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
@pytest.mark.parametrize("initialize_status,final_verify_status,expected_calls", [(19, 0, 2), (0, 19, 3)])
def test_native_initialization_and_verification_failures_propagate(
    tmp_path: Path, realm: str, initialize_status: int, final_verify_status: int, expected_calls: int,
) -> None:
    result, calls = _ensure_native_state(
        tmp_path, realm, initialize_status=initialize_status, final_verify_status=final_verify_status,
    )
    assert result.returncode != 0
    assert len(calls) == expected_calls
    assert "result=initialized-empty" not in result.stdout


def _trace_handover_realm(
    tmp_path: Path,
    *,
    state_status: int,
    start_status: int,
    retirement_status: int = 0,
    clear_status: int = 0,
) -> tuple[int, list[str]]:
    trace = tmp_path / f"handover-{state_status}-{start_status}-{retirement_status}-{clear_status}.trace"
    deploy = _remote_script()
    harness = "\n".join(
        [
            "set -uo pipefail",
            'trace() { printf \'%s\\n\' "$1" >> "$HANDOVER_TRACE"; }',
            "stop_realm_units() { trace stop; }",
            'retire_legacy_signal_sources() { trace retire; return "$RETIREMENT_STATUS"; }',
            'ensure_native_strategy_state() { trace state; return "$STATE_STATUS"; }',
            'clear_reconciliation_if_requested() { trace clear; return "$CLEAR_STATUS"; }',
            'start_realm() { trace start; return "$START_STATUS"; }',
            "rollback_after_failure() { trace rollback; }",
            "record_realm_fingerprint() { trace record; }",
            _function(deploy, "handover_realm"),
            "handover_realm demo; exit $?",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={
            **os.environ,
            "HANDOVER_TRACE": str(trace),
            "STATE_STATUS": str(state_status),
            "START_STATUS": str(start_status),
            "RETIREMENT_STATUS": str(retirement_status),
            "CLEAR_STATUS": str(clear_status),
        },
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return result.returncode, trace.read_text(encoding="utf-8").splitlines()


def _units(command: str) -> list[str]:
    return subprocess.run(
        ["bash", "-c", f"source deploy/lib_sleeves.sh; {command}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split()


def test_a_deploy_handover_stops_units_without_disabling_the_watchdogs(tmp_path: Path) -> None:
    for realm in ("demo", "mainnet"):
        calls = _trace_stop_realm(realm, tmp_path)
        units = _units(f"lm_realm_units {realm}")
        assert units, realm
        for unit in units:
            assert f"stop {unit}" in calls, (realm, unit, calls)
            assert f"reset-failed {unit}" in calls, (realm, unit, calls)
        assert all(not call.startswith("disable ") for call in calls), (realm, calls)


def test_every_handover_failure_rolls_back_before_recording_a_fingerprint(tmp_path: Path) -> None:
    assert _trace_handover_realm(tmp_path, state_status=0, start_status=0, retirement_status=1) == (
        1,
        ["stop", "retire", "rollback"],
    )
    assert _trace_handover_realm(tmp_path, state_status=1, start_status=0) == (
        1,
        ["stop", "retire", "state", "rollback"],
    )
    assert _trace_handover_realm(tmp_path, state_status=0, start_status=1) == (
        1,
        ["stop", "retire", "state", "clear", "start", "rollback"],
    )
    assert _trace_handover_realm(tmp_path, state_status=0, start_status=0) == (
        0,
        ["stop", "retire", "state", "clear", "start", "record"],
    )


def test_reconciliation_clear_failure_prevents_start_after_verified_state(tmp_path: Path) -> None:
    assert _trace_handover_realm(tmp_path, state_status=0, start_status=0, clear_status=19) == (
        1,
        ["stop", "retire", "state", "clear", "rollback"],
    )


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
def test_legacy_retirement_uses_the_realms_optional_plan_and_preserves_failure(
    tmp_path: Path, realm: str
) -> None:
    helper = _function(_remote_script(), "retire_legacy_signal_sources")
    helper = helper.replace("/etc/liquidity-migration/", f"{tmp_path}/")
    harness = "\n".join(
        [
            "set -uo pipefail",
            "ENGINE_DEMO_CONFIG=demo.toml",
            "ENGINE_MAINNET_CONFIG=mainnet.toml",
            'run_engine_takeover_command() { printf \'%s\\n\' "$@"; return 19; }',
            helper,
            f"retire_legacy_signal_sources {realm}",
        ]
    )
    missing = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, check=False)
    assert (missing.returncode, missing.stdout) == (0, "")
    plan = tmp_path / f"legacy-signal-retirements.{realm}.json"
    plan.write_text("[]\n", encoding="utf-8")
    present = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, check=False)
    assert present.returncode == 19
    assert present.stdout.splitlines() == [
        realm, f"{realm}.toml", "retire-legacy-signal-sources", "--plan", str(plan), "--execute"
    ]


def _run_reconciliation_clear(tmp_path: Path, realm: str, status: int) -> subprocess.CompletedProcess[str]:
    helper = _function(_remote_script(), "clear_reconciliation_if_requested")
    helper = helper.replace("/etc/liquidity-migration/", f"{tmp_path}/")
    harness = "\n".join(
        [
            "set -uo pipefail",
            "ENGINE_DEMO_CONFIG=demo.toml",
            "ENGINE_MAINNET_CONFIG=mainnet.toml",
            "fail() { printf '%s\\n' \"$*\" >&2; exit 1; }",
            "run_engine_takeover_command() {",
            '  "$TEST_PYTHON" -c \'import json, os, sys; '
            'open(os.environ["ARGUMENTS_PATH"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\' "$@"',
            '  return "$CLEAR_STATUS"',
            "}",
            helper,
            f"clear_reconciliation_if_requested {realm}",
        ]
    )
    return subprocess.run(
        ["bash", "-c", harness],
        env={
            **os.environ,
            "TEST_PYTHON": sys.executable,
            "ARGUMENTS_PATH": str(tmp_path / "arguments.jsonl"),
            "CLEAR_STATUS": str(status),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
def test_optional_reconciliation_note_is_literal_preserved_on_failure_and_retired_on_success(
    tmp_path: Path, realm: str
) -> None:
    arguments = tmp_path / "arguments.jsonl"
    note_path = tmp_path / f"reconcile-clear.{realm}.note"
    applied = tmp_path / f"reconcile-clear.{realm}.note.applied"
    sentinel = tmp_path / "must-not-execute"
    note = f"ENA historical close; evidence=proof.json; literal=$(touch {sentinel}) `touch {sentinel}`"
    other = "mainnet" if realm == "demo" else "demo"
    (tmp_path / f"reconcile-clear.{other}.note").write_text("other realm\n", encoding="utf-8")
    assert _run_reconciliation_clear(tmp_path, realm, 0).returncode == 0
    assert not arguments.exists()

    note_path.write_text(note + "\n", encoding="utf-8")
    failed = _run_reconciliation_clear(tmp_path, realm, 19)
    assert failed.returncode == 19, failed.stderr
    assert note_path.read_text(encoding="utf-8") == note + "\n"
    assert not applied.exists()
    assert not sentinel.exists()

    succeeded = _run_reconciliation_clear(tmp_path, realm, 0)
    assert succeeded.returncode == 0, succeeded.stderr
    assert not note_path.exists()
    assert applied.read_text(encoding="utf-8") == note + "\n"
    assert not sentinel.exists()
    expected = [realm, f"{realm}.toml", "reconcile-clear", "--note", note, "--execute"]
    assert [json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()] == [expected, expected]
    assert _run_reconciliation_clear(tmp_path, realm, 0).returncode == 0
    assert len(arguments.read_text(encoding="utf-8").splitlines()) == 2


def test_empty_reconciliation_note_is_retained_without_running_a_clear(tmp_path: Path) -> None:
    pending = tmp_path / "reconcile-clear.demo.note"
    pending.write_text("\n", encoding="utf-8")
    result = _run_reconciliation_clear(tmp_path, "demo", 0)
    assert result.returncode == 1
    assert "note is empty" in result.stderr
    assert pending.exists()
    assert not (tmp_path / "arguments.jsonl").exists()


@pytest.mark.parametrize("realm", ["demo", "mainnet"])
def test_pending_reconciliation_note_prevents_unchanged_realm_skip(tmp_path: Path, realm: str) -> None:
    helper = _function(_remote_script(), "realm_unchanged")
    helper = helper.replace("/etc/liquidity-migration/", f"{tmp_path}/")
    (tmp_path / f"{realm}.fingerprint").write_text("unchanged\n", encoding="utf-8")
    harness = "\n".join(
        [
            "set -uo pipefail",
            "lm_signal_worker_unit() { printf worker; }",
            "lm_owner_unit() { printf engine; }",
            "realm_fingerprint() { printf unchanged; }",
            "systemctl() { return 0; }",
            helper,
            f"realm_unchanged {realm}",
        ]
    )

    def unchanged() -> int:
        return subprocess.run(
            ["bash", "-c", harness],
            env={**os.environ, "RELEASE_DIR": str(tmp_path)},
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        ).returncode

    assert unchanged() == 0
    pending = tmp_path / f"reconcile-clear.{realm}.note"
    pending.write_text("verified historical close\n", encoding="utf-8")
    assert unchanged() == 1
    pending.rename(tmp_path / f"reconcile-clear.{realm}.note.applied")
    assert unchanged() == 0


def test_a_realm_start_runs_its_liveness_watchdog_after_every_unit_it_watches(
    tmp_path: Path,
) -> None:
    """The watchdog alerts on any inactive manifest unit, so it goes last.

    Its stop order puts it ahead of the realm's timers in the start list. Run
    it there and its first pass pages CRITICAL on timers this same start is
    about to enable.
    """

    for realm in ("demo", "mainnet"):
        calls = _trace_start_realm(realm, tmp_path)
        jobs = _units(f"lm_immediate_timer_jobs {realm}")
        assert jobs, realm
        others = [unit for unit in _units(f"lm_activation_units {realm} start") if unit not in jobs]
        assert others, realm
        for job in jobs:
            assert f"start {job}" in calls, (realm, job, calls)
            assert calls.count(f"start {job}") == 1, (realm, job, calls)
            for unit in others:
                assert f"enable --now {unit}" in calls, (realm, unit, calls)
                assert calls.index(f"enable --now {unit}") < calls.index(f"start {job}"), (
                    realm,
                    unit,
                    calls,
                )


def _run_heartbeat_gate(
    tmp_path: Path, name: str, crash_loop: bool, unhealthy: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """`wait_fresh_heartbeat` against a stubbed unit that always writes a
    fresh heartbeat, restarting between reads only when crash_loop is set."""

    bin_dir = tmp_path / name / "bin"
    bin_dir.mkdir(parents=True)
    counter = tmp_path / name / "restarts"
    counter.write_text("0", encoding="utf-8")
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
property=""
for argument in "$@"; do
    case "$argument" in --property=*) property="${argument#--property=}" ;; esac
done
case "$property" in
    ActiveState) echo active ;;
    MainPID)
        if [ "$LM_TEST_CRASH_LOOP" = 1 ]; then
            count=$(( $(cat "$LM_TEST_RESTARTS") + 1 ))
            printf '%s' "$count" > "$LM_TEST_RESTARTS"
            echo "$(( 1000 + count ))"
        else
            echo 4242
        fi
        ;;
    NRestarts) [ "$LM_TEST_CRASH_LOOP" = 1 ] && cat "$LM_TEST_RESTARTS" || echo 0 ;;
    *) echo "" ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    # The gate's own waits; the loop under test does not need wall-clock time.
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    stat = bin_dir / "stat"
    stat.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:3] == ['-c', '%Y']\n"
        "print(int(os.stat(sys.argv[3]).st_mtime))\n",
        encoding="utf-8",
    )
    stat.chmod(0o755)

    heartbeat = tmp_path / name / "heartbeat.json"
    import time
    payload = {"pid": 4242, "wall_ts_ms": time.time_ns() // 1_000_000,
               "may_open": True, "rolling_loss_tripped": False, "strategy_errors": []}
    if unhealthy == "latched":
        payload["may_open"] = False
    elif unhealthy == "strategy":
        payload["strategy_errors"] = [{"strategy": "LONG", "error": "failed reducer"}]
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")
    remote = _remote_script()
    # Defaulted, not required, so the gate's behaviour is what fails this
    # harness rather than the absence of the constant.
    settle = next(
        (line for line in remote.splitlines() if line.startswith("HEARTBEAT_SETTLE_SECONDS=")),
        "HEARTBEAT_SETTLE_SECONDS=12",
    )
    harness = "\n".join(
        [
            "set -euo pipefail",
            'fail() { echo "deploy failed: $*" >&2; exit 1; }',
            settle,
            _function(remote, "wait_fresh_heartbeat"),
            f'wait_fresh_heartbeat liquidity-migration-engine.service "{heartbeat}" 1',
        ]
    )
    return subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "LM_TEST_CRASH_LOOP": "1" if crash_loop else "0",
            "LM_TEST_RESTARTS": str(counter),
            "PYTHON": sys.executable,
            "REPO_DIR": str(ROOT),
        },
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_the_heartbeat_gate_refuses_a_unit_that_restarts_after_each_heartbeat(
    tmp_path: Path,
) -> None:
    """The engine and the signal worker write the heartbeat before they read
    the state that can abort them, so a crash loop republishes a fresh
    heartbeat every RestartSec. The rollback of 2026-09-05 22:44 UTC read that
    as `heartbeat-ok` and reported `deploy-ok` over two dead signal workers."""

    result = _run_heartbeat_gate(tmp_path, "crash-loop", crash_loop=True)

    assert result.returncode != 0, result.stdout
    assert "heartbeat-ok" not in result.stdout
    assert "restarts after each heartbeat" in result.stderr


def test_the_heartbeat_gate_accepts_a_unit_that_holds_one_process(tmp_path: Path) -> None:
    result = _run_heartbeat_gate(tmp_path, "settled", crash_loop=False)

    assert result.returncode == 0, result.stderr
    assert "heartbeat-ok unit=liquidity-migration-engine.service" in result.stdout
    assert "pid=4242" in result.stdout


@pytest.mark.parametrize("unhealthy", ["latched", "strategy"])
def test_stable_fresh_but_unhealthy_engine_cannot_pass_handover(tmp_path: Path, unhealthy: str) -> None:
    result = _run_heartbeat_gate(tmp_path, unhealthy, crash_loop=False, unhealthy=unhealthy)
    assert result.returncode != 0, "fresh heartbeat with a stable process incorrectly passed unhealthy handover"
    assert "heartbeat-ok" not in result.stdout


def test_the_heartbeat_gate_reads_unit_state_and_not_only_file_freshness() -> None:
    gate = _function_body(_remote_script(), "wait_fresh_heartbeat")

    assert 'systemctl show --property=NRestarts --value "$unit"' in gate
    assert 'systemctl show --property=MainPID --value "$unit"' in gate
    assert 'sleep "$HEARTBEAT_SETTLE_SECONDS"' in gate
    # is-active is true for the instant a crash-looping process is running.
    assert "systemctl is-active" not in gate


def _rollback_fixture(tmp_path: Path, changed: str) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Rollback fixture")
    git("config", "user.email", "rollback@example.invalid")
    for path in (
        "engine/engine-core/src/lib.rs",
        "engine/Cargo.lock",
        "rust-toolchain.toml",
        ".cargo/config.toml",
        ".github/workflows/vps-deploy.yml",
        "scripts/ops.sh",
    ):
        destination = repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("before\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "predecessor")
    previous = git("rev-parse", "HEAD")
    (repo / changed).write_text("after\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "candidate")
    return repo, previous, git("rev-parse", "HEAD")


def _run_rollback(
    tmp_path: Path, *, changed: str, automatic: bool, displaced_checkout: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], dict[str, bytes], dict[str, bytes]]:
    repo, target, current = _rollback_fixture(tmp_path, changed)
    deployed = tmp_path / "deployed-commit"
    deployed.write_text(current if displaced_checkout else target, encoding="utf-8")
    previous = tmp_path / "previous-commit"
    previous.write_text(target, encoding="utf-8")
    if displaced_checkout:
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", target], check=True)
    state = tmp_path / "state"
    state.mkdir()
    for name, data in {
        "engine": b"candidate executable",
        "signal-worker": b"candidate worker",
        "engine.wal.000001": b"legacy history",
        "engine.wal.000002": b"ExecutionPrecisionV1, OrderIdEpoch, exact fill",
        "worker-checkpoint.json": b"new committed sequence",
        "spool.json": b"unconsumed candidate event",
    }.items():
        (state / name).write_bytes(data)
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    trace = tmp_path / "rollback.trace"
    trace.touch()
    remote = _remote_script()
    compatibility = (
        _function(remote, "rollback_runtime_compatible")
        if "rollback_runtime_compatible() {" in remote else ""
    )
    harness = "\n".join([
        "set -euo pipefail",
        'fail() { echo "deploy failed: $*" >&2; exit 1; }',
        'deploy_mode() { printf "deploy %s\\n" "$EXPECTED_COMMIT" >> "$ROLLBACK_TRACE"; '
        'printf predecessor > "$ROLLBACK_STATE/engine"; }',
        'systemctl() { printf "systemctl %s\\n" "$*" >> "$ROLLBACK_TRACE"; }',
        'rollback_target() { printf "%s\\n" "$ROLLBACK_TARGET"; }',
        compatibility,
        _function(remote, "rollback_after_failure"),
        _function(remote, "rollback_mode"),
        "rollback_after_failure mainnet" if automatic else "rollback_mode",
    ])
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env={
            **os.environ,
            "REPO_DIR": str(repo),
            "EXPECTED_COMMIT": current,
            "DEPLOYED_COMMIT_FILE": str(deployed),
            "PREVIOUS_COMMIT_FILE": str(previous),
            "ROLLBACK_TARGET": target,
            "ROLLBACK_TRACE": str(trace),
            "ROLLBACK_STATE": str(state),
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    after = {path.name: path.read_bytes() for path in state.iterdir()}
    return result, trace.read_text(encoding="utf-8").splitlines(), before, after


@pytest.mark.parametrize("automatic", [True, False], ids=["automatic", "manual"])
@pytest.mark.parametrize("changed", [
    "engine/engine-core/src/lib.rs",
    "engine/Cargo.lock",
    "rust-toolchain.toml",
    ".cargo/config.toml",
    ".github/workflows/vps-deploy.yml",
])
def test_rollback_preserves_candidate_and_advanced_state_when_runtime_compatibility_is_unknown(
    tmp_path: Path, changed: str, automatic: bool,
) -> None:
    result, calls, before, after = _run_rollback(tmp_path, changed=changed, automatic=automatic)
    assert calls == [], result.stdout + result.stderr
    assert after == before
    assert result.returncode != 0
    assert "forward repair" in result.stderr


@pytest.mark.parametrize("automatic", [True, False], ids=["automatic", "manual"])
def test_an_ops_only_rollback_with_identical_runtime_inputs_remains_available(
    tmp_path: Path, automatic: bool,
) -> None:
    result, calls, before, after = _run_rollback(
        tmp_path, changed="scripts/ops.sh", automatic=automatic,
    )
    assert len(calls) == 1 and calls[0].startswith("deploy "), result.stdout + result.stderr
    assert result.returncode == (1 if automatic else 0)
    assert after.pop("engine") == b"predecessor"
    before.pop("engine")
    assert after == before


def test_rollback_also_checks_the_recorded_deployed_runtime_when_checkout_has_moved(
    tmp_path: Path,
) -> None:
    result, calls, before, after = _run_rollback(
        tmp_path, changed="engine/engine-core/src/lib.rs", automatic=False,
        displaced_checkout=True,
    )
    assert calls == [], result.stdout + result.stderr
    assert after == before
    assert result.returncode != 0
    assert "forward repair" in result.stderr


def test_an_explicit_older_deploy_cannot_bypass_rollback_compatibility(tmp_path: Path) -> None:
    repo, target, current = _rollback_fixture(tmp_path, "engine/engine-core/src/lib.rs")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)], check=True)
    branch = subprocess.check_output(
        ["git", "-C", str(repo), "branch", "--show-current"], text=True,
    ).strip()
    deployed = tmp_path / "deployed-commit"
    deployed.write_text(current, encoding="utf-8")
    remote = _remote_script()
    harness = "\n".join([
        "set -euo pipefail",
        'fail() { echo "deploy failed: $*" >&2; exit 1; }',
        'git_authorized() { git -C "$REPO_DIR" "$@"; }',
        _function(remote, "rollback_runtime_compatible"),
        _function(remote, "fetch_exact_commit"),
        "fetch_exact_commit",
    ])
    result = subprocess.run(
        ["bash", "-c", harness],
        env={
            **os.environ,
            "REPO_DIR": str(repo),
            "EXPECTED_COMMIT": target,
            "DEPLOYED_COMMIT_FILE": str(deployed),
            "REMOTE": "origin",
            "BRANCH": branch,
        },
        text=True, capture_output=True, timeout=30, check=False,
    )
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert head == current, result.stdout + result.stderr
    assert result.returncode != 0
    assert "forward repair" in result.stderr
