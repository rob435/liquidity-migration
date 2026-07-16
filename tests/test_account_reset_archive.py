from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.account_reset_archive as reset_archive
from liquidity_migration.account_reset_archive import (
    create_reset_archive,
    verify_reset_archive,
)


def _private_file(path: Path, payload: bytes = b"payload\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repo"
    target = repository / "data" / "account"
    target.mkdir(parents=True)
    _private_file(target / "journal" / "000001.json")
    archive_directory = repository / "data" / "_archive"
    archive_directory.mkdir(mode=0o700)
    manifest = _private_file(tmp_path / "manifest" / "ledger-reset-manifest.txt", b"manifest\n")
    return repository, archive_directory, manifest


def _create(tmp_path: Path) -> reset_archive.ResetArchive:
    repository, archive_directory, manifest = _fixture(tmp_path)
    return create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=("data/account",),
    )


def test_create_publishes_private_descriptor_bound_archive_and_sidecar(tmp_path: Path) -> None:
    artifact = _create(tmp_path)

    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600
    assert artifact.path.stat().st_nlink == 1
    assert stat.S_IMODE(artifact.sidecar_path.stat().st_mode) == 0o600
    assert artifact.sidecar_path.stat().st_nlink == 1
    assert artifact.path.stat().st_size == artifact.size_bytes
    assert hashlib.sha256(artifact.path.read_bytes()).hexdigest() == artifact.sha256
    assert artifact.sidecar_path.read_text(encoding="utf-8") == (
        f"{artifact.sha256}  {artifact.path.name}\n"
    )
    with tarfile.open(artifact.path, mode="r:gz") as handle:
        assert "data/account/journal/000001.json" in handle.getnames()
        assert "ledger-reset-manifest.txt" in handle.getnames()

    verified = verify_reset_archive(
        repository=(tmp_path / "repo").absolute(),
        archive=artifact.path,
        sidecar=artifact.sidecar_path,
        expected_sha256=artifact.sha256,
        expected_device=artifact.device,
        expected_inode=artifact.inode,
        expected_size=artifact.size_bytes,
    )
    assert verified == artifact


def test_create_supports_manifest_only_archive_for_fully_absent_layout(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    archive_directory = repository / "data" / "_archive"
    archive_directory.mkdir(parents=True, mode=0o700)
    manifest = _private_file(tmp_path / "manifest" / "ledger-reset-manifest.txt", b"manifest\n")

    artifact = create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=(),
    )

    with tarfile.open(artifact.path, mode="r:gz") as handle:
        names = handle.getnames()
        assert names.count("ledger-reset-manifest.txt") == 1
        assert not any(name.startswith("data/") for name in names)


def test_create_never_follows_planted_archive_symlink(tmp_path: Path) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    victim = _private_file(tmp_path / "victim.txt", b"must survive\n")
    planted = archive_directory / "ledger-reset-20260716T120000Z.tar.gz"
    planted.symlink_to(victim)

    artifact = create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=("data/account",),
    )

    assert artifact.path.name == "ledger-reset-20260716T120000Z-2.tar.gz"
    assert planted.is_symlink()
    assert victim.read_bytes() == b"must survive\n"


def test_create_does_not_inherit_tar_dereference_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    victim = _private_file(external / "secret.txt", b"must not be archived\n")
    link = repository / "data" / "account" / "external-link"
    link.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("TAR_OPTIONS", "--dereference")
    actual_run = reset_archive.subprocess.run
    child_commands: list[list[str]] = []
    child_environments: list[dict[str, str]] = []

    def record_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        child_commands.append(list(args[0]))
        child_environments.append(dict(kwargs["env"]))
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(reset_archive.subprocess, "run", record_run)

    artifact = create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=("data/account",),
    )

    with tarfile.open(artifact.path, mode="r:gz") as handle:
        member = handle.getmember("data/account/external-link")
        assert member.issym()
        assert f"data/account/external-link/{victim.name}" not in handle.getnames()
    assert len(child_environments) == 2
    assert all("TAR_OPTIONS" not in environment for environment in child_environments)
    assert all(
        environment["PATH"] == reset_archive._TRUSTED_EXECUTABLE_PATH
        for environment in child_environments
    )
    assert child_commands[0][-3:] == ["-C", str(manifest.parent), manifest.name]


@pytest.mark.parametrize("manifest_name", ["-checkpoint-action=exec=sh", "manifest\n.txt"])
def test_create_rejects_manifest_basenames_unsafe_for_tar(
    tmp_path: Path,
    manifest_name: str,
) -> None:
    repository, archive_directory, _manifest = _fixture(tmp_path)
    manifest = _private_file(tmp_path / "manifest" / manifest_name, b"manifest\n")

    with pytest.raises(ValueError, match="manifest basename is invalid"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=archive_directory,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert list(archive_directory.iterdir()) == []


def test_create_does_not_resolve_tar_from_inherited_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_tar = fake_bin / "tar"
    fake_tar.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
    fake_tar.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    artifact = create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=("data/account",),
    )

    assert artifact.path.is_file()


@pytest.mark.parametrize(
    ("mode", "final"),
    [
        (0o0777, False),
        (0o1777, True),
    ],
)
def test_trusted_directory_rejects_unsafe_writable_components(
    mode: int,
    final: bool,
) -> None:
    metadata = type(
        "DirectoryMetadata",
        (),
        {"st_mode": stat.S_IFDIR | mode, "st_uid": 0},
    )()

    with pytest.raises(ValueError, match="component is not trusted"):
        reset_archive._trusted_directory(metadata, path=Path("/unsafe"), final=final)  # type: ignore[arg-type]


def test_trusted_directory_allows_root_owned_sticky_writable_ancestor() -> None:
    metadata = type(
        "DirectoryMetadata",
        (),
        {"st_mode": stat.S_IFDIR | 0o1777, "st_uid": 0},
    )()

    reset_archive._trusted_directory(metadata, path=Path("/tmp"), final=False)  # type: ignore[arg-type]


def test_create_refuses_planted_sidecar_and_cleans_new_archive(tmp_path: Path) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    victim = _private_file(tmp_path / "victim.txt", b"must survive\n")
    sidecar = archive_directory / "ledger-reset-20260716T120000Z.tar.gz.sha256"
    sidecar.symlink_to(victim)

    with pytest.raises(RuntimeError, match="cannot be created exclusively"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=archive_directory,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert victim.read_bytes() == b"must survive\n"
    assert sidecar.is_symlink()
    assert not (archive_directory / "ledger-reset-20260716T120000Z.tar.gz").exists()


def test_create_refuses_symlink_archive_directory(tmp_path: Path) -> None:
    repository, _archive_directory, manifest = _fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    symlink = repository / "data" / "archive-link"
    symlink.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="component is unavailable"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=symlink,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert list(external.iterdir()) == []


def test_create_refuses_group_writable_archive_directory(tmp_path: Path) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    archive_directory.chmod(0o770)

    with pytest.raises(ValueError, match="component is not trusted"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=archive_directory,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )


def test_create_refuses_archive_directory_that_contains_target(tmp_path: Path) -> None:
    repository, _archive_directory, manifest = _fixture(tmp_path)
    data_root = repository / "data"
    original_mode = stat.S_IMODE(data_root.stat().st_mode)

    with pytest.raises(ValueError, match="must be disjoint"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=data_root,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert stat.S_IMODE(data_root.stat().st_mode) == original_mode


def test_create_refuses_alternate_mount_view_of_reset_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    mount_ids = iter((101, 101, 202, 101))

    def alternate_mount_id(_descriptor: int) -> int:
        return next(mount_ids)

    monkeypatch.setattr(reset_archive, "_mount_id_for_fd", alternate_mount_id)

    with pytest.raises(ValueError, match="alternate mount view"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=archive_directory,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert list(archive_directory.iterdir()) == []


def test_create_rejects_hidden_parent_mount_before_creating_missing_archive_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    archive_directory.rmdir()
    mount_ids = iter((202, 101))
    monkeypatch.setattr(
        reset_archive,
        "_mount_id_for_fd",
        lambda _descriptor: next(mount_ids),
    )

    with pytest.raises(ValueError, match="alternate mount view"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=archive_directory,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert not archive_directory.exists()


def test_create_leaves_safe_existing_archive_directory_mode_unchanged(tmp_path: Path) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    archive_directory.chmod(0o755)

    create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=("data/account",),
    )

    assert stat.S_IMODE(archive_directory.stat().st_mode) == 0o755


def test_create_uses_private_mode_for_new_archive_directory(tmp_path: Path) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)
    archive_directory.rmdir()

    create_reset_archive(
        repository=repository.absolute(),
        archive_directory=archive_directory,
        stem="ledger-reset-20260716T120000Z",
        manifest=manifest.absolute(),
        targets=("data/account",),
    )

    assert stat.S_IMODE(archive_directory.stat().st_mode) == 0o700


def test_verify_rejects_archive_path_replacement(tmp_path: Path) -> None:
    artifact = _create(tmp_path)
    displaced = artifact.path.with_suffix(".displaced")
    artifact.path.rename(displaced)
    _private_file(artifact.path, displaced.read_bytes())

    with pytest.raises((RuntimeError, ValueError), match="archive"):
        verify_reset_archive(
            repository=(tmp_path / "repo").absolute(),
            archive=artifact.path,
            sidecar=artifact.sidecar_path,
            expected_sha256=artifact.sha256,
            expected_device=artifact.device,
            expected_inode=artifact.inode,
            expected_size=artifact.size_bytes,
        )


def test_verify_rejects_archive_hardlink(tmp_path: Path) -> None:
    artifact = _create(tmp_path)
    os.link(artifact.path, tmp_path / "archive-hardlink.tar.gz")

    with pytest.raises(ValueError, match="not hard-linked"):
        verify_reset_archive(
            repository=(tmp_path / "repo").absolute(),
            archive=artifact.path,
            sidecar=artifact.sidecar_path,
            expected_sha256=artifact.sha256,
            expected_device=artifact.device,
            expected_inode=artifact.inode,
            expected_size=artifact.size_bytes,
        )


def test_verify_rejects_archive_mutation(tmp_path: Path) -> None:
    artifact = _create(tmp_path)
    with artifact.path.open("ab") as handle:
        handle.write(b"mutation")

    with pytest.raises(RuntimeError, match="size changed"):
        verify_reset_archive(
            repository=(tmp_path / "repo").absolute(),
            archive=artifact.path,
            sidecar=artifact.sidecar_path,
            expected_sha256=artifact.sha256,
            expected_device=artifact.device,
            expected_inode=artifact.inode,
            expected_size=artifact.size_bytes,
        )


def test_tar_failure_removes_unpublished_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, archive_directory, manifest = _fixture(tmp_path)

    def fail_tar(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(2, "tar")

    monkeypatch.setattr(reset_archive.subprocess, "run", fail_tar)

    with pytest.raises(RuntimeError, match="tar creation failed"):
        create_reset_archive(
            repository=repository.absolute(),
            archive_directory=archive_directory,
            stem="ledger-reset-20260716T120000Z",
            manifest=manifest.absolute(),
            targets=("data/account",),
        )

    assert list(archive_directory.iterdir()) == []
