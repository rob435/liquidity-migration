#!/usr/bin/env python3
"""Dynamic regime-timed capital tilt between the LONG (v11a) and CONTINUOUS sleeves.

Research question (operator goal 2026-06-16): can a causal, regime-timed capital
tilt between the long-native v11a sleeve and the continuous fade book BEAT the best
FIXED-weight blend, with GENUINE predictive skill (not a 3-4-rotation overfit)?

This harness is deliberately split:

  * ``stage0b`` is DESCRIPTIVE ONLY (no alpha claim): per-year / per-regime returns
    of each sleeve, inter-sleeve correlation, and the oracle (hindsight) best fixed
    weight. It establishes whether a rotation to time even exists.

  * ``stage1`` runs the PRE-REGISTERED allocator + the negative controls + the
    pre-stated decision rule. Do not run it until the preregistration receipt is
    committed (see docs/preregistration/2026-06-16-dynamic-tilt-allocator.md).

Inputs are the freshly reconstructed, costed, full-PIT daily return series emitted
by ``scripts/equity_curves.sh`` (long -> long_native_equity_mtm.csv ``mtm_return``;
continuous -> continuous_equity.csv ``basket_return``). BTC daily returns and the
deployed BTC-vol regime come from the SAME causal machinery the live book uses
(``_w5_shared._btc_inputs`` + ``continuous_regime``), so the regime is causal at
decision time by construction.

Everything here is a CAPITAL allocation (gross = 1): the day-t blended return is
``w_t * r_long_t + (1 - w_t) * r_cont_t`` with ``w_t`` set from data strictly
before day t. MAR = annualized return / |max drawdown| of the compounded blend.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from liquidity_migration import continuous_regime  # noqa: E402

MS_PER_DAY = 86_400_000
ROOTS = {
    "bybit": Path("~/SHARED_DATA/bybit_full_pit").expanduser(),
    "binance": Path("~/SHARED_DATA/binance_full_pit").expanduser(),
}

# Fixed, a-priori regime windows reused verbatim from the deployed signal
# (continuous_regime.FROZEN_BTCVOL_REGIME): 30d trailing measure, 250d percentile.
VOL_WINDOW = continuous_regime.FROZEN_BTCVOL_REGIME["vol_window"]
PCT_WINDOW = continuous_regime.FROZEN_BTCVOL_REGIME["pct_window"]
TREND_WINDOW = 30  # trailing BTC return window (same scale as vol_window)
WARMUP = continuous_regime.PCT_WARMUP


# --------------------------------------------------------------------------- IO
def _load_sleeve_returns(curves_dir: Path, venue: str) -> pl.DataFrame:
    """Return a tidy frame: date(str), r_long, r_cont over the common window."""
    long_csv = curves_dir / "long" / "long_native_equity_mtm.csv"
    if not long_csv.exists():  # fall back to the basket-day curve if MTM absent
        long_csv = curves_dir / "long" / "long_native_equity.csv"
    cont_csv = curves_dir / "continuous" / venue / "continuous_equity.csv"
    if not long_csv.exists():
        raise FileNotFoundError(f"missing long curve: {long_csv}")
    if not cont_csv.exists():
        raise FileNotFoundError(f"missing continuous curve: {cont_csv}")

    lf = pl.read_csv(long_csv)
    ret_col = "mtm_return" if "mtm_return" in lf.columns else "basket_return"
    if "date" not in lf.columns:
        lf = lf.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
        )
    lf = lf.select(pl.col("date").cast(pl.Utf8), pl.col(ret_col).alias("r_long"))

    cf = pl.read_csv(cont_csv)
    cf = cf.with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
    ).select(pl.col("date").cast(pl.Utf8), pl.col("basket_return").alias("r_cont"))

    # CRITICAL (backtesting integrity): the continuous ledger OMITS flat/cash days
    # (gate closed -> no row), while the long MTM curve carries every calendar day.
    # Inner-joining would silently drop the ~390 days where continuous is flat but
    # long may be active -> a biased blend. So build a COMPLETE daily spine over the
    # common live window [max(starts), min(ends)] and fill both sleeves' missing days
    # with 0.0 (a flat sleeve earns the cash return = 0).
    start = max(lf["date"].min(), cf["date"].min())
    end = min(lf["date"].max(), cf["date"].max())
    spine = pl.DataFrame(
        {"date": pl.date_range(
            dt.date.fromisoformat(start), dt.date.fromisoformat(end), interval="1d", eager=True
        ).dt.strftime("%Y-%m-%d")}
    )
    df = (
        spine.join(lf, on="date", how="left")
        .join(cf, on="date", how="left")
        .with_columns(pl.col("r_long").fill_null(0.0), pl.col("r_cont").fill_null(0.0))
        .sort("date")
    )
    return df


def _btc_daily_returns(root: Path) -> dict[int, float]:
    import _w5_shared  # type: ignore

    rets, _fund = _w5_shared._btc_inputs(root, "bybit", [])
    return rets


# ----------------------------------------------------------------- regime build
def _percentile_series(
    days: list[int], measure: dict[int, float | None]
) -> dict[int, float | None]:
    """Causal trailing-PCT_WINDOW percentile rank of ``measure`` (0..1), warm-up None.

    Mirrors continuous_regime.btcvol_intensity_series exactly: today's measure is
    scored against the deque of PRIOR days' measures, then appended."""
    dq: deque[float] = deque(maxlen=PCT_WINDOW)
    out: dict[int, float | None] = {}
    for d in days:
        v = measure.get(d)
        if v is None or len(dq) < WARMUP:
            out[d] = None
        else:
            out[d] = sum(1 for x in dq if x <= v) / len(dq)
        if v is not None:
            dq.append(v)
    return out


def build_regime_scores(btc_rets: dict[int, float]) -> dict[str, dict[int, float | None]]:
    """Two causal [0,1] regime scores keyed by day-ms: vol_score, trend_score.

    vol_score   = trailing-30d BTC vol, percentile over trailing 250 (deployed signal).
    trend_score = trailing-30d BTC cumulative return, percentile over trailing 250.
    Both use only returns strictly before the scored day (deque appended after)."""
    days = sorted(btc_rets)
    # trailing-30d vol measure (population stdev of prior 30 returns, exclusive)
    vol_meas: dict[int, float | None] = {}
    trend_meas: dict[int, float | None] = {}
    for i, d in enumerate(days):
        prior = [btc_rets[days[j]] for j in range(max(0, i - VOL_WINDOW), i)]
        vol_meas[d] = float(np.std(prior)) if len(prior) >= continuous_regime.VOL_MIN_OBS else None
        prior_t = [btc_rets[days[j]] for j in range(max(0, i - TREND_WINDOW), i)]
        trend_meas[d] = float(np.prod([1 + r for r in prior_t]) - 1.0) if len(prior_t) >= continuous_regime.VOL_MIN_OBS else None
    return {
        "vol_score": _percentile_series(days, vol_meas),
        "trend_score": _percentile_series(days, trend_meas),
    }


def _date_to_ms(date: str) -> int:
    return int(dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# ----------------------------------------------------------------- portfolio math
def _equity_metrics(rets: np.ndarray, *, ann: float = 365.25) -> dict[str, float]:
    """MAR/Sharpe/return/DD from a daily simple-return series (compounded)."""
    if rets.size == 0:
        return {"total_return": 0.0, "max_drawdown": 0.0, "mar": 0.0, "sharpe": 0.0, "cagr": 0.0}
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    maxdd = float(dd.min())
    total = float(eq[-1] - 1.0)
    years = rets.size / ann
    cagr = float(eq[-1] ** (1.0 / years) - 1.0) if years > 0 and eq[-1] > 0 else float("nan")
    std = float(rets.std())
    return {
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": maxdd,
        "mar": float(cagr / abs(maxdd)) if maxdd < 0 else float("inf"),
        "sharpe": float(rets.mean() / std * math.sqrt(ann)) if std > 0 else 0.0,
    }


def blend_returns(
    r_long: np.ndarray, r_cont: np.ndarray, w: np.ndarray, *, turn_cost_bps: float = 0.0
) -> np.ndarray:
    """w_t = LONG weight on day t (already causal). Optional rebalancing turnover cost
    charged on |Δw| each day (moving capital between sleeves costs the round-trip)."""
    p = w * r_long + (1.0 - w) * r_cont
    if turn_cost_bps > 0.0:
        dw = np.abs(np.diff(w, prepend=w[0]))
        p = p - dw * (turn_cost_bps * 1e-4)
    return p


# ----------------------------------------------------------------- the allocator
def primary_weight(vol_s: np.ndarray, trend_s: np.ndarray) -> np.ndarray:
    """PRE-REGISTERED primary LONG-weight form (no free coefficients):

        w_t = clip( trend_score_t - vol_score_t + 0.5 , 0, 1 )

    -> all-long when trend high & vol low; all-continuous when trend low & vol high;
    neutral 0.5 otherwise. Symmetric, mean ~0.5. Warm-up days (None) -> 0.5."""
    return np.clip(trend_s - vol_s + 0.5, 0.0, 1.0)


def variant_weights(vol_s: np.ndarray, trend_s: np.ndarray) -> dict[str, np.ndarray]:
    """A-priori robustness variants (reported, NOT used to pick the winner)."""
    return {
        "primary": primary_weight(vol_s, trend_s),
        "trend_only": np.clip(trend_s, 0.0, 1.0),
        "vol_only": np.clip(1.0 - vol_s, 0.0, 1.0),
        "binary": ((trend_s > 0.5) & (vol_s < 0.5)).astype(float),
    }


def risk_based_weights(
    r_long: np.ndarray, r_cont: np.ndarray, vol_lb: int = 30
) -> dict[str, np.ndarray]:
    """Causal risk-based LONG weights (no regime prediction; established mechanisms).

    Each uses ONLY returns strictly before day t (trailing realized vol over ``vol_lb``
    days, computed on the prior window). These are the robust, non-overfit way a
    *dynamic* allocation can beat a fixed CAPITAL weight: track each sleeve's drifting
    risk rather than predict which regime wins.

    * ``inverse_vol`` (risk parity): w_long_t = (1/vol_long) / (1/vol_long + 1/vol_cont)
      -> equal risk contribution; rebalances as the sleeves' vols drift.
    """
    n = r_long.size

    def _invvol(lb: int) -> np.ndarray:
        w = np.full(n, 0.5)
        for i in range(n):
            if i < lb:
                continue
            vl = float(np.std(r_long[i - lb:i]))
            vc = float(np.std(r_cont[i - lb:i]))
            il = 1.0 / vl if vl > 0 else 0.0
            ic = 1.0 / vc if vc > 0 else 0.0
            w[i] = il / (il + ic) if (il + ic) > 0 else 0.5
        return np.clip(w, 0.0, 1.0)

    def _relmom(lb: int) -> np.ndarray:
        """Relative risk-adjusted momentum: tilt to whichever sleeve has the higher
        positive trailing Sharpe-like score (mean/vol over the prior ``lb`` days).
        Parameter-free given lb; both-non-positive -> neutral 0.5. This is the literal
        'shift capital to whichever sleeve is winning'."""
        w = np.full(n, 0.5)
        for i in range(n):
            if i < lb:
                continue
            sl = slice(i - lb, i)
            sL = float(r_long[sl].mean() / (r_long[sl].std() + 1e-12))
            sC = float(r_cont[sl].mean() / (r_cont[sl].std() + 1e-12))
            pL, pC = max(sL, 0.0), max(sC, 0.0)
            w[i] = pL / (pL + pC) if (pL + pC) > 0 else 0.5
        return np.clip(w, 0.0, 1.0)

    return {
        "inverse_vol": _invvol(vol_lb),
        "inverse_vol_60": _invvol(60),
        "rel_mom_63": _relmom(63),
        "rel_mom_126": _relmom(126),
    }


def _assemble(curves_dir: Path, root: Path, venue: str) -> dict[str, Any]:
    """Aligned arrays: dates, r_long, r_cont, vol_score, trend_score over common win."""
    df = _load_sleeve_returns(curves_dir, venue)
    btc_rets = _btc_daily_returns(root)
    scores = build_regime_scores(btc_rets)
    dates = df["date"].to_list()
    day_ms = [(_date_to_ms(d) // MS_PER_DAY) * MS_PER_DAY for d in dates]
    vol_s = np.array([scores["vol_score"].get(d) for d in day_ms], dtype=object)
    trend_s = np.array([scores["trend_score"].get(d) for d in day_ms], dtype=object)
    # warm-up / missing regime -> neutral 0.5 (no look-ahead, no modulation)
    vol_s = np.array([0.5 if v is None else float(v) for v in vol_s])
    trend_s = np.array([0.5 if v is None else float(v) for v in trend_s])
    return {
        "dates": dates,
        "r_long": np.asarray(df["r_long"].to_list(), dtype=float),
        "r_cont": np.asarray(df["r_cont"].to_list(), dtype=float),
        "vol_score": vol_s,
        "trend_score": trend_s,
    }


# ----------------------------------------------------------------- stage 0b
def stage0b(curves: dict[str, Path]) -> dict[str, Any]:
    out: dict[str, Any] = {"stage": "0b_descriptive", "run_label": "exploratory_descriptive", "venues": {}}
    for venue, cdir in curves.items():
        d = _assemble(cdir, ROOTS[venue], venue)
        dates, rL, rC = d["dates"], d["r_long"], d["r_cont"]
        years = sorted({s[:4] for s in dates})
        per_year = {}
        for y in years:
            idx = [i for i, s in enumerate(dates) if s[:4] == y]
            if not idx:
                continue
            mL = _equity_metrics(rL[idx])
            mC = _equity_metrics(rC[idx])
            per_year[y] = {
                "n": len(idx),
                "long_ret": round(mL["total_return"], 4),
                "cont_ret": round(mC["total_return"], 4),
                "long_mar": round(mL["mar"], 3),
                "cont_mar": round(mC["mar"], 3),
                "winner": "long" if mL["total_return"] > mC["total_return"] else "cont",
            }
        # oracle best fixed weight by MAR over a grid
        grid = np.linspace(0.0, 1.0, 101)
        mars = [_equity_metrics(w * rL + (1 - w) * rC)["mar"] for w in grid]
        best_i = int(np.argmax(mars))
        # split-by-vol-regime sleeve performance (descriptive)
        vs = d["vol_score"]
        hi = vs >= 0.5
        out["venues"][venue] = {
            "n_days": len(dates),
            "window": [dates[0], dates[-1]],
            "corr_long_cont": round(float(np.corrcoef(rL, rC)[0, 1]), 3),
            "long_full": {k: round(v, 3) for k, v in _equity_metrics(rL).items()},
            "cont_full": {k: round(v, 3) for k, v in _equity_metrics(rC).items()},
            "per_year": per_year,
            "oracle_best_fixed_w_long": round(float(grid[best_i]), 2),
            "oracle_best_fixed_mar": round(float(mars[best_i]), 3),
            "mar_w0_allcont": round(mars[0], 3),
            "mar_w1_alllong": round(mars[-1], 3),
            "long_ret_hivol": round(_equity_metrics(rL[hi])["total_return"], 4),
            "long_ret_lovol": round(_equity_metrics(rL[~hi])["total_return"], 4),
            "cont_ret_hivol": round(_equity_metrics(rC[hi])["total_return"], 4),
            "cont_ret_lovol": round(_equity_metrics(rC[~hi])["total_return"], 4),
        }
    return out


# ----------------------------------------------------------------- stage 1
def _walkforward_best_fixed(rL: np.ndarray, rC: np.ndarray, lookback: int = 365) -> np.ndarray:
    """Causal walk-forward best-fixed-weight: each day use argmax-MAR weight from the
    trailing ``lookback`` days only. Neutral 0.5 during warm-up."""
    grid = np.linspace(0.0, 1.0, 21)
    w = np.full(rL.size, 0.5)
    for i in range(rL.size):
        if i < lookback:
            continue
        sl = slice(i - lookback, i)
        mars = [_equity_metrics(g * rL[sl] + (1 - g) * rC[sl])["mar"] for g in grid]
        w[i] = float(grid[int(np.argmax(mars))])
    return w


def _block_shuffle(w: np.ndarray, block: int, rng: np.random.Generator) -> np.ndarray:
    """Circular block bootstrap of the weight series: preserves autocorrelation /
    turnover, destroys alignment to the actual regime timing."""
    n = w.size
    nb = math.ceil(n / block)
    starts = rng.integers(0, n, size=nb)
    out = np.concatenate([np.take(w, range(s, s + block), mode="wrap") for s in starts])[:n]
    return out


def stage1(curves: dict[str, Path], *, n_shuffle: int = 300, seed: int = 12345,
           cost_bps: float = 1.0, block: int = 63) -> dict[str, Any]:
    out: dict[str, Any] = {
        "stage": "1_allocator_test", "run_label": "exploratory",
        "preregistration": "docs/preregistration/2026-06-16-dynamic-tilt-allocator.md",
        "params": {"n_shuffle": n_shuffle, "seed": seed, "cost_bps": cost_bps,
                   "block": block, "vol_window": VOL_WINDOW, "pct_window": PCT_WINDOW,
                   "trend_window": TREND_WINDOW},
        "venues": {},
    }
    rng = np.random.default_rng(seed)
    for venue, cdir in curves.items():
        d = _assemble(cdir, ROOTS[venue], venue)
        rL, rC = d["r_long"], d["r_cont"]
        vs, ts = d["vol_score"], d["trend_score"]
        n = rL.size

        def mar(weights: np.ndarray, cost: float = cost_bps) -> float:
            return _equity_metrics(blend_returns(rL, rC, weights, turn_cost_bps=cost))["mar"]

        def shp(weights: np.ndarray, cost: float = cost_bps) -> float:
            return _equity_metrics(blend_returns(rL, rC, weights, turn_cost_bps=cost))["sharpe"]

        # benchmarks (hindsight oracle + causal walk-forward fixed weight)
        grid = np.linspace(0.0, 1.0, 101)
        oracle_mars = [_equity_metrics(g * rL + (1 - g) * rC)["mar"] for g in grid]
        oracle_w = float(grid[int(np.argmax(oracle_mars))])
        oracle_mar = float(np.max(oracle_mars))
        wf_w = _walkforward_best_fixed(rL, rC)
        wf_mar = mar(wf_w)

        # all candidate weight series, evaluated on equal footing
        candidates = variant_weights(vs, ts)
        candidates.update(risk_based_weights(rL, rC))

        def evaluate(w: np.ndarray) -> dict[str, Any]:
            mean_w = float(np.mean(w))
            dyn = mar(w)
            sh = np.array([mar(_block_shuffle(w, block, rng)) for _ in range(n_shuffle)])
            thirds = {}
            for k, (a, b) in enumerate([(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]):
                sl = slice(a, b)
                thirds[f"third{k+1}_delta_vs_oracle"] = round(
                    _equity_metrics(blend_returns(rL[sl], rC[sl], w[sl], turn_cost_bps=cost_bps))["mar"]
                    - _equity_metrics(oracle_w * rL[sl] + (1 - oracle_w) * rC[sl])["mar"], 3)
            return {
                "mean_long_weight": round(mean_w, 3),
                "weight_turnover_per_day": round(float(np.abs(np.diff(w)).mean()), 4),
                "dynamic_mar": round(dyn, 3),
                "dynamic_sharpe": round(shp(w), 3),
                "dynamic_mar_2xcost": round(mar(w, cost=2 * cost_bps), 3),
                "delta_vs_oracle_fixed": round(dyn - oracle_mar, 3),
                "delta_vs_meanfix": round(dyn - mar(np.full_like(w, mean_w)), 3),
                "delta_vs_wf_fixed": round(dyn - wf_mar, 3),
                "lagged_signal_control_mar": round(mar(np.roll(w, 180)), 3),
                "shuffle_null_mean_mar": round(float(sh.mean()), 3),
                "shuffle_null_p95_mar": round(float(np.percentile(sh, 95)), 3),
                "dynamic_shuffle_pctile": round(float((sh < dyn).mean()), 3),
                "thirds": thirds,
            }

        cand_results = {name: evaluate(w) for name, w in candidates.items()}
        out["venues"][venue] = {
            "n_days": n,
            "window": [d["dates"][0], d["dates"][-1]],
            "oracle_best_fixed_w": round(oracle_w, 2),
            "oracle_best_fixed_mar": round(oracle_mar, 3),
            "oracle_best_fixed_sharpe": round(_equity_metrics(oracle_w * rL + (1 - oracle_w) * rC)["sharpe"], 3),
            "walkforward_fixed_mar": round(wf_mar, 3),
            "PRIMARY": cand_results["primary"],
            "candidates": cand_results,
        }
    # pooled (decision rests on PRIMARY)
    vs_ = out["venues"]
    if len(vs_) >= 1:
        out["pooled_primary"] = {
            "mean_delta_vs_oracle_fixed": round(float(np.mean([v["PRIMARY"]["delta_vs_oracle_fixed"] for v in vs_.values()])), 3),
            "mean_delta_vs_meanfix": round(float(np.mean([v["PRIMARY"]["delta_vs_meanfix"] for v in vs_.values()])), 3),
            "min_shuffle_pctile": round(float(np.min([v["PRIMARY"]["dynamic_shuffle_pctile"] for v in vs_.values()])), 3),
        }
    return out


# ----------------------------------------------------------------- cli
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["stage0b", "stage1"])
    p.add_argument("--bybit-curves", default="~/SHARED_DATA/dynamic_tilt_2026-06-16/bybit_curves")
    p.add_argument("--binance-curves", default="~/SHARED_DATA/dynamic_tilt_2026-06-16/binance_curves")
    p.add_argument("--venues", default="bybit,binance")
    p.add_argument("--out", default="~/SHARED_DATA/dynamic_tilt_2026-06-16")
    p.add_argument("--cost-bps", type=float, default=1.0)
    args = p.parse_args()

    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    curves = {}
    for v in venues:
        cd = args.bybit_curves if v == "bybit" else args.binance_curves
        curves[v] = Path(cd).expanduser()
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.stage == "stage0b":
        res = stage0b(curves)
        dest = out_root / "stage0b_descriptive.json"
    else:
        res = stage1(curves, cost_bps=args.cost_bps)
        dest = out_root / "stage1_allocator.json"
    dest.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
