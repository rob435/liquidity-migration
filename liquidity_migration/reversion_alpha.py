"""Ground-up rebuild of the liquidity-migration short strategy as a cleanly
separated three-layer stack:

    Layer 1  ALPHA          compute_reversion_score
        A single cross-sectional, equal-weight z-score composite per (coin, day).
        Replaces v1's six correlated binary gates with one continuous forecast.
        Equal-weight => no fitted coefficients => cannot overfit. Cross-sectional
        => universe-size invariant (no absolute thresholds like rank_imp>=150).

    Layer 2  PORTFOLIO      construct_target_book
        Turns the alpha into a desired book: select the most extended names,
        size continuously by score, scale total gross continuously by the
        alt-regime. Pure "what I want to hold" — no capacity/timing here.

    Layer 3  EXECUTION      simulate
        Walks the hourly grid: admits entries subject to capacity (max active,
        cooldown), runs the conservative intrabar exit scan (stop / take-profit
        / max-hold), applies round-trip cost, and marks the book to market daily
        to produce an equity curve. Capacity is an execution constraint and
        lives here, not in the portfolio layer.

The economic thesis: alt perps that pump hard on abnormal volume are bought by
price-insensitive momentum flow that exhausts; perp funding flips positive; the
overshoot mean-reverts over 1-5 days. Shorting the most extended names collects
a liquidity-provision premium — but only when the broad regime is not turning
those pumps into real trends, hence the continuous regime scaler.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import math

import polars as pl

from .storage import read_dataset
from .volume_features import build_volume_features
from .volume_events import _enriched_event_features, DEFAULT_EXCLUDED_SYMBOLS

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR


@dataclass(frozen=True, slots=True)
class ReversionConfig:
    # --- Layer 1: alpha ---
    # Each named feature is converted to a cross-sectional z-score within the
    # day's tradable universe, then equal-weight averaged into reversion_score.
    # Higher score = more extended pump = better short.
    alpha_features: tuple[str, ...] = (
        "z_residual_return",   # idiosyncratic 1d up-move vs market median
        "z_turnover_ratio",    # log(turnover / prior-7d-mean turnover): volume abnormality
        "z_close_location",    # closed near the high: still-extended pump
        "z_rank_jump",         # 7d liquidity-rank migration up the leaderboard
    )
    min_alpha_components: int = 3       # require >=3 of 4 sub-scores present

    # --- Layer 1: tradable universe ---
    min_pit_age_days: int = 90
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    min_universe_size: int = 20         # skip days with too few names to rank

    # --- Layer 2: portfolio construction ---
    select_top_pct: float = 0.10        # short the top decile by reversion_score
    score_weight_power: float = 1.0     # weight ~ (score - cutoff) ** power; 0 = equal weight
    base_gross: float = 1.0
    per_name_cap: float = 0.25          # max fraction of gross in one name
    # regime: gross multiplier ramps linearly from 1.0 (alts weak) to 0.0 (alts strong)
    regime_feature: str = "market_median_return_30d_sum"
    regime_full_at: float = -0.15       # full gross when 30d alt return <= this
    regime_zero_at: float = 0.10        # zero gross when 30d alt return >= this
    # hard regime gate (WS-3): when set, overrides the continuous ramp — gross
    # is full (1.0) when the 30d alt return <= threshold, else zero. v2 found a
    # hard bear-only gate (threshold -0.05) cleanly excluded the alt-bull losses
    # the continuous scaler still traded into.
    regime_hard_threshold: float | None = None

    # --- Layer 3: execution / capacity ---
    max_active: int = 5
    cooldown_days: int = 5
    entry_delay_hours: int = 1
    max_hold_hours: int = 72
    stop_pct: float = 0.12
    take_profit_pct: float = 0.26
    cost_round_trip_bps: float = 28.8   # matches v1's 3x effective maker/taker blend
    # stop fill model: "stop" fills the stop at exactly the trigger price (an
    # optimistic best case); "bar_extreme" fills it at the bar's high — the
    # worst case for a short — modelling adverse fills on fast moves. Methodology
    # law #7 requires reporting every backtest under the adverse-fill stress.
    stop_fill_mode: str = "stop"

    # --- window ---
    start: str = ""                     # inclusive signal date YYYY-MM-DD
    end: str = ""                       # exclusive signal date YYYY-MM-DD


# --------------------------------------------------------------------------
# Layer 1 — ALPHA
# --------------------------------------------------------------------------

def _tradable_universe(features: pl.DataFrame, config: ReversionConfig) -> pl.DataFrame:
    """Daily tradable set: PIT-mature, non-stable, with a valid daily return."""
    frame = features
    if config.exclude_symbols:
        frame = frame.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    frame = frame.filter(
        (pl.col("pit_age_days") >= config.min_pit_age_days)
        & pl.col("daily_return_1d").is_not_null()
        & pl.col("daily_close").is_not_null()
        & (pl.col("daily_close") > 0.0)
    )
    return frame


def _cross_sectional_z(frame: pl.DataFrame, raw_col: str, out_col: str) -> pl.DataFrame:
    """Standardise raw_col within each date. Nulls stay null and are excluded
    from the day's mean/std."""
    return frame.with_columns(
        (
            (pl.col(raw_col) - pl.col(raw_col).mean().over("date"))
            / pl.col(raw_col).std().over("date")
        ).alias(out_col)
    )


def compute_reversion_score(features: pl.DataFrame, config: ReversionConfig) -> pl.DataFrame:
    """LAYER 1. Return the tradable panel with a `reversion_score` column.

    The score is the equal-weight mean of available cross-sectional z-scores.
    Every sub-signal is a same-day cross-sectional rank/z so the score carries
    no absolute, universe-size-dependent thresholds.
    """
    frame = _tradable_universe(features, config)

    # raw sub-signals (sign convention: larger = more short-worthy)
    frame = frame.with_columns(
        # idiosyncratic up-move
        pl.col("residual_return_1d").alias("_raw_residual_return"),
        # volume abnormality: log ratio of turnover to its prior-7d mean
        pl.when((pl.col("prior7_turnover_quote_mean") > 0.0))
        .then(
            (pl.col("turnover_quote") / pl.col("prior7_turnover_quote_mean")).log()
        )
        .otherwise(None)
        .alias("_raw_turnover_ratio"),
        # range extension: closed near the high
        pl.col("signal_day_close_location").alias("_raw_close_location"),
        # rank migration: recompute signed in int64 to avoid u32 underflow
        (
            pl.col("prior7_liquidity_rank").cast(pl.Int64)
            - pl.col("liquidity_rank").cast(pl.Int64)
        ).alias("_raw_rank_jump"),
    )

    frame = _cross_sectional_z(frame, "_raw_residual_return", "z_residual_return")
    frame = _cross_sectional_z(frame, "_raw_turnover_ratio", "z_turnover_ratio")
    frame = _cross_sectional_z(frame, "_raw_close_location", "z_close_location")
    frame = _cross_sectional_z(frame, "_raw_rank_jump", "z_rank_jump")

    components = list(config.alpha_features)
    present = pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in components])
    total = pl.sum_horizontal([pl.col(c).fill_null(0.0) for c in components])
    frame = frame.with_columns(
        pl.when(present >= config.min_alpha_components)
        .then(total / present)
        .otherwise(None)
        .alias("reversion_score")
    )
    return frame.filter(pl.col("reversion_score").is_not_null())


# --------------------------------------------------------------------------
# Layer 2 — PORTFOLIO CONSTRUCTION
# --------------------------------------------------------------------------

def _regime_multiplier(alt_30d: float | None, config: ReversionConfig) -> float:
    """Gross scaler driven by the 30d alt regime.

    Default: a continuous ramp from 1.0 (alts weak) to 0.0 (alts strong).
    When `regime_hard_threshold` is set: a hard gate — full gross at/below the
    threshold, zero above it (WS-3).
    """
    if alt_30d is None or math.isnan(alt_30d):
        return 1.0  # no regime info -> do not throttle (conservative w.r.t. coverage)
    if config.regime_hard_threshold is not None:
        return 1.0 if alt_30d <= config.regime_hard_threshold else 0.0
    lo, hi = config.regime_full_at, config.regime_zero_at
    if alt_30d <= lo:
        return 1.0
    if alt_30d >= hi:
        return 0.0
    return (hi - alt_30d) / (hi - lo)


def construct_target_book(scored: pl.DataFrame, config: ReversionConfig) -> pl.DataFrame:
    """LAYER 2. Per signal day, produce a ranked desired book.

    Output columns: date, symbol, rank (1=strongest), target_weight, regime_mult.
    target_weight already includes the regime scaler and per-name cap, and sums
    to base_gross*regime_mult across the selected set. Capacity/timing is NOT
    applied here — that is the execution layer's job.
    """
    rows: list[dict[str, Any]] = []
    for key, day in scored.sort("date").partition_by("date", as_dict=True).items():
        date = str(key[0] if isinstance(key, tuple) else key)
        n = day.height
        if n < config.min_universe_size:
            continue
        day = day.sort("reversion_score", descending=True)
        k = max(1, int(round(n * config.select_top_pct)))
        selected = day.head(k)
        scores = selected["reversion_score"].to_list()
        symbols = selected["symbol"].to_list()
        regime_vals = selected[config.regime_feature].to_list() if config.regime_feature in selected.columns else [None] * k
        regime_mult = _regime_multiplier(regime_vals[0] if regime_vals else None, config)
        gross = config.base_gross * regime_mult
        if gross <= 0.0:
            continue

        # continuous score-proportional weights, measured above the selection cutoff
        cutoff = scores[-1]
        excess = [max(0.0, s - cutoff) ** config.score_weight_power for s in scores]
        if sum(excess) <= 0.0:
            excess = [1.0] * k  # degenerate (all equal) -> equal weight
        wsum = sum(excess)
        raw_w = [gross * e / wsum for e in excess]
        # per-name cap, then renormalise the remainder so gross is preserved
        cap = config.per_name_cap * gross
        capped = [min(w, cap) for w in raw_w]
        deficit = gross - sum(capped)
        if deficit > 1e-12:
            head = [i for i, w in enumerate(capped) if w < cap - 1e-12]
            if head:
                room = [cap - capped[i] for i in head]
                rsum = sum(room)
                for idx, i in enumerate(head):
                    capped[i] += deficit * room[idx] / rsum
        for rank, (sym, w) in enumerate(zip(symbols, capped), start=1):
            rows.append(
                {
                    "date": date,
                    "symbol": sym,
                    "rank": rank,
                    "target_weight": w,
                    "regime_mult": regime_mult,
                }
            )
    if not rows:
        return pl.DataFrame(
            schema={"date": pl.Utf8, "symbol": pl.Utf8, "rank": pl.Int64,
                    "target_weight": pl.Float64, "regime_mult": pl.Float64}
        )
    return pl.DataFrame(rows)


# --------------------------------------------------------------------------
# Layer 3 — EXECUTION
# --------------------------------------------------------------------------

def _hourly_bars_by_symbol(klines: pl.DataFrame) -> dict[str, list[dict[str, float]]]:
    cols = ["ts_ms", "open", "high", "low", "close"]
    out: dict[str, list[dict[str, float]]] = {}
    clean = klines.select(["symbol", *cols]).drop_nulls(cols)
    for key, part in clean.partition_by("symbol", as_dict=True).items():
        sym = str(key[0] if isinstance(key, tuple) else key)
        part = part.sort("ts_ms")
        out[sym] = [
            {"ts_ms": int(r["ts_ms"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])}
            for r in part.iter_rows(named=True)
        ]
    return out


def _scan_exit(bars: list[dict[str, float]], entry_idx: int, entry_price: float,
               config: ReversionConfig) -> tuple[int, float, str]:
    """Conservative intrabar exit scan for a SHORT. Returns (exit_idx, exit_price, reason).

    Stop is above entry, take-profit below. If a bar touches both, the stop is
    taken (worst case for a short). Otherwise exit at max-hold close or data end.

    `stop_fill_mode` controls the stop fill price: "stop" fills at the trigger
    price; "bar_extreme" fills at the bar high — the adverse-fill stress.
    """
    stop = entry_price * (1.0 + config.stop_pct)
    take = entry_price * (1.0 - config.take_profit_pct)
    adverse = config.stop_fill_mode == "bar_extreme"
    last = min(entry_idx + config.max_hold_hours, len(bars) - 1)
    for i in range(entry_idx, last + 1):
        bar = bars[i]
        hit_stop = bar["high"] >= stop
        hit_take = bar["low"] <= take
        if hit_stop:
            # stop priority over take-profit (worst case for a short); under
            # the adverse-fill stress the stop fills at the bar high.
            return i, (bar["high"] if adverse else stop), "stop"
        if hit_take:
            return i, take, "take_profit"
    return last, bars[last]["close"], ("max_hold" if last == entry_idx + config.max_hold_hours else "data_end")


@lru_cache(maxsize=None)
def _day_str_from_index(day_index: int) -> str:
    return datetime.fromtimestamp(day_index * 86_400, tz=timezone.utc).strftime("%Y-%m-%d")


def _day_str(ts_ms: int) -> str:
    """UTC calendar date of an epoch-ms timestamp. Cached per day index — the
    panel spans only ~1k distinct days, so this collapses millions of calls
    down to ~1k strftimes."""
    return _day_str_from_index(ts_ms // MS_PER_DAY)


def prepare_klines(klines: pl.DataFrame) -> dict[str, Any]:
    """Build the per-symbol bar structures `simulate` needs, once. Reusing this
    across a config grid avoids re-partitioning millions of klines per cell.
    The result is config-independent — purely a function of the klines."""
    bars_by_symbol = _hourly_bars_by_symbol(klines)
    ts_index = {
        sym: {b["ts_ms"]: i for i, b in enumerate(bars)} for sym, bars in bars_by_symbol.items()
    }
    daily_close: dict[str, dict[str, float]] = {}
    for sym, bars in bars_by_symbol.items():
        dd: dict[str, float] = {}
        for b in bars:
            dd[_day_str(b["ts_ms"])] = b["close"]   # later bars overwrite -> last close of day
        daily_close[sym] = dd
    return {"bars_by_symbol": bars_by_symbol, "ts_index": ts_index, "daily_close": daily_close}


def simulate(book: pl.DataFrame, klines: pl.DataFrame, config: ReversionConfig,
             *, prepared: dict[str, Any] | None = None) -> dict[str, Any]:
    """LAYER 3. Walk the hourly grid, admit entries under capacity + cooldown,
    run the exit scan, apply round-trip cost, and mark the book to market daily.

    `prepared` is the optional output of `prepare_klines(klines)` — pass it to
    reuse the bar index across many simulate calls (config grids). It must be
    built from the SAME klines. When omitted it is built here.

    Returns dict with `trades` (ledger) and `equity` (daily curve) DataFrames
    plus summary metrics.
    """
    if prepared is None:
        prepared = prepare_klines(klines)
    bars_by_symbol = prepared["bars_by_symbol"]
    ts_index = prepared["ts_index"]

    candidates = book.sort(["date", "rank"]).to_dicts()
    by_date: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_date.setdefault(c["date"], []).append(c)

    open_until: dict[str, int] = {}      # symbol -> exit ts_ms of an open position
    cooldown_until: dict[str, int] = {}  # symbol -> ts_ms before which re-entry is blocked
    trades: list[dict[str, Any]] = []

    for date in sorted(by_date):
        # The daily bar is stamped at day_start + 24h, so `date` already marks
        # the signal-close moment — `day_ms` IS when the signal is fully known.
        # Entry is `entry_delay_hours` after it. (A prior version added a
        # spurious `+23h` here, entering ~25h late instead of 1h; setting
        # entry_delay_hours=24 reproduces that legacy behaviour exactly.)
        day_ms = int(pl.Series([date]).str.to_datetime().dt.timestamp("ms")[0])
        entry_ts = day_ms + config.entry_delay_hours * MS_PER_HOUR
        # free capacity at this entry time
        active = sum(1 for s, x in open_until.items() if x > entry_ts)
        for cand in by_date[date]:
            if active >= config.max_active:
                break
            sym = cand["symbol"]
            if open_until.get(sym, 0) > entry_ts:
                continue  # already holding this name
            if cooldown_until.get(sym, 0) > entry_ts:
                continue  # within cooldown
            bars = bars_by_symbol.get(sym)
            idx = ts_index.get(sym, {}).get(entry_ts)
            if bars is None or idx is None or idx >= len(bars) - 1:
                continue  # no executable bar
            entry_price = bars[idx]["open"]
            if not (entry_price > 0.0):
                continue
            exit_idx, exit_price, reason = _scan_exit(bars, idx, entry_price, config)
            gross_ret = (entry_price - exit_price) / entry_price          # short
            cost = config.cost_round_trip_bps / 10_000.0
            net_ret = gross_ret - cost
            exit_ts = bars[exit_idx]["ts_ms"]
            trades.append({
                "symbol": sym, "entry_date": date, "entry_ts_ms": entry_ts,
                "exit_ts_ms": exit_ts, "exit_date": _day_str(exit_ts),
                "entry_price": entry_price, "exit_price": exit_price,
                "weight": cand["target_weight"], "regime_mult": cand["regime_mult"],
                "gross_return": gross_ret, "net_return": net_ret,
                "cost": cost, "exit_reason": reason,
                "hold_hours": exit_idx - idx,
            })
            open_until[sym] = exit_ts
            cooldown_until[sym] = exit_ts + config.cooldown_days * MS_PER_DAY
            active += 1

    equity = _daily_mtm(trades, bars_by_symbol, config, daily_close=prepared["daily_close"])
    metrics = _summary_metrics(trades, equity)
    return {
        "trades": pl.DataFrame(trades) if trades else pl.DataFrame(),
        "equity": equity,
        "metrics": metrics,
        "config": config,
    }


def _daily_mtm(trades: list[dict[str, Any]], bars_by_symbol: dict[str, list[dict[str, float]]],
               config: ReversionConfig,
               *, daily_close: dict[str, dict[str, float]] | None = None) -> pl.DataFrame:
    """Correct overlapping-position accounting: each calendar day, sum every
    open position's weighted short return, compound the portfolio equity.
    Round-trip cost is charged on the exit day.

    `daily_close` (per-symbol date->close) may be supplied to skip rebuilding
    it from every bar; when omitted it is built here."""
    if not trades:
        return pl.DataFrame(schema={"date": pl.Utf8, "equity": pl.Float64,
                                    "drawdown": pl.Float64, "portfolio_return": pl.Float64})
    # per-symbol daily close (close of the last bar each day)
    if daily_close is None:
        daily_close = {}
        for sym, bars in bars_by_symbol.items():
            dd: dict[str, float] = {}
            for b in bars:
                dd[_day_str(b["ts_ms"])] = b["close"]   # later bars overwrite -> last close of day
            daily_close[sym] = dd

    # expand each trade into daily (date -> contribution) using its own price path
    day_returns: dict[str, float] = {}
    for t in trades:
        sym = t["symbol"]
        dd = daily_close.get(sym, {})
        days = _date_range(t["entry_date"], t["exit_date"])
        w = t["weight"]
        for i, d in enumerate(days):
            start_p = t["entry_price"] if i == 0 else dd.get(days[i - 1])
            end_p = t["exit_price"] if d == t["exit_date"] else dd.get(d)
            if start_p is None or end_p is None or start_p <= 0:
                continue
            short_ret = (start_p - end_p) / start_p
            day_returns[d] = day_returns.get(d, 0.0) + w * short_ret
        # charge round-trip cost on exit day
        day_returns[t["exit_date"]] = day_returns.get(t["exit_date"], 0.0) - w * t["cost"]

    rows = []
    equity = 1.0
    peak = 1.0
    for d in sorted(day_returns):
        equity *= (1.0 + day_returns[d])
        peak = max(peak, equity)
        rows.append({"date": d, "equity": equity,
                     "drawdown": equity / peak - 1.0,
                     "portfolio_return": day_returns[d]})
    return pl.DataFrame(rows)


def _date_range(start: str, end: str) -> list[str]:
    s = pl.Series([start]).str.to_date()
    e = pl.Series([end]).str.to_date()
    return pl.date_range(s[0], e[0], interval="1d", eager=True).dt.strftime("%Y-%m-%d").to_list()


def _summary_metrics(trades: list[dict[str, Any]], equity: pl.DataFrame) -> dict[str, Any]:
    if not trades or equity.is_empty():
        return {"trades": 0, "total_return": 0.0, "max_drawdown": 0.0,
                "sharpe": 0.0, "win_rate": 0.0, "avg_net": 0.0}
    nets = [t["net_return"] for t in trades]
    rets = equity["portfolio_return"].to_list()
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if len(rets) > 1 else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0.0
    return {
        "trades": len(trades),
        "total_return": equity["equity"][-1] - 1.0,
        "max_drawdown": equity["drawdown"].min(),
        "sharpe": sharpe,
        "win_rate": sum(1 for n in nets if n > 0) / len(nets),
        "avg_net": mean if False else sum(nets) / len(nets),
    }


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def run_reversion_backtest(data_root: str | Path, config: ReversionConfig) -> dict[str, Any]:
    """End-to-end: load PIT data, run the three layers, return results."""
    root = Path(data_root).expanduser()
    klines = read_dataset(root, "klines_1h")
    if klines.is_empty():
        raise RuntimeError("klines_1h is empty")
    manifest = read_dataset(root, "archive_trade_manifest")
    features = _enriched_event_features(
        build_volume_features(klines), klines, manifest,
        funding=read_dataset(root, "funding"),
        open_interest=read_dataset(root, "open_interest"),
        signed_flow_1h=pl.DataFrame(),
        mark_price_1h=read_dataset(root, "mark_price_1h"),
        index_price_1h=read_dataset(root, "index_price_1h"),
        premium_index_1h=read_dataset(root, "premium_index_1h"),
    )
    if config.start:
        features = features.filter(pl.col("date") >= config.start)
    if config.end:
        features = features.filter(pl.col("date") < config.end)

    scored = compute_reversion_score(features, config)          # Layer 1
    book = construct_target_book(scored, config)                # Layer 2
    result = simulate(book, klines, config)                     # Layer 3
    result["scored_rows"] = scored.height
    result["book_rows"] = book.height
    return result
