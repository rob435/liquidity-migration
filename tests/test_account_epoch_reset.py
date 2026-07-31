from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from liquidity_migration.ops import account_epoch_reset as epoch_reset
from liquidity_migration.ops.account_epoch_reset import clear_account_epoch_roots_preserving_locks
from liquidity_migration.account.account_route import ensure_account_route, require_account_route


def _private_file(path: Path, payload: str = "lock\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_clear_account_epoch_root_preserves_lock_inodes_and_removes_payload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    locks = (
        _private_file(root / "account_execution_owner.lock"),
        _private_file(root / "account_journal" / "journal.lock"),
        _private_file(root / ".locks" / "account_route.lock"),
        _private_file(root / ".locks" / f".account_route.lock.create-{'a' * 32}"),
    )
    identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in locks}
    _private_file(root / "account_route.json", "route-state\n")
    _private_file(root / "account_journal" / "transactions" / "000001.json", "event\n")
    external = tmp_path / "external"
    external.write_text("keep\n", encoding="utf-8")
    (root / "payload-link").symlink_to(external)

    (result,) = clear_account_epoch_roots_preserving_locks((root,))

    assert result.root == root.absolute()
    assert result.removed_entries >= 4
    assert set(result.preserved_lock_files) == set(locks)
    assert {path: (path.stat().st_dev, path.stat().st_ino) for path in locks} == identities
    assert external.read_text(encoding="utf-8") == "keep\n"
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) == sorted(
        path.relative_to(root).as_posix() for path in locks
    )


def test_batch_preflight_prevents_partial_clear_on_later_unsafe_lock(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    payload = _private_file(first / "epoch.json", "must-survive\n")
    unsafe = _private_file(second / ".locks" / "unsafe.lock")
    os.link(unsafe, tmp_path / "external-hardlink.lock")

    with pytest.raises(ValueError, match="persistent account lock is unsafe"):
        clear_account_epoch_roots_preserving_locks((first, second))

    assert payload.read_text(encoding="utf-8") == "must-survive\n"


def test_clear_rejects_symlinked_lock_namespace_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    target = tmp_path / "foreign-locks"
    target.mkdir(mode=0o700)
    marker = _private_file(target / "marker.lock")
    (root / ".locks").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="account epoch directory must be real"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert marker.exists()


def test_clear_rejects_same_device_nested_mount_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    nested = root / "external-bind-mount"
    nested.mkdir(mode=0o700)
    victim = _private_file(nested / "must-survive.json", "external evidence\n")
    nested_identity = (nested.stat().st_dev, nested.stat().st_ino)

    def mocked_mount_id(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == nested_identity else 101

    monkeypatch.setattr(epoch_reset, "_mount_id_for_fd", mocked_mount_id)

    with pytest.raises(ValueError, match="refuses a nested mount boundary"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert victim.read_text(encoding="utf-8") == "external evidence\n"


def test_clear_rejects_same_device_root_mount_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "accounts"
    root = parent / "account"
    root.mkdir(parents=True, mode=0o700)
    victim = _private_file(root / "must-survive.json", "external evidence\n")
    root_identity = (root.stat().st_dev, root.stat().st_ino)

    def mocked_mount_id(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == root_identity else 101

    monkeypatch.setattr(epoch_reset, "_mount_id_for_fd", mocked_mount_id)

    with pytest.raises(ValueError, match="refuses a root mount boundary"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert victim.read_text(encoding="utf-8") == "external evidence\n"


def test_clear_rejects_same_device_regular_file_mount_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    victim = _private_file(root / "must-survive.json", "external evidence\n")

    monkeypatch.setattr(epoch_reset, "_mount_id_for_fd", lambda _descriptor: 101)

    def mocked_entry_mount_id(
        _directory_fd: int,
        _name: str,
        path: Path,
        _observed: os.stat_result,
    ) -> int:
        return 202 if path == victim else 101

    monkeypatch.setattr(epoch_reset, "_entry_mount_id", mocked_entry_mount_id)

    with pytest.raises(ValueError, match="refuses a nested mount boundary"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert victim.read_text(encoding="utf-8") == "external evidence\n"


def test_clear_rejects_lock_ancestor_without_owner_rwx(tmp_path: Path) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    lock_directory = root / ".locks"
    lock_directory.mkdir(mode=0o500)

    with pytest.raises(ValueError, match="directory is not owner-controlled"):
        clear_account_epoch_roots_preserving_locks((root,))


def test_clear_rejects_root_without_owner_rwx(tmp_path: Path) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o500)

    with pytest.raises(ValueError, match="root is not owner-controlled"):
        clear_account_epoch_roots_preserving_locks((root,))


def test_clear_accepts_owner_rwx_with_readonly_group_and_world(tmp_path: Path) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o755)
    payload = _private_file(root / "epoch.json", "old epoch\n")
    root.chmod(0o755)

    clear_account_epoch_roots_preserving_locks((root,))

    assert not payload.exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_linux_fdinfo_mount_identity_fails_closed_when_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fdinfo_root = tmp_path / "fdinfo"
    fdinfo_root.mkdir()
    (fdinfo_root / "17").write_text("flags:\t0100000\n", encoding="utf-8")
    monkeypatch.setattr(epoch_reset, "_LINUX_FDINFO_ROOT", fdinfo_root)

    with pytest.raises(RuntimeError, match="Linux mount identity is invalid"):
        epoch_reset._mount_id_for_fd(17)


def test_linux_fdinfo_mount_identity_parses_single_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fdinfo_root = tmp_path / "fdinfo"
    fdinfo_root.mkdir()
    (fdinfo_root / "23").write_text("pos:\t0\nflags:\t0100000\nmnt_id:\t417\n", encoding="utf-8")
    monkeypatch.setattr(epoch_reset, "_LINUX_FDINFO_ROOT", fdinfo_root)

    assert epoch_reset._mount_id_for_fd(23) == 417


def test_clear_refuses_parent_symlink_swap_without_deleting_external_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    nested = root / "payload"
    nested.mkdir(mode=0o700)
    _private_file(nested / "old.json", "old epoch\n")
    displaced = tmp_path / "displaced-payload"
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    victim = _private_file(external / "must-survive.json", "external evidence\n")
    original_entry_metadata = epoch_reset._entry_metadata
    swapped = False

    def swap_after_directory_stat(directory_fd: int, name: str) -> os.stat_result:
        nonlocal swapped
        metadata = original_entry_metadata(directory_fd, name)
        if name == nested.name and not swapped:
            swapped = True
            nested.rename(displaced)
            nested.symlink_to(external, target_is_directory=True)
        return metadata

    monkeypatch.setattr(epoch_reset, "_entry_metadata", swap_after_directory_stat)

    with pytest.raises(ValueError, match="directory changed while inspected"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "external evidence\n"
    assert (displaced / "old.json").read_text(encoding="utf-8") == "old epoch\n"


def test_apply_refuses_planned_parent_symlink_swap_without_external_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    nested = root / "payload"
    nested.mkdir(mode=0o700)
    old_payload = _private_file(nested / "old.json", "old epoch\n")
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    victim = _private_file(external / "must-survive.json", "external evidence\n")

    plan = epoch_reset._build_plan(root.absolute())
    displaced = tmp_path / "displaced-payload"
    nested.rename(displaced)
    nested.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="directory changed during clear"):
        epoch_reset._apply_plan(plan)

    assert victim.read_text(encoding="utf-8") == "external evidence\n"
    assert (displaced / old_payload.name).read_text(encoding="utf-8") == "old epoch\n"


@pytest.mark.parametrize("mutation", ["replacement", "mode"])
def test_apply_final_root_revalidation_refuses_changed_canonical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    _private_file(root / "epoch.json", "old epoch\n")
    plan = epoch_reset._build_plan(root.absolute())
    displaced = tmp_path / "displaced-account"
    original_fsync = epoch_reset._fsync_directory_fd
    mutated = False

    def mutate_after_first_fsync(descriptor: int) -> None:
        nonlocal mutated
        original_fsync(descriptor)
        if mutated:
            return
        mutated = True
        if mutation == "replacement":
            root.rename(displaced)
            root.mkdir(mode=0o700)
        else:
            root.chmod(0o755)

    monkeypatch.setattr(epoch_reset, "_fsync_directory_fd", mutate_after_first_fsync)

    with pytest.raises(RuntimeError, match="root changed during clear"):
        epoch_reset._apply_plan(plan)

    assert mutated is True


def test_apply_final_directory_revalidation_refuses_replaced_empty_lock_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    lock_directory = root / ".locks"
    lock_directory.mkdir(mode=0o700)
    plan = epoch_reset._build_plan(root.absolute())
    displaced = tmp_path / "displaced-locks"
    lock_directory.rename(displaced)
    lock_directory.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="directory changed during clear"):
        epoch_reset._apply_plan(plan)

    assert displaced.is_dir()


def test_clear_creates_missing_distinct_root_without_recursive_aliasing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    (result,) = clear_account_epoch_roots_preserving_locks((missing,))

    assert result.removed_entries == 0
    assert missing.is_dir()
    assert (missing.stat().st_mode & 0o777) == 0o700


def test_missing_root_creation_reopens_identity_after_durability_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    displaced = tmp_path / "displaced-created-root"
    plan = epoch_reset._build_plan(missing.absolute())
    original_fsync = epoch_reset._fsync_directory_fd
    fsync_calls = 0
    replacement_identity: tuple[int, int] | None = None

    def replace_after_parent_fsync(descriptor: int) -> None:
        nonlocal fsync_calls, replacement_identity
        original_fsync(descriptor)
        fsync_calls += 1
        if fsync_calls == 2:
            missing.rename(displaced)
            missing.mkdir(mode=0o700)
            replacement_identity = (missing.stat().st_dev, missing.stat().st_ino)

    monkeypatch.setattr(epoch_reset, "_fsync_directory_fd", replace_after_parent_fsync)

    with pytest.raises(RuntimeError, match="root changed during creation"):
        epoch_reset._apply_plan(plan)

    assert fsync_calls == 2
    assert displaced.is_dir()
    assert missing.is_dir()
    assert (missing.stat().st_dev, missing.stat().st_ino) == replacement_identity


def test_missing_root_plan_binds_parent_identity_before_creation(tmp_path: Path) -> None:
    parent = tmp_path / "accounts"
    parent.mkdir(mode=0o700)
    missing = parent / "missing"
    plan = epoch_reset._build_plan(missing.absolute())
    displaced = tmp_path / "displaced-accounts"
    parent.rename(displaced)
    parent.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="parent changed"):
        epoch_reset._apply_plan(plan)

    assert not (parent / "missing").exists()
    assert not (displaced / "missing").exists()


def test_batch_rejects_existing_roots_aliased_through_an_intermediate_symlink(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    branch = real / "branch"
    root = branch / "account"
    root.mkdir(parents=True, mode=0o700)
    branch.chmod(0o700)
    payload = _private_file(root / "must-survive.json", "old epoch\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="root must be a real directory"):
        clear_account_epoch_roots_preserving_locks((root, alias / "branch" / "account"))

    assert payload.read_text(encoding="utf-8") == "old epoch\n"


def test_single_root_rejects_intermediate_symlink_without_clearing_target(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    branch = real / "branch"
    root = branch / "account"
    root.mkdir(parents=True, mode=0o700)
    branch.chmod(0o700)
    payload = _private_file(root / "must-survive.json", "old epoch\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="root must be a real directory"):
        clear_account_epoch_roots_preserving_locks((alias / "branch" / "account",))

    assert payload.read_text(encoding="utf-8") == "old epoch\n"


def test_batch_rejects_missing_roots_aliased_to_the_same_parent_entry(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    branch = real / "branch"
    branch.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    first = branch / "missing"
    second = alias / "branch" / "missing"

    with pytest.raises(ValueError, match="root must be a real directory"):
        clear_account_epoch_roots_preserving_locks((first, second))

    assert not first.exists()


def test_final_rescan_rejects_entry_created_after_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    _private_file(root / "old.json", "old epoch\n")
    late = root / "late.json"
    original_fsync = epoch_reset._fsync_directory_fd
    injected = False

    def inject_after_removal(descriptor: int) -> None:
        nonlocal injected
        original_fsync(descriptor)
        if not injected and not (root / "old.json").exists():
            injected = True
            _private_file(late, "late writer\n")

    monkeypatch.setattr(epoch_reset, "_fsync_directory_fd", inject_after_removal)

    with pytest.raises(RuntimeError, match="changed after clear"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert injected is True
    assert late.read_text(encoding="utf-8") == "late writer\n"


def test_final_rescan_rejects_replaced_empty_preserved_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "account"
    root.mkdir(mode=0o700)
    locks = root / ".locks"
    locks.mkdir(mode=0o700)
    displaced = tmp_path / "displaced-locks"
    original_fsync = epoch_reset._fsync_directory_fd
    replaced = False

    def replace_after_durability_boundary(descriptor: int) -> None:
        nonlocal replaced
        original_fsync(descriptor)
        if not replaced:
            replaced = True
            locks.rename(displaced)
            locks.mkdir(mode=0o700)

    monkeypatch.setattr(epoch_reset, "_fsync_directory_fd", replace_after_durability_boundary)

    with pytest.raises(RuntimeError, match="changed after clear"):
        clear_account_epoch_roots_preserving_locks((root,))

    assert replaced is True
    assert displaced.is_dir()
    assert locks.is_dir()


def test_clear_then_route_rebind_preserves_every_mutex_inode(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    original_route = ensure_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    locks = (
        _private_file(account_root / "account_execution_owner.lock"),
        _private_file(account_root / "account_journal" / "journal.lock"),
        account_root / ".locks" / "account_route.lock",
        inbox_root / ".locks" / "account_route.lock",
        _private_file(inbox_root / ".locks" / "account_intent_inbox.lock"),
        _private_file(inbox_root / ".locks" / f".account_intent_inbox.lock.create-{'b' * 32}"),
    )
    for path in locks:
        path.chmod(0o600)
    identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in locks}
    _private_file(account_root / "account_journal" / "transactions" / "000001.json")
    _private_file(inbox_root / "pending" / "request.json")

    clear_account_epoch_roots_preserving_locks((account_root, inbox_root))
    rebound = ensure_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=account_root,
        inbox_root=inbox_root,
    )

    assert rebound == original_route
    assert (
        require_account_route(
            account_id="bybit-paper-unified",
            environment="paper",
            account_root=account_root,
            inbox_root=inbox_root,
        )
        == original_route
    )
    assert {path: (path.stat().st_dev, path.stat().st_ino) for path in locks} == identities
