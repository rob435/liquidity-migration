from __future__ import annotations

from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe


class _Scalar:
    def __init__(self, value: Any) -> None:
        self.value = value

    def item(self) -> Any:
        return self.value


class _BrokenScalar:
    def item(self) -> Any:
        raise ValueError("not scalar")

    def __str__(self) -> str:
        return "broken-scalar"


def test_canonical_json_preserves_exact_normalization_and_bytes() -> None:
    payload = {
        "z": float("nan"),
        "é": "✓",
        2: _Scalar(7),
        "nested": {"b": float("inf"), "a": (1.25, float("-inf"))},
        "set": {"beta", "alpha"},
    }

    assert canonical_json(payload) == (
        '{"2":7,"nested":{"a":[1.25,null],"b":null},"set":["alpha","beta"],"z":null,"é":"✓"}'
    ).encode("utf-8")


def test_json_safe_normalizes_item_scalars_and_falls_back_to_text() -> None:
    assert json_safe(_Scalar(float("nan"))) is None
    assert json_safe(_BrokenScalar()) == "broken-scalar"
