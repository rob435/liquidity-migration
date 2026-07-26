"""Executable form of the three ``lane2_*financed*`` / ``lane2_carry_hold`` configs.

Research-only. Reads a cross-venue panel (``scripts/build_cross_venue_panel.py``)
and produces daily score rows. No venue access, no order path, no runtime
surface; nothing here can open the real-money door.

Two mechanisms, selected in the 2026-07-26 Lane-1 program
(``docs/research_2026-07-26_financed_longs.md``), both long-only expressions of
one macro-premium: the market pays longs while the short side is paying funding.

* **Carry-hold** — a per-name hysteresis state machine on the settled funding
  rate: enter LONG when funding prints below ``-enter_bp``, stay while it stays
  below ``-exit_bp``. Payment = funding received plus the squeeze pressure on
  crowded shorts; measured attribution is ~3.4 units funding per -1 unit price.
* **Financed leaders** — the top momentum decile, admitted only while the
  name's own funding is at-or-below the financing cap (longs not paying above
  baseline) and BTC's prior-30d return clears the regime gate. Rationale: ride
  leaders only while shorts finance the move; a leader whose longs are paying
  is crowded and reverts (the D3a result, same document).

Accounting conventions shared with the rest of the research surface:

* Decisions on a fixed 24h grid of hourly-close bars; entry at the decision
  close (``execution_delay_ms=0`` on top of bar completion — the house
  convention; both books strengthen, not weaken, under +1h/+4h entry delays).
* Funding accrues settlement-exact (``lane2_blend.settlement_exact_funding``).
* Costs are charged as measured one-way turnover x the measured per-side fee,
  not a flat round trip per period (docs/anomaly_research_2026-07-24.md §17.1).
* Per-name weight cap plus a total gross cap: the uncapped book trebles gross
  exactly during cascades, which is the opposite of the design intent.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .cross_section import MEASURED_ROUND_TRIP_BP

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

#: Measured demo taker fee per side (bp); provenance in cross_section.
MEASURED_FEE_SIDE_BP = MEASURED_ROUND_TRIP_BP / 2.0

REQUIRED_COLUMNS = (
    "symbol",
    "bar_ts_ms",
    "by_close",
    "by_turnover_quote",
    "by_funding",
    "by_funding_age_h",
)

#: Renames that make Binance the traded venue. One implementation, two venues:
#: the replication arms must not be two different code paths.
BINANCE_VIEW = {
    "bn_close": "by_close",
    "bn_turnover_quote": "by_turnover_quote",
    "bn_funding": "by_funding",
    "bn_funding_age_h": "by_funding_age_h",
}


class FinancedLongsError(ValueError):
    """A financed-longs read was requested with incoherent inputs."""


@dataclasses.dataclass(frozen=True)
class CarryHoldConfig:
    """Committed carry-hold rule. Field names mirror the JSON."""

    config_id: str
    venue: str
    universe_top_n: int
    enter_bp: float
    exit_bp: float
    per_name_cap: float
    gross_cap: float
    fee_side_bp: float
    vol_target_annual: float
    vol_lookback_days: int
    max_leverage: float

    @classmethod
    def from_json(cls, path: str | Path) -> "CarryHoldConfig":
        payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        rule = payload["rule"]
        return cls(
            config_id=payload["config_id"],
            venue=rule["universe"]["venue"],
            universe_top_n=int(rule["universe"]["top_n"]),
            enter_bp=float(rule["state"]["enter_below_funding_bp"]),
            exit_bp=float(rule["state"]["exit_above_funding_bp"]),
            per_name_cap=float(rule["sizing"]["per_name_cap"]),
            gross_cap=float(rule["sizing"]["gross_cap"]),
            fee_side_bp=float(payload["cost_model"]["measured_fee_side_bp"]),
            vol_target_annual=float(rule["risk"]["vol_target_annual"]),
            vol_lookback_days=int(rule["risk"]["vol_lookback_days"]),
            max_leverage=float(rule["risk"]["max_leverage"]),
        )


@dataclasses.dataclass(frozen=True)
class FinancedLeadersConfig:
    """Committed financed-leaders rule. Field names mirror the JSON."""

    config_id: str
    venue: str
    universe_top_n: int
    momentum_lookback_hours: int
    leader_percentile: float
    funding_cap_bp: float
    btc_gate_lookback_days: int
    btc_gate_threshold: float
    per_name_cap: float
    gross_cap: float
    fee_side_bp: float
    vol_target_annual: float
    vol_lookback_days: int
    max_leverage: float

    @classmethod
    def from_json(cls, path: str | Path) -> "FinancedLeadersConfig":
        payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        rule = payload["rule"]
        return cls(
            config_id=payload["config_id"],
            venue=rule["universe"]["venue"],
            universe_top_n=int(rule["universe"]["top_n"]),
            momentum_lookback_hours=int(rule["signal"]["momentum_lookback_hours"]),
            leader_percentile=float(rule["signal"]["leader_percentile"]),
            funding_cap_bp=float(rule["signal"]["funding_cap_bp"]),
            btc_gate_lookback_days=int(rule["gate"]["btc_lookback_days"]),
            btc_gate_threshold=float(rule["gate"]["btc_threshold"]),
            per_name_cap=float(rule["sizing"]["per_name_cap"]),
            gross_cap=float(rule["sizing"]["gross_cap"]),
            fee_side_bp=float(payload["cost_model"]["measured_fee_side_bp"]),
            vol_target_annual=float(rule["risk"]["vol_target_annual"]),
            vol_lookback_days=int(rule["risk"]["vol_lookback_days"]),
            max_leverage=float(rule["risk"]["max_leverage"]),
        )


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


def settlement_exact_funding(hold_hours: int) -> pl.Expr:
    """Funding a LONG pays over ``(t, t + hold_hours]``; settlements only."""
    fresh = (
        pl.when(pl.col("by_funding_age_h") < 1.0)
        .then(pl.col("by_funding"))
        .otherwise(0.0)
    )
    return fresh.rolling_sum(hold_hours).over("symbol").shift(-hold_hours)


def prepare(panel: pl.DataFrame, momentum_lookback_hours: int = 168) -> pl.DataFrame:
    """Attach adv24, forward 24h net return, momentum, and contiguity."""
    missing = [c for c in REQUIRED_COLUMNS if c not in panel.columns]
    if missing:
        raise FinancedLongsError(f"panel is missing required columns: {missing}")
    close = pl.col("by_close")
    frame = panel.filter(close > 0).sort(["symbol", "bar_ts_ms"])
    frame = frame.with_columns(
        [
            pl.col("by_turnover_quote").rolling_sum(24).over("symbol").alias("adv24"),
            settlement_exact_funding(24).alias("funding_paid"),
            (close.shift(-24).over("symbol") / close - 1.0).alias("price_return"),
            (close / close.shift(momentum_lookback_hours).over("symbol") - 1.0).alias("momentum"),
            (
                pl.col("bar_ts_ms").shift(-24).over("symbol") - pl.col("bar_ts_ms") == 24 * HOUR_MS
            ).alias("contiguous"),
        ]
    )
    frame = frame.filter(
        pl.col("contiguous")
        & pl.col("price_return").is_finite()
        & pl.col("funding_paid").is_finite()
        & pl.col("momentum").is_finite()
    )
    return frame.with_columns(
        (pl.col("price_return") - pl.col("funding_paid")).alias("net_return")
    )


def daily_grid(frame: pl.DataFrame) -> pl.DataFrame:
    """Sample one decision bar per 24h so holding windows never overlap."""
    if frame.height == 0:
        raise FinancedLongsError(
            "prepared panel is empty; the momentum lookback plus the forward "
            "24h hold need more history than the input provides"
        )
    origin = int(frame["bar_ts_ms"].min())  # type: ignore[arg-type]
    return (
        frame.with_columns(((pl.col("bar_ts_ms") - origin) // HOUR_MS).alias("_off"))
        .filter(pl.col("_off") % 24 == 0)
        .drop("_off")
    )


def top_n_universe(grid: pl.DataFrame, top_n: int) -> pl.DataFrame:
    return (
        grid.with_columns(
            pl.col("adv24").rank("ordinal", descending=True).over("bar_ts_ms").alias("_rk")
        )
        .filter(pl.col("_rk") <= top_n)
        .drop("_rk")
    )


def btc_gate(grid: pl.DataFrame, lookback_days: int) -> pl.DataFrame:
    """Prior-N-day BTC return per decision bar, excluding the current bar."""
    btc = (
        grid.filter(pl.col("symbol") == "BTCUSDT")
        .sort("bar_ts_ms")
        .select("bar_ts_ms", "by_close")
    )
    return btc.with_columns(
        (pl.col("by_close").shift(1) / pl.col("by_close").shift(1 + lookback_days) - 1.0).alias(
            "btc_trend"
        )
    ).select("bar_ts_ms", "btc_trend")


def _apply_gross_cap(weights: pl.DataFrame, gross_cap: float) -> pl.DataFrame:
    if weights.height == 0:
        return weights
    g = weights.group_by("bar_ts_ms").agg(pl.col("w").abs().sum().alias("_g"))
    return (
        weights.join(g, on="bar_ts_ms", how="left")
        .with_columns(
            pl.when(pl.col("_g") > gross_cap)
            .then(pl.col("w") * gross_cap / pl.col("_g"))
            .otherwise(pl.col("w"))
            .alias("w")
        )
        .select("bar_ts_ms", "symbol", "w")
    )


def carry_hold_weights(universe: pl.DataFrame, cfg: CarryHoldConfig) -> pl.DataFrame:
    """Hysteresis long state per name; fixed per-name cap, total gross cap.

    The loop is deliberately explicit: the state at bar ``i`` depends only on
    settled funding at bars ``<= i``, which is the entire PIT argument.
    """
    enter, exit_ = cfg.enter_bp / 1e4, cfg.exit_bp / 1e4
    d = (
        universe.select("bar_ts_ms", "symbol", "by_funding")
        .drop_nulls()
        .sort(["symbol", "bar_ts_ms"])
    )
    rows: dict[str, list] = {"bar_ts_ms": [], "symbol": [], "w": []}
    for (sym,), g in d.group_by("symbol", maintain_order=True):
        fv = g["by_funding"].to_numpy()
        ts = g["bar_ts_ms"].to_numpy()
        state = False
        for i in range(len(ts)):
            if state and not (fv[i] < -exit_):
                state = False
            if fv[i] < -enter:
                state = True
            if state:
                rows["bar_ts_ms"].append(int(ts[i]))
                rows["symbol"].append(str(sym))
                rows["w"].append(cfg.per_name_cap)
    weights = pl.DataFrame(
        rows, schema={"bar_ts_ms": pl.Int64, "symbol": pl.String, "w": pl.Float64}
    )
    return _apply_gross_cap(weights, cfg.gross_cap)


def financed_leaders_weights(
    universe: pl.DataFrame, gate: pl.DataFrame, cfg: FinancedLeadersConfig
) -> pl.DataFrame:
    """Top momentum decile, financed (funding <= cap), inside the BTC gate."""
    d = universe.join(gate, on="bar_ts_ms", how="left").filter(
        pl.col("btc_trend") > cfg.btc_gate_threshold
    )
    d = d.filter(pl.len().over("bar_ts_ms") >= 10).with_columns(
        (
            (pl.col("momentum").rank("ordinal").over("bar_ts_ms") - 0.5)
            / pl.len().over("bar_ts_ms")
        ).alias("_p")
    )
    sel = d.filter(
        (pl.col("_p") >= cfg.leader_percentile)
        & (pl.col("by_funding") <= cfg.funding_cap_bp / 1e4)
    )
    n = sel.group_by("bar_ts_ms").agg(pl.len().alias("_n"))
    weights = (
        sel.join(n, on="bar_ts_ms", how="left")
        .with_columns(
            pl.min_horizontal(pl.lit(cfg.per_name_cap), 1.0 / pl.col("_n")).alias("w")
        )
        .select("bar_ts_ms", "symbol", "w")
    )
    return _apply_gross_cap(weights, cfg.gross_cap)


def daily_scores(
    weights: pl.DataFrame, universe: pl.DataFrame, fee_side_bp: float
) -> pl.DataFrame:
    """One row per decision day: gross, measured-turnover cost, and net, in bp."""
    rets = universe.select("bar_ts_ms", "symbol", "net_return")
    j = weights.join(rets, on=["bar_ts_ms", "symbol"], how="left").with_columns(
        (pl.col("w") * pl.col("net_return").fill_null(0.0)).alias("_pnl")
    )
    gross = j.group_by("bar_ts_ms").agg((pl.col("_pnl").sum() * 1e4).alias("gross_bp"))
    pivot = {
        int(k[0] if isinstance(k, tuple) else k): dict(zip(v["symbol"].to_list(), v["w"].to_list()))
        for k, v in weights.partition_by("bar_ts_ms", as_dict=True).items()
    }
    ts_sorted = sorted(pivot)
    prev: dict[str, float] = {}
    rows: dict[str, list] = {"bar_ts_ms": [], "oneway": []}
    for t in ts_sorted:
        cur = pivot[t]
        rows["bar_ts_ms"].append(t)
        rows["oneway"].append(
            sum(abs(cur.get(s, 0.0) - prev.get(s, 0.0)) for s in set(cur) | set(prev))
        )
        prev = cur
    turn = pl.DataFrame(rows, schema={"bar_ts_ms": pl.Int64, "oneway": pl.Float64})
    return (
        gross.join(turn, on="bar_ts_ms", how="left")
        .with_columns((pl.col("oneway").fill_null(0.0) * fee_side_bp).alias("cost_bp"))
        .with_columns((pl.col("gross_bp") - pl.col("cost_bp")).alias("net_bp"))
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


def summarize(scores: pl.DataFrame, cfg: CarryHoldConfig | FinancedLeadersConfig) -> dict[str, float]:
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


def score_financed_leaders(panel: pl.DataFrame, cfg: FinancedLeadersConfig) -> dict[str, Any]:
    view = venue_view(panel, cfg.venue)
    grid = daily_grid(prepare(view, cfg.momentum_lookback_hours))
    universe = top_n_universe(grid, cfg.universe_top_n)
    gate = btc_gate(grid, cfg.btc_gate_lookback_days)
    weights = financed_leaders_weights(universe, gate, cfg)
    scores = daily_scores(weights, universe, cfg.fee_side_bp)
    out: dict[str, Any] = {"config_id": cfg.config_id, "venue": cfg.venue}
    out.update(summarize(scores, cfg))
    return out
