from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path


AUTHORIZED_UNITS = (
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-bybit-carry-demo.service",
    "liquidity-migration-bybit-carry-paper.service",
    "liquidity-migration-continuous-hedge.service",
    "liquidity-migration-continuous-rmom-refresh.service",
    "liquidity-migration-demo-liveness.service",
)


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
WRAPPER = ROOT / "scripts" / "run_authorized_runtime.sh"
SYSTEMD = ROOT / "deploy" / "systemd"


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
        ROOT / "scripts" / "run_bybit_carry_demo_event_engine.sh",
        ROOT / "scripts" / "run_continuous_hedge.sh",
        ROOT / "scripts" / "run_continuous_rmom_refresh.sh",
        ROOT / "scripts" / "reset_demo_paper_ledgers.sh",
    ]
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)
    for path in scripts[:7] + scripts[9:]:
        assert path.stat().st_mode & stat.S_IXUSR
    assert (ROOT / "scripts" / "check_deploy_rollout_readiness.py").stat().st_mode & stat.S_IXUSR
    paper_runner = _read("scripts/run_account_paper_execution_service.sh")
    assert "CALIBRATION" not in paper_runner
    assert "--latency-quantile" not in paper_runner
    assert "--slippage-quantile" not in paper_runner
    assert "/etc/liquidity-migration/account-execution/demo-rules.json" not in paper_runner
    assert "/etc/liquidity-migration/account-paper-execution/demo-rules.json" in paper_runner


def test_authorized_wrapper_owns_every_runtime_argv() -> None:
    wrapper = _read(WRAPPER)
    assert 'if [ "$#" -ne 2 ]' in wrapper
    for unit in AUTHORIZED_UNITS:
        assert f"{unit}:main" in wrapper
        fragment = _unit(unit)
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
        "liquidity-migration-bybit-carry-paper.service",
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
        "liquidity-migration-bybit-carry-demo.service": ("768M", "896M", "384M"),
        "liquidity-migration-bybit-continuous-paper.service": ("640M", "768M", "384M"),
        "liquidity-migration-bybit-long-paper.service": ("640M", "768M", "384M"),
        "liquidity-migration-bybit-carry-paper.service": ("640M", "768M", "384M"),
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
    mainnet_owner = "liquidity-migration-account-execution-mainnet.service"
    pairs = {
        "liquidity-migration-bybit-long-demo.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-bybit-continuous-demo.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-bybit-carry-demo.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-continuous-hedge.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-bybit-long-paper.service": "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-continuous-paper.service": "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-carry-paper.service": "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-long-mainnet.service": mainnet_owner,
        "liquidity-migration-bybit-carry-mainnet.service": mainnet_owner,
    }
    for producer, owner in pairs.items():
        fragment = _unit(producer)
        assert f"Requires={owner}" in fragment
        assert owner in next(line for line in fragment.splitlines() if line.startswith("After="))
        # Every producer, in every realm, has both credential pairs and the
        # arming switch stripped from its inherited environment.
        unset = next(line for line in fragment.splitlines() if line.startswith("UnsetEnvironment="))
        for stripped in (
            "BYBIT_DEMO_API_KEY",
            "BYBIT_DEMO_API_SECRET",
            "BYBIT_REAL_API_KEY",
            "BYBIT_REAL_API_SECRET",
            "REAL_MONEY",
        ):
            assert stripped in unset, (producer, stripped)
    for runner in (
        "scripts/run_bybit_long_demo_event_engine.sh",
        "scripts/run_bybit_continuous_demo_event_engine.sh",
        "scripts/run_continuous_hedge.sh",
    ):
        text = _read(runner)
        assert "place_order" not in text
        # A credential name may appear only inside a refusal. Reading one to
        # reject it is the opposite of holding order authority; assigning one
        # or handing it to the workload is what this forbids.
        for variable in ("BYBIT_DEMO_API_SECRET", "BYBIT_REAL_API_SECRET"):
            for line in text.splitlines():
                if variable not in line:
                    continue
                assert f"{variable}=" not in line.replace(f"{variable}:-", ""), (runner, line)
                assert "--api-secret" not in line, (runner, line)
                assert "export" not in line, (runner, line)


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
    carry_demo = _environment("liquidity-migration-bybit-carry-demo.service")
    carry_paper = _environment("liquidity-migration-bybit-carry-paper.service")
    for environment in (
        long_demo,
        long_paper,
        continuous_demo,
        continuous_paper,
        carry_demo,
        carry_paper,
    ):
        assert set(environment).isdisjoint(sizing_keys)
    for key in (
        "LOOKBACK_DAYS",
        "WORKERS",
    ):
        assert continuous_demo[key] == continuous_paper[key]
    for key in ("LOOKBACK_DAYS", "WORKERS", "WS_KLINES_ENABLED", "WS_KLINES_BOOTSTRAP_WORKERS"):
        assert long_demo[key] == long_paper[key]
    for key in ("LOOKBACK_DAYS", "WORKERS", "WS_KLINES_ENABLED"):
        assert carry_demo[key] == carry_paper[key]
    # Carry has no WS kline plane in either environment.
    assert carry_demo["WS_KLINES_ENABLED"] == "0"
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
    assert carry_demo["EXECUTION_ENVIRONMENT"] == "demo"
    assert carry_paper["EXECUTION_ENVIRONMENT"] == "paper"


def test_demo_account_notification_reads_no_retired_continuous_status_root() -> None:
    # CONTINUOUS retired 2026-07-29. The status root is now deliberately unset,
    # which is what removes the retired sleeve's line from the hourly digest;
    # re-promotion must set it explicitly again.
    demo_owner = _environment("liquidity-migration-account-execution.service")
    assert "CONTINUOUS_CYCLE_ROOT" not in demo_owner
    assert "CONTINUOUS_CYCLE_MAX_AGE_MINUTES" not in demo_owner


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

    # Carry follows the demo market plane through its own follow variable
    # (no WS kline store; the follower reads the demo public REST store).
    carry_demo = _environment("liquidity-migration-bybit-carry-demo.service")
    carry_paper = _environment("liquidity-migration-bybit-carry-paper.service")
    assert carry_paper["DATA_ROOT"] != carry_demo["DATA_ROOT"]
    assert carry_paper["CARRY_MARKET_FOLLOW_ROOT"] == carry_demo["DATA_ROOT"]
    assert "KLINES_FOLLOW_ROOT" not in carry_paper

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
        "liquidity-migration-bybit-carry-paper.service": (
            "/opt/liquidity-migration/data/bybit-carry-paper-event"
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
    assert "tests/test_candidate_rule_coverage.py" in install
    assert "tests/test_demo_rule_probe.py" in install
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
    # The paper twin's capital base derives from the committed operational
    # profile rather than a hidden per-host tuning value.
    assert (
        'values["PAPER_EQUITY_USDT"] = '
        "f\"{load_operational_profile(sys.argv[8]).capital_reference_usdt:g}\""
        in boundary
    )
    assert '"PAPER_EQUITY_USDT",' not in boundary
    assert "reset_path_safety preflight-paper" in boundary
    assert "reset_path_safety preflight-demo" in boundary
    assert "reset_path_safety normalize-paper" in boundary
    assert "reset_path_safety normalize-demo" in boundary
    assert "run_phase_pair runtime-tree-preflight" in boundary
    assert "run_phase_pair runtime-tree-normalize" in boundary
    assert boundary.index("runtime-tree-preflight") < boundary.index(
        "runtime-tree-normalize"
    )
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


def test_guarded_rollout_proves_flatness_around_ordered_shutdown() -> None:
    text = _read(DEPLOY)
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert rollout.index("rollout-target-prefetch") < rollout.index("current-topology-verification")
    assert rollout.index("current-topology-verification") < rollout.index("pre-stop-flat-account-proof")
    assert rollout.index("pre-stop-flat-account-proof") < rollout.index("stop-downstream-units")
    assert rollout.index("stop-downstream-units") < rollout.index("post-producer-flat-account-proof")
    assert rollout.index("post-producer-flat-account-proof") < rollout.index("stop-account-owners")
    assert rollout.index("stop-account-owners") < rollout.index("final-stopped-flat-account-proof")
    assert rollout.index("rollout-recovery-boundary rollback=unavailable") < rollout.index(
        "ROLLOUT_STOPPED=1"
    )
    assert rollout.index("final-stopped-flat-account-proof") < rollout.rindex(
        "ROLLOUT_IRREVERSIBLE=1"
    )
    assert rollout.index("stopped-install") < rollout.index("record-installed-profile")
    assert rollout.index("stopped-install") < rollout.index("post-rule-refresh-flat-account-proof")
    assert rollout.index("post-rule-refresh-flat-account-proof") < rollout.index("record-installed-profile")
    assert rollout.index("record-installed-profile") < rollout.index("activate-and-verify")
    assert "ROLLOUT_REFRESH_STALE_DEMO_RULES=1" in rollout
    assert "rollout-recovery-boundary rollback=unavailable" in rollout

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
    assert '|| status=$?' in rollout_check
    assert 'return "$status"' in rollout_check
    assert 'ROLLOUT_READINESS_HELPER_B64' in text
    assert '"$EXPECTED_COMMIT:scripts/check_deploy_rollout_readiness.py"' in text
    refresh = text[
        text.index("refresh_stale_demo_rules_if_requested()") :
        text.index("install_mode()")
    ]
    assert '--prior-rules-file "$demo_rules"' in refresh
    assert "probe_bybit_demo_rules.py" in refresh
    assert "project_demo_rules_to_candidate.py" in refresh
    assert "classify_demo_rule_receipt_freshness" in refresh
    assert 'status == "expired"' in refresh
    # Proactive renewal: a rollout in the receipt's back half must re-probe so
    # freshness never depends on timing a dispatch against the expiry instant.
    assert "REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS" in refresh
    assert "refresh-due-past-half-life" in refresh
    assert 'stale or future-dated' not in refresh
    assert "fresh-candidate-subset" in refresh
    assert "candidate-addition-or-structural-drift" in refresh
    assert "freeze_account_candidate_universe.py" in refresh
    assert "build_candidate_rule_coverage" in refresh
    assert "demo-rule refresh refuses mainnet credentials" in refresh
    assert "expired-authority-pre-exec" in text
    assert "ExecMainStatus" in text
    assert "expected_downstream_on()" in text
    assert "enabled-not-active cause=expired-authority-recovery" in text



def test_deploy_has_bounded_activation_waits_and_visible_expensive_phases() -> None:
    text = _read(DEPLOY)
    assert 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS="${RMOM_BOOTSTRAP_TIMEOUT_SECONDS:-300}"' in text
    assert 'RMOM_BOOTSTRAP_RETRY_SECONDS="${RMOM_BOOTSTRAP_RETRY_SECONDS:-10}"' in text
    assert "phase-start name=%s" in text
    assert "phase-ok name=%s elapsed_seconds=%s" in text
    assert "phase-group-start name=%s" in text
    assert "phase-group-ok name=%s elapsed_seconds=%s" in text
    assert 'if wait "$left_pid"; then left_status=0; else left_status=$?; fi' in text
    for phase in (
        "install-locked-dependencies",
        "focused-runtime-tests",
        "paper-tree-preflight",
        "paper-tree-normalize",
        "seed-residual-momentum",
    ):
        assert phase in text

    seed = text[text.index("seed_rmom()") : text.index("activate_mode()")]
    gate_check = 'scripts/check_residual_momentum_gate.py --path "$gate_path"'
    assert seed.index(gate_check) < seed.index("while true")
    assert seed.index("systemctl is-failed --quiet") < seed.index(gate_check)
    assert 'rmom-bootstrap path=reuse reason=current-valid-gate' in seed
    assert 'rmom-bootstrap path=refresh reason=missing-stale-invalid-or-failed-unit' in seed
    assert seed.count("systemctl start liquidity-migration-continuous-rmom-refresh.service") == 1


def test_parallel_phase_group_waits_for_both_and_returns_the_failed_member() -> None:
    text = _read(DEPLOY)
    helpers = text[text.index("run_phase()") : text.index("GIT_ENV=(")]
    result = subprocess.run(
        [
            "bash",
            "-c",
            helpers
            + r'''
left_phase() { sleep 0.05; echo left-finished; return 7; }
right_phase() { sleep 0.10; echo right-finished; return 0; }
if run_phase_pair test-group left left_phase right right_phase; then
    status=0
else
    status=$?
fi
printf 'captured-status=%s\n' "$status"
''',
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    combined = result.stdout + result.stderr
    assert "left-finished" in combined
    assert "right-finished" in combined
    assert "phase-group-failed name=test-group" in combined
    assert "left_status=7 right_status=0" in combined
    assert "captured-status=7" in combined


def test_rmom_seed_reuses_valid_gate_but_repairs_a_failed_refresh_unit() -> None:
    text = _read(DEPLOY)
    seed = text[text.index("seed_rmom()") : text.index("activate_mode()")]
    harness = r'''
set -u
PYTHON=fake_python
RMOM_BOOTSTRAP_TIMEOUT_SECONDS=5
RMOM_BOOTSTRAP_RETRY_SECONDS=1
CONTINUOUS_SLEEVE=on
CONTINUOUS_PAPER_SLEEVE=on
AUTH_PROFILE=operational
fail() { echo "fail:$*"; return 99; }
sleeve_on() { [ "$1" = on ]; }
fake_python() { echo gate-checked; return 0; }
'''

    reuse = subprocess.run(
        [
            "bash",
            "-c",
            harness
            + r'''
systemctl() {
    if [ "$1" = is-failed ]; then return 1; fi
    echo "unexpected-systemctl:$*"
    return 0
}
'''
            + seed
            + "\nseed_rmom\n",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "rmom-bootstrap path=reuse" in reuse.stdout
    assert "unexpected-systemctl" not in reuse.stdout

    repair = subprocess.run(
        [
            "bash",
            "-c",
            harness
            + r'''
systemctl() {
    case "$1" in
        is-failed) return 0 ;;
        reset-failed) echo reset-failed ;;
        start) echo refresh-started ;;
    esac
    return 0
}
'''
            + seed
            + "\nseed_rmom\n",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "rmom-bootstrap path=refresh" in repair.stdout
    assert "reset-failed" in repair.stdout
    assert "refresh-started" in repair.stdout
    assert "gate-checked" in repair.stdout


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


def test_workflow_runs_ci_on_push_and_only_manual_guarded_vps_modes() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert "pull_request:" in workflow and "push:" in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m ruff check" in workflow
    assert "--only-binary=:all: -r requirements.lock" in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    assert "options: [rollout, recover, install, activate, verify]" in workflow
    assert "authorize_demo_paper_operation:" in workflow
    assert "authorization_reference:" in workflow
    assert "reset_receipt:" in workflow
    assert 'deploy_args=("$DEPLOY_MODE_INPUT")' in workflow
    assert 'scripts/deploy_vps_live.sh "${deploy_args[@]}"' in workflow
    assert "AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION" in workflow
    assert 'test "$DEPLOY_OWNER_ACKNOWLEDGED_INPUT" = true' in workflow
    assert '--reset-receipt "$DEPLOY_RESET_RECEIPT_INPUT"' in workflow


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
    remote = deploy[deploy.index("set -Eeuo pipefail", deploy.index("cat <<'REMOTE_SCRIPT'")) :]
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


def test_paper_owner_owns_paper_telegram_notifications() -> None:
    owner = _unit("liquidity-migration-account-paper-execution.service")
    owner_unset = " ".join(
        line for line in owner.splitlines() if line.startswith("UnsetEnvironment=")
    )
    assert "TELEGRAM_BOT_TOKEN" not in owner_unset
    assert "TELEGRAM_CHAT_ID" not in owner_unset
    environment = _environment("liquidity-migration-account-paper-execution.service")
    assert environment["TELEGRAM_ENABLED"] == "1"
    assert "CONTINUOUS_CYCLE_ROOT" not in environment
    assert "CONTINUOUS_CYCLE_MAX_AGE_MINUTES" not in environment
    for producer in (
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-paper.service",
    ):
        unset = " ".join(
            line for line in _unit(producer).splitlines() if line.startswith("UnsetEnvironment=")
        )
        assert "TELEGRAM_BOT_TOKEN" in unset
        assert "TELEGRAM_CHAT_ID" in unset
    script = _read("scripts/run_account_paper_execution_service.sh")
    assert '"${TELEGRAM_ENABLED:-0}" == "1"' in script
    assert "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID is missing" in script
    assert "--telegram" in script
    assert "--continuous-cycle-root" in script
    deploy = _read(DEPLOY)
    assert "paper Telegram credentials unavailable" in deploy


def test_a_failing_nested_phase_aborts_the_rollout_instead_of_reporting_ok() -> None:
    """Bash suppresses errexit for the whole dynamic extent of a function called
    from a condition context.  Nesting a mode inside ``run_phase``'s ``if "$@"``
    therefore used to demote every gate the mode runs (pip/ruff/mypy/pytest) to a
    non-fatal warning: ``phase-failed name=ruff`` was followed by ``install-ok``
    and ``rollout-ok``."""
    text = _read(DEPLOY)
    helpers = text[text.index("fail() {") : text.index("run_phase_pair() {")]
    script = (
        "set -Eeuo pipefail\n"
        + helpers
        + r'''
install_mode() {
    run_phase ruff false
    echo "install-ok"
}
rollout_mode() {
    run_strict_phase stopped-install install_mode
    echo "rollout-ok"
}
rollout_mode
'''
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "phase-failed name=ruff" in combined
    assert "phase-failed name=stopped-install" in combined
    assert "install-ok" not in combined
    assert "rollout-ok" not in combined
    assert "phase-ok name=stopped-install" not in combined


def test_a_bare_failing_command_inside_a_strict_phase_aborts_the_rollout() -> None:
    text = _read(DEPLOY)
    helpers = text[text.index("fail() {") : text.index("run_phase_pair() {")]
    script = (
        "set -Eeuo pipefail\n"
        + helpers
        + r'''
install_mode() {
    false
    echo "install-ok"
}
run_strict_phase stopped-install install_mode
echo "rollout-ok"
'''
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "phase-failed name=stopped-install" in combined
    assert "install-ok" not in combined
    assert "rollout-ok" not in combined


def test_every_mode_level_phase_uses_the_strict_wrapper() -> None:
    text = _read(DEPLOY)
    for mode_function in ("install_mode", "activate_mode", "verify_topology"):
        assert f"run_phase {mode_function}" not in text
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("run_phase ") and stripped.endswith(mode_function):
                raise AssertionError(f"mode nested in a condition context: {stripped}")
    assert "run_strict_phase stopped-install install_mode" in text
    assert "run_strict_phase activate-and-verify activate_mode" in text
    assert "run_strict_phase current-topology-verification verify_topology" in text


def test_named_deploy_gates_fail_closed_even_under_a_suppressed_errexit() -> None:
    text = _read(DEPLOY)
    for gate in (
        'check_demo_order_permissions verify \\\n        || fail',
        'check_demo_order_permissions deploy \\\n        || fail',
        'validate_hedge_model_prior || fail',
        '--confirm-demo-probe \\\n            || fail',
    ):
        assert gate in text, gate


def test_rollout_and_reset_survive_a_dying_ssh_transport() -> None:
    for script in (DEPLOY, ROOT / "scripts" / "reset_demo_paper_ledgers.sh"):
        text = _read(script)
        assert "trap 'exit 129' HUP" in text
        assert "trap 'exit 141' PIPE" in text
        # Cleanup output must outlive a dead stdout.
        assert "cleanup_notice()" in text
        assert "logger -t liquidity-migration-" in text
    deploy = _read(DEPLOY)
    assert "trap '' INT TERM HUP PIPE" in deploy


def test_oneshot_units_have_explicit_start_timeouts_and_memory_bounds() -> None:
    # systemd's oneshot default is TimeoutStartSec=infinity and an
    # OnUnitActiveSec timer cannot re-trigger while its unit is activating.
    expected = {
        "liquidity-migration-demo-liveness.service": 120,
        "liquidity-migration-continuous-hedge.service": 300,
        "liquidity-migration-continuous-rmom-refresh.service": 900,
    }
    for name, seconds in expected.items():
        unit = _unit(name)
        assert "Type=oneshot" in unit
        assert f"TimeoutStartSec={seconds}" in unit
        assert "MemoryMax=" in unit
    rmom = _unit("liquidity-migration-continuous-rmom-refresh.service")
    assert "MemoryHigh=1G" in rmom
    assert "MemoryMax=1536M" in rmom


def test_deploy_workflow_keeps_the_ssh_session_alive_and_is_time_bounded() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert "ServerAliveInterval=15" in workflow
    assert "ServerAliveCountMax=3" in workflow
    assert "timeout-minutes:" in workflow


def test_paper_runner_has_no_hidden_equity_fallback() -> None:
    """A hidden 10,000 default silently ran the twin 25x under-scaled against the
    deployed 250,000 capital reference after a hand-edited env file; every
    sibling required input in this script already fails closed (audit M1)."""
    script = _read("scripts/run_account_paper_execution_service.sh")
    assert 'PAPER_EQUITY_USDT="${PAPER_EQUITY_USDT:-}"' in script
    assert "PAPER_EQUITY_USDT:-10000" not in script
    assert 'if [[ -z "$PAPER_EQUITY_USDT" ]]; then' in script
    required_check = script[script.index('if [[ -z "$PAPER_EQUITY_USDT" ]]; then') :]
    assert "exit 2" in required_check.split("\nfi", 1)[0]


def test_account_owner_units_configure_no_retired_sleeve_cycle_root() -> None:
    """A retired sleeve must leave no cycle root behind.

    Otherwise the owners keep reading a dead sleeve's completion receipt and
    every hourly Telegram digest carries a permanently growing
    "CONTINUOUS BTC gate: STALE" line (observed 2026-07-29).
    """

    for unit in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
    ):
        text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=CONTINUOUS_CYCLE_ROOT=" not in text, unit


def test_demo_owner_runner_passes_no_cycle_root_when_unset() -> None:
    runner = (ROOT / "scripts" / "run_account_execution_service.sh").read_text(encoding="utf-8")
    assert 'CONTINUOUS_CYCLE_ROOT="${CONTINUOUS_CYCLE_ROOT:-}"' in runner
    assert "data/bybit-continuous-demo-event" not in runner
    assert 'if [[ -n "$CONTINUOUS_CYCLE_ROOT" ]]; then' in runner
