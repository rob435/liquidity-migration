from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from liquidity_migration.ops import reset_path_safety as safety
from liquidity_migration.ops.reset_path_safety import (
    normalize_demo_runtime_roots,
    normalize_paper_runtime_roots,
    preflight_demo_runtime_roots,
    preflight_paper_runtime_roots,
    preflight_reset_targets,
    remove_reset_targets,
)


def _file(path: Path, payload: str = "payload\n", mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    path.chmod(mode)
    return path


def test_preflight_and_remove_are_descriptor_rooted_and_do_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    target = anchor / "strategy" / "generated"
    nested = target / "nested"
    nested.mkdir(parents=True)
    _file(target / "root.json")
    _file(nested / "row.parquet")
    external = tmp_path / "external"
    external.mkdir()
    victim = _file(external / "must-survive.json", "external evidence\n")
    (target / "external-link").symlink_to(external, target_is_directory=True)

    (inspection,) = preflight_reset_targets(anchor, (target,))
    (removal,) = remove_reset_targets(anchor, (target,))

    assert inspection.exists is True
    assert inspection.entries == 5
    assert removal.existed is True
    assert removal.removed_entries == 5
    assert not target.exists()
    assert (anchor / "strategy").is_dir()
    assert victim.read_text(encoding="utf-8") == "external evidence\n"


def test_remove_accepts_regular_file_and_symlink_targets_without_following_them(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    anchor.mkdir()
    regular = _file(anchor / "generated.json")
    external = _file(tmp_path / "outside.json", "keep\n")
    link = anchor / "outside-link"
    link.symlink_to(external)

    results = remove_reset_targets(anchor, (regular, link))

    assert [result.removed_entries for result in results] == [1, 1]
    assert not regular.exists()
    assert not link.exists()
    assert external.read_text(encoding="utf-8") == "keep\n"


def test_batch_preflight_rejects_later_hardlink_before_first_unlink(tmp_path: Path) -> None:
    anchor = tmp_path / "data"
    first = anchor / "first"
    second = anchor / "second"
    first.mkdir(parents=True)
    second.mkdir()
    survivor = _file(first / "must-survive.json")
    linked = _file(second / "linked.json")
    os.link(linked, tmp_path / "external-hardlink.json")

    with pytest.raises(ValueError, match="multiply-linked regular file"):
        remove_reset_targets(anchor, (first, second))

    assert survivor.read_text(encoding="utf-8") == "payload\n"


@pytest.mark.parametrize("mounted_kind", ["root", "directory", "file"])
def test_preflight_rejects_same_device_mount_ids_at_every_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mounted_kind: str,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "strategy"
    nested = root / "nested"
    nested.mkdir(parents=True)
    leaf = _file(nested / "row.parquet", "external evidence\n")
    mounted_path = {"root": root, "directory": nested, "file": leaf}[mounted_kind]

    monkeypatch.setattr(safety, "_mount_id_for_fd", lambda _descriptor: 101)

    def mocked_entry_mount_id(
        _directory_fd: int,
        _name: str,
        path: Path,
        _observed: os.stat_result,
    ) -> int:
        return 202 if path == mounted_path else 101

    monkeypatch.setattr(safety, "_entry_mount_id", mocked_entry_mount_id)

    with pytest.raises(ValueError, match="crosses a mount boundary"):
        preflight_reset_targets(anchor, (root,))

    assert leaf.read_text(encoding="utf-8") == "external evidence\n"


def test_preflight_refuses_directory_to_symlink_swap_without_touching_external_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "strategy"
    nested = root / "nested"
    nested.mkdir(parents=True)
    _file(nested / "local.json")
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    external.mkdir()
    victim = _file(external / "must-survive.json", "external evidence\n")
    original_metadata = safety._entry_metadata
    swapped = False

    def swap_after_stat(directory_fd: int, name: str) -> os.stat_result:
        nonlocal swapped
        metadata = original_metadata(directory_fd, name)
        if name == nested.name and not swapped:
            swapped = True
            nested.rename(displaced)
            nested.symlink_to(external, target_is_directory=True)
        return metadata

    monkeypatch.setattr(safety, "_entry_metadata", swap_after_stat)

    with pytest.raises(ValueError, match="directory changed while inspected"):
        preflight_reset_targets(anchor, (root,))

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "external evidence\n"
    assert (displaced / "local.json").exists()


def test_remove_refuses_planned_parent_symlink_swap_without_external_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    target = anchor / "strategy" / "generated"
    target.mkdir(parents=True)
    _file(target / "old.json")
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    external.mkdir()
    victim = _file(external / "must-survive.json", "external evidence\n")
    original_validate = safety._ApplyContext.validate_entry
    swapped = False

    def swap_before_validate(
        context: safety._ApplyContext,
        planned: safety._Entry,
    ) -> tuple[int, str, os.stat_result]:
        nonlocal swapped
        if planned.path.name == "old.json" and not swapped:
            swapped = True
            target.rename(displaced)
            target.symlink_to(external, target_is_directory=True)
        return original_validate(context, planned)

    monkeypatch.setattr(safety._ApplyContext, "validate_entry", swap_before_validate)

    with pytest.raises(RuntimeError, match="reset directory changed before mutation"):
        remove_reset_targets(anchor, (target,))

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "external evidence\n"
    assert (displaced / "old.json").exists()


def test_remove_preflights_missing_targets_and_refuses_late_appearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    anchor.mkdir()
    missing = anchor / "missing"
    original_assert = safety._assert_missing_target

    def create_before_absence_check(context: safety._ApplyContext, target: safety._Target) -> None:
        missing.mkdir()
        original_assert(context, target)

    monkeypatch.setattr(safety, "_assert_missing_target", create_before_absence_check)

    with pytest.raises(RuntimeError, match="appeared before mutation"):
        remove_reset_targets(anchor, (missing,))

    assert missing.is_dir()


def test_remove_rechecks_missing_targets_after_other_removals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    existing = anchor / "existing"
    existing.mkdir(parents=True)
    _file(existing / "old.json")
    missing = anchor / "missing"
    original_validate = safety._ApplyContext.validate_entry
    injected = False

    def create_missing_during_remove(
        context: safety._ApplyContext,
        planned: safety._Entry,
    ) -> tuple[int, str, os.stat_result]:
        nonlocal injected
        if not injected:
            injected = True
            missing.mkdir()
        return original_validate(context, planned)

    monkeypatch.setattr(safety._ApplyContext, "validate_entry", create_missing_during_remove)

    with pytest.raises(RuntimeError, match="appeared before mutation"):
        remove_reset_targets(anchor, (existing, missing))

    assert injected is True
    assert missing.is_dir()


def test_remove_rechecks_that_deleted_target_did_not_reappear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    target = anchor / "generated"
    target.mkdir(parents=True)
    _file(target / "old.json")
    original_rmdir = safety.os.rmdir
    injected = False

    def recreate_after_rmdir(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal injected
        original_rmdir(name, dir_fd=dir_fd)
        if name == target.name and not injected:
            injected = True
            os.mkdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(safety.os, "rmdir", recreate_after_rmdir)

    with pytest.raises(RuntimeError, match="reappeared during mutation"):
        remove_reset_targets(anchor, (target,))

    assert injected is True
    assert target.is_dir()


def test_paper_normalization_sets_exact_private_permissions_and_creates_locks(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    account = anchor / "account"
    strategy = anchor / "paper-strategy"
    (account / "journal").mkdir(parents=True, mode=0o755)
    strategy.mkdir(mode=0o755)
    account.chmod(0o755)
    (account / "journal").chmod(0o755)
    strategy.chmod(0o755)
    _file(account / "journal" / "event.json", mode=0o644)
    _file(strategy / "cycle.parquet", mode=0o644)
    missing = anchor / "missing"

    results = normalize_paper_runtime_roots(
        anchor,
        (account, strategy, missing),
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert {result.path: result.existed for result in results} == {
        account.absolute(): True,
        strategy.absolute(): True,
        missing.absolute(): False,
    }
    assert not missing.exists()
    for root in (account, strategy):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        locks = root / ".locks"
        assert locks.is_dir()
        assert stat.S_IMODE(locks.stat().st_mode) == 0o700
        assert locks.stat().st_uid == os.getuid()
        assert locks.stat().st_gid == os.getgid()
    assert stat.S_IMODE((account / "journal").stat().st_mode) == 0o700
    assert stat.S_IMODE((account / "journal" / "event.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((strategy / "cycle.parquet").stat().st_mode) == 0o600


def test_paper_normalization_noop_tree_skips_permission_writes_and_syncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    nested = root / "journal"
    locks = root / ".locks"
    nested.mkdir(parents=True)
    locks.mkdir()
    root.chmod(0o700)
    nested.chmod(0o700)
    locks.chmod(0o700)
    _file(nested / "event.json", mode=0o600)
    final_rescan_called = False
    original_verify = safety._verify_normalized_paper_tree

    def observe_final_rescan(
        original: safety._InspectionPlan,
        *,
        created_entries: dict[tuple[str, ...], safety._Entry],
        uid: int,
        gid: int,
    ) -> None:
        nonlocal final_rescan_called
        final_rescan_called = True
        original_verify(
            original,
            created_entries=created_entries,
            uid=uid,
            gid=gid,
        )

    def unexpected_mutation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an already-normalized paper tree must not be rewritten")

    monkeypatch.setattr(safety, "_normalize_planned_regular", unexpected_mutation)
    monkeypatch.setattr(safety, "_normalize_planned_directory", unexpected_mutation)
    monkeypatch.setattr(safety.os, "fchmod", unexpected_mutation)
    monkeypatch.setattr(safety.os, "fsync", unexpected_mutation)
    monkeypatch.setattr(safety, "_verify_normalized_paper_tree", observe_final_rescan)

    normalize_paper_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())

    assert final_rescan_called is True


def test_paper_normalization_noop_tree_final_rescan_rejects_late_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    locks = root / ".locks"
    locks.mkdir(parents=True)
    root.chmod(0o700)
    locks.chmod(0o700)
    _file(root / "event.json", mode=0o600)
    original_verify = safety._verify_normalized_paper_tree

    def add_late_entry(
        original: safety._InspectionPlan,
        *,
        created_entries: dict[tuple[str, ...], safety._Entry],
        uid: int,
        gid: int,
    ) -> None:
        _file(root / "late.json", mode=0o600)
        original_verify(
            original,
            created_entries=created_entries,
            uid=uid,
            gid=gid,
        )

    monkeypatch.setattr(safety, "_verify_normalized_paper_tree", add_late_entry)

    with pytest.raises(RuntimeError, match="tree entries changed during normalization"):
        normalize_paper_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())


def test_paper_normalization_bounds_open_directory_cache_for_wide_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    for index in range(128):
        child = root / f"partition-{index:03d}"
        child.mkdir()
        _file(child / "event.json")

    original_directory_fd = safety._ApplyContext.directory_fd
    maximum_open = 0

    def observe_open_count(
        context: safety._ApplyContext,
        relative_parts: tuple[str, ...],
    ) -> int:
        nonlocal maximum_open
        descriptor = original_directory_fd(context, relative_parts)
        maximum_open = max(maximum_open, len(context.open_directories))
        return descriptor

    monkeypatch.setattr(safety._ApplyContext, "directory_fd", observe_open_count)

    normalize_paper_runtime_roots(
        anchor,
        (root,),
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert maximum_open <= 3


def test_paper_normalization_bounds_open_directory_cache_across_many_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    roots: list[Path] = []
    for index in range(128):
        root = anchor / f"paper-{index:03d}"
        (root / ".locks").mkdir(parents=True)
        roots.append(root)

    original_directory_fd = safety._ApplyContext.directory_fd
    maximum_open = 0

    def observe_open_count(
        context: safety._ApplyContext,
        relative_parts: tuple[str, ...],
    ) -> int:
        nonlocal maximum_open
        descriptor = original_directory_fd(context, relative_parts)
        maximum_open = max(maximum_open, len(context.open_directories))
        return descriptor

    monkeypatch.setattr(safety._ApplyContext, "directory_fd", observe_open_count)

    normalize_paper_runtime_roots(
        anchor,
        roots,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert maximum_open <= 3


def test_paper_normalization_batch_rejects_symlink_before_permissions_change(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    first = anchor / "first"
    second = anchor / "second"
    first.mkdir(parents=True, mode=0o755)
    second.mkdir(mode=0o755)
    first.chmod(0o755)
    second.chmod(0o755)
    _file(first / "event.json", mode=0o644)
    external = tmp_path / "external"
    external.mkdir()
    victim = _file(external / "must-survive.json", "external evidence\n", mode=0o644)
    (second / "external-link").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="paper runtime tree contains a symlink"):
        normalize_paper_runtime_roots(
            anchor,
            (first, second),
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert stat.S_IMODE(first.stat().st_mode) == 0o755
    assert stat.S_IMODE((first / "event.json").stat().st_mode) == 0o644
    assert victim.read_text(encoding="utf-8") == "external evidence\n"


def test_paper_normalization_rejects_unsafe_lock_namespace_before_mutation(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True, mode=0o755)
    _file(root / ".locks", "not a directory\n", mode=0o644)

    with pytest.raises(ValueError, match="lock namespace must be a real directory"):
        normalize_paper_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())

    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_paper_normalization_safely_creates_missing_direct_child_roots(tmp_path: Path) -> None:
    anchor = tmp_path / "data"
    anchor.mkdir()
    missing = anchor / "paper"

    (result,) = normalize_paper_runtime_roots(
        anchor,
        (missing,),
        uid=os.getuid(),
        gid=os.getgid(),
        create_missing=True,
    )

    assert result.existed is False
    assert result.root_created is True
    assert result.normalized_directories == 2
    assert stat.S_IMODE(missing.stat().st_mode) == 0o700
    assert stat.S_IMODE((missing / ".locks").stat().st_mode) == 0o700


def test_create_missing_rejects_nested_root_before_mutating_existing_batch(tmp_path: Path) -> None:
    anchor = tmp_path / "data"
    existing = anchor / "existing"
    existing.mkdir(parents=True, mode=0o755)
    existing.chmod(0o755)
    nested = anchor / "absent-parent" / "nested"

    with pytest.raises(ValueError, match="missing direct-child paper roots"):
        normalize_paper_runtime_roots(
            anchor,
            (existing, nested),
            uid=os.getuid(),
            gid=os.getgid(),
            create_missing=True,
        )

    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


def test_preflight_accepts_overlapping_selected_roots_and_zero_targets(tmp_path: Path) -> None:
    anchor = tmp_path / "data"
    root = anchor / "strategy"
    child = root / "generated"
    child.mkdir(parents=True)
    _file(child / "row.json")

    (inspection,) = preflight_reset_targets(anchor, (root, child))

    assert inspection.path == root.absolute()
    assert preflight_reset_targets(anchor, ()) == ()
    assert safety.main(("preflight", "--anchor", str(anchor))) == 0


def test_strict_and_paper_preflight_reject_symlinks_before_mutation(tmp_path: Path) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (root / "external-link").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="strict reset preflight rejects a symlink"):
        preflight_reset_targets(anchor, (root,), reject_symlinks=True)
    with pytest.raises(ValueError, match="paper runtime tree contains a symlink"):
        preflight_paper_runtime_roots(anchor, (root,))


def test_paper_normalization_detects_post_chmod_hardlink_and_restores_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    payload = _file(root / "event.json", mode=0o644)
    alias = tmp_path / "late-hardlink.json"
    original_set = safety._set_descriptor_permissions
    injected = False

    def add_hardlink_after_chmod(
        descriptor: int,
        *,
        uid: int,
        gid: int,
        mode: int,
        path: Path,
    ) -> bool:
        nonlocal injected
        changed = original_set(descriptor, uid=uid, gid=gid, mode=mode, path=path)
        if path == payload and not injected:
            injected = True
            os.link(payload, alias)
        return changed

    monkeypatch.setattr(safety, "_set_descriptor_permissions", add_hardlink_after_chmod)
    owner = payload.stat().st_uid, payload.stat().st_gid

    with pytest.raises(RuntimeError, match="changed"):
        normalize_paper_runtime_roots(anchor, (root,), uid=owner[0], gid=owner[1])

    assert injected is True
    assert stat.S_IMODE(payload.stat().st_mode) == 0o644
    assert stat.S_IMODE(alias.stat().st_mode) == 0o644


def test_paper_normalization_final_rescan_rejects_late_unplanned_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    original_normalize = safety._normalize_planned_directory
    injected = False

    def add_late_entry(
        context: safety._ApplyContext,
        planned: safety._Entry,
        *,
        uid: int,
        gid: int,
        mode: int,
    ) -> None:
        nonlocal injected
        original_normalize(context, planned, uid=uid, gid=gid, mode=mode)
        if planned.path == root and not injected:
            injected = True
            _file(root / "late.json", mode=0o666)

    monkeypatch.setattr(safety, "_normalize_planned_directory", add_late_entry)

    with pytest.raises(RuntimeError, match="tree entries changed during normalization"):
        normalize_paper_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())

    assert injected is True


def test_paper_normalization_final_rescan_rejects_created_lock_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    displaced = tmp_path / "displaced-locks"
    original_normalize = safety._normalize_planned_directory
    injected = False

    def replace_created_locks(
        context: safety._ApplyContext,
        planned: safety._Entry,
        *,
        uid: int,
        gid: int,
        mode: int,
    ) -> None:
        nonlocal injected
        original_normalize(context, planned, uid=uid, gid=gid, mode=mode)
        if planned.path == root and not injected:
            injected = True
            (root / ".locks").rename(displaced)
            (root / ".locks").mkdir(mode=0o777)
            (root / ".locks").chmod(0o777)

    monkeypatch.setattr(safety, "_normalize_planned_directory", replace_created_locks)

    with pytest.raises(RuntimeError, match="entry changed during normalization"):
        normalize_paper_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())

    assert injected is True
    assert stat.S_IMODE((root / ".locks").stat().st_mode) == 0o777
    assert stat.S_IMODE(displaced.stat().st_mode) == 0o700


def test_permission_normalization_rolls_back_after_partial_fchmod_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    payload = _file(root / "setuid.json", mode=0o4644)
    identity = (payload.stat().st_dev, payload.stat().st_ino)
    original_fchmod = safety.os.fchmod
    injected = False

    def fail_payload_fchmod(descriptor: int, mode: int) -> None:
        nonlocal injected
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == identity and not injected:
            injected = True
            original_fchmod(descriptor, 0o600)
            raise OSError("injected fchmod failure")
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(safety.os, "fchmod", fail_payload_fchmod)
    owner = payload.stat().st_uid, payload.stat().st_gid

    with pytest.raises(RuntimeError, match="cannot normalize reset path permissions"):
        normalize_paper_runtime_roots(anchor, (root,), uid=owner[0], gid=owner[1])

    assert injected is True
    assert stat.S_IMODE(payload.stat().st_mode) == 0o4644


def test_demo_normalization_creates_cache_and_sets_only_shared_component_modes(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    long_root = anchor / "long-demo"
    continuous_root = anchor / "continuous-demo"
    long_root.mkdir(parents=True, mode=0o755)
    (continuous_root / ".cache" / "ws_klines").mkdir(parents=True, mode=0o755)
    long_root.chmod(0o755)
    continuous_root.chmod(0o755)
    (continuous_root / ".cache").chmod(0o755)
    (continuous_root / ".cache" / "ws_klines").chmod(0o755)
    snapshot = _file(continuous_root / ".cache" / "ws_klines" / "store.parquet", mode=0o600)
    residual = _file(continuous_root / "residual_momentum.parquet", mode=0o600)
    unrelated = _file(continuous_root / "cycle.parquet", mode=0o604)

    preflight_demo_runtime_roots(
        anchor,
        (long_root, continuous_root),
        continuous_root=continuous_root,
    )
    results = normalize_demo_runtime_roots(
        anchor,
        (long_root, continuous_root),
        uid=os.getuid(),
        gid=os.getgid(),
        continuous_root=continuous_root,
    )

    assert len(results) == 2
    for root in (long_root, continuous_root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o2710
        assert stat.S_IMODE((root / ".locks").stat().st_mode) == 0o700
        assert stat.S_IMODE((root / ".cache").stat().st_mode) == 0o2710
        assert stat.S_IMODE((root / ".cache" / "ws_klines").stat().st_mode) == 0o2750
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o640
    assert stat.S_IMODE(residual.stat().st_mode) == 0o640
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o604


def test_demo_normalization_safely_creates_missing_roots_and_components(tmp_path: Path) -> None:
    anchor = tmp_path / "data"
    anchor.mkdir()
    long_root = anchor / "long-demo"
    continuous_root = anchor / "continuous-demo"

    results = normalize_demo_runtime_roots(
        anchor,
        (long_root, continuous_root),
        uid=os.getuid(),
        gid=os.getgid(),
        continuous_root=continuous_root,
        create_missing=True,
    )

    assert all(result.root_created for result in results)
    assert all(result.lock_directory_created for result in results)
    for root in (long_root, continuous_root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o2710
        assert stat.S_IMODE((root / ".locks").stat().st_mode) == 0o700
        assert stat.S_IMODE((root / ".cache").stat().st_mode) == 0o2710
        assert stat.S_IMODE((root / ".cache" / "ws_klines").stat().st_mode) == 0o2750


def test_regular_mutation_rechecks_descriptor_mount_id_after_leaf_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "paper"
    root.mkdir(parents=True)
    payload = _file(root / "event.json", mode=0o644)
    payload_identity = (payload.stat().st_dev, payload.stat().st_ino)

    monkeypatch.setattr(safety, "_entry_mount_id", lambda *_args: 101)

    def descriptor_mount_id(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == payload_identity else 101

    monkeypatch.setattr(safety, "_mount_id_for_fd", descriptor_mount_id)

    with pytest.raises(RuntimeError, match="file changed before normalization"):
        normalize_paper_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())

    assert stat.S_IMODE(payload.stat().st_mode) == 0o644


def test_demo_preflight_rejects_cache_symlink_without_touching_external_tree(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "demo"
    root.mkdir(parents=True, mode=0o755)
    external = tmp_path / "external"
    external.mkdir()
    victim = _file(external / "must-survive.json", "external evidence\n", mode=0o644)
    (root / ".cache").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="demo cache must be a real directory"):
        preflight_demo_runtime_roots(anchor, (root,))

    assert victim.read_text(encoding="utf-8") == "external evidence\n"
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_demo_normalization_refuses_cache_directory_swap_without_external_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "data"
    root = anchor / "demo"
    cache = root / ".cache"
    (cache / "ws_klines").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    victim = _file(external / "must-survive.json", "external evidence\n", mode=0o644)
    displaced = tmp_path / "displaced-cache"
    original_directory_fd = safety._ApplyContext.directory_fd
    swapped = False

    def swap_before_open(context: safety._ApplyContext, parts: tuple[str, ...]) -> int:
        nonlocal swapped
        if parts and parts[-1] == ".cache" and not swapped:
            swapped = True
            cache.rename(displaced)
            cache.symlink_to(external, target_is_directory=True)
        return original_directory_fd(context, parts)

    monkeypatch.setattr(safety._ApplyContext, "directory_fd", swap_before_open)

    with pytest.raises(RuntimeError, match="reset directory changed before mutation"):
        normalize_demo_runtime_roots(anchor, (root,), uid=os.getuid(), gid=os.getgid())

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "external evidence\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
