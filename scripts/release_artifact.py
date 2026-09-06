"""Qualify, package, and verify the binary bytes consumed by VPS deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TextIO

BINARIES = ("engine", "engine-tools", "signal-worker")
CHECKS = ("release-tests", "account-state-soak", "engine-bench", "binary-smoke")
METADATA = ("binaries.sha256", "qualification.json", "qualification.log")


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


def qualify(repo: Path, commit: str, output: Path, target: Path) -> None:
    import tomllib

    _check_source(repo, _commit(commit))
    if output.exists():
        raise ValueError(f"output already exists: {output}")
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
    with tempfile.TemporaryDirectory(prefix="liquidity-qualification-") as temporary:
        evidence = Path(temporary)
        with (evidence / "qualification.log").open("w") as log:
            _run(
                [
                    "cargo",
                    "build",
                    "--release",
                    "--locked",
                    "--workspace",
                    "--bins",
                    "--examples",
                    "--target-dir",
                    str(target),
                    "--target",
                    native_target,
                ],
                repo / "engine",
                log,
                commit,
            )
            hashes = _binary_hashes(release)
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
            _run(
                [
                    str(release / "engine"),
                    "bench",
                    "--events",
                    "20000",
                    "--rate",
                    "0",
                    "--every",
                    "20",
                    "--symbols",
                    "BTCUSDT",
                    "--wal",
                    str(evidence / "bench.wal"),
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
            qualify(repo, args.commit, args.output.resolve(), target)
        else:
            manifest = verify(
                args.artifact, args.commit, getattr(args, "output", None), require_platform=args.require_platform
            )
            print(json.dumps(manifest, sort_keys=True))
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(1, f"release artifact: {error}\n")


if __name__ == "__main__":
    main()
