"""Executable form of the three ``lane2_*financed*`` / ``lane2_carry_hold`` configs.

Reads a cross-venue panel (``scripts/data/build_cross_venue_panel.py``) and produces
daily score rows. Two mechanisms, both long-only expressions of one premium: the
market pays longs while the short side is paying funding.

* Carry-hold — a per-name hysteresis state machine on the settled funding rate:
  enter LONG when funding prints below ``-enter_bp``, stay while it stays below
  ``-exit_bp``. Payment is funding received plus squeeze pressure on crowded
  shorts; measured attribution ~3.4 units funding per -1 unit price.
* Financed leaders — the top momentum decile, admitted only while the name's own
  funding is at-or-below the financing cap and BTC's prior-30d return clears the
  regime gate: ride leaders only while shorts finance the move.

Accounting conventions shared with the rest of the research surface:

* Decisions on a fixed 24h grid of hourly-close bars; entry at the decision
  close (``execution_delay_ms=0`` on top of bar completion). Both books
  strengthen under +1h/+4h entry delays.
* Funding accrues settlement-exact (``lane2_blend.settlement_exact_funding``).
* Costs are measured one-way turnover x the measured per-side fee, not a flat
  round trip per period.
* Per-name weight cap plus a total gross cap; uncapped, gross trebles during
  cascades.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
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
    """Committed carry-hold rule. Field names mirror the JSON.

    ``depth_ref_bp_per_day``: when set, a held name's weight is ``per_name_cap *
    clip(|trailing 24h settled funding| / ref, depth_floor, 1.0)`` — bet size
    proportional to the premium being paid. ``None`` keeps the flat per-name cap.
    """

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
    depth_ref_bp_per_day: float | None = None
    depth_floor: float = 0.25
    #: v3 filters, all default OFF so v1/v2 stay bit-identical.
    #: toxic_band: no entry, and holds suspend to zero weight, while the
    #: trailing 3d return sits in [lo, hi) — shorts are slowly right there.
    #: min_vol30: no entry while trailing 30d daily vol is below the floor
    #: (pinned price, no squeeze fuel). trail_recovery_exit: state ends when
    #: the trailing daily funding rate recovers by more than this over 2 days.
    toxic_band_ret3d: tuple[float, float] | None = None
    min_vol30_daily: float | None = None
    trail_recovery_exit_bp_2d: float | None = None

    @classmethod
    def from_json(cls, path: str | Path) -> "CarryHoldConfig":
        payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        rule = payload["rule"]
        depth = rule["sizing"].get("depth_scaling")
        if depth is not None and depth.get("basis") != "trail_fund_24h":
            raise FinancedLongsError(
                f"unsupported depth_scaling basis {depth.get('basis')!r}; "
                "only 'trail_fund_24h' is implemented"
            )
        filters = rule.get("filters") or {}
        band = filters.get("toxic_band_ret_3d")
        exit_vel = rule["state"].get("exit_on_trail_recovery_bp_2d")
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
            depth_ref_bp_per_day=(
                float(depth["ref_bp_per_day"]) if depth is not None else None
            ),
            depth_floor=float(depth["floor"]) if depth is not None else 0.25,
            toxic_band_ret3d=(
                (float(band["lo"]), float(band["hi"])) if band is not None else None
            ),
            min_vol30_daily=(
                float(filters["min_vol_30d_daily"])
                if filters.get("min_vol_30d_daily") is not None
                else None
            ),
            trail_recovery_exit_bp_2d=(
                float(exit_vel) if exit_vel is not None else None
            ),
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


def _settlement_flag() -> pl.Expr:
    """True on bars whose funding print settled at this bar's close.

    A bar carries a settlement iff the print's age just reset: age ~0
    (settlement at this bar's close) or an age drop versus the prior bar (the
    settlement bar itself is missing from the panel). Ages carry float-epsilon
    noise — an hour after a settlement the age reads 0.9999999999999999 — so
    the threshold must be well below 1.0 or every print is charged twice.
    """
    age = pl.col("by_funding_age_h")
    return (age < 0.5) | (age < age.shift(1).over("symbol")).fill_null(False)


def settlement_exact_funding(hold_hours: int) -> pl.Expr:
    """Funding a LONG pays over ``(t, t + hold_hours]``; settlements only."""
    fresh = pl.when(_settlement_flag()).then(pl.col("by_funding")).otherwise(0.0)
    return fresh.rolling_sum(hold_hours).over("symbol").shift(-hold_hours)


def _signal_frame(panel: pl.DataFrame, momentum_lookback_hours: int) -> pl.DataFrame:
    """Shared signal construction for the research and live frames.

    Attaches every column both frames use; callers apply their own row
    filter. Extracted so ``prepare`` (research, forward-return-gated) and
    ``prepare_decision`` (live, backward-only) can never drift apart.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in panel.columns]
    if missing:
        raise FinancedLongsError(f"panel is missing required columns: {missing}")
    close = pl.col("by_close")
    frame = panel.filter(close > 0).sort(["symbol", "bar_ts_ms"])
    frame = frame.with_columns(
        [
            pl.col("by_turnover_quote").rolling_sum(24).over("symbol").alias("adv24"),
            settlement_exact_funding(24).alias("funding_paid"),
            # PIT trailing settled funding over (t-24h, t]: the daily premium
            # currently being paid; v2's sizing basis. Settlements at bars
            # <= t only — same convention as the by_funding decision signal.
            pl.when(_settlement_flag()).then(pl.col("by_funding")).otherwise(0.0)
            .rolling_sum(24).over("symbol").alias("trail_fund_24h"),
            (close.shift(-24).over("symbol") / close - 1.0).alias("price_return"),
            (close / close.shift(momentum_lookback_hours).over("symbol") - 1.0).alias("momentum"),
            # v3 conditioning variables, all PIT at bars <= t: trailing 3d
            # return, trailing 30d vol of 24h returns (shifted a bar), and the
            # 2d change in the trailing daily funding rate.
            (close / close.shift(72).over("symbol") - 1.0).alias("ret_3d"),
            (close / close.shift(24).over("symbol") - 1.0).alias("_r24"),
            (
                pl.col("bar_ts_ms").shift(-24).over("symbol") - pl.col("bar_ts_ms") == 24 * HOUR_MS
            ).alias("contiguous"),
        ]
    )
    return frame.with_columns(
        pl.col("_r24").rolling_std(720).shift(1).over("symbol").alias("vol_30d_daily"),
        (pl.col("trail_fund_24h") - pl.col("trail_fund_24h").shift(48).over("symbol")).alias(
            "dtrail_2d"
        ),
    ).drop("_r24")


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


def prepare_decision(panel: pl.DataFrame, momentum_lookback_hours: int = 168) -> pl.DataFrame:
    """The LIVE frame: every bar whose backward-looking signals are mature.

    Identical signal construction to :func:`prepare`, but rows are kept
    whenever the 168h momentum lookback is satisfied; the forward-looking
    columns (``price_return``, ``funding_paid``, ``contiguous``) may be
    null here. A live decision cannot condition on the next 24h existing, so
    this frame keeps the bars the research frame drops: each symbol's terminal
    24h and bars ahead of data holes. That is the registered terminal-day frame
    caveat (~+0.13 Sharpe in the research frame's favor), so scored-vs-live
    divergence around symbol deaths is expected.
    """
    frame = _signal_frame(panel, momentum_lookback_hours)
    return frame.filter(pl.col("momentum").is_finite())


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
    """Hysteresis long state per name; per-name cap (optionally depth-scaled),
    total gross cap.

    The loop is deliberately explicit: the state at bar ``i`` depends only on
    settled funding at bars ``<= i``, which is the entire PIT argument. With
    ``depth_ref_bp_per_day`` set (v2), a held name's weight is scaled by
    ``clip(|trail_fund_24h| / ref, depth_floor, 1.0)`` — size follows the
    premium being paid; a missing trailing value fails to the floor, never up.
    """
    enter, exit_ = cfg.enter_bp / 1e4, cfg.exit_bp / 1e4
    sized = cfg.depth_ref_bp_per_day is not None
    banded = cfg.toxic_band_ret3d is not None
    volfloored = cfg.min_vol30_daily is not None
    veled = cfg.trail_recovery_exit_bp_2d is not None
    need = ["trail_fund_24h"] * sized + ["ret_3d"] * banded + ["vol_30d_daily"] * volfloored + ["dtrail_2d"] * veled
    missing = [c for c in dict.fromkeys(need) if c not in universe.columns]
    if missing:
        raise FinancedLongsError(
            f"{cfg.config_id}: enabled features require prepared columns {missing}"
        )
    cols = ["bar_ts_ms", "symbol", "by_funding", *dict.fromkeys(need)]
    d = (
        universe.select(cols)
        .drop_nulls(subset=["by_funding"])
        .sort(["symbol", "bar_ts_ms"])
    )
    ref = (cfg.depth_ref_bp_per_day or 0.0) / 1e4
    band_lo, band_hi = cfg.toxic_band_ret3d or (0.0, 0.0)
    vel_thr = (cfg.trail_recovery_exit_bp_2d or 0.0) / 1e4
    rows: dict[str, list] = {"bar_ts_ms": [], "symbol": [], "w": []}
    for (sym,), g in d.group_by("symbol", maintain_order=True):
        fv = g["by_funding"].to_numpy()
        tr = g["trail_fund_24h"].to_numpy() if sized else None
        bd = g["ret_3d"].to_numpy() if banded else None
        vf = g["vol_30d_daily"].to_numpy() if volfloored else None
        vl = g["dtrail_2d"].to_numpy() if veled else None
        ts = g["bar_ts_ms"].to_numpy()
        state = False
        for i in range(len(ts)):
            if state and not (fv[i] < -exit_):
                state = False
            if state and veled and vl is not None and math.isfinite(vl[i]) and vl[i] > vel_thr:
                state = False
            if fv[i] < -enter:
                # Filters block ENTRY only on known-bad values; a null
                # conditioning value fails open (young history), documented in
                # the v3 registration.
                in_band = bd is not None and math.isfinite(bd[i]) and band_lo <= bd[i] < band_hi
                dead = vf is not None and math.isfinite(vf[i]) and vf[i] < (cfg.min_vol30_daily or 0.0)
                if not ((banded and in_band) or (volfloored and dead)):
                    state = True
            if state:
                if banded and bd is not None and math.isfinite(bd[i]) and band_lo <= bd[i] < band_hi:
                    continue  # hold suspends to zero weight while in the band
                w = cfg.per_name_cap
                if sized and tr is not None:
                    depth = abs(tr[i]) if math.isfinite(tr[i]) else 0.0
                    w *= min(1.0, max(cfg.depth_floor, depth / ref))
                rows["bar_ts_ms"].append(int(ts[i]))
                rows["symbol"].append(str(sym))
                rows["w"].append(w)
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
    # Iterate every DECISION bar in the record, not only the bars that produced
    # weights: a gate-flip day selects nothing, so its exit and re-entry would
    # go uncharged and the flat day would vanish from the day count. The record
    # still spans first-weighted to last-weighted bar.
    weighted_bars = sorted(pivot)
    ts_sorted: list[int] = []
    if weighted_bars:
        low, high = weighted_bars[0], weighted_bars[-1]
        ts_sorted = [
            int(value)
            for value in sorted({int(item) for item in universe["bar_ts_ms"].unique().to_list()})
            if low <= int(value) <= high
        ]
    prev: dict[str, float] = {}
    rows: dict[str, list] = {"bar_ts_ms": [], "oneway": []}
    for t in ts_sorted:
        cur = pivot.get(t, {})
        rows["bar_ts_ms"].append(t)
        rows["oneway"].append(
            sum(abs(cur.get(s, 0.0) - prev.get(s, 0.0)) for s in set(cur) | set(prev))
        )
        prev = cur
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


def summarize(
    scores: pl.DataFrame, cfg: "CarryHoldConfig | FinancedLeadersConfig | FundingSpreadConfig"
) -> dict[str, float]:
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


@dataclasses.dataclass(frozen=True)
class FundingSpreadConfig:
    """Committed cross-venue neutral funding-spread rule.

    Long the perp on the venue whose funding is more negative, short the same
    symbol's perp on the other: the price legs cancel to basis noise and the
    P&L keeps the funding differential. Hysteresis on the trailing 24h settled
    spread (bybit minus binance, bp/day); costs charged on BOTH legs.
    """

    config_id: str
    universe_top_n: int
    enter_bp_per_day: float
    exit_bp_per_day: float
    per_name_cap: float
    gross_cap: float
    fee_side_bp: float
    vol_target_annual: float
    vol_lookback_days: int
    max_leverage: float

    @classmethod
    def from_json(cls, path: str | Path) -> "FundingSpreadConfig":
        payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        rule = payload["rule"]
        return cls(
            config_id=payload["config_id"],
            universe_top_n=int(rule["universe"]["top_n"]),
            enter_bp_per_day=float(rule["spread"]["enter_abs_bp_per_day"]),
            exit_bp_per_day=float(rule["spread"]["exit_abs_bp_per_day"]),
            per_name_cap=float(rule["sizing"]["per_name_cap"]),
            gross_cap=float(rule["sizing"]["gross_cap"]),
            fee_side_bp=float(payload["cost_model"]["measured_fee_side_bp"]),
            vol_target_annual=float(rule["risk"]["vol_target_annual"]),
            vol_lookback_days=int(rule["risk"]["vol_lookback_days"]),
            max_leverage=float(rule["risk"]["max_leverage"]),
        )


SPREAD_REQUIRED_COLUMNS = (
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_funding", "bn_funding_age_h",
)


def prepare_spread(panel: pl.DataFrame) -> pl.DataFrame:
    """Both-venue daily-grid universe for the neutral spread book.

    One row per (decision bar, both-venue mature name) with ``spread_bpd``
    (trailing 24h settled funding, bybit minus binance, bp/day) and
    ``net_return`` = the fwd-24h NEUTRAL P&L of being long the
    more-negative-funding venue (price legs netted, both venues' settled
    funding applied). ``net_return``'s name lets ``daily_scores`` reuse the
    registered cost machinery verbatim; the fee must be passed DOUBLED.
    """
    missing = [c for c in SPREAD_REQUIRED_COLUMNS if c not in panel.columns]
    if missing:
        raise FinancedLongsError(f"panel is missing spread columns: {missing}")

    def settle(age_col: str) -> pl.Expr:
        a = pl.col(age_col)
        return (a < 0.5) | (a < a.shift(1).over("symbol")).fill_null(False)

    byc, bnc = pl.col("by_close"), pl.col("bn_close")
    frame = panel.filter(byc > 0).sort(["symbol", "bar_ts_ms"]).with_columns(
        pl.when(settle("by_funding_age_h")).then(pl.col("by_funding")).otherwise(0.0)
        .rolling_sum(24).over("symbol").alias("trail_by"),
        pl.when(settle("bn_funding_age_h")).then(pl.col("bn_funding")).otherwise(0.0)
        .rolling_sum(24).over("symbol").alias("trail_bn"),
        pl.when(settle("by_funding_age_h")).then(pl.col("by_funding")).otherwise(0.0)
        .rolling_sum(24).over("symbol").shift(-24).alias("paid_by"),
        pl.when(settle("bn_funding_age_h")).then(pl.col("bn_funding")).otherwise(0.0)
        .rolling_sum(24).over("symbol").shift(-24).alias("paid_bn"),
        (byc.shift(-24).over("symbol") / byc - 1.0).alias("ret_by"),
        (bnc.shift(-24).over("symbol") / bnc - 1.0).alias("ret_bn"),
        pl.col("by_turnover_quote").rolling_sum(24).over("symbol").alias("adv24"),
        byc.shift(168).over("symbol").is_not_null().alias("_mby"),
        bnc.shift(168).over("symbol").is_not_null().alias("_mbn"),
        (
            pl.col("bar_ts_ms").shift(-24).over("symbol") - pl.col("bar_ts_ms") == 24 * HOUR_MS
        ).alias("contiguous"),
    )
    frame = frame.filter(
        pl.col("contiguous") & pl.col("_mby") & pl.col("_mbn")
        & pl.col("trail_by").is_finite() & pl.col("trail_bn").is_finite()
        & pl.col("ret_by").is_finite() & pl.col("ret_bn").is_finite()
        & pl.col("paid_by").is_finite() & pl.col("paid_bn").is_finite()
    )
    sgn = pl.when(pl.col("trail_by") <= pl.col("trail_bn")).then(1.0).otherwise(-1.0)
    return frame.with_columns(
        ((pl.col("trail_by") - pl.col("trail_bn")) * 1e4).alias("spread_bpd"),
        (
            sgn
            * ((pl.col("ret_by") - pl.col("ret_bn")) - pl.col("paid_by") + pl.col("paid_bn"))
        ).alias("net_return"),
    ).drop("_mby", "_mbn")


def funding_spread_weights(universe: pl.DataFrame, cfg: FundingSpreadConfig) -> pl.DataFrame:
    """Signed hysteresis on |spread|; the emitted weight is the (positive)
    one-side notional — direction is embedded in ``net_return``'s sign
    convention, and a direction flip forces an exit before any re-entry."""
    d = (
        universe.select("bar_ts_ms", "symbol", "spread_bpd")
        .drop_nulls()
        .sort(["symbol", "bar_ts_ms"])
    )
    rows: dict[str, list] = {"bar_ts_ms": [], "symbol": [], "w": []}
    for (sym,), g in d.group_by("symbol", maintain_order=True):
        sp = g["spread_bpd"].to_numpy()
        ts = g["bar_ts_ms"].to_numpy()
        state = 0
        for i in range(len(ts)):
            if state != 0 and (
                abs(sp[i]) < cfg.exit_bp_per_day or (1.0 if sp[i] < 0 else -1.0) != state
            ):
                state = 0
            if sp[i] < -cfg.enter_bp_per_day:
                state = 1
            elif sp[i] > cfg.enter_bp_per_day:
                state = -1
            if state != 0:
                rows["bar_ts_ms"].append(int(ts[i]))
                rows["symbol"].append(str(sym))
                rows["w"].append(cfg.per_name_cap)
    weights = pl.DataFrame(
        rows, schema={"bar_ts_ms": pl.Int64, "symbol": pl.String, "w": pl.Float64}
    )
    return _apply_gross_cap(weights, cfg.gross_cap)


def score_funding_spread(panel: pl.DataFrame, cfg: FundingSpreadConfig) -> dict[str, Any]:
    universe = top_n_universe(daily_grid(prepare_spread(panel)), cfg.universe_top_n)
    weights = funding_spread_weights(universe, cfg)
    # BOTH legs trade: the per-side fee is charged twice per unit one-way.
    scores = daily_scores(weights, universe, 2.0 * cfg.fee_side_bp)
    out: dict[str, Any] = {"config_id": cfg.config_id, "venue": "bybit+binance"}
    out.update(summarize(scores, cfg))
    return out


def config_scores(panel: pl.DataFrame, config_path: str | Path) -> tuple[pl.DataFrame, pl.DataFrame, str, str]:
    """Daily score rows for any registered financed-longs config JSON.

    Returns ``(scores, venue_view_frame, config_id, venue)``. Dispatches on the
    committed rule shape: a ``rule.state`` block is carry-hold's hysteresis; a
    ``rule.signal`` block is financed-leaders; a ``rule.spread`` block is the
    cross-venue neutral funding-spread book.
    """
    payload: dict[str, Any] = json.loads(Path(config_path).read_text(encoding="utf-8"))
    rule = payload.get("rule") or {}
    if "spread" in rule:
        spread_cfg = FundingSpreadConfig.from_json(config_path)
        universe = top_n_universe(daily_grid(prepare_spread(panel)), spread_cfg.universe_top_n)
        weights = funding_spread_weights(universe, spread_cfg)
        scores = daily_scores(weights, universe, 2.0 * spread_cfg.fee_side_bp)
        return scores, panel, spread_cfg.config_id, "bybit+binance"
    if "state" in rule:
        carry = CarryHoldConfig.from_json(config_path)
        view = venue_view(panel, carry.venue)
        universe = top_n_universe(daily_grid(prepare(view)), carry.universe_top_n)
        weights = carry_hold_weights(universe, carry)
        return daily_scores(weights, universe, carry.fee_side_bp), view, carry.config_id, carry.venue
    if "signal" in rule:
        leaders = FinancedLeadersConfig.from_json(config_path)
        view = venue_view(panel, leaders.venue)
        grid = daily_grid(prepare(view, leaders.momentum_lookback_hours))
        universe = top_n_universe(grid, leaders.universe_top_n)
        weights = financed_leaders_weights(universe, btc_gate(grid, leaders.btc_gate_lookback_days), leaders)
        return daily_scores(weights, universe, leaders.fee_side_bp), view, leaders.config_id, leaders.venue
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
    from .volume_events_charts import _write_equity_benchmark_chart

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
