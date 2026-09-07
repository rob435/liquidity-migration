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
HELPER = Path(os.environ.get("R3_QUALIFICATION_SOURCE", str(ROOT / "scripts" / "release_artifact.py")))
COMMIT = "a" * 40
BINARIES = ("engine", "engine-tools", "signal-worker")


def _remote_function(name: str) -> str:
    script = "deploy_vps_live.sh" if name == "stage_release_binaries" else "vps/deploy_remote.sh"
    remote = (ROOT / "scripts" / script).read_text()
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
            "ENGINE_TOOLS_BINARY": str(release / "bin" / "engine-tools"),
            "SIGNAL_WORKER_BINARY": str(release / "bin" / "signal-worker"),
            "DEPLOYED_COMMIT_FILE": str(release / "deployed-commit"),
            "CARGO_CALLED": str(tmp_path / "cargo-called"),
            "QUALIFIED_RELEASE_DIR": "",
            "INCUMBENT_STAGE": "",
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact_module: ModuleType, request: pytest.FixtureRequest,
) -> tuple[Path, str, Path, list[list[str]], dict[str, str]]:
    decision_baseline, submit_baseline, *paired = getattr(request, "param", (40000, 4000000))
    repo = tmp_path / "source"
    (repo / "engine").mkdir(parents=True)
    (repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.90.0"\n')
    (repo / "engine" / "Cargo.toml").write_text("# fixture source\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "execution-latency-budgets.toml").write_text(f'''schema_version = 1
maximum_baseline_ratio = 1.5
[bench]
events = 2000
rate = 100
every = 20
symbols = ["BTCUSDT"]
[runners.{"linux-x86_64" if paired else f"{platform.system().lower()}-{platform.machine().lower()}"}]
status = "measured-fixture"
decision_p99_ns = {decision_baseline}
submit_p50_ns = {submit_baseline}
''')
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
    reference_commit = commit if paired else ""
    if paired:
        config = repo / "docs" / "execution-latency-budgets.toml"
        config.write_text(config.read_text() + f'reference_commit = "{reference_commit}"\n')
        (repo / "engine" / "Cargo.toml").write_text("# candidate source\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "Candidate source"],
            cwd=repo, check=True, capture_output=True,
        )
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    rustc = tmp_path / "rustc"
    rustc.write_text("#!/bin/sh\nprintf 'rustc 1.90.0 (fixture)\\nhost: fixture-host\\n'\n")
    rustc.chmod(0o755)
    monkeypatch.setenv("RUSTC", str(rustc))
    target = tmp_path / "target"
    release = target / "fixture-host" / "release"
    calls: list[list[str]] = []
    behavior: dict[str, str] = {"reference_commit": reference_commit}

    def run(command: list[str], cwd: Path, log: TextIO, source_commit: str) -> None:
        assert source_commit in (commit, reference_commit)
        role = "A" if source_commit == reference_commit else "B"
        calls.append(command)
        log.write(shlex.join(command) + "\n")
        phase = command[1] if command[0] == "cargo" else Path(command[0]).name
        if phase == "build":
            expected_source = "# candidate source\n" if paired and role == "B" else "# fixture source\n"
            assert (cwd / "Cargo.toml").read_text() == expected_source
            behavior[f"build-{role}-cwd"] = str(cwd)
            release.mkdir(parents=True, exist_ok=True)
            for name in BINARIES:
                binary = release / name
                binary.write_bytes(f"{name} at {source_commit}\n".encode())
                binary.chmod(0o755)
        if len(command) > 1 and command[1] == "bench":
            assert Path(command[0]).read_bytes() == f"engine at {source_commit}\n".encode()
            index = sum(len(call) > 1 and call[1] == "bench" for call in calls)
            role_index = sum(len(call) > 1 and call[1] == "bench" and call[0] == command[0] for call in calls)
            wal = Path(command[command.index("--wal") + 1])
            assert not wal.exists()
            wal.write_bytes(f"fixture WAL {index}\n".encode())
            log.write(behavior.get(f"bench-{index}", behavior.get(f"bench-{role}-{role_index}", behavior.get(f"bench-{role}", behavior.get("bench", """  market to decision               100        20.0us        30.0us         40.0us        50.0us     50.0us
  market to submit result          100        4.00ms        5.00ms         6.00ms        7.00ms     7.00ms
""")))))
            if behavior.get("fail-bench") == str(index):
                raise subprocess.CalledProcessError(7, command)
            if index == 8 and behavior.get("mutate-latency-image"):
                changed = Path(calls[-8][0]) if behavior["mutate-latency-image"] == "A" else release / "engine"
                changed.write_bytes(b"changed after the last latency cell\n")
        if phase == behavior.get("fail"):
            raise subprocess.CalledProcessError(7, command)
        if phase == "signal-worker":
            if behavior.get("mutate") == "binary":
                (release / "engine").write_bytes(b"replaced after tests")
            elif behavior.get("mutate") == "source":
                (repo / "engine" / "Cargo.toml").write_text("changed source after tests\n")

    monkeypatch.setattr(artifact_module, "_run", run)
    return repo, commit, target, calls, behavior


def _latency_cell(decision: int, submit: int, *, decision_scale: int = 1, submit_scale: int = 1) -> str:
    rows = (
        ("market to decision", [1000, 2000, decision, max(20000, decision), max(20000, decision)], decision_scale),
        ("market to submit result", [submit, 2 * submit, 3 * submit, 4 * submit, 4 * submit], submit_scale),
    )
    return "".join(f"{name} 100 " + " ".join(f"{value * scale}ns" for value in values) + "\n"
                   for name, values, scale in rows)


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
@pytest.mark.parametrize("metric", ["decision_p99_ns", "submit_p50_ns"])
def test_relative_pass_cannot_publish_after_an_absolute_median_miss(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], metric: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    behavior["bench-A"] = _latency_cell(20000, 2000000)
    behavior["bench-B"] = _latency_cell(20000 if metric == "decision_p99_ns" else 9300,
                                      2000000 if metric == "submit_p50_ns" else 1090000)
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(ValueError, match=f"latency budget failed: {metric}="):
        artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    assert not output.exists()
    benches = [command for command in calls if len(command) > 1 and command[1] == "bench"]
    assert len(benches) == 8
    summary = next(line.removeprefix("latency budget: ") for line in capsys.readouterr().out.splitlines()
                   if line.startswith("latency budget: "))
    verdict = json.loads(summary)
    assert verdict["absolute_passed"] is False and verdict["relative_passed"] is True
    assert [cell["image"] for cell in verdict["runs"]] == list("ABBABAAB")


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
def test_paired_source_keeps_a_slow_reference_and_packages_a_passing_candidate(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    behavior["bench-A"] = _latency_cell(30000, 1090000)
    for index, (decision, submit) in enumerate(zip((14300, 8600, 9400, 8000), (1090000, 1040000, 1030000, 1070000)), 1):
        behavior[f"bench-B-{index}"] = _latency_cell(decision, submit)
    output = tmp_path / "qualified.tar.gz"
    artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    extracted = tmp_path / "verified"
    manifest = artifact_module.verify(output, commit, extracted)
    latency = manifest["latency_budget"]
    assert latency["contract"] == "absolute_and_paired_source_relative"
    assert latency["measured_ns"] == {"decision_p99_ns": 9000, "submit_p50_ns": 1055000}
    assert latency["reference_measured_ns"] == {"decision_p99_ns": 30000, "submit_p50_ns": 1090000}
    assert latency["limits_ns"] == {"decision_p99_ns": 13950, "submit_p50_ns": 1635000}
    assert latency["relative_limits_ns"] == {"decision_p99_ns": 45000, "submit_p50_ns": 1635000}
    assert latency["relative_passed"] is True and latency["absolute_passed"] is True
    assert latency["reference_absolute_passed"] is False
    assert latency["aggregation"] == "median_of_run_metrics" and "samples" not in latency
    assert [cell["image"] for cell in latency["runs"]] == list("ABBABAAB")
    assert [cell["budget_passed"] for cell in latency["runs"]] == [False, False, True, False, True, False, False, True]
    benches = [command for command in calls if len(command) > 1 and command[1] == "bench"]
    assert len(benches) == len({command[command.index("--wal") + 1] for command in benches}) == 8
    assert calls[-8:] == benches
    assert [command[1] for command in calls if command[0] == "cargo"] == ["build", "build", "test"]
    builds = [command for command in calls if command[:2] == ["cargo", "build"]]
    assert builds[0] == builds[1]
    assert builds[0][-4:] == ["--target-dir", str(target), "--target", "fixture-host"]
    reference_commit = behavior["reference_commit"]
    assert reference_commit != commit
    assert latency["images"]["A"] == {
        "commit": reference_commit, "source": "fresh_reference_source_build",
        "binaries": {name: hashlib.sha256(f"{name} at {reference_commit}\n".encode()).hexdigest() for name in BINARIES},
    }
    assert latency["images"]["B"] == {
        "commit": commit, "source": "candidate_qualification_build", "binaries": manifest["binaries"],
    }
    assert behavior["build-B-cwd"] == str(repo / "engine")
    assert not Path(behavior["build-A-cwd"]).exists()
    assert not Path(benches[0][0]).parent.exists()
    assert (repo / "engine" / "Cargo.toml").read_text() == "# candidate source\n"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True) == ""
    assert {path.name for path in extracted.iterdir()} == {*BINARIES, "binaries.sha256", "qualification.json", "qualification.log"}
    log = (extracted / "qualification.log").read_text()
    assert all(behavior[f"bench-B-{index}"] in log for index in range(1, 5))
    assert log.count(behavior["bench-A"]) == 4
    assert log.index("latency images:") < log.index("latency qualification:")
    assert '"absolute_passed": true' in log and '"reference_absolute_passed": false' in log


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
@pytest.mark.parametrize("metric", ["decision_p99_ns", "submit_p50_ns"])
def test_twice_the_same_worker_reference_fails_either_relative_metric_after_all_eight_cells(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], metric: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    behavior["bench-A"] = _latency_cell(5000, 500000)
    behavior["bench-B"] = _latency_cell(5000, 500000, decision_scale=2 if metric == "decision_p99_ns" else 1,
                                      submit_scale=2 if metric == "submit_p50_ns" else 1)
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(ValueError, match=f"relative latency budget failed: {metric}="):
        artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    benches = [command for command in calls if len(command) > 1 and command[1] == "bench"]
    assert len(benches) == 8 and not output.exists()
    assert not Path(benches[0][0]).parent.exists()
    summary = next(line.removeprefix("latency budget: ") for line in capsys.readouterr().out.splitlines()
                   if line.startswith("latency budget: "))
    verdict = json.loads(summary)
    assert verdict["relative_passed"] is False and verdict["absolute_passed"] is True
    assert len(verdict["runs"]) == 8


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
def test_doubling_an_already_fast_candidate_can_remain_within_both_budgets(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, _, behavior = qualification_workspace
    behavior["bench-A"] = _latency_cell(12000, 1200000)
    behavior["bench-B"] = _latency_cell(6000, 600000, decision_scale=2, submit_scale=2)
    output = tmp_path / "qualified.tar.gz"
    artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    latency = artifact_module.verify(output, commit)["latency_budget"]
    assert latency["measured_ns"] == latency["reference_measured_ns"]
    assert latency["relative_passed"] is True and latency["absolute_passed"] is True


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
def test_reference_build_failure_does_not_fall_back_to_the_candidate(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    behavior["fail"] = "build"
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(subprocess.CalledProcessError):
        artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    assert len(calls) == 1 and calls[0][:2] == ["cargo", "build"]
    assert not Path(behavior["build-A-cwd"]).exists() and not output.exists()


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
@pytest.mark.parametrize("image", ["A", "B"])
def test_paired_images_remain_frozen_through_the_last_cell(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], image: str,
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    behavior["bench"] = _latency_cell(9300, 1090000)
    behavior["mutate-latency-image"] = image
    output = tmp_path / "unqualified.tar.gz"
    message = "latency reference binary bytes changed during qualification" if image == "A" else "release binary bytes changed during qualification"
    with pytest.raises(ValueError, match=message):
        artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    assert len([command for command in calls if len(command) > 1 and command[1] == "bench"]) == 8
    assert not output.exists()


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000, True)], indirect=True)
@pytest.mark.parametrize("fault", ["missing-source", "different-compiler"])
def test_an_unavailable_or_differently_pinned_reference_is_fatal_before_any_build(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], fault: str,
) -> None:
    repo, _, target, calls, behavior = qualification_workspace

    def commit_fixture() -> str:
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "Reference fixture"],
            cwd=repo, check=True, capture_output=True,
        )
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    reference_commit = "f" * 40
    if fault == "different-compiler":
        (repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.89.0"\n')
        reference_commit = commit_fixture()
        (repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.90.0"\n')
    config = repo / "docs" / "execution-latency-budgets.toml"
    config.write_text(config.read_text().replace(behavior["reference_commit"], reference_commit))
    commit = commit_fixture()
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(subprocess.CalledProcessError if fault == "missing-source" else ValueError):
        artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64")
    assert not calls and not output.exists()


def test_registered_linux_source_and_absolute_darwin_contracts(
    artifact_module: ModuleType, capsys: pytest.CaptureFixture[str],
) -> None:
    linux = artifact_module._latency_budget(ROOT, "linux-x86_64")
    assert linux["contract"] == "absolute_and_paired_source_relative"
    assert linux["reference_commit"] == "a4189a4897409e65acba7a2078b964986ceea928"
    assert linux["limits_ns"] == {"decision_p99_ns": 13950, "submit_p50_ns": 1635000}
    darwin = artifact_module._latency_budget(ROOT, "darwin-arm64")
    assert darwin["contract"] == "absolute" and "reference_commit" not in darwin
    with pytest.raises(ValueError, match="latency budget failed"):
        artifact_module._check_latency(_latency_cell(30000, 1090000), linux)
    standalone = artifact_module._check_latency(_latency_cell(10000, 1090000), linux)
    assert standalone["contract"] == "absolute" and standalone["reference_commit"] == linux["reference_commit"]
    assert "reference_measured_ns" not in standalone
    assert all(json.loads(line.removeprefix("latency budget: "))["contract"] == "absolute"
               for line in capsys.readouterr().out.splitlines() if line.startswith("latency budget: "))


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000)], indirect=True)
def test_four_fixed_latency_cells_keep_high_first_verdict_and_qualify_by_run_medians(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    decisions = (14300, 8600, 9400, 8000)
    submits = (1160000, 1230000, 1330000, 1280000)
    for index, (decision, submit) in enumerate(zip(decisions, submits), 1):
        behavior[f"bench-{index}"] = _latency_cell(decision, submit)
    output = tmp_path / "qualified.tar.gz"
    artifact_module.qualify(repo, commit, output, target)
    extracted = tmp_path / "verified"
    manifest = artifact_module.verify(output, commit, extracted)
    latency = manifest["latency_budget"]
    assert latency["aggregation"] == "median_of_run_metrics"
    assert "samples" not in latency
    assert latency["measured_ns"] == {"decision_p99_ns": 9000, "submit_p50_ns": 1255000}
    assert latency["limits_ns"] == {"decision_p99_ns": 13950, "submit_p50_ns": 1635000}
    assert [cell["budget_passed"] for cell in latency["runs"]] == [False, True, True, True]
    assert [cell["measured_ns"]["decision_p99_ns"] for cell in latency["runs"]] == list(decisions)
    assert all(cell["samples"] == {"decision_p99_ns": 100, "submit_p50_ns": 100} for cell in latency["runs"])
    benches = [command for command in calls if len(command) > 1 and command[1] == "bench"]
    assert len(benches) == 4
    assert len({command[command.index("--wal") + 1] for command in benches}) == 4
    log = (extracted / "qualification.log").read_text()
    assert all(behavior[f"bench-{index}"] in log for index in range(1, 5))
    assert '"budget_passed": false' in log
    assert "decision_p99_ns=14300 exceeds 13950 ns" in log
    assert "not pooled quantiles" in log


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000)], indirect=True)
@pytest.mark.parametrize("metric", ["decision_p99_ns", "submit_p50_ns"])
def test_doubling_either_histogram_across_all_four_cells_fails_qualification(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], metric: str,
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    for index, (decision, submit) in enumerate(zip((7900, 10000, 8600, 10500), (1310000, 1310000, 1340000, 1290000)), 1):
        behavior[f"bench-{index}"] = _latency_cell(
            decision, submit, decision_scale=2 if metric == "decision_p99_ns" else 1,
            submit_scale=2 if metric == "submit_p50_ns" else 1,
        )
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(ValueError, match=f"latency budget failed: {metric}="):
        artifact_module.qualify(repo, commit, output, target)
    assert len([command for command in calls if len(command) > 1 and command[1] == "bench"]) == 4
    assert not output.exists()


@pytest.mark.parametrize("qualification_workspace", [(40000, 4000000), (9300, 1090000, True)], indirect=True)
@pytest.mark.parametrize("fault", ["process", "missing", "empty", "unit", "duplicate", "malformed-duplicate", "nan", "unordered"])
def test_one_invalid_cell_is_fatal_after_all_fixed_cells_execute(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], fault: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    text = _latency_cell(9300, 1090000)
    behavior["bench"] = text
    paired = bool(behavior["reference_commit"])
    if fault == "process":
        behavior["fail-bench"] = "1"
    elif fault == "missing":
        text = text.splitlines()[0] + "\n"
    elif fault == "empty":
        text = text.replace("100 ", "0 ")
    elif fault == "unit":
        text = text.replace("9300ns", "9300")
    elif fault == "duplicate":
        text += text.splitlines()[0] + "\n"
    elif fault == "malformed-duplicate":
        text += "market to decision 100 1us\n"
    elif fault == "nan":
        text = text.replace("9300ns", "NaNns")
    elif fault == "unordered":
        text = text.replace("9300ns", "100ns")
    behavior["bench-1"] = text
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(subprocess.CalledProcessError if fault == "process" else ValueError):
        artifact_module.qualify(repo, commit, output, target, runner_class="linux-x86_64" if paired else None)
    assert len([command for command in calls if len(command) > 1 and command[1] == "bench"]) == (8 if paired else 4)
    verdicts = [json.loads(line.removeprefix("latency cell: "))
                for line in capsys.readouterr().out.splitlines() if line.startswith("latency cell: ")]
    assert len(verdicts) == (8 if paired else 4)
    assert "error" in verdicts[0]
    assert all(cell["budget_passed"] for cell in verdicts[1:])
    assert not output.exists()


@pytest.mark.parametrize("qualification_workspace", [(9300, 1090000)], indirect=True)
def test_four_run_median_does_not_round_down_across_the_limit(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]],
) -> None:
    repo, commit, target, calls, behavior = qualification_workspace
    for index, decision in enumerate((13950, 13950, 13951, 13951), 1):
        behavior[f"bench-{index}"] = _latency_cell(decision, 1090000)
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(ValueError, match="decision_p99_ns=13950.5 exceeds 13950 ns"):
        artifact_module.qualify(repo, commit, output, target)
    assert len([command for command in calls if len(command) > 1 and command[1] == "bench"]) == 4
    assert not output.exists()


@pytest.mark.parametrize("fault", ["double-decision", "double-submit", "missing", "empty", "unit", "duplicate", "nan", "unordered"])
def test_unqualified_latency_cannot_publish_an_artifact(
    tmp_path: Path, artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], fault: str,
) -> None:
    repo, commit, target, _, behavior = qualification_workspace
    decision = "  market to decision               100        20.0us        30.0us         40.0us        50.0us     50.0us\n"
    submit = "  market to submit result          100        4.00ms        5.00ms         6.00ms        7.00ms     7.00ms\n"
    if fault == "double-decision":
        decision = decision.replace("40.0us", "80.0us").replace("50.0us", "90.0us")
    elif fault == "double-submit":
        submit = submit.replace("4.00ms", "8.00ms").replace("5.00ms", "9.00ms").replace("6.00ms", "10.00ms").replace("7.00ms", "11.00ms")
    elif fault == "missing":
        submit = ""
    elif fault == "empty":
        submit = submit.replace("100", "0")
    elif fault == "unit":
        submit = submit.replace("4.00ms", "4.00")
    elif fault == "duplicate":
        submit += submit
    elif fault == "nan":
        submit = submit.replace("4.00ms", "NaNms")
    else:
        submit = submit.replace("5.00ms", "3.00ms")
    behavior["bench"] = decision + submit
    output = tmp_path / "unqualified.tar.gz"
    with pytest.raises(ValueError, match="latency"):
        artifact_module.qualify(repo, commit, output, target)
    assert not output.exists()


@pytest.mark.parametrize("ratio", ["2", "nan", "true", "0.5"])
def test_latency_budget_cannot_allow_a_twofold_regression(
    artifact_module: ModuleType,
    qualification_workspace: tuple[Path, str, Path, list[list[str]], dict[str, str]], ratio: str,
) -> None:
    repo, *_ = qualification_workspace
    path = repo / "docs/execution-latency-budgets.toml"
    path.write_text(path.read_text().replace("maximum_baseline_ratio = 1.5", f"maximum_baseline_ratio = {ratio}"))
    with pytest.raises(ValueError, match="latency budget ratio"):
        artifact_module._latency_budget(repo, None)


def test_latency_units_are_converted_to_nanoseconds(artifact_module: ModuleType) -> None:
    budget = {"limits_ns": {"decision_p99_ns": 60000, "submit_p50_ns": 6000000}}
    measured = artifact_module._check_latency(
        "market to decision 100 20000ns 0.03ms 40us 0.00005s 50000ns\n"
        "market to submit result 100 4000us 0.005s 6000000ns 7ms 7ms\n", budget,
    )
    assert measured["measured_ns"] == {"decision_p99_ns": 40000, "submit_p50_ns": 4000000}


def test_qualification_runs_on_demand_and_uploads_only_after_it_passes() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/vps-deploy.yml").read_text())
    deploy = workflow["jobs"]["rust-artifact"]
    assert "inputs.mode == 'deploy'" in deploy["if"]
    assert not any("release_artifact.py qualify" in step.get("run", "") for step in deploy["steps"])
    job = workflow["jobs"]["rust-qualify"]
    assert "inputs.mode == 'qualify'" in job["if"]
    steps = job["steps"]
    upload = next(i for i, step in enumerate(steps) if "actions/upload-artifact@" in step.get("uses", ""))
    qualify = next(i for i, step in enumerate(steps) if "scripts/release_artifact.py qualify" in step.get("run", ""))
    assert qualify < upload
    assert not steps[qualify].get("continue-on-error", False)
    assert "rust-soak-bench" not in workflow["jobs"]
    assert "--runner-class linux-x86_64" in steps[qualify]["run"]
    checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0


def test_deploy_missing_qualified_artifact_never_compiles_on_host(tmp_path: Path) -> None:
    result = _prepare_release(tmp_path)
    assert result.returncode != 0, result.stdout
    assert not (tmp_path / "cargo-called").exists()
    assert "release artifact" in result.stderr


def test_deploy_unpacks_a_checksummed_archive_without_qualification_metadata(tmp_path: Path) -> None:
    files = {name: f"old {name}\n".encode() for name in BINARIES}
    files["binaries.sha256"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()
    ).encode()
    release = tmp_path / "release"
    _write_tar(release / "staged" / f"{COMMIT}.tar.gz", files)
    result = _prepare_release(tmp_path)
    assert result.returncode == 0, result.stderr
    extracted = list((release / "staged").glob(".qualified.*"))
    assert len(extracted) == 1
    assert (extracted[0] / "engine").read_bytes() == files["engine"]


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
        del files["signal-worker"]
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
    assert "$QUALIFIED_RELEASE_DIR/engine-tools" in install
    assert "$QUALIFIED_RELEASE_DIR/signal-worker" in install
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
    latency = manifest["latency_budget"]
    assert latency["measured_ns"] == {"decision_p99_ns": 40000, "submit_p50_ns": 4000000}
    assert latency["limits_ns"] == {"decision_p99_ns": 60000, "submit_p50_ns": 6000000}
    assert latency["aggregation"] == "median_of_run_metrics"
    assert len(latency["runs"]) == 4
    assert all(cell["samples"] == {"decision_p99_ns": 100, "submit_p50_ns": 100} for cell in latency["runs"])
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
    assert bench[bench.index("--events") + 1] == "2000"
    assert bench[bench.index("--rate") + 1] == "100"
    for name in BINARIES:
        assert (tmp_path / "verified" / name).read_bytes() == (release / name).read_bytes()


@pytest.mark.parametrize("phase", ["test", "account_state_soak", "engine", "engine-tools", "signal-worker"])
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
        + _remote_function("stage_release_binaries")
        + '\nstage_release_binaries "$COMMIT" fixture-host',
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
