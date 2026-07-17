from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.operational_runtime_authority as authority
from liquidity_migration.account_candidate_universe import (
    build_candidate_universe_artifact,
    load_candidate_universe,
    write_candidate_universe,
)
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.demo_rule_probe import (
    DEMO_RULE_PROBE_EVIDENCE_KIND,
    DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    ORDER_CANCEL_SOURCE,
    ORDER_CREATE_SOURCE,
    ORDER_HISTORY_SOURCE,
    ORDER_REALTIME_SOURCE,
    TRADE_HISTORY_SOURCE,
)
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig


def _private(path: Path, text: str, *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _schema_v3_candidate_and_rules(config: Path) -> tuple[Path, Path]:
    now_ns = time.time_ns()
    symbol = "AAAUSDT"
    candidate_payload = build_candidate_universe_artifact(
        [
            {
                "symbol": symbol,
                "contractType": "LinearPerpetual",
                "status": "Trading",
                "baseCoin": "AAA",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "launchTime": "1700000000000",
                "deliveryTime": "0",
                "priceFilter": {"tickSize": "0.1"},
                "lotSizeFilter": {
                    "qtyStep": "0.01",
                    "minOrderQty": "0.01",
                    "minNotionalValue": "5",
                    "maxOrderQty": "1000",
                    "maxMktOrderQty": "500",
                },
                "fundingInterval": "480",
                "isPreListing": False,
            }
        ],
        [{"symbol": symbol, "lastPrice": "10", "turnover24h": "3000000"}],
        snapshot_ts_ns=now_ns,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
    )
    candidate_path = write_candidate_universe(
        config / "candidate.json",
        candidate_payload,
    )
    candidate = load_candidate_universe(candidate_path)
    order_id = "order-aaa-1"
    order_link_id = "lm-demo-rule-aaa-1"
    rules_payload: dict[str, object] = {
        "schema_version": DEMO_RULES_SCHEMA_VERSION,
        "kind": DEMO_RULES_KIND,
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": now_ns,
        "max_probe_notional_usdt": 200.0,
        "probe_distance_bps": 100.0,
        "max_private_requests_per_second": 5,
        "symbol_source": {
            "kind": "candidate_universe_artifact",
            "path": str(candidate.path),
            "size_bytes": candidate.path.stat().st_size,
            "sha256": candidate.file_sha256,
            "artifact_sha256": candidate.artifact_sha256,
            "artifact_self_hash_verified": True,
        },
        "rules": {
            symbol: {
                "symbol": symbol,
                "qty_step": 0.01,
                "min_qty": 0.01,
                "min_notional": 5.0,
                "tick_size": 0.1,
                "max_order_qty": 1000.0,
                "max_leverage": 10.0,
                "source": "bybit_demo_post_only_acceptance_probe",
                "environment": "demo",
                "observed_ts_ns": now_ns,
            }
        },
        "evidence": {
            symbol: {
                "schema_version": DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
                "kind": DEMO_RULE_PROBE_EVIDENCE_KIND,
                "environment": "demo",
                "observed_ts_ns": now_ns,
                "symbol": symbol,
                "probe_price": 10.0,
                "probe_distance_bps": 100.0,
                "lowest_accepted_qty": 0.5,
                "lowest_accepted_notional_usdt": 5.0,
                "highest_rejected_qty": 0.0,
                "highest_rejected_notional_usdt": 0.0,
                "tested_leverage": 10.0,
                "terminal_history_timeout_seconds": 5.0,
                "terminal_history_poll_seconds": 0.1,
                "terminal_history_max_polls": 50,
                "required_terminal_confirmation_polls": 2,
                "attempts": [
                    {
                        "step_count": 50,
                        "qty": 0.5,
                        "notional_usdt": 5.0,
                        "accepted": True,
                        "outcome": "verified_cancelled_no_fill",
                        "rejection": "",
                        "order_link_id": order_link_id,
                        "order_id": order_id,
                        "create_ack_source": ORDER_CREATE_SOURCE,
                        "create_ack_order_id": order_id,
                        "create_ack_order_link_id": order_link_id,
                        "cancel_ack_source": ORDER_CANCEL_SOURCE,
                        "cancel_ack_order_id": order_id,
                        "cancel_ack_order_link_id": order_link_id,
                        "order_history_source": ORDER_HISTORY_SOURCE,
                        "order_history_query_symbol": symbol,
                        "order_history_query_order_id": order_id,
                        "order_history_query_order_link_id": order_link_id,
                        "realtime_order_source": ORDER_REALTIME_SOURCE,
                        "realtime_order_query_symbol": symbol,
                        "realtime_order_query_order_id": order_id,
                        "realtime_order_query_order_link_id": order_link_id,
                        "terminal_order_id": order_id,
                        "terminal_order_link_id": order_link_id,
                        "terminal_order_source": ORDER_HISTORY_SOURCE,
                        "terminal_confirmation_sources": [
                            ORDER_HISTORY_SOURCE,
                            ORDER_HISTORY_SOURCE,
                        ],
                        "terminal_status": "Cancelled",
                        "terminal_cum_exec_qty": "0",
                        "terminal_cum_exec_value": "0",
                        "terminal_observed_ts_ns": now_ns,
                        "terminal_poll_count": 2,
                        "terminal_confirmation_polls": 2,
                        "trade_history_source": TRADE_HISTORY_SOURCE,
                        "trade_history_query_symbol": symbol,
                        "trade_history_query_order_id": order_id,
                        "trade_history_query_order_link_id": order_link_id,
                        "trade_history_row_count": 0,
                    }
                ],
            }
        },
        "artifact_sha256": "",
    }
    rules_payload["artifact_sha256"] = hashlib.sha256(
        canonical_json(rules_payload)
    ).hexdigest()
    rules_path = _private(
        config / "rules.json",
        json.dumps(rules_payload, sort_keys=True) + "\n",
    )
    return candidate_path, rules_path


def _git_repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    (path / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "candidate"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return path, commit


def _commit_all(repository: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_persistence: str = "0",
    liveness_scope: str = "demo-paper",
    continuous_paper_sleeve: str = "on",
    real_candidate_rule_coverage: bool = False,
) -> tuple[Path, str, Path, dict[str, Path]]:
    repository, commit = _git_repository(tmp_path / "repo")
    machine_id = _private(tmp_path / "machine-id", "test-machine\n")
    config = tmp_path / "config"
    roots = {
        name: tmp_path / "roots" / name
        for name in (
            "demo-account",
            "demo-inbox",
            "demo-market",
            "paper-account",
            "paper-inbox",
            "paper-market",
        )
    }
    for root in roots.values():
        root.mkdir(parents=True)
    for name in ("paper-account", "paper-inbox", "paper-market"):
        roots[name].chmod(0o700)
    if real_candidate_rule_coverage:
        symbols, rules = _schema_v3_candidate_and_rules(config)
    else:
        symbols = _private(config / "symbols.txt", "BTCUSDT\n")
        rules = _private(config / "rules.json", '{"schema_version":1}\n')
    risk = _private(config / "risk.json", '{"max_leverage":2}\n')
    paper_symbols = _private(config / "paper" / "symbols.txt", symbols.read_text())
    paper_rules = _private(config / "paper" / "rules.json", rules.read_text())
    paper_risk = _private(config / "paper" / "risk.json", risk.read_text())
    environment_paths = {
        "account-execution.env": _private(
            config / "account-execution.env",
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
            f"ACCOUNT_RAW_MARKET_PERSISTENCE={raw_persistence}\n"
            f"ACCOUNT_LIVENESS_SCOPE={liveness_scope}\n"
            f"ACCOUNT_EXECUTION_ROOT={roots['demo-account']}\n"
            f"ACCOUNT_INTENT_INBOX_ROOT={roots['demo-inbox']}\n"
            f"ACCOUNT_CAPTURE_ROOT={roots['demo-market']}\n"
            f"ACCOUNT_SYMBOLS_FILE={symbols}\n"
            f"CANDIDATE_UNIVERSE_FILE={symbols}\n"
            f"ACCOUNT_DEMO_RULES_FILE={rules}\n"
            f"ACCOUNT_RISK_POLICY_FILE={risk}\n",
        ),
        "account-paper-execution.env": _private(
            config / "account-paper-execution.env",
            "ACCOUNT_PAPER_KERNEL_REQUIRED=1\n"
            f"ACCOUNT_RAW_MARKET_PERSISTENCE={raw_persistence}\n"
            f"ACCOUNT_EXECUTION_ROOT={roots['paper-account']}\n"
            f"ACCOUNT_INTENT_INBOX_ROOT={roots['paper-inbox']}\n"
            f"ACCOUNT_PAPER_CAPTURE_ROOT={roots['paper-market']}\n"
            f"ACCOUNT_SYMBOLS_FILE={paper_symbols}\n"
            f"CANDIDATE_UNIVERSE_FILE={paper_symbols}\n"
            f"ACCOUNT_DEMO_RULES_FILE={paper_rules}\n"
            f"ACCOUNT_RISK_POLICY_FILE={paper_risk}\n",
            mode=0o640,
        ),
        "bybit-demo.env": _private(
            config / "bybit-demo.env",
            "BYBIT_DEMO_API_KEY=demo-key\n"
            "BYBIT_DEMO_API_SECRET=demo-secret\n"
            "REAL_MONEY=false\n",
        ),
        "sleeves.resolved.env": _private(
            config / "sleeves.resolved.env",
            "LONG_SLEEVE=on\n"
            "CONTINUOUS_SLEEVE=on\n"
            f"CONTINUOUS_PAPER_SLEEVE={continuous_paper_sleeve}\n"
            "CONTINUOUS_HEDGE_TIMER=on\n",
            mode=0o640,
        ),
    }
    paper_group_id = environment_paths["account-paper-execution.env"].stat().st_gid
    assert environment_paths["sleeves.resolved.env"].stat().st_gid == paper_group_id
    monkeypatch.setattr(authority, "_paper_user_id", os.geteuid)
    monkeypatch.setattr(authority, "_paper_group_id", lambda: paper_group_id)
    monkeypatch.setattr(authority, "_authorization_owner_uid", os.geteuid)
    monkeypatch.setattr(
        authority,
        "REQUIRED_ENVIRONMENT_PATHS",
        tuple(environment_paths.values()),
    )
    if not real_candidate_rule_coverage:
        monkeypatch.setattr(
            authority,
            "build_candidate_rule_coverage",
            lambda candidate_path, rules_path, **_kwargs: {
                "candidate_path": str(candidate_path),
                "rules_path": str(rules_path),
                "status": "passed",
            },
        )
    return repository, commit, machine_id, {
        **environment_paths,
        "symbols": symbols,
        "rules": rules,
        "paper-symbols": paper_symbols,
        "paper-rules": paper_rules,
    }


def _issue(
    tmp_path: Path,
    repository: Path,
    commit: str,
    machine_id: Path,
    *,
    profile: str = authority.OPERATIONAL_PROFILE,
) -> tuple[Path, dict[str, Any]]:
    receipt = tmp_path / "etc" / "account-execution-operational-ready"
    receipt.parent.mkdir(exist_ok=True)
    payload = authority.issue_operational_authorization(
        receipt_path=receipt,
        expected_commit=commit,
        repo_root=repository,
        machine_id_path=machine_id,
        authorization_reference="owner task 019f6257 explicit demo/paper operation",
        owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
        profile=profile,
    )
    return receipt, payload


def test_issue_and_verify_bind_clean_commit_machine_inputs_and_narrow_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, payload = _issue(tmp_path, repository, commit, machine_id)

    assert stat.S_IMODE(receipt.stat().st_mode) == 0o640
    assert payload["authorized_commit"] == commit
    assert payload["scope"] == "demo_paper_operational_only_no_real_money"
    assert payload["raw_market_persistence"] == "disabled"
    assert payload["research_evidence_status"] == "not_claimed_or_authorized"
    assert payload["paper_execution_model_scope"] == (
        "integration_only_uncalibrated"
    )
    assert len(payload["runtime_roots"]) == 6
    assert len(payload["runtime_inputs"]) == 8
    assert not any(
        "calibration" in name.lower() for name in payload["runtime_inputs"]
    )

    monkeypatch.setenv("ACCOUNT_RAW_MARKET_PERSISTENCE", "0")
    verified = authority.verify_operational_authorization(
        receipt_path=receipt,
        repo_root=repository,
        machine_id_path=machine_id,
        unit="liquidity-migration-account-execution.service",
    )
    assert verified["artifact_sha256"] == payload["artifact_sha256"]


def test_issue_validates_schema_v3_sources_and_byte_exact_paper_mirrors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(
        tmp_path,
        monkeypatch,
        real_candidate_rule_coverage=True,
    )

    _receipt, payload = _issue(tmp_path, repository, commit, machine_id)

    assert json.loads(paths["symbols"].read_bytes())["schema_version"] == 3
    assert json.loads(paths["rules"].read_bytes())["schema_version"] == 3
    assert paths["paper-symbols"].read_bytes() == paths["symbols"].read_bytes()
    assert paths["paper-rules"].read_bytes() == paths["rules"].read_bytes()
    runtime_inputs = payload["runtime_inputs"]
    assert isinstance(runtime_inputs, dict)
    assert (
        runtime_inputs["account-execution.env:ACCOUNT_DEMO_RULES_FILE"]["sha256"]
        == runtime_inputs[
            "account-paper-execution.env:ACCOUNT_DEMO_RULES_FILE"
        ]["sha256"]
    )


def test_paper_runtime_verification_never_reopens_demo_route_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(tmp_path, monkeypatch)
    receipt, payload = _issue(tmp_path, repository, commit, machine_id)
    real_read = authority.read_stable_file
    forbidden = {
        paths["account-execution.env"].resolve(),
        paths["bybit-demo.env"].resolve(),
    }
    opened: set[Path] = set()

    def credential_fenced_read(path: str | Path, **kwargs: Any):
        candidate = Path(path).resolve()
        opened.add(candidate)
        if candidate in forbidden:
            raise AssertionError(f"paper runtime opened forbidden environment: {candidate}")
        return real_read(path, **kwargs)

    monkeypatch.setattr(authority, "read_stable_file", credential_fenced_read)
    monkeypatch.setenv("REAL_MONEY", "false")
    monkeypatch.setenv("ACCOUNT_RAW_MARKET_PERSISTENCE", "0")
    for unit in authority.PAPER_RUNTIME_UNITS:
        verified = authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit=unit,
        )
        assert verified["artifact_sha256"] == payload["artifact_sha256"]
    assert forbidden.isdisjoint(opened)

    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "must-not-cross-paper-boundary")
    with pytest.raises(ValueError, match="paper runtime environment contains demo credentials"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit="liquidity-migration-account-paper-execution.service",
        )


def test_authority_rejects_exchange_credentials_in_paper_route_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(tmp_path, monkeypatch)
    paper_environment = paths["account-paper-execution.env"]
    paper_environment.write_text(
        paper_environment.read_text(encoding="utf-8")
        + "BYBIT_DEMO_API_KEY=forbidden\n",
        encoding="utf-8",
    )
    paper_environment.chmod(0o640)

    with pytest.raises(ValueError, match="paper operational environment must not contain exchange credentials"):
        _issue(tmp_path, repository, commit, machine_id)


def test_demo_operational_profile_runs_demo_fleet_without_paper_twin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(
        tmp_path,
        monkeypatch,
        raw_persistence="0",
        liveness_scope="demo",
        continuous_paper_sleeve="off",
    )
    paths["account-paper-execution.env"].unlink()
    receipt, payload = _issue(
        tmp_path,
        repository,
        commit,
        machine_id,
        profile=authority.DEMO_OPERATIONAL_PROFILE,
    )

    assert payload["scope"] == "demo_operational_only_no_paper_no_real_money"
    assert payload["raw_market_persistence"] == "disabled"
    assert payload["research_evidence_status"] == "not_claimed_or_authorized"
    assert payload["paper_execution_model_scope"] == "not_applicable_no_paper"
    assert payload["authorized_units"] == list(
        authority.DEMO_OPERATIONAL_AUTHORIZED_UNITS
    )
    assert set(payload["environment_files"]) == {
        "account-execution.env",
        "bybit-demo.env",
        "sleeves.resolved.env",
    }
    assert len(payload["runtime_roots"]) == 3
    assert len(payload["runtime_inputs"]) == 4
    assert not any("paper" in unit for unit in payload["authorized_units"])

    monkeypatch.setenv("ACCOUNT_RAW_MARKET_PERSISTENCE", "0")
    monkeypatch.setenv("ACCOUNT_LIVENESS_SCOPE", "demo")
    for unit in authority.DEMO_OPERATIONAL_AUTHORIZED_UNITS:
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit=unit,
        )
    for unit in (
        "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-paper.service",
    ):
        with pytest.raises(ValueError, match="not authorized"):
            authority.verify_operational_authorization(
                receipt_path=receipt,
                repo_root=repository,
                machine_id_path=machine_id,
                unit=unit,
            )


def test_demo_operational_profile_rejects_paper_scope_and_paper_sleeve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(
        tmp_path,
        monkeypatch,
        liveness_scope="demo-paper",
    )
    receipt = tmp_path / "etc" / "ready"
    receipt.parent.mkdir()
    with pytest.raises(ValueError, match="ACCOUNT_LIVENESS_SCOPE=demo"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )

    second = tmp_path / "second"
    second.mkdir()
    repository, commit, machine_id, _paths = _fixture(
        second,
        monkeypatch,
        liveness_scope="demo",
        continuous_paper_sleeve="on",
    )
    receipt = second / "etc" / "ready"
    receipt.parent.mkdir()
    with pytest.raises(ValueError, match="CONTINUOUS_PAPER_SLEEVE=off"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )


def test_authority_rejects_raw_research_mode_and_wrong_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(
        tmp_path,
        monkeypatch,
        raw_persistence="1",
    )
    receipt = tmp_path / "etc" / "ready"
    receipt.parent.mkdir()
    with pytest.raises(ValueError, match="ACCOUNT_RAW_MARKET_PERSISTENCE=0"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
        )

    with pytest.raises(ValueError, match="exact demo/paper-only"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement="yes",
        )


def test_demo_operational_profile_requires_one_source_bound_candidate_population(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(
        tmp_path,
        monkeypatch,
        liveness_scope="demo",
        continuous_paper_sleeve="off",
    )
    alternate = _private(tmp_path / "alternate-symbols.txt", "BTCUSDT\n")
    environment = paths["account-execution.env"].read_text(encoding="utf-8")
    paths["account-execution.env"].write_text(
        environment.replace(
            f"CANDIDATE_UNIVERSE_FILE={paths['symbols']}",
            f"CANDIDATE_UNIVERSE_FILE={alternate}",
        ),
        encoding="utf-8",
    )
    paths["account-execution.env"].chmod(0o600)

    with pytest.raises(ValueError, match="must also be the owner symbols file"):
        _issue(
            tmp_path,
            repository,
            commit,
            machine_id,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )

    paths["account-execution.env"].write_text(environment, encoding="utf-8")
    paths["account-execution.env"].chmod(0o600)

    def reject_coverage(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError(
            "demo-rule receipt does not exactly cover candidate universe"
        )

    monkeypatch.setattr(authority, "build_candidate_rule_coverage", reject_coverage)
    with pytest.raises(ValueError, match="does not exactly cover"):
        _issue(
            tmp_path,
            repository,
            commit,
            machine_id,
            profile=authority.DEMO_OPERATIONAL_PROFILE,
        )


def test_verify_rejects_changed_input_mainnet_environment_and_unknown_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(tmp_path, monkeypatch)
    receipt, _payload = _issue(tmp_path, repository, commit, machine_id)

    paths["symbols"].write_text("ETHUSDT\n", encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="differs from its validated demo source|changed after operational authorization",
    ):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )

    # Restore and issue a new fixture/receipt for independent runtime-boundary checks.
    second = tmp_path / "second"
    second.mkdir()
    repository, commit, machine_id, _paths = _fixture(second, monkeypatch)
    receipt, _payload = _issue(second, repository, commit, machine_id)
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "forbidden")
    with pytest.raises(ValueError, match="mainnet credentials"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )
    monkeypatch.delenv("BYBIT_REAL_API_KEY")
    with pytest.raises(ValueError, match="not authorized"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
            unit="liquidity-migration-mainnet.service",
        )


def test_verify_rejects_dirty_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, _payload = _issue(tmp_path, repository, commit, machine_id)

    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checkout is dirty"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )
    (repository / "untracked.txt").unlink()


def test_git_checks_trust_only_the_explicit_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = (tmp_path / "shared checkout").resolve()
    repository.mkdir()
    (repository / ".git").mkdir()
    observed: dict[str, Any] = {}
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "decoy.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "decoy-worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "decoy-index"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "decoy-system-config"))

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="verified\n", stderr="")

    monkeypatch.setattr(authority.subprocess, "run", fake_run)

    assert authority._git_output(repository, "rev-parse", "HEAD") == "verified"
    command = observed["command"]
    environment = observed["environment"]
    assert command[0] == "/usr/bin/git"
    assert "--no-optional-locks" in command
    assert f"safe.directory={repository}" in command
    assert f"--git-dir={repository / '.git'}" in command
    assert f"--work-tree={repository}" in command
    assert command[-2:] == ["rev-parse", "HEAD"]
    assert environment == authority._git_environment()
    assert environment["HOME"] == "/nonexistent"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert "GIT_DIR" not in environment
    assert "GIT_WORK_TREE" not in environment
    assert "GIT_INDEX_FILE" not in environment
    assert "GIT_CONFIG_SYSTEM" not in environment


def test_clean_checkout_ignores_ambient_repository_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _git_repository(tmp_path / "authorized")
    decoy = tmp_path / "decoy"
    subprocess.run(["git", "clone", "-q", str(repository), str(decoy)], check=True)
    (repository / "tracked.txt").write_text("modified runtime code\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))

    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_clean_checkout_uses_private_index_instead_of_mutable_index_flags(
    tmp_path: Path,
    flag: str,
) -> None:
    repository, commit = _git_repository(tmp_path / "authorized")
    subprocess.run(
        ["git", "-C", str(repository), "update-index", flag, "tracked.txt"],
        check=True,
    )
    (repository / "tracked.txt").write_text("hidden tracked edit\n", encoding="utf-8")

    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


def test_clean_checkout_ignores_git_replacement_objects(tmp_path: Path) -> None:
    repository, original = _git_repository(tmp_path / "authorized")
    tracked = repository / "tracked.txt"
    tracked.write_text("replacement tree\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qam", "replacement"],
        check=True,
    )
    replacement = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repository), "checkout", "-q", original], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "replace", original, replacement],
        check=True,
    )
    tracked.write_text("replacement tree\n", encoding="utf-8")

    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, original)


def test_clean_checkout_compares_raw_bytes_despite_clean_filter(tmp_path: Path) -> None:
    repository, _commit = _git_repository(tmp_path / "authorized")
    (repository / ".gitattributes").write_text(
        "tracked.txt filter=mask\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "filter.mask.clean",
            "printf 'candidate\\n'",
        ],
        check=True,
    )
    commit = _commit_all(repository, "filtered candidate")
    (repository / "tracked.txt").write_text(
        "malicious runtime\n",
        encoding="utf-8",
    )

    filtered_diff = subprocess.run(
        ["git", "-C", str(repository), "diff", "--quiet", "--", "tracked.txt"],
        check=False,
    )
    assert filtered_diff.returncode == 0
    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


def test_clean_checkout_never_executes_repo_local_clean_filter(tmp_path: Path) -> None:
    repository, _commit = _git_repository(tmp_path / "authorized")
    sentinel = tmp_path / "filter-ran"
    filter_script = tmp_path / "clean-filter.sh"
    filter_script.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(sentinel))}\ncat\n",
        encoding="utf-8",
    )
    filter_script.chmod(0o755)
    (repository / ".gitattributes").write_text(
        "tracked.txt filter=audit\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "filter.audit.clean",
            shlex.quote(str(filter_script)),
        ],
        check=True,
    )
    # `git add -A` may reuse the unchanged tracked file's stat-cache entry even
    # though .gitattributes now assigns a filter. Force the setup-time filter
    # execution so this test does not depend on filesystem timestamp granularity.
    subprocess.run(
        ["git", "-C", str(repository), "add", "--renormalize", "tracked.txt"],
        check=True,
    )
    commit = _commit_all(repository, "audited filter")
    assert sentinel.exists()
    sentinel.unlink()

    assert authority.require_clean_authorized_checkout(repository, commit) == commit
    assert not sentinel.exists()


def test_clean_checkout_rejects_crlf_hidden_by_text_attributes(tmp_path: Path) -> None:
    repository, _commit = _git_repository(tmp_path / "authorized")
    (repository / ".gitattributes").write_text(
        "tracked.txt text eol=lf\n",
        encoding="utf-8",
    )
    commit = _commit_all(repository, "normalized text")
    (repository / "tracked.txt").write_bytes(b"candidate\r\n")

    filtered_diff = subprocess.run(
        ["git", "-C", str(repository), "diff", "--quiet", "--", "tracked.txt"],
        check=False,
    )
    assert filtered_diff.returncode == 0
    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


def test_clean_checkout_compares_git_executable_mode(tmp_path: Path) -> None:
    repository, commit = _git_repository(tmp_path / "authorized")
    tracked = repository / "tracked.txt"
    tracked.chmod(stat.S_IMODE(tracked.stat().st_mode) | stat.S_IXUSR)

    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


def test_clean_checkout_compares_symlink_type_and_target_bytes(tmp_path: Path) -> None:
    repository, _commit = _git_repository(tmp_path / "authorized")
    link = repository / "runtime-link"
    link.symlink_to("target-a")
    commit = _commit_all(repository, "tracked symlink")
    assert authority.require_clean_authorized_checkout(repository, commit) == commit

    link.unlink()
    link.write_bytes(b"target-a")
    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)

    link.unlink()
    link.symlink_to("target-b")
    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


def test_clean_checkout_parses_nul_delimited_tree_paths(tmp_path: Path) -> None:
    repository, _commit = _git_repository(tmp_path / "authorized")
    unusual = repository / "runtime\nmodule.py"
    unusual.write_text("expected\n", encoding="utf-8")
    commit = _commit_all(repository, "unusual tracked path")
    assert authority.require_clean_authorized_checkout(repository, commit) == commit

    unusual.write_text("modified\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)


@pytest.mark.parametrize(
    "manifest",
    [
        b"100644 blob " + b"a" * 40 + b"\ttracked.txt",
        b"100644 blob " + b"a" * 40 + b"\t.git/config\0",
    ],
)
def test_commit_tree_parser_rejects_unterminated_or_git_admin_paths(
    manifest: bytes,
) -> None:
    with pytest.raises(ValueError, match="tree (output|path)"):
        authority._parse_commit_tree(manifest)


def test_clean_checkout_rejects_gitlinks_fail_closed(tmp_path: Path) -> None:
    repository, _commit = _git_repository(tmp_path / "authorized")
    _git_repository(repository / "dependency")
    commit = _commit_all(repository, "embedded repository")

    with pytest.raises(ValueError, match="unsupported gitlink"):
        authority.require_clean_authorized_checkout(repository, commit)


def test_clean_checkout_rechecks_early_entry_metadata_after_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _git_repository(tmp_path / "authorized")
    real_output = authority._git_output_bytes
    manifest_reads = 0

    def mutate_after_manifest(repo_root: Path, *args: str) -> bytes:
        nonlocal manifest_reads
        output = real_output(repo_root, *args)
        manifest_reads += 1
        if manifest_reads == 2:
            (repository / "tracked.txt").write_text(
                "changed after initial scan\n",
                encoding="utf-8",
            )
        return output

    monkeypatch.setattr(authority, "_git_output_bytes", mutate_after_manifest)

    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)

    assert manifest_reads == 2


def test_clean_checkout_rechecks_untracked_files_after_raw_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _git_repository(tmp_path / "authorized")
    late_untracked = repository / "late_runtime_override.py"
    real_compare = authority._compare_raw_authorized_tree

    def create_untracked_after_raw_scan(repo_root: Path, candidate: str) -> None:
        real_compare(repo_root, candidate)
        late_untracked.write_text("raise SystemExit('unexpected')\n", encoding="utf-8")

    monkeypatch.setattr(
        authority,
        "_compare_raw_authorized_tree",
        create_untracked_after_raw_scan,
    )

    with pytest.raises(ValueError, match="authorized checkout is dirty"):
        authority.require_clean_authorized_checkout(repository, commit)

    assert late_untracked.exists()


def test_clean_checkout_rejects_head_change_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = _git_repository(tmp_path / "authorized")
    real_output = authority._git_output
    head_reads = 0

    def changing_head(
        repo_root: Path,
        *args: str,
        index_file: Path | None = None,
    ) -> str:
        nonlocal head_reads
        result = real_output(repo_root, *args, index_file=index_file)
        if args == ("rev-parse", "--verify", "HEAD^{commit}"):
            head_reads += 1
            if head_reads == 2:
                return "f" * 40
        return result

    monkeypatch.setattr(authority, "_git_output", changing_head)

    with pytest.raises(ValueError, match="HEAD changed"):
        authority.require_clean_authorized_checkout(repository, commit)

    assert head_reads == 2


def test_verify_rejects_replaced_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, payload = _issue(tmp_path, repository, commit, machine_id)
    root_path = Path(next(iter(payload["runtime_roots"].values()))["path"])
    original = root_path.with_name(root_path.name + "-original")
    root_path.rename(original)
    root_path.mkdir()

    with pytest.raises(ValueError, match="runtime root changed"):
        authority.verify_operational_authorization(
            receipt_path=receipt,
            repo_root=repository,
            machine_id_path=machine_id,
        )


def test_runtime_wrapper_verifies_operational_authority() -> None:
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_authorized_runtime.sh"
    ).read_text(encoding="utf-8")

    assert "operational_runtime_authority verify-runtime" in wrapper
    assert "account-execution-operational-ready" in wrapper


def test_receipt_is_private_canonical_and_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt, _payload = _issue(tmp_path, repository, commit, machine_id)
    original = receipt.read_bytes()

    with pytest.raises(FileExistsError):
        _issue(tmp_path, repository, commit, machine_id)
    assert receipt.read_bytes() == original
    assert os.stat(receipt).st_nlink == 1


def test_issue_reopens_bound_inputs_before_publication_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(tmp_path, monkeypatch)
    real_self_hash = authority._self_hash
    mutated = False

    def mutate_after_capture(payload: dict[str, Any]) -> str:
        nonlocal mutated
        digest = real_self_hash(payload)
        if (
            not mutated
            and payload.get("kind") == authority.KIND
            and payload.get("artifact_sha256") == ""
        ):
            mutated = True
            paths["symbols"].write_text("ETHUSDT\n", encoding="utf-8")
        return digest

    monkeypatch.setattr(authority, "_self_hash", mutate_after_capture)

    with pytest.raises(
        ValueError,
        match="differs from its validated demo source|changed after operational authorization",
    ):
        _issue(tmp_path, repository, commit, machine_id)

    receipt = tmp_path / "etc" / "account-execution-operational-ready"
    assert mutated is True
    assert not receipt.exists()
    assert not list(receipt.parent.glob(".operational-authority-stage-*"))


def test_issue_rechecks_bound_inputs_after_final_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, paths = _fixture(tmp_path, monkeypatch)
    receipt = tmp_path / "etc" / "account-execution-operational-ready"
    receipt.parent.mkdir(exist_ok=True)
    checks = 0

    def mutate_after_first_source_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            paths["rules"].write_text('{"schema_version":2}\n', encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="differs from its validated demo source|changed after operational authorization",
    ):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
            final_publication_check=mutate_after_first_source_check,
        )

    assert checks == 2
    assert not receipt.exists()
    assert not list(receipt.parent.glob(".operational-authority-stage-*"))


def test_post_link_authority_check_observes_only_unloadable_mode_0400(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit, machine_id, _paths = _fixture(tmp_path, monkeypatch)
    receipt = tmp_path / "etc" / "account-execution-operational-ready"
    receipt.parent.mkdir(exist_ok=True)
    checks = 0

    def final_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            metadata = receipt.stat()
            assert stat.S_IMODE(metadata.st_mode) == 0o400
            assert metadata.st_nlink == 1
            with pytest.raises(ValueError, match="mode 0600 or 0640"):
                authority._load_receipt(receipt)
            raise RuntimeError("synthetic late authority failure")

    with pytest.raises(RuntimeError, match="synthetic late authority failure"):
        authority.issue_operational_authorization(
            receipt_path=receipt,
            expected_commit=commit,
            repo_root=repository,
            machine_id_path=machine_id,
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
            final_publication_check=final_check,
        )

    assert checks == 3
    assert not receipt.exists()
    assert not list(receipt.parent.glob(".operational-authority-stage-*"))


def test_receipt_loader_rejects_oversized_artifact_before_json_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "oversized-receipt"
    receipt.write_bytes(b"{" + b"x" * authority._MAX_RECEIPT_SIZE + b"}")
    receipt.chmod(0o600)
    monkeypatch.setattr(authority, "_authorization_owner_uid", os.geteuid)

    with pytest.raises(ValueError, match="size limit"):
        authority._load_receipt(receipt)


def test_receipt_loader_requires_root_ownership(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("non-root ownership rejection requires an unprivileged test user")
    receipt = tmp_path / "user-owned-receipt"
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o600)

    with pytest.raises(ValueError, match="owned by root"):
        authority._load_receipt(receipt)


def test_direct_issue_api_requires_authorization_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(authority, "_authorization_owner_uid", lambda: 0)

    with pytest.raises(RuntimeError, match="issuance requires root"):
        authority.issue_operational_authorization(
            receipt_path=tmp_path / "receipt",
            expected_commit="a" * 40,
            repo_root=tmp_path,
            machine_id_path=tmp_path / "machine-id",
            authorization_reference="owner authorization",
            owner_acknowledgement=authority.OWNER_ACKNOWLEDGEMENT,
        )


def test_managed_unit_inactivity_observer_checks_exact_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    systemctl = Path("/trusted/systemctl")
    monkeypatch.setattr(
        authority,
        "_trusted_executable",
        lambda path, *, label: path,
    )

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(systemctl)
        assert kwargs["env"] == {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
        }
        observed.append(command[-1])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="LoadState=loaded\nActiveState=inactive\n",
            stderr="",
        )

    monkeypatch.setattr(authority.subprocess, "run", fake_run)

    authority.require_managed_units_inactive(systemctl_path=systemctl)

    assert observed == list(authority.ISSUANCE_QUIESCENCE_UNITS)
    assert len(observed) == 12
    assert set(authority.AUTHORIZED_UNITS).issubset(observed)
    assert set(observed) - set(authority.AUTHORIZED_UNITS) == {
        "liquidity-migration-demo-liveness.timer",
        "liquidity-migration-continuous-hedge.timer",
        "liquidity-migration-continuous-rmom-refresh.timer",
    }


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("LoadState=not-found\nActiveState=inactive\n", "not exactly loaded"),
        ("LoadState=loaded\nActiveState=active\n", "not inactive"),
        ("LoadState=loaded\nActiveState=failed\n", "not inactive"),
        ("LoadState=loaded\n", "incomplete state"),
    ],
)
def test_managed_unit_inactivity_observer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        authority,
        "_trusted_executable",
        lambda path, *, label: path,
    )
    monkeypatch.setattr(
        authority.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        authority.require_managed_units_inactive(
            systemctl_path=Path("/trusted/systemctl")
        )


def test_issue_cli_enters_production_guard_and_supplies_inactivity_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []

    @contextmanager
    def held_lock():
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def fake_issue(**kwargs: Any) -> dict[str, Any]:
        events.append("issue")
        assert events == ["lock-enter", "issue"]
        assert kwargs["final_publication_check"] is authority.require_managed_units_inactive
        return {"status": "passed"}

    monkeypatch.setattr(authority.os, "geteuid", lambda: 0)
    monkeypatch.setattr(authority, "_production_maintenance_guard", held_lock)
    monkeypatch.setattr(authority, "issue_operational_authorization", fake_issue)

    result = authority.main(
        [
            "issue",
            "--receipt",
            "/etc/liquidity-migration/receipt",
            "--expected-commit",
            "a" * 40,
            "--repo-root",
            "/opt/liquidity-migration",
            "--authorization-reference",
            "owner task",
            "--owner-acknowledgement",
            authority.OWNER_ACKNOWLEDGEMENT,
        ]
    )

    assert result == 0
    assert events == ["lock-enter", "issue", "lock-exit"]
    assert json.loads(capsys.readouterr().out) == {"status": "passed"}


def test_issue_cli_refuses_missing_preimport_lock_handoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issued = False

    def fake_issue(**_kwargs: Any) -> dict[str, Any]:
        nonlocal issued
        issued = True
        return {"status": "passed"}

    monkeypatch.setattr(authority.os, "geteuid", lambda: 0)
    monkeypatch.delenv(authority._INHERITED_MAINTENANCE_LOCK_MARKER, raising=False)
    monkeypatch.setattr(authority, "issue_operational_authorization", fake_issue)

    result = authority.main(
        [
            "issue",
            "--receipt",
            "/etc/liquidity-migration/receipt",
            "--expected-commit",
            "a" * 40,
            "--repo-root",
            "/opt/liquidity-migration",
            "--authorization-reference",
            "owner task",
            "--owner-acknowledgement",
            authority.OWNER_ACKNOWLEDGEMENT,
        ]
    )

    assert result == 2
    assert issued is False
    assert "pre-import maintenance locks from scripts/ops.sh" in capsys.readouterr().err


def test_production_guard_revalidates_preimport_inherited_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = tmp_path / "maintenance.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    descriptor = os.open(lock, os.O_RDONLY)
    observed: dict[str, Any] = {}
    try:
        monkeypatch.setattr(
            authority,
            "_INHERITED_MAINTENANCE_LOCKS",
            ((descriptor, lock),),
        )
        monkeypatch.setenv(
            authority._INHERITED_MAINTENANCE_LOCK_MARKER,
            str(descriptor),
        )

        def acquire(records: list[tuple[int, Path, int, int]], **kwargs: Any) -> None:
            observed["records"] = records
            observed["kwargs"] = kwargs

        monkeypatch.setattr(authority, "acquire_inherited_locks", acquire)

        with authority._production_maintenance_guard():
            observed["held"] = True

        metadata = lock.stat()
        assert observed["records"] == [
            (descriptor, lock, metadata.st_dev, metadata.st_ino)
        ]
        assert observed["kwargs"] == {
            "expected_uid": 0,
            "expected_gid": 0,
            "require_mount_ids": True,
        }
        assert observed["held"] is True
    finally:
        os.close(descriptor)


def test_production_guard_rejects_forged_inherited_lock_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(authority._INHERITED_MAINTENANCE_LOCK_MARKER, "9,8")

    with pytest.raises(RuntimeError, match="marker is invalid"):
        authority._production_maintenance_guard()
