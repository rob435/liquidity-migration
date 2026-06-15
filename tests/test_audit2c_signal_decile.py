"""Regression tests for audit2c fixes in liquidity_migration.signal_harness.

Two corrected behaviours are pinned here:

  (1) Decile under-deploy — ``build_combined_signal_portfolio`` must exclude
      names with null / non-positive ``realized_vol`` from the rank pool BEFORE
      selecting the per-day decile, so every selected (short/long) name is
      sizable and carries a non-null weight. The old code ranked un-sizable
      names into the decile and they silently deployed weight=null.

  (2) Daily-aggregation day key — ``_aggregate_daily_{funding,open_interest,
      premium}`` must snap the per-day key to the 00:00-UTC day floor
      ((ts_ms // MS_PER_DAY) * MS_PER_DAY) rather than the first intraday ts,
      so the daily row joins the kline 00:00 grid even on a gap-edge day whose
      first observation is not at midnight.
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.signal_harness import (
    _aggregate_daily_funding,
    _aggregate_daily_open_interest,
    _aggregate_daily_premium,
    build_combined_signal_portfolio,
)


def _date_ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


# ---------------------------------------------------------------------------
# (1) Decile breadth: no selected name with a null weight
# ---------------------------------------------------------------------------


def _panel_with_unsizable_extremes() -> pl.DataFrame:
    """Panel where the most-extreme names by signal have unusable realized_vol.

    Per day, 12 symbols. ``feature_a`` is monotone in symbol id, so the
    lowest-id symbols are the most-negative-signal (short candidates) and the
    highest-id the most-positive (long candidates). The single most-extreme
    name on EACH tail has a non-sizable realized_vol (null on the short tail,
    0.0 on the long tail) — under the old code those two names would be picked
    into the decile and deploy weight=null.
    """
    rows: list[dict] = []
    n_days = 3
    n_symbols = 12
    for d in range(n_days):
        ts = _date_ms("2025-03-01") + d * MS_PER_DAY
        for s in range(n_symbols):
            if s == 0:
                vol = None  # most-negative signal, null vol -> not sizable
            elif s == n_symbols - 1:
                vol = 0.0  # most-positive signal, zero vol -> not sizable
            else:
                vol = 0.5
            rows.append(
                {
                    "symbol": f"S{s:02d}",
                    "ts_ms": ts,
                    "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "feature_a": float(s),
                    "realized_vol_7d": vol,
                    "fwd_ret_3d": 0.0,
                }
            )
    return pl.DataFrame(rows)


def test_decile_excludes_unsizable_names_from_pool() -> None:
    """No selected (short/long) name carries a null weight (fix #1).

    Old behaviour: S00 (null vol) and S11 (0.0 vol) rank into the decile and
    get weight=null, silently zeroing one slot per side.
    """
    panel = _panel_with_unsizable_extremes()
    out = build_combined_signal_portfolio(
        panel,
        surviving_features=["feature_a"],
        weighting="equal",
        top_decile=0.20,  # ceil(0.20 * 10 sizable) = 2 per side
        vol_target_per_name=0.01,
        forward_horizon=3,
    )

    selected = out.filter(pl.col("position_side") != "flat")
    # Every selected name must have a finite, non-null weight.
    assert selected["weight"].null_count() == 0
    assert selected["weight"].is_finite().all()

    # The un-sizable extremes must NOT be selected — they were masked out of the
    # rank pool, so they fall through to "flat" with weight 0.
    unsizable = out.filter(pl.col("symbol").is_in(["S00", "S11"]))
    assert (unsizable["position_side"] == "flat").all()
    assert (unsizable["weight"] == 0.0).all()

    # Breadth guarantee holds over the 10 sizable names: ceil(0.20 * 10) = 2
    # shorts and 2 longs each day, all sizable.
    by_side = out.group_by(["ts_ms", "position_side"]).agg(pl.len().alias("n"))
    assert by_side.filter(pl.col("position_side") == "short")["n"].max() == 2
    assert by_side.filter(pl.col("position_side") == "long")["n"].max() == 2
    shorts = out.filter(pl.col("position_side") == "short")
    longs = out.filter(pl.col("position_side") == "long")
    assert (shorts["weight"] < 0.0).all()
    assert (longs["weight"] > 0.0).all()


def test_decile_all_sizable_unchanged() -> None:
    """When every name is sizable the selection is unchanged (no regression)."""
    rows: list[dict] = []
    n_symbols = 10
    ts = _date_ms("2025-03-01")
    for s in range(n_symbols):
        rows.append(
            {
                "symbol": f"S{s:02d}",
                "ts_ms": ts,
                "date": "2025-03-01",
                "feature_a": float(s),
                "realized_vol_7d": 0.5,
                "fwd_ret_3d": 0.0,
            }
        )
    out = build_combined_signal_portfolio(
        pl.DataFrame(rows),
        surviving_features=["feature_a"],
        weighting="equal",
        top_decile=0.20,
        vol_target_per_name=0.01,
        forward_horizon=3,
    )
    by_side = out.group_by("position_side").agg(pl.len().alias("n"))
    assert by_side.filter(pl.col("position_side") == "short")["n"][0] == 2
    assert by_side.filter(pl.col("position_side") == "long")["n"][0] == 2
    assert out["weight"].null_count() == 0


# ---------------------------------------------------------------------------
# (2) Daily-aggregation day key snapped to the 00:00 day floor
# ---------------------------------------------------------------------------


def _gap_edge_intraday(value_col: str, *, extra: dict[str, list] | None = None) -> pl.DataFrame:
    """Intraday rows for one symbol/day whose first ts is OFF the 00:00 grid.

    The day's 00:00 bar is missing (a gap edge); the first observation is at
    01:00 UTC. ``min(ts_ms)`` is therefore the day floor + 1h, not the floor.
    """
    day_floor = _date_ms("2025-03-02")
    hours = [1, 2, 23]  # 00:00 bar absent
    data: dict[str, list] = {
        "symbol": ["S00"] * len(hours),
        "ts_ms": [day_floor + h * 3_600_000 for h in hours],
        value_col: [1.0, 2.0, 3.0],
    }
    if extra:
        data.update(extra)
    return pl.DataFrame(data)


def test_funding_day_key_snapped_to_day_floor() -> None:
    funding = _gap_edge_intraday("funding_rate")
    out = _aggregate_daily_funding(funding)
    day_floor = _date_ms("2025-03-02")
    assert out["ts_ms"].to_list() == [day_floor]
    # sanity: the un-snapped first ts would have been day_floor + 1h
    assert out["ts_ms"][0] != day_floor + 3_600_000
    # aggregation content unaffected by the key snap
    assert out["funding_rate_1d_sum"][0] == 6.0
    assert out["funding_rate_last"][0] == 3.0


def test_open_interest_day_key_snapped_to_day_floor() -> None:
    oi = _gap_edge_intraday("open_interest")
    out = _aggregate_daily_open_interest(oi)
    day_floor = _date_ms("2025-03-02")
    assert out["ts_ms"].to_list() == [day_floor]
    assert out["open_interest"][0] == 3.0  # last of the day


def test_premium_day_key_snapped_to_day_floor() -> None:
    premium = _gap_edge_intraday("close")
    out = _aggregate_daily_premium(premium)
    day_floor = _date_ms("2025-03-02")
    assert out["ts_ms"].to_list() == [day_floor]
    assert out["premium_close"][0] == 3.0  # last hourly close


def test_snapped_key_joins_kline_grid() -> None:
    """The snapped daily key joins a kline-grid row keyed at 00:00 (fix #2).

    The kline daily grid keys each day at the 00:00 floor. The un-snapped OI
    key (first intraday ts = floor + 1h) would miss this join; the snapped key
    lands exactly on the grid and the join keeps the day.
    """
    day_floor = _date_ms("2025-03-02")
    oi_daily = _aggregate_daily_open_interest(_gap_edge_intraday("open_interest"))
    kline_grid = pl.DataFrame({"symbol": ["S00"], "ts_ms": [day_floor], "adv_30d": [10.0]})
    joined = oi_daily.join(kline_grid, on=["symbol", "ts_ms"], how="inner")
    assert joined.height == 1
    assert joined["ts_ms"][0] == day_floor
