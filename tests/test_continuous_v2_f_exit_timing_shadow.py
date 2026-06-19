from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "continuous_v2_f_exit_timing_shadow.py"


def _load():
    spec = importlib.util.spec_from_file_location("continuous_v2_f_exit_timing_shadow", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _rising_loser():
    # short entry 100; price rises (short loses); no TP (low never <= 90)
    return [(h, 100 + h * 0.4, 100 + h * 0.4 + 0.1, 100 + h * 0.4 - 0.1, 100 + h * 0.4) for h in range(1, 25)]


def _tp_winner():
    # short entry 100; hour 3 trades to 89 (<= tp 90) -> TP fires at h3
    path = [(1, 100, 100.2, 99.5, 99.6), (2, 99.6, 99.7, 95.0, 95.2), (3, 95.2, 95.3, 89.0, 90.5)]
    path += [(h, 90, 90.1, 89.9, 90.0) for h in range(4, 25)]
    return path


def test_simulate_tp_precedence_and_loser_cut() -> None:
    f = _load()
    # Loser: shorter_hold_12h exits earlier (less adverse) -> shadow raw > control raw
    sim = f._simulate(_rising_loser(), 100.0, 12, "shorter_hold", 0.0, 0.0, None)
    assert sim["control"][0] == 24 and sim["control"][1] == "hold"
    assert sim["shadow"][0] == 12 and sim["shadow"][1] == "rule"
    assert f._short_ret(100.0, sim["shadow"][2]) > f._short_ret(100.0, sim["control"][2])
    # TP winner: TP at h3 takes precedence over the 12h rule -> shadow is TP, not a cut
    sim2 = f._simulate(_tp_winner(), 100.0, 12, "shorter_hold", 0.0, 0.0, None)
    assert sim2["control"][1] == "tp" and sim2["control"][0] == 3
    assert sim2["shadow"][1] == "tp" and sim2["shadow"][0] == 3


def test_simulate_mfe_giveback_triggers() -> None:
    f = _load()
    # drops to 95 by h5 (MFE 5%) then rises back to 99 by h10 (gives back >50% of peak)
    path = [(1, 100, 100, 99, 99), (2, 99, 99, 98, 98), (3, 98, 98, 96, 96), (4, 96, 96, 95, 95),
            (5, 95, 95, 95, 95), (6, 95, 96, 95, 96), (7, 96, 97, 96, 97), (8, 97, 98, 97, 98),
            (9, 98, 99, 98, 99), (10, 99, 99, 99, 99)]
    path += [(h, 99, 99, 99, 99) for h in range(11, 25)]
    sim = f._simulate(path, 100.0, None, "mfe_giveback", 0.03, 0.5, None)
    assert sim["shadow"][1] == "rule"
    assert sim["shadow"][0] <= 10  # exits on the giveback, not at 24h


def test_evaluate_aggregates_and_classifies() -> None:
    f = _load()
    paths = {"LOSER": _rising_loser(), "WINNER": _tp_winner()}
    trades = [
        {"symbol": "LOSER", "entry_ts_ms": 0, "entry_price": 100.0, "exit_price": 109.6,
         "exit_reason": "max_hold", "net_return": -0.096, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0},
        {"symbol": "WINNER", "entry_ts_ms": 0, "entry_price": 100.0, "exit_price": 90.0,
         "exit_reason": "take_profit", "net_return": 0.10, "cost_return": 0.0, "funding_return": 0.0, "notional_weight": 1.0},
    ]

    def fetch(symbol: str, entry_ts_ms: int):
        return paths.get(symbol)

    res = f.evaluate_exit_rules(trades, fetch, rules=[{"name": "sh12", "kind": "shorter_hold", "hour": 12}], seed=0)
    assert res["n_with_path"] == 2
    r = res["rules"]["sh12"]
    # loser saved (positive), TP winner untouched
    assert r["net_effect_contrib"] > 0.0
    assert r["tp_winners_cut_n"] == 0
    assert res["path_control_recon_mean_abs_raw_err"] == pytest.approx(0.0, abs=1e-6)
