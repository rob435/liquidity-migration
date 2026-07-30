from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import liquidity_migration.account_owner_lease as lease_module
from liquidity_migration.account_owner_lease import (
    AccountOwnerLease,
    AccountOwnerLeaseAlreadyHeldError,
    DemoAccountIdentity,
    DemoAccountMutationLease,
    acquire_inherited_account_owner_lease,
    canonical_demo_account_lease_path,
    prepare_account_owner_lease,
    revalidate_inherited_account_owner_lease,
)


def _try_lease(path: str, output: multiprocessing.Queue) -> None:
    try:
        with AccountOwnerLease(path):
            output.put("acquired")
    except RuntimeError as exc:
        output.put(str(exc))


def _prepared_cli_fields(prepared: lease_module.PreparedAccountOwnerLease) -> list[str]:
    return [
        str(prepared.device),
        str(prepared.inode),
        str(prepared.uid),
        str(prepared.gid),
        "-" if prepared.mount_id is None else str(prepared.mount_id),
        str(prepared.parent_device),
        str(prepared.parent_inode),
        str(prepared.parent_uid),
        str(prepared.parent_gid),
        "-" if prepared.parent_mount_id is None else str(prepared.parent_mount_id),
    ]


def _run_inherited_cli(
    descriptor: int,
    prepared: lease_module.PreparedAccountOwnerLease,
    *,
    environment: str = "paper",
    role: str = "ledger_reset",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "liquidity_migration.account_owner_lease",
            "acquire-inherited",
            str(descriptor),
            str(prepared.path),
            *_prepared_cli_fields(prepared),
            environment,
            role,
        ],
        check=False,
        capture_output=True,
        text=True,
        pass_fds=(descriptor,),
        timeout=10.0,
    )


def test_only_one_process_can_hold_account_owner_lease(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    output: multiprocessing.Queue = multiprocessing.Queue()
    with AccountOwnerLease(path):
        process = multiprocessing.Process(target=_try_lease, args=(str(path), output))
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert "already held" in output.get(timeout=1)

    with AccountOwnerLease(path):
        pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork inheritance")
def test_fork_child_close_does_not_unlock_parent_lease(tmp_path: Path) -> None:
    path = tmp_path / "fork-owner.lock"
    script = textwrap.dedent(
        """\
        import json
        import os
        import sys

        from liquidity_migration.account_owner_lease import AccountOwnerLease

        path = sys.argv[1]
        lease = AccountOwnerLease(path)
        lease.acquire()

        child_pid = os.fork()
        if child_pid == 0:
            before = lease.held
            lease.close()
            os._exit(0 if not before and not lease.held else 2)
        _, child_status = os.waitpid(child_pid, 0)

        read_fd, write_fd = os.pipe()
        contender_pid = os.fork()
        if contender_pid == 0:
            os.close(read_fd)
            try:
                with AccountOwnerLease(path):
                    result = "acquired"
            except RuntimeError as exc:
                result = "blocked" if "already held" in str(exc) else f"error:{exc}"
            os.write(write_fd, result.encode("utf-8"))
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        contender = os.read(read_fd, 1024).decode("utf-8")
        os.close(read_fd)
        _, contender_status = os.waitpid(contender_pid, 0)
        parent_held = lease.held
        lease.close()
        print(
            json.dumps(
                {
                    "child_exit": os.waitstatus_to_exitcode(child_status),
                    "contender": contender,
                    "contender_exit": os.waitstatus_to_exitcode(contender_status),
                    "parent_held": parent_held,
                }
            )
        )
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert json.loads(completed.stdout) == {
        "child_exit": 0,
        "contender": "blocked",
        "contender_exit": 0,
        "parent_held": True,
    }


def test_account_owner_lease_refuses_symbolic_and_hard_link_aliases(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.lock"
    target.write_text("untouched\n", encoding="utf-8")
    symbolic = tmp_path / "symbolic.lock"
    symbolic.symlink_to(target)
    with pytest.raises(RuntimeError, match="opened safely"):
        AccountOwnerLease(symbolic).acquire()
    assert target.read_text(encoding="utf-8") == "untouched\n"

    hard_link = tmp_path / "hard-link.lock"
    hard_link.hardlink_to(target)
    with pytest.raises(RuntimeError, match="single-link regular file"):
        AccountOwnerLease(hard_link).acquire()
    assert target.read_text(encoding="utf-8") == "untouched\n"


def test_prepare_preserves_existing_metadata_contents_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    path.write_text('{"existing": true}\n', encoding="utf-8")
    path.chmod(0o600)
    before = path.stat()

    prepared = prepare_account_owner_lease(path)

    after = path.stat()
    assert (prepared.device, prepared.inode) == (before.st_dev, before.st_ino)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert path.read_text(encoding="utf-8") == '{"existing": true}\n'
    assert stat.S_IMODE(after.st_mode) == 0o600


def test_prepare_descriptor_creates_only_immediate_private_parent(tmp_path: Path) -> None:
    parent = tmp_path / "lease-namespace"
    path = parent / "owner.lock"

    prepared = prepare_account_owner_lease(path)

    assert prepared.path == path.absolute()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (path.stat().st_uid, path.stat().st_gid) == (parent.stat().st_uid, parent.stat().st_gid)
    with pytest.raises(RuntimeError, match="cannot be opened safely"):
        prepare_account_owner_lease(tmp_path / "missing" / "nested" / "owner.lock")


def test_prepare_normalizes_readonly_public_parent_but_rejects_writable_parent(
    tmp_path: Path,
) -> None:
    safe_parent = tmp_path / "safe"
    safe_parent.mkdir(mode=0o755)
    prepare_account_owner_lease(safe_parent / "owner.lock")
    assert stat.S_IMODE(safe_parent.stat().st_mode) == 0o700

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    with pytest.raises(RuntimeError, match="not owner-controlled"):
        prepare_account_owner_lease(unsafe_parent / "owner.lock")
    assert not (unsafe_parent / "owner.lock").exists()


def test_prepare_refuses_symlinked_parent_namespace_without_touching_target(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    parent = real / "private"
    parent.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(RuntimeError, match="cannot be opened safely"):
        prepare_account_owner_lease(alias / "private" / "owner.lock")

    assert not (parent / "owner.lock").exists()


def test_prepare_refuses_parent_and_regular_file_mount_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)

    def parent_bind_mount(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == parent_identity else 101

    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", parent_bind_mount)
    with pytest.raises(RuntimeError, match="not owner-controlled"):
        prepare_account_owner_lease(parent / "owner.lock")
    assert not (parent / "owner.lock").exists()

    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", lambda _descriptor: 101)
    path = parent / "owner.lock"
    path.write_text("external evidence\n", encoding="utf-8")
    path.chmod(0o600)
    leaf_identity = (path.stat().st_dev, path.stat().st_ino)

    def file_bind_mount(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == leaf_identity else 101

    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", file_bind_mount)
    with pytest.raises(RuntimeError, match="parent mount"):
        prepare_account_owner_lease(path)
    assert path.read_text(encoding="utf-8") == "external evidence\n"


def test_explicit_private_parent_mount_boundary_is_pinned_for_lease_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("the private-parent mount-boundary opt-in is non-root only")
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
    path = parent / "owner.lock"
    private_mount_id = 202

    def owner_bind_mount(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if identity == parent_identity:
            return private_mount_id
        if path.exists() and identity == (path.stat().st_dev, path.stat().st_ino):
            return private_mount_id
        return 101

    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", owner_bind_mount)
    lease = AccountOwnerLease(
        path,
        allow_private_parent_mount_boundary=True,
    )

    lease.acquire()
    assert lease.held is True

    private_mount_id = 303
    assert lease.held is False
    lease.close()


def test_root_cannot_opt_into_private_parent_mount_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
    path = parent / "owner.lock"

    def owner_bind_mount(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return 202 if (metadata.st_dev, metadata.st_ino) == parent_identity else 101

    monkeypatch.setattr(lease_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", owner_bind_mount)
    with pytest.raises(RuntimeError, match="not owner-controlled"):
        AccountOwnerLease(
            path,
            allow_private_parent_mount_boundary=True,
        ).acquire()
    assert not path.exists()


def test_canonical_prepare_allows_trusted_dedicated_run_lock_mount_but_rejects_untrusted_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    lock = run / "lock"
    parent = lock / "liquidity-migration"
    lock.mkdir(parents=True)
    run.chmod(0o755)
    lock.chmod(0o1777)
    expected_owner = (run.stat().st_uid, run.stat().st_gid)
    monkeypatch.setattr(lease_module, "_CANONICAL_DEMO_LEASE_DIRECTORY", parent)
    monkeypatch.setattr(
        lease_module,
        "_canonical_demo_expected_owner",
        lambda: expected_owner,
    )
    lock_identity = (lock.stat().st_dev, lock.stat().st_ino)
    run_identity = (run.stat().st_dev, run.stat().st_ino)

    def dedicated_lock_mount(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if identity == run_identity:
            return 101
        if identity == lock_identity:
            return 202
        if parent.exists() and identity == (parent.stat().st_dev, parent.stat().st_ino):
            return 202
        leaf = parent / "owner.lock"
        if leaf.exists() and identity == (leaf.stat().st_dev, leaf.stat().st_ino):
            return 202
        return 101

    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", dedicated_lock_mount)
    prepare_account_owner_lease(parent / "owner.lock")
    assert parent.is_dir()
    assert (parent / "owner.lock").is_file()

    monkeypatch.setattr(lease_module, "_lease_mount_id_for_fd", lambda _descriptor: 101)
    (parent / "owner.lock").unlink()
    lock.chmod(0o777)
    with pytest.raises(RuntimeError, match="/run/lock anchor is not trusted"):
        prepare_account_owner_lease(parent / "owner.lock")
    assert not (parent / "owner.lock").exists()


def test_acquire_refuses_parent_replacement_before_returning_prepared_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "owner.lock"
    path.write_text("original metadata\n", encoding="utf-8")
    path.chmod(0o600)
    displaced = tmp_path / "displaced-private"
    original_validate = lease_module._validate_prepared_account_owner_lease
    replaced = False

    def replace_parent(
        checked_path: Path,
        descriptor: int,
        prepared: lease_module.PreparedAccountOwnerLease,
        *,
        allow_private_parent_mount_boundary: bool = False,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            parent.rename(displaced)
            parent.mkdir(mode=0o700)
            replacement = parent / path.name
            replacement.write_text("replacement metadata\n", encoding="utf-8")
            replacement.chmod(0o600)
        original_validate(
            checked_path,
            descriptor,
            prepared,
            allow_private_parent_mount_boundary=allow_private_parent_mount_boundary,
        )

    monkeypatch.setattr(lease_module, "_validate_prepared_account_owner_lease", replace_parent)

    with pytest.raises(RuntimeError, match="single-link regular file"):
        AccountOwnerLease(path).acquire()

    assert replaced is True
    assert path.read_text(encoding="utf-8") == "replacement metadata\n"
    assert (displaced / path.name).read_text(encoding="utf-8") == "original metadata\n"


def test_acquire_revalidates_leaf_after_flock_before_truncating_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.lock"
    path.write_text("original metadata\n", encoding="utf-8")
    path.chmod(0o600)
    displaced = tmp_path / "displaced-owner.lock"
    original_validate = lease_module._validate_prepared_account_owner_lease
    validations = 0

    def replace_on_post_flock_validation(
        checked_path: Path,
        descriptor: int,
        prepared: lease_module.PreparedAccountOwnerLease,
        *,
        allow_private_parent_mount_boundary: bool = False,
    ) -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            path.rename(displaced)
            path.write_text("replacement metadata\n", encoding="utf-8")
            path.chmod(0o600)
        original_validate(
            checked_path,
            descriptor,
            prepared,
            allow_private_parent_mount_boundary=allow_private_parent_mount_boundary,
        )

    monkeypatch.setattr(
        lease_module,
        "_validate_prepared_account_owner_lease",
        replace_on_post_flock_validation,
    )

    with pytest.raises(RuntimeError, match="single-link regular file"):
        AccountOwnerLease(path).acquire()

    assert validations == 2
    assert path.read_text(encoding="utf-8") == "replacement metadata\n"
    assert displaced.read_text(encoding="utf-8") == "original metadata\n"


def test_prepare_cli_prints_identity_without_truncating_contents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "owner.lock"
    path.write_text("preserve me\n", encoding="utf-8")
    path.chmod(0o600)

    assert lease_module.main(["prepare", str(path)]) == 0

    prepared = prepare_account_owner_lease(path)
    fields = capsys.readouterr().out.strip().split("\t")
    assert fields == _prepared_cli_fields(prepared)
    assert path.read_text(encoding="utf-8") == "preserve me\n"


def test_revalidate_inherited_checks_full_receipt_and_held_lock_without_rewriting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner.lock"
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    try:
        acquire_inherited_account_owner_lease(
            descriptor,
            path,
            prepared.device,
            prepared.inode,
            "paper",
            "ledger_reset",
            prepared_receipt=prepared,
        )
        payload = path.read_bytes()

        revalidate_inherited_account_owner_lease(descriptor, prepared)

        assert path.read_bytes() == payload
        with pytest.raises(RuntimeError, match="acquisition receipt"):
            revalidate_inherited_account_owner_lease(
                descriptor,
                replace(prepared, parent_mount_id=987654321),
            )
        assert path.read_bytes() == payload

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        with pytest.raises(RuntimeError, match="no longer held"):
            revalidate_inherited_account_owner_lease(descriptor, prepared)
        assert path.read_bytes() == payload

        unrelated = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(unrelated, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="another open description"):
                revalidate_inherited_account_owner_lease(descriptor, prepared)
            assert path.read_bytes() == payload
        finally:
            os.close(unrelated)
    finally:
        os.close(descriptor)


def test_revalidate_inherited_cli_uses_complete_receipt_without_rewriting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "owner.lock"
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    try:
        acquire_inherited_account_owner_lease(
            descriptor,
            path,
            prepared.device,
            prepared.inode,
            "paper",
            "ledger_reset",
            prepared_receipt=prepared,
        )
        payload = path.read_bytes()
        assert (
            lease_module.main(
                [
                    "revalidate-inherited",
                    str(descriptor),
                    str(path),
                    *_prepared_cli_fields(prepared),
                ]
            )
            == 0
        )
        assert capsys.readouterr().out == ""
        assert path.read_bytes() == payload
    finally:
        os.close(descriptor)


def test_acquire_inherited_cli_keeps_lock_on_parent_descriptor_after_return(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner.lock"
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    contender = -1
    try:
        completed = _run_inherited_cli(descriptor, prepared, environment="paper")
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == ""
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata == {
            "environment": "paper",
            "pid": os.getpid(),
            "role": "ledger_reset",
            "started_at_ns": metadata["started_at_ns"],
        }
        assert isinstance(metadata["started_at_ns"], int)
        assert metadata["started_at_ns"] > 0

        contender = os.open(path, os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if contender >= 0:
            os.close(contender)
        os.close(descriptor)

    released = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(released)


def test_acquire_inherited_demo_requires_and_supports_canonical_host_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alternate = tmp_path / "alternate"
    alternate.mkdir(mode=0o700)
    alternate_path = alternate / "owner.lock"
    alternate_prepared = prepare_account_owner_lease(alternate_path)
    alternate_fd = os.open(alternate_path, os.O_RDWR)
    try:
        with pytest.raises(ValueError, match="canonical Bybit /run/lock identity path"):
            acquire_inherited_account_owner_lease(
                alternate_fd,
                alternate_path,
                alternate_prepared.device,
                alternate_prepared.inode,
                "demo",
                "ledger_reset",
            )
    finally:
        os.close(alternate_fd)

    run = tmp_path / "run"
    lock = run / "lock"
    canonical = lock / "liquidity-migration"
    canonical.mkdir(parents=True, mode=0o700)
    run.chmod(0o755)
    lock.chmod(0o1777)
    canonical.chmod(0o700)
    expected_owner = (run.stat().st_uid, run.stat().st_gid)
    monkeypatch.setattr(lease_module, "_CANONICAL_DEMO_LEASE_DIRECTORY", canonical)
    monkeypatch.setattr(
        lease_module,
        "_canonical_demo_expected_owner",
        lambda: expected_owner,
    )
    path = canonical / "bybit-demo-user-12345.lock"
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    try:
        acquire_inherited_account_owner_lease(
            descriptor,
            path,
            prepared.device,
            prepared.inode,
            "demo",
            "ledger_reset",
        )
    finally:
        os.close(descriptor)

    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["environment"] == "demo"
    assert metadata["venue"] == "bybit"


def test_acquire_inherited_cli_returns_distinct_already_held_status(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner.lock"
    prepared = prepare_account_owner_lease(path)
    holder = os.open(path, os.O_RDWR)
    contender = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = _run_inherited_cli(contender, prepared)
        assert completed.returncode == 73
        assert "already held" in completed.stderr
    finally:
        os.close(contender)
        os.close(holder)


@pytest.mark.parametrize("attack", ["replacement", "symlink", "hardlink"])
def test_acquire_inherited_refuses_path_attacks_without_truncating_victim(
    tmp_path: Path,
    attack: str,
) -> None:
    path = tmp_path / "owner.lock"
    path.write_text("original metadata\n", encoding="utf-8")
    path.chmod(0o600)
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    displaced = tmp_path / "displaced-owner.lock"
    victim = tmp_path / "victim.lock"
    try:
        if attack == "hardlink":
            victim.hardlink_to(path)
        else:
            path.rename(displaced)
            victim.write_text("victim metadata\n", encoding="utf-8")
            victim.chmod(0o600)
            if attack == "replacement":
                path.write_text("replacement metadata\n", encoding="utf-8")
                path.chmod(0o600)
            else:
                path.symlink_to(victim)

        completed = _run_inherited_cli(descriptor, prepared)

        assert completed.returncode == 1
        if attack == "hardlink":
            assert path.read_text(encoding="utf-8") == "original metadata\n"
            assert victim.read_text(encoding="utf-8") == "original metadata\n"
        else:
            assert displaced.read_text(encoding="utf-8") == "original metadata\n"
            assert victim.read_text(encoding="utf-8") == "victim metadata\n"
            if attack == "replacement":
                assert path.read_text(encoding="utf-8") == "replacement metadata\n"
    finally:
        os.close(descriptor)


def test_acquire_inherited_revalidates_after_flock_before_truncating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.lock"
    path.write_text("original metadata\n", encoding="utf-8")
    path.chmod(0o600)
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    displaced = tmp_path / "displaced-owner.lock"
    original_validate = lease_module._validate_prepared_account_owner_lease
    replaced = False

    def replace_before_first_post_flock_validation(
        checked_path: Path,
        checked_descriptor: int,
        receipt: lease_module.PreparedAccountOwnerLease,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            path.rename(displaced)
            path.write_text("replacement metadata\n", encoding="utf-8")
            path.chmod(0o600)
        original_validate(checked_path, checked_descriptor, receipt)

    monkeypatch.setattr(
        lease_module,
        "_validate_prepared_account_owner_lease",
        replace_before_first_post_flock_validation,
    )
    try:
        with pytest.raises(RuntimeError, match="single-link regular file"):
            acquire_inherited_account_owner_lease(
                descriptor,
                path,
                prepared.device,
                prepared.inode,
                "paper",
                "ledger_reset",
            )
    finally:
        os.close(descriptor)

    assert replaced is True
    assert path.read_text(encoding="utf-8") == "replacement metadata\n"
    assert displaced.read_text(encoding="utf-8") == "original metadata\n"


def test_acquire_inherited_requires_exact_mode_before_truncating(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    path.write_text("original metadata\n", encoding="utf-8")
    path.chmod(0o600)
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    path.chmod(0o640)
    try:
        completed = _run_inherited_cli(descriptor, prepared)
    finally:
        os.close(descriptor)

    assert completed.returncode == 1
    assert path.read_text(encoding="utf-8") == "original metadata\n"


def test_acquire_inherited_final_revalidation_detects_post_fsync_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "owner.lock"
    path.write_text("original metadata\n", encoding="utf-8")
    path.chmod(0o600)
    prepared = prepare_account_owner_lease(path)
    descriptor = os.open(path, os.O_RDWR)
    displaced = tmp_path / "displaced-owner.lock"
    original_validate = lease_module._validate_prepared_account_owner_lease
    validations = 0

    def replace_on_final_validation(
        checked_path: Path,
        checked_descriptor: int,
        receipt: lease_module.PreparedAccountOwnerLease,
    ) -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            path.rename(displaced)
            path.write_text("replacement metadata\n", encoding="utf-8")
            path.chmod(0o600)
        original_validate(checked_path, checked_descriptor, receipt)

    monkeypatch.setattr(
        lease_module,
        "_validate_prepared_account_owner_lease",
        replace_on_final_validation,
    )
    try:
        with pytest.raises(RuntimeError, match="single-link regular file"):
            acquire_inherited_account_owner_lease(
                descriptor,
                path,
                prepared.device,
                prepared.inode,
                "paper",
                "ledger_reset",
            )
    finally:
        os.close(descriptor)

    assert validations == 2
    assert path.read_text(encoding="utf-8") == "replacement metadata\n"
    assert json.loads(displaced.read_text(encoding="utf-8"))["role"] == "ledger_reset"


def test_acquire_inherited_api_raises_typed_contention_error(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    prepared = prepare_account_owner_lease(path)
    holder = os.open(path, os.O_RDWR)
    contender = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AccountOwnerLeaseAlreadyHeldError):
            acquire_inherited_account_owner_lease(
                contender,
                path,
                prepared.device,
                prepared.inode,
                "paper",
                "ledger_reset",
            )
    finally:
        os.close(contender)
        os.close(holder)


def _identity(*, api_key: str, user_id: int = 12345) -> DemoAccountIdentity:
    return DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info={"apiKey": api_key, "userID": user_id},
    )


def test_demo_identity_requires_authenticated_matching_key_and_user_id() -> None:
    with pytest.raises(TypeError, match="cannot be constructed directly"):
        DemoAccountIdentity(
            user_id="12345",
            api_key_sha256="invented",
            environment="demo",
        )

    identity = _identity(api_key="demo-key", user_id=12345)
    assert identity.user_id == "12345"
    assert identity.environment == "demo"
    assert identity.api_key_sha256 != "demo-key"

    with pytest.raises(RuntimeError, match="does not match"):
        DemoAccountIdentity.from_api_key_info(
            api_key="configured-key",
            api_key_info={"apiKey": "different-key", "userID": 12345},
        )
    with pytest.raises(RuntimeError, match="valid userID"):
        DemoAccountIdentity.from_api_key_info(
            api_key="demo-key",
            api_key_info={"apiKey": "demo-key"},
        )
    # Both venue realms are legitimate identities, and the realm is baked into
    # the canonical lease path so the two owners can never share a lock.
    mainnet = DemoAccountIdentity.from_api_key_info(
        api_key="demo-key",
        api_key_info={"apiKey": "demo-key", "userID": 12345},
        environment="mainnet",
    )
    assert mainnet.environment == "mainnet"
    assert canonical_demo_account_lease_path(mainnet) != canonical_demo_account_lease_path(identity)
    # ``paper`` is not a venue and must not produce a credential identity.
    for bogus in ("paper", "live", ""):
        with pytest.raises(ValueError, match="explicitly set to 'demo' or 'mainnet'"):
            DemoAccountIdentity.from_api_key_info(
                api_key="demo-key",
                api_key_info={"apiKey": "demo-key", "userID": 12345},
                environment=bogus,
            )
    with pytest.raises(TypeError, match="authenticated DemoAccountIdentity"):
        DemoAccountMutationLease(
            SimpleNamespace(
                user_id="alternate",
                api_key_sha256="invented",
                environment="demo",
            )
        )


def test_demo_lease_uses_host_global_account_identity_not_data_root() -> None:
    identity = _identity(api_key="demo-key")
    assert canonical_demo_account_lease_path(identity) == Path(
        "/run/lock/liquidity-migration/bybit-demo-user-12345.lock"
    )


def test_different_keys_for_same_demo_uid_contend_on_one_canonical_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lease_module,
        "canonical_demo_account_lease_path",
        lambda identity: tmp_path / f"bybit-{identity.environment}-{identity.user_id}.lock",
    )
    first = DemoAccountMutationLease(_identity(api_key="first-key"))
    second = DemoAccountMutationLease(_identity(api_key="second-key"))
    assert first.path == second.path
    with first:
        with pytest.raises(RuntimeError, match="already held"):
            second.acquire()
    with second:
        pass


def test_demo_mutation_capability_rechecks_held_path_credential_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_path = tmp_path / "canonical.lock"
    monkeypatch.setattr(
        lease_module,
        "canonical_demo_account_lease_path",
        lambda _identity: canonical_path,
    )
    lease = DemoAccountMutationLease(_identity(api_key="demo-key"))
    with pytest.raises(RuntimeError, match="not currently held"):
        lease.require_held_for(
            api_key="demo-key",
            environment="demo",
            action="place_order",
        )
    with lease:
        lease.require_held_for(
            api_key="demo-key",
            environment="demo",
            action="place_order",
        )
        with pytest.raises(RuntimeError, match="different API credential"):
            lease.require_held_for(
                api_key="other-key",
                environment="demo",
                action="place_order",
            )
        with pytest.raises(RuntimeError, match="different environment"):
            lease.require_held_for(
                api_key="demo-key",
                environment="paper",
                action="place_order",
            )
        lease.path = tmp_path / "alternate.lock"
        with pytest.raises(RuntimeError, match="not currently held"):
            lease.require_held_for(
                api_key="demo-key",
                environment="demo",
                action="place_order",
            )


def test_demo_mutation_capability_refuses_replaced_lock_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_path = tmp_path / "canonical.lock"
    monkeypatch.setattr(
        lease_module,
        "canonical_demo_account_lease_path",
        lambda _identity: canonical_path,
    )
    lease = DemoAccountMutationLease(_identity(api_key="demo-key"))
    with lease:
        canonical_path.unlink()
        canonical_path.write_text("replacement\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not currently held"):
            lease.require_held_for(
                api_key="demo-key",
                environment="demo",
                action="place_order",
            )


def test_demo_lease_permission_failure_has_no_alternate_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def permission_denied(_lease: AccountOwnerLease) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(AccountOwnerLease, "acquire", permission_denied)
    lease = DemoAccountMutationLease(_identity(api_key="demo-key"))
    with pytest.raises(RuntimeError, match="no alternate path is allowed"):
        lease.acquire()
