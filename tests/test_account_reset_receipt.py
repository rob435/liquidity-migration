from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration import account_reset_receipt as reset_receipts
from liquidity_migration.account_reset_receipt import (
    DEMO_BOUNDARY,
    KIND,
    MANAGED_UNITS,
    PAPER_BOUNDARY,
    build_account_reset_receipt,
    load_account_reset_receipt,
    validate_account_reset_receipt_output,
    write_account_reset_receipt,
)


def _git_repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "candidate"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repo"
    candidate = _git_repository(repository)
    roots: dict[str, dict[str, Path]] = {"demo": {}, "paper": {}}
    relative: list[str] = []
    for environment in ("demo", "paper"):
        for kind in ("account", "inbox", "capture"):
            root = repository / "data" / f"{environment}-{kind}"
            root.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            roots[environment][kind] = root
            relative.append(root.relative_to(repository).as_posix())
    active_before = [MANAGED_UNITS[-2], MANAGED_UNITS[-1]]
    manifest = (
        "\n".join(
            [
                "ledger_reset_utc=20260714T120000Z",
                f"git_head={candidate}",
                "sleeves=long continuous retire-shared-compat",
                "include_reports=0",
                "include_caches=0",
                "leave_stopped=1",
                "env_file=/etc/liquidity-migration/bybit-demo.env",
                "account_env_file=/etc/liquidity-migration/account-execution.env",
                "paper_account_env_file=/etc/liquidity-migration/account-paper-execution.env",
                "demo_account_lease_path=/run/liquidity-migration/demo-account.lock",
                f"demo_boundary={DEMO_BOUNDARY}",
                f"paper_boundary={PAPER_BOUNDARY}",
                f"active_before={' '.join(active_before)}",
                *[f"account_epoch_target={value}" for value in relative],
            ]
        ).encode()
        + b"\n"
    )
    archive = tmp_path / "reset.tar.gz"
    with tarfile.open(archive, mode="w:gz") as handle:
        member = tarfile.TarInfo("ledger-reset-manifest.txt")
        member.size = len(manifest)
        member.mode = 0o600
        handle.addfile(member, io.BytesIO(manifest))
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = tmp_path / "reset.tar.gz.sha256"
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)
    return {
        "repository_root": repository,
        "candidate_commit": candidate,
        "started_ts_ns": 1,
        "finished_ts_ns": 2,
        "sleeves": ["long", "continuous", "retire-shared-compat"],
        "include_reports": False,
        "include_caches": False,
        "leave_stopped": True,
        "account_epoch_roots": roots,
        "managed_units": list(MANAGED_UNITS),
        "active_before": active_before,
        "inactive_after": list(MANAGED_UNITS),
        "archive_path": archive,
        "sha256_sidecar_path": sidecar,
    }


def test_build_write_load_reopens_archive_and_fresh_roots(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    payload = build_account_reset_receipt(**arguments)
    output = tmp_path / "reset-receipt.json"
    write_account_reset_receipt(output, payload)

    assert (output.stat().st_mode & 0o777) == 0o600
    assert payload["kind"] == KIND
    assert payload["reset"]["fresh_roots_verified"] is True
    assert payload["services"]["inactive_after"] == list(MANAGED_UNITS)
    assert (
        load_account_reset_receipt(
            output,
            expected_candidate_commit=arguments["candidate_commit"],
            expected_roots=arguments["account_epoch_roots"],
            require_leave_stopped=True,
            require_fresh_roots=True,
        )
        == payload
    )

    with pytest.raises(FileExistsError):
        write_account_reset_receipt(output, payload)


def test_load_rejects_archive_mutation(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    output = tmp_path / "reset-receipt.json"
    write_account_reset_receipt(output, build_account_reset_receipt(**arguments))
    archive = Path(arguments["archive_path"])
    archive.write_bytes(archive.read_bytes() + b"changed")
    archive.chmod(0o600)

    with pytest.raises(ValueError, match="sidecar|changed"):
        load_account_reset_receipt(output)


def test_failed_final_reopen_removes_success_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = _fixture(tmp_path)
    output = tmp_path / "reset-receipt.json"
    payload = build_account_reset_receipt(**arguments)

    def fail_reopen(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("synthetic final reopen failure")

    monkeypatch.setattr(reset_receipts, "load_account_reset_receipt", fail_reopen)
    with pytest.raises(RuntimeError, match="synthetic final reopen failure"):
        write_account_reset_receipt(output, payload)

    assert not output.exists(), "a failed reset-receipt transaction cannot leave a pass"


def test_fresh_root_check_is_explicit_and_time_scoped(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    output = tmp_path / "reset-receipt.json"
    payload = build_account_reset_receipt(**arguments)
    write_account_reset_receipt(output, payload)
    demo_account = Path(arguments["account_epoch_roots"]["demo"]["account"])
    (demo_account / "later-natural-event.json").write_text("later\n", encoding="utf-8")

    assert load_account_reset_receipt(output) == payload
    with pytest.raises(ValueError, match="not empty"):
        load_account_reset_receipt(output, require_fresh_roots=True)


def test_build_rejects_sidecar_or_service_claim_mismatch(tmp_path: Path) -> None:
    arguments = _fixture(tmp_path)
    sidecar = Path(arguments["sha256_sidecar_path"])
    sidecar.write_text(f"{'0' * 64}  reset.tar.gz\n", encoding="utf-8")
    sidecar.chmod(0o600)
    with pytest.raises(ValueError, match="sidecar"):
        build_account_reset_receipt(**arguments)

    arguments = _fixture(tmp_path / "other")
    arguments["inactive_after"] = list(MANAGED_UNITS[:-1])
    with pytest.raises(ValueError, match="every managed unit inactive"):
        build_account_reset_receipt(**arguments)


def test_preflight_rejects_existing_relative_or_nested_output(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    existing = tmp_path / "receipt.json"
    existing.write_text(json.dumps({}), encoding="utf-8")

    with pytest.raises(ValueError, match="absolute"):
        validate_account_reset_receipt_output(Path("relative.json"))
    with pytest.raises(FileExistsError):
        validate_account_reset_receipt_output(existing)
    with pytest.raises(ValueError, match="inside a reset root"):
        validate_account_reset_receipt_output(root / "receipt.json", forbidden_roots=[root])
