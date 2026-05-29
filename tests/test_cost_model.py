from __future__ import annotations

import random

import polars as pl

from liquidity_migration.cost_model import (
    COST_FEATURE_COLUMNS,
    DEFAULT_PARAMS,
    TAKER_ROUND_TRIP_BPS,
    CostModelParams,
    fit_cost_model,
    predict_cost_bps,
)


def _synth_observations(coefs: dict, *, n: int = 300, seed: int = 1, noise: float = 0.0) -> pl.DataFrame:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        f = {
            "size_adv_ratio": rng.uniform(0.0, 0.5),
            "realized_vol_7d": rng.uniform(0.2, 1.5),
            "spread_proxy_bps": rng.uniform(1.0, 8.0),
            "hour_norm": rng.uniform(0.0, 1.0),
            "funding_term": rng.uniform(-0.02, 0.02),
        }
        cost = coefs["alpha"] + sum(coefs.get(c, 0.0) * f[c] for c in COST_FEATURE_COLUMNS)
        if noise:
            cost += rng.gauss(0.0, noise)
        f["realized_cost_bps"] = cost
        rows.append(f)
    return pl.DataFrame(rows)


def test_fit_recovers_known_coefficients() -> None:
    coefs = {"alpha": 6.0, "size_adv_ratio": 2.5, "realized_vol_7d": 1.2,
             "spread_proxy_bps": 0.8, "hour_norm": 3.0, "funding_term": 4.0}
    params, diag = fit_cost_model(_synth_observations(coefs), venue="bybit")
    assert diag["status"] == "fitted"
    assert diag["n_obs"] == 300
    assert abs(params.alpha - 6.0) < 1e-6, params.alpha
    assert abs(params.b_size - 2.5) < 1e-6
    assert abs(params.b_vol - 1.2) < 1e-6
    assert abs(params.b_spread - 0.8) < 1e-6
    assert abs(params.b_hour - 3.0) < 1e-6
    assert abs(params.b_funding - 4.0) < 1e-6
    assert diag["r2"] > 0.999
    assert diag["pred_vs_realized_corr"] > 0.999
    assert diag["rmse_bps"] < 1e-6
    assert params.venue == "bybit"


def test_fit_insufficient_obs_returns_default() -> None:
    obs = pl.DataFrame({
        "realized_cost_bps": [10.0, 12.0, 11.0],
        "size_adv_ratio": [0.1, 0.2, 0.3], "realized_vol_7d": [0.5, 0.6, 0.7],
        "spread_proxy_bps": [2.0, 3.0, 4.0], "hour_norm": [0.1, 0.5, 0.9],
        "funding_term": [0.0, 0.01, -0.01],
    })  # n=3 < 5 features + 2 = 7
    params, diag = fit_cost_model(obs, venue="binance")
    assert diag["status"] == "insufficient_obs"
    assert params.alpha == TAKER_ROUND_TRIP_BPS
    assert params.venue == "binance"
    assert params.b_size == 0.0


def test_fit_no_target_or_empty() -> None:
    params, diag = fit_cost_model(pl.DataFrame(), venue="bybit")
    assert diag["status"] == "no_data"
    assert params.alpha == TAKER_ROUND_TRIP_BPS
    _, d2 = fit_cost_model(pl.DataFrame({"size_adv_ratio": [0.1]}), venue="bybit")
    assert d2["status"] == "no_data"


def test_predict_applies_functional_form_unfloored() -> None:
    params = CostModelParams(alpha=5.0, b_size=2.0, b_vol=1.0, b_spread=0.5,
                             b_hour=3.0, b_funding=10.0, cost_floor_bps=0.0)
    feats = pl.DataFrame({
        "size_adv_ratio": [1.0], "realized_vol_7d": [2.0], "spread_proxy_bps": [4.0],
        "hour_norm": [0.5], "funding_term": [0.1],
    })
    # 5 + 2*1 + 1*2 + 0.5*4 + 3*0.5 + 10*0.1 = 13.5
    assert abs(predict_cost_bps(feats, params)[0] - 13.5) < 1e-9


def test_predict_floor_and_missing_columns() -> None:
    params = CostModelParams(alpha=2.0, b_size=1.0, cost_floor_bps=15.0)
    feats = pl.DataFrame({"size_adv_ratio": [0.0, 1.0]})  # other feature cols absent
    pred = predict_cost_bps(feats, params)
    assert pred[0] == 15.0 and pred[1] == 15.0  # 2 and 3 both below the 15 floor
    params2 = CostModelParams(alpha=2.0, b_size=20.0, cost_floor_bps=15.0)
    pred2 = predict_cost_bps(feats, params2)
    assert pred2[0] == 15.0           # 2 -> floored
    assert abs(pred2[1] - 22.0) < 1e-9  # 2 + 20 = 22 > 15 passes through


def test_default_params_predict_flat_floor() -> None:
    feats = pl.DataFrame({
        "size_adv_ratio": [0.1, 0.5], "realized_vol_7d": [0.5, 1.0],
        "spread_proxy_bps": [2.0, 5.0], "hour_norm": [0.2, 0.8],
        "funding_term": [0.01, -0.01],
    })
    assert predict_cost_bps(feats, DEFAULT_PARAMS["bybit"]).to_list() == [15.0, 15.0]


def test_predict_empty_frame() -> None:
    assert predict_cost_bps(pl.DataFrame(), DEFAULT_PARAMS["binance"]).len() == 0


def test_coef_vector_order() -> None:
    p = CostModelParams(alpha=1.0, b_size=2.0, b_vol=3.0, b_spread=4.0, b_hour=5.0, b_funding=6.0)
    assert list(p.coef_vector()) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
