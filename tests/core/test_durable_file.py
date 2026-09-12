from __future__ import annotations

import threading
from pathlib import Path

import pytest

from liquidity_migration.core import durable_file


def test_durable_create_does_not_publish_while_bytes_are_still_being_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "objects" / "book.json"
    write_started = threading.Event()
    release_write = threading.Event()
    real_write = durable_file.os.write
    errors: list[BaseException] = []

    def delayed_write(descriptor: int, data: bytes) -> int:
        write_started.set()
        assert release_write.wait(timeout=5.0)
        return real_write(descriptor, data)

    def create() -> None:
        try:
            durable_file.durable_create(target, b'{"complete":true}\n')
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(durable_file.os, "write", delayed_write)
    thread = threading.Thread(target=create)
    thread.start()
    assert write_started.wait(timeout=5.0)
    assert not target.exists()

    release_write.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert errors == []
    assert target.read_bytes() == b'{"complete":true}\n'
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_durable_create_keeps_an_existing_artifact_whole(tmp_path: Path) -> None:
    target = tmp_path / "object.json"
    durable_file.durable_create(target, b"first")

    with pytest.raises(FileExistsError):
        durable_file.durable_create(target, b"second")

    assert target.read_bytes() == b"first"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_durable_create_keeps_a_published_artifact_when_directory_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the rename lands, every reader can see the artifact. A directory
    that will not sync makes the *name* unproven, not the contents wrong, and
    deleting what readers can already see would be the worse failure."""

    target = tmp_path / "object.json"
    real_fsync = durable_file.os.fsync
    calls = 0

    def fail_directory_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(durable_file.os, "fsync", fail_directory_sync)

    with pytest.raises(durable_file.ArtifactDurabilityError, match="published but its directory entry is not durable"):
        durable_file.durable_create(target, b"complete")

    assert target.read_bytes() == b"complete", "the artifact the caller asked for is still there"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []
    # It is an OSError, so a caller that only catches OSError still sees it.
    assert isinstance(durable_file.ArtifactDurabilityError("x"), OSError)


def test_durable_create_publishes_nothing_when_the_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "object.json"

    def refuse(source, destination, *, label):  # type: ignore[no-untyped-def]
        raise OSError("rename failed")

    monkeypatch.setattr(durable_file, "rename_noreplace", refuse)
    with pytest.raises(OSError, match="rename failed"):
        durable_file.durable_create(target, b"complete")

    assert not target.exists()
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == [], "the temporary is cleaned up"
