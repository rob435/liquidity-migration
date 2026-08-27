from __future__ import annotations

import os
import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy_vps_live.sh"
SYSTEMD = ROOT / "deploy" / "systemd"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _function(text: str, name: str, next_name: str) -> str:
    return text[text.index(f"{name}()") : text.index(f"{next_name}()")]


def _units() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(SYSTEMD.glob("liquidity-migration-*.*"))
        if path.suffix in {".service", ".timer"}
    }


def test_deployed_shell_entrypoints_are_executable() -> None:
    if os.name == "nt":
        return
    for relative in (
        "scripts/deploy_vps_live.sh",
        "scripts/ops.sh",
        "scripts/run_authorized_runtime.sh",
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
    ):
        assert (ROOT / relative).stat().st_mode & stat.S_IXUSR, relative


def test_manifest_contains_only_the_current_rust_owned_fleet() -> None:
    units = _units()
    assert len(units) == 15
    assert "liquidity-migration-engine.service" in units
    assert "liquidity-migration-engine-mainnet.service" in units
    assert not any("account-execution" in name for name in units)


def test_guarded_units_use_only_the_commit_owned_wrapper() -> None:
    guarded = [body for name, body in _units().items() if name.endswith(".service")]
    for body in guarded:
        assert "ExecStart=/opt/liquidity-migration/scripts/run_authorized_runtime.sh" in body
    wrapper = _read("scripts/run_authorized_runtime.sh")
    for name in _units():
        if name.endswith(".service"):
            assert f"{name}:main" in wrapper


def test_execution_engines_and_producers_run_as_distinct_unprivileged_users() -> None:
    units = _units()
    assert "User=liquidity-engine-demo" in units["liquidity-migration-engine.service"]
    assert "User=liquidity-engine-mainnet" in units["liquidity-migration-engine-mainnet.service"]
    for name, body in units.items():
        if "bybit-" in name:
            assert "User=liquidity-producer" in body
            assert "User=root" not in body


def test_producers_never_receive_venue_credentials_or_the_arming_switch() -> None:
    for name, body in _units().items():
        if "bybit-" not in name:
            continue
        unset = next(line for line in body.splitlines() if line.startswith("UnsetEnvironment="))
        for key in (
            "BYBIT_DEMO_API_KEY",
            "BYBIT_DEMO_API_SECRET",
            "BYBIT_REAL_API_KEY",
            "BYBIT_REAL_API_SECRET",
            "REAL_MONEY",
        ):
            assert key in unset, (name, key)


def test_producer_source_templates_are_non_secret_and_realm_bound() -> None:
    for realm in ("demo", "mainnet"):
        body = _read(f"deploy/producer-{realm}-source.env.template")
        assert f"PRODUCER_REALM={realm}" in body
        for key in (
            "CANDIDATE_UNIVERSE_FILE",
            "VENUE_RULES_FILE",
            "OPERATIONAL_PROFILE_FILE",
        ):
            assert f"{key}=/" in body
        assert "BYBIT_" not in body and "REAL_MONEY" not in body


def test_producer_projection_is_an_explicit_non_secret_allowlist() -> None:
    block = _function(DEPLOY.read_text(encoding="utf-8"), "write_producer_environment", "project_mainnet_telegram_environment")
    assert '"OPERATIONAL_PROFILE_FILE", "PRODUCER_REALM", "VENUE_RULES_FILE"' in block
    assert "producer source contains forbidden secret/control keys" in block
    assert "os.replace(temporary, target)" in block
    assert "os.fsync(directory)" in block


def test_producer_wrappers_require_rust_books_binding_and_heartbeat() -> None:
    for relative, book in (
        ("scripts/runtime/run_bybit_long_demo_event_engine.sh", "LONG_ENGINE_TARGET_BOOK_PATH"),
        ("scripts/runtime/run_bybit_carry_demo_event_engine.sh", "CARRY_ENGINE_TARGET_BOOK_PATH"),
    ):
        body = _read(relative)
        for key in (
            book,
            "LIVENESS_ENGINE_HEARTBEAT_FILE",
            "EXPECTED_ENGINE_ACCOUNT_USER_ID",
            "OPERATIONAL_PROFILE_FILE",
            "PRODUCER_REALM",
        ):
            assert key in body
        assert '[ "$PRODUCER_REALM" = "$EXECUTION_ENVIRONMENT" ]' in body
        assert 'ENGINE_ACCOUNT_HEARTBEAT_FILE="$LIVENESS_ENGINE_HEARTBEAT_FILE"' in body


def test_engine_environment_is_bound_to_account_venue_realm_config_and_heartbeat() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "validate_engine_environment", "quarantine_engine_inputs")
    for key in (
        "EXPECTED_ENGINE_ACCOUNT_USER_ID",
        "EXPECTED_ENGINE_VENUE",
        "EXPECTED_ENGINE_REALM",
        "EXPECTED_ENGINE_VERSION",
        "LIVENESS_ENGINE_HEARTBEAT_FILE",
    ):
        assert key in block
    assert "config heartbeat_path disagrees" in block
    assert "book_path is" in block


def test_demo_engine_template_has_an_exact_account_id_and_mainnet_requires_one() -> None:
    demo = _read("deploy/engine.env.template")
    mainnet = _read("deploy/engine.mainnet.env.template")
    assert "EXPECTED_ENGINE_ACCOUNT_USER_ID=555899665" in demo
    assert "EXPECTED_ENGINE_ACCOUNT_USER_ID=" in mainnet
    assert "must set EXPECTED_ENGINE_ACCOUNT_USER_ID to the exact venue id" in DEPLOY.read_text(encoding="utf-8")


def test_engine_release_is_locked_commit_bound_and_digest_checked() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    build = _function(text, "build_engine", "verify_engine_release")
    verify = _function(text, "verify_engine_release", "validate_engine_environment")
    assert "cargo" in build and "build --release --locked" in build
    assert "locked release engine build failed" in build
    assert "commit=%s\\nsha256=%s\\nrustc=1.90.0" in build
    assert 'marker_commit" = "$installed_head' in verify
    assert 'marker_digest" = "$actual_digest' in verify
    assert _read("rust-toolchain.toml").split('channel = "', 1)[1].split('"', 1)[0] == "1.90.0"


def test_engine_heartbeat_activation_gate_checks_identity_freshness_and_may_open() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "wait_engine_heartbeat", "wait_fresh_producer_book")
    for field in (
        '"account_user_id"',
        '"venue"',
        '"realm"',
        '"engine_version"',
        '"account_observed_wall_ts_ms"',
    ):
        assert field in block
    assert 'payload.get("mode") != "live"' in block
    assert 'payload.get("may_open") is not True' in block


def test_activation_accepts_only_books_from_the_current_producer_generation() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "wait_fresh_producer_book", "start_required_engine")
    assert "InvocationID" in block
    assert 'health.get("invocation_id") != invocation' in block
    assert 'health["completed_ts_ns"] < started_ns' in block
    assert "stat.st_mtime_ns < started_ns" in block
    assert 'valid_until <= now_ms' in block


def test_activation_starts_engine_then_producers_then_immediate_liveness() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    demo = _function(text, "activate_mode", "provision_mainnet_prerequisites")
    assert demo.index("start_required_engine") < demo.index("start_if")
    assert demo.index("wait_fresh_producer_book") < demo.index("demo-liveness.timer")
    assert demo.index("demo-liveness.timer") < demo.index("demo-liveness.service")
    mainnet = _function(text, "start_mainnet_fleet", "disarm_mainnet_mode")
    assert mainnet.index("start_required_engine") < mainnet.index("carry-mainnet.service")
    assert mainnet.index("wait_fresh_producer_book") < mainnet.index("MAINNET_LIVENESS_TIMER")


def test_mainnet_preflight_uses_only_credentials_and_neutral_producer_inputs() -> None:
    arming = _read("liquidity_migration/policy/real_money_arming.py")
    assert "MAINNET_CREDENTIAL_ENV" in arming
    assert "MAINNET_PRODUCER_SOURCE_ENV" in arming
    assert "OPERATIONAL_PROFILE_FILE" in arming
    for retired in ("ACCOUNT_EXECUTION_ROOT", "ACCOUNT_INTENT_INBOX_ROOT", "create-state-roots"):
        assert retired not in arming


def test_mainnet_disarm_replaces_and_syncs_switch_before_stopping_units() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "disarm_mainnet_mode", "stop_mainnet_mode")
    replace_at = block.index("os.replace(temporary, path)")
    sync_at = block.index("os.fsync(directory)")
    stop_at = block.index("systemctl disable --now")
    assert replace_at < sync_at < stop_at
    assert 'values["REAL_MONEY"] = "false"' in block
    assert "remained enabled" in block and "remained active" in block


def test_generation_changing_rollout_has_no_configured_symbol_flat_fallback() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "rollout_flat_check", "verify_topology")
    assert "venue-global-flat-attestation-unavailable" in block
    assert "return 3" in block
    assert "configured-symbol fallback" in block
    assert "check_deploy_rollout_readiness" not in text


def test_rollout_calls_the_flat_gate_before_any_stop_or_checkout_mutation() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    gate = block.index("pre-stop-flat-account-proof")
    assert gate < block.index("ROLLOUT_STOPPED=1")
    assert gate < block.index("stop-downstream-units")
    assert gate < block.index("stopped-install")


def test_exact_commit_checkout_gate_uses_an_independent_index_and_rechecks_head() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "clean_checkout_status", "require_clean_checkout_at")
    assert "mktemp -d /run/liquidity-migration/deploy-index" in block
    assert "read-tree" in block and "diff-index --quiet" in block
    exact = _function(text, "require_clean_checkout_at", "require_clean_head")
    assert exact.count('safe_git rev-parse HEAD') >= 2


def test_systemd_install_is_manifest_exact_and_refuses_dropins() -> None:
    lib = _read("deploy/lib_sleeves.sh")
    assert "lm_cleanup_unknown_liqmig_units" in lib
    assert "lm_verify_no_unknown_liqmig_units" in lib
    assert 'cmp -s "$_lvgus_source" "$_lvgus_installed"' in lib
    assert "guarded unit has an unreviewed drop-in" in lib
    assert "guarded unit loaded from unexpected fragment" in lib


def test_remote_git_transport_ignores_host_configuration_and_hides_token() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "GIT_CONFIG_NOSYSTEM=1" in text
    assert "GIT_ENV=(\n    /usr/bin/env -i" in text
    assert "GIT_TERMINAL_PROMPT=0" in text
    fetch = _function(text, "git_fetch", "validate_declared_demo_rules")
    assert 'GIT_CONFIG_GLOBAL="$config_file"' in fetch
    assert "AUTHORIZATION: Basic %s" in fetch
    assert "chmod 0600" in fetch


def test_every_remote_mode_shares_the_maintenance_locks() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    call = text.rindex("acquire_maintenance_locks\n")
    dispatch = text.rindex('case "$MODE" in')
    assert call < dispatch
    assert "maintenance.lock" in text and "deploy.lock" in text
    assert "acquire-inherited" in text


def test_ci_tests_and_builds_the_exact_locked_release_shape() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert "cargo test --workspace --all-targets --release --locked" in workflow
    assert "cargo build --release --locked" in workflow
    assert "sha256sum target/release/engine" in workflow
    assert "--only-binary=:all: -r requirements.lock" in workflow
    action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)
    assert action_refs and all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_vps_workflow_is_serialized_and_time_bounded() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    vps = workflow[workflow.index("  vps:") :]
    assert "timeout-minutes: 45" in vps
    assert "group: liquidity-migration-vps" in vps
    assert "cancel-in-progress: false" in vps
    assert "ServerAliveInterval=15" in vps and "ServerAliveCountMax=3" in vps


def test_runtime_dependencies_are_exact_version_pins() -> None:
    rows = [
        line.strip()
        for line in _read("requirements.lock").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rows and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", row) for row in rows)


def test_oneshots_and_daemons_have_bounded_resources() -> None:
    for name, body in _units().items():
        if not name.endswith(".service"):
            continue
        assert "NoNewPrivileges=true" in body, name
        assert "MemoryMax=" in body, name
        if "Type=oneshot" in body:
            assert "TimeoutStartSec=" in body, name


def test_liveness_units_bind_exact_engine_identity_without_venue_credentials() -> None:
    units = _units()
    for name in (
        "liquidity-migration-demo-liveness.service",
        "liquidity-migration-mainnet-liveness.service",
    ):
        body = units[name]
        assert "EXPECTED_ENGINE_ACCOUNT_USER_ID" in body or "engine" in body
        assert "LIVENESS_ENGINE_HEARTBEAT_FILE" in body or "engine" in body
        assert "UnsetEnvironment=BYBIT_DEMO_API_KEY" in body


def test_ops_surface_has_no_python_execution_owner_commands() -> None:
    body = _read("scripts/ops.sh")
    assert "real-money preflight" in body
    assert "real-money render-profile" in body
    assert "create-state-roots" not in body
    assert "wedged-command" not in body
    assert "venue-accounting" not in body
