from __future__ import annotations

from pathlib import Path


def test_runtime_scripts_do_not_delete_live_cycle_locks() -> None:
    repo = Path(__file__).resolve().parents[1]
    scripts = [
        repo / "scripts" / "run_bybit_demo_event_engine.sh",
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "rm -f \"$DATA_ROOT/.locks/" not in text
        assert "mkdir -p \"$DATA_ROOT/.locks\"" in text


def test_event_entry_runner_default_cadence_is_rate_limit_safe() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_demo_event_engine.sh").read_text(encoding="utf-8")

    assert 'INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"' in text
    assert "cycle_elapsed_seconds=$(($(date +%s) - cycle_start_epoch))" in text
    assert "starting next cycle immediately" in text


def test_systemd_entry_runner_uses_vps_cadence() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "deploy" / "systemd" / "model050426-bybit-demo.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=STRATEGY_PROFILE=demo_relaxed" in text
    assert "Environment=UNIVERSE_RANK_END=300" in text
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in text


def test_event_entry_runner_only_submits_active_champion_profile() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_demo_event_engine.sh").read_text(encoding="utf-8")

    assert 'SUBMIT_ORDERS:-0}" == "1"' in text
    assert '$STRATEGY_PROFILE" != "demo_relaxed"' in text
    assert "champion/challenger stack" in text


def test_live_runners_do_not_write_repo_bytecode() -> None:
    repo = Path(__file__).resolve().parents[1]
    paths = [
        repo / "scripts" / "run_bybit_demo_event_engine.sh",
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
        repo / "deploy" / "systemd" / "model050426-bybit-demo.service",
        repo / "deploy" / "systemd" / "model050426-bybit-risk.service",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "PYTHONDONTWRITEBYTECODE" in text


def test_vps_deploy_script_verifies_promoted_live_settings() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")

    assert "EXPECTED_COMMIT" in text
    assert "BatchMode=yes" in text
    assert "git remote set-url" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    assert "liqmig_union_q40_h3_tp26_g100_qsqueeze" in text
    assert "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6" in text
    assert "demo.take_profit_pcts == (0.21,)" in text
    assert "demo.failed_fade_exit_hours == 6" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "model050426-bybit-demo.service" in text
    assert "model050426-bybit-risk.service" in text
    assert "--property=Environment" not in text


def test_vps_verify_script_is_read_only_and_checks_live_state() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "verify_vps_live.sh").read_text(encoding="utf-8")

    assert "git pull" not in text
    assert "systemctl restart" not in text
    assert "liqmig_union_q40_h3_tp26_g100_qsqueeze" in text
    assert "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "Environment=STRATEGY_PROFILE=demo_relaxed" in text
    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=UNIVERSE_RANK_END=300" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    assert "verify-ok commit=" in text
    assert "--property=Environment" not in text


def test_github_vps_deploy_workflow_uses_checked_scripts_and_host_key() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch" in text
    assert "push:" in text
    assert "branches:" in text
    assert "github.event_name == 'push' || inputs.mode == 'deploy'" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'verify'" in text
    assert "VPS_SSH_PRIVATE_KEY" in text
    assert "ssh-keyscan -T 10 -t ed25519" in text
    assert "SHA256:c4K1qg1rx5kH/706qNTdsHYsCDP/o5GIHW1GAHCjwgY" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "EXPECTED_COMMIT=\"$GITHUB_SHA\"" in text
    assert "EXPECTED_TELEGRAM_CHAT_ID" in text


def test_vps_recovery_command_printer_uses_pinned_commit_url() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "print_vps_recovery_command.sh").read_text(
        encoding="utf-8"
    )

    assert "git rev-parse" in text
    assert "raw.githubusercontent.com/rob435/MODEL05042026" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    assert 'EXPECTED_COMMIT="$commit_sha" bash' in text
    assert "CLEAN_DIRTY_CHECKOUT=1" in text
    assert 'EXPECTED_COMMIT="$commit_sha" CLEAN_DIRTY_CHECKOUT=1 bash' in text
    assert "scripts/verify_vps_live.sh" in text


def test_vps_console_recovery_script_restores_key_and_deploys() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_console_recover_and_deploy.sh").read_text(
        encoding="utf-8"
    )

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX" in text
    assert "GITHUB_ACTIONS_SSH_PUBLIC_KEY" in text
    assert "apt-get install -y ca-certificates git openssh-server python3 python3-venv python3-pip" in text
    assert "CLEAN_DIRTY_CHECKOUT" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "99-model050426-recovery.conf" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "PubkeyAuthentication yes" in text
    assert "systemctl restart ssh.service" in text
    assert "model050426-deploy-backups" in text
    assert "git reset --hard" in text
    assert "git clean -fd" in text
    assert "git clone" in text
    assert "git remote set-url" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    assert "pip install -e \".[dev]\"" in text
    assert "liqmig_union_q40_h3_tp26_g100_qsqueeze" in text
    assert "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6" in text
    assert "model050426-bybit-demo.service" in text
    assert "model050426-bybit-risk.service" in text
    assert "--property=Environment" not in text
