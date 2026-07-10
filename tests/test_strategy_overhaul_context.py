from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.strategy_overhaul_context import (
    attach_continuous_market_context,
    attach_continuous_static_diagnostics,
)


START = 1_700_006_400_000  # UTC hour/day boundary


def _market_frame() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for day in range(32):
        for hour in (0, 1):
            ts = START + day * MS_PER_DAY + hour * MS_PER_HOUR
            for symbol, initial, daily_growth in (
                ("BTCUSDT", 100.0, 0.01),
                ("ETHUSDT", 50.0, 0.005),
                ("ALTAUSDT", 10.0, 0.0),
                ("ALTBUSDT", 20.0, 0.0),
            ):
                close = initial * (1.0 + daily_growth) ** day
                if hour == 1:
                    if symbol == "ALTAUSDT" and day == 31:
                        close *= 1.04
                    elif symbol == "ALTBUSDT" and day == 31:
                        close *= 0.98
                    else:
                        close *= 1.001
                rows.append({"symbol": symbol, "ts_ms": ts, "close": close})
    return pl.DataFrame(rows)


def test_market_context_is_exact_gap_aware_and_preserves_population() -> None:
    frame = _market_frame()
    missing_prior = START + 30 * MS_PER_DAY + MS_PER_HOUR
    frame = frame.filter(~((pl.col("symbol") == "ALTBUSDT") & (pl.col("ts_ms") == missing_prior)))

    output = attach_continuous_market_context(frame)
    ts = START + 31 * MS_PER_DAY + MS_PER_HOUR
    row = output.filter((pl.col("symbol") == "ALTAUSDT") & (pl.col("ts_ms") == ts)).to_dicts()[0]

    assert output.height == frame.height
    assert output.select(["symbol", "ts_ms"]).n_unique() == frame.height
    assert row["btc_uptrend_known"] is True
    assert row["btc_uptrend_pass"] is True
    assert row["btc_uptrend_fail"] is False
    assert row["btc_uptrend_unknown"] is False
    assert row["btc_uptrend_value"] > 0.0
    assert row["alt_breadth_ret1_ge_3pct"] == pytest.approx(0.5)
    assert row["alt_breadth_ret1_ge_3pct_denominator_count"] == 2
    assert row["alt_breadth_ret24_positive_peer_count"] == 2
    assert row["alt_breadth_ret24_positive_denominator_count"] == 1
    assert row["alt_breadth_ret24_positive_missing_peer_count"] == 1
    assert row["xs_ret1_dispersion_denominator_count"] == 4

    unknown_row = output.filter(pl.col("ts_ms") == START).row(0, named=True)
    assert unknown_row["btc_uptrend_known"] is False
    assert unknown_row["btc_uptrend_pass"] is False
    assert unknown_row["btc_uptrend_fail"] is False
    assert unknown_row["btc_uptrend_unknown"] is True


def test_market_context_is_invariant_to_later_price_mutations() -> None:
    frame = _market_frame()
    cutoff = START + 20 * MS_PER_DAY + MS_PER_HOUR
    before = attach_continuous_market_context(frame).filter(pl.col("ts_ms") <= cutoff)
    mutated = frame.with_columns(
        pl.when(pl.col("ts_ms") > cutoff).then(pl.col("close") * 999.0).otherwise(pl.col("close")).alias("close")
    )

    after = attach_continuous_market_context(mutated).filter(pl.col("ts_ms") <= cutoff)

    assert before.equals(after, null_equal=True)


def test_static_first_rejection_uses_frozen_order_per_component() -> None:
    base = {
        "symbol": ["A", "B"],
        "ts_ms": [START, START],
        "close": [10.0, 11.0],
        "rmom_stable_available": [True, True],
        "current_q25_pass": [False, True],
        "btc_uptrend_known": [False, True],
        "btc_uptrend_pass": [False, True],
        "current_q25_d9": [False, True],
        "current_liquidity_500k_pass": [False, True],
        "trigger_turn3_pop3": [False, True],
        "trigger_turn4_pop3": [False, False],
        "trigger_turn4_pop5": [False, False],
        "current_age_source_available": [False, True],
        "current_age_240_pass": [False, True],
    }

    output = attach_continuous_static_diagnostics(pl.DataFrame(base)).sort("symbol")
    a, b = output.to_dicts()

    assert a["p3_static_first_rejection_reason"] == "not_current_q25"
    assert a["p3_static_candidate"] is False
    assert b["p3_static_first_rejection_reason"] == "static_candidate"
    assert b["p3_static_candidate"] is True
    assert b["p4p3_static_first_rejection_reason"] == "component_not_triggered"
    assert b["p4p3_static_candidate"] is False


def test_context_refuses_outcome_columns_and_emits_typed_empty_schema() -> None:
    empty = pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "close": pl.Float64})
    annotated = attach_continuous_market_context(empty)
    assert annotated.schema["btc_uptrend_value"] == pl.Float64
    assert annotated.schema["btc_uptrend_known"] == pl.Boolean
    assert annotated.schema["btc_uptrend_fail"] == pl.Boolean
    assert annotated["btc_uptrend_unknown"].dtype == pl.Boolean
    assert annotated.schema["xs_ret1_dispersion_peer_count"] == pl.Int64

    with pytest.raises(ValueError, match="outcome/entry"):
        attach_continuous_market_context(
            pl.DataFrame(
                {
                    "symbol": ["A"],
                    "ts_ms": [START],
                    "close": [10.0],
                    "path_72h_underlying_return": [0.2],
                }
            )
        )

    with pytest.raises(ValueError, match="must be BTCUSDT"):
        attach_continuous_market_context(empty, btc_symbol="XBTUSDT")


def test_context_and_static_diagnostics_accept_canonical_signal_key() -> None:
    canonical = _market_frame().rename({"ts_ms": "signal_ts_ms"})
    context = attach_continuous_market_context(canonical)
    assert "ts_ms" not in context.columns
    assert context.height == canonical.height

    static_input = pl.DataFrame(
        {
            "symbol": ["A"],
            "signal_ts_ms": [START],
            "close": [10.0],
            "rmom_stable_available": [True],
            "current_q25_pass": [True],
            "btc_uptrend_known": [True],
            "btc_uptrend_pass": [True],
            "current_q25_d9": [True],
            "current_liquidity_500k_pass": [True],
            "trigger_turn3_pop3": [True],
            "trigger_turn4_pop3": [False],
            "trigger_turn4_pop5": [False],
            "current_age_source_available": [True],
            "current_age_240_pass": [True],
        }
    )
    static = attach_continuous_static_diagnostics(static_input)
    assert static["p3_static_candidate"].item() is True
    assert "ts_ms" not in static.columns


def test_context_refuses_disagreeing_raw_and_canonical_time_keys() -> None:
    frame = _market_frame().with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("signal_ts_ms"))
    with pytest.raises(ValueError, match="must agree"):
        attach_continuous_market_context(frame)
