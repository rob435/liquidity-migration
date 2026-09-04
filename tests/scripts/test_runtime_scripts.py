"""The deploy script, the fleet's unit files, and their load-bearing wiring."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
SYSTEMD = ROOT / "deploy" / "systemd"
WORKFLOW = ROOT / ".github" / "workflows" / "vps-deploy.yml"


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
        "scripts/runtime/pack_market_tape.py",
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
    assert "start_realm mainnet" in deploy_body
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
    assert "options: [deploy, rollback, verify, diagnose, disarm-mainnet]" in workflow
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
    assert "if ! (start_realm demo); then\n            rollback_after_failure demo" in deploy_body
    assert "if ! (start_realm mainnet); then\n                rollback_after_failure mainnet" in deploy_body
    assert "record_generation" in deploy_body
    rollback = remote[remote.index("rollback_after_failure()") : remote.index("# Every liquidity-migration unit")]
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
    for realm in ("demo", "mainnet"):
        assert f"if realm_unchanged {realm}; then" in deploy_body
        assert f'echo "{realm}-ok result=unchanged-left-running"' in deploy_body
        # The handover, when it runs, records what it started so the next deploy can compare.
        handover = deploy_body[deploy_body.index(f"stop_realm_units {realm}") :]
        assert handover.index(f"record_realm_fingerprint {realm}") > handover.index(f"start_realm {realm}")
    # Nothing stops before the release is on disk; both realms stay up through install.
    assert deploy_body.index("install_release") < deploy_body.index("stop_realm_units demo")
    # The first gated deploy seeds the record from the commit that started the realm,
    # before anything is rendered, so it compares against what actually runs.
    assert deploy_body.index("seed_realm_fingerprints") < deploy_body.index("install_release")
    seed = _function_body(remote, "seed_realm_fingerprints")
    assert 'realm_fingerprint "$realm" "$deployed"' in seed and "$DEPLOYED_COMMIT_FILE" in seed
    assert "stop_realm_units demo" not in deploy_body[: deploy_body.index("prepare_demo_inputs")]


def test_ci_tests_the_debug_build_on_the_gate_and_the_release_build_off_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    rust = workflow[workflow.index("\n  rust:\n") : workflow.index("\n  rust-artifact:\n")]
    assert "cargo test --workspace --all-targets --locked" in rust
    assert "--release" not in rust and "--profile" not in rust
    release = workflow[workflow.index("\n  rust-soak-bench:\n") : workflow.index("\n  disarm:\n")]
    assert "cargo test --workspace --all-targets --release --locked" in release
    vps = workflow[workflow.index("\n  vps:\n") :]
    assert "needs: [ci, rust, rust-artifact]" in vps
    # Push runs never queue behind each other; only dispatched VPS operations share a group.
    concurrency = workflow[workflow.index("concurrency:") : workflow.index("\njobs:\n")]
    assert "format('liquidity-migration-ci-{0}', github.run_id)" in concurrency
    assert "format('liquidity-migration-vps-{0}', github.ref)" in concurrency
    assert "inputs.mode != 'diagnose'" in concurrency
    # The host's fetch of a private repository needs the run's token (issue #18).
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in vps[vps.index("Run VPS mode") :]


def test_deploy_never_stops_an_independent_unit() -> None:
    remote = _remote_script()
    stop_fleet = remote[remote.index("stop_fleet()") : remote.index("install_units()")]
    assert "lm_independent_units" in stop_fleet
    assert "lm_host_liqmig_units" in stop_fleet
    assert "list-unit-files" not in stop_fleet
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
    # requirements.lock without the project, so every
    # `python -m liquidity_migration.*` resolves from the working directory.
    remote = _remote_script()
    assert 'cd "$REPO_DIR"' in _function_body(remote, "fetch_exact_commit")
    order = _function_body(remote, "deploy_mode")
    assert order.index("fetch_exact_commit") < order.index("install_python_environment")


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

    deploy = DEPLOY.read_text(encoding="utf-8")
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


def _units(command: str) -> list[str]:
    return subprocess.run(
        ["bash", "-c", f"source deploy/lib_sleeves.sh; {command}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split()


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
