from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "continuous_v2_f2b_vol_tp_screen.py"


def _load():
    spec = importlib.util.spec_from_file_location("continuous_v2_f2b_vol_tp_screen", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_simulate_tp_hit_and_time_exit():
    f = _load()
    # low reaches 90 (=10% TP) at h3
    path = [(1, 98, 98), (2, 94, 95), (3, 89, 91), (4, 95, 95)]
    raw, h = f.simulate_tp(path, 100.0, 0.10, 24)
    assert raw == pytest.approx(0.10) and h == 3
    # never hits 12% TP within hold -> time exit at last close
    p2 = [(1, 95, 96), (2, 95, 96), (3, 95, 96)]
    raw2, h2 = f.simulate_tp(p2, 100.0, 0.12, 24)
    assert raw2 == pytest.approx((100 - 96) / 100)
    assert h2 == 3


def test_evaluate_vol_norm_and_mar_proxy():
    f = _load()
    # two trades on different days so the proxy builds a 2-day equity
    trades = [
        {"symbol": "A", "entry_ts_ms": 0, "entry_price": 100.0, "exit_price": 90.0,
         "notional_weight": 1.0, "cost_return": 0.0, "funding_return": 0.0, "rv168": 2.0},
        {"symbol": "B", "entry_ts_ms": f.DAY_MS, "entry_price": 100.0, "exit_price": 90.0,
         "notional_weight": 1.0, "cost_return": 0.0, "funding_return": 0.0, "rv168": 0.5},
    ]
    pathA = [(1, 98, 98), (2, 80, 82)]   # deep favorable -> hits any TP up to 20%
    pathB = [(1, 98, 98), (2, 80, 82)]
    paths = {"A": pathA, "B": pathB}
    res = f.evaluate(trades, lambda s, t: paths[s], f.POLICIES, median_rv=1.0)
    assert res["n_with_path"] == 2
    # control_10 mar_delta is 0 by construction
    assert res["policies"]["control_10"]["mar_delta_vs_control"] == pytest.approx(0.0)
    # vol_norm_p10: high-rv name A gets T=clip(0.10*2,..)=0.20, low-rv B gets clip(0.10*0.5)=0.05
    # both paths reach 20% favorable so A realizes 0.20, B realizes 0.05 -> distinct from flat 0.10
    assert "vol_norm_p10" in res["policies"]
