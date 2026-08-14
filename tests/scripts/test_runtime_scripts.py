from __future__ import annotations

import re
import shlex
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
WRAPPER = ROOT / "scripts" / "run_authorized_runtime.sh"
SYSTEMD = ROOT / "deploy" / "systemd"

# Read from the deploy library rather than restated: a second copy silently
# stopped covering the mainnet owner, both mainnet producers, and the mirror.
AUTHORIZED_UNITS = tuple(
    re.findall(
        r"(liquidity-migration-\S+\.service)",
        re.findall(
            r'^LM_AUTHORIZED_UNITS="([^"]*)"',
            (ROOT / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8"),
            re.M,
        )[0],
    )
)


def _read(path: str | Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.read_text(encoding="utf-8")


def _unit(name: str) -> str:
    return _read(SYSTEMD / name)


def _section(unit_text: str, section: str) -> str:
    """One ``[Section]`` body. systemd reads most directives in exactly one
    section and silently ignores them elsewhere, so a substring check over the
    whole file proves nothing about whether a directive is in effect."""

    # Anchored to the start of a line: a section name also appears inside the
    # comments, and an unanchored search lands in the middle of one.
    header = f"[{section}]\n"
    start = unit_text.index(header) if unit_text.startswith(header) else unit_text.index(f"\n{header}")
    rest = unit_text[start + len(header) :].lstrip("\n")
    end = rest.find("\n[")
    return rest if end < 0 else rest[:end]


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
        ROOT / "scripts" / "runtime" / "run_bybit_long_demo_event_engine.sh",
        ROOT / "scripts" / "runtime" / "run_bybit_carry_demo_event_engine.sh",
        ROOT / "scripts" / "maintain" / "reset_demo_ledgers.sh",
    ]
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)
    for path in scripts:
        assert path.stat().st_mode & stat.S_IXUSR
    assert (ROOT / "scripts" / "vps" / "check_deploy_rollout_readiness.py").stat().st_mode & stat.S_IXUSR


def test_authorized_wrapper_owns_every_runtime_argv() -> None:
    wrapper = _read(WRAPPER)
    assert 'if [ "$#" -ne 2 ]' in wrapper
    for unit in AUTHORIZED_UNITS:
        assert f"{unit}:main" in wrapper
        fragment = _unit(unit)
        assert f"run_authorized_runtime.sh {unit} main" in fragment
    # No readiness entrypoint is registered any more. It belonged to the two
    # Python account owner units, and both are gone, so a wrapper arm for one
    # would be an argv nothing can reach.
    assert ":readiness" not in wrapper


ENGINE_UNIT = "liquidity-migration-engine.service"


def test_engine_unit_runs_its_own_demo_account_and_never_the_fleets() -> None:
    """Two writers on one venue account wedge each other: on 2026-08-14 a live
    engine on the fleet's demo account blocked the demo owner for ~100 seconds.
    The engine's per-account lock makes that a silent refusal to start rather
    than a loud collision, so the credential file this names is the whole
    defence.
    """

    unit = _unit(ENGINE_UNIT)
    service = _section(unit, "Service")
    environment_files = [
        line.removeprefix("EnvironmentFile=")
        for line in service.splitlines()
        if line.startswith("EnvironmentFile=")
    ]
    assert environment_files == [
        "/etc/liquidity-migration/bybit-quote-lab.env",
        "/etc/liquidity-migration/engine.env",
    ]
    # The fleet's own demo account and the funded one are both out of reach.
    # Directives only: the comments name both files on purpose.
    directives = "\n".join(
        line for line in service.splitlines() if line and not line.startswith("#")
    )
    assert "bybit-demo.env" not in directives
    assert "bybit-mainnet.env" not in directives
    unset = " ".join(line for line in service.splitlines() if line.startswith("UnsetEnvironment="))
    for stripped in ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET", "REAL_MONEY"):
        assert stripped in unset, stripped
    # The account it runs, and the one it must not, are named where somebody
    # editing this file will read them.
    assert "579580669" in unit
    assert "555899665" in unit

    assert f"ExecStart=/opt/liquidity-migration/scripts/run_authorized_runtime.sh {ENGINE_UNIT} main" in service
    assert "Restart=always" in service
    assert "WantedBy=multi-user.target" in _section(unit, "Install")
    for hardening in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
        "ProtectHome=false",
        "KillMode=control-group",
        "MemoryMax=",
    ):
        assert hardening in service, hardening


def test_engine_never_works_inside_the_deployed_checkout() -> None:
    """engine.toml's log path may be relative. Resolved inside
    /opt/liquidity-migration it writes an untracked file into the tree the
    deploy proves clean at every exact-commit step, and the next deploy of the
    funded fleet stops on it.
    """

    service = _section(_unit(ENGINE_UNIT), "Service")
    working = [
        line.removeprefix("WorkingDirectory=")
        for line in service.splitlines()
        if line.startswith("WorkingDirectory=")
    ]
    assert working == ["/var/lib/liquidity-migration-engine"]
    assert "ReadWritePaths=/var/lib/liquidity-migration-engine" in service
    # systemd creates it, so the unit does not depend on a deploy step having
    # run to be startable at all.
    assert "StateDirectory=liquidity-migration-engine" in service
    # Every other unit works from the checkout; this one must not.
    assert "WorkingDirectory=/opt/liquidity-migration\n" not in service


def _engine_command(tmp_path: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    """The real dispatch in run_authorized_runtime.sh, with the exec replaced by
    a print so the argv it built can be read.
    """

    script = tmp_path / "wrapper.sh"
    script.write_text(
        _read(WRAPPER).replace('exec "${COMMAND[@]}"', 'printf "%s\\n" "${COMMAND[@]}"'),
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(script), ENGINE_UNIT, "main"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **environment},
    )


def test_the_engine_stays_in_shadow_unless_the_host_file_plainly_says_otherwise(
    tmp_path: Path,
) -> None:
    """Shadow computes and sends nothing. The host's environment file can say
    "live" and can say nothing else: the argv is committed here.
    """

    shadow = _engine_command(tmp_path, ENGINE_CONFIG_FILE="/etc/liquidity-migration/engine.toml")
    assert shadow.returncode == 0, shadow.stdout + shadow.stderr
    assert shadow.stdout.split() == [
        "/opt/liquidity-migration-engine/bin/engine",
        "run",
        "--config",
        "/etc/liquidity-migration/engine.toml",
    ]

    for off in ("false", "0", "no", "off", "", "maybe", "1 --live"):
        result = _engine_command(tmp_path, ENGINE_CONFIG_FILE="/etc/engine.toml", ENGINE_LIVE=off)
        assert result.returncode == 0, off
        assert "--live" not in result.stdout, off

    for on in ("true", "TRUE", "1", "yes", "YES", "on", "On"):
        result = _engine_command(tmp_path, ENGINE_CONFIG_FILE="/etc/engine.toml", ENGINE_LIVE=on)
        assert result.returncode == 0, on
        assert result.stdout.split()[-1] == "--live", on

    # No config, no run: an engine started against a defaulted relative path
    # would read whatever engine.toml happened to be beside it.
    missing = _engine_command(tmp_path)
    assert missing.returncode != 0
    assert "ENGINE_CONFIG_FILE is required" in missing.stderr


def test_persistent_demo_workers_have_small_box_memory_limits() -> None:
    """carry/long are MemoryMax-only since the 2026-08-03 retune: both ran
    pinned at their MemoryHigh watermark for weeks (reclaim throttling, slow
    cycles), and a restart off the journal cursor beats silently stale cycles.
    """
    expected = {
        "liquidity-migration-bybit-long-demo.service": ("1024M", "384M"),
        "liquidity-migration-bybit-carry-demo.service": ("1408M", "384M"),
    }
    for unit, (maximum, swap) in expected.items():
        fragment = _unit(unit)
        assert "MemoryHigh=" not in fragment, unit
        assert f"MemoryMax={maximum}" in fragment
        assert f"MemorySwapMax={swap}" in fragment


def test_liveness_timer_has_one_bounded_activation_grace() -> None:
    # The demo watchdog's first pass is one minute after the timer arms: cold
    # start noise is handled by the watchdog's own startup grace, not by staying
    # blind for ten minutes across the window the owner now restarts in.
    expected_first_pass = {
        "liquidity-migration-demo-liveness.timer": "OnActiveSec=1min",
        "liquidity-migration-mainnet-liveness.timer": "OnActiveSec=10min",
    }
    for name, first_pass in expected_first_pass.items():
        timer = _unit(name)
        assert first_pass in timer
        assert "OnUnitActiveSec=3min" in timer
        assert "OnBootSec=" not in timer


def test_demo_watchdog_repages_within_the_hour_like_the_mainnet_one() -> None:
    """A six-hour cooldown meant one page, then silence across the whole outage."""
    wrapper = _read(WRAPPER)
    for unit in (
        "liquidity-migration-demo-liveness.service",
        "liquidity-migration-mainnet-liveness.service",
    ):
        start = wrapper.index(f"{unit}:main)")
        case = wrapper[start : wrapper.index("\n        ;;", start)]
        assert "--cooldown-min 60" in case, unit
        assert "--cooldown-min 360" not in case, unit


def test_producer_runners_carry_no_kernel_latch_cross_product() -> None:
    """Two tri-state parsers with eight accepted spellings each, plus an
    EXECUTION_ENVIRONMENT x latch consistency matrix, only re-derived values the unit
    files hard-code. The demo Telegram refusals were the same shape and were a latent
    fleet-wide start failure: a runner that had them would exit 2 on a variable another
    runner ignored, and no runner passes --telegram either way.
    """
    for runner in (
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
    ):
        text = _read(runner)
        assert "ACCOUNT_PAPER_KERNEL_REQUIRED" not in text, runner
        assert "kernel_required" not in text, runner
        assert "Kernel latch requires" not in text, runner
        assert "requires only ACCOUNT_" not in text, runner
        assert "Sleeve Telegram is retired" not in text, runner
        assert "--telegram" not in text, runner
        # What is left is the route and its owner roots.
        assert "case \"${EXECUTION_ENVIRONMENT:-}\" in" in text, runner
        assert "    demo) ;;" in text, runner
        assert "EXECUTION_ENVIRONMENT must be explicitly set" in text, runner
        assert (
            'if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then' in text
        ), runner
        # Both surviving runners are mainnet-capable and keep their strip
        # verification verbatim.
        mainnet_arm = text[text.index("    mainnet)") : text.index("\n    *)\n        echo \"EXECUTION_ENVIRONMENT")]
        assert "A target producer must not receive venue credentials." in mainnet_arm, runner
        assert "A target producer must not receive REAL_MONEY; it submits no orders." in mainnet_arm, runner


def test_retired_auth_shutdown_toggle_has_no_remaining_reference() -> None:
    for path in (".env.example", "scripts/deploy_vps_live.sh", "deploy/lib_sleeves.sh"):
        assert "AUTH_SHUTDOWN_EXPIRED_DEMO_RULES" not in _read(path), path


def test_liveness_observer_never_activates_or_orders_after_monitored_owner() -> None:
    observers = {
        "liquidity-migration-demo-liveness.service": "liquidity-migration-account-execution.service",
        "liquidity-migration-mainnet-liveness.service": (
            "liquidity-migration-account-execution-mainnet.service"
        ),
    }
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
    for observer, owner in observers.items():
        fragment = _unit(observer)
        for line in fragment.splitlines():
            directive, separator, _value = line.partition("=")
            if separator and directive in lifecycle_directives:
                assert owner not in line, (observer, line)

        assert "Wants=network-online.target" in fragment
        assert "After=network-online.target" in fragment


def test_mainnet_liveness_observer_pages_without_holding_trading_authority() -> None:
    fragment = _unit("liquidity-migration-mainnet-liveness.service")
    assert "EnvironmentFile=/etc/liquidity-migration/account-execution-mainnet.env" in fragment
    assert "EnvironmentFile=/etc/liquidity-migration/bybit-mainnet.env" in fragment
    assert "EnvironmentFile=/etc/liquidity-migration/account-execution.env" not in fragment
    assert "EnvironmentFile=/etc/liquidity-migration/bybit-demo.env" not in fragment
    unset = next(line for line in fragment.splitlines() if line.startswith("UnsetEnvironment="))
    assert unset == (
        "UnsetEnvironment=BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET "
        "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY"
    )
    environment = _environment("liquidity-migration-mainnet-liveness.service")
    assert environment["TELEGRAM_ENABLED"] == "1"

    wrapper = _read(WRAPPER)
    assert "scripts/check_demo_liveness.py" not in wrapper
    start = wrapper.index("liquidity-migration-mainnet-liveness.service:main)")
    case = wrapper[start : wrapper.index("\n        ;;", start)]
    assert "scripts/runtime/check_fleet_liveness.py" in case
    # The scope is the committed literal, not an env indirection a stale unit
    # file could repoint.
    assert "--account-scope mainnet" in case
    assert "--carry-mainnet-root /opt/liquidity-migration/data/bybit-carry-mainnet-event" in case
    assert "--long-mainnet-root /opt/liquidity-migration/data/bybit-long-mainnet-event" in case
    # Funded accounts re-page well inside the demo watchdog's 6-hour cooldown.
    assert "--cooldown-min 60" in case


def test_demo_strategy_units_use_one_validated_operational_profile() -> None:
    long_demo = _environment("liquidity-migration-bybit-long-demo.service")
    sizing_keys = (
        "NOTIONAL_MULTIPLIER",
        "ENTRY_LEVERAGE",
        "MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY",
        "ORDER_NOTIONAL_PCT_EQUITY",
        "MAX_NEW_ENTRIES_PER_CYCLE",
        "MAX_ACTIVE",
        "BTC_TREND_GATE",
        "PER_POSITION_NOTIONAL_PCT_EQUITY",
    )
    carry_demo = _environment("liquidity-migration-bybit-carry-demo.service")
    for environment in (long_demo, carry_demo):
        assert set(environment).isdisjoint(sizing_keys)
    # Carry streams its klines like LONG; REST is the gap/funding fallback.
    assert carry_demo["WS_KLINES_ENABLED"] == "1"
    assert carry_demo["WS_KLINES_BOOTSTRAP_WORKERS"] == "2"
    long_runner = _read("scripts/runtime/run_bybit_long_demo_event_engine.sh")
    carry_runner = _read("scripts/runtime/run_bybit_carry_demo_event_engine.sh")
    # Both runners take the one shared profile from ACCOUNT_RISK_POLICY_FILE and
    # hand it to the workload; the flag name each engine reads differs.
    for runner, flag in (
        (long_runner, '--operational-profile-file'),
        (carry_runner, '--risk-policy-file'),
    ):
        assert 'ACCOUNT_RISK_POLICY_FILE' in runner
        assert flag in runner
    assert long_demo["EXECUTION_ENVIRONMENT"] == "demo"
    assert carry_demo["EXECUTION_ENVIRONMENT"] == "demo"


def test_install_is_stopped_exact_commit_preparation_only() -> None:
    text = _read(DEPLOY)
    install = text[text.index("install_mode()") : text.index("load_authorization()")]
    assert install.index("require_quiescent") < install.index("git_fetch fetch")
    assert install.index("git_fetch fetch") < install.index("git checkout -B")
    assert "requirements.lock" in install
    assert "--no-deps" in install and "--only-binary=:all:" in install
    # No lint/type/test phase: CI runs scripts/dev.sh lint+types+test on every
    # push to main and install proves the commit is an ancestor of the branch,
    # so re-running them only lengthens the stopped window.
    assert "-m ruff check" not in install
    assert "-m mypy" not in install
    assert "-m pytest" not in install
    assert "lm_install_current_systemd_units" in install
    assert "systemctl disable --now" in install
    assert "systemctl start" not in install
    assert "systemctl enable --now" not in install
    assert "lm_write_resolved_sleeve_toggles" in install
    # The resolved toggles are written atomically (mktemp + mv), so install does
    # not re-read its own write; load_authorization validates the file a
    # previous process wrote.
    assert "lm_verify_resolved_sleeve_toggles" not in install
    assert "units_started=0" in install


def test_install_prepares_the_demo_runtime_config_with_bounded_tree_normalization() -> None:
    text = _read(DEPLOY)
    boundary = text[
        text.index("prepare_demo_runtime_config()") : text.index("require_checkout()")
    ]
    assert "reset_path_safety preflight-demo" in boundary
    assert "reset_path_safety normalize-demo" in boundary
    assert "run_phase demo-tree-preflight" in boundary
    assert "run_phase demo-tree-normalize" in boundary
    assert boundary.index("demo-tree-preflight") < boundary.index("demo-tree-normalize")
    assert "--create-missing" in boundary
    assert "chown -R" not in boundary
    # The retired paper host config is removed, never re-provisioned.
    assert "retire_paper_host_config" in boundary
    retired = text[text.index("RETIRED_PAPER_CONFIG_DIR=") : text.index("prepare_demo_runtime_config()")]
    assert "/etc/liquidity-migration/account-paper-execution" in retired


def test_install_binds_the_shared_strategy_target_tape() -> None:
    text = _read(DEPLOY)
    boundary = text[
        text.index("prepare_demo_runtime_config()") : text.index("require_checkout()")
    ]

    assert '"$demo_capture/strategy-targets.jsonl"' in boundary
    assert 'values["STRATEGY_TARGET_CAPTURE_PATH"] = str(target_capture)' in boundary


def test_activation_verifies_bound_state_before_start_and_cannot_reconfigure_it() -> None:
    text = _read(DEPLOY)
    activate = text[text.index("activate_mode()") : text.index('case "$MODE" in', text.index("activate_mode()"))]
    assert activate.index("load_authorization") < activate.index("systemctl start")
    # The account owner is no longer started here by name: the engine block
    # below starts it, and only where the engine is installed.
    assert "account-execution.service" not in activate.split("start_if")[0]
    for forbidden in ("git fetch", "git checkout", "pip install", "lm_write_resolved", "sed -i"):
        assert forbidden not in activate


def test_guarded_rollout_proves_flatness_around_ordered_shutdown() -> None:
    text = _read(DEPLOY)
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert rollout.index("rollout-target-prefetch") < rollout.index("current-topology-verification")
    assert rollout.index("current-topology-verification") < rollout.index("pre-stop-flat-account-proof")
    assert rollout.index("pre-stop-flat-account-proof") < rollout.index("stop-downstream-units")
    assert rollout.index("stop-downstream-units") < rollout.index("post-producer-flat-account-proof")
    assert rollout.index("post-producer-flat-account-proof") < rollout.index("stop-account-owners")
    assert rollout.index("stop-account-owners") < rollout.index("final-stopped-flat-account-proof")
    assert rollout.index("final-stopped-flat-account-proof") < rollout.rindex(
        "ROLLOUT_IRREVERSIBLE=1"
    )
    assert rollout.index("stopped-install") < rollout.index("record-installed-profile")
    assert rollout.index("stopped-install") < rollout.index("post-rule-refresh-flat-account-proof")
    assert rollout.index("post-rule-refresh-flat-account-proof") < rollout.index("record-installed-profile")
    assert rollout.index("record-installed-profile") < rollout.index("activate-and-verify")
    # The engine build is the one step allowed to fail, so it comes after the
    # two assignments that disarm the rollback trap. Before them, an abort
    # inside it would reach the machinery that stops the whole fleet.
    assert rollout.index("activate-and-verify") < rollout.index("ROLLOUT_COMPLETE=1")
    assert rollout.index("ROLLOUT_COMPLETE=1") < rollout.index("run_phase engine-build")
    assert rollout.index("ROLLOUT_STOPPED=0") < rollout.index("run_phase engine-build")
    assert "ROLLOUT_REFRESH_STALE_DEMO_RULES=1" in rollout
    # Every flat-account proof runs in the same order either way; only whether a
    # residual stops the rollout depends on the realm.
    for proof in (
        "pre-stop-flat-account-proof",
        "post-producer-flat-account-proof",
        "final-stopped-flat-account-proof",
        "post-rule-refresh-flat-account-proof",
    ):
        assert f"rollout_flat_phase {proof}" in rollout

    readiness = _read("scripts/vps/check_deploy_rollout_readiness.py")
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
    assert '"$EXPECTED_COMMIT:scripts/vps/check_deploy_rollout_readiness.py"' in text
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



def test_deploy_has_bounded_activation_waits_and_visible_expensive_phases() -> None:
    text = _read(DEPLOY)
    assert "phase-start name=%s" in text
    assert "phase-ok name=%s elapsed_seconds=%s" in text
    assert "phase-group-start name=%s" in text
    assert "phase-group-ok name=%s elapsed_seconds=%s" in text
    assert 'if wait "$left_pid"; then left_status=0; else left_status=$?; fi' in text
    for phase in (
        "install-locked-dependencies",
        "demo-tree-preflight",
        "demo-tree-normalize",
        "engine-build",
    ):
        assert phase in text


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
    # The retired split profile is rejected loudly, not silently accepted.
    assert "profile demo-operational retired" in text


def test_systemd_installer_is_manifest_exact_and_never_starts_current_units() -> None:
    lib = _read("deploy/lib_sleeves.sh")
    install = lib[lib.index("lm_install_current_systemd_units()") :]
    assert "lm_cleanup_unknown_liqmig_units" in install
    assert "lm_verify_no_unknown_liqmig_units" in install
    assert "lm_verify_guarded_unit_surfaces" in install
    assert "systemctl start" not in install
    assert re.search(r"^\s*systemctl enable", install, re.MULTILINE) is None
    assert "cp " in install


def test_resolved_sleeves_are_atomically_generated_then_root_bound() -> None:
    lib = _read("deploy/lib_sleeves.sh")
    writer = lib[lib.index("lm_write_resolved_sleeve_toggles()") : lib.index("lm_verify_resolved_sleeve_toggles()")]
    assert "chmod 0600" in writer
    assert 'mv "$_lr_tmp" "$LM_RESOLVED_SLEEVES_ENV"' in writer
    deploy = _read(DEPLOY)
    assert "chown root:root /etc/liquidity-migration/sleeves.resolved.env" in deploy
    assert "chmod 0600 /etc/liquidity-migration/sleeves.resolved.env" in deploy
    # Every producer that reads a toggle reads the resolved file, never the
    # repo's own sleeves.env.
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
    ):
        assert "EnvironmentFile=/etc/liquidity-migration/sleeves.resolved.env" in _unit(unit), unit


def test_workflow_runs_ci_on_push_and_only_manual_guarded_vps_modes() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert "pull_request:" in workflow and "push:" in workflow
    # CI runs the same gate the local check does, through one entry point, so
    # the two cannot silently diverge on what they cover.
    assert "scripts/dev.sh lint" in workflow
    assert "scripts/dev.sh types" in workflow
    assert "scripts/dev.sh test" in workflow
    assert "--only-binary=:all: -r requirements.lock" in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    # The workflow must offer only modes the deploy script has, and pass only
    # flags it parses: rollout rejects anything but `--profile`, so a stale flag
    # fails the dispatch outright.
    assert "options: [rollout, install, activate, verify]" in workflow
    assert "inputs.reset_receipt" not in workflow
    assert "--reset-receipt" not in workflow
    assert "--authorization-reference" not in workflow
    assert "--owner-acknowledgement" not in workflow
    # No dispatchable branch may name a mode the deploy script does not have.
    assert "\n            recover)" not in workflow
    # Dispatching the workflow at all is the operator's act. A checkbox and a
    # free-text reference that no script reads added nothing to that, so both
    # inputs and their four `test` assertions are gone.
    assert "authorize_demo_paper_operation" not in workflow
    assert "authorization_reference" not in workflow
    assert "DEPLOY_OWNER_ACKNOWLEDGED_INPUT" not in workflow
    assert "DEPLOY_AUTHORIZATION_REFERENCE_INPUT" not in workflow
    assert 'deploy_args=("$DEPLOY_MODE_INPUT")' in workflow
    assert 'scripts/deploy_vps_live.sh "${deploy_args[@]}"' in workflow
    assert 'deploy_args+=(--profile "$DEPLOY_PROFILE_INPUT")' in workflow
    assert 'EXPECTED_COMMIT="$GITHUB_SHA"' in workflow


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
    assert '"$EXPECTED_COMMIT:liquidity_migration/ops/maintenance_lock.py"' in transmission
    assert '/usr/bin/git --no-pager --git-dir="$LOCAL_REPOSITORY/.git"' in transmission
    assert 'cat-file -t "$EXPECTED_COMMIT"' in transmission
    assert "GIT_NO_REPLACE_OBJECTS=1" in transmission
    assert "EXPECTED_COMMIT is not a local commit object" in transmission
    assert "MAINTENANCE_LOCK_HELPER_B64" in transmission
    assert "../liquidity_migration/ops/maintenance_lock.py" not in transmission
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
    text = _read("scripts/vps/print_vps_recovery_command.sh")
    assert 'git show "$commit:$1"' in text
    assert "vps_restore_ssh_access.sh" in text
    assert "vps_rescue_restore_ssh_access.sh" in text
    assert "deploy_vps_live.sh install" in text
    assert "deploy_vps_live.sh activate" in text
    assert "reset --hard" not in text
    assert "git clean" not in text
    assert "GITHUB_TOKEN" not in text


def test_a_failing_nested_phase_aborts_the_rollout_instead_of_reporting_ok() -> None:
    """Bash suppresses errexit for the whole dynamic extent of a function called from a
    condition context, so nesting a mode inside ``run_phase``'s ``if "$@"`` demotes
    every gate the mode runs (pip/ruff/mypy/pytest) to a non-fatal warning.
    """
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
        'check_demo_order_permissions deploy \\\n        || fail',
        '--confirm-demo-probe \\\n            || fail',
    ):
        assert gate in text, gate
    # Inside verify_topology the venue probe routes through verify_probe, which
    # records a mismatch (fatal at the end of any mode but read-only `verify`)
    # rather than swallowing a nonzero status.
    verify = text[text.index("verify_topology()") : text.index("start_if()")]
    assert (
        'verify_probe demo-order-permissions "demo order permission verification failed" \\\n'
        "        check_demo_order_permissions verify" in verify
    )
    probe = text[text.index("verify_probe() {") : text.index("verify_unit() {")]
    assert "verify_note" in probe
    assert 'if verify_report_only; then' in probe


def test_rollout_and_reset_survive_a_dying_ssh_transport() -> None:
    deploy = _read(DEPLOY)
    assert "trap 'exit 129' HUP" in deploy
    assert "trap 'exit 141' PIPE" in deploy
    # Cleanup output must outlive a dead stdout.
    assert "cleanup_notice()" in deploy
    assert "logger -t liquidity-migration-" in deploy
    assert "trap '' INT TERM HUP PIPE" in deploy
    # The reset moved to Python (2026-08-03): HUP/PIPE route through the
    # fail-closed handoff with the same exit codes, and cleanup lines still
    # reach the host journal even on a dead transport.
    reset_module = _read(
        ROOT / "liquidity_migration" / "ops" / "demo_ledger_reset.py"
    )
    assert '_raise_signal(129)' in reset_module
    assert '_raise_signal(141)' in reset_module
    assert 'signal.SIGHUP' in reset_module and 'signal.SIGPIPE' in reset_module
    assert '"logger", "-t", "liquidity-migration-reset"' in reset_module
    assert "def cleanup_notice" in reset_module


def test_oneshot_units_have_explicit_start_timeouts_and_memory_bounds() -> None:
    # systemd's oneshot default is TimeoutStartSec=infinity and an
    # OnUnitActiveSec timer cannot re-trigger while its unit is activating.
    expected = {
        "liquidity-migration-demo-liveness.service": 120,
        "liquidity-migration-mainnet-liveness.service": 120,
    }
    for name, seconds in expected.items():
        unit = _unit(name)
        assert "Type=oneshot" in unit
        assert f"TimeoutStartSec={seconds}" in unit
        assert "MemoryMax=" in unit


def test_deploy_workflow_keeps_the_ssh_session_alive_and_is_time_bounded() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert "ServerAliveInterval=15" in workflow
    assert "ServerAliveCountMax=3" in workflow
    assert "timeout-minutes:" in workflow


def _mainnet_harness(
    armed: str,
    preflight_status: int,
    *,
    rules_file: str = "/fake/etc/venue-rules.json",
    rule_age_status: int = 0,
) -> str:
    """The real mainnet functions over stub systemctl/python, so behavior is exercised
    rather than pattern-matched. ``armed`` pre-seeds the cached switch state.

    ``rules_file`` is the receipt the route env binds; point it at a real path to
    exercise the renewal branch. ``rule_age_status`` is what the staleness probe
    exits with -- 4 is the past-half-life reading the deploy must act on.
    """

    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")
    return (
        "set -u\n"
        f"PYTHON=fake_python\n"
        f"PREFLIGHT_STATUS={preflight_status}\n"
        f"RULE_AGE_STATUS={rule_age_status}\n"
        f"RULES_FILE={shlex.quote(rules_file)}\n"
        "EXPECTED_COMMIT=00112233445566778899\n"
        'fail() { echo "fail:$*"; exit 9; }\n'
        "require_checkout() { :; }\n"
        "load_authorization() { :; }\n"
        'verify_topology() { echo "verify_topology"; }\n'
        "systemctl() {\n"
        '    printf "systemctl:%s\\n" "$*"\n'
        '    [ "$1" != is-active ] || return "$IS_ACTIVE_STATUS"\n'
        "    return 0\n"
        "}\n"
        "IS_ACTIVE_STATUS=1\n"
        "fake_python() {\n"
        '    printf "python:%s\\n" "$*"\n'
        '    case "$*" in *preflight*) return "$PREFLIGHT_STATUS" ;; esac\n'
        '    case "$*" in "- $RULES_FILE") return "$RULE_AGE_STATUS" ;; esac\n'
        "    return 0\n"
        "}\n"
        'install() { printf "install:%s\\n" "$*"; }\n'
        'chown() { :; }\n'
        'chmod() { :; }\n'
        'mkdir() { printf "mkdir:%s\\n" "$*"; }\n'
        "lm_load_private_systemd_environment() {\n"
        '    shift\n'
        '    printf "load:%s\\n" "$*"\n'
        "    ACCOUNT_RISK_POLICY_FILE=/fake/etc/risk-policy.json\n"
        "    ACCOUNT_SYMBOLS_FILE=/fake/etc/candidate-universe.json\n"
        '    ACCOUNT_DEMO_RULES_FILE="$RULES_FILE"\n'
        "    ACCOUNT_EXECUTION_ROOT=/fake/var/account-mainnet\n"
        "    ACCOUNT_INTENT_INBOX_ROOT=/fake/var/inbox-mainnet\n"
        "    BYBIT_REAL_API_KEY=fake-key\n"
        "    BYBIT_REAL_API_SECRET=fake-secret\n"
        "    return 0\n"
        "}\n"
        'REPO_DIR=/fake/repo\n'
        + library[library.index("sleeve_on() {") : library.index("lm_write_resolved_sleeve_toggles()")]
        + text[
            text.index("# The single arming switch") : text.index("# verify_topology collects")
        ]
        + text[text.index("start_if() {") : text.index("activate_mode()")]
        + text[
            text.index("MAINNET_OWNER_UNIT=") : text.index("ROLLOUT_DOWNSTREAM_UNITS=(")
        ]
        + f"MAINNET_ARMED_STATE={armed}\n"
    )


def test_mainnet_venue_rules_are_renewed_past_half_life(tmp_path: Path) -> None:
    """A receipt frozen once and never renewed becomes a hard refusal to start.

    The funded owner enforces the registered 168-hour ceiling and cannot be given
    a larger one, so nothing about an existing file makes it usable. Freezing only
    when the path is absent leaves the funded account one expiry away from an
    owner that will not start with exposure already on the book.
    """

    rules_file = tmp_path / "venue-rules.json"
    rules_file.write_text("{}", encoding="utf-8")

    fresh = subprocess.run(
        [
            "bash",
            "-c",
            _mainnet_harness("armed", 0, rules_file=str(rules_file), rule_age_status=0)
            + "\nprovision_mainnet_prerequisites\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = fresh.stdout + fresh.stderr
    assert fresh.returncode == 0, combined
    assert "mainnet-venue-rule-plan path=reuse reason=fresh" in combined
    # A fresh receipt is left exactly as it is: no venue read, no rebind.
    assert "freeze_venue_instrument_rules.py" not in combined, combined

    stale = subprocess.run(
        [
            "bash",
            "-c",
            _mainnet_harness("armed", 0, rules_file=str(rules_file), rule_age_status=4)
            + "\nprovision_mainnet_prerequisites\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = stale.stdout + stale.stderr
    assert stale.returncode == 0, combined
    assert "mainnet-venue-rule-plan path=freeze reason=refresh-due-past-half-life" in combined
    # Renewal freezes a FRESH universe+rules pair (membership follows the
    # venue — a delisted symbol must leave the universe rather than block
    # every renewal), each to a new artifact plus one rebind of the pair.
    fresh_universe = re.search(
        r"freeze_account_candidate_universe\.py --realm mainnet "
        r"--output (/var/lib/liquidity-migration/mainnet-candidate-universe-receipts/\S+)",
        combined,
    )
    assert fresh_universe is not None, combined
    assert "freeze_venue_instrument_rules.py --realm mainnet" in combined, combined
    refreshed = re.search(
        r"--output (/var/lib/liquidity-migration/mainnet-venue-rule-receipts/\S+)",
        combined,
    )
    assert refreshed is not None, combined
    assert str(rules_file) != refreshed.group(1)
    # The rules bind the FRESH universe, and the receipt covers what the
    # account still holds: the exposure scan and the prior receipt travel
    # with the freeze so a held symbol keeps a rule.
    assert f"--symbols-file {fresh_universe.group(1)}" in combined, combined
    assert "--held-exposure-account-root /fake/var/account-mainnet" in combined, combined
    assert "--held-exposure-inbox-root /fake/var/inbox-mainnet" in combined, combined
    assert f"--prior-rules-file {rules_file}" in combined, combined
    assert (
        f"python:- /etc/liquidity-migration/account-execution-mainnet.env "
        f"{refreshed.group(1)} {fresh_universe.group(1)}" in combined
    ), combined
    assert "mainnet-venue-rule-refresh-ok" in combined
    # The renewal reads the venue; it never places an order, so it needs no
    # stopped window and must not be gated behind one.
    assert "probe_bybit_demo_rules.py" not in combined

    # A failed re-freeze of EITHER half keeps the installed pair and lets the
    # deploy finish (2026-08-13: a fatal renewal stranded the mainnet units
    # stopped). Nothing may rebind on the failed path.
    for failing_output in (
        "mainnet-candidate-universe-receipts",
        "mainnet-venue-rule-receipts",
    ):
        # Keyed on the renewal receipt DIRECTORIES so the bootstrap freezes
        # (which write the /fake/etc paths and rightly hard-fail) stay green.
        harness = _mainnet_harness(
            "armed", 0, rules_file=str(rules_file), rule_age_status=4
        ).replace(
            "fake_python() {\n",
            "fake_python() {\n"
            f'    case "$*" in *freeze*--output*{failing_output}*) '
            'printf "python:%s\\n" "$*"; return 7 ;; esac\n',
            1,
        )
        survived = subprocess.run(
            ["bash", "-c", harness + "\nprovision_mainnet_prerequisites\n"],
            capture_output=True,
            text=True,
        )
        combined = survived.stdout + survived.stderr
        assert survived.returncode == 0, (failing_output, combined)
        assert "REFRESH-FAILED-KEEPING-VALID-RECEIPT" in combined, (failing_output, combined)
        assert "mainnet-venue-rule-refresh-ok" not in combined, (failing_output, combined)
        assert (
            "python:- /etc/liquidity-migration/account-execution-mainnet.env"
            not in combined
        ), (failing_output, combined)

    # An unreadable or future-dated receipt is not an age question, and must not
    # be answered by silently freezing over it.
    for broken in (3, 5):
        refused = subprocess.run(
            [
                "bash",
                "-c",
                _mainnet_harness(
                    "armed", 0, rules_file=str(rules_file), rule_age_status=broken
                )
                + "\nprovision_mainnet_prerequisites\n",
            ],
            capture_output=True,
            text=True,
        )
        combined = refused.stdout + refused.stderr
        assert refused.returncode != 0, combined
        assert "fail:installed mainnet venue-rule receipt failed validation" in combined


def test_mainnet_start_creates_roots_then_gates_on_preflight() -> None:
    blocked = subprocess.run(
        ["bash", "-c", _mainnet_harness("armed", 1) + "\nstart_mainnet_fleet\n"],
        capture_output=True,
        text=True,
    )
    combined = blocked.stdout + blocked.stderr
    assert blocked.returncode != 0
    assert "fail:mainnet preflight has outstanding steps" in combined
    # Nothing mainnet may start while a precondition is outstanding.
    assert "systemctl:enable" not in combined
    assert "systemctl:start" not in combined

    started = subprocess.run(
        ["bash", "-c", _mainnet_harness("armed", 0) + "\nstart_mainnet_fleet\n"],
        capture_output=True,
        text=True,
    )
    combined = started.stdout + started.stderr
    assert started.returncode == 0, combined
    assert "python:-m liquidity_migration.policy.real_money_arming create-state-roots --execute" in combined
    assert combined.index("create-state-roots") < combined.index("preflight")
    # The collapsed arming path provisions everything before the gate: route
    # env from the template, telegram default, profile always re-rendered
    # from the dials, frozen inputs when absent — in that order, all of it
    # before create-state-roots and the preflight.
    assert "install:-o root -g root -m 600" in combined
    assert "default-telegram" in combined
    assert (
        "render-profile --execute --overwrite --output /fake/etc/risk-policy.json"
        in combined
    )
    assert "freeze_account_candidate_universe.py --realm mainnet" in combined
    assert (
        "freeze_venue_instrument_rules.py --realm mainnet "
        "--symbols-file /fake/etc/candidate-universe.json" in combined
    )
    assert combined.index("install:") < combined.index("default-telegram")
    assert combined.index("render-profile") < combined.index("create-state-roots")
    assert (
        "systemctl:enable liquidity-migration-account-execution-mainnet.service" in combined
    )
    # The owner starts first; both registered producers always start — the
    # installed risk profile, not a toggle, decides their shares.
    assert (
        combined.index("systemctl:enable liquidity-migration-account-execution-mainnet.service")
        < combined.index("systemctl:enable --now liquidity-migration-bybit-carry-mainnet.service")
    )
    assert (
        combined.index("systemctl:enable --now liquidity-migration-bybit-carry-mainnet.service")
        < combined.index("systemctl:enable --now liquidity-migration-bybit-long-mainnet.service")
    )
    assert (
        combined.index("systemctl:enable --now liquidity-migration-bybit-long-mainnet.service")
        < combined.index("systemctl:enable --now liquidity-migration-mainnet-liveness.timer")
    )
    for foreign in ("-demo.service", "-paper", "account-execution.service"):
        assert foreign not in combined, foreign


def test_mainnet_armed_reads_the_single_switch(tmp_path: Path) -> None:
    """REAL_MONEY=true in the mainnet credential file is the whole arming decision:
    absent file, any other value, or an unreadable file must read as disarmed --
    the last of those loudly. The read stands alone (real interpreter, real
    strict parser) because early install stages carry no helpers.
    """
    import sys

    text = _read(DEPLOY)
    block = text[text.index("# The single arming switch") : text.index("# verify_topology collects")]

    def harness(body: str | None, tag: str) -> str:
        cred = tmp_path / f"cred-{tag}.env"
        if body is not None:
            cred.write_text(body, encoding="utf-8")
            cred.chmod(0o600)
        return (
            "set -u\n"
            'fail() { echo "fail:$*" >&2; exit 9; }\n'
            f"PYTHON={sys.executable}\n"
            f"REPO_DIR={ROOT}\n"
            "chown() { :; }\n"
            + block
            + f"MAINNET_CREDENTIAL_ENV={cred}\n"
            "if mainnet_armed; then echo armed-yes; else echo armed-no; fi\n"
        )

    for body, tag, expected in (
        ("REAL_MONEY=true\n", "true", "armed-yes"),
        ("REAL_MONEY=1\n", "one", "armed-yes"),
        ("REAL_MONEY=false\n", "false", "armed-no"),
        ("", "empty", "armed-no"),
        (None, "absent", "armed-no"),
    ):
        result = subprocess.run(
            ["bash", "-c", harness(body, tag)], capture_output=True, text=True
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, (tag, combined)
        assert expected in combined, (tag, combined)

    # A file the strict parser refuses reads as a loud failure, never as a guess.
    broken = subprocess.run(
        ["bash", "-c", harness("NOT A STRICT LINE\n", "broken")],
        capture_output=True,
        text=True,
    )
    combined = broken.stdout + broken.stderr
    assert broken.returncode != 0
    assert "cannot read the arming switch" in combined



def test_stop_mainnet_stops_only_mainnet_and_says_exposure_is_unchanged() -> None:
    result = subprocess.run(
        ["bash", "-c", _mainnet_harness("armed", 0) + "\nstop_mainnet_mode\n"],
        check=True,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    for unit in (
        "liquidity-migration-mainnet-liveness.timer",
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-bybit-long-mainnet.service",
        "liquidity-migration-account-execution-mainnet.service",
    ):
        assert f"systemctl:disable --now {unit}" in combined
    # The owner stops last: the producers declare Requires=/After= on it.
    assert combined.rindex("disable --now liquidity-migration-account-execution-mainnet.service") > (
        combined.index("disable --now liquidity-migration-bybit-carry-mainnet.service")
    )
    assert "stop-mainnet-ok" in combined
    assert "exposure is unchanged" in combined
    for foreign in ("-demo.service", "-paper", "account-execution.service"):
        assert foreign not in combined, foreign


def test_stop_mainnet_fails_when_a_unit_survives_the_stop() -> None:
    harness = _mainnet_harness("armed", 0).replace(
        "IS_ACTIVE_STATUS=1", "IS_ACTIVE_STATUS=0"
    )
    result = subprocess.run(
        ["bash", "-c", harness + "\nstop_mainnet_mode\n"],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unit remained active after mainnet stop" in combined


def test_verify_asserts_the_mainnet_fleet_only_when_armed() -> None:
    text = _read(DEPLOY)
    verify = text[text.index("verify_topology()") : text.index("start_if()")]
    assert "if mainnet_armed; then" in verify
    # No mainnet account-owner row: the Python owner is deleted and the
    # mainnet engine is not installed anywhere yet.
    assert "account-execution-mainnet.service" not in verify
    assert "verify_unit on liquidity-migration-mainnet-liveness.timer" in verify
    assert "is active under demo authorization" in verify
    assert "mainnet=%s" in verify

    # An enabled timer is not a succeeding watchdog: without this the funded
    # observer can fail every fire and verify still reads green. Scoped inside
    # the armed branch so a stale failure cannot break demo verify.
    failed_check = "systemctl is-failed --quiet liquidity-migration-mainnet-liveness.service"
    assert failed_check in verify
    assert (
        'verify_note "liquidity-migration-mainnet-liveness.service is failed"' in verify
    )
    assert verify.index("if mainnet_armed; then") < verify.index(failed_check)
    assert verify.index(failed_check) < verify.index("for oneshot in")


def test_resolved_sleeve_allowlist_covers_every_generated_toggle() -> None:
    """``lm_load_private_systemd_environment`` unsets any key it was not asked for, so a
    toggle missing from this allowlist is an unbound-variable abort under ``set -u``
    at the next activation.
    """

    library = _read("deploy/lib_sleeves.sh")
    writer = library[
        library.index("lm_write_resolved_sleeve_toggles()") :
        library.index("lm_verify_resolved_sleeve_toggles()")
    ]
    generated = set(re.findall(r"printf '([A-Z_]+)=%s", writer))
    assert generated == {
        "LONG_SLEEVE",
        "CARRY_SLEEVE",
    }

    text = _read(DEPLOY)
    authorization = text[text.index("load_authorization()") : text.index("unit_on()")]
    call = authorization[
        authorization.index("lm_load_private_systemd_environment") :
        authorization.index("# A resolved file written by a pre-carry install")
    ]
    allowed = set(re.findall(r"\b([A-Z][A-Z0-9_]*)\b", call))
    assert generated <= allowed, generated - allowed
    for key in generated:
        assert f'"${key}"' in authorization, key
    # An older resolved file predates the newest keys; absent must mean off
    # rather than a hard failure mid-rollout.
    assert "carry-keys=absent treated-as=off" in authorization


def test_routine_activation_restores_and_rollout_stops_the_funded_fleet() -> None:
    text = _read(DEPLOY)
    activate = text[text.index("activate_mode()") : text.index("MAINNET_OWNER_UNIT=")]
    assert activate.index("liquidity-migration-demo-liveness.timer") < activate.index(
        "if mainnet_armed; then\n        start_mainnet_fleet"
    )
    assert activate.index("start_mainnet_fleet") < activate.index("verify_topology")

    downstream = text[
        text.index("ROLLOUT_DOWNSTREAM_UNITS=(") : text.index("ROLLOUT_STOPPED=0")
    ]
    for unit in (
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-bybit-long-mainnet.service",
        "liquidity-migration-mainnet-liveness.timer",
        "liquidity-migration-mainnet-liveness.service",
    ):
        assert unit in downstream.split("ROLLOUT_OWNER_UNITS=(")[0], unit
    owners = downstream.split("ROLLOUT_OWNER_UNITS=(")[1]
    assert "liquidity-migration-account-execution-mainnet.service" in owners

    # The rollout that first installs one of these units must still be able to
    # restore the prior topology from the cleanup path.
    cleanup = text[
        text.index("stop_all_rollout_units_best_effort()") : text.index("cleanup_notice()")
    ]
    assert 'systemctl cat "$unit" >/dev/null 2>&1 || continue' in cleanup


def test_demo_rule_refresh_freezes_the_demo_realm_explicitly() -> None:
    text = _read(DEPLOY)
    refresh = text[
        text.index("refresh_stale_demo_rules_if_requested()") : text.index("install_mode()")
    ]
    assert "freeze_account_candidate_universe.py \\\n        --realm demo --output" in refresh


def test_deploy_rejects_an_unknown_mode_and_a_stray_mode_argument() -> None:
    for argv, expected in (
        (["activate-mainnnet"], "invalid deploy mode"),
        (["stop"], "invalid deploy mode"),
        (["activate-mainnet", "--profile", "operational"], "activate-mainnet retired"),
    ):
        result = subprocess.run(
            ["bash", str(DEPLOY), *argv], capture_output=True, text=True, check=False
        )
        assert result.returncode == 2, (argv, result.stdout, result.stderr)
        assert expected in result.stderr, (argv, result.stderr)
    usage = subprocess.run(
        ["bash", str(DEPLOY), "verify", "--extra"], capture_output=True, text=True, check=False
    ).stderr
    for mode in (
        "install",
        "activate",
        "verify",
        "staged",
        "rollout",
        "stop-mainnet",
    ):
        assert mode in usage, mode
    assert "activate-mainnet" not in usage
    # Every flag is scoped to the modes that read it, and the usage says so.
    for flag in ("--profile", "--stop-first", "--require-flat", "--refresh-demo-rules"):
        assert flag in usage, flag
    for argv in (
        ["verify", "--stop-first"],
        ["install", "--profile", "operational"],
        ["install", "--require-flat"],
        ["rollout", "--refresh-demo-rules"],
    ):
        rejected = subprocess.run(
            ["bash", str(DEPLOY), *argv], capture_output=True, text=True, check=False
        )
        assert rejected.returncode == 2, argv
        assert f"is not a {argv[0]} argument" in rejected.stderr, argv
    for mode in ("staged", "rollout"):
        missing = subprocess.run(
            ["bash", str(DEPLOY), mode], capture_output=True, text=True, check=False
        )
        assert missing.returncode == 2, mode
        assert f"{mode} requires --profile" in missing.stderr, mode


def test_stop_mainnet_says_the_armed_switch_still_restarts_the_fleet() -> None:
    """`activate`/`rollout` restart the funded fleet from the arming switch, so a
    stop that leaves REAL_MONEY armed is undone by the next routine deploy.
    """

    result = subprocess.run(
        ["bash", "-c", _mainnet_harness("armed", 0) + "\nstop_mainnet_mode\n"],
        check=True,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert "the next activate or rollout restarts this fleet" in combined
    assert "Set REAL_MONEY=false in /etc/liquidity-migration/bybit-mainnet.env" in combined


def _verify_harness(
    mode: str,
    active: str,
    enabled: str,
    failed: str = "",
    permissions_status: int = 0,
    engine_root: Path | None = None,
) -> str:
    """The real verify_topology over a stub systemd, so accumulation is exercised
    rather than pattern-matched. Every sleeve off is the smallest topology that
    still covers on-expectations, off-expectations, the
    oneshot sweep and the venue probe.

    ``engine_root``: where the engine's binary and environment file live. None
    points both at a path that does not exist, which is what almost every host
    looks like.
    """

    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")
    if engine_root is None:
        engine_binary = "/nonexistent/liquidity-migration-engine/bin/engine"
        engine_environment = "/nonexistent/liquidity-migration/engine.env"
    else:
        engine_binary = str(engine_root / "engine")
        engine_environment = str(engine_root / "engine.env")
    engine_override = (
        f"\nENGINE_BINARY={shlex.quote(engine_binary)}\n"
        f"ENGINE_ENVIRONMENT={shlex.quote(engine_environment)}\n"
    )
    return (
        "set -u\n"
        f"MODE={mode}\n"
        "AUTH_PROFILE=operational\n"
        "PYTHON=fake_python\n"
        "EXPECTED_COMMIT=abc123\n"
        "EXPECTED_COMMIT_EXPLICIT=1\n"
        "LONG_SLEEVE=off\nCARRY_SLEEVE=off\n"
        'mainnet_armed() { [ "${MAINNET_ARMED_STATE:-off}" = armed ]; }\n'
        f"ACTIVE_UNITS={active!r}\nENABLED_UNITS={enabled!r}\nFAILED_UNITS={failed!r}\n"
        f"PERMISSIONS_STATUS={permissions_status}\n"
        'fail() { echo "fail:$*" >&2; exit 1; }\n'
        'safe_git() { echo "$EXPECTED_COMMIT"; }\n'
        'fake_python() { return 0; }\n'
        "check_demo_order_permissions() { return \"$PERMISSIONS_STATUS\"; }\n"
        "systemctl() {\n"
        '    local verb="$1" unit\n'
        "    shift\n"
        '    [ "${1:-}" != --quiet ] || shift\n'
        '    unit="${1:-}"\n'
        '    case "$verb" in\n'
        "        is-active)\n"
        '            case " $ACTIVE_UNITS " in *" $unit "*) echo active; return 0 ;; esac\n'
        "            echo inactive; return 3 ;;\n"
        "        is-enabled)\n"
        '            case " $ENABLED_UNITS " in *" $unit "*) echo enabled; return 0 ;; esac\n'
        "            echo disabled; return 1 ;;\n"
        "        is-failed)\n"
        '            case " $FAILED_UNITS " in *" $unit "*) echo failed; return 0 ;; esac\n'
        "            echo active; return 1 ;;\n"
        "    esac\n"
        "    return 0\n"
        "}\n"
        + library[library.index("sleeve_on() {") : library.index("lm_write_resolved_sleeve_toggles()")]
        + text[text.index("unit_on() {") : text.index("check_demo_order_permissions() {")]
        + text[text.index("verify_topology()") : text.index("start_if()")]
        # After the slice that defines them, so the harness decides where this
        # host keeps the engine rather than reading the deploy's real paths.
        + engine_override
    )


_VERIFY_GREEN = (
        "liquidity-migration-demo-liveness.timer "
    "liquidity-migration-telegram-controls.service"
)


def test_verify_reports_every_mismatch_with_a_unit_table_not_just_the_first() -> None:
    """Failing on the first mismatch made every check after it invisible: the operator
    fixed one line, re-ran, and found the next. One run now names them all.
    """

    green = subprocess.run(
        ["bash", "-c", _verify_harness("verify", _VERIFY_GREEN, _VERIFY_GREEN) + "\nverify_topology\n"],
        capture_output=True,
        text=True,
    )
    combined = green.stdout + green.stderr
    assert green.returncode == 0, combined
    assert "verify-ok commit=abc123" in combined
    assert "verify-units unit|expected|active|enabled" in combined
    # Units that pass are in the table too, or the table is only a failure list.
    assert (
        "liquidity-migration-bybit-carry-demo.service|off|inactive|disabled" in combined
    )
    assert "verify-mismatch" not in combined

    # Owner down, liveness timer down, controls daemon down, one failed
    # oneshot, and the venue probe refusing: five independent findings from a
    # single run.
    broken = subprocess.run(
        [
            "bash",
            "-c",
            _verify_harness(
                "activate",
                "",
                "",
                failed="liquidity-migration-demo-liveness.service",
                permissions_status=1,
            )
            + "\nverify_topology\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = broken.stdout + broken.stderr
    assert broken.returncode != 0
    for finding in (
        "verify-mismatch liveness timer is not active",
        "verify-mismatch telegram controls daemon is not active",
        "verify-mismatch liquidity-migration-demo-liveness.service is failed",
        "verify-mismatch demo order permission verification failed",
    ):
        assert finding in combined, finding
    assert "found 4 mismatch(es)" in combined
    assert "verify-ok" not in combined
    # The table is printed before the verdict, so it survives the failure.
    assert combined.index("verify-units") < combined.index("fail:topology verification")


def test_the_live_venue_probes_report_in_verify_and_still_gate_every_other_mode() -> None:
    """`verify` is the read-only status command an operator runs to find out what is
    wrong; a venue probe that cannot answer must not be what stops it printing the
    topology. Every mutating mode still treats the same probe as fatal.
    """

    reporting = subprocess.run(
        [
            "bash",
            "-c",
            _verify_harness("verify", _VERIFY_GREEN, _VERIFY_GREEN, permissions_status=1)
            + "\nverify_topology\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = reporting.stdout + reporting.stderr
    assert reporting.returncode == 0, combined
    assert "verify-warn demo-order-permissions" in combined
    assert "verify-ok" in combined

    for mode in ("activate", "rollout"):
        gating = subprocess.run(
            [
                "bash",
                "-c",
                _verify_harness(mode, _VERIFY_GREEN, _VERIFY_GREEN, permissions_status=1)
                + "\nverify_topology\n",
            ],
            capture_output=True,
            text=True,
        )
        combined = gating.stdout + gating.stderr
        assert gating.returncode != 0, mode
        assert "verify-mismatch demo order permission verification failed" in combined


def test_verify_reports_commit_drift_as_information_only_when_it_was_defaulted() -> None:
    harness = _verify_harness("verify", _VERIFY_GREEN, _VERIFY_GREEN).replace(
        'safe_git() { echo "$EXPECTED_COMMIT"; }', "safe_git() { echo other-commit; }"
    )
    explicit = subprocess.run(
        ["bash", "-c", harness + "\nverify_topology\n"], capture_output=True, text=True
    )
    combined = explicit.stdout + explicit.stderr
    assert explicit.returncode != 0
    assert "verify-mismatch installed checkout is other-commit" in combined

    defaulted = subprocess.run(
        [
            "bash",
            "-c",
            harness.replace("EXPECTED_COMMIT_EXPLICIT=1", "EXPECTED_COMMIT_EXPLICIT=0")
            + "\nverify_topology\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = defaulted.stdout + defaulted.stderr
    assert defaulted.returncode == 0, combined
    assert "verify-drift installed=other-commit expected=abc123" in combined
    assert "verify-ok commit=other-commit" in combined


def _quiescence_harness(
    tmp_path: Path, stop_first: str, mainnet: str, running: str = "demo"
) -> str:
    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "units"
    lines = (
        "liquidity-migration-bybit-carry-demo.service loaded active running carry\n"
        "liquidity-migration-account-execution.service loaded active running owner\n"
    )
    if running == "with-mainnet":
        lines += (
            "liquidity-migration-account-execution-mainnet.service loaded active running owner\n"
        )
    if running == "with-engine":
        lines += "liquidity-migration-engine.service loaded active running engine\n"
    state.write_text(lines, encoding="utf-8")
    return (
        "set -u\n"
        f"STATE_FILE={state}\n"
        f"STOP_FIRST={stop_first}\n"
        'fail() { echo "fail:$*" >&2; exit 1; }\n'
        "systemctl() {\n"
        '    case "$1" in\n'
        '        list-units) cat "$STATE_FILE"; return 0 ;;\n'
        "        stop)\n"
        '            printf "stopped:%s\\n" "$2"\n'
        '            grep -v "^$2 " "$STATE_FILE" > "$STATE_FILE.tmp" || true\n'
        '            mv "$STATE_FILE.tmp" "$STATE_FILE"\n'
        "            return 0 ;;\n"
        "        is-active)\n"
        '            grep -q "^${2#--quiet } " "$STATE_FILE"\n'
        "            return $? ;;\n"
        "    esac\n"
        "    return 0\n"
        "}\n"
        + library[library.index("sleeve_on() {") : library.index("lm_write_resolved_sleeve_toggles()")]
        + text[text.index("# The single arming switch") : text.index("# verify_topology collects")]
        + text[text.index("running_liqmig_units() {") : text.index("git_fetch() {")]
        + text[
            text.index("ROLLOUT_DOWNSTREAM_UNITS=(") : text.index(
                "stop_all_rollout_units_best_effort()"
            )
        ]
        + ("MAINNET_ARMED_STATE=armed\n" if mainnet == "on" else "MAINNET_ARMED_STATE=off\n")
    )


def test_stop_first_stops_a_demo_fleet_and_refuses_a_funded_one(
    tmp_path: Path,
) -> None:
    """A demo install that found the fleet running used to print a list and quit,
    leaving the operator to stop it by hand. It stops it now -- except when a funded
    sleeve is on, where the refusal is the whole point.
    """

    demo = subprocess.run(
        [
            "bash",
            "-c",
            _quiescence_harness(tmp_path / "demo", "auto", "off")
            + "\nresolve_stop_first\nrequire_quiescent\necho quiescent-ok\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = demo.stdout + demo.stderr
    assert demo.returncode == 0, combined
    assert "stop-first: stopping the running fleet" in combined
    assert "stopped:liquidity-migration-bybit-carry-demo.service" in combined
    assert "stopped:liquidity-migration-account-execution.service" in combined
    # Producers stop before owners. On the demo fleet that order comes from the
    # stop list itself, not from a Requires= (see
    # test_demo_producers_are_ordered_after_the_owner_but_never_taken_down_with_it).
    assert combined.index("stopped:liquidity-migration-bybit-carry-demo.service") < combined.index(
        "stopped:liquidity-migration-account-execution.service"
    )
    assert "quiescent-ok" in combined

    # Armed switch, but nothing funded is running: the demo fleet keeps its
    # normal auto-cycle — this exact refusal burned a live arming attempt.
    armed_demo_only = subprocess.run(
        [
            "bash",
            "-c",
            _quiescence_harness(tmp_path / "armed_demo", "auto", "on")
            + "\nresolve_stop_first\nrequire_quiescent\necho quiescent-ok\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = armed_demo_only.stdout + armed_demo_only.stderr
    assert armed_demo_only.returncode == 0, combined
    assert "quiescent-ok" in combined

    # A running funded unit is the one thing never stopped automatically.
    funded = subprocess.run(
        [
            "bash",
            "-c",
            _quiescence_harness(tmp_path / "funded", "auto", "on", running="with-mainnet")
            + "\nresolve_stop_first\nrequire_quiescent\necho quiescent-ok\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = funded.stdout + funded.stderr
    assert funded.returncode == 1
    assert "quiesce these units first" in combined
    assert "stopped:" not in combined
    assert "quiescent-ok" not in combined

    explicit_off = subprocess.run(
        [
            "bash",
            "-c",
            _quiescence_harness(tmp_path / "explicit", "0", "off")
            + "\nresolve_stop_first\nrequire_quiescent\necho quiescent-ok\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = explicit_off.stdout + explicit_off.stderr
    assert explicit_off.returncode == 1
    assert "quiesce these units first" in combined


def test_staged_installs_records_the_profile_then_activates_in_one_session() -> None:
    """Only rollout used to write the profile marker, so a staged install left
    load_authorization falling back to `operational` whatever the operator asked for.
    """

    text = _read(DEPLOY)
    harness = (
        "set -Eeuo pipefail\n"
        "DEPLOY_PROFILE=operational\n"
        "EXPECTED_COMMIT=abc123\n"
        + text[text.index("fail() {") : text.index("run_phase_pair() {")]
        + 'install_mode() { echo "ran:install"; }\n'
        'record_installed_profile() { echo "ran:profile-marker=$DEPLOY_PROFILE"; }\n'
        'activate_mode() { echo "ran:activate"; }\n'
        + 'build_engine() { echo "ran:engine-build"; }\n'
        + text[text.index("staged_mode() {") : text.index("# A funded fleet keeps")]
        + "\nstaged_mode\n"
    )
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert combined.index("ran:install") < combined.index("ran:profile-marker=operational")
    assert combined.index("ran:profile-marker=operational") < combined.index("ran:activate")
    # The engine is built after the fleet is up and verified, never before.
    assert combined.index("ran:activate") < combined.index("ran:engine-build")
    assert "staged-ok commit=abc123 profile=operational" in combined
    # Each step is a strict phase, so a failure aborts rather than continuing to
    # activate a half-installed checkout.
    failing = subprocess.run(
        ["bash", "-c", harness.replace('install_mode() { echo "ran:install"; }', "install_mode() { false; }")],
        capture_output=True,
        text=True,
    )
    combined = failing.stdout + failing.stderr
    assert failing.returncode != 0
    assert "phase-failed name=staged-install" in combined
    assert "ran:activate" not in combined
    assert "ran:engine-build" not in combined
    assert "staged-ok" not in combined


def test_rollout_gates_on_a_flat_account_for_a_funded_fleet_and_reports_for_a_demo_one() -> None:
    """The hard gate is what made rollout unusable on the demo fleet: a single residual
    stopped the deploy and its rollback machinery with it. It still gates the funded
    fleet, and --require-flat asks for the same gate anywhere.
    """

    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")

    def harness(require_flat: str, mainnet: str) -> str:
        return (
            "set -Eeuo pipefail\n"
            f"REQUIRE_FLAT={require_flat}\n"
            + text[text.index("fail() {") : text.index("run_phase_pair() {")]
            + library[
                library.index("sleeve_on() {") : library.index("lm_write_resolved_sleeve_toggles()")
            ]
            + text[
                text.index("# The single arming switch") : text.index("# verify_topology collects")
            ]
            + text[text.index("rollout_flat_required() {") : text.index("rollout_mode() {")]
            + ("MAINNET_ARMED_STATE=armed\n" if mainnet == "on" else "MAINNET_ARMED_STATE=off\n")
            + "\nresidual() { return 4; }\n"
            "rollout_flat_phase pre-stop-flat-account-proof residual\n"
            "echo reached-the-stop\n"
        )

    demo = subprocess.run(["bash", "-c", harness("0", "off")], capture_output=True, text=True)
    combined = demo.stdout + demo.stderr
    assert demo.returncode == 0, combined
    assert "rollout-flat-warn phase=pre-stop-flat-account-proof status=4" in combined
    assert "reached-the-stop" in combined

    for require_flat, mainnet in (("0", "on"), ("1", "off")):
        gated = subprocess.run(
            ["bash", "-c", harness(require_flat, mainnet)], capture_output=True, text=True
        )
        combined = gated.stdout + gated.stderr
        assert gated.returncode != 0, (require_flat, mainnet)
        assert "phase-failed name=pre-stop-flat-account-proof" in combined
        assert "reached-the-stop" not in combined


def test_stop_mainnet_cannot_report_ok_without_systemctl() -> None:
    harness = _mainnet_harness("armed", 0).replace("systemctl() {\n", "unused() {\n", 1)
    result = subprocess.run(
        ["bash", "-c", "PATH=/nonexistent\n" + harness + "\nstop_mainnet_mode\n"],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "systemctl is unavailable" in combined
    assert "stop-mainnet-ok" not in combined


def test_mainnet_provision_runs_before_a_blocked_preflight_gate() -> None:
    """A blocked preflight still blocks every unit start, and provisioning ran
    first so the report the operator sees reflects the provisioned state."""
    blocked = subprocess.run(
        ["bash", "-c", _mainnet_harness("armed", 1) + "\nstart_mainnet_fleet\n"],
        capture_output=True,
        text=True,
    )
    combined = blocked.stdout + blocked.stderr
    assert blocked.returncode != 0
    assert "systemctl:enable" not in combined
    assert "systemctl:start" not in combined
    assert combined.index("render-profile") < combined.index("preflight")


def _engine_template() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read("deploy/engine.env.template").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, line
        values[key] = value
    return values


def test_the_engines_heartbeat_reaches_the_fleets_watchdog() -> None:
    """Two halves that never meet is the failure this guards: the engine writes
    a file nobody reads, or the watchdog pages about a file nobody writes.
    """

    template = _engine_template()
    watchdog = _read("scripts/runtime/check_fleet_liveness.py")
    variable = "LIVENESS_ENGINE_HEARTBEAT_FILE"

    # The name the watchdog reads is the name the template writes.
    assert f'os.environ.get("{variable}")' in watchdog
    assert variable in template

    # And a unit carries it from one to the other. The leading `-` is what
    # keeps a host without an engine starting its watchdog at all.
    liveness = _section(_unit("liquidity-migration-demo-liveness.service"), "Service")
    assert "EnvironmentFile=-/etc/liquidity-migration/engine.env" in liveness

    # The heartbeat lands where the engine is allowed to write.
    engine = _section(_unit(ENGINE_UNIT), "Service")
    writable = next(
        line.removeprefix("ReadWritePaths=") for line in engine.splitlines()
        if line.startswith("ReadWritePaths=")
    )
    assert template[variable].startswith(writable + "/"), template[variable]

    # The switch and the config path the unit's wrapper needs are here too.
    assert template["ENGINE_CONFIG_FILE"].startswith("/")
    assert template["ENGINE_LIVE"] == "false"


def test_verify_asks_for_the_engine_only_where_the_engine_is_installed(
    tmp_path: Path,
) -> None:
    """The manifest installs the engine's unit file on every host. If that alone
    made it expected, every ops.sh status and every deploy — the funded fleet's
    included — would fail on a box that has never built one.
    """

    absent = subprocess.run(
        ["bash", "-c", _verify_harness("verify", _VERIFY_GREEN, _VERIFY_GREEN) + "\nverify_topology\n"],
        capture_output=True,
        text=True,
    )
    combined = absent.stdout + absent.stderr
    assert absent.returncode == 0, combined
    assert "verify-ok" in combined
    assert ENGINE_UNIT not in combined

    installed = tmp_path / "engine-host"
    installed.mkdir()
    (installed / "engine").write_text("#!/bin/sh\n", encoding="utf-8")
    (installed / "engine").chmod(0o755)
    (installed / "engine.env").write_text("ENGINE_LIVE=false\n", encoding="utf-8")

    stopped = subprocess.run(
        [
            "bash",
            "-c",
            _verify_harness("activate", _VERIFY_GREEN, _VERIFY_GREEN, engine_root=installed)
            + "\nverify_topology\n",
        ],
        capture_output=True,
        text=True,
    )
    # Reported, never a mismatch. Every other unit in this table carries
    # orders; the engine carries none — it trades a demo account of its own.
    # A rollout that read its silence as a failure would reach the cleanup
    # that stops the whole funded fleet, so a bad engine.toml or an expired
    # demo credential would take the funded account down with it.
    combined = stopped.stdout + stopped.stderr
    assert stopped.returncode == 0, combined
    assert "engine-not-running" in combined
    assert "not a deploy failure" in combined
    assert "verify-mismatch" not in combined
    assert "verify-ok" in combined
    # The operator still sees it in the table rather than having to know to look.
    assert f"{ENGINE_UNIT}|on(reported)|inactive" in combined

    running = f"{_VERIFY_GREEN} {ENGINE_UNIT}"
    green = subprocess.run(
        [
            "bash",
            "-c",
            _verify_harness("activate", running, running, engine_root=installed)
            + "\nverify_topology\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = green.stdout + green.stderr
    assert green.returncode == 0, combined
    assert f"{ENGINE_UNIT}|on(reported)|active|enabled" in combined
    assert "verify-ok" in combined

    # A built binary with no environment file is a host that never asked for
    # the engine: nothing to verify.
    (installed / "engine.env").unlink()
    unasked = subprocess.run(
        [
            "bash",
            "-c",
            _verify_harness("activate", _VERIFY_GREEN, _VERIFY_GREEN, engine_root=installed)
            + "\nverify_topology\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = unasked.stdout + unasked.stderr
    assert unasked.returncode == 0, combined
    assert ENGINE_UNIT not in combined


def _activate_harness(engine_root: Path | None, start_status: int = 0) -> str:
    """The real activate_mode over stubs, so what it starts is exercised rather
    than pattern-matched.
    """

    text = _read(DEPLOY)
    if engine_root is None:
        binary = "/nonexistent/engine"
        environment = "/nonexistent/engine.env"
    else:
        binary = str(engine_root / "engine")
        environment = str(engine_root / "engine.env")
    return (
        "set -Eeuo pipefail\n"
        f"ENGINE_START_STATUS={start_status}\n"
        "LONG_SLEEVE=off\nCARRY_SLEEVE=off\n"
        'fail() { echo "fail:$*" >&2; exit 1; }\n'
        "load_authorization() { :; }\n"
        "resolve_stop_first() { :; }\n"
        "require_quiescent() { :; }\n"
        "check_demo_order_permissions() { :; }\n"
        "lm_expected_systemd_units() { :; }\n"
        "sleeve_on() { return 1; }\n"
        "mainnet_armed() { return 1; }\n"
        "start_if() { :; }\n"
        'verify_topology() { echo "ran:verify"; }\n'
        "systemctl() {\n"
        '    printf "systemctl:%s:%s\\n" "$1" "${2:-}"\n'
        f'    if [ "${{2:-}}" = {ENGINE_UNIT} ] && [ "$1" = start ]; then\n'
        '        return "$ENGINE_START_STATUS"\n'
        "    fi\n"
        "    return 0\n"
        "}\n"
        + text[text.index("# The Rust execution engine.") : text.index("# The single arming switch")]
        + text[text.index("activate_mode() {") : text.index("MAINNET_OWNER_UNIT=")]
        + f"ENGINE_BINARY={shlex.quote(binary)}\n"
        f"ENGINE_ENVIRONMENT={shlex.quote(environment)}\n"
        "activate_mode\n"
    )


def test_activation_starts_the_engine_where_it_is_installed_and_never_dies_on_it(
    tmp_path: Path,
) -> None:
    """activate disables every unit in the manifest before starting the fleet,
    so a host that runs the engine needs it started again here. A start that
    will not take is the verification's finding to report — aborting here would,
    in a rollout, reach the rollback trap and stop the fleet a second time.
    """

    absent = subprocess.run(
        ["bash", "-c", _activate_harness(None)], capture_output=True, text=True
    )
    combined = absent.stdout + absent.stderr
    assert absent.returncode == 0, combined
    assert f"systemctl:start:{ENGINE_UNIT}" not in combined
    assert "ran:verify" in combined

    installed = tmp_path / "engine-host"
    installed.mkdir()
    (installed / "engine").write_text("#!/bin/sh\n", encoding="utf-8")
    (installed / "engine").chmod(0o755)
    (installed / "engine.env").write_text("ENGINE_LIVE=false\n", encoding="utf-8")

    started = subprocess.run(
        ["bash", "-c", _activate_harness(installed)], capture_output=True, text=True
    )
    combined = started.stdout + started.stderr
    assert started.returncode == 0, combined
    assert f"systemctl:enable:{ENGINE_UNIT}" in combined
    assert f"systemctl:start:{ENGINE_UNIT}" in combined
    # After the fleet, and before the verification that reports on it.
    assert combined.index(f"systemctl:start:{ENGINE_UNIT}") < combined.index("ran:verify")

    refusing = subprocess.run(
        ["bash", "-c", _activate_harness(installed, start_status=1)],
        capture_output=True,
        text=True,
    )
    combined = refusing.stdout + refusing.stderr
    assert refusing.returncode == 0, combined
    assert "engine-start-failed" in combined
    assert "ran:verify" in combined


def _build_engine_harness(tmp_path: Path, cargo_status: int | None) -> str:
    """The real build_engine with a stub cargo. ``cargo_status`` None means no
    toolchain on this host at all.
    """

    text = _read(DEPLOY)
    toolchain = tmp_path / "rust"
    if cargo_status is not None:
        (toolchain / "cargo" / "bin").mkdir(parents=True)
        cargo = toolchain / "cargo" / "bin" / "cargo"
        cargo.write_text(
            f'#!/bin/sh\necho "cargo $*"\nexit {cargo_status}\n', encoding="utf-8"
        )
        cargo.chmod(0o755)
    build = tmp_path / "engine-build"
    (build / "engine" / "target" / "release").mkdir(parents=True)
    (build / "engine" / "target" / "release" / "engine").write_text("#!/bin/sh\n", encoding="utf-8")
    (build / ".git").mkdir()
    return (
        "set -Eeuo pipefail\n"
        'safe_git() { echo deadbeef; }\n'
        # The build clone's own Git, stubbed: what it syncs is not what this
        # test is about, but where it points is.
        "engine_git() {\n"
        '    if [ "${1:-}" = rev-parse ]; then echo deadbeef; return 0; fi\n'
        '    printf "engine_git:%s\\n" "$*"\n'
        "}\n"
        'systemctl() { printf "systemctl:%s:%s\\n" "$1" "${2:-}"; return 0; }\n'
        # The deploy runs as root and this test does not, so the ownership
        # flags are dropped. Everything else about the install is real.
        "install() {\n"
        "    local argument skip=0 kept=()\n"
        '    for argument in "$@"; do\n'
        '        if [ "$skip" = 1 ]; then skip=0; continue; fi\n'
        '        case "$argument" in\n'
        "            -o|-g) skip=1 ;;\n"
        '            *) kept+=("$argument") ;;\n'
        "        esac\n"
        "    done\n"
        '    command install "${kept[@]}"\n'
        "}\n"
        + text[text.index("fail() {") : text.index("run_phase_pair() {")]
        + text[text.index("# The Rust execution engine.") : text.index("# The single arming switch")]
        + text[text.index("build_engine() {") : text.index("activate_mode() {")]
        # After the slice that defines them: this test decides where the
        # toolchain, the build clone and the binary live.
        + f"REPO_DIR={shlex.quote(str(tmp_path / 'checkout'))}\n"
        + f"ENGINE_TOOLCHAIN_DIR={shlex.quote(str(toolchain))}\n"
        + f"ENGINE_BUILD_DIR={shlex.quote(str(build))}\n"
        + f"ENGINE_BINARY={shlex.quote(str(tmp_path / 'installed' / 'bin' / 'engine'))}\n"
        + f"ENGINE_ENVIRONMENT={shlex.quote(str(tmp_path / 'engine.env'))}\n"
        + "run_phase engine-build build_engine\n"
        "echo deploy-continued\n"
    )


def test_a_failed_engine_build_cannot_fail_the_deploy(tmp_path: Path) -> None:
    """The engine trades a demo account of its own and none of the fleet's work.
    A toolchain that is missing, or code that will not compile, must leave the
    deploy of the funded fleet exactly as it is today.
    """

    (tmp_path / "engine.env").write_text("ENGINE_LIVE=false\n", encoding="utf-8")

    failing = subprocess.run(
        ["bash", "-c", _build_engine_harness(tmp_path / "broken", cargo_status=1)],
        capture_output=True,
        text=True,
    )
    combined = failing.stdout + failing.stderr
    assert failing.returncode == 0, combined
    assert "engine-build-failed status=1" in combined
    assert "phase-ok name=engine-build" in combined
    assert "deploy-continued" in combined
    # Nothing was installed and nothing was restarted onto a build that failed.
    assert "systemctl:restart" not in combined
    assert not (tmp_path / "broken" / "installed" / "bin" / "engine").exists()

    missing = subprocess.run(
        ["bash", "-c", _build_engine_harness(tmp_path / "no-rust", cargo_status=None)],
        capture_output=True,
        text=True,
    )
    combined = missing.stdout + missing.stderr
    assert missing.returncode == 0, combined
    assert "engine-build-skipped reason=no-toolchain" in combined
    assert "deploy-continued" in combined


def test_a_good_engine_build_installs_the_binary_and_restarts_only_that_unit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "good"
    (root / "engine.env").parent.mkdir(parents=True, exist_ok=True)
    harness = _build_engine_harness(root, cargo_status=0)
    (root / "engine.env").write_text("ENGINE_LIVE=false\n", encoding="utf-8")

    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "engine-ok commit=deadbeef" in combined
    installed = root / "installed" / "bin" / "engine"
    assert installed.exists()
    # Renamed into place, so a half-copied file is never at the path the unit
    # starts.
    assert not (root / "installed" / "bin" / "engine.new").exists()
    assert f"systemctl:restart:{ENGINE_UNIT}" in combined
    # Only its own unit. Nothing here touches the fleet.
    assert combined.count("systemctl:restart") == 1
    assert "account-execution" not in combined

    # Built in a clone of its own, never the deployed checkout: cargo writes a
    # target/ tree beside the source, and an untracked file in the deployed
    # checkout stops the next deploy.
    assert f"engine_git:fetch --no-tags --quiet {root / 'checkout'} HEAD" in combined
    assert "engine_git:reset --hard --quiet FETCH_HEAD" in combined

    # No environment file means the host never asked for the engine: the build
    # still runs and installs, and nothing is started.
    (root / "engine.env").unlink()
    unasked = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    combined = unasked.stdout + unasked.stderr
    assert unasked.returncode == 0, combined
    assert "engine-start-skipped reason=no-environment-file" in combined
    assert "systemctl:restart" not in combined


def test_a_running_engine_does_not_stop_the_next_deploy(tmp_path: Path) -> None:
    """require_quiescent refuses to install while ANY liquidity-migration-*
    unit is running, and stop-first only stops what the rollout lists name.
    Leaving the engine out of them would stop every deploy dead, the funded
    fleet's included.
    """

    result = subprocess.run(
        [
            "bash",
            "-c",
            _quiescence_harness(tmp_path / "engine", "auto", "off", running="with-engine")
            + "\nresolve_stop_first\nrequire_quiescent\necho quiescent-ok\n",
        ],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert f"stopped:{ENGINE_UNIT}" in combined
    assert "quiescent-ok" in combined
    assert "units still running after stop-first" not in combined
