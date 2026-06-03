"""Unit tests for liquidity_migration._common shared coercion helpers."""

from __future__ import annotations


def test_coerce_int_matches_legacy_int_helper_behaviour() -> None:
    """quality-dup-9: the shared coerce_int reproduces the trivial parse that
    reconciliation._int and ws_risk._int each used to define locally -- int on
    success, default (0) on TypeError/ValueError, truncation for float-ish input."""
    from liquidity_migration._common import coerce_int
    from liquidity_migration.reconciliation import _int as recon_int
    from liquidity_migration.ws_risk import _int as ws_int

    cases = ["5", 5, 5.9, "  7  ", None, "", "abc", [], {}]
    for value in cases:
        try:
            expected = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            expected = 0
        assert coerce_int(value) == expected
        assert recon_int(value) == expected
        assert ws_int(value) == expected
    assert coerce_int("nope", default=-1) == -1
