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
    assert _run("deploy", "install").returncode == 2
    assert _run("deploy", "--execute", "verify").returncode == 2
    assert _run("deploy", "--execute", "rollout").returncode == 2
    assert _run("deploy", "--execute", "recover").returncode == 2


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


def test_rollout_requires_and_serializes_an_explicit_profile(
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
        base,
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert incomplete.returncode == 2
    assert "--profile" in incomplete.stderr
    assert not capture.exists()

    complete = subprocess.run(
        [
            *base,
            "--profile",
            "operational",
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


def test_recovery_requires_and_serializes_exact_reset_receipt(
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
        "recover",
        "--profile",
        "operational",
    ]
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "EXPECTED_COMMIT": commit,
        "GITHUB_TOKEN": "test-token",
    }

    incomplete = subprocess.run(
        base,
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert incomplete.returncode == 2
    assert "reset-receipt" in incomplete.stderr
    assert not capture.exists()

    complete = subprocess.run(
        [*base, "--reset-receipt", "/var/lib/liquidity-migration/reset.json"],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert complete.returncode == 0, complete.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "MODE=recover" in payload
    assert "DEPLOY_RESET_RECEIPT=/var/lib/liquidity-migration/reset.json" in payload


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
    remote_start = deploy.index("set -Eeuo pipefail", deploy.index("cat <<'REMOTE_SCRIPT'"))
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


def test_remote_helpers_tolerate_an_empty_argument_array_under_set_u() -> None:
    """`"${arr[@]}"` on an EMPTY array is an unbound-variable error under
    `set -u` on Bash 3.2 (the operator laptops this repo deliberately supports),
    which broke the documented no-argument `ops.sh reset` preview and bare
    `clock-offset --execute`. The portable guard idiom was already used three
    lines earlier in the same function (2026-07-27 audit L11)."""

    text = (Path(__file__).resolve().parents[1] / "scripts" / "ops.sh").read_text(encoding="utf-8")
    for array in ("reset_args", "script_args", "module_args"):
        assert f'"${{{array}[@]}}"' not in text.replace(f'${{{array}[@]+"${{{array}[@]}}"}}', "")
        assert f'${{{array}[@]+"${{{array}[@]}}"}}' in text

    # The guarded expansion really is empty-safe.
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail\n'
            'declare -a a=()\n'
            'for x in ${a[@]+"${a[@]}"}; do echo "unexpected $x"; done\n'
            'b=(--dry-run ${a[@]+"${a[@]}"})\n'
            'printf "%s\\n" "${b[@]}"\n',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "--dry-run"


def test_authenticated_fetch_keeps_the_github_token_off_argv() -> None:
    """`GIT_ENV` begins with `/usr/bin/env -i`, so a `GIT_CONFIG_VALUE_0=...`
    prefix is an argv word of env and is world-readable via /proc for the
    fork-exec window — carrying the operator's long-lived `gh auth token` on a
    local dispatch (2026-07-27 audit L16)."""

    deploy = (
        Path(__file__).resolve().parents[1] / "scripts" / "deploy_vps_live.sh"
    ).read_text(encoding="utf-8")
    fetch = deploy[deploy.index("git_fetch() {") : deploy.index("invalidate_operational_authorization() {")]
    code = "\n".join(
        line for line in fetch.splitlines() if not line.lstrip().startswith("#")
    )
    assert "GIT_CONFIG_VALUE_0=" not in code
    assert "GIT_CONFIG_KEY_0=" not in code
    assert "GIT_CONFIG_GLOBAL=" in fetch
    assert 'chmod 0600 "$config_file"' in fetch
    assert 'rm -f "$config_file"' in fetch
    # The credential reaches the file through a shell builtin, never a process
    # argument list.
    assert 'printf \'[http "https://github.com/"]' in fetch
