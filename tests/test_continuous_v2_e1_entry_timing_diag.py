from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "continuous_v2_e1_entry_timing_diag.py"


def _load():
    spec = importlib.util.spec_from_file_location("continuous_v2_e1_entry_timing_diag", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_entry_timing_fill_miss_and_accounting() -> None:
    e1 = _load()
    # T1 time-exit spikes -> filled, entry improves; T2 time-exit no spike -> missed (foregone winner);
    # T3 take_profit spikes -> filled but entry-relative so raw unchanged.
    trades = [
        {"symbol": "T1", "entry_ts_ms": 1000, "entry_price": 100.0, "exit_price": 95.0,
         "exit_reason": "max_hold", "net_return": 0.05, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0},
        {"symbol": "T2", "entry_ts_ms": 2000, "entry_price": 100.0, "exit_price": 99.0,
         "exit_reason": "max_hold", "net_return": 0.01, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0},
        {"symbol": "T3", "entry_ts_ms": 3000, "entry_price": 100.0, "exit_price": 90.0,
         "exit_reason": "take_profit", "net_return": 0.10, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0},
    ]
    bars = {
        "T1": pl.DataFrame({"high": [101.0], "close": [100.5]}),   # >= 100.5 -> filled
        "T2": pl.DataFrame({"high": [100.2], "close": [100.1]}),   # < 100.5 -> missed
        "T3": pl.DataFrame({"high": [102.0], "close": [101.0]}),   # filled, TP
    }

    def fetch(symbol: str, entry_ts_ms: int):
        return bars.get(symbol)

    res = e1.evaluate_entry_timing(trades, fetch, deltas=(0.005,), seed=1)
    assert res["n_with_5m"] == 3
    row = res["deltas"]["0.0050"]
    assert row["n_filled"] == 2 and row["n_missed"] == 1
    assert row["fill_rate"] == pytest.approx(2 / 3)
    # T1: raw_new=(100.5-95)/100.5=0.054726..., delta=+0.004726; T3 TP delta=0
    assert row["filled_entry_improve_contrib"] == pytest.approx((100.5 - 95.0) / 100.5 - 0.05, abs=1e-9)
    assert row["missed_foregone_contrib"] == pytest.approx(0.01)
    # net effect = filled improvement - missed foregone (missing the 0.01 winner dominates)
    assert row["net_effect_contrib"] == pytest.approx((100.5 - 95.0) / 100.5 - 0.05 - 0.01, abs=1e-9)
    assert row["net_effect_contrib"] < 0.0


def test_missing_5m_data_excludes_trade() -> None:
    e1 = _load()
    trades = [
        {"symbol": "A", "entry_ts_ms": 1000, "entry_price": 100.0, "exit_price": 95.0,
         "exit_reason": "max_hold", "net_return": 0.05, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0},
    ]

    def fetch(symbol: str, entry_ts_ms: int):
        return None

    res = e1.evaluate_entry_timing(trades, fetch, deltas=(0.005,), seed=0)
    assert res["n_with_5m"] == 0
    assert res["deltas"] == {}
