from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from liquidity_migration import natural_cutover_freeze_manifest as freeze
from liquidity_migration import account_reset_receipt as reset_receipt_module


CANDIDATE = "a" * 40
ORIGIN_MAIN = "b" * 40
T0_NS = 2_000 * freeze.HOUR_NS
T1_NS = T0_NS + freeze.WINDOW_NS


def _private(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _semantic_artifacts(
    *,
    identities: Mapping[str, freeze._FileIdentity],
    now_ns: int,
    **_: Any,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for name in (
        "local_suite",
        "linux_ci",
        "clock",
        "candidate_universe",
        "demo_rules",
        "rule_coverage",
        "calibration",
        "archive_map",
        "baseline_config",
        "stress_config",
        "paper",
        "demo",
    ):
        source = {
            "paper": "paper_owner_first",
            "demo": "demo_owner_first",
        }.get(name, name)
        output[name] = {
            "path": identities[source].path,
            "file_sha256": identities[source].sha256,
            "artifact_sha256": hashlib.sha256(name.encode()).hexdigest(),
        }
        if name == "clock":
            output[name]["observed_ts_ns"] = now_ns
        elif name == "paper":
            output[name]["reviewed_ts_ns"] = now_ns - 2
        elif name == "demo":
            output[name]["reviewed_ts_ns"] = now_ns - 1
    return output


def _reset_archive(tmp_path: Path, *, roots: list[str]) -> tuple[Path, Path]:
    manifest = "\n".join(
        [
            "ledger_reset_utc=20260714T120000Z",
            f"git_head={CANDIDATE}",
            "sleeves=long continuous retire-shared-compat",
            "include_reports=0",
            "include_caches=0",
            "leave_stopped=1",
            "env_file=/etc/demo.env",
            "account_env_file=/etc/account.env",
            "paper_account_env_file=/etc/paper.env",
            "demo_account_lease_path=/run/demo.lock",
            "demo_boundary=venue_verified_flat_positions_0_open_orders_0",
            "paper_boundary=archived_deterministic_epoch_not_carried_forward",
            "active_before=",
            *[f"account_epoch_target={root}" for root in roots],
        ]
    ).encode() + b"\n"
    archive = tmp_path / "reset.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        member = tarfile.TarInfo("ledger-reset-manifest.txt")
        member.size = len(manifest)
        member.mode = 0o600
        handle.addfile(member, io.BytesIO(manifest))
    archive.chmod(0o600)
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = _private(
        tmp_path / "reset.tar.gz.sha256",
        f"{archive_sha}  {archive.name}\n".encode(),
    )
    return archive, sidecar


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reset_archive_mismatch: bool = False,
    reset_loader_calls: list[dict[str, Any]] | None = None,
    reset_finished_ns: int | None = None,
) -> dict[str, Any]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "data").mkdir()
    roots: dict[str, dict[str, Path]] = {}
    relative_roots: list[str] = []
    for environment in freeze.ROOT_ENVIRONMENTS:
        roots[environment] = {}
        for kind in freeze.ROOT_KINDS:
            root = repository / "data" / f"{environment}-{kind}"
            root.mkdir(mode=0o700)
            roots[environment][kind] = root
            relative_roots.append(root.relative_to(repository).as_posix())
    archive, sidecar = _reset_archive(tmp_path, roots=relative_roots)
    sources: dict[str, Path] = {}
    for name in (
        "local_suite",
        "linux_ci",
        "clock",
        "candidate_universe",
        "demo_rules",
        "rule_coverage",
        "calibration",
        "archive_map",
        "baseline_config",
        "stress_config",
        "paper_owner_first",
        "demo_owner_first",
        "reset_receipt",
        "seed",
        "route-demo",
        "route-paper",
        "risk-demo",
        "risk-paper",
    ):
        sources[name] = _private(tmp_path / f"{name}.json", (name + "\n").encode())
    monkeypatch.setattr(
        freeze,
        "_repository_binding",
        lambda **_: (repository, CANDIDATE, ORIGIN_MAIN),
    )
    monkeypatch.setattr(freeze, "_semantic_artifacts", _semantic_artifacts)
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    nested_relative = {
        environment: {
            kind: roots[environment][kind].relative_to(repository).as_posix()
            for kind in freeze.ROOT_KINDS
        }
        for environment in freeze.ROOT_ENVIRONMENTS
    }

    def load_reset(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if reset_loader_calls is not None:
            reset_loader_calls.append(dict(kwargs))
        return {
            "artifact_sha256": hashlib.sha256(b"reset receipt").hexdigest(),
            "started_ts_ns": T0_NS - freeze.HOUR_NS - 4,
            "finished_ts_ns": (
                T0_NS - freeze.HOUR_NS - 3
                if reset_finished_ns is None
                else reset_finished_ns
            ),
            "reset": {
                "sleeves": ["long", "continuous", "retire-shared-compat"],
                "boundaries": {
                    "demo": reset_receipt_module.DEMO_BOUNDARY,
                    "paper": reset_receipt_module.PAPER_BOUNDARY,
                },
                "account_epoch_relative_roots": nested_relative,
                "fresh_roots_verified": True,
            },
            "archive": {
                "file": {
                    "path": str(archive),
                    "sha256": "0" * 64 if reset_archive_mismatch else archive_sha,
                    "size_bytes": archive.stat().st_size,
                },
                "sha256_sidecar": {
                    "path": str(sidecar),
                    "sha256": sidecar_sha,
                },
                "embedded_manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
                "embedded_manifest": {"ledger_reset_utc": "20260714T120000Z"},
            },
            "services": {"inactive_after": list(reset_receipt_module.MANAGED_UNITS)},
        }

    monkeypatch.setattr(reset_receipt_module, "load_account_reset_receipt", load_reset)
    return {
        "repository_root": repository,
        "candidate_commit": CANDIDATE,
        "origin_main_commit": ORIGIN_MAIN,
        "t0_ns": T0_NS,
        "t1_ns": T1_NS,
        "account_ids": dict(freeze.EXPECTED_ACCOUNT_IDS),
        "roots": roots,
        "local_suite_path": sources["local_suite"],
        "linux_ci_path": sources["linux_ci"],
        "clock_offset_path": sources["clock"],
        "candidate_universe_path": sources["candidate_universe"],
        "demo_rules_path": sources["demo_rules"],
        "rule_coverage_path": sources["rule_coverage"],
        "calibration_path": sources["calibration"],
        "archive_map_path": sources["archive_map"],
        "baseline_config_path": sources["baseline_config"],
        "stress_config_path": sources["stress_config"],
        "reset_archive_path": archive,
        "reset_sha256_path": sidecar,
        "reset_receipt_path": sources["reset_receipt"],
        "paper_owner_first_path": sources["paper_owner_first"],
        "demo_owner_first_path": sources["demo_owner_first"],
        "route_paths": {
            "demo": sources["route-demo"],
            "paper": sources["route-paper"],
        },
        "risk_policy_paths": {
            "demo": sources["risk-demo"],
            "paper": sources["risk-paper"],
        },
        "seed_path": sources["seed"],
        "created_ts_ns": T0_NS - freeze.HOUR_NS,
        "validation_now_ns": T0_NS - freeze.HOUR_NS,
    }


def _runtime_source_fixture(tmp_path: Path) -> tuple[
    dict[str, Path],
    dict[str, bytes],
    dict[str, dict[str, str]],
]:
    roots = {
        environment: {
            kind: str(tmp_path / f"{environment}-{kind}")
            for kind in freeze.ROOT_KINDS
        }
        for environment in freeze.ROOT_ENVIRONMENTS
    }
    paths = {
        "candidate_universe": tmp_path / "candidate.json",
        "demo_rules": tmp_path / "rules.json",
        "calibration": tmp_path / "calibration.json",
        "route:demo": tmp_path / "demo.env",
        "route:paper": tmp_path / "paper.env",
        "risk:demo": tmp_path / "demo-risk.json",
        "risk:paper": tmp_path / "paper-risk.json",
    }
    risk = json.dumps(
        {
            "max_component_gross_notional_usdt": 100.0,
            "max_account_gross_notional_usdt": 200.0,
            "max_symbol_notional_usdt": 100.0,
            "max_initial_margin_usdt": 100.0,
            "max_leverage": 2.0,
            "quantity_tolerance": 1e-12,
        }
    ).encode()
    common = (
        f"ACCOUNT_SYMBOLS_FILE={paths['candidate_universe']}\n"
        f"ACCOUNT_DEMO_RULES_FILE={paths['demo_rules']}\n"
        "MAX_DEMO_RULE_AGE_HOURS=168\n"
    )
    data = {
        "risk:demo": risk,
        "risk:paper": risk,
        "route:demo": (
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
            f"ACCOUNT_EXECUTION_ROOT={roots['demo']['account']}\n"
            f"ACCOUNT_INTENT_INBOX_ROOT={roots['demo']['inbox']}\n"
            f"ACCOUNT_CAPTURE_ROOT={roots['demo']['capture']}\n"
            "ACCOUNT_RAW_MARKET_PERSISTENCE=1\n"
            f"ACCOUNT_RISK_POLICY_FILE={paths['risk:demo']}\n"
            "DISASTER_STOP_FRACTION=0.1\n"
            f"{common}"
        ).encode(),
        "route:paper": (
            "ACCOUNT_PAPER_KERNEL_REQUIRED=1\n"
            f"ACCOUNT_EXECUTION_ROOT={roots['paper']['account']}\n"
            f"ACCOUNT_INTENT_INBOX_ROOT={roots['paper']['inbox']}\n"
            f"ACCOUNT_PAPER_CAPTURE_ROOT={roots['paper']['capture']}\n"
            "ACCOUNT_RAW_MARKET_PERSISTENCE=1\n"
            f"ACCOUNT_RISK_POLICY_FILE={paths['risk:paper']}\n"
            f"ACCOUNT_TWIN_CALIBRATION_FILE={paths['calibration']}\n"
            "ACCOUNT_TWIN_LATENCY_QUANTILE=p50\n"
            "ACCOUNT_TWIN_SLIPPAGE_QUANTILE=p50\n"
            "PAPER_EQUITY_USDT=10000\n"
            f"{common}"
        ).encode(),
    }
    return paths, data, roots


def test_runtime_sources_bind_routes_risk_and_seed(tmp_path: Path) -> None:
    paths, data, roots = _runtime_source_fixture(tmp_path)
    freeze._validate_runtime_sources(
        paths=paths,
        data=data,
        roots=roots,
        candidate_symbols=["BTCUSDT", "ETHUSDT", "BUSDT", "XUSDT"],
        seed_symbols={"BTCUSDT", "ETHUSDT", "BUSDT"},
    )

    data["route:paper"] = data["route:paper"].replace(b"p50", b"p95", 1)
    with pytest.raises(ValueError, match="LATENCY_QUANTILE"):
        freeze._validate_runtime_sources(
            paths=paths,
            data=data,
            roots=roots,
            candidate_symbols=["BTCUSDT", "ETHUSDT", "BUSDT"],
            seed_symbols={"BTCUSDT", "ETHUSDT", "BUSDT"},
        )


def test_runtime_sources_reject_weakened_or_incomplete_contract(tmp_path: Path) -> None:
    paths, data, roots = _runtime_source_fixture(tmp_path)
    for real_money in (b"1", b"maybe"):
        data["route:demo"] += b"REAL_MONEY=" + real_money + b"\n"
        with pytest.raises(ValueError, match="REAL_MONEY"):
            freeze._validate_runtime_sources(
                paths=paths,
                data=data,
                roots=roots,
                candidate_symbols=["BTCUSDT", "ETHUSDT", "BUSDT"],
                seed_symbols={"BTCUSDT", "ETHUSDT", "BUSDT"},
            )
        data["route:demo"] = data["route:demo"].replace(
            b"REAL_MONEY=" + real_money + b"\n", b""
        )

    for key, value, message in (
        (b"MAX_DEMO_RULE_AGE_HOURS", b"nan", "rule freshness"),
        (b"ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS", b"nan", "market warmup"),
        (b"ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS", b"31", "market warmup"),
    ):
        original = data["route:demo"]
        if key + b"=" in original:
            data["route:demo"] = original.replace(
                key + b"=168\n", key + b"=" + value + b"\n"
            )
        else:
            data["route:demo"] = original + key + b"=" + value + b"\n"
        with pytest.raises(ValueError, match=message):
            freeze._validate_runtime_sources(
                paths=paths,
                data=data,
                roots=roots,
                candidate_symbols=["BTCUSDT", "ETHUSDT", "BUSDT"],
                seed_symbols={"BTCUSDT", "ETHUSDT", "BUSDT"},
            )
        data["route:demo"] = original

    with pytest.raises(ValueError, match="exact registered V7 symbol set"):
        freeze._validate_runtime_sources(
            paths=paths,
            data=data,
            roots=roots,
            candidate_symbols=["BTCUSDT", "ETHUSDT", "BUSDT"],
            seed_symbols={"BTCUSDT"},
        )


def test_freeze_requires_exact_demo_and_paper_source_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _fixture(tmp_path, monkeypatch)
    arguments["risk_policy_paths"] = {
        "demo": arguments["risk_policy_paths"]["demo"]
    }
    with pytest.raises(ValueError, match="exact demo and paper"):
        freeze.build_natural_cutover_freeze_manifest(**arguments)


def test_build_write_load_reopens_every_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _fixture(tmp_path, monkeypatch)
    payload = freeze.build_natural_cutover_freeze_manifest(**arguments)
    output = tmp_path / "freeze.json"
    freeze.write_natural_cutover_freeze_manifest(output, payload)

    assert stat_mode(output) == 0o600
    assert freeze.load_natural_cutover_freeze_manifest(output) == payload
    assert payload["gates"] == {
        "pre_window_freeze_passed": True,
        "execution_authorization": "not_granted",
    }
    assert payload["reset"]["account_epoch_roots"] == [
        f"data/{environment}-{kind}"
        for environment in freeze.ROOT_ENVIRONMENTS
        for kind in freeze.ROOT_KINDS
    ]
    assert payload["reset"]["receipt"]["path"] == str(
        arguments["reset_receipt_path"].resolve()
    )
    assert payload["reset"]["fresh_roots_verified_at_reset"] is True
    assert payload["clock"]["initial_to_t0_ns"] == freeze.HOUR_NS
    assert payload["clock"]["max_initial_to_t0_hours"] == 6.0
    assert "stdout_log" not in payload["reset"]


def test_build_rejects_initial_clock_more_than_six_hours_before_t0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _fixture(tmp_path, monkeypatch)
    original = freeze._semantic_artifacts

    def stale_clock(**kwargs: Any) -> dict[str, dict[str, Any]]:
        values = original(**kwargs)
        values["clock"]["observed_ts_ns"] = T0_NS - 7 * freeze.HOUR_NS
        return values

    monkeypatch.setattr(freeze, "_semantic_artifacts", stale_clock)
    with pytest.raises(ValueError, match="no more than six hours before T0"):
        freeze.build_natural_cutover_freeze_manifest(**arguments)


def test_load_rejects_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _fixture(tmp_path, monkeypatch)
    payload = freeze.build_natural_cutover_freeze_manifest(**arguments)
    output = tmp_path / "freeze.json"
    freeze.write_natural_cutover_freeze_manifest(output, payload)
    seed = Path(arguments["seed_path"])
    seed.write_text("changed\n", encoding="utf-8")
    seed.chmod(0o600)

    with pytest.raises(ValueError, match="changed after creation"):
        freeze.load_natural_cutover_freeze_manifest(output)


def test_build_rejects_reset_receipt_bound_to_another_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _fixture(tmp_path, monkeypatch, reset_archive_mismatch=True)
    with pytest.raises(ValueError, match="another archive bundle"):
        freeze.build_natural_cutover_freeze_manifest(**arguments)


def test_freeze_reopens_reset_receipt_without_requiring_post_owner_emptiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    arguments = _fixture(tmp_path, monkeypatch, reset_loader_calls=calls)
    freeze.build_natural_cutover_freeze_manifest(**arguments)

    assert len(calls) == 1
    assert calls[0]["expected_candidate_commit"] == CANDIDATE
    assert calls[0]["require_leave_stopped"] is True
    assert calls[0]["require_fresh_roots"] is False
    assert calls[0]["snapshot"].data == Path(
        arguments["reset_receipt_path"]
    ).read_bytes()
    assert calls[0]["archive_snapshot"].data == Path(
        arguments["reset_archive_path"]
    ).read_bytes()
    assert calls[0]["sidecar_snapshot"].data == Path(
        arguments["reset_sha256_path"]
    ).read_bytes()
    assert calls[0]["expected_roots"] == {
        environment: {
            kind: str(arguments["roots"][environment][kind].resolve())
            for kind in freeze.ROOT_KINDS
        }
        for environment in freeze.ROOT_ENVIRONMENTS
    }


def test_freeze_rejects_owner_evidence_created_before_reset_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _fixture(
        tmp_path,
        monkeypatch,
        reset_finished_ns=T0_NS - freeze.HOUR_NS,
    )
    with pytest.raises(ValueError, match="out of order"):
        freeze.build_natural_cutover_freeze_manifest(**arguments)


def test_build_rejects_non_120_hour_or_non_hour_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _fixture(tmp_path, monkeypatch)
    arguments["t1_ns"] = T1_NS + 1
    with pytest.raises(ValueError, match="exact 120-hour"):
        freeze.build_natural_cutover_freeze_manifest(**arguments)


def test_write_is_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = _fixture(tmp_path, monkeypatch)
    payload = freeze.build_natural_cutover_freeze_manifest(**arguments)
    output = tmp_path / "freeze.json"
    freeze.write_natural_cutover_freeze_manifest(output, payload)

    with pytest.raises(FileExistsError):
        freeze.write_natural_cutover_freeze_manifest(output, payload)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def _local_suite_payload(tmp_path: Path, *, pytest_exit: int = 0) -> tuple[dict[str, Any], Path]:
    repository = tmp_path / "local-repo"
    (repository / ".git" / "tmp").mkdir(parents=True)
    first = b"ruff passed\n"
    second = b"pytest passed\n"
    log = _private(tmp_path / "local-suite.log", first + second)
    identity, _ = freeze._read_identity(
        log, label="local_suite_log", require_private=True, include_data=False
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": freeze.LOCAL_SUITE_KIND,
        "validator": freeze.LOCAL_SUITE_VALIDATOR,
        "status": "passed" if pytest_exit == 0 else "failed",
        "candidate_commit": CANDIDATE,
        "started_ts_ns": 10,
        "finished_ts_ns": 40,
        "repository": {
            "root": str(repository),
            "origin_url": "https://github.com/example/project.git",
            "head_before": CANDIDATE,
            "head_after": CANDIDATE,
            "clean_before": True,
            "clean_after": True,
        },
        "python_executable": str(Path(sys.executable).resolve()),
        "commands": [
            {
                "command_id": "ruff",
                "argv": [
                    str(Path(sys.executable).resolve()),
                    "-m",
                    "ruff",
                    "check",
                    "liquidity_migration",
                    "tests",
                    "scripts",
                ],
                "cwd": str(repository),
                "started_ts_ns": 11,
                "finished_ts_ns": 20,
                "exit_code": 0,
                "log_start_byte": 0,
                "log_end_byte": len(first),
                "log_sha256": hashlib.sha256(first).hexdigest(),
            },
            {
                "command_id": "pytest",
                "argv": [
                    str(Path(sys.executable).resolve()),
                    "-m",
                    "pytest",
                    "-q",
                    "--basetemp",
                    str(
                        log.parent
                        / f"pytest-natural-freeze-{CANDIDATE[:12]}-10-20"
                    ),
                ],
                "cwd": str(repository),
                "started_ts_ns": 21,
                "finished_ts_ns": 39,
                "exit_code": pytest_exit,
                "log_start_byte": len(first),
                "log_end_byte": len(first) + len(second),
                "log_sha256": hashlib.sha256(second).hexdigest(),
            },
        ],
        "log_source": identity.to_dict(),
        "gate_passed": pytest_exit == 0,
        "limitations": [
            "local_process_receipt_is_not_a_remote_attestation",
            "linux_ci_is_an_independent_required_gate",
        ],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = freeze._self_hash(payload)
    return payload, log


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _github_sources(
    workflow_bytes: bytes, *, conclusion: str = "success", mutating: bool = False
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    run_id = 123456
    repository = "example/project"
    run = {
        "id": run_id,
        "run_attempt": 2,
        "name": freeze.WORKFLOW_NAME,
        "path": freeze.WORKFLOW_PATH,
        "head_sha": CANDIDATE,
        "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": conclusion,
        "repository": {"full_name": repository},
    }
    steps = [
        {"name": name, "conclusion": conclusion}
        for name in freeze.CI_REQUIRED_STEPS
    ]
    jobs: list[dict[str, Any]] = [
        {
            "id": 444,
            "run_attempt": 2,
            "name": freeze.CI_JOB_NAME,
            "head_sha": CANDIDATE,
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}/job/444",
            "status": "completed",
            "conclusion": conclusion,
            "steps": steps,
        }
    ]
    jobs.append(
        {
            "id": 445,
            "run_attempt": 2,
            "name": "vps",
            "head_sha": CANDIDATE,
            "status": "completed",
            "conclusion": "success",
            "steps": [
                *[
                    {
                        "name": name,
                        "conclusion": (
                            "success"
                            if mutating and name == "Checked deploy"
                            else "skipped"
                        ),
                    }
                    for name in sorted(freeze.MUTATING_WORKFLOW_STEPS)
                ],
                *[
                    {"name": name, "conclusion": "skipped"}
                    for name in sorted(freeze.CANDIDATE_CI_EXTERNAL_STEPS)
                ],
                {"name": freeze.CANDIDATE_CI_STEP, "conclusion": "success"},
            ],
        }
    )
    workflow = {
        "type": "file",
        "path": freeze.WORKFLOW_PATH,
        "encoding": "base64",
        "content": base64.b64encode(workflow_bytes).decode(),
        "sha": _git_blob_sha(workflow_bytes),
    }
    return run, {"total_count": len(jobs), "jobs": jobs}, workflow


def _linux_ci_provenance(
    tmp_path: Path, *, conclusion: str = "success", mutating: bool = False
) -> tuple[Path, Path]:
    repository = tmp_path / "ci-repo"
    workflow_path = repository / freeze.WORKFLOW_PATH
    workflow_path.parent.mkdir(parents=True)
    workflow_bytes = b"name: VPS Deploy\n"
    workflow_path.write_bytes(workflow_bytes)
    workflow_path.chmod(0o644)
    run, jobs, workflow = _github_sources(
        workflow_bytes, conclusion=conclusion, mutating=mutating
    )
    provenance = freeze._github_provenance_payload(
        repository_full_name="example/project",
        candidate_commit=CANDIDATE,
        run_id=123456,
        fetched_ts_ns=50,
        run=run,
        jobs=jobs,
        workflow_content=workflow,
    )
    provenance_path = tmp_path / "github-provenance.json"
    freeze._atomic_create(provenance_path, provenance)
    return repository, provenance_path


def test_local_suite_receipt_reopens_exact_command_log(tmp_path: Path) -> None:
    payload, log = _local_suite_payload(tmp_path)
    output = freeze.write_local_suite_receipt(tmp_path / "local-suite.json", payload)

    loaded = freeze.load_local_suite_receipt(
        output, expected_candidate_commit=CANDIDATE
    )
    assert loaded["gate_passed"] is True
    assert stat_mode(output) == 0o600

    log.write_bytes(log.read_bytes() + b"post-receipt mutation\n")
    log.chmod(0o600)
    with pytest.raises(ValueError, match="changed after receipt creation"):
        freeze.load_local_suite_receipt(output)


def test_local_suite_failed_command_cannot_satisfy_freeze_gate(tmp_path: Path) -> None:
    payload, _ = _local_suite_payload(tmp_path, pytest_exit=1)
    output = freeze.write_local_suite_receipt(tmp_path / "failed-local-suite.json", payload)

    assert freeze.load_local_suite_receipt(output, require_passed=False)["status"] == "failed"
    with pytest.raises(ValueError, match="gate has not passed"):
        freeze.load_local_suite_receipt(output)


def test_local_suite_rejects_repository_local_pytest_basetemp(tmp_path: Path) -> None:
    payload, _ = _local_suite_payload(tmp_path)
    repository = Path(payload["repository"]["root"])
    payload["commands"][1]["argv"][-1] = str(
        repository
        / ".git"
        / "tmp"
        / f"pytest-natural-freeze-{CANDIDATE[:12]}-10-20"
    )
    payload["artifact_sha256"] = freeze._self_hash(payload)

    with pytest.raises(ValueError, match="beside the external command log"):
        freeze.verify_local_suite_receipt(payload)


def test_python_executable_preserves_venv_launcher_symlink(tmp_path: Path) -> None:
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(Path(sys.executable).resolve())

    observed = freeze._python_executable(launcher)

    assert observed == launcher.absolute()
    assert observed.is_symlink()


def test_local_suite_producer_executes_registered_commands_and_hashes_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "producer-repo"
    (repository / ".git").mkdir(parents=True)
    evidence = tmp_path / "producer-evidence"
    evidence.mkdir()
    monkeypatch.setattr(
        freeze,
        "_candidate_checkout",
        lambda *_args, **_kwargs: (
            repository,
            CANDIDATE,
            "https://github.com/example/project.git",
        ),
    )
    monkeypatch.setattr(
        freeze,
        "_git",
        lambda _root, *arguments: CANDIDATE if arguments[:2] == ("rev-parse", "HEAD") else "",
    )
    observed: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        observed.append(argv)
        os.write(kwargs["stdout"], (argv[2] + " passed\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(freeze.subprocess, "run", fake_run)
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(Path(sys.executable).resolve())
    output, receipt = freeze.run_local_suite(
        repository_root=repository,
        candidate_commit=CANDIDATE,
        python_executable=launcher,
        log_path=evidence / "suite.log",
        output_path=evidence / "suite.json",
    )

    assert [command[2] for command in observed] == ["ruff", "pytest"]
    assert all(command[0] == str(launcher.absolute()) for command in observed)
    pytest_basetemp = Path(observed[1][-1])
    assert pytest_basetemp.parent == evidence
    assert pytest_basetemp.name.startswith(
        f"pytest-natural-freeze-{CANDIDATE[:12]}-"
    )
    assert not pytest_basetemp.is_relative_to(repository)
    assert receipt["python_executable"] == str(launcher.absolute())
    assert receipt["gate_passed"] is True
    assert freeze.load_local_suite_receipt(output) == receipt


def test_linux_ci_receipt_reopens_github_provenance_and_workflow(tmp_path: Path) -> None:
    repository, provenance = _linux_ci_provenance(tmp_path)
    receipt = freeze.build_linux_ci_receipt(
        provenance_path=provenance,
        candidate_commit=CANDIDATE,
        repository_root=repository,
    )
    output = freeze.write_linux_ci_receipt(
        tmp_path / "linux-ci.json", receipt, repository_root=repository
    )

    loaded = freeze.load_linux_ci_receipt(
        output,
        repository_root=repository,
        expected_candidate_commit=CANDIDATE,
    )
    assert loaded["run"] == {
        "id": 123456,
        "attempt": 2,
        "url": "https://github.com/example/project/actions/runs/123456",
        "event": "workflow_dispatch",
        "head_sha": CANDIDATE,
        "status": "completed",
        "conclusion": "success",
    }
    assert loaded["workflow"]["path"] == freeze.WORKFLOW_PATH
    assert loaded["ci_job"]["required_steps"] == {
        name: "success" for name in freeze.CI_REQUIRED_STEPS
    }
    assert loaded["deployment_safety"]["all_external_steps_skipped"] is True
    assert loaded["deployment_safety"]["candidate_ci_only_step"] == {
        "name": freeze.CANDIDATE_CI_STEP,
        "conclusion": "success",
    }

    provenance.write_bytes(provenance.read_bytes() + b"\n")
    provenance.chmod(0o600)
    with pytest.raises(ValueError, match="changed after receipt creation"):
        freeze.load_linux_ci_receipt(output, repository_root=repository)


def test_linux_ci_failed_or_mutating_run_cannot_pass(tmp_path: Path) -> None:
    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    repository, provenance = _linux_ci_provenance(
        failed_root, conclusion="failure"
    )
    receipt = freeze.build_linux_ci_receipt(
        provenance_path=provenance,
        candidate_commit=CANDIDATE,
        repository_root=repository,
    )
    assert receipt["status"] == "failed"
    with pytest.raises(ValueError, match="gate has not passed"):
        freeze.verify_linux_ci_receipt(receipt, repository_root=repository)

    mutating_root = tmp_path / "mutating"
    mutating_root.mkdir()
    repository, provenance = _linux_ci_provenance(mutating_root, mutating=True)
    with pytest.raises(ValueError, match="executed a mutating deployment step"):
        freeze.build_linux_ci_receipt(
            provenance_path=provenance,
            candidate_commit=CANDIDATE,
            repository_root=repository,
        )


def test_linux_ci_rejects_a_dispatch_that_touched_the_vps(tmp_path: Path) -> None:
    repository, provenance = _linux_ci_provenance(tmp_path)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    vps = next(job for job in payload["jobs"]["jobs"] if job["name"] == "vps")
    read_only = next(
        step for step in vps["steps"] if step["name"] == freeze.READ_ONLY_VERIFY_STEP
    )
    read_only["conclusion"] = "success"
    payload["artifact_sha256"] = freeze._self_hash(payload)
    provenance.write_bytes(freeze.canonical_json(payload) + b"\n")
    provenance.chmod(0o600)

    with pytest.raises(ValueError, match="CI-only path"):
        freeze.build_linux_ci_receipt(
            provenance_path=provenance,
            candidate_commit=CANDIDATE,
            repository_root=repository,
        )


def test_linux_ci_rejects_another_head_even_with_a_valid_source_hash(
    tmp_path: Path,
) -> None:
    repository, provenance = _linux_ci_provenance(tmp_path)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["run"]["head_sha"] = "c" * 40
    payload["artifact_sha256"] = freeze._self_hash(payload)
    provenance.write_bytes(freeze.canonical_json(payload) + b"\n")
    provenance.chmod(0o600)

    with pytest.raises(ValueError, match="exact candidate workflow identity"):
        freeze.build_linux_ci_receipt(
            provenance_path=provenance,
            candidate_commit=CANDIDATE,
            repository_root=repository,
        )


def test_linux_ci_producer_fetches_exact_api_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "fetch-repo"
    workflow_path = repository / freeze.WORKFLOW_PATH
    workflow_path.parent.mkdir(parents=True)
    workflow_bytes = b"name: VPS Deploy\n"
    workflow_path.write_bytes(workflow_bytes)
    evidence = tmp_path / "fetch-evidence"
    evidence.mkdir()
    run, jobs, workflow = _github_sources(workflow_bytes)
    responses = {
        "repos/example/project/actions/runs/123456": run,
        "repos/example/project/actions/runs/123456/jobs?filter=all&per_page=100": jobs,
        (
            f"repos/example/project/contents/{freeze.WORKFLOW_PATH}?ref={CANDIDATE}"
        ): workflow,
    }
    observed: list[str] = []
    monkeypatch.setattr(
        freeze,
        "_candidate_checkout",
        lambda *_args, **_kwargs: (
            repository,
            CANDIDATE,
            "https://github.com/example/project.git",
        ),
    )

    def fake_api(endpoint: str, **_kwargs: Any) -> dict[str, Any]:
        observed.append(endpoint)
        return responses[endpoint]

    monkeypatch.setattr(freeze, "_gh_api_json", fake_api)
    output, receipt = freeze.capture_linux_ci_receipt(
        repository_root=repository,
        candidate_commit=CANDIDATE,
        run_id=123456,
        provenance_path=evidence / "github.json",
        output_path=evidence / "linux-ci.json",
    )

    assert observed == list(responses)
    assert receipt["gate_passed"] is True
    assert stat_mode(evidence / "github.json") == 0o600
    assert freeze.load_linux_ci_receipt(output, repository_root=repository) == receipt


def test_freeze_cli_help_exposes_producer_create_and_verify_surfaces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        freeze.main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "local-suite" in help_text
    assert "linux-ci" in help_text
    assert "create" in help_text
    assert "verify" in help_text


def test_github_provenance_fetch_has_stdlib_https_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return b'{"source":"github"}'

    observed: list[str] = []
    monkeypatch.setattr(freeze.shutil, "which", lambda _name: None)

    def fake_urlopen(request: Any, *, timeout: int) -> Response:
        observed.append(request.full_url)
        assert timeout == 60
        return Response()

    monkeypatch.setattr(freeze.urllib.request, "urlopen", fake_urlopen)
    payload = freeze._gh_api_json(
        "repos/example/project/actions/runs/1", repository_root=tmp_path
    )

    assert payload == {"source": "github"}
    assert observed == ["https://api.github.com/repos/example/project/actions/runs/1"]
