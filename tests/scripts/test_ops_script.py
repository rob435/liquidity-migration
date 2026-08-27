from __future__ import annotations

import os
import shutil
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
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
        "units",
        "logs",
        "restart",
        "stop",
        "start",
        "equity",
        "research-refresh",
        "flatten",
        "real-money",
        "deploy",
    ):
        assert command in result.stdout
    assert "reset" not in result.stdout


def test_unknown_command_fails_with_usage() -> None:
    result = _run("does-not-exist")
    assert result.returncode == 2
    assert "unknown command" in result.stderr
    assert "Usage:" in result.stderr


def test_deploy_allowlists_modes_and_no_longer_demands_execute() -> None:
    """`--execute` was a typing tax, not a gate: the deploy script has its own mode
    allowlist and every real gate lives on the host. It is still accepted so the
    documented command lines keep working, but the allowlist is what refuses.
    """

    # `verify` is `status`, not a deploy mode; the rest are not modes at all.
    for rejected in (
        ("deploy", "verify"),
        ("deploy", "--execute", "verify"),
        ("deploy", "activate-mainnnet"),
        ("deploy", "stop"),
        ("deploy",),
    ):
        result = _run(*rejected)
        assert result.returncode == 2, rejected
        assert "deploy mode must be" in result.stderr, rejected


def _ssh_capture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    capture = tmp_path / "capture"
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)
    return capture, {"PATH": f"{tmp_path}:{os.environ['PATH']}", "CAPTURE": str(capture)}


def test_flatten_payload_hands_its_arguments_to_the_remote_script(tmp_path: Path) -> None:
    """The bug this pins: the flatten script body was the bare script path, so the
    remote host ran flatten_account.sh with zero argv and it refused every call —
    the args were serialized into REMOTE_ARGS and then never consumed.
    """

    capture, environment = _ssh_capture(tmp_path)
    result = _run("flatten", "--environment", "demo", env=environment)
    assert result.returncode == 0, result.stderr
    payload = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( --dry-run --environment demo )" in payload
    # The script body must pass the array on, not just name the script.
    assert 'flatten_account.sh" "${REMOTE_ARGS[@]}"' in payload

    execute = _run("flatten", "--environment", "demo", "--execute", env=environment)
    assert execute.returncode == 0, execute.stderr
    executed = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( --environment demo --execute )" in executed
    assert "--dry-run" not in executed


def test_unit_verbs_reach_systemd_and_qualify_short_names(tmp_path: Path) -> None:
    capture, environment = _ssh_capture(tmp_path)

    assert _run("units", env=environment).returncode == 0
    payload = capture.read_text(encoding="utf-8")
    assert "systemctl list-units 'liquidity-migration-*'" in payload
    assert "systemctl list-timers 'liquidity-migration-*'" in payload

    assert _run("logs", "bybit-carry-demo.service", env=environment).returncode == 0
    payload = capture.read_text(encoding="utf-8")
    assert "REMOTE_ARGS=( liquidity-migration-bybit-carry-demo.service 100 )" in payload
    assert "journalctl -u" in payload

    assert (
        _run("logs", "liquidity-migration-engine.service", "40", env=environment)
        .returncode
        == 0
    )
    payload = capture.read_text(encoding="utf-8")
    # An already-qualified name is not prefixed twice.
    assert "REMOTE_ARGS=( liquidity-migration-engine.service 40 )" in payload

    for verb in ("restart", "stop", "start"):
        assert _run(verb, "bybit-long-demo.service", env=environment).returncode == 0
        payload = capture.read_text(encoding="utf-8")
        assert f'exec systemctl {verb} "${{REMOTE_ARGS[@]}}"' in payload
        assert "REMOTE_ARGS=( liquidity-migration-bybit-long-demo.service )" in payload
        assert _run(verb, env=environment).returncode == 2


def test_real_money_allowlist_covers_the_arming_subcommands() -> None:
    rejected = _run("real-money", "arm-it")
    assert rejected.returncode == 2
    assert "preflight or render-profile" in rejected.stderr
    help_text = _run("help").stdout
    assert "create-state-roots" not in help_text
    assert "stop-mainnet" in help_text
    assert "activate-mainnet" not in help_text


def _deploy_harness(tmp_path: Path) -> tuple[Path, str, Path, dict[str, str]]:
    """An isolated checkout plus a stub ssh that captures the remote payload."""

    checkout, commit = _isolated_deploy_checkout(tmp_path)
    capture = tmp_path / "capture"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/usr/bin/env bash\ncat > \"$CAPTURE\"\n", encoding="utf-8")
    ssh.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "EXPECTED_COMMIT": commit,
        "GITHUB_TOKEN": "test-token",
    }
    return checkout, commit, capture, environment


def test_deploy_forwards_every_mode_without_an_execute_handshake(tmp_path: Path) -> None:
    checkout, _commit, capture, environment = _deploy_harness(tmp_path)
    for mode in ("install", "activate", "stop-mainnet"):
        result = subprocess.run(
            ["bash", str(checkout / "scripts/ops.sh"), "deploy", mode],
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert f"MODE={mode}" in capture.read_text(encoding="utf-8")


def test_deploy_still_accepts_and_discards_a_leading_execute(tmp_path: Path) -> None:
    checkout, _commit, capture, environment = _deploy_harness(tmp_path)
    payloads = []
    for argv in (["deploy", "--execute", "install"], ["deploy", "install"]):
        result = subprocess.run(
            ["bash", str(checkout / "scripts/ops.sh"), *argv],
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payloads.append(capture.read_text(encoding="utf-8"))
    # The compatibility word is consumed, never forwarded as a deploy argument:
    # the two remote programs are byte-identical.
    assert payloads[0] == payloads[1]
    assert "MODE=install" in payloads[0]


def test_deploy_forwards_staged_with_its_profile(tmp_path: Path) -> None:
    checkout, _commit, capture, environment = _deploy_harness(tmp_path)
    incomplete = subprocess.run(
        ["bash", str(checkout / "scripts/ops.sh"), "deploy", "staged"],
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
            "bash",
            str(checkout / "scripts/ops.sh"),
            "deploy",
            "staged",
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
    assert "MODE=staged" in payload
    assert "DEPLOY_PROFILE=operational" in payload


def test_expected_commit_defaults_to_the_known_branch_tip(tmp_path: Path) -> None:
    """An unset EXPECTED_COMMIT is the common case; it defaults to the remote-tracking
    tip, or to HEAD when there is none. The host still refuses any commit that is not
    an ancestor of the branch, so the default cannot deploy an unpushed commit.
    """

    checkout, commit, capture, environment = _deploy_harness(tmp_path)
    del environment["EXPECTED_COMMIT"]

    without_remote = subprocess.run(
        ["bash", str(checkout / "scripts/ops.sh"), "deploy", "install"],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert without_remote.returncode == 0, without_remote.stderr
    assert f"EXPECTED_COMMIT={commit}" in capture.read_text(encoding="utf-8")
    assert "EXPECTED_COMMIT_EXPLICIT=0" in capture.read_text(encoding="utf-8")
    assert "(HEAD)" in without_remote.stderr

    # A remote-tracking ref wins over HEAD.
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-q", "--allow-empty", "-m", "later"],
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", commit],
        check=True,
    )
    with_remote = subprocess.run(
        ["bash", str(checkout / "scripts/ops.sh"), "deploy", "install"],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert with_remote.returncode == 0, with_remote.stderr
    assert f"EXPECTED_COMMIT={commit}" in capture.read_text(encoding="utf-8")
    assert "(origin/main)" in with_remote.stderr


def test_an_explicit_expected_commit_is_still_validated(tmp_path: Path) -> None:
    checkout, _commit, capture, environment = _deploy_harness(tmp_path)
    result = subprocess.run(
        ["bash", str(checkout / "scripts/ops.sh"), "deploy", "install"],
        cwd=checkout,
        env={**environment, "EXPECTED_COMMIT": "not-a-commit"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "EXPECTED_COMMIT must be a full lowercase 40-character commit" in result.stderr
    assert not capture.exists()


def test_rollout_requires_and_serializes_an_explicit_profile(
    tmp_path: Path,
) -> None:
    checkout, _commit, capture, environment = _deploy_harness(tmp_path)
    base = [
        "bash",
        str(checkout / "scripts/ops.sh"),
        "deploy",
        "rollout",
    ]

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
        ["bash", str(checkout / "scripts/ops.sh"), "deploy", "install"],
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
    helper = checkout / "scripts/deploy_vps_live.sh"
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "update-index",
            index_flag,
            "--",
            "scripts/deploy_vps_live.sh",
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
    """``"${arr[@]}"`` on an EMPTY array is an unbound-variable error under ``set -u`` on
    Bash 3.2, which this repo supports; the portable guard idiom is used three lines
    earlier in the same function.
    """

    text = (Path(__file__).resolve().parents[2] / "scripts" / "ops.sh").read_text(encoding="utf-8")
    for array in ("remote_args",):
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
            'printf "%s\\n" ${a[@]+"${a[@]}"}\n',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_authenticated_fetch_keeps_the_github_token_off_argv() -> None:
    """``GIT_ENV`` begins with ``/usr/bin/env -i``, so a ``GIT_CONFIG_VALUE_0=...`` prefix
    is an argv word of env and is world-readable via /proc for the fork-exec window.
    """

    deploy = (
        Path(__file__).resolve().parents[2] / "scripts" / "deploy_vps_live.sh"
    ).read_text(encoding="utf-8")
    fetch = deploy[
        deploy.index("git_fetch() {") : deploy.index("install_mode() {")
    ]
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
