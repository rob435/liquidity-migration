"""Qualify, package, and verify the binary bytes consumed by VPS deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import statistics
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, TextIO

BINARIES = ("engine", "engine-tools", "signal-worker")
CHECKS = ("release-tests", "account-state-soak", "engine-bench", "binary-smoke")
METADATA = ("binaries.sha256", "qualification.json", "qualification.log")
LATENCY_RUNS = 4


def _latency_budget(repo: Path, runner_class: str | None) -> dict[str, Any]:
    import tomllib

    selected = runner_class or f"{platform.system().lower()}-{platform.machine().lower()}"
    config = tomllib.loads((repo / "docs" / "execution-latency-budgets.toml").read_text())
    if config.get("schema_version") != 1:
        raise ValueError("unsupported latency budget schema")
    ratio = config.get("maximum_baseline_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (float, int)) or not math.isfinite(ratio) or not 1 <= ratio < 2:
        raise ValueError("latency budget ratio must be at least 1 and less than 2")
    baseline = config.get("runners", {}).get(selected)
    if not isinstance(baseline, dict) or not isinstance(baseline.get("status"), str) or not baseline["status"]:
        raise ValueError(f"latency budget has no runner class: {selected}")
    metrics = ("decision_p99_ns", "submit_p50_ns")
    if any(type(baseline.get(key)) is not int or baseline[key] <= 0 for key in metrics):
        raise ValueError("latency baselines must be positive integer nanoseconds")
    bench = config.get("bench", {})
    if any(type(bench.get(key)) is not int or bench[key] <= 0 for key in ("events", "rate", "every")):
        raise ValueError("latency bench events, rate and every must be positive integers")
    symbols = bench.get("symbols")
    if not isinstance(symbols, list) or not symbols or any(not isinstance(item, str) or not item for item in symbols):
        raise ValueError("latency bench symbols must be nonempty strings")
    result = {
        "runner_class": selected,
        "baseline_status": baseline["status"],
        "baseline_ns": {key: baseline[key] for key in metrics},
        "limits_ns": {key: math.floor(baseline[key] * ratio) for key in metrics},
        "maximum_baseline_ratio": ratio,
        "bench": bench,
        "contract": "absolute",
    }
    if selected == "linux-x86_64" and "reference_commit" in baseline:
        reference = baseline["reference_commit"]
        if not isinstance(reference, str):
            raise ValueError("latency reference commit must be a full SHA")
        result.update(contract="paired_source_relative", reference_commit=_commit(reference))
    return result


def _latency_measurement(text: str, budget: dict[str, Any]) -> dict[str, Any]:
    result = dict(budget)
    result["measured_ns"], result["samples"] = {}, {}
    scales = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000}
    for segment, metric, quantile in (
        ("market to decision", "decision_p99_ns", 2),
        ("market to submit result", "submit_p50_ns", 0),
    ):
        row_count = len(re.findall(r"^[ \t]*" + re.escape(segment) + r"(?:[ \t]|$)", text, re.MULTILINE))
        rows = re.findall(r"^\s*" + re.escape(segment) + r"\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", text, re.MULTILINE)
        if row_count != 1 or len(rows) != 1 or int(rows[0][0]) <= 0:
            raise ValueError(f"latency histogram is missing, duplicated or empty: {segment}")
        values = []
        for cell in rows[0][1:]:
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s)", cell)
            if match is None:
                raise ValueError(f"latency histogram requires a finite value with ns/us/ms/s units: {segment}: {cell}")
            value = float(match[1]) * scales[match[2]]
            if not math.isfinite(value):
                raise ValueError(f"latency histogram has a nonfinite value: {segment}")
            values.append(round(value))
        if values != sorted(values):
            raise ValueError(f"latency histogram quantiles are not ordered: {segment}")
        measured = values[quantile]
        result["measured_ns"][metric] = measured
        result["samples"][metric] = int(rows[0][0])
    return result


def _latency_failures(result: dict[str, Any]) -> list[str]:
    return [f"{key}={value} exceeds {result['limits_ns'][key]} ns"
            for key, value in result["measured_ns"].items() if value > result["limits_ns"][key]]


def _check_latency(text: str, budget: dict[str, Any]) -> dict[str, Any]:
    result = _latency_measurement(text, budget)
    result["contract"] = "absolute"
    failures = _latency_failures(result)
    print("latency budget: " + json.dumps(result, sort_keys=True), flush=True)
    if failures:
        raise ValueError("latency budget failed: " + "; ".join(failures))
    return result


def _commit(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("commit must be a full lowercase 40-character SHA")
    return value


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _binary_hashes(directory: Path) -> dict[str, str]:
    for name in BINARIES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"missing regular executable: {path}")
    return {name: _sha256(directory / name) for name in BINARIES}


def _check_source(repo: Path, commit: str) -> None:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=repo, text=True
    ).strip()
    if head != commit or status:
        raise ValueError("qualification requires a clean checkout at the requested commit")


def _run(command: list[str], repo: Path, log: TextIO, commit: str) -> None:
    heading = f"$ {shlex.join(command)}\n"
    print(heading, end="", flush=True)
    log.write(heading)
    with subprocess.Popen(
        command,
        cwd=repo,
        env={**os.environ, "ENGINE_GIT_COMMIT": commit},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, command)


def _qualify_latency(
    repo: Path, release: Path, evidence: Path, log: TextIO, commit: str, budget: dict[str, Any],
    reference: Path | None = None, images: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bench = budget["bench"]
    order = "ABBABAAB" if reference is not None else "B" * LATENCY_RUNS
    heading = f"latency qualification: {order}; fixed fresh-WAL runs; median of per-run metrics, not pooled quantiles\n"
    print(heading, end="", flush=True)
    log.write(heading)
    runs = []
    errors: list[Exception] = []
    for index, image in enumerate(order, 1):
        cell: dict[str, Any] = {"run": index, "image": image}
        directory = reference if image == "A" else release
        assert directory is not None
        source_commit = budget["reference_commit"] if image == "A" else commit
        start = log.tell()
        try:
            _run(
                [str(directory / "engine"), "bench", "--events", str(bench["events"]),
                 "--rate", str(bench["rate"]), "--every", str(bench["every"]),
                 "--symbols", ",".join(bench["symbols"]), "--wal", str(evidence / f"bench-{index}.wal")],
                repo, log, source_commit,
            )
            log.flush()
            with (evidence / "qualification.log").open() as bench_log:
                bench_log.seek(start)
                measured = _latency_measurement(bench_log.read(), budget)
            failures = _latency_failures(measured)
            cell.update(measured_ns=measured["measured_ns"], samples=measured["samples"],
                        budget_passed=not failures, budget_failures=failures)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            cell["error"] = str(error)
            errors.append(error)
        runs.append(cell)
        line = "latency cell: " + json.dumps(cell, sort_keys=True) + "\n"
        print(line, end="", flush=True)
        log.write(line)
    if errors:
        raise errors[0]
    result = dict(budget)
    result.update(aggregation="median_of_run_metrics", runs=runs, measured_ns={
        metric: statistics.median(cell["measured_ns"][metric] for cell in runs if cell["image"] == "B")
        for metric in budget["limits_ns"]
    })
    failures = _latency_failures(result)
    result.update(absolute_passed=not failures, absolute_failures=failures)
    if reference is not None:
        reference_measured = {
            metric: statistics.median(cell["measured_ns"][metric] for cell in runs if cell["image"] == "A")
            for metric in budget["limits_ns"]
        }
        relative_limits = {metric: value * budget["maximum_baseline_ratio"] for metric, value in reference_measured.items()}
        reference_failures = _latency_failures({**budget, "measured_ns": reference_measured})
        failures = _latency_failures({**result, "limits_ns": relative_limits})
        result.update(reference_measured_ns=reference_measured, relative_limits_ns=relative_limits,
                      reference_absolute_passed=not reference_failures, reference_absolute_failures=reference_failures,
                      relative_passed=not failures, relative_failures=failures, images=images)
    line = "latency budget: " + json.dumps(result, sort_keys=True) + "\n"
    print(line, end="", flush=True)
    log.write(line)
    if failures:
        prefix = "relative latency budget failed: " if reference is not None else "latency budget failed: "
        raise ValueError(prefix + "; ".join(failures))
    return result


def qualify(repo: Path, commit: str, output: Path, target: Path, runner_class: str | None = None) -> None:
    import tomllib

    _check_source(repo, _commit(commit))
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    budget = _latency_budget(repo, runner_class)
    pinned = tomllib.loads((repo / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    compiler = subprocess.check_output(
        [os.environ.get("RUSTC", "rustc"), "--version", "--verbose"], cwd=repo, text=True
    ).strip()
    if not compiler.startswith(f"rustc {pinned} "):
        raise ValueError(f"qualification requires Rust {pinned}; found {compiler}")
    host = re.search(r"^host: ([a-zA-Z0-9_-]+)$", compiler, re.MULTILINE)
    if host is None:
        raise ValueError("cannot read the Rust compiler host target")
    native_target = host.group(1)
    release = target / native_target / "release"
    build = ["cargo", "build", "--release", "--locked", "--workspace", "--bins", "--examples",
             "--target-dir", str(target), "--target", native_target]
    with tempfile.TemporaryDirectory(prefix="liquidity-qualification-") as temporary:
        evidence = Path(temporary)
        with (evidence / "qualification.log").open("w") as log:
            reference = None
            images: dict[str, Any] = {}
            if budget["contract"] == "paired_source_relative":
                reference_commit = budget["reference_commit"]
                line = f"latency reference source: {reference_commit}; fresh build with the candidate compiler, target and build command\n"
                print(line, end="", flush=True)
                log.write(line)
                source = evidence / "reference-source"
                source_archive = evidence / "reference-source.tar"
                subprocess.run(
                    ["git", "archive", "--format=tar", f"--output={source_archive}", reference_commit],
                    cwd=repo, check=True,
                )
                with tarfile.open(source_archive) as archive:
                    archive.extractall(source, filter="data")
                source_archive.unlink()
                reference_pinned = tomllib.loads((source / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
                if reference_pinned != pinned:
                    raise ValueError("latency reference and candidate must use the same pinned Rust compiler")
                _run(build, source / "engine", log, reference_commit)
                reference = evidence / "reference-release"
                reference.mkdir()
                for name in BINARIES:
                    shutil.copy2(release / name, reference / name)
                images["A"] = {"commit": reference_commit, "binaries": _binary_hashes(reference),
                               "source": "fresh_reference_source_build"}
                shutil.rmtree(source)
            _run(build, repo / "engine", log, commit)
            hashes = _binary_hashes(release)
            images["B"] = {"commit": commit, "binaries": hashes, "source": "candidate_qualification_build"}
            _run(
                [
                    "cargo",
                    "test",
                    "--workspace",
                    "--all-targets",
                    "--release",
                    "--locked",
                    "--target-dir",
                    str(target),
                    "--target",
                    native_target,
                ],
                repo / "engine",
                log,
                commit,
            )
            _run(
                [
                    str(release / "examples" / "account_state_soak"),
                    "--operations",
                    "2000000",
                    "--live-ids",
                    "65536",
                    "--sample-ops",
                    "4096",
                    "--history-rows",
                    "0,1000,10000,100000",
                    "--repeats",
                    "3",
                    "--json",
                ],
                repo,
                log,
                commit,
            )
            for command in (
                [str(release / "engine"), "--help"],
                [str(release / "engine-tools"), "--help"],
                [str(release / "signal-worker"), "--help"],
            ):
                _run(command, repo, log, commit)
            if _binary_hashes(release) != hashes:
                raise ValueError("release binary bytes changed during qualification")
            if reference is not None:
                if _binary_hashes(reference) != images["A"]["binaries"]:
                    raise ValueError("latency reference binary bytes changed during qualification")
                line = "latency images: " + json.dumps({"rustc": compiler, "target": native_target, "images": images}, sort_keys=True) + "\n"
                print(line, end="", flush=True)
                log.write(line)
            latency = _qualify_latency(repo, release, evidence, log, commit, budget, reference, images)
            if reference is not None and _binary_hashes(reference) != images["A"]["binaries"]:
                raise ValueError("latency reference binary bytes changed during qualification")
        _check_source(repo, commit)
        if _binary_hashes(release) != hashes:
            raise ValueError("release binary bytes changed during qualification")
        manifest = {
            "schema_version": 1,
            "commit": commit,
            "profile": "release",
            "rustc": compiler,
            "target": native_target,
            "platform": [platform.system(), platform.machine()],
            "wal_compatibility": "not_assessed",
            "checks": list(CHECKS),
            "binaries": hashes,
            "log_sha256": _sha256(evidence / "qualification.log"),
            "latency_budget": latency,
        }
        (evidence / "qualification.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        (evidence / "binaries.sha256").write_text("".join(f"{hashes[name]}  {name}\n" for name in BINARIES))
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.") as archive_file:
            with tarfile.open(fileobj=archive_file, mode="w:gz") as archive:
                for name in (*METADATA, *BINARIES):
                    path = (release if name in BINARIES else evidence) / name
                    archive.add(path, arcname=name, recursive=False)
            archive_file.flush()
            verify(Path(archive_file.name), commit)
            # Verification also detects changes while the binary bytes are packed.
            os.link(archive_file.name, output)
    print(f"qualified artifact: {output}")


def verify(
    artifact: Path, commit: str, output: Path | None = None, *, require_platform: bool = False
) -> dict[str, object]:
    _commit(commit)
    if output is not None:
        if output.is_symlink():
            raise ValueError("qualified release extraction directory must not be a symlink")
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise ValueError("qualified release extraction directory must be empty")
    seen: set[str] = set()
    hashes: dict[str, str] = {}
    metadata: dict[str, bytes] = {}
    try:
        with tarfile.open(artifact, "r|gz") as archive:
            for member in archive:
                name = member.name
                if name not in (*BINARIES, *METADATA) or name in seen or not member.isfile():
                    raise ValueError(f"unexpected or duplicate release artifact member: {name}")
                seen.add(name)
                limit = 512 * 1024 * 1024 if name in BINARIES else 16 * 1024 * 1024
                if not 0 < member.size <= limit:
                    raise ValueError(f"invalid release artifact member size: {name}")
                source = archive.extractfile(member)
                assert source is not None
                if name in BINARIES:
                    if not member.mode & 0o111:
                        raise ValueError(f"release binary is not executable: {name}")
                    digest = hashlib.sha256()
                    destination = (output / name).open("xb") if output is not None else None
                    try:
                        while data := source.read(1024 * 1024):
                            digest.update(data)
                            if destination is not None:
                                destination.write(data)
                    finally:
                        if destination is not None:
                            destination.close()
                    hashes[name] = digest.hexdigest()
                    if output is not None:
                        (output / name).chmod(0o755)
                else:
                    metadata[name] = source.read()
        if not set(BINARIES).issubset(seen) or "binaries.sha256" not in seen:
            raise ValueError("release artifact lacks binaries or checksums")
        checksums = "".join(f"{hashes[name]}  {name}\n" for name in BINARIES).encode()
        if metadata["binaries.sha256"] != checksums:
            raise ValueError("release binary checksum mismatch")
        if "qualification.json" not in seen:
            # An archive from before qualification metadata: binaries and their checksums only.
            if output is not None:
                (output / "binaries.sha256").write_bytes(metadata["binaries.sha256"])
            return {"commit": commit, "profile": "release", "binaries": hashes, "qualified": False}
        if "qualification.log" not in seen:
            raise ValueError("release artifact lacks the qualification log")
        manifest = json.loads(metadata["qualification.json"])
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("unsupported release qualification schema")
        if manifest.get("commit") != commit or manifest.get("profile") != "release":
            raise ValueError("release qualification commit/profile mismatch")
        if manifest.get("checks") != list(CHECKS) or not manifest.get("rustc") or not manifest.get("target"):
            raise ValueError("release qualification checks are incomplete")
        if manifest.get("wal_compatibility") != "not_assessed":
            raise ValueError("release qualification must state its WAL compatibility limit")
        qualified_platform = manifest.get("platform")
        if not isinstance(qualified_platform, list) or len(qualified_platform) != 2:
            raise ValueError("release qualification platform is missing")
        if (output is not None or require_platform) and qualified_platform != [platform.system(), platform.machine()]:
            raise ValueError("qualified artifact platform does not match the install host")
        if manifest.get("binaries") != hashes:
            raise ValueError("release binary checksum mismatch")
        if manifest.get("log_sha256") != hashlib.sha256(metadata["qualification.log"]).hexdigest():
            raise ValueError("release qualification log checksum mismatch")
        if output is not None:
            for name, data in metadata.items():
                (output / name).write_bytes(data)
        return manifest
    except BaseException:
        if output is not None:
            shutil.rmtree(output)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    qualify_parser = commands.add_parser("qualify", help="local optimized tests, workloads, and packaging")
    default_repo = Path.cwd() if __file__ == "<stdin>" else Path(__file__).resolve().parents[1]
    qualify_parser.add_argument("--repo", type=Path, default=default_repo)
    qualify_parser.add_argument("--commit", required=True)
    qualify_parser.add_argument("--output", type=Path, required=True)
    qualify_parser.add_argument("--target-dir", type=Path)
    qualify_parser.add_argument("--runner-class", help="latency baseline class; defaults to the native OS and architecture")
    for name in ("verify", "unpack"):
        command = commands.add_parser(name)
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--commit", required=True)
        command.add_argument("--require-platform", action="store_true")
        if name == "unpack":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "qualify":
            repo = args.repo.resolve()
            target = args.target_dir.resolve() if args.target_dir else repo / "engine" / "target"
            qualify(repo, args.commit, args.output.resolve(), target, args.runner_class)
        else:
            manifest = verify(
                args.artifact, args.commit, getattr(args, "output", None), require_platform=args.require_platform
            )
            print(json.dumps(manifest, sort_keys=True))
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(1, f"release artifact: {error}\n")


if __name__ == "__main__":
    main()
