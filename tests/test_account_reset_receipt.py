from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

from liquidity_migration import account_reset_receipt as reset_receipts
from liquidity_migration.account_reset_receipt import (
    DEMO_BOUNDARY,
    KIND,
    MANAGED_UNITS,
    PAPER_BOUNDARY,
    build_account_reset_receipt,
    load_account_reset_receipt,
    validate_account_reset_receipt_output,
    write_account_reset_receipt,
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
    (path / ".gitignore").write_text("data/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
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


def _fixture(
    tmp_path: Path,
    *,
    sleeves: tuple[str, ...] = ("long", "continuous"),
    archived_payload: bytes | None = None,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repo"
    candidate = _git_repository(repository)
    roots: dict[str, dict[str, Path]] = {"demo": {}, "paper": {}}
    relative: list[str] = []
    for environment in ("demo", "paper"):
        for kind in ("account", "inbox", "capture"):
            root = repository / "data" / f"{environment}-{kind}"
            root.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            roots[environment][kind] = root
            relative.append(root.relative_to(repository).as_posix())
    active_before = [MANAGED_UNITS[-2], MANAGED_UNITS[-1]]
    manifest = (
        "\n".join(
            [
                "ledger_reset_utc=20260714T120000Z",
                f"git_head={candidate}",
                f"sleeves={' '.join(sleeves)}",
                "include_reports=0",
                "include_caches=0",
                "leave_stopped=1",
                "env_file=/etc/liquidity-migration/bybit-demo.env",
                "account_env_file=/etc/liquidity-migration/account-execution.env",
                "paper_account_env_file=/etc/liquidity-migration/account-paper-execution.env",
                "demo_account_lease_path=/run/liquidity-migration/demo-account.lock",
                f"demo_boundary={DEMO_BOUNDARY}",
                f"paper_boundary={PAPER_BOUNDARY}",
                f"active_before={' '.join(active_before)}",
                *[f"account_epoch_target={value}" for value in relative],
            ]
        ).encode()
        + b"\n"
    )
    archive = tmp_path / "reset.tar.gz"
    with tarfile.open(archive, mode="w:gz") as handle:
        if archived_payload is not None:
            archived_member = tarfile.TarInfo(f"{relative[0]}/journal/events.bin")
            archived_member.size = len(archived_payload)
            archived_member.mode = 0o600
            handle.addfile(archived_member, io.BytesIO(archived_payload))
        member = tarfile.TarInfo("ledger-reset-manifest.txt")
        member.size = len(manifest)
        member.mode = 0o600
        handle.addfile(member, io.BytesIO(manifest))
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = tmp_path / "reset.tar.gz.sha256"
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)
    return {
        "repository_root": repository,
        "candidate_commit": candidate,
        "started_ts_ns": 1,
        "finished_ts_ns": 2,
        "sleeves": list(sleeves),
        "include_reports": False,
        "include_caches": False,
        "leave_stopped": True,
        "account_epoch_roots": roots,
        "managed_units": list(MANAGED_UNITS),
        "active_before": active_before,
        "inactive_after": list(MANAGED_UNITS),
        "archive_path": archive,
        "sha256_sidecar_path": sidecar,
    }


def test_build_write_load_reopens_archive_and_fresh_roots(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "reset-receipt.json"
    write_account_reset_receipt(output, payload)

    assert (output.stat().st_mode & 0o777) == 0o600
    assert output.stat().st_nlink == 1
    assert payload["kind"] == KIND
    assert payload["reset"]["fresh_roots_verified"] is True
    assert payload["services"]["inactive_after"] == list(MANAGED_UNITS)
    assert (
        load_account_reset_receipt(
            output,
            expected_candidate_commit=arguments["candidate_commit"],
            expected_roots=arguments["account_epoch_roots"],
            require_leave_stopped=True,
            require_fresh_roots=True,
        )
        == payload
    )


def test_build_rejects_dirty_candidate_checkout(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    repository = Path(arguments["repository_root"])
    (repository / "untracked-runtime.py").write_text("changed = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="candidate checkout is dirty"):
        build_account_reset_receipt(**arguments)


def test_build_binds_git_provenance_despite_caller_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path / "fixture")
    other_repository = tmp_path / "other-repo"
    _git_repository(other_repository)
    (other_repository / "other.txt").write_text("different candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=other_repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "other"],
        cwd=other_repository,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("GIT_DIR", str(other_repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other_repository))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other_repository / ".git" / "index"))

    payload = build_account_reset_receipt(**arguments)

    assert payload["repository"]["candidate_commit"] == arguments["candidate_commit"]


def test_build_rejects_dirty_checkout_despite_local_core_worktree(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path / "fixture")
    repository = Path(arguments["repository_root"])
    alternate_worktree = tmp_path / "alternate-clean-tree"
    alternate_worktree.mkdir()
    subprocess.run(
        ["git", "config", "core.worktree", str(alternate_worktree)],
        cwd=repository,
        check=True,
    )
    (repository / "untracked-runtime.py").write_text("changed = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="candidate checkout is dirty"):
        build_account_reset_receipt(**arguments)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_build_rejects_tracked_changes_hidden_by_index_flags(tmp_path: Path, flag: str) -> None:
    arguments = _fixture(tmp_path / "fixture")
    repository = Path(arguments["repository_root"])
    subprocess.run(
        ["git", "update-index", flag, ".gitignore"],
        cwd=repository,
        check=True,
    )
    (repository / ".gitignore").write_text("data/\n# modified\n", encoding="utf-8")

    with pytest.raises(ValueError, match="candidate checkout is dirty"):
        build_account_reset_receipt(**arguments)


def test_build_rejects_head_change_during_cleanliness_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path / "fixture")
    repository = Path(arguments["repository_root"])
    original_commit = str(arguments["candidate_commit"])
    (repository / "other.txt").write_text("next candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "next"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    next_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "reset", "--hard", original_commit], cwd=repository, check=True)
    actual_run = reset_receipts.subprocess.run
    changed = False

    def change_head_after_read_tree(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        nonlocal changed
        completed = actual_run(*args, **kwargs)
        command = args[0]
        if not changed and "read-tree" in command:
            changed = True
            actual_run(
                ["git", "update-ref", "HEAD", next_commit],
                cwd=repository,
                check=True,
            )
        return completed

    monkeypatch.setattr(reset_receipts.subprocess, "run", change_head_after_read_tree)

    with pytest.raises(ValueError, match="HEAD changed during verification"):
        build_account_reset_receipt(**arguments)
    assert changed is True


def test_schema_v1_legacy_shared_compat_selection_remains_readable(tmp_path: Path) -> None:
    arguments = _fixture(
        tmp_path,
        sleeves=("long", "continuous", "retire-shared-compat"),
    )

    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "legacy-selection-receipt.json"
    write_account_reset_receipt(output, payload)

    assert load_account_reset_receipt(output)["reset"]["sleeves"] == [
        "long",
        "continuous",
        "retire-shared-compat",
    ]

    with pytest.raises(FileExistsError):
        write_account_reset_receipt(output, payload)


def test_load_rejects_archive_mutation(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    output = tmp_path / "reset-receipt.json"
    write_account_reset_receipt(output, build_account_reset_receipt(**arguments))
    archive = Path(arguments["archive_path"])
    archive.write_bytes(archive.read_bytes() + b"changed")
    archive.chmod(0o600)

    with pytest.raises(ValueError, match="sidecar|changed|readable gzip"):
        load_account_reset_receipt(output)


def test_archive_verification_rejects_a_corrupt_gzip_trailer(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    archive = Path(arguments["archive_path"])
    archive_data = bytearray(archive.read_bytes())
    archive_data[-8] ^= 0xFF
    archive.write_bytes(archive_data)
    archive.chmod(0o600)
    sidecar = Path(arguments["sha256_sidecar_path"])
    digest = hashlib.sha256(archive_data).hexdigest()
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)

    with pytest.raises(ValueError, match="readable gzip"):
        build_account_reset_receipt(**arguments)


def test_archive_verification_streams_without_a_whole_file_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(
        tmp_path,
        archived_payload=os.urandom(2 * 1024 * 1024),
    )
    archive = Path(arguments["archive_path"])
    archive_metadata = archive.stat()
    original_private_snapshot = reset_receipts._private_snapshot
    original_read = os.read
    original_tar_next = tarfile.TarFile.next
    requested_sizes: list[int] = []
    returned_bytes = 0
    max_cached_members = 0

    def reject_archive_snapshot(
        path: str | Path,
        *,
        label: str,
        max_bytes: int | None = None,
    ) -> Any:
        if Path(path).expanduser().absolute() == archive.absolute():
            raise AssertionError("archive verification must not materialize a StableFileSnapshot")
        return original_private_snapshot(path, label=label, max_bytes=max_bytes)

    def limited_read(descriptor: int, size: int) -> bytes:
        nonlocal returned_bytes
        metadata = os.fstat(descriptor)
        chunk = original_read(descriptor, size)
        if (metadata.st_dev, metadata.st_ino) == (
            archive_metadata.st_dev,
            archive_metadata.st_ino,
        ):
            if size > 1024 * 1024:
                raise AssertionError("archive verification requested an unbounded read")
            requested_sizes.append(size)
            returned_bytes += len(chunk)
        return chunk

    def limited_tar_next(handle: tarfile.TarFile) -> tarfile.TarInfo | None:
        nonlocal max_cached_members
        member = original_tar_next(handle)
        cached_members = cast(Any, handle).members
        max_cached_members = max(max_cached_members, len(cached_members))
        if len(cached_members) > 1:
            raise AssertionError("archive verification retained prior tar member metadata")
        return member

    monkeypatch.setattr(reset_receipts, "_private_snapshot", reject_archive_snapshot)
    monkeypatch.setattr(reset_receipts.os, "read", limited_read)
    monkeypatch.setattr(reset_receipts.tarfile.TarFile, "next", limited_tar_next)

    payload = build_account_reset_receipt(**arguments)

    assert requested_sizes
    assert max(requested_sizes) <= 1024 * 1024
    assert returned_bytes == archive_metadata.st_size
    assert max_cached_members == 1
    assert payload["archive"]["archived_account_epoch_presence"]["data/demo-account"] is True


def test_failed_final_reopen_removes_success_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = _fixture(tmp_path)
    output = tmp_path / "reset-receipt.json"
    payload = build_account_reset_receipt(**arguments)
    actual_load = reset_receipts._load_account_reset_receipt_snapshot
    loads = 0

    def fail_final_reopen(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal loads
        loads += 1
        if loads == 2:
            raise RuntimeError("synthetic final reopen failure")
        return actual_load(*args, **kwargs)

    monkeypatch.setattr(
        reset_receipts,
        "_load_account_reset_receipt_snapshot",
        fail_final_reopen,
    )
    with pytest.raises(RuntimeError, match="synthetic final reopen failure"):
        write_account_reset_receipt(output, payload)

    assert loads == 2
    assert not output.exists(), "a failed reset-receipt transaction cannot leave a pass"


def test_fresh_root_check_is_explicit_and_time_scoped(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    output = tmp_path / "reset-receipt.json"
    payload = build_account_reset_receipt(**arguments)
    write_account_reset_receipt(output, payload)
    demo_account = Path(arguments["account_epoch_roots"]["demo"]["account"])
    (demo_account / "later-natural-event.json").write_text("later\n", encoding="utf-8")

    assert load_account_reset_receipt(output) == payload
    with pytest.raises(ValueError, match="not empty"):
        load_account_reset_receipt(output, require_fresh_roots=True)


def test_fresh_root_allows_only_validated_persistent_lock_skeleton(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    roots = arguments["account_epoch_roots"]
    for environment in ("demo", "paper"):
        for kind in ("account", "inbox", "capture"):
            root = Path(roots[environment][kind])
            lock_directory = root / ".locks"
            lock_directory.mkdir(mode=0o700)
            lock_directory.chmod(0o700)
            leaves = [lock_directory / "persistent.lock"]
            if kind == "account":
                leaves.extend(
                    (
                        root / "account_execution_owner.lock",
                        root / "account_journal" / "journal.lock",
                    )
                )
            if kind == "inbox":
                leaves.append(
                    lock_directory / f".account_intent_inbox.lock.create-{'a' * 32}"
                )
            for leaf in leaves:
                leaf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                leaf.parent.chmod(0o700)
                leaf.touch(mode=0o600)
                leaf.chmod(0o600)

    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "lock-skeleton-receipt.json"
    write_account_reset_receipt(output, payload)

    assert load_account_reset_receipt(
        output,
        require_fresh_roots=True,
    ) == payload


def test_fresh_root_accepts_nonwritable_legacy_lock_ancestor_mode(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    root = Path(arguments["account_epoch_roots"]["demo"]["account"])
    journal = root / "account_journal"
    journal.mkdir(mode=0o755)
    journal.chmod(0o755)
    lock = journal / "journal.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)

    payload = build_account_reset_receipt(**arguments)

    assert payload["reset"]["fresh_roots_verified"] is True


def test_fresh_root_rejects_lock_ancestor_without_owner_write(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    root = Path(arguments["account_epoch_roots"]["demo"]["account"])
    journal = root / "account_journal"
    journal.mkdir(mode=0o700)
    lock = journal / "journal.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    journal.chmod(0o500)

    with pytest.raises(ValueError, match="unsafe directory state"):
        build_account_reset_receipt(**arguments)


def test_fresh_root_rejects_unsafe_lock_namespace_inode(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    root = Path(arguments["account_epoch_roots"]["demo"]["account"])
    lock_directory = root / ".locks"
    lock_directory.mkdir(mode=0o700)
    lock_path = lock_directory / "persistent.lock"
    lock_path.touch(mode=0o600)
    os.link(lock_path, tmp_path / "external-alias.lock")

    with pytest.raises(ValueError, match="unsafe filesystem state"):
        build_account_reset_receipt(**arguments)


def test_build_rejects_sidecar_or_service_claim_mismatch(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    sidecar = Path(arguments["sha256_sidecar_path"])
    sidecar.write_text(f"{'0' * 64}  reset.tar.gz\n", encoding="utf-8")
    sidecar.chmod(0o600)
    with pytest.raises(ValueError, match="sidecar"):
        build_account_reset_receipt(**arguments)

    arguments = _fixture(tmp_path / "other")
    arguments["inactive_after"] = list(MANAGED_UNITS[:-1])
    with pytest.raises(ValueError, match="every managed unit inactive"):
        build_account_reset_receipt(**arguments)


def test_build_rejects_oversized_sidecar_before_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path)
    sidecar = Path(arguments["sha256_sidecar_path"])
    sidecar.write_bytes(b"x" * 2048)
    sidecar.chmod(0o600)
    sidecar_metadata = sidecar.stat()
    original_read = os.read

    def reject_sidecar_read(descriptor: int, size: int) -> bytes:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (
            sidecar_metadata.st_dev,
            sidecar_metadata.st_ino,
        ):
            raise AssertionError("oversized sidecar must be rejected before reading")
        return original_read(descriptor, size)

    monkeypatch.setattr(reset_receipts.os, "read", reject_sidecar_read)

    with pytest.raises(ValueError, match="1024-byte size limit"):
        build_account_reset_receipt(**arguments)


def test_preflight_rejects_existing_relative_or_nested_output(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    existing = tmp_path / "receipt.json"
    existing.write_text(json.dumps({}), encoding="utf-8")

    with pytest.raises(ValueError, match="absolute"):
        validate_account_reset_receipt_output(Path("relative.json"))
    with pytest.raises(FileExistsError):
        validate_account_reset_receipt_output(existing)
    with pytest.raises(ValueError, match="inside a reset root"):
        validate_account_reset_receipt_output(root / "receipt.json", forbidden_roots=[root])


def test_build_ignores_a_caller_controlled_git_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path / "fixture")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "fake-git-ran"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        f"#!/bin/sh\ntouch '{marker}'\nexit 91\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    payload = build_account_reset_receipt(**arguments)

    assert payload["repository"]["candidate_commit"] == arguments["candidate_commit"]
    assert not marker.exists()


def test_write_rejects_a_payload_after_checkout_head_changes(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    repository = Path(arguments["repository_root"])
    (repository / "next.txt").write_text("next\n", encoding="utf-8")
    subprocess.run(["git", "add", "next.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "next"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    output = tmp_path / "stale-candidate-receipt.json"

    with pytest.raises(ValueError, match="HEAD changed during account reset receipt publication"):
        write_account_reset_receipt(output, payload)

    assert not output.exists()


def test_write_removes_receipt_when_checkout_changes_after_final_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    repository = Path(arguments["repository_root"])
    output = tmp_path / "raced-candidate-receipt.json"
    actual_load = reset_receipts._load_account_reset_receipt_snapshot
    loads = 0
    changed = False

    def load_then_change_head(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal changed, loads
        loads += 1
        loaded = actual_load(*args, **kwargs)
        if loads == 2:
            changed = True
            (repository / "next.txt").write_text("next\n", encoding="utf-8")
            subprocess.run(["git", "add", "next.txt"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "next"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
        return loaded

    monkeypatch.setattr(
        reset_receipts,
        "_load_account_reset_receipt_snapshot",
        load_then_change_head,
    )

    with pytest.raises(ValueError, match="HEAD changed during account reset receipt publication"):
        write_account_reset_receipt(output, payload)

    assert changed is True
    assert loads == 2
    assert not output.exists()


def test_historical_load_does_not_depend_on_current_checkout_head(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "historical-receipt.json"
    write_account_reset_receipt(output, payload)
    repository = Path(arguments["repository_root"])
    (repository / "next.txt").write_text("next\n", encoding="utf-8")
    subprocess.run(["git", "add", "next.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "next"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert load_account_reset_receipt(output) == payload


def test_load_rejects_oversized_receipt_before_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "oversized-receipt.json"
    output.write_bytes(b"x" * (reset_receipts._MAX_RECEIPT_SIZE + 1))
    output.chmod(0o600)
    metadata = output.stat()
    actual_read = os.read

    def reject_receipt_read(descriptor: int, size: int) -> bytes:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == (metadata.st_dev, metadata.st_ino):
            raise AssertionError("oversized receipt must be rejected before reading")
        return actual_read(descriptor, size)

    monkeypatch.setattr(reset_receipts.os, "read", reject_receipt_read)

    with pytest.raises(ValueError, match="1048576-byte size limit"):
        load_account_reset_receipt(output)


def test_write_removes_receipt_from_held_parent_when_path_is_redirected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path / "fixture")
    payload = build_account_reset_receipt(**arguments)
    parent = tmp_path / "receipt-parent"
    parent.mkdir()
    moved_parent = tmp_path / "moved-receipt-parent"
    redirected_parent = tmp_path / "redirected-parent"
    redirected_parent.mkdir()
    output = parent / "receipt.json"
    actual_check = reset_receipts._require_current_candidate
    redirected = False

    def redirect_after_parent_open(candidate: dict[str, Any]) -> None:
        nonlocal redirected
        actual_check(candidate)
        if not redirected:
            redirected = True
            parent.rename(moved_parent)
            parent.symlink_to(redirected_parent, target_is_directory=True)

    monkeypatch.setattr(reset_receipts, "_require_current_candidate", redirect_after_parent_open)

    with pytest.raises(RuntimeError, match="parent path or mount changed"):
        write_account_reset_receipt(output, payload)

    assert redirected is True
    assert not (moved_parent / "receipt.json").exists()
    assert not (redirected_parent / "receipt.json").exists()


def test_fresh_root_rejects_directory_replaced_by_symlink_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path)
    root = Path(arguments["account_epoch_roots"]["demo"]["account"])
    lock_directory = root / ".locks"
    lock_directory.mkdir(mode=0o700)
    replacement = tmp_path / "external-locks"
    replacement.mkdir(mode=0o700)
    moved = root / ".locks-moved"
    root_identity = (root.stat().st_dev, root.stat().st_ino)
    actual_entry_metadata = reset_receipts._entry_metadata
    replaced = False

    def replace_after_stat(directory_fd: int, name: str) -> os.stat_result:
        nonlocal replaced
        observed = actual_entry_metadata(directory_fd, name)
        directory = os.fstat(directory_fd)
        if not replaced and (directory.st_dev, directory.st_ino) == root_identity and name == ".locks":
            replaced = True
            lock_directory.rename(moved)
            lock_directory.symlink_to(replacement, target_is_directory=True)
        return observed

    monkeypatch.setattr(reset_receipts, "_entry_metadata", replace_after_stat)

    with pytest.raises(ValueError, match="unsafe directory state"):
        build_account_reset_receipt(**arguments)

    assert replaced is True


def test_fresh_root_rejects_nested_mount_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path)
    root = Path(arguments["account_epoch_roots"]["demo"]["account"])
    lock_directory = root / ".locks"
    lock_directory.mkdir(mode=0o700)
    lock_identity = (lock_directory.stat().st_dev, lock_directory.stat().st_ino)
    actual_mount_id = reset_receipts._mount_id_for_fd

    def alternate_mount_id(descriptor: int) -> int | None:
        observed = actual_mount_id(descriptor)
        metadata = os.fstat(descriptor)
        if observed is not None and (metadata.st_dev, metadata.st_ino) == lock_identity:
            return observed + 1
        return observed

    monkeypatch.setattr(reset_receipts, "_mount_id_for_fd", alternate_mount_id)

    descriptor = os.open(root, os.O_RDONLY)
    try:
        mount_id_available = actual_mount_id(descriptor) is not None
    finally:
        os.close(descriptor)
    if not mount_id_available:
        pytest.skip("mount identities are available only on Linux")
    with pytest.raises(ValueError, match="unsafe directory state"):
        build_account_reset_receipt(**arguments)


def test_failed_final_publication_check_removes_success_receipt(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "service-raced-receipt.json"

    def fail_service_check() -> None:
        raise ValueError("synthetic active service")

    with pytest.raises(ValueError, match="synthetic active service"):
        write_account_reset_receipt(
            output,
            payload,
            final_publication_check=fail_service_check,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".account-reset-receipt-stage-*"))


def test_post_link_failure_observes_only_an_invalid_final_receipt(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "post-link-service-raced-receipt.json"
    checks = 0

    def fail_after_link() -> None:
        nonlocal checks
        checks += 1
        if checks == 1:
            assert not output.exists()
            return
        metadata = output.stat()
        assert stat.S_IMODE(metadata.st_mode) == 0o400
        assert metadata.st_nlink == 1
        with pytest.raises(ValueError, match="mode 0600"):
            load_account_reset_receipt(output)
        raise ValueError("synthetic post-link active service")

    with pytest.raises(ValueError, match="synthetic post-link active service"):
        write_account_reset_receipt(
            output,
            payload,
            final_publication_check=fail_after_link,
        )

    assert checks == 2
    assert not output.exists()
    assert not list(tmp_path.glob(".account-reset-receipt-stage-*"))


def test_staging_receipt_is_not_loadable_and_is_removed_before_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "staging-failure-receipt.json"

    def fail_before_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        assert destination == output.name
        assert src_dir_fd == dst_dir_fd
        assert follow_symlinks is False
        metadata = os.stat(source, dir_fd=src_dir_fd, follow_symlinks=False)
        assert stat.S_IMODE(metadata.st_mode) == 0o400
        assert metadata.st_nlink == 1
        with pytest.raises(ValueError, match="mode 0600"):
            load_account_reset_receipt(tmp_path / source)
        raise OSError("synthetic pre-publication failure")

    monkeypatch.setattr(reset_receipts.os, "link", fail_before_link)

    with pytest.raises(OSError, match="synthetic pre-publication failure"):
        write_account_reset_receipt(output, payload)

    assert not output.exists()
    assert not list(tmp_path.glob(".account-reset-receipt-stage-*"))


def test_atomic_publication_preserves_a_late_final_name_collision(
    tmp_path: Path,
) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "collision-receipt.json"
    collision = b"foreign\n"
    checked = False

    def create_collision() -> None:
        nonlocal checked
        if not checked:
            checked = True
            output.write_bytes(collision)
            output.chmod(0o600)

    with pytest.raises(FileExistsError, match="already exists"):
        write_account_reset_receipt(
            output,
            payload,
            final_publication_check=create_collision,
        )

    assert output.read_bytes() == collision
    assert not list(tmp_path.glob(".account-reset-receipt-stage-*"))


def test_systemd_inactivity_observation_uses_trusted_executable_and_fixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/usr/bin/true")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        environment = cast(dict[str, str], kwargs["env"])
        calls.append((command, environment))
        value = "loaded\n" if command[-2] == "--property=LoadState" else "inactive\n"
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")

    monkeypatch.setattr(reset_receipts.subprocess, "run", fake_run)

    assert reset_receipts._observe_managed_units_inactive(executable, MANAGED_UNITS) == list(
        MANAGED_UNITS
    )
    assert calls
    assert all(command[0] == str(executable) for command, _environment in calls)
    assert all(
        environment == {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
        }
        for _command, environment in calls
    )


def test_systemd_inactivity_observation_rejects_an_active_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/usr/bin/true")

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        value = "loaded\n" if command[-2] == "--property=LoadState" else "active\n"
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")

    monkeypatch.setattr(reset_receipts.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="not loaded and inactive"):
        reset_receipts._observe_managed_units_inactive(executable, MANAGED_UNITS)
