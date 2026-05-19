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
    text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-demo.service").read_text(
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
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-demo.service",
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-risk.service",
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
    assert "bybit-demo.env.backup" in text
    assert "sed -i \"s/^TELEGRAM_CHAT_ID=" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "systemctl disable --now" in text
    assert "model050426.service" in text
    assert "model050426-bybit-demo-signal.timer" in text
    assert "liquidity-migration-bybit-demo.service" in text
    assert "liquidity-migration-bybit-risk.service" in text
    assert "retired unit" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-demo.service" in text
    assert "Environment=STRATEGY_PROFILE=demo_relaxed" in text
    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=UNIVERSE_RANK_END=300" in text
    assert "Environment=UNIVERSE_MAX_SYMBOLS=300" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment" not in text


def test_vps_verify_script_is_read_only_and_checks_live_state() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "verify_vps_live.sh").read_text(encoding="utf-8")

    assert "git pull" not in text
    assert "systemctl restart" not in text
    assert "retired unit" in text
    assert "model050426.service" in text
    assert "model050426-bybit-demo-signal.timer" in text
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
    assert '"deploy/systemd/*.service"' in text
    assert '"deploy/systemd/**"' not in text
    assert '"scripts/**"' not in text
    assert "wait-deploy" in text
    assert "wait_timeout_seconds" in text
    assert "wait_interval_seconds" in text
    assert "github.event_name == 'push' || inputs.mode == 'deploy'" in text
    assert (
        "github.event_name == 'workflow_dispatch' && inputs.mode == 'wait-deploy'"
        in text
    )
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'verify'" in text
    assert "VPS_SSH_PRIVATE_KEY" in text
    assert "GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT" in text
    assert "SHA256:oC3JWnYE9LTto1dHEkdT+puS1n4z1qm2EWjQ+QUEf0s" in text
    assert "ssh-keygen -y -f ~/.ssh/vps_deploy_key" in text
    assert "ssh-keygen -lf ~/.ssh/vps_deploy_key.pub -E sha256" in text
    assert "ssh-keyscan -T 10 -t ed25519" in text
    assert "SHA256:c4K1qg1rx5kH/706qNTdsHYsCDP/o5GIHW1GAHCjwgY" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    assert "EXPECTED_COMMIT=\"$GITHUB_SHA\"" in text
    assert "EXPECTED_TELEGRAM_CHAT_ID" in text


def test_vps_recovery_command_printer_uses_pinned_commit_url() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "print_vps_recovery_command.sh").read_text(
        encoding="utf-8"
    )

    assert "git rev-parse" in text
    assert "--recommended-only" in text
    assert "--rescue-only" in text
    assert "recommended_only" in text
    assert "rescue_only" in text
    assert "recommended_command=" in text
    assert "rescue_command=" in text
    assert "raw.githubusercontent.com/rob435/MODEL05042026" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "Wait locally for restored SSH access" in text
    assert "Hetzner Rescue SSH-key restore" in text
    assert "Recommended full Hetzner Cloud console recovery" in text
    assert "Open the Hetzner Cloud web console for 204.168.202.167" in text
    assert "enable" in text
    assert "Hetzner Rescue" in text
    assert "Strict full recovery" in text
    assert "CLEAN_DIRTY_CHECKOUT=1" in text
    assert 'EXPECTED_COMMIT="$commit_sha" CLEAN_DIRTY_CHECKOUT=1 bash' in text
    assert 'curl -fsSL $rescue_script_url | bash' in text
    assert 'EXPECTED_COMMIT="$commit_sha" bash' in text
    assert "scripts/verify_vps_live.sh" in text


def test_wait_for_vps_recovery_script_waits_then_runs_checked_deploy_and_verify() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "wait_for_vps_recovery_and_deploy.sh").read_text(
        encoding="utf-8"
    )

    assert "WAIT_TIMEOUT_SECONDS" in text
    assert "WAIT_INTERVAL_SECONDS" in text
    assert "BatchMode=yes" in text
    assert "ssh-ready" in text
    assert "ssh-not-ready" in text
    assert "accept SSH public-key auth" in text
    assert "scripts/print_vps_recovery_command.sh --rescue-only" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "EXPECTED_COMMIT" in text
    assert "EXPECTED_TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "wait-deploy-verify-ok" in text
    assert "systemctl restart" not in text


def test_vps_ssh_restore_script_only_restores_access() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_restore_ssh_access.sh").read_text(
        encoding="utf-8"
    )

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "AuthenticationMethods publickey" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "sshd_config.liquidity-migration-backup" in text
    assert "Restored authorized key fingerprints:" in text
    assert 'ssh-keygen -lf "$tmp_public_key" -E sha256' in text
    assert "effective_sshd_config" in text
    assert "grep -Eq '^authenticationmethods publickey$'" in text
    assert "mkdir -p /run/sshd" in text
    assert 'sshd_root_context="user=root,host=localhost,addr=127.0.0.1"' in text
    assert 'sshd -T -C "$sshd_root_context"' in text
    assert "systemctl restart ssh.service" in text
    assert "ssh-restore-ok" in text
    assert "liquidity-migration-bybit-demo.service" not in text
    assert "pip install" not in text


def test_vps_rescue_restore_script_mounts_installed_root_and_restores_keys() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_rescue_restore_ssh_access.sh").read_text(
        encoding="utf-8"
    )

    assert "TARGET_ROOT" in text
    assert "MOUNT_ROOT" in text
    assert "is_installed_root" in text
    assert "lsblk -rpno NAME,FSTYPE,TYPE,MOUNTPOINT" in text
    assert "vgchange -ay" in text
    assert 'mount "$device" "$MOUNT_ROOT"' in text
    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX" in text
    assert "chroot \"$target_root\" usermod -U root" in text
    assert "99-liquidity-migration-recovery.conf" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "AuthenticationMethods publickey" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "sshd_config.liquidity-migration-backup" in text
    assert "Restored authorized key fingerprints" in text
    assert "rescue-ssh-restore-ok" in text
    assert "Reboot the VPS from local disk" in text
    assert "liquidity-migration-bybit-demo.service" not in text
    assert "pip install" not in text


def test_vps_console_recovery_script_restores_key_and_deploys() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_console_recover_and_deploy.sh").read_text(
        encoding="utf-8"
    )

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX" in text
    assert "GITHUB_ACTIONS_SSH_PUBLIC_KEY" in text
    assert "for binary in git python3 sshd" in text
    assert "apt-get install -y ca-certificates git openssh-server python3 python3-venv python3-pip" in text
    assert "CLEAN_DIRTY_CHECKOUT" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "bybit-demo.env.backup" in text
    assert "sed -i \"s/^TELEGRAM_CHAT_ID=" in text
    assert "99-liquidity-migration-recovery.conf" in text
    assert "chmod 700 /root" in text
    assert "usermod -U root" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "PubkeyAuthentication yes" in text
    assert "AuthenticationMethods publickey" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "sshd_config.liquidity-migration-backup" in text
    assert "Restored authorized key fingerprints:" in text
    assert 'ssh-keygen -lf "$tmp_public_key" -E sha256' in text
    assert "effective_sshd_config" in text
    assert "grep -Eq '^authenticationmethods publickey$'" in text
    assert "mkdir -p /run/sshd" in text
    assert 'sshd_root_context="user=root,host=localhost,addr=127.0.0.1"' in text
    assert 'sshd -T -C "$sshd_root_context"' in text
    assert "systemctl restart ssh.service" in text
    assert "liquidity-migration-deploy-backups" in text
    assert "non-git-checkout-" in text
    assert 'mv "$REPO_DIR" "$backup_path"' in text
    assert "git reset --hard" in text
    assert "git clean -fd" in text
    assert "git ls-files --others --exclude-standard -z" in text
    assert 'tar --null -czf "$untracked_archive" --files-from "$untracked_nul"' in text
    assert "git clone" in text
    assert "git remote set-url" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    assert "pip install -e \".[dev]\"" in text
    assert "liqmig_union_q40_h3_tp26_g100_qsqueeze" in text
    assert "demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6" in text
    assert "systemctl disable --now" in text
    assert "model050426.service" in text
    assert "model050426-bybit-demo-signal.timer" in text
    assert "liquidity-migration-bybit-demo.service" in text
    assert "liquidity-migration-bybit-risk.service" in text
    assert "retired unit" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-demo.service" in text
    assert "Environment=STRATEGY_PROFILE=demo_relaxed" in text
    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=UNIVERSE_RANK_END=300" in text
    assert "Environment=UNIVERSE_MAX_SYMBOLS=300" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment" not in text
