from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration import v7_archive_materialization as materializer
from liquidity_migration.account_reset_receipt import (
    DEMO_BOUNDARY,
    MANAGED_UNITS,
    PAPER_BOUNDARY,
)


def _git_repository(path: Path) -> str:
    path.mkdir()
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


def _write_private(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path.resolve()


def _fixture(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "repo"
    candidate = _git_repository(repository)
    account = repository / "data" / "v7-account"
    capture = repository / "data" / "v7-capture"
    _write_private(
        account / "account_journal" / "transactions" / "event.json",
        b'{"event":"training"}\n',
    )
    _write_private(
        capture / "2026-07-14" / "BTCUSDT" / "segment-000000.jsonl",
        b'{"book":"training"}\n',
    )
    account.chmod(0o700)
    capture.chmod(0o700)
    calibration = _write_private(tmp_path / "calibration.json", b"calibration\n")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\nprintf 'inactive\\n'\nexit 3\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    payload = {
        "execution_twin_gate_passed": True,
        "artifact_sha256": "c" * 64,
        "inputs": {
            "account_root": str(account.resolve()),
            "market_capture_root": str(capture.resolve()),
        },
    }
    return {
        "repository": repository,
        "candidate": candidate,
        "account": account.resolve(),
        "capture": capture.resolve(),
        "calibration": calibration,
        "calibration_payload": payload,
        "systemctl": systemctl,
        "destination": (tmp_path / "v7-immutable").resolve(),
        "map_output": (tmp_path / "v7-archive-map.json").resolve(),
    }


def _stub_map_functions(
    monkeypatch: pytest.MonkeyPatch,
    fixture: dict[str, Any],
    *,
    fail_build: bool = False,
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        materializer,
        "load_calibration_receipt",
        lambda *_args, **_kwargs: fixture["calibration_payload"],
    )

    def build(**kwargs: Any) -> dict[str, Any]:
        calls.append("build")
        if fail_build:
            raise ValueError("synthetic map build failure")
        return {
            "kind": "v7_execution_twin_archive_source_map",
            "artifact_sha256": "d" * 64,
            "archived_sources": {
                "account_root": str(Path(kwargs["archived_account_root"]).resolve()),
                "market_capture_root": str(Path(kwargs["archived_market_capture_root"]).resolve()),
            },
        }

    def write(path: str | Path, payload: dict[str, Any]) -> Path:
        calls.append("write")
        output = Path(path)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        output.chmod(0o600)
        return output.resolve()

    def load(path: str | Path, **_kwargs: Any) -> dict[str, Any]:
        calls.append("load")
        return json.loads(Path(path).read_bytes())

    monkeypatch.setattr(materializer, "build_v7_archive_source_map", build)
    monkeypatch.setattr(materializer, "write_v7_archive_source_map", write)
    monkeypatch.setattr(materializer, "load_v7_archive_source_map", load)

    @contextlib.contextmanager
    def lease() -> Iterator[object]:
        calls.append("lease_enter")
        yield object()
        calls.append("lease_exit")

    monkeypatch.setattr(materializer, "_authenticated_demo_lease", lease)
    return calls


def _cleanup(destination: Path, map_output: Path) -> None:
    if destination.exists():
        materializer._make_tree_writable(destination)
        shutil.rmtree(destination)
    if map_output.exists():
        map_output.unlink()


def test_atomic_publish_never_replaces_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "source-marker").write_text("source", encoding="utf-8")
    (destination / "destination-marker").write_text("destination", encoding="utf-8")

    with pytest.raises(FileExistsError):
        materializer._rename_noreplace(source, destination)

    assert (source / "source-marker").read_text(encoding="utf-8") == "source"
    assert (destination / "destination-marker").read_text(encoding="utf-8") == "destination"


def test_stopped_root_materialization_is_read_only_atomic_and_reopened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    calls = _stub_map_functions(monkeypatch, fixture)
    result = materializer.materialize_v7_from_stopped_roots(
        repository_root=fixture["repository"],
        expected_candidate_commit=fixture["candidate"],
        calibration_file=fixture["calibration"],
        destination_root=fixture["destination"],
        archive_map_output=fixture["map_output"],
        systemctl_bin=fixture["systemctl"],
    )

    destination = fixture["destination"]
    map_output = fixture["map_output"]
    try:
        assert result["source_mode"] == "stopped_live_roots"
        assert result["execution_authorization"] == "not_granted"
        assert calls == ["lease_enter", "build", "write", "load", "lease_exit"]
        assert (destination.stat().st_mode & 0o777) == 0o500
        assert ((destination / "account").stat().st_mode & 0o777) == 0o500
        archived_event = destination / "account/account_journal/transactions/event.json"
        archived_book = destination / "market_capture/2026-07-14/BTCUSDT/segment-000000.jsonl"
        assert (archived_event.stat().st_mode & 0o777) == 0o400
        assert (archived_book.stat().st_mode & 0o777) == 0o400
        assert archived_event.read_bytes() == b'{"event":"training"}\n'
        assert archived_book.read_bytes() == b'{"book":"training"}\n'
        assert (map_output.stat().st_mode & 0o777) == 0o600
        assert Path(fixture["account"]).is_dir(), "live V7 source must remain untouched"
    finally:
        _cleanup(destination, map_output)


def test_stopped_root_materialization_refuses_active_or_unknown_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _stub_map_functions(monkeypatch, fixture)
    active_systemctl = tmp_path / "systemctl-active"
    active_systemctl.write_text(
        "#!/usr/bin/env bash\nprintf 'active\\n'\nexit 0\n",
        encoding="utf-8",
    )
    active_systemctl.chmod(0o755)

    with pytest.raises(RuntimeError, match="still active"):
        materializer.materialize_v7_from_stopped_roots(
            repository_root=fixture["repository"],
            expected_candidate_commit=fixture["candidate"],
            calibration_file=fixture["calibration"],
            destination_root=fixture["destination"],
            archive_map_output=fixture["map_output"],
            systemctl_bin=active_systemctl,
        )
    assert not fixture["destination"].exists()
    assert not fixture["map_output"].exists()


def test_map_failure_rolls_back_materialized_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    _stub_map_functions(monkeypatch, fixture, fail_build=True)

    with pytest.raises(ValueError, match="synthetic map build failure"):
        materializer.materialize_v7_from_stopped_roots(
            repository_root=fixture["repository"],
            expected_candidate_commit=fixture["candidate"],
            calibration_file=fixture["calibration"],
            destination_root=fixture["destination"],
            archive_map_output=fixture["map_output"],
            systemctl_bin=fixture["systemctl"],
        )
    assert not fixture["destination"].exists()
    assert not fixture["map_output"].exists()
    assert Path(fixture["account"]).is_dir()


def test_map_publish_race_preserves_competing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    _stub_map_functions(monkeypatch, fixture)
    real_rename = materializer._rename_noreplace

    def race_map_publish(source: Path, destination: Path) -> None:
        if destination == fixture["map_output"]:
            destination.write_text("competing output\n", encoding="utf-8")
            destination.chmod(0o600)
        real_rename(source, destination)

    monkeypatch.setattr(materializer, "_rename_noreplace", race_map_publish)
    with pytest.raises(FileExistsError):
        materializer.materialize_v7_from_stopped_roots(
            repository_root=fixture["repository"],
            expected_candidate_commit=fixture["candidate"],
            calibration_file=fixture["calibration"],
            destination_root=fixture["destination"],
            archive_map_output=fixture["map_output"],
            systemctl_bin=fixture["systemctl"],
        )

    assert fixture["map_output"].read_text(encoding="utf-8") == "competing output\n"
    assert not fixture["destination"].exists()


def _reset_archive(fixture: dict[str, Any], *, unsafe_selected_symlink: bool = False) -> tuple[Path, Path]:
    repository = Path(fixture["repository"])
    account_prefix = Path(fixture["account"]).relative_to(repository).as_posix()
    capture_prefix = Path(fixture["capture"]).relative_to(repository).as_posix()
    manifest = (
        "\n".join(
            [
                f"git_head={fixture['candidate']}",
                "leave_stopped=1",
                f"demo_boundary={DEMO_BOUNDARY}",
                f"paper_boundary={PAPER_BOUNDARY}",
                f"account_epoch_target={account_prefix}",
                f"account_epoch_target={capture_prefix}",
                f"target={account_prefix}",
                f"target={capture_prefix}",
            ]
        ).encode()
        + b"\n"
    )
    archive = Path(fixture["repository"]).parent / "reset-v7.tar.gz"
    with tarfile.open(archive, mode="w:gz") as handle:
        manifest_member = tarfile.TarInfo("ledger-reset-manifest.txt")
        manifest_member.size = len(manifest)
        handle.addfile(manifest_member, io.BytesIO(manifest))
        account_data = b'{"event":"archived"}\n'
        account_member = tarfile.TarInfo(f"{account_prefix}/transactions/event.json")
        if unsafe_selected_symlink:
            account_member.type = tarfile.SYMTYPE
            account_member.linkname = "/etc/passwd"
            handle.addfile(account_member)
        else:
            account_member.size = len(account_data)
            handle.addfile(account_member, io.BytesIO(account_data))
        capture_data = b'{"book":"archived"}\n'
        capture_member = tarfile.TarInfo(f"{capture_prefix}/2026-07-14/BTCUSDT/segment-000000.jsonl")
        capture_member.size = len(capture_data)
        handle.addfile(capture_member, io.BytesIO(capture_data))
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)
    return archive.resolve(), sidecar.resolve()


def _replace_live_sources_with_fresh_roots(fixture: dict[str, Any]) -> None:
    for label in ("account", "capture"):
        root = Path(fixture[label])
        shutil.rmtree(root)
        root.mkdir(mode=0o700)


def test_reset_archive_materialization_verifies_sidecar_and_selected_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _stub_map_functions(monkeypatch, fixture)
    archive, sidecar = _reset_archive(fixture)
    _replace_live_sources_with_fresh_roots(fixture)
    result = materializer.materialize_v7_from_reset_archive(
        repository_root=fixture["repository"],
        expected_candidate_commit=fixture["candidate"],
        calibration_file=fixture["calibration"],
        reset_archive=archive,
        reset_sha256_sidecar=sidecar,
        destination_root=fixture["destination"],
        archive_map_output=fixture["map_output"],
    )

    destination = fixture["destination"]
    try:
        assert result["source_mode"] == "verified_reset_archive"
        archived_event = destination / "account/transactions/event.json"
        assert archived_event.read_bytes() == b'{"event":"archived"}\n'
        assert (archived_event.stat().st_mode & 0o777) == 0o400
    finally:
        _cleanup(destination, fixture["map_output"])


def test_reset_archive_materialization_refuses_reused_live_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _stub_map_functions(monkeypatch, fixture)
    archive, sidecar = _reset_archive(fixture)

    with pytest.raises(ValueError, match="already been reused"):
        materializer.materialize_v7_from_reset_archive(
            repository_root=fixture["repository"],
            expected_candidate_commit=fixture["candidate"],
            calibration_file=fixture["calibration"],
            reset_archive=archive,
            reset_sha256_sidecar=sidecar,
            destination_root=fixture["destination"],
            archive_map_output=fixture["map_output"],
        )

    assert not fixture["destination"].exists()
    assert not fixture["map_output"].exists()


def test_reset_archive_materialization_rejects_tamper_or_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    _stub_map_functions(monkeypatch, fixture)
    archive, sidecar = _reset_archive(fixture)
    _replace_live_sources_with_fresh_roots(fixture)
    sidecar.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)
    with pytest.raises(ValueError, match="sidecar"):
        materializer.materialize_v7_from_reset_archive(
            repository_root=fixture["repository"],
            expected_candidate_commit=fixture["candidate"],
            calibration_file=fixture["calibration"],
            reset_archive=archive,
            reset_sha256_sidecar=sidecar,
            destination_root=fixture["destination"],
            archive_map_output=fixture["map_output"],
        )

    archive.unlink()
    sidecar.unlink()
    archive, sidecar = _reset_archive(fixture, unsafe_selected_symlink=True)
    with pytest.raises(ValueError, match="unsafe member"):
        materializer.materialize_v7_from_reset_archive(
            repository_root=fixture["repository"],
            expected_candidate_commit=fixture["candidate"],
            calibration_file=fixture["calibration"],
            reset_archive=archive,
            reset_sha256_sidecar=sidecar,
            destination_root=fixture["destination"],
            archive_map_output=fixture["map_output"],
        )
    assert not fixture["destination"].exists()
    assert not fixture["map_output"].exists()
    assert len(MANAGED_UNITS) == 12
