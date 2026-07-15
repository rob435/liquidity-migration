from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import liquidity_migration.account_owner_readiness as readiness
from liquidity_migration.account_kernel import GENESIS_HASH
from liquidity_migration.account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    write_account_owner_health,
)
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.market_capture import (
    OWNER_CAPTURE_READINESS_FILENAME,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
)


NOW_NS = 2_000_000_000_000
CURRENT_INVOCATION_ID = "a1" * 16
PREVIOUS_INVOCATION_ID = "b2" * 16
REPO_ROOT = Path(__file__).resolve().parents[1]


def _capture_config() -> MarketCaptureConfig:
    return MarketCaptureConfig(
        depth=50,
        segment_max_bytes=1_000_000,
        fsync_every_records=100,
        min_free_disk_bytes=1,
        ring_records_per_symbol=100,
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
        config=_capture_config(),
        owner_invocation_id=capture_invocation_id or invocation_id,
    )
    recorder.on_message(_snapshot(), local_receive_ts_ns=capture_receive_ts_ns)
    recorder.close()
    return account, inbox, capture


@pytest.mark.parametrize("environment", ["demo", "paper"])
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
    assert receipt.capture_latest_receive_ts_ns == NOW_NS - 500_000
    assert receipt.capture_age_ns == 500_000


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


def test_readiness_rejects_fresh_capture_from_previous_systemd_generation(
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

    assert receipt.capture_latest_receive_ts_ns == NOW_NS - 500_000


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

    assert receipt.capture_latest_receive_ts_ns < receipt.health_observed_ts_ns


def test_readiness_rejects_stale_or_malformed_capture(tmp_path: Path) -> None:
    account, inbox, capture = _ready_roots(
        tmp_path,
        capture_receive_ts_ns=NOW_NS - 10_000_000,
    )

    with pytest.raises(RuntimeError, match="capture is stale"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            now_ns=NOW_NS,
            max_age_ns=2_000_000,
        )

    sidecar = capture / OWNER_CAPTURE_READINESS_FILENAME
    sidecar.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        readiness.latest_capture_receive_ts_ns(
            capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


def test_stale_sidecar_is_rejected_before_opening_its_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, inbox, capture = _ready_roots(
        tmp_path,
        capture_receive_ts_ns=NOW_NS - 10_000_000,
    )

    def should_not_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stale sidecar must fail before segment I/O")

    monkeypatch.setattr(readiness, "_read_referenced_capture_record", should_not_open)

    with pytest.raises(RuntimeError, match="capture is stale"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            now_ns=NOW_NS,
            max_age_ns=2_000_000,
        )


def test_standalone_capture_does_not_require_or_publish_owner_sidecar(tmp_path: Path) -> None:
    capture = tmp_path / "standalone-capture"
    recorder = SequenceAwareMarketRecorder(capture, config=_capture_config())
    row = recorder.on_message(_snapshot(), local_receive_ts_ns=NOW_NS - 500_000)[0]
    recorder.close()

    assert "owner_invocation_id" not in row
    assert not (capture / OWNER_CAPTURE_READINESS_FILENAME).exists()
    assert len(list(capture.rglob("segment-*.jsonl"))) == 1


def test_capture_generation_filter_validates_expected_invocation_id(tmp_path: Path) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)

    with pytest.raises(ValueError, match="capture invocation id"):
        readiness.latest_capture_receive_ts_ns(
            capture,
            expected_invocation_id="A1" * 16,
        )


def test_capture_readiness_is_bounded_and_does_not_glob_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)

    def reject_glob(_self: Path, _pattern: str) -> list[Path]:
        raise AssertionError("readiness must not enumerate capture segments")

    monkeypatch.setattr(Path, "glob", reject_glob)

    assert readiness.latest_capture_receive_ts_ns(
        capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
    ) == NOW_NS - 500_000


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("record_sha256", "0" * 64, "record hash"),
        ("segment_inode", 1, "inode"),
        ("byte_offset", 1, "record hash|byte range"),
    ),
)
def test_capture_readiness_rejects_tampered_sidecar_target(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)
    sidecar_path = capture / OWNER_CAPTURE_READINESS_FILENAME
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload[field] = value
    sidecar_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        readiness.latest_capture_receive_ts_ns(
            capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


@pytest.mark.parametrize("escaped", ("../outside.jsonl", "/tmp/outside.jsonl", "a/../../b"))
def test_capture_readiness_rejects_segment_path_escape(
    tmp_path: Path,
    escaped: str,
) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)
    sidecar_path = capture / OWNER_CAPTURE_READINESS_FILENAME
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["segment_path"] = escaped
    sidecar_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="escape|relative"):
        readiness.latest_capture_receive_ts_ns(
            capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


def test_capture_readiness_refuses_symlinked_referenced_segment(tmp_path: Path) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)
    sidecar = json.loads(
        (capture / OWNER_CAPTURE_READINESS_FILENAME).read_text(encoding="utf-8")
    )
    segment = capture / str(sidecar["segment_path"])
    replacement = segment.with_name("segment-copy.jsonl")
    replacement.write_bytes(segment.read_bytes())
    replacement.chmod(0o600)
    segment.unlink()
    segment.symlink_to(replacement.name)

    with pytest.raises(ValueError, match="regular non-symlink"):
        readiness.latest_capture_receive_ts_ns(
            capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


def test_capture_readiness_tolerates_concurrent_append_after_referenced_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account, _inbox, capture = _ready_roots(tmp_path)
    sidecar = json.loads(
        (capture / OWNER_CAPTURE_READINESS_FILENAME).read_text(encoding="utf-8")
    )
    segment = capture / str(sidecar["segment_path"])
    real_pread = os.pread
    appended = False

    def append_then_read(descriptor: int, length: int, offset: int) -> bytes:
        nonlocal appended
        if not appended:
            with segment.open("ab") as handle:
                handle.write(b'{"concurrent_append":true}\n')
                handle.flush()
            appended = True
        return real_pread(descriptor, length, offset)

    monkeypatch.setattr(readiness.os, "pread", append_then_read)

    assert readiness.latest_capture_receive_ts_ns(
        capture,
        expected_invocation_id=CURRENT_INVOCATION_ID,
    ) == NOW_NS - 500_000
    assert appended


def test_readiness_rejects_wrong_account_identity_and_root_alias(tmp_path: Path) -> None:
    account, inbox, capture = _ready_roots(tmp_path)

    with pytest.raises(ValueError, match="does not match its environment"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=capture,
            expected_invocation_id=CURRENT_INVOCATION_ID,
            expected_account_id="bybit-paper-unified",
            now_ns=NOW_NS,
        )
    with pytest.raises(ValueError, match="must be distinct"):
        readiness.require_account_owner_ready(
            environment="demo",
            account_root=account,
            inbox_root=inbox,
            capture_root=account,
            expected_invocation_id=CURRENT_INVOCATION_ID,
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
        readiness.latest_capture_receive_ts_ns(
            alias,
            expected_invocation_id=CURRENT_INVOCATION_ID,
        )


@pytest.mark.parametrize(
    "unit_name",
    (
        "liquidity-migration-account-execution.service",
        "liquidity-migration-account-paper-execution.service",
    ),
)
def test_owner_exec_start_post_binds_current_systemd_invocation_in_registered_wrapper(
    unit_name: str,
) -> None:
    text = (REPO_ROOT / "deploy" / "systemd" / unit_name).read_text(encoding="utf-8")
    assert (
        "ExecStartPost=/opt/liquidity-migration/scripts/run_authorized_fresh_runtime.sh "
        f"{unit_name} readiness"
    ) in text

    wrapper = (REPO_ROOT / "scripts" / "run_authorized_fresh_runtime.sh").read_text(encoding="utf-8")
    assert 'if [ "$#" -ne 2 ]; then' in wrapper
    assert '--expected-invocation-id "${INVOCATION_ID:?INVOCATION_ID is required}"' in wrapper
