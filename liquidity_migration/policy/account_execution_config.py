"""Shared, venue-mutation-free account rules and risk-policy loaders."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liquidity_migration.account.account_contracts import (
    AccountRiskPolicy,
    InstrumentRules,
)
from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file
from liquidity_migration.venue.account_service_bybit import VerifiedBybitDemoRulesProvider
from liquidity_migration.venue.demo_rule_probe import (
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    require_registered_demo_rule_probe_parameters,
    validate_demo_rule_probe_evidence,
)
from liquidity_migration.core.deterministic_serialization import canonical_json


REGISTERED_MAX_DEMO_RULE_AGE_HOURS = 168.0


def require_registered_demo_rule_max_age_hours(
    value: object,
    *,
    enforce_registered_ceiling: bool = True,
) -> float:
    """Validate the instrument-rule receipt age bound.

    Mainnet holds the registered 168-hour ceiling. Demo only needs a
    finite positive value: with a stale receipt and a probe that will not run,
    a hard ceiling leaves no way to start the owner short of a code deploy.
    """

    try:
        hours = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("max demo rule age must be numeric") from exc
    if not math.isfinite(hours) or hours <= 0.0:
        raise ValueError("max demo rule age must be finite and positive")
    if enforce_registered_ceiling and hours > REGISTERED_MAX_DEMO_RULE_AGE_HOURS:
        raise ValueError("max demo rule age cannot exceed the registered 168 hours")
    return hours


def _load_json_bytes(data: bytes, *, label: str) -> Mapping[str, Any]:
    payload = json.loads(data)
    if not isinstance(payload, Mapping):
        raise ValueError(f"configuration must be a JSON object: {label}")
    return payload


def load_demo_rules_bytes(
    data: bytes,
    *,
    now_ns: int | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, InstrumentRules]:
    payload = _load_json_bytes(data, label="demo rules")
    if (
        int(payload.get("schema_version") or 0) != DEMO_RULES_SCHEMA_VERSION
        or payload.get("kind") != DEMO_RULES_KIND
        or payload.get("status") != "passed"
        or payload.get("environment") != "demo"
    ):
        raise ValueError(
            "demo rules file must be a passed source-bound demo-rule receipt "
            f"with schema_version={DEMO_RULES_SCHEMA_VERSION}"
        )
    max_probe_notional_usdt, _, _ = require_registered_demo_rule_probe_parameters(
        max_probe_notional_usdt=payload.get("max_probe_notional_usdt"),
        probe_distance_bps=payload.get("probe_distance_bps"),
        max_private_requests_per_second=payload.get(
            "max_private_requests_per_second"
        ),
    )
    observed_hash = str(payload.get("artifact_sha256") or "")
    expected_hash = hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("demo rules file artifact_sha256 is missing or invalid")
    verified_ts_ns = int(payload.get("verified_ts_ns") or 0)
    if verified_ts_ns <= 0:
        raise ValueError("demo rules file requires verified_ts_ns")
    if max_age_seconds is not None:
        current_ns = time.time_ns() if now_ns is None else now_ns
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0.0:
            raise ValueError("max demo rule age must be finite and positive")
        age_ns = current_ns - verified_ts_ns
        if age_ns < 0 or age_ns > max_age_seconds * 1_000_000_000:
            raise ValueError("demo rules receipt is stale or future-dated")
    rows = payload.get("rules")
    if not isinstance(rows, Mapping) or not rows:
        raise ValueError("demo rules file requires a non-empty 'rules' object")
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("demo rules file requires per-symbol probe evidence")
    rule_symbols = [str(symbol).upper() for symbol in rows]
    evidence_symbols = [str(symbol).upper() for symbol in evidence]
    if (
        len(set(rule_symbols)) != len(rule_symbols)
        or len(set(evidence_symbols)) != len(evidence_symbols)
        or sorted(rule_symbols) != sorted(evidence_symbols)
    ):
        raise ValueError("demo rules and probe evidence must have exact one-to-one symbol coverage")
    output: dict[str, InstrumentRules] = {}
    for symbol, raw in rows.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"demo rule {symbol} must be an object")
        normalized_symbol = str(symbol).upper()
        receipt = evidence.get(symbol) or evidence.get(normalized_symbol)
        if not isinstance(receipt, Mapping):
            raise ValueError(f"demo rule {normalized_symbol} lacks probe evidence")
        rule = InstrumentRules(
            symbol=normalized_symbol,
            qty_step=float(raw["qty_step"]),
            min_qty=float(raw["min_qty"]),
            min_notional=float(raw["min_notional"]),
            tick_size=float(raw["tick_size"]),
            max_order_qty=float(raw.get("max_order_qty") or 0.0),
            max_leverage=float(raw.get("max_leverage") or 0.0),
            source=str(raw.get("source") or ""),
            environment=str(raw.get("environment") or ""),
            observed_ts_ns=int(raw.get("observed_ts_ns") or 0),
        )
        accepted_notional = validate_demo_rule_probe_evidence(
            receipt,
            expected_symbol=normalized_symbol,
            expected_observed_ts_ns=verified_ts_ns,
            expected_rules=rule,
            max_probe_notional_usdt=max_probe_notional_usdt,
        )
        if abs(rule.min_notional - accepted_notional) > max(1e-12, accepted_notional * 1e-12):
            raise ValueError(f"demo rule {normalized_symbol} minimum does not match acceptance evidence")
        if rule.observed_ts_ns != verified_ts_ns:
            raise ValueError(f"demo rule {normalized_symbol} timestamp does not match receipt")
        if rule.source != "bybit_demo_post_only_acceptance_probe":
            raise ValueError(f"demo rule {normalized_symbol} source is not the terminal demo probe")
        output[normalized_symbol] = rule
    VerifiedBybitDemoRulesProvider(output)  # validate every row before returning
    return output


def load_demo_rules(
    path: str | Path,
    *,
    now_ns: int | None = None,
    max_age_seconds: float | None = None,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, InstrumentRules]:
    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label="demo rules",
            require_single_link=False,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("demo rules snapshot path differs")
    return load_demo_rules_bytes(
        snapshot.data,
        now_ns=now_ns,
        max_age_seconds=max_age_seconds,
    )


def load_risk_policy_bytes(data: bytes) -> AccountRiskPolicy:
    payload = _load_json_bytes(data, label="risk policy")
    if payload.get("kind") == "liquidity_migration_operational_profile":
        # Producers and owner share one profile. The flat policy shape stays
        # readable for isolated tools/tests.
        from liquidity_migration.policy.operational_profile import load_operational_profile_bytes  # noqa: PLC0415

        return load_operational_profile_bytes(data).account_risk.to_policy()
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=float(payload["max_component_gross_notional_usdt"]),
        max_account_gross_notional_usdt=float(payload["max_account_gross_notional_usdt"]),
        max_symbol_notional_usdt=float(payload["max_symbol_notional_usdt"]),
        max_initial_margin_usdt=float(payload["max_initial_margin_usdt"]),
        max_leverage=float(payload["max_leverage"]),
        quantity_tolerance=float(payload.get("quantity_tolerance") or 1e-12),
        max_daily_loss_usdt=float(payload.get("max_daily_loss_usdt") or 0.0),
    )


def load_risk_policy(path: str | Path) -> AccountRiskPolicy:
    snapshot = read_stable_file(
        path,
        label="risk policy",
        require_single_link=False,
    )
    return load_risk_policy_bytes(snapshot.data)
