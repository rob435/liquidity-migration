from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

from liquidity_migration.operational_runtime_authority import AUTHORIZED_UNITS


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
WRAPPER = ROOT / "scripts" / "run_authorized_runtime.sh"
SYSTEMD = ROOT / "deploy" / "systemd"
RECEIPT = "/etc/liquidity-migration/account-execution-operational-ready"


def _read(path: str | Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.read_text(encoding="utf-8")


def _unit(name: str) -> str:
    return _read(SYSTEMD / name)


def _environment(name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _unit(name).splitlines():
        if not line.startswith("Environment="):
            continue
        key, separator, value = line.removeprefix("Environment=").partition("=")
        assert separator
        values[key] = value
    return values


def test_deployed_shell_scripts_parse_and_are_executable() -> None:
    scripts = [
        DEPLOY,
        WRAPPER,
        ROOT / "scripts" / "run_account_execution_service.sh",
        ROOT / "scripts" / "run_account_paper_execution_service.sh",
        ROOT / "scripts" / "run_bybit_long_demo_event_engine.sh",
        ROOT / "scripts" / "run_bybit_continuous_demo_event_engine.sh",
        ROOT / "scripts" / "run_continuous_hedge.sh",
        ROOT / "scripts" / "run_continuous_rmom_refresh.sh",
        ROOT / "scripts" / "reset_demo_paper_ledgers.sh",
    ]
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)
    for path in scripts[:6] + scripts[8:]:
        assert path.stat().st_mode & stat.S_IXUSR
    assert (ROOT / "scripts" / "check_deploy_rollout_readiness.py").stat().st_mode & stat.S_IXUSR
    paper_runner = _read("scripts/run_account_paper_execution_service.sh")
    assert "CALIBRATION" not in paper_runner
    assert "--latency-quantile" not in paper_runner
    assert "--slippage-quantile" not in paper_runner
    assert "/etc/liquidity-migration/account-execution/demo-rules.json" not in paper_runner
    assert "/etc/liquidity-migration/account-paper-execution/demo-rules.json" in paper_runner


def test_authorized_wrapper_owns_every_runtime_argv_and_verifies_before_exec() -> None:
    wrapper = _read(WRAPPER)
    assert 'if [ "$#" -ne 2 ]' in wrapper
    assert wrapper.index("operational_runtime_authority verify-runtime") < wrapper.index('exec "${COMMAND[@]}"')
    for unit in AUTHORIZED_UNITS:
        assert f"{unit}:main" in wrapper
        fragment = _unit(unit)
        assert f"ConditionPathExists={RECEIPT}" in fragment
        assert f"run_authorized_runtime.sh {unit} main" in fragment
    for owner in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
    ):
        assert f"{owner}:readiness" in wrapper
        assert f"run_authorized_runtime.sh {owner} readiness" in _unit(owner)
    assert "--account-paper-environment-file /etc/liquidity-migration/account-paper-execution.env" in wrapper


def test_only_demo_owner_inherits_demo_credentials() -> None:
    demo_owner = _unit("liquidity-migration-account-execution.service")
    assert "EnvironmentFile=/etc/liquidity-migration/bybit-demo.env" in demo_owner
    assert "BYBIT_DEMO_API_KEY" not in next(
        line for line in demo_owner.splitlines() if line.startswith("UnsetEnvironment=")
    )
    for unit in AUTHORIZED_UNITS:
        if unit == "liquidity-migration-account-execution.service":
            continue
        fragment = _unit(unit)
        unset = " ".join(line for line in fragment.splitlines() if line.startswith("UnsetEnvironment="))
        assert "BYBIT_DEMO_API_KEY" in unset
        assert "BYBIT_DEMO_API_SECRET" in unset
    for unit in AUTHORIZED_UNITS:
        fragment = _unit(unit)
        assert "BYBIT_REAL_API_KEY" in fragment
        assert "BYBIT_REAL_API_SECRET" in fragment
    for unit in (
        "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-paper.service",
    ):
        fragment = _unit(unit)
        assert "EnvironmentFile=/etc/liquidity-migration/bybit-demo.env" not in fragment
        assert "Environment=REAL_MONEY=false" in fragment
        assert "integration-only uncalibrated" in fragment
        read_only = next(
            line for line in fragment.splitlines() if line.startswith("ReadOnlyPaths=")
        )
        assert read_only == "ReadOnlyPaths=/opt/liquidity-migration"


def test_persistent_demo_and_paper_workers_have_small_box_memory_limits() -> None:
    expected = {
        "liquidity-migration-account-execution.service": ("384M", "512M", "256M"),
        "liquidity-migration-account-paper-execution.service": ("256M", "384M", "256M"),
        "liquidity-migration-bybit-continuous-demo.service": ("768M", "896M", "384M"),
        "liquidity-migration-bybit-long-demo.service": ("576M", "640M", "384M"),
        "liquidity-migration-bybit-continuous-paper.service": ("640M", "768M", "384M"),
        "liquidity-migration-bybit-long-paper.service": ("640M", "768M", "384M"),
    }
    for unit, (high, maximum, swap) in expected.items():
        fragment = _unit(unit)
        assert f"MemoryHigh={high}" in fragment
        assert f"MemoryMax={maximum}" in fragment
        assert f"MemorySwapMax={swap}" in fragment


def test_liveness_timer_has_one_bounded_activation_grace() -> None:
    timer = _unit("liquidity-migration-demo-liveness.timer")

    assert "OnActiveSec=10min" in timer
    assert "OnUnitActiveSec=3min" in timer
    assert "OnBootSec=" not in timer


def test_producers_require_owner_readiness_and_never_hold_private_order_authority() -> None:
    pairs = {
        "liquidity-migration-bybit-long-demo.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-bybit-continuous-demo.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-continuous-hedge.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-bybit-long-paper.service": "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-continuous-paper.service": "liquidity-migration-account-paper-execution.service",
    }
    for producer, owner in pairs.items():
        fragment = _unit(producer)
        assert f"Requires={owner}" in fragment
        assert owner in next(line for line in fragment.splitlines() if line.startswith("After="))
    for runner in (
        "scripts/run_bybit_long_demo_event_engine.sh",
        "scripts/run_bybit_continuous_demo_event_engine.sh",
        "scripts/run_continuous_hedge.sh",
    ):
        text = _read(runner)
        assert "BYBIT_DEMO_API_SECRET" not in text
        assert "place_order" not in text


def test_liveness_observer_never_activates_or_orders_after_monitored_owner() -> None:
    fragment = _unit("liquidity-migration-demo-liveness.service")
    owner = "liquidity-migration-account-execution.service"
    lifecycle_directives = {
        "After",
        "Before",
        "BindsTo",
        "PartOf",
        "Requisite",
        "Requires",
        "Upholds",
        "Wants",
    }
    for line in fragment.splitlines():
        directive, separator, _value = line.partition("=")
        if separator and directive in lifecycle_directives:
            assert owner not in line

    assert "Wants=network-online.target" in fragment
    assert "After=network-online.target" in fragment


def test_demo_and_paper_strategy_units_use_one_validated_operational_profile() -> None:
    long_demo = _environment("liquidity-migration-bybit-long-demo.service")
    long_paper = _environment("liquidity-migration-bybit-long-paper.service")
    sizing_keys = (
        "NOTIONAL_MULTIPLIER",
        "ENTRY_LEVERAGE",
        "MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY",
        "MAX_ORDER_NOTIONAL_PCT_EQUITY",
        "MAX_NEW_ENTRIES_PER_CYCLE",
        "MAX_ACTIVE",
        "BTC_TREND_GATE",
        "PER_POSITION_NOTIONAL_PCT_EQUITY",
    )
    continuous_demo = _environment("liquidity-migration-bybit-continuous-demo.service")
    continuous_paper = _environment("liquidity-migration-bybit-continuous-paper.service")
    for environment in (long_demo, long_paper, continuous_demo, continuous_paper):
        assert set(environment).isdisjoint(sizing_keys)
    for key in (
        "LOOKBACK_DAYS",
        "WORKERS",
    ):
        assert continuous_demo[key] == continuous_paper[key]
    for key in ("LOOKBACK_DAYS", "WORKERS", "WS_KLINES_ENABLED", "WS_KLINES_BOOTSTRAP_WORKERS"):
        assert long_demo[key] == long_paper[key]
    long_runner = _read("scripts/run_bybit_long_demo_event_engine.sh")
    continuous_runner = _read("scripts/run_bybit_continuous_demo_event_engine.sh")
    hedge_runner = _read("scripts/run_continuous_hedge.sh")
    for runner in (long_runner, continuous_runner, hedge_runner):
        assert 'ACCOUNT_RISK_POLICY_FILE' in runner
        assert '--operational-profile-file' in runner
    assert long_demo["EXECUTION_ENVIRONMENT"] == "demo"
    assert long_paper["EXECUTION_ENVIRONMENT"] == "paper"
    assert continuous_demo["EXECUTION_ENVIRONMENT"] == "demo"
    assert continuous_paper["EXECUTION_ENVIRONMENT"] == "paper"


def test_demo_account_notification_reads_the_explicit_continuous_status_root() -> None:
    demo_owner = _environment("liquidity-migration-account-execution.service")
    assert demo_owner["CONTINUOUS_CYCLE_ROOT"] == (
        "/opt/liquidity-migration/data/bybit-continuous-demo-event"
    )
    assert demo_owner["CONTINUOUS_CYCLE_MAX_AGE_MINUTES"] == "15"


def test_paper_producers_follow_demo_kline_planes_without_crossing_write_roots() -> None:
    long_demo = _environment("liquidity-migration-bybit-long-demo.service")
    long_paper = _environment("liquidity-migration-bybit-long-paper.service")
    assert long_paper["DATA_ROOT"] != long_demo["DATA_ROOT"]
    assert long_paper["KLINES_FOLLOW_ROOT"] == long_demo["DATA_ROOT"]
    assert long_paper["USE_DAEMON"] == "1"

    continuous_demo = _environment("liquidity-migration-bybit-continuous-demo.service")
    continuous_paper = _environment("liquidity-migration-bybit-continuous-paper.service")
    assert continuous_paper["DATA_ROOT"] != continuous_demo["DATA_ROOT"]
    assert continuous_paper["KLINES_FOLLOW_ROOT"] == continuous_demo["DATA_ROOT"]

    rmom = _read("scripts/run_continuous_rmom_refresh.sh")
    assert "CONTINUOUS_PAPER_DATA_ROOT" not in rmom
    assert rmom.count("precompute_residual_momentum.py") == 1


def test_paper_producer_sandboxes_include_the_authorized_shared_capture_root() -> None:
    common = {
        "/opt/liquidity-migration/data/bybit-account-paper",
        "/opt/liquidity-migration/data/bybit-account-paper-intents",
        "/opt/liquidity-migration/data/bybit-account-paper-market-capture",
    }
    strategy_roots = {
        "liquidity-migration-bybit-long-paper.service": (
            "/opt/liquidity-migration/data/bybit-long-paper-event"
        ),
        "liquidity-migration-bybit-continuous-paper.service": (
            "/opt/liquidity-migration/data/bybit-continuous-paper-event"
        ),
    }
    for unit, strategy_root in strategy_roots.items():
        fragment = _unit(unit)
        directive = next(
            line for line in fragment.splitlines() if line.startswith("ReadWritePaths=")
        )
        assert set(directive.removeprefix("ReadWritePaths=").split()) == {
            *common,
            strategy_root,
        }


def test_install_is_stopped_exact_commit_preparation_only() -> None:
    text = _read(DEPLOY)
    install = text[text.index("install_mode()") : text.index("load_authorization()")]
    assert install.index("require_quiescent") < install.index("git_fetch fetch")
    assert install.index("git_fetch fetch") < install.index("git checkout -B")
    assert "requirements.lock" in install
    assert "--no-deps" in install and "--only-binary=:all:" in install
    assert "tests/test_forward_epoch_start.py" in install
    assert "lm_install_current_systemd_units" in install
    assert "systemctl disable --now" in install
    assert "systemctl start" not in install
    assert "systemctl enable --now" not in install
    assert "lm_write_resolved_sleeve_toggles" in install
    assert "invalidate_operational_authorization" in install
    assert "units_started=0" in install


def test_install_provisions_a_credential_fenced_paper_runtime_boundary() -> None:
    text = _read(DEPLOY)
    boundary = text[
        text.index("ensure_paper_runtime_identity()") : text.index("require_checkout()")
    ]
    assert "useradd --system" in boundary
    assert "--shell /usr/sbin/nologin" in boundary
    assert "PAPER_RUNTIME_USER=liquidity-migration-paper" in text
    assert "PAPER_ENVIRONMENT" in boundary
    assert "allowed_tuning" in boundary
    assert "CANDIDATE_UNIVERSE_FILE" in boundary
    assert "BYBIT_DEMO_API_KEY" in boundary
    assert "reset_path_safety preflight-paper" in boundary
    assert "reset_path_safety preflight-demo" in boundary
    assert "reset_path_safety normalize-paper" in boundary
    assert "reset_path_safety normalize-demo" in boundary
    assert boundary.count("--create-missing") >= 2
    assert "chown -R" not in boundary
    assert 'test -w "$root/.locks"' in boundary
    assert "--continuous-root" in boundary
    assert "find \"$root\" -type f -exec chmod 0640" not in boundary
    assert "test ! -r \"$path\"" in boundary
    assert "test ! -w \"$root\"" in boundary
    assert "lm_load_group_systemd_environment" in text


def test_install_binds_one_shared_strategy_target_tape_per_environment() -> None:
    text = _read(DEPLOY)
    boundary = text[
        text.index("prepare_paper_runtime_boundary()") : text.index("require_checkout()")
    ]

    assert '"$demo_capture/strategy-targets.jsonl"' in boundary
    assert 'values["STRATEGY_TARGET_CAPTURE_PATH"] = str(target_capture)' in boundary
    assert (
        '"STRATEGY_TARGET_CAPTURE_PATH": str(Path(sys.argv[4]) / "strategy-targets.jsonl")'
        in boundary
    )


def test_activation_verifies_bound_state_before_start_and_cannot_reconfigure_it() -> None:
    text = _read(DEPLOY)
    activate = text[text.index("activate_mode()") : text.index('case "$MODE" in', text.index("activate_mode()"))]
    assert activate.index("load_authorization") < activate.index("systemctl start")
    assert activate.index("validate_hedge_model_prior") < activate.index("systemctl start")
    assert activate.index("account-execution.service") < activate.index("bybit-long-demo.service")
    for forbidden in ("git fetch", "git checkout", "pip install", "lm_write_resolved", "sed -i"):
        assert forbidden not in activate
    assert 'if [ "$AUTH_PROFILE" = operational ]' in activate
    assert "liquidity-migration-account-paper-execution.service" in activate


def test_guarded_rollout_proves_flatness_around_ordered_shutdown_and_binds_new_authority() -> None:
    text = _read(DEPLOY)
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert rollout.index("rollout-target-prefetch") < rollout.index("current-topology-verification")
    assert rollout.index("current-topology-verification") < rollout.index("pre-stop-flat-account-proof")
    assert rollout.index("pre-stop-flat-account-proof") < rollout.index("stop-downstream-units")
    assert rollout.index("stop-downstream-units") < rollout.index("post-producer-flat-account-proof")
    assert rollout.index("post-producer-flat-account-proof") < rollout.index("stop-account-owners")
    assert rollout.index("stop-account-owners") < rollout.index("final-stopped-flat-account-proof")
    assert rollout.index("final-stopped-flat-account-proof") < rollout.index("ROLLOUT_IRREVERSIBLE=1")
    assert rollout.index("stopped-install") < rollout.index("create-operational-authority")
    assert rollout.index("create-operational-authority") < rollout.index("activate-and-verify")

    readiness = _read("scripts/check_deploy_rollout_readiness.py")
    assert "read_account_journal(root, verify=True)" in readiness
    assert "canonical aggregate targets are non-flat" in readiness
    assert "canonical working orders remain" in readiness
    assert "require_recent_account_owner_health" in readiness
    assert "head_binding=head_binding" in readiness
    assert "BybitPrivateClient(" in readiness
    assert "demo=True" in readiness
    assert 'client.get_positions(settle_coin="USDT")' in readiness
    assert 'order_filter="StopOrder"' in readiness
    rollout_check = text[text.index("rollout_flat_check()") : text.index("verify_topology()")]
    assert "BYBIT_REAL_API_KEY" in rollout_check
    assert "rollout_readiness_helper" in rollout_check
    assert 'ROLLOUT_READINESS_HELPER_B64' in text
    assert '"$EXPECTED_COMMIT:scripts/check_deploy_rollout_readiness.py"' in text

    authority = text[
        text.index("issue_rollout_authorization()") : text.index("rollout_mode()")
    ]
    assert "LIQUIDITY_MIGRATION_MAINTENANCE_LOCK_FDS=9,8,7" in authority
    assert "operational_runtime_authority issue" in authority
    assert '--profile "$DEPLOY_PROFILE"' in authority
    assert '--authorization-reference "$DEPLOY_AUTHORIZATION_REFERENCE"' in authority
    assert '--owner-acknowledgement "$DEPLOY_OWNER_ACKNOWLEDGEMENT"' in authority


def test_deploy_has_bounded_activation_waits_and_visible_expensive_phases() -> None:
    text = _read(DEPLOY)
    assert 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS="${RMOM_BOOTSTRAP_TIMEOUT_SECONDS:-300}"' in text
    assert 'RMOM_BOOTSTRAP_RETRY_SECONDS="${RMOM_BOOTSTRAP_RETRY_SECONDS:-10}"' in text
    assert "phase-start name=%s" in text
    assert "phase-ok name=%s elapsed_seconds=%s" in text
    for phase in (
        "install-locked-dependencies",
        "focused-runtime-tests",
        "paper-tree-preflight",
        "paper-tree-normalize",
        "seed-residual-momentum",
    ):
        assert phase in text


def test_deploy_permission_probe_uses_only_bound_demo_credentials() -> None:
    text = _read(DEPLOY)
    probe = text[text.index("check_demo_order_permissions()") : text.index("verify_topology()")]
    assert "/etc/liquidity-migration/bybit-demo.env" in probe
    assert "lm_load_private_systemd_environment" in probe
    assert "BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET REAL_MONEY" in probe
    assert probe.count("unset BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET") == 2
    assert "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET" in probe
    assert "check_demo_order_permissions deploy" in text
    assert "check_demo_order_permissions verify" in text


def test_verify_mode_is_read_only_and_checks_profile_topology() -> None:
    text = _read(DEPLOY)
    dispatch = text[text.rindex('case "$MODE" in') :]
    assert "verify) load_authorization; verify_topology" in dispatch
    verify = text[text.index("verify_topology()") : text.index("start_if()")]
    assert "systemctl start" not in verify
    assert "systemctl enable" not in verify
    assert "systemctl disable" not in verify
    assert "demo-operational" in text
    assert "paper owner is active under demo-only authorization" in verify


def test_systemd_installer_is_manifest_exact_and_never_starts_current_units() -> None:
    lib = _read("deploy/lib_sleeves.sh")
    install = lib[lib.index("lm_install_current_systemd_units()") :]
    assert "lm_cleanup_unknown_liqmig_units" in install
    assert "lm_verify_no_unknown_liqmig_units" in install
    assert "lm_verify_guarded_unit_surfaces" in install
    assert "systemctl start" not in install
    assert re.search(r"^\s*systemctl enable", install, re.MULTILINE) is None
    assert "cp " in install


def test_resolved_sleeves_are_atomically_generated_then_group_bound() -> None:
    lib = _read("deploy/lib_sleeves.sh")
    writer = lib[lib.index("lm_write_resolved_sleeve_toggles()") : lib.index("lm_verify_resolved_sleeve_toggles()")]
    assert "chmod 0600" in writer
    assert "CONTINUOUS_HEDGE_TIMER" in writer
    deploy = _read(DEPLOY)
    assert 'chown root:"$PAPER_RUNTIME_GROUP" /etc/liquidity-migration/sleeves.resolved.env' in deploy
    assert "chmod 0640 /etc/liquidity-migration/sleeves.resolved.env" in deploy
    rmom = _read("scripts/run_continuous_rmom_refresh.sh")
    assert "lm_load_sleeve_toggles" not in rmom
    assert "CONTINUOUS_SLEEVE is required" in rmom
    rmom_unit = _unit("liquidity-migration-continuous-rmom-refresh.service")
    assert "EnvironmentFile=/etc/liquidity-migration/sleeves.resolved.env" in rmom_unit


def test_workflow_runs_ci_on_push_and_only_manual_staged_vps_modes() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert "pull_request:" in workflow and "push:" in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m ruff check" in workflow
    assert "--only-binary=:all: -r requirements.lock" in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    assert "options: [install, activate, verify]" in workflow
    assert 'scripts/deploy_vps_live.sh "${{ inputs.mode }}"' in workflow


def test_workflow_serializes_vps_operations_across_refs() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    vps_job = workflow[workflow.index("  vps:") :]
    assert re.search(
        r"(?m)^    concurrency:\n"
        r"      group: liquidity-migration-vps\n"
        r"      cancel-in-progress: false$",
        vps_job,
    )


def test_remote_deploy_entrypoint_serializes_every_mode() -> None:
    deploy = _read(DEPLOY)
    lock = deploy[
        deploy.index("acquire_maintenance_locks()") : deploy.rindex('case "$MODE" in')
    ]
    assert "maintenance_lock_helper prepare-host" in lock
    assert "maintenance_lock_helper acquire-inherited" in lock
    assert 'exec 9<"$lock_dir/maintenance.lock"' in lock
    assert 'exec 8<"$lock_dir/deploy.lock"' in lock
    assert 'exec 7</run/lock/liquidity-migration-ledger-reset.lock' in lock
    assert 'exec 9>"' not in lock
    assert deploy.index(
        "acquire_maintenance_locks\n",
        deploy.index("acquire_maintenance_locks()"),
    ) < deploy.rindex('case "$MODE" in')
    transmission = deploy[: deploy.index("read -r -a SSH_ARGS")]
    assert '"$EXPECTED_COMMIT:liquidity_migration/maintenance_lock.py"' in transmission
    assert '/usr/bin/git --no-pager --git-dir="$LOCAL_REPOSITORY/.git"' in transmission
    assert 'cat-file -t "$EXPECTED_COMMIT"' in transmission
    assert "GIT_NO_REPLACE_OBJECTS=1" in transmission
    assert "EXPECTED_COMMIT is not a local commit object" in transmission
    assert "MAINTENANCE_LOCK_HELPER_B64" in transmission
    assert "../liquidity_migration/maintenance_lock.py" not in transmission
    remote = deploy[deploy.index("set -euo pipefail", deploy.index("cat <<'REMOTE_SCRIPT'")) :]
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexport PATH" in remote[:200]
    assert "GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1" in remote
    assert "HOME=/nonexistent" in remote
    assert '--git-dir="$REPO_DIR/.git" --work-tree="$REPO_DIR"' in remote
    assert "-c core.fsmonitor=false -c core.filemode=true -c core.hooksPath=/dev/null" in remote
    clean = remote[remote.index("clean_checkout_status()") : remote.index("require_quiescent()")]
    for command in (
        'read-tree "$expected_commit"',
        "update-index --refresh",
        'diff-index --quiet "$expected_commit" --',
        "ls-files --others --exclude-standard",
    ):
        assert command in clean
    assert 'GIT_INDEX_FILE="$index_path"' in remote
    assert "/run/liquidity-migration/deploy-index.XXXXXX" in clean
    assert clean.count('/bin/rm -f -- "$temporary_index"') >= 4
    assert clean.count('safe_git rev-parse HEAD') == 2
    install = remote[remote.index("install_mode()") : remote.index("activate_mode()")]
    assert 'require_clean_checkout_at "$installed_head" "install"' in install
    assert "safe_git checkout" in install
    assert "safe_git merge-base" in install
    assert "\n    git checkout" not in install


def test_dependency_contract_has_one_source_and_exact_runtime_pins() -> None:
    assert not (ROOT / "requirements.txt").exists()
    lock = _read("requirements.lock")
    pins = [line for line in lock.splitlines() if line and not line.startswith("#")]
    assert pins
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", line) for line in pins)
    assert len({line.split("==", 1)[0].lower() for line in pins}) == len(pins)


def test_recovery_generator_is_exact_commit_and_non_destructive() -> None:
    text = _read("scripts/print_vps_recovery_command.sh")
    assert 'git show "$commit:$1"' in text
    assert "vps_restore_ssh_access.sh" in text
    assert "vps_rescue_restore_ssh_access.sh" in text
    assert "deploy_vps_live.sh install" in text
    assert "deploy_vps_live.sh activate" in text
    assert "reset --hard" not in text
    assert "git clean" not in text
    assert "GITHUB_TOKEN" not in text
