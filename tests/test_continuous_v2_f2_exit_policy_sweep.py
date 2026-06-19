from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "continuous_v2_f2_exit_policy_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("continuous_v2_f2_exit_policy_sweep", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bar(h, low, close):
    return (h, 100.0, 100.0, low, close)


def test_tp_hold_threshold_and_hold():
    f = _load()
    # low crosses 94 at h2 (=> 6% TP) and 90 at h5 (=> 10% TP)
    path = [_bar(1, 98, 98), _bar(2, 93, 95), _bar(3, 92, 94), _bar(4, 91, 93), _bar(5, 89, 91)]
    path += [_bar(h, 95, 95) for h in range(6, 25)]
    assert f.simulate_policy(path, 100.0, {"kind": "tp_hold", "T": 0.06, "H": 24}) == pytest.approx(0.06)
    assert f.simulate_policy(path, 100.0, {"kind": "tp_hold", "T": 0.10, "H": 24}) == pytest.approx(0.10)

    # TP not reached within 24h but reached at h30 -> hold extension catches it
    p2 = [_bar(h, 95, 96) for h in range(1, 30)] + [_bar(30, 89, 90)] + [_bar(h, 90, 90) for h in range(31, 40)]
    r24 = f.simulate_policy(p2, 100.0, {"kind": "tp_hold", "T": 0.10, "H": 24})
    r36 = f.simulate_policy(p2, 100.0, {"kind": "tp_hold", "T": 0.10, "H": 36})
    assert r24 == pytest.approx((100 - 96) / 100)  # time exit at 24h close
    assert r36 == pytest.approx(0.10)              # TP captured by 36h hold
    assert r36 > r24


def test_decay_tp_captures_sub10_favorable():
    f = _load()
    # favorable to ~7% after h12 (low 93), never 10%; close@24 = 94 (time raw 6%)
    path = [_bar(h, 98, 98) for h in range(1, 13)] + [_bar(h, 93, 94) for h in range(13, 25)]
    decay = f.simulate_policy(path, 100.0, {"kind": "decay_tp", "T0": 0.10, "Tmin": 0.06, "h_start": 12, "H": 24})
    ctrl = f.simulate_policy(path, 100.0, {"kind": "tp_hold", "T": 0.10, "H": 24})
    assert ctrl == pytest.approx(0.06)        # time exit
    assert decay > ctrl                        # decayed TP locks in more than the 24h time exit
    assert 0.06 <= decay <= 0.10


def test_partial_two_stage():
    f = _load()
    # hits T1=5% (95) at h2 and T2=10% (90) at h5
    path = [_bar(1, 98, 98), _bar(2, 95, 96), _bar(3, 93, 94), _bar(4, 91, 92), _bar(5, 89, 90)]
    path += [_bar(h, 95, 95) for h in range(6, 25)]
    both = f.simulate_policy(path, 100.0, {"kind": "partial", "f": 0.5, "T1": 0.05, "T2": 0.10, "H": 24})
    assert both == pytest.approx(0.5 * 0.05 + 0.5 * 0.10)
    # hits T1 only; remaining leg exits at 24h close=96 (raw 4%)
    p2 = [_bar(1, 98, 98), _bar(2, 95, 96)] + [_bar(h, 96, 96) for h in range(3, 25)]
    only1 = f.simulate_policy(p2, 100.0, {"kind": "partial", "f": 0.5, "T1": 0.05, "T2": 0.10, "H": 24})
    assert only1 == pytest.approx(0.5 * 0.05 + 0.5 * 0.04)


def test_evaluate_recon_matches_ledger():
    f = _load()
    # control policy (10%,24) must reproduce the recorded exit_price for a TP trade
    path = [_bar(1, 98, 98), _bar(2, 93, 94), _bar(3, 89, 90)] + [_bar(h, 95, 95) for h in range(4, 25)]
    trades = [{"symbol": "X", "entry_ts_ms": 0, "entry_price": 100.0, "exit_price": 90.0,
               "net_return": 0.10, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0}]
    res = f.evaluate_policies(trades, lambda s, t: path, f.POLICIES)
    assert res["recon_mean_abs_raw_err_vs_ledger"] == pytest.approx(0.0, abs=1e-9)
