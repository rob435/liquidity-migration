from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
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


def _python_heredoc(block: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\nPY(?:\n|$)", block, re.DOTALL)
    assert match is not None
    return match.group(1)


def _units() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(SYSTEMD.glob("liquidity-migration-*.*"))
        if path.suffix in {".service", ".timer"}
    }


def test_no_guard_ends_a_deploy_function_as_an_and_list() -> None:
    """`check && fail ...` as a function's last statement returns the check's
    own status, so a healthy check makes the function return 1 with nothing
    printed. The funded liveness guard shipped that way and failed a rollout
    precisely because the watchdog was well. Use `if check; then fail; fi`.
    """
    lines = DEPLOY.read_text(encoding="utf-8").splitlines()
    offenders = [
        index + 1 for index, line in enumerate(lines[:-1]) if "&& fail" in line and lines[index + 1].rstrip() == "}"
    ]
    assert not offenders, f"&& fail ends a function at line(s) {offenders}"


def test_deployed_shell_entrypoints_are_executable() -> None:
    if os.name == "nt":
        return
    for relative in (
        "scripts/deploy_vps_live.sh",
        "scripts/ops.sh",
        "scripts/run_authorized_runtime.sh",
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
        "scripts/runtime/run_exodus_event_engine.sh",
    ):
        assert (ROOT / relative).stat().st_mode & stat.S_IXUSR, relative


def test_manifest_contains_only_the_current_rust_owned_fleet() -> None:
    units = _units()
    manifest = {
        row.split("|", 1)[0]
        for row in _read("deploy/fleet_manifest.tsv").splitlines()
        if row and not row.startswith("#") and row.split("|")[1] == "current"
    }
    assert set(units) == manifest
    assert "liquidity-migration-engine.service" in units
    assert "liquidity-migration-engine-mainnet.service" in units
    assert not any("account-execution" in name for name in units)


def _authorized_commands() -> dict[str, str]:
    """Each unit's committed command line, with any shell wrapper it names inlined."""
    dispatcher = _read("scripts/run_authorized_runtime.sh")
    body = dispatcher[dispatcher.index('case "$UNIT:$ENTRYPOINT" in') : dispatcher.index("\nesac")]
    commands: dict[str, str] = {}
    for block in body.split(";;"):
        names = re.findall(r"(liquidity-migration-[\w-]+\.service):main", block)
        text = block + "".join(
            _read(f"scripts/runtime/{wrapper}") for wrapper in re.findall(r"scripts/runtime/([\w.-]+\.sh)", block)
        )
        commands.update(dict.fromkeys(names, text))
    return commands


def test_polars_units_keep_cgroup_memory_visibility() -> None:
    """Polars sizes its memory manager from /proc/meminfo, so a unit that can
    reach it must not hide the non-process /proc files. Deriving the set from
    the dispatcher is the point: a hand-listed set stayed green while the two
    liveness units ran the same library behind ProcSubset=pid.
    """
    reaches_polars = {
        name
        for name, command in _authorized_commands().items()
        if re.search(r"-m liquidity_migration(?![\w.])", command) or "check_fleet_liveness.py" in command
    }
    assert reaches_polars == {
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-mainnet.service",
        "liquidity-migration-bybit-carry-demo.service",
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-bybit-exodus-demo.service",
        "liquidity-migration-bybit-exodus-mainnet.service",
        "liquidity-migration-demo-liveness.service",
        "liquidity-migration-mainnet-liveness.service",
    }
    units = _units()
    for name in reaches_polars:
        body = units[name]
        assert "ProtectProc=invisible" in body, name
        assert "ProcSubset=pid" not in body, name
        assert "/proc/meminfo" in body, name
    # The compiled engines read no Parquet and keep the tighter setting.
    for name in ("liquidity-migration-engine.service", "liquidity-migration-engine-mainnet.service"):
        assert "ProcSubset=pid" in units[name], name


def test_guarded_units_cross_the_installed_release_gate_before_checkout_code() -> None:
    guarded = [body for name, body in _units().items() if name.endswith(".service")]
    for body in guarded:
        assert "ExecStart=/opt/liquidity-migration-engine/bin/run-authorized-runtime" in body
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
        "exit 78",
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
    capture = units["liquidity-migration-forward-capture.service"]
    assert "User=liquidity-capture" in capture
    assert "StateDirectory=liquidity-migration/forward-market" in capture
    assert "IOSchedulingClass=idle" in capture
    assert "BYBIT_REAL_API_SECRET" in capture


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
    for action in ("pause-demo", "resume-demo", "pause-mainnet", "resume-mainnet", "status-fleet"):
        assert action in helper
    assert 'case "$ACTION" in' in helper
    assert "/usr/bin/systemd-run --quiet --wait --pipe --collect" in helper
    assert '"$HELPER" --worker "$ACTION"' in helper
    assert "demo resume requires this generation's completed activation receipt" in helper
    assert "control_helper_sha256" in helper
    assert "controls_sudoers_sha256" in helper
    assert "telegram_bot_sha256" in helper
    assert "InaccessiblePaths=" in helper
    assert "lm_target_producer_units" in helper
    assert "quarantine_units" in helper
    assert 'systemctl is-active --quiet "$unit"' in helper
    assert 'systemctl is-enabled "$unit"' in helper
    assert "demo pause could not quarantine every target producer" in helper
    assert "mainnet pause could not quarantine every funded target producer" in helper
    assert "demo resume requires the account owner to be active" in helper
    # The funded resume carries the same two proofs as the demo one, and a
    # failure to bring the manifest-selected fleet up quarantines all of it.
    assert "funded resume requires this generation's completed activation receipt" in helper
    assert "funded resume requires the funded account owner to be active" in helper
    assert "funded resume failed; every funded target producer was re-quarantined" in helper
    assert "deploy/fleet_manifest.tsv" in helper
    for producer in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
        "liquidity-migration-bybit-exodus-demo.service",
        "liquidity-migration-bybit-long-mainnet.service",
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-bybit-exodus-mainnet.service",
    ):
        assert producer not in helper

    sudoers = _read("deploy/liquidity-controls.sudoers")
    allowed = {line.split("NOPASSWD: ", 1)[1] for line in sudoers.splitlines() if "NOPASSWD: " in line}
    assert allowed == {
        f"/opt/liquidity-migration-engine/bin/telegram-control-helper {action}"
        for action in ("pause-demo", "resume-demo", "pause-mainnet", "resume-mainnet", "status-fleet")
    }
    assert "!setenv" in sudoers
    policy = _function(
        DEPLOY.read_text(encoding="utf-8"),
        "verify_controls_sudo_policy",
        "build_engine",
    )
    assert '/usr/bin/sudo -l -U "$CONTROLS_USER"' in policy
    assert policy.count("LC_ALL=C sed 's/[[:space:]]//g'") == 2
    assert policy.count("| LC_ALL=C sort") == 2
    assert "tr -d '[:space:]'" not in policy
    actual_block = policy[policy.index("actual=") : policy.index("expected=")]
    expected_block = policy[policy.index("expected=") :]
    assert all(block.index("LC_ALL=C sed") < block.index("LC_ALL=C sort") for block in (actual_block, expected_block))
    assert "exact five-command boundary" in policy
    assert "status_action=status-fleet" in policy
    assert "status_action=status-demo" in policy

    bot = _read("liquidity_migration/ops/telegram_controls.py")
    fleet = bot[bot.index("class VpsFleet:") : bot.index("# The panel")]
    assert '"/usr/bin/sudo", "-n", CONTROL_HELPER' in bot
    assert 'self._control("status-fleet")' in fleet
    assert 'systemctl", "enable' not in fleet
    assert 'systemctl", "disable' not in fleet
    assert '"systemctl", "is-active"' not in fleet
    assert "write_text(" not in fleet and "os.replace(" not in fleet
    for unit in (
        "liquidity-migration-engine.service",
        "liquidity-migration-engine-mainnet.service",
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
        "liquidity-migration-bybit-exodus-demo.service",
        "liquidity-migration-bybit-long-mainnet.service",
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-bybit-exodus-mainnet.service",
    ):
        assert unit not in bot

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
    unset = next(line for line in funded.splitlines() if line.startswith("UnsetEnvironment="))
    assert "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" not in unset
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERT_CHAT_ID"):
        assert key in unset
    for name, body in units.items():
        if not name.endswith(".service") or name == "liquidity-migration-engine-mainnet.service":
            continue
        other_unset = " ".join(line for line in body.splitlines() if line.startswith("UnsetEnvironment="))
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
            "BYBIT_REAL_API_KEY_IP",
            "BYBIT_REAL_API_KEY_BACKUP_IP",
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
        assert "CARRY_NOTIONAL_MULTIPLIER" not in body
        assert "LONG_NOTIONAL_MULTIPLIER" not in body


def test_producer_projection_is_an_explicit_non_secret_allowlist() -> None:
    block = _function(
        DEPLOY.read_text(encoding="utf-8"), "write_producer_environment", "project_mainnet_telegram_environment"
    )
    assert '"OPERATIONAL_PROFILE_FILE", "PRODUCER_REALM"' in block
    assert "CARRY_NOTIONAL_MULTIPLIER" not in block
    assert "LONG_NOTIONAL_MULTIPLIER" not in block
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


def test_both_liveness_units_can_receive_the_dead_mans_switch() -> None:
    """The watchdog code already pings LIVENESS_HEARTBEAT_URL on a healthy run.
    A dial only works where its unit loads it, so both units read the optional
    operator file that carries it; absent, the switch is simply unprovisioned.
    """
    for name in ("demo", "mainnet"):
        body = _units()[f"liquidity-migration-{name}-liveness.service"]
        assert "EnvironmentFile=-/etc/liquidity-migration/liveness.env" in body, name
        assert "LIVENESS_HEARTBEAT_URL" not in body.split("EnvironmentFile")[0], name
    source = _read("scripts/runtime/check_fleet_liveness.py")
    assert 'os.environ.get("LIVENESS_HEARTBEAT_URL")' in source


def test_both_realms_state_sole_leverage_authority() -> None:
    """An absent key means "shared", which silently costs an entry from flat a
    ~172 ms set_leverage round trip. Both realms state the value they want.
    """
    for template in ("deploy/engine.demo.toml.template", "deploy/engine.mainnet.toml.template"):
        config = tomllib.loads(_read(template))
        assert config["engine"]["leverage_authority"] == "sole", template


def test_funded_exodus_and_maker_canary_are_wired_to_the_engine() -> None:
    config = tomllib.loads(_read("deploy/engine.mainnet.toml.template"))
    strategies = config["strategy"]
    assert [row["sleeve"] for row in strategies] == [
        "carry",
        "long",
        "exodus",
        "maker_canary",
    ]
    exodus = strategies[2]
    assert exodus == {
        "name": "target_book",
        "sleeve": "exodus",
        "book_path": "/var/lib/liquidity-migration/targets/exodus-mainnet.json",
        "rest_entries": False,
        "symbols": ["DOGEUSDT"],
    }
    maker = strategies[3]
    assert maker["name"] == "quoter"
    assert maker["quote_enabled"] is False
    assert maker["symbols"] == ["AGIUSDT"]
    assert maker["qty_usdt"] == 5.25
    assert maker["max_position_usdt"] == 6.0
    assert maker["maker_fee_bps"] == 4.0
    assert maker["flow_fast_half_life_ms"] == 250.0
    assert maker["flow_slow_half_life_ms"] == 3000.0
    assert maker["flow_fast_weight"] == 0.65
    assert maker["flow_slow_weight"] == 0.35
    assert maker["flow_response_bps"] == 4.0
    assert maker["flow_max_widen_bps"] == 8.0
    assert "flow_pull_score" not in maker
    assert "toxicity_bps" not in maker
    assert "trade_lean_bps" not in maker

    carry_unit = _read("deploy/systemd/liquidity-migration-bybit-carry-mainnet.service")
    assert "EXODUS_SHORT_PROFILE" not in carry_unit
    assert "EXODUS_ENGINE_TARGET_BOOK_PATH" not in carry_unit
    assert (
        "Environment=CARRY_PRESETTLEMENT_EVENT_PATH="
        "/opt/liquidity-migration/data/bybit-carry-mainnet-event/"
        "carry_presettlement_events.jsonl"
    ) in carry_unit

    deploy = DEPLOY.read_text(encoding="utf-8")
    validation = _function(deploy, "validate_engine_environment", "quarantine_engine_inputs")
    quarantine = _function(deploy, "quarantine_engine_inputs", "wait_engine_heartbeat")
    activation = _function(deploy, "start_mainnet_fleet", "resolve_fail_safe_python")
    producer_activation = _function(deploy, "activate_target_producers", "start_required_engine")
    expected = "/var/lib/liquidity-migration/targets/exodus-mainnet.json"
    assert expected in validation
    assert "exodus-mainnet.json" in quarantine
    assert "activate_target_producers mainnet off off on" in activation
    assert "lm_output_artifact_for_unit" in producer_activation
    assert expected in _read("deploy/fleet_manifest.tsv")
    install_config = _function(deploy, "install_mainnet_engine_config", "require_rollout_for_funded_generation_change")
    install_mode = _function(deploy, "install_mode", "load_authorization")
    assert "deploy/engine.mainnet.toml.template" in install_config
    assert 'mv -f "${ENGINE_MAINNET_CONFIG}.new"' in install_config
    assert install_mode.index("checkout -B") < install_mode.index("install-mainnet-engine-config")

    flatten = _read("scripts/vps/flatten_account.sh")
    assert "lm_target_producer_units" in flatten
    assert "lm_output_artifact_for_unit" in flatten
    assert 'BOOKS+=("$TARGET_ROOT/$(basename "$artifact")")' in flatten


def test_demo_engine_template_has_an_exact_account_id_and_mainnet_requires_one() -> None:
    demo = _read("deploy/engine.env.template")
    mainnet = _read("deploy/engine.mainnet.env.template")
    assert "EXPECTED_ENGINE_ACCOUNT_USER_ID=555899665" in demo
    assert "EXPECTED_ENGINE_ACCOUNT_USER_ID=" in mainnet
    assert "must set EXPECTED_ENGINE_ACCOUNT_USER_ID to the exact venue id" in DEPLOY.read_text(encoding="utf-8")


def test_demo_engine_environment_reconciliation_is_atomic_conservative_and_idempotent(
    tmp_path: Path,
) -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    block = _function(
        deploy,
        "reconcile_demo_engine_environment",
        "prepare_demo_runtime_config",
    )
    program = _python_heredoc(block)
    source = ROOT / "deploy" / "engine.env.template"

    def write_target(path: Path, body: bytes) -> None:
        path.write_bytes(body)
        path.chmod(0o600)

    def run(target: Path) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-", str(source), str(target)],
            cwd=ROOT,
            input=program.encode("utf-8"),
            capture_output=True,
            check=False,
        )

    absent = tmp_path / "absent.env"
    result = run(absent)
    assert result.returncode == 0, result.stderr.decode()
    assert absent.read_bytes() == source.read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE(absent.stat().st_mode) == 0o600
    assert absent.stat().st_nlink == 1

    legacy = tmp_path / "legacy.env"
    original = (
        b"ENGINE_CONFIG_FILE=/etc/liquidity-migration/engine.toml\n"
        b"LIVENESS_ENGINE_HEARTBEAT_FILE="
        b"/var/lib/liquidity-migration-engine/heartbeat.json\n"
        b"EXPECTED_ENGINE_VERSION=engine-core-0.1.0\n"
        b"RUST_LOG=debug\n"
        b"HOST_ONLY_DIAL=preserved\n"
    )
    write_target(legacy, original)
    result = run(legacy)
    assert result.returncode == 0, result.stderr.decode()
    reconciled = legacy.read_bytes()
    assert reconciled.startswith(original)
    for assignment in (
        b"EXPECTED_ENGINE_ACCOUNT_USER_ID=555899665\n",
        b"EXPECTED_ENGINE_VENUE=bybit\n",
        b"EXPECTED_ENGINE_REALM=demo\n",
    ):
        assert reconciled.count(assignment) == 1
    assert b"EXPECTED_ENGINE_VERSION=engine-core-0.1.0\n" in reconciled
    assert b"RUST_LOG=debug\n" in reconciled
    assert b"HOST_ONLY_DIAL=preserved\n" in reconciled
    before = (legacy.stat().st_ino, legacy.read_bytes())
    result = run(legacy)
    assert result.returncode == 0, result.stderr.decode()
    assert (legacy.stat().st_ino, legacy.read_bytes()) == before
    assert not list(tmp_path.glob(".*.env.*"))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("EXPECTED_ENGINE_ACCOUNT_USER_ID", ""),
        ("EXPECTED_ENGINE_ACCOUNT_USER_ID", "579580669"),
        ("EXPECTED_ENGINE_VENUE", ""),
        ("EXPECTED_ENGINE_VENUE", "mexc"),
        ("EXPECTED_ENGINE_REALM", ""),
        ("EXPECTED_ENGINE_REALM", "mainnet"),
    ],
)
def test_demo_engine_environment_reconciliation_refuses_conflicting_identity(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    program = _python_heredoc(
        _function(
            deploy,
            "reconcile_demo_engine_environment",
            "prepare_demo_runtime_config",
        )
    )
    source = ROOT / "deploy" / "engine.env.template"
    target = tmp_path / "engine.env"
    body = f"RUST_LOG=debug\n{key}={value}\n".encode()
    target.write_bytes(body)
    target.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-", str(source), str(target)],
        cwd=ROOT,
        input=program.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert target.read_bytes() == body
    assert not list(tmp_path.glob(".engine.env.*"))


def test_demo_engine_environment_reconciliation_precedes_runtime_build() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    prepare = _function(
        deploy,
        "prepare_demo_runtime_config",
        "trusted_checkout_directory",
    )
    install = _function(deploy, "install_mode", "load_authorization")
    assert prepare.index("reconcile_demo_engine_environment") < prepare.index("write_producer_environment")
    assert install.index("prepare_demo_runtime_config") < install.index("build_engine")


def test_engine_release_is_locked_commit_bound_and_digest_checked() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    compile_exact = _function(text, "compile_engine_commit", "verify_prefetched_engine_candidate")
    toolchain_check = _function(text, "require_pinned_engine_toolchain", "compile_engine_commit")
    builder = _function(text, "run_engine_builder_step", "compile_engine_commit")
    prefetch = _function(text, "prefetch_rollout_target", "record_installed_profile")
    build = _function(text, "build_engine", "verify_engine_release")
    verify = _function(text, "verify_engine_release", "validate_engine_environment")
    assert "cargo fetch --locked" in builder
    assert "build --release --locked --offline" in builder
    assert "locked offline release engine build failed" in compile_exact
    assert 'RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN"' in toolchain_check
    assert 'RUSTUP_TOOLCHAIN="$ENGINE_RUST_TOOLCHAIN"' in builder
    assert "require_pinned_engine_toolchain" in prefetch
    assert "engine_git reset --hard --quiet FETCH_HEAD" in compile_exact
    assert 'built" = "$commit' in compile_exact
    assert 'compile_engine_commit "$EXPECTED_COMMIT"' in prefetch
    assert 'verify_prefetched_engine_candidate "$commit"' in build
    assert "compile_engine_commit" not in build
    assert (
        "commit=%s\\nsha256=%s\\nlauncher_sha256=%s\\n"
        "control_helper_sha256=%s\\ncontrols_sudoers_sha256=%s\\n"
        "telegram_bot_sha256=%s\\nrustc=1.90.0"
    ) in build
    assert '"$ENGINE_LAUNCHER.new"' in build
    assert 'install -d -o root -g root -m 0755 "${ENGINE_BINARY%/*}"' in build
    assert 'install -d -o root -g liquidity-migration -m 0755 "${ENGINE_BINARY%/*}"' not in build
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
    for boundary in (
        '[ ! -L "${ENGINE_BINARY%/*}" ]',
        'readlink -f "${ENGINE_BINARY%/*}"',
        'stat -c %u "${ENGINE_BINARY%/*}"',
        'stat -c %g "${ENGINE_BINARY%/*}"',
        'stat -c %a "${ENGINE_BINARY%/*}"',
        "root:root mode 0755 fixed boundary",
    ):
        assert boundary in verify
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
    assert "valid_until <= now_ms" in block


def test_activation_starts_engine_then_producers_then_immediate_liveness() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    producer_activation = _function(text, "activate_target_producers", "activate_manifest_units")
    assert "lm_target_producer_units" in producer_activation
    assert 'systemctl enable --now "$unit"' in producer_activation
    assert producer_activation.index('systemctl enable --now "$unit"') < producer_activation.index(
        'wait_fresh_producer_book "$unit"'
    )
    demo = _function(text, "activate_mode", "provision_mainnet_prerequisites")
    producer_call = (
        'activate_target_producers \\\n        demo "$LONG_SLEEVE" "$CARRY_SLEEVE" off "$producer_started_ns"'
    )
    assert demo.index("start_required_engine") < demo.index(producer_call)
    assert demo.index(producer_call) < demo.index("activate_manifest_units demo")
    mainnet = _function(text, "start_mainnet_fleet", "disarm_mainnet_mode")
    mainnet_call = 'activate_target_producers mainnet off off on "$producer_started_ns"'
    assert mainnet.index("start_required_engine") < mainnet.index(mainnet_call)
    assert mainnet.index(mainnet_call) < mainnet.index("activate_manifest_units mainnet")

    manifest_activation = _function(text, "activate_manifest_units", "start_required_engine")
    assert manifest_activation.index('lm_immediate_timer_jobs "$realm"') < (
        manifest_activation.index('lm_activation_units "$realm" start')
    )


def test_activation_starts_and_waits_for_the_exact_manifest_producer_set() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    activation = _function(text, "activate_target_producers", "start_required_engine")
    program = f"""
set -euo pipefail
. deploy/lib_sleeves.sh
REPO_DIR=/opt/liquidity-migration
fail() {{ printf 'fail|%s\\n' "$*" >&2; return 1; }}
systemctl() {{ printf 'start|%s\\n' "$3"; }}
wait_fresh_producer_book() {{ printf 'wait|%s|%s|%s|%s|%s\\n' "$1" "$2" "$3" "$4" "$5"; }}
{activation}
printf 'realm|demo\\n'
activate_target_producers demo on off off 123
printf 'realm|mainnet\\n'
activate_target_producers mainnet off off on 456
"""
    completed = subprocess.run(
        ["bash"],
        cwd=ROOT,
        input=program,
        text=True,
        capture_output=True,
        check=True,
    )
    rows = [
        line.split("|") for line in _read("deploy/fleet_manifest.tsv").splitlines() if line and not line.startswith("#")
    ]

    def selected(realm: str, *, long: str, carry: str, mainnet: str) -> list[list[str]]:
        states = {"always": "on", "long": long, "carry": carry, "mainnet": mainnet}
        return sorted(
            (
                row
                for row in rows
                if row[1] == "current"
                and row[2] == "service"
                and row[3] == realm
                and row[4] == "downstream"
                and row[9].startswith("cycle:")
                and row[10] != "-"
                and states.get(row[6], "off") == "on"
            ),
            key=lambda row: int(row[5]),
            reverse=True,
        )

    expected_lines: list[str] = []
    for realm, started_ns, selected_rows in (
        ("demo", "123", selected("demo", long="on", carry="off", mainnet="off")),
        (
            "mainnet",
            "456",
            selected("mainnet", long="off", carry="off", mainnet="on"),
        ),
    ):
        expected_lines.append(f"realm|{realm}")
        expected_lines.extend(f"start|{row[0]}" for row in selected_rows)
        for row in selected_rows:
            _kind, _sleeve, relative_root, _dataset = row[9].split(":")
            expected_lines.append(
                f"wait|{row[0]}|/opt/liquidity-migration/{relative_root}|{row[10]}|{realm}|{started_ns}"
            )
    assert completed.stdout.splitlines() == expected_lines
    assert any("exodus-demo.service" in line for line in expected_lines)
    assert not any("carry-demo.service" in line for line in expected_lines)


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
    prerequisites = _function(text, "provision_mainnet_prerequisites", "require_mainnet_preflight")
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
    compile_exact = _function(text, "compile_engine_commit", "verify_prefetched_engine_candidate")
    builder = _function(text, "run_engine_builder_step", "compile_engine_commit")
    for required in (
        "ENGINE_BUILDER_USER",
        "ENGINE_BUILDER_CARGO_HOME",
        "ENGINE_BUILDER_TARGET_DIR",
        "systemd-run --quiet --wait --pipe --collect",
        "KillMode=control-group",
        "/usr/sbin/runuser -u",
        "/usr/bin/env -i",
        "CARGO_HOME=",
        "CARGO_TARGET_DIR=",
    ):
        assert required in builder
    for required in (
        "chown -R root:root",
        "chmod -R a-w",
        "engine_git status --porcelain=v1 --untracked-files=all",
        'readlink -f "$ENGINE_CANDIDATE_BINARY"',
        'stat -c %h "$ENGINE_CANDIDATE_BINARY"',
    ):
        assert required in compile_exact
    assert compile_exact.index("chmod -R a-w") < compile_exact.index("run_engine_builder_step fetch")
    assert compile_exact.index("run_engine_builder_step fetch") < compile_exact.index("run_engine_builder_step build")
    assert compile_exact.index("run_engine_builder_step build") < compile_exact.rindex("engine_git status")


def test_engine_build_fetches_locked_crates_then_compiles_offline() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    builder = _function(text, "run_engine_builder_step", "compile_engine_commit")
    fetch_case = builder[builder.index("fetch)") : builder.index("build)")]
    build_case = builder[builder.index("build)") : builder.index("python-fetch)")]

    assert "cargo fetch --locked" in fetch_case
    assert "PrivateNetwork" not in fetch_case
    assert "PrivateNetwork=true" in build_case
    assert "RestrictAddressFamilies=AF_UNIX" in build_case
    assert "cargo build --release --locked --offline" in build_case


def test_cargo_hardlinks_are_confined_then_materialized_as_one_link() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    materialize = _function(text, "materialize_single_link_engine_candidate", "compile_engine_commit")
    compile_exact = _function(text, "compile_engine_commit", "verify_prefetched_engine_candidate")

    assert '-xdev -type f -samefile "$candidate"' in materialize
    assert 'internal_links" -eq "$hardlink_count"' in materialize
    assert 'mktemp "$candidate_dir/.engine-candidate.XXXXXX"' in materialize
    assert 'temporary_digest" = "$source_digest"' in materialize
    assert 'stat -c %h "$temporary")" -eq 1' in materialize
    assert 'mv -fT -- "$temporary" "$candidate"' in materialize
    assert (
        compile_exact.index("run_engine_builder_step build")
        < compile_exact.index("materialize_single_link_engine_candidate")
        < compile_exact.index('[ -f "$ENGINE_CANDIDATE_BINARY" ]')
    )


def test_engine_candidate_is_built_before_rollout_stops_and_reused_exactly() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    prefetch = _function(text, "prefetch_rollout_target", "record_installed_profile")
    verifier = _function(text, "verify_prefetched_engine_candidate", "verify_controls_sudo_policy")
    build = _function(text, "build_engine", "verify_engine_release")
    install = _function(text, "install_mode", "load_authorization")
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]

    assert 'compile_engine_commit "$EXPECTED_COMMIT"' in prefetch
    assert install.index("verify_prefetched_deploy_inputs") < install.index("require_quiescent")
    assert rollout.index("rollout-target-prefetch") < rollout.index("snapshot-prior-topology")
    assert rollout.index("rollout-target-prefetch") < rollout.index("ROLLOUT_STOPPED=1")
    assert rollout.index("rollout-target-prefetch") < rollout.index("stop-downstream-units")
    assert 'ENGINE_PREFETCHED_COMMIT" = "$commit' in verifier
    assert 'actual_digest" = "$ENGINE_PREFETCHED_DIGEST' in verifier
    assert 'stat -c %U "$ENGINE_CANDIDATE_BINARY"' in verifier
    assert 'verify_prefetched_engine_candidate "$commit"' in build
    assert "compile_engine_commit" not in build
    assert "cargo build" not in build


def test_rollout_stop_list_covers_every_current_non_owner_unit() -> None:
    units = set(_units())
    owners = {
        "liquidity-migration-engine.service",
        "liquidity-migration-engine-mainnet.service",
    }
    completed = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail; . deploy/lib_sleeves.sh; lm_rollout_units downstream",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    listed = set(completed.stdout.splitlines())
    assert units - owners == listed - {unit for unit in listed if unit not in units}


def test_transient_builder_is_bounded_tracked_and_cleaned_on_exit() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    stop = _function(text, "stop_active_engine_builder_unit", "run_engine_builder_step")
    stale = _function(text, "stop_stale_engine_builder_units", "run_engine_builder_step")
    builder = _function(text, "run_engine_builder_step", "compile_engine_commit")
    deploy_cleanup = _function(text, "deploy_cleanup", "rollout_cancel")
    rollout_cleanup = _function(text, "rollout_cleanup", "prefetch_rollout_target")
    dispatch = text[text.index("trap deploy_cleanup EXIT") : text.index("REMOTE_SCRIPT\n")]

    assert 'ENGINE_ACTIVE_BUILDER_UNIT="$unit"' in builder
    assert builder.index('ENGINE_ACTIVE_BUILDER_UNIT="$unit"') < builder.index("systemd-run")
    assert "RuntimeMaxSec=45m" in builder
    assert "TimeoutStopSec=30s" in builder
    assert "stop_active_engine_builder_unit" in builder
    assert 'systemctl stop "$unit"' in stop
    assert 'systemctl reset-failed "$unit"' in stop
    assert 'ENGINE_ACTIVE_BUILDER_UNIT=""' in stop
    assert "liquidity-migration-engine-python-fetch-" in stale
    assert 'systemctl stop "$unit"' in stale
    assert "stop_stale_engine_builder_units" in _function(text, "prefetch_rollout_target", "record_installed_profile")
    assert "stop_active_engine_builder_unit" in deploy_cleanup
    assert "stop_active_engine_builder_unit" in rollout_cleanup
    for signal in ("INT", "TERM", "HUP", "PIPE"):
        assert f"trap 'deploy_cancel {signal} " in dispatch


def test_all_network_inputs_are_prefetched_and_stopped_install_is_offline() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    prefetch = _function(text, "prefetch_rollout_target", "record_installed_profile")
    python_prefetch = _function(text, "prefetch_python_dependencies", "verify_prefetched_python_dependencies")
    wheel_digest = _function(text, "python_wheel_cache_digest", "prefetch_python_dependencies")
    install = _function(text, "install_mode", "load_authorization")
    venv_install = _function(text, "install_python_environment", "verify_controls_sudo_policy")
    staged = _function(text, "staged_mode", "rollout_mode")
    dispatch = text[text.index("acquire_maintenance_locks\ncase") : text.index("REMOTE_SCRIPT\n")]

    assert "git_fetch fetch" in prefetch
    assert "prefetch_python_dependencies" in prefetch
    assert (
        prefetch.index("git_fetch fetch")
        < prefetch.index("compile_engine_commit")
        < prefetch.index("prefetch_python_dependencies")
    )
    assert "python-fetch" in python_prefetch
    assert "PYTHON_PREFETCHED_LOCK_DIGEST" in python_prefetch
    assert "PYTHON_PREFETCHED_WHEEL_DIGEST" in python_prefetch
    assert "st_nlink != 1" in wheel_digest
    assert "content.digest()" in wheel_digest
    assert "git_fetch" not in install
    assert "pip download" not in install
    assert "install_python_environment" in install
    assert "--no-index" in venv_install
    assert '--find-links "$PYTHON_WHEEL_CACHE"' in venv_install
    assert install.count("verify_prefetched_deploy_inputs") >= 2
    assert "run_strict_phase staged-target-prefetch" in staged
    assert "run_strict_phase install-target-prefetch" in dispatch


def test_engine_prefetch_scrubs_only_its_disposable_build_clone() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    prepare = _function(text, "prepare_disposable_engine_build_root", "compile_engine_commit")
    compile_commit = _function(text, "compile_engine_commit", "verify_prefetched_engine_candidate")

    assert "ENGINE_BUILD_DIR=/opt/engine-build" in text
    assert '[ "$ENGINE_BUILD_DIR" = /opt/engine-build ]' in prepare
    assert '[ ! -L "$ENGINE_BUILD_DIR" ]' in prepare
    assert 'readlink -f "$ENGINE_BUILD_DIR"' in prepare
    assert 'readlink -f "$ENGINE_BUILD_DIR/.git"' in prepare
    assert "/proc/self/mountinfo" in prepare
    assert 'index($5, root "/") == 1' in prepare
    assert "0022" in prepare
    assert 'install -d -o root -g root -m 0755 "$ENGINE_BUILD_DIR"' in prepare
    assert 'chmod 0755 "$ENGINE_BUILD_DIR"' in prepare
    assert "prepare_disposable_engine_build_root" in compile_commit
    assert "engine_git clean -ffdx --quiet" in compile_commit
    assert (
        compile_commit.index("prepare_disposable_engine_build_root")
        < (compile_commit.index('chmod -R u+rwX "$ENGINE_BUILD_DIR"'))
        < compile_commit.index("engine_git reset --hard --quiet FETCH_HEAD")
        < (compile_commit.index("engine_git clean -ffdx --quiet"))
        < compile_commit.index('dirty="$(engine_git status --porcelain=v1')
    )
    assert "safe_git clean" not in compile_commit


def test_early_arming_read_uses_the_system_python_bootstrap() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    armed = _function(text, "mainnet_armed", "verify_note")

    assert '"${PYTHON:-/usr/bin/python3}" -c' in armed
    assert ".venv/bin/python" not in armed


def test_python_environment_is_fresh_verified_and_atomically_exchanged() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    install = _function(text, "install_python_environment", "verify_controls_sudo_policy")
    cleanup = _function(text, "remove_deploy_venv_staging", "install_python_environment")
    deploy_cleanup = _function(text, "deploy_cleanup", "rollout_cancel")
    rollout_cleanup = _function(text, "rollout_cleanup", "prefetch_rollout_target")

    assert 'mktemp -d "$DEPLOY_VENV_STAGING_ROOT/deploy.XXXXXX"' in install
    assert 'chmod 0755 "$staging"' in install
    assert '/usr/bin/python3 -m venv "$staging"' in install
    assert '"$staging/bin/python" -m pip install' in install
    assert "environment_root = Path(sys.prefix).resolve()" in install
    assert 'for kind in ("purelib", "platlib")' in install
    assert "Path(path).is_relative_to(environment_root)" in install
    assert "distributions(path=site_packages)" in install
    assert "for distribution in distributions():" not in install
    assert "actual != expected" in install
    assert "extra = sorted" in install
    assert '|| fail "fresh Python environment does not exactly match the deployment lock"' in install
    assert "renameat2" in install
    assert "RENAME_EXCHANGE = 2" in install
    assert "renameat2(AT_FDCWD, source, AT_FDCWD, target, RENAME_EXCHANGE)" in install
    assert '|| fail "cannot atomically install the fresh Python environment"' in install
    assert "os.fsync(directory)" in install
    assert install.index("actual != expected") < install.index("renameat2")
    assert "secure_venv_directory" in install
    assert "rm -rf" not in install[: install.index("renameat2")]
    assert '"${path%/*}" = "$DEPLOY_VENV_STAGING_ROOT"' in cleanup
    assert "remove_deploy_venv_staging" in deploy_cleanup
    assert "remove_deploy_venv_staging" in rollout_cleanup


def test_deployed_python_is_verified_from_unprivileged_runtime_identities() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    install_mode = _function(text, "install_mode", "load_authorization")
    verify = _function(text, "verify_python_runtime_environment", "remove_deploy_venv_staging")

    assert "run_phase verify-python-runtime verify_python_runtime_environment" in install_mode
    assert '[ "$(stat -c %a "$deployed")" = 755 ]' in verify
    assert '"$PRODUCER_USER" "$CONTROLS_USER" "$OBSERVER_USER" "$LLM_USER"' in verify
    assert '/usr/bin/sudo -u "$runtime_user"' in verify
    assert "/usr/bin/env PYTHONDONTWRITEBYTECODE=1" in verify
    assert "Path(sys.prefix).resolve() != expected" in verify
    assert "import polars" in verify
    assert "from liquidity_migration.cli.commands import main" in verify
    assert "long-demo-state.json" in verify
    assert "long-mainnet-state.json" in verify
    assert "migrate_empty_v1_book_state(sys.argv[1])" in verify
    assert "read_book_state(sys.argv[1])" in verify


def test_preserved_long_book_state_is_migrated_for_its_producer() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    identities = _function(text, "ensure_runtime_identities", "write_producer_environment")
    migrate = _function(
        text,
        "normalize_producer_book_state_access",
        "write_producer_environment",
    )

    assert "normalize_producer_book_state_access" in identities
    assert "long-demo-state.json" in migrate
    assert "long-mainnet-state.json" in migrate
    assert "stat.S_ISREG" in migrate
    assert "before.st_nlink != 1" in migrate
    assert "root_before = os.lstat(root_path)" in migrate
    assert "os.O_DIRECTORY" in migrate
    assert "os.O_NOFOLLOW" in migrate
    assert "os.O_NONBLOCK" in migrate
    assert "dir_fd=root" in migrate
    assert "os.path.samestat" in migrate
    assert "os.fchown" in migrate
    assert "os.fchmod(descriptor, 0o640)" in migrate


def test_llm_ledger_is_research_only_and_uses_its_writer_owned_state_directory() -> None:
    units = _units()
    llm = units["liquidity-migration-llm-ledger.service"]
    long_demo = units["liquidity-migration-bybit-long-demo.service"]
    text = DEPLOY.read_text(encoding="utf-8")
    migration = _function(text, "migrate_legacy_llm_gate_candidates", "ensure_runtime_identities")

    assert "LONG_ENGINE_LLM_GATE" not in long_demo
    assert "StateDirectory=liquidity-migration/llm-driver-ledger" in llm
    assert "StateDirectoryMode=0750" in llm
    assert "ReadWritePaths=/var/lib/liquidity-migration/llm-driver-ledger" in llm
    assert "ReadWritePaths=/var/lib/liquidity-migration/targets" not in llm
    assert "LEGACY_LLM_GATE_CANDIDATES_PATH" in migration
    assert "LLM_GATE_CANDIDATES_PATH" in migration
    assert 'mv -T -- "$source" "$target"' in migration


def test_live_producer_wrappers_never_fall_back_to_system_python() -> None:
    for relative in (
        "scripts/runtime/run_bybit_long_demo_event_engine.sh",
        "scripts/runtime/run_bybit_carry_demo_event_engine.sh",
    ):
        body = _read(relative)
        assert "Pinned Python runtime is unavailable" in body
        assert "command -v python" not in body


def test_long_wrapper_names_its_configuration_boundary() -> None:
    body = _read("scripts/runtime/run_bybit_long_demo_event_engine.sh")
    assert 'LONG_RUNTIME_CONFIG_SOURCE="scripts/runtime/run_bybit_long_demo_event_engine.sh"' in body


def test_fresh_python_verifier_ignores_source_tree_distribution_metadata(
    tmp_path: Path,
) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    install = _function(text, "install_python_environment", "verify_controls_sudo_policy")
    verifier = install.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source_metadata = checkout / "liquidity_migration-0.1.0.dist-info"
    source_metadata.mkdir()
    (source_metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: liquidity-migration\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    lock = tmp_path / "requirements.lock"
    lock.write_text("", encoding="utf-8")

    result = subprocess.run(
        [str(interpreter), "-", str(lock)],
        cwd=checkout,
        input=verifier,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_funded_attestor_template_has_only_the_read_only_identity_contract() -> None:
    template = _read("deploy/bybit-mainnet-attestor.env.template")
    assignments = {line.split("=", 1)[0] for line in template.splitlines() if line and not line.startswith("#")}
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
        unset = " ".join(line for line in body.splitlines() if line.startswith("UnsetEnvironment="))
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
        assert "require_rollout_for_funded_generation_change" in _function(text, function, boundary)
    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert "ROLLOUT_FUNDED_AUTHORITY=1" in rollout


def test_rollout_accepts_a_markerless_incumbent_until_the_stopped_install() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    before_install = block[: block.index("stopped-install")]
    assert before_install.index("rollout-target-prefetch") < before_install.index("stop-downstream-units")
    assert before_install.index("snapshot-prior-topology") < before_install.index("stop-downstream-units")
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

    fence = _function(text, "disable_rollout_units_for_boot_fence", "stop_all_rollout_units_best_effort")
    for required in (
        'systemctl disable "$unit"',
        "systemctl daemon-reload",
        "systemctl is-active --quiet",
        "systemctl is-enabled",
        "invalidate_activation_authority",
        "sync",
    ):
        assert required in fence

    quarantine = _function(text, "stop_all_rollout_units_best_effort", "cleanup_notice")
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
    cancellation_start = 'if [ "$status" -ne 0 ] && [ -n "$cancellation_signal" ]'
    assert cleanup.index(cancellation_start) < cleanup.index('if [ "$ROLLOUT_IRREVERSIBLE" -eq 0 ]')
    cancellation = cleanup[
        cleanup.index(cancellation_start) : cleanup.index('elif [ "$status" -ne 0 ] && [ "$ROLLOUT_STOPPED" -eq 1 ]')
    ]
    assert "stop_all_rollout_units_best_effort" in cancellation
    assert 'ROLLOUT_STOPPED" -eq 1' in cancellation
    assert "incumbent topology untouched" in cancellation
    assert "activate_mode" not in cancellation
    for signal, status in (("INT", 130), ("TERM", 143), ("HUP", 129), ("PIPE", 141)):
        assert f"trap 'rollout_cancel {signal} {status}' {signal}" in rollout
    snapshot_restore = _function(text, "restore_prior_topology_snapshot", "restore_prior_topology")
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
    ("cancellation_signal", "expected_status", "rollout_stopped", "expected_actions"),
    [
        ("INT", 130, 1, ["stop"]),
        ("TERM", 143, 1, ["stop"]),
        ("HUP", 129, 1, ["stop"]),
        ("PIPE", 141, 1, ["stop"]),
        ("INT", 130, 0, []),
        ("TERM", 143, 0, []),
        ("HUP", 129, 0, []),
        ("PIPE", 141, 0, []),
        ("", 71, 1, ["restore"]),
        # A command may itself return a signal-shaped status. Only the shell's
        # explicit signal handler marks cancellation and suppresses restore.
        ("", 143, 1, ["restore"]),
    ],
)
def test_rollout_cleanup_quarantines_cancellation_but_restores_ordinary_failure(
    cancellation_signal: str,
    expected_status: int,
    rollout_stopped: int,
    expected_actions: list[str],
) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    cancel = _function(text, "rollout_cancel", "rollout_cleanup")
    cleanup = _function(text, "rollout_cleanup", "prefetch_rollout_target")
    bash = shutil.which("bash")
    assert bash is not None
    trigger = (
        f"rollout_cancel {cancellation_signal} {expected_status}" if cancellation_signal else f"exit {expected_status}"
    )
    script = f"""
set -u
ROLLOUT_CANCELLATION_SIGNAL=""
ROLLOUT_STOPPED={rollout_stopped}
ROLLOUT_COMPLETE=0
ROLLOUT_IRREVERSIBLE=0
ROLLOUT_CURRENT_COMMIT=prior-commit
ENGINE_ACTIVE_BUILDER_UNIT=""
DEPLOY_VENV_STAGING=""
cleanup_notice() {{ :; }}
stop_active_engine_builder_unit() {{ :; }}
remove_deploy_venv_staging() {{ :; }}
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

    permit = _function(text, "begin_activation_generation", "complete_activation_generation")
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

    complete = _function(text, "complete_activation_generation", "validate_engine_environment")
    receipt_move = complete.index('mv -f "$temporary" "$ACTIVATION_RECEIPT"')
    watchdog_stop = complete.index("stop_activation_watchdog")
    permit_remove = complete.index('rm -f -- "$ACTIVATION_PERMIT"')
    assert complete.index("sync") < receipt_move < watchdog_stop < permit_remove
    assert 'activation_authority_matches "$ACTIVATION_RECEIPT" complete' in complete

    topology = _function(text, "verify_topology", "start_if")
    assert "activation-in-progress" in topology
    assert 'activation_authority_matches "$ACTIVATION_PERMIT" permit' in topology
    assert 'activation_authority_matches "$ACTIVATION_RECEIPT" complete' in topology

    rollout = text[text.index("rollout_mode()") : text.index("acquire_maintenance_locks\n")]
    assert rollout.index("activate-and-verify") < rollout.index("ROLLOUT_COMPLETE=1")


def test_runtime_supervisor_revokes_same_boot_partial_activation() -> None:
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    child_start = launcher.index('/bin/bash "$CHECKOUT_WRAPPER" "$@" &')
    monitor = launcher.index("while child_is_running", child_start)
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
    watchdog = _function(deploy, "start_activation_watchdog", "activation_authority_matches")
    assert "/usr/bin/systemd-run" in watchdog
    assert '"$ENGINE_LAUNCHER" --activation-watchdog' in watchdog
    assert "SuccessExitStatus=143" in watchdog
    assert "PrivateNetwork=true" in watchdog
    assert "ProtectSystem=strict" in watchdog
    assert "ReadWritePaths=${ACTIVATION_PERMIT%/*}" in watchdog
    assert "activation_authority_matches_unlocked" in deploy
    assert '/usr/bin/flock -s "$authority_fd"' in deploy

    lifecycle = deploy[
        deploy.index("activation_watchdog_state()") : deploy.index("start_activation_watchdog()")
    ]
    assert "systemctl list-units" in lifecycle
    assert "absent) return 0" in lifecycle
    assert 'systemctl stop --no-block "$ACTIVATION_WATCHDOG_UNIT"' in lifecycle
    assert 'fragment="/run/systemd/transient/$ACTIVATION_WATCHDOG_UNIT"' in lifecycle
    assert '[ ! -e "$fragment" ]' in lifecycle
    assert "systemctl show" not in lifecycle
    assert "systemctl is-active" not in lifecycle
    assert "systemctl is-failed" not in lifecycle


def test_activation_authority_stat_failure_is_quiet_and_fail_closed() -> None:
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    exposed = "\n".join(
        (
            _function(launcher, "watchdog_permit_matches", "watchdog_remove_owned_permit"),
            _function(launcher, "watchdog_open_permit_is_current", "watchdog_refresh_permit"),
            _function(
                launcher,
                "with_locked_activation_authority",
                "activation_authority_matches",
            ),
        )
    )
    guards = re.findall(
        r'(?:^|&& )(?P<guard>\[ "\$\(stat [^\n]+?\)" = [0-9]+ \])',
        exposed,
        re.MULTILINE,
    )
    assert len(guards) == 8
    assert re.search(r'\$\(stat [^\n]+\)" -eq', launcher) is None
    for guard in guards:
        script = f"""
stat() {{ :; }}
descriptor_path=unavailable
path=unavailable
{guard}
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert result.stderr == ""

    deploy = DEPLOY.read_text(encoding="utf-8")
    deployed_check = _function(
        deploy,
        "activation_authority_matches",
        "invalidate_activation_authority",
    )
    assert '[ "$(stat -Lc %h "$descriptor_path")" = 1 ]' in deployed_check


def test_trusted_launcher_rejects_mutable_checkout_and_git_boundaries() -> None:
    launcher = _read("deploy/run_authorized_runtime_trusted.sh")
    ancestry = launcher[launcher.index("trusted_checkout_directory()") : launcher.index('for path in "$ENGINE"')]
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
    checkout = deploy[deploy.index("trusted_checkout_directory()") : deploy.index("clean_checkout_status()")]
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
    resolver = _function(deploy, "resolve_fail_safe_python", "quarantine_mainnet_units")
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
    refresh = launcher[launcher.index("watchdog_refresh_permit()") : launcher.index("activation_watchdog_mode()")]
    assert 'exec {pinned_permit_fd}<"$ACTIVATION_PERMIT"' in launcher
    assert 'exec {WATCHDOG_PERMIT_FD}<>"/proc/self/fd/$pinned_permit_fd"' in launcher
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

    hot = launcher[launcher.index("activation_authority_valid()") : launcher.index('child_pid=""')]
    assert hot.count('activation_authority_content_matches "$ACTIVATION_RECEIPT" complete') == 2
    assert hot.count('activation_authority_matches "$ACTIVATION_RECEIPT" complete') == 2


def test_tmpfiles_recreates_only_runtime_lock_boundaries_after_reboot() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    identities = _function(text, "ensure_runtime_identities", "write_producer_environment")
    for exact_rule in (
        "d /run/liquidity-migration 0755 root root -",
        "f /run/liquidity-migration/maintenance.lock 0600 root root -",
        "f /run/liquidity-migration/deploy.lock 0600 root root -",
        "d /run/lock/liquidity-migration 0770 root %s -",
        "f /run/lock/liquidity-migration-ledger-reset.lock 0600 root root -",
    ):
        assert exact_rule in identities
    assert "systemd-tmpfiles --create" in identities


def test_install_reopens_persistent_account_leases_for_isolated_engines() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    normalize = _function(text, "normalize_account_lease_access", "ensure_runtime_identities")
    identities = _function(text, "ensure_runtime_identities", "write_producer_environment")

    assert "-mindepth 1 -maxdepth 1" in normalize
    assert "-name '*-user-*.lock' -print0" in normalize
    assert '[ -f "$lease" ] && [ ! -L "$lease" ]' in normalize
    assert 'stat -c %h -- "$lease"' in normalize
    assert '[ "$links" -eq 1 ]' in normalize
    assert 'chown root:"$RUNTIME_GROUP" -- "$lease"' in normalize
    assert 'chmod 0660 -- "$lease"' in normalize
    assert identities.index("systemd-tmpfiles --create") < identities.index("normalize_account_lease_access")


def test_install_rehomes_persistent_runtime_state_without_replacing_it() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    normalize = _function(text, "normalize_runtime_state_access", "migrate_legacy_llm_gate_candidates")
    identities = _function(text, "ensure_runtime_identities", "write_producer_environment")

    for directory, owner in (
        ("/var/lib/liquidity-migration-engine", "$DEMO_ENGINE_USER"),
        ("/var/lib/liquidity-migration-engine-mainnet", "$MAINNET_ENGINE_USER"),
        ("$LONG_DEMO_ROOT", "$PRODUCER_USER"),
        ("$CARRY_DEMO_ROOT", "$PRODUCER_USER"),
        ("$LONG_MAINNET_ROOT", "$PRODUCER_USER"),
        ("$CARRY_MAINNET_ROOT", "$PRODUCER_USER"),
        ("$LLM_STATE_ROOT", "$LLM_USER"),
    ):
        assert directory in normalize
        assert owner in normalize
    assert "os.lstat(path)" in normalize
    assert "stat.S_ISLNK" in normalize
    assert "os.O_NOFOLLOW" in normalize
    assert "row.st_nlink != 1" in normalize
    assert "row.st_dev != device" in normalize
    assert "validate_directory(descriptor, path, device)" in normalize
    assert "migrate_directory(descriptor, path, device, owner, group)" in normalize
    assert "os.path.samestat" in normalize
    assert "os.fchown" in normalize
    assert "os.fchmod" in normalize
    assert '[ "$?" -eq 0 ] || fail "cannot migrate runtime-state ownership"' in normalize
    assert normalize.index("migrate_directory(child") < normalize.index("os.fchown(child")
    for replacing in ("rm -rf", "shutil.move", "os.replace", "shutil.copy"):
        assert replacing not in normalize
    assert identities.index("normalize_account_lease_access") < identities.index("normalize_runtime_state_access")
    assert "chown -R --no-dereference" not in text


def test_runtime_state_refusal_cannot_be_masked_by_run_phase(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("descriptor migration is a Linux deployment boundary")
    import grp
    import pwd

    text = DEPLOY.read_text(encoding="utf-8")
    normalize = _function(text, "normalize_runtime_state_access", "migrate_legacy_llm_gate_candidates")
    demo_engine = tmp_path / "absent-demo-engine"
    mainnet_engine = tmp_path / "absent-mainnet-engine"
    normalize = normalize.replace(
        "/var/lib/liquidity-migration-engine-mainnet", shlex.quote(str(mainnet_engine))
    ).replace("/var/lib/liquidity-migration-engine", shlex.quote(str(demo_engine)))
    target = tmp_path / "real"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    absent = shlex.quote(str(tmp_path / "absent"))
    user = shlex.quote(pwd.getpwuid(os.getuid()).pw_name)
    group = shlex.quote(grp.getgrgid(os.getgid()).gr_name)
    script = f"""
set -euo pipefail
PYTHON={shlex.quote(sys.executable)}
RUNTIME_GROUP={group}
DEMO_ENGINE_USER={user}
MAINNET_ENGINE_USER={user}
PRODUCER_USER={user}
LLM_USER={user}
LONG_DEMO_ROOT={shlex.quote(str(linked))}
    CARRY_DEMO_ROOT={absent}
    EXODUS_DEMO_ROOT={absent}
    LONG_MAINNET_ROOT={absent}
    CARRY_MAINNET_ROOT={absent}
    EXODUS_MAINNET_ROOT={absent}
LLM_STATE_ROOT={absent}
fail() {{ exit 77; }}
{normalize}
ensure_runtime_identities() {{ normalize_runtime_state_access; :; }}
run_phase() {{ shift; if "$@"; then return 0; else return $?; fi; }}
run_phase identity ensure_runtime_identities
"""

    result = subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 77, result.stderr
    assert "runtime state root is linked" in result.stderr


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
        '$(stat -c %u "$REPOSITORY")" = 0',
        '$(stat -c %u "$path")" = 0',
        '$(stat -c %u "$REPOSITORY/.git")" = 0',
        '$(stat -c %u "$directory")" = 0',
        '$(stat -c %u "${ENGINE%/*}")" = 0',
        '$(stat -c %u "${ACTIVATION_PERMIT%/*}")" = 0',
        '$(stat -c %u "$LAUNCHER")" = 0',
        '$(stat -c %u "${LAUNCHER%/*}")" = 0',
        '$(stat -c %u "$MARKER")" = 0',
        '$(stat -Lc %u "$descriptor_path")" = 0',
    ):
        source = source.replace(expression, expression.removesuffix("0") + str(uid))
    for expression in (
        '$(stat -c %g "$path")" = 0',
        '$(stat -c %g "${ACTIVATION_PERMIT%/*}")" = 0',
        '$(stat -c %g "$LAUNCHER")" = 0',
        '$(stat -c %g "${LAUNCHER%/*}")" = 0',
        '$(stat -c %g "$MARKER")" = 0',
        '$(stat -Lc %g "$descriptor_path")" = 0',
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
        assert child_pid_file.exists(), process.stderr.read() if process.poll() is not None else ""
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
        assert child_pid_file.exists(), process.stderr.read() if process.poll() is not None else ""
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
def test_watchdog_initial_inode_pin_rejects_raced_revocation(tmp_path: Path, revocation: str) -> None:
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
        assert ready.exists(), watchdog.stderr.read() if watchdog.poll() is not None else ""
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
    _write_runtime_permit(permit, authority_prefix, owner.pid, stale_start_ticks)
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
    _write_runtime_permit(permit, authority_prefix, owner.pid, owner_start_ticks)
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
        assert child_pid_file.exists(), process.stderr.read() if process.poll() is not None else ""
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
        "install -o root -g root -m 0755",
        "install -o root -g root -m 0440",
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
    assert activate.index("begin_activation_generation") < activate.index("activate_manifest_units demo")


def test_exact_commit_checkout_gate_uses_an_independent_index_and_rechecks_head() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = _function(text, "clean_checkout_status", "require_clean_checkout_at")
    assert "mktemp -d /run/liquidity-migration/deploy-index" in block
    assert "read-tree" in block and "diff-index --quiet" in block
    exact = _function(text, "require_clean_checkout_at", "require_clean_head")
    assert exact.count("safe_git rev-parse HEAD") >= 2


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
        if not (ROOT / relative).with_suffix(".py").is_file() and not (ROOT / relative / "__init__.py").is_file():
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
        relative: sorted(references) for relative, references in owners.items() if not (ROOT / relative).is_file()
    }
    assert not missing


def test_rust_engine_is_the_only_live_instrument_rule_source() -> None:
    engine_paths = [
        ROOT / "engine" / "engine-core" / "src" / "engine.rs",
        *sorted((ROOT / "engine" / "engine-core" / "src" / "engine").glob("*.inc.rs")),
    ]
    engine = "\n".join(path.read_text(encoding="utf-8") for path in engine_paths)
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


def test_vps_workflow_builds_known_hosts_from_the_configured_host() -> None:
    workflow = _read(".github/workflows/vps-deploy.yml")
    assert workflow.count('ssh-keyscan -T 10 -t ed25519 -- "$VPS_HOST"') == 2
    assert workflow.count('test "$host_fingerprints" = "$VPS_ED25519_FINGERPRINT"') == 2
    assert workflow.count("mv ~/.ssh/known_hosts.candidate ~/.ssh/known_hosts") == 2
    assert "208.84.103.4 ssh-ed25519" not in workflow


def test_one_click_deploy_pins_a_fetched_commit_and_uses_rollout() -> None:
    script = _read("scripts/deploy_everything.command")
    assert "git fetch --quiet origin main" in script
    assert "git fetch --quiet origin main ||" not in script
    assert "deploy_commit=\"$(git rev-parse 'origin/main^{commit}')\"" in script
    assert 'ssh_target="${SSH_TARGET:-root@208.84.103.4}"' in script
    assert 'git clone --quiet --no-local --no-checkout "$repo_root" "$controller_root"' in script
    assert 'git -C "$controller_root" fetch --quiet --no-tags "$repo_root" "$deploy_commit"' in script
    assert 'git -C "$controller_root" remote set-url origin "$origin_url"' in script
    assert '[ -d "$controller_root/.git" ]' in script
    assert "git worktree" not in script
    assert 'git -C "$controller_root" rev-parse HEAD' in script
    assert 'cd "$controller_root"' in script
    assert 'SSH_TARGET="$ssh_target" EXPECTED_COMMIT="$deploy_commit"' in script
    assert 'EXPECTED_COMMIT="$deploy_commit"' in script
    assert "scripts/ops.sh deploy rollout --profile operational" in script
    assert "deploy staged" not in script


def test_rollout_controller_must_be_clean_and_at_expected_commit(tmp_path: Path) -> None:
    repository = tmp_path / "controller"
    (repository / "scripts").mkdir(parents=True)
    shutil.copy2(DEPLOY, repository / "scripts" / DEPLOY.name)
    shutil.copytree(ROOT / "deploy", repository / "deploy")
    subprocess.run(["/usr/bin/git", "init", "--quiet", str(repository)], check=True)
    commit = [
        "/usr/bin/git",
        "-C",
        str(repository),
        "-c",
        "user.name=Rollout Controller Test",
        "-c",
        "user.email=rollout-controller@example.invalid",
        "-c",
        "core.autocrlf=false",
        "commit",
        "--quiet",
        "-m",
    ]
    subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "."], check=True)
    subprocess.run([*commit, "candidate one"], check=True)
    first = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    marker = repository / "tracked-marker"
    marker.write_text("second\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(repository), "add", marker.name], check=True)
    subprocess.run([*commit, "candidate two"], check=True)
    second = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = {
        **os.environ,
        "GITHUB_TOKEN": "test-only",
        "REPO_URL": str(repository),
        "SSH_TARGET": "unused.invalid",
    }

    mismatch = subprocess.run(
        ["bash", "scripts/deploy_vps_live.sh", "rollout", "--profile", "operational"],
        cwd=repository,
        env={**environment, "EXPECTED_COMMIT": first},
        capture_output=True,
        text=True,
        check=False,
    )
    assert mismatch.returncode != 0
    assert f"HEAD {second} is not EXPECTED_COMMIT {first}" in mismatch.stderr

    (repository / "untracked-marker").write_text("dirty\n", encoding="utf-8")
    dirty = subprocess.run(
        ["bash", "scripts/deploy_vps_live.sh", "rollout", "--profile", "operational"],
        cwd=repository,
        env={**environment, "EXPECTED_COMMIT": second},
        capture_output=True,
        text=True,
        check=False,
    )
    assert dirty.returncode != 0
    assert "rollout controller checkout is dirty" in dirty.stderr


def test_ssh_recovery_never_claims_success_after_a_failed_restart() -> None:
    restore = _read("scripts/vps/vps_restore_ssh_access.sh")
    assert "systemctl restart ssh.service ||" not in restore
    assert "service ssh restart ||" not in restore
    assert restore.index("systemctl is-active --quiet ssh.service") < restore.index('echo "ssh-restore-ok"')
    assert '[ "$ssh_restarted" -eq 1 ]' in restore

    generator = _read("scripts/vps/print_vps_recovery_command.sh")
    assert generator.count("SSH_TARGET=$ssh_target_q EXPECTED_COMMIT=") == 3
    rescue = _read("scripts/vps/vps_rescue_restore_ssh_access.sh")
    assert "SSH_TARGET=root@YOUR_VPS_HOST EXPECTED_COMMIT=" in rescue


def test_runtime_dependencies_are_exact_version_pins() -> None:
    rows = [
        line.strip() for line in _read("requirements.lock").splitlines() if line.strip() and not line.startswith("#")
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


def test_status_checks_every_timer_backed_oneshot_result() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    fleet = _function(deploy, "verify_fleet_units", "verify_topology")
    assert "lm_fleet_health_rows" in fleet
    assert 'verify_unit "$expectation" "$unit"' in fleet
    assert 'verify_timer_job "$unit" "$timer_service"' in fleet
    assert fleet.count("verify_timer_job") == 1
    verify = _function(deploy, "verify_topology", "start_if")
    assert "verify_fleet_units" in verify


def test_status_selects_the_installed_fleet_contract_across_manifest_boundary(
    tmp_path: Path,
) -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    unmanifested = _function(deploy, "verify_unmanifested_fleet", "verify_fleet_units")
    fleet = _function(deploy, "verify_fleet_units", "verify_topology")
    stubs = """
fail() { printf 'fail|%s\n' "$*" >&2; return 1; }
sleeve_on() { [ "${1:-off}" = on ]; }
mainnet_armed() { return 0; }
verify_unit() { printf 'unit|%s|%s\n' "$2" "$1"; }
verify_timer_job() { :; }
LONG_SLEEVE=on
CARRY_SLEEVE=on
ENGINE_UNIT=liquidity-migration-engine.service
MAINNET_OWNER_UNIT=liquidity-migration-engine-mainnet.service
"""

    incumbent_root = tmp_path / "incumbent"
    incumbent_root.mkdir()
    incumbent = subprocess.run(
        ["bash"],
        cwd=incumbent_root,
        input=f"set -euo pipefail\n{stubs}\n{unmanifested}\n{fleet}\nverify_fleet_units\n",
        text=True,
        capture_output=True,
        check=True,
    )
    incumbent_units = {line.split("|")[1] for line in incumbent.stdout.splitlines() if line}
    assert incumbent_units == {
        "liquidity-migration-engine.service",
        "liquidity-migration-engine-mainnet.service",
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
        "liquidity-migration-bybit-long-mainnet.service",
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-demo-liveness.timer",
        "liquidity-migration-mainnet-liveness.timer",
        "liquidity-migration-telegram-controls.service",
        "liquidity-migration-llm-ledger.timer",
        "liquidity-migration-trade-notify.timer",
        "liquidity-migration-backup.timer",
        "liquidity-migration-chaos-drill.timer",
        "liquidity-migration-forward-capture.service",
        "liquidity-migration-forward-upload.timer",
    }

    candidate = subprocess.run(
        ["bash"],
        cwd=ROOT,
        input=(f"set -euo pipefail\n. deploy/lib_sleeves.sh\n{stubs}\n{unmanifested}\n{fleet}\nverify_fleet_units\n"),
        text=True,
        capture_output=True,
        check=True,
    )
    candidate_units = {line.split("|")[1] for line in candidate.stdout.splitlines() if line}
    manifest_rows = [
        line.split("|") for line in _read("deploy/fleet_manifest.tsv").splitlines() if line and not line.startswith("#")
    ]
    expected_candidate = {row[0] for row in manifest_rows if row[1] == "current" and row[9] != "none"}
    assert candidate_units == expected_candidate
    assert {
        "liquidity-migration-bybit-exodus-demo.service",
        "liquidity-migration-bybit-exodus-mainnet.service",
    } <= candidate_units


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


def test_dispatched_scripts_can_import_the_package_when_run_as_a_file() -> None:
    # The wrapper runs each script by path, so Python puts the script's own
    # directory on the import path and not the repo root, and the package is
    # not installed into the venv. A script that imports it therefore has to
    # put the root on the path itself, before the first such import.
    wrapper = _read("scripts/run_authorized_runtime.sh")
    dispatched = sorted(set(re.findall(r"(scripts/[\w/]+\.py)", wrapper)))
    assert dispatched, "no python entrypoints found in the wrapper"

    for relative in dispatched:
        source = _read(relative)
        imports = [match.start() for match in re.finditer(r"^\s*(?:from|import) liquidity_migration", source, re.M)]
        if not imports:
            continue
        bootstrap = source.find("sys.path.insert")
        assert bootstrap != -1, f"{relative} imports the package but never puts the root on the path"
        assert bootstrap < min(imports), f"{relative} imports the package before the path is set"


def test_a_long_running_unit_records_only_a_graceful_commanded_stop_as_success() -> None:
    wrapper = _read("deploy/run_authorized_runtime_trusted.sh")
    assert "stop_for_signal 143 70' TERM" in wrapper

    for name, text in _units().items():
        if not name.endswith(".service") or "Type=simple" not in text:
            continue
        allowed = [line.split("=", 1)[1].split() for line in text.splitlines() if line.startswith("SuccessExitStatus=")]
        assert allowed, f"{name} never says a commanded stop is a clean one"
        assert any("143" in codes for codes in allowed), (
            f"{name} does not count 143 as success, so every stop of it pages"
        )
        grace = [
            int(line.rsplit("=", 1)[1])
            for line in text.splitlines()
            if line.startswith("Environment=LM_CHILD_STOP_GRACE_SECONDS=")
        ]
        timeout = [
            int(line.split("=", 1)[1].removesuffix("s"))
            for line in text.splitlines()
            if line.startswith("TimeoutStopSec=")
        ]
        assert len(grace) == len(timeout) == 1, name
        assert 0 < grace[0] < timeout[0], name


def test_trusted_supervisor_reports_a_forced_child_kill_as_failure() -> None:
    wrapper = _read("deploy/run_authorized_runtime_trusted.sh")
    helpers = wrapper[wrapper.index("child_is_running()") : wrapper.index("\ntrap 'stop_for_signal")]
    harness = f"""
set -u
stop_grace_seconds=1
child_pid=""
child_was_forced=0
{helpers}
{shlex.quote(sys.executable)} -c 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)' &
child_pid=$!
sleep 0.2
stop_for_signal 143 70
"""
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 70
    assert "had to kill workload" in result.stderr


@pytest.mark.parametrize(
    ("handler_status", "expected_status", "message"),
    [(0, 143, ""), (1, 70, "failed during shutdown status=1")],
)
def test_trusted_supervisor_preserves_child_shutdown_failure(
    handler_status: int, expected_status: int, message: str
) -> None:
    wrapper = _read("deploy/run_authorized_runtime_trusted.sh")
    helpers = wrapper[wrapper.index("child_is_running()") : wrapper.index("\ntrap 'stop_for_signal")]
    child = f"import signal,time; signal.signal(signal.SIGTERM, lambda *_: exit({handler_status})); time.sleep(30)"
    harness = f"""
set -u
stop_grace_seconds=2
child_pid=""
child_was_forced=0
child_stop_status=0
{helpers}
{shlex.quote(sys.executable)} -c {shlex.quote(child)} &
child_pid=$!
sleep 0.2
stop_for_signal 143 70
"""
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == expected_status
    assert message in result.stderr


def test_trusted_supervisor_accepts_a_default_sigterm_exit() -> None:
    wrapper = _read("deploy/run_authorized_runtime_trusted.sh")
    helpers = wrapper[wrapper.index("child_is_running()") : wrapper.index("\ntrap 'stop_for_signal")]
    harness = f"""
set -u
stop_grace_seconds=2
child_pid=""
child_was_forced=0
child_stop_status=0
{helpers}
{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)' &
child_pid=$!
sleep 0.2
stop_for_signal 143 70
"""
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 143, result.stderr
