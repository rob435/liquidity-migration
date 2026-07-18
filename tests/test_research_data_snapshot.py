from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

import liquidity_migration.research_data_snapshot as snapshot_module
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.research_data_snapshot import (
    ResearchSnapshotError,
    build_snapshot_plan,
    capture_snapshot,
    extract_snapshot,
    plan_payload,
    verify_snapshot,
)


CONTRACT_SHA = "a" * 64
COMMIT = "b" * 40


def _write(root: Path, relative: str, data: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "mutable-root"
    _write(root, "archive_trade_manifest/date=2022-01-01/symbol=OLD/part.parquet", b"old-manifest")
    _write(root, "archive_trade_manifest/date=2026-01-01/symbol=BTCUSDT/part.parquet", b"manifest")
    _write(root, "klines_1h/date=2025-12-31/symbol=BTCUSDT/part.parquet", b"before")
    _write(root, "klines_1h/date=2026-01-01/symbol=BTCUSDT/part.parquet", b"inside-a")
    _write(root, "klines_1h/date=2026-01-02/symbol=ETHUSDT/part.parquet", b"inside-b")
    _write(root, "klines_1h/date=2026-01-03/symbol=BTCUSDT/part.parquet", b"after")
    _write(root, "funding/date=2026-01-02/symbol=ETHUSDT/part.parquet", b"funding")
    _write(root, "reports/date=2026-01-01/ignored.parquet", b"outcome")
    _write(root, "residual_momentum.parquet", b"legacy-feature")
    return root


def _plan(root: Path):
    return build_snapshot_plan(
        root,
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        datasets=("archive_trade_manifest", "klines_1h", "funding"),
    )


def test_plan_bounds_raw_datasets_but_retains_full_manifest(tmp_path: Path) -> None:
    plan = _plan(_fixture_root(tmp_path))
    paths = [row.relative_path for row in plan.files]
    assert paths == sorted(paths)
    assert "archive_trade_manifest/date=2022-01-01/symbol=OLD/part.parquet" in paths
    assert "klines_1h/date=2025-12-31/symbol=BTCUSDT/part.parquet" not in paths
    assert "klines_1h/date=2026-01-03/symbol=BTCUSDT/part.parquet" not in paths
    assert all(not path.startswith("reports/") for path in paths)
    assert "residual_momentum.parquet" not in paths
    payload = plan_payload(plan)
    assert payload["outcomes_inspected"] is False
    assert payload["file_count"] == 5


def test_capture_verify_extract_round_trip(tmp_path: Path) -> None:
    source = _fixture_root(tmp_path)
    plan = _plan(source)
    container = tmp_path / "evidence" / "snapshot.sqlite"
    receipt = tmp_path / "evidence" / "receipt.json"
    captured = capture_snapshot(
        plan,
        output=container,
        receipt_path=receipt,
        contract_sha256=CONTRACT_SHA,
        code_commit=COMMIT,
        batch_size=2,
        progress_every=0,
    )
    assert captured["file_count"] == len(plan.files)
    assert captured["contract_sha256"] == CONTRACT_SHA
    assert captured["verification"] == "full_content"
    assert hashlib.sha256(container.read_bytes()).hexdigest() == captured["container"]["sha256"]

    verified = verify_snapshot(container, receipt_path=receipt, full_content=True, progress_every=0)
    assert verified["logical_sha256"] == captured["logical_sha256"]
    output = tmp_path / "reconstructed"
    reconstruction_receipt = tmp_path / "reconstruction.json"
    reconstructed = extract_snapshot(
        container,
        receipt_path=receipt,
        output_root=output,
        reconstruction_receipt_path=reconstruction_receipt,
        progress_every=0,
    )
    assert reconstructed["file_count"] == len(plan.files)
    for row in plan.files:
        source_bytes = source.joinpath(*row.relative_path.split("/")).read_bytes()
        assert output.joinpath(*row.relative_path.split("/")).read_bytes() == source_bytes
    assert json.loads(reconstruction_receipt.read_text(encoding="utf-8"))["outcomes_inspected"] is False


def test_capture_is_create_only_and_tampering_fails(tmp_path: Path) -> None:
    plan = _plan(_fixture_root(tmp_path))
    container = tmp_path / "snapshot.sqlite"
    receipt = tmp_path / "receipt.json"
    capture_snapshot(
        plan,
        output=container,
        receipt_path=receipt,
        contract_sha256=CONTRACT_SHA,
        code_commit=COMMIT,
        progress_every=0,
    )
    with pytest.raises(FileExistsError, match="create-only"):
        capture_snapshot(
            plan,
            output=container,
            receipt_path=receipt,
            contract_sha256=CONTRACT_SHA,
            code_commit=COMMIT,
            progress_every=0,
        )
    os.chmod(container, 0o600)
    with container.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        original = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([original[0] ^ 1]))
    with pytest.raises(ResearchSnapshotError, match="container SHA-256 mismatch"):
        verify_snapshot(container, receipt_path=receipt, progress_every=0)


def test_receipt_change_is_rejected(tmp_path: Path) -> None:
    plan = _plan(_fixture_root(tmp_path))
    container = tmp_path / "snapshot.sqlite"
    receipt = tmp_path / "receipt.json"
    capture_snapshot(
        plan,
        output=container,
        receipt_path=receipt,
        contract_sha256=CONTRACT_SHA,
        code_commit=COMMIT,
        progress_every=0,
    )
    os.chmod(receipt, 0o600)
    value = json.loads(receipt.read_text(encoding="utf-8"))
    value["file_count"] += 1
    receipt.write_bytes(canonical_json(value) + b"\n")
    with pytest.raises(ResearchSnapshotError, match="file_count"):
        verify_snapshot(container, receipt_path=receipt, progress_every=0)


def test_capture_resumes_only_the_same_planned_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(_fixture_root(tmp_path))
    container = tmp_path / "snapshot.sqlite"
    receipt = tmp_path / "receipt.json"
    real_capture_one = snapshot_module._capture_one
    calls = 0

    def interrupt_after_two(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("registered interruption")
        return real_capture_one(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_capture_one", interrupt_after_two)
    with pytest.raises(RuntimeError, match="registered interruption"):
        capture_snapshot(
            plan,
            output=container,
            receipt_path=receipt,
            contract_sha256=CONTRACT_SHA,
            code_commit=COMMIT,
            batch_size=1,
            progress_every=0,
        )
    working = container.with_name(f".{container.name}.working")
    assert working.exists()
    monkeypatch.setattr(snapshot_module, "_capture_one", real_capture_one)
    result = capture_snapshot(
        plan,
        output=container,
        receipt_path=receipt,
        contract_sha256=CONTRACT_SHA,
        code_commit=COMMIT,
        batch_size=1,
        progress_every=0,
    )
    assert result["file_count"] == len(plan.files)
    assert not working.exists()


def test_capture_refuses_source_change_after_inventory(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    plan = _plan(root)
    changed = root / "klines_1h" / "date=2026-01-01" / "symbol=BTCUSDT" / "part.parquet"
    changed.write_bytes(b"changed-after-plan")
    with pytest.raises(ResearchSnapshotError, match="metadata changed after inventory"):
        capture_snapshot(
            plan,
            output=tmp_path / "snapshot.sqlite",
            receipt_path=tmp_path / "receipt.json",
            contract_sha256=CONTRACT_SHA,
            code_commit=COMMIT,
            progress_every=0,
        )


@pytest.mark.skipif(os.name == "nt", reason="ordinary Windows users cannot always create symlinks")
def test_plan_rejects_symlinked_dataset_content(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    target = root / "outside.parquet"
    target.write_bytes(b"outside")
    link = root / "klines_1h" / "date=2026-01-01" / "symbol=LINK"
    link.symlink_to(target.parent, target_is_directory=True)
    with pytest.raises(ResearchSnapshotError, match="symlink or reparse"):
        _plan(root)
