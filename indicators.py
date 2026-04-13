from __future__ import annotations

import math
from typing import Iterable

import numpy as np


def log_returns(prices: np.ndarray) -> np.ndarray:
    prices = np.asarray(prices, dtype=float)
    if prices.size < 2 or np.any(prices <= 0):
        return np.array([], dtype=float)
    return np.diff(np.log(prices))


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if window <= 0:
        raise ValueError("window must be > 0")
    if values.size < window:
        return np.array([], dtype=float)
    weights = np.ones(window, dtype=float) / window
    return np.convolve(values, weights, mode="valid")


def volatility_adjusted_momentum(
    prices: np.ndarray,
    returns: np.ndarray | None = None,
    lookback: int = 48,
    skip: int = 4,
    min_volatility: float | None = None,
    volatility_floor: float | None = None,
) -> float:
    prices = np.asarray(prices, dtype=float)
    if returns is None:
        returns = log_returns(prices)
    returns = np.asarray(returns, dtype=float)
    floor = min_volatility if min_volatility is not None else volatility_floor
    if floor is None:
        floor = 1e-8
    required = lookback + skip + 1
    if prices.size < required or returns.size < lookback + skip:
        return float("nan")
    vol_slice = returns[-(lookback + skip) : -skip] if skip > 0 else returns[-lookback:]
    if vol_slice.size == 0:
        return float("nan")
    volatility = float(np.std(vol_slice, ddof=1)) if vol_slice.size > 1 else 0.0
    if not math.isfinite(volatility) or volatility < floor:
        volatility = floor
    momentum = float(np.log(prices[-skip] / prices[-(lookback + skip)]))
    return float(momentum / volatility)


def curvature_signal(
    returns: np.ndarray,
    ma_window: int,
    signal_window: int,
) -> float:
    returns = np.asarray(returns, dtype=float)
    smoothed_returns = moving_average(returns, ma_window)
    if smoothed_returns.size < 3:
        return float("nan")
    momentum_slope = np.diff(smoothed_returns)
    if momentum_slope.size < 2:
        return float("nan")
    curvature = np.diff(momentum_slope)
    if curvature.size == 0:
        return float("nan")
    tail = curvature[-signal_window:] if curvature.size >= signal_window else curvature
    return float(np.mean(tail))


def ema(values: np.ndarray, period: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return np.array([], dtype=float)
    alpha = 2.0 / (period + 1.0)
    result = np.empty_like(values, dtype=float)
    result[0] = values[0]
    for idx in range(1, values.size):
        result[idx] = alpha * values[idx] + (1.0 - alpha) * result[idx - 1]
    return result


def realized_volatility(returns: np.ndarray, annualization: float = 365.0) -> float:
    returns = np.asarray(returns, dtype=float)
    if returns.size < 2:
        return float("nan")
    return float(np.std(returns, ddof=1) * math.sqrt(annualization))


def path_efficiency(prices: np.ndarray) -> float:
    prices = np.asarray(prices, dtype=float)
    if prices.size < 2 or np.any(prices <= 0):
        return float("nan")
    returns = log_returns(prices)
    if returns.size == 0:
        return float("nan")
    total_path = float(np.sum(np.abs(returns)))
    if not math.isfinite(total_path) or total_path == 0.0:
        return 0.0
    net_move = abs(float(np.log(prices[-1] / prices[0])))
    return float(np.clip(net_move / total_path, 0.0, 1.0))


def correlation_cluster_labels(
    price_history_by_symbol: dict[str, np.ndarray],
    *,
    lookback_bars: int,
    threshold: float,
) -> dict[str, str]:
    symbols = sorted(price_history_by_symbol)
    if len(symbols) < 2:
        return {symbol: f"solo:{symbol}" for symbol in symbols}
    returns_by_symbol: dict[str, np.ndarray] = {}
    for symbol in symbols:
        prices = np.asarray(price_history_by_symbol[symbol], dtype=float)
        if prices.size < lookback_bars + 1 or np.any(prices <= 0):
            returns_by_symbol[symbol] = np.array([], dtype=float)
            continue
        returns_by_symbol[symbol] = log_returns(prices[-(lookback_bars + 1) :])
    adjacency: dict[str, set[str]] = {symbol: set() for symbol in symbols}
    for idx, left in enumerate(symbols):
        left_returns = returns_by_symbol[left]
        if left_returns.size < 3 or float(np.std(left_returns, ddof=0)) < 1e-12:
            continue
        for right in symbols[idx + 1 :]:
            right_returns = returns_by_symbol[right]
            if (
                right_returns.size != left_returns.size
                or right_returns.size < 3
                or float(np.std(right_returns, ddof=0)) < 1e-12
            ):
                continue
            correlation = float(np.corrcoef(left_returns, right_returns)[0, 1])
            if not math.isfinite(correlation):
                continue
            if correlation >= threshold:
                adjacency[left].add(right)
                adjacency[right].add(left)
    labels: dict[str, str] = {}
    visited: set[str] = set()
    for symbol in symbols:
        if symbol in visited:
            continue
        stack = [symbol]
        component: list[str] = []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            stack.extend(neighbor for neighbor in adjacency[current] if neighbor not in visited)
        component.sort()
        if len(component) == 1:
            labels[component[0]] = f"solo:{component[0]}"
            continue
        leader = component[0]
        label = f"corr:{leader}"
        for member in component:
            labels[member] = label
    return labels


def btc_regime_score(
    btc_daily_closes: np.ndarray,
    vol_lookback: int,
    vol_threshold: float,
) -> int:
    closes = np.asarray(btc_daily_closes, dtype=float)
    if closes.size < 200:
        return 0
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    returns = log_returns(closes[-(vol_lookback + 1) :])
    vol = realized_volatility(returns)
    above_200 = int(closes[-1] > ema200[-1])
    ema_slope = int(ema50[-1] > ema50[-2]) if ema50.size >= 2 else 0
    low_vol = int(math.isfinite(vol) and vol < vol_threshold)
    return above_200 + ema_slope + low_vol


def hurst_exponent(prices: np.ndarray, min_window: int = 8) -> float:
    prices = np.asarray(prices, dtype=float)
    if prices.size < 32 or np.any(prices <= 0):
        return float("nan")

    returns = log_returns(prices)
    if returns.size < 32:
        return float("nan")
    profile = np.cumsum(returns - returns.mean())
    max_window = profile.size // 4
    if max_window <= min_window:
        return float("nan")

    window_sizes = np.unique(np.geomspace(min_window, max_window, num=8).astype(int))
    fluctuation_points: list[tuple[int, float]] = []
    for window in window_sizes:
        chunk_count = profile.size // window
        if chunk_count < 2:
            continue
        samples = profile[: chunk_count * window].reshape(chunk_count, window)
        x = np.arange(window, dtype=float)
        x_centered = x - x.mean()
        denominator = float(np.dot(x_centered, x_centered))
        if denominator == 0.0:
            continue
        sample_means = samples.mean(axis=1, keepdims=True)
        centered_samples = samples - sample_means
        slopes = centered_samples @ x_centered / denominator
        trend = sample_means + (slopes[:, None] * x_centered[None, :])
        residuals = samples - trend
        rms_values = np.sqrt(np.mean(residuals**2, axis=1))
        finite_rms = rms_values[np.isfinite(rms_values) & (rms_values > 0.0)]
        if finite_rms.size > 0:
            fluctuation_points.append((window, float(np.mean(finite_rms))))
    if len(fluctuation_points) < 2:
        return float("nan")
    window_logs = np.log(np.asarray([point[0] for point in fluctuation_points], dtype=float))
    fluctuation_logs = np.log(np.asarray([point[1] for point in fluctuation_points], dtype=float))
    slope, _ = np.polyfit(window_logs, fluctuation_logs, 1)
    return float(np.clip(slope, 0.0, 1.0))


def cross_sectional_zscores(raw_values: dict[str, float]) -> dict[str, float]:
    if not raw_values:
        return {}
    tickers = list(raw_values)
    values = np.asarray([raw_values[ticker] for ticker in tickers], dtype=float)
    if values.size == 1:
        return {tickers[0]: 0.0}
    mean = float(values.mean())
    std = float(values.std(ddof=0))
    if not math.isfinite(std) or std == 0.0:
        return {ticker: 0.0 for ticker in tickers}
    z_values = (values - mean) / std
    return {ticker: float(z) for ticker, z in zip(tickers, z_values)}


def dominance_proxy_series(
    btc_prices: np.ndarray,
    alt_price_vectors: Iterable[np.ndarray],
) -> np.ndarray:
    btc_prices = np.asarray(btc_prices, dtype=float)
    alt_vectors = [np.asarray(prices, dtype=float) for prices in alt_price_vectors]
    if btc_prices.size == 0 or not alt_vectors:
        return np.array([], dtype=float)
    min_len = min([btc_prices.size] + [vector.size for vector in alt_vectors])
    if min_len < 15:
        return np.array([], dtype=float)
    btc_norm = btc_prices[-min_len:] / btc_prices[-min_len]
    alt_matrix = np.vstack([vector[-min_len:] / vector[-min_len] for vector in alt_vectors])
    alt_basket = alt_matrix.mean(axis=0)
    return btc_norm / alt_basket


def dominance_rotation_signal(series: np.ndarray, ema_period: int = 5, lag: int = 10) -> int:
    series = np.asarray(series, dtype=float)
    if series.size < lag + 1:
        return 1
    ema_series = ema(series, ema_period)
    return int(ema_series[-1] < ema_series[-lag])


def dominance_state(
    series: np.ndarray,
    ema_period: int = 5,
    lag: int = 4,
    neutral_threshold_pct: float = 0.002,
) -> tuple[int, float]:
    series = np.asarray(series, dtype=float)
    if series.size < max(lag + 1, ema_period + 1):
        return 0, 0.0
    ema_series = ema(series, ema_period)
    base_value = float(ema_series[-lag])
    if not math.isfinite(base_value) or base_value == 0.0:
        return 0, 0.0
    change_pct = float((ema_series[-1] / base_value) - 1.0)
    if change_pct <= -neutral_threshold_pct:
        return -1, change_pct
    if change_pct >= neutral_threshold_pct:
        return 1, change_pct
    return 0, change_pct


def clip_value(value: float, floor: float, ceiling: float) -> float:
    return float(np.clip(value, floor, ceiling))


def zscore(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return values
    mean = float(values.mean())
    std = float(values.std(ddof=0))
    if not math.isfinite(std) or std == 0.0:
        return np.zeros_like(values, dtype=float)
    return (values - mean) / std
