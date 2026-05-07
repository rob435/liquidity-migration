from __future__ import annotations

from collections.abc import Iterable

import numpy as np


def rank_correlation(x_values: Iterable[float], y_values: Iterable[float]) -> float:
    x = np.asarray(list(x_values), dtype=float)
    y = np.asarray(list(y_values), dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3:
        return float("nan")
    x_rank = _ordinal_rank(x[mask])
    y_rank = _ordinal_rank(y[mask])
    if np.std(x_rank) <= 1e-12 or np.std(y_rank) <= 1e-12:
        return float("nan")
    return float(np.corrcoef(x_rank, y_rank)[0, 1])


def _ordinal_rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, values.size + 1, dtype=float)
    return ranks
