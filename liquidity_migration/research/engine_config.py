"""Render strategy plug blocks for the Rust execution engine.

This is the research system's half of the plug seam described in
docs/engine.md: research decides parameters, renders them as ``[[strategy]]``
TOML blocks, and the engine loads them by name. Nothing else crosses the
wall, so this module is deliberately plain: dataclass in, TOML text out,
no engine imports, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_SCALAR_TYPES = (str, bool, int, float)

#: Keys the engine core reads out of a ``[[strategy]]`` block itself.
_ENGINE_KEYS = ("name", "sleeve", "book_path")


@dataclass(frozen=True)
class EngineStrategyBlock:
    """One strategy plug: the engine builds ``name`` from ``params``."""

    name: str
    params: dict[str, str | bool | int | float] = field(default_factory=dict)


def render_engine_strategies(blocks: list[EngineStrategyBlock]) -> str:
    """Render blocks as deterministic TOML (params sorted by key)."""
    if not blocks:
        raise ValueError("no strategy blocks to render")
    parts: list[str] = []
    for block in blocks:
        parts.append(_render_block(block))
    return "\n".join(parts)


def _render_block(block: EngineStrategyBlock) -> str:
    if not block.name or not block.name.replace("_", "").isalnum():
        raise ValueError(f"strategy name {block.name!r} is not a plain identifier")
    # Flat blocks: the engine keeps these keys for itself and hands every other
    # one to the strategy, so they are reserved here.
    lines = [
        "[[strategy]]",
        f'name = "{block.name}"',
    ]
    for key in sorted(block.params):
        if key in _ENGINE_KEYS:
            raise ValueError(f"{block.name}: param key {key!r} is reserved for the engine")
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"{block.name}: param key {key!r} is not a plain identifier")
        lines.append(f"{key} = {_render_value(block.params[key], key, block.name)}")
    lines.append("")
    return "\n".join(lines)


def _render_value(value: str | bool | int | float, key: str, strategy: str) -> str:
    # bool checked before int: Python bools are ints.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{strategy}: param {key!r} is not a finite number")
        rendered = repr(value)
        return rendered if any(c in rendered for c in ".e") else f"{rendered}.0"
    if isinstance(value, str):
        if any(c in value for c in '"\\\n'):
            raise ValueError(f"{strategy}: param {key!r} contains characters TOML quoting would mangle")
        return f'"{value}"'
    raise ValueError(f"{strategy}: param {key!r} has unsupported type {type(value).__name__}")
