"""Qualification and deployment share one commit-bound set of binary bytes."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import platform
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import TextIO

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "release_artifact.py"
COMMIT = "a" * 40
BINARIES = ("engine", "signal-worker", "market-tape")


def _remote_function(name: str) -> str:
    remote = (ROOT / "scripts" / "deploy_vps_live.sh").read_text()
    body = remote[remote.index(f"{name}() {{") :]
    return body[: body.index("\n}\n") + 3]


def _prepare_release(tmp_path: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    release = tmp_path / "release"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "engine").mkdir(exist_ok=True)
    (repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.90.0"\n')
    target = tmp_path / "target"
    function = _remote_function("build_engine").replace("/opt/liquidity-migration-engine", str(release))
    script = f"""
set -euo pipefail
fail() {{ echo "$*" >&2; exit 1; }}
install() {{ mkdir -p "${{@: -1}}"; }}
nice() {{ shift 2; "$@"; }}
cargo() {{ touch "$CARGO_CALLED"; }}
release_artifact() {{ {shlex.quote(sys.executable)} {shlex.quote(str(HELPER))} "$@"; }}
{_remote_function("cleanup_release")}
{function}
build_engine
"""
    return subprocess.run(
        ["bash"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "EXPECTED_COMMIT": COMMIT,
            "REPO_DIR": str(repo),
            "RELEASE_DIR": str(release),
            "CARGO_TARGET_ROOT": str(target),
            "RUST_TOOLCHAIN_DIR": str(tmp_path / "rust"),
            "ENGINE_BINARY": str(release / "bin" / "engine"),
            "SIGNAL_WORKER_BINARY": str(release / "bin" / "signal-worker"),
            "MARKET_TAPE_BINARY": str(release / "bin" / "market-tape"),
            "DEPLOYED_COMMIT_FILE": str(release / "deployed-commit"),
            "CARGO_CALLED": str(tmp_path / "cargo-called"),
            "QUALIFIED_RELEASE_DIR": "",
            **environment,
        },
    )


def _write_tar(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name in BINARIES else 0o644
            archive.addfile(member, io.BytesIO(data))


@pytest.fixture
def artifact_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_artifact", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _qualified_files(commit: str = COMMIT) -> dict[str, bytes]:
    files = {name: f"qualified {name}\n".encode() for name in BINARIES}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["binaries.sha256"] = "".join(f"{hashes[name]}  {name}\n" for name in BINARIES).encode()
    files["qualification.log"] = b"optimized suite and local workloads passed\n"
    files["qualification.json"] = json.dumps(
        {
            "schema_version": 1,
            "commit": commit,
            "profile": "release",
            "rustc": "rustc 1.90.0 (test)",
            "target": "test-host",
            "platform": [platform.system(), platform.machine()],
            "wal_compatibility": "not_assessed",
            "checks": ["release-tests", "account-state-soak", "engine-bench", "binary-smoke"],
            "binaries": hashes,
            "log_sha256": hashlib.sha256(files["qualification.log"]).hexdigest(),
        }
    ).encode()
    return files


@pytest.fixture
def qualification_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact_module: ModuleType
) -> tuple[Path, str, Path, list[list[str]], dict[str, str]]:
    repo = tmp_path / "source"
    (repo / "engine").mkdir(parents=True)
    (repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.90.0"\n')
    (repo / "engine" / "Cargo.toml").write_text("# fixture source\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Fixture source",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    rustc = tmp_path / "rustc"
    rustc.write_text("#!/bin/sh\nprintf 'rustc 1.90.0 (fixture)\\nhost: fixture-host\\n'\n")
    rustc.chmod(0o755)
    monkeypatch.setenv("RUSTC", str(rustc))
    target = tmp_path / "target"
    release = target / "fixture-host" / "release"
    calls: list[list[str]] = []
    behavior: dict[str, str] = {}

    def run(command: list[str], cwd: Path, log: TextIO, source_commit: str) -> None:
        assert source_commit == commit
        calls.append(command)
        log.write(shlex.join(command) + "\n")
        phase = command[1] if command[0] == "cargo" else Path(command[0]).name
        if phase == "build":
            release.mkdir(parents=True)
            for name in BINARIES:
                binary = release / name
                binary.write_bytes(f"{name} at {commit}\n".encode())
                binary.chmod(0o755)
        if phase == behavior.get("fail"):
            raise subprocess.CalledProcessError(7, command)
        if phase == "market-tape":
            if behavior.get("mutate") == "binary":
                (release / "engine").write_bytes(b"replaced after tests")
            elif behavior.get("mutate") == "source":
                (repo / "engine" / "Cargo.toml").write_text("changed source after tests\n")

    monkeypatch.setattr(artifact_module, "_run", run)
    return repo, commit, target, calls, behavior


def test_artifact_producer_qualifies_before_upload_for_deploy_and_qualify() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/vps-deploy.yml").read_text())
    job = workflow["jobs"]["rust-artifact"]
    assert "inputs.mode == 'deploy'" in job["if"]
    assert "inputs.mode == 'qualify'" in job["if"]
    steps = job["steps"]
    upload = next(i for i, step in enumerate(steps) if "actions/upload-artifact@" in step.get("uses", ""))
    qualify = next(i for i, step in enumerate(steps) if "scripts/release_artifact.py qualify" in step.get("run", ""))
    assert qualify < upload
    assert not steps[qualify].get("continue-on-error", False)
    assert "rust-soak-bench" not in workflow["jobs"]


def test_deploy_missing_qualified_artifact_never_compiles_on_host(tmp_path: Path) -> None:
    result = _prepare_release(tmp_path)
    assert result.returncode != 0, result.stdout
    assert not (tmp_path / "cargo-called").exists()
    assert "qualified" in result.stderr


def test_deploy_refuses_legacy_checksums_without_qualification(tmp_path: Path) -> None:
    files = {name: f"old {name}\n".encode() for name in BINARIES}
    files["binaries.sha256"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()
    ).encode()
    _write_tar(tmp_path / "release" / "staged" / f"{COMMIT}.tar.gz", files)
    result = _prepare_release(tmp_path)
    assert result.returncode != 0, result.stdout
    assert "qualification" in result.stderr


def test_verified_unpack_preserves_exact_qualified_bytes(tmp_path: Path, artifact_module: ModuleType) -> None:
    files = _qualified_files()
    bundle = tmp_path / "release.tar.gz"
    _write_tar(bundle, files)
    output = tmp_path / "unpacked"
    manifest = artifact_module.verify(bundle, COMMIT, output)
    assert manifest["commit"] == COMMIT
    assert {path.name: path.read_bytes() for path in output.iterdir()} == files
    assert all(os.access(output / name, os.X_OK) for name in BINARIES)


@pytest.mark.parametrize("change", ["commit", "profile", "checks", "binary", "checksum", "log", "missing", "wal_scope"])
def test_verifier_refuses_mismatched_or_incomplete_artifacts(
    tmp_path: Path, artifact_module: ModuleType, change: str
) -> None:
    files = _qualified_files()
    manifest = json.loads(files["qualification.json"])
    if change in ("commit", "profile", "checks"):
        manifest[change] = {"commit": "b" * 40, "profile": "dev", "checks": ["release-tests"]}[change]
        files["qualification.json"] = json.dumps(manifest).encode()
    elif change == "binary":
        files["engine"] += b"changed after qualification"
    elif change == "checksum":
        files["binaries.sha256"] = b"unrelated checksums\n"
    elif change == "log":
        files["qualification.log"] += b"changed evidence\n"
    elif change == "wal_scope":
        manifest["wal_compatibility"] = "certified"
        files["qualification.json"] = json.dumps(manifest).encode()
    else:
        del files["market-tape"]
    bundle = tmp_path / "release.tar.gz"
    _write_tar(bundle, files)
    output = tmp_path / "unpacked"
    with pytest.raises(ValueError):
        artifact_module.verify(bundle, COMMIT, output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "traversal", "non-executable"])
def test_archive_members_cannot_replace_or_escape_verified_files(
    tmp_path: Path, artifact_module: ModuleType, kind: str
) -> None:
    bundle = tmp_path / "release.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, data in _qualified_files().items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name in BINARIES else 0o644
            if kind == "non-executable" and name == "engine":
                member.mode = 0o644
            if kind == "symlink" and name == "engine":
                member.type = tarfile.SYMTYPE
                member.linkname = "../outside"
                member.size = 0
            archive.addfile(member, io.BytesIO(data))
        if kind in ("duplicate", "traversal"):
            member = tarfile.TarInfo("engine" if kind == "duplicate" else "../outside")
            archive.addfile(member)
    with pytest.raises(ValueError):
        artifact_module.verify(bundle, COMMIT, tmp_path / "unpacked")
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "unpacked").exists()


def test_unpack_does_not_reuse_stale_files(tmp_path: Path, artifact_module: ModuleType) -> None:
    bundle = tmp_path / "release.tar.gz"
    _write_tar(bundle, _qualified_files())
    output = tmp_path / "unpacked"
    output.mkdir()
    (output / "engine").write_bytes(b"old build")
    with pytest.raises(ValueError, match="must be empty"):
        artifact_module.verify(bundle, COMMIT, output)
    assert (output / "engine").read_bytes() == b"old build"


def test_preparation_requires_incumbent_qualification_for_rollback(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _write_tar(release / "staged" / f"{COMMIT}.tar.gz", _qualified_files())
    (release / "bin").mkdir()
    (release / "bin" / "engine").write_bytes(b"incumbent")
    incumbent = "b" * 40
    (release / "deployed-commit").write_text(incumbent + "\n")
    refused = _prepare_release(tmp_path)
    assert refused.returncode != 0
    assert "qualified rollback artifact" in refused.stderr
    assert not list((release / "staged").glob(".qualified.*"))
    _write_tar(release / "staged" / f"{incumbent}.tar.gz", _qualified_files(incumbent))
    accepted = _prepare_release(tmp_path)
    assert accepted.returncode == 0, accepted.stderr
    extracted = list((release / "staged").glob(".qualified.*"))
    assert len(extracted) == 1
    assert (extracted[0] / "engine").read_bytes() == _qualified_files()["engine"]
    assert (release / "bin" / "engine").read_bytes() == b"incumbent"


def test_automatic_rollback_requires_original_qualified_artifact(tmp_path: Path) -> None:
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    for name in BINARIES:
        (release / "bin" / f"{name}.previous").write_bytes(b"unqualified cache")
    refused = _prepare_release(tmp_path, AUTO_ROLLBACK="1")
    assert refused.returncode != 0
    assert not (tmp_path / "cargo-called").exists()
    _write_tar(release / "staged" / f"{COMMIT}.tar.gz", _qualified_files())
    accepted = _prepare_release(tmp_path, AUTO_ROLLBACK="1")
    assert accepted.returncode == 0, accepted.stderr


def test_rollback_verifier_survives_checkout_without_the_helper(tmp_path: Path) -> None:
    bundle = tmp_path / "release.tar.gz"
    _write_tar(bundle, _qualified_files())
    result = subprocess.run(
        ["bash"],
        input=_remote_function("release_artifact")
        + '\nrelease_artifact verify --artifact "$ARTIFACT" --commit "$COMMIT"',
        env={**os.environ, "RELEASE_ARTIFACT_PY": HELPER.read_text(), "ARTIFACT": str(bundle), "COMMIT": COMMIT},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["commit"] == COMMIT


def test_empty_verifier_cannot_qualify_a_release(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash"],
        input='fail() { echo "$*" >&2; exit 1; }\n'
        + _remote_function("release_artifact")
        + "\nrelease_artifact verify",
        env={**os.environ, "RELEASE_ARTIFACT_PY": ""},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "missing release artifact verifier" in result.stderr


def test_qualification_preflight_runs_before_remote_checkout_or_install() -> None:
    deploy = _remote_function("deploy_mode")
    assert deploy.index("build_engine") < deploy.index("fetch_exact_commit")
    assert deploy.index("build_engine") < deploy.index("install_release")
    install = _remote_function("install_release")
    assert "$QUALIFIED_RELEASE_DIR/engine" in install
    assert "$QUALIFIED_RELEASE_DIR/signal-worker" in install
    assert "$QUALIFIED_RELEASE_DIR/market-tape" in install
    assert ".previous" not in install


def test_qualification_packages_the_tested_native_bytes_without_rebuilding(
    tmp_path: Path,
    artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, _ = qualification_workspace
    output = tmp_path / "qualified.tar.gz"
    artifact_module.qualify(repo, commit, output, target)
    manifest = artifact_module.verify(output, commit, tmp_path / "verified")
    assert manifest["checks"] == ["release-tests", "account-state-soak", "engine-bench", "binary-smoke"]
    assert manifest["target"] == "fixture-host"
    assert manifest["wal_compatibility"] == "not_assessed"
    cargo_calls = [command for command in calls if command[0] == "cargo"]
    assert [command[1] for command in cargo_calls] == ["build", "test"]
    for command in cargo_calls:
        assert "--release" in command and "--locked" in command
        assert command[command.index("--target-dir") + 1] == str(target)
        assert command[command.index("--target") + 1] == "fixture-host"
    assert "--all-targets" in cargo_calls[1]
    release = target / "fixture-host" / "release"
    bench = next(command for command in calls if len(command) > 1 and command[1] == "bench")
    assert bench[0] == str(release / "engine")
    for name in BINARIES:
        assert (tmp_path / "verified" / name).read_bytes() == (release / name).read_bytes()


@pytest.mark.parametrize("phase", ["test", "account_state_soak", "engine", "signal-worker", "market-tape"])
def test_failed_qualification_cannot_publish_an_artifact(
    tmp_path: Path,
    artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
    phase: str,
) -> None:
    repo, commit, target, _, behavior = qualification_workspace
    behavior["fail"] = phase
    output = tmp_path / "qualified.tar.gz"
    with pytest.raises(subprocess.CalledProcessError):
        artifact_module.qualify(repo, commit, output, target)
    assert not output.exists()
    assert not list(tmp_path.glob(".qualified.tar.gz.*"))


@pytest.mark.parametrize("mutation", ["binary", "source"])
def test_source_and_binary_mutation_during_qualification_is_rejected(
    tmp_path: Path,
    artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
    mutation: str,
) -> None:
    repo, commit, target, _, behavior = qualification_workspace
    behavior["mutate"] = mutation
    output = tmp_path / "qualified.tar.gz"
    with pytest.raises(ValueError, match="changed during qualification|clean checkout"):
        artifact_module.qualify(repo, commit, output, target)
    assert not output.exists()


def test_wrong_commit_and_dirty_checkout_do_not_start_builds(
    tmp_path: Path,
    artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, _ = qualification_workspace
    with pytest.raises(ValueError, match="clean checkout"):
        artifact_module.qualify(repo, COMMIT, tmp_path / "wrong-commit.tar.gz", target)
    (repo / "untracked.rs").write_text("uncommitted source\n")
    with pytest.raises(ValueError, match="clean checkout"):
        artifact_module.qualify(repo, commit, tmp_path / "dirty.tar.gz", target)
    assert not calls


def test_wrong_compiler_cannot_qualify_a_release(
    tmp_path: Path,
    artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, _ = qualification_workspace
    (tmp_path / "rustc").write_text("#!/bin/sh\nprintf 'rustc 1.97.0 (fixture)\\nhost: fixture-host\\n'\n")
    with pytest.raises(ValueError, match="requires Rust 1.90.0"):
        artifact_module.qualify(repo, commit, tmp_path / "wrong-compiler.tar.gz", target)
    assert not calls


def test_local_qualification_can_be_inspected_but_not_installed_on_another_platform(
    tmp_path: Path,
    artifact_module: ModuleType,
) -> None:
    files = _qualified_files()
    manifest = json.loads(files["qualification.json"])
    manifest["platform"] = ["another operating system", "another architecture"]
    files["qualification.json"] = json.dumps(manifest).encode()
    output = tmp_path / "qualified.tar.gz"
    _write_tar(output, files)
    assert artifact_module.verify(output, COMMIT)["commit"] == COMMIT
    with pytest.raises(ValueError, match="platform does not match"):
        artifact_module.verify(output, COMMIT, tmp_path / "unpacked")
    with pytest.raises(ValueError, match="platform does not match"):
        artifact_module.verify(output, COMMIT, require_platform=True)


def test_failed_subprocess_is_recorded_and_binds_engine_build_commit(artifact_module: ModuleType) -> None:
    log = io.StringIO()
    with pytest.raises(subprocess.CalledProcessError) as error:
        artifact_module._run(
            [sys.executable, "-c", "import os; print(os.environ['ENGINE_GIT_COMMIT']); raise SystemExit(7)"],
            ROOT,
            log,
            COMMIT,
        )
    assert error.value.returncode == 7
    assert COMMIT in log.getvalue()


def _stage_download(tmp_path: Path, files: dict[str, bytes], **environment: str) -> subprocess.CompletedProcess[str]:
    bundle = tmp_path / "download.tar.gz"
    _write_tar(bundle, files)
    return subprocess.run(
        ["bash"],
        input="""
set -euo pipefail
SSH_ARGS=()
ssh() {
    local operation="${@: -1}"
    case "$operation" in
        "test -f "*) return 1 ;;
        *) printf 'ssh %s\n' "$operation" >> "$OPERATIONS" ;;
    esac
}
scp() { printf 'scp %s\n' "$*" >> "$OPERATIONS"; }
gh() {
    case "$1 $2" in
        "api "*) printf '123\n' ;;
        "run view") printf '%s\n' "$RUN_IDENTITY" ;;
        "run download") cp "$BUNDLE" "${@: -1}/engine-binaries-$COMMIT.tar.gz" ;;
        *) return 9 ;;
    esac
}
"""
        + _remote_function("stage_qualified_binaries")
        + '\nstage_qualified_binaries "$COMMIT" fixture-host',
        env={
            **os.environ,
            "LOCAL_REPOSITORY": str(ROOT),
            "COMMIT": COMMIT,
            "BUNDLE": str(bundle),
            "OPERATIONS": str(tmp_path / "operations"),
            "RUN_IDENTITY": f"{COMMIT} success",
            **environment,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_download_stages_only_verified_bytes_and_publishes_after_copy(tmp_path: Path) -> None:
    result = _stage_download(tmp_path, _qualified_files())
    assert result.returncode == 0, result.stderr
    operations = (tmp_path / "operations").read_text().splitlines()
    assert len(operations) == 3
    assert operations[0].startswith("ssh mkdir")
    assert operations[1].startswith("scp ") and operations[1].endswith(".tar.gz.partial")
    assert operations[2].startswith("ssh mv -f ")
    assert operations[2].endswith(f"/{COMMIT}.tar.gz'")


@pytest.mark.parametrize("identity", [f"{'b' * 40} success", f"{COMMIT} failure", f"{COMMIT} null"])
def test_download_refuses_unrelated_or_unsuccessful_runs_before_staging(tmp_path: Path, identity: str) -> None:
    result = _stage_download(tmp_path, _qualified_files(), RUN_IDENTITY=identity)
    assert result.returncode != 0
    assert "not successful" in result.stderr
    assert not (tmp_path / "operations").exists()


def test_unqualified_download_is_rejected_before_staging(tmp_path: Path) -> None:
    files = _qualified_files()
    del files["qualification.json"]
    result = _stage_download(tmp_path, files)
    assert result.returncode != 0
    assert "could not stage a qualified artifact" in result.stderr
    assert not (tmp_path / "operations").exists()
