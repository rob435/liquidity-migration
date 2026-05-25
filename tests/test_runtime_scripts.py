from __future__ import annotations

import time
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
    assert '--max-active-symbols "$MAX_ACTIVE_SYMBOLS"' in text


def test_systemd_entry_runner_uses_vps_cadence() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-demo.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=STRATEGY_PROFILE=promoted" in text
    assert "Environment=UNIVERSE_RANK_END=400" in text
    assert "Environment=UNIVERSE_MAX_SYMBOLS=400" in text
    assert "Environment=UNIVERSE_MIN_TURNOVER_24H=0" in text
    assert "Environment=MAX_ACTIVE_SYMBOLS=3" in text
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in text


def test_event_entry_runner_only_submits_promoted_profile() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_demo_event_engine.sh").read_text(encoding="utf-8")

    assert 'SUBMIT_ORDERS:-0}" == "1"' in text
    assert '$STRATEGY_PROFILE" != "promoted"' in text


def test_event_entry_runner_wires_record_dry_run() -> None:
    """Both short + long bash runners must surface --record-dry-run via the
    RECORD_DRY_RUN env var so paper services can persist their planned
    orders/trades for reconciliation against demo. Found 2026-05-24: paper
    services were firing entries=1/1 every cycle but writing nothing to disk."""
    repo = Path(__file__).resolve().parents[1]
    for script_name in ("run_bybit_demo_event_engine.sh", "run_bybit_long_demo_event_engine.sh"):
        text = (repo / "scripts" / script_name).read_text(encoding="utf-8")
        assert 'RECORD_DRY_RUN:-0}" == "1"' in text, f"{script_name} missing RECORD_DRY_RUN gate"
        assert "--record-dry-run" in text, f"{script_name} does not pass --record-dry-run"


def test_paper_services_enable_record_dry_run() -> None:
    """Paper services must set RECORD_DRY_RUN=1 so their dry-run cycles
    persist trades — otherwise paper-vs-demo reconciliation has no paper-side
    data to pair against the live demo ledger."""
    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-paper.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=SUBMIT_ORDERS=0" in text, f"{unit}: paper service must not submit orders"
        assert "Environment=RECORD_DRY_RUN=1" in text, f"{unit}: paper service must enable RECORD_DRY_RUN"


def test_demo_services_use_unblocked_entry_lag() -> None:
    """Live audit on 2026-05-24 found 15min lag rejected every signal as stale
    (feature pipeline builds 3-4h after bar close). Both demo + paper use 360min
    (6h) — enough for the natural feature-build cadence (~218min) plus buffer,
    while still skipping signals stale enough to have lost their entry alpha
    (the backtest assumes T+1h fills; >6h late degrades the edge meaningfully)."""
    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-demo.service",
        "liquidity-migration-bybit-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=MAX_ENTRY_LAG_MINUTES=360" in text, f"{unit}: MAX_ENTRY_LAG_MINUTES regression"


def test_services_enable_ws_klines() -> None:
    """All four demo services (short+long, demo+paper) must enable the WS
    kline manager. WS_KLINES_ENABLED=1 flips the daemon onto the in-memory
    store, eliminating the per-cycle REST kline burst that caused 3-4h late
    entries on the legacy path."""
    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-demo.service",
        "liquidity-migration-bybit-paper.service",
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=WS_KLINES_ENABLED=1" in text, f"{unit}: WS_KLINES_ENABLED not set"


def test_bash_runners_wire_ws_klines_env() -> None:
    """Both short + long bash runners must expose the WS_KLINES_* env vars
    as CLI args. Without this, the systemd Environment lines are silently
    dropped and the daemon stays on the legacy REST path."""
    repo = Path(__file__).resolve().parents[1]
    for script_name in (
        "run_bybit_demo_event_engine.sh",
        "run_bybit_long_demo_event_engine.sh",
    ):
        text = (repo / "scripts" / script_name).read_text(encoding="utf-8")
        # Env vars are read with defaults.
        assert 'WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"' in text, f"{script_name}: missing WS_KLINES_ENABLED default"
        assert "WS_KLINES_BOOTSTRAP_WORKERS" in text, f"{script_name}: missing WS_KLINES_BOOTSTRAP_WORKERS"
        assert "WS_KLINES_LOOKBACK_DAYS" in text, f"{script_name}: missing WS_KLINES_LOOKBACK_DAYS"
        assert "WS_KLINES_UNIVERSE_REFRESH_SECONDS" in text, f"{script_name}: missing WS_KLINES_UNIVERSE_REFRESH_SECONDS"
        assert "WS_KLINES_TOPICS_PER_CONNECTION" in text, f"{script_name}: missing WS_KLINES_TOPICS_PER_CONNECTION"
        assert "WS_KLINES_STALE_WARNING_SECONDS" in text, f"{script_name}: missing WS_KLINES_STALE_WARNING_SECONDS"
        # And they're passed through the CLI.
        assert "--ws-klines-enabled" in text, f"{script_name}: missing --ws-klines-enabled"
        assert "--no-ws-klines" in text, f"{script_name}: missing --no-ws-klines kill-switch"
        assert "--ws-klines-bootstrap-workers" in text
        assert "--ws-klines-lookback-days" in text
        assert "--ws-klines-universe-refresh-seconds" in text
        assert "--ws-klines-topics-per-connection" in text
        assert "--ws-klines-stale-warning-seconds" in text
        assert "--ws-klines-stale-reconnect-seconds" in text


def test_demo_health_watchdog_units_present() -> None:
    """The hourly entry-health watchdog timer + service must ship together so
    'no entries in 24h' regressions don't go silent. Validates wire-up of the
    check_demo_entry_health.py script behind a systemd timer + Telegram alert."""
    repo = Path(__file__).resolve().parents[1]
    service = (repo / "deploy" / "systemd" / "liquidity-migration-demo-health.service").read_text(encoding="utf-8")
    timer = (repo / "deploy" / "systemd" / "liquidity-migration-demo-health.timer").read_text(encoding="utf-8")
    script = (repo / "scripts" / "check_demo_entry_health.py").read_text(encoding="utf-8")

    assert "check_demo_entry_health.py" in service
    assert "--telegram" in service
    assert "SuccessExitStatus=0 1" in service, "alert exit code 1 must not register as failure"
    assert "OnCalendar=" in timer
    assert "--window-hours" in script and "--telegram" in script


def _write_demo_cycle_parquet(
    root: Path,
    *,
    cycles: list[dict],
    date: str = "2026-05-24",
) -> None:
    """Helper for the watchdog tests: write cycles to the date-partitioned
    layout the script reads (data/{root}/event_demo_cycles/date=YYYY-MM-DD/*.parquet)."""
    import polars as pl  # local: keep top-level imports lean

    cycles_dir = root / "event_demo_cycles" / f"date={date}"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(cycles).write_parquet(cycles_dir / "cycles.parquet")


def test_health_watchdog_alerts_on_universe_coverage_gap(tmp_path: Path) -> None:
    """Even with entries > 0, a non-zero coverage_gap means the universe
    doesn't reach the rank ceiling and the strategy is effectively
    signal-starved. The Composer audit caught this: 1 entry/24h was
    reported "healthy" while observed_prior7_rank_max=150 < required=300
    blocked the entire signal pipeline.

    The watchdog reads coverage_gap from the LATEST cycle (gap is a
    point-in-time bootstrap state, not an accumulator) so a 24h window
    with one early entry must still alert when the current cycle shows
    the gap is back."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from check_demo_entry_health import check_entries

    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    ts_ms = int(time.time() * 1000) - 60_000
    # Ascending ts_ms — first cycle had a working strategy (entry+no gap),
    # latest cycle shows the coverage gap reopened.
    cycles = [
        {
            "mode": "submit", "ts_ms": ts_ms - (1499 - i) * 60_000,
            "entries_executed": 1 if i == 0 else 0,
            "entry_candidates": 1 if i == 0 else 0,
            "skipped_stale": 50,
            "universe_coverage": {"coverage_gap": 0 if i < 1400 else 150},
        }
        for i in range(1500)  # ~25h of cycles
    ]
    _write_demo_cycle_parquet(tmp_path, cycles=cycles, date=today)

    code, msg = check_entries(data_root=tmp_path, window_hours=24)
    assert code == 1
    assert "coverage gap=150" in msg


def test_health_watchdog_alerts_on_cycle_starvation(tmp_path: Path) -> None:
    """A daemon that's stuck in bootstrap or OOM-looping emits far fewer
    cycles than the expected 60/h cadence. Composer's pre-fix snapshot
    showed 19 minutes between cycles — the watchdog should catch this
    rather than waiting 24h for the entries==0 alert to fire."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from check_demo_entry_health import check_entries

    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    ts_ms = int(time.time() * 1000) - 60_000
    # Only 5 cycles in 24h — well below the 0.8/hr * 24h = ~19 expected.
    cycles = [
        {
            "mode": "submit", "ts_ms": ts_ms - i * 3_600_000,
            "entries_executed": 0, "entry_candidates": 0, "skipped_stale": 0,
            "universe_coverage": {"coverage_gap": 0},
        }
        for i in range(5)
    ]
    _write_demo_cycle_parquet(tmp_path, cycles=cycles, date=today)

    code, msg = check_entries(data_root=tmp_path, window_hours=24)
    assert code == 1
    assert "cycles in last 24h" in msg


def test_health_watchdog_passes_when_coverage_ok_and_entries_present(tmp_path: Path) -> None:
    """Sanity: the happy path still returns 0 when coverage gap is 0,
    cycle cadence is healthy, and at least one entry fired."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from check_demo_entry_health import check_entries

    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    ts_ms = int(time.time() * 1000) - 60_000
    cycles = [
        {
            "mode": "submit", "ts_ms": ts_ms - i * 60_000,
            "entries_executed": 2 if i == 0 else 0,
            "entry_candidates": 2 if i == 0 else 0,
            "skipped_stale": 0,
            "universe_coverage": {"coverage_gap": 0},
        }
        for i in range(60)  # 60 cycles in last hour
    ]
    _write_demo_cycle_parquet(tmp_path, cycles=cycles, date=today)

    code, msg = check_entries(data_root=tmp_path, window_hours=1)
    assert code == 0
    assert "healthy" in msg


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
    # Timers ship with the unit files but `systemctl enable` is required to
    # actually schedule them. Pin both timers so a deploy can't silently leave
    # the demo-health watchdog or daily combined-book report inactive.
    assert "systemctl enable --now liquidity-migration-demo-health.timer" in text
    assert "systemctl enable --now liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-demo-health.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-health.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-combined-book-report.timer" in text
    assert "Environment=STRATEGY_PROFILE=promoted" in text
    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=UNIVERSE_RANK_END=400" in text
    assert "Environment=UNIVERSE_MAX_SYMBOLS=400" in text
    assert "Environment=UNIVERSE_MIN_TURNOVER_24H=0" in text
    assert "Environment=MAX_ACTIVE_SYMBOLS=3" in text
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
    assert "Environment=STRATEGY_PROFILE=promoted" in text
    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=UNIVERSE_RANK_END=400" in text
    assert "Environment=UNIVERSE_MAX_SYMBOLS=400" in text
    assert "Environment=UNIVERSE_MIN_TURNOVER_24H=0" in text
    assert "Environment=MAX_ACTIVE_SYMBOLS=3" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    # Read-only verify must catch a missing-timer regression that the deploy
    # script would have caused — parity check, no-write semantics.
    assert "systemctl is-enabled --quiet liquidity-migration-demo-health.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-health.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-combined-book-report.timer" in text
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
    # Pin the CI deploy key fingerprint so accidental rotations or tampering
    # of the workflow file get flagged. When you intentionally rotate the
    # deploy key, update this constant in lockstep with the
    # GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT line in
    # .github/workflows/vps-deploy.yml AND the public key in
    # /root/.ssh/authorized_keys on the VPS AND the VPS_SSH_PRIVATE_KEY
    # secret in GitHub.
    assert "SHA256:KpDkvlvmK93qXC9Ocvb9n4Zsk8Gn/pzDzdvAR0XHkgo" in text
    assert "ssh-keygen -y -f ~/.ssh/vps_deploy_key" in text
    assert "ssh-keygen -lf ~/.ssh/vps_deploy_key.pub -E sha256" in text
    assert "ssh-keyscan -T 10 -t ed25519" in text
    assert "SHA256:zQjT3bst/N43fyt5L4vRKmNDuwtxVuaPiHVINBO2elU" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    # demo-health.service launches check_demo_entry_health.py; pin it so
    # script-only changes to the health check still trigger the deploy.
    assert "scripts/check_demo_entry_health.py" in text
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
    assert "raw.githubusercontent.com/rob435/liquidity-migration" in text
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
    assert "Environment=STRATEGY_PROFILE=promoted" in text
    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=UNIVERSE_RANK_END=400" in text
    assert "Environment=UNIVERSE_MAX_SYMBOLS=400" in text
    assert "Environment=UNIVERSE_MIN_TURNOVER_24H=0" in text
    assert "Environment=MAX_ACTIVE_SYMBOLS=3" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment" not in text
