from __future__ import annotations

import os
from pathlib import Path

import pytest

import liquidity_migration.core.artifact_snapshot as snapshot_module
from liquidity_migration.core.artifact_snapshot import read_stable_file, rename_noreplace


def test_read_stable_file_returns_descriptor_bound_bytes(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b'{"gate":"passed"}\n')
    path.chmod(0o600)

    snapshot = read_stable_file(
        path,
        label="evidence",
        require_mode=0o600,
        require_owner=True,
    )

    assert snapshot.path == path.absolute()
    assert snapshot.data == b'{"gate":"passed"}\n'
    assert snapshot.size == len(snapshot.data)
    assert snapshot.mode == 0o600
    assert snapshot.device == path.stat().st_dev
    assert snapshot.inode == path.stat().st_ino


def test_read_stable_file_rejects_path_replacement_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "evidence.json"
    replacement = tmp_path / "replacement.json"
    path.write_bytes(b"original\n")
    replacement.write_bytes(b"replacement\n")
    original_open = os.open
    replaced = False

    def replacing_open(candidate: str, flags: int, *args: int) -> int:
        nonlocal replaced
        if Path(candidate) == path and not replaced:
            replaced = True
            replacement.replace(path)
        return original_open(candidate, flags, *args)

    monkeypatch.setattr(snapshot_module.os, "open", replacing_open)

    with pytest.raises(RuntimeError, match="path changed while it was opened"):
        read_stable_file(path, label="evidence")


def test_read_stable_file_rejects_hard_links_by_default(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    alias = tmp_path / "evidence-alias.json"
    path.write_bytes(b"evidence\n")
    os.link(path, alias)

    with pytest.raises(ValueError, match="must not be hard-linked"):
        read_stable_file(path, label="evidence")


def test_read_stable_file_enforces_a_descriptor_size_bound(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"bounded\n")

    assert read_stable_file(path, label="evidence", max_bytes=8).data == b"bounded\n"
    with pytest.raises(ValueError, match="7-byte size limit"):
        read_stable_file(path, label="evidence", max_bytes=7)


def test_rename_noreplace_preserves_an_existing_evidence_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "staging"
    destination = tmp_path / "published"
    source.mkdir()
    (source / "new.txt").write_text("new\n", encoding="utf-8")
    destination.mkdir()
    (destination / "preserved.txt").write_text("preserved\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        rename_noreplace(source, destination, label="evidence output")

    assert (source / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert (destination / "preserved.txt").read_text(encoding="utf-8") == (
        "preserved\n"
    )
