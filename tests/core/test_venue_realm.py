"""The realm contract: named, never inferred, and never reachable by omission."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from liquidity_migration.core.venue_realm import (
    REALM_REST_ENDPOINTS,
    VenueRealm,
    venue_realm,
)

REPO = Path(__file__).resolve().parents[2]


def test_realm_parser_rejects_every_fallback() -> None:
    for bogus in ("", None, " ", "live", "paper", "prod"):
        with pytest.raises(ValueError):
            venue_realm(bogus)
    assert venue_realm(" Demo ") is VenueRealm.DEMO


def test_each_realm_has_a_distinct_public_endpoint() -> None:
    assert set(REALM_REST_ENDPOINTS) == set(VenueRealm)
    assert len(set(REALM_REST_ENDPOINTS.values())) == len(VenueRealm)


def test_top_level_python_cli_has_no_execution_realm_route() -> None:
    source = (REPO / "liquidity_migration" / "cli" / "parsers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    environment_flags: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not any(isinstance(arg, ast.Constant) and arg.value == "--execution-environment" for arg in call.args):
                continue
            environment_flags.append(node.name)
    assert environment_flags == []
