"""Deterministic, finite JSON serialization shared by runtime journals.

Replay-sensitive code hashes and persists these bytes, so normalization and
encoding changes are compatibility changes rather than formatting cleanups.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


__all__ = ["canonical_json", "json_safe"]


def _string_key(pair: tuple[Any, Any]) -> str:
    return str(pair[0])


def json_safe(value: Any) -> Any:
    """Return a deterministic, finite JSON representation.

    Polars/numpy scalars are normalized through ``item`` where available.
    Non-finite floats become ``None`` rather than invalid JSON ``NaN`` tokens.

    The exact-type branches below are a fast path, not a second set of rules:
    each returns what the general chain after them returns for that concrete
    type. They exist because this is the hottest function on the order path --
    a profile counted 2,315 calls per order -- and the general chain reaches
    ``isinstance(value, Mapping)``, where ``Mapping`` is the ``typing`` alias,
    so every dict paid ABC ``__instancecheck__``/``__subclasscheck__``
    machinery. Anything that is not one of these exact types still falls
    through to the original chain unchanged.
    """
    cls = type(value)
    if cls is str or cls is int or cls is bool:
        return value
    if cls is float:
        return value if math.isfinite(value) else None
    if cls is dict:
        items = value.items()
        for key in value:
            if type(key) is not str:
                break
        else:
            # Every key is already a string, so ``str(key)`` is the identity
            # and ``sorted(items)`` orders by exactly the same thing the key
            # function did. Dict keys are unique, so tuple comparison never
            # reaches the values.
            return {key: json_safe(item) for key, item in sorted(items)}
        return {str(key): json_safe(item) for key, item in sorted(items, key=_string_key)}
    if cls is list or cls is tuple:
        return [json_safe(item) for item in value]
    if value is None:
        return value
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in sorted(value.items(), key=_string_key)}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe(item) for item in sorted(value, key=lambda item: str(item))]
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            return json_safe(item_method())
        except (TypeError, ValueError):
            pass
    return str(value)


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Encode one mapping into the repository's canonical JSON byte form."""
    return json.dumps(
        json_safe(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
