from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from liquidity_migration.account_candidate_universe import (
    build_candidate_universe_artifact,
    load_candidate_universe,
    write_candidate_universe,
)
from liquidity_migration.candidate_rule_coverage import (
    REGISTERED_MAX_RULE_AGE_SECONDS,
    build_candidate_rule_coverage,
    load_candidate_rule_coverage,
    write_candidate_rule_coverage,
)
from liquidity_migration.artifact_snapshot import read_stable_file
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.demo_rule_probe import (
    DEMO_RULE_PROBE_EVIDENCE_KIND,
    DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    ORDER_CANCEL_SOURCE,
    ORDER_CREATE_SOURCE,
    ORDER_HISTORY_SOURCE,
    TRADE_HISTORY_SOURCE,
)
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from scripts import verify_candidate_rule_coverage as coverage_script


NOW_NS = 1_800_000_000_000_000_000


def _candidate(tmp_path: Path) -> Path:
    instrument = {
        "symbol": "AAAUSDT",
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
    ticker = {
        "symbol": "AAAUSDT",
        "lastPrice": "10",
        "turnover24h": "3000000",
    }
    payload = build_candidate_universe_artifact(
        [instrument],
        [ticker],
        snapshot_ts_ns=NOW_NS,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
    )
    return write_candidate_universe(tmp_path / "candidate.json", payload)


def _rules(
    tmp_path: Path,
    candidate_path: Path,
    *,
    extra: bool = False,
    legacy_evidence: bool = False,
) -> Path:
    candidate = load_candidate_universe(candidate_path)
    symbols = list(candidate.symbols) + (["EXTRAUSDT"] if extra else [])
    payload = {
        "schema_version": DEMO_RULES_SCHEMA_VERSION,
        "kind": DEMO_RULES_KIND,
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": NOW_NS,
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
                "observed_ts_ns": NOW_NS,
            }
            for symbol in symbols
        },
        "evidence": {
            symbol: ({
                "lowest_accepted_notional_usdt": 5.0,
                "attempts": [{"accepted": True}],
            } if legacy_evidence else {
                "schema_version": DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
                "kind": DEMO_RULE_PROBE_EVIDENCE_KIND,
                "environment": "demo",
                "observed_ts_ns": NOW_NS,
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
                "attempts": [{
                    "step_count": 50,
                    "qty": 0.5,
                    "notional_usdt": 5.0,
                    "accepted": True,
                    "outcome": "verified_cancelled_no_fill",
                    "rejection": "",
                    "order_link_id": f"lm-demo-rule-{symbol}-1",
                    "order_id": f"order-{symbol}-1",
                    "create_ack_source": ORDER_CREATE_SOURCE,
                    "create_ack_order_id": f"order-{symbol}-1",
                    "create_ack_order_link_id": f"lm-demo-rule-{symbol}-1",
                    "cancel_ack_source": ORDER_CANCEL_SOURCE,
                    "cancel_ack_order_id": f"order-{symbol}-1",
                    "cancel_ack_order_link_id": f"lm-demo-rule-{symbol}-1",
                    "order_history_source": ORDER_HISTORY_SOURCE,
                    "order_history_query_symbol": symbol,
                    "order_history_query_order_id": f"order-{symbol}-1",
                    "order_history_query_order_link_id": f"lm-demo-rule-{symbol}-1",
                    "terminal_order_id": f"order-{symbol}-1",
                    "terminal_order_link_id": f"lm-demo-rule-{symbol}-1",
                    "terminal_status": "Cancelled",
                    "terminal_cum_exec_qty": "0",
                    "terminal_cum_exec_value": "0",
                    "terminal_observed_ts_ns": NOW_NS,
                    "terminal_poll_count": 2,
                    "terminal_confirmation_polls": 2,
                    "trade_history_source": TRADE_HISTORY_SOURCE,
                    "trade_history_query_symbol": symbol,
                    "trade_history_query_order_id": f"order-{symbol}-1",
                    "trade_history_query_order_link_id": f"lm-demo-rule-{symbol}-1",
                    "trade_history_row_count": 0,
                }],
            })
            for symbol in symbols
        },
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    path = tmp_path / ("rules-extra.json" if extra else "rules.json")
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_coverage_receipt_reopens_and_reproduces_sources(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    payload = build_candidate_rule_coverage(
        candidate,
        rules,
        created_ts_ns=NOW_NS,
        validation_now_ns=NOW_NS + 1,
    )
    receipt = write_candidate_rule_coverage(tmp_path / "coverage.json", payload)
    loaded = load_candidate_rule_coverage(
        receipt,
        validation_now_ns=NOW_NS + 2,
    )
    assert loaded["status"] == "passed"
    assert loaded["symbols"] == ["AAAUSDT"]
    assert loaded["coverage"]["missing"] == 0


def test_coverage_rejects_weakened_rule_freshness_before_sources(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="registered 604800-second maximum"):
        build_candidate_rule_coverage(
            tmp_path / "missing-candidate.json",
            tmp_path / "missing-rules.json",
            max_rule_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS + 1,
        )


def test_coverage_script_rejects_weakened_rule_freshness_before_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        coverage_script.main(
            [
                "--candidate-universe",
                str(tmp_path / "missing-candidate.json"),
                "--demo-rules",
                str(tmp_path / "missing-rules.json"),
                "--output",
                str(tmp_path / "coverage.json"),
                "--max-rule-age-hours",
                "168.0001",
            ]
        )
    assert raised.value.code == 2
    assert "registered 604800-second maximum" in capsys.readouterr().err


def test_coverage_loader_uses_supplied_source_snapshots(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    payload = build_candidate_rule_coverage(
        candidate,
        rules,
        created_ts_ns=NOW_NS,
        validation_now_ns=NOW_NS + 1,
    )
    receipt = write_candidate_rule_coverage(tmp_path / "coverage.json", payload)

    loaded = load_candidate_rule_coverage(
        receipt,
        validation_now_ns=NOW_NS + 2,
        snapshot=read_stable_file(receipt, label="coverage"),
        candidate_snapshot=read_stable_file(candidate, label="candidate"),
        demo_rules_snapshot=read_stable_file(rules, label="rules"),
    )

    assert loaded == payload


def test_coverage_rejects_extra_rule_or_wrong_source_binding(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    with pytest.raises(ValueError, match="does not exactly cover"):
        build_candidate_rule_coverage(
            candidate,
            _rules(tmp_path, candidate, extra=True),
            validation_now_ns=NOW_NS + 1,
        )

    rules = _rules(tmp_path, candidate)
    payload = json.loads(rules.read_text(encoding="utf-8"))
    payload["symbol_source"]["artifact_sha256"] = "0" * 64
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    rules.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not bind"):
        build_candidate_rule_coverage(
            candidate,
            rules,
            validation_now_ns=NOW_NS + 1,
        )


def test_coverage_rejects_legacy_boolean_only_acceptance_evidence(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    with pytest.raises(ValueError, match="probe evidence identity"):
        build_candidate_rule_coverage(
            candidate,
            _rules(tmp_path, candidate, legacy_evidence=True),
            validation_now_ns=NOW_NS + 1,
        )


def test_loaded_receipt_fails_after_source_mutation(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    receipt = write_candidate_rule_coverage(
        tmp_path / "coverage.json",
        build_candidate_rule_coverage(
            candidate,
            rules,
            created_ts_ns=NOW_NS,
            validation_now_ns=NOW_NS + 1,
        ),
    )
    rules.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_candidate_rule_coverage(receipt, validation_now_ns=NOW_NS + 2)


def test_loaded_receipt_rejects_group_readable_file(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    receipt = write_candidate_rule_coverage(
        tmp_path / "coverage.json",
        build_candidate_rule_coverage(
            candidate,
            rules,
            created_ts_ns=NOW_NS,
            validation_now_ns=NOW_NS + 1,
        ),
    )
    os.chmod(receipt, 0o640)
    with pytest.raises(ValueError, match="mode 0600"):
        load_candidate_rule_coverage(receipt, validation_now_ns=NOW_NS + 2)
