"""Causality tests for the residual-momentum precompute (scripts/precompute_residual_momentum.py).

These pin the fix for the CRITICAL rmom look-ahead (audit 2026-06-03): the residual is fit against a
FORWARD return that only completes ~2 days late, so residual_momentum[D] must be a rolling sum shifted
far enough that its NEWEST term is knowable strictly before the live consumer's earliest decision at
D 00:00 UTC. They fail under the old shift(1).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "precompute_residual_momentum", REPO / "scripts" / "precompute_residual_momentum.py"
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["precompute_residual_momentum"] = m
    spec.loader.exec_module(m)
    return m


MOD = _load()


def _apply(residual_returns: list[float | None]) -> list[float | None]:
    return (
        pl.DataFrame(
            {
                "symbol": ["AAA"] * len(residual_returns),
                "ts_ms": list(range(len(residual_returns))),
                "residual_return": residual_returns,
            }
        )
        .with_columns(MOD.residual_momentum_expr())["residual_momentum"]
        .to_list()
    )


def test_residual_momentum_is_causal_shift3() -> None:
    """residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)."""
    assert (MOD.RMOM_WINDOW, MOD.RMOM_CAUSAL_SHIFT) == (7, 3)
    rr = [float(i) for i in range(16)]  # residual_return[i] = i
    out = _apply(rr)
    assert out[12] == float(sum(range(3, 10)))   # D=12 -> sum(3..9) = 42
    assert out[15] == float(sum(range(6, 13)))   # D=15 -> sum(6..12) = 63


def test_residual_momentum_does_not_read_future_residuals() -> None:
    """Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d in
    {D-2, D-1, D} — those forward residuals only complete AFTER D's decision. (Fails under shift(1),
    which summed residual_return[D-1].)"""
    n = 16
    d_decision = 12
    base = [float(i) for i in range(n)]
    poisoned = list(base)
    for d in (d_decision - 2, d_decision - 1, d_decision):
        poisoned[d] = 1e9  # a value not knowable at D's decision
    assert _apply(base)[d_decision] == _apply(poisoned)[d_decision]
