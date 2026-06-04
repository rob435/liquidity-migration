"""Causality + behavior tests for the BTC-30d-trend regime gate.

The gate (`VolumeEventResearchConfig.btc_trend_gate`) conditions the deployed SHORT
fade's SELECTION on BTC's trailing-30d return, lagged one day. These tests pin the two
things that matter: the feature is strictly causal (no look-ahead — appending future
bars cannot change a past value), and the filter partitions entries by regime exactly
as specified. Pre-reg: docs/preregistration/2026-06-04-btc-trend-regime-gate.md.
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.volume_events import (
    VolumeEventResearchConfig,
    _apply_market_context_filters,
    _attach_market_context,
    _validate_event_config,
)


def _btc_frame(returns: list[float], *, symbol: str = "BTCUSDT") -> pl.DataFrame:
    """A contiguous daily BTC series: ts_ms on a 1-day grid, daily_return_1d = returns."""
    n = len(returns)
    ts = [(i + 1) * MS_PER_DAY for i in range(n)]  # day-aligned stamps (close of day i)
    return pl.DataFrame(
        {
            "ts_ms": ts,
            "symbol": [symbol] * n,
            "daily_return_1d": returns,
            "abs_daily_return_1d": [abs(r) for r in returns],
        }
    )


def _btc_return_30d(frame: pl.DataFrame) -> list[float | None]:
    out = _attach_market_context(frame)
    assert "btc_return_30d" in out.columns
    return out.sort("ts_ms")["btc_return_30d"].to_list()


# --- causality: the feature is a lagged trailing window, no look-ahead -----------


def test_btc_return_30d_equals_lagged_30d_sum() -> None:
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0, 0.03, size=45).tolist()
    got = _btc_return_30d(_btc_frame(returns))
    # closed="left", window=30d, min_samples=30 -> row i = sum(returns[i-30:i]); null for i<30.
    for i in range(len(returns)):
        if i < 30:
            assert got[i] is None
        else:
            assert got[i] == pytest.approx(sum(returns[i - 30 : i]))


def test_btc_return_30d_excludes_current_day() -> None:
    # A spike on day i must NOT be in btc_return_30d[i] (lagged one day), but MUST be in
    # btc_return_30d[i+1]. This is the "known strictly before the decision" guarantee.
    returns = [0.0] * 45
    returns[35] = 1.0
    got = _btc_return_30d(_btc_frame(returns))
    assert got[35] == pytest.approx(0.0)  # the day-35 spike is excluded from day-35's value
    assert got[36] == pytest.approx(1.0)  # and present from day 36 onward


def test_btc_return_30d_truncation_invariance() -> None:
    # The decisive look-ahead falsifier: computing the feature on bars truncated at t is
    # identical to the full-series value at t. Appending future bars changes nothing prior.
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0, 0.04, size=50).tolist()
    full = _btc_return_30d(_btc_frame(returns))
    for cut in (33, 40, 47):
        truncated = _btc_return_30d(_btc_frame(returns[:cut]))
        for i in range(cut):
            a, b = full[i], truncated[i]
            if a is None or b is None:
                assert a is None and b is None
            else:
                assert a == pytest.approx(b)


# --- filter behavior: regime partition is exact ----------------------------------


def _regime_frame() -> pl.DataFrame:
    # Minimal frame carrying the precomputed btc_return_30d the filter reads.
    return pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC", "DDD", "EEE"],
            "ts_ms": [d * MS_PER_DAY for d in range(1, 6)],
            "btc_return_30d": [0.10, -0.05, 0.0, 0.20, None],  # up, down, flat(=down), up, unknown
        }
    )


def test_gate_off_is_noop() -> None:
    frame = _regime_frame()
    cfg = VolumeEventResearchConfig(btc_trend_gate="off")
    assert _apply_market_context_filters(frame, cfg).height == frame.height


def test_gate_uptrend_keeps_positive_only() -> None:
    frame = _regime_frame()
    cfg = VolumeEventResearchConfig(btc_trend_gate="uptrend")
    kept = set(_apply_market_context_filters(frame, cfg)["symbol"].to_list())
    assert kept == {"AAA", "DDD"}  # strictly > 0; flat and null excluded


def test_gate_downtrend_keeps_nonpositive_only() -> None:
    frame = _regime_frame()
    cfg = VolumeEventResearchConfig(btc_trend_gate="downtrend")
    kept = set(_apply_market_context_filters(frame, cfg)["symbol"].to_list())
    assert kept == {"BBB", "CCC"}  # <= 0; flat counts as down, null excluded


def test_gate_uptrend_downtrend_partition_the_known_regime() -> None:
    # up ∪ down == all rows with a KNOWN regime (null dropped by both); up ∩ down == ∅.
    frame = _regime_frame()
    up = set(_apply_market_context_filters(frame, VolumeEventResearchConfig(btc_trend_gate="uptrend"))["symbol"].to_list())
    down = set(_apply_market_context_filters(frame, VolumeEventResearchConfig(btc_trend_gate="downtrend"))["symbol"].to_list())
    known = set(frame.drop_nulls("btc_return_30d")["symbol"].to_list())
    assert up | down == known
    assert up & down == set()


def test_gate_missing_column_yields_empty() -> None:
    frame = _regime_frame().drop("btc_return_30d")
    cfg = VolumeEventResearchConfig(btc_trend_gate="uptrend")
    assert _apply_market_context_filters(frame, cfg).height == 0


# --- config validation -----------------------------------------------------------


def test_invalid_gate_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="btc_trend_gate"):
        _validate_event_config(VolumeEventResearchConfig(btc_trend_gate="bull"))


def test_valid_gates_accepted_by_validation() -> None:
    for g in ("off", "uptrend", "downtrend"):
        _validate_event_config(VolumeEventResearchConfig(btc_trend_gate=g))  # no raise


def test_default_gate_is_off() -> None:
    # Off-by-default is the AGENTS.md hard line: the deployed profile must be unchanged.
    assert VolumeEventResearchConfig().btc_trend_gate == "off"
