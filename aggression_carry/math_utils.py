from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np


def ema(values: Iterable[float], span: int) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return arr
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(arr, dtype=float)
    prev = arr[0]
    out[0] = prev
    for index in range(1, arr.size):
        value = arr[index]
        if not math.isfinite(value):
            value = prev
        prev = alpha * value + (1.0 - alpha) * prev
        out[index] = prev
    return out


def rolling_median(values: Iterable[float], window: int, *, min_periods: int = 1) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    out = np.full(arr.shape, np.nan, dtype=float)
    for index in range(arr.size):
        start = max(0, index + 1 - window)
        sample = arr[start : index + 1]
        sample = sample[np.isfinite(sample)]
        if sample.size >= min_periods:
            out[index] = float(np.median(sample))
    return out


def robust_z(values: Iterable[float], *, clip: float = 3.0) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    out = np.full(arr.shape, np.nan, dtype=float)
    mask = np.isfinite(arr)
    if not mask.any():
        return out
    finite = arr[mask]
    med = float(np.median(finite))
    mad = float(np.median(np.abs(finite - med)))
    if mad <= 1e-12:
        out[mask] = 0.0
        return out
    out[mask] = np.clip(0.6745 * (finite - med) / mad, -clip, clip)
    return out


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
