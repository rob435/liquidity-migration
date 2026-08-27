"""The realm contract: named, never inferred, and never reachable by omission."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from liquidity_migration.policy.execution_environment import (
    ExecutionEnvironment,
    account_id_for_environment,
    candidate_universe_realm,
    execution_environment,
)
from liquidity_migration.core.venue_realm import (
    REALM_REST_ENDPOINTS,
    VenueRealm,
    venue_realm,
)

REPO = Path(__file__).resolve().parents[2]


def test_every_environment_has_an_account_id_and_a_defined_realm() -> None:
    account_ids = {
        member: account_id_for_environment(member) for member in ExecutionEnvironment
    }
    assert len(set(account_ids.values())) == len(ExecutionEnvironment)
    assert account_ids[ExecutionEnvironment.MAINNET] == "bybit-mainnet-unified"
    assert candidate_universe_realm("demo") is VenueRealm.DEMO
    assert candidate_universe_realm("mainnet") is VenueRealm.MAINNET


def test_environment_and_realm_parsers_reject_every_fallback() -> None:
    for bogus in ("", None, " ", "live", "real", "prod", "mainet", "paper"):
        with pytest.raises(ValueError):
            execution_environment(bogus)
    for bogus in ("", None, " ", "live", "paper", "prod"):
        with pytest.raises(ValueError):
            venue_realm(bogus)
    assert execution_environment("MAINNET") is ExecutionEnvironment.MAINNET
    assert venue_realm(" Demo ") is VenueRealm.DEMO


def test_each_realm_has_a_distinct_public_endpoint() -> None:
    assert set(REALM_REST_ENDPOINTS) == set(VenueRealm)
    assert len(set(REALM_REST_ENDPOINTS.values())) == len(VenueRealm)


def test_mainnet_is_a_choice_only_for_the_funded_sleeves_and_never_a_default() -> None:
    """CARRY and LONG may address mainnet; nothing defaults to it.

    CONTINUOUS is retired and stays ``demo``-only.
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


