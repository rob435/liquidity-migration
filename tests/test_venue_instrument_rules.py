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

REPO = Path(__file__).resolve().parents[1]


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
    with pytest.raises(Exception):
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
