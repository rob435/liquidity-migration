from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from liquidity_migration.strategy.account_candidate_universe import (
    build_candidate_universe_artifact,
    load_candidate_universe,
    write_candidate_universe,
)
from liquidity_migration.ops.candidate_rule_coverage import (
    CandidateRuleRefreshRequired,
    REGISTERED_MAX_RULE_AGE_SECONDS,
    build_candidate_rule_coverage,
    classify_demo_rule_receipt_freshness,
    project_demo_rules_to_candidate_subset,
)
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.strategy.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.venue.demo_rule_probe import (
    DEMO_RULE_PROBE_EVIDENCE_KIND,
    DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    ORDER_CANCEL_SOURCE,
    ORDER_CREATE_SOURCE,
    ORDER_HISTORY_SOURCE,
    TRADE_HISTORY_SOURCE,
)
from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig


NOW_NS = 1_800_000_000_000_000_000


def _projection_cli_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "maintain" / "project_demo_rules_to_candidate.py"
    spec = importlib.util.spec_from_file_location("project_demo_rules_to_candidate_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate_symbols(
    tmp_path: Path,
    symbols: tuple[str, ...],
    *,
    filename: str,
    min_notional: str = "5",
    qty_step: str = "0.01",
    min_qty: str = "0.01",
    tick_size: str = "0.1",
    max_market_qty: str = "500",
    max_leverage: str = "10",
) -> Path:
    instruments = [
        {
            "symbol": symbol,
            "contractType": "LinearPerpetual",
            "status": "Trading",
            "baseCoin": symbol.removesuffix("USDT"),
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "launchTime": "1700000000000",
            "deliveryTime": "0",
            "priceFilter": {"tickSize": tick_size},
            "leverageFilter": {"maxLeverage": max_leverage},
            "lotSizeFilter": {
                "qtyStep": qty_step,
                "minOrderQty": min_qty,
                "minNotionalValue": min_notional,
                "maxOrderQty": "1000",
                "maxMktOrderQty": max_market_qty,
            },
            "fundingInterval": "480",
            "isPreListing": False,
        }
        for symbol in symbols
    ]
    tickers = [
        {
            "symbol": symbol,
            "lastPrice": "10",
            "turnover24h": str(3_000_000 - index),
        }
        for index, symbol in enumerate(symbols)
    ]
    payload = build_candidate_universe_artifact(
        instruments,
        tickers,
        snapshot_ts_ns=NOW_NS,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
    )
    return write_candidate_universe(tmp_path / filename, payload)


def _candidate(tmp_path: Path) -> Path:
    return _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="candidate.json",
    )


def _rules(
    tmp_path: Path,
    candidate_path: Path,
    *,
    extra: bool = False,
    legacy_evidence: bool = False,
    filename: str | None = None,
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
                "max_order_qty": 500.0,
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
    path = tmp_path / (
        filename or ("rules-extra.json" if extra else "rules.json")
    )
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_coverage_validation_reproduces_sources(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    payload = build_candidate_rule_coverage(
        candidate,
        rules,
        created_ts_ns=NOW_NS,
        validation_now_ns=NOW_NS + 1,
    )
    assert payload["status"] == "passed"
    assert payload["symbols"] == ["AAAUSDT"]
    assert payload["coverage"]["missing"] == 0


def test_rule_freshness_classifier_separates_expiry_from_future_dating(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)

    assert classify_demo_rule_receipt_freshness(
        rules,
        validation_now_ns=NOW_NS + 1,
    ) == "fresh"
    assert classify_demo_rule_receipt_freshness(
        rules,
        validation_now_ns=(
            NOW_NS + (REGISTERED_MAX_RULE_AGE_SECONDS + 1) * 1_000_000_000
        ),
    ) == "expired"
    with pytest.raises(ValueError, match="future-dated"):
        classify_demo_rule_receipt_freshness(
            rules,
            validation_now_ns=NOW_NS - 1,
        )


def test_fresh_rule_evidence_projects_to_current_candidate_subset(
    tmp_path: Path,
) -> None:
    source_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT", "BBBUSDT"),
        filename="source-candidate.json",
    )
    target_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="target-candidate.json",
    )
    source_rules = _rules(
        tmp_path,
        source_candidate,
        filename="source-rules.json",
    )
    source_bytes = source_rules.read_bytes()
    output = tmp_path / "projected-rules.json"

    projected = project_demo_rules_to_candidate_subset(
        target_candidate,
        source_rules,
        output,
        validation_now_ns=NOW_NS + 1,
    )

    assert projected == output.resolve()
    assert source_rules.read_bytes() == source_bytes
    payload = json.loads(projected.read_text(encoding="utf-8"))
    assert payload["verified_ts_ns"] == NOW_NS
    assert list(payload["rules"]) == ["AAAUSDT"]
    assert list(payload["evidence"]) == ["AAAUSDT"]
    assert payload["candidate_projection"]["removed_symbols"] == ["BBBUSDT"]
    assert payload["candidate_projection"]["added_symbols"] == []
    assert payload["candidate_projection"]["limitation"] == (
        "projection_does_not_extend_empirical_evidence_freshness"
    )
    coverage = build_candidate_rule_coverage(
        target_candidate,
        projected,
        created_ts_ns=NOW_NS + 1,
        validation_now_ns=NOW_NS + 1,
    )
    assert coverage["symbols"] == ["AAAUSDT"]


def test_candidate_expansion_requires_fresh_probe_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    source_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="source-candidate.json",
    )
    expanded_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT", "BBBUSDT"),
        filename="expanded-candidate.json",
    )
    source_rules = _rules(
        tmp_path,
        source_candidate,
        filename="source-rules.json",
    )
    output = tmp_path / "projected-rules.json"

    with pytest.raises(CandidateRuleRefreshRequired, match="BBBUSDT"):
        project_demo_rules_to_candidate_subset(
            expanded_candidate,
            source_rules,
            output,
            validation_now_ns=NOW_NS + 1,
        )

    assert not output.exists()


def test_projection_cli_reserves_exit_three_for_fresh_probe_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="source-candidate.json",
    )
    expanded_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT", "BBBUSDT"),
        filename="expanded-candidate.json",
    )
    source_rules = _rules(
        tmp_path,
        source_candidate,
        filename="source-rules.json",
    )
    output = tmp_path / "projected-rules.json"

    module = _projection_cli_module()
    monkeypatch.setattr(module.time, "time_ns", lambda: NOW_NS + 1)
    status = module.main(
        [
            "--candidate-file",
            str(expanded_candidate),
            "--prior-rules-file",
            str(source_rules),
            "--output",
            str(output),
        ]
    )

    assert status == 3
    assert not output.exists()
    assert '"status": "fresh_probe_required"' in capsys.readouterr().err


@pytest.mark.parametrize(
    "changed_field",
    [
        {"min_notional": "6"},
        {"min_qty": "0.02"},
        {"qty_step": "0.02"},
        {"tick_size": "0.2"},
        {"max_market_qty": "400"},
        {"max_market_qty": "0"},
        {"max_leverage": "5"},
        {"max_leverage": "0"},
    ],
)
def test_candidate_projection_rejects_unsafe_structural_rule_drift(
    tmp_path: Path,
    changed_field: dict[str, str],
) -> None:
    source_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="source-candidate.json",
    )
    changed_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="changed-candidate.json",
        **changed_field,
    )
    source_rules = _rules(
        tmp_path,
        source_candidate,
        filename="source-rules.json",
    )
    output = tmp_path / "projected-rules.json"

    with pytest.raises(CandidateRuleRefreshRequired, match="structural rules"):
        project_demo_rules_to_candidate_subset(
            changed_candidate,
            source_rules,
            output,
            validation_now_ns=NOW_NS + 1,
        )

    assert not output.exists()


def test_candidate_projection_accepts_only_conservative_structural_relaxation(
    tmp_path: Path,
) -> None:
    source_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="source-candidate.json",
    )
    relaxed_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="relaxed-candidate.json",
        min_notional="4",
        max_market_qty="600",
        max_leverage="20",
    )
    source_rules = _rules(
        tmp_path,
        source_candidate,
        filename="source-rules.json",
    )

    projected = project_demo_rules_to_candidate_subset(
        relaxed_candidate,
        source_rules,
        tmp_path / "projected-rules.json",
        validation_now_ns=NOW_NS + 1,
    )

    assert projected.is_file()


def test_candidate_projection_cannot_retimestamp_stale_evidence(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    output = tmp_path / "projected-rules.json"

    with pytest.raises(ValueError, match="stale or future-dated"):
        project_demo_rules_to_candidate_subset(
            candidate,
            rules,
            output,
            validation_now_ns=(
                NOW_NS + (REGISTERED_MAX_RULE_AGE_SECONDS + 1) * 1_000_000_000
            ),
        )

    assert not output.exists()


def test_coverage_rejects_weakened_rule_freshness_before_sources(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="registered 604800-second maximum"):
        build_candidate_rule_coverage(
            tmp_path / "missing-candidate.json",
            tmp_path / "missing-rules.json",
            max_rule_age_seconds=REGISTERED_MAX_RULE_AGE_SECONDS + 1,
        )


def test_coverage_validation_uses_supplied_source_snapshots(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    loaded = build_candidate_rule_coverage(
        candidate,
        rules,
        created_ts_ns=NOW_NS,
        validation_now_ns=NOW_NS + 1,
        candidate_snapshot=read_stable_file(candidate, label="candidate"),
        demo_rules_snapshot=read_stable_file(rules, label="rules"),
    )
    assert loaded["coverage"]["symbol_source_bound"] is True


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


def test_rollout_refresh_threshold_is_half_the_hard_bound_and_tightens_only(
    tmp_path: Path,
) -> None:
    from liquidity_migration.ops.candidate_rule_coverage import (
        REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS,
        require_registered_rule_age,
    )

    assert (
        REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS
        == REGISTERED_MAX_RULE_AGE_SECONDS // 2
    )
    # The refresh threshold must be a legal (tighter) freshness bound.
    assert (
        require_registered_rule_age(REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS)
        == REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS
    )

    candidate = _candidate(tmp_path)
    rules = _rules(tmp_path, candidate)
    just_before_half_life = (
        NOW_NS + (REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS - 1) * 1_000_000_000
    )
    just_after_half_life = (
        NOW_NS + (REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS + 1) * 1_000_000_000
    )
    assert (
        classify_demo_rule_receipt_freshness(
            rules,
            validation_now_ns=just_before_half_life,
            max_rule_age_seconds=REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS,
        )
        == "fresh"
    )
    # Past half-life the rollout classifier reads "expired" (refresh due)
    # while the hard runtime bound still reads "fresh".
    assert (
        classify_demo_rule_receipt_freshness(
            rules,
            validation_now_ns=just_after_half_life,
            max_rule_age_seconds=REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS,
        )
        == "expired"
    )
    assert (
        classify_demo_rule_receipt_freshness(
            rules,
            validation_now_ns=just_after_half_life,
        )
        == "fresh"
    )


def test_prior_receipt_bound_to_older_candidate_schema_requires_fresh_probe(
    tmp_path: Path,
) -> None:
    # When the prior rules receipt binds a candidate artifact frozen under an
    # older schema, the projection cannot validate the subset relationship
    # against evidence it can no longer load. That structural drift must fall
    # through to the full probe (exit 3), never crash the rollout.
    source_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT", "BBBUSDT"),
        filename="source-candidate.json",
    )
    target_candidate = _candidate_symbols(
        tmp_path,
        ("AAAUSDT",),
        filename="target-candidate.json",
    )
    source_rules = _rules(
        tmp_path,
        source_candidate,
        filename="source-rules.json",
    )
    stale = json.loads(source_candidate.read_text(encoding="utf-8"))
    stale["schema_version"] = stale["schema_version"] - 1
    source_candidate.write_text(json.dumps(stale), encoding="utf-8")
    source_candidate.chmod(0o600)

    with pytest.raises(CandidateRuleRefreshRequired, match="structural drift"):
        project_demo_rules_to_candidate_subset(
            target_candidate,
            source_rules,
            tmp_path / "projected-rules.json",
            validation_now_ns=NOW_NS + 1,
        )
