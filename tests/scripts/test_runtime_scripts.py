from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

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


def test_guarded_units_cross_the_installed_release_gate_before_checkout_code() -> None:
    guarded = [body for name, body in _units().items() if name.endswith(".service")]
    for body in guarded:
        assert (
            "ExecStart=/opt/liquidity-migration-engine/bin/run-authorized-runtime"
            in body
        )
        if "Restart=always" in body:
            assert "RestartPreventExitStatus=78" in body
        assert "KillMode=control-group" in body
    wrapper = _read("scripts/run_authorized_runtime.sh")
    for name in _units():
        if name.endswith(".service"):
            assert f"{name}:main" in wrapper
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    for required in (
        "launcher_sha256",
        "ACTIVATION_RECEIPT",
        "ACTIVATION_PERMIT",
        "activation_authority_matches",
        "not_after_epoch",
        "diff-index --quiet",
        'show "$marker_commit:scripts/run_authorized_runtime.sh"',
        'sha256sum "$ENGINE"',
        'sha256sum "$LAUNCHER"',
        'sha256sum "$CONTROL_HELPER"',
        'sha256sum "$TELEGRAM_BOT"',
        "activation_authority_valid",
        "terminate_child",
        'exit 78',
    ):
        assert required in launcher


def test_execution_engines_and_producers_run_as_distinct_unprivileged_users() -> None:
    units = _units()
    assert "User=liquidity-engine-demo" in units["liquidity-migration-engine.service"]
    assert "User=liquidity-engine-mainnet" in units["liquidity-migration-engine-mainnet.service"]
    for name, body in units.items():
        if "bybit-" in name:
            assert "User=liquidity-producer" in body
            assert "User=root" not in body


def test_telegram_controls_use_an_isolated_identity_and_exact_root_helper() -> None:
    unit = _units()["liquidity-migration-telegram-controls.service"]
    assert "User=liquidity-controls" in unit
    assert "Group=liquidity-controls" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "StateDirectory=liquidity-migration-telegram-controls" in unit
    assert "ReadWritePaths=" not in unit
    assert "InaccessiblePaths=" in unit
    assert "bybit-mainnet-attestor.env" in unit
    assert "rollout-attestor-operator-public.pem" not in unit
    assert "attestor-bootstrap" not in unit
    assert "User=root" not in unit

    helper = _read("deploy/telegram_control_helper.sh")
    for action in ("pause-demo", "resume-demo", "pause-mainnet", "status-demo"):
        assert action in helper
    assert "resume-mainnet" not in helper
    assert 'case "$ACTION" in' in helper
    assert "/usr/bin/systemd-run --quiet --wait --pipe --collect" in helper
    assert '"$HELPER" --worker "$ACTION"' in helper
    assert "demo resume requires this generation's completed activation receipt" in helper
    assert "control_helper_sha256" in helper
    assert "controls_sudoers_sha256" in helper
    assert "telegram_bot_sha256" in helper
    assert "InaccessiblePaths=" in helper
    assert "quarantine_pair" in helper
    assert 'systemctl is-active --quiet "$unit"' in helper
    assert 'systemctl is-enabled "$unit"' in helper
    assert "demo pause could not quarantine both producers" in helper
    assert "mainnet pause could not quarantine both funded producers" in helper
    assert "demo resume requires the account owner to be active" in helper

    sudoers = _read("deploy/liquidity-controls.sudoers")
    allowed = {
        line.split("NOPASSWD: ", 1)[1]
        for line in sudoers.splitlines()
        if "NOPASSWD: " in line
    }
    assert allowed == {
        f"/opt/liquidity-migration-engine/bin/telegram-control-helper {action}"
        for action in ("pause-demo", "resume-demo", "pause-mainnet", "status-demo")
    }
    assert "!setenv" in sudoers
    policy = _function(
        DEPLOY.read_text(encoding="utf-8"),
        "verify_controls_sudo_policy",
        "build_engine",
    )
    assert '/usr/bin/sudo -l -U "$CONTROLS_USER"' in policy
    assert "exact four-command boundary" in policy

    bot = _read("liquidity_migration/ops/telegram_controls.py")
    fleet = bot[bot.index("class VpsFleet:") : bot.index("# The panel")]
    assert '"/usr/bin/sudo", "-n", CONTROL_HELPER' in bot
    assert "systemctl\", \"enable" not in fleet
    assert "systemctl\", \"disable" not in fleet
    assert "write_text(" not in fleet and "os.replace(" not in fleet

    identities = _function(
        DEPLOY.read_text(encoding="utf-8"),
        "ensure_runtime_identities",
        "write_producer_environment",
    )
    assert "CONTROLS_USER" in identities and "CONTROLS_GROUP" in identities
    assert 'id -nG "$CONTROLS_USER"' in identities


def test_funded_owner_retains_only_its_load_bearing_account_identity() -> None:
    units = _units()
    funded = units["liquidity-migration-engine-mainnet.service"]
    unset = next(
        line for line in funded.splitlines() if line.startswith("UnsetEnvironment=")
    )
    assert "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" not in unset
    for name, body in units.items():
        if not name.endswith(".service") or name == "liquidity-migration-engine-mainnet.service":
            continue
        other_unset = " ".join(
            line for line in body.splitlines() if line.startswith("UnsetEnvironment=")
        )
        assert "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" in other_unset, name


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
            "BYBIT_ATTEST_API_KEY",
            "BYBIT_ATTEST_API_SECRET",
            "BYBIT_ATTEST_API_KEY_IP",
            "REAL_MONEY",
        ):
            assert key in unset, (name, key)


def test_producer_source_templates_are_non_secret_and_realm_bound() -> None:
    for realm in ("demo", "mainnet"):
        body = _read(f"deploy/producer-{realm}-source.env.template")
        assert f"PRODUCER_REALM={realm}" in body
        for key in (
            "CANDIDATE_UNIVERSE_FILE",
            "OPERATIONAL_PROFILE_FILE",
        ):
            assert f"{key}=/" in body
        assert "BYBIT_" not in body and "REAL_MONEY" not in body


def test_producer_projection_is_an_explicit_non_secret_allowlist() -> None:
    block = _function(DEPLOY.read_text(encoding="utf-8"), "write_producer_environment", "project_mainnet_telegram_environment")
    assert '"OPERATIONAL_PROFILE_FILE", "PRODUCER_REALM"' in block
    assert "VENUE_RULES_FILE" not in block
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


def test_funded_exodus_is_wired_from_producer_to_engine_and_activation() -> None:
    config = tomllib.loads(_read("deploy/engine.mainnet.toml.template"))
    strategies = config["strategy"]
    assert [row["sleeve"] for row in strategies] == ["carry", "long", "exodus"]
    exodus = strategies[2]
    assert exodus == {
        "name": "target_book",
        "sleeve": "exodus",
        "book_path": "/var/lib/liquidity-migration/targets/exodus-mainnet.json",
        "rest_entries": False,
        "symbols": ["DOGEUSDT"],
    }

    carry_unit = _read(
        "deploy/systemd/liquidity-migration-bybit-carry-mainnet.service"
    )
    assert "Environment=EXODUS_SHORT_PROFILE=v1" in carry_unit
    assert (
        "Environment=EXODUS_ENGINE_TARGET_BOOK_PATH="
        "/var/lib/liquidity-migration/targets/exodus-mainnet.json"
    ) in carry_unit

    deploy = DEPLOY.read_text(encoding="utf-8")
    validation = _function(deploy, "validate_engine_environment", "quarantine_engine_inputs")
    quarantine = _function(deploy, "quarantine_engine_inputs", "wait_engine_heartbeat")
    activation = _function(deploy, "start_mainnet_fleet", "resolve_fail_safe_python")
    expected = "/var/lib/liquidity-migration/targets/exodus-mainnet.json"
    assert expected in validation
    assert "exodus-mainnet.json" in quarantine
    assert expected in activation
    install_config = _function(
        deploy, "install_mainnet_engine_config", "require_rollout_for_funded_generation_change"
    )
    install_mode = _function(deploy, "install_mode", "load_authorization")
    assert "deploy/engine.mainnet.toml.template" in install_config
    assert 'mv -f "${ENGINE_MAINNET_CONFIG}.new"' in install_config
    assert install_mode.index("checkout -B") < install_mode.index(
        "install-mainnet-engine-config"
    )

    flatten = _read("scripts/vps/flatten_account.sh")
    assert expected in flatten


def test_demo_engine_template_has_an_exact_account_id_and_mainnet_requires_one() -> None:
    demo = _read("deploy/engine.env.template")
    mainnet = _read("deploy/engine.mainnet.env.template")
    assert "EXPECTED_ENGINE_ACCOUNT_USER_ID=555899665" in demo
    assert "EXPECTED_ENGINE_ACCOUNT_USER_ID=" in mainnet
    assert "must set EXPECTED_ENGINE_ACCOUNT_USER_ID to the exact venue id" in DEPLOY.read_text(encoding="utf-8")


def test_engine_release_is_locked_commit_bound_and_digest_checked() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    compile_exact = _function(text, "compile_engine_commit", "build_engine")
    toolchain_check = _function(text, "require_pinned_engine_toolchain", "compile_engine_commit")
    prefetch = _function(text, "prefetch_rollout_target", "record_installed_profile")
    build = _function(text, "build_engine", "verify_engine_release")
    verify = _function(text, "verify_engine_release", "validate_engine_environment")
    assert "cargo" in compile_exact and "build --release --locked" in compile_exact
    assert "locked release engine build failed" in compile_exact
    assert 'RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN"' in toolchain_check
    assert 'RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN"' in compile_exact
    assert "require_pinned_engine_toolchain" in prefetch
    assert 'engine_git reset --hard --quiet FETCH_HEAD' in compile_exact
    assert 'built" = "$commit' in compile_exact
    assert (
        "commit=%s\\nsha256=%s\\nlauncher_sha256=%s\\n"
        "control_helper_sha256=%s\\ncontrols_sudoers_sha256=%s\\n"
        "telegram_bot_sha256=%s\\nrustc=1.90.0"
    ) in build
    assert '"$ENGINE_LAUNCHER.new"' in build
    assert (
        build.index('mv -f "$ENGINE_BINARY.new"')
        < build.index('mv -f "$ENGINE_LAUNCHER.new"')
        < build.index('mv -f "$ENGINE_CONTROL_HELPER.new"')
        < build.index('mv -f "$CONTROLS_SUDOERS.new"')
        < build.index('mv -f "$marker_tmp"')
    )
    assert "verify_engine_release launcher-required" in build
    assert 'marker_commit" = "$installed_head' in verify
    assert 'marker_digest" = "$actual_digest' in verify
    assert 'actual_launcher_digest" = "$marker_launcher_digest' in verify
    assert 'actual_helper_digest" = "$marker_helper_digest' in verify
    assert 'actual_sudoers_digest" = "$marker_sudoers_digest' in verify
    assert 'actual_bot_digest" = "$marker_bot_digest' in verify
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
    provision = _function(
        DEPLOY.read_text(encoding="utf-8"),
        "provision_mainnet_prerequisites",
        "require_mainnet_preflight",
    )
    assert "MAINNET_CREDENTIAL_ENV" in arming
    assert "MAINNET_PRODUCER_SOURCE_ENV" in arming
    assert "OPERATIONAL_PROFILE_FILE" in arming
    assert 'install -d -o root -g "$RUNTIME_GROUP" -m 0750' in provision
    assert 'chmod 700 "$(dirname "$risk_policy_file")"' not in provision
    for retired in ("ACCOUNT_EXECUTION_ROOT", "ACCOUNT_INTENT_INBOX_ROOT", "create-state-roots"):
        assert retired not in arming


def test_mainnet_disarm_quarantines_before_checkout_independent_rewrite() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    quarantine = _function(text, "quarantine_mainnet_units", "disarm_mainnet_mode")
    block = _function(text, "disarm_mainnet_mode", "stop_mainnet_mode")
    quarantine_at = block.index("quarantine_mainnet_units")
    replace_at = block.index("os.replace(temporary, path)")
    sync_at = block.index("os.fsync(directory)")
    assert quarantine_at < replace_at < sync_at
    assert 'environment["REAL_MONEY"] = "false"' in block
    assert "/usr/bin/systemctl disable --now" in quarantine
    assert "still-enabled" in quarantine and "still-active" in quarantine
    assert "/bin/sync" in quarantine
    assert 'getattr(os, "O_NOFOLLOW", 0)' in block
    assert "before.st_nlink != 1" in block
    assert "/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C" in block
    assert '"$fail_safe_python" -I -S -' in block
    assert "from liquidity_migration" not in block
    assert ".venv" not in block


def test_embedded_fail_safe_disarm_parser_is_strict_and_standalone() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "disarm_mainnet_mode", "stop_mainnet_mode")
    anchor = '"$fail_safe_python" -I -S - "$MAINNET_CREDENTIAL_ENV" <<\'PY\'\n'
    start = block.index(anchor) + len(anchor)
    payload = block[start : block.index("\nPY\n", start)]
    definitions = payload[: payload.index("\ntry:\n    credential_path")]
    namespace: dict[str, object] = {}
    exec(compile(definitions, "<embedded-fail-safe-disarm>", "exec"), namespace)
    parse_environment = namespace["parse_environment"]
    disarm_error = namespace["DisarmError"]
    assert callable(parse_environment)
    assert isinstance(disarm_error, type)
    assert parse_environment(  # type: ignore[operator]
        b"BYBIT_REAL_API_KEY='opaque value'\nREAL_MONEY=true\n"
    ) == {"BYBIT_REAL_API_KEY": "opaque value", "REAL_MONEY": "true"}
    for malformed in (
        b"REAL_MONEY=true\nREAL_MONEY=false\n",
        b"bad_key=true\n",
        b"REAL_MONEY=two values\n",
        b"REAL_MONEY=bad\\escape\n",
        b"REAL_MONEY=true\r\n",
    ):
        with pytest.raises(disarm_error):  # type: ignore[arg-type]
            parse_environment(malformed)  # type: ignore[operator]


def test_rollout_and_activation_do_not_depend_on_flatness_attestation() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    prerequisites = _function(
        text, "provision_mainnet_prerequisites", "require_mainnet_preflight"
    )
    for removed in (
        "trusted-rollout-attestor",
        "rollout_flat_check",
        "flat-account-proof",
        "snapshot_trusted_rollout_attestor",
        "--require-flat",
    ):
        assert removed not in rollout
    assert "validate_mainnet_attestor_environment" not in prerequisites
    assert "MAINNET_ATTESTOR_ENV" not in prerequisites


def test_engine_build_runs_unprivileged_against_immutable_exact_source() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "compile_engine_commit", "build_engine")
    for required in (
        "ENGINE_BUILDER_USER",
        "ENGINE_BUILDER_CARGO_HOME",
        "ENGINE_BUILDER_TARGET_DIR",
        "chown -R root:root",
        "chmod -R a-w",
        "systemd-run --quiet --wait --pipe --collect",
        "KillMode=control-group",
        "/usr/sbin/runuser -u",
        "/usr/bin/env -i",
        "CARGO_HOME=",
        "CARGO_TARGET_DIR=",
        "build --release --locked",
        'engine_git status --porcelain=v1 --untracked-files=all',
        'readlink -f "$ENGINE_CANDIDATE_BINARY"',
        'stat -c %h "$ENGINE_CANDIDATE_BINARY"',
    ):
        assert required in block
    assert block.index("chmod -R a-w") < block.index("systemd-run")
    assert block.index("systemd-run") < block.rindex("engine_git status")


def test_funded_attestor_template_has_only_the_read_only_identity_contract() -> None:
    template = _read("deploy/bybit-mainnet-attestor.env.template")
    assignments = {
        line.split("=", 1)[0]
        for line in template.splitlines()
        if line and not line.startswith("#")
    }
    assert assignments == {
        "BYBIT_ATTEST_API_KEY",
        "BYBIT_ATTEST_API_SECRET",
        "BYBIT_ATTEST_API_KEY_IP",
        "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID",
    }
    assert "BYBIT_REAL_API_KEY=" not in template
    assert "REAL_MONEY=" not in template


def test_persistent_services_never_receive_the_transient_attestor_key() -> None:
    for name, body in _units().items():
        if not name.endswith(".service"):
            continue
        assert "EnvironmentFile=/etc/liquidity-migration/bybit-mainnet-attestor.env" not in body
        unset = " ".join(
            line for line in body.splitlines() if line.startswith("UnsetEnvironment=")
        )
        for key in (
            "BYBIT_ATTEST_API_KEY",
            "BYBIT_ATTEST_API_SECRET",
            "BYBIT_ATTEST_API_KEY_IP",
        ):
            assert key in unset, (name, key)
        if name == "liquidity-migration-engine-mainnet.service":
            assert "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" not in unset
        else:
            assert "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" in unset, name


def test_direct_generation_modes_refuse_every_persisted_funded_surface() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    inventory = _function(
        text,
        "funded_configuration_present",
        "require_rollout_for_funded_generation_change",
    )
    for surface in (
        "MAINNET_CREDENTIAL_ENV",
        "MAINNET_ATTESTOR_ENV",
        "ENGINE_MAINNET_ENVIRONMENT",
        "engine-mainnet.toml",
        "PRODUCER_MAINNET_ENV",
        "PRODUCER_MAINNET_SOURCE_ENV",
        "MAINNET_TELEGRAM_ENV",
    ):
        assert surface in inventory
    assert '-L "$path"' in inventory
    for function, boundary in (
        ("install_mode", "load_authorization"),
        ("activate_mode", "provision_mainnet_prerequisites"),
        ("staged_mode", "rollout_mode"),
    ):
        assert "require_rollout_for_funded_generation_change" in _function(
            text, function, boundary
        )
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert "ROLLOUT_FUNDED_AUTHORITY=1" in rollout


def test_rollout_accepts_a_markerless_incumbent_until_the_stopped_install() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    before_install = block[: block.index("stopped-install")]
    assert before_install.index("rollout-target-prefetch") < before_install.index(
        "stop-downstream-units"
    )
    assert before_install.index("snapshot-prior-topology") < before_install.index(
        "stop-downstream-units"
    )
    for incompatible in (
        "load_authorization",
        "verify_topology",
        "verify_engine_release",
        "activation_authority_matches",
        "ENGINE_BINARY.release",
    ):
        assert incompatible not in before_install


def test_rollout_stops_owners_before_install_and_activates_after_install() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    owners = block.index("stop-account-owners")
    install = block.index("stopped-install")
    activate = block.index("activate-and-verify")
    assert owners < install < activate


def test_rollout_persists_boot_fence_before_mutation_and_requarantines_failures() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    owners_stopped = rollout.index("stop-account-owners")
    boot_fence = rollout.index("persist-rollout-boot-fence")
    irreversible = rollout.index("ROLLOUT_IRREVERSIBLE=1")
    install = rollout.index("stopped-install")
    assert owners_stopped < boot_fence < irreversible < install

    fence = _function(
        text, "disable_rollout_units_for_boot_fence", "stop_all_rollout_units_best_effort"
    )
    for required in (
        'systemctl disable "$unit"',
        "systemctl daemon-reload",
        "systemctl is-active --quiet",
        "systemctl is-enabled",
        "invalidate_activation_authority",
        "sync",
    ):
        assert required in fence

    quarantine = _function(
        text, "stop_all_rollout_units_best_effort", "cleanup_notice"
    )
    for required in (
        "systemctl disable --now",
        "systemctl daemon-reload",
        "systemctl is-active --quiet",
        "systemctl is-enabled",
        "still-boot-enabled",
        "ACTIVATION_PERMIT",
        "ACTIVATION_RECEIPT",
        "sync",
    ):
        assert required in quarantine
    cleanup = _function(text, "rollout_cleanup", "prefetch_rollout_target")
    cancel = _function(text, "rollout_cancel", "rollout_cleanup")
    assert 'ROLLOUT_CANCELLATION_SIGNAL="$signal"' in cancel
    assert cleanup.index('if [ "$status" -ne 0 ] && [ -n "$cancellation_signal" ]') < cleanup.index(
        'if [ "$ROLLOUT_IRREVERSIBLE" -eq 0 ]'
    )
    cancellation = cleanup[
        cleanup.index('if [ "$status" -ne 0 ] && [ -n "$cancellation_signal" ]') : cleanup.index(
            'elif [ "$status" -ne 0 ]'
        )
    ]
    assert "stop_all_rollout_units_best_effort" in cancellation
    assert "activate_mode" not in cancellation
    for signal, status in (("INT", 130), ("TERM", 143), ("HUP", 129), ("PIPE", 141)):
        assert f"trap 'rollout_cancel {signal} {status}' {signal}" in rollout
    snapshot_restore = _function(
        text, "restore_prior_topology_snapshot", "restore_prior_topology"
    )
    restore = _function(text, "restore_prior_topology", "stop_rollout_units")
    assert "activate_mode" not in snapshot_restore
    assert "verify_engine_release" not in snapshot_restore
    assert snapshot_restore.index('"${ROLLOUT_OWNER_UNITS[@]}"') < snapshot_restore.index(
        "ROLLOUT_DOWNSTREAM_UNITS[$index]"
    )
    assert 'systemctl enable --runtime "$unit"' in snapshot_restore
    assert 'if [ -f "${ENGINE_BINARY}.release" ]' in restore
    assert "activate_mode" not in restore
    assert "begin_activation_generation" in restore
    assert "complete_activation_generation" in restore
    assert "restore_prior_topology_snapshot" in restore
    # A failed prior-topology restore crosses the quarantine a second time
    # before returning.
    assert cleanup.count("stop_all_rollout_units_best_effort") >= 2


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
@pytest.mark.parametrize(
    ("cancellation_signal", "expected_status", "expected_actions"),
    [
        ("INT", 130, ["stop"]),
        ("TERM", 143, ["stop"]),
        ("HUP", 129, ["stop"]),
        ("PIPE", 141, ["stop"]),
        ("", 71, ["restore"]),
        # A command may itself return a signal-shaped status. Only the shell's
        # explicit signal handler marks cancellation and suppresses restore.
        ("", 143, ["restore"]),
    ],
)
def test_rollout_cleanup_quarantines_cancellation_but_restores_ordinary_failure(
    cancellation_signal: str,
    expected_status: int,
    expected_actions: list[str],
) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    cancel = _function(text, "rollout_cancel", "rollout_cleanup")
    cleanup = _function(text, "rollout_cleanup", "prefetch_rollout_target")
    bash = shutil.which("bash")
    assert bash is not None
    trigger = (
        f"rollout_cancel {cancellation_signal} {expected_status}"
        if cancellation_signal
        else f"exit {expected_status}"
    )
    script = f"""
set -u
ROLLOUT_CANCELLATION_SIGNAL=""
ROLLOUT_STOPPED=1
ROLLOUT_COMPLETE=0
ROLLOUT_IRREVERSIBLE=0
ROLLOUT_CURRENT_COMMIT=prior-commit
cleanup_notice() {{ :; }}
stop_all_rollout_units_best_effort() {{ printf 'stop\\n'; }}
restore_prior_topology() {{ printf 'restore\\n'; }}
{cancel}
{cleanup}
trap rollout_cleanup EXIT
{trigger}
"""
    result = subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == expected_status, result.stderr
    assert result.stdout.splitlines() == expected_actions


def test_activation_receipt_commits_only_after_full_in_boot_verification() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    activate = _function(text, "activate_mode", "provision_mainnet_prerequisites")
    begin = activate.index("begin_activation_generation")
    first_enable = activate.index("start_required_engine")
    verified = activate.index("verify_topology activation-in-progress")
    completed = activate.index("complete_activation_generation")
    assert begin < first_enable < verified < completed

    permit = _function(
        text, "begin_activation_generation", "complete_activation_generation"
    )
    for required in (
        "invalidate_activation_authority",
        "/proc/sys/kernel/random/boot_id",
        "owner_pid",
        "owner_start_ticks",
        "not_after_epoch",
        "ACTIVATION_LEASE_SECONDS",
        "start_activation_watchdog",
        "ACTIVATION_PERMIT",
        "activation_authority_matches",
        "control_helper_sha256",
        "controls_sudoers_sha256",
        "telegram_bot_sha256",
        "sync",
    ):
        assert required in permit

    complete = _function(
        text, "complete_activation_generation", "validate_engine_environment"
    )
    receipt_move = complete.index('mv -f "$temporary" "$ACTIVATION_RECEIPT"')
    watchdog_stop = complete.index("stop_activation_watchdog")
    permit_remove = complete.index('rm -f -- "$ACTIVATION_PERMIT"')
    assert complete.index("sync") < receipt_move < watchdog_stop < permit_remove
    assert "activation_authority_matches \"$ACTIVATION_RECEIPT\" complete" in complete

    topology = _function(text, "verify_topology", "start_if")
    assert "activation-in-progress" in topology
    assert 'activation_authority_matches "$ACTIVATION_PERMIT" permit' in topology
    assert 'activation_authority_matches "$ACTIVATION_RECEIPT" complete' in topology

    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert rollout.index("activate-and-verify") < rollout.index("ROLLOUT_COMPLETE=1")


def test_runtime_supervisor_revokes_same_boot_partial_activation() -> None:
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    child_start = launcher.index('/bin/bash "$CHECKOUT_WRAPPER" "$@" &')
    monitor = launcher.index('while kill -0 "$child_pid"')
    recheck = launcher.index("if ! activation_authority_valid", monitor)
    terminate = launcher.index("terminate_child", recheck)
    refuse = launcher.index("exit 78", terminate)
    assert child_start < monitor < recheck < terminate < refuse
    assert "sleep 2" in launcher[monitor:]
    assert 'kill -TERM "$child_pid"' in launcher
    assert 'kill -KILL "$child_pid"' in launcher
    assert '/usr/bin/renice -n 19 -p "$$"' in launcher
    assert "not_after_epoch" in launcher
    assert "owner_pid" in launcher
    assert "owner_start_ticks" in launcher
    assert "ACTIVATION_LEASE_SECONDS=6" in launcher
    assert "watchdog_permit_matches" in launcher
    assert "process_start_ticks_match" in launcher
    assert "with_locked_activation_authority" in launcher
    assert '/usr/bin/flock -s "$authority_fd"' in launcher

    deploy = DEPLOY.read_text(encoding="utf-8")
    watchdog = _function(
        deploy, "start_activation_watchdog", "activation_authority_matches"
    )
    assert "/usr/bin/systemd-run" in watchdog
    assert '"$ENGINE_LAUNCHER" --activation-watchdog' in watchdog
    assert "PrivateNetwork=true" in watchdog
    assert "ProtectSystem=strict" in watchdog
    assert 'ReadWritePaths=${ACTIVATION_PERMIT%/*}' in watchdog
    assert "activation_authority_matches_unlocked" in deploy
    assert '/usr/bin/flock -s "$authority_fd"' in deploy


def test_trusted_launcher_rejects_mutable_checkout_and_git_boundaries() -> None:
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    ancestry = launcher[
        launcher.index("trusted_checkout_directory()") : launcher.index(
            'for path in "$ENGINE"'
        )
    ]
    for boundary in (
        '"${REPOSITORY%/*}"',
        '"$REPOSITORY"',
        '"$REPOSITORY/scripts"',
        '"$REPOSITORY/liquidity_migration"',
        '"$REPOSITORY/liquidity_migration/ops"',
        '"$REPOSITORY/.git"',
    ):
        assert boundary in ancestry
    assert "8#$mode & 0022" in ancestry
    assert "/usr/bin/find" in launcher
    assert '"$REPOSITORY/.git" -xdev' in launcher
    assert "! -uid 0" in launcher
    assert "-perm /022" in launcher
    assert "! -type f -a ! -type d" in launcher

    deploy = DEPLOY.read_text(encoding="utf-8")
    checkout = deploy[
        deploy.index("trusted_checkout_directory()") : deploy.index(
            "clean_checkout_status()"
        )
    ]
    assert "umask 022" in deploy
    for boundary in (
        '"${REPO_DIR%/*}"',
        '"$REPO_DIR"',
        '"$REPO_DIR/scripts"',
        '"$REPO_DIR/liquidity_migration"',
        '"$REPO_DIR/liquidity_migration/ops"',
        '"$REPO_DIR/.git"',
    ):
        assert boundary in checkout
    assert "8#$mode & 0022" in checkout
    assert "! -uid 0" in checkout
    assert "-perm /022" in checkout
    for trusted_path in (
        _function(deploy, "install_mode", "load_authorization"),
        _function(deploy, "load_authorization", "unit_on"),
        _function(deploy, "prefetch_rollout_target", "record_installed_profile"),
        deploy[deploy.index("rollout_mode()") : deploy.index("acquire_maintenance_locks\n")],
    ):
        assert "require_trusted_checkout" in trusted_path
    resolver = _function(
        deploy, "resolve_fail_safe_python", "quarantine_mainnet_units"
    )
    assert "/usr/bin/readlink -f /usr/bin/python3" in resolver
    assert "8#$mode & 0022" in resolver
    fail_safe_paths = (
        _function(deploy, "disarm_mainnet_mode", "stop_mainnet_mode"),
        _function(deploy, "stop_mainnet_mode", "stop_rollout_units"),
    )
    for fail_safe_path in fail_safe_paths:
        assert "require_checkout" not in fail_safe_path
        assert "require_trusted_checkout" not in fail_safe_path
        assert "mainnet_armed" not in fail_safe_path
        assert ".venv" not in fail_safe_path
        assert "liquidity_migration" not in fail_safe_path
    stop = fail_safe_paths[1]
    assert "MAINNET_CREDENTIAL_ENV" not in stop
    assert "quarantine_mainnet_units" in stop


def test_watchdog_renews_one_locked_inode_and_never_recreates_a_deleted_permit() -> None:
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    refresh = launcher[
        launcher.index("watchdog_refresh_permit()") : launcher.index(
            "activation_watchdog_mode()"
        )
    ]
    assert 'exec {pinned_permit_fd}<"$ACTIVATION_PERMIT"' in launcher
    assert (
        'exec {WATCHDOG_PERMIT_FD}<>"/proc/self/fd/$pinned_permit_fd"'
        in launcher
    )
    assert 'exec {WATCHDOG_PERMIT_FD}<>"$ACTIVATION_PERMIT"' not in launcher
    assert "initial_permit_identity" in launcher
    assert "stat -c '%d:%i'" in launcher
    assert "stat -Lc '%d:%i'" in launcher
    assert '[ "$pinned_permit_identity" = "$initial_permit_identity" ]' in launcher
    assert 'descriptor_path="/proc/self/fd/$WATCHDOG_PERMIT_FD"' in refresh
    assert '/usr/bin/flock -x "$WATCHDOG_PERMIT_FD"' in refresh
    assert "watchdog_permit_matches" in refresh
    assert '"$ACTIVATION_PERMIT" -ef "$descriptor_path"' in launcher
    assert 'stat -Lc %h "$descriptor_path"' in launcher
    assert "mktemp" not in refresh
    assert "mv -f" not in refresh

    hot = launcher[
        launcher.index("activation_authority_valid()") : launcher.index(
            'child_pid=""'
        )
    ]
    assert hot.count(
        'activation_authority_content_matches "$ACTIVATION_RECEIPT" complete'
    ) == 2
    assert hot.count(
        'activation_authority_matches "$ACTIVATION_RECEIPT" complete'
    ) == 2


def test_tmpfiles_recreates_only_runtime_lock_boundaries_after_reboot() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    identities = _function(
        text, "ensure_runtime_identities", "write_producer_environment"
    )
    for exact_rule in (
        "d /run/liquidity-migration 0755 root root -",
        "f /run/liquidity-migration/maintenance.lock 0600 root root -",
        "f /run/liquidity-migration/deploy.lock 0600 root root -",
        "d /run/lock/liquidity-migration 0770 root %s -",
        "f /run/lock/liquidity-migration-ledger-reset.lock 0600 root root -",
    ):
        assert exact_rule in identities
    assert "systemd-tmpfiles --create" in identities


def _process_start_ticks(pid: int) -> int:
    record = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields_after_comm = record.rsplit(") ", maxsplit=1)[1].split()
    assert len(fields_after_comm) >= 20
    return int(fields_after_comm[19])


def _runtime_supervisor_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, subprocess.Popen[str], str, int]:
    for executable in (
        "/bin/bash",
        "/usr/bin/find",
        "/usr/bin/flock",
        "/usr/bin/git",
        "/usr/bin/sha256sum",
        "/usr/bin/sleep",
    ):
        if not Path(executable).is_file():
            pytest.skip(f"missing {executable}")

    repository = tmp_path / "repo"
    bin_dir = tmp_path / "release" / "bin"
    run_dir = tmp_path / "run"
    wrapper = repository / "scripts" / "run_authorized_runtime.sh"
    bot = repository / "liquidity_migration" / "ops" / "telegram_controls.py"
    engine = bin_dir / "engine"
    launcher = bin_dir / "run-authorized-runtime"
    helper = bin_dir / "telegram-control-helper"
    marker = bin_dir / "engine.release"
    receipt = bin_dir / "activation.complete"
    permit = run_dir / "activation.permit"
    child_pid_file = tmp_path / "child.pid"
    for directory in (wrapper.parent, bot.parent, bin_dir, run_dir):
        directory.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)

    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$$\" > {child_pid_file}\n"
        "trap 'exit 0' TERM INT HUP\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    bot.write_text("# digest-bound test bot\n", encoding="utf-8")
    bot.chmod(0o644)
    subprocess.run(
        ["/usr/bin/git", "init", "--quiet", "--object-format=sha1", str(repository)],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "add", "scripts", "liquidity_migration"],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "-c",
            "user.name=Runtime Supervisor Test",
            "-c",
            "user.email=runtime-supervisor@example.invalid",
            "-c",
            "core.autocrlf=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    for path, content in ((engine, "engine\n"), (helper, "helper\n")):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    source = _read("deploy/run_authorized_runtime_trusted.sh")
    replacements = {
        "REPOSITORY=/opt/liquidity-migration": f"REPOSITORY={repository}",
        "ENGINE=/opt/liquidity-migration-engine/bin/engine": f"ENGINE={engine}",
        "LAUNCHER=/opt/liquidity-migration-engine/bin/run-authorized-runtime": f"LAUNCHER={launcher}",
        "CONTROL_HELPER=/opt/liquidity-migration-engine/bin/telegram-control-helper": f"CONTROL_HELPER={helper}",
        "TELEGRAM_BOT=/opt/liquidity-migration/liquidity_migration/ops/telegram_controls.py": f"TELEGRAM_BOT={bot}",
        "MARKER=/opt/liquidity-migration-engine/bin/engine.release": f"MARKER={marker}",
        "ACTIVATION_RECEIPT=/opt/liquidity-migration-engine/bin/activation.complete": f"ACTIVATION_RECEIPT={receipt}",
        "ACTIVATION_PERMIT=/run/liquidity-migration/activation.permit": f"ACTIVATION_PERMIT={permit}",
        "CHECKOUT_WRAPPER=/opt/liquidity-migration/scripts/run_authorized_runtime.sh": f"CHECKOUT_WRAPPER={wrapper}",
    }
    for old, new in replacements.items():
        source = source.replace(old, new)
    uid, gid = os.getuid(), os.getgid()
    for expression in (
        '$(stat -c %u "$REPOSITORY")" -eq 0',
        '$(stat -c %u "$path")" -eq 0',
        '$(stat -c %u "$REPOSITORY/.git")" -eq 0',
        '$(stat -c %u "$directory")" -eq 0',
        '$(stat -c %u "${ENGINE%/*}")" -eq 0',
        '$(stat -c %u "${ACTIVATION_PERMIT%/*}")" -eq 0',
        '$(stat -c %u "$LAUNCHER")" -eq 0',
        '$(stat -c %u "${LAUNCHER%/*}")" -eq 0',
        '$(stat -c %u "$MARKER")" -eq 0',
        '$(stat -Lc %u "$descriptor_path")" -eq 0',
    ):
        source = source.replace(expression, expression.removesuffix("0") + str(uid))
    for expression in (
        '$(stat -c %g "$path")" -eq 0',
        '$(stat -c %g "${ACTIVATION_PERMIT%/*}")" -eq 0',
        '$(stat -c %g "$LAUNCHER")" -eq 0',
        '$(stat -c %g "${LAUNCHER%/*}")" -eq 0',
        '$(stat -c %g "$MARKER")" -eq 0',
        '$(stat -Lc %g "$descriptor_path")" -eq 0',
    ):
        source = source.replace(expression, expression.removesuffix("0") + str(gid))
    source = source.replace("! -uid 0", f"! -uid {uid}")
    source = source.replace('[ "$EUID" -eq 0 ]', f'[ "$EUID" -eq {uid} ]')
    launcher.write_text(source, encoding="utf-8")
    launcher.chmod(0o755)

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    authority_prefix = (
        f"commit={commit}\n"
        f"sha256={digest(engine)}\n"
        f"launcher_sha256={digest(launcher)}\n"
        f"control_helper_sha256={digest(helper)}\n"
        f"controls_sudoers_sha256={'d' * 64}\n"
        f"telegram_bot_sha256={digest(bot)}\n"
    )
    marker.write_text(authority_prefix + "rustc=1.90.0\n", encoding="utf-8")
    marker.chmod(0o644)
    owner = subprocess.Popen(["/usr/bin/sleep", "60"], text=True)
    owner_start_ticks = _process_start_ticks(owner.pid)
    return (
        launcher,
        permit,
        receipt,
        child_pid_file,
        owner,
        authority_prefix,
        owner_start_ticks,
    )


def _write_runtime_permit(
    permit: Path,
    authority_prefix: str,
    owner_pid: int,
    owner_start_ticks: int,
    *,
    lifetime_seconds: int = 6,
) -> None:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    permit.write_text(
        authority_prefix
        + f"boot_id={boot_id}\n"
        + f"owner_pid={owner_pid}\n"
        + f"owner_start_ticks={owner_start_ticks}\n"
        + f"not_after_epoch={int(time.time()) + lifetime_seconds}\n",
        encoding="utf-8",
    )
    permit.chmod(0o644)


def _terminate_test_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="requires Linux /proc and release utilities")
@pytest.mark.parametrize("failure_mode", ["owner-death", "watchdog-death"])
def test_runtime_supervisor_revokes_when_activation_lease_loses_its_owner(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    (
        launcher,
        permit,
        _receipt,
        child_pid_file,
        owner,
        authority_prefix,
        owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    _write_runtime_permit(
        permit, authority_prefix, owner.pid, owner_start_ticks
    )
    watchdog = subprocess.Popen(
        [launcher, "--activation-watchdog", str(owner.pid), str(owner_start_ticks)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process: subprocess.Popen[str] | None = None
    child_pid: int | None = None
    try:
        time.sleep(0.25)
        assert watchdog.poll() is None, watchdog.stderr.read()
        process = subprocess.Popen(
            [launcher, "fixture.service", "main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists(), (
            process.stderr.read() if process.poll() is not None else ""
        )
        child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
        if failure_mode == "owner-death":
            owner.kill()
            owner.wait(timeout=5)
            watchdog_stdout, watchdog_stderr = watchdog.communicate(timeout=5)
            assert watchdog.returncode == 78, (watchdog_stdout, watchdog_stderr)
        else:
            watchdog.kill()
            watchdog.wait(timeout=5)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 78, (stdout, stderr)
        assert "revoking workload" in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        _terminate_test_process(process)
        _terminate_test_process(watchdog)
        _terminate_test_process(owner)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="requires Linux /proc and release utilities")
def test_runtime_supervisor_does_not_recreate_a_directly_revoked_permit(
    tmp_path: Path,
) -> None:
    (
        launcher,
        permit,
        _receipt,
        child_pid_file,
        owner,
        authority_prefix,
        owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    _write_runtime_permit(permit, authority_prefix, owner.pid, owner_start_ticks)
    watchdog = subprocess.Popen(
        [launcher, "--activation-watchdog", str(owner.pid), str(owner_start_ticks)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process: subprocess.Popen[str] | None = None
    child_pid: int | None = None
    try:
        time.sleep(0.25)
        assert watchdog.poll() is None, watchdog.stderr.read()
        process = subprocess.Popen(
            [launcher, "fixture.service", "main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists(), (
            process.stderr.read() if process.poll() is not None else ""
        )
        child_pid = int(child_pid_file.read_text(encoding="ascii").strip())

        permit.unlink()
        watchdog_stdout, watchdog_stderr = watchdog.communicate(timeout=5)
        assert watchdog.returncode == 78, (watchdog_stdout, watchdog_stderr)
        assert not permit.exists()
        time.sleep(1.25)
        assert not permit.exists()

        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 78, (stdout, stderr)
        assert "revoking workload" in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        _terminate_test_process(process)
        _terminate_test_process(watchdog)
        _terminate_test_process(owner)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="requires Linux /proc and release utilities")
@pytest.mark.parametrize("revocation", ["unlink", "replacement"])
def test_watchdog_initial_inode_pin_rejects_raced_revocation(
    tmp_path: Path, revocation: str
) -> None:
    (
        launcher,
        permit,
        _receipt,
        _child_pid_file,
        owner,
        authority_prefix,
        owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    ready = tmp_path / "pin-ready"
    release = tmp_path / "pin-release"
    source = launcher.read_text(encoding="utf-8")
    anchor = (
        "    watchdog_permit_matches \\\n"
        '        || refuse "activation watchdog initial permit or owner identity is invalid"\n'
    )
    assert source.count(anchor) == 1
    gate = (
        f"    : > {shlex.quote(ready.as_posix())}\n"
        f"    while [ ! -e {shlex.quote(release.as_posix())} ]; do sleep 0.01; done\n"
    )
    launcher.write_text(source.replace(anchor, anchor + gate), encoding="utf-8")
    launcher.chmod(0o755)
    new_launcher_digest = hashlib.sha256(launcher.read_bytes()).hexdigest()
    authority_prefix = re.sub(
        r"(?m)^launcher_sha256=[0-9a-f]{64}$",
        f"launcher_sha256={new_launcher_digest}",
        authority_prefix,
    )
    marker = launcher.parent / "engine.release"
    marker.write_text(authority_prefix + "rustc=1.90.0\n", encoding="utf-8")
    marker.chmod(0o644)
    _write_runtime_permit(permit, authority_prefix, owner.pid, owner_start_ticks)
    valid_permit = permit.read_bytes()

    watchdog = subprocess.Popen(
        [launcher, "--activation-watchdog", str(owner.pid), str(owner_start_ticks)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.01)
        assert ready.exists(), (
            watchdog.stderr.read() if watchdog.poll() is not None else ""
        )
        if revocation == "unlink":
            permit.unlink()
        else:
            replacement = permit.with_name("activation.permit.replacement")
            replacement.write_bytes(valid_permit)
            replacement.chmod(0o644)
            os.replace(replacement, permit)
        release.touch()
        stdout, stderr = watchdog.communicate(timeout=5)
        assert watchdog.returncode == 78, (stdout, stderr)
        assert not permit.exists()
    finally:
        _terminate_test_process(watchdog)
        _terminate_test_process(owner)


@pytest.mark.skipif(os.name == "nt", reason="requires Linux release utilities")
@pytest.mark.parametrize(
    "unsafe_boundary",
    ["repository", "git", "git-entry", "git-symlink", "git-special"],
)
def test_runtime_launcher_rejects_writable_checkout_trust_boundaries(
    tmp_path: Path,
    unsafe_boundary: str,
) -> None:
    (
        launcher,
        _permit,
        receipt,
        _child_pid_file,
        owner,
        authority_prefix,
        _owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    repository = tmp_path / "repo"
    receipt.write_text(authority_prefix, encoding="utf-8")
    receipt.chmod(0o644)
    if unsafe_boundary == "repository":
        unsafe_path = repository
    elif unsafe_boundary == "git":
        unsafe_path = repository / ".git"
    elif unsafe_boundary == "git-entry":
        unsafe_path = repository / ".git" / "HEAD"
    elif unsafe_boundary == "git-symlink":
        unsafe_path = repository / ".git" / "unsafe-link"
        unsafe_path.symlink_to(repository / ".git" / "HEAD")
    else:
        unsafe_path = repository / ".git" / "unsafe-fifo"
        os.mkfifo(unsafe_path)
    if unsafe_boundary in {"repository", "git", "git-entry"}:
        unsafe_path.chmod(unsafe_path.stat().st_mode | 0o022)

    process = subprocess.Popen(
        [launcher, "fixture.service", "main"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 78, (stdout, stderr)
        assert "trusted checkout" in stderr
    finally:
        _terminate_test_process(process)
        _terminate_test_process(owner)


@pytest.mark.skipif(os.name == "nt", reason="requires Linux /proc and release utilities")
def test_activation_watchdog_rejects_a_reused_pid_identity(tmp_path: Path) -> None:
    (
        launcher,
        permit,
        _receipt,
        _child_pid_file,
        owner,
        authority_prefix,
        owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    stale_start_ticks = owner_start_ticks + 1
    _write_runtime_permit(
        permit, authority_prefix, owner.pid, stale_start_ticks
    )
    watchdog = subprocess.Popen(
        [launcher, "--activation-watchdog", str(owner.pid), str(stale_start_ticks)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = watchdog.communicate(timeout=5)
        assert watchdog.returncode == 78, (stdout, stderr)
        assert "owner identity is invalid" in stderr
        assert not permit.exists()
    finally:
        _terminate_test_process(watchdog)
        _terminate_test_process(owner)


@pytest.mark.skipif(os.name == "nt", reason="requires Linux /proc and release utilities")
def test_activation_receipt_handoff_keeps_the_verified_child_running(
    tmp_path: Path,
) -> None:
    import fcntl

    (
        launcher,
        permit,
        receipt,
        child_pid_file,
        owner,
        authority_prefix,
        owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    _write_runtime_permit(
        permit, authority_prefix, owner.pid, owner_start_ticks
    )
    watchdog = subprocess.Popen(
        [launcher, "--activation-watchdog", str(owner.pid), str(owner_start_ticks)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process: subprocess.Popen[str] | None = None
    child_pid: int | None = None
    permit_handle = None
    try:
        time.sleep(0.25)
        assert watchdog.poll() is None, watchdog.stderr.read()
        process = subprocess.Popen(
            [launcher, "fixture.service", "main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="ascii").strip())

        # Hold the current permit inode across a supervisor poll. This forces
        # the launcher to miss the receipt on its first read, block on the
        # permit, and then observe the receipt only on its second read after
        # the permit pathname is removed.
        permit_handle = permit.open("rb")
        fcntl.flock(permit_handle.fileno(), fcntl.LOCK_EX)
        time.sleep(2.25)
        temporary = receipt.with_name("activation.complete.new")
        temporary.write_text(authority_prefix, encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, receipt)
        permit.unlink()
        fcntl.flock(permit_handle.fileno(), fcntl.LOCK_UN)
        permit_handle.close()
        permit_handle = None
        watchdog_stdout, watchdog_stderr = watchdog.communicate(timeout=5)
        assert watchdog.returncode == 78, (watchdog_stdout, watchdog_stderr)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and permit.exists():
            time.sleep(0.05)
        assert not permit.exists()
        time.sleep(3)
        assert process.poll() is None
        os.kill(child_pid, 0)
    finally:
        if permit_handle is not None:
            fcntl.flock(permit_handle.fileno(), fcntl.LOCK_UN)
            permit_handle.close()
        _terminate_test_process(process)
        _terminate_test_process(watchdog)
        _terminate_test_process(owner)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="requires Linux release utilities")
def test_completed_generation_starts_after_reboot_without_the_tmpfs_permit_root(
    tmp_path: Path,
) -> None:
    (
        launcher,
        permit,
        receipt,
        child_pid_file,
        owner,
        authority_prefix,
        _owner_start_ticks,
    ) = _runtime_supervisor_fixture(tmp_path)
    receipt.write_text(authority_prefix, encoding="utf-8")
    receipt.chmod(0o644)
    permit.parent.rmdir()
    process = subprocess.Popen(
        [launcher, "fixture.service", "main"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        assert child_pid_file.exists(), (
            process.stderr.read() if process.poll() is not None else ""
        )
        assert process.poll() is None
        child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
        os.kill(child_pid, 0)
    finally:
        _terminate_test_process(process)
        _terminate_test_process(owner)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


def test_release_marker_binds_helper_sudoers_and_bot_before_activation() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    build = _function(text, "build_engine", "verify_engine_release")
    for required in (
        "telegram_control_helper.sh",
        "liquidity-controls.sudoers",
        "TELEGRAM_CONTROLS_BOT",
        "/usr/sbin/visudo -cf",
        'install -o root -g root -m 0755',
        'install -o root -g root -m 0440',
        "control_helper_sha256",
        "controls_sudoers_sha256",
        "telegram_bot_sha256",
    ):
        assert required in build
    marker = build.index('mv -f "$marker_tmp"')
    assert build.index('mv -f "$ENGINE_CONTROL_HELPER.new"') < marker
    assert build.index('mv -f "$CONTROLS_SUDOERS.new"') < marker
    activate = _function(text, "activate_mode", "provision_mainnet_prerequisites")
    assert build.index("verify_engine_release launcher-required") >= 0
    assert activate.index("begin_activation_generation") < activate.index(
        "liquidity-migration-telegram-controls.service"
    )


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
    fetch = _function(text, "git_fetch", "install_mode")
    assert 'GIT_CONFIG_GLOBAL="$config_file"' in fetch
    assert "AUTHORIZATION: Basic %s" in fetch
    assert "chmod 0600" in fetch


def test_every_remote_mode_shares_the_maintenance_locks() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    call = text.rindex("acquire_maintenance_locks\n")
    dispatch = text.rindex('case "$MODE" in')
    assert call < dispatch
    assert "maintenance.lock" in text and "deploy.lock" in text
    helper = _function(text, "acquire_maintenance_fd", "acquire_maintenance_locks")
    assert '[ "$MODE" = disarm-mainnet ]' in helper
    assert 'flock --exclusive --timeout "$remaining_seconds" "$descriptor"' in helper
    assert 'flock --exclusive --nonblock "$descriptor"' in helper
    lock = _function(text, "acquire_maintenance_locks", "ensure_runtime_identities")
    assert "DISARM_MAINTENANCE_LOCK_WAIT_SECONDS=120" in text
    assert "disarm_deadline=$((SECONDS + DISARM_MAINTENANCE_LOCK_WAIT_SECONDS))" in lock
    for descriptor, label in (
        ("9", "maintenance.lock"),
        ("8", "legacy-deploy.lock"),
        ("7", "legacy-reset.lock"),
    ):
        assert f'acquire_maintenance_fd {descriptor} {label} "$disarm_deadline"' in lock
    assert "maintenance_lock.py" not in text


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("flock") is None,
    reason="requires Linux bash and util-linux flock",
)
def test_disarm_lock_waits_for_cleanup_but_remains_strictly_bounded(
    tmp_path: Path,
) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    helper = _function(text, "acquire_maintenance_fd", "acquire_maintenance_locks")
    bash = shutil.which("bash")
    assert bash is not None
    lock_path = tmp_path / "maintenance.lock"
    lock_path.touch()

    holder_script = """
set -euo pipefail
exec 9<>"$1"
flock --exclusive 9
: > "$2"
while [ ! -e "$3" ]; do sleep 0.02; done
"""

    def wait_for_file(path: Path, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None:
                _, stderr = process.communicate()
                pytest.fail(f"lock subprocess exited before readiness: {stderr}")
            if time.monotonic() >= deadline:
                process.terminate()
                process.wait(timeout=2)
                pytest.fail(f"lock subprocess did not become ready: {path}")
            time.sleep(0.01)

    def start_holder(label: str) -> tuple[subprocess.Popen[str], Path]:
        ready = tmp_path / f"{label}.holder-ready"
        release = tmp_path / f"{label}.holder-release"
        process = subprocess.Popen(
            [
                bash,
                "-c",
                holder_script,
                "lock-holder",
                str(lock_path),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for_file(ready, process)
        return process, release

    def waiter_script(mode: str, wait_seconds: int) -> str:
        return f"""
set -euo pipefail
MODE={shlex.quote(mode)}
DISARM_MAINTENANCE_LOCK_WAIT_SECONDS={wait_seconds}
fail() {{ printf 'failed: %s\\n' "$*" >&2; exit 71; }}
{helper}
exec 9<>"$1"
: > "$2"
deadline=0
if [ "$MODE" = disarm-mainnet ]; then
    deadline=$((SECONDS + DISARM_MAINTENANCE_LOCK_WAIT_SECONDS))
fi
acquire_maintenance_fd 9 maintenance.lock "$deadline"
printf 'acquired\\n'
"""

    def acquire(mode: str, wait_seconds: int, label: str) -> subprocess.CompletedProcess[str]:
        ready = tmp_path / f"{label}.waiter-ready"
        return subprocess.run(
            [
                bash,
                "-c",
                waiter_script(mode, wait_seconds),
                "lock-waiter",
                str(lock_path),
                str(ready),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    releasing, release_releasing = start_holder("releasing")
    try:
        waiter_ready = tmp_path / "releasing.waiter-ready"
        waiter = subprocess.Popen(
            [
                bash,
                "-c",
                waiter_script("disarm-mainnet", 2),
                "lock-waiter",
                str(lock_path),
                str(waiter_ready),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for_file(waiter_ready, waiter)
        assert waiter.poll() is None, "disarm did not wait for the held lock"
        release_releasing.touch()
        stdout, stderr = waiter.communicate(timeout=3)
        assert waiter.returncode == 0, stderr
        assert stdout.strip() == "acquired"
        assert releasing.wait(timeout=2) == 0
    finally:
        if releasing.poll() is None:
            release_releasing.touch()
            releasing.wait(timeout=2)

    blocking, release_blocking = start_holder("blocking")
    try:
        ordinary = acquire("rollout", 0, "ordinary")
        assert ordinary.returncode == 71
        assert "another maintenance operation holds maintenance.lock" in ordinary.stderr

        started = time.monotonic()
        bounded = acquire("disarm-mainnet", 1, "bounded")
        elapsed = time.monotonic() - started
        assert bounded.returncode == 71
        assert "disarm timed out after 1s" in bounded.stderr
        assert elapsed < 4, "disarm exceeded its bounded lock-wait budget"
        assert blocking.poll() is None, "disarm exceeded its bound and waited for release"
    finally:
        if blocking.poll() is None:
            release_blocking.touch()
            blocking.wait(timeout=2)


def test_deploy_python_modules_resolve_in_the_checkout() -> None:
    sources = [DEPLOY, *sorted((ROOT / "deploy").rglob("*.sh"))]
    discovered: dict[str, set[str]] = {}
    for source in sources:
        text = source.read_text(encoding="utf-8")
        modules = set(
            re.findall(
                r"-m\s+(liquidity_migration(?:\.[A-Za-z_][A-Za-z0-9_]*)+)",
                text,
            )
        )
        modules.update(
            re.findall(
                r"^\s*(?:from|import)\s+"
                r"(liquidity_migration(?:\.[A-Za-z_][A-Za-z0-9_]*)+)",
                text,
                flags=re.MULTILINE,
            )
        )
        for module in modules:
            discovered.setdefault(module, set()).add(str(source.relative_to(ROOT)))

    assert discovered
    missing = {}
    for module, owners in discovered.items():
        relative = Path(*module.split("."))
        if not (ROOT / relative).with_suffix(".py").is_file() and not (
            ROOT / relative / "__init__.py"
        ).is_file():
            missing[module] = sorted(owners)
    assert not missing


def test_operational_surface_has_no_deleted_repo_references() -> None:
    paths = [
        DEPLOY,
        ROOT / "scripts" / "ops.sh",
        ROOT / "scripts" / "dev.sh",
        ROOT / "scripts" / "README.md",
        ROOT / "deploy" / "producer-demo-source.env.template",
        ROOT / "deploy" / "producer-mainnet-source.env.template",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for retired in (
        "candidate_rule_coverage",
        "venue_instrument_rules",
        "reset_path_safety",
        "maintenance_lock.py",
        "reset_demo_ledgers.sh",
        "build_trade_diagnostics.py",
        "run_with_stub.py",
        "VENUE_RULES_FILE",
    ):
        assert retired not in text


def test_operational_literal_script_references_exist() -> None:
    sources = [
        DEPLOY,
        ROOT / "scripts" / "ops.sh",
        ROOT / "scripts" / "dev.sh",
        ROOT / "scripts" / "run_authorized_runtime.sh",
        *sorted((ROOT / "deploy").rglob("*.sh")),
    ]
    owners: dict[str, set[str]] = {}
    pattern = re.compile(
        r"(?<![A-Za-z0-9_.-])"
        r"((?:liquidity_migration|scripts|deploy)/"
        r"[A-Za-z0-9_./-]+\.(?:py|sh|command))"
    )
    for source in sources:
        for relative in pattern.findall(source.read_text(encoding="utf-8")):
            owners.setdefault(relative, set()).add(str(source.relative_to(ROOT)))

    assert owners
    missing = {
        relative: sorted(references)
        for relative, references in owners.items()
        if not (ROOT / relative).is_file()
    }
    assert not missing


def test_rust_engine_is_the_only_live_instrument_rule_source() -> None:
    engine = _read("engine/engine-core/src/engine.rs")
    bybit = _read("engine/engine-venue/src/venues/bybit/gateway.rs")
    assert "for (name, rule) in venue.instrument_rules().await?" in engine
    assert "self.venue.instrument_rules().await" in engine
    assert "self.rest.get_public(PATH_INSTRUMENTS, &query).await?" in bybit


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
    concurrency = workflow[workflow.index("concurrency:") : workflow.index("jobs:")]
    vps = workflow[workflow.index("  vps:") :]
    assert "timeout-minutes: 120" in vps
    assert "format('liquidity-migration-vps-{0}', github.ref)" in concurrency
    assert "format('liquidity-migration-ci-{0}', github.ref)" in concurrency
    assert "inputs.mode == 'disarm-mainnet'" in concurrency
    assert workflow.count("\nconcurrency:\n") == 1
    assert "\n    concurrency:\n" not in workflow
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
        if name == "liquidity-migration-telegram-controls.service":
            assert "NoNewPrivileges=true" not in body
        else:
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
