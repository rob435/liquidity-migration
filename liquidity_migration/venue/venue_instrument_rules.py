"""Instrument rules read from the venue's read-only ``get_instruments_info``.

The demo fleet instead derives rules from ``demo_rule_probe``, which submits and
cancels real PostOnly orders to find the empirically accepted minimum notional,
because Bybit's demo realm rejects some orders its own ``minNotionalValue`` says
it should accept. That probe places live orders and refuses any realm but demo,
so off demo the declared ``minNotionalValue`` is taken at face value.

The weaker evidence is recorded as such: the artifact says ``venue_declared``,
and each rule's ``source`` names its endpoint.
Undersized entries are then rejected by the venue at submit rather than
pre-empted locally.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Iterable, Mapping

from liquidity_migration.account.account_contracts import InstrumentRules
from liquidity_migration.venue.account_service_bybit import instrument_rules_from_bybit_row
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.venue_realm import VenueRealm, venue_realm

__all__ = [
    "VENUE_RULES_KIND",
    "VENUE_RULES_SCHEMA_VERSION",
    "build_venue_instrument_rules",
    "candidate_symbol_source",
    "load_venue_rules_bytes",
    "render_venue_rules_artifact",
]

VENUE_RULES_SCHEMA_VERSION = 1
VENUE_RULES_KIND = "bybit_venue_declared_instrument_rules"

#: The evidence standard, stamped into the artifact.
VENUE_RULES_EVIDENCE = "venue_declared"


def _rules_source(realm: VenueRealm) -> str:
    return f"bybit_{realm.value}_instruments_info"


def build_venue_instrument_rules(
    client: Any,
    *,
    realm: VenueRealm | str,
    symbols: Iterable[str],
    observed_ts_ns: int,
    optional_symbols: Iterable[str] = (),
) -> dict[str, InstrumentRules]:
    """Read structural rules for ``symbols`` through the read-only endpoint.

    A symbol named in ``optional_symbols`` whose live row is absent or
    degenerate (a retiring contract can lose its declared floors) is skipped
    instead of failing the whole read. That is only correct for held-exposure
    carryover symbols — a symbol leaving the venue while the account still has
    exposure on it — where the caller supplies the rule from a prior receipt.
    Universe symbols must never be optional: the universe is frozen from the
    same live venue moments earlier, so a fault there is a real fault.
    """

    selected = venue_realm(realm)
    wanted = {str(symbol).upper() for symbol in symbols}
    optional = {str(symbol).upper() for symbol in optional_symbols}
    if not wanted:
        raise ValueError("venue instrument rules require at least one symbol")
    if int(observed_ts_ns) <= 0:
        raise ValueError("venue instrument rules require a positive observation time")
    if bool(getattr(client, "demo", False)) is not (selected is VenueRealm.DEMO):
        raise ValueError(
            f"instrument-rule client does not address the {selected.value} realm"
        )
    rows = {
        str(row.get("symbol") or "").upper(): row
        for row in client.get_instruments_info()
        if isinstance(row, Mapping)
    }
    missing = sorted(wanted - set(rows) - optional)
    if missing:
        raise RuntimeError(
            f"absent from {selected.value} instruments-info: {', '.join(missing)}"
        )
    rules: dict[str, InstrumentRules] = {}
    for symbol in sorted(wanted & set(rows)):
        rule = instrument_rules_from_bybit_row(
            rows[symbol],
            source=_rules_source(selected),
            environment=selected.value,
            observed_ts_ns=int(observed_ts_ns),
        )
        # A zero or absent floor would admit dust orders the venue then rejects
        # at submit, one symbol at a time.
        if not math.isfinite(rule.min_notional) or rule.min_notional <= 0.0:
            if symbol in optional:
                continue
            raise RuntimeError(f"{symbol}: {selected.value} declares no minimum notional")
        # A non-positive max_leverage voids the venue leverage cap downstream.
        if not math.isfinite(rule.max_leverage) or rule.max_leverage <= 0.0:
            if symbol in optional:
                continue
            raise RuntimeError(f"{symbol}: {selected.value} declares no maximum leverage")
        # A non-positive tick or step cannot round an order or a stop; taking
        # such a live row for an optional symbol would also replace a good
        # prior-receipt carryover with a broken rule.
        if (
            not math.isfinite(rule.tick_size)
            or rule.tick_size <= 0.0
            or not math.isfinite(rule.qty_step)
            or rule.qty_step <= 0.0
        ):
            if symbol in optional:
                continue
            raise RuntimeError(
                f"{symbol}: {selected.value} declares no positive tick or qty step"
            )
        rules[symbol] = rule
    return rules


def candidate_symbol_source(candidate: Any, *, size_bytes: int) -> dict[str, Any]:
    """The exact binding shape ``build_candidate_rule_coverage`` re-derives.

    Shared so the freeze script and the coverage check cannot drift into two
    shapes that never compare equal.
    """

    return {
        "kind": "candidate_universe_artifact",
        "path": str(candidate.path),
        "size_bytes": int(size_bytes),
        "sha256": candidate.file_sha256,
        "artifact_sha256": candidate.artifact_sha256,
        "artifact_self_hash_verified": True,
    }


def render_venue_rules_artifact(
    rules: Mapping[str, InstrumentRules],
    *,
    realm: VenueRealm | str,
    verified_ts_ns: int | None = None,
    symbol_source: Mapping[str, Any] | None = None,
    held_exposure: Mapping[str, Mapping[str, Any]] | None = None,
) -> bytes:
    """Serialize one self-describing, hash-bound venue-declared rules receipt.

    ``symbol_source`` binds the receipt to the candidate-universe artifact its
    symbol list came from, so a receipt frozen against a different universe
    cannot be installed as if it covered this one.

    ``held_exposure`` declares the rules that go BEYOND that universe: symbols
    the account still has exposure on after they left the entry population.
    Each entry maps the symbol to its basis — ``live_instruments_info`` for a
    symbol the venue still lists, or ``prior_receipt_carryover`` (with the
    source receipt's hash) for a settled symbol whose structural rule is
    carried forward so its remaining exits can still be built and can die
    properly on the venue's definite reject. The coverage proof accepts
    exactly the declared set and nothing else.
    """

    selected = venue_realm(realm)
    if not rules:
        raise ValueError("venue rules artifact requires at least one rule")
    stamped = int(time.time_ns() if verified_ts_ns is None else verified_ts_ns)
    if stamped <= 0:
        raise ValueError("venue rules artifact requires a positive verified_ts_ns")
    declared_exposure = {
        str(symbol).upper(): dict(entry)
        for symbol, entry in (held_exposure or {}).items()
    }
    for symbol, entry in declared_exposure.items():
        if symbol not in {str(key).upper() for key in rules}:
            raise ValueError(
                f"held-exposure declaration {symbol} has no rule in this receipt"
            )
        if entry.get("basis") not in {"live_instruments_info", "prior_receipt_carryover"}:
            raise ValueError(f"held-exposure declaration {symbol} has an invalid basis")
    for symbol, rule in rules.items():
        if rule.environment != selected.value or rule.symbol != str(symbol).upper():
            raise ValueError(f"rule {symbol} does not belong to realm {selected.value}")
        if rule.source != _rules_source(selected):
            raise ValueError(f"rule {symbol} was not read from instruments-info")
    payload: dict[str, Any] = {
        "schema_version": VENUE_RULES_SCHEMA_VERSION,
        "kind": VENUE_RULES_KIND,
        "status": "passed",
        "environment": selected.value,
        "evidence": VENUE_RULES_EVIDENCE,
        "verified_ts_ns": stamped,
        "symbol_source": dict(symbol_source) if symbol_source is not None else None,
        "held_exposure_symbols": {
            symbol: declared_exposure[symbol] for symbol in sorted(declared_exposure)
        },
        "rules": {
            symbol.upper(): {
                "qty_step": rule.qty_step,
                "min_qty": rule.min_qty,
                "min_notional": rule.min_notional,
                "tick_size": rule.tick_size,
                "max_order_qty": rule.max_order_qty,
                "max_leverage": rule.max_leverage,
                "source": rule.source,
                "environment": rule.environment,
                "observed_ts_ns": rule.observed_ts_ns,
            }
            for symbol, rule in sorted(rules.items())
        },
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return canonical_json(payload) + b"\n"


def load_venue_rules_bytes(
    data: bytes,
    *,
    realm: VenueRealm | str,
    now_ns: int | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, InstrumentRules]:
    """Strictly read one venue-declared rules receipt for an explicit realm."""

    selected = venue_realm(realm)
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("venue rules file is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("venue rules file must contain one object")
    if (
        int(payload.get("schema_version") or 0) != VENUE_RULES_SCHEMA_VERSION
        or payload.get("kind") != VENUE_RULES_KIND
        or payload.get("status") != "passed"
        or payload.get("evidence") != VENUE_RULES_EVIDENCE
    ):
        raise ValueError(
            "venue rules file must be a passed venue-declared receipt with "
            f"schema_version={VENUE_RULES_SCHEMA_VERSION}"
        )
    if payload.get("environment") != selected.value:
        raise ValueError(
            f"venue rules file is for realm {payload.get('environment')!r}, "
            f"not {selected.value!r}"
        )
    observed_hash = str(payload.get("artifact_sha256") or "")
    expected_hash = hashlib.sha256(
        canonical_json({**payload, "artifact_sha256": ""})
    ).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("venue rules file artifact_sha256 is missing or invalid")
    verified_ts_ns = int(payload.get("verified_ts_ns") or 0)
    if verified_ts_ns <= 0:
        raise ValueError("venue rules file requires verified_ts_ns")
    if max_age_seconds is not None:
        current_ns = time.time_ns() if now_ns is None else int(now_ns)
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0.0:
            raise ValueError("max venue rule age must be finite and positive")
        age_ns = current_ns - verified_ts_ns
        if age_ns < 0 or age_ns > max_age_seconds * 1_000_000_000:
            raise ValueError("venue rules receipt is stale or future-dated")
    rows = payload.get("rules")
    if not isinstance(rows, Mapping) or not rows:
        raise ValueError("venue rules file requires a non-empty 'rules' object")
    declared_exposure = payload.get("held_exposure_symbols")
    if declared_exposure is not None:
        # Optional for receipts frozen before 2026-08-13; always written since.
        if not isinstance(declared_exposure, Mapping):
            raise ValueError("venue rules held_exposure_symbols must be an object")
        rule_keys = {str(symbol).upper() for symbol in rows}
        for symbol, entry in declared_exposure.items():
            if (
                str(symbol).upper() not in rule_keys
                or not isinstance(entry, Mapping)
                or entry.get("basis")
                not in {"live_instruments_info", "prior_receipt_carryover"}
            ):
                raise ValueError(
                    f"venue rules held-exposure declaration {symbol} is invalid"
                )
    output: dict[str, InstrumentRules] = {}
    for symbol, raw in rows.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"venue rule {symbol} must be an object")
        normalized = str(symbol).upper()
        rule = InstrumentRules(
            symbol=normalized,
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
        if rule.environment != selected.value or rule.source != _rules_source(selected):
            raise ValueError(f"venue rule {normalized} is not bound to {selected.value}")
        if rule.max_leverage <= 0.0:
            raise ValueError(f"venue rule {normalized} has a void maximum leverage")
        output[normalized] = rule
    return output
