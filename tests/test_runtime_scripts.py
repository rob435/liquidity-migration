from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.storage import read_dataset, write_dataset

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy_vps_live.sh"
VERIFY_SH = REPO_ROOT / "scripts" / "verify_vps_live.sh"
RECOVERY_SH = REPO_ROOT / "scripts" / "vps_console_recover_and_deploy.sh"

_ACCOUNT_ROOT_LABELS = (
    "demo account root",
    "demo intent inbox root",
    "demo capture root",
    "paper account root",
    "paper intent inbox root",
    "paper capture root",
)


def _run_account_root_validator(
    script: Path,
    roots: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    assert len(roots) == len(_ACCOUNT_ROOT_LABELS)
    text = script.read_text(encoding="utf-8")
    start = text.index("validate_account_execution_roots() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    function = text[start:end]
    args = ["test account routes"]
    for label, root in zip(_ACCOUNT_ROOT_LABELS, roots, strict=True):
        args.extend((label, root))
    command = "\n".join(
        (
            function,
            f"PYTHON={shlex.quote(sys.executable)}",
            "validate_account_execution_roots " + " ".join(shlex.quote(value) for value in args),
        )
    )
    return subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_v7_operational_launcher_cannot_resume_a_spent_tape() -> None:
    launcher = (
        REPO_ROOT / "scripts" / "run_demo_execution_calibration.py"
    ).read_text(encoding="utf-8")

    assert 'parser.add_argument("--resume"' not in launcher
    assert "driver.run(plan, resume=False)" in launcher
    assert "preserve the failed attempt and register a new epoch" in launcher


@pytest.mark.parametrize("real_money", ["1", "enabled"])
def test_v7_operational_launcher_rejects_non_false_real_money(
    real_money: str,
) -> None:
    env = os.environ.copy()
    for name in (
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
    ):
        env.pop(name, None)
    env["REAL_MONEY"] = real_money
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_demo_execution_calibration.py"),
            "--account-root",
            "unused",
            "--inbox-root",
            "unused",
            "--demo-rules-file",
            "unused",
            "--event-tape",
            "unused",
            "--output",
            "/unused",
            "--expected-commit",
            "0" * 40,
            "--plan-id",
            "unused",
            "--confirm-demo-calibration",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 2
    assert "REAL_MONEY unset or explicitly false" in result.stderr


@pytest.mark.parametrize(
    ("script", "first_startup_action"),
    [
        (
            DEPLOY_SH,
            "systemctl enable liquidity-migration-liquidation-collector.service",
        ),
        (
            RECOVERY_SH,
            "systemctl enable liquidity-migration-account-execution.service",
        ),
        (
            VERIFY_SH,
            "systemctl is-enabled --quiet liquidity-migration-account-execution.service",
        ),
    ],
)
def test_account_root_isolation_runs_before_authorized_unit_startup(
    script: Path,
    first_startup_action: str,
) -> None:
    text = script.read_text(encoding="utf-8")
    call = text.index("if ! validate_account_execution_roots")
    call_end = text.index("; then", call)
    call_block = text[call:call_end]

    for variable in (
        "DEMO_ACCOUNT_EXECUTION_ROOT",
        "DEMO_ACCOUNT_INTENT_INBOX_ROOT",
        "DEMO_ACCOUNT_CAPTURE_ROOT",
        "PAPER_ACCOUNT_EXECUTION_ROOT",
        "PAPER_ACCOUNT_INTENT_INBOX_ROOT",
        "PAPER_ACCOUNT_CAPTURE_ROOT",
    ):
        assert f'"${variable}"' in call_block
    assert call < text.index(first_startup_action)


@pytest.mark.parametrize("script", [DEPLOY_SH, RECOVERY_SH])
def test_install_preflight_exits_before_secrets_readiness_and_startup(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    branch_start = text.index('if [ "$INSTALL_PREFLIGHT_ONLY" = "1" ]; then')
    branch_end = text.index("\nfi", branch_start) + len("\nfi")
    branch = text[branch_start:branch_end]

    assert "lm_install_current_systemd_units" in branch
    assert "install-preflight-ok" in branch
    assert "exit 0" in branch
    assert branch_start < text.index(
        "if [ ! -f /etc/liquidity-migration/bybit-demo.env",
        branch_end,
    )
    assert branch_start < text.index("account-execution-ready", branch_end)
    for forbidden in (
        "systemctl enable",
        "systemctl start",
        "systemctl restart",
        "touch ",
        "/etc/liquidity-migration/bybit-demo.env",
        "/etc/liquidity-migration/account-execution-ready",
    ):
        assert forbidden not in branch


@pytest.mark.parametrize("script", [DEPLOY_SH, RECOVERY_SH])
def test_install_preflight_requires_fleet_quiescence_before_checkout(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    call = text.index("require_install_preflight_quiescence\n")
    checkout = text.index('cd "$REPO_DIR"') if script == DEPLOY_SH else text.index('if [ -e "$REPO_DIR" ]')

    assert call < checkout
    function = text[text.index("require_install_preflight_quiescence()") : call]
    assert "systemctl list-units 'liquidity-migration-*' --all" in function
    assert '$3 != "inactive" && $3 != "failed"' in function
    assert "failed to inspect liquidity-migration unit state" in function
    assert "quiesce every liquidity-migration unit before checkout" in function


def test_install_preflight_can_checkout_candidate_branch_ahead_of_main(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    vps = tmp_path / "vps"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=seed, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=seed,
        check=True,
    )
    (seed / "state.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "state.txt"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed, check=True)
    subprocess.run(
        ["git", "checkout", "-b", "codex/account-cutover"],
        cwd=seed,
        check=True,
    )
    (seed / "state.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "candidate"], cwd=seed, check=True)
    candidate = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=seed,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-u", "origin", "codex/account-cutover"],
        cwd=seed,
        check=True,
    )
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(vps)],
        check=True,
        capture_output=True,
        text=True,
    )

    text = DEPLOY_SH.read_text(encoding="utf-8")

    def function(name: str) -> str:
        start = text.index(f"{name}() {{")
        end = text.index("\n}\n", start) + len("\n}\n")
        return text[start:end]

    command = "\n".join(
        (
            "set -euo pipefail",
            function("git_with_optional_github_token"),
            function("checkout_expected_branch_commit"),
            f"REPO_URL={shlex.quote(str(remote))}",
            "REMOTE=origin",
            "BRANCH=codex/account-cutover",
            f"EXPECTED_COMMIT={candidate}",
            "GITHUB_TOKEN=",
            "checkout_expected_branch_commit",
        )
    )
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=vps,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    observed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=vps,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert observed == candidate


def test_full_deploy_requires_capture_and_verified_authorization_before_checkout() -> None:
    text = DEPLOY_SH.read_text(encoding="utf-8")
    checkout = text.index('cd "$REPO_DIR"')
    authority_check = text.index("require_full_deploy_authority\n")
    function = text[text.index("require_full_deploy_authority()") : authority_check]

    assert authority_check < checkout
    assert "account-execution-capture-enabled" in function
    assert "account-execution-deploy-ready" in function
    assert "account-execution-ready" in function
    assert "account_execution_cutover_authority.py" in function
    assert "--expected-commit" in function
    assert "--repo-root" in function
    assert "staged cutover authority verifier is unavailable" in function
    assert "[ ! -e /etc/liquidity-migration/account-execution-deploy-ready ]" not in function


def test_recovery_routes_activated_state_through_latch_and_refuses_other_phases() -> None:
    text = RECOVERY_SH.read_text(encoding="utf-8")
    exact_head = text.index('git -C "$REPO_DIR" rev-parse --verify HEAD')
    archive = text.index('git diff --no-ext-diff --binary')
    cleanup = text.index('git reset --hard "$EXPECTED_COMMIT"')
    source_clean_helper = text.index('. "$phase_library"')
    phase = text.index('CUTOVER_PHASE="$(lm_fresh_epoch_phase')
    latch_verify = text.index("lm_verify_authorized_deploy_epoch", phase)
    fetch = text.index('if [ "$CUTOVER_PHASE" = "activated" ]; then', latch_verify)
    first_start = text.index("systemctl enable liquidity-migration-account-execution.service")

    assert exact_head < archive < cleanup < source_clean_helper < phase < latch_verify < fetch < first_start
    assert "lm_fresh_epoch_phase" not in text[:cleanup]
    assert "lm_verify_authorized_deploy_epoch" not in text[:cleanup]
    assert 'phase_python="/usr/bin/python3"' in text[cleanup:latch_verify]
    assert ".venv/bin/python" not in text[cleanup:latch_verify]
    assert "PYTHONPYCACHEPREFIX" in text[cleanup:latch_verify]
    assert 'git reset --hard "$EXPECTED_COMMIT"' in text
    assert "full lowercase latch-bound commit" in text
    assert "use the checked initial deploy while its short-lived authorization is valid" in text
    assert "fresh-epoch state is partial" in text
    assert "account_execution_cutover_authority.py verify" not in text
    assert "Reusing activated latch-bound checkout without a private-repository fetch" in text


def test_checked_deploy_keeps_private_repo_token_only_through_authority_checks() -> None:
    text = DEPLOY_SH.read_text(encoding="utf-8")
    precheck = text.index('GITHUB_TOKEN="$GITHUB_TOKEN"', text.index("require_full_deploy_authority"))
    fetch = text.index("checkout_expected_branch_commit\n")
    postcheck = text.index(
        'GITHUB_TOKEN="$GITHUB_TOKEN"',
        text.index("scripts/check_bybit_order_permissions.py --context deploy"),
    )
    prepare = text.index("lm_prepare_authorized_deploy_epoch", postcheck)
    unset = text.index("unset GITHUB_TOKEN", postcheck)

    assert precheck < fetch < postcheck < prepare < unset


@pytest.mark.parametrize("script", [DEPLOY_SH, RECOVERY_SH])
def test_private_repo_token_never_enters_git_argv(
    script: Path, tmp_path: Path
) -> None:
    text = script.read_text(encoding="utf-8")
    start = text.index("git_with_optional_github_token() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    function = text[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_capture = tmp_path / "argv"
    config_capture = tmp_path / "config"
    token_capture = tmp_path / "token-env"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\0' \"$@\" >\"$ARGV_CAPTURE\"\n"
        "printf '%s' \"${GIT_CONFIG_VALUE_0:-}\" >\"$CONFIG_CAPTURE\"\n"
        "printf '%s' \"${GITHUB_TOKEN:-}\" >\"$TOKEN_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    token = "ghp_private-token-sentinel"
    command = "\n".join(
        (
            "set -euo pipefail",
            function,
            'REPO_URL="https://github.com/rob435/liquidity-migration.git"',
            f"GITHUB_TOKEN={shlex.quote(token)}",
            "git_with_optional_github_token fetch origin main",
        )
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ARGV_CAPTURE": str(argv_capture),
            "CONFIG_CAPTURE": str(config_capture),
            "TOKEN_CAPTURE": str(token_capture),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    argv = argv_capture.read_bytes()
    encoded = base64.b64encode(f"x-access-token:{token}".encode())
    assert token.encode() not in argv
    assert encoded not in argv
    assert config_capture.read_text(encoding="utf-8") == (
        "AUTHORIZATION: Basic " + encoded.decode()
    )
    assert token_capture.read_text(encoding="utf-8") == ""
    assert token not in result.stdout
    assert token not in result.stderr


def test_deploy_and_recovery_install_only_exact_locked_dependencies() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(
        encoding="utf-8"
    )
    for text in (
        DEPLOY_SH.read_text(encoding="utf-8"),
        RECOVERY_SH.read_text(encoding="utf-8"),
        workflow,
    ):
        assert "--no-deps" in text
        assert "--only-binary=:all:" in text
        assert "-r requirements.lock" in text
        assert "pip install --upgrade pip" not in text
        assert 'pip install -e ".[dev]"' not in text


def test_prepare_helper_passes_private_repo_token_only_in_child_environment(
    tmp_path: Path,
) -> None:
    fake_python = tmp_path / "python"
    token_capture = tmp_path / "token"
    argv_capture = tmp_path / "argv"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\' "${GITHUB_TOKEN:-}" >"$TOKEN_CAPTURE"\n'
        'printf \'%s\\n\' "$@" >"$ARGV_CAPTURE"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    token = "private-token-sentinel"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; GITHUB_TOKEN="$1"; source "$2"; '
            'lm_prepare_authorized_deploy_epoch "$3" /repo "' + "a" * 40 + '"',
            "_",
            token,
            str(REPO_ROOT / "deploy" / "lib_fresh_epoch.sh"),
            str(fake_python),
        ],
        env={
            **os.environ,
            "TOKEN_CAPTURE": str(token_capture),
            "ARGV_CAPTURE": str(argv_capture),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert token_capture.read_text(encoding="utf-8") == token
    assert token not in argv_capture.read_text(encoding="utf-8")
    assert token not in result.stdout
    assert token not in result.stderr


def test_live_verifier_uses_activated_latch_without_renewing_authorization() -> None:
    text = VERIFY_SH.read_text(encoding="utf-8")

    assert "scripts/account_execution_cutover_authority.py verify" not in text
    assert "lm_verify_authorized_deploy_epoch" in text
    assert 'lm_verify_authorized_deploy_epoch "$PYTHON" "$REPO_DIR" "$actual_commit"' in text
    assert 'lm_verify_active_fresh_processes "$PYTHON" "$REPO_DIR" "$actual_commit"' in text
    assert "private-repository network lookup" in text

    workflow = (REPO_ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(
        encoding="utf-8"
    )
    verify_step = workflow.split("      - name: Read-only verify\n", 1)[1]
    assert "GITHUB_TOKEN:" not in verify_step


def test_install_current_systemd_units_purges_only_unknown_units_without_starting(
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / "etc-systemd"
    runtime_dir = tmp_path / "run-systemd"
    fake_bin = tmp_path / "bin"
    log = tmp_path / "systemctl.log"
    unit_dir.mkdir()
    runtime_dir.mkdir()
    fake_bin.mkdir()

    retired = "liquidity-migration-bybit-risk.service"
    (unit_dir / retired).write_text("retired\n", encoding="utf-8")
    retired_dropin = runtime_dir / f"{retired}.d"
    retired_dropin.mkdir()
    (retired_dropin / "legacy.conf").write_text("legacy\n", encoding="utf-8")
    orphaned = "liquidity-migration-combined-book-report.service"
    orphaned_dropin = unit_dir / f"{orphaned}.d"
    orphaned_dropin.mkdir()
    (orphaned_dropin / "legacy.conf").write_text("legacy\n", encoding="utf-8")
    broken = "liquidity-migration-continuous-forward-report.timer"
    wants = unit_dir / "timers.target.wants"
    wants.mkdir()
    broken_link = wants / broken
    broken_link.symlink_to(unit_dir / broken)

    current = "liquidity-migration-bybit-continuous-demo.service"
    current_dropin = unit_dir / f"{current}.d"
    current_dropin.mkdir()
    (current_dropin / "telegram-quiet.conf").write_text("mute\n", encoding="utf-8")
    operator_dropin = current_dropin / "operator.conf"
    operator_dropin.write_text("keep\n", encoding="utf-8")

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
            case "${1:-}" in
              list-unit-files|list-units) ;;
              show)
                unit="$2"
                case "$*" in
                  *--property=FragmentPath*) printf '%s/%s\n' "$LM_SYSTEMD_UNIT_DIR" "$unit" ;;
                  *--property=DropInPaths*) printf '\n' ;;
                  *--property=ExecStartPost*)
                    printf '{ path=/opt/liquidity-migration/scripts/run_authorized_fresh_runtime.sh ; argv[]=/opt/liquidity-migration/scripts/run_authorized_fresh_runtime.sh %s readiness ; ignore_errors=no ; }\n' "$unit"
                    ;;
                  *--property=ExecStart*)
                    printf '{ path=/opt/liquidity-migration/scripts/run_authorized_fresh_runtime.sh ; argv[]=/opt/liquidity-migration/scripts/run_authorized_fresh_runtime.sh %s main ; ignore_errors=no ; }\n' "$unit"
                    ;;
                esac
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    command = textwrap.dedent(
        f"""\
        set -euo pipefail
        export PATH={shlex.quote(str(fake_bin))}:$PATH
        export SYSTEMCTL_LOG={shlex.quote(str(log))}
        export LM_SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
        export LM_RUNTIME_SYSTEMD_UNIT_DIR={shlex.quote(str(runtime_dir))}
        . {shlex.quote(str(REPO_ROOT / "deploy" / "lib_sleeves.sh"))}
        lm_install_current_systemd_units
        """
    )
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "guarded unit has an unreviewed drop-in" in result.stderr
    assert operator_dropin.read_text(encoding="utf-8") == "keep\n"

    operator_dropin.unlink()
    current_dropin.rmdir()
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    for source in (REPO_ROOT / "deploy" / "systemd").glob("liquidity-migration-*.*"):
        if source.suffix not in {".service", ".timer"}:
            continue
        assert (unit_dir / source.name).read_bytes() == source.read_bytes()
    assert not (unit_dir / retired).exists()
    assert not retired_dropin.exists()
    assert not orphaned_dropin.exists()
    assert not broken_link.is_symlink()
    assert not (current_dropin / "telegram-quiet.conf").exists()

    commands = log.read_text(encoding="utf-8").splitlines()
    assert f"disable --now {retired}" in commands
    assert f"disable --now {orphaned}" in commands
    assert f"disable --now {broken}" in commands
    assert commands.count("daemon-reload") == 4
    assert not any(re.match(r"^(enable|start|restart|try-restart)(?:\s|$)", command) for command in commands)


@pytest.mark.parametrize("script", [DEPLOY_SH, VERIFY_SH, RECOVERY_SH])
def test_account_root_validator_accepts_six_absolute_disjoint_roots(script: Path) -> None:
    roots = tuple(f"/srv/liquidity-migration/{index}" for index in range(6))

    result = _run_account_root_validator(script, roots)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [DEPLOY_SH, VERIFY_SH, RECOVERY_SH])
@pytest.mark.parametrize("overlap", ["equal", "nested", "canonical-alias"])
def test_account_root_validator_rejects_overlapping_demo_and_paper_roots(
    script: Path,
    overlap: str,
) -> None:
    roots = [f"/srv/liquidity-migration/{index}" for index in range(6)]
    if overlap == "equal":
        roots[3] = roots[0]
    elif overlap == "nested":
        roots[3] = f"{roots[0]}/paper"
    else:
        roots[3] = "/srv/liquidity-migration/alias/../0"

    result = _run_account_root_validator(script, tuple(roots))

    assert result.returncode != 0
    assert "account execution roots must be pairwise disjoint" in result.stderr
    assert "demo account root" in result.stderr
    assert "paper account root" in result.stderr


@pytest.mark.parametrize("script", [DEPLOY_SH, VERIFY_SH, RECOVERY_SH])
@pytest.mark.parametrize(
    ("bad_index", "bad_value", "message"),
    [
        (1, "data/demo-intents", "must be absolute"),
        (5, "", "paper capture root is required"),
    ],
)
def test_account_root_validator_rejects_relative_or_incomplete_routes(
    script: Path,
    bad_index: int,
    bad_value: str,
    message: str,
) -> None:
    roots = [f"/srv/liquidity-migration/{index}" for index in range(6)]
    roots[bad_index] = bad_value

    result = _run_account_root_validator(script, tuple(roots))

    assert result.returncode != 0
    assert message in result.stderr


def test_continuous_hedge_timer_reconciles_within_five_minutes() -> None:
    timer = (REPO_ROOT / "deploy/systemd/liquidity-migration-continuous-hedge.timer").read_text(encoding="utf-8")
    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "OnCalendar=" not in timer


def _unit_env(unit: str) -> dict[str, str]:
    text = (REPO_ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        body = line.split("=", 1)[1]
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        env[key] = value
    return env


def test_deploy_verify_require_unit_env_matches_unit_files() -> None:
    units = {
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
    }
    unit_env = {unit: _unit_env(unit) for unit in units}
    pattern = re.compile(r"require_unit_env\s+([^\s]+)\s+'([^'=]+)=([^']*)'")

    for script in (DEPLOY_SH, VERIFY_SH, RECOVERY_SH):
        text = script.read_text(encoding="utf-8")
        for unit, key, expected in pattern.findall(text):
            if unit not in unit_env:
                continue
            assert key in unit_env[unit], f"{script.name}: {unit} checks missing env {key}"
            assert unit_env[unit][key] == expected, (
                f"{script.name}: {unit} checks {key}={expected!r}, but unit file sets {unit_env[unit][key]!r}"
            )


def test_verify_vps_serializes_remote_values_without_shell_injection(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "ssh-stdin"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncat >"$CAPTURE"\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    sentinel = tmp_path / "injected"
    values = {
        "REPO_DIR": f"/tmp/o'hare; touch {sentinel}; #",
        "EXPECTED_COMMIT": f"abc$(touch {sentinel})'def",
        "EXPECTED_TELEGRAM_CHAT_ID": "id with spaces;false",
        "SYSTEMD_SETTLE_SECONDS": "0",
    }
    env = {
        **os.environ,
        **values,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
    }

    subprocess.run(["bash", str(VERIFY_SH)], env=env, check=True, timeout=10)

    assert not sentinel.exists()
    prelude = tmp_path / "prelude.sh"
    prelude.write_text("\n".join(capture.read_text().splitlines()[:4]) + "\n")
    decoded = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; printf "%s\\0%s\\0%s\\0%s" '
            '"$REPO_DIR" "$EXPECTED_COMMIT" "$EXPECTED_TELEGRAM_CHAT_ID" '
            '"$SYSTEMD_SETTLE_SECONDS"',
            "_",
            str(prelude),
        ],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    assert [part.decode() for part in decoded] == list(values.values())


def test_deploy_uses_local_gh_token_without_exposing_it(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "ssh-stdin"
    gh_args = tmp_path / "gh-args"
    sentinel = tmp_path / "injected"
    token = f"ghp_test token;$(touch {sentinel})'"

    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncat >"$CAPTURE"\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$*" >"$GH_ARGS_CAPTURE"\n'
        "printf '%s\\n' \"$GH_TOKEN_SENTINEL\"\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "GH_ARGS_CAPTURE": str(gh_args),
        "GH_TOKEN_SENTINEL": token,
    }
    env.pop("GITHUB_TOKEN", None)

    result = subprocess.run(
        ["bash", str(DEPLOY_SH)],
        env=env,
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )

    assert gh_args.read_text(encoding="utf-8").strip() == "auth token --hostname github.com"
    assert "authenticated local gh credential" in result.stdout
    assert token not in result.stdout
    assert token not in result.stderr
    assert not sentinel.exists()

    prelude = tmp_path / "prelude.sh"
    prelude.write_text("\n".join(capture.read_text().splitlines()[:8]) + "\n")
    decoded = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; printf "%s" "$GITHUB_TOKEN"',
            "_",
            str(prelude),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert decoded == token
    assert not sentinel.exists()


def test_verify_vps_accepts_unique_abbreviated_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "test"], cwd=repo, check=True)
    full_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "ssh-stdin"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncat >"$CAPTURE"\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "REPO_DIR": str(repo),
        "EXPECTED_COMMIT": full_sha[:8],
        "SYSTEMD_SETTLE_SECONDS": "0",
    }
    subprocess.run(["bash", str(VERIFY_SH)], env=env, check=True, timeout=10)

    remote_lines = capture.read_text(encoding="utf-8").splitlines()
    python_marker = remote_lines.index("if [ -x .venv/bin/python ]; then")
    commit_check = "\n".join(remote_lines[:python_marker]) + "\n"
    result = subprocess.run(
        ["bash"],
        input=commit_check,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_execution_authorities_are_removed() -> None:
    repo = Path(__file__).resolve().parents[1]
    retired = (
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-risk.service",
        repo / "deploy" / "systemd" / "liquidity-migration-combined-book-report.service",
        repo / "deploy" / "systemd" / "liquidity-migration-combined-book-report.timer",
    )

    assert all(not path.exists() for path in retired)


def test_continuous_event_demo_cycle_parser_target_profile_flags() -> None:
    from liquidity_migration.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "continuous-event-demo-cycle",
            "--execution-environment",
            "demo",
            "--strategy-profile",
            "continuous_ensemble_v2",
            "--feature-set",
            "max_ret168",
            "--entry-event-trigger",
            "none",
            "--btc-trend-gate",
            "uptrend",
        ]
    )

    assert args.command == "continuous-event-demo-cycle"
    assert args.strategy_profile == "continuous_ensemble_v2"
    assert args.feature_set == "max_ret168"
    assert args.entry_event_trigger == "none"
    assert args.btc_trend_gate == "uptrend"


def test_continuous_runner_wires_target_profile_env() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_continuous_demo_event_engine.sh").read_text(encoding="utf-8")

    # 2026-06-18: the live default is the repaired v2 lifecycle.
    assert 'STRATEGY_PROFILE="${STRATEGY_PROFILE:-continuous_ensemble_v2}"' in text
    assert 'FEATURE_SET="${FEATURE_SET:-max_ret168}"' in text
    assert 'MAX_HOLD_HOURS="${MAX_HOLD_HOURS:-24}"' in text
    assert "confirmed-bar +1h membership" in text
    stale_entry_labels = ("no " + "1h", "no" + "-1h")
    assert all(label not in text.casefold() for label in stale_entry_labels)
    assert '--strategy-profile "$STRATEGY_PROFILE"' in text
    assert '--feature-set "$FEATURE_SET"' in text
    assert '--max-hold-hours "$MAX_HOLD_HOURS"' in text
    assert 'NOTIONAL_MULTIPLIER="${NOTIONAL_MULTIPLIER:-1}"' in text
    assert '--notional-multiplier "$NOTIONAL_MULTIPLIER"' in text
    assert 'SIZING_MODE="${SIZING_MODE:-inverse_vol}"' in text
    assert 'TARGET_VOL_PER_NAME="${TARGET_VOL_PER_NAME:-0.01}"' in text
    assert 'VOL_WEIGHT_CLAMP="${VOL_WEIGHT_CLAMP:-2}"' in text
    assert '--sizing-mode "$SIZING_MODE"' in text
    assert '--target-vol-per-name "$TARGET_VOL_PER_NAME"' in text
    assert '--vol-weight-clamp "$VOL_WEIGHT_CLAMP"' in text
    for retired in (
        "LEFT_DECILE_EXIT_ENABLED",
        "STOP_APPROACH_FRAC",
        "FAILED_FADE_HOURS",
        "BREAKEVEN_ARM_PCT",
        "FALLBACK_EQUITY_USDT",
        "ENTRY_PORTFOLIO_HEAT_CAP_FRAC",
        "ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC",
        "DAILY_REBALANCE_ENABLED",
        "--telegram",
        "--record-dry-run",
    ):
        assert retired not in text
    # Rejected adverse-limit mode has no launcher reactivation surface.
    assert "CONTINUOUS_SNIPER" not in text
    assert "--sniper-enabled" not in text


def test_continuous_units_target_profile_but_stay_kill_switch_controlled() -> None:
    repo = Path(__file__).resolve().parents[1]
    # 2026-06-10: both units run the validated continuous_ensemble_v2 ensemble
    # (the profile owns triggers/age/TP per component; unit-level trigger is none).
    for unit_name in (
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit_name).read_text(encoding="utf-8")
        assert "Environment=STRATEGY_PROFILE=continuous_ensemble_v2" in text
        assert "Environment=FEATURE_SET=max_ret168" in text
        assert "Environment=ENTRY_EVENT_TRIGGER=none" in text
        assert "Environment=BTC_TREND_GATE=uptrend" in text
        assert "Environment=SIZING_MODE=inverse_vol" in text
        assert "Environment=TARGET_VOL_PER_NAME=0.01" in text
        assert "Environment=VOL_WEIGHT_CLAMP=2" in text
        assert "Environment=ENTRY_LEVERAGE=10" in text
        assert "Environment=NOTIONAL_MULTIPLIER=10" in text
        assert "Environment=PER_POSITION_NOTIONAL_PCT_EQUITY=2" in text
        for retired in (
            "LEFT_DECILE_EXIT_ENABLED",
            "STOP_APPROACH_FRAC",
            "FAILED_FADE_HOURS",
            "BREAKEVEN_ARM_PCT",
            "DAILY_REBALANCE_ENABLED",
            "ENTRY_PORTFOLIO_HEAT_CAP_FRAC",
            "ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC",
            "TELEGRAM_ENABLED",
        ):
            assert retired not in text
    demo_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-demo.service").read_text(
        encoding="utf-8"
    )
    paper_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-paper.service").read_text(
        encoding="utf-8"
    )
    # The rejected demo-only adverse add is absent rather than flag-disabled.
    assert "CONTINUOUS_SNIPER" not in demo_text
    assert "CONTINUOUS_SNIPER" not in paper_text
    # 2f hedge publisher armed. It requires the account owner route and never
    # receives credentials or direct venue-order confirmation.
    hedge_text = (repo / "deploy" / "systemd" / "liquidity-migration-continuous-hedge.service").read_text(
        encoding="utf-8"
    )
    assert "EnvironmentFile=/etc/liquidity-migration/sleeves.resolved.env" in hedge_text
    assert "EnvironmentFile=/etc/liquidity-migration/account-execution.env" in hedge_text
    assert "Environment=ACCOUNT_EXECUTION_KERNEL_REQUIRED=1" in hedge_text
    assert "Environment=ACCOUNT_PAPER_KERNEL_REQUIRED=0" in hedge_text
    assert "Environment=EXECUTION_ENVIRONMENT=demo" in hedge_text
    assert "Environment=HEDGE_MODE=2f" in hedge_text
    assert "Environment=HEDGE_ACTION=execute" in hedge_text
    assert "SUBMIT_HEDGE" not in hedge_text
    assert "Environment=TELEGRAM_ENABLED=0" in hedge_text
    assert "UnsetEnvironment=BYBIT_DEMO_API_KEY" in hedge_text
    assert "CONFIRM_DEMO_ORDERS" not in hedge_text
    assert "bybit-demo.env" not in hedge_text


def test_continuous_target_producers_pin_the_same_live_sizing_defaults() -> None:
    repo = Path(__file__).resolve().parents[1]
    required = (
        "ENTRY_LEVERAGE=10",
        "NOTIONAL_MULTIPLIER=10",
        "PER_POSITION_NOTIONAL_PCT_EQUITY=2",
    )
    units = (
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
    )

    for script_name in (
        "deploy_vps_live.sh",
        "verify_vps_live.sh",
        "vps_console_recover_and_deploy.sh",
    ):
        text = (repo / "scripts" / script_name).read_text(encoding="utf-8")
        for unit in units:
            for assignment in required:
                assert f"require_unit_env {unit} '{assignment}'" in text


def test_long_units_pin_descriptive_v11a_profile() -> None:
    repo = Path(__file__).resolve().parents[1]
    demo_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-long-demo.service").read_text(
        encoding="utf-8"
    )
    paper_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-long-paper.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=STRATEGY_PROFILE=LongV11aDivWeekendVol" in demo_text
    assert "Environment=EXECUTION_ENVIRONMENT=demo" in demo_text
    assert "Environment=STRATEGY_PROFILE=LongV11aDivWeekendVol" in paper_text
    assert "Environment=EXECUTION_ENVIRONMENT=paper" in paper_text


def test_continuous_rmom_refresh_rebuilds_each_active_sleeve_root() -> None:
    """audit2 (deploy-env-timers-3 follow-up): since 7d39d61 the paper shadow streams
    its OWN kline pool (KLINES_FOLLOW_ROOT dropped from the paper unit), so it reads
    its rmom gate from its OWN root. The refresh must therefore rebuild EACH on
    sleeve's own root — refreshing only the demo root left the paper book reading a
    gate nothing builds, so it emitted zero entries forever and the paper<->demo cost
    reconcile had nothing to pair."""
    repo = Path(__file__).resolve().parents[1]
    service = (repo / "deploy" / "systemd" / "liquidity-migration-continuous-rmom-refresh.service").read_text(
        encoding="utf-8"
    )
    script = (repo / "scripts" / "run_continuous_rmom_refresh.sh").read_text(encoding="utf-8")

    assert "run_authorized_fresh_runtime.sh liquidity-migration-continuous-rmom-refresh.service main" in service
    runtime_wrapper = (repo / "scripts" / "run_authorized_fresh_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "run_continuous_rmom_refresh.sh" in runtime_wrapper
    # Sleeve-aware: each root is rebuilt only when ITS sleeve is on.
    assert 'sleeve_on "${CONTINUOUS_SLEEVE' in script
    assert 'sleeve_on "${CONTINUOUS_PAPER_SLEEVE' in script
    assert 'CONTINUOUS_DEMO_DATA_ROOT="${CONTINUOUS_DEMO_DATA_ROOT-data/bybit-continuous-demo-event}"' in script
    assert 'CONTINUOUS_PAPER_DATA_ROOT="${CONTINUOUS_PAPER_DATA_ROOT-data/bybit-continuous-paper-event}"' in script
    assert '--root "$CONTINUOUS_DEMO_DATA_ROOT"' in script
    assert '--root "$CONTINUOUS_PAPER_DATA_ROOT"' in script
    assert "must be non-empty" in script
    assert "--full-rewrite" in script  # live roots are rolling stores; append overlap can drift
    deploy = (repo / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    assert '_check_rmom_root "demo" "$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet"' in deploy
    assert '_check_rmom_root "paper" "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet"' in deploy
    assert "run_continuous_rmom_refresh.sh never writes the paper root" not in deploy


def _active_lines(unit_text: str) -> list[str]:
    """Non-comment, non-blank directive lines of a systemd unit."""
    return [ln.strip() for ln in unit_text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_runtime_consumers_load_one_late_per_unit_fresh_environment() -> None:
    unit_names = (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
        "liquidity-migration-continuous-hedge.service",
        "liquidity-migration-continuous-rmom-refresh.service",
        "liquidity-migration-demo-liveness.service",
    )
    systemd_dir = REPO_ROOT / "deploy" / "systemd"
    for unit_name in unit_names:
        lines = _active_lines((systemd_dir / unit_name).read_text(encoding="utf-8"))
        expected = f"EnvironmentFile=-/etc/liquidity-migration/fresh-deploy/{unit_name}.env"
        assert lines.count(expected) == 1, unit_name
        late_index = lines.index(expected)
        wrapper_prefix = (
            "ExecStart=/opt/liquidity-migration/scripts/"
            f"run_authorized_fresh_runtime.sh {unit_name} "
        )
        exec_start_lines = [line for line in lines if line.startswith("ExecStart=")]
        assert len(exec_start_lines) == 1, unit_name
        assert exec_start_lines[0].startswith(wrapper_prefix), unit_name
        assert not any(line.startswith("ExecStartPre=") for line in lines), unit_name
        assert late_index < lines.index(exec_start_lines[0]), unit_name
        prior_environment = [
            index
            for index, line in enumerate(lines)
            if line.startswith(("Environment=", "EnvironmentFile=", "UnsetEnvironment=")) and line != expected
        ]
        assert prior_environment and late_index > max(prior_environment), unit_name

    for unit_name in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-continuous-demo.service",
    ):
        lines = _active_lines((systemd_dir / unit_name).read_text(encoding="utf-8"))
        natural = "EnvironmentFile=-/etc/liquidity-migration/natural-run.env"
        fresh = f"EnvironmentFile=-/etc/liquidity-migration/fresh-deploy/{unit_name}.env"
        assert lines.index(natural) < lines.index(fresh)


def test_fresh_runtime_wrapper_verifies_inherited_environment_then_execs() -> None:
    wrapper = (REPO_ROOT / "scripts" / "run_authorized_fresh_runtime.sh").read_text(
        encoding="utf-8"
    )
    verify = wrapper.index(
        "-m liquidity_migration.authorized_deploy_epoch verify-runtime"
    )
    execute = wrapper.index('exec "${COMMAND[@]}"')
    assert verify < execute
    assert "ExecStartPre" not in wrapper
    assert "--unit \"$UNIT\"" in wrapper
    assert "${PYTHON" not in wrapper
    assert 'if [ "$#" -ne 2 ]' in wrapper
    assert "unregistered authorized fresh-runtime entrypoint" in wrapper
    assert "liquidity-migration-account-execution.service:main" in wrapper
    assert "liquidity-migration-account-execution.service:readiness" in wrapper
    assert "-m liquidity_migration.account_owner_readiness" in wrapper
    assert "--environment demo" in wrapper
    assert "--environment paper" in wrapper
    assert "--timeout-seconds 180" in wrapper
    assert "scripts/check_demo_liveness.py" in wrapper
    assert "--telegram" in wrapper
    for selector, command in {
        "liquidity-migration-account-execution.service:main": "run_account_execution_service.sh",
        "liquidity-migration-account-paper-execution.service:main": "run_account_paper_execution_service.sh",
        "liquidity-migration-bybit-long-demo.service:main": "run_bybit_long_demo_event_engine.sh",
        "liquidity-migration-bybit-long-paper.service:main": "run_bybit_long_demo_event_engine.sh",
        "liquidity-migration-bybit-continuous-demo.service:main": "run_bybit_continuous_demo_event_engine.sh",
        "liquidity-migration-bybit-continuous-paper.service:main": "run_bybit_continuous_demo_event_engine.sh",
        "liquidity-migration-continuous-hedge.service:main": "run_continuous_hedge.sh",
        "liquidity-migration-continuous-rmom-refresh.service:main": "run_continuous_rmom_refresh.sh",
        "liquidity-migration-demo-liveness.service:main": "scripts/check_demo_liveness.py",
    }.items():
        selector_index = wrapper.index(selector)
        command_index = wrapper.index(command, selector_index)
        assert selector_index < command_index < wrapper.index(";;", selector_index)

    rejected = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "run_authorized_fresh_runtime.sh"),
            "liquidity-migration-account-execution.service",
            "main",
            "/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert rejected.returncode == 2
    assert "usage:" in rejected.stderr


def test_deploy_surfaces_source_reopen_authority_bound_fresh_epoch() -> None:
    fresh_lib = (REPO_ROOT / "deploy" / "lib_fresh_epoch.sh").read_text(encoding="utf-8")
    assert "liquidity_migration.authorized_deploy_epoch prepare" in fresh_lib
    assert "liquidity_migration.authorized_deploy_epoch verify" in fresh_lib
    assert "liquidity_migration.authorized_deploy_epoch verify-processes" in fresh_lib
    assert "lm_load_fresh_epoch_roots()" in fresh_lib
    assert "verify_authorized_deploy_epoch(" in fresh_lib
    assert "NUL-delimited data" in fresh_lib
    assert "lm_fresh_unit_env_file" not in fresh_lib
    assert '    . "$(lm_fresh_unit_env_file' not in fresh_lib

    deploy = DEPLOY_SH.read_text(encoding="utf-8")
    second_authority = deploy.index("deploy-ready authorization failed after checkout")
    prepare = deploy.index("lm_prepare_authorized_deploy_epoch")
    load = deploy.index("lm_load_fresh_epoch_roots")
    first_owner_start = deploy.index("systemctl restart liquidity-migration-account-execution.service")
    assert second_authority < prepare < load < first_owner_start
    assert "lm_verify_active_fresh_processes" in deploy
    assert (
        'lm_load_fresh_epoch_roots "$PYTHON" "$REPO_DIR" "$EXPECTED_COMMIT"'
        in deploy
    )

    verify = VERIFY_SH.read_text(encoding="utf-8")
    recovery = RECOVERY_SH.read_text(encoding="utf-8")
    for text in (verify, recovery):
        assert "lm_verify_authorized_deploy_epoch" in text
        assert "lm_load_fresh_epoch_roots" in text
        assert "lm_verify_active_fresh_processes" in text
        assert "lm_prepare_authorized_deploy_epoch" not in text
    assert (
        'lm_load_fresh_epoch_roots "$PYTHON" "$REPO_DIR" "$actual_commit"'
        in verify
    )
    assert (
        'lm_load_fresh_epoch_roots "$PYTHON" "$REPO_DIR" "$EXPECTED_COMMIT"'
        in recovery
    )
    assert "$CONTINUOUS_DEMO_DATA_ROOT/residual_momentum.parquet" in deploy
    assert "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet" in deploy
    assert "DATA_ROOT=data/bybit-continuous-paper-event" not in deploy
    assert "DATA_ROOT=data/bybit-continuous-paper-event" not in verify
    assert "DATA_ROOT=data/bybit-continuous-paper-event" not in recovery


def test_fresh_epoch_root_loader_treats_paths_as_data(tmp_path: Path) -> None:
    marker = tmp_path / "shell-evaluated"
    malicious_path = f"/fresh/$(touch {marker})"
    pairs = (
        ("DEMO_ACCOUNT_EXECUTION_ROOT", malicious_path),
        ("DEMO_ACCOUNT_INTENT_INBOX_ROOT", "/fresh/demo-inbox"),
        ("DEMO_ACCOUNT_CAPTURE_ROOT", "/fresh/demo-capture"),
        ("PAPER_ACCOUNT_EXECUTION_ROOT", "/fresh/paper-account"),
        ("PAPER_ACCOUNT_INTENT_INBOX_ROOT", "/fresh/paper-inbox"),
        ("PAPER_ACCOUNT_CAPTURE_ROOT", "/fresh/paper-capture"),
        ("LONG_DEMO_DATA_ROOT", "/fresh/long-demo"),
        ("LONG_PAPER_DATA_ROOT", "/fresh/long-paper"),
        ("CONTINUOUS_DEMO_DATA_ROOT", "/fresh/continuous-demo"),
        ("CONTINUOUS_PAPER_DATA_ROOT", "/fresh/continuous-paper"),
    )
    payload = b"".join(
        key.encode("ascii") + b"\0" + value.encode("utf-8") + b"\0"
        for key, value in pairs
    )
    fake_python = tmp_path / "emit-roots"
    fake_python.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import base64",
                "import sys",
                f"sys.stdout.buffer.write(base64.b64decode({base64.b64encode(payload)!r}))",
                "",
            )
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    variable_expansions = " ".join(f'"${{{key}}}"' for key, _ in pairs)
    command = "\n".join(
        (
            f'. {shlex.quote(str(REPO_ROOT / "deploy" / "lib_fresh_epoch.sh"))}',
            "lm_load_fresh_epoch_roots "
            f"{shlex.quote(str(fake_python))} /repo {'a' * 40}",
            f"printf '%s\\0' {variable_expansions}",
        )
    )
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.split(b"\0")[:-1] == [
        value.encode("utf-8") for _, value in pairs
    ]
    assert not marker.exists()

    failing_python = tmp_path / "emit-roots-then-fail"
    failing_python.write_text(
        fake_python.read_text(encoding="utf-8") + "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    failing_python.chmod(0o755)
    failure = subprocess.run(
        [
            "bash",
            "-c",
            "\n".join(
                (
                    f'. {shlex.quote(str(REPO_ROOT / "deploy" / "lib_fresh_epoch.sh"))}',
                    "lm_load_fresh_epoch_roots "
                    f"{shlex.quote(str(failing_python))} /repo {'a' * 40}",
                )
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failure.returncode == 1
    assert "failed authority verification" in failure.stderr


def test_private_systemd_environment_loader_treats_values_as_data(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "shell-evaluated"
    literal = f"$(touch {marker})"
    environment_file = tmp_path / "private.env"
    environment_file.write_text(
        f'TOKEN="{literal}"\nEMPTY=\nIGNORED=value\n',
        encoding="utf-8",
    )
    environment_file.chmod(0o600)
    command = "\n".join(
        (
            f'. {shlex.quote(str(REPO_ROOT / "deploy" / "lib_systemd_environment.sh"))}',
            "export TOKEN=ambient MISSING=ambient EMPTY=ambient",
            "lm_load_private_systemd_environment "
            f"{shlex.quote(sys.executable)} {shlex.quote(str(environment_file))} "
            "TOKEN MISSING EMPTY",
            "printf '%s\\0%s\\0%s\\0%s' "
            '"$TOKEN" "${MISSING+x}" "${EMPTY+x}" "$EMPTY"',
        )
    )
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.split(b"\0") == [
        literal.encode("utf-8"),
        b"",
        b"x",
        b"",
    ]
    assert not marker.exists()


def test_liveness_unit_consumes_environment_bound_roots() -> None:
    unit = (REPO_ROOT / "deploy" / "systemd" / "liquidity-migration-demo-liveness.service").read_text(encoding="utf-8")
    for option in (
        "--continuous-root",
        "--continuous-paper-root",
        "--long-root",
        "--long-paper-root",
        "--account-root",
        "--account-paper-root",
        "--account-capture-root",
        "--account-paper-capture-root",
    ):
        assert option not in unit


def test_continuous_paper_unit_streams_its_own_kline_plane() -> None:
    """audit2: since 7d39d61 the paper unit no longer FOLLOWS the demo kline plane —
    KLINES_FOLLOW_ROOT was dropped so the shadow stays live even when the demo (leader)
    sleeve is off. Guard against a regression that re-adds an ACTIVE follow directive
    and against the optional drop-in mechanism being deleted. Both continuous units still
    pin their threadpools."""
    repo = Path(__file__).resolve().parents[1]
    paper = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-paper.service").read_text(
        encoding="utf-8"
    )
    demo = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-demo.service").read_text(
        encoding="utf-8"
    )
    run_script = (repo / "scripts" / "run_bybit_continuous_demo_event_engine.sh").read_text(encoding="utf-8")

    # No ACTIVE follow directive on the paper unit (comment-only mention is fine).
    assert not any(ln.startswith("Environment=KLINES_FOLLOW_ROOT=") for ln in _active_lines(paper))
    # the LEADER must never follow anyone
    assert not any(ln.startswith("Environment=KLINES_FOLLOW_ROOT=") for ln in _active_lines(demo))
    # the run script keeps the (now dormant) passthrough so a drop-in can re-enable it.
    assert "--klines-follow-root" in run_script
    for unit in (paper, demo):
        assert "Environment=POLARS_MAX_THREADS=1" in unit
        assert "Environment=OMP_NUM_THREADS=1" in unit
        assert "Environment=OPENBLAS_NUM_THREADS=1" in unit


def test_liveness_watchdog_checks_continuous_paper_evidence_root() -> None:
    repo = Path(__file__).resolve().parents[1]
    service = (repo / "deploy" / "systemd" / "liquidity-migration-demo-liveness.service").read_text(encoding="utf-8")
    script = (repo / "scripts" / "check_demo_liveness.py").read_text(encoding="utf-8")

    assert "CONTINUOUS_PAPER_DATA_ROOT" in script
    assert "ACCOUNT_EXECUTION_ROOT" in script
    assert "ACCOUNT_PAPER_EXECUTION_ROOT" in script
    assert "ACCOUNT_CAPTURE_ROOT" in script
    assert "ACCOUNT_PAPER_CAPTURE_ROOT" in script
    assert (
        "EnvironmentFile=-/etc/liquidity-migration/fresh-deploy/liquidity-migration-demo-liveness.service.env"
    ) in service
    assert "EnvironmentFile=/etc/liquidity-migration/bybit-demo.env" in service
    assert "run_authorized_fresh_runtime.sh liquidity-migration-demo-liveness.service main" in service
    wrapper = (repo / "scripts" / "run_authorized_fresh_runtime.sh").read_text(encoding="utf-8")
    assert "scripts/check_demo_liveness.py" in wrapper
    assert "--telegram" in wrapper
    assert "Environment=TELEGRAM_ENABLED=1" in service
    assert "UnsetEnvironment=BYBIT_DEMO_API_KEY" in service
    assert not any("--continuous-stop-check" in line for line in _active_lines(service))
    assert "liquidity-migration-bybit-continuous-paper.service" in script
    assert '_sleeve_on("CONTINUOUS_PAPER_SLEEVE")' in script
    assert "continuous_stop_check" not in script
    assert "--account-root /opt/liquidity-migration/data/bybit-account-execution" not in service
    assert "--account-capture-root /opt/liquidity-migration/data/bybit-account-market-capture" not in service
    assert "liquidity-migration-account-execution.service" in service
    assert "liquidity-migration-account-paper-execution.service" in service
    assert "gather_account_health_alerts" in script
    assert "gather_risk_alerts" not in script
    assert "gather_hedge_orphan_alerts" not in script


def test_continuous_rmom_timer_wired_to_paper_evidence_gate() -> None:
    repo = Path(__file__).resolve().parents[1]
    deploy = (repo / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    verify = (repo / "scripts" / "verify_vps_live.sh").read_text(encoding="utf-8")
    recovery = (repo / "scripts" / "vps_console_recover_and_deploy.sh").read_text(encoding="utf-8")
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")

    for text in (deploy, verify, recovery):
        assert "continuous_rmom_refresh_on" in text
        assert "apply_timer_enable" in text or "verify_timer" in text
        assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in text
    assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in lib
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert 'CONTINUOUS_HEDGE_SERVICES="liquidity-migration-continuous-hedge.service"' in lib


def test_account_owners_are_mandatory_for_every_live_sleeve() -> None:
    repo = Path(__file__).resolve().parents[1]
    systemd = repo / "deploy" / "systemd"
    expected = {
        "liquidity-migration-bybit-long-demo.service": (
            "liquidity-migration-account-execution.service",
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1",
            "account-execution.env",
        ),
        "liquidity-migration-bybit-continuous-demo.service": (
            "liquidity-migration-account-execution.service",
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1",
            "account-execution.env",
        ),
        "liquidity-migration-bybit-long-paper.service": (
            "liquidity-migration-account-paper-execution.service",
            "ACCOUNT_PAPER_KERNEL_REQUIRED=1",
            "account-paper-execution.env",
        ),
        "liquidity-migration-bybit-continuous-paper.service": (
            "liquidity-migration-account-paper-execution.service",
            "ACCOUNT_PAPER_KERNEL_REQUIRED=1",
            "account-paper-execution.env",
        ),
    }
    for unit, (owner, latch, env_file) in expected.items():
        text = (systemd / unit).read_text(encoding="utf-8")
        assert f"Requires={owner}" in text
        assert f"Environment={latch}" in text
        assert f"EnvironmentFile=/etc/liquidity-migration/{env_file}" in text
        assert ("ConditionPathExists=/etc/liquidity-migration/account-execution-capture-enabled") in text
        if "continuous" in unit:
            assert "Environment=TELEGRAM_ENABLED" not in text
        else:
            assert "Environment=TELEGRAM_ENABLED=0" in text
        assert "CONFIRM_DEMO_ORDERS" not in text
        assert (
            "UnsetEnvironment=BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET "
            "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY "
            "TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID"
        ) in text

    liveness = (systemd / "liquidity-migration-demo-liveness.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/liquidity-migration/bybit-demo.env" in liveness
    assert "UnsetEnvironment=BYBIT_DEMO_API_KEY" in liveness
    assert "TELEGRAM_BOT_TOKEN" not in next(
        line for line in liveness.splitlines() if line.startswith("UnsetEnvironment=")
    )


def test_long_units_lookback_days_satisfies_validation_floor() -> None:
    """ls-4: the deployed long demo/paper units MUST pass _validate_long_demo_config's
    lookback_days floor (>=95) — else every long cycle crash-fails (ValueError) and the sleeve
    silently stops trading. The env override broke this once on deploy (LOOKBACK_DAYS=90 < 95);
    pin the unit env to the code's requirement so the two can never drift apart again."""
    import re

    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        m = re.search(r"^Environment=LOOKBACK_DAYS=(\d+)", text, re.MULTILINE)
        assert m is not None, f"{unit}: no LOOKBACK_DAYS env"
        assert int(m.group(1)) >= 95, (
            f"{unit}: LOOKBACK_DAYS={m.group(1)} < 95 — _validate_long_demo_config would crash-fail every long cycle"
        )


def test_long_paper_service_selects_explicit_paper_environment() -> None:
    """The paper producer must route targets to the deterministic paper owner."""
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-long-paper.service").read_text(encoding="utf-8")
    assert "Environment=EXECUTION_ENVIRONMENT=paper" in text, (
        "long-paper service must select only the paper account route."
    )


def test_long_runner_wires_explicit_execution_environment() -> None:
    """The runner passes one environment through to the target producer."""
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_long_demo_event_engine.sh").read_text(encoding="utf-8")
    assert 'case "${EXECUTION_ENVIRONMENT:-}" in' in text
    assert '--execution-environment "$EXECUTION_ENVIRONMENT"' in text


def test_long_runner_and_units_default_to_safe_1x_sizing() -> None:
    repo = Path(__file__).resolve().parents[1]
    runner = (repo / "scripts" / "run_bybit_long_demo_event_engine.sh").read_text(encoding="utf-8")
    assert 'NOTIONAL_MULTIPLIER="${NOTIONAL_MULTIPLIER:-1}"' in runner
    assert 'MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY="${MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY:-0.5}"' in runner
    assert "--max-projected-initial-margin-pct-equity" in runner
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=NOTIONAL_MULTIPLIER=1" in text, f"{unit} must not default to 10x"
        assert "Environment=MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY=0.5" in text


def test_services_enable_ws_klines() -> None:
    """Long demo/paper services must enable the WS
    kline manager. WS_KLINES_ENABLED=1 flips the daemon onto the in-memory
    store, eliminating the per-cycle REST kline burst that caused 3-4h late
    entries on the legacy path."""
    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=WS_KLINES_ENABLED=1" in text, f"{unit}: WS_KLINES_ENABLED not set"


def test_bash_runners_wire_ws_klines_env() -> None:
    """The long bash runner must expose the WS_KLINES_* env vars as CLI args.
    Without this, the systemd Environment lines are silently dropped and the
    daemon stays on the legacy REST path."""
    repo = Path(__file__).resolve().parents[1]
    for script_name in ("run_bybit_long_demo_event_engine.sh",):
        text = (repo / "scripts" / script_name).read_text(encoding="utf-8")
        # Env vars are read with defaults.
        assert 'WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"' in text, (
            f"{script_name}: missing WS_KLINES_ENABLED default"
        )
        assert "WS_KLINES_BOOTSTRAP_WORKERS" in text, f"{script_name}: missing WS_KLINES_BOOTSTRAP_WORKERS"
        assert "WS_KLINES_LOOKBACK_DAYS" in text, f"{script_name}: missing WS_KLINES_LOOKBACK_DAYS"
        assert "WS_KLINES_UNIVERSE_REFRESH_SECONDS" in text, (
            f"{script_name}: missing WS_KLINES_UNIVERSE_REFRESH_SECONDS"
        )
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


def test_live_runners_do_not_write_repo_bytecode() -> None:
    repo = Path(__file__).resolve().parents[1]
    paths = [
        repo / "deploy" / "systemd" / "liquidity-migration-account-execution.service",
        repo / "deploy" / "systemd" / "liquidity-migration-account-paper-execution.service",
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
    assert "GITHUB_TOKEN" in text
    assert "http.https://github.com/.extraheader" in text
    assert "x-access-token:%s" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    # The deploy gate pins the active LONG profile constants.
    assert "long_cfg.universe_size == 50" in text
    assert "long_cfg.weekend_size_mult == 1.5" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "bybit-demo.env.backup" in text
    assert 'sed -i "s/^TELEGRAM_CHAT_ID=' in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "apply_timer_enable" in text
    assert "systemctl disable --now" in lib
    retired_unit_marker = "model050426"
    assert retired_unit_marker not in text
    # Deploy installs the checked-in manifest and generically removes anything
    # outside it; a hand-maintained retired allowlist can drift behind the host.
    assert "lm_install_current_systemd_units" in text
    assert "lm_install_current_systemd_units()" in lib
    assert "RETIRED_SLEEVE_UNITS" not in lib
    assert "/etc/liquidity-migration/account-execution-capture-enabled" in text
    assert "/etc/liquidity-migration/account-execution-deploy-ready" in text
    assert "/etc/liquidity-migration/account-execution.env" in text
    assert "/etc/liquidity-migration/account-paper-execution.env" in text
    for owner in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
    ):
        assert f"systemctl enable {owner}" in text
        assert f"systemctl restart {owner}" in text
        assert f"systemctl is-active --quiet {owner}" in text
        assert f"systemctl is-enabled --quiet {owner}" in text
    assert "liquidity-migration-bybit-risk.service" not in text
    # Per-sleeve kill-switch: the deploy sources the shared lib, loads the toggles, then
    # enables/restarts/verifies each sleeve THROUGH the toggle-aware helpers (an off
    # sleeve gets `disable --now`d and is not expected up). The exact unit set each
    # sleeve owns is pinned in deploy/lib_sleeves.sh (asserted just below) so a deploy
    # still can't silently drop a unit — the names just live in one canonical place.
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "lm_write_resolved_sleeve_toggles" in text
    assert "lm_verify_resolved_sleeve_toggles" in text
    assert "lm_cleanup_unknown_liqmig_units" in lib
    assert "lm_verify_no_unknown_liqmig_units" in lib
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'apply_sleeve_enable "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The canonical unit set (what each sleeve enables/restarts/verifies, and what the
    # liveness watchdog/recovery must bring up) lives in the lib — pin it there.
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "lm_expected_systemd_units()" in lib
    assert "liquidity-migration-bybit-demo.service" not in lib
    assert "liquidity-migration-bybit-paper.service" not in lib
    assert "liquidity-migration-continuous-forward-report.service" not in lib
    assert "liquidity-migration-continuous-forward-report.timer" not in lib
    assert (
        'LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"'
        in lib
    )
    assert 'CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"' in lib
    assert 'CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert 'CONTINUOUS_HEDGE_SERVICES="liquidity-migration-continuous-hedge.service"' in lib
    assert "apply_timer_enable()" in lib
    assert "verify_timer()" in lib
    assert "apply_hedge_timer_enable()" in lib
    assert "verify_hedge_timer_enable()" in lib
    assert "LM_HOST_SLEEVES_ENV" in lib
    assert "LM_RESOLVED_SLEEVES_ENV" in lib
    assert "lm_write_resolved_sleeve_toggles()" in lib
    assert "lm_verify_resolved_sleeve_toggles()" in lib
    assert "Host overrides may only turn repo-on sleeves off" in lib
    assert "timer is OFF in sleeves.env but still enabled" in lib
    assert "continuous_rmom_refresh_on()" in lib
    assert "is OFF in sleeves.env but still enabled" in lib
    sleeves = (repo / "deploy" / "sleeves.env").read_text(encoding="utf-8")
    # Continuous demo orders are ON; long is controlled by its own sleeve toggle.
    assert "CONTINUOUS_SLEEVE=on" in sleeves
    assert "CONTINUOUS_PAPER_SLEEVE=on" in sleeves
    # The liveness timer remains mandatory. Hourly reports now come from the
    # account owner rather than a second oneshot report authority.
    assert "systemctl enable --now liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-liveness.timer" in text
    assert "liquidity-migration-combined-book-report.timer" not in text
    assert "telegram-quiet.conf" in lib
    assert "liquidity-migration-bybit-continuous-demo.service.d" in lib
    assert "liquidity-migration-combined-book-report.service.d" not in lib
    assert "require_unit_env()" in text
    assert "systemctl cat" not in text
    assert (
        "require_unit_env liquidity-migration-account-execution.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'" in text
    )
    assert "require_unit_env liquidity-migration-account-execution.service 'CONFIRM_DEMO_ORDERS=1'" in text
    assert (
        "require_unit_env liquidity-migration-account-paper-execution.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    )
    # Continuous-fade sleeve (live on demo 2026-06-01): brought up like the other
    # live daemons, plus its rmom timer; risk service wired to read its ledger.
    assert "liquidity-migration-bybit-continuous-demo.service" in text
    assert "liquidity-migration-bybit-continuous-paper.service" in text
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    # The hedge timer is gated on canonical account-owned hedge state. Apply and
    # verify must use the same computed state.
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in text
    assert 'apply_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in text
    assert "canonical_strategy_trade_rows" in text
    assert 'ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT"' in text
    assert "data/bybit-continuous-hedge-event" not in text
    assert "_hedge_timer_state=on" in text
    assert "continuous_rmom_refresh_on" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'"
        in text
    )
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'EXECUTION_ENVIRONMENT=demo'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'EXECUTION_ENVIRONMENT=paper'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'EXECUTION_ENVIRONMENT=demo'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.entry_btc_risk_low == 0.70" in text
    assert "cont.entry_btc_risk_high == 0.90" in text
    assert "cont.entry_btc_risk_tail_mult == 0.35" in text
    assert "c[0]: c[3] for c in cont.ensemble_components" in text
    # Deploy must start the owner-dependent continuous daemons so their fresh
    # kline roots bootstrap before a required RMOM seed. Missing RMOM is fatal;
    # no first-boot zero-signal override is allowed.
    assert "systemctl start liquidity-migration-continuous-rmom-refresh.service" in text
    assert 'git diff --quiet "$previous_commit" HEAD --' in text
    assert 'if [ "$_rmom_needs_seed" -eq 1 ]; then' in text
    assert "skipping seed" in text
    for rmom_dependency in (
        "pyproject.toml",
        "requirements.lock",
        "deploy/systemd/liquidity-migration-continuous-rmom-refresh.service",
        "scripts/run_continuous_rmom_refresh.sh",
        "scripts/precompute_residual_momentum.py",
        "scripts/check_residual_momentum_gate.py",
        "liquidity_migration/_common.py",
        "liquidity_migration/risk_model.py",
        "liquidity_migration/daily_feature_panel.py",
        "liquidity_migration/storage.py",
    ):
        assert rmom_dependency in text
    first_continuous_restart = min(
        text.index("systemctl restart liquidity-migration-bybit-continuous-demo.service"),
        text.index("systemctl restart liquidity-migration-bybit-continuous-paper.service"),
    )
    assert first_continuous_restart < text.index("systemctl start liquidity-migration-continuous-rmom-refresh.service")
    assert "rmom gate is EMPTY, provisional-only, or stale after deploy gate check" in text
    assert "scripts/check_residual_momentum_gate.py" in text
    assert "ALLOW_EMPTY_RMOM_GATE" not in text
    assert "RMOM_BOOTSTRAP_TIMEOUT_SECONDS" in text
    assert "fresh continuous roots did not produce valid RMOM gates" in text
    # Reboot-safety invariant: the single account owner must start before any
    # target producer.
    assert text.index("systemctl enable liquidity-migration-account-execution.service") < text.index(
        'apply_sleeve_enable "$CONTINUOUS_SLEEVE"'
    )
    assert text.index("systemctl restart liquidity-migration-account-execution.service") < text.index(
        "systemctl restart liquidity-migration-bybit-continuous-demo.service"
    )
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment --value" in text
    # Daemons no longer fire startup telegrams (default off as of the
    # rapid-deploy-spam fix), so the deploy script owns the single
    # "deploy succeeded" signal — one telegram per deploy regardless of
    # how many daemons restarted.
    assert "api.telegram.org/bot" in text
    assert "deploy-verify-ok commit=$python_commit" in text
    assert "TELEGRAM_BOT_TOKEN" in text
    # Best-effort: a curl failure must not flip the deploy result. The
    # `|| echo WARN` clause keeps the script from exit-1-ing if Telegram is
    # down; verify already passed before this line runs.
    assert "deploy-confirm telegram send failed" in text


def test_vps_deploy_script_pytest_nodeids_still_collect() -> None:
    """The deploy + disaster-recovery scripts run pinned pytest subsets as
    pre-restart smoke tests. Because they `set -euo pipefail`, a stale node-id
    (e.g. a test moved by a test-file split, or deleted in a purge) makes pytest
    exit non-zero and aborts the deploy/recovery. String-presence tests can't
    catch a moved path, so verify every `tests/...` node-id BOTH scripts
    reference actually collects."""
    import re
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    for script in ("deploy_vps_live.sh", "vps_console_recover_and_deploy.sh"):
        text = (repo / "scripts" / script).read_text(encoding="utf-8")
        nodeids = re.findall(r"tests/[^\s\\]+\.py(?:::\w+)?", text)
        assert nodeids, f"expected {script} to pin a pytest smoke subset"
        for nodeid in nodeids:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", nodeid],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert proc.returncode == 0 and "no tests ran" not in proc.stdout.lower(), (
                f"{script} smoke-test node-id no longer collects: {nodeid}\n{proc.stdout}\n{proc.stderr}"
            )


def test_vps_verify_script_is_read_only_and_checks_live_state() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "verify_vps_live.sh").read_text(encoding="utf-8")

    assert "git pull" not in text
    assert "systemctl restart" not in text
    retired_unit_marker = "model050426"
    assert retired_unit_marker not in text
    # Verify must pin active configs and must not import removed strategy hubs.
    assert "liquidity_migration.volume_events" not in text
    assert "_demo_event_config" not in text
    assert "_v11a_long_native_config" in text
    assert "ContinuousDemoCycleConfig" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "require_unit_env()" in text
    assert "systemctl cat" not in text
    assert (
        "require_unit_env liquidity-migration-account-execution.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'" in text
    )
    assert (
        "require_unit_env liquidity-migration-account-paper-execution.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    )
    # Per-sleeve kill-switch: verify is toggle-aware — it sources the shared lib, loads
    # the toggles, and routes per-sleeve active+enabled checks through verify_sleeve (so
    # an intentionally-off sleeve is required DOWN, not flagged as a failed deploy). The
    # account owners are not toggled and are always verified up.
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "lm_verify_resolved_sleeve_toggles" in text
    assert "lm_verify_no_unknown_liqmig_units" in text
    assert "systemctl is-enabled --quiet liquidity-migration-account-execution.service" in text
    assert "systemctl is-active --quiet liquidity-migration-account-execution.service" in text
    assert "systemctl is-enabled --quiet liquidity-migration-account-paper-execution.service" in text
    assert "systemctl is-active --quiet liquidity-migration-account-paper-execution.service" in text
    assert "liquidity-migration-bybit-risk.service" not in text
    assert "systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service" in text
    assert "systemctl is-active --quiet liquidity-migration-liquidation-collector.service" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The exact unit set each sleeve must bring up is pinned in the shared lib, so a
    # regression that stops/disables a sleeve's daemon still fails verify.
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert (
        'LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"'
        in lib
    )
    assert 'CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"' in lib
    assert 'CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert 'CONTINUOUS_HEDGE_SERVICES="liquidity-migration-continuous-hedge.service"' in lib
    # Read-only verify must catch a missing-timer regression that the deploy
    # script would have caused — parity check, no-write semantics.
    assert "systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-liveness.timer" in text
    assert "liquidity-migration-combined-book-report" not in text
    assert "emergency Telegram mute still installed" in text
    assert "/etc/systemd/system/liquidity-migration-bybit-continuous-demo.service.d" in text
    # Continuous-fade sleeve (live on demo 2026-06-01): its daily rmom-refresh timer is
    # verified only when the sleeve is on (guarded by sleeve_on); the daemon's own
    # active+enabled state is covered by the verify_sleeve loop above.
    assert "continuous_rmom_refresh_on" in text
    assert "verify_timer on $CONTINUOUS_SLEEVE_TIMERS" in text
    assert "_verify_rmom_root" in text
    assert "ALLOW_EMPTY_RMOM_GATE" not in text
    assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in text
    assert "_hedge_timer_state" in text
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in text
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in text
    assert "canonical_strategy_trade_rows" in text
    assert 'ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT"' in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'"
        in text
    )
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'EXECUTION_ENVIRONMENT=demo'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'EXECUTION_ENVIRONMENT=paper'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'EXECUTION_ENVIRONMENT=demo'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.entry_btc_risk_low == 0.70" in text
    assert "cont.entry_btc_risk_high == 0.90" in text
    assert "cont.entry_btc_risk_tail_mult == 0.35" in text
    assert "c[0]: c[3] for c in cont.ensemble_components" in text
    assert "verify-ok commit=" in text
    assert "--property=Environment --value" in text


def test_github_vps_deploy_workflow_uses_checked_scripts_and_host_key() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch" in text
    assert "push:" in text
    assert "branches:" in text
    assert '"deploy/systemd/*.service"' in text
    assert '"deploy/systemd/**"' not in text
    assert '"scripts/**"' not in text
    # Per-sleeve kill-switch files must be in the push path filter, else flipping a
    # toggle in deploy/sleeves.env wouldn't trigger a redeploy and the sleeve would
    # never actually stop/start (the deploy sources both at runtime).
    assert '"deploy/sleeves.env"' in text
    assert '"deploy/lib_sleeves.sh"' in text
    assert '"deploy/lib_fresh_epoch.sh"' in text
    assert '"scripts/account_execution_cutover_authority.py"' in text
    assert '"scripts/check_residual_momentum_gate.py"' in text
    assert "install-preflight" in text
    assert "INSTALL_PREFLIGHT_ONLY=1" in text
    assert 'test "$GITHUB_REF_TYPE" = branch' in text
    assert 'BRANCH="$GITHUB_REF_NAME"' in text
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'install-preflight'" in text
    assert "wait-deploy" in text
    assert "wait_timeout_seconds" in text
    assert "wait_interval_seconds" in text
    assert "github.event_name == 'push' || inputs.mode == 'deploy'" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'wait-deploy'" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'verify'" in text
    assert "- candidate-ci" in text
    assert "Confirm candidate CI-only boundary" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'candidate-ci'" in text
    assert text.count("github.event_name != 'workflow_dispatch' || inputs.mode != 'candidate-ci'") == 3
    assert "VPS_SSH_PRIVATE_KEY" in text
    assert "permissions:" in text
    assert "contents: read" in text
    assert "GITHUB_TOKEN: ${{ github.token }}" in text
    assert "GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT" in text
    # Pin the CI deploy key fingerprint so accidental rotations or tampering
    # of the workflow file get flagged. When you intentionally rotate the
    # deploy key, update this constant in lockstep with the
    # GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT line in
    # .github/workflows/vps-deploy.yml AND the public key in
    # /root/.ssh/authorized_keys on the VPS AND the VPS_SSH_PRIVATE_KEY
    # secret in GitHub.
    # 2026-06-04: rotated for the 116.202.15.128 migration (old SHA256:KpDkvlvm…).
    assert "SHA256:Gki6YjdsUksh/TozZ/55sxSwimK7T9MOf2pgWSbqFNU" in text
    assert "ssh-keygen -y -f ~/.ssh/vps_deploy_key" in text
    assert "ssh-keygen -lf ~/.ssh/vps_deploy_key.pub -E sha256" in text
    # Host key is PINNED directly (no live keyscan — GitHub runners can't reliably
    # keyscan this box); the pinned key is fail-closed against the fingerprint.
    assert 'grep -F "$VPS_ED25519_FINGERPRINT"' in text
    # VPS host key fingerprint — update in lockstep with the rebuild/migration.
    # 2026-05-25 rebuild: SHA256:zQjT3bst... → SHA256:RzhZupfx...
    # 2026-06-04 migrate to new box 116.202.15.128 (old 5.223.42.109 decommissioned for cost):
    #   SHA256:RzhZupfx... → SHA256:2Jw88AJV...
    # 2026-06-09 operator full rebuild of 116.202.15.128 (fresh Ubuntu 24.04; rescue-mode key
    #   restore performed from the research box): SHA256:2Jw88AJV... → SHA256:TJRbvgB8...
    assert "SHA256:TJRbvgB8nfhwmNDv4hM3jDkPXnRv6BGLQ3cPst2PfE4" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    # The deploy script runs this smoke subset on the VPS. If a test-only fix is
    # needed to unblock deploy, the push must trigger the workflow again.
    assert "tests/test_runtime_scripts.py" in text
    assert "tests/test_promoted_profiles.py" in text
    assert 'EXPECTED_COMMIT="$GITHUB_SHA"' in text
    assert "EXPECTED_TELEGRAM_CHAT_ID" in text


def test_vps_recovery_command_printer_embeds_exact_private_repo_commit() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "print_vps_recovery_command.sh").read_text(encoding="utf-8")

    assert "git rev-parse" in text
    assert "--recommended-only" in text
    assert "--rescue-only" in text
    assert "recommended_only" in text
    assert "rescue_only" in text
    assert "recommended_command=" in text
    assert "rescue_command=" in text
    assert "raw.githubusercontent.com" not in text
    assert "curl -fsSL" not in text
    assert 'git show "$commit_sha:$1"' in text
    assert "base64 --decode" in text
    assert "mktemp" in text
    assert "chmod 0700" in text
    assert "contains no GitHub credential" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "Wait locally for restored SSH access" in text
    assert "Hetzner Rescue SSH-key restore" in text
    assert "Recommended full Hetzner Cloud console recovery" in text
    assert "Open the Hetzner Cloud web console for 116.202.15.128" in text
    assert "enable" in text
    assert "Hetzner Rescue" in text
    assert "Strict full recovery" in text
    assert "CLEAN_DIRTY_CHECKOUT=1" in text
    assert "scripts/verify_vps_live.sh" in text

    rendered = subprocess.run(
        ["bash", str(repo / "scripts" / "print_vps_recovery_command.sh"), "--recommended-only", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    assert "raw.githubusercontent.com" not in rendered
    assert "curl" not in rendered
    encoded = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", rendered)
    assert encoded is not None
    expected = subprocess.run(
        ["git", "show", "HEAD:scripts/vps_console_recover_and_deploy.sh"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert base64.b64decode(encoded.group(1)) == expected


def test_wait_for_vps_recovery_script_waits_then_runs_checked_deploy_and_verify() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "wait_for_vps_recovery_and_deploy.sh").read_text(encoding="utf-8")

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
    text = (repo / "scripts" / "vps_restore_ssh_access.sh").read_text(encoding="utf-8")

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv" in text
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
    text = (repo / "scripts" / "vps_rescue_restore_ssh_access.sh").read_text(encoding="utf-8")

    assert "TARGET_ROOT" in text
    assert "MOUNT_ROOT" in text
    assert "is_installed_root" in text
    assert "lsblk -rpno NAME,FSTYPE,TYPE,MOUNTPOINT" in text
    assert "vgchange -ay" in text
    assert 'mount "$device" "$MOUNT_ROOT"' in text
    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv" in text
    assert 'chroot "$target_root" usermod -U root' in text
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
    text = (repo / "scripts" / "vps_console_recover_and_deploy.sh").read_text(encoding="utf-8")

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv" in text
    assert "GITHUB_ACTIONS_SSH_PUBLIC_KEY" in text
    assert "for binary in git python3 sshd" in text
    assert "apt-get install -y ca-certificates git openssh-server python3 python3-venv python3-pip" in text
    assert "CLEAN_DIRTY_CHECKOUT" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "bybit-demo.env.backup" in text
    assert 'sed -i "s/^TELEGRAM_CHAT_ID=' in text
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
    assert "git_with_optional_github_token clone" in text
    assert "git remote set-url" in text
    assert "GITHUB_TOKEN" in text
    assert "http.https://github.com/.extraheader" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    assert "--no-deps" in text
    assert "--only-binary=:all:" in text
    assert "-r requirements.lock" in text
    assert "pip install --upgrade pip" not in text
    assert 'pip install -e ".[dev]"' not in text
    # Recovery pins active configs and does not import removed strategy hubs.
    assert "liquidity_migration.volume_events" not in text
    assert "_demo_event_config" not in text
    assert "_v11a_long_native_config" in text
    assert "ContinuousDemoCycleConfig" in text
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "apply_timer_enable" in text
    assert "systemctl disable --now" in lib
    retired_unit_marker = "model050426"
    assert retired_unit_marker not in text
    # Recovery shares the same manifest installer and generic retired-unit purge.
    assert "lm_install_current_systemd_units" in text
    assert "lm_install_current_systemd_units()" in lib
    assert "RETIRED_SLEEVE_UNITS" not in lib
    assert "/etc/liquidity-migration/account-execution-capture-enabled" in text
    assert "lm_fresh_epoch_phase" in text
    assert "lm_verify_authorized_deploy_epoch" in text
    assert "account_execution_cutover_authority.py verify" not in text
    assert "/etc/liquidity-migration/account-execution.env" in text
    assert "/etc/liquidity-migration/account-paper-execution.env" in text
    assert "liquidity-migration-bybit-risk.service" not in text
    # Recovery routes sleeve enable/restart/verify through the SAME kill-switch as
    # deploy_vps_live.sh (single source of truth) — NO hardcoded per-sleeve enables that
    # could resurrect an OFF sleeve (e.g. the look-ahead-disabled continuous sleeve).
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "lm_write_resolved_sleeve_toggles" in text
    assert "lm_verify_resolved_sleeve_toggles" in text
    assert "lm_cleanup_unknown_liqmig_units" in lib
    assert "lm_verify_no_unknown_liqmig_units" in lib
    for owner in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
    ):
        assert f"systemctl enable {owner}" in text
        assert f"systemctl restart {owner}" in text
        assert f"systemctl is-active --quiet {owner}" in text
        assert f"systemctl is-enabled --quiet {owner}" in text
    assert "liquidity-migration-liquidation-collector.service" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'apply_sleeve_enable "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The continuous rmom timer + its go-live asserts are gated behind the toggle, so a
    # recovery with CONTINUOUS_SLEEVE=off cannot bring the disabled sleeve back.
    assert "continuous_rmom_refresh_on" in text
    assert "require_unit_env()" in text
    assert "systemctl cat" not in text
    assert "telegram-quiet.conf" in lib
    assert "liquidity-migration-bybit-continuous-demo.service.d" in lib
    assert "liquidity-migration-combined-book-report" not in text
    assert (
        "require_unit_env liquidity-migration-account-execution.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'" in text
    )
    assert (
        "require_unit_env liquidity-migration-account-paper-execution.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    )
    # Continuous-fade sleeve is brought up like the other target producers.
    assert "liquidity-migration-bybit-continuous-demo.service" in text
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    assert "_hedge_timer_state" in text
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in text
    assert 'apply_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'apply_timer_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in text
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in text
    assert "canonical_strategy_trade_rows" in text
    assert 'ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT"' in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ACCOUNT_EXECUTION_KERNEL_REQUIRED=1'"
        in text
    )
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-paper.service 'ACCOUNT_PAPER_KERNEL_REQUIRED=1'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'EXECUTION_ENVIRONMENT=demo'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'EXECUTION_ENVIRONMENT=paper'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    )
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'EXECUTION_ENVIRONMENT=demo'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.entry_btc_risk_low == 0.70" in text
    assert "cont.entry_btc_risk_high == 0.90" in text
    assert "cont.entry_btc_risk_tail_mult == 0.35" in text
    assert "c[0]: c[3] for c in cont.ensemble_components" in text
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment --value" in text


_LEDGER_RESET_ACTIVE_UNITS = (
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-continuous-rmom-refresh.timer",
    "liquidity-migration-continuous-hedge.timer",
    "liquidity-migration-demo-liveness.timer",
)


def _initialize_empty_git_candidate(path: Path) -> str:
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "candidate"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_reset_demo_paper_ledgers_checks_conditional_orders_explicitly() -> None:
    text = (REPO_ROOT / "scripts" / "reset_demo_paper_ledgers.sh").read_text(encoding="utf-8")

    assert 'client.get_open_orders(settle_coin="USDT")' in text
    assert 'client.get_open_orders(settle_coin="USDT", order_filter="StopOrder")' in text
    assert "orders_by_identity" in text
    assert "DemoAccountIdentity" in text
    assert "canonical_demo_account_lease_path" in text
    assert "--owner-lock" not in text
    assert "DEMO_ACCOUNT_LEASE_PATH" in text


def _ledger_reset_harness(
    tmp_path: Path,
    *,
    real_money: str = "false",
    account_guard_rc: int = 0,
    active_units: tuple[str, ...] = _LEDGER_RESET_ACTIVE_UNITS,
) -> tuple[Path, Path, dict[str, str], Path]:
    """Create deterministic systemctl/account guards around the VPS-only script."""
    (tmp_path / "liquidity_migration").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    state = tmp_path / "systemctl.state"
    state.write_text("".join(f"{unit}\n" for unit in active_units), encoding="utf-8")
    log = tmp_path / "systemctl.log"

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
cmd="$1"
shift
if [[ -n "${FAKE_SECRET_LEAK_MARKER:-}" ]] && \
   [[ -n "${TELEGRAM_BOT_TOKEN:-}" || -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  : > "$FAKE_SECRET_LEAK_MARKER"
fi
printf '%s %s\\n' "$cmd" "$*" >> "$SYSTEMCTL_LOG"
case "$cmd" in
  show)
    unit="$1"
    if [[ "$*" == *"--property=EnvironmentFiles"* ]]; then
      env_file="$FAKE_SYSTEMD_ENV_FILE"
      if [[ "${FAKE_SYSTEMD_ENV_MISMATCH_UNIT:-}" == "$unit" ]]; then
        env_file="$FAKE_SYSTEMD_ENV_MISMATCH_FILE"
      fi
      printf '%s (ignore_errors=no)\n' "$env_file"
      if [[ "$unit" == "liquidity-migration-account-execution.service" ]]; then
        printf '%s (ignore_errors=no)\n' "$FAKE_ACCOUNT_ROUTE_ENV_FILE"
      elif [[ "$unit" == "liquidity-migration-account-paper-execution.service" ]]; then
        printf '%s (ignore_errors=no)\n' "$FAKE_PAPER_ROUTE_ENV_FILE"
      fi
      if [[ "${FAKE_SYSTEMD_EXTRA_ENV_UNIT:-}" == "$unit" ]]; then
        printf '%s (ignore_errors=no)\n' "$FAKE_SYSTEMD_EXTRA_ENV_FILE"
      fi
    elif [[ "$*" == *"--property=Environment"* ]]; then
      printf '%s\n' "${FAKE_SYSTEMD_DIRECT_ENVIRONMENT:-}"
    else
      echo loaded
    fi
    ;;
  is-active)
    [[ "${1:-}" == "--quiet" ]] && shift
    grep -Fqx "$1" "$SYSTEMCTL_STATE"
    ;;
  stop)
    if [[ -n "${FAKE_CREATE_DURING_STOP:-}" && ! -e "${FAKE_CREATE_DURING_STOP_MARKER:-}" ]]; then
      mkdir -p "$(dirname "$FAKE_CREATE_DURING_STOP")"
      printf 'created-before-quiescence\n' > "$FAKE_CREATE_DURING_STOP"
      : > "$FAKE_CREATE_DURING_STOP_MARKER"
    fi
    for unit in "$@"; do
      grep -Fxv "$unit" "$SYSTEMCTL_STATE" > "$SYSTEMCTL_STATE.tmp" || true
      mv "$SYSTEMCTL_STATE.tmp" "$SYSTEMCTL_STATE"
    done
    ;;
  start)
    for unit in "$@"; do
      if [[ "${FAKE_COMPETING_DEMO_MUTATOR_AT_HANDOFF:-0}" == "1" && \
            "$unit" == "liquidity-migration-account-execution.service" ]]; then
        "$REAL_PYTHON" -c '
import fcntl
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
marker = pathlib.Path(sys.argv[2])
with path.open("a+", encoding="utf-8") as competitor:
    fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    marker.write_text("competing-lease-acquired\\n", encoding="utf-8")
    with path.open("a+", encoding="utf-8") as owner:
        try:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(73)
raise SystemExit(92)
' "$FAKE_DEMO_ACCOUNT_LEASE_PATH" "$FAKE_COMPETING_DEMO_MUTATOR_MARKER"
        rc="$?"
        [[ "$rc" == "73" ]] || exit "$rc"
        exit 1
      fi
      if [[ "${FAKE_REQUIRE_OWNER_HANDOFF_UNLOCKED:-0}" == "1" && \
            "$unit" == "liquidity-migration-account-execution.service" ]]; then
        "$REAL_PYTHON" -c '
import fcntl
import pathlib
import sys

with pathlib.Path(sys.argv[1]).open("a+", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
' "$FAKE_DEMO_ACCOUNT_LEASE_PATH" || exit 93
      fi
      if [[ -n "${FAKE_START_WAIT_FILE:-}" && \
            "${FAKE_START_WAIT_UNIT:-}" == "$unit" ]]; then
        while [[ ! -e "$FAKE_START_WAIT_FILE" ]]; do
          sleep 0.02
        done
      fi
      grep -Fqx "$unit" "$SYSTEMCTL_STATE" || echo "$unit" >> "$SYSTEMCTL_STATE"
    done
    ;;
  *)
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    # The production script runs an embedded Python demo-account flat check.
    # A fake interpreter keeps this unit test offline while preserving the
    # subprocess ordering and failure/recovery behaviour.
    python = fake_bin / "python3"
    python.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "liquidity_migration.account_reset_receipt" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == "-" && "${2:-}" == "--resolve-demo-account-lease" ]]; then
  cat >/dev/null
  printf '%s\n' "$FAKE_DEMO_ACCOUNT_LEASE_PATH"
  exit 0
fi
if [[ "${1:-}" == "-" && "${2:-}" == "--write-reset-boundary" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == "-" && "${2:-}" == "--prepare-canonical-reset" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == "-" && "${2:-}" == "--rebuild-canonical-projections" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == "-" && "$#" -ge 3 ]]; then
  exec "$REAL_PYTHON" "$@"
fi
cat >/dev/null
if [[ "${FAKE_REQUIRE_DEMO_LEASE_HELD:-0}" == "1" ]]; then
  if ! "$REAL_PYTHON" -c '
import fcntl
import pathlib
import sys

with pathlib.Path(sys.argv[1]).open("a+", encoding="utf-8") as handle:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
raise SystemExit(91)
' "$FAKE_DEMO_ACCOUNT_LEASE_PATH"; then
    echo "ERROR: canonical demo-account lease was not held during flatness" >&2
    exit 91
  fi
fi
if [[ -n "${FAKE_FLAT_ENV_PROBE:-}" ]]; then
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" || -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    printf 'secret-leaked\\n' > "$FAKE_FLAT_ENV_PROBE"
  else
    printf 'credentials-only\\n' > "$FAKE_FLAT_ENV_PROBE"
  fi
fi
if [[ "${FAKE_ACCOUNT_GUARD_RC:-0}" == "0" ]]; then
  echo "  demo-account-flat-ok positions=0 open_orders=0"
  exit 0
fi
echo "ERROR: synthetic demo account is not flat" >&2
exit "$FAKE_ACCOUNT_GUARD_RC"
""",
        encoding="utf-8",
    )
    python.chmod(0o755)

    env_file = tmp_path / "bybit-demo.env"
    env_file.write_text(
        f"DEMO=true\nREAL_MONEY={real_money}\nBYBIT_DEMO_API_KEY=fake\nBYBIT_DEMO_API_SECRET=fake\n",
        encoding="utf-8",
    )
    account_env = tmp_path / "account-execution.env"
    account_env.write_text(
        "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
        "ACCOUNT_EXECUTION_ROOT=data/bybit-account-execution\n"
        "ACCOUNT_INTENT_INBOX_ROOT=data/bybit-account-intents\n"
        "ACCOUNT_CAPTURE_ROOT=data/bybit-account-market-capture\n",
        encoding="utf-8",
    )
    paper_account_env = tmp_path / "account-paper-execution.env"
    paper_account_env.write_text(
        "ACCOUNT_PAPER_KERNEL_REQUIRED=1\n"
        "ACCOUNT_EXECUTION_ROOT=data/bybit-account-paper\n"
        "ACCOUNT_INTENT_INBOX_ROOT=data/bybit-account-paper-intents\n"
        "ACCOUNT_PAPER_CAPTURE_ROOT=data/bybit-account-paper-market-capture\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("REAL_MONEY", None)
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "SYSTEMCTL_BIN": str(systemctl),
            "SYSTEMCTL_STATE": str(state),
            "SYSTEMCTL_LOG": str(log),
            "FAKE_ACCOUNT_GUARD_RC": str(account_guard_rc),
            "FAKE_SYSTEMD_ENV_FILE": str(env_file),
            "ACCOUNT_EXECUTION_ENV_FILE": str(account_env),
            "ACCOUNT_PAPER_EXECUTION_ENV_FILE": str(paper_account_env),
            "FAKE_ACCOUNT_ROUTE_ENV_FILE": str(account_env),
            "FAKE_PAPER_ROUTE_ENV_FILE": str(paper_account_env),
            "REAL_PYTHON": sys.executable,
            "PYTHONPATH": (f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"),
            "LEDGER_RESET_LOCK_FILE": str(tmp_path / "ledger-reset.lock"),
            "LEDGER_RESET_SETTLE_SECONDS": "0",
            "FAKE_DEMO_ACCOUNT_LEASE_PATH": str(tmp_path / "canonical-demo-account.lock"),
            "FAKE_REQUIRE_DEMO_LEASE_HELD": "1",
            "FAKE_REQUIRE_OWNER_HANDOFF_UNLOCKED": "1",
        }
    )
    script = REPO_ROOT / "scripts" / "reset_demo_paper_ledgers.sh"
    return script, env_file, env, log


def test_reset_demo_paper_ledgers_is_dry_run_by_default_and_execute_is_archival(
    tmp_path: Path,
) -> None:
    import fcntl
    import tarfile

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    cycles = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_cycles"
    klines = tmp_path / "data" / "bybit-long-demo-event" / "event_demo_klines_1h"
    cache = tmp_path / "data" / "bybit-long-demo-event" / ".cache"
    reports = tmp_path / "data" / "bybit-long-demo-event" / "reports"
    account_epoch_files = (
        tmp_path / "data/bybit-account-execution/account_journal/events.jsonl",
        tmp_path / "data/bybit-account-intents/pending/request.json",
        tmp_path / "data/bybit-account-market-capture/books.jsonl",
        tmp_path / "data/bybit-account-paper/account_journal/events.jsonl",
        tmp_path / "data/bybit-account-paper-intents/pending/request.json",
        tmp_path / "data/bybit-account-paper-market-capture/books.jsonl",
    )
    for directory in (cycles, klines, cache, reports):
        directory.mkdir(parents=True)
        (directory / "part.parquet").write_bytes(b"x")
    for account_file in account_epoch_files:
        account_file.parent.mkdir(parents=True, exist_ok=True)
        account_file.write_text("old-account-epoch\n", encoding="utf-8")
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "long-reset-row",
                    "strategy_id": "long_native_v11a_div_weekend_vol",
                    "symbol": "AAAUSDT",
                    "side": "long",
                    "status": "open",
                    "entry_ts_ms": 1_700_000_000_000,
                    "entry_price": 10.0,
                    "qty": "1",
                }
            ]
        ),
        tmp_path / "data" / "bybit-long-demo-event",
        "long_native_demo_trades",
        partition_by=(),
    )

    preview = subprocess.run(
        ["bash", str(script), "--sleeves", "long"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert preview.returncode == 0, preview.stderr
    assert "mode: dry-run" in preview.stdout.lower()
    assert "no services or files were changed" in preview.stdout
    assert ledger.exists() and cycles.exists() and klines.exists()
    assert cache.exists() and reports.exists()
    assert all(path.exists() for path in account_epoch_files)
    assert not log.exists(), "dry-run must not even query or stop systemd units"
    demo_lease_path = Path(env["FAKE_DEMO_ACCOUNT_LEASE_PATH"])
    assert not demo_lease_path.exists(), "dry-run must not query identity or open its lease"
    assert not (tmp_path / "data" / "_archive").exists()

    # systemd may expose the canonical path while an operator supplies a safe
    # symlink alias. The account-binding guard compares resolved paths.
    env_alias = tmp_path / "bybit-demo-alias.env"
    env_alias.symlink_to(env_file)

    executed = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "long",
            "--env-file",
            str(env_alias),
            "--label",
            "exit-overhaul",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert executed.returncode == 0, executed.stderr
    assert ledger.exists() and not cycles.exists()
    rebuilt = read_dataset(ledger.parent, "long_native_demo_trades")
    assert rebuilt.select("status").item() == "awaiting_pnl"
    assert (ledger.parent / "canonical_journal" / "events.jsonl").exists()
    assert klines.exists(), "root-level market data must be preserved"
    assert cache.exists(), "cache removal requires --include-caches"
    assert reports.exists(), "report removal requires --include-reports"
    assert all(not path.exists() for path in account_epoch_files)
    for path in account_epoch_files:
        # Each configured account root/inbox/capture is recreated, but old
        # journal/request/capture payloads cannot leak into the new epoch.
        assert path.parents[1].exists() or path.parent.exists()
    archives = list((tmp_path / "data" / "_archive").glob("ledger-reset-*-exit-overhaul.tar.gz"))
    assert len(archives) == 1
    digest = archives[0].with_name(archives[0].name + ".sha256")
    assert digest.exists()
    assert archives[0].name in digest.read_text(encoding="utf-8")
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
    assert "ledger-reset-manifest.txt" in names
    assert any(name.startswith("data/bybit-long-demo-event/long_native_demo_trades") for name in names)
    for account_file in account_epoch_files:
        assert str(account_file.relative_to(tmp_path)) in names

    systemctl_log = log.read_text(encoding="utf-8")
    assert "stop liquidity-migration-bybit-long-demo.service" in systemctl_log
    assert "stop liquidity-migration-bybit-continuous-demo.service" in systemctl_log
    assert "stop liquidity-migration-account-execution.service" in systemctl_log
    assert "stop liquidity-migration-account-paper-execution.service" in systemctl_log
    assert "start liquidity-migration-account-execution.service" in systemctl_log
    assert "start liquidity-migration-account-paper-execution.service" in systemctl_log
    assert "is-active --quiet liquidity-migration-account-execution.service" in systemctl_log
    assert "liquidity-migration-bybit-risk.service" not in systemctl_log
    assert "liquidity-migration-combined-book-report" not in systemctl_log
    systemctl_lines = systemctl_log.splitlines()
    assert systemctl_lines.index("stop liquidity-migration-bybit-long-demo.service") < systemctl_lines.index(
        "stop liquidity-migration-account-execution.service"
    )
    assert systemctl_lines.index("stop liquidity-migration-bybit-continuous-paper.service") < systemctl_lines.index(
        "stop liquidity-migration-account-paper-execution.service"
    )
    assert systemctl_lines.index("start liquidity-migration-account-execution.service") < systemctl_lines.index(
        "start liquidity-migration-bybit-long-demo.service"
    )
    assert systemctl_lines.index("start liquidity-migration-account-paper-execution.service") < systemctl_lines.index(
        "start liquidity-migration-bybit-long-paper.service"
    )
    assert systemctl_lines.index("start liquidity-migration-bybit-continuous-demo.service") < systemctl_lines.index(
        "start liquidity-migration-continuous-hedge.timer"
    )
    assert "service state: restored" in executed.stdout
    assert "canonical demo-account lease acquired for flatness/archive/reset" in executed.stdout
    assert "canonical demo-account lease released for owner-first restart handoff" in executed.stdout
    assert executed.stdout.index(
        "canonical demo-account lease acquired for flatness/archive/reset"
    ) < executed.stdout.index("demo-account-flat-ok")
    assert executed.stdout.index(
        "canonical demo-account lease released for owner-first restart handoff"
    ) < executed.stdout.index("Restarting previously-active account owners first")
    with demo_lease_path.open("a+", encoding="utf-8") as released_lease:
        fcntl.flock(released_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_reset_demo_paper_ledgers_reinventories_state_after_quiescence(
    tmp_path: Path,
) -> None:
    import tarfile

    script, env_file, env, _log = _ledger_reset_harness(tmp_path)
    old_account_event = tmp_path / "data/bybit-account-execution/old-event.jsonl"
    old_account_event.parent.mkdir(parents=True)
    old_account_event.write_text("old-account-epoch\n", encoding="utf-8")
    raced_request = tmp_path / "data/bybit-account-intents/pending/raced.json"
    env["FAKE_CREATE_DURING_STOP"] = str(raced_request)
    env["FAKE_CREATE_DURING_STOP_MARKER"] = str(tmp_path / "race-created")

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "long",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert not raced_request.exists(), "pre-quiescence request must not enter the new epoch"
    archives = list((tmp_path / "data/_archive").glob("ledger-reset-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        assert str(raced_request.relative_to(tmp_path)) in archive.getnames()


@pytest.mark.parametrize("via_symlink", [False, True])
def test_reset_demo_paper_ledgers_refuses_archive_inside_reset_target_after_canonicalization(
    tmp_path: Path,
    via_symlink: bool,
) -> None:
    script, _env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    if via_symlink:
        alias = tmp_path / "archive-alias"
        alias.symlink_to(ledger, target_is_directory=True)
        archive_dir = alias / "_archive"
    else:
        archive_dir = Path("data/bybit-long-demo-event/./long_native_demo_trades/_archive")

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--sleeves",
            "long",
            "--archive-dir",
            str(archive_dir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "--archive-dir must be outside reset targets" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "containment refusal must precede all systemd access"


def test_reset_demo_paper_ledgers_refuses_archive_inside_preserved_canonical_journal(
    tmp_path: Path,
) -> None:
    script, _env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    archive_dir = Path("data/bybit-long-demo-event/canonical_journal/operator-archive")

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--sleeves",
            "long",
            "--archive-dir",
            str(archive_dir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "--archive-dir must be outside reset targets" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "containment refusal must precede all systemd access"


@pytest.mark.parametrize("overlap_kind", ["strategy", "account"])
def test_reset_demo_paper_ledgers_refuses_overlapping_account_routes(
    tmp_path: Path,
    overlap_kind: str,
) -> None:
    script, _env_file, env, log = _ledger_reset_harness(tmp_path)
    account_env = Path(env["ACCOUNT_EXECUTION_ENV_FILE"])
    if overlap_kind == "strategy":
        account_root = "data/bybit-long-demo-event"
        inbox_root = "data/bybit-account-intents"
    else:
        account_root = "data/bybit-account-execution"
        inbox_root = "data/bybit-account-execution/inbox"
    account_env.write_text(
        "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
        f"ACCOUNT_EXECUTION_ROOT={account_root}\n"
        f"ACCOUNT_INTENT_INBOX_ROOT={inbox_root}\n"
        "ACCOUNT_CAPTURE_ROOT=data/bybit-account-market-capture\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        ["bash", str(script), "--sleeves", "long"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "account execution roots must be pairwise disjoint" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "route-layout refusal must precede all systemd access"


def test_reset_demo_paper_ledgers_reads_route_files_as_data_not_shell_code(
    tmp_path: Path,
) -> None:
    script, _env_file, env, log = _ledger_reset_harness(tmp_path)
    account_env = Path(env["ACCOUNT_EXECUTION_ENV_FILE"])
    with account_env.open("a", encoding="utf-8") as handle:
        handle.write("MODE=execute\nARCHIVE_DIR=/should-not-be-used\n")
    ledger = tmp_path / "data/bybit-long-demo-event/long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        ["bash", str(script), "--sleeves", "long"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "mode: dry-run" in result.stdout.lower()
    assert "archive dir: data/_archive" in result.stdout
    assert not log.exists()


def test_reset_demo_paper_ledgers_scopes_credential_and_telegram_environment(
    tmp_path: Path,
) -> None:
    script, env_file, env, _log = _ledger_reset_harness(tmp_path)
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write("TELEGRAM_BOT_TOKEN=must-not-leak\nTELEGRAM_CHAT_ID=must-not-leak\n")
    old_account_event = tmp_path / "data/bybit-account-execution/old-event.jsonl"
    old_account_event.parent.mkdir(parents=True)
    old_account_event.write_text("old-account-epoch\n", encoding="utf-8")
    flat_probe = tmp_path / "flat-env-probe"
    systemctl_leak = tmp_path / "systemctl-secret-leak"
    env["FAKE_FLAT_ENV_PROBE"] = str(flat_probe)
    env["FAKE_SECRET_LEAK_MARKER"] = str(systemctl_leak)

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "long",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert flat_probe.read_text(encoding="utf-8") == "credentials-only\n"
    assert not systemctl_leak.exists()


def test_reset_demo_paper_ledgers_continuous_selection_includes_hedge_and_cache_is_opt_in(
    tmp_path: Path,
) -> None:
    import tarfile

    script, env_file, env, _ = _ledger_reset_harness(tmp_path)
    long_ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    continuous_ledger = tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_fade_demo_trades"
    paper_ledger = tmp_path / "data" / "bybit-continuous-paper-event" / "continuous_fade_paper_trades"
    hedge_ledger = tmp_path / "data" / "bybit-continuous-hedge-event" / "continuous_fade_demo_trades"
    cache = tmp_path / "data" / "bybit-continuous-demo-event" / ".cache"
    demo_equity_state = tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_account_equity_state.json"
    paper_equity_state = tmp_path / "data" / "bybit-continuous-paper-event" / "continuous_account_equity_state.json"
    demo_risk_events = continuous_ledger.parent / "continuous_risk_events.jsonl"
    demo_lifecycle_events = continuous_ledger.parent / "continuous_lifecycle_events.jsonl"
    paper_risk_events = paper_ledger.parent / "continuous_risk_events.jsonl"
    paper_lifecycle_events = paper_ledger.parent / "continuous_lifecycle_events.jsonl"
    long_ledger.mkdir(parents=True)
    (long_ledger / "part.parquet").write_bytes(b"x")
    cache.mkdir(parents=True)
    (cache / "part.parquet").write_bytes(b"x")
    for root, dataset, trade_id in (
        (continuous_ledger.parent, "continuous_fade_demo_trades", "continuous-demo-reset"),
        (paper_ledger.parent, "continuous_fade_paper_trades", "continuous-paper-reset"),
        (hedge_ledger.parent, "continuous_fade_demo_trades", "continuous-hedge-reset"),
    ):
        write_dataset(
            pl.DataFrame(
                [
                    {
                        "trade_id": trade_id,
                        "strategy_id": "continuous_fade_v2",
                        "symbol": "BUSDT",
                        "side": "short",
                        "status": "open",
                        "entry_ts_ms": 1_700_000_000_000,
                        "entry_price": 0.125,
                        "qty": "10",
                    }
                ]
            ),
            root,
            dataset,
            partition_by=(),
        )
    for events in (
        demo_risk_events,
        demo_lifecycle_events,
        paper_risk_events,
        paper_lifecycle_events,
    ):
        events.write_text('{"event":"old-forward-window"}\n', encoding="utf-8")
    for state in (demo_equity_state, paper_equity_state):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"high_water_usdt":10039.68}', encoding="utf-8")

    executed = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "continuous",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert executed.returncode == 0, executed.stderr
    assert long_ledger.exists(), "continuous-only reset must preserve long ledgers"
    assert continuous_ledger.exists() and paper_ledger.exists()
    assert not hedge_ledger.parent.exists(), "retired hedge compatibility root must not survive account-owner cutover"
    assert (
        read_dataset(continuous_ledger.parent, "continuous_fade_demo_trades").select("status").item() == "awaiting_pnl"
    )
    paper_reset_row = read_dataset(
        paper_ledger.parent,
        "continuous_fade_paper_trades",
    ).to_dicts()[0]
    assert paper_reset_row["status"] == "archived"
    assert paper_reset_row["canonical_reconciliation_state"] == "paper_epoch_archived"
    assert "canonical_flat_verified_at_ms" not in paper_reset_row
    assert not demo_risk_events.exists() and not demo_lifecycle_events.exists()
    assert not paper_risk_events.exists() and not paper_lifecycle_events.exists(), (
        "old operational failures must not contaminate the post-reset forward window"
    )
    assert "reset-boundary-heartbeats-ok" in executed.stdout
    demo_boundary = read_dataset(
        continuous_ledger.parent,
        "continuous_fade_demo_cycles",
    )
    paper_boundary = read_dataset(
        paper_ledger.parent,
        "continuous_fade_paper_cycles",
    )
    assert demo_boundary.height == 1 and paper_boundary.height == 1
    assert demo_boundary.select("reason").item() == "verified_flat_ledger_reset"
    assert demo_boundary.select("account_flat_verified").item() is True
    assert paper_boundary.select("reason").item() == "archived_paper_epoch_reset"
    assert paper_boundary.select("account_flat_verified").item() is False
    assert paper_boundary.select("paper_epoch_archived").item() is True
    assert cache.exists(), "cache is preserved unless explicitly selected"
    assert demo_equity_state.exists() and paper_equity_state.exists(), (
        "a ledger reset must not erase the account drawdown high-water risk memory"
    )
    archives = list((tmp_path / "data" / "_archive").glob("ledger-reset-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
        manifest = archive.extractfile("ledger-reset-manifest.txt")
        assert manifest is not None
        manifest_text = manifest.read().decode("utf-8")
    assert str(demo_equity_state.relative_to(tmp_path)) in names
    assert str(paper_equity_state.relative_to(tmp_path)) in names
    assert str(demo_risk_events.relative_to(tmp_path)) in names
    assert str(paper_lifecycle_events.relative_to(tmp_path)) in names
    assert f"preserved_risk_state={demo_equity_state.relative_to(tmp_path)}" in manifest_text
    assert "demo_boundary=venue_verified_flat_positions_0_open_orders_0" in manifest_text
    assert "paper_boundary=archived_deterministic_epoch_not_carried_forward" in manifest_text

    cache_reset = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "continuous",
            "--include-caches",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cache_reset.returncode == 0, cache_reset.stderr
    assert not cache.exists()
    assert demo_equity_state.exists() and paper_equity_state.exists()


def test_reset_demo_paper_ledgers_all_archives_and_removes_shared_compatibility_root(
    tmp_path: Path,
) -> None:
    script, env_file, env, _ = _ledger_reset_harness(tmp_path)
    shared_trade = tmp_path / "data" / "bybit-demo-event" / "event_demo_trades"
    shared_order = tmp_path / "data" / "bybit-demo-event" / "event_demo_orders"
    retired_artifact = tmp_path / "data" / "bybit-demo-event" / "retired-artifact"
    retired_artifact.mkdir(parents=True)
    (retired_artifact / "part.parquet").write_bytes(b"x")
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "shared-reset",
                    "strategy_id": "compatibility_short",
                    "symbol": "BUSDT",
                    "side": "short",
                    "status": "open",
                    "entry_ts_ms": 1_700_000_000_000,
                    "entry_price": 0.125,
                    "qty": "10",
                }
            ]
        ),
        shared_trade.parent,
        "event_demo_trades",
        partition_by=(),
    )

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "all", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not shared_trade.parent.exists()
    assert not shared_order.exists()
    assert not retired_artifact.exists()
    assert "retire-shared-compat" in result.stdout


def test_reset_demo_paper_ledgers_refuses_real_money_before_service_mutation(tmp_path: Path) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path, real_money="true")
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "REAL_MONEY='true'" in result.stderr
    assert "demo/paper only" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "mainnet refusal must happen before any systemd mutation"


def test_reset_demo_paper_ledgers_refuses_concurrent_execute_before_systemd_query(
    tmp_path: Path,
) -> None:
    import fcntl

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    lock_path = Path(env["LEDGER_RESET_LOCK_FILE"])
    with lock_path.open("w", encoding="utf-8") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--execute",
                "--sleeves",
                "long",
                "--env-file",
                str(env_file),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode != 0
    assert "another demo/paper ledger reset is already executing" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "lock contention must refuse before querying or mutating systemd"


def test_reset_demo_paper_ledgers_canonical_lease_contention_refuses_before_flatness(
    tmp_path: Path,
) -> None:
    import fcntl

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    flatness_probe = tmp_path / "flatness-reached"
    env["FAKE_FLAT_ENV_PROBE"] = str(flatness_probe)

    lease_path = Path(env["FAKE_DEMO_ACCOUNT_LEASE_PATH"])
    with lease_path.open("a+", encoding="utf-8") as competing_mutator:
        fcntl.flock(competing_mutator.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--execute",
                "--sleeves",
                "long",
                "--env-file",
                str(env_file),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode != 0
    assert "canonical demo-account lease is already held" in result.stderr
    assert ledger.exists(), "lease contention must refuse before archive/removal"
    assert not flatness_probe.exists(), "flatness must run under the canonical lease"
    assert not (tmp_path / "data" / "_archive").exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert "stop liquidity-migration-account-execution.service" in systemctl_log
    assert "start liquidity-migration-account-execution.service" in systemctl_log


def test_reset_demo_paper_ledgers_leave_stopped_creates_all_six_fresh_roots(
    tmp_path: Path,
) -> None:
    import tarfile

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--leave-stopped",
            "--sleeves",
            "long",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    roots = (
        tmp_path / "data/bybit-account-execution",
        tmp_path / "data/bybit-account-intents",
        tmp_path / "data/bybit-account-market-capture",
        tmp_path / "data/bybit-account-paper",
        tmp_path / "data/bybit-account-paper-intents",
        tmp_path / "data/bybit-account-paper-market-capture",
    )
    assert all(root.is_dir() for root in roots)
    archives = list((tmp_path / "data/_archive").glob("ledger-reset-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        manifest = archive.extractfile("ledger-reset-manifest.txt")
        assert manifest is not None
        assert "leave_stopped=1" in manifest.read().decode("utf-8")
    systemctl_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("start ") for line in systemctl_lines)
    assert "all managed units stopped and verified" in result.stdout


def test_reset_demo_paper_ledgers_archives_every_strategy_clock_and_natural_capture_prefix(
    tmp_path: Path,
) -> None:
    import tarfile

    script, env_file, env, _log = _ledger_reset_harness(tmp_path)
    relative_tapes = (
        "data/bybit-long-demo-event/strategy_event_tape.jsonl",
        "data/bybit-long-demo-event/strategy_event_decision_tape.jsonl",
        "data/bybit-long-demo-event/strategy_target_scheduling_capture.jsonl",
        "data/bybit-long-demo-event/natural-effective-runtime-config.json",
        "data/bybit-long-paper-event/strategy_event_tape.jsonl",
        "data/bybit-long-paper-event/strategy_event_decision_tape.jsonl",
        "data/bybit-long-paper-event/strategy_target_scheduling_capture.jsonl",
        "data/bybit-continuous-demo-event/strategy_event_tape.jsonl",
        "data/bybit-continuous-demo-event/strategy_event_decision_tape.jsonl",
        "data/bybit-continuous-demo-event/strategy_target_scheduling_capture.jsonl",
        "data/bybit-continuous-demo-event/natural-effective-runtime-config.json",
        "data/bybit-continuous-paper-event/strategy_event_tape.jsonl",
        "data/bybit-continuous-paper-event/strategy_event_decision_tape.jsonl",
        "data/bybit-continuous-paper-event/strategy_target_scheduling_capture.jsonl",
        "data/bybit-natural-account-cutover/strategy_target_scheduling_capture.jsonl",
        "data/bybit-natural-account-cutover/natural-run-config.json",
        "data/bybit-natural-account-cutover/effective-runtime-config-bundle.json",
    )
    for relative in relative_tapes:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old-prefix\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--leave-stopped",
            "--sleeves",
            "all",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert all(not (tmp_path / relative).exists() for relative in relative_tapes)
    archives = list((tmp_path / "data/_archive").glob("ledger-reset-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        archived = set(archive.getnames())
    assert set(relative_tapes) <= archived

    # The old hash-chain/sequence prefix is gone. The next daemon-created
    # event therefore begins a new epoch at source_sequence=1.
    from liquidity_migration.strategy_event_clock import (
        JsonlStrategyEventTape,
        StrategyEvent,
    )

    fresh = tmp_path / "data/bybit-long-demo-event/strategy_event_tape.jsonl"
    JsonlStrategyEventTape(fresh).append(
        StrategyEvent(
            event_ts_ns=1,
            ingest_ts_ns=1,
            source="long:demo",
            source_sequence=1,
            kind="startup",
        )
    )
    assert JsonlStrategyEventTape(fresh).prior_events[0].source_sequence == 1


def test_reset_demo_paper_ledgers_writes_source_reopening_success_receipt(
    tmp_path: Path,
) -> None:
    from liquidity_migration.account_reset_receipt import load_account_reset_receipt

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    candidate = _initialize_empty_git_candidate(tmp_path)
    receipt = tmp_path / "reset-success.json"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--leave-stopped",
            "--sleeves",
            "long",
            "--env-file",
            str(env_file),
            "--receipt",
            str(receipt),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert (receipt.stat().st_mode & 0o777) == 0o600
    payload = load_account_reset_receipt(
        receipt,
        expected_candidate_commit=candidate,
        require_leave_stopped=True,
        require_fresh_roots=True,
    )
    assert payload["status"] == "passed"
    assert payload["reset"]["sleeves"] == ["long"]
    assert payload["services"]["all_managed_units_stopped_verified"] is True
    assert payload["archive"]["file"]["sha256"] in result.stdout
    assert "Ledger reset complete." in result.stdout
    assert "structured reset receipt" in result.stdout
    assert not any(line.startswith("start ") for line in log.read_text(encoding="utf-8").splitlines())


def test_reset_demo_paper_ledgers_failure_never_writes_success_receipt(
    tmp_path: Path,
) -> None:
    script, env_file, env, _log = _ledger_reset_harness(tmp_path, account_guard_rc=7)
    _initialize_empty_git_candidate(tmp_path)
    receipt = tmp_path / "reset-success.json"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--leave-stopped",
            "--sleeves",
            "long",
            "--env-file",
            str(env_file),
            "--receipt",
            str(receipt),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "synthetic demo account is not flat" in result.stderr
    assert not receipt.exists()


def test_reset_demo_paper_ledgers_competing_handoff_keeps_producers_stopped(
    tmp_path: Path,
) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    pending = tmp_path / "data/bybit-account-intents/pending/request.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("old-account-epoch\n", encoding="utf-8")
    marker = tmp_path / "competing-demo-mutation"
    env["FAKE_COMPETING_DEMO_MUTATOR_AT_HANDOFF"] = "1"
    env["FAKE_COMPETING_DEMO_MUTATOR_MARKER"] = str(marker)

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "long",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "competing-lease-acquired\n"
    assert "owner start failed; downstream producers remain stopped" in result.stderr
    assert "refusing an automatic retry" in result.stderr
    systemctl_lines = log.read_text(encoding="utf-8").splitlines()
    assert systemctl_lines.count("start liquidity-migration-account-execution.service") == 1
    assert not any(line.startswith("start liquidity-migration-bybit-") for line in systemctl_lines)


def test_reset_demo_paper_ledgers_refuses_systemd_env_mismatch_before_service_mutation(
    tmp_path: Path,
) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    other_env = tmp_path / "different-demo-account.env"
    other_env.write_text(
        "DEMO=true\nREAL_MONEY=false\nBYBIT_DEMO_API_KEY=other\nBYBIT_DEMO_API_SECRET=other\n",
        encoding="utf-8",
    )
    mismatch_unit = "liquidity-migration-account-execution.service"
    env["FAKE_SYSTEMD_ENV_MISMATCH_UNIT"] = mismatch_unit
    env["FAKE_SYSTEMD_ENV_MISMATCH_FILE"] = str(other_env)

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert mismatch_unit in result.stderr
    assert "ambiguous credential environment" in result.stderr
    assert "refusing before stopping services" in result.stderr
    assert ledger.exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert f"show {mismatch_unit} --property=EnvironmentFiles --value" in systemctl_log
    assert not any(line.startswith(("stop ", "start ")) for line in systemctl_log.splitlines()), (
        "credential-file mismatch must refuse before service mutation"
    )


@pytest.mark.parametrize("override_kind", ["later_file", "direct"])
def test_reset_demo_paper_ledgers_refuses_later_credential_override(
    tmp_path: Path,
    override_kind: str,
) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    unit = "liquidity-migration-account-execution.service"
    if override_kind == "later_file":
        override = tmp_path / "later-credentials.env"
        override.write_text(
            "BYBIT_DEMO_API_KEY=different\nREAL_MONEY=false\n",
            encoding="utf-8",
        )
        env["FAKE_SYSTEMD_EXTRA_ENV_UNIT"] = unit
        env["FAKE_SYSTEMD_EXTRA_ENV_FILE"] = str(override)
    else:
        env["FAKE_SYSTEMD_DIRECT_ENVIRONMENT"] = "BYBIT_DEMO_API_KEY=different"

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "ambiguous credential environment" in result.stderr
    assert ledger.exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert not any(line.startswith(("stop ", "start ")) for line in systemctl_log.splitlines())


@pytest.mark.parametrize(
    ("unit", "key"),
    [
        ("liquidity-migration-account-execution.service", "ACCOUNT_CAPTURE_ROOT"),
        (
            "liquidity-migration-account-paper-execution.service",
            "ACCOUNT_PAPER_CAPTURE_ROOT",
        ),
    ],
)
def test_reset_demo_paper_ledgers_refuses_owner_route_override(
    tmp_path: Path,
    unit: str,
    key: str,
) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    override = tmp_path / "later-owner-route.env"
    override.write_text(f"{key}=data/wrong-owner-route\n", encoding="utf-8")
    env["FAKE_SYSTEMD_EXTRA_ENV_UNIT"] = unit
    env["FAKE_SYSTEMD_EXTRA_ENV_FILE"] = str(override)

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "ambiguous route environment" in result.stderr
    assert ledger.exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert not any(line.startswith(("stop ", "start ")) for line in systemctl_log.splitlines())


def test_reset_demo_paper_ledgers_lock_stays_held_during_failure_recovery_restart(
    tmp_path: Path,
) -> None:
    import time

    owner_unit = "liquidity-migration-account-execution.service"
    script, env_file, env, log = _ledger_reset_harness(
        tmp_path,
        account_guard_rc=7,
        active_units=(owner_unit,),
    )
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    release_restart = tmp_path / "release-restart"
    env["FAKE_START_WAIT_FILE"] = str(release_restart)
    env["FAKE_START_WAIT_UNIT"] = owner_unit

    first = subprocess.Popen(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and f"start {owner_unit}" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("first reset never reached its failure-recovery restart")

        overlapping = subprocess.run(
            [
                "bash",
                str(script),
                "--execute",
                "--sleeves",
                "long",
                "--env-file",
                str(env_file),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert overlapping.returncode != 0
        assert "another demo/paper ledger reset is already executing" in overlapping.stderr
    finally:
        release_restart.touch()
        first_stdout, first_stderr = first.communicate(timeout=10)

    assert first.returncode != 0, first_stdout
    assert "synthetic demo account is not flat" in first_stderr
    assert ledger.exists()


def test_reset_demo_paper_ledgers_flat_check_failure_restores_services_without_deleting(
    tmp_path: Path,
) -> None:
    active = (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-continuous-demo.service",
    )
    script, env_file, env, log = _ledger_reset_harness(tmp_path, account_guard_rc=7, active_units=active)
    ledger = tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_fade_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "continuous",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "synthetic demo account is not flat" in result.stderr
    assert ledger.exists(), "flat-account guard must run before archive/removal"
    assert not (tmp_path / "data" / "_archive").exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert "stop liquidity-migration-account-execution.service" in systemctl_log
    assert "start liquidity-migration-account-execution.service" in systemctl_log
    assert "start liquidity-migration-account-paper-execution.service" in systemctl_log
    assert "start liquidity-migration-bybit-continuous-demo.service" in systemctl_log


def test_unit_execstart_args_parse_against_their_script_parsers() -> None:
    """THE class-test for the 2026-06-11 demo-liveness crash-loop: the unit kept
    passing --data-root after the purge dropped that argparse argument, every
    string-presence unit test stayed green, and only the VPS journal noticed the
    watchdog dying every 3 minutes. For every unit whose ExecStart invokes a repo
    python script or module with flags, parse the unit's actual argv against the
    target's actual parser — argv↔argparse drift fails HERE, not on the box.
    The authorization wrapper is transparent; env-driven workload wrappers are
    still out of scope."""
    import shlex

    repo = Path(__file__).resolve().parents[1]

    def _execstart_tokens(unit_text: str) -> list[str]:
        lines = unit_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("ExecStart="):
                block = [line[len("ExecStart=") :]]
                while block[-1].rstrip().endswith("\\"):
                    block[-1] = block[-1].rstrip()[:-1]
                    i += 1
                    block.append(lines[i])
                return shlex.split(" ".join(block))
        return []

    import importlib.util
    import sys as _sys

    def _script_parser(script: str):
        spec = importlib.util.spec_from_file_location(f"_parity_{Path(script).stem}", repo / script)
        module = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    checked = 0
    for unit_path in sorted((repo / "deploy" / "systemd").glob("*.service")):
        tokens = _execstart_tokens(unit_path.read_text(encoding="utf-8"))
        assert tokens, f"{unit_path.name}: no ExecStart found"
        if tokens[0].endswith("/run_authorized_fresh_runtime.sh"):
            assert len(tokens) == 3, f"{unit_path.name}: authorization wrapper accepts caller argv"
            assert tokens[1] == unit_path.name
            assert tokens[2] == "main"
            continue
        # locate the target: a scripts/*.py path, a -m module, or a wrapper (skip)
        argv: list[str] | None = None
        parse = None
        for idx, tok in enumerate(tokens):
            if tok.endswith(".sh"):
                break  # env-driven wrapper
            if tok.endswith(".py"):
                script_rel = tok.removeprefix("/opt/liquidity-migration/")
                mod = _script_parser(script_rel)
                argv = tokens[idx + 1 :]

                def parse(a, m=mod):
                    fn = m.build_arg_parser().parse_args if hasattr(m, "build_arg_parser") else m.parse_args
                    return fn(a)

                break
            if tok == "-m":
                target = tokens[idx + 1]
                argv = tokens[idx + 2 :]
                if target == "liquidity_migration":
                    from liquidity_migration.cli import build_parser

                    def parse(a, _build=build_parser):
                        return _build().parse_args(a)
                else:
                    module = __import__(target, fromlist=["build_arg_parser"])

                    def parse(a, m=module):
                        return m.build_arg_parser().parse_args(a)

                break
        if parse is None or argv is None:
            continue
        try:
            parse(argv)
        except SystemExit as exc:
            raise AssertionError(
                f"{unit_path.name}: ExecStart args do not parse against the target's argparse (exit {exc.code}): {argv}"
            ) from exc
        checked += 1
    # the units this test exists for must actually be covered
    assert checked >= 2, f"expected at least 2 direct argv-driven units, checked {checked}"


def test_vps_deploy_paths_filter_covers_runtime_and_authority_dependencies() -> None:
    """Round 4: the workflow paths-filter class bit twice (configs/ in round 2,
    hedge warmstart CSVs in round 3) because the filter is hand-listed with no
    structural guard. Derive the required entries from the units themselves —
    every repo-relative script referenced by a unit (ExecStart + continuation
    lines), every scripts/* file a run_*.sh wrapper invokes, and every helper
    sourced or invoked by deploy/verify/recovery must be in the workflow paths
    filter, else a change to it deploys NOTHING."""
    import re

    repo = Path(__file__).resolve().parents[1]
    workflow = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    push_block = workflow.split("  push:\n", 1)[1].split("  workflow_dispatch:\n", 1)[0]
    registered_paths = {
        stripped[2:].strip('"') for line in push_block.splitlines() if (stripped := line.strip()).startswith('- "')
    }
    script_ref = re.compile(r"(?:/opt/liquidity-migration/)?(scripts/[A-Za-z0-9_./-]+\.(?:py|sh))")
    deploy_ref = re.compile(r"(deploy/[A-Za-z0-9_./-]+\.sh)")

    required: set[str] = set()
    for unit in sorted((repo / "deploy" / "systemd").glob("liquidity-migration-*.service")):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in script_ref.findall(line):
                if (repo / match).exists():
                    required.add(match)
    for wrapper in sorted((repo / "scripts").glob("run_*.sh")):
        for line in wrapper.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in script_ref.findall(line):
                if (repo / match).exists():
                    required.add(match)
    for entrypoint in (
        "scripts/deploy_vps_live.sh",
        "scripts/verify_vps_live.sh",
        "scripts/vps_console_recover_and_deploy.sh",
    ):
        required.add(entrypoint)
        for line in (repo / entrypoint).read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in (*script_ref.findall(line), *deploy_ref.findall(line)):
                if (repo / match).exists():
                    required.add(match)
    # Runtime data/config files units read at startup or on every timer run.
    required |= {"deploy/sleeves.env", "deploy/lib_sleeves.sh", "configs/volume_alpha.default.yaml"}

    missing = sorted(required - registered_paths)
    assert not missing, f"vps-deploy.yml paths filter is missing runtime-invoked files: {missing}"
    # Globbed entries the derivation above can't see.
    assert '"deploy/systemd/*.service"' in workflow
    assert '"deploy/systemd/*.timer"' in workflow
    # The armed hedge reads these CSVs every run; the operator-pending
    # warmstart-refresh commit deploys ONLY if this entry stays (round 3/4).
    assert '"deploy/hedge_warmstart/*.csv"' in workflow


def test_vps_deploy_workflow_has_full_suite_ci_gate() -> None:
    """deploy-ci-2 (folded from test_audit_fix_b05.py): the deploy workflow must run a
    server-side ruff + full-pytest CI job, and the deploy job must depend on it — so an
    uninstalled local pre-push hook, a --no-verify, or a GitHub web edit can no longer
    auto-deploy untested code."""
    repo = Path(__file__).resolve().parents[1]
    wf = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")

    # A dedicated CI job running the FULL gate (ruff over all three trees + pytest -q).
    assert "ruff check liquidity_migration tests scripts" in wf
    assert "pytest -q" in wf
    # The deploy job gates on it.
    assert "needs: ci" in wf
    # CI runs on PRs too (the deploy steps stay push/dispatch-guarded).
    assert "pull_request:" in wf
    # The deploy job must not touch the box on a PR.
    assert "github.event_name != 'pull_request'" in wf


def test_pre_push_hook_keeps_pytest_basetemp_outside_repository(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    hook = repo / "pre-push"
    hook.write_bytes((REPO_ROOT / "scripts" / "git-hooks" / "pre-push").read_bytes())
    hook.chmod(0o755)

    argv_log = tmp_path / "python-argv.jsonl"
    python_stub = repo / ".venv" / "bin" / "python"
    python_stub.parent.mkdir(parents=True)
    python_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['PREPUSH_ARGV_LOG'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)
    external_tmp = tmp_path / "external-tmp"
    external_tmp.mkdir()
    env = {
        **os.environ,
        "PREPUSH_ARGV_LOG": str(argv_log),
        "TMPDIR": str(external_tmp),
    }

    subprocess.run([str(hook)], cwd=repo, env=env, check=True)
    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert len(calls) == 2
    pytest_argv = calls[1]
    basetemp = Path(pytest_argv[pytest_argv.index("--basetemp") + 1]).resolve()
    assert not basetemp.is_relative_to(repo.resolve())
    assert basetemp.parent == external_tmp.resolve()

    rejected = subprocess.run(
        [str(hook)],
        cwd=repo,
        env={**env, "PYTEST_BASETEMP": str(repo / ".git" / "pytest")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "refusing pytest basetemp inside repository" in rejected.stderr


def _unit_environment(unit_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("Environment=") and "=" in line[len("Environment=") :]:
            key, value = line[len("Environment=") :].split("=", 1)
            env[key] = value.strip('"')
    return env


@pytest.mark.parametrize(
    ("unit_name", "wrapper_name"),
    [
        ("liquidity-migration-bybit-continuous-demo.service", "run_bybit_continuous_demo_event_engine.sh"),
        ("liquidity-migration-bybit-continuous-paper.service", "run_bybit_continuous_demo_event_engine.sh"),
        ("liquidity-migration-bybit-long-demo.service", "run_bybit_long_demo_event_engine.sh"),
        ("liquidity-migration-bybit-long-paper.service", "run_bybit_long_demo_event_engine.sh"),
    ],
)
def test_wrapper_unit_env_builds_argv_that_parses(unit_name: str, wrapper_name: str, tmp_path: Path) -> None:
    """Round 4: the ExecStart<->argparse parity test deliberately skips the
    env-driven run_*.sh wrapper units — so a dropped/renamed CLI flag bricked
    the target producer at restart instead of failing the pre-restart
    smoke gate. Run each wrapper with PYTHON_BIN pointed at an argv-capturing
    stub under the unit's own Environment= values, then parse the captured argv
    with the real CLI parser."""
    import os
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    unit_path = repo / "deploy" / "systemd" / unit_name
    if not unit_path.exists():
        pytest.skip(f"{unit_name} not present")
    argv_out = tmp_path / "argv.bin"
    stub = tmp_path / "python_stub.sh"
    # as_posix() + quoting: a raw WindowsPath embeds backslashes into the bash
    # script/redirect, which bash strips — the stub then writes to a mangled
    # filename and the test false-fails on any Windows dev box.
    stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > '{argv_out.as_posix()}'\n", encoding="utf-8")
    stub.chmod(0o755)

    env = {**os.environ, **_unit_environment(unit_path)}
    env["PYTHON_BIN"] = stub.as_posix()
    env["ACCOUNT_INTENT_INBOX_ROOT"] = str(tmp_path / "account-intents")
    env["ACCOUNT_EXECUTION_ROOT"] = str(tmp_path / "account-root")
    env["RUN_ONCE"] = "1"

    # Preserve the production capture-authority check while relocating its filesystem
    # fixture and repo-root lookup into this unprivileged test directory.
    capture_enabled = tmp_path / "account-execution-capture-enabled"
    capture_enabled.touch()
    wrapper_text = (repo / "scripts" / wrapper_name).read_text(encoding="utf-8")
    wrapper_text = wrapper_text.replace(
        "/etc/liquidity-migration/account-execution-capture-enabled",
        str(capture_enabled),
    ).replace(
        'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        f'REPO_ROOT="{repo}"',
    )
    test_wrapper = tmp_path / wrapper_name
    test_wrapper.write_text(wrapper_text, encoding="utf-8")
    test_wrapper.chmod(0o755)

    # Daemon-mode wrappers exec the stub and return immediately; the legacy
    # single-cycle loop (USE_DAEMON=0, the long paper unit) honors RUN_ONCE here
    # so this smoke gate remains deterministic.
    try:
        result = subprocess.run(
            ["bash", str(test_wrapper)],
            env=env,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"{wrapper_name} failed under {unit_name} env: {result.stderr}"
    except subprocess.TimeoutExpired:
        assert argv_out.exists(), f"{wrapper_name} looped without ever invoking PYTHON_BIN"
    raw = argv_out.read_bytes().decode("utf-8")
    tokens = [t for t in raw.split("\0") if t]
    assert tokens[:2] == ["-m", "liquidity_migration"], tokens[:4]

    from liquidity_migration.cli import build_parser

    try:
        build_parser().parse_args(tokens[2:])
    except SystemExit as exc:
        raise AssertionError(
            f"{unit_name} -> {wrapper_name}: wrapper argv does not parse against the CLI "
            f"(exit {exc.code}): {tokens[2:]}"
        ) from exc


# ---------------------------------------------------------------------------
# audit2b: sh_nsymbols — N_SYMBOLS empty-list miscount in
# scripts/build_full_pit_bybit.sh.
#
# The build script derives a count of symbols from a comma-separated string for
# a build-log line. The original logic ``echo "$SYMBOLS" | tr ',' '\n' | wc -l``
# miscounts an EMPTY list as 1, because ``echo ""`` emits a single newline that
# ``wc -l`` then counts. The fix guards the empty case to produce 0 while leaving
# every non-empty (happy-path) count byte-identical.
# ---------------------------------------------------------------------------

_NSYMBOLS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_full_pit_bybit.sh"

# The buggy formulation, preserved verbatim to prove the regression existed.
OLD_SNIPPET = "N_SYMBOLS=$(echo \"$SYMBOLS\" | tr ',' '\\n' | wc -l)"


def _count_with_new_logic(symbols: str) -> int:
    """Run the script's current N_SYMBOLS logic in isolation via bash."""
    script = (
        f"SYMBOLS={symbols!r}\n"
        'if [ -z "$SYMBOLS" ]; then\n'
        "  N_SYMBOLS=0\n"
        "else\n"
        "  N_SYMBOLS=$(echo \"$SYMBOLS\" | tr ',' '\\n' | wc -l)\n"
        "fi\n"
        'echo "$N_SYMBOLS"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return int(out.stdout.strip())


def _count_with_old_logic(symbols: str) -> int:
    """Run the original buggy N_SYMBOLS logic, for the failing-on-old assertion."""
    script = f"SYMBOLS={symbols!r}\n" + OLD_SNIPPET + "\n" + 'echo "$N_SYMBOLS"\n'
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return int(out.stdout.strip())


def test_empty_list_counts_zero_not_one() -> None:
    # OLD code is wrong: blank line counted as one symbol.
    assert _count_with_old_logic("") == 1
    # NEW code: empty list -> 0.
    assert _count_with_new_logic("") == 0


def test_happy_path_counts_unchanged() -> None:
    # Non-empty inputs are byte-identical between old and new logic.
    for symbols in ("BTCUSDT", "BTCUSDT,ETHUSDT", "BTCUSDT,ETHUSDT,SOLUSDT"):
        old = _count_with_old_logic(symbols)
        new = _count_with_new_logic(symbols)
        assert old == new, f"happy path changed for {symbols!r}: {old} != {new}"
    assert _count_with_new_logic("BTCUSDT") == 1
    assert _count_with_new_logic("BTCUSDT,ETHUSDT") == 2
    assert _count_with_new_logic("BTCUSDT,ETHUSDT,SOLUSDT") == 3


def test_script_carries_the_guard() -> None:
    text = _NSYMBOLS_SCRIPT.read_text()
    # The empty-list guard is present and the bare buggy one-liner is gone.
    assert 'if [ -z "$SYMBOLS" ]; then' in text
    assert "N_SYMBOLS=0" in text
    assert not re.search(
        r"^N_SYMBOLS=\$\(echo \"\$SYMBOLS\" \| tr",
        text,
        flags=re.MULTILINE,
    ), "the unguarded buggy N_SYMBOLS one-liner is still present"


# ---------------------------------------------------------------------------
# audit2b: sh_ruff — gate-7 ruff fallback in scripts/verify_full_pit_rebuild.sh.
#
# The verification script runs ``set -euo pipefail`` and, in gate 7, linted with::
#
#     .venv/bin/ruff check liquidity_migration tests || ruff check liquidity_migration tests
#
# The ``||`` was intended only as a fallback for a *missing* ``.venv/bin/ruff``
# (exit 127), but it fires on ANY non-zero exit — including a genuine lint
# failure (ruff exits 1 when it finds errors). So if the canonical venv ruff found
# a real lint error, the gate silently re-checked against a different PATH ruff;
# when that one passed (version/config drift), the gate reported PASS and the
# script printed "All gates PASSED" despite a real lint failure.
#
# The fix selects the ruff binary up-front (prefer ``.venv/bin/ruff`` if
# executable, else PATH ``ruff``) and runs it exactly once, so its exit code —
# including a lint failure — propagates and fails the gate. When the venv binary
# is absent the fallback to PATH ruff is preserved, and the happy path (venv ruff
# present and clean) is byte-identical.
# ---------------------------------------------------------------------------

_RUFF_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_full_pit_rebuild.sh"

# Stub ruff binaries: a passing stub (exit 0) and a failing stub (exit 1,
# mimicking ruff finding a lint error).
_PASS_STUB = "#!/usr/bin/env bash\nexit 0\n"
_FAIL_STUB = '#!/usr/bin/env bash\necho "F401 unused import"\nexit 1\n'


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run_old_gate(tmp_path: Path, venv_body: str, path_body: str | None) -> int:
    """Model the OLD gate-7 lint line: ``$VENV check || ruff check``.

    Returns the exit code under ``set -euo pipefail`` (what the script as a whole
    would have done at that line). ``path_body=None`` means no PATH ruff exists.
    """
    venv = tmp_path / "venv_ruff"
    _make_stub(venv, venv_body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if path_body is not None:
        _make_stub(bindir / "ruff", path_body)
    script = f'set -euo pipefail\n"{venv}" check liquidity_migration tests || ruff check liquidity_migration tests\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        timeout=5,
    ).returncode


def _run_new_gate(tmp_path: Path, venv_body: str | None, path_body: str | None) -> int:
    """Model the NEW gate-7 lint logic: pick the binary, then run it once.

    ``venv_body=None`` means ``.venv/bin/ruff`` is absent (fallback to PATH).
    """
    venv = tmp_path / "venv_ruff"
    if venv_body is not None:
        _make_stub(venv, venv_body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if path_body is not None:
        _make_stub(bindir / "ruff", path_body)
    script = (
        "set -euo pipefail\n"
        f'if [ -x "{venv}" ]; then\n'
        f'  RUFF_BIN="{venv}"\n'
        "else\n"
        '  RUFF_BIN="ruff"\n'
        "fi\n"
        '"$RUFF_BIN" check liquidity_migration tests\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        timeout=5,
    ).returncode


def test_old_logic_masks_a_real_lint_failure(tmp_path: Path) -> None:
    # Canonical venv ruff finds a lint error (exit 1); PATH ruff passes.
    # OLD: the `||` swallows the failure -> gate exits 0 (masked).
    assert _run_old_gate(tmp_path, _FAIL_STUB, _PASS_STUB) == 0


def test_new_logic_fails_the_gate_on_a_real_lint_failure(tmp_path: Path) -> None:
    # Same inputs as above. NEW: venv ruff is chosen and its exit 1 propagates.
    assert _run_new_gate(tmp_path, _FAIL_STUB, _PASS_STUB) != 0


def test_new_logic_happy_path_unchanged(tmp_path: Path) -> None:
    # Venv ruff present and clean -> gate passes, identical to old behavior.
    assert _run_old_gate(tmp_path, _PASS_STUB, _PASS_STUB) == 0
    assert _run_new_gate(tmp_path, _PASS_STUB, _PASS_STUB) == 0


def test_new_logic_falls_back_when_venv_ruff_absent(tmp_path: Path) -> None:
    # The original fallback intent is preserved: missing venv ruff -> PATH ruff.
    assert _run_new_gate(tmp_path, None, _PASS_STUB) == 0
    # And a PATH-ruff lint failure still fails the gate.
    assert _run_new_gate(tmp_path, None, _FAIL_STUB) != 0


def test_script_carries_the_fix() -> None:
    text = _RUFF_SCRIPT.read_text()
    # The single-binary selection is present.
    assert "if [ -x .venv/bin/ruff ]; then" in text
    assert 'RUFF_BIN=".venv/bin/ruff"' in text
    assert '"$RUFF_BIN" check liquidity_migration tests' in text
    # The masking `||` fallback one-liner is gone.
    assert ".venv/bin/ruff check liquidity_migration tests || ruff check" not in text


# ──────────────────────────────────────────────────────────────────────────────
# deploy script — structural guards (from audit b13; deploy-ci-3, deploy-ci-6,
#                 deploy-env-timers-1, deploy-env-timers-3)
# ──────────────────────────────────────────────────────────────────────────────
def test_deploy_verifies_liquidation_collector_active() -> None:
    # deploy-ci-3: the always-on collector must be verified active+enabled in the
    # post-settle block (not just enabled+restarted), so a crash on new code fails loud.
    txt = DEPLOY_SH.read_text()
    assert "systemctl is-active --quiet liquidity-migration-liquidation-collector.service" in txt
    assert "systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service" in txt


def _assert_depth_collector_operator_gated_but_verified(text: str, *, success_marker: str) -> None:
    unit = "liquidity-migration-depth-collector.service"
    assert f"systemctl enable {unit}" not in text
    assert f"systemctl enable --now {unit}" not in text
    assert f"systemctl is-enabled --quiet {unit} 2>/dev/null" in text
    assert f"systemctl is-active --quiet {unit}" in text
    assert "is active but not enabled" in text
    verify_block = text[text.index('if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then') : text.index(success_marker)]
    assert f"systemctl is-enabled --quiet {unit} 2>/dev/null" in verify_block
    assert f"systemctl is-active --quiet {unit}" in verify_block


def test_deploy_depth_collector_is_operator_gated_but_verified_if_enabled() -> None:
    # Bybit historical book depth is unbuyable: deploy must not enable this data
    # collector by surprise, but once an operator enables it, success must require
    # the enabled unit to be active.
    text = DEPLOY_SH.read_text()
    assert "systemctl restart liquidity-migration-depth-collector.service" in text
    _assert_depth_collector_operator_gated_but_verified(text, success_marker='echo "deploy-verify-ok')


def test_verify_depth_collector_is_operator_gated_but_verified_if_enabled() -> None:
    _assert_depth_collector_operator_gated_but_verified(
        VERIFY_SH.read_text(),
        success_marker='echo "verify-ok',
    )


def test_recovery_depth_collector_is_operator_gated_but_verified_if_enabled() -> None:
    text = RECOVERY_SH.read_text()
    assert "systemctl restart liquidity-migration-depth-collector.service" in text
    _assert_depth_collector_operator_gated_but_verified(text, success_marker='echo "deploy-verify-ok')


def test_deploy_refuses_real_money_env() -> None:
    # Deploy must fail closed if the parsed config is truthy or ambiguous.
    txt = DEPLOY_SH.read_text()
    assert "REAL_MONEY" in txt
    assert "Refusing deploy: REAL_MONEY" in txt
    assert "is ambiguous" in txt


def test_deploy_and_verify_check_bybit_order_permissions_after_env_guard() -> None:
    # The VPS verifier previously passed with a read-only demo key; the order
    # daemons then failed later at set_leverage. Pin a live permission probe in
    # every deploy/verify path after the REAL_MONEY guard has resolved the env.
    for path, token in [
        (DEPLOY_SH, "--context deploy"),
        (VERIFY_SH, "--context verify"),
        (REPO_ROOT / "scripts" / "vps_console_recover_and_deploy.sh", "--context recovery-deploy"),
    ]:
        text = path.read_text(encoding="utf-8")
        load_idx = text.index("lm_load_private_systemd_environment")
        guard_idx = text.index('case "${REAL_MONEY:-}" in')
        check_idx = text.index("scripts/check_bybit_order_permissions.py")
        assert load_idx < guard_idx < check_idx
        assert ". /etc/liquidity-migration/bybit-demo.env" not in text
        assert ". /etc/liquidity-migration/account-execution.env" not in text
        assert ". /etc/liquidity-migration/account-paper-execution.env" not in text
        assert ". deploy/lib_systemd_environment.sh" in text
        assert token in text
        assert "ACCOUNT_EXECUTION_KERNEL_REQUIRED" in text
        assert "ACCOUNT_PAPER_KERNEL_REQUIRED" in text
        assert "ACCOUNT_CAPTURE_ROOT" in text
        assert "PAPER_ACCOUNT_CAPTURE_ROOT" in text


def test_target_producer_runners_have_no_private_venue_authority() -> None:
    for script_name in (
        "run_bybit_long_demo_event_engine.sh",
        "run_bybit_continuous_demo_event_engine.sh",
    ):
        text = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1" in text
        assert "ACCOUNT_INTENT_INBOX_ROOT" in text
        assert "ACCOUNT_EXECUTION_ROOT" in text
        assert "check_bybit_order_permissions.py" not in text
        assert "CONFIRM_DEMO_ORDERS" not in text
        assert "BYBIT_DEMO_API_KEY" not in text
        assert "BYBIT_DEMO_API_SECRET" not in text

    long_runner = (REPO_ROOT / "scripts" / "run_bybit_long_demo_event_engine.sh").read_text(encoding="utf-8")
    for retired in (
        "ORDER_FILL_CONFIRM_SECONDS",
        "ORDER_FILL_POLL_INTERVAL_SECONDS",
        "FALLBACK_EQUITY_USDT",
        "--order-fill-confirm-seconds",
        "--order-fill-poll-interval-seconds",
        "--fallback-equity-usdt",
        "--record-dry-run",
        "--telegram",
    ):
        assert retired not in long_runner


def test_hedge_runner_only_publishes_to_the_mandatory_account_owner() -> None:
    text = (REPO_ROOT / "scripts" / "run_continuous_hedge.sh").read_text(encoding="utf-8")

    assert 'case "${EXECUTION_ENVIRONMENT:-}" in' in text
    assert 'case "${ACCOUNT_EXECUTION_KERNEL_REQUIRED:-0}" in' in text
    assert 'case "${ACCOUNT_PAPER_KERNEL_REQUIRED:-0}" in' in text
    assert "ACCOUNT_INTENT_INBOX_ROOT and ACCOUNT_EXECUTION_ROOT are required" in text
    assert "account-execution-capture-enabled" in text
    assert '--execution-environment "$EXECUTION_ENVIRONMENT"' in text
    assert "--account-inbox-root" in text
    assert "--account-root" in text
    assert 'case "${HEDGE_ACTION:-dry-run}" in' in text
    assert "--execute" in text
    assert "--submit" not in text
    assert "SUBMIT_HEDGE" not in text
    assert "CONFIRM_DEMO_ORDERS" not in text
    assert "check_bybit_order_permissions.py" not in text
    assert "HEDGE_DATA_ROOT" not in text
    assert "--data-root" not in text


def test_order_permission_checker_fails_cleanly_without_demo_credentials() -> None:
    env = os.environ.copy()
    for key in [
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
        "REAL_MONEY",
    ]:
        env.pop(key, None)
    env["DEMO"] = "true"

    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_bybit_order_permissions.py"),
            "--context",
            "unit-test",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 1
    assert "missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_deploy_keeps_hedge_timer_when_continuous_off_but_leg_open() -> None:
    # deploy-env-timers-1: the gating must NOT be a bare apply_timer_enable on
    # CONTINUOUS_SLEEVE; it must consult the canonical account projection and
    # keep the timer enabled while an open hedge target exists.
    txt = DEPLOY_SH.read_text()
    assert "_hedge_timer_state" in txt
    assert "canonical_strategy_trade_rows" in txt
    assert 'ACCOUNT_ROOT="$ACCOUNT_EXECUTION_ROOT"' in txt
    assert "bybit-continuous-hedge-event" not in txt
    # The verify side must mirror the apply side, not raw CONTINUOUS_SLEEVE.
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in txt
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in txt
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in txt


def test_deploy_no_longer_warns_on_paper_following_frozen_demo_root() -> None:
    # deploy-env-timers-3 follow-up: paper no longer follows the demo kline/rmom
    # root, so deploy must validate the paper root instead of printing the stale
    # frozen-demo warning.
    txt = DEPLOY_SH.read_text()
    assert "KLINES_FOLLOW_ROOT still points at the now-FROZEN demo kline store" not in txt
    assert "$CONTINUOUS_PAPER_DATA_ROOT/residual_momentum.parquet" in txt
    assert 'sleeve_on "$CONTINUOUS_PAPER_SLEEVE"' in txt


# ──────────────────────────────────────────────────────────────────────────────
# deploy script — behavioral test of the REAL_MONEY refuse guard (from audit b13;
#                 deploy-ci-6)
# Replicates the exact case-statement from deploy_vps_live.sh so the truthy-detection
# logic is exercised, not just present. Kept in sync via the structural test above.
# ──────────────────────────────────────────────────────────────────────────────
_REAL_MONEY_GUARD = r"""
real_money_refused() {
  case "${REAL_MONEY:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
    ""|0|false|FALSE|False|no|NO|No|off|OFF|Off) return 1 ;;
    *) return 0 ;;
  esac
}
if real_money_refused; then echo REFUSED; else echo OK; fi
"""


def _run_bash(body: str, env_line: str = "") -> str:
    script = textwrap.dedent(f"""
        set -euo pipefail
        {env_line}
        {body}
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "YES", "on", "ON"])
def test_real_money_truthy_values_are_refused(val: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, f'export REAL_MONEY="{val}"') == "REFUSED"


@pytest.mark.parametrize(
    "env_line", ['export REAL_MONEY=""', "unset REAL_MONEY || true", "export REAL_MONEY=false", "export REAL_MONEY=0"]
)
def test_real_money_demo_values_are_allowed(env_line: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, env_line) == "OK"


@pytest.mark.parametrize("val", ["enabled", "maybe", "demo", "null"])
def test_real_money_ambiguous_values_are_refused(val: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, f'export REAL_MONEY="{val}"') == "REFUSED"


# Verify the structural test's source-of-truth: the deploy script's actual case arms
# must contain every truthy token the behavioral guard tests (so they can't drift).
def test_deploy_real_money_case_covers_truthy_tokens() -> None:
    txt = DEPLOY_SH.read_text()
    for token in ("1|true|TRUE", "yes|YES", "on|ON"):
        assert token in txt, f"deploy REAL_MONEY case missing arm {token!r}"


# --- audit bucket b15: kill-switch fake-systemctl semantics (kill-switch-3) ---
_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$@" >> "$LOG"
cmd="$1"; shift
now=0; for a in "$@"; do [ "$a" = "--now" ] && now=1; done
args=(); for a in "$@"; do [ "$a" = "--quiet" ] || [ "$a" = "--now" ] || args+=("$a"); done
case "$cmd" in
  enable)  for u in "${args[@]}"; do touch "$STATE/$u.enabled"; [ "$now" = 1 ] && touch "$STATE/$u.active"; done ;;
  disable) for u in "${args[@]}"; do rm -f "$STATE/$u.enabled" "$STATE/$u.active"; done ;;
  restart|start) for u in "${args[@]}"; do touch "$STATE/$u.active"; done ;;
  is-active)  for u in "${args[@]}"; do [ -f "$STATE/$u.active"  ] || exit 1; done ;;
  is-enabled) for u in "${args[@]}"; do [ -f "$STATE/$u.enabled" ] || exit 1; done ;;
esac
exit 0
"""


def test_fake_systemctl_enable_without_now_does_not_start_unit(tmp_path: Path) -> None:
    # kill-switch-3: the test fake must mirror real `systemctl enable` (no --now):
    # it writes the wants-symlink (.enabled) but does NOT start the unit (.active).
    # A fake that set .active on bare `enable` would let an on-path verify pass with
    # no start step, hiding a deploy that dropped the `systemctl restart` lines.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(_FAKE_SYSTEMCTL)
    (fake_bin / "systemctl").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "log"
    log.write_text("")
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "STATE": str(state), "LOG": str(log)}
    unit = "demo.service"
    subprocess.run(["bash", "-c", f"systemctl enable {unit}"], env=env, check=True, timeout=5)
    assert (state / f"{unit}.enabled").exists()
    assert not (state / f"{unit}.active").exists(), "bare enable must NOT start the unit"
    # enable --now and start DO mark active.
    subprocess.run(["bash", "-c", f"systemctl start {unit}"], env=env, check=True, timeout=5)
    assert (state / f"{unit}.active").exists()


# ==========================================================================
# Relocated from tests/test_audit_int_iI.py (audit bucket iI): cross-file
# integration-completion regression tests for the deploy-gate fixes whose
# owned-file side (scripts/deploy_vps_live.sh) landed in another bucket. Both
# scripts here are SSH/systemctl deploy plumbing that cannot run in CI, so —
# matching the existing deploy-script regression style above — these tests
# assert the static content of the fail-closed guards.
#
# Findings covered:
#   deploy-ci-6  verify_vps_live.sh and vps_console_recover_and_deploy.sh now
#                carry the same fail-closed `case "${REAL_MONEY:-}" in 1|true|...)
#                exit 1` guard as deploy_vps_live.sh.
#   deploy-ci-3  The console-recovery verify block now asserts the always-on
#                liquidation collector is active+enabled before 'deploy-verify-ok'.
# ==========================================================================

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "scripts" / "verify_vps_live.sh"
RECOVERY = REPO / "scripts" / "vps_console_recover_and_deploy.sh"

COLLECTOR = "liquidity-migration-liquidation-collector.service"
# The truthy-REAL_MONEY case arm shared verbatim with deploy_vps_live.sh.
REAL_MONEY_CASE_ARM = "1|true|TRUE|True|yes|YES|Yes|on|ON|On)"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# deploy-ci-6 : fail-closed REAL_MONEY guard in BOTH owned scripts
# --------------------------------------------------------------------------


def _assert_real_money_guard(text: str, *, refusal_token: str) -> None:
    """A guard whose truthy and ambiguous arms exit nonzero."""
    assert 'case "${REAL_MONEY:-}" in' in text, "missing REAL_MONEY case guard"
    assert REAL_MONEY_CASE_ARM in text, "REAL_MONEY guard does not match the truthy arm set"
    # The guard must be fail-closed: the truthy arm exits 1, and it references
    # the env file in the refusal so an operator knows what to fix.
    guard = text.split('case "${REAL_MONEY:-}" in', 1)[1].split("esac", 1)[0]
    assert REAL_MONEY_CASE_ARM in guard
    assert "exit 1" in guard, "REAL_MONEY guard must exit 1 on a truthy value"
    assert "REAL_MONEY" in guard
    assert refusal_token in guard
    assert "/etc/liquidity-migration/bybit-demo.env" in guard
    assert '""|0|false|FALSE|False|no|NO|No|off|OFF|Off)' in guard
    assert "is ambiguous" in guard


def test_verify_script_fails_closed_on_real_money() -> None:
    text = _read(VERIFY)
    _assert_real_money_guard(text, refusal_token="Verification failed")
    # The guard must come after the strict data-only loader.
    load_idx = text.index("lm_load_private_systemd_environment")
    guard_idx = text.index('case "${REAL_MONEY:-}" in')
    assert load_idx < guard_idx


def test_recovery_script_fails_closed_on_real_money() -> None:
    text = _read(RECOVERY)
    _assert_real_money_guard(text, refusal_token="Refusing deploy")
    load_idx = text.index("lm_load_private_systemd_environment")
    guard_idx = text.index('case "${REAL_MONEY:-}" in')
    assert load_idx < guard_idx


def test_real_money_truthy_arm_does_not_include_false_forms() -> None:
    truthy = {"1", "true", "TRUE", "True", "yes", "YES", "Yes", "on", "ON", "On"}
    explicit_false = {"", "0", "false", "FALSE", "False", "no", "NO", "off", "OFF"}
    arm = REAL_MONEY_CASE_ARM.rstrip(")")
    patterns = arm.split("|")
    for value in truthy:
        assert value in patterns, f"truthy {value!r} must trip the guard"
    for value in explicit_false:
        assert value not in patterns, f"false form {value!r} must not be truthy"


def test_demo_owner_does_not_inherit_mainnet_credentials() -> None:
    owner = (
        REPO_ROOT
        / "deploy"
        / "systemd"
        / "liquidity-migration-account-execution.service"
    ).read_text(encoding="utf-8")
    assert "UnsetEnvironment=BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET" in owner


def test_paper_owner_does_not_inherit_private_credentials() -> None:
    owner = (
        REPO_ROOT
        / "deploy"
        / "systemd"
        / "liquidity-migration-account-paper-execution.service"
    ).read_text(encoding="utf-8")

    assert (
        "UnsetEnvironment=BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET "
        "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET REAL_MONEY "
        "TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID"
    ) in owner


def test_non_owner_fresh_epoch_units_do_not_inherit_private_credentials() -> None:
    unit_names = (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
        "liquidity-migration-continuous-hedge.service",
        "liquidity-migration-continuous-rmom-refresh.service",
    )
    expected = {
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
        "REAL_MONEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }

    for unit_name in unit_names:
        text = (REPO_ROOT / "deploy" / "systemd" / unit_name).read_text(
            encoding="utf-8"
        )
        line = next(
            row for row in text.splitlines() if row.startswith("UnsetEnvironment=")
        )
        assert set(line.removeprefix("UnsetEnvironment=").split()) == expected


# --------------------------------------------------------------------------
# deploy-ci-3 : recovery verify block asserts the liquidation collector is up
# --------------------------------------------------------------------------


def test_recovery_enables_and_restarts_the_collector() -> None:
    """Sanity precondition for the finding: the recovery path DOES bring the
    always-on collector up, so the verify block owes it an is-active check."""
    text = _read(RECOVERY)
    assert f"systemctl enable {COLLECTOR}" in text
    assert f"systemctl restart {COLLECTOR}" in text


def test_recovery_verify_block_checks_collector_active_and_enabled() -> None:
    text = _read(RECOVERY)
    assert f"systemctl is-active --quiet {COLLECTOR}" in text, (
        "recovery verify must assert the liquidation collector is active (catches a crash-loop reaching 'failed')"
    )
    assert f"systemctl is-enabled --quiet {COLLECTOR}" in text, (
        "recovery verify must assert the liquidation collector is enabled"
    )


def test_recovery_collector_verify_is_in_the_post_settle_block_before_verify_ok() -> None:
    """The collector check must sit in the POST-settle verify block (after the
    sleep) and BEFORE 'deploy-verify-ok' is emitted — otherwise a broken
    collector still reaches the success message + Telegram."""
    text = _read(RECOVERY)
    # Post-settle block begins at the settle sleep guard.
    settle_idx = text.index('if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then')
    # Anchor on the actual success echo, NOT any mention of the string (the
    # deploy-ci-3 comment block also references 'deploy-verify-ok').
    verify_ok_idx = text.index('echo "deploy-verify-ok')
    is_active_idx = text.index(f"systemctl is-active --quiet {COLLECTOR}")
    is_enabled_idx = text.index(f"systemctl is-enabled --quiet {COLLECTOR}")
    assert settle_idx < is_active_idx < verify_ok_idx
    assert settle_idx < is_enabled_idx < verify_ok_idx


def test_recovery_collector_verify_matches_account_owner_pattern() -> None:
    """The collector and both owners fail loud on inactive/disabled state."""
    text = _read(RECOVERY)
    for unit in (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
        COLLECTOR,
    ):
        assert re.search(rf"^\s*systemctl is-active --quiet {re.escape(unit)}\s*$", text, re.MULTILINE), (
            f"missing is-active --quiet for {unit}"
        )
        assert re.search(rf"^\s*systemctl is-enabled --quiet {re.escape(unit)}\s*$", text, re.MULTILINE), (
            f"missing is-enabled --quiet for {unit}"
        )


# The account execution owner is now the sole operational notification and
# reconciliation authority; the legacy report/risk units must stay deleted.
_SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"


def test_account_owner_replaces_legacy_risk_and_report_units() -> None:
    owner = (_SYSTEMD_DIR / "liquidity-migration-account-execution.service").read_text(encoding="utf-8")
    liveness = (_SYSTEMD_DIR / "liquidity-migration-demo-liveness.service").read_text(encoding="utf-8")

    wrapper = (REPO_ROOT / "scripts" / "run_authorized_fresh_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "liquidity-migration-account-execution.service main" in owner
    assert "run_account_execution_service.sh" in wrapper
    assert "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1" in owner
    assert "CONFIRM_DEMO_ORDERS=1" in owner
    assert "TELEGRAM_ENABLED=1" in owner
    assert "liquidity-migration-demo-liveness.service main" in liveness
    assert "--cooldown-min 360" in wrapper
    for retired in (
        "liquidity-migration-bybit-risk.service",
        "liquidity-migration-combined-book-report.service",
        "liquidity-migration-combined-book-report.timer",
    ):
        assert not (_SYSTEMD_DIR / retired).exists()


def test_account_owner_launchers_have_no_default_state_routes() -> None:
    demo = (REPO_ROOT / "scripts" / "run_account_execution_service.sh").read_text(encoding="utf-8")
    paper = (REPO_ROOT / "scripts" / "run_account_paper_execution_service.sh").read_text(encoding="utf-8")

    for text in (demo, paper):
        assert "account-execution-capture-enabled" in text
        assert "ACCOUNT_INTENT_INBOX_ROOT" in text
        assert 'ACCOUNT_ROOT="${ACCOUNT_EXECUTION_ROOT:-}"' in text
    assert 'ACCOUNT_CAPTURE_ROOT="${ACCOUNT_CAPTURE_ROOT:-}"' in demo
    for text in (demo, paper):
        assert "ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS" in text
        assert "--request-market-warmup-timeout-seconds" in text
    assert 'ACCOUNT_CAPTURE_ROOT="${ACCOUNT_PAPER_CAPTURE_ROOT:-}"' in paper
    assert "data/bybit-account-execution" not in demo
    assert "data/bybit-account-paper" not in paper


def test_account_owner_units_gate_dependents_on_real_readiness() -> None:
    units = {
        "demo": _SYSTEMD_DIR / "liquidity-migration-account-execution.service",
        "paper": _SYSTEMD_DIR / "liquidity-migration-account-paper-execution.service",
    }
    for environment, path in units.items():
        text = path.read_text(encoding="utf-8")
        assert "ExecStartPost=" in text
        assert (
            "ExecStartPost=/opt/liquidity-migration/scripts/"
            "run_authorized_fresh_runtime.sh liquidity-migration-account-"
        ) in text
        assert re.search(r"^ExecStartPost=.* readiness$", text, re.MULTILINE)
        assert "TimeoutStartSec=240" in text
    wrapper = (REPO_ROOT / "scripts" / "run_authorized_fresh_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "-m liquidity_migration.account_owner_readiness" in wrapper
    assert "--environment demo" in wrapper
    assert "--environment paper" in wrapper
    assert '--account-root "${ACCOUNT_EXECUTION_ROOT:' in wrapper
    assert '--inbox-root "${ACCOUNT_INTENT_INBOX_ROOT:' in wrapper
    assert "--timeout-seconds 180" in wrapper
    assert '--capture-root "${ACCOUNT_CAPTURE_ROOT:' in wrapper
    assert '--capture-root "${ACCOUNT_PAPER_CAPTURE_ROOT:' in wrapper


# --- relocated from tests/test_audit_int_iM.py (audit bucket iM) ---------------
# deploy-env-timers-3: the continuous-PAPER systemd unit must stream its own
# kline pool. These tests pin that the follow override is absent, no paper
# Environment assignment points at the demo root, and the rest of the
# load-bearing paper config is undisturbed.
_PAPER_UNIT = (
    Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-paper.service"
)


def _paper_unit_text() -> str:
    return _PAPER_UNIT.read_text(encoding="utf-8")


def _paper_environment_assignments() -> dict[str, str]:
    """Parse the unit's active ``Environment=KEY=VALUE`` lines (skip comments)."""
    env: dict[str, str] = {}
    for raw in _paper_unit_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith("Environment="):
            continue
        assignment = line[len("Environment=") :]
        key, _, value = assignment.partition("=")
        env[key] = value
    return env


def test_paper_unit_no_longer_follows_demo_kline_root() -> None:
    """deploy-env-timers-3: the PAPER shadow must not carry a KLINES_FOLLOW_ROOT
    override, so it always runs its own kline pool and never follows a frozen demo
    snapshot when CONTINUOUS_SLEEVE=off + CONTINUOUS_PAPER_SLEEVE=on."""
    env = _paper_environment_assignments()
    assert "KLINES_FOLLOW_ROOT" not in env, (
        "PAPER unit still sets KLINES_FOLLOW_ROOT — it would follow the demo "
        "kline store and freeze when the demo sleeve is toggled off"
    )


def test_paper_unit_environment_never_points_at_demo_root() -> None:
    """No active Environment= assignment in the PAPER unit may reference the demo
    data root: the shadow's market-data plane must be self-contained."""
    env = _paper_environment_assignments()
    offenders = {key: value for key, value in env.items() if "bybit-continuous-demo-event" in value}
    assert not offenders, f"PAPER unit Environment assignments still point at the demo root: {offenders}"


def test_paper_unit_keeps_its_own_paper_data_root() -> None:
    """The paper sleeve must still write/read its own dataset root so reconcile can
    pair it against the demo ledger — only the follow override was removed."""
    env = _paper_environment_assignments()
    assert env.get("DATA_ROOT") == "data/bybit-continuous-paper-event", "PAPER unit lost or changed its own DATA_ROOT"


def test_paper_unit_load_bearing_paper_knobs_intact() -> None:
    """The target-only paper service keeps its routing and strategy knobs."""
    env = _paper_environment_assignments()
    for key, expected in (
        ("EXECUTION_ENVIRONMENT", "paper"),
        ("STRATEGY_PROFILE", "continuous_ensemble_v2"),
    ):
        assert env.get(key) == expected, f"PAPER unit knob {key} changed: expected {expected!r}, got {env.get(key)!r}"
    for retired in (
        "LEFT_DECILE_EXIT_ENABLED",
        "STOP_APPROACH_FRAC",
        "FAILED_FADE_HOURS",
        "BREAKEVEN_ARM_PCT",
        "DAILY_REBALANCE_ENABLED",
        "ENTRY_PORTFOLIO_HEAT_CAP_FRAC",
        "ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC",
        "TELEGRAM_ENABLED",
        "CONTINUOUS_SNIPER",
    ):
        assert retired not in env


def test_paper_unit_documents_the_dropped_follow_override() -> None:
    """The removal is documented in-unit (audit id + rationale) so an operator
    re-adding the follow knob understands the demo-off hazard."""
    text = _paper_unit_text()
    assert "deploy-env-timers-3" in text, (
        "the dropped KLINES_FOLLOW_ROOT override should be documented with its audit id"
    )
