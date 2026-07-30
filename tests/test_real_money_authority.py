"""The real-money authority receipt has to be issuable, and hard to issue.

The constants for this profile existed before this file did, but the issuance
path had never been walked: ``_parse_environment_snapshots`` raised ``KeyError``
on the mainnet environment names, ``demo_raw_value`` was ``None`` so the
raw-persistence check could never pass, and the root/input filename sets fell
back to "every file this module knows about" — which silently became wrong the
moment a third profile with a different file set existed. Authority that cannot
be issued is not a safety property; it is an untested path that will be
discovered at the worst moment.

So these tests do two jobs at once: prove a correct mainnet environment is
accepted, and prove each individual weakening is refused. The second half is
the point. A test suite that only proved the happy path would have passed
against the version that could not issue at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import liquidity_migration.operational_runtime_authority as runtime_authority
from liquidity_migration.account_candidate_universe import (
    build_candidate_universe_artifact,
    load_candidate_universe,
    write_candidate_universe,
)
from liquidity_migration.artifact_snapshot import read_stable_file
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.venue_instrument_rules import (
    candidate_symbol_source,
    render_venue_rules_artifact,
)
from liquidity_migration.account_contracts import InstrumentRules

REPO = Path(__file__).resolve().parents[1]
NOW_NS = 1_800_000_000_000_000_000
SYMBOLS = ("AAAUSDT", "BBBUSDT")


def _candidate(tmp_path: Path) -> Path:
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
            "priceFilter": {"tickSize": "0.1"},
            "leverageFilter": {"maxLeverage": "10"},
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
        for symbol in SYMBOLS
    ]
    tickers = [
        {"symbol": symbol, "lastPrice": "10", "turnover24h": str(3_000_000 - index)}
        for index, symbol in enumerate(SYMBOLS)
    ]
    payload = build_candidate_universe_artifact(
        instruments,
        tickers,
        snapshot_ts_ns=NOW_NS,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
        realm="mainnet",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = write_candidate_universe(tmp_path / "candidate-universe.json", payload)
    path.chmod(0o600)
    return path


def _venue_rules(tmp_path: Path, candidate_path: Path, *, realm: str = "mainnet") -> Path:
    snapshot = read_stable_file(candidate_path, label="candidate", require_single_link=False)
    candidate = load_candidate_universe(candidate_path, snapshot=snapshot, realm="mainnet")
    rules = {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=0.01,
            min_qty=0.01,
            min_notional=5.0,
            tick_size=0.1,
            max_order_qty=500.0,
            max_leverage=10.0,
            source=f"bybit_{realm}_instruments_info",
            environment=realm,
            observed_ts_ns=NOW_NS,
        )
        for symbol in candidate.symbols
    }
    data = render_venue_rules_artifact(
        rules,
        realm=realm,
        verified_ts_ns=NOW_NS,
        symbol_source=candidate_symbol_source(candidate, size_bytes=snapshot.size),
    )
    path = tmp_path / f"venue-rules-{realm}.json"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _profile(tmp_path: Path, *, partitioned: bool = True) -> Path:
    payload = json.loads((REPO / "configs" / "operational.mainnet.json").read_text())
    if not partitioned:
        del payload["account_risk"]["sleeve_limits"]
        # Without a partition the producers must fit the shared envelope, which
        # they already do -- so this is a profile that loads cleanly and is
        # refused only because real money requires a partition.
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "risk-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _values(tmp_path: Path) -> dict[str, dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidate = _candidate(tmp_path)
    rules = _venue_rules(tmp_path, candidate)
    profile = _profile(tmp_path)
    roots = {}
    for name in ("account", "inbox", "capture"):
        root = tmp_path / f"root-{name}"
        root.mkdir()
        roots[name] = root
    values = {
        "account-execution-mainnet.env": {
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED": "1",
            "ACCOUNT_VENUE_REALM": "mainnet",
            "ACCOUNT_RAW_MARKET_PERSISTENCE": "0",
            "ACCOUNT_EXECUTION_ROOT": str(roots["account"]),
            "ACCOUNT_INTENT_INBOX_ROOT": str(roots["inbox"]),
            "ACCOUNT_CAPTURE_ROOT": str(roots["capture"]),
            "STRATEGY_TARGET_CAPTURE_PATH": str(roots["capture"] / "strategy-targets.jsonl"),
            "ACCOUNT_SYMBOLS_FILE": str(candidate),
            "CANDIDATE_UNIVERSE_FILE": str(candidate),
            "ACCOUNT_DEMO_RULES_FILE": str(rules),
            "ACCOUNT_RISK_POLICY_FILE": str(profile),
        },
        "bybit-mainnet.env": {
            "BYBIT_REAL_API_KEY": "placeholder-not-a-real-key",
            "BYBIT_REAL_API_SECRET": "placeholder-not-a-real-secret",
            "REAL_MONEY": "true",
        },
        "sleeves.resolved.env": {
            "LONG_SLEEVE": "off",
            "CONTINUOUS_SLEEVE": "off",
            "CONTINUOUS_PAPER_SLEEVE": "off",
            "CARRY_SLEEVE": "off",
            "CARRY_PAPER_SLEEVE": "off",
            "CONTINUOUS_HEDGE_TIMER": "off",
            "CARRY_MAINNET_SLEEVE": "on",
            "LONG_MAINNET_SLEEVE": "on",
        },
    }
    return values


@pytest.fixture(autouse=True)
def _fresh_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "liquidity_migration.candidate_rule_coverage.time.time_ns", lambda: NOW_NS + 1
    )


def _validate(values: dict) -> tuple[dict, dict]:
    return runtime_authority._validate_environments(
        values, profile=runtime_authority.REAL_MONEY_PROFILE
    )


# --------------------------------------------------------------------------
# The path exists at all
# --------------------------------------------------------------------------


def test_the_mainnet_environment_names_resolve_to_real_paths() -> None:
    """This raised KeyError before; the profile could never be issued."""

    names = runtime_authority._PROFILE_ENVIRONMENT_NAMES[runtime_authority.REAL_MONEY_PROFILE]
    known = {
        path.name
        for path in (
            *runtime_authority.REQUIRED_ENVIRONMENT_PATHS,
            *runtime_authority.REAL_MONEY_ENVIRONMENT_PATHS,
        )
    }
    assert set(names) <= known
    # Disjoint from the demo credential file, like the variables it carries.
    assert "bybit-demo.env" not in names
    assert "account-execution.env" not in names


def test_a_correct_mainnet_environment_validates(tmp_path: Path) -> None:
    root_identities, inputs = _validate(_values(tmp_path))

    assert set(root_identities) == {
        "account-execution-mainnet.env:ACCOUNT_EXECUTION_ROOT",
        "account-execution-mainnet.env:ACCOUNT_INTENT_INBOX_ROOT",
        "account-execution-mainnet.env:ACCOUNT_CAPTURE_ROOT",
    }
    # No demo or paper file is read for this profile.
    assert all(name.startswith("account-execution-mainnet.env:") for name in inputs)


def test_the_profile_scope_and_units_name_both_funded_sleeves() -> None:
    fields = runtime_authority._PROFILE_FIELDS[runtime_authority.REAL_MONEY_PROFILE]
    assert fields["demo_raw_value"] == "0", "None could never match any env value"
    assert "carry_and_long" in fields["scope"]
    assert set(fields["authorized_units"]) == {
        "liquidity-migration-account-execution-mainnet.service",
        "liquidity-migration-bybit-carry-mainnet.service",
        "liquidity-migration-bybit-long-mainnet.service",
    }
    # The acknowledgement must not claim a narrower scope than it grants.
    assert "CARRY_AND_LONG" in runtime_authority.REAL_MONEY_OWNER_ACKNOWLEDGEMENT
    assert (
        runtime_authority.REAL_MONEY_OWNER_ACKNOWLEDGEMENT
        != runtime_authority.OWNER_ACKNOWLEDGEMENT
    )


# --------------------------------------------------------------------------
# Each weakening is refused
# --------------------------------------------------------------------------


def test_real_money_requires_the_switch_to_be_armed(tmp_path: Path) -> None:
    """A receipt issued against a disarmed environment can never be satisfied."""

    values = _values(tmp_path)
    values["bybit-mainnet.env"]["REAL_MONEY"] = "false"
    with pytest.raises(ValueError, match="must set REAL_MONEY=true"):
        _validate(values)


def test_an_ambiguous_arming_value_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["bybit-mainnet.env"]["REAL_MONEY"] = "yes-please"
    with pytest.raises(RuntimeError, match="not a recognised boolean"):
        _validate(values)


def test_mainnet_credentials_are_required(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["bybit-mainnet.env"]["BYBIT_REAL_API_SECRET"] = ""
    with pytest.raises(ValueError, match="mainnet credentials are missing"):
        _validate(values)


def test_a_demo_key_in_the_mainnet_file_is_refused(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["bybit-mainnet.env"]["BYBIT_DEMO_API_KEY"] = "leftover"
    with pytest.raises(ValueError, match="must not contain demo credentials"):
        _validate(values)


def test_the_environment_must_declare_the_mainnet_realm(tmp_path: Path) -> None:
    """A mainnet receipt must not be able to cover a demo-realm run."""

    values = _values(tmp_path)
    values["account-execution-mainnet.env"]["ACCOUNT_VENUE_REALM"] = "demo"
    with pytest.raises(ValueError, match="must set ACCOUNT_VENUE_REALM=mainnet"):
        _validate(values)


def test_an_unpartitioned_profile_is_outside_what_this_authorization_means(
    tmp_path: Path,
) -> None:
    """B3. Without a partition, one sleeve can spend the other's capital."""

    values = _values(tmp_path)
    values["account-execution-mainnet.env"]["ACCOUNT_RISK_POLICY_FILE"] = str(
        _profile(tmp_path / "unpartitioned", partitioned=False)
    )
    with pytest.raises(ValueError, match="requires account_risk.sleeve_limits"):
        _validate(values)


def test_a_demo_rules_receipt_cannot_cover_a_mainnet_owner(tmp_path: Path) -> None:
    """B17/B11: the artifact names its realm and the loader refuses the other."""

    candidate = tmp_path / "candidate-universe.json"
    assert _candidate(tmp_path) == candidate
    demo_rules = _venue_rules(tmp_path, candidate, realm="demo")
    values = _values(tmp_path / "second")
    values["account-execution-mainnet.env"]["ACCOUNT_DEMO_RULES_FILE"] = str(demo_rules)
    with pytest.raises(ValueError, match="is for realm 'demo'"):
        _validate(values)


def test_the_mainnet_sleeve_toggles_must_be_stated(tmp_path: Path) -> None:
    values = _values(tmp_path)
    values["sleeves.resolved.env"]["CARRY_MAINNET_SLEEVE"] = ""
    with pytest.raises(ValueError, match="invalid CARRY_MAINNET_SLEEVE"):
        _validate(values)


def test_a_demo_profile_refuses_a_mainnet_sleeve_that_is_switched_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mainnet producer must not run under demo authority."""

    monkeypatch.setattr(runtime_authority, "_paper_user_id", os.geteuid)
    values = _values(tmp_path)
    demo_owner = dict(values["account-execution-mainnet.env"])
    demo_owner["ACCOUNT_VENUE_REALM"] = "demo"
    demo_owner["ACCOUNT_LIVENESS_SCOPE"] = "demo"
    demo_values = {
        "account-execution.env": demo_owner,
        "bybit-demo.env": {
            "BYBIT_DEMO_API_KEY": "demo",
            "BYBIT_DEMO_API_SECRET": "secret",
            "REAL_MONEY": "false",
        },
        "sleeves.resolved.env": {
            **values["sleeves.resolved.env"],
            "CARRY_MAINNET_SLEEVE": "on",
            "LONG_MAINNET_SLEEVE": "off",
        },
    }
    with pytest.raises(ValueError, match="require CARRY_MAINNET_SLEEVE=off"):
        runtime_authority._validate_environments(
            demo_values, profile=runtime_authority.DEMO_OPERATIONAL_PROFILE
        )


def test_a_demo_profile_still_validates_without_the_new_sleeve_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-resolved sleeve file predating these keys keeps working."""

    monkeypatch.setattr(runtime_authority, "_paper_user_id", os.geteuid)
    values = _values(tmp_path)
    demo_owner = dict(values["account-execution-mainnet.env"])
    demo_owner["ACCOUNT_VENUE_REALM"] = "demo"
    demo_owner["ACCOUNT_LIVENESS_SCOPE"] = "demo"
    sleeves = {
        key: value
        for key, value in values["sleeves.resolved.env"].items()
        if not key.endswith("MAINNET_SLEEVE")
    }
    demo_values = {
        "account-execution.env": demo_owner,
        "bybit-demo.env": {
            "BYBIT_DEMO_API_KEY": "demo",
            "BYBIT_DEMO_API_SECRET": "secret",
            "REAL_MONEY": "false",
        },
        "sleeves.resolved.env": sleeves,
    }
    # The demo rules artifact belongs to the mainnet realm here, so this must
    # fail on the *rules realm*, having got past the sleeve check.
    with pytest.raises(ValueError, match="is for realm 'mainnet'"):
        runtime_authority._validate_environments(
            demo_values, profile=runtime_authority.DEMO_OPERATIONAL_PROFILE
        )


def test_issuance_still_requires_a_ceiling_and_an_expiry() -> None:
    """Unchanged by the LONG addition, and worth re-pinning next to it."""

    with pytest.raises(ValueError, match="capital_ceiling"):
        runtime_authority._validate_real_money_ceiling(None)
    with pytest.raises(ValueError, match="cannot exceed 2 in equity-multiple mode"):
        runtime_authority._validate_real_money_ceiling(
            {"mode": "account_equity_multiple", "value": 3.0}
        )
    assert runtime_authority.REAL_MONEY_MAX_AUTHORITY_SECONDS <= 30 * 24 * 60 * 60
