"""Registered CARRY config parsing and causal feature construction.

Rust owns the hysteresis and weight decisions. Python constructs the
settlement-exact feature frame used by research replays.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import calendar_roll, calendar_shift

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


REQUIRED_COLUMNS = (
    "symbol",
    "bar_ts_ms",
    "by_close",
    "by_turnover_quote",
    "by_funding",
    "by_funding_age_h",
)


class FinancedLongsError(ValueError):
    """A financed-longs read was requested with incoherent inputs."""


@dataclasses.dataclass(frozen=True)
class CarryHoldConfig:
    """Committed carry-hold rule. Field names mirror the JSON.

    ``depth_ref_bp_per_day``: when set, a held name's weight is ``per_name_cap *
    clip((|trailing 24h settled funding| / ref) ** depth_exponent, depth_floor,
    1.0)`` — bet size follows the premium being paid, bent by the exponent
    (1.0 = the straight v2..v5 ladder). ``None`` keeps the flat per-name cap.
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
    #: v6 sizing, default 1.0 so v1..v5 stay bit-identical. Bends the depth
    #: ladder: the ratio |trail_fund_24h|/ref is raised to this power before
    #: the clip, so mid-depth names get less size while the floor and the cap
    #: do not move. Chosen 2026-08-19 after every other response-shape cell
    #: (smoothed cuts, softened kills, raised caps, inverse-vol) failed its
    #: battery; this one passed 24/24 clock phases and a 0/20 placebo.
    depth_exponent: float = 1.0
    #: v3 filters, all default OFF so v1/v2 stay bit-identical.
    #: toxic_band: no entry, and holds suspend to zero weight, while the
    #: trailing 3d return sits in [lo, hi) — shorts are slowly right there.
    #: min_vol30: no entry while trailing 30d daily vol is below the floor
    #: (pinned price, no squeeze fuel). trail_recovery_exit: state ends when
    #: the trailing daily funding rate recovers by more than this over 2 days.
    toxic_band_ret3d: tuple[float, float] | None = None
    min_vol30_daily: float | None = None
    trail_recovery_exit_bp_2d: float | None = None
    #: v4 sizing, default OFF so v1/v2/v3 stay bit-identical. Crowding
    #: persistence is the share of the symbol's last ``persistence_window``
    #: SETTLEMENTS whose rate was deeper than ``enter_bp``. It answers a
    #: different question from ``depth_ref_bp_per_day``: depth is how much the
    #: crowd is paying now, persistence is whether that is a pattern or a blip.
    #: The two multiply. Measured in the symbol's own settlement sequence rather
    #: than on a clock, because Bybit's interval mix went from 100% 8h in 2021 to
    #: 52% 4h / 21% 1h in 2025 and any hours-based version reports the cadence.
    persistence_window: int | None = None
    persistence_cut: float = 0.10
    #: Weight multiplier at or below the cut. 0.0 drops the name entirely.
    persistence_lo: float = 0.0
    #: v5 sizing, default OFF so v1..v4 stay bit-identical. Two multipliers on
    #: axes deliberately OUTSIDE the funding/price complex (any depth-correlated
    #: cut removes the book's payoff days — measured 2026-08-19, ~60 cells).
    #: flow: trailing 24h turnover vs 72h earlier; a held name whose turnover
    #: is not growing is a stale crowd. whale: 3-day change of Binance's
    #: top-trader position long/short ratio; falling = the informed side is
    #: de-longing the name. Null conditioning values fail OPEN at full size,
    #: the same convention as every v3/v4 conditioning variable.
    flow_cut: float | None = None
    flow_lo: float = 0.5
    whale_cut: float | None = None
    whale_lo: float = 0.5

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
        pers = rule["sizing"].get("persistence_scaling")
        if pers is not None and pers.get("basis") != "deep_settlement_share":
            raise FinancedLongsError(
                f"unsupported persistence_scaling basis {pers.get('basis')!r}; "
                "only 'deep_settlement_share' is implemented"
            )
        flow = rule["sizing"].get("flow_scaling")
        if flow is not None and flow.get("basis") != "turnover_growth_3d":
            raise FinancedLongsError(
                f"unsupported flow_scaling basis {flow.get('basis')!r}; "
                "only 'turnover_growth_3d' is implemented"
            )
        whale = rule["sizing"].get("whale_scaling")
        if whale is not None and whale.get("basis") != "binance_toptrader_ls_3d_change":
            raise FinancedLongsError(
                f"unsupported whale_scaling basis {whale.get('basis')!r}; "
                "only 'binance_toptrader_ls_3d_change' is implemented"
            )
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
            depth_exponent=(
                float(depth.get("exponent", 1.0)) if depth is not None else 1.0
            ),
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
            persistence_window=(int(pers["window_settlements"]) if pers is not None else None),
            persistence_cut=(float(pers["cut"]) if pers is not None else 0.10),
            persistence_lo=(float(pers["low_multiplier"]) if pers is not None else 0.0),
            flow_cut=(float(flow["cut"]) if flow is not None else None),
            flow_lo=(float(flow["low_multiplier"]) if flow is not None else 0.5),
            whale_cut=(float(whale["cut"]) if whale is not None else None),
            whale_lo=(float(whale["low_multiplier"]) if whale is not None else 0.5),
        )


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
    aligned = pl.col("bar_ts_ms").shift(-hold_hours).over("symbol") == (
        pl.col("bar_ts_ms") + hold_hours * HOUR_MS
    )
    future_sum = (
        calendar_roll(
            fresh,
            "sum",
            hold_hours,
            shifted=False,
            min_samples=hold_hours,
            time_col="bar_ts_ms",
            period_ms=HOUR_MS,
        )
        .shift(-hold_hours)
        .over("symbol")
    )
    return pl.when(aligned).then(future_sum).otherwise(None)


def _hour_shift(value: pl.Expr, hours: int) -> pl.Expr:
    return calendar_shift(value, hours, time_col="bar_ts_ms", day_ms=HOUR_MS)


def _hour_roll(
    value: pl.Expr,
    agg: str,
    hours: int,
    *,
    shifted: bool = False,
    min_samples: int | None = None,
) -> pl.Expr:
    return calendar_roll(
        value,
        agg,
        hours,
        shifted=shifted,
        min_samples=hours if min_samples is None else min_samples,
        time_col="bar_ts_ms",
        period_ms=HOUR_MS,
    ).over("symbol")


#: Crowding persistence is measured over this many of the symbol's own
#: settlements. Fixed here rather than per-config because ``prepare`` and
#: ``prepare_decision`` must attach the identical column for the research and
#: live frames. The Rust scorer rejects a rule asking for another window.
PERSISTENCE_WINDOW = 20

#: Depth that makes a settlement "deep" for the persistence count. Every
#: registered carry-hold config enters below 10 bp, so persistence asks "how
#: often has this name printed at entry depth", not a second free parameter.
#: The Rust scorer rejects a persistence rule whose entry depth differs.
DEFAULT_ENTER_BP = 10.0


def _persistence_frame(frame: pl.DataFrame, deep_bp: float) -> pl.DataFrame:
    """Attach ``crowd_persistence``: how habitual this name's crowding is.

    The share of the symbol's last :data:`PERSISTENCE_WINDOW` **settlements**
    that printed deeper than ``deep_bp``, carried forward onto the bars between
    settlements.

    Counted in the symbol's own settlement sequence rather than on a clock. The
    clock version is not the same measurement: Bybit's interval mix went from
    100% 8h in 2021 to 52% 4h / 21% 1h in 2025, so "deep prints in the last 30
    days" mostly reports a symbol's cadence, and the confound has an era
    gradient on top.

    The rolling mean is shifted one settlement, so the value a bar carries
    describes the history *before* the settlement on that bar — a name's first
    deep print never counts itself as evidence that it prints deep habitually.
    """
    events = (
        frame.filter(_settlement_flag())
        .select(
            "symbol",
            "bar_ts_ms",
            (pl.col("by_funding") < -deep_bp / 1e4).cast(pl.Float64).alias("_deep"),
        )
        .with_columns(pl.col("_deep").rolling_mean(PERSISTENCE_WINDOW).over("symbol").alias("_p"))
        .with_columns(pl.col("_p").shift(1).over("symbol").alias("crowd_persistence"))
        .select("symbol", "bar_ts_ms", "crowd_persistence")
    )
    return frame.join(events, on=["symbol", "bar_ts_ms"], how="left").with_columns(
        pl.col("crowd_persistence").forward_fill().over("symbol")
    )


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
            _hour_roll(pl.col("by_turnover_quote"), "sum", 24).alias("adv24"),
            settlement_exact_funding(24).alias("funding_paid"),
            # PIT trailing settled funding over (t-24h, t]: the daily premium
            # currently being paid; v2's sizing basis. Settlements at bars
            # <= t only — same convention as the by_funding decision signal.
            _hour_roll(
                pl.when(_settlement_flag()).then(pl.col("by_funding")).otherwise(0.0),
                "sum",
                24,
            ).alias("trail_fund_24h"),
            (_hour_shift(close, -24) / close - 1.0).alias("price_return"),
            (close / _hour_shift(close, momentum_lookback_hours) - 1.0).alias("momentum"),
            # v3 conditioning variables, all PIT at bars <= t: trailing 3d
            # return, trailing 30d vol of 24h returns (shifted a bar), and the
            # 2d change in the trailing daily funding rate.
            (close / _hour_shift(close, 72) - 1.0).alias("ret_3d"),
            (close / _hour_shift(close, 24) - 1.0).alias("_r24"),
            (
                pl.col("bar_ts_ms").shift(-24).over("symbol") - pl.col("bar_ts_ms") == 24 * HOUR_MS
            ).alias("contiguous"),
        ]
    )
    frame = frame.with_columns(
        _hour_roll(pl.col("_r24"), "std", 720, shifted=True).alias("_vol_30d_daily"),
        _hour_roll(
            pl.col("_r24").is_finite().fill_null(False).cast(pl.UInt16),
            "sum",
            720,
            shifted=True,
        ).alias("_vol_30d_samples"),
        (pl.col("trail_fund_24h") - _hour_shift(pl.col("trail_fund_24h"), 48)).alias(
            "dtrail_2d"
        ),
        # v5 conditioning: trailing-24h turnover now vs 72h earlier, PIT at
        # bars <= t. A zero denominator yields a non-finite value, which the
        # weights loop treats as null (fails open).
        (pl.col("adv24") / _hour_shift(pl.col("adv24"), 72) - 1.0).alias(
            "turn_growth_3d"
        ),
    ).with_columns(
        pl.when(pl.col("_vol_30d_samples") == 720)
        .then(pl.col("_vol_30d_daily"))
        .otherwise(None)
        .alias("vol_30d_daily")
    ).drop("_r24", "_vol_30d_daily", "_vol_30d_samples")
    if "bn_tt_ls" in frame.columns:
        # Binance top-trader position long/short ratio, panel-attached as the
        # last COMPLETE UTC day's end value (join-asof, age recorded). Values
        # older than 48h mean the feed died for the name (delisting, outage);
        # they are nulled so the 3d change fails open instead of freezing.
        fresh = (
            pl.when(pl.col("bn_tt_ls_age_h") <= 48.0)
            .then(pl.col("bn_tt_ls"))
            .otherwise(None)
        )
        frame = frame.with_columns(
            (fresh - _hour_shift(fresh, 72)).alias("d_tt_ls_3d")
        )
    # Attached unconditionally so the research and live frames cannot diverge on
    # whether the column exists; configs that leave persistence off ignore it.
    return _persistence_frame(frame, DEFAULT_ENTER_BP)


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
