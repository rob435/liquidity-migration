from __future__ import annotations

import re
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
        ROOT / "scripts" / "runtime" / "run_account_execution_service.sh",
        ROOT / "scripts" / "runtime" / "run_bybit_long_demo_event_engine.sh",
        ROOT / "scripts" / "runtime" / "run_bybit_continuous_demo_event_engine.sh",
        ROOT / "scripts" / "runtime" / "run_bybit_carry_demo_event_engine.sh",
        ROOT / "scripts" / "runtime" / "run_continuous_hedge.sh",
        ROOT / "scripts" / "runtime" / "run_continuous_rmom_refresh.sh",
        ROOT / "scripts" / "maintain" / "reset_demo_ledgers.sh",
    ]
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)
    for path in scripts[:6] + scripts[8:]:
        assert path.stat().st_mode & stat.S_IXUSR
    assert (ROOT / "scripts" / "vps" / "check_deploy_rollout_readiness.py").stat().st_mode & stat.S_IXUSR


def test_authorized_wrapper_owns_every_runtime_argv() -> None:
    wrapper = _read(WRAPPER)
    assert 'if [ "$#" -ne 2 ]' in wrapper
    for unit in AUTHORIZED_UNITS:
        assert f"{unit}:main" in wrapper
        fragment = _unit(unit)
        assert f"run_authorized_runtime.sh {unit} main" in fragment
    # The readiness entrypoint stays registered for manual and verify use even
    # where it no longer gates startup.
    for owner in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-execution-mainnet.service",
    ):
        assert f"{owner}:readiness" in wrapper
    # Only this owner still runs it as an ExecStartPost startup gate.
    for gated in (
        "liquidity-migration-account-execution-mainnet.service",
    ):
        assert f"run_authorized_runtime.sh {gated} readiness" in _unit(gated)


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
    # The mainnet owner is the one deliberate carrier of the real pair; every
    # other guarded unit strips it, in the directive rather than a comment.
    for unit in AUTHORIZED_UNITS:
        if unit == "liquidity-migration-account-execution-mainnet.service":
            continue
        unset = " ".join(
            line for line in _unit(unit).splitlines() if line.startswith("UnsetEnvironment=")
        )
        assert "BYBIT_REAL_API_KEY" in unset, unit
        assert "BYBIT_REAL_API_SECRET" in unset, unit


def test_persistent_demo_workers_have_small_box_memory_limits() -> None:
    """carry/long are MemoryMax-only since the 2026-08-03 retune: both ran
    pinned at their MemoryHigh watermark for weeks (reclaim throttling, slow
    cycles), and a restart off the journal cursor beats silently stale cycles.
    """
    expected = {
        "liquidity-migration-bybit-long-demo.service": ("1024M", "384M"),
        "liquidity-migration-bybit-carry-demo.service": ("1152M", "384M"),
    }
    for unit, (maximum, swap) in expected.items():
        fragment = _unit(unit)
        assert "MemoryHigh=" not in fragment, unit
        assert f"MemoryMax={maximum}" in fragment
        assert f"MemorySwapMax={swap}" in fragment
    fragment = _unit("liquidity-migration-bybit-continuous-demo.service")
    assert "MemoryHigh=768M" in fragment
    assert "MemoryMax=896M" in fragment
    assert "MemorySwapMax=384M" in fragment


def test_demo_owner_is_bounded_but_never_reclaim_throttled() -> None:
    """MemoryHigh throttles rather than kills, and throttling the one latency-critical
    unit is what stretched venue round-trips into the 2026-08-01..03 stale-exposure
    wedge. MemoryMax already bounds the cgroup, so the high watermark is gone.
    """
    fragment = _unit("liquidity-migration-account-execution.service")
    assert "MemoryHigh=" not in fragment
    assert "MemoryMax=1024M" in fragment
    assert "MemorySwapMax=384M" in fragment


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


def test_producers_require_owner_readiness_and_never_hold_private_order_authority() -> None:
    mainnet_owner = "liquidity-migration-account-execution-mainnet.service"
    demo_owner = "liquidity-migration-account-execution.service"
    # A demo producer only ORDERS after its owner; it is not bound to the
    # owner's fate. See test_demo_producers_are_ordered_after_the_owner_but_
    # never_taken_down_with_it for why.
    ordered_only = {
        "liquidity-migration-bybit-long-demo.service": demo_owner,
        "liquidity-migration-bybit-continuous-demo.service": demo_owner,
        "liquidity-migration-bybit-carry-demo.service": demo_owner,
        "liquidity-migration-continuous-hedge.service": demo_owner,
    }
    pairs = {
        "liquidity-migration-bybit-long-mainnet.service": mainnet_owner,
        "liquidity-migration-bybit-carry-mainnet.service": mainnet_owner,
    }
    for producer, owner in {**ordered_only, **pairs}.items():
        fragment = _unit(producer)
        if producer in pairs:
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
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_continuous_demo_event_engine.sh",
        "scripts/runtime/run_continuous_hedge.sh",
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


def test_demo_producers_are_ordered_after_the_owner_but_never_taken_down_with_it() -> None:
    """``Requires=`` propagates a stop: when the demo owner failed on 2026-08-01 it took
    every producer down with it and kept them down for two days. Every invariant the
    hard dependency was standing in for is enforced at point of use — producers
    re-check owner health per cycle and plan entries as blocked while still publishing
    exits, queued entries self-expire, exits never expire, and order submission is
    gated inside the owner — so ordering is all the unit file needs to say.
    """
    owner = "liquidity-migration-account-execution.service"
    for producer in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
    ):
        directives = [line for line in _unit(producer).splitlines() if not line.startswith("#")]
        assert f"Requires={owner}" not in directives, producer
        assert f"Wants={owner}" in directives, producer
        assert owner in next(line for line in directives if line.startswith("After="))
        # A producer draining a cycle must not hold a restart for three minutes.
        assert "TimeoutStopSec=90" in directives, producer
        assert "TimeoutStopSec=180" not in directives, producer

    # The hedge publishes into a durable inbox and already degrades in-process on
    # dead-owner health; it declares no lifecycle dependency on the owner at all.
    hedge = [line for line in _unit("liquidity-migration-continuous-hedge.service").splitlines() if not line.startswith("#")]
    assert not [line for line in hedge if line.startswith("Requires=")]
    assert f"Wants={owner}" not in hedge
    assert owner in next(line for line in hedge if line.startswith("After="))


def test_demo_owner_startup_is_not_gated_and_its_restart_is_not_a_tight_loop() -> None:
    """The ExecStartPost readiness gate failed while the book was wedged and systemd
    killed a live owner that was still draining exits; RestartSec=2 plus the 180s gate
    made a ~182s cycle that never trips the default rate limiter. Removing the gate
    removes the kill; the slower RestartSec keeps a genuine crash loop visible in the
    journal rather than pegged.
    """
    fragment = _unit("liquidity-migration-account-execution.service")
    assert "ExecStartPost=" not in fragment
    # TimeoutStartSec existed only to bound that gate.
    assert "TimeoutStartSec=" not in fragment
    assert "Restart=always" in fragment
    assert "RestartSec=5" in fragment
    # No rate limiter: a limiter that trips leaves the owner stopped, which is
    # the outcome this whole change exists to prevent.
    assert "StartLimit" not in fragment
    # The mainnet owner is deliberately untouched: it gates, and it may.
    mainnet = _unit("liquidity-migration-account-execution-mainnet.service")
    assert (
        "ExecStartPost=/opt/liquidity-migration/scripts/run_authorized_runtime.sh "
        "liquidity-migration-account-execution-mainnet.service readiness"
    ) in mainnet
    assert "TimeoutStartSec=240" in mainnet
    assert "RestartSec=2" in mainnet


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


def test_owner_runner_degrades_rather_than_refusing_to_start() -> None:
    """A notification channel, and an unset diagnostic toggle, must never be able to
    keep the account owner down. Only the mainnet realm still re-checks the latch
    variables its own unit sets.
    """
    script = _read("scripts/runtime/run_account_execution_service.sh")

    # Misconfigured Telegram drops the flag and warns; it does not exit.
    telegram = script[script.index("telegram_args=()") : script.index("continuous_cycle_args=()")]
    assert "running without Telegram" in telegram
    assert "exit 2" not in telegram
    assert "--telegram" in telegram

    # Bulk raw-market persistence is a diagnostic; unset means off.
    assert 'ACCOUNT_RAW_MARKET_PERSISTENCE="${ACCOUNT_RAW_MARKET_PERSISTENCE:-0}"' in script
    assert "ACCOUNT_RAW_MARKET_PERSISTENCE must be explicitly set" not in script
    raw_market = script[script.index("case \"$ACCOUNT_RAW_MARKET_PERSISTENCE\"") :]
    raw_market = raw_market[: raw_market.index("esac")]
    assert "--no-persist-raw-market" in raw_market
    assert "exit 2" not in raw_market

    # The unit sets these two lines above the check; only mainnet re-reads them.
    realm_case = script[script.index('case "$ACCOUNT_VENUE_REALM" in\n    mainnet)') :]
    mainnet_arm = realm_case[: realm_case.index("    demo)")]
    demo_arm = realm_case[realm_case.index("    demo)") : realm_case.index("\nesac")]
    for variable in ("ACCOUNT_EXECUTION_KERNEL_REQUIRED", "CONFIRM_DEMO_ORDERS"):
        assert variable in mainnet_arm, variable
        assert variable not in demo_arm, variable
    # The mainnet credential/REAL_MONEY strip verification is unchanged.
    assert "ACCOUNT_VENUE_REALM=mainnet requires BYBIT_REAL_API_KEY and BYBIT_REAL_API_SECRET." in mainnet_arm
    assert "requires REAL_MONEY to be explicitly armed by the owner." in mainnet_arm
    assert "The demo owner must not receive mainnet credentials." in demo_arm

    # The Python runner accepts --confirm-demo-orders as a deprecated no-op, so
    # the wrapper stops passing a flag that decides nothing.
    assert "--confirm-demo-orders" not in script


def test_producer_runners_carry_no_kernel_latch_cross_product() -> None:
    """Two tri-state parsers with eight accepted spellings each, plus an
    EXECUTION_ENVIRONMENT x latch consistency matrix, only re-derived values the unit
    files hard-code. The demo Telegram refusals were the same shape and were a latent
    fleet-wide start failure: the two runners that had them would exit 2 on a variable
    the third ignores, and no runner passes --telegram either way.
    """
    for runner in (
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
        "scripts/runtime/run_bybit_continuous_demo_event_engine.sh",
        "scripts/runtime/run_continuous_hedge.sh",
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
    for runner in (
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
        "scripts/runtime/run_bybit_continuous_demo_event_engine.sh",
    ):
        text = _read(runner)
        assert (
            'if [[ -z "${ACCOUNT_INTENT_INBOX_ROOT:-}" || -z "${ACCOUNT_EXECUTION_ROOT:-}" ]]; then' in text
        ), runner
    # Both mainnet-capable producer runners keep their strip verification verbatim.
    for runner in (
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
    ):
        text = _read(runner)
        mainnet_arm = text[text.index("    mainnet)") : text.index("\n    *)\n        echo \"EXECUTION_ENVIRONMENT")]
        assert "A target producer must not receive venue credentials." in mainnet_arm, runner
        assert "A target producer must not receive REAL_MONEY; it submits no orders." in mainnet_arm, runner


def test_only_the_mainnet_owner_surface_check_asserts_an_exec_start_post() -> None:
    """``lm_verify_guarded_unit_surfaces`` would fail every verify against the demo
    owner now that it has no ExecStartPost. The ExecStart argv check still covers
    every guarded unit.
    """
    text = _read("deploy/lib_sleeves.sh")
    function = text[text.index("lm_verify_guarded_unit_surfaces() {") :]
    function = function[: function.index("\nlm_install_current_systemd_units")]
    post_case = function[function.index("--property=ExecStartPost") - 400 :]
    assert "liquidity-migration-account-execution-mainnet.service)" in post_case
    assert "liquidity-migration-account-execution.service |" not in post_case
    # Argv verification is unscoped and stays that way.
    assert 'run_authorized_runtime.sh $_lvgus_unit main ;"*) ;;' in function


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
        "MAX_ORDER_NOTIONAL_PCT_EQUITY",
        "MAX_NEW_ENTRIES_PER_CYCLE",
        "MAX_ACTIVE",
        "BTC_TREND_GATE",
        "PER_POSITION_NOTIONAL_PCT_EQUITY",
    )
    continuous_demo = _environment("liquidity-migration-bybit-continuous-demo.service")
    carry_demo = _environment("liquidity-migration-bybit-carry-demo.service")
    for environment in (long_demo, continuous_demo, carry_demo):
        assert set(environment).isdisjoint(sizing_keys)
    # Carry has no WS kline plane.
    assert carry_demo["WS_KLINES_ENABLED"] == "0"
    long_runner = _read("scripts/runtime/run_bybit_long_demo_event_engine.sh")
    continuous_runner = _read("scripts/runtime/run_bybit_continuous_demo_event_engine.sh")
    hedge_runner = _read("scripts/runtime/run_continuous_hedge.sh")
    for runner in (long_runner, continuous_runner, hedge_runner):
        assert 'ACCOUNT_RISK_POLICY_FILE' in runner
        assert '--operational-profile-file' in runner
    assert long_demo["EXECUTION_ENVIRONMENT"] == "demo"
    assert continuous_demo["EXECUTION_ENVIRONMENT"] == "demo"
    assert carry_demo["EXECUTION_ENVIRONMENT"] == "demo"


def test_demo_account_notification_reads_no_retired_continuous_status_root() -> None:
    # CONTINUOUS is retired. The status root is deliberately unset, which is what
    # removes the sleeve's line from the hourly digest; re-promotion must set it
    # explicitly again.
    demo_owner = _environment("liquidity-migration-account-execution.service")
    assert "CONTINUOUS_CYCLE_ROOT" not in demo_owner
    assert "CONTINUOUS_CYCLE_MAX_AGE_MINUTES" not in demo_owner


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
    assert "--continuous-root" in boundary
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
    assert activate.index("validate_hedge_model_prior") < activate.index("systemctl start")
    assert activate.index("account-execution.service") < activate.index("bybit-long-demo.service")
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
    assert 'RMOM_BOOTSTRAP_TIMEOUT_SECONDS="${RMOM_BOOTSTRAP_TIMEOUT_SECONDS:-300}"' in text
    assert 'RMOM_BOOTSTRAP_RETRY_SECONDS="${RMOM_BOOTSTRAP_RETRY_SECONDS:-10}"' in text
    assert "phase-start name=%s" in text
    assert "phase-ok name=%s elapsed_seconds=%s" in text
    assert "phase-group-start name=%s" in text
    assert "phase-group-ok name=%s elapsed_seconds=%s" in text
    assert 'if wait "$left_pid"; then left_status=0; else left_status=$?; fi' in text
    for phase in (
        "install-locked-dependencies",
        "demo-tree-preflight",
        "demo-tree-normalize",
        "seed-residual-momentum",
    ):
        assert phase in text

    seed = text[text.index("seed_rmom()") : text.index("activate_mode()")]
    gate_check = 'scripts/research/check_residual_momentum_gate.py --path "$gate_path"'
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
    assert "CONTINUOUS_HEDGE_TIMER" in writer
    deploy = _read(DEPLOY)
    assert "chown root:root /etc/liquidity-migration/sleeves.resolved.env" in deploy
    assert "chmod 0600 /etc/liquidity-migration/sleeves.resolved.env" in deploy
    rmom = _read("scripts/runtime/run_continuous_rmom_refresh.sh")
    assert "lm_load_sleeve_toggles" not in rmom
    assert "CONTINUOUS_SLEEVE is required" in rmom
    assert "CONTINUOUS_PAPER_DATA_ROOT" not in rmom
    assert rmom.count("precompute_residual_momentum.py") == 1
    rmom_unit = _unit("liquidity-migration-continuous-rmom-refresh.service")
    assert "EnvironmentFile=/etc/liquidity-migration/sleeves.resolved.env" in rmom_unit


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
        'validate_hedge_model_prior || fail',
        '--confirm-demo-probe \\\n            || fail',
    ):
        assert gate in text, gate
    # Inside verify_topology the same two probes route through verify_probe,
    # which records a mismatch (fatal at the end of any mode but read-only
    # `verify`) rather than swallowing a nonzero status.
    verify = text[text.index("verify_topology()") : text.index("start_if()")]
    assert (
        'verify_probe demo-order-permissions "demo order permission verification failed" \\\n'
        "        check_demo_order_permissions verify" in verify
    )
    assert (
        'verify_probe hedge-model-prior "hedge model prior validation failed" \\\n'
        "            validate_hedge_model_prior" in verify
    )
    probe = text[text.index("verify_probe() {") : text.index("verify_unit() {")]
    assert "verify_note" in probe
    assert 'if verify_report_only; then' in probe


def test_rollout_and_reset_survive_a_dying_ssh_transport() -> None:
    for script in (DEPLOY, ROOT / "scripts" / "maintain" / "reset_demo_ledgers.sh"):
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
        "liquidity-migration-mainnet-liveness.service": 120,
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


def test_account_owner_units_configure_no_retired_sleeve_cycle_root() -> None:
    """A retired sleeve must leave no cycle root behind, or the owners keep reading a
    dead sleeve's completion receipt and every hourly digest carries a permanently
    growing staleness line.
    """

    for unit in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-execution-mainnet.service",
    ):
        text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=CONTINUOUS_CYCLE_ROOT=" not in text, unit


def test_demo_owner_runner_passes_no_cycle_root_when_unset() -> None:
    runner = (ROOT / "scripts" / "runtime" / "run_account_execution_service.sh").read_text(encoding="utf-8")
    assert 'CONTINUOUS_CYCLE_ROOT="${CONTINUOUS_CYCLE_ROOT:-}"' in runner
    assert "data/bybit-continuous-demo-event" not in runner
    assert 'if [[ -n "$CONTINUOUS_CYCLE_ROOT" ]]; then' in runner


def _mainnet_harness(carry: str, long_: str, preflight_status: int) -> str:
    """The real mainnet functions over stub systemctl/python, so behavior is exercised
    rather than pattern-matched.
    """

    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")
    return (
        "set -u\n"
        f"PYTHON=fake_python\nCARRY_MAINNET_SLEEVE={carry}\nLONG_MAINNET_SLEEVE={long_}\n"
        f"PREFLIGHT_STATUS={preflight_status}\n"
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
        "    return 0\n"
        "}\n"
        + library[library.index("sleeve_on() {") : library.index("continuous_rmom_refresh_on()")]
        + text[
            text.index("any_mainnet_sleeve_on()") : text.index("# verify_topology collects")
        ]
        + text[text.index("start_if() {") : text.index("seed_rmom()")]
        + text[
            text.index("MAINNET_OWNER_UNIT=") : text.index("ROLLOUT_DOWNSTREAM_UNITS=(")
        ]
    )


def test_mainnet_activation_creates_roots_then_gates_on_preflight() -> None:
    blocked = subprocess.run(
        ["bash", "-c", _mainnet_harness("on", "off", 1) + "\nactivate_mainnet_mode\n"],
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
        ["bash", "-c", _mainnet_harness("on", "off", 0) + "\nactivate_mainnet_mode\n"],
        capture_output=True,
        text=True,
    )
    combined = started.stdout + started.stderr
    assert started.returncode == 0, combined
    assert "python:-m liquidity_migration.policy.real_money_arming create-state-roots --execute" in combined
    assert combined.index("create-state-roots") < combined.index("preflight")
    assert (
        "systemctl:enable liquidity-migration-account-execution-mainnet.service" in combined
    )
    assert (
        combined.index("systemctl:enable liquidity-migration-account-execution-mainnet.service")
        < combined.index("systemctl:start liquidity-migration-bybit-carry-mainnet.service")
    )
    assert (
        combined.index("systemctl:start liquidity-migration-bybit-carry-mainnet.service")
        < combined.index("systemctl:enable --now liquidity-migration-mainnet-liveness.timer")
    )
    # A sleeve that is off is disabled, not started.
    assert "systemctl:disable --now liquidity-migration-bybit-long-mainnet.service" in combined
    assert "systemctl:start liquidity-migration-bybit-long-mainnet.service" not in combined
    assert combined.index("mainnet-liveness.timer") < combined.index("verify_topology")
    for foreign in ("-demo.service", "-paper", "account-execution.service"):
        assert foreign not in combined, foreign


def test_activate_mainnet_refuses_when_no_mainnet_sleeve_is_on() -> None:
    result = subprocess.run(
        ["bash", "-c", _mainnet_harness("off", "off", 0) + "\nactivate_mainnet_mode\n"],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "no mainnet sleeve is on" in combined
    assert "deploy/sleeves.env" in combined
    assert "systemctl:" not in combined
    assert "python:" not in combined


def test_stop_mainnet_stops_only_mainnet_and_says_exposure_is_unchanged() -> None:
    result = subprocess.run(
        ["bash", "-c", _mainnet_harness("on", "on", 0) + "\nstop_mainnet_mode\n"],
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
    harness = _mainnet_harness("on", "on", 0).replace(
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


def test_verify_asserts_the_mainnet_fleet_only_when_a_mainnet_sleeve_is_on() -> None:
    text = _read(DEPLOY)
    verify = text[text.index("verify_topology()") : text.index("start_if()")]
    assert "if any_mainnet_sleeve_on; then" in verify
    assert "mainnet owner is not active and enabled" in verify
    assert "verify_unit on liquidity-migration-mainnet-liveness.timer" in verify
    assert "is active under demo authorization" in verify
    assert "mainnet_carry=%s mainnet_long=%s" in verify

    # An enabled timer is not a succeeding watchdog: without this the funded
    # observer can fail every fire and verify still reads green. Scoped inside
    # the mainnet branch so a stale failure cannot break demo verify.
    failed_check = "systemctl is-failed --quiet liquidity-migration-mainnet-liveness.service"
    assert failed_check in verify
    assert (
        'verify_note "liquidity-migration-mainnet-liveness.service is failed"' in verify
    )
    assert verify.index("if any_mainnet_sleeve_on; then") < verify.index(failed_check)
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
        "CONTINUOUS_SLEEVE",
        "CARRY_SLEEVE",
        "CARRY_MAINNET_SLEEVE",
        "LONG_MAINNET_SLEEVE",
        "CONTINUOUS_HEDGE_TIMER",
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
    assert "mainnet-keys=absent treated-as=off" in authorization


def test_routine_activation_restores_and_rollout_stops_the_funded_fleet() -> None:
    text = _read(DEPLOY)
    activate = text[text.index("activate_mode()") : text.index("MAINNET_OWNER_UNIT=")]
    assert activate.index("liquidity-migration-demo-liveness.timer") < activate.index(
        "if any_mainnet_sleeve_on; then\n        start_mainnet_fleet"
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
        (["activate-mainnet", "--profile", "operational"], "usage: deploy_vps_live.sh"),
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
        "activate-mainnet",
        "stop-mainnet",
    ):
        assert mode in usage, mode
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


def test_stop_mainnet_says_the_sleeves_still_restart_the_fleet() -> None:
    """`activate`/`rollout` restart the funded fleet from the sleeve toggles, so a
    stop that leaves them on is undone by the next routine deploy.
    """

    result = subprocess.run(
        ["bash", "-c", _mainnet_harness("on", "off", 0) + "\nstop_mainnet_mode\n"],
        check=True,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert "the next activate or rollout restarts this fleet" in combined
    assert "CARRY_MAINNET_SLEEVE/LONG_MAINNET_SLEEVE off" in combined


def _verify_harness(
    mode: str,
    active: str,
    enabled: str,
    failed: str = "",
    permissions_status: int = 0,
) -> str:
    """The real verify_topology over a stub systemd, so accumulation is exercised
    rather than pattern-matched. Every sleeve off is the smallest topology that
    still covers on-expectations, off-expectations, the
    oneshot sweep and the venue probe.
    """

    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")
    return (
        "set -u\n"
        f"MODE={mode}\n"
        "AUTH_PROFILE=operational\n"
        "PYTHON=fake_python\n"
        "EXPECTED_COMMIT=abc123\n"
        "EXPECTED_COMMIT_EXPLICIT=1\n"
        "LONG_SLEEVE=off\nCONTINUOUS_SLEEVE=off\nCARRY_SLEEVE=off\n"
        "CARRY_MAINNET_SLEEVE=off\nLONG_MAINNET_SLEEVE=off\nCONTINUOUS_HEDGE_TIMER=off\n"
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
        + library[library.index("sleeve_on() {") : library.index("continuous_rmom_refresh_on()")]
        + text[text.index("unit_on() {") : text.index("check_demo_order_permissions() {")]
        + text[text.index("verify_topology()") : text.index("start_if()")]
    )


_VERIFY_GREEN = (
    "liquidity-migration-account-execution.service "
    "liquidity-migration-demo-liveness.timer"
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
    assert "liquidity-migration-account-execution.service|on|active|enabled" in combined
    assert (
        "liquidity-migration-bybit-carry-demo.service|off|inactive|disabled" in combined
    )
    assert "verify-mismatch" not in combined

    # Owner down, liveness timer down, one failed oneshot, and the venue probe
    # refusing: four independent findings from a single run.
    broken = subprocess.run(
        [
            "bash",
            "-c",
            _verify_harness(
                "activate",
                "",
                "",
                failed="liquidity-migration-continuous-hedge.service",
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
        "verify-mismatch demo owner is not active and enabled",
        "verify-mismatch liveness timer is not active",
        "verify-mismatch liquidity-migration-continuous-hedge.service is failed",
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


def _quiescence_harness(tmp_path: Path, stop_first: str, mainnet: str) -> str:
    text = _read(DEPLOY)
    library = _read("deploy/lib_sleeves.sh")
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "units"
    state.write_text(
        "liquidity-migration-bybit-carry-demo.service loaded active running carry\n"
        "liquidity-migration-account-execution.service loaded active running owner\n",
        encoding="utf-8",
    )
    return (
        "set -u\n"
        f"STATE_FILE={state}\n"
        f"STOP_FIRST={stop_first}\n"
        f"CARRY_MAINNET_SLEEVE={mainnet}\nLONG_MAINNET_SLEEVE=off\n"
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
        + library[library.index("sleeve_on() {") : library.index("continuous_rmom_refresh_on()")]
        + text[text.index("any_mainnet_sleeve_on()") : text.index("# verify_topology collects")]
        + text[text.index("running_liqmig_units() {") : text.index("git_fetch() {")]
        + text[
            text.index("ROLLOUT_DOWNSTREAM_UNITS=(") : text.index(
                "stop_all_rollout_units_best_effort()"
            )
        ]
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

    funded = subprocess.run(
        [
            "bash",
            "-c",
            _quiescence_harness(tmp_path / "funded", "auto", "on")
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
        + text[text.index("staged_mode() {") : text.index("# A funded fleet keeps")]
        + "\nstaged_mode\n"
    )
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert combined.index("ran:install") < combined.index("ran:profile-marker=operational")
    assert combined.index("ran:profile-marker=operational") < combined.index("ran:activate")
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
            f"CARRY_MAINNET_SLEEVE={mainnet}\nLONG_MAINNET_SLEEVE=off\n"
            + text[text.index("fail() {") : text.index("run_phase_pair() {")]
            + library[
                library.index("sleeve_on() {") : library.index("continuous_rmom_refresh_on()")
            ]
            + text[
                text.index("any_mainnet_sleeve_on()") : text.index("# verify_topology collects")
            ]
            + text[text.index("rollout_flat_required() {") : text.index("rollout_mode() {")]
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
    harness = _mainnet_harness("on", "off", 0).replace("systemctl() {\n", "unused() {\n", 1)
    result = subprocess.run(
        ["bash", "-c", "PATH=/nonexistent\n" + harness + "\nstop_mainnet_mode\n"],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "systemctl is unavailable" in combined
    assert "stop-mainnet-ok" not in combined
