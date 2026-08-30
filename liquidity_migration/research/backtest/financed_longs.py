"""Executable form of the ``lane2_carry_hold`` configs.

Reads a cross-venue panel (``scripts/data/build_cross_venue_panel.py``) and produces
daily score rows for carry-hold — a per-name hysteresis state machine on the
settled funding rate: enter LONG when funding prints below ``-enter_bp``, stay
while it stays below ``-exit_bp``. Payment is funding received plus squeeze
pressure on crowded shorts; measured attribution ~3.4 units funding per -1 unit
price. (The financed-leaders and funding-spread expressions this module also
carried were deleted 2026-08-19 by operator override, with their configs.)

Accounting conventions shared with the rest of the research surface:

* Decisions on a fixed 24h grid of hourly-close bars; entry at the decision
  close (``execution_delay_ms=0`` on top of bar completion). Entry delays were
  free at v1 registration but are not on v4 — every fill-delay arm measured
  2026-08-03 is flat-to-negative (research_findings §2, settlement-instant
  timing).
* Funding accrues settlement-exact (``carry_hold.settlement_exact_funding``).
* Costs are measured one-way turnover x the measured per-side fee, not a flat
  round trip per period.
* Per-name weight cap plus a total gross cap; uncapped, gross trebles during
  cascades.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.rules.carry_hold import (
    CarryHoldConfig,
    FinancedLongsError,
    _signal_frame,
    carry_hold_weights,
    daily_grid,
    top_n_universe,
)

#: Renames that make Binance the traded venue. One implementation, two venues:
#: the replication arms must not be two different code paths.
BINANCE_VIEW = {
    "bn_close": "by_close",
    "bn_turnover_quote": "by_turnover_quote",
    "bn_funding": "by_funding",
    "bn_funding_age_h": "by_funding_age_h",
}


def venue_view(panel: pl.DataFrame, venue: str) -> pl.DataFrame:
    """Return the panel with ``venue`` as the traded venue."""
    if venue == "bybit":
        return panel
    if venue != "binance":
        raise FinancedLongsError(f"unknown venue {venue!r}")
    missing = [c for c in BINANCE_VIEW if c not in panel.columns]
    if missing:
        raise FinancedLongsError(f"panel lacks Binance columns: {missing}")
    keep = [c for c in panel.columns if c not in set(BINANCE_VIEW.values())]
    return panel.select(keep).rename(BINANCE_VIEW)


def prepare(panel: pl.DataFrame, momentum_lookback_hours: int = 168) -> pl.DataFrame:
    """Attach adv24, forward 24h net return, momentum, and contiguity."""
    frame = _signal_frame(panel, momentum_lookback_hours)
    frame = frame.filter(
        pl.col("contiguous")
        & pl.col("price_return").is_finite()
        & pl.col("funding_paid").is_finite()
        & pl.col("momentum").is_finite()
    )
    return frame.with_columns(
        (pl.col("price_return") - pl.col("funding_paid")).alias("net_return")
    )


def daily_scores(
    weights: pl.DataFrame, universe: pl.DataFrame, fee_side_bp: float
) -> pl.DataFrame:
    """Daily gross, turnover cost, and net rows, including cash and final liquidation."""
    rets = universe.select("bar_ts_ms", "symbol", "net_return")
    j = weights.join(rets, on=["bar_ts_ms", "symbol"], how="left").with_columns(
        (pl.col("w") * pl.col("net_return").fill_null(0.0)).alias("_pnl")
    )
    gross = j.group_by("bar_ts_ms").agg((pl.col("_pnl").sum() * 1e4).alias("gross_bp"))
    pivot = {
        int(k[0] if isinstance(k, tuple) else k): dict(zip(v["symbol"].to_list(), v["w"].to_list()))
        for k, v in weights.partition_by("bar_ts_ms", as_dict=True).items()
    }
    # Score the whole decision record. Flat bars are cash, and the first flat
    # bar after a hold carries the exit turnover.
    ts_sorted = sorted({int(item) for item in universe["bar_ts_ms"].unique().to_list()})
    prev: dict[str, float] = {}
    rows: dict[str, list] = {"bar_ts_ms": [], "oneway": []}
    for t in ts_sorted:
        cur = pivot.get(t, {})
        rows["bar_ts_ms"].append(t)
        rows["oneway"].append(
            sum(abs(cur.get(s, 0.0) - prev.get(s, 0.0)) for s in set(cur) | set(prev))
        )
        prev = cur
    if prev and rows["oneway"]:
        rows["oneway"][-1] += sum(abs(weight) for weight in prev.values())
    turn = pl.DataFrame(rows, schema={"bar_ts_ms": pl.Int64, "oneway": pl.Float64})
    return (
        turn.join(gross, on="bar_ts_ms", how="left")
        .with_columns(pl.col("gross_bp").fill_null(0.0).alias("gross_bp"))
        .with_columns((pl.col("oneway").fill_null(0.0) * fee_side_bp).alias("cost_bp"))
        .with_columns((pl.col("gross_bp") - pl.col("cost_bp")).alias("net_bp"))
        .select("bar_ts_ms", "gross_bp", "oneway", "cost_bp", "net_bp")
        .sort("bar_ts_ms")
    )


def volatility_scale(
    net_bp: np.ndarray, *, target_annual: float, lookback_days: int, max_leverage: float
) -> np.ndarray:
    """Leverage per day from volatility measured strictly before that day."""
    returns = np.asarray(net_bp, dtype=float) / 1e4
    scale = np.zeros(len(returns))
    for i in range(lookback_days, len(returns)):
        realized = returns[i - lookback_days : i].std(ddof=1) * math.sqrt(365.0)
        if realized > 0:
            scale[i] = min(target_annual / realized, max_leverage)
    return scale


def summarize(scores: pl.DataFrame, cfg: CarryHoldConfig) -> dict[str, float]:
    """Scoring recipe: raw and vol-targeted Sharpe, compounded return/drawdown."""
    net = scores["net_bp"].to_numpy()
    if len(net) < 2:
        return {"days": float(len(net))}
    raw = net / 1e4
    sd = raw.std(ddof=1)
    lev = volatility_scale(
        net,
        target_annual=cfg.vol_target_annual,
        lookback_days=cfg.vol_lookback_days,
        max_leverage=cfg.max_leverage,
    )
    extra_cost = np.abs(np.diff(lev, prepend=0.0)) * (cfg.fee_side_bp / 1e4)
    scaled = lev * raw - extra_cost
    ssd = scaled.std(ddof=1)
    eq_raw = np.cumprod(1.0 + raw)
    eq_vt = np.cumprod(1.0 + scaled)
    return {
        "days": float(len(net)),
        "mean_net_bp_per_day": float(net.mean()),
        "sharpe_raw": float(raw.mean() / sd * math.sqrt(365.0)) if sd > 0 else 0.0,
        "sharpe_vol_targeted": float(scaled.mean() / ssd * math.sqrt(365.0)) if ssd > 0 else 0.0,
        "total_return_raw_pct": float((eq_raw[-1] - 1.0) * 100.0),
        "total_return_vt_pct": float((eq_vt[-1] - 1.0) * 100.0),
        "max_drawdown_vt_pct": float(
            np.max(1.0 - eq_vt / np.maximum.accumulate(np.maximum(eq_vt, 1e-12))) * 100.0
        ),
        "worst_day_vt_pct": float(scaled.min() * 100.0),
        "mean_oneway_turnover": float(scores["oneway"].mean() or 0.0),  # type: ignore[arg-type]
    }


def score_carry_hold(panel: pl.DataFrame, cfg: CarryHoldConfig) -> dict[str, Any]:
    view = venue_view(panel, cfg.venue)
    grid = daily_grid(prepare(view))
    universe = top_n_universe(grid, cfg.universe_top_n)
    weights = carry_hold_weights(universe, cfg)
    scores = daily_scores(weights, universe, cfg.fee_side_bp)
    out: dict[str, Any] = {"config_id": cfg.config_id, "venue": cfg.venue}
    out.update(summarize(scores, cfg))
    return out


def config_scores(panel: pl.DataFrame, config_path: str | Path) -> tuple[pl.DataFrame, pl.DataFrame, str, str]:
    """Daily score rows for a registered carry-hold config JSON.

    Returns ``(scores, venue_view_frame, config_id, venue)``. Only the
    carry-hold rule shape (a ``rule.state`` block) remains; the leaders and
    spread shapes were deleted 2026-08-19 by operator override.
    """
    payload: dict[str, Any] = json.loads(Path(config_path).read_text(encoding="utf-8"))
    rule = payload.get("rule") or {}
    if "state" in rule:
        carry = CarryHoldConfig.from_json(config_path)
        view = venue_view(panel, carry.venue)
        universe = top_n_universe(daily_grid(prepare(view)), carry.universe_top_n)
        weights = carry_hold_weights(universe, carry)
        return daily_scores(weights, universe, carry.fee_side_bp), view, carry.config_id, carry.venue
    raise FinancedLongsError(f"unrecognized financed-longs rule shape in {config_path}")


def research_equity_chart(
    panel: pl.DataFrame,
    config_path: str | Path,
    output_dir: str | Path,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Render a registered financed-longs config through the standard equity
    chart renderer, labelled as research.

    ``end`` is exclusive; the daily series is the settlement-exact scorer's
    full-calendar record clipped to ``[start, end)`` and compounded at native
    raw-book size (no presentation leverage).
    """
    from liquidity_migration.research.backtest.volume_events_charts import _write_equity_benchmark_chart

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    scores, view, config_id, venue = config_scores(panel, config_path)
    start_ms = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC).timestamp() * 1000)
    end_ms = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC).timestamp() * 1000)
    window = scores.filter((pl.col("bar_ts_ms") >= start_ms) & (pl.col("bar_ts_ms") < end_ms)).sort("bar_ts_ms")
    if window.height < 2:
        raise FinancedLongsError(f"{config_id}: fewer than 2 scored days in [{start}, {end})")

    returns = window["net_bp"].to_numpy() / 1e4
    equity_values = np.cumprod(1.0 + returns)
    days = [
        dt.datetime.fromtimestamp(int(ts) / 1000, dt.UTC).date().isoformat()
        for ts in window["bar_ts_ms"].to_list()
    ]
    equity = pl.DataFrame({"date": days, "equity": equity_values})
    equity.write_csv(out / f"{config_id}_daily_equity.csv")

    years = max((dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days, 1) / 365.25
    total = float(equity_values[-1] - 1.0)
    annualized = float(equity_values[-1] ** (1.0 / years) - 1.0)
    drawdown = float((equity_values / np.maximum.accumulate(equity_values) - 1.0).min())
    deviation = float(returns.std(ddof=1))
    metrics = {
        "total_return_pct": total * 100.0,
        "annualized_pct": annualized * 100.0,
        "max_drawdown_pct": drawdown * 100.0,
        "worst_day_pct": float(returns.min()) * 100.0,
        "sharpe_daily_ann": float(returns.mean() / deviation * math.sqrt(365.0)) if deviation > 0 else 0.0,
        "mar": (annualized / abs(drawdown)) if drawdown < 0 else None,
        "years": years,
    }

    raw_klines = (
        view.filter(pl.col("symbol") == "BTCUSDT")
        .select("symbol", "bar_ts_ms", "by_close")
        .rename({"bar_ts_ms": "ts_ms", "by_close": "close"})
        .with_columns(
            pl.from_epoch("ts_ms", time_unit="ms").dt.date().cast(pl.String).alias("date")
        )
    )
    chart = _write_equity_benchmark_chart(
        out,
        equity=equity,
        raw_klines=raw_klines,
        monthly=None,
        png_name=f"{config_id}_equity_btc.png",
        title=f"RESEARCH {config_id} [{venue}] - registered Lane-2 config",
        subtitle=(
            "SIMULATION ON SEEN DATA - opinion, not evidence. Corrected settlement-exact scorer; "
            f"native raw-book size (no presentation leverage); window {start} -> {end} (end exclusive)."
        ),
        step=False,
        strategy_name=config_id,
        metrics=metrics,
    )
    run_label = f"{config_id}_research_seen_data_corrected_scorer"
    payload = {
        "run_label": run_label,
        "summary": {
            "total_return": total,
            "max_drawdown": drawdown,
            "sharpe_like": metrics["sharpe_daily_ann"],
            "mar": metrics["mar"],
        },
        "metrics": metrics,
        "png": chart.get("png"),
        "config_id": config_id,
        "venue": venue,
    }
    (out / f"{config_id}_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload
