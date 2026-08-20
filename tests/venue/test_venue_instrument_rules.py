"""B17 — rules from a read-only endpoint, and a probe that refuses off demo."""

from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.venue.account_service_bybit import VerifiedBybitDemoRulesProvider
from liquidity_migration.venue.demo_rule_probe import require_demo_probe_realm
from liquidity_migration.venue.venue_instrument_rules import (
    build_venue_instrument_rules,
    load_venue_rules_bytes,
    render_venue_rules_artifact,
)
from liquidity_migration.core.venue_realm import VenueRealm

REPO = Path(__file__).resolve().parents[2]


def _row(symbol: str, *, min_notional: str = "5", max_leverage: str = "25") -> dict:
    return {
        "symbol": symbol,
        "lotSizeFilter": {
            "qtyStep": "0.1",
            "minOrderQty": "0.1",
            "minNotionalValue": min_notional,
            "maxMktOrderQty": "1000",
        },
        "priceFilter": {"tickSize": "0.01"},
        "leverageFilter": {"maxLeverage": max_leverage},
    }


class _ReadOnlyVenue:
    """Only ``get_instruments_info``: any order call would be an AttributeError."""

    def __init__(self, rows: list[dict], *, demo: bool = False) -> None:
        self.rows = rows
        self.demo = demo
        self.realm = VenueRealm.DEMO if demo else VenueRealm.MAINNET
        self.calls = 0

    def get_instruments_info(self, **_params: object) -> list[dict]:
        self.calls += 1
        return list(self.rows)


def test_rules_are_read_without_placing_a_single_order() -> None:
    venue = _ReadOnlyVenue([_row("BUSDT"), _row("CUSDT"), _row("ZUSDT")])

    rules = build_venue_instrument_rules(
        venue, realm="mainnet", symbols=["BUSDT", "CUSDT"], observed_ts_ns=1_000
    )

    assert venue.calls == 1
    assert sorted(rules) == ["BUSDT", "CUSDT"]
    assert rules["BUSDT"].min_notional == 5.0
    assert rules["BUSDT"].environment == "mainnet"
    assert rules["BUSDT"].source == "bybit_mainnet_instruments_info"


def test_a_void_declared_floor_or_leverage_is_refused() -> None:
    """B7: a non-positive max_leverage silently voids the venue leverage cap."""

    with pytest.raises(RuntimeError, match="declares no maximum leverage"):
        build_venue_instrument_rules(
            _ReadOnlyVenue([_row("BUSDT", max_leverage="0")]),
            realm="mainnet",
            symbols=["BUSDT"],
            observed_ts_ns=1_000,
        )
    with pytest.raises(RuntimeError, match="declares no minimum notional"):
        build_venue_instrument_rules(
            _ReadOnlyVenue([_row("BUSDT", min_notional="0")]),
            realm="mainnet",
            symbols=["BUSDT"],
            observed_ts_ns=1_000,
        )


def test_a_missing_symbol_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="absent from mainnet instruments-info"):
        build_venue_instrument_rules(
            _ReadOnlyVenue([_row("BUSDT")]),
            realm="mainnet",
            symbols=["BUSDT", "MISSINGUSDT"],
            observed_ts_ns=1_000,
        )


def test_a_client_addressing_the_other_realm_is_refused() -> None:
    with pytest.raises(ValueError, match="does not address the mainnet realm"):
        build_venue_instrument_rules(
            _ReadOnlyVenue([_row("BUSDT")], demo=True),
            realm="mainnet",
            symbols=["BUSDT"],
            observed_ts_ns=1_000,
        )


def test_the_artifact_round_trips_and_refuses_the_other_realm() -> None:
    rules = build_venue_instrument_rules(
        _ReadOnlyVenue([_row("BUSDT")]),
        realm="mainnet",
        symbols=["BUSDT"],
        observed_ts_ns=1_000,
    )
    data = render_venue_rules_artifact(rules, realm="mainnet", verified_ts_ns=2_000)

    loaded = load_venue_rules_bytes(data, realm="mainnet")
    assert loaded["BUSDT"] == rules["BUSDT"]

    # Loading a mainnet receipt into a demo owner (or the reverse) is exactly
    # the mislabelling the realm axis exists to prevent.
    with pytest.raises(ValueError, match="is for realm 'mainnet', not 'demo'"):
        load_venue_rules_bytes(data, realm="demo")

    tampered = data.replace(b'"min_notional":5.0', b'"min_notional":0.5')
    assert tampered != data
    with pytest.raises(ValueError, match="artifact_sha256"):
        load_venue_rules_bytes(tampered, realm="mainnet")

    with pytest.raises(ValueError, match="stale or future-dated"):
        load_venue_rules_bytes(
            data, realm="mainnet", now_ns=2_000 + 10**12, max_age_seconds=1.0
        )


def test_the_order_placing_probe_refuses_every_realm_but_demo() -> None:
    class _Client:
        demo = False
        realm = VenueRealm.MAINNET

    with pytest.raises(RuntimeError, match="demo-only; it places live orders"):
        require_demo_probe_realm(_Client())
    with pytest.raises(RuntimeError, match="demo-only; it places live orders"):
        require_demo_probe_realm(_ReadOnlyVenue([], demo=True), realm="mainnet")

    class _DemoClient:
        demo = True
        realm = VenueRealm.DEMO

    require_demo_probe_realm(_DemoClient())


def test_the_rules_provider_refuses_a_rule_from_the_other_realm() -> None:
    rules = build_venue_instrument_rules(
        _ReadOnlyVenue([_row("BUSDT")]),
        realm="mainnet",
        symbols=["BUSDT"],
        observed_ts_ns=1_000,
    )
    VerifiedBybitDemoRulesProvider(rules, environment="mainnet")
    with pytest.raises(ValueError, match="not explicitly verified for demo"):
        VerifiedBybitDemoRulesProvider(rules)


def test_the_deploy_rule_probe_is_gated_by_realm() -> None:
    source = (REPO / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    assert 'the order-placing rule probe is demo-only' in source
    probe_index = source.index("scripts/maintain/probe_bybit_demo_rules.py")
    gate_index = source.index('[ "${DEPLOY_VENUE_REALM:-demo}" = "demo" ]')
    assert gate_index < probe_index


def test_an_optional_symbol_is_skipped_when_absent_or_degenerate() -> None:
    """Held-exposure carryover symbols must not fail the whole rules read.

    A universe symbol stays strict — the universe was frozen from the same
    live venue moments earlier — but a symbol the account merely still holds
    can be gone (settled) or structurally degenerate (a retiring contract),
    and either used to block every renewal (VANRYUSDT, 2026-08-12).
    """

    rules = build_venue_instrument_rules(
        _ReadOnlyVenue([_row("BUSDT"), _row("DYINGUSDT", max_leverage="0")]),
        realm="mainnet",
        symbols=["BUSDT", "GONEUSDT", "DYINGUSDT"],
        observed_ts_ns=1_000,
        optional_symbols=["GONEUSDT", "DYINGUSDT"],
    )
    assert sorted(rules) == ["BUSDT"]

    with pytest.raises(RuntimeError, match="absent from mainnet instruments-info"):
        build_venue_instrument_rules(
            _ReadOnlyVenue([_row("BUSDT")]),
            realm="mainnet",
            symbols=["BUSDT", "GONEUSDT"],
            observed_ts_ns=1_000,
            optional_symbols=["OTHERUSDT"],
        )


def test_held_exposure_declarations_round_trip_and_are_validated() -> None:
    venue = _ReadOnlyVenue([_row("BUSDT"), _row("HELDUSDT")])
    rules = build_venue_instrument_rules(
        venue,
        realm="mainnet",
        symbols=["BUSDT", "HELDUSDT"],
        observed_ts_ns=1_000,
    )

    data = render_venue_rules_artifact(
        rules,
        realm="mainnet",
        verified_ts_ns=2_000,
        held_exposure={"HELDUSDT": {"basis": "live_instruments_info"}},
    )
    loaded = load_venue_rules_bytes(data, realm="mainnet")
    assert sorted(loaded) == ["BUSDT", "HELDUSDT"]

    with pytest.raises(ValueError, match="has no rule in this receipt"):
        render_venue_rules_artifact(
            rules,
            realm="mainnet",
            verified_ts_ns=2_000,
            held_exposure={"GHOSTUSDT": {"basis": "live_instruments_info"}},
        )
    with pytest.raises(ValueError, match="invalid basis"):
        render_venue_rules_artifact(
            rules,
            realm="mainnet",
            verified_ts_ns=2_000,
            held_exposure={"HELDUSDT": {"basis": "wishful_thinking"}},
        )


def test_a_receipt_frozen_before_the_exposure_key_still_loads() -> None:
    rules = build_venue_instrument_rules(
        _ReadOnlyVenue([_row("BUSDT")]),
        realm="mainnet",
        symbols=["BUSDT"],
        observed_ts_ns=1_000,
    )
    data = render_venue_rules_artifact(rules, realm="mainnet", verified_ts_ns=2_000)
    import json

    payload = json.loads(data)
    del payload["held_exposure_symbols"]
    payload["artifact_sha256"] = ""
    import hashlib

    from liquidity_migration.core.deterministic_serialization import canonical_json

    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    loaded = load_venue_rules_bytes(canonical_json(payload) + b"\n", realm="mainnet")
    assert sorted(loaded) == ["BUSDT"]


def test_a_degenerate_tick_or_step_is_refused_or_skipped() -> None:
    """A zero tick cannot round an order or a stop. For a universe symbol the
    freeze fails loud; for an optional (held-exposure) symbol the broken live
    row is skipped so the prior receipt's good rule carries forward instead.
    """

    zero_tick = _row("DYINGUSDT")
    zero_tick["priceFilter"] = {"tickSize": "0"}

    with pytest.raises(RuntimeError, match="no positive tick or qty step"):
        build_venue_instrument_rules(
            _ReadOnlyVenue([_row("BUSDT"), dict(zero_tick)]),
            realm="mainnet",
            symbols=["BUSDT", "DYINGUSDT"],
            observed_ts_ns=1_000,
        )

    rules = build_venue_instrument_rules(
        _ReadOnlyVenue([_row("BUSDT"), dict(zero_tick)]),
        realm="mainnet",
        symbols=["BUSDT", "DYINGUSDT"],
        observed_ts_ns=1_000,
        optional_symbols=["DYINGUSDT"],
    )
    assert sorted(rules) == ["BUSDT"]
