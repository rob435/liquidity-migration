"""The realm contract: named, never inferred, and never reachable by omission."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from liquidity_migration.account.account_route import derive_account_route
from liquidity_migration.account.execution_environment import (
    EXECUTION_ENVIRONMENT_CHOICES,
    ExecutionEnvironment,
    account_id_for_environment,
    execution_environment,
    venue_realm_for_environment,
)
from liquidity_migration.core.venue_realm import (
    REALM_CREDENTIAL_VARIABLES,
    REALM_REST_ENDPOINTS,
    VenueRealm,
    client_venue_realm,
    venue_realm,
)

REPO = Path(__file__).resolve().parents[2]


def test_every_environment_has_an_account_id_and_a_defined_realm() -> None:
    account_ids = {
        member: account_id_for_environment(member) for member in ExecutionEnvironment
    }
    assert len(set(account_ids.values())) == len(ExecutionEnvironment)
    assert account_ids[ExecutionEnvironment.MAINNET] == "bybit-mainnet-unified"
    assert venue_realm_for_environment("demo") is VenueRealm.DEMO
    assert venue_realm_for_environment("mainnet") is VenueRealm.MAINNET


def test_environment_and_realm_parsers_reject_every_fallback() -> None:
    for bogus in ("", None, " ", "live", "real", "prod", "mainet", "paper"):
        with pytest.raises(ValueError):
            execution_environment(bogus)
    for bogus in ("", None, " ", "live", "paper", "prod"):
        with pytest.raises(ValueError):
            venue_realm(bogus)
    assert execution_environment("MAINNET") is ExecutionEnvironment.MAINNET
    assert venue_realm(" Demo ") is VenueRealm.DEMO


def test_each_realm_has_a_distinct_endpoint_and_credential_pair() -> None:
    assert set(REALM_REST_ENDPOINTS) == set(VenueRealm)
    assert len(set(REALM_REST_ENDPOINTS.values())) == len(VenueRealm)
    assert set(REALM_CREDENTIAL_VARIABLES) == set(VenueRealm)
    demo_vars = set(REALM_CREDENTIAL_VARIABLES[VenueRealm.DEMO])
    mainnet_vars = set(REALM_CREDENTIAL_VARIABLES[VenueRealm.MAINNET])
    # Disjoint on purpose: a stale demo key cannot authenticate a mainnet run.
    assert not demo_vars & mainnet_vars


def test_the_durable_route_identity_carries_the_realm(tmp_path: Path) -> None:
    """The mainnet owner necessarily gets its own journal root."""

    roots = {
        environment: derive_account_route(
            account_id=account_id_for_environment(environment),
            environment=environment,
            account_root=tmp_path / "account",
            inbox_root=tmp_path / "inbox",
        )
        for environment in EXECUTION_ENVIRONMENT_CHOICES
    }
    # Same roots, different environments -> different route ids, so
    # ensure_account_route refuses to bind one owner's root to another realm.
    assert len({route.route_id for route in roots.values()}) == len(roots)
    with pytest.raises(Exception, match="exactly one of"):
        derive_account_route(
            account_id="bybit-demo-unified",
            environment="live",
            account_root=tmp_path / "account",
            inbox_root=tmp_path / "inbox",
        )


def test_no_module_restates_the_environment_arity_as_a_literal() -> None:
    """The environment arity is one enum, not a scatter of two-valued literals."""

    offenders: list[str] = []
    for path in sorted((REPO / "liquidity_migration").rglob("*.py")):
        if path.name in {
            "execution_environment.py",
            # Producer choices are asserted precisely by the test below.
            "parsers.py",
        }:
            continue
        source = path.read_text(encoding="utf-8")
        for literal in ('{"demo", "paper"}', '{"paper", "demo"}', '("demo", "paper")'):
            if literal in source:
                offenders.append(f"{path.name}: {literal}")
    assert offenders == []


def test_mainnet_is_a_choice_only_for_partitioned_sleeves_and_never_a_default() -> None:
    """CARRY and LONG may address the mainnet owner; nothing defaults to it. LONG can
    only join because the profile's ``sleeve_limits`` partition means it cannot spend
    CARRY's share. CONTINUOUS is retired and stays ``demo``-only.
    """

    partitioned = {
        "_add_carry_demo_cycle_parser",
        "_add_long_native_event_demo_cycle_parser",
    }
    source = (REPO / "liquidity_migration" / "cli" / "parsers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    environment_choices: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not any(
                isinstance(arg, ast.Constant) and arg.value == "--execution-environment"
                for arg in call.args
            ):
                continue
            for keyword in call.keywords:
                if keyword.arg == "choices":
                    environment_choices.append((node.name, ast.unparse(keyword.value)))
                if keyword.arg == "default":
                    raise AssertionError(
                        f"{node.name} gives --execution-environment a default"
                    )
    assert environment_choices, "no producer exposes --execution-environment"
    assert partitioned <= {name for name, _ in environment_choices}
    for function_name, rendered in environment_choices:
        if function_name in partitioned:
            assert rendered == "EXECUTION_ENVIRONMENT_CHOICES"
        else:
            assert rendered == "('demo',)", (function_name, rendered)


def test_the_owner_runner_defaults_its_realm_to_demo() -> None:
    source = (
        REPO / "liquidity_migration" / "runtime" / "account_service_runner.py"
    ).read_text(encoding="utf-8")
    assert 'default=VenueRealm.DEMO.value' in source
    # Every venue-touching construction must carry the resolved realm rather
    # than a hardcoded demo flag.
    assert 'demo=True,\n' not in source
    assert 'required_rules_environment="demo"' not in source
    assert 'environment="demo",' not in source


class TestClientVenueRealm:
    """What the demo-only constructor fences were replaced with.

    They used to refuse any non-demo client outright, which blocked a mainnet
    account owner from starting at all. What they were actually worth is the
    coherence check: a client whose declared realm and transport disagree would
    act on a different account than the one the caller believes it is holding.
    """

    def test_a_coherent_client_of_either_realm_is_accepted(self) -> None:
        class _Demo:
            demo = True
            realm = "demo"

        class _Mainnet:
            demo = False
            realm = "mainnet"

        assert client_venue_realm(_Demo(), what="t") is VenueRealm.DEMO
        assert client_venue_realm(_Mainnet(), what="t") is VenueRealm.MAINNET

    def test_an_object_with_no_realm_reads_as_demo(self) -> None:
        """Hand-rolled test doubles carry no realm and must stay usable."""

        class _Double:
            demo = True

        assert client_venue_realm(_Double(), what="t") is VenueRealm.DEMO

    @pytest.mark.parametrize(
        ("demo", "realm", "expected"),
        (
            (False, None, "demo"),
            (False, "demo", "demo"),
            (True, "mainnet", "mainnet"),
        ),
    )
    def test_a_transport_contradicting_its_realm_is_refused(
        self, demo: bool, realm: str | None, expected: str
    ) -> None:
        client = type("_C", (), {"demo": demo} | ({} if realm is None else {"realm": realm}))()
        with pytest.raises(ValueError, match=f"contradicts its {expected} realm"):
            client_venue_realm(client, what="t")

    def test_a_testnet_client_is_refused_in_either_realm(self) -> None:
        """Testnet is a third venue with its own book; neither realm is it."""

        class _Testnet:
            demo = True
            realm = "demo"
            testnet = True

        with pytest.raises(ValueError, match="refuses a testnet client"):
            client_venue_realm(_Testnet(), what="t")

    def test_an_unparseable_realm_refuses_rather_than_defaulting(self) -> None:
        class _Bogus:
            demo = True
            realm = "prod"

        with pytest.raises(ValueError, match="explicitly set"):
            client_venue_realm(_Bogus(), what="t")
