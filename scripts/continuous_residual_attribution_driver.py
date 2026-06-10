"""P4: factor-residual attribution of the continuous book (control / btc / 2f hedged).

Pre-registered in docs/preregistration/continuous-residual-attribution-2026-06-10.md.
ANALYSIS only — full-sample time-series OLS of book daily returns on the R4-validated
6-factor return series + raw BTC/ETH market legs; reports betas, R^2, annualized alpha,
and residual Sharpe per spec per book per venue.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_residual_attribution_driver.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_rs_squeeze_probe as probe  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
)
from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_residual_attribution_2026-06-10"
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
ANN = 365.25
MS_PER_DAY = 86_400_000
FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}


def winner_rule() -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=90, target_daily_vol=0.045, max_scale=4.0,
        drawdown_half_threshold=-0.04, drawdown_zero_threshold=None,
        resize_cost_bps=10.0, strategy_momentum_window_days=0,
    )


def instrument_inputs(venue: str, days: list[int], symbol: str, panel: pl.DataFrame):
    p = (
        panel.filter(pl.col("symbol") == symbol).sort("d")
        .with_columns(pl.col("close").shift(1).alias("close_prev"), pl.col("d").shift(1).alias("d_prev"))
        .filter(((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1) & (pl.col("close_prev") > 0))
        .with_columns((pl.col("close") / pl.col("close_prev") - 1.0).alias("ret"))
    )
    rets = {
        int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000): float(r)
        for d, r in p.select(["d", "ret"]).iter_rows()
    }
    fund: dict[int, float] = {}
    root = FUNDING_ROOT[venue]
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = root / f"date={date}" / f"symbol={symbol}"
        if part.exists():
            fund[day] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
    return rets, fund


def book_ledgers(venue: str, panel: pl.DataFrame) -> dict[str, pl.DataFrame]:
    pieces = {}
    for src in WINNER_WEIGHTS:
        comp, _n, _cfg = scout._load_source(scout.SOURCES[src], venue)
        pieces[src] = comp
    combined = scout._combine_components(pieces, WINNER_WEIGHTS)
    btc_ret, btc_fund = instrument_inputs(venue, combined.days, "BTCUSDT", panel)
    eth_ret, eth_fund = instrument_inputs(venue, combined.days, "ETHUSDT", panel)
    hr = ContinuousHedgeRule(90, 60, 2.0, 5.0)
    return {
        "control": apply_rebalance_rule(combined, winner_rule()),
        "hedged_btc": apply_rebalance_rule(combined, winner_rule(), hr, btc_ret, btc_fund),
        "hedged_2f": apply_rebalance_rule(combined, winner_rule(), hr, btc_ret, btc_fund, eth_ret, eth_fund),
    }


def regress(y: np.ndarray, X: np.ndarray, names: list[str]) -> dict:
    Xi = np.column_stack([np.ones(len(y)), X])
    beta, *_ = np.linalg.lstsq(Xi, y, rcond=None)
    fit = Xi @ beta
    resid_full = y - fit                       # noise only
    resid_series = y - X @ beta[1:]            # intercept kept in (per receipt)
    ss_res = float(((y - fit) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    sh = float(resid_series.mean() / resid_series.std() * np.sqrt(ANN)) if resid_series.std() > 0 else 0.0
    raw_sh = float(y.mean() / y.std() * np.sqrt(ANN)) if y.std() > 0 else 0.0
    return {
        "betas": {n: round(float(b), 4) for n, b in zip(names, beta[1:])},
        "alpha_ann_pct": round(float(beta[0]) * ANN * 100, 2),
        "r2": round(r2, 3),
        "raw_sharpe": round(raw_sh, 3),
        "residual_sharpe": round(sh, 3),
        "n_days": len(y),
        "resid_noise_std_ann_pct": round(float(resid_full.std()) * np.sqrt(ANN) * 100, 2),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {"preregistration": "docs/preregistration/continuous-residual-attribution-2026-06-10.md"}
    for venue in ["bybit", "binance"]:
        print(f"[{venue}] building factor panel ...", flush=True)
        fp = build_factor_panel(ROOTS[venue], start="2023-04-01", end="2026-05-28")
        f_ret, _resid = fit_factor_returns(fp)
        wide = f_ret.pivot(values="factor_return", index="ts_ms", on="factor").sort("ts_ms")
        factor_names = [c for c in wide.columns if c != "ts_ms"]
        print(f"[{venue}] factor days: {wide.height}  factors: {factor_names}", flush=True)
        wide.write_parquet(OUT / f"factor_returns_{venue}.parquet")

        panel = probe.load_daily_panel(venue)
        ledgers = book_ledgers(venue, panel)
        # market legs aligned exactly to ledger day d
        btc_ret, _ = instrument_inputs(venue, [], "BTCUSDT", panel)
        eth_ret, _ = instrument_inputs(venue, [], "ETHUSDT", panel)
        # factor return stamped d-1 is earned ~day d
        fmap = {int(r["ts_ms"]): {n: r[n] for n in factor_names} for r in wide.to_dicts()}

        results[venue] = {}
        for book, led in ledgers.items():
            rows = []
            for d, r in zip(led["ts_ms"].to_list(), led["basket_return"].to_list()):
                f = fmap.get(int(d) - MS_PER_DAY)
                b, e = btc_ret.get(int(d)), eth_ret.get(int(d))
                if f is None or any(f[n] is None for n in factor_names) or b is None or e is None:
                    continue
                rows.append((float(r), [float(f[n]) for n in factor_names], float(b), float(e)))
            y = np.array([r[0] for r in rows])
            F = np.array([r[1] for r in rows])
            M = np.array([[r[2], r[3]] for r in rows])
            results[venue][book] = {
                "S1_market": regress(y, M, ["btc", "eth"]),
                "S2_factors": regress(y, F, factor_names),
                "S3_full": regress(y, np.column_stack([F, M]), factor_names + ["btc", "eth"]),
            }
            s3 = results[venue][book]["S3_full"]
            print(f"[{venue}] {book:11s} rawSh {s3['raw_sharpe']:6.3f}  S3 residSh {s3['residual_sharpe']:6.3f} "
                  f"R2 {s3['r2']:5.3f} alpha {s3['alpha_ann_pct']:6.2f}%/yr", flush=True)

    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
