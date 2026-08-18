"""Registered carry-hold decision rule and the live decision frame.

The committed ``lane2_carry_hold`` rule the CARRY sleeve replays: config
parsing, the settlement-exact funding features, the hysteresis state machine,
and the daily decision grid. The research scorers that grade this rule on a
historical panel live in
``liquidity_migration/research/backtest/financed_longs.py``.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any

import polars as pl

HOUR_MS = 3_600_000


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
            persistence_window=(int(pers["window_settlements"]) if pers is not None else None),
            persistence_cut=(float(pers["cut"]) if pers is not None else 0.10),
            persistence_lo=(float(pers["low_multiplier"]) if pers is not None else 0.0),
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
    # The shift must sit inside the window: outside it shifts the materialized
    # column, handing each symbol's last `hold_hours` rows the next symbol's sums.
    return fresh.rolling_sum(hold_hours).shift(-hold_hours).over("symbol")


#: Crowding persistence is measured over this many of the symbol's own
#: settlements. Fixed here rather than per-config because ``prepare`` and
#: ``prepare_decision`` must attach the identical column for the research and
#: live frames; a config asking for a different window is rejected in
#: :func:`carry_hold_weights` rather than silently scored against the wrong
#: feature.
PERSISTENCE_WINDOW = 20

#: Depth that makes a settlement "deep" for the persistence count. Every
#: registered carry-hold config enters below 10 bp, so persistence asks "how
#: often has this name printed at entry depth", not a second free parameter.
#: A config whose ``enter_bp`` differs is rejected in :func:`carry_hold_weights`.
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
    frame = frame.with_columns(
        pl.col("_r24").rolling_std(720).shift(1).over("symbol").alias("vol_30d_daily"),
        (pl.col("trail_fund_24h") - pl.col("trail_fund_24h").shift(48).over("symbol")).alias(
            "dtrail_2d"
        ),
    ).drop("_r24")
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

    With ``persistence_window`` set (v4) that weight is multiplied again by a
    crowding-persistence step: names whose recent settlements have rarely been
    deep are cut to ``persistence_lo``. Depth and persistence answer different
    questions — how much is being paid now, versus whether this name pays
    habitually — which is why they compose rather than replace each other.
    """
    enter, exit_ = cfg.enter_bp / 1e4, cfg.exit_bp / 1e4
    sized = cfg.depth_ref_bp_per_day is not None
    banded = cfg.toxic_band_ret3d is not None
    volfloored = cfg.min_vol30_daily is not None
    veled = cfg.trail_recovery_exit_bp_2d is not None
    persisted = cfg.persistence_window is not None
    if persisted:
        if cfg.persistence_window != PERSISTENCE_WINDOW:
            raise FinancedLongsError(
                f"{cfg.config_id}: persistence window {cfg.persistence_window} but the "
                f"prepared column is built over {PERSISTENCE_WINDOW} settlements"
            )
        if cfg.enter_bp != DEFAULT_ENTER_BP:
            raise FinancedLongsError(
                f"{cfg.config_id}: persistence counts settlements deeper than "
                f"{DEFAULT_ENTER_BP} bp but this config enters at {cfg.enter_bp} bp"
            )
    need = (
        ["trail_fund_24h"] * sized
        + ["ret_3d"] * banded
        + ["vol_30d_daily"] * volfloored
        + ["dtrail_2d"] * veled
        + ["crowd_persistence"] * persisted
    )
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
        pr = g["crowd_persistence"].to_numpy() if persisted else None
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
                if persisted and pr is not None:
                    # A null persistence — fewer than PERSISTENCE_WINDOW
                    # settlements of history — fails OPEN at full size.
                    # Downsizing it would make this a covert listing-age screen,
                    # and listing age has produced two false positives in this
                    # program. Measured on the registered book the branch never
                    # fires: every held name-day has a full window.
                    if math.isfinite(pr[i]) and pr[i] <= cfg.persistence_cut:
                        w *= cfg.persistence_lo
                if w <= 0.0:
                    continue  # persistence cut this name to nothing today
                rows["bar_ts_ms"].append(int(ts[i]))
                rows["symbol"].append(str(sym))
                rows["w"].append(w)
    weights = pl.DataFrame(
        rows, schema={"bar_ts_ms": pl.Int64, "symbol": pl.String, "w": pl.Float64}
    )
    return _apply_gross_cap(weights, cfg.gross_cap)
