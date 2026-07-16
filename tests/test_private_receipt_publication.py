from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

import liquidity_migration.private_receipt_publication as publication
from liquidity_migration.artifact_snapshot import StableFileSnapshot, read_stable_file
from liquidity_migration.private_receipt_publication import publish_private_receipt


DATA = b'{"status":"passed"}\n'
LABEL = "private test receipt"
STAGING_PREFIX = ".private-test-receipt-stage-"


def _publish(
    output: Path,
    *,
    committed_mode: int = 0o600,
    committed_gid: int | None = None,
    max_bytes: int = 1024,
    validate_uncommitted: Callable[[StableFileSnapshot], None] | None = None,
    revalidate_sources: Callable[[], None] | None = None,
    forbidden_roots: tuple[Path, ...] = (),
) -> Path:
    def validate(snapshot: StableFileSnapshot) -> None:
        assert snapshot.data == DATA
        assert snapshot.mode == 0o400
        assert snapshot.nlink == 1

    def revalidate() -> None:
        return None

    validator = validate if validate_uncommitted is None else validate_uncommitted
    source_check = revalidate if revalidate_sources is None else revalidate_sources
    return publish_private_receipt(
        output,
        DATA,
        label=LABEL,
        staging_prefix=STAGING_PREFIX,
        committed_mode=committed_mode,
        committed_gid=committed_gid,
        max_bytes=max_bytes,
        validate_uncommitted=validator,
        revalidate_sources=source_check,
        forbidden_roots=forbidden_roots,
    )


def _stages(parent: Path) -> list[Path]:
    return list(parent.glob(f"{STAGING_PREFIX}*"))


def test_publish_uses_two_uncommitted_validations_before_mode_commit(
    tmp_path: Path,
) -> None:
    output = tmp_path / "receipt.json"
    snapshots: list[StableFileSnapshot] = []
    source_checks = 0

    def validate(snapshot: StableFileSnapshot) -> None:
        snapshots.append(snapshot)
        assert snapshot.data == DATA
        assert snapshot.mode == 0o400
        assert snapshot.nlink == 1

    def revalidate() -> None:
        nonlocal source_checks
        source_checks += 1
        if source_checks == 1:
            assert not output.exists()
        else:
            metadata = output.stat()
            assert stat.S_IMODE(metadata.st_mode) == 0o400
            assert metadata.st_nlink == 1
            with pytest.raises(ValueError, match="mode 0600"):
                read_stable_file(
                    output,
                    label="committed receipt",
                    require_mode=0o600,
                )

    result = _publish(
        output,
        validate_uncommitted=validate,
        revalidate_sources=revalidate,
    )

    assert result == output
    assert len(snapshots) == 2
    assert snapshots[0].path.parent == tmp_path
    assert snapshots[0].path.name.startswith(STAGING_PREFIX)
    assert snapshots[1].path == output
    assert source_checks == 2
    assert output.read_bytes() == DATA
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1
    assert not _stages(tmp_path)


def test_publish_commits_group_readable_mode_only_at_the_end(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    observed_modes: list[int] = []

    def revalidate() -> None:
        if output.exists():
            observed_modes.append(stat.S_IMODE(output.stat().st_mode))

    _publish(
        output,
        committed_mode=0o640,
        committed_gid=os.getegid(),
        revalidate_sources=revalidate,
    )

    assert observed_modes == [0o400]
    metadata = output.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o640
    assert metadata.st_gid == os.getegid()


def test_validation_failure_removes_the_private_stage(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"

    def reject(_snapshot: StableFileSnapshot) -> None:
        raise ValueError("synthetic schema failure")

    with pytest.raises(ValueError, match="synthetic schema failure"):
        _publish(output, validate_uncommitted=reject)

    assert not output.exists()
    assert not _stages(tmp_path)


def test_post_link_source_failure_observes_only_an_uncommitted_final(
    tmp_path: Path,
) -> None:
    output = tmp_path / "receipt.json"
    checks = 0

    def reject_second_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            metadata = output.stat()
            assert stat.S_IMODE(metadata.st_mode) == 0o400
            assert metadata.st_nlink == 1
            raise ValueError("synthetic stale source")

    with pytest.raises(ValueError, match="synthetic stale source"):
        _publish(output, revalidate_sources=reject_second_check)

    assert checks == 2
    assert not output.exists()
    assert not _stages(tmp_path)


def test_late_final_collision_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    collision = b"foreign\n"
    checks = 0

    def create_collision() -> None:
        nonlocal checks
        checks += 1
        if checks == 1:
            output.write_bytes(collision)
            output.chmod(0o600)

    with pytest.raises(FileExistsError, match="already exists"):
        _publish(output, revalidate_sources=create_collision)

    assert checks == 1
    assert output.read_bytes() == collision
    assert not _stages(tmp_path)


def test_cleanup_does_not_unlink_a_foreign_final_replacement(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    foreign = b"foreign replacement\n"
    checks = 0

    def replace_and_fail() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            output.unlink()
            output.write_bytes(foreign)
            output.chmod(0o600)
            raise ValueError("synthetic replacement race")

    with pytest.raises(ValueError, match="synthetic replacement race"):
        _publish(output, revalidate_sources=replace_and_fail)

    assert checks == 2
    assert output.read_bytes() == foreign
    assert not _stages(tmp_path)


def test_post_validation_content_change_cannot_cross_the_mode_commit(
    tmp_path: Path,
) -> None:
    output = tmp_path / "receipt.json"
    checks = 0

    def corrupt_after_validation() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            output.chmod(0o600)
            output.write_bytes(b"x" * len(DATA))
            output.chmod(0o400)

    with pytest.raises(RuntimeError, match="content changed before commit"):
        _publish(output, revalidate_sources=corrupt_after_validation)

    assert checks == 2
    assert not output.exists()
    assert not _stages(tmp_path)


def test_parent_redirect_is_detected_and_cleanup_uses_held_directory(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "receipt-parent"
    parent.mkdir()
    moved_parent = tmp_path / "moved-parent"
    redirected_parent = tmp_path / "redirected-parent"
    redirected_parent.mkdir()
    output = parent / "receipt.json"
    redirected = False

    def redirect_parent() -> None:
        nonlocal redirected
        if not redirected:
            redirected = True
            parent.rename(moved_parent)
            parent.symlink_to(redirected_parent, target_is_directory=True)

    with pytest.raises(RuntimeError, match="parent path or mount changed"):
        _publish(output, revalidate_sources=redirect_parent)

    assert redirected is True
    assert not (moved_parent / output.name).exists()
    assert not (redirected_parent / output.name).exists()
    assert not _stages(moved_parent)


def test_mount_identity_change_is_detected_before_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "receipt.json"
    calls = 0

    def changing_mount_id(_descriptor: int) -> int:
        nonlocal calls
        calls += 1
        return 1 if calls == 4 else 0

    monkeypatch.setattr(publication, "_mount_id_for_fd", changing_mount_id)

    with pytest.raises(RuntimeError, match="parent path or mount changed"):
        _publish(output)

    assert calls == 4
    assert not output.exists()
    assert not _stages(tmp_path)


def test_existing_final_is_exclusive_and_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    original = b"existing\n"
    output.write_bytes(original)
    output.chmod(0o600)

    with pytest.raises(FileExistsError, match="already exists"):
        _publish(output)

    assert output.read_bytes() == original
    assert not _stages(tmp_path)


def test_size_and_forbidden_root_fail_before_staging(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"

    with pytest.raises(ValueError, match="10-byte size limit"):
        _publish(output, max_bytes=10)
    assert not _stages(tmp_path)

    with pytest.raises(ValueError, match="inside a forbidden root"):
        _publish(output, forbidden_roots=(tmp_path,))
    assert not output.exists()
    assert not _stages(tmp_path)


def test_writable_output_parent_is_rejected_before_staging(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe-parent"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    output = unsafe / "receipt.json"

    with pytest.raises(ValueError, match="not writable by group or other"):
        _publish(output)

    assert not output.exists()
    assert not _stages(unsafe)


def test_parent_permissions_are_revalidated_before_mode_commit(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    checks = 0

    def weaken_parent_after_link() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            tmp_path.chmod(0o777)

    try:
        with pytest.raises(ValueError, match="not writable by group or other"):
            _publish(output, revalidate_sources=weaken_parent_after_link)
    finally:
        tmp_path.chmod(0o700)

    assert checks == 2
    assert not output.exists()
    assert not _stages(tmp_path)


def test_staging_namespace_exhaustion_preserves_the_existing_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "receipt.json"
    stage = tmp_path / f"{STAGING_PREFIX}fixed"
    original = b"foreign stage\n"
    stage.write_bytes(original)
    stage.chmod(0o400)
    monkeypatch.setattr(publication.secrets, "token_hex", lambda _size: "fixed")

    with pytest.raises(RuntimeError, match="staging namespace is exhausted"):
        _publish(output)

    assert stage.read_bytes() == original
    assert not output.exists()


@pytest.mark.parametrize("mode", [0o400, 0o644, 0o660])
def test_publish_rejects_modes_that_are_not_private_commits(
    tmp_path: Path,
    mode: int,
) -> None:
    with pytest.raises(ValueError, match="committed mode must be 0600 or 0640"):
        _publish(tmp_path / "receipt.json", committed_mode=mode)


def test_group_readable_commit_requires_an_explicit_group(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires an explicit committed group"):
        _publish(tmp_path / "receipt.json", committed_mode=0o640)


def test_post_commit_fsync_failure_removes_only_the_created_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "receipt.json"
    actual_fsync = os.fsync
    calls = 0

    def fail_committed_file_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            assert output.exists()
            assert stat.S_IMODE(output.stat().st_mode) == 0o600
            raise OSError("synthetic committed-file fsync failure")
        actual_fsync(descriptor)

    monkeypatch.setattr(publication.os, "fsync", fail_committed_file_fsync)

    with pytest.raises(OSError, match="synthetic committed-file fsync failure"):
        _publish(output)

    assert calls == 6
    assert not output.exists()
    assert not _stages(tmp_path)
