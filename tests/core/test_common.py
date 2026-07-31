"""Unit tests for liquidity_migration.core._common shared coercion helpers."""

from __future__ import annotations


def test_coerce_int_matches_legacy_int_helper_behaviour() -> None:
    """The shared helper accepts integer-like values and falls back safely."""
    from liquidity_migration.core._common import coerce_int

    cases = ["5", 5, 5.9, "  7  ", None, "", "abc", [], {}]
    for value in cases:
        try:
            expected = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            expected = 0
        assert coerce_int(value) == expected
    assert coerce_int("nope", default=-1) == -1
