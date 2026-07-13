"""Deterministic, finite JSON serialization shared by runtime journals.

Replay-sensitive code hashes and persists these bytes, so normalization and
encoding changes are compatibility changes rather than formatting cleanups.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


__all__ = ["canonical_json", "json_safe"]


def json_safe(value: Any) -> Any:
    """Return a deterministic, finite JSON representation.

    Polars/numpy scalars are normalized through ``item`` where available.
    Non-finite floats become ``None`` rather than invalid JSON ``NaN`` tokens.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
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
