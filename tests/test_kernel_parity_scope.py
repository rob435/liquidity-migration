from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import liquidity_migration.captured_account_replay as replay_module
import liquidity_migration.strategy_event_parity as event_module
from liquidity_migration.kernel_parity import (
    _load_comparison_scope,
    build_comparison_scope,
    write_comparison_scope,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stub_evidence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_capture: Path,
    replay_input_sha256: str | None = None,
    event_gate_passed: bool = True,
) -> tuple[Path, Path]:
    replay_receipt = target_capture.parent / "captured-account-replay.json"
    replay_receipt.write_text("{}\n", encoding="utf-8")
    replay_receipt.chmod(0o400)
    event_receipt = target_capture.parent / "event-parity.json"
    event_receipt.write_text('{"event":true}\n', encoding="utf-8")
    event_receipt.chmod(0o600)
    target_bytes = target_capture.read_bytes()
    target_hash = _sha(target_bytes)
    target_stat = target_capture.stat()
    monkeypatch.setattr(
        replay_module,
        "load_captured_account_replay_receipt",
        lambda _path: {
            "has_durable_request_batches": True,
            "ordered_batch_ids": ["natural/a", "natural/b"],
            "effective_runtime_config": {
                "artifact_sha256": _sha(b"effective-config"),
                "execution_authorization": "not_granted",
            },
            "source_files": {
                "target_scheduling_capture": {
                    "path": str(target_capture.resolve()),
                    "size": len(target_bytes),
                    "sha256": target_hash,
                    "device": target_stat.st_dev,
                    "inode": target_stat.st_ino,
                    "mtime_ns": target_stat.st_mtime_ns,
                    "mode": target_stat.st_mode & 0o777,
                }
            },
            "outputs": {
                "historical_root": str((target_capture.parent / "historical").resolve()),
                "paper_root": str((target_capture.parent / "paper").resolve()),
                "historical_account_journal_sha256": _sha(b"historical"),
                "paper_account_journal_sha256": _sha(b"paper"),
            },
        },
    )
    observed_replay_hash = replay_input_sha256 or target_hash
    monkeypatch.setattr(
        event_module,
        "load_strategy_event_parity_receipt",
        lambda _path: {
            "strategy_event_replay_gate_passed": event_gate_passed,
            "replay_provenance": {
                "deployment_valid": True,
                "replay_manifest": {
                    "path": str((target_capture.parent / "replay-manifest.json").resolve()),
                    "size_bytes": 1,
                    "sha256": _sha(b"manifest"),
                    "schema_version": 2,
                    "artifact_sha256": _sha(b"manifest-artifact"),
                    "created_ts_ns": 1,
                },
                "canonical_source_capture": {
                    "path": str(target_capture.resolve()),
                    "size_bytes": len(target_bytes),
                    "sha256": observed_replay_hash,
                    "device": target_stat.st_dev,
                    "inode": target_stat.st_ino,
                    "mtime_ns": target_stat.st_mtime_ns,
                    "mode": target_stat.st_mode & 0o777,
                    "uid": target_stat.st_uid,
                    "nlink": target_stat.st_nlink,
                    "capture_event_count": 1,
                    "capture_chain_hash": _sha(b"capture-chain"),
                    "source_environment": "demo",
                },
            }
        },
    )
    return replay_receipt, event_receipt


def test_builds_and_publishes_source_derived_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_capture = (tmp_path / "target-capture.jsonl").resolve()
    target_capture.write_bytes(b'{"target":1}\n')
    replay_receipt, event_receipt = _stub_evidence(
        monkeypatch,
        target_capture=target_capture,
    )

    payload = build_comparison_scope(
        captured_account_replay_receipt=replay_receipt,
        event_parity_receipt=event_receipt,
    )
    output = write_comparison_scope((tmp_path / "scope.json").resolve(), payload)

    assert payload["batch_ids"] == ["natural/a", "natural/b"]
    assert payload["captured_account_replay_receipt"]["path"] == str(replay_receipt.resolve())
    assert payload["event_parity_receipt"]["path"] == str(event_receipt.resolve())
    assert output.stat().st_mode & 0o777 == 0o600
    _identity, batch_ids, effective_runtime_config, provenance = _load_comparison_scope(
        output,
        expected_event_parity_identity=payload["event_parity_receipt"],
    )
    assert batch_ids == ("natural/a", "natural/b")
    assert effective_runtime_config["execution_authorization"] == "not_granted"
    assert provenance["captured_account_replay_receipt"] == payload["captured_account_replay_receipt"]


def test_rejects_event_replay_from_another_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_capture = (tmp_path / "target-capture.jsonl").resolve()
    target_capture.write_bytes(b'{"target":1}\n')
    replay_receipt, event_receipt = _stub_evidence(
        monkeypatch,
        target_capture=target_capture,
        replay_input_sha256=_sha(b"other capture"),
    )

    with pytest.raises(ValueError, match="not replayed from the natural target capture"):
        build_comparison_scope(
            captured_account_replay_receipt=replay_receipt,
            event_parity_receipt=event_receipt,
        )


def test_rejects_failed_event_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_capture = (tmp_path / "target-capture.jsonl").resolve()
    target_capture.write_bytes(b'{"target":1}\n')
    replay_receipt, event_receipt = _stub_evidence(
        monkeypatch,
        target_capture=target_capture,
        event_gate_passed=False,
    )

    with pytest.raises(ValueError, match="event parity gate did not pass"):
        build_comparison_scope(
            captured_account_replay_receipt=replay_receipt,
            event_parity_receipt=event_receipt,
        )


def test_rejects_target_capture_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_capture = (tmp_path / "target-capture.jsonl").resolve()
    target_capture.write_bytes(b'{"target":1}\n')
    replay_receipt, event_receipt = _stub_evidence(
        monkeypatch,
        target_capture=target_capture,
    )
    target_capture.write_bytes(b'{"target":2}\n')

    with pytest.raises(ValueError, match="source changed"):
        build_comparison_scope(
            captured_account_replay_receipt=replay_receipt,
            event_parity_receipt=event_receipt,
        )


def test_scope_reopens_both_receipts_and_rejects_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_capture = (tmp_path / "target-capture.jsonl").resolve()
    target_capture.write_bytes(b'{"target":1}\n')
    replay_receipt, event_receipt = _stub_evidence(
        monkeypatch,
        target_capture=target_capture,
    )
    payload = build_comparison_scope(
        captured_account_replay_receipt=replay_receipt,
        event_parity_receipt=event_receipt,
    )
    output = write_comparison_scope((tmp_path / "scope.json").resolve(), payload)

    replay_receipt.chmod(0o600)
    replay_receipt.write_text('{"forged":true}\n', encoding="utf-8")
    replay_receipt.chmod(0o400)
    with pytest.raises(ValueError, match="does not reproduce from its bound receipts"):
        _load_comparison_scope(
            output,
            expected_event_parity_identity=payload["event_parity_receipt"],
        )

    replay_receipt.unlink()
    replay_receipt.write_text("{}\n", encoding="utf-8")
    replay_receipt.chmod(0o400)
    hardlink = tmp_path / "event-parity-hardlink.json"
    os.link(event_receipt, hardlink)
    with pytest.raises(ValueError, match="must not be hard-linked"):
        build_comparison_scope(
            captured_account_replay_receipt=replay_receipt,
            event_parity_receipt=event_receipt,
        )
