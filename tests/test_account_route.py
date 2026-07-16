from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import liquidity_migration.account_route as account_route_module
from liquidity_migration.account_route import (
    ACCOUNT_ROUTE_FILENAME,
    AccountRouteConfigurationError,
    AccountRouteCutoverRequiredError,
    AccountRouteIntegrityError,
    AccountRouteMismatchError,
    AccountRouteMissingError,
    account_route_manifest_path,
    derive_account_route,
    ensure_account_route,
    read_account_route_manifest,
    require_account_route,
)
from liquidity_migration.deterministic_serialization import canonical_json


def _route_paths(account_root: Path, inbox_root: Path) -> tuple[Path, Path]:
    return (
        account_route_manifest_path(account_root),
        account_route_manifest_path(inbox_root),
    )


def _rewrite_canonical(path: Path, payload: dict[str, object]) -> None:
    path.write_bytes(canonical_json(payload) + b"\n")


@pytest.mark.parametrize("runner_name", ["demo", "paper"])
def test_owner_acquires_lease_before_route_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_name: str,
) -> None:
    monkeypatch.setenv("INVOCATION_ID", "ab" * 16)
    monkeypatch.setenv("REAL_MONEY", "false")
    for key in (
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    calls: list[str] = []

    if runner_name == "demo":
        import liquidity_migration.account_service_runner as runner

        argv = [
            "--account-root",
            str(account_root),
            "--inbox-root",
            str(inbox_root),
            "--capture-root",
            str(tmp_path / "capture"),
            "--symbols-file",
            str(tmp_path / "symbols.json"),
            "--demo-rules-file",
            str(tmp_path / "rules.json"),
            "--risk-policy-file",
            str(tmp_path / "risk.json"),
            "--disaster-stop-fraction",
            "0.25",
        ]

        class FakeDemoLease:
            def __init__(self, _identity: object) -> None:
                calls.append("lease-created")

            def acquire(self) -> None:
                assert not account_root.exists()
                assert not inbox_root.exists()
                calls.append("lease-acquired")

            def close(self) -> None:
                calls.append("lease-closed")

        monkeypatch.setattr(runner, "validate_demo_order_permission", lambda **_kwargs: None)
        monkeypatch.setattr(runner, "resolve_demo_credentials", lambda: ("demo-key", "demo-secret"))
        monkeypatch.setattr(runner, "BybitPrivateClient", lambda **_kwargs: object())
        monkeypatch.setattr(
            runner,
            "require_order_submit_permission",
            lambda _client: {"apiKey": "demo-key", "userID": "123"},
        )
        monkeypatch.setattr(runner, "DemoAccountMutationLease", FakeDemoLease)
        later_resources = (
            "load_demo_rules",
            "AccountExecutionKernel",
            "SequenceAwareMarketRecorder",
            "BybitRawPublicMarketStream",
            "AccountExecutionService",
            "AccountIntentInbox",
        )
    else:
        import liquidity_migration.account_paper_runner as runner

        argv = [
            "--account-root",
            str(account_root),
            "--inbox-root",
            str(inbox_root),
            "--capture-root",
            str(tmp_path / "capture"),
            "--symbols-file",
            str(tmp_path / "symbols.json"),
            "--demo-rules-file",
            str(tmp_path / "rules.json"),
            "--risk-policy-file",
            str(tmp_path / "risk.json"),
            "--equity-usdt",
            "10000",
        ]

        class FakePaperLease:
            def __init__(
                self,
                path: Path,
                *,
                allow_private_parent_mount_boundary: bool = False,
            ) -> None:
                assert path == account_root.resolve() / "account_execution_owner.lock"
                assert allow_private_parent_mount_boundary is True
                calls.append("lease-created")

            def acquire(self) -> None:
                assert not account_root.exists()
                assert not inbox_root.exists()
                calls.append("lease-acquired")

            def close(self) -> None:
                calls.append("lease-closed")

        monkeypatch.setattr(runner, "AccountOwnerLease", FakePaperLease)
        later_resources = (
            "load_demo_rules",
            "AccountExecutionKernel",
            "SequenceAwareMarketRecorder",
            "BybitRawPublicMarketStream",
            "AccountExecutionService",
            "AccountIntentInbox",
        )

    actual_derive = runner.derive_account_route

    def derive_route(**kwargs: object) -> object:
        calls.append("route-derived")
        route = actual_derive(**kwargs)
        assert not account_root.exists()
        assert not inbox_root.exists()
        return route

    def reject_route(**_kwargs: object) -> None:
        assert "lease-acquired" in calls
        calls.append("route-initialization")
        raise RuntimeError("route identity rejected")

    def unexpected_resource(*_args: object, **_kwargs: object) -> None:
        pytest.fail("runtime resource initialized before the leased account route")

    monkeypatch.setattr(runner, "derive_account_route", derive_route)
    monkeypatch.setattr(runner, "ensure_account_route", reject_route)
    for name in later_resources:
        monkeypatch.setattr(runner, name, unexpected_resource)

    with pytest.raises(RuntimeError, match="route identity rejected"):
        runner.main(argv)

    assert calls == [
        "route-derived",
        "lease-created",
        "lease-acquired",
        "route-initialization",
        "lease-closed",
    ]
    assert not account_root.exists()
    assert not inbox_root.exists()


def test_paper_owner_rejects_private_credentials_before_route_or_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import liquidity_migration.account_paper_runner as runner

    monkeypatch.setenv("INVOCATION_ID", "ab" * 16)
    monkeypatch.setenv("REAL_MONEY", "false")
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "must-not-cross-paper-boundary")
    monkeypatch.setattr(
        runner,
        "ensure_account_route",
        lambda **_kwargs: pytest.fail("paper route opened before credential rejection"),
    )
    argv = [
        "--account-root",
        str(tmp_path / "account"),
        "--inbox-root",
        str(tmp_path / "inbox"),
        "--capture-root",
        str(tmp_path / "capture"),
        "--symbols-file",
        str(tmp_path / "symbols.json"),
        "--demo-rules-file",
        str(tmp_path / "rules.json"),
        "--risk-policy-file",
        str(tmp_path / "risk.json"),
        "--equity-usdt",
        "10000",
    ]

    with pytest.raises(RuntimeError, match="paper owner received private exchange credentials"):
        runner.main(argv)


def test_owner_initializes_deterministic_canonical_mirrors_and_reader_validates(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"

    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )

    expected = derive_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest, inbox_manifest = _route_paths(account_root, inbox_root)
    expected_bytes = canonical_json(expected.to_dict()) + b"\n"
    assert route == expected
    assert route.route_id.startswith("account-route-v1-")
    assert len(route.route_id) == len("account-route-v1-") + 64
    assert route.account_root == str(account_root.resolve())
    assert route.inbox_root == str(inbox_root.resolve())
    assert account_manifest.read_bytes() == expected_bytes
    assert inbox_manifest.read_bytes() == expected_bytes
    assert (
        require_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )
        == route
    )
    assert (
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )
        == route
    )


def test_unbound_account_journal_requires_explicit_cutover(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    journal = account_root / "account_journal" / "events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"sequence":1}\n')

    with pytest.raises(
        AccountRouteCutoverRequiredError,
        match=r"account_journal/events\.jsonl.*explicit route cutover",
    ):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )

    assert not account_route_manifest_path(account_root).exists()
    assert not account_route_manifest_path(inbox_root).exists()


@pytest.mark.parametrize("queue_state", ["pending", "completed"])
def test_unbound_inbox_requests_require_explicit_cutover(
    tmp_path: Path,
    queue_state: str,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    request = inbox_root / queue_state / "request.json"
    request.parent.mkdir(parents=True)
    request.write_text("{}\n")

    with pytest.raises(
        AccountRouteCutoverRequiredError,
        match=rf"{queue_state}/request\.json.*explicit route cutover",
    ):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )

    assert not account_route_manifest_path(account_root).exists()
    assert not account_route_manifest_path(inbox_root).exists()


def test_unbound_empty_directory_layout_and_route_temp_are_safe_to_initialize(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    (account_root / "account_journal" / "transactions").mkdir(parents=True)
    for directory in (
        "pending",
        "processing",
        "completed",
        "failed",
        "arrival",
        ".locks",
    ):
        (inbox_root / directory).mkdir(parents=True, exist_ok=True)
    stale_initializer_temp = inbox_root / f".{ACCOUNT_ROUTE_FILENAME}.123.456.tmp"
    stale_initializer_temp.write_bytes(b"incomplete route initializer bytes")

    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    assert require_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    ) == route


def test_unbound_persistent_lock_skeleton_is_safe_to_initialize(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    lock_paths = (
        account_root / "account_execution_owner.lock",
        account_root / "account_journal" / "journal.lock",
        account_root / ".locks" / "account_route.lock",
        inbox_root / ".locks" / "account_route.lock",
        inbox_root / ".locks" / "account_intent_inbox.lock",
        inbox_root / ".locks" / f".account_intent_inbox.lock.create-{'a' * 32}",
    )
    for path in lock_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.chmod(0o600)
    identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in lock_paths}

    route = ensure_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=account_root,
        inbox_root=inbox_root,
    )

    assert require_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=account_root,
        inbox_root=inbox_root,
    ) == route
    assert {
        path: (path.stat().st_dev, path.stat().st_ino) for path in lock_paths
    } == identities


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "mode", "symlink"])
def test_unbound_unsafe_lock_leaf_still_requires_cutover(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    lock_path = account_root / "account_journal" / "journal.lock"
    lock_path.parent.mkdir(parents=True)
    if unsafe_kind == "symlink":
        target = tmp_path / "target.lock"
        target.touch(mode=0o600)
        lock_path.symlink_to(target)
    else:
        lock_path.touch(mode=0o600)
        if unsafe_kind == "hardlink":
            os.link(lock_path, tmp_path / "alias.lock")
        else:
            lock_path.chmod(0o644)

    with pytest.raises(
        AccountRouteCutoverRequiredError,
        match=r"account_journal/journal\.lock.*explicit route cutover",
    ):
        ensure_account_route(
            account_id="bybit-paper-unified",
            environment="paper",
            account_root=account_root,
            inbox_root=inbox_root,
        )


def test_producer_validation_is_read_only_when_route_is_missing(tmp_path: Path) -> None:
    account_root = tmp_path / "missing-account"
    inbox_root = tmp_path / "missing-inbox"

    with pytest.raises(AccountRouteMissingError, match="manifest is missing"):
        require_account_route(
            account_id="bybit-paper-unified",
            environment="paper",
            account_root=account_root,
            inbox_root=inbox_root,
        )

    assert not account_root.exists()
    assert not inbox_root.exists()


def test_owner_recovers_exact_one_sided_creation_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    real_atomic_create = account_route_module._atomic_create
    calls = 0

    def fail_second_create(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-mirror failure")
        real_atomic_create(path, data)

    monkeypatch.setattr(
        account_route_module,
        "_atomic_create",
        fail_second_create,
    )
    with pytest.raises(OSError, match="second-mirror failure"):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )

    account_manifest, inbox_manifest = _route_paths(account_root, inbox_root)
    assert account_manifest.is_file()
    assert not inbox_manifest.exists()
    assert not list(account_root.glob(f".{ACCOUNT_ROUTE_FILENAME}.*.tmp"))
    monkeypatch.setattr(
        account_route_module,
        "_atomic_create",
        real_atomic_create,
    )

    recovered = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )

    assert account_manifest.read_bytes() == inbox_manifest.read_bytes()
    assert read_account_route_manifest(account_root) == recovered
    assert read_account_route_manifest(inbox_root) == recovered


def test_one_sided_manifest_never_adopts_a_different_requested_route(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / "account"
    original_inbox = tmp_path / "inbox-a"
    different_inbox = tmp_path / "inbox-b"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=original_inbox,
    )
    account_manifest, original_inbox_manifest = _route_paths(
        account_root,
        original_inbox,
    )
    original_inbox_manifest.unlink()
    before = account_manifest.read_bytes()

    with pytest.raises(AccountRouteMismatchError, match="does not match requested"):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=different_inbox,
        )

    assert account_manifest.read_bytes() == before
    assert not account_route_manifest_path(different_inbox).exists()


def test_one_sided_recovery_rejects_nonempty_missing_root(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest, inbox_manifest = _route_paths(account_root, inbox_root)
    before = account_manifest.read_bytes()
    inbox_manifest.unlink()
    pending = inbox_root / "pending" / "late-request.json"
    pending.parent.mkdir(exist_ok=True)
    pending.write_text("{}\n")

    with pytest.raises(
        AccountRouteCutoverRequiredError,
        match=r"pending/late-request\.json.*explicit route cutover",
    ):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )

    assert account_manifest.read_bytes() == before
    assert not inbox_manifest.exists()


def test_cross_wired_valid_manifests_are_rejected_without_rewrite(
    tmp_path: Path,
) -> None:
    account_a = tmp_path / "account-a"
    inbox_a = tmp_path / "inbox-a"
    account_b = tmp_path / "account-b"
    inbox_b = tmp_path / "inbox-b"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_a,
        inbox_root=inbox_a,
    )
    ensure_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=account_b,
        inbox_root=inbox_b,
    )
    account_manifest = account_route_manifest_path(account_a)
    inbox_manifest = account_route_manifest_path(inbox_b)
    before = (account_manifest.read_bytes(), inbox_manifest.read_bytes())

    with pytest.raises(AccountRouteMismatchError, match="manifests disagree"):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_a,
            inbox_root=inbox_b,
        )

    assert (account_manifest.read_bytes(), inbox_manifest.read_bytes()) == before


@pytest.mark.parametrize(
    ("account_id", "environment"),
    [
        ("another-demo-account", "demo"),
        ("bybit-demo-unified", "paper"),
    ],
)
def test_account_or_environment_change_is_rejected(
    tmp_path: Path,
    account_id: str,
    environment: str,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )

    with pytest.raises(AccountRouteMismatchError, match="does not match requested"):
        require_account_route(
            account_id=account_id,
            environment=environment,
            account_root=account_root,
            inbox_root=inbox_root,
        )


def test_root_path_change_is_rejected_without_creating_a_new_manifest(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    different_inbox = tmp_path / "different-inbox"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )

    with pytest.raises(AccountRouteMismatchError, match="does not match requested"):
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=different_inbox,
        )

    assert not account_route_manifest_path(different_inbox).exists()


def test_unknown_manifest_fields_are_rejected_before_canonical_adoption(
    tmp_path: Path,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest = account_route_manifest_path(account_root)
    payload = route.to_dict()
    payload["silent_extension"] = True
    _rewrite_canonical(account_manifest, payload)

    with pytest.raises(AccountRouteIntegrityError, match="unknown fields: silent_extension"):
        require_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=account_root,
            inbox_root=inbox_root,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("route_id", "account-route-v1-" + "0" * 64, "route_id does not match"),
        ("schema_version", 2, "unsupported account route schema"),
        ("environment", "live", "environment must be exactly"),
    ],
)
def test_manifest_tampering_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest = account_route_manifest_path(account_root)
    payload = route.to_dict()
    payload[field] = value
    _rewrite_canonical(account_manifest, payload)

    with pytest.raises(AccountRouteIntegrityError, match=message):
        read_account_route_manifest(account_root)


def test_noncanonical_manifest_bytes_are_rejected(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest = account_route_manifest_path(account_root)
    account_manifest.write_text(json.dumps(route.to_dict(), indent=2) + "\n")

    with pytest.raises(AccountRouteIntegrityError, match="not canonical"):
        read_account_route_manifest(account_root)


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest, inbox_manifest = _route_paths(account_root, inbox_root)
    account_manifest.unlink()
    account_manifest.symlink_to(inbox_manifest)

    with pytest.raises(AccountRouteIntegrityError, match="must not be a symlink"):
        read_account_route_manifest(account_root)


def test_manifest_must_remain_owner_only_and_single_link(tmp_path: Path) -> None:
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest = account_route_manifest_path(account_root)
    account_manifest.chmod(0o644)
    with pytest.raises(AccountRouteIntegrityError, match="mode 0600"):
        read_account_route_manifest(account_root)

    account_manifest.chmod(0o600)
    os.link(account_manifest, tmp_path / "route-hardlink.json")
    with pytest.raises(AccountRouteIntegrityError, match="must not be hard-linked"):
        read_account_route_manifest(account_root)


def test_resolved_symlink_roots_have_one_identity(tmp_path: Path) -> None:
    real_account = tmp_path / "real-account"
    real_inbox = tmp_path / "real-inbox"
    real_account.mkdir()
    real_inbox.mkdir()
    linked_account = tmp_path / "linked-account"
    linked_inbox = tmp_path / "linked-inbox"
    linked_account.symlink_to(real_account, target_is_directory=True)
    linked_inbox.symlink_to(real_inbox, target_is_directory=True)

    route = ensure_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=linked_account,
        inbox_root=linked_inbox,
    )

    assert route.account_root == str(real_account.resolve())
    assert route.inbox_root == str(real_inbox.resolve())
    assert (
        require_account_route(
            account_id="bybit-paper-unified",
            environment="paper",
            account_root=real_account,
            inbox_root=real_inbox,
        )
        == route
    )


def test_crossed_concurrent_initializers_serialize_on_both_root_locks(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    barrier = threading.Barrier(2)

    def initialize(account_root: Path, inbox_root: Path):
        barrier.wait(timeout=5)
        try:
            return ensure_account_route(
                account_id="bybit-demo-unified",
                environment="demo",
                account_root=account_root,
                inbox_root=inbox_root,
            )
        except AccountRouteMismatchError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(initialize, first_root, second_root),
            executor.submit(initialize, second_root, first_root),
        )
        outcomes = [future.result(timeout=10) for future in futures]

    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, AccountRouteMismatchError) for outcome in outcomes) == 1
    assert read_account_route_manifest(first_root) == read_account_route_manifest(second_root)


def test_manifest_creation_fsyncs_files_and_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_modes: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        observed_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(account_route_module.os, "fsync", recording_fsync)
    ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )

    assert sum(stat.S_ISREG(mode) for mode in observed_modes) >= 2
    assert sum(stat.S_ISDIR(mode) for mode in observed_modes) >= 2


@pytest.mark.parametrize(
    ("account_id", "environment", "same_root", "message"),
    [
        (" bybit-demo-unified", "demo", False, "without surrounding whitespace"),
        ("bybit-demo-unified", "live", False, "exactly 'demo' or 'paper'"),
        ("bybit-demo-unified", "demo", True, "must be distinct"),
    ],
)
def test_invalid_requested_identity_fails_before_initialization(
    tmp_path: Path,
    account_id: str,
    environment: str,
    same_root: bool,
    message: str,
) -> None:
    account_root = tmp_path / "account"
    inbox_root = account_root if same_root else tmp_path / "inbox"

    with pytest.raises(AccountRouteConfigurationError, match=message):
        ensure_account_route(
            account_id=account_id,
            environment=environment,
            account_root=account_root,
            inbox_root=inbox_root,
        )

    assert not account_root.exists()
    assert not inbox_root.exists()
