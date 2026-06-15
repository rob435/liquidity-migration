"""audit2b regression: _normalize_binance_taker_flow must guard non-finite / negative
taker volumes before computing the imbalance ratio, instead of emitting a fabricated 0.0.

Defect (data-download-7 family): a NaN/inf or negative buy/sell volume slips past the
`is None` check. `total = buy + sell` is then NaN or <= 0, so the
`(buy - sell) / total if total > 0 else 0.0` branch fabricates a 0.0 imbalance (or a NaN)
alongside a NaN/garbage signed volume — the exact spurious-zero corruption the function's
own docstring forbids for missing data.

Fix: treat a non-finite or negative volume as missing data and emit null derived fields,
identical to how an absent buy/sell field is already handled. The happy path
(finite, non-negative volumes) is unchanged.
"""

import math

from liquidity_migration.downloaders import _normalize_binance_taker_flow


def _row(ts, buy, sell, ratio="1.0"):
    return {"timestamp": ts, "buyVol": buy, "sellVol": sell, "buySellRatio": ratio}


def test_happy_path_unchanged():
    """Normal finite, non-negative volumes: derived fields are the exact prior values."""
    rows = [
        _row(1_000, "30", "10", ratio="3.0"),
        _row(2_000, "0", "0", ratio="0.0"),  # both-zero -> total == 0 -> imbalance 0.0
        _row(3_000, "5", "15", ratio="0.333"),
    ]
    out = _normalize_binance_taker_flow("BTCUSDT", rows, period="1h")
    assert [r["ts_ms"] for r in out] == [1_000, 2_000, 3_000]

    # ts=1000: imbalance (30-10)/40 = 0.5, signed = 20
    assert out[0]["buy_volume_base"] == 30.0
    assert out[0]["sell_volume_base"] == 10.0
    assert out[0]["signed_volume_base"] == 20.0
    assert out[0]["taker_imbalance"] == 0.5

    # ts=2000: legitimate both-zero -> derived 0.0 / 0.0 preserved (NOT nulled)
    assert out[1]["signed_volume_base"] == 0.0
    assert out[1]["taker_imbalance"] == 0.0

    # ts=3000: imbalance (5-15)/20 = -0.5, signed = -10
    assert out[2]["signed_volume_base"] == -10.0
    assert out[2]["taker_imbalance"] == -0.5

    # buy_sell_ratio is passed through verbatim on every row
    assert out[0]["buy_sell_ratio"] == 3.0
    assert out[2]["buy_sell_ratio"] == 0.333


def test_absent_field_still_nulls_derived():
    """Existing behavior: a missing side nulls the derived fields (guard must not regress)."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [{"timestamp": 5_000, "sellVol": "10", "buySellRatio": "1"}], period="1h"
    )
    assert len(out) == 1
    assert out[0]["buy_volume_base"] is None
    assert out[0]["signed_volume_base"] is None
    assert out[0]["taker_imbalance"] is None


def test_nan_volume_does_not_fabricate_zero_imbalance():
    """A NaN volume must null the derived fields, not emit a spurious 0.0 imbalance."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [_row(7_000, "nan", "10")], period="1h"
    )
    assert len(out) == 1
    r = out[0]
    # OLD code: signed_volume_base is NaN and taker_imbalance is a fabricated 0.0.
    assert r["taker_imbalance"] is None
    assert r["signed_volume_base"] is None


def test_inf_volume_does_not_fabricate_imbalance():
    """An inf volume must null derived fields rather than produce 0.0 / NaN."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [_row(8_000, "inf", "10")], period="1h"
    )
    r = out[0]
    assert r["taker_imbalance"] is None
    assert r["signed_volume_base"] is None
    # And it must never be a sneaky NaN that passes an `is not None` check downstream.
    assert not (isinstance(r["taker_imbalance"], float) and math.isnan(r["taker_imbalance"]))


def test_negative_volume_does_not_fabricate_zero_imbalance():
    """A negative volume (physically impossible) must null derived fields, not emit 0.0."""
    out = _normalize_binance_taker_flow(
        "BTCUSDT", [_row(9_000, "-3", "1")], period="1h"
    )
    r = out[0]
    # OLD code: total = -2, total > 0 is False -> taker_imbalance fabricated as 0.0.
    assert r["taker_imbalance"] is None
    assert r["signed_volume_base"] is None
