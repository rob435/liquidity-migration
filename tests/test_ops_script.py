from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS = REPO_ROOT / "scripts" / "ops.sh"


def _read_nul_args(path: Path) -> list[str]:
    raw = path.read_bytes()
    return [part.decode() for part in raw.split(b"\0") if part]


def _write_capture_command(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
: > "$CALL_LOG"
for arg in "$@"; do
  printf '%s\\0' "$arg" >> "$CALL_LOG"
done
printf '%s\\n%s\\n' "${SSH_TARGET-}" "${REPO_DIR-}" > "$ENV_LOG"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_ops(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["/bin/bash", str(OPS), *args],
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("help_args", [[], ["help"], ["-h"], ["--help"]])
def test_ops_help_is_read_only(help_args: list[str], tmp_path: Path) -> None:
    call_log = tmp_path / "called"
    result = _run_ops(help_args, env={"CALL_LOG": str(call_log)})

    assert result.returncode == 0, result.stderr
    assert "Usage: scripts/ops.sh" in result.stdout
    assert "never enables REAL_MONEY" in result.stdout
    assert "never auto-promoted" in result.stdout
    assert "event-parity" in result.stdout
    assert "target-replay" in result.stdout
    assert "account-replay" in result.stdout
    assert "account-parity" in result.stdout
    assert "account-parity-scope" in result.stdout
    assert "natural-freeze" in result.stdout
    assert "natural-run-config" in result.stdout
    assert "natural-effective-config" in result.stdout
    assert "natural-sufficiency" in result.stdout
    assert "clock-offset" in result.stdout
    assert "clock-series" in result.stdout
    assert "demo-calibration" in result.stdout
    assert "natural-safety-flatten" in result.stdout
    assert "twin-drift" in result.stdout
    assert "v7-archive" in result.stdout
    assert "stopped-epoch" in result.stdout
    assert "fresh-deploy-epoch" in result.stdout
    assert "fresh-deploy-env" in result.stdout
    assert "authorized-deploy-epoch" in result.stdout
    assert "venue-accounting" in result.stdout
    assert "cutover-authority" in result.stdout
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["status", "--probe", "value with spaces"],
            [str(REPO_ROOT / "scripts" / "verify_vps_live.sh"), "--probe", "value with spaces"],
        ),
        (
            ["equity", "--sleeves", "long,continuous", "--output", "path with spaces"],
            [
                str(REPO_ROOT / "scripts" / "equity_curves.sh"),
                "--sleeves",
                "long,continuous",
                "--output",
                "path with spaces",
            ],
        ),
    ],
)
def test_ops_routes_canonical_shell_commands_with_exact_arguments(
    args: list[str], expected: list[str], tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_capture_command(fake_bin / "bash")
    call_log = tmp_path / "call.bin"
    env_log = tmp_path / "env.txt"
    env = {
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
        "CALL_LOG": str(call_log),
        "ENV_LOG": str(env_log),
        "SSH_TARGET": "unit@example.test",
        "REPO_DIR": "/srv/liquidity migration",
    }

    result = _run_ops(args, env=env)

    assert result.returncode == 0, result.stderr
    assert _read_nul_args(call_log) == expected
    assert env_log.read_text(encoding="utf-8").splitlines() == [
        "unit@example.test",
        "/srv/liquidity migration",
    ]


@pytest.mark.parametrize(
    ("args", "expected_prefix"),
    [
        (
            ["tail-plan", "--output-root", "root with spaces"],
            [str(REPO_ROOT / "scripts" / "continuous_tail_survival_2026_07_10.py"), "--plan"],
        ),
        (
            ["data-audit", "--venue", "both"],
            [str(REPO_ROOT / "scripts" / "granular_data_surface.py")],
        ),
        (
            ["data-build", "--execute", "--datasets", "funding"],
            [str(REPO_ROOT / "scripts" / "granular_data_surface.py"), "--execute"],
        ),
        (
            ["tail-run", "--cells", "budget_010", "control"],
            [str(REPO_ROOT / "scripts" / "continuous_tail_survival_2026_07_10.py")],
        ),
        (
            ["overhaul-plan", "--binance-root", "root with spaces"],
            [str(REPO_ROOT / "scripts" / "strategy_overhaul_scout_2026_07_10.py"), "--plan"],
        ),
        (
            ["overhaul-phase0", "--output-root", "root with spaces"],
            [
                str(REPO_ROOT / "scripts" / "strategy_overhaul_scout_2026_07_10.py"),
                "--phase0-inventory",
            ],
        ),
        (
            ["event-parity", "--environment", "historical=tape with spaces"],
            ["-m", "liquidity_migration.strategy_event_parity"],
        ),
        (
            ["target-replay", "--capture", "capture with spaces.jsonl"],
            ["-m", "liquidity_migration.strategy_target_replay"],
        ),
        (
            ["account-replay", "--target-capture", "capture with spaces.jsonl"],
            ["-m", "liquidity_migration.captured_account_replay"],
        ),
        (
            ["account-parity", "--environment", "historical=root with spaces"],
            ["-m", "liquidity_migration.kernel_parity"],
        ),
        (
            ["account-parity-scope", "--output", "scope with spaces.json"],
            [str(REPO_ROOT / "scripts" / "build_kernel_parity_scope.py")],
        ),
        (
            ["natural-freeze", "verify", "--manifest", "freeze with spaces.json"],
            ["-m", "liquidity_migration.natural_cutover_freeze_manifest"],
        ),
        (
            ["natural-run-config", "build", "--freeze-manifest", "freeze with spaces.json"],
            ["-m", "liquidity_migration.natural_run_config"],
        ),
        (
            ["natural-effective-config", "verify-bundle", "--bundle", "bundle with spaces.json"],
            ["-m", "liquidity_migration.natural_effective_config"],
        ),
        (
            ["natural-sufficiency", "--output", "receipt with spaces"],
            ["-m", "liquidity_migration.natural_tape_sufficiency"],
        ),
        (
            ["clock-series", "verify", "--series", "series with spaces.json"],
            ["-m", "liquidity_migration.clock_offset_series"],
        ),
        (
            ["twin-calibrate", "--account-root", "root with spaces"],
            [str(REPO_ROOT / "scripts" / "calibrate_execution_twin.py")],
        ),
        (
            ["twin-drift", "verify", "--output", "receipt with spaces"],
            ["-m", "liquidity_migration.execution_twin_drift"],
        ),
        (
            [
                "v7-archive",
                "from-stopped-roots",
                "--destination-root",
                "archive with spaces",
            ],
            ["-m", "liquidity_migration.v7_archive_materialization"],
        ),
        (
            ["stopped-epoch", "verify", "--seal", "seal with spaces.json"],
            ["-m", "liquidity_migration.stopped_natural_epoch"],
        ),
        (
            [
                "fresh-deploy-epoch",
                "create",
                "--stopped-seal",
                "seal with spaces.json",
                "--epoch-parent",
                "epoch with spaces",
            ],
            ["-m", "liquidity_migration.fresh_deploy_epoch"],
        ),
        (
            ["fresh-deploy-env", "verify", "--manifest", "fresh with spaces.json"],
            ["-m", "liquidity_migration.fresh_deploy_environment"],
        ),
        (
            [
                "authorized-deploy-epoch",
                "prepare",
                "--authorization",
                "authority with spaces.json",
            ],
            ["-m", "liquidity_migration.authorized_deploy_epoch"],
        ),
        (
            ["venue-accounting", "--output", "receipt with spaces"],
            [str(REPO_ROOT / "scripts" / "reconcile_bybit_demo_accounting.py")],
        ),
        (
            ["cutover-authority", "verify", "--receipt", "receipt with spaces"],
            [str(REPO_ROOT / "scripts" / "account_execution_cutover_authority.py")],
        ),
        (["test", "-q", "tests/a file.py"], ["-m", "pytest"]),
    ],
)
def test_ops_python_override_and_argument_forwarding(
    args: list[str], expected_prefix: list[str], tmp_path: Path
) -> None:
    python_dir = tmp_path / "python tools"
    python_dir.mkdir()
    python = python_dir / "python shim"
    _write_capture_command(python)
    call_log = tmp_path / "call.bin"
    env_log = tmp_path / "env.txt"

    result = _run_ops(
        args,
        env={
            "PYTHON": str(python),
            "CALL_LOG": str(call_log),
            "ENV_LOG": str(env_log),
        },
    )

    assert result.returncode == 0, result.stderr
    routed = _read_nul_args(call_log)
    assert routed[: len(expected_prefix)] == expected_prefix
    if args[0] == "tail-plan":
        assert routed == [*expected_prefix, "--output-root", "root with spaces"]
    elif args[0] == "data-audit":
        assert routed == [*expected_prefix, "--venue", "both"]
    elif args[0] == "data-build":
        assert routed == [*expected_prefix, "--datasets", "funding"]
    elif args[0] == "tail-run":
        assert routed == [*expected_prefix, "--cells", "budget_010", "control"]
    elif args[0] == "overhaul-plan":
        assert routed == [*expected_prefix, "--binance-root", "root with spaces"]
    elif args[0] == "overhaul-phase0":
        assert routed == [*expected_prefix, "--output-root", "root with spaces"]
    elif args[0] == "event-parity":
        assert routed == [
            *expected_prefix,
            "--environment",
            "historical=tape with spaces",
        ]
    elif args[0] == "target-replay":
        assert routed == [
            *expected_prefix,
            "--capture",
            "capture with spaces.jsonl",
        ]
    elif args[0] == "account-replay":
        assert routed == [
            *expected_prefix,
            "--target-capture",
            "capture with spaces.jsonl",
        ]
    elif args[0] == "account-parity":
        assert routed == [
            *expected_prefix,
            "--environment",
            "historical=root with spaces",
        ]
    elif args[0] == "account-parity-scope":
        assert routed == [
            *expected_prefix,
            "--output",
            "scope with spaces.json",
        ]
    elif args[0] == "natural-freeze":
        assert routed == [
            *expected_prefix,
            "verify",
            "--manifest",
            "freeze with spaces.json",
        ]
    elif args[0] == "natural-run-config":
        assert routed == [
            *expected_prefix,
            "build",
            "--freeze-manifest",
            "freeze with spaces.json",
        ]
    elif args[0] == "natural-effective-config":
        assert routed == [
            *expected_prefix,
            "verify-bundle",
            "--bundle",
            "bundle with spaces.json",
        ]
    elif args[0] == "natural-sufficiency":
        assert routed == [
            *expected_prefix,
            "--output",
            "receipt with spaces",
        ]
    elif args[0] == "clock-series":
        assert routed == [
            *expected_prefix,
            "verify",
            "--series",
            "series with spaces.json",
        ]
    elif args[0] == "twin-calibrate":
        assert routed == [
            *expected_prefix,
            "--account-root",
            "root with spaces",
        ]
    elif args[0] == "twin-drift":
        assert routed == [
            *expected_prefix,
            "verify",
            "--output",
            "receipt with spaces",
        ]
    elif args[0] == "v7-archive":
        assert routed == [
            *expected_prefix,
            "from-stopped-roots",
            "--destination-root",
            "archive with spaces",
        ]
    elif args[0] == "stopped-epoch":
        assert routed == [
            *expected_prefix,
            "verify",
            "--seal",
            "seal with spaces.json",
        ]
    elif args[0] == "fresh-deploy-epoch":
        assert routed == [
            *expected_prefix,
            "create",
            "--stopped-seal",
            "seal with spaces.json",
            "--epoch-parent",
            "epoch with spaces",
        ]
    elif args[0] == "fresh-deploy-env":
        assert routed == [
            *expected_prefix,
            "verify",
            "--manifest",
            "fresh with spaces.json",
        ]
    elif args[0] == "authorized-deploy-epoch":
        assert routed == [
            *expected_prefix,
            "prepare",
            "--authorization",
            "authority with spaces.json",
        ]
    elif args[0] == "venue-accounting":
        assert routed == [
            *expected_prefix,
            "--output",
            "receipt with spaces",
        ]
    elif args[0] == "cutover-authority":
        assert routed == [
            *expected_prefix,
            "verify",
            "--receipt",
            "receipt with spaces",
        ]
    else:
        assert routed == [*expected_prefix, "-q", "tests/a file.py"]


def test_ops_twin_calibrate_help_works_without_an_editable_install(tmp_path: Path) -> None:
    bootstrap = tmp_path / "isolated_bootstrap.py"
    bootstrap.write_text(
        "\n".join(
            (
                "import runpy",
                "import sys",
                f"sys.path.append({sysconfig.get_paths()['purelib']!r})",
                "script = sys.argv[1]",
                "sys.argv = sys.argv[1:]",
                "runpy.run_path(script, run_name='__main__')",
                "",
            )
        ),
        encoding="utf-8",
    )
    python = tmp_path / "isolated-python"
    python.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -I -S "{bootstrap}" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)

    result = _run_ops(
        ["twin-calibrate", "--help"],
        env={"PYTHON": str(python)},
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: calibrate_execution_twin.py" in result.stdout
    assert "--market-capture-root" in result.stdout


def test_ops_data_build_requires_explicit_execute() -> None:
    result = _run_ops(["data-build", "--datasets", "funding"])

    assert result.returncode == 2
    assert "first argument must be --execute" in result.stderr


def test_ops_reset_is_explicit_remote_dry_run_and_preserves_arguments(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text(
        """#!/bin/sh
: > "$SSH_LOG"
for arg in "$@"; do
  printf '%s\\0' "$arg" >> "$SSH_LOG"
done
exec /bin/bash -s
""",
        encoding="utf-8",
    )
    ssh.chmod(0o755)

    remote = tmp_path / "remote repo"
    (remote / "scripts").mkdir(parents=True)
    (remote / "scripts" / "reset_demo_paper_ledgers.sh").write_text(
        """#!/bin/bash
: > "$RESET_ARG_LOG"
for arg in "$@"; do
  printf '%s\\0' "$arg" >> "$RESET_ARG_LOG"
done
pwd > "$RESET_CWD_LOG"
""",
        encoding="utf-8",
    )
    ssh_log = tmp_path / "ssh.bin"
    reset_log = tmp_path / "reset.bin"
    cwd_log = tmp_path / "cwd.txt"
    injected = tmp_path / "injected"
    archive_arg = f"archive with spaces $(touch {injected}) and 'quotes'"
    env = {
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
        "SSH_TARGET": "operator@example.test",
        "REPO_DIR": str(remote),
        "SSH_LOG": str(ssh_log),
        "RESET_ARG_LOG": str(reset_log),
        "RESET_CWD_LOG": str(cwd_log),
    }

    result = _run_ops(
        ["reset", "--sleeves", "continuous", "--archive-dir", archive_arg],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert _read_nul_args(ssh_log) == [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        "operator@example.test",
        "bash",
        "-s",
    ]
    assert _read_nul_args(reset_log) == [
        "--dry-run",
        "--sleeves",
        "continuous",
        "--archive-dir",
        archive_arg,
    ]
    assert cwd_log.read_text(encoding="utf-8").strip() == str(remote)
    assert not injected.exists(), "forwarded reset arguments must not be evaluated as shell code"


def test_ops_reset_passes_execute_without_injecting_dry_run(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text("#!/bin/sh\nexec /bin/bash -s\n", encoding="utf-8")
    ssh.chmod(0o755)
    remote = tmp_path / "remote"
    (remote / "scripts").mkdir(parents=True)
    (remote / "scripts" / "reset_demo_paper_ledgers.sh").write_text(
        """#!/bin/bash
: > "$RESET_ARG_LOG"
for arg in "$@"; do
  printf '%s\\0' "$arg" >> "$RESET_ARG_LOG"
done
""",
        encoding="utf-8",
    )
    reset_log = tmp_path / "reset.bin"

    result = _run_ops(
        ["reset", "--sleeves", "all", "--execute", "--label", "new-window"],
        env={
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
            "SSH_TARGET": "operator@example.test",
            "REPO_DIR": str(remote),
            "RESET_ARG_LOG": str(reset_log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert _read_nul_args(reset_log) == [
        "--sleeves",
        "all",
        "--execute",
        "--label",
        "new-window",
    ]

    explicit_preview = _run_ops(
        ["reset", "--dry-run", "--sleeves", "long"],
        env={
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
            "SSH_TARGET": "operator@example.test",
            "REPO_DIR": str(remote),
            "RESET_ARG_LOG": str(reset_log),
        },
    )
    assert explicit_preview.returncode == 0, explicit_preview.stderr
    assert _read_nul_args(reset_log) == ["--dry-run", "--sleeves", "long"]


def test_ops_deploy_requires_first_argument_execute_and_routes_after_handshake(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_capture_command(fake_bin / "bash")
    call_log = tmp_path / "call.bin"
    env_log = tmp_path / "env.txt"
    env = {
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
        "CALL_LOG": str(call_log),
        "ENV_LOG": str(env_log),
        "SSH_TARGET": "deploy@example.test",
        "REPO_DIR": "/opt/custom repo",
    }

    for refused_args in (["deploy"], ["deploy", "--force"], ["deploy", "--force", "--execute"]):
        call_log.unlink(missing_ok=True)
        refused = _run_ops(list(refused_args), env=env)
        assert refused.returncode == 2
        assert "first argument must be --execute" in refused.stderr
        assert not call_log.exists()

    executed = _run_ops(["deploy", "--execute", "arg with spaces"], env=env)

    assert executed.returncode == 0, executed.stderr
    assert _read_nul_args(call_log) == [
        str(REPO_ROOT / "scripts" / "deploy_vps_live.sh"),
        "arg with spaces",
    ]
    assert env_log.read_text(encoding="utf-8").splitlines() == [
        "deploy@example.test",
        "/opt/custom repo",
    ]


@pytest.mark.parametrize(
    ("command", "script", "injected"),
    [
        (
            "clock-offset",
            "scripts/capture_bybit_clock_offset.py",
            [],
        ),
        (
            "demo-calibration",
            "scripts/run_demo_execution_calibration.py",
            ["--confirm-demo-calibration"],
        ),
        (
            "natural-safety-flatten",
            "scripts/publish_natural_safety_flatten.py",
            ["--confirm-demo-safety-flatten"],
        ),
    ],
)
def test_ops_remote_calibration_routes_require_execute_and_preserve_arguments(
    command: str,
    script: str,
    injected: list[str],
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text("#!/bin/sh\nexec /bin/bash -s\n", encoding="utf-8")
    ssh.chmod(0o755)
    remote = tmp_path / "remote repo"
    python = remote / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    _write_capture_command(python)
    call_log = tmp_path / "call.bin"
    env_log = tmp_path / "env.txt"
    env = {
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
        "SSH_TARGET": "operator@example.test",
        "REPO_DIR": str(remote),
        "CALL_LOG": str(call_log),
        "ENV_LOG": str(env_log),
    }

    refused = _run_ops([command, "--output", "x"], env=env)
    assert refused.returncode == 2
    assert "first argument must be --execute" in refused.stderr
    assert not call_log.exists()

    executed = _run_ops(
        [command, "--execute", "--output", "receipt with spaces"],
        env=env,
    )

    assert executed.returncode == 0, executed.stderr
    assert _read_nul_args(call_log) == [
        script,
        *injected,
        "--output",
        "receipt with spaces",
    ]


@pytest.mark.parametrize("args", [["unknown"], ["reconcile"], ["reconcile", "almost"]])
def test_ops_rejects_unknown_or_incomplete_routes(args: list[str]) -> None:
    result = _run_ops(args)

    assert result.returncode == 2
    assert "Usage: scripts/ops.sh" in result.stderr
