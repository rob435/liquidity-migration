from __future__ import annotations

import json
from pathlib import Path

import pytest

import liquidity_migration.runtime.account_owner_readiness as readiness
from liquidity_migration.account.account_kernel import GENESIS_HASH
from liquidity_migration.account.account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    write_account_owner_health,
)
from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.account.market_capture import (
    OWNER_CAPTURE_READINESS_FILENAME,
    OWNER_MARKET_READINESS_FILENAME,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
)


NOW_NS = 2_000_000_000_000
CURRENT_INVOCATION_ID = "a1" * 16
PREVIOUS_INVOCATION_ID = "b2" * 16
REPO_ROOT = Path(__file__).resolve().parents[2]


def _capture_config() -> MarketCaptureConfig:
    return MarketCaptureConfig(
        depth=50,
        segment_max_bytes=1_000_000,
        fsync_every_records=100,
        min_free_disk_bytes=1,
    )


def _snapshot() -> dict[str, object]:
    return {
        "topic": "orderbook.50.BTCUSDT",
        "type": "snapshot",
        "ts": 1,
        "cts": 1,
        "data": {
            "s": "BTCUSDT",
            "b": [["10", "1"]],
            "a": [["11", "1"]],
            "u": 1,
            "seq": 1,
        },
    }


def _ready_roots(
    tmp_path: Path,
    *,
    environment: str = "demo",
    invocation_id: str = CURRENT_INVOCATION_ID,
    capture_invocation_id: str | None = None,
    health_observed_ts_ns: int = NOW_NS - 1_000_000,
    capture_receive_ts_ns: int = NOW_NS - 500_000,
    persist_raw_market: bool = True,
) -> tuple[Path, Path, Path]:
    account = tmp_path / f"{environment}-account"
    inbox = tmp_path / f"{environment}-inbox"
    capture = tmp_path / f"{environment}-capture"
    account_id = f"bybit-{environment}-unified"
    ensure_account_route(
        account_id=account_id,
        environment=environment,
        account_root=account,
        inbox_root=inbox,
    )
    write_account_owner_health(
        account,
        AccountOwnerHealth(
            owner="account_execution",
            environment=environment,
            account_id=account_id,
            status=AccountOwnerHealthStatus.HEALTHY,
            observed_ts_ns=health_observed_ts_ns,
            loop_sequence=3,
            journal_sequence=0,
            journal_state_hash=GENESIS_HASH,
            equity_usdt=10_000.0,
            available_margin_usdt=9_000.0,
            requested_symbols_ready=True,
            invocation_id=invocation_id,
        ),
    )
    recorder = SequenceAwareMarketRecorder(
        capture,
        config=MarketCaptureConfig(
            depth=50,
            segment_max_bytes=1_000_000,
            fsync_every_records=100,
            min_free_disk_bytes=1,
            persist_raw_market=persist_raw_market,
        ),
        owner_invocation_id=capture_invocation_id or invocation_id,
    )
    recorder.on_message(_snapshot(), local_receive_ts_ns=capture_receive_ts_ns)
    recorder.close()
    return account, inbox, capture


@pytest.mark.parametrize("environment", ["demo", "mainnet"])
def test_require_owner_ready_binds_route_health_journal_and_capture(
    tmp_path: Path,
    environment: str,
) -> None:
    account, inbox, capture = _ready_roots(tmp_path, environment=environment)

    receipt = readiness.require_account_owner_ready(
        environment=environment,
        account_root=account,
        inbox_root=inbox,
        capture_root=capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
        now_ns=NOW_NS,
        max_age_ns=2_000_000,
    )

    assert receipt.environment == environment
    assert receipt.account_id == f"bybit-{environment}-unified"
    assert receipt.owner_invocation_id == CURRENT_INVOCATION_ID
    assert receipt.journal_sequence == 0
    assert receipt.journal_state_hash == GENESIS_HASH
    assert receipt.market_symbol == "BTCUSDT"
    assert receipt.market_required_symbol_count == 1
    assert receipt.market_oldest_required_receive_ts_ns == NOW_NS - 500_000
    assert receipt.market_age_ns == 500_000
    assert receipt.raw_market_persistence_enabled is True


def test_owner_readiness_does_not_require_bulk_raw_market_persistence(
    tmp_path: Path,
) -> None:
    account, inbox, capture = _ready_roots(tmp_path, persist_raw_market=False)

    receipt = readiness.require_account_owner_ready(
        environment="demo",
        account_root=account,
        inbox_root=inbox,
        capture_root=capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
        now_ns=NOW_NS,
        max_age_ns=2_000_000,
    )

    assert receipt.raw_market_persistence_enabled is False
    assert receipt.market_oldest_required_receive_ts_ns == NOW_NS - 500_000
    assert not list(capture.rglob("segment-*.jsonl"))
    assert not (capture / OWNER_CAPTURE_READINESS_FILENAME).exists()
    assert (capture / OWNER_MARKET_READINESS_FILENAME).is_file()


def test_market_readiness_accepts_only_the_explicit_owner_uid(
    tmp_path: Path,
) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)
    owner_uid = (capture / OWNER_MARKET_READINESS_FILENAME).stat().st_uid

    assert (
        readiness.latest_market_readiness(
            capture,
            expected_owner_uid=owner_uid,
        ).symbol
        == "BTCUSDT"
    )
    with pytest.raises(ValueError, match="not the expected owner uid"):
        readiness.latest_market_readiness(
            capture,
            expected_owner_uid=owner_uid + 1,
        )


def test_production_readiness_uses_adjacent_operational_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, inbox, capture = _ready_roots(tmp_path)
    real_health_check = readiness.require_recent_account_owner_health
    observed: dict[str, object] = {}

    def health_check(*args: object, **kwargs: object) -> AccountOwnerHealth:
        observed["caller_now_ns"] = kwargs["now_ns"]
        kwargs["now_ns"] = NOW_NS
        return real_health_check(*args, **kwargs)

    monkeypatch.setattr(readiness, "require_recent_account_owner_health", health_check)
    monkeypatch.setattr(readiness.time, "time_ns", lambda: NOW_NS)

    receipt = readiness.require_account_owner_ready(
        environment="demo",
        account_root=account,
        inbox_root=inbox,
        capture_root=capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
        max_age_ns=2_000_000,
    )

    assert observed["caller_now_ns"] is None
    assert receipt.market_age_ns == 500_000


def test_readiness_rejects_weakened_freshness_bound_before_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="registered 30 seconds"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=tmp_path / "missing-account",
            inbox_root=tmp_path / "missing-inbox",
            capture_root=tmp_path / "missing-capture",
            expected_invocation_id=CURRENT_INVOCATION_ID,
            max_age_ns=readiness.REGISTERED_MAX_AGE_NS + 1,
        )


def test_readiness_rejects_fresh_health_from_previous_systemd_generation(
    tmp_path: Path,
) -> None:
    account, inbox, capture = _ready_roots(
        tmp_path,
        invocation_id=PREVIOUS_INVOCATION_ID,
    )

    with pytest.raises(RuntimeError, match="current systemd generation"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            now_ns=NOW_NS,
            max_age_ns=2_000_000,
        )


def test_readiness_rejects_fresh_market_from_previous_systemd_generation(
    tmp_path: Path,
) -> None:
    account, inbox, capture = _ready_roots(
        tmp_path,
        capture_invocation_id=PREVIOUS_INVOCATION_ID,
    )

    with pytest.raises(RuntimeError, match="does not match the current systemd generation"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            now_ns=NOW_NS,
            max_age_ns=2_000_000,
        )


def test_readiness_ignores_old_malformed_and_crash_tail_segments(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "demo-capture"
    historical = capture / "1970-01-01" / "BTCUSDT" / "segment-000000.jsonl"
    historical.parent.mkdir(parents=True)
    historical.write_bytes(b'{"unterminated_previous_generation":')
    account, inbox, observed_capture = _ready_roots(tmp_path)
    assert observed_capture == capture
    sidecar = json.loads(
        (capture / OWNER_CAPTURE_READINESS_FILENAME).read_text(encoding="utf-8")
    )
    assert sidecar["segment_path"].endswith("segment-000001.jsonl")

    receipt = readiness.require_account_owner_ready(
        environment="demo",
        account_root=account,
        inbox_root=inbox,
        capture_root=capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
        now_ns=NOW_NS,
        max_age_ns=2_000_000,
    )

    assert receipt.market_oldest_required_receive_ts_ns == NOW_NS - 500_000


def test_readiness_does_not_infer_cross_projection_wall_clock_order(
    tmp_path: Path,
) -> None:
    account, inbox, capture = _ready_roots(
        tmp_path,
        capture_receive_ts_ns=NOW_NS - 1_000_000,
        health_observed_ts_ns=NOW_NS - 500_000,
    )

    receipt = readiness.require_account_owner_ready(
        environment="demo",
        account_root=account,
        inbox_root=inbox,
        capture_root=capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
        now_ns=NOW_NS,
        max_age_ns=2_000_000,
    )

    assert (
        receipt.market_oldest_required_receive_ts_ns
        < receipt.health_observed_ts_ns
    )


def test_readiness_rejects_stale_or_malformed_capture(tmp_path: Path) -> None:
    account, inbox, capture = _ready_roots(
        tmp_path,
        capture_receive_ts_ns=NOW_NS - 10_000_000,
    )

    with pytest.raises(RuntimeError, match="live market is stale"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            now_ns=NOW_NS,
            max_age_ns=2_000_000,
        )

    sidecar = capture / OWNER_MARKET_READINESS_FILENAME
    sidecar.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        readiness.latest_market_readiness(
            capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


def test_standalone_capture_does_not_require_or_publish_owner_sidecar(tmp_path: Path) -> None:
    capture = tmp_path / "standalone-capture"
    recorder = SequenceAwareMarketRecorder(capture, config=_capture_config())
    row = recorder.on_message(_snapshot(), local_receive_ts_ns=NOW_NS - 500_000)[0]
    recorder.close()

    assert "owner_invocation_id" not in row
    assert not (capture / OWNER_CAPTURE_READINESS_FILENAME).exists()
    assert not (capture / OWNER_MARKET_READINESS_FILENAME).exists()
    assert len(list(capture.rglob("segment-*.jsonl"))) == 1


def test_readiness_rejects_wrong_account_identity(tmp_path: Path) -> None:
    account, inbox, capture = _ready_roots(tmp_path)

    with pytest.raises(ValueError, match="does not match its environment"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            expected_account_id="bybit-mainnet-unified",
            now_ns=NOW_NS,
        )


def test_wait_is_bounded_and_reports_last_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, inbox, capture = _ready_roots(tmp_path)
    calls = 0

    def unavailable(**_kwargs: object) -> readiness.AccountOwnerReadiness:
        nonlocal calls
        calls += 1
        raise RuntimeError("not reconciled")

    ticks = iter((0.0, 0.0, 0.5, 1.0))
    monkeypatch.setattr(readiness, "require_account_owner_ready", unavailable)

    with pytest.raises(TimeoutError, match="not reconciled"):
        readiness.wait_for_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            timeout_seconds=1.0,
            poll_seconds=0.5,
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )
    assert calls == 3


def test_capture_root_must_not_be_a_symlink(tmp_path: Path) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)
    alias = tmp_path / "capture-link"
    alias.symlink_to(capture, target_is_directory=True)

    with pytest.raises(ValueError, match="non-symlink directory"):
        readiness.latest_market_readiness(
            alias,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


# The demo owner deliberately has no ExecStartPost since 2026-08-03: a failed
# readiness probe was systemd-killing a live owner that was still draining
# exits. The gate survives only on the mainnet owner.
@pytest.mark.parametrize(
    "unit_name",
    ("liquidity-migration-account-execution-mainnet.service",),
)
def test_owner_exec_start_post_binds_current_systemd_invocation_in_registered_wrapper(
    unit_name: str,
) -> None:
    text = (REPO_ROOT / "deploy" / "systemd" / unit_name).read_text(encoding="utf-8")
    assert (
        "ExecStartPost=/opt/liquidity-migration/scripts/run_authorized_runtime.sh "
        f"{unit_name} readiness"
    ) in text

    wrapper = (REPO_ROOT / "scripts" / "run_authorized_runtime.sh").read_text(encoding="utf-8")
    assert 'if [ "$#" -ne 2 ]; then' in wrapper
    assert '--expected-invocation-id "${INVOCATION_ID:?INVOCATION_ID is required}"' in wrapper
