from __future__ import annotations

import os
import shutil
import shlex
import subprocess
from pathlib import Path

import pytest


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


def _isolated_deploy_checkout(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "checkout"
    for relative in (
        Path("scripts/ops.sh"),
        Path("scripts/deploy_vps_live.sh"),
        Path("scripts/check_deploy_rollout_readiness.py"),
        Path("liquidity_migration/maintenance_lock.py"),
    ):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-q", "-m", "test deploy fixture"],
        env=commit_env,
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return checkout, commit


def test_help_lists_only_current_operator_routes() -> None:
    result = _run("help")
    assert result.returncode == 0
    for command in (
        "status",
        "equity",
        "research-refresh",
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
    assert _run("deploy", "--execute", "rollout").returncode == 2


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


def test_operational_authority_issue_locks_before_opening_remote_checkout(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)

    result = _run(
        "operational-authority",
        "--execute",
        "issue",
        "--expected-commit",
        "a" * 40,
        "--repo-root",
        "/opt/liquidity-migration",
        "--authorization-reference",
        "owner task",
        "--owner-acknowledgement",
        "AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION",
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "CAPTURE": str(capture)},
    )

    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    canonical_lock = payload.index("/usr/bin/flock -n 9")
    checkout_open = payload.index('cd "$REPO_DIR"')
    module_exec = payload.index(
        'exec "$PYTHON" -m liquidity_migration.operational_runtime_authority'
    )
    assert canonical_lock < checkout_open < module_exec
    assert "/usr/bin/flock -n 8" in payload
    assert "/usr/bin/flock -n 7" in payload
    assert "prepare-host" in payload
    assert "acquire-inherited" in payload
    assert "LIQUIDITY_MIGRATION_MAINTENANCE_LOCK_FDS=9,8,7" in payload
    assert "AUTHORITY_ARGS=( issue --expected-commit" in payload


def test_deploy_forwards_install_and_activate_after_valid_handshake(tmp_path: Path) -> None:
    checkout, commit = _isolated_deploy_checkout(tmp_path)
    capture = tmp_path / "capture"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n",
        encoding="utf-8",
    )
    ssh.chmod(0o700)
    for mode in ("install", "activate"):
        result = subprocess.run(
            ["bash", str(checkout / "scripts/ops.sh"), "deploy", "--execute", mode],
            cwd=checkout,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "CAPTURE": str(capture),
                "EXPECTED_COMMIT": commit,
                "GITHUB_TOKEN": "test-token",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert f"MODE={mode}" in capture.read_text(encoding="utf-8")


def test_rollout_requires_and_serializes_explicit_operational_authority(
    tmp_path: Path,
) -> None:
    checkout, commit = _isolated_deploy_checkout(tmp_path)
    capture = tmp_path / "capture"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)
    base = [
        "bash",
        str(checkout / "scripts/ops.sh"),
        "deploy",
        "--execute",
        "rollout",
    ]
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "EXPECTED_COMMIT": commit,
        "GITHUB_TOKEN": "test-token",
    }

    incomplete = subprocess.run(
        [*base, "--profile", "operational"],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert incomplete.returncode == 2
    assert "authorization-reference" in incomplete.stderr
    assert not capture.exists()

    complete = subprocess.run(
        [
            *base,
            "--profile",
            "operational",
            "--authorization-reference",
            "owner task: bounded rollout",
            "--owner-acknowledgement",
            "AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION",
        ],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert complete.returncode == 0, complete.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "MODE=rollout" in payload
    assert "DEPLOY_PROFILE=operational" in payload
    assert "DEPLOY_AUTHORIZATION_REFERENCE=owner\\ task:\\ bounded\\ rollout" in payload
    assert (
        "DEPLOY_OWNER_ACKNOWLEDGEMENT="
        "AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION"
    ) in payload


def test_deploy_rejects_tree_object_before_ssh(tmp_path: Path) -> None:
    checkout, _commit = _isolated_deploy_checkout(tmp_path)
    tree = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    capture = tmp_path / "capture"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)

    result = subprocess.run(
        ["bash", str(checkout / "scripts/ops.sh"), "deploy", "--execute", "install"],
        cwd=checkout,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "EXPECTED_COMMIT": tree,
            "GITHUB_TOKEN": "test-token",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"EXPECTED_COMMIT is not a local commit object: {tree}" in result.stderr
    assert not capture.exists()


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_remote_clean_check_ignores_current_index_flags(
    tmp_path: Path,
    index_flag: str,
) -> None:
    checkout, commit = _isolated_deploy_checkout(tmp_path)
    helper = checkout / "liquidity_migration/maintenance_lock.py"
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "update-index",
            index_flag,
            "--",
            "liquidity_migration/maintenance_lock.py",
        ],
        check=True,
    )
    helper.write_text(helper.read_text(encoding="utf-8") + "\n# hidden mutation\n", encoding="utf-8")

    deploy = (checkout / "scripts/deploy_vps_live.sh").read_text(encoding="utf-8")
    remote_start = deploy.index("set -euo pipefail", deploy.index("cat <<'REMOTE_SCRIPT'"))
    remote_end = deploy.index("require_quiescent()", remote_start)
    probe_source = deploy[remote_start:remote_end]
    index_root = tmp_path / "deploy-index"
    index_root.mkdir(mode=0o700)
    probe_source = probe_source.replace(
        "/run/liquidity-migration/deploy-index.XXXXXX",
        f"{index_root}/deploy-index.XXXXXX",
    )
    probe = (
        f"REPO_DIR={shlex.quote(str(checkout))}\n"
        f"EXPECTED_COMMIT={commit}\n"
        f"{probe_source}\n"
        f"require_clean_checkout_at {commit} test-probe\n"
    )

    result = subprocess.run(
        ["bash", "-c", probe],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "checkout is dirty before test-probe" in result.stderr
    assert list(index_root.iterdir()) == []
