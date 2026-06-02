"""Execution-grade backtest for the CONTINUOUS liquidity-migration fade.

Every prior continuous-fade result (P0 -> p1m) is an EXPLORATORY proxy: per-spell
additive PnL, a mid-fill at the SAME close used to rank, a flat 15/30 bps cost with
NO market impact, and no compounding. This module closes those gaps so the continuous
book is measured under the SAME execution machinery as the deployed daily strategy.

It REPRODUCES the proxy SELECTION exactly (so it is auditable against p1d/p1j/p1k):
  - 5 trailing closed-bar features (rv_168h, vov, dist_low, xsret7, xsret3),
  - within-ts composite decile on the rmom-LOW half (causal day-floor lag1 join of
    residual_momentum.parquet),
  - short the top composite decile (D9), fresh spell entry (gap > 1h), liquid gate
    (signal-bar hourly turnover_quote >= threshold).

It runs that selection through the daily engine's VALIDATED execution core
(`_simulate_indexed_trade` + `trade_lifecycle`: stop fills, funding-to-exit, MAE/MFE,
compounding equity, drawdown, Sharpe) and adds the three realism upgrades the proxy
lacked (see docs/preregistration/continuous-engine-2026-06-01.md):
  1. HONEST +1h entry -- fill at the bar AFTER the deciding bar's close. The proxy
     filled at the same close it ranked on (execution look-ahead). entry_delay_hours=0
     reproduces the proxy (validation only).
  2. A real round-trip COST = 2*taker + 2*spread + 2*impact, where impact rises with
     size and falls with liquidity: impact_bps = impact_coef_bps * participation^exp,
     participation = position_notional / signal-bar hourly turnover. This is the
     capacity-aware cost the p1c argument + the integrity gate demand.
  3. COMPOUNDING equity + true concurrency (heap of exit-times, max_active cap) +
     per-symbol cooldown, and the full artifact set (ledger, equity curve, splits,
     drawdown, worst-day, config hash, run label).

EXPLORATORY engine: impact coefficients are MODELED, not venue-calibrated, and the
selection params come from a heavily multiple-tested research arc -> never promotion
evidence. Forward demo (operator-gated) is the only Tier-3 arbiter.
"""
from __future__ import annotations

import bisect
import hashlib
import heapq
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .config import DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .signal_harness import _autodetect_dataset_names, _date_str_to_ms, _read_window
from .trade_lifecycle import _empty_trades, _funding_lookup
from .volume_events import _indexed_price_bars_by_symbol, _simulate_indexed_trade

FEATURES = ("rv_168h", "vov", "dist_low", "xsret7", "xsret3")


@dataclass(frozen=True, slots=True)
class ContinuousEventConfig:
    """Continuous-fade engine config. See module docstring + the pre-registration."""

    start_date: str = "2023-04-01"
    end_date: str = "2026-05-28"
    # --- selection (ported from p1d._deciled_panel) ---
    side: str = "short"
    decile: int = 9                       # short the top composite decile
    rmom_quantile: float = 0.5            # rmom-LOW half: keep within-ts rmom rank <= this
    liq_turnover_min: float = 500_000.0   # liquid gate: signal-bar hourly turnover_quote (USD)
    # --- execution ---
    entry_delay_hours: int = 1            # bars AFTER the deciding bar's close (0 = proxy/look-ahead)
    exit_mode: str = "fixed"              # "fixed" = hold_hours timer; "state" = hold while in the fade decile
    hold_hours: int = 12                  # fixed-mode hold horizon
    max_hold_hours: int = 48              # state-mode cap (force exit if the name never leaves the decile)
    cooldown_hours: int = 0               # 0 -> fixed: hold_hours; state: 0 (spell-fresh already dedupes)
    stop_loss_pct: float = 0.0            # 0 -> no stop (proxy parity)
    stop_fill_mode: str = "bar_extreme_capped"
    stop_slippage_cap_pct: float = 0.10
    # --- sizing ---
    gross_exposure: float = 0.5           # 0.5 / 25 = 2% per name (matches the p1j/p1k proxy)
    max_active: int = 25
    # --- cost model: round_trip = 2*(taker+spread) + 2*impact ---
    taker_fee_bps: float = 5.5
    spread_bps: float = 2.5
    impact_coef_bps: float = 50.0
    impact_exponent: float = 0.5
    deploy_capital_usd: float = 1_000_000.0
    flat_round_trip_bps: float | None = None   # override the cost model (proxy-parity validation)
    # --- inherited-from-daily refinements (ablation knobs; default OFF = current baseline) ---
    sizing_mode: str = "flat"             # "flat" (2% each) | "inverse_vol" (size by target_vol/rv, clamped)
    target_vol_per_name: float = 0.02     # inverse_vol: per-name hourly-vol target
    vol_weight_clamp: float = 3.0         # inverse_vol: clamp weight multiplier to [1/clamp, clamp]
    age_days_min: int = 0                 # skip symbols younger than this (fresh-listing squeezers)
    entry_decel_lookback_h: int = 0       # 0=off; require close[t]/close[t-lookback]-1 <= entry_decel_max_ret
    entry_decel_max_ret: float = 0.0      # fade-started confirmation: recent move must be <= this
    market_min_ret_1d: float = -1.0       # skip entry if equal-weight market 1d return < this (-1 = off)
    failed_fade_hours: int = 0            # 0=off; cut a fade that hasn't worked after N hours
    failed_fade_loss_pct: float = 0.0
    failed_fade_min_mfe_pct: float = 0.0
    breakeven_arm_pct: float = 0.0        # 0=off; once MFE>=this, exit if it returns to entry
    mfe_giveback_trigger_pct: float = 0.0
    mfe_giveback_retain_pct: float = 0.0
    # Portfolio circuit breaker (correlated-squeeze defense): PAUSE new entries when >= N net-negative
    # exits (the live sleeve's "adverse cover" footprint of a market-wide alt melt-up) have completed
    # within the trailing entry_pause_window_hours. Causal: counts only exits that closed strictly
    # before the candidate entry. 0 = OFF. Mirrors continuous_demo.entry_circuit_breaker_tripped so the
    # engine validates the live knob (net_return<0 is the engine-side proxy for the live adverse set).
    entry_pause_after_adverse_exits: int = 0
    entry_pause_window_hours: int = 24
    # --- funding / splits / universe ---
    use_funding: bool = True
    split_date: str = "2025-06-01"        # early/recent boundary
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS

    @property
    def effective_cooldown_hours(self) -> int:
        if self.cooldown_hours > 0:
            return self.cooldown_hours
        # state-mode entries are one-per-D9-spell and can't double-open, so no extra cooldown;
        # fixed-mode uses hold_hours so a name can't re-enter mid-hold (matches the proxy).
        return 0 if self.exit_mode == "state" else self.hold_hours

    @property
    def notional_weight(self) -> float:
        return self.gross_exposure / max(self.max_active, 1)

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]


def _iso_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def build_continuous_panel(
    data_root: str | Path, config: ContinuousEventConfig, *, cache: bool = True
) -> pl.DataFrame:
    """Build the deciled rmom-low panel: (symbol, ts_ms, decile, composite, turnover_quote).

    PIT-causal: all 5 features are trailing closed-bar windows on `ts_ms`'s close (known at
    ts_ms+1h); the rmom join is a day-floor lag1 (residual_momentum[D] uses residuals <= D-1).
    Reproduces scripts/p1d_continuous_turnover._deciled_panel (minus the proxy's `fwd` column,
    which the engine does not use -- it computes the realised fill itself).
    """
    root = Path(str(data_root)).expanduser()
    cache_path = root / f"_continuous_engine_panel_rmom{int(round(config.rmom_quantile * 100))}.parquet"
    if cache and cache_path.exists():
        return pl.read_parquet(cache_path)
    start_ms, end_ms = _date_str_to_ms(config.start_date), _date_str_to_ms(config.end_date)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(
        root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms,
        columns=["ts_ms", "symbol", "close", "turnover_quote"],
    )
    if k.is_empty():
        return pl.DataFrame()
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    rmom_path = root / "residual_momentum.parquet"
    if not rmom_path.exists():
        raise FileNotFoundError(
            f"{rmom_path} missing -- build it first: "
            f"POLARS_MAX_THREADS=8 python scripts/precompute_residual_momentum.py --root {root}"
        )
    rmom = pl.read_parquet(rmom_path).rename({"ts_ms": "day_ts"})
    panel = compute_continuous_decile_panel(k, rmom, rmom_quantile=config.rmom_quantile, start_ms=start_ms)
    if cache:
        panel.write_parquet(cache_path)
    return panel


def per_symbol_timeseries_features(k: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol trailing-window features (NO cross-section): the rolling parts that depend only
    on a symbol's own closed-bar history (ret1, rv_168h, ret72, ret168, min720, max720, vov,
    dist_low). Split out of `compute_continuous_decile_panel` so the live demo can recompute these
    once per bar close and cache them (see `continuous_demo.LivePanelCache`); the expressions and
    their order are byte-for-byte the original inline version, so behaviour is unchanged."""
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"))
    k = k.with_columns(
        pl.col("ret1").rolling_std(window_size=168, min_samples=48).over("symbol").alias("rv_168h"),
        (pl.col("close") / pl.col("close").shift(72).over("symbol") - 1.0).alias("ret72"),
        (pl.col("close") / pl.col("close").shift(168).over("symbol") - 1.0).alias("ret168"),
        pl.col("close").rolling_min(window_size=720, min_samples=168).over("symbol").alias("min720"),
        pl.col("close").rolling_max(window_size=720, min_samples=168).over("symbol").alias("max720"),
    )
    k = k.with_columns(
        pl.col("rv_168h").rolling_std(window_size=720, min_samples=168).over("symbol").alias("vov"),
        pl.when(pl.col("max720") > pl.col("min720"))
        .then((pl.col("close") - pl.col("min720")) / (pl.col("max720") - pl.col("min720")))
        .otherwise(None).alias("dist_low"),
    )
    return k


def cross_sectional_decile(
    k: pl.DataFrame, rmom: pl.DataFrame, *, rmom_quantile: float = 0.5
) -> pl.DataFrame:
    """Cross-sectional composite -> decile from per-symbol features. `k` must already carry
    [symbol, ts_ms, turnover_quote, rv_168h, vov, dist_low, ret72, ret168]; the backtest passes the
    full multi-ts panel, the live cache assembles a single current-ts frame from cached carry +
    the live price. This is the EXACT tail of the original `compute_continuous_decile_panel`, so
    the live signal that flows through it is provably identical to the verified backtest signal.

    Cross-sectional ops (`xsret*`, `_rr`, `_n_*`, decile) rank WITHIN each `ts_ms` group, so they
    are unaffected by which other timestamps are present — hence computing them here (after the
    backtest's start_ms filter) matches computing them before it (start_ms drops whole timestamps,
    never individual symbols within a surviving timestamp)."""
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    k = k.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    k = k.join(rmom, on=["symbol", "day_ts"], how="left").filter(pl.col("residual_momentum").is_not_null())
    k = k.with_columns(
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
    ).filter(pl.col("_rr") <= rmom_quantile)
    present = [f for f in FEATURES if f in k.columns]
    k = k.with_columns(
        [((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}") for f in present]
    )
    k = k.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    k = k.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )
    return k.select("symbol", "ts_ms", "decile", "composite", "turnover_quote").sort(["symbol", "ts_ms"])


def compute_continuous_decile_panel(
    k: pl.DataFrame, rmom: pl.DataFrame, *, rmom_quantile: float = 0.5, start_ms: int = 0
) -> pl.DataFrame:
    """Shared feature -> composite -> decile pipeline (used by BOTH the backtest panel and the
    live demo state, so the live signal is provably identical to the verified backtest).

    `k`: hourly klines [ts_ms, symbol, close, turnover_quote] (closed bars; the live caller appends
    a synthetic current bar per symbol at `now` with close = live ticker price). `rmom`: the daily
    residual-momentum table [symbol, day_ts, residual_momentum] (day-floored ts). All features are
    trailing closed-bar windows; the rmom join is a causal day-floor lag1. Returns
    [symbol, ts_ms, decile, composite, turnover_quote]. Thin composition of the two halves above."""
    k = per_symbol_timeseries_features(k)
    if start_ms:
        k = k.filter(pl.col("ts_ms") >= start_ms)
    return cross_sectional_decile(k, rmom, rmom_quantile=rmom_quantile)


def _fresh_entries(panel: pl.DataFrame, config: ContinuousEventConfig) -> pl.DataFrame:
    """Fresh (new D9 spell) entries on the liquid universe, ts-ordered.

    "Fresh" is computed on the FULL target-decile membership timeline (a gap > 1h marks a new
    spell), and the liquid gate is applied AFTER -- matching the proxy. Filtering liquid FIRST
    would let illiquid hours inside one continuous spell open artificial gaps -> spurious fresh
    entries (it inflated the count ~2x in the first cut)."""
    if panel.is_empty():
        return panel
    d = panel.filter(pl.col("decile") == config.decile).sort(["symbol", "ts_ms"])
    d = d.with_columns(
        ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR).fill_null(True).alias("fresh")
    )
    # spell id + the last consecutive in-decile hour of each spell (the STATE-exit timestamp:
    # the name stops being a top-decile fade candidate after spell_end_ts).
    d = d.with_columns(pl.col("fresh").cum_sum().over("symbol").alias("_spell"))
    d = d.with_columns(pl.col("ts_ms").max().over(["symbol", "_spell"]).alias("spell_end_ts"))
    d = d.filter(pl.col("fresh")).filter(pl.col("turnover_quote") >= config.liq_turnover_min)
    return d.select("symbol", "ts_ms", "composite", "turnover_quote", "spell_end_ts").sort(["ts_ms", "symbol"])


def _round_trip_bps(config: ContinuousEventConfig, turnover_quote: float) -> float:
    """Round-trip cost (bps): 2*taker + 2*spread + 2*impact; impact is size/ADV-aware.

    participation = position_notional / signal-bar hourly turnover (the ADV proxy).
    A flat_round_trip_bps override bypasses the model for proxy-parity validation.
    """
    if config.flat_round_trip_bps is not None:
        return float(config.flat_round_trip_bps)
    base = 2.0 * (config.taker_fee_bps + config.spread_bps)
    notional = config.notional_weight * config.deploy_capital_usd
    adv = max(float(turnover_quote), 1.0)
    participation = notional / adv
    impact = config.impact_coef_bps * (participation ** config.impact_exponent)
    return base + 2.0 * impact


def _market_daily_returns(klines: pl.DataFrame) -> dict[int, float]:
    """Equal-weight cross-sectional mean daily return per day-floored ts (the alt-market regime gate)."""
    dc = (
        klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c")).sort(["symbol", "day"])
        .with_columns((pl.col("c") / pl.col("c").shift(1).over("symbol") - 1.0).alias("r"))
        .group_by("day").agg(pl.col("r").mean().alias("mkt")).drop_nulls()
    )
    return {int(r[0]): float(r[1]) for r in dc.iter_rows()}


def _entry_vol(close_arr: "np.ndarray", entry_bar: int, window: int = 168, min_n: int = 48) -> float:
    """Trailing hourly return std ending at entry_bar (the per-name vol for risk-sizing)."""
    lo = max(0, entry_bar - window)
    seg = close_arr[lo:entry_bar + 1]
    if seg.size < min_n + 1:
        return 0.0
    rets = np.diff(seg) / seg[:-1]
    rets = rets[np.isfinite(rets)]
    return float(np.std(rets)) if rets.size >= min_n else 0.0


def _run_trades(
    entries: pl.DataFrame,
    symbol_bars: dict[str, Any],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: ContinuousEventConfig,
    market_daily: dict[int, float] | None = None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Walk fresh entries in ts order; apply concurrency + cooldown + the inherited selection gates
    (age / fade-deceleration / market-context), size by the chosen rule, and simulate each via the
    daily engine's `_simulate_indexed_trade` (identical fills/funding/exit semantics).

    Returns (trades, skip-counts)."""
    if entries.is_empty():
        return _empty_trades(), {}
    # Exit ladder inherited from the daily engine (off unless the config sets them).
    lifecycle = TradeLifecycleConfig(
        start_date=config.start_date, end_date=config.end_date,
        hold_days=max(1, round(config.hold_hours / 24)), take_profit_pct=0.0,
        failed_fade_exit_hours=config.failed_fade_hours,
        failed_fade_loss_pct=config.failed_fade_loss_pct,
        failed_fade_min_mfe_pct=config.failed_fade_min_mfe_pct,
        failed_fade_close_location_min=0.0 if config.failed_fade_hours > 0 else 1.0,
        breakeven_arm_pct=config.breakeven_arm_pct,
        mfe_giveback_trigger_pct=config.mfe_giveback_trigger_pct,
        mfe_giveback_retain_pct=config.mfe_giveback_retain_pct,
    )
    base_nw = config.notional_weight
    inverse_vol = config.sizing_mode == "inverse_vol"
    clamp = max(config.vol_weight_clamp, 1.0)
    cooldown_ms = config.effective_cooldown_hours * MS_PER_HOUR
    hold_ms = config.hold_hours * MS_PER_HOUR
    max_hold_ms = config.max_hold_hours * MS_PER_HOUR
    delay_ms = (1 + config.entry_delay_hours) * MS_PER_HOUR
    decel_h = config.entry_decel_lookback_h
    age_min_ms = config.age_days_min * MS_PER_DAY
    state_mode = config.exit_mode == "state"
    stop_pct = config.stop_loss_pct if config.stop_loss_pct > 0.0 else None
    pause_window_ms = config.entry_pause_window_hours * MS_PER_HOUR
    breaker_on = config.entry_pause_after_adverse_exits > 0 and pause_window_ms > 0
    adverse_exit_ts: list[int] = []   # ASCENDING net-negative exit timestamps (circuit-breaker, causal)
    active: list[int] = []          # min-heap of actual exit timestamps of open positions
    last_entry: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    skipped_capacity = skipped_cooldown = skipped_no_bar = skipped_gate = skipped_breaker = 0
    syms = entries["symbol"].to_list()
    tss = entries["ts_ms"].to_list()
    comps = entries["composite"].to_list()
    turns = entries["turnover_quote"].to_list()
    spell_ends = entries["spell_end_ts"].to_list() if "spell_end_ts" in entries.columns else tss
    for sym, sig_ts, comp, turn, spell_end in zip(syms, tss, comps, turns, spell_ends):
        bars = symbol_bars.get(sym)
        if bars is None:
            skipped_no_bar += 1
            continue
        entry_bar_end = int(sig_ts) + delay_ms
        while active and active[0] <= entry_bar_end:
            heapq.heappop(active)
        # Circuit breaker: pause this entry if too many net-negative covers have CLOSED in the trailing
        # window (a correlated alt-squeeze). Causal — adverse_exit_ts holds only exits already simulated,
        # and the [entry_bar_end-window, entry_bar_end) slice counts only those that closed before now.
        if breaker_on:
            lo = bisect.bisect_left(adverse_exit_ts, entry_bar_end - pause_window_ms)
            hi = bisect.bisect_left(adverse_exit_ts, entry_bar_end)
            if hi - lo >= config.entry_pause_after_adverse_exits:
                skipped_breaker += 1
                continue
        if sym in last_entry and entry_bar_end - last_entry[sym] < cooldown_ms:
            skipped_cooldown += 1
            continue
        if len(active) >= config.max_active:
            skipped_capacity += 1
            continue
        entry_bar = bars["by_end"].get(entry_bar_end)
        if entry_bar is None:
            skipped_no_bar += 1
            continue
        close_arr = bars["close"]
        # --- inherited selection gates (squeeze-defense) ---
        if age_min_ms > 0 and (entry_bar_end - int(bars["bar_end_ts_ms"][0])) < age_min_ms:
            skipped_gate += 1
            continue
        if decel_h > 0 and entry_bar - decel_h >= 0:
            base_px = float(close_arr[entry_bar - decel_h])
            recent_ret = (float(close_arr[entry_bar]) / base_px - 1.0) if base_px > 0 else 0.0
            if recent_ret > config.entry_decel_max_ret:   # still ripping up -> not a confirmed fade
                skipped_gate += 1
                continue
        if config.market_min_ret_1d > -1.0 and market_daily is not None:
            mkt = market_daily.get((entry_bar_end // MS_PER_DAY) * MS_PER_DAY)
            if mkt is not None and mkt < config.market_min_ret_1d:   # short into a weak tape = squeeze risk
                skipped_gate += 1
                continue
        # --- sizing ---
        nw = base_nw
        if inverse_vol:
            rv = _entry_vol(close_arr, int(entry_bar))
            mult = min(max(config.target_vol_per_name / rv, 1.0 / clamp), clamp) if rv > 0 else 1.0
            nw = base_nw * mult
        if state_mode:
            planned_exit = min(int(spell_end) + delay_ms, entry_bar_end + max_hold_ms)
            planned_exit = max(planned_exit, entry_bar_end + MS_PER_HOUR)
        else:
            planned_exit = entry_bar_end + hold_ms
        trade = _simulate_indexed_trade(
            symbol=sym, side=config.side, score=float(comp) if comp is not None else 0.0,
            rank=int(config.decile), basket_id=_iso_day(entry_bar_end), signal_ts_ms=int(sig_ts),
            entry_bar=int(entry_bar), symbol_bars=bars, planned_exit_ts_ms=planned_exit,
            notional_weight=nw, position_weight=1.0, config=lifecycle,
            round_trip_cost_bps=_round_trip_bps(config, turn), stop_pct=stop_pct,
            rank_lookup={}, event_decay_threshold=0.0,
            funding_lookup=funding_lookup if config.use_funding else None,
            stop_fill_mode=config.stop_fill_mode, stop_slippage_cap_pct=config.stop_slippage_cap_pct,
        )
        if trade is None:
            skipped_no_bar += 1
            continue
        rows.append(trade)
        heapq.heappush(active, int(trade["exit_ts_ms"]))
        last_entry[sym] = entry_bar_end
        if breaker_on and float(trade.get("net_return") or 0.0) < 0.0:
            bisect.insort(adverse_exit_ts, int(trade["exit_ts_ms"]))
    skips = {
        "skipped_capacity": skipped_capacity,
        "skipped_cooldown": skipped_cooldown,
        "skipped_no_bar": skipped_no_bar,
        "skipped_gate": skipped_gate,
        "skipped_breaker": skipped_breaker,
    }
    if not rows:
        return _empty_trades(), skips
    return pl.DataFrame(rows), skips


def _additive_equity(trades: pl.DataFrame) -> pl.DataFrame:
    """Fixed-capital ADDITIVE daily equity (NOT compounding).

    For a capacity-capped book whose impact is sized off a FIXED deploy capital, additive PnL is
    the consistent accounting -- compounding implies a growing book that would face growing impact
    the fixed-capital model does not charge. `net_return` is already the fraction-of-capital PnL of
    each trade (notional_weight applied); sum it onto its exit day, then cumulative-SUM across days.
    Schema matches build_equity_curve so the same plot/CSV helpers work; equity = 1 + cum PnL,
    drawdown = equity - running-max (<=0, in return units)."""
    if trades.is_empty():
        return pl.DataFrame(
            {"ts_ms": pl.Series([], dtype=pl.Int64), "equity": pl.Series([], dtype=pl.Float64),
             "drawdown": pl.Series([], dtype=pl.Float64), "basket_return": pl.Series([], dtype=pl.Float64)}
        )
    daily = (
        trades.with_columns(((pl.col("exit_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("ts_ms"))
        .group_by("ts_ms").agg(pl.col("net_return").sum().alias("basket_return")).sort("ts_ms")
    )
    return daily.with_columns(
        (1.0 + pl.col("basket_return").cum_sum()).alias("equity")
    ).with_columns(
        (pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")
    ).select("ts_ms", "equity", "drawdown", "basket_return")


def _additive_summary(trades: pl.DataFrame, config: ContinuousEventConfig) -> dict[str, Any]:
    import statistics as st

    equity = _additive_equity(trades)
    if equity.is_empty():
        return {"n_trades": 0, "total_return": 0.0, "annualized_return": 0.0, "max_drawdown": 0.0,
                "mar": None, "sharpe_like": 0.0, "worst_day_return": 0.0}
    pnl = equity["basket_return"].to_list()
    total = float(equity["equity"][-1] - 1.0)
    maxdd = float(equity["drawdown"].min())          # <= 0, in return units
    ts = equity["ts_ms"]
    years = max((int(ts[-1]) - int(ts[0])) / (365.25 * MS_PER_DAY), 1e-9)
    ann = total / years
    sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else 0.0
    split_ms = _date_str_to_ms(config.split_date)
    funding_modes = set(str(m) for m in trades["funding_mode"].to_list()) if "funding_mode" in trades.columns else set()
    fmode = "missing" if (not funding_modes or funding_modes == {"missing"}) else (
        "modeled" if funding_modes == {"modeled"} else "partial")
    # compounding reference (the daily engine's accounting) -- shown for transparency, NOT headline
    comp = float((pl.Series([p + 1.0 for p in pnl]).cum_prod()[-1]) - 1.0) if pnl else 0.0
    return {
        "n_trades": int(trades.height),
        "total_return": total,
        "annualized_return": ann,
        "max_drawdown": maxdd,
        "mar": (ann / abs(maxdd)) if abs(maxdd) > 1e-9 else None,
        "sharpe_like": sharpe,
        "worst_day_return": float(min(pnl)) if pnl else 0.0,
        "win_rate": float((trades["net_return"] > 0.0).mean()),
        "gross_return": float(trades["gross_return"].sum()),
        "cost_return": float(trades["cost_return"].sum()),
        "funding_return": float(trades["funding_return"].sum()),
        "funding_mode": fmode,
        "early_return": float(trades.filter(pl.col("entry_ts_ms") < split_ms)["net_return"].sum()),
        "recent_return": float(trades.filter(pl.col("entry_ts_ms") >= split_ms)["net_return"].sum()),
        "compounding_total_return_ref": comp,
    }


def _daily_pnl_metrics(equity: pl.DataFrame) -> dict[str, Any]:
    """MAR/Sharpe/DD from any daily PnL series (cols: ts_ms, equity, drawdown, basket_return)."""
    import statistics as st

    if equity.is_empty():
        return {"total_return": 0.0, "annualized_return": 0.0, "max_drawdown": 0.0, "mar": None,
                "sharpe_like": 0.0, "worst_day_return": 0.0}
    pnl = equity["basket_return"].to_list()
    total = float(equity["equity"][-1] - 1.0)
    maxdd = float(equity["drawdown"].min())
    ts = equity["ts_ms"]
    years = max((int(ts[-1]) - int(ts[0])) / (365.25 * MS_PER_DAY), 1e-9)
    ann = total / years
    sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else 0.0
    return {"total_return": total, "annualized_return": ann, "max_drawdown": maxdd,
            "mar": (ann / abs(maxdd)) if abs(maxdd) > 1e-9 else None,
            "sharpe_like": sharpe, "worst_day_return": float(min(pnl)) if pnl else 0.0}


def _portfolio_mtm_equity(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """Daily portfolio MARK-TO-MARKET equity (additive, fixed-capital).

    Realized-PnL-at-exit (`_additive_equity`) only books a trade's whole PnL on its exit day, so a
    day where 25 still-open alt shorts all move against you shows nothing until they exit. This marks
    every OPEN position to the daily close, distributing each trade's gross PnL across the calendar
    days it is held; cost is booked on the entry day, funding on the exit day. Concurrent correlated
    moves therefore aggregate into the daily series → a real portfolio drawdown. Gross daily marks
    telescope to the trade's realized gross, so total return is unchanged; only the PATH (and thus DD
    and Sharpe) differ."""
    if trades.is_empty() or klines.is_empty():
        return _additive_equity(trades)  # empty-safe schema
    dc = (
        klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c"))
    )
    close = {(r[0], r[1]): r[2] for r in dc.iter_rows()}
    daily: dict[int, float] = {}
    for t in trades.select(
        "symbol", "side", "entry_ts_ms", "exit_ts_ms", "entry_price", "exit_price",
        "notional_weight", "cost_return", "funding_return",
    ).iter_rows():
        s, side, e_ts, x_ts, p0, px, w, cost_r, fund_r = t
        if p0 is None or p0 <= 0:
            continue
        sign = -1.0 if side == "short" else 1.0
        e_day = (int(e_ts) // MS_PER_DAY) * MS_PER_DAY
        x_day = (int(x_ts) // MS_PER_DAY) * MS_PER_DAY
        prev = float(p0)
        d = e_day
        while d <= x_day:
            curr = float(px) if d == x_day else float(close.get((s, d), prev))
            daily[d] = daily.get(d, 0.0) + w * sign * (curr - prev) / float(p0)  # gross MTM increment
            prev = curr
            d += MS_PER_DAY
        daily[e_day] = daily.get(e_day, 0.0) + float(cost_r)      # cost on entry day
        daily[x_day] = daily.get(x_day, 0.0) + float(fund_r)      # funding on exit day
    if not daily:
        return _additive_equity(trades)
    days = sorted(daily)
    return pl.DataFrame({"ts_ms": days, "basket_return": [daily[d] for d in days]}).with_columns(
        (1.0 + pl.col("basket_return").cum_sum()).alias("equity")
    ).with_columns(
        (pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")
    ).select("ts_ms", "equity", "drawdown", "basket_return")


def _split_metrics(trades: pl.DataFrame, config: ContinuousEventConfig) -> dict[str, dict[str, Any]]:
    """Additive (fixed-capital) summaries: full + early/recent (split on entry ts)."""
    if trades.is_empty():
        return {}
    split_ms = _date_str_to_ms(config.split_date)
    out: dict[str, dict[str, Any]] = {"full": _additive_summary(trades, config)}
    early = trades.filter(pl.col("entry_ts_ms") < split_ms)
    recent = trades.filter(pl.col("entry_ts_ms") >= split_ms)
    if not early.is_empty():
        out["early"] = _additive_summary(early, config)
    if not recent.is_empty():
        out["recent"] = _additive_summary(recent, config)
    return out


def _write_equity_png(equity: pl.DataFrame, path: Path, *, title: str) -> None:
    if equity.is_empty():
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    eq = equity.sort("ts_ms")
    xs = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in eq["ts_ms"].to_list()]
    fig, (ax, axd) = plt.subplots(2, 1, figsize=(13, 7), sharex=True, height_ratios=[3, 1])
    ax.plot(xs, [(v - 1.0) * 100 for v in eq["equity"].to_list()], color="#c0392b", lw=1.5)
    ax.set_ylabel("cumulative return (%)")
    ax.set_title(title)
    ax.grid(alpha=0.25)
    ax.text(0.99, 0.02, "EXPLORATORY engine — NOT promotion evidence", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=7, color="grey", style="italic")
    axd.fill_between(xs, [d * 100 for d in eq["drawdown"].to_list()], 0.0, color="#2c3e50", alpha=0.5)
    axd.set_ylabel("drawdown (%)")
    axd.grid(alpha=0.25)
    axd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def run_continuous_event_research(
    data_root: str | Path,
    *,
    config: ContinuousEventConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the execution-grade continuous-fade backtest and (optionally) write artifacts."""
    config = config or ContinuousEventConfig()
    root = Path(str(data_root)).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Data root does not exist: {root}")

    panel = build_continuous_panel(root, config)
    entries = _fresh_entries(panel, config) if not panel.is_empty() else panel

    kname = _autodetect_dataset_names(root)["klines_dataset"]
    start_ms, end_ms = _date_str_to_ms(config.start_date), _date_str_to_ms(config.end_date)
    pad_fwd = (config.hold_hours + config.entry_delay_hours + 4) * MS_PER_HOUR
    klines = _read_window(
        root, kname, start_ms=start_ms, end_ms=end_ms + pad_fwd,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
    )
    if config.exclude_symbols and not klines.is_empty():
        klines = klines.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    symbol_bars = _indexed_price_bars_by_symbol(klines) if not klines.is_empty() else {}

    funding_lookup = None
    if config.use_funding:
        fname = _autodetect_dataset_names(root)["funding_dataset"]
        funding = _read_window(root, fname, start_ms=start_ms - 10 * MS_PER_DAY, end_ms=end_ms + pad_fwd)
        funding_lookup = _funding_lookup(funding)

    market_daily = None
    if config.market_min_ret_1d > -1.0 and not klines.is_empty():
        market_daily = _market_daily_returns(klines)

    if not entries.is_empty() and symbol_bars:
        trades, skips = _run_trades(entries, symbol_bars, funding_lookup, config, market_daily)
    else:
        trades, skips = _empty_trades(), {}

    equity = _additive_equity(trades)
    splits = _split_metrics(trades, config)
    mtm_equity = _portfolio_mtm_equity(trades, klines)
    mtm = _daily_pnl_metrics(mtm_equity)

    funding_mode = splits.get("full", {}).get("funding_mode", "missing")
    run_label = "exploratory"  # engine-grade fills but modeled impact + research-tuned selection

    payload: dict[str, Any] = {
        "config": asdict(config),
        "config_hash": config.config_hash(),
        "data_root": str(root),
        "run_label": run_label,
        "n_fresh_entries": int(entries.height) if not entries.is_empty() else 0,
        "n_trades": int(trades.height),
        "skips": skips,
        "funding_mode": funding_mode,
        "metrics": splits,                 # realized-PnL-at-exit (additive, fixed-capital)
        "metrics_mtm": mtm,                # portfolio mark-to-market (correlated-DD aware)
    }

    if report_dir is not None:
        out_dir = Path(str(report_dir)).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        if not trades.is_empty():
            trades.write_csv(out_dir / "continuous_trades.csv")
            equity.write_csv(out_dir / "continuous_equity.csv")
            mtm_equity.write_csv(out_dir / "continuous_mtm_equity.csv")
            _write_equity_png(
                mtm_equity, out_dir / "continuous_mtm_equity.png",
                title=(f"PORTFOLIO MARK-TO-MARKET — {config.side} D{config.decile} | hold {config.hold_hours}h "
                       f"({config.exit_mode}) | MAR {mtm.get('mar')} DD {abs(mtm.get('max_drawdown') or 0)*100:.1f}%  [EXPLORATORY]"),
            )
            _write_equity_png(
                equity, out_dir / "continuous_equity.png",
                title=(f"continuous fade {config.side} D{config.decile} | hold {config.hold_hours}h | "
                       f"liq>=${int(config.liq_turnover_min/1000)}k | stop {config.stop_loss_pct:.0%} | "
                       f"MAR {splits.get('full', {}).get('mar')}  [EXPLORATORY]"),
            )
        (out_dir / "continuous_report.json").write_text(json.dumps(payload, indent=2, default=str))
        payload["report_dir"] = str(out_dir)

    return payload
