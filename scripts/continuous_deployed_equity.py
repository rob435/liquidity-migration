"""Official-format equity curve for the DEPLOYED continuous strategy.

Book = winner_base 4-component ensemble @ w90/tv0.045/max4/ddh-0.04 + the banked
BTC+ETH 2f hedge with real funding — the exact object wired live 2026-06-10
(profile continuous_ensemble_v1 + HEDGE_MODE=2f), reproduced through the
s0-parity-verified Stage-B pipeline. Renders via the shared official chart writer
(`_write_equity_benchmark_chart`) → strategy-vs-BTC normalized PNG + monthly table,
one per venue. IN-SAMPLE RESEARCH (window 2023-04 → 2026-05; the window is spent;
forward demo is the arbiter) — the title says so.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_deployed_equity.py
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

from liquidity_migration.continuous_forward_replay import frozen_hedge_regime  # noqa: E402
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402
from liquidity_migration.volume_events_charts import _write_equity_benchmark_chart  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_deployed_equity_2026-06-10"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
ANN = 365.25
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


def deployed_hedge_intensity(days: list[int], btc_ret: dict[int, float]) -> dict[int, float] | None:
    """The BTC-vol regime-hedge intensity for the deployed object (None when no
    regime is frozen). Reads the same hash-pinned ``frozen_hedge_regime()`` the live
    book and forward ledger use, so the reported equity curve tracks the identical
    hedge policy (no silent divergence from demo/forward)."""
    regime = frozen_hedge_regime()
    if not regime:
        return None
    return btcvol_intensity_series(
        days, btc_ret, regime["lam"], regime["vol_window"], regime["pct_window"]
    )


def instrument_inputs(venue: str, days: list[int], symbol: str, panel: pl.DataFrame):
    p = (
        panel.filter(pl.col("symbol") == symbol).sort("d")
        .with_columns(pl.col("close").shift(1).alias("prev"), pl.col("d").shift(1).alias("d_prev"))
        .filter(((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1) & (pl.col("prev") > 0))
        .with_columns((pl.col("close") / pl.col("prev") - 1.0).alias("ret"))
    )
    rets = {
        int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000): float(r)
        for d, r in p.select(["d", "ret"]).iter_rows()
    }
    fund: dict[int, float] = {}
    root = FUNDING_ROOT[venue]
    missing = 0
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = root / f"date={date}" / f"symbol={symbol}"
        if part.exists():
            fund[day] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
        else:
            missing += 1
    # Surface funding-coverage gaps: a missing partition silently defaults to zero
    # funding for the hedge legs (audit-iter3 backlog scripts). Diagnostic only.
    if missing:
        print(f"[{venue}] {symbol} funding coverage: {len(days) - missing}/{len(days)} days; "
              f"{missing} defaulted to zero", flush=True)
    return rets, fund


def stats(df: pl.DataFrame) -> dict:
    dates = [dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).date() for t in df["ts_ms"].to_list()]
    rets = df["basket_return"].to_numpy()
    ncal = (dates[-1] - dates[0]).days + 1
    series = np.zeros(ncal)
    for d, r in zip(dates, rets):
        series[(d - dates[0]).days] = r
    eq = np.cumprod(1.0 + series)
    dd = eq / np.maximum.accumulate(eq) - 1.0
    total = float(eq[-1] - 1.0)
    years = ncal / ANN
    max_dd = float(dd.min())
    # audit2: a no-drawdown curve gives abs(max_dd)==0 -> ZeroDivisionError;
    # mirror reconciliation._calendar_metrics and report mar=None instead.
    mar = round((total / years) / abs(max_dd), 2) if abs(max_dd) > 1e-12 else None
    # audit-iter3: use the repo-canonical Sharpe convention (sample std, ddof=1) and
    # guard zero variance / single-day series, mirroring trade_lifecycle.annualized_sharpe
    # (np.std's ddof=0 understates vol and a flat series would divide by ~0).
    std = float(series.std(ddof=1)) if series.size > 1 else 0.0
    sharpe = round(float(series.mean()) / std * np.sqrt(ANN), 2) if std > 1e-12 else None
    return {
        "window": f"{dates[0]} -> {dates[-1]}",
        "years": round(years, 2),
        "total_return_pct": round(total * 100, 2),
        "annualized_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "mar": mar,
        "sharpe_daily_ann": sharpe,
        "worst_day_pct": round(float(series.min() * 100), 2),
    }


def main() -> None:
    summary = {}
    for venue in ["bybit", "binance"]:
        out_dir = OUT / venue
        out_dir.mkdir(parents=True, exist_ok=True)
        pieces = {}
        for src in WINNER_WEIGHTS:
            comp, _n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces[src] = comp
        combined = scout._combine_components(pieces, WINNER_WEIGHTS)
        panel = probe.load_daily_panel(venue)
        btc_ret, btc_fund = instrument_inputs(venue, combined.days, "BTCUSDT", panel)
        eth_ret, eth_fund = instrument_inputs(venue, combined.days, "ETHUSDT", panel)
        df = apply_rebalance_rule(
            combined, winner_rule(), ContinuousHedgeRule(90, 60, 2.0, 5.0),
            btc_ret, btc_fund, eth_ret, eth_fund,
            hedge_intensity=deployed_hedge_intensity(combined.days, btc_ret),
        )
        raw_klines = (
            panel.filter(pl.col("symbol") == "BTCUSDT")
            .with_columns(
                pl.col("d").cast(pl.String).alias("date"),
                (pl.col("d").cast(pl.Datetime(time_unit="ms", time_zone="UTC")).dt.epoch("ms")).alias("ts_ms"),
            )
            .select(["symbol", "date", "ts_ms", "close"])
        )
        summary[venue] = {}
        for mult in (1.0, 4.0):
            tag = "" if mult == 1.0 else f"_{mult:g}x"
            mdf = df
            if mult != 1.0:
                # Pure leverage on the same signal (the official tool's
                # --notional-multiplier convention): daily returns x mult,
                # recompounded. Margin/liquidation mechanics NOT modeled.
                rets = mdf["basket_return"].to_numpy() * mult
                eq = np.cumprod(1.0 + rets)
                mdf = mdf.with_columns(
                    pl.Series("basket_return", rets), pl.Series("equity", eq)
                )
            equity = mdf.with_columns(
                pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().cast(pl.String).alias("date")
            )
            equity.write_csv(out_dir / f"continuous_equity{tag}.csv")
            name = "Continuous (deployed cfg)" if mult == 1.0 else f"Continuous (deployed cfg) {mult:g}x"
            sub = "IN-SAMPLE RESEARCH (window spent; forward demo is the arbiter) — not a promotion claim"
            if mult != 1.0:
                sub += f" | {mult:g}x = pure leverage on the same returns; margin/liquidation NOT modeled"
            meta = _write_equity_benchmark_chart(
                out_dir,
                root=out_dir,
                equity=equity,
                raw_klines=raw_klines,
                monthly=None,
                png_name=f"continuous_equity_btc{tag}.png",
                title=f"CONTINUOUS deployed — winner_base ensemble + 2f hedge (max4){'' if mult == 1.0 else f', {mult:g}x levered'} [{venue}]",
                subtitle=sub,
                strategy_name=name,
            )
            summary[venue][f"{mult:g}x"] = stats(mdf)
            print(f"[{venue}] {mult:g}x {json.dumps(summary[venue][f'{mult:g}x'])}")
            print(f"[{venue}] png: {out_dir / f'continuous_equity_btc{tag}.png'} ({'ok' if meta else 'CHART FAILED'})")
    with open(OUT / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
