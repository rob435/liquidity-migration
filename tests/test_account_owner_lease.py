from __future__ import annotations

import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

import liquidity_migration.account_owner_lease as lease_module
from liquidity_migration.account_owner_lease import (
    AccountOwnerLease,
    DemoAccountIdentity,
    DemoAccountMutationLease,
    canonical_demo_account_lease_path,
)


def _try_lease(path: str, output: multiprocessing.Queue) -> None:
    try:
        with AccountOwnerLease(path):
            output.put("acquired")
    except RuntimeError as exc:
        output.put(str(exc))


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
    with pytest.raises(RuntimeError, match="environment='demo'"):
        DemoAccountIdentity.from_api_key_info(
            api_key="demo-key",
            api_key_info={"apiKey": "demo-key", "userID": 12345},
            environment="mainnet",
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
