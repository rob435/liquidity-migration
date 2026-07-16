from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(OPS), *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def test_help_lists_only_current_operator_routes() -> None:
    result = _run("help")
    assert result.returncode == 0
    for command in (
        "status",
        "equity",
        "reset",
        "clock-offset",
        "operational-authority",
        "venue-accounting",
        "test",
        "deploy",
    ):
        assert command in result.stdout


def test_unknown_command_fails_with_usage() -> None:
    result = _run("does-not-exist")
    assert result.returncode == 2
    assert "unknown command" in result.stderr
    assert "Usage:" in result.stderr


def test_reset_defaults_to_remote_dry_run(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\ncat > \"$CAPTURE\"\nprintf '%s\\n' \"$*\" >> \"$CAPTURE\"\n",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    result = _run(
        "reset",
        "--scope",
        "long",
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "CAPTURE": str(capture)},
    )
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "--dry-run" in payload
    assert "reset_demo_paper_ledgers.sh" in payload
    assert "--scope long" in payload


def test_reset_execute_is_forwarded_without_added_dry_run(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)
    result = _run(
        "reset",
        "--execute",
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "CAPTURE": str(capture)},
    )
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "--execute" in payload
    assert "--dry-run" not in payload


def test_mutating_remote_routes_require_explicit_handshake() -> None:
    assert _run("clock-offset").returncode == 2
    assert _run("operational-authority", "issue").returncode == 2
    assert _run("deploy", "install").returncode == 2
    assert _run("deploy", "--execute", "verify").returncode == 2


def test_operational_authority_defaults_to_remote_verification(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)

    result = _run(
        "operational-authority",
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "CAPTURE": str(capture)},
    )

    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "MODULE_ARGS=( verify --repo-root /opt/liquidity-migration )" in payload


def test_deploy_accepts_only_install_or_activate_after_handshake(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\ncat >/dev/null\nprintf '%s\\n' \"$*\" > \"$CAPTURE\"\n",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    commit = "a" * 40
    for mode in ("install", "activate"):
        result = _run(
            "deploy",
            "--execute",
            mode,
            env={
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "CAPTURE": str(capture),
                "EXPECTED_COMMIT": commit,
            },
        )
        assert result.returncode == 0, result.stderr
