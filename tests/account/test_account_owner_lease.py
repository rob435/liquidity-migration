"""The slim account-owner lease: one flock per venue account.

What must stay true: only one process mutates an account at a time, the lock
dies with its holder, a deleted or replaced lease file reads as *not held*
(flock binds the inode, not the name), and the demo mutation lease is bound
to the credential that authenticated it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import liquidity_migration.account.account_owner_lease as owner_lease_module
from liquidity_migration.account.account_owner_lease import (
    AccountOwnerLease,
    DemoAccountIdentity,
    DemoAccountMutationLease,
    acquire_inherited_account_owner_lease,
    canonical_demo_account_lease_path,
    revalidate_inherited_account_owner_lease,
)


def _identity(api_key: str = "demo-key", user_id: int = 424242) -> DemoAccountIdentity:
    return DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info={"apiKey": api_key, "userID": user_id},
    )


def test_lease_is_exclusive_and_released_on_close(tmp_path) -> None:
    path = tmp_path / "owner.lock"
    first = AccountOwnerLease(path)
    first.acquire()
    assert first.held

    second = AccountOwnerLease(path)
    with pytest.raises(RuntimeError, match="already held"):
        second.acquire()

    first.close()
    assert not first.held
    second.acquire()
    assert second.held
    second.close()


def test_held_goes_false_when_the_lease_file_is_deleted(tmp_path) -> None:
    path = tmp_path / "owner.lock"
    lease = AccountOwnerLease(path)
    lease.acquire()
    assert lease.held

    path.unlink()
    assert not lease.held
    lease.close()


def test_held_goes_false_when_the_lease_file_is_replaced(tmp_path) -> None:
    path = tmp_path / "owner.lock"
    lease = AccountOwnerLease(path)
    lease.acquire()
    assert lease.held

    path.unlink()
    path.write_text("{}\n", encoding="utf-8")
    assert not lease.held
    lease.close()


def test_lease_refuses_a_symlinked_path(tmp_path) -> None:
    real = tmp_path / "real.lock"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "owner.lock"
    link.symlink_to(real)
    with pytest.raises(OSError):
        AccountOwnerLease(link).acquire()


def test_lease_writes_holder_metadata(tmp_path) -> None:
    path = tmp_path / "owner.lock"
    with AccountOwnerLease(path):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["pid"] == os.getpid()


def test_identity_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError, match="from_api_key_info"):
        DemoAccountIdentity(  # type: ignore[call-arg]
            user_id="1",
            api_key_sha256="x",
            environment="demo",
        )


def test_identity_rejects_a_mismatched_reported_key() -> None:
    with pytest.raises(RuntimeError, match="does not match"):
        DemoAccountIdentity.from_api_key_info(
            api_key="demo-key",
            api_key_info={"apiKey": "another-key", "userID": 7},
        )


@pytest.mark.parametrize("user_id", [None, "", "abc", 0, -3, True])
def test_identity_rejects_invalid_user_ids(user_id) -> None:
    with pytest.raises(RuntimeError, match="userID"):
        DemoAccountIdentity.from_api_key_info(
            api_key="demo-key",
            api_key_info={"apiKey": "demo-key", "userID": user_id},
        )


def test_canonical_path_is_keyed_by_realm_and_user_id() -> None:
    identity = _identity(user_id=31337)
    assert canonical_demo_account_lease_path(identity) == (
        owner_lease_module._CANONICAL_DEMO_LEASE_DIRECTORY / "bybit-demo-user-31337.lock"
    )


def test_mutation_lease_requires_an_authenticated_identity() -> None:
    with pytest.raises(TypeError, match="authenticated"):
        DemoAccountMutationLease("not-an-identity")  # type: ignore[arg-type]


def test_require_held_for_proves_credential_and_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        owner_lease_module,
        "canonical_demo_account_lease_path",
        lambda identity: tmp_path / f"bybit-{identity.environment}-user-{identity.user_id}.lock",
    )
    lease = DemoAccountMutationLease(_identity(api_key="demo-key"))

    with pytest.raises(RuntimeError, match="not currently held"):
        lease.require_held_for(api_key="demo-key", environment="demo", action="submit")

    lease.acquire()
    lease.require_held_for(api_key="demo-key", environment="demo", action="submit")
    with pytest.raises(RuntimeError, match="different API credential"):
        lease.require_held_for(api_key="other-key", environment="demo", action="submit")
    with pytest.raises(RuntimeError, match="different environment"):
        lease.require_held_for(api_key="demo-key", environment="mainnet", action="submit")

    # The deleted-file check flows through: a replaced lock file must fail the
    # per-mutation proof, not just the property.
    lease.path.unlink()
    lease.path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not currently held"):
        lease.require_held_for(api_key="demo-key", environment="demo", action="submit")
    lease.close()


def test_mutation_lease_metadata_records_account_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        owner_lease_module,
        "canonical_demo_account_lease_path",
        lambda identity: tmp_path / f"bybit-{identity.environment}-user-{identity.user_id}.lock",
    )
    lease = DemoAccountMutationLease(_identity(api_key="demo-key", user_id=99))
    with lease:
        metadata = json.loads(lease.path.read_text(encoding="utf-8"))
    assert metadata["user_id"] == "99"
    assert metadata["environment"] == "demo"
    assert metadata["venue"] == "bybit"
    assert metadata["api_key_sha256"] == lease.identity.api_key_sha256


def _canonical_tmp_lease(tmp_path, monkeypatch):
    """Route the canonical demo directory into tmp so CLI paths validate."""

    directory = tmp_path / "run-lock"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(owner_lease_module, "_CANONICAL_DEMO_LEASE_DIRECTORY", directory)
    return directory / "bybit-demo-user-424242.lock"


def test_acquire_inherited_locks_annotates_and_detects_contention(tmp_path, monkeypatch) -> None:
    path = _canonical_tmp_lease(tmp_path, monkeypatch)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        acquire_inherited_account_owner_lease(descriptor, path, "demo", "ledger_reset")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["role"] == "ledger_reset"
        assert metadata["environment"] == "demo"
        assert metadata["venue"] == "bybit"

        revalidate_inherited_account_owner_lease(descriptor, path)

        contender = AccountOwnerLease(path)
        with pytest.raises(RuntimeError, match="already held"):
            contender.acquire()
    finally:
        os.close(descriptor)


def test_acquire_inherited_refuses_a_noncanonical_demo_path(tmp_path) -> None:
    path = tmp_path / "elsewhere.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(ValueError, match="canonical"):
            acquire_inherited_account_owner_lease(descriptor, path, "demo", "ledger_reset")
    finally:
        os.close(descriptor)


def test_revalidate_fails_after_the_lease_file_is_replaced(tmp_path, monkeypatch) -> None:
    path = _canonical_tmp_lease(tmp_path, monkeypatch)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        acquire_inherited_account_owner_lease(descriptor, path, "demo", "ledger_reset")
        os.unlink(path)
        path.write_text("{}\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="no longer matches"):
            revalidate_inherited_account_owner_lease(descriptor, path)
    finally:
        os.close(descriptor)


def test_revalidate_fails_when_the_lock_is_not_held(tmp_path, monkeypatch) -> None:
    path = _canonical_tmp_lease(tmp_path, monkeypatch)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(RuntimeError, match="no longer held"):
            revalidate_inherited_account_owner_lease(descriptor, path)
    finally:
        os.close(descriptor)


def test_cli_acquire_inherited_reports_contention_as_73(tmp_path) -> None:
    # The reset script keys its "already held" message on exit code 73; run the
    # real CLI against a held lock to pin that contract. The subprocess uses a
    # mainnet-named path because the canonical demo directory cannot be
    # monkeypatched across the process boundary.
    path = tmp_path / "owner.lock"
    holder = AccountOwnerLease(path)
    holder.acquire()
    try:
        script = (
            "import os, sys\n"
            "from liquidity_migration.account.account_owner_lease import main\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "sys.exit(main(['acquire-inherited', str(fd), sys.argv[1], 'mainnet', 'test']))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 73, result.stderr
        assert "already held" in result.stderr
    finally:
        holder.close()
