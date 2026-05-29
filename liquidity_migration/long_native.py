"""Crypto-native long-only event sleeve — NOT derived from academic papers.

Three asymmetric setups that exploit specific crypto market-structure patterns
the short sleeve does not cover:

1. CAPITULATION_REBOUND
   Violent 5-day flush + same-day absorption + volume spike + broad-regime
   bullish. The flush forces panic-sells from leveraged longs; absorption
   confirms the bottom-fishing crowd is real. Asymmetric stop/TP.

2. VOLUME_RESURRECTION
   A "forgotten" coin (low trailing 30d volume rank) suddenly re-enters the
   top tier on a strong green day. Captures the start of attention cycles
   before momentum factor strategies catch on.

3. FUNDING_SQUEEZE_LONG
   Trailing 24h funding heavily negative (shorts crowded) flips to positive
   (shorts capitulating) with a green day. The mechanical short cover
   produces fast, large upside.

Each pattern is an independent entry condition; an OR over all three feeds the
trade-selection queue. Per-symbol cooldown + max-concurrent + vol-parity
sizing manage the portfolio.

This is heuristic trader-style technical analysis, NOT factor research.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, date_ms, pct
from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .momentum_signals import daily_bars, add_returns_and_age
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import (
    _funding_lookup,
    _perp_funding_return,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from .volume_events import (
    _date_range,
    _exclude_symbols,
    _full_pit_universe_error,
    _full_pit_universe_pass,
    _iso_date,
    _iso_month,
    _pit_manifest_metadata,
    _write_equity_benchmark_chart,
)


# Splits are now exclusively a per-run config: see LongNativeConfig.splits.
# Default is `()` (whole-period reporting), which is the post-rebuild norm.
# Pristine OOS lives on the forward demo/paper ledger, not in a backtest split.


@dataclass(frozen=True, slots=True)
class LongNativeConfig:
    # --- window ---
    start_date: str = ""
    end_date: str = ""

    # --- universe ---
    universe_size: int = 30
    universe_volume_window_days: int = 90
    min_listing_history_days: int = 60
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS

    # --- broad regime ---
    regime_symbol: str = "BTCUSDT"
    regime_sma_days: int = 30  # looser than 50d so we catch capitulations early

    # --- pattern toggles ---
    enable_capitulation_rebound: bool = True
    enable_volume_resurrection: bool = True
    enable_funding_squeeze: bool = True

    # --- pattern 1: capitulation rebound ---
    cap_flush_window_days: int = 5
    cap_flush_min_return: float = -0.15
    cap_min_close_location: float = 0.65  # closed at upper 65% of day's range
    cap_volume_multiple: float = 1.5
    cap_volume_lookback_days: int = 7
    cap_stop_pct: float = 0.08
    cap_take_profit_pct: float = 0.25
    cap_max_hold_days: int = 14

    # --- pattern 2: volume resurrection ---
    res_prior_rank_pct_min: float = 0.50  # was below median in trailing 30d
    res_prior_rank_lookback_days: int = 30
    res_today_rank_max: int = 30
    res_min_day_return: float = 0.05
    res_volume_multiple: float = 3.0
    res_volume_lookback_days: int = 7
    res_stop_pct: float = 0.10
    res_take_profit_pct: float = 0.30
    res_max_hold_days: int = 21

    # --- pattern 3: funding squeeze ---
    fs_funding_lookback_days: int = 1  # trailing 24h
    fs_cumulative_funding_max: float = -0.001  # ≤ -0.1% across the lookback
    fs_recent_funding_min: float = 0.00005  # last 8h funding flipped to ≥ 0.005%
    fs_min_day_return: float = 0.03
    fs_volume_multiple: float = 1.0  # at least at trailing average
    fs_stop_pct: float = 0.08
    fs_take_profit_pct: float = 0.25
    fs_max_hold_days: int = 7

    # --- pattern 4: oversold bounce (deep flush + reversal day) ---
    enable_oversold_bounce: bool = False
    ob_flush_window_days: int = 14
    ob_flush_min_return: float = -0.20
    ob_today_min_return: float = 0.05
    ob_volume_multiple: float = 1.5
    ob_volume_lookback_days: int = 14
    ob_stop_pct: float = 0.10
    ob_take_profit_pct: float = 0.30
    ob_max_hold_days: int = 14

    # --- pattern 5: FOMO chase (huge 1d pump in confirmed trend) ---
    enable_fomo_chase: bool = False
    fc_min_day_return: float = 0.12
    fc_top_volume_rank_max: int = 8
    fc_min_close_location: float = 0.70
    fc_eth_regime_required: bool = True  # eth above 50d sma too
    fc_coin_above_own_sma_days: int = 0  # 0=disabled. If >0, coin must be above its own N-day SMA
    fc_coin_min_30d_return: float = -1.0  # -1=disabled. If higher, coin's own 30d return must be ≥ this
    fc_btc_not_near_high_window_days: int = 0  # 0=disabled. Exclude when BTC within fc_btc_high_proximity_pct of its N-day high
    fc_btc_high_proximity_pct: float = 0.95  # if BTC close / N-day high > this, exclude
    fc_btc_must_be_near_high_window_days: int = 0  # 0=disabled. REQUIRE BTC within fc_btc_must_be_near_high_pct of N-day high (opposite filter — only trade in fresh-high regimes)
    fc_btc_must_be_near_high_pct: float = 0.95
    fc_stop_pct: float = 0.08
    fc_take_profit_pct: float = 0.40
    fc_max_hold_days: int = 14
    # --- FC v2: forensic-derived filters & dynamic exits (all gated, disabled by default) ---
    fc_max_atr_pct: float = 1.0  # 1.0=disabled. Exclude entries when ATR/close > this (e.g. 0.11)
    fc_min_vol_vs_median: float = 0.0  # 0=disabled. Require today's vol/30d-median ≥ this
    fc_max_vol_vs_median: float = 0.0  # 0=disabled. Require today's vol/30d-median ≤ this
    fc_max_coin_60d_return: float = 0.0  # 0=disabled. Reject entries when 60d return > this (avoid late-cycle chase)
    fc_min_btc_sma_dist: float = -1.0  # -1=disabled. Require BTC > N% above 30d SMA
    fc_max_btc_sma_dist: float = 1.0  # 1=disabled. Reject when BTC is too far above SMA (parabolic guard)
    fc_use_atr_exits: bool = False  # If True, stop/TP are ATR multiples of entry price
    fc_atr_stop_mult: float = 1.5    # stop = K_stop × ATR_14 / close
    fc_atr_tp_mult: float = 4.0      # tp   = K_tp   × ATR_14 / close
    # --- FC v4: per-coin sigma-relative thresholds + multi-day triggers + loose gates ---
    fc_use_sigma_threshold: bool = False  # If True, entry pump must exceed K_sigma × σ_daily (not fixed pct)
    fc_sigma_mult: float = 2.5            # K_sigma for sigma-relative entry
    fc_enable_3d_trigger: bool = False    # If True, ALSO trigger on 3d cumulative pump (OR-of with 1d)
    fc_enable_7d_trigger: bool = False    # If True, ALSO trigger on 7d cumulative pump
    fc_enable_intraday_trigger: bool = False  # If True, ALSO trigger on max N-hour pump within the day
    fc_intraday_window_hours: int = 6     # rolling window size for intraday pump (4-12 typical)
    # --- FC v6: multi-feature alpha score for entry quality + sizing ---
    fc_use_alpha_score: bool = False      # If True, compute alpha score on every FC candidate
    fc_min_score: float = 0.0             # Skip entries with score below this (0=no filter, 0.5=median)
    fc_score_size_scaling: bool = False   # If True, scale position weight by score (full=score 1.0)
    fc_rank_by_score: bool = False        # If True, sort daily candidates by score (else by raw pump)
    # --- FC v7: per-coin own-quantile filters (truly per-coin individually) ---
    fc_use_own_pump_quantile: bool = False  # If True, require today's pump > coin's own P95 of past 90d
    fc_pump_quantile: float = 0.95          # quantile cutoff for own-pump trigger
    fc_max_atr_own_percentile: float = 1.0  # 1.0=disabled. Reject if current ATR > coin's own Pxx of past 90d
    fc_atr_quantile_window_days: int = 90   # window for both quantile rollings
    # --- FC v9: per-coin track-record sizing (in-backtest walk-forward) ---
    fc_use_coin_track_record: bool = False  # If True, scale position weight by coin's past win rate
    fc_track_min_trades: int = 3            # require ≥N past trades on this coin before scaling
    fc_track_scale_min: float = 0.4         # weight multiplier floor (poor track record)
    fc_track_scale_max: float = 1.6         # weight multiplier cap (strong track record)
    fc_track_blacklist_below: float = 0.0   # If >0, skip entry entirely when coin's win rate < this
    # --- FC v11: sniper entries (wait for retrace below signal close) ---
    fc_use_sniper_entry: bool = False       # If True, wait for retrace before entering
    fc_sniper_retrace_pct: float = 0.02     # require close ≤ signal_close × (1 - this)
    fc_sniper_deadline_hours: int = 12      # max hours to wait for retrace
    fc_sniper_skip_on_no_retrace: bool = False  # if True, skip signal when no retrace fires
    # --- FC v13: ADAPTIVE sniper — retrace pct = K × coin's ATR_14d/close ---
    fc_sniper_use_atr_retrace: bool = False  # if True, retrace_pct = atr_mult × atr_14d_pct
    fc_sniper_atr_mult: float = 0.5          # 0.5 × ATR = ~half-day move retrace
    # --- FC v11: scaled exit + breakeven move ---
    fc_use_scaled_exit: bool = False        # If True, take 50% off at TP/2 then move stop to BE
    fc_scaled_exit_trail_atr_mult: float = 0.0  # If >0, trail remainder by K × ATR after BE move
    # --- FC v12: daily-best-only filter (take top-N candidates per day, not all) ---
    fc_daily_best_n: int = 0                # 0=disabled. If >0, take only top-N FC candidates per day
    fc_btc_regime_required: bool = True   # If False, skip BTC SMA gate (loosen regime)
    fc_close_loc_multi_day: float = 0.6   # close-location requirement for multi-day triggers (looser)
    # FC quality FILTER from positioning data (combine new L/S alpha into FC):
    # skip FC pumps fired when retail is already euphorically long (buying the crowd's top).
    fc_lsr_filter: bool = False
    fc_max_lsr: float = 10.0              # skip FC entry if global (retail) long/short ratio > this
    fc_min_lsr: float = 0.0               # skip FC entry if global L/S ratio < this (0=off)
    # OI-confirmation: only take FC pumps where open interest is RISING (new money /
    # conviction) vs falling (short-covering fakeout). Real per-trade quality filter.
    fc_require_oi_rising: bool = False
    fc_oi_chg_min: float = 0.0           # require 7d OI change > this at the pump

    # --- trailing-stop overlay (applies to all patterns when enabled) ---
    use_trailing_stop: bool = False
    trailing_atr_multiple: float = 1.5
    trailing_atr_window_days: int = 20

    # --- pattern 6: buy-the-dip in confirmed uptrend ---
    enable_uptrend_dip: bool = False
    ud_sma_days: int = 50  # coin must be above this SMA
    ud_today_min_drop: float = -0.15  # today's drop ≥ -7%
    ud_today_max_drop: float = -0.07  # but ≤ -15% (not catastrophic)
    ud_7d_return_min: float = -0.05  # 7d still net positive-ish
    ud_stop_pct: float = 0.06
    ud_take_profit_pct: float = 0.18
    ud_max_hold_days: int = 14

    # --- pattern 7: cross-sectional momentum rotation (continuous, NOT event-sparse) ---
    # Rank in-universe names by trailing return each day, hold the top-K. Unlike the
    # FC event patterns this fires continuously, so it is the lever for trade COUNT.
    enable_xsec_momentum: bool = False
    xsec_lookback_days: int = 30        # trailing-return window for the momentum score
    xsec_top_k: int = 5                 # hold the top-K in-universe momentum names
    xsec_require_regime: bool = True    # only rotate while BTC regime_on
    xsec_min_momentum: float = 0.0      # require trailing return strictly above this
    xsec_use_residual: bool = False     # rank by BTC-beta-stripped RESIDUAL momentum (idiosyncratic)
    xsec_beta_window: int = 60          # rolling window for the coin-vs-BTC beta
    xsec_stop_pct: float = 0.15
    xsec_take_profit_pct: float = 0.50
    xsec_max_hold_days: int = 10
    xsec_require_btc_200: bool = False  # also require BTC above its 200d SMA (slow institutional trend)
    xsec_min_btc_vol_pos: float = 0.0   # require BTC 30d-RV position in trailing-year range >= this (0=off)

    # --- pattern 8: low-volatility selection (hold the CALMEST in-universe names) ---
    # Attacks the Sharpe denominator: low-vol majors trend smoothly. Fires continuously.
    enable_lowvol: bool = False
    lowvol_top_k: int = 5
    lowvol_require_regime: bool = True
    lowvol_min_mom: float = -1.0        # require coin 30d return >= this (-1=off; 0.0 = calm AND rising)
    lowvol_stop_pct: float = 0.10
    lowvol_take_profit_pct: float = 0.30
    lowvol_max_hold_days: int = 14

    # --- pattern 9: short-term reversal (buy a 1-day dip inside an uptrend) ---
    # High win-rate / low-variance profile. Fires often.
    enable_reversal: bool = False
    rev_drop_min: float = -0.12         # yesterday's simple return must be within [rev_drop_min, rev_drop_max]
    rev_drop_max: float = -0.04
    rev_require_uptrend: bool = True     # coin's own 30d return must be > 0 (dip in an uptrend, not a knife)
    rev_require_regime: bool = True
    rev_stop_pct: float = 0.08
    rev_take_profit_pct: float = 0.10
    rev_max_hold_days: int = 3

    # --- pattern 10: funding-carry (long the most short-crowded names) ---
    # Structurally different edge: negative funding pays the long to hold + squeeze upside.
    enable_funding_carry: bool = False
    funding_carry_top_k: int = 5
    funding_carry_require_regime: bool = True
    funding_carry_require_uptrend: bool = True   # coin_30d_return > 0 (skip negative-funding downtrends)
    funding_carry_max_lookback_sum: float = 0.0  # only coins with trailing funding <= this (0 = negative only)
    funding_carry_stop_pct: float = 0.10
    funding_carry_take_profit_pct: float = 0.30
    funding_carry_max_hold_days: int = 10

    # --- pattern 11: OI-confirmed momentum (positioning conviction) ---
    # Long the in-universe names with the fastest-growing open interest AND a rising
    # price (new money building positions = real trend fuel, not a hollow rally).
    enable_oi_momentum: bool = False
    oi_top_k: int = 5
    oi_require_regime: bool = True
    oi_min_oi_chg_7d: float = 0.0       # require 7d OI growth >= this (positioning building)
    oi_require_price_up: bool = True    # require coin_30d_return > 0 (price trending)
    oi_stop_pct: float = 0.12
    oi_take_profit_pct: float = 0.40
    oi_max_hold_days: int = 10

    # --- pattern 12: positioning metrics (binance.vision OI / LSR / taker) ---
    # Cross-sectional select by a positioning factor. Genuinely-new participant-
    # composition data (top-trader vs retail long/short ratio, taker flow).
    enable_metrics_signal: bool = False
    metrics_factor: str = "smart_minus_dumb"  # global_lsr_low | toptrader_lsr_high | smart_minus_dumb | taker_buy | oi_growth
    metrics_top_k: int = 5
    metrics_require_regime: bool = True
    metrics_require_uptrend: bool = False
    metrics_stop_pct: float = 0.12
    metrics_take_profit_pct: float = 0.40
    metrics_max_hold_days: int = 10
    metrics_fill_only: bool = False     # FC-priority: metrics fills only leftover daily capacity

    # --- portfolio ---
    max_concurrent_positions: int = 5
    cooldown_days: int = 7
    entry_delay_hours: int = 1
    gross_exposure: float = 1.0
    # H1: per-position notional multiplier, mirroring the LIVE long daemon's
    # --notional-multiplier. Default 1.0 = the gross the backtest has always
    # modelled (NO change to existing results). The deployed long sleeve runs
    # notional_multiplier=10 (an explicit owner choice, 2x the research-Sharpe
    # peak of ~5x), so its live P&L volatility/drawdown is NOT comparable to a
    # multiplier=1 backtest. Set this to the deployed value to validate the long
    # sleeve at the gross it actually trades before any real-money decision.
    # See docs/preregistration/round2/r-audit-methodology-hardening.md.
    notional_multiplier: float = 1.0
    sizing: str = "vol_parity"  # or "equal"
    vol_estimate_window_days: int = 30
    vol_floor_annual: float = 0.30
    max_position_weight: float = 0.30
    # Volatility-managed exposure (Moreira-Muir / Barroso-Santa-Clara): scale the
    # whole book by vol_target / BTC-realized-vol. Sizes DOWN into high-vol regimes
    # (where drawdowns cluster) and UP when calm. Lifts Sharpe + MAR if vol clusters.
    enable_vol_target: bool = False
    vol_target_annual: float = 0.60
    vol_target_min_scale: float = 0.30
    vol_target_max_scale: float = 2.0
    # Drawdown throttle: when the book's realized equity is in drawdown beyond
    # dd_throttle_trigger, scale new-position size by dd_throttle_scale (de-lever
    # during losing streaks). Caps the worst peak-to-trough -> lifts MAR.
    enable_dd_throttle: bool = False
    dd_throttle_trigger: float = -0.03
    dd_throttle_scale: float = 0.0

    # --- B.3 concentration limits ---
    # max_per_symbol_concurrent: explicit cap on the same-symbol-twice case
    #   (already implicit via skipped_already_held; set to 0 to disable, 1 to
    #   keep the current behaviour, >1 to allow pyramiding).
    # max_per_sector_concurrent: 0=disabled. e.g. 2 caps meme-coin concurrency.
    # max_per_symbol_weight: per-trade cap on post-vol-parity weight as a
    #   fraction of gross. The trade is sized down (not skipped) when this binds.
    # sector_map_path: optional JSON {"WIFUSDT":"meme",...}; missing symbols
    #   fall into bucket "unknown". Path relative to repo root unless absolute.
    max_per_symbol_concurrent: int = 1
    max_per_sector_concurrent: int = 0
    max_per_symbol_weight: float = 0.30
    sector_map_path: str | None = None

    # --- costs ---
    cost_multiplier: float = 3.0

    # --- gates ---
    require_pit_membership: bool = True
    require_full_pit_universe: bool = True

    # --- reporting ---
    # Whole-period only by default. There is no honest internal OOS surface;
    # both per-venue datasets span their full available history and pristine
    # OOS is the forward demo/paper ledger. Pass an explicit `splits` tuple
    # only when you specifically want per-window reporting and have a
    # pre-registered reason for that choice (see docs/parameter_pre_registration.md).
    splits: tuple[tuple[str, str, str], ...] = ()


def run_long_native_research(
    data_root: str | Path,
    *,
    config: LongNativeConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = config or LongNativeConfig()
    costs = cost_config or CostConfig()
    root = Path(data_root).expanduser()
    output_dir = Path(report_dir) if report_dir else root / "reports" / "long_native_research"
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_klines = read_dataset_columns(
        root, "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    if raw_klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    funding = read_dataset(root, "funding")
    archive_manifest = read_dataset(root, "archive_trade_manifest")
    open_interest = read_dataset(root, "open_interest") if (cfg.enable_oi_momentum or cfg.fc_require_oi_rising) else None
    metrics_df = None
    if cfg.enable_metrics_signal or cfg.fc_lsr_filter:
        mfiles: list[Path] = []
        for cand in ("binance_usdm_metrics", "positioning_lsr"):  # Binance metrics / Bybit L/S
            mdir = root / cand
            if mdir.exists():
                mfiles = sorted(mdir.glob("*.parquet"))
                break
        if mfiles:
            metrics_df = pl.concat([pl.read_parquet(f) for f in mfiles], how="vertical_relaxed")

    klines = _exclude_symbols(raw_klines, cfg.exclude_symbols)
    funding = _exclude_symbols(funding, cfg.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, cfg.exclude_symbols)
    if open_interest is not None and not open_interest.is_empty():
        open_interest = _exclude_symbols(open_interest, cfg.exclude_symbols)
    if metrics_df is not None and not metrics_df.is_empty():
        metrics_df = _exclude_symbols(metrics_df, cfg.exclude_symbols)

    full_pit_universe_pass = _full_pit_universe_pass(klines, archive_manifest)
    if cfg.require_full_pit_universe and not full_pit_universe_pass:
        raise RuntimeError(_full_pit_universe_error(klines, archive_manifest))

    features = build_long_features(klines, funding=funding, open_interest=open_interest, config=cfg, metrics=metrics_df)
    features = _filter_signal_window(features, start=cfg.start_date, end=cfg.end_date)
    if features.is_empty():
        raise RuntimeError("No features generated")

    bars_by_symbol = _bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding) if funding is not None and not funding.is_empty() else None

    trades, lifecycle_stats, event_counts = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg, costs=costs,
    )

    bt_config = TradeLifecycleConfig(
        score="long_native", hold_days=max(cfg.cap_max_hold_days, cfg.res_max_hold_days, cfg.fs_max_hold_days),
        rebalance_days=7, gross_exposure=cfg.gross_exposure, entry_delay_hours=cfg.entry_delay_hours,
        cost_multiplier=cfg.cost_multiplier, side_mode="long_high_short_low",
    )
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    monthly = _monthly_returns(baskets)
    splits = _split_rows(baskets, config=bt_config, splits=cfg.splits)
    funding_mode = summary.get("funding_mode", "missing")
    promotion = _evaluate_promotion(splits=splits, summary=summary,
                                    funding_mode=funding_mode,
                                    full_pit_universe_pass=full_pit_universe_pass)

    if not trades.is_empty():
        trades.write_csv(output_dir / "long_native_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "long_native_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "long_native_equity.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "long_native_monthly.csv")

    # Equity-vs-BTC PNG mirrors the short-sleeve `volume_event_best_equity_btc.png`
    # so operators get a comparable visual benchmark without a side-step renderer.
    # The long-only sleeve fires sparse FOMO-chase setups so the strategy line is
    # near-flat compared to BTC over the same window — that's the design (low DD,
    # uncorrelated returns). For a purer view of strategy P&L use the CSVs.
    chart_metadata: dict[str, Any] = {}
    if not equity.is_empty() and not raw_klines.is_empty():
        try:
            chart_metadata = _write_equity_benchmark_chart(
                output_dir,
                root=root,
                equity=equity,
                raw_klines=raw_klines,
                monthly=monthly if not monthly.is_empty() else None,
                png_name="long_native_equity_btc.png",
            )
        except Exception:  # noqa: BLE001 - chart failure must not fail the run
            chart_metadata = {}

    metadata = {
        "config": asdict(cfg),
        "rows": {"features": features.height, "trades": trades.height, "baskets": baskets.height},
        "date_range": _date_range(features),
        "pit_manifest": _pit_manifest_metadata(archive_manifest, features, klines),
        "cost_model": {
            **asdict(costs), "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
            "cost_multiplier": cfg.cost_multiplier,
            "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier,
        },
        "summary": summary, "lifecycle": lifecycle_stats, "splits": splits, "promotion": promotion,
        "event_counts": event_counts,
        "run_label": _run_label(full_pit_universe_pass=full_pit_universe_pass, funding_mode=funding_mode,
                                 archive_manifest_empty=archive_manifest.is_empty()),
        "equity_chart": chart_metadata,
    }
    (output_dir / "long_native_research_report.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    (output_dir / "long_native_research_report.md").write_text(format_long_native_report(metadata), encoding="utf-8")
    return {**metadata, "report_dir": str(output_dir)}


def build_long_features(klines_1h: pl.DataFrame, *, funding: pl.DataFrame | None, config: LongNativeConfig, open_interest: pl.DataFrame | None = None, metrics: pl.DataFrame | None = None) -> pl.DataFrame:
    daily = daily_bars(klines_1h)
    if daily.is_empty():
        return daily
    daily = add_returns_and_age(daily)
    # Intraday pump: max rolling N-hour log return observed within each (symbol, date).
    # Computed from 1h klines, then aggregated to daily — PIT-safe (uses only bars ≤ signal close).
    win = max(1, int(config.fc_intraday_window_hours))
    intra = (
        klines_1h.sort(["symbol", "ts_ms"])
        .with_columns(
            (pl.col("close") / pl.col("close").shift(win).over("symbol")).log().alias("_pump_intra")
        )
        .group_by(["symbol", "date"])
        .agg(pl.col("_pump_intra").max().alias("intra_max_Nh_pump_log"))
    )
    daily = daily.join(intra, on=["symbol", "date"], how="left")

    # Per-day features needed by the patterns.
    annualization = math.sqrt(365.0)
    daily = daily.sort(["symbol", "ts_ms"]).with_columns([
        # close-location (where in the daily range did we close?)
        pl.when((pl.col("high") - pl.col("low")) > 1e-12)
          .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
          .otherwise(0.5).alias("close_location"),
        # rolling realized vol
        (pl.col("log_return").rolling_std(window_size=config.vol_estimate_window_days,
                                          min_samples=config.vol_estimate_window_days)
         .over("symbol") * annualization).alias("realized_vol"),
        # trailing turnover for universe
        pl.col("turnover_quote").rolling_median(window_size=config.universe_volume_window_days,
                                                min_samples=config.universe_volume_window_days)
         .over("symbol").alias("turnover_median_90d"),
        # rolling 5-day return for capitulation flush detection
        (pl.col("close") / pl.col("close").shift(config.cap_flush_window_days).over("symbol") - 1.0)
         .alias("return_5d"),
        # rolling min low over flush window
        pl.col("low").rolling_min(window_size=config.cap_flush_window_days, min_samples=config.cap_flush_window_days)
          .over("symbol").alias("low_5d"),
        # 14d return for oversold bounce
        (pl.col("close") / pl.col("close").shift(config.ob_flush_window_days).over("symbol") - 1.0)
         .alias("return_14d_ob"),
        # 7d return (for uptrend dip)
        (pl.col("close") / pl.col("close").shift(7).over("symbol") - 1.0).alias("return_7d"),
        # coin SMA for uptrend filter
        pl.col("close").rolling_mean(window_size=config.ud_sma_days, min_samples=config.ud_sma_days)
         .over("symbol").alias("coin_sma"),
        # streak of days above own SMA
        (pl.col("close") > pl.col("close").rolling_mean(window_size=config.ud_sma_days, min_samples=config.ud_sma_days).over("symbol"))
         .cast(pl.Int64).alias("_above_sma_flag"),
        # coin's own 30d return for FC filter
        (pl.col("close") / pl.col("close").shift(30).over("symbol") - 1.0).alias("coin_30d_return"),
        # coin's own 60d return (FC v2 — late-cycle chase guard)
        (pl.col("close") / pl.col("close").shift(60).over("symbol") - 1.0).alias("coin_60d_return"),
        # turnover-vs-own-30d-median (FC v2 — relative volume confirmation, not exhaustion)
        pl.col("turnover_quote").rolling_median(window_size=30, min_samples=15)
         .over("symbol").alias("turnover_median_30d"),
    ])
    daily = daily.with_columns(
        (pl.col("turnover_quote") / pl.col("turnover_median_30d")).alias("vol_vs_30d_median")
    )
    # v4: multi-day cumulative log returns + per-window close locations + daily sigma
    daily = daily.with_columns([
        (pl.col("close") / pl.col("close").shift(3).over("symbol")).log().alias("pump_3d_log"),
        (pl.col("close") / pl.col("close").shift(7).over("symbol")).log().alias("pump_7d_log"),
        # 3d high/low for 3d close-location
        pl.col("high").rolling_max(window_size=3, min_samples=3).over("symbol").alias("high_3d"),
        pl.col("low").rolling_min(window_size=3, min_samples=3).over("symbol").alias("low_3d"),
        pl.col("high").rolling_max(window_size=7, min_samples=7).over("symbol").alias("high_7d"),
        pl.col("low").rolling_min(window_size=7, min_samples=7).over("symbol").alias("low_7d"),
        # daily sigma derived from annualized 30d realized vol
        (pl.col("realized_vol") / math.sqrt(365.0)).alias("sigma_daily_30d"),
    ]).with_columns([
        pl.when((pl.col("high_3d") - pl.col("low_3d")) > 1e-12)
          .then((pl.col("close") - pl.col("low_3d")) / (pl.col("high_3d") - pl.col("low_3d")))
          .otherwise(0.5).alias("close_loc_3d"),
        pl.when((pl.col("high_7d") - pl.col("low_7d")) > 1e-12)
          .then((pl.col("close") - pl.col("low_7d")) / (pl.col("high_7d") - pl.col("low_7d")))
          .otherwise(0.5).alias("close_loc_7d"),
    ])
    # coin own N-day SMA (FC filter) — computed only if enabled to keep features small
    if config.fc_coin_above_own_sma_days > 0:
        daily = daily.with_columns(
            pl.col("close").rolling_mean(window_size=config.fc_coin_above_own_sma_days,
                                         min_samples=config.fc_coin_above_own_sma_days).over("symbol")
            .alias("coin_fc_sma")
        )
    else:
        daily = daily.with_columns(pl.lit(None, dtype=pl.Float64).alias("coin_fc_sma"))
    daily = daily.with_columns([
        # 7d volume avg for confirmation
        pl.col("turnover_quote").rolling_mean(window_size=config.cap_volume_lookback_days,
                                              min_samples=config.cap_volume_lookback_days)
         .over("symbol").alias("vol7d_mean"),
        pl.col("turnover_quote").rolling_mean(window_size=config.res_volume_lookback_days,
                                              min_samples=config.res_volume_lookback_days)
         .over("symbol").alias("vol7d_mean_res"),
        # 14d volume avg for oversold bounce
        pl.col("turnover_quote").rolling_mean(window_size=config.ob_volume_lookback_days,
                                              min_samples=config.ob_volume_lookback_days)
         .over("symbol").alias("vol14d_mean"),
        # ATR for trailing stop
        pl.max_horizontal([
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1).over("symbol")).abs(),
            (pl.col("low") - pl.col("close").shift(1).over("symbol")).abs(),
        ]).alias("true_range"),
    ])
    daily = daily.with_columns([
        pl.col("true_range").rolling_mean(window_size=config.trailing_atr_window_days,
                                          min_samples=config.trailing_atr_window_days)
        .over("symbol").alias("atr_20d"),
        # 14d ATR for FC dynamic exits + ATR filter (shorter window = more responsive)
        pl.col("true_range").rolling_mean(window_size=14, min_samples=7)
        .over("symbol").alias("atr_14d"),
    ])
    daily = daily.with_columns(
        (pl.col("atr_14d") / pl.col("close")).alias("atr_14d_pct")
    )
    # FC v7: per-coin own-quantile features (each coin gets its own thresholds)
    qwin = max(30, int(config.fc_atr_quantile_window_days))
    daily = daily.with_columns([
        # coin's own past-90d pump quantile (e.g. P95 of own log_return distribution)
        pl.col("log_return").rolling_quantile(
            quantile=config.fc_pump_quantile, window_size=qwin, min_samples=30
        ).over("symbol").alias("own_pump_quantile_90d"),
        # coin's own past-90d ATR quantile (used to detect "unusually high vol for this coin")
        pl.col("atr_14d_pct").rolling_quantile(
            quantile=min(0.99, max(0.50, config.fc_max_atr_own_percentile)),
            window_size=qwin, min_samples=30
        ).over("symbol").alias("own_atr_quantile_90d"),
    ])

    # universe rank by today's volume (descending)
    daily = daily.with_columns(
        pl.col("turnover_quote").rank(method="ordinal", descending=True).over("ts_ms").alias("today_volume_rank")
    )
    # rolling turnover-rank percentile based on 30d average rank
    daily = daily.with_columns(
        pl.col("today_volume_rank").rolling_mean(window_size=config.res_prior_rank_lookback_days,
                                                 min_samples=config.res_prior_rank_lookback_days)
         .over("symbol").alias("avg_rank_30d")
    )
    # Normalize avg rank to a percentile (0=best, 1=worst) vs configured universe size.
    daily = daily.with_columns(
        (pl.col("avg_rank_30d") / pl.lit(float(config.universe_size))).clip(0.0, 1.0).alias("avg_rank_pct_30d")
    )
    daily = daily.with_columns([
        pl.col("close").rolling_max(window_size=90, min_samples=30).over("symbol").alias("high_90d"),
        pl.col("close").rolling_min(window_size=90, min_samples=30).over("symbol").alias("low_90d"),
    ])
    daily = daily.with_columns(
        pl.when((pl.col("high_90d") - pl.col("low_90d")).abs() > 1e-12)
        .then((pl.col("close") - pl.col("low_90d")) / (pl.col("high_90d") - pl.col("low_90d")))
        .otherwise(None)
        .alias("pos_in_90d_range")
    )

    # universe membership
    daily = daily.with_columns([
        pl.col("turnover_median_90d").rank(method="ordinal", descending=True).over("ts_ms").alias("universe_rank"),
    ]).with_columns(
        (
            (pl.col("universe_rank") <= config.universe_size)
            & (pl.col("symbol_age_days") >= config.min_listing_history_days)
            & pl.col("turnover_median_90d").is_finite()
        ).alias("in_universe")
    )

    # cross-sectional momentum score + per-day rank among in-universe names. PIT-safe.
    # If xsec_use_residual: score = BTC-beta-stripped RESIDUAL momentum (idiosyncratic
    # cumulative return), documented ~2x more robust than total-return momentum.
    if config.xsec_use_residual:
        _btc_lr = daily.filter(pl.col("symbol") == config.regime_symbol).select(
            ["ts_ms", pl.col("log_return").alias("btc_lr")]
        )
        w = max(20, int(config.xsec_beta_window))
        daily = daily.join(_btc_lr, on="ts_ms", how="left").with_columns(pl.col("btc_lr").fill_null(0.0))
        daily = daily.sort(["symbol", "ts_ms"]).with_columns([
            (pl.col("log_return") * pl.col("btc_lr")).rolling_mean(w, min_samples=w).over("symbol").alias("_xy"),
            pl.col("log_return").rolling_mean(w, min_samples=w).over("symbol").alias("_mx"),
            pl.col("btc_lr").rolling_mean(w, min_samples=w).over("symbol").alias("_my"),
            (pl.col("btc_lr") ** 2).rolling_mean(w, min_samples=w).over("symbol").alias("_yy"),
        ]).with_columns(
            ((pl.col("_xy") - pl.col("_mx") * pl.col("_my")) /
             (pl.col("_yy") - pl.col("_my") ** 2).clip(lower_bound=1e-9)).alias("_beta")
        ).with_columns(
            (pl.col("log_return") - pl.col("_beta") * pl.col("btc_lr")).alias("_resid_lr")
        ).with_columns(
            pl.col("_resid_lr").rolling_sum(config.xsec_lookback_days, min_samples=config.xsec_lookback_days)
            .over("symbol").alias("mom_score")
        )
    else:
        daily = daily.with_columns(
            (pl.col("close") / pl.col("close").shift(config.xsec_lookback_days).over("symbol") - 1.0).alias("mom_score")
        )
    daily = daily.with_columns(
        pl.when(pl.col("in_universe") & pl.col("mom_score").is_not_null())
          .then(pl.col("mom_score")).otherwise(-1e9).alias("_mom_rankable")
    ).with_columns(
        pl.col("_mom_rankable").rank(method="ordinal", descending=True).over("ts_ms").alias("mom_rank")
    )
    # low-volatility rank among in-universe names (ascending: rank 1 = calmest)
    daily = daily.with_columns(
        pl.when(pl.col("in_universe") & pl.col("realized_vol").is_not_null())
          .then(pl.col("realized_vol")).otherwise(1e9).alias("_lv_rankable")
    ).with_columns(
        pl.col("_lv_rankable").rank(method="ordinal", descending=False).over("ts_ms").alias("lowvol_rank")
    )

    # BTC regime gate + BTC N-day high (for "not near top" filter)
    btc = daily.filter(pl.col("symbol") == config.regime_symbol).sort("ts_ms")
    if not btc.is_empty():
        btc = btc.with_columns([
            pl.col("close").rolling_mean(window_size=config.regime_sma_days, min_samples=config.regime_sma_days).alias("regime_sma"),
            pl.col("close").rolling_mean(window_size=200, min_samples=100).alias("regime_sma_200"),
            (pl.col("log_return").rolling_std(window_size=30, min_samples=20) * math.sqrt(365.0)).alias("btc_rv_30"),
        ])
        btc = btc.with_columns([
            pl.col("btc_rv_30").rolling_min(window_size=365, min_samples=120).alias("_rvmin"),
            pl.col("btc_rv_30").rolling_max(window_size=365, min_samples=120).alias("_rvmax"),
        ])
        # Pick the larger of the two high-window settings for computing the rolling max
        high_w = max(config.fc_btc_not_near_high_window_days, config.fc_btc_must_be_near_high_window_days)
        if high_w > 0:
            btc = btc.with_columns(
                pl.col("close").rolling_max(window_size=high_w, min_samples=high_w).alias("btc_recent_high")
            ).with_columns(
                (pl.col("close") / pl.col("btc_recent_high")).alias("btc_high_proximity")
            )
        else:
            btc = btc.with_columns(pl.lit(0.0, dtype=pl.Float64).alias("btc_high_proximity"))
        btc = btc.with_columns([
            (pl.col("close") > pl.col("regime_sma")).alias("regime_on"),
            (pl.col("close") / pl.col("regime_sma") - 1.0).alias("btc_sma_dist"),
            (pl.col("close") > pl.col("regime_sma_200")).alias("btc_trend_200"),
            pl.when((pl.col("_rvmax") - pl.col("_rvmin")) > 1e-12)
              .then((pl.col("btc_rv_30") - pl.col("_rvmin")) / (pl.col("_rvmax") - pl.col("_rvmin")))
              .otherwise(0.5).alias("btc_vol_pos"),
        ]).select(["ts_ms", "regime_on", "btc_high_proximity", "btc_sma_dist", "btc_trend_200", "btc_vol_pos", "btc_rv_30"])
        daily = daily.join(btc, on="ts_ms", how="left").with_columns([
            pl.col("regime_on").fill_null(False),
            pl.col("btc_high_proximity").fill_null(0.0),
            pl.col("btc_sma_dist").fill_null(0.0),
            pl.col("btc_trend_200").fill_null(False),
            pl.col("btc_vol_pos").fill_null(0.5),
            pl.col("btc_rv_30").fill_null(0.8),
        ])
    else:
        daily = daily.with_columns([
            pl.lit(False).alias("regime_on"),
            pl.lit(0.0, dtype=pl.Float64).alias("btc_high_proximity"),
            pl.lit(0.0, dtype=pl.Float64).alias("btc_sma_dist"),
            pl.lit(False).alias("btc_trend_200"),
            pl.lit(0.5, dtype=pl.Float64).alias("btc_vol_pos"),
            pl.lit(0.8, dtype=pl.Float64).alias("btc_rv_30"),
        ])

    # ETH regime gate (used by FOMO chase pattern)
    eth = daily.filter(pl.col("symbol") == "ETHUSDT").sort("ts_ms")
    if not eth.is_empty():
        eth = eth.with_columns(
            pl.col("close").rolling_mean(window_size=config.regime_sma_days, min_samples=config.regime_sma_days).alias("eth_sma")
        ).with_columns(
            (pl.col("close") > pl.col("eth_sma")).alias("eth_regime_on")
        ).select(["ts_ms", "eth_regime_on"])
        daily = daily.join(eth, on="ts_ms", how="left").with_columns(pl.col("eth_regime_on").fill_null(False))
    else:
        daily = daily.with_columns(pl.lit(False).alias("eth_regime_on"))

    # Funding: cumulative trailing N-day funding + most recent funding rate
    if funding is not None and not funding.is_empty():
        rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in funding.columns else (
            "funding_rate" if "funding_rate" in funding.columns else None
        )
        if rate_col is not None:
            fund_daily = (
                funding.select(["ts_ms", "symbol", rate_col])
                .filter(pl.col(rate_col).is_finite())
                .with_columns(((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)) + MS_PER_DAY).alias("ts_ms_day_end"))
                .sort(["symbol", "ts_ms"])
                .group_by(["symbol", "ts_ms_day_end"], maintain_order=True)
                .agg([
                    pl.col(rate_col).sum().alias("funding_day_sum"),
                    pl.col(rate_col).last().alias("funding_recent"),
                ])
                .rename({"ts_ms_day_end": "ts_ms"})
                .sort(["symbol", "ts_ms"])
                .with_columns(
                    pl.col("funding_day_sum").rolling_sum(window_size=config.fs_funding_lookback_days,
                                                          min_samples=1).over("symbol")
                     .alias("funding_lookback_sum")
                )
                .select(["ts_ms", "symbol", "funding_lookback_sum", "funding_recent"])
            )
            daily = daily.join(fund_daily, on=["ts_ms", "symbol"], how="left")
        else:
            daily = daily.with_columns([
                pl.lit(None, dtype=pl.Float64).alias("funding_lookback_sum"),
                pl.lit(None, dtype=pl.Float64).alias("funding_recent"),
            ])
    else:
        daily = daily.with_columns([
            pl.lit(None, dtype=pl.Float64).alias("funding_lookback_sum"),
            pl.lit(None, dtype=pl.Float64).alias("funding_recent"),
        ])

    # funding-carry rank: most-negative trailing cumulative funding first
    # (short-crowded -> long gets paid the carry + has squeeze upside).
    daily = daily.with_columns(
        pl.when(pl.col("in_universe") & pl.col("funding_lookback_sum").is_not_null())
          .then(pl.col("funding_lookback_sum")).otherwise(1e9).alias("_fund_rankable")
    ).with_columns(
        pl.col("_fund_rankable").rank(method="ordinal", descending=False).over("ts_ms").alias("funding_rank")
    )

    # open-interest features (positioning / conviction). Hourly OI -> daily last,
    # aligned to the same day-end ts_ms convention as funding. oi_rank = rank
    # in-universe by 7d OI growth (descending: rank 1 = fastest-growing positioning).
    if open_interest is not None and not open_interest.is_empty() and "open_interest" in open_interest.columns:
        oi_daily = (
            open_interest.select(["ts_ms", "symbol", "open_interest"])
            .filter(pl.col("open_interest").is_finite())
            .with_columns(((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)) + MS_PER_DAY).alias("ts_ms_day_end"))
            .sort(["symbol", "ts_ms"])
            .group_by(["symbol", "ts_ms_day_end"], maintain_order=True)
            .agg(pl.col("open_interest").last().alias("oi"))
            .rename({"ts_ms_day_end": "ts_ms"})
            .sort(["symbol", "ts_ms"])
            .with_columns([
                (pl.col("oi") / pl.col("oi").shift(3).over("symbol") - 1.0).alias("oi_chg_3d"),
                (pl.col("oi") / pl.col("oi").shift(7).over("symbol") - 1.0).alias("oi_chg_7d"),
            ])
            .select(["ts_ms", "symbol", "oi", "oi_chg_3d", "oi_chg_7d"])
        )
        daily = daily.join(oi_daily, on=["ts_ms", "symbol"], how="left")
        daily = daily.with_columns(
            pl.when(pl.col("in_universe") & pl.col("oi_chg_7d").is_not_null())
              .then(pl.col("oi_chg_7d")).otherwise(-1e9).alias("_oi_rankable")
        ).with_columns(
            pl.col("_oi_rankable").rank(method="ordinal", descending=True).over("ts_ms").alias("oi_rank")
        )
    else:
        daily = daily.with_columns([
            pl.lit(None, dtype=pl.Float64).alias("oi_chg_3d"),
            pl.lit(None, dtype=pl.Float64).alias("oi_chg_7d"),
            pl.lit(None, dtype=pl.Int64).alias("oi_rank"),
        ])

    # positioning metrics (binance.vision): top-trader vs retail L/S ratio, taker flow, OI.
    # Build a cross-sectional factor score per config.metrics_factor, then rank in-universe.
    if metrics is not None and not metrics.is_empty() and "global_lsr" in metrics.columns:
        m = metrics.sort(["symbol", "ts_ms"]).with_columns(
            (pl.col("oi") / pl.col("oi").shift(7).over("symbol") - 1.0).alias("oi_chg7_m")
        ).select(["ts_ms", "symbol", "toptrader_lsr", "global_lsr", "taker_lsr", "oi_chg7_m"])
        daily = daily.join(m, on=["ts_ms", "symbol"], how="left")
        factor = config.metrics_factor
        if factor == "global_lsr_low":
            score = -pl.col("global_lsr")               # retail capitulated/short = contrarian long
        elif factor == "toptrader_lsr_high":
            score = pl.col("toptrader_lsr")             # smart money positioned long = follow
        elif factor == "taker_buy":
            score = pl.col("taker_lsr")                 # aggressive taker buying
        elif factor == "oi_growth":
            score = pl.col("oi_chg7_m")
        else:  # smart_minus_dumb (default): smart longer than the crowd
            score = pl.col("toptrader_lsr") - pl.col("global_lsr")
        daily = daily.with_columns(score.alias("metrics_score"))
        daily = daily.with_columns(
            pl.when(pl.col("in_universe") & pl.col("metrics_score").is_not_null())
              .then(pl.col("metrics_score")).otherwise(-1e9).alias("_m_rankable")
        ).with_columns(
            pl.col("_m_rankable").rank(method="ordinal", descending=True).over("ts_ms").alias("metrics_rank")
        )
    else:
        daily = daily.with_columns([
            pl.lit(None, dtype=pl.Float64).alias("metrics_score"),
            pl.lit(None, dtype=pl.Int64).alias("metrics_rank"),
        ])

    return daily.sort(["ts_ms", "symbol"])


def detect_pattern_capitulation(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 1: violent flush + same-day absorption."""
    if not cfg.enable_capitulation_rebound:
        return False
    if not row.get("in_universe") or not row.get("regime_on"):
        return False
    r5 = _safe_float(row.get("return_5d"))
    if r5 is None or r5 > cfg.cap_flush_min_return:
        return False
    # today's low must be the lowest of the 5-day window (flush is recent)
    today_low = _safe_float(row.get("low"))
    low_5d = _safe_float(row.get("low_5d"))
    if today_low is None or low_5d is None or today_low > low_5d * 1.001:
        return False
    cl = _safe_float(row.get("close_location"))
    if cl is None or cl < cfg.cap_min_close_location:
        return False
    today_vol = _safe_float(row.get("turnover_quote"))
    vol_avg = _safe_float(row.get("vol7d_mean"))
    if today_vol is None or vol_avg is None or vol_avg <= 0 or today_vol < cfg.cap_volume_multiple * vol_avg:
        return False
    return True


def detect_pattern_resurrection(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 2: forgotten coin re-enters top tier on green volume day."""
    if not cfg.enable_volume_resurrection:
        return False
    if not row.get("regime_on"):
        return False
    today_rank = _safe_float(row.get("today_volume_rank"))
    if today_rank is None or today_rank > cfg.res_today_rank_max:
        return False
    avg_rank_pct = _safe_float(row.get("avg_rank_pct_30d"))
    if avg_rank_pct is None or avg_rank_pct < cfg.res_prior_rank_pct_min:
        return False
    day_return = _safe_float(row.get("log_return"))
    if day_return is None or day_return < math.log1p(cfg.res_min_day_return):
        return False
    today_vol = _safe_float(row.get("turnover_quote"))
    vol_avg = _safe_float(row.get("vol7d_mean_res"))
    if today_vol is None or vol_avg is None or vol_avg <= 0 or today_vol < cfg.res_volume_multiple * vol_avg:
        return False
    return True


def detect_pattern_funding_squeeze(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 3: trailing negative funding + recent flip + green volume."""
    if not cfg.enable_funding_squeeze:
        return False
    if not row.get("in_universe") or not row.get("regime_on"):
        return False
    sumf = _safe_float(row.get("funding_lookback_sum"))
    recentf = _safe_float(row.get("funding_recent"))
    if sumf is None or recentf is None:
        return False
    if sumf > cfg.fs_cumulative_funding_max:
        return False
    if recentf < cfg.fs_recent_funding_min:
        return False
    day_return = _safe_float(row.get("log_return"))
    if day_return is None or day_return < math.log1p(cfg.fs_min_day_return):
        return False
    today_vol = _safe_float(row.get("turnover_quote"))
    vol_avg = _safe_float(row.get("vol7d_mean"))
    if today_vol is None or vol_avg is None or vol_avg <= 0 or today_vol < cfg.fs_volume_multiple * vol_avg:
        return False
    return True


def detect_pattern_oversold_bounce(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 4: deep 14-day flush + reversal day with volume."""
    if not cfg.enable_oversold_bounce:
        return False
    if not row.get("in_universe") or not row.get("regime_on"):
        return False
    r14 = _safe_float(row.get("return_14d_ob"))
    if r14 is None or r14 > cfg.ob_flush_min_return:
        return False
    today_ret = _safe_float(row.get("log_return"))
    if today_ret is None or today_ret < math.log1p(cfg.ob_today_min_return):
        return False
    today_vol = _safe_float(row.get("turnover_quote"))
    vol_avg = _safe_float(row.get("vol14d_mean"))
    if today_vol is None or vol_avg is None or vol_avg <= 0 or today_vol < cfg.ob_volume_multiple * vol_avg:
        return False
    return True


def detect_pattern_uptrend_dip(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 6: established-uptrend coin pulls back 7-15% on the day."""
    if not cfg.enable_uptrend_dip:
        return False
    if not row.get("in_universe") or not row.get("regime_on"):
        return False
    close = _safe_float(row.get("close"))
    coin_sma = _safe_float(row.get("coin_sma"))
    if close is None or coin_sma is None or close <= coin_sma:
        return False
    # today's drop in the band
    today_ret = _safe_float(row.get("log_return"))
    if today_ret is None:
        return False
    today_pct = math.expm1(today_ret)
    if today_pct < cfg.ud_today_min_drop or today_pct > cfg.ud_today_max_drop:
        return False
    r7 = _safe_float(row.get("return_7d"))
    if r7 is None or r7 < cfg.ud_7d_return_min:
        return False
    return True


def detect_pattern_fomo_chase(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 5: huge 1d pump in confirmed broad trend, chase the breakout.

    v4: also fires on 3d/7d cumulative pumps if enabled, and entry threshold can be
    sigma-relative (per-coin) instead of fixed pct. BTC regime gate is optional.
    """
    if not cfg.enable_fomo_chase:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.fc_btc_regime_required and not row.get("regime_on"):
        return False
    if cfg.fc_eth_regime_required and not row.get("eth_regime_on"):
        return False
    today_rank = _safe_float(row.get("today_volume_rank"))
    if today_rank is None or today_rank > cfg.fc_top_volume_rank_max:
        return False
    # Determine entry threshold (sigma-relative or absolute) for the 1d trigger
    today_ret = _safe_float(row.get("log_return"))
    if today_ret is None:
        return False
    sigma_d = _safe_float(row.get("sigma_daily_30d")) if cfg.fc_use_sigma_threshold else None
    if cfg.fc_use_sigma_threshold and sigma_d is not None and sigma_d > 0:
        threshold_1d = cfg.fc_sigma_mult * sigma_d
    else:
        threshold_1d = math.log1p(cfg.fc_min_day_return)
    # Check 1d trigger (with close_location)
    cl_1d = _safe_float(row.get("close_location"))
    trigger_1d = (today_ret >= threshold_1d and cl_1d is not None and cl_1d >= cfg.fc_min_close_location)
    # Check 3d trigger (sqrt(N) scaling for cumulative thresholds)
    trigger_3d = False
    if cfg.fc_enable_3d_trigger:
        p3 = _safe_float(row.get("pump_3d_log"))
        cl_3 = _safe_float(row.get("close_loc_3d"))
        if p3 is not None and cl_3 is not None and cl_3 >= cfg.fc_close_loc_multi_day:
            if cfg.fc_use_sigma_threshold and sigma_d is not None and sigma_d > 0:
                thr3 = cfg.fc_sigma_mult * sigma_d * math.sqrt(3)
            else:
                thr3 = math.log1p(cfg.fc_min_day_return) * math.sqrt(3)
            trigger_3d = (p3 >= thr3)
    # Check 7d trigger
    trigger_7d = False
    if cfg.fc_enable_7d_trigger:
        p7 = _safe_float(row.get("pump_7d_log"))
        cl_7 = _safe_float(row.get("close_loc_7d"))
        if p7 is not None and cl_7 is not None and cl_7 >= cfg.fc_close_loc_multi_day:
            if cfg.fc_use_sigma_threshold and sigma_d is not None and sigma_d > 0:
                thr7 = cfg.fc_sigma_mult * sigma_d * math.sqrt(7)
            else:
                thr7 = math.log1p(cfg.fc_min_day_return) * math.sqrt(7)
            trigger_7d = (p7 >= thr7)
    # Check intraday trigger (max N-hour pump within today's session)
    trigger_intra = False
    if cfg.fc_enable_intraday_trigger:
        pi = _safe_float(row.get("intra_max_Nh_pump_log"))
        if pi is not None:
            # σ_hourly ≈ σ_daily / sqrt(24); pump over N hours scales by sqrt(N/24) relative to daily σ
            scale = math.sqrt(cfg.fc_intraday_window_hours / 24.0)
            if cfg.fc_use_sigma_threshold and sigma_d is not None and sigma_d > 0:
                thri = cfg.fc_sigma_mult * sigma_d * scale
            else:
                thri = math.log1p(cfg.fc_min_day_return) * scale
            trigger_intra = (pi >= thri)
    # v7: per-coin own-pump-quantile trigger (today's pump > coin's own past 90d Pxx)
    trigger_own_q = False
    if cfg.fc_use_own_pump_quantile:
        own_q = _safe_float(row.get("own_pump_quantile_90d"))
        if own_q is not None and today_ret >= own_q and cl_1d is not None and cl_1d >= cfg.fc_min_close_location:
            trigger_own_q = True
    if not (trigger_1d or trigger_3d or trigger_7d or trigger_intra or trigger_own_q):
        return False
    # NEW: coin own SMA filter (own uptrend confirmation)
    if cfg.fc_coin_above_own_sma_days > 0:
        cs = _safe_float(row.get("coin_fc_sma"))
        cl_price = _safe_float(row.get("close"))
        if cs is None or cl_price is None or cl_price <= cs:
            return False
    # NEW: coin's own 30d return floor (not a bear-rally bounce)
    if cfg.fc_coin_min_30d_return > -1.0:
        r30 = _safe_float(row.get("coin_30d_return"))
        if r30 is None or r30 < cfg.fc_coin_min_30d_return:
            return False
    # NEW: BTC not near its cycle top (exclude crashing-from-tops)
    if cfg.fc_btc_not_near_high_window_days > 0:
        prox = _safe_float(row.get("btc_high_proximity"))
        if prox is not None and prox > cfg.fc_btc_high_proximity_pct:
            return False
    # NEW: REQUIRE BTC near recent highs (opposite — only trade in clear bull confirmation)
    if cfg.fc_btc_must_be_near_high_window_days > 0:
        prox = _safe_float(row.get("btc_high_proximity"))
        if prox is None or prox < cfg.fc_btc_must_be_near_high_pct:
            return False
    # FC v2 forensic filters
    # Cap ATR (high-vol coins lose more often)
    if cfg.fc_max_atr_pct < 1.0:
        atr_pct = _safe_float(row.get("atr_14d_pct"))
        if atr_pct is None or atr_pct > cfg.fc_max_atr_pct:
            return False
    # FC v7: per-coin ATR percentile guard (this coin is in unusually high vol regime for itself)
    if cfg.fc_max_atr_own_percentile < 1.0:
        atr_pct = _safe_float(row.get("atr_14d_pct"))
        atr_p = _safe_float(row.get("own_atr_quantile_90d"))
        if atr_pct is not None and atr_p is not None and atr_pct > atr_p:
            return False
    # Volume confirmation band
    if cfg.fc_min_vol_vs_median > 0.0:
        vm = _safe_float(row.get("vol_vs_30d_median"))
        if vm is None or vm < cfg.fc_min_vol_vs_median:
            return False
    if cfg.fc_max_vol_vs_median > 0.0:
        vm = _safe_float(row.get("vol_vs_30d_median"))
        if vm is None or vm > cfg.fc_max_vol_vs_median:
            return False
    # Late-cycle chase guard
    if cfg.fc_max_coin_60d_return > 0.0:
        r60 = _safe_float(row.get("coin_60d_return"))
        if r60 is None or r60 > cfg.fc_max_coin_60d_return:
            return False
    # BTC SMA-distance band (avoid both weak regime AND parabolic exhaustion)
    if cfg.fc_min_btc_sma_dist > -1.0:
        bsd = _safe_float(row.get("btc_sma_dist"))
        if bsd is None or bsd < cfg.fc_min_btc_sma_dist:
            return False
    if cfg.fc_max_btc_sma_dist < 1.0:
        bsd = _safe_float(row.get("btc_sma_dist"))
        if bsd is None or bsd > cfg.fc_max_btc_sma_dist:
            return False
    # positioning quality filter: avoid FC pumps when retail is over-long (crowd top)
    if cfg.fc_lsr_filter:
        glsr = _safe_float(row.get("global_lsr"))
        if glsr is not None and (glsr > cfg.fc_max_lsr or glsr < cfg.fc_min_lsr):
            return False
    # OI-confirmation: require open interest rising at the pump (new money, not short-cover)
    if cfg.fc_require_oi_rising:
        oichg = _safe_float(row.get("oi_chg_7d"))
        if oichg is None or oichg <= cfg.fc_oi_chg_min:
            return False
    return True


def _fc_exit_params(row: dict[str, Any], cfg: LongNativeConfig) -> tuple[float, float]:
    """Return (stop_pct, take_profit_pct) for FC. Dynamic if fc_use_atr_exits, else fixed."""
    if not cfg.fc_use_atr_exits:
        return cfg.fc_stop_pct, cfg.fc_take_profit_pct
    atr_pct = _safe_float(row.get("atr_14d_pct"))
    if atr_pct is None or atr_pct <= 0:
        # Fall back to fixed if ATR is missing
        return cfg.fc_stop_pct, cfg.fc_take_profit_pct
    return atr_pct * cfg.fc_atr_stop_mult, atr_pct * cfg.fc_atr_tp_mult


def _coin_track_record_scale(symbol: str, trade_rows: list[dict[str, Any]], cfg: LongNativeConfig) -> tuple[float, float | None]:
    """Compute position-size multiplier from this coin's past trades in this backtest.

    Returns (scale_multiplier, win_rate_or_None). Walk-forward safe — only past trades.
    """
    if not cfg.fc_use_coin_track_record:
        return 1.0, None
    past = [t for t in trade_rows if t.get("symbol") == symbol]
    if len(past) < cfg.fc_track_min_trades:
        return 1.0, None  # not enough history → neutral
    wins = sum(1 for t in past if (t.get("net_return") or 0) > 0)
    win_rate = wins / len(past)
    # Optional hard blacklist
    if cfg.fc_track_blacklist_below > 0 and win_rate < cfg.fc_track_blacklist_below:
        return 0.0, win_rate
    # Map win_rate [0.3, 0.7] linearly to [scale_min, scale_max]
    norm = max(0.0, min(1.0, (win_rate - 0.3) / 0.4))
    scale = cfg.fc_track_scale_min + norm * (cfg.fc_track_scale_max - cfg.fc_track_scale_min)
    return scale, win_rate


def _fc_alpha_score(row: dict[str, Any]) -> float:
    """Multi-feature alpha score for FC entry quality, range ~[0, 1].

    Equal-weight blend (no learned weights → no parameter mining). Each component
    is derived from the forensic-evidence quintile signals on the 351-trade dataset:

    - pump_strength : log_return / (3 × σ_daily), capped at 3σ (forensic: bigger pumps slightly better)
    - close_recency : close_location (forensic: close near day high → 71% win rate)
    - vol_confirm   : log(vol_vs_median) / log(5), capped (forensic: 3-7× median is sweet spot)
    - breakout_freshness : (pos_in_90d - 0.7) / 0.3 if pos > 0.7 else 0 (forensic: bimodal, top end works)
    - low_vol_bonus : (0.12 - atr_pct) / 0.10, capped (forensic: low-ATR coins win, high-ATR lose)
    - btc_quality   : 1 - |btc_sma_dist - 0.11| / 0.10, capped (forensic: 11-17% above SMA = best)
    """
    components = []
    # Pump magnitude in sigma terms
    logret = _safe_float(row.get("log_return")) or 0.0
    sigma_d = _safe_float(row.get("sigma_daily_30d")) or 0.05
    if sigma_d > 0:
        pump_sigma = logret / (3.0 * sigma_d)
        components.append(max(0.0, min(1.0, pump_sigma)))
    # Close location
    cl = _safe_float(row.get("close_location"))
    if cl is not None:
        components.append(max(0.0, min(1.0, cl)))
    # Volume confirmation (log scale)
    vm = _safe_float(row.get("vol_vs_30d_median"))
    if vm is not None and vm > 0:
        components.append(max(0.0, min(1.0, math.log(max(vm, 0.5)) / math.log(5.0))))
    # Breakout freshness (above 70% of 90d range)
    p90 = _safe_float(row.get("pos_in_90d_range"))
    if p90 is not None:
        components.append(max(0.0, min(1.0, (p90 - 0.7) / 0.3)))
    # Low-vol bonus (penalize high-ATR coins)
    atr_pct = _safe_float(row.get("atr_14d_pct"))
    if atr_pct is not None:
        components.append(max(0.0, min(1.0, (0.12 - atr_pct) / 0.10)))
    # BTC regime quality (peak around 11% above SMA)
    bsd = _safe_float(row.get("btc_sma_dist"))
    if bsd is not None:
        components.append(max(0.0, min(1.0, 1.0 - abs(bsd - 0.11) / 0.10)))
    if not components:
        return 0.0
    return sum(components) / len(components)


def detect_pattern_xsec_momentum(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 7: cross-sectional momentum rotation — hold the top-K in-universe
    names by trailing return. Fires continuously (every rebalance), so it lifts
    trade count rather than waiting for a rare pump event."""
    if not cfg.enable_xsec_momentum:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.xsec_require_regime and not row.get("regime_on"):
        return False
    if cfg.xsec_require_btc_200 and not row.get("btc_trend_200"):
        return False
    if cfg.xsec_min_btc_vol_pos > 0.0:
        vpos = _safe_float(row.get("btc_vol_pos"))
        if vpos is None or vpos < cfg.xsec_min_btc_vol_pos:
            return False
    rank = _safe_float(row.get("mom_rank"))
    score = _safe_float(row.get("mom_score"))
    if rank is None or score is None:
        return False
    if score <= cfg.xsec_min_momentum:
        return False
    return rank <= cfg.xsec_top_k


def detect_pattern_lowvol(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 8: hold the lowest-realized-vol in-universe names (regime-gated).
    Low-vol majors trend smoothly -> small Sharpe denominator. Fires continuously."""
    if not cfg.enable_lowvol:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.lowvol_require_regime and not row.get("regime_on"):
        return False
    lr = _safe_float(row.get("lowvol_rank"))
    if lr is None or lr > cfg.lowvol_top_k:
        return False
    if cfg.lowvol_min_mom > -1.0:
        m = _safe_float(row.get("coin_30d_return"))
        if m is None or m < cfg.lowvol_min_mom:
            return False
    return True


def detect_pattern_reversal(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 9: buy a 1-day dip inside an uptrend (short-term mean reversion).
    High win-rate / low-variance profile; the opposite bet to FOMO-chase."""
    if not cfg.enable_reversal:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.rev_require_regime and not row.get("regime_on"):
        return False
    lr = _safe_float(row.get("log_return"))
    if lr is None:
        return False
    drop = math.expm1(lr)  # simple return of the (down) signal day
    if drop > cfg.rev_drop_max or drop < cfg.rev_drop_min:
        return False
    if cfg.rev_require_uptrend:
        m = _safe_float(row.get("coin_30d_return"))
        if m is None or m <= 0.0:
            return False
    return True


def detect_pattern_funding_carry(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 10: long the most short-crowded in-universe names (most-negative
    trailing funding). Carry edge (paid to hold) + short-squeeze upside; gated to
    uptrends so it doesn't sit long in negative-funding downtrends."""
    if not cfg.enable_funding_carry:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.funding_carry_require_regime and not row.get("regime_on"):
        return False
    fr = _safe_float(row.get("funding_rank"))
    fsum = _safe_float(row.get("funding_lookback_sum"))
    if fr is None or fsum is None:
        return False
    if fsum > cfg.funding_carry_max_lookback_sum:
        return False
    if fr > cfg.funding_carry_top_k:
        return False
    if cfg.funding_carry_require_uptrend:
        m = _safe_float(row.get("coin_30d_return"))
        if m is None or m <= 0.0:
            return False
    return True


def detect_pattern_oi_momentum(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 11: long the in-universe names with the fastest-growing open
    interest in a rising price (positioning conviction). New money building
    positions = trend fuel, distinct from a hollow short-covering rally."""
    if not cfg.enable_oi_momentum:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.oi_require_regime and not row.get("regime_on"):
        return False
    orank = _safe_float(row.get("oi_rank"))
    ochg = _safe_float(row.get("oi_chg_7d"))
    if orank is None or ochg is None:
        return False
    if ochg < cfg.oi_min_oi_chg_7d:
        return False
    if orank > cfg.oi_top_k:
        return False
    if cfg.oi_require_price_up:
        m = _safe_float(row.get("coin_30d_return"))
        if m is None or m <= 0.0:
            return False
    return True


def detect_pattern_metrics(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Pattern 12: cross-sectional positioning factor (binance.vision metrics).
    Select top-K in-universe names by the configured factor (smart-vs-retail L/S
    divergence, retail contrarian, top-trader follow, taker flow, or OI growth)."""
    if not cfg.enable_metrics_signal:
        return False
    if not row.get("in_universe"):
        return False
    if cfg.metrics_require_regime and not row.get("regime_on"):
        return False
    rank = _safe_float(row.get("metrics_rank"))
    score = _safe_float(row.get("metrics_score"))
    if rank is None or score is None:
        return False
    if rank > cfg.metrics_top_k:
        return False
    if cfg.metrics_require_uptrend:
        m = _safe_float(row.get("coin_30d_return"))
        if m is None or m <= 0.0:
            return False
    return True


def _classify_entry(row: dict[str, Any], cfg: LongNativeConfig) -> tuple[str | None, float, float, int]:
    """Returns (pattern_name, stop_pct, take_profit_pct, max_hold_days) or (None, ...)."""
    if detect_pattern_capitulation(row, cfg):
        return "capitulation_rebound", cfg.cap_stop_pct, cfg.cap_take_profit_pct, cfg.cap_max_hold_days
    if detect_pattern_funding_squeeze(row, cfg):
        return "funding_squeeze", cfg.fs_stop_pct, cfg.fs_take_profit_pct, cfg.fs_max_hold_days
    if detect_pattern_oversold_bounce(row, cfg):
        return "oversold_bounce", cfg.ob_stop_pct, cfg.ob_take_profit_pct, cfg.ob_max_hold_days
    if detect_pattern_uptrend_dip(row, cfg):
        return "uptrend_dip", cfg.ud_stop_pct, cfg.ud_take_profit_pct, cfg.ud_max_hold_days
    if detect_pattern_fomo_chase(row, cfg):
        stop_pct, tp_pct = _fc_exit_params(row, cfg)
        return "fomo_chase", stop_pct, tp_pct, cfg.fc_max_hold_days
    if detect_pattern_xsec_momentum(row, cfg):
        return "xsec_momentum", cfg.xsec_stop_pct, cfg.xsec_take_profit_pct, cfg.xsec_max_hold_days
    if detect_pattern_lowvol(row, cfg):
        return "lowvol", cfg.lowvol_stop_pct, cfg.lowvol_take_profit_pct, cfg.lowvol_max_hold_days
    if detect_pattern_reversal(row, cfg):
        return "reversal", cfg.rev_stop_pct, cfg.rev_take_profit_pct, cfg.rev_max_hold_days
    if detect_pattern_funding_carry(row, cfg):
        return "funding_carry", cfg.funding_carry_stop_pct, cfg.funding_carry_take_profit_pct, cfg.funding_carry_max_hold_days
    if detect_pattern_oi_momentum(row, cfg):
        return "oi_momentum", cfg.oi_stop_pct, cfg.oi_take_profit_pct, cfg.oi_max_hold_days
    if detect_pattern_metrics(row, cfg):
        return "metrics", cfg.metrics_stop_pct, cfg.metrics_take_profit_pct, cfg.metrics_max_hold_days
    if detect_pattern_resurrection(row, cfg):
        return "volume_resurrection", cfg.res_stop_pct, cfg.res_take_profit_pct, cfg.res_max_hold_days
    return None, 0.0, 0.0, 0


def _bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, Any]]:
    required = {"ts_ms", "symbol", "open", "high", "low", "close"}
    missing = required - set(klines.columns)
    if missing:
        raise RuntimeError(f"klines missing columns: {sorted(missing)}")
    output: dict[str, dict[str, Any]] = {}
    prepared = klines.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
    for key, part in prepared.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        ends = part["bar_end_ts_ms"].to_numpy()
        output[symbol] = {
            "ends": ends.tolist(),
            "by_end": {int(e): i for i, e in enumerate(ends)},
            "bar_end_ts_ms": ends,
            "open": part["open"].to_numpy(),
            "high": part["high"].to_numpy(),
            "low": part["low"].to_numpy(),
            "close": part["close"].to_numpy(),
        }
    return output


def _filter_signal_window(features: pl.DataFrame, *, start: str, end: str) -> pl.DataFrame:
    if features.is_empty():
        return features
    if start:
        features = features.filter(pl.col("ts_ms") >= date_ms(start))
    if end:
        features = features.filter(pl.col("ts_ms") < date_ms(end))
    return features


def _load_sector_map(path: str | None) -> dict[str, str]:
    """Load the symbol→sector map. Returns empty dict if path is None.

    Bare symbol→sector JSON: {"WIFUSDT": "meme", "ETHUSDT": "core_l1", ...}.
    Missing symbols become "unknown".
    """
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_absolute():
        # Try repo-relative first, then CWD-relative.
        repo_root = Path(__file__).resolve().parent.parent
        repo_candidate = repo_root / p
        if repo_candidate.exists():
            p = repo_candidate
    if not p.exists():
        raise RuntimeError(f"sector_map_path does not exist: {p}")
    try:
        payload = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sector_map_path {p} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"sector_map_path {p} must contain a JSON object, got {type(payload).__name__}")
    out: dict[str, str] = {}
    for k, v in payload.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RuntimeError(f"sector_map_path {p}: all keys+values must be strings")
        out[k.upper()] = v
    return out


def _run_long_pipeline(
    *,
    features: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, Any]],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: LongNativeConfig,
    costs: CostConfig,
) -> tuple[pl.DataFrame, dict[str, int], dict[str, int]]:
    dates_all = sorted(int(ts) for ts in features["ts_ms"].unique().to_list())
    features_by_date: dict[int, list[dict[str, Any]]] = {}
    for part in features.partition_by("ts_ms", maintain_order=True):
        ts = int(part["ts_ms"][0])
        features_by_date[ts] = part.to_dicts()

    sector_map = _load_sector_map(config.sector_map_path) if config.max_per_sector_concurrent > 0 else {}

    open_positions: dict[str, dict[str, Any]] = {}
    cooldown_until: dict[str, int] = {}
    trade_rows: list[dict[str, Any]] = []
    stats = {
        "candidates_total": 0, "skipped_capacity": 0, "skipped_cooldown": 0,
        "skipped_already_held": 0, "skipped_no_entry_bar": 0,
        "skipped_sector_cap": 0, "skipped_symbol_cap": 0,
        "exits_stop": 0, "exits_take_profit": 0, "exits_time": 0,
    }
    event_counts = {"capitulation_rebound": 0, "funding_squeeze": 0, "volume_resurrection": 0,
                    "oversold_bounce": 0, "fomo_chase": 0, "uptrend_dip": 0, "xsec_momentum": 0,
                    "lowvol": 0, "reversal": 0, "funding_carry": 0, "oi_momentum": 0, "metrics": 0}
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier
    notional_weight = config.gross_exposure / max(config.max_concurrent_positions, 1)
    # drawdown-throttle state (realized book equity from closed trades)
    dd_realized_pnl = 0.0
    dd_peak_eq = 1.0
    dd_counted = 0
    dd_cur = 0.0

    for ts in dates_all:
        rows_today = features_by_date.get(ts, [])

        # ---- check exits for currently held positions ----
        for symbol in list(open_positions.keys()):
            pos = open_positions[symbol]
            bars = bars_by_symbol.get(symbol)
            if bars is None:
                continue
            # Iterate this day's 1h bars (24 hours) from pos["entry_ts_ms"] onwards
            day_end_ts = ts  # day_end timestamp = ts
            # Find indices in the symbol's 1h bars whose bar_end is within (yesterday_end, today_end]
            ends = bars["ends"]
            start_idx = bisect_right(ends, day_end_ts - MS_PER_DAY)
            end_idx = bisect_right(ends, day_end_ts)
            exited = False
            for idx in range(max(start_idx, pos["entry_bar_idx"] + 1), end_idx):
                bar_high = float(bars["high"][idx])
                bar_low = float(bars["low"][idx])
                bar_close = float(bars["close"][idx])
                bar_end_ts = int(bars["bar_end_ts_ms"][idx])
                entry_price = pos["entry_price"]
                # v11 exit alpha: move stop to breakeven once bar_high reaches half-TP
                if config.fc_use_scaled_exit and pos.get("pattern") == "fomo_chase" and not pos.get("be_armed", False):
                    half_tp_price = entry_price * (1.0 + pos["tp_pct"] / 2.0)
                    if bar_high >= half_tp_price:
                        if entry_price > pos["stop_price"]:
                            pos["stop_price"] = entry_price  # raise to breakeven
                        pos["be_armed"] = True
                # Update trailing stop if enabled
                if config.use_trailing_stop and pos.get("trailing_atr") is not None:
                    pos["high_water_close"] = max(pos.get("high_water_close", entry_price), bar_close)
                    trail_stop = pos["high_water_close"] - config.trailing_atr_multiple * pos["trailing_atr"]
                    if trail_stop > pos["stop_price"]:
                        pos["stop_price"] = trail_stop
                # v11 scaled-exit ATR trail (after BE move, trail by K × ATR)
                if (config.fc_use_scaled_exit and pos.get("be_armed", False)
                        and config.fc_scaled_exit_trail_atr_mult > 0
                        and pos.get("trailing_atr") is not None):
                    pos["high_water_close"] = max(pos.get("high_water_close", entry_price), bar_close)
                    trail = pos["high_water_close"] - config.fc_scaled_exit_trail_atr_mult * pos["trailing_atr"]
                    if trail > pos["stop_price"]:
                        pos["stop_price"] = trail
                # Stop check (use bar low for long)
                if bar_low <= pos["stop_price"]:
                    trade = _finalize_trade(pos, exit_ts_ms=bar_end_ts, exit_price=pos["stop_price"],
                                            reason="stop_loss", notional_weight=notional_weight,
                                            round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup,
                                            notional_multiplier=config.notional_multiplier)
                    trade_rows.append(trade)
                    cooldown_until[symbol] = bar_end_ts + config.cooldown_days * MS_PER_DAY
                    stats["exits_stop"] += 1
                    exited = True
                    break
                # Take profit check
                if bar_high >= pos["take_profit_price"]:
                    trade = _finalize_trade(pos, exit_ts_ms=bar_end_ts, exit_price=pos["take_profit_price"],
                                            reason="take_profit", notional_weight=notional_weight,
                                            round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup,
                                            notional_multiplier=config.notional_multiplier)
                    trade_rows.append(trade)
                    cooldown_until[symbol] = bar_end_ts + config.cooldown_days * MS_PER_DAY
                    stats["exits_take_profit"] += 1
                    exited = True
                    break
                # Time-stop check (if bar_end past planned_exit_ts)
                if bar_end_ts >= pos["planned_exit_ts_ms"]:
                    trade = _finalize_trade(pos, exit_ts_ms=bar_end_ts, exit_price=bar_close,
                                            reason="time_stop", notional_weight=notional_weight,
                                            round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup,
                                            notional_multiplier=config.notional_multiplier)
                    trade_rows.append(trade)
                    cooldown_until[symbol] = bar_end_ts + config.cooldown_days * MS_PER_DAY
                    stats["exits_time"] += 1
                    exited = True
                    break
            if exited:
                del open_positions[symbol]

        # update realized-equity drawdown (from trades closed so far) for the throttle
        if config.enable_dd_throttle:
            for t in trade_rows[dd_counted:]:
                dd_realized_pnl += float(t["net_return"])
            dd_counted = len(trade_rows)
            dd_peak_eq = max(dd_peak_eq, 1.0 + dd_realized_pnl)
            dd_cur = (1.0 + dd_realized_pnl) / dd_peak_eq - 1.0

        # ---- check entries ----
        candidates = []
        for row in rows_today:
            pattern, stop_pct, tp_pct, hold_days = _classify_entry(row, config)
            if pattern is None:
                continue
            # FC v6: alpha score (only for FC entries; other patterns get neutral 1.0)
            if pattern == "fomo_chase" and (config.fc_use_alpha_score or config.fc_min_score > 0
                                            or config.fc_score_size_scaling or config.fc_rank_by_score):
                score = _fc_alpha_score(row)
                if score < config.fc_min_score:
                    continue
            else:
                score = 1.0
            stats["candidates_total"] += 1
            event_counts[pattern] += 1
            candidates.append((row, pattern, stop_pct, tp_pct, hold_days, score))

        # Rank candidates: by score if v6 enabled, else by raw 24h log return
        if config.fc_rank_by_score:
            candidates.sort(key=lambda c: -c[5])
        else:
            candidates.sort(key=lambda c: -(_safe_float(c[0].get("log_return")) or 0.0))
        if config.metrics_fill_only:
            # FC-priority: non-metrics patterns take slots first (stable sort keeps
            # their relative order); metrics candidates only fill leftover capacity.
            candidates.sort(key=lambda c: 1 if c[1] == "metrics" else 0)
        # FC v12: daily-best-N filter
        if config.fc_daily_best_n > 0:
            fc_kept = []
            kept_fc = 0
            for cand in candidates:
                if cand[1] == "fomo_chase":
                    if kept_fc < config.fc_daily_best_n:
                        fc_kept.append(cand)
                        kept_fc += 1
                else:
                    fc_kept.append(cand)
            candidates = fc_kept
        for row, pattern, stop_pct, tp_pct, hold_days, score in candidates:
            symbol = str(row["symbol"])
            if symbol in open_positions:
                stats["skipped_already_held"] += 1
                continue
            # B.3: explicit per-symbol concurrent cap (orthogonal to the
            # "skipped_already_held" gate above, which only covers exact-symbol
            # re-entry; the symbol cap is meant for the future case where the
            # universe allows multiple positions in correlated derivatives of
            # the same symbol, e.g. perp + dated future).
            if config.max_per_symbol_concurrent > 0:
                held_same_symbol = sum(1 for p in open_positions.values() if p["symbol"] == symbol)
                if held_same_symbol >= config.max_per_symbol_concurrent:
                    stats["skipped_symbol_cap"] += 1
                    continue
            # B.3: per-sector concurrent cap (disabled when 0).
            if config.max_per_sector_concurrent > 0 and sector_map:
                cand_sector = sector_map.get(symbol, "unknown")
                held_in_sector = sum(
                    1 for p in open_positions.values()
                    if sector_map.get(p["symbol"], "unknown") == cand_sector
                )
                if held_in_sector >= config.max_per_sector_concurrent:
                    stats["skipped_sector_cap"] += 1
                    continue
            if cooldown_until.get(symbol, 0) > ts:
                stats["skipped_cooldown"] += 1
                continue
            if len(open_positions) >= config.max_concurrent_positions:
                stats["skipped_capacity"] += 1
                continue
            entry_ts_ms = ts + config.entry_delay_hours * MS_PER_HOUR
            bars = bars_by_symbol.get(symbol)
            entry_idx = bars["by_end"].get(entry_ts_ms) if bars else None
            if entry_idx is None or bars is None:
                stats["skipped_no_entry_bar"] += 1
                continue
            # v11: sniper retrace entry — wait for price to retrace N% below signal close
            if config.fc_use_sniper_entry and pattern == "fomo_chase":
                # signal_close = close of signal bar (ts is signal close ts ms)
                signal_idx = bars["by_end"].get(ts)
                if signal_idx is None:
                    stats["skipped_no_entry_bar"] += 1
                    continue
                signal_close = float(bars["close"][signal_idx])
                # v13: adaptive retrace based on coin's own ATR
                if config.fc_sniper_use_atr_retrace:
                    atr_pct = _safe_float(row.get("atr_14d_pct")) or 0.05
                    retrace_pct = config.fc_sniper_atr_mult * atr_pct
                else:
                    retrace_pct = config.fc_sniper_retrace_pct
                retrace_threshold = signal_close * (1.0 - retrace_pct)
                first_h = max(1, config.entry_delay_hours)
                deadline_h = max(first_h, config.fc_sniper_deadline_hours)
                fired = False
                for hour in range(first_h, deadline_h + 1):
                    cand_ts = ts + hour * MS_PER_HOUR
                    cand_idx = bars["by_end"].get(cand_ts)
                    if cand_idx is None:
                        continue
                    if float(bars["close"][cand_idx]) <= retrace_threshold:
                        entry_ts_ms = cand_ts
                        entry_idx = cand_idx
                        fired = True
                        break
                if not fired:
                    if config.fc_sniper_skip_on_no_retrace:
                        stats["skipped_no_entry_bar"] += 1
                        continue
                    # Else fall through: use deadline entry (or original entry_ts_ms if deadline missing)
                    deadline_ts = ts + deadline_h * MS_PER_HOUR
                    deadline_idx = bars["by_end"].get(deadline_ts)
                    if deadline_idx is not None:
                        entry_ts_ms = deadline_ts
                        entry_idx = deadline_idx
            entry_price = float(bars["close"][entry_idx])
            if not math.isfinite(entry_price) or entry_price <= 0.0:
                stats["skipped_no_entry_bar"] += 1
                continue
            stop_price = entry_price * (1.0 - stop_pct)
            take_profit_price = entry_price * (1.0 + tp_pct)
            planned_exit_ts_ms = entry_ts_ms + hold_days * MS_PER_DAY

            # Position weight via inverse vol
            vol_est = _safe_float(row.get("realized_vol")) or config.vol_floor_annual
            vol_used = max(vol_est, config.vol_floor_annual)
            position_weight = min(config.vol_floor_annual / vol_used, config.max_position_weight / notional_weight)
            position_weight = max(position_weight, 0.25)
            # FC v6: scale position weight by alpha score (full at score=1.0, half at score=0.5)
            if config.fc_score_size_scaling and pattern == "fomo_chase":
                position_weight = position_weight * max(0.3, min(1.0, score))
            # FC v9: scale by coin's in-backtest track record (walk-forward per-coin learning)
            if config.fc_use_coin_track_record and pattern == "fomo_chase":
                tr_scale, tr_wr = _coin_track_record_scale(symbol, trade_rows, config)
                if tr_scale == 0.0:
                    stats["skipped_already_held"] += 1  # repurpose counter for blacklist skips
                    continue
                position_weight = position_weight * tr_scale

            # B.3: per-symbol weight cap on gross exposure. notional_weight is
            # the slot-fraction (gross_exposure / max_concurrent_positions);
            # the effective gross share is notional_weight * position_weight.
            # Cap that at max_per_symbol_weight by reducing position_weight.
            if config.max_per_symbol_weight > 0.0:
                effective_gross = notional_weight * position_weight
                if effective_gross > config.max_per_symbol_weight:
                    position_weight = config.max_per_symbol_weight / max(notional_weight, 1e-12)

            # Volatility-managed book scalar (applied last so it's a clean book-level tilt):
            # size inversely to BTC realized vol, clipped. PIT-safe (btc_rv_30 is trailing).
            if config.enable_vol_target:
                btc_rv = _safe_float(row.get("btc_rv_30")) or config.vol_target_annual
                vt_scale = config.vol_target_annual / max(btc_rv, 1e-6)
                vt_scale = max(config.vol_target_min_scale, min(config.vol_target_max_scale, vt_scale))
                position_weight = position_weight * vt_scale

            # Drawdown throttle: de-lever while the book is in its own realized drawdown
            # (caps the worst peak-to-trough -> lifts MAR), restore on recovery.
            if config.enable_dd_throttle and dd_cur <= config.dd_throttle_trigger:
                position_weight = position_weight * config.dd_throttle_scale

            trailing_atr = _safe_float(row.get("atr_20d"))
            open_positions[symbol] = {
                "symbol": symbol,
                "pattern": pattern,
                "entry_signal_ts_ms": int(ts),
                "entry_ts_ms": int(entry_ts_ms),
                "entry_bar_idx": int(entry_idx),
                "entry_price": float(entry_price),
                "stop_price": float(stop_price),
                "take_profit_price": float(take_profit_price),
                "planned_exit_ts_ms": int(planned_exit_ts_ms),
                "position_weight": float(position_weight),
                "stop_pct": float(stop_pct),
                "tp_pct": float(tp_pct),
                "max_hold_days": int(hold_days),
                "basket_id": f"native-{_iso_date(int(ts))}-{symbol}",
                "high_water_close": float(entry_price),
                "trailing_atr": trailing_atr,
            }

    # Force-close at end
    if open_positions:
        for symbol, pos in list(open_positions.items()):
            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars["close"]) == 0:
                continue
            exit_price = float(bars["close"][-1])
            exit_ts = int(bars["bar_end_ts_ms"][-1])
            trade = _finalize_trade(pos, exit_ts_ms=exit_ts, exit_price=exit_price,
                                    reason="data_end", notional_weight=notional_weight,
                                    round_trip_cost_bps=round_trip_cost_bps, funding_lookup=funding_lookup,
                                    notional_multiplier=config.notional_multiplier)
            trade_rows.append(trade)
            stats["exits_time"] += 1

    trades = (
        pl.DataFrame(trade_rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"])
        if trade_rows else _empty_trades()
    )
    return trades, stats, event_counts


def _finalize_trade(pos, *, exit_ts_ms, exit_price, reason, notional_weight, round_trip_cost_bps, funding_lookup, notional_multiplier=1.0):
    side = "long"
    entry_price = float(pos["entry_price"])
    gross_trade_return = (exit_price / entry_price) - 1.0
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup, symbol=pos["symbol"], side=side,
        entry_ts_ms=int(pos["entry_ts_ms"]), exit_ts_ms=int(exit_ts_ms),
    )
    # H1: scale the per-position gross by the deployed notional multiplier
    # (applied AFTER the B.3 per-symbol cap, matching the live semantics).
    # Default 1.0 leaves the historical backtest gross unchanged.
    effective_weight = abs(notional_weight * float(pos["position_weight"])) * notional_multiplier
    funding_return = effective_weight * raw_funding_return
    cost_return = -effective_weight * round_trip_cost_bps / 10_000.0
    gross_return = effective_weight * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    return {
        "trade_id": f"{pos['basket_id']}-l-{pos['symbol']}",
        "basket_id": pos["basket_id"],
        "entry_signal_ts_ms": int(pos["entry_signal_ts_ms"]),
        "entry_ts_ms": int(pos["entry_ts_ms"]),
        "exit_ts_ms": int(exit_ts_ms),
        "entry_date": _iso_date(int(pos["entry_ts_ms"])),
        "exit_date": _iso_date(int(exit_ts_ms)),
        "exit_month": _iso_month(int(exit_ts_ms)),
        "symbol": pos["symbol"], "side": side, "score": 0.0, "rank": 0,
        "entry_price": entry_price, "exit_price": float(exit_price),
        "exit_reason": reason, "planned_exit_ts_ms": int(pos["planned_exit_ts_ms"]),
        "stop_price": float(pos["stop_price"]), "take_profit_price": float(pos["take_profit_price"]),
        "notional_weight": effective_weight, "position_weight": float(pos["position_weight"]),
        "gross_trade_return": gross_trade_return, "gross_return": gross_return,
        "cost_return": cost_return, "funding_return": funding_return,
        "funding_mode": funding_mode, "funding_event_count": int(funding_event_count),
        # The long sleeve does not track intra-hold price path, so MAE/MFE are
        # NOT measured here. Emit NaN (not 0.0 — a fabricated zero reads as "no
        # adverse excursion ever" and silently zeroes the H2 intra-hold MAE
        # diagnostic; the consumer drops non-finite values as not-measured).
        "net_return": net_return, "mae": float("nan"), "mfe": float("nan"),
        "bars_held": int(round((int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR)),
        "hold_hours": (int(exit_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR,
        "actual_entry_delay_hours": (int(pos["entry_ts_ms"]) - int(pos["entry_signal_ts_ms"])) / MS_PER_HOUR,
        "pattern": pos["pattern"],
    }


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame({
        "trade_id": pl.Series([], dtype=pl.String), "basket_id": pl.Series([], dtype=pl.String),
        "entry_signal_ts_ms": pl.Series([], dtype=pl.Int64), "entry_ts_ms": pl.Series([], dtype=pl.Int64),
        "exit_ts_ms": pl.Series([], dtype=pl.Int64), "entry_date": pl.Series([], dtype=pl.String),
        "exit_date": pl.Series([], dtype=pl.String), "exit_month": pl.Series([], dtype=pl.String),
        "symbol": pl.Series([], dtype=pl.String), "side": pl.Series([], dtype=pl.String),
        "score": pl.Series([], dtype=pl.Float64), "rank": pl.Series([], dtype=pl.Int64),
        "entry_price": pl.Series([], dtype=pl.Float64), "exit_price": pl.Series([], dtype=pl.Float64),
        "exit_reason": pl.Series([], dtype=pl.String), "planned_exit_ts_ms": pl.Series([], dtype=pl.Int64),
        "stop_price": pl.Series([], dtype=pl.Float64), "take_profit_price": pl.Series([], dtype=pl.Float64),
        "notional_weight": pl.Series([], dtype=pl.Float64), "position_weight": pl.Series([], dtype=pl.Float64),
        "gross_trade_return": pl.Series([], dtype=pl.Float64), "gross_return": pl.Series([], dtype=pl.Float64),
        "cost_return": pl.Series([], dtype=pl.Float64), "funding_return": pl.Series([], dtype=pl.Float64),
        "funding_mode": pl.Series([], dtype=pl.String), "funding_event_count": pl.Series([], dtype=pl.Int64),
        "net_return": pl.Series([], dtype=pl.Float64), "mae": pl.Series([], dtype=pl.Float64),
        "mfe": pl.Series([], dtype=pl.Float64), "bars_held": pl.Series([], dtype=pl.Int64),
        "hold_hours": pl.Series([], dtype=pl.Float64), "actual_entry_delay_hours": pl.Series([], dtype=pl.Float64),
        "pattern": pl.Series([], dtype=pl.String),
    })


def _split_rows(
    baskets: pl.DataFrame,
    *,
    config: TradeLifecycleConfig,
    splits: tuple[tuple[str, str, str], ...] = (),
) -> list[dict[str, Any]]:
    from .trade_lifecycle import _daily_sharpe
    rows = []
    for name, start, end in splits:
        start_ms = date_ms(start)
        end_ms = date_ms(end)
        part = (baskets.filter((pl.col("entry_signal_ts_ms") >= start_ms) & (pl.col("entry_signal_ts_ms") < end_ms))
                if not baskets.is_empty() else baskets)
        if part.is_empty():
            rows.append({
                "name": name,
                "basket_count": 0,
                "total_return": 0.0,
                "sharpe_like": 0.0,
                "max_drawdown": 0.0,
            })
            continue
        returns = np.asarray(part.sort("entry_signal_ts_ms")["basket_return"].to_list(), dtype=float)
        equity_curve = np.cumprod(1.0 + returns)
        peaks = np.maximum.accumulate(equity_curve)
        drawdowns = equity_curve / peaks - 1.0
        rows.append({
            "name": name,
            "basket_count": int(returns.size),
            "total_return": float(equity_curve[-1] - 1.0),
            "sharpe_like": _daily_sharpe(build_equity_curve(part)),
            "max_drawdown": float(drawdowns.min()),
        })
    return rows


SHARPE_PROMOTION_THRESHOLD = 0.7  # Daily-aligned honest Sharpe; whole-period gate.


def _evaluate_promotion(*, splits, summary, funding_mode, full_pit_universe_pass) -> dict[str, Any]:
    """Whole-period promotion gate. Splits are diagnostic only.

    Pristine OOS lives on the forward demo/paper ledger, not in any internal
    backtest split. The gate evaluates whole-period honest Sharpe, max DD and
    funding-mode against the universe-pass precondition. When splits are
    supplied, an additional all-positive check fires, but the threshold logic
    remains whole-period.
    """
    whole_sharpe = float(summary.get("sharpe_like", 0.0))
    max_dd = float(summary.get("max_drawdown", 0.0))
    funding_ok = funding_mode != "missing"
    splits_with_baskets = [r for r in splits if r["basket_count"] > 0]
    all_pos = bool(splits_with_baskets) and all(r["total_return"] > 0 for r in splits_with_baskets)

    reasons: list[str] = []
    if not full_pit_universe_pass:
        reasons.append("full_pit_universe_missing")
    if max_dd < -0.30:
        reasons.append("max_drawdown_too_deep")
    if whole_sharpe < SHARPE_PROMOTION_THRESHOLD:
        reasons.append("whole_period_sharpe_below_threshold")
    if not funding_ok:
        reasons.append("funding_missing")
    if splits_with_baskets and not all_pos:
        reasons.append("not_all_splits_positive")

    gate_pass = bool(
        full_pit_universe_pass
        and max_dd >= -0.30
        and whole_sharpe >= SHARPE_PROMOTION_THRESHOLD
        and funding_ok
        and (not splits_with_baskets or all_pos)
    )
    return {
        "promotion_gate_pass": gate_pass,
        "whole_period_sharpe": whole_sharpe,
        "max_drawdown": max_dd,
        "funding_mode": funding_mode,
        "sharpe_threshold": SHARPE_PROMOTION_THRESHOLD,
        "splits_with_baskets": len(splits_with_baskets),
        "all_splits_positive": all_pos,
        "promotion_reasons": reasons,
    }


def _monthly_returns(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame({"month": pl.Series([], dtype=pl.String), "strategy_return": pl.Series([], dtype=pl.Float64), "baskets": pl.Series([], dtype=pl.Int64)})
    return (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg([((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
              pl.col("long_return").sum().alias("long_return"),
              pl.col("cost_return").sum().alias("cost_return"),
              pl.col("funding_return").sum().alias("funding_return"),
              pl.len().alias("baskets")])
        .sort("month")
    )


def _run_label(*, full_pit_universe_pass: bool, funding_mode: str, archive_manifest_empty: bool) -> str:
    if archive_manifest_empty:
        return "pit_required_missing_manifest"
    if not full_pit_universe_pass:
        return "pit_membership_filtered_current_universe"
    if funding_mode == "missing":
        return "full_pit_universe_funding_missing"
    if funding_mode == "partial":
        return "full_pit_universe_funding_partial"
    return "full_pit_universe"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def format_long_native_report(metadata: dict[str, Any]) -> str:
    cfg = metadata.get("config", {})
    summary = metadata.get("summary", {})
    promo = metadata.get("promotion", {})
    splits = metadata.get("splits", [])
    lifecycle = metadata.get("lifecycle", {})
    event_counts = metadata.get("event_counts", {})
    pit = metadata.get("pit_manifest", {})
    date_range = metadata.get("date_range", {})
    lines = [
        "# Long-Native Long-Only Sleeve",
        "",
        "Crypto-native long-only strategy. Three asymmetric setups: CAPITULATION_REBOUND, FUNDING_SQUEEZE, VOLUME_RESURRECTION. NOT derived from academic papers.",
        "",
        "## Inputs",
        f"- Run label: `{metadata.get('run_label')}`",
        f"- Date range: {date_range.get('start')} to {date_range.get('end')}",
        f"- Feature rows: {metadata.get('rows', {}).get('features', 0)}",
        f"- Trades: {metadata.get('rows', {}).get('trades', 0)}",
        f"- Universe: top {cfg.get('universe_size')} by {cfg.get('universe_volume_window_days')}d turnover",
        f"- BTC regime SMA: {cfg.get('regime_sma_days')}d  (regime gate active: {cfg.get('regime_sma_days', 0) > 0})",
        f"- Patterns enabled: cap={cfg.get('enable_capitulation_rebound')}  fs={cfg.get('enable_funding_squeeze')}  res={cfg.get('enable_volume_resurrection')}",
        f"- Max concurrent: {cfg.get('max_concurrent_positions')}  Cooldown: {cfg.get('cooldown_days')}d",
        f"- Cost multiplier: {cfg.get('cost_multiplier')}x",
        f"- Full PIT: {pit.get('full_pit_universe_pass', False)}",
        "",
        "## Event counts (signals fired before capacity/cooldown gates)",
        f"- capitulation_rebound: {event_counts.get('capitulation_rebound', 0)}",
        f"- funding_squeeze: {event_counts.get('funding_squeeze', 0)}",
        f"- volume_resurrection: {event_counts.get('volume_resurrection', 0)}",
        f"- TOTAL signal-firings: {lifecycle.get('candidates_total', 0)}",
        f"- Skipped: capacity={lifecycle.get('skipped_capacity', 0)}, cooldown={lifecycle.get('skipped_cooldown', 0)}, held={lifecycle.get('skipped_already_held', 0)}, no_entry_bar={lifecycle.get('skipped_no_entry_bar', 0)}",
        "",
        "## Headline metrics",
        f"- Total return: {pct(summary.get('total_return'))}",
        f"- Sharpe-like: {summary.get('sharpe_like', 0.0):.2f}",
        f"- Max drawdown: {pct(summary.get('max_drawdown'))}",
        f"- Max underwater days: {summary.get('max_underwater_days', 0)}",
        f"- Worst 30d/60d/90d/120d: {pct(summary.get('worst_30d_return'))} / {pct(summary.get('worst_60d_return'))} / {pct(summary.get('worst_90d_return'))} / {pct(summary.get('worst_120d_return'))}",
        f"- Trade win rate: {pct(summary.get('trade_win_rate'))}",
        f"- Profit factor: {summary.get('profit_factor', 0.0):.2f}",
        f"- Gross: {pct(summary.get('gross_return'))} | cost: {pct(summary.get('cost_return'))} | funding: {pct(summary.get('funding_return'))} ({summary.get('funding_mode')})",
        "",
        "## Exits",
        f"- stop_loss: {lifecycle.get('exits_stop', 0)}",
        f"- take_profit: {lifecycle.get('exits_take_profit', 0)}",
        f"- time/data_end: {lifecycle.get('exits_time', 0)}",
        "",
        "## Promotion gate",
        f"- Pass: **{promo.get('promotion_gate_pass', False)}**",
        f"- All splits positive: {promo.get('all_splits_positive', False)} ({promo.get('splits_with_baskets', 0)}/{len(splits)})",
        f"- Avg split sharpe: {promo.get('avg_split_sharpe', 0.0):.2f}",
        f"- Block reasons: {', '.join(promo.get('promotion_reasons', [])) or 'none'}",
        "",
    ]
    if splits:
        lines += [
            "## Splits",
            "| Split | Baskets | Return | Sharpe | Max DD |",
            "|---|---:|---:|---:|---:|",
        ]
        for r in splits:
            lines.append(f"| {r.get('name')} | {r.get('basket_count', 0)} | {pct(r.get('total_return'))} | {r.get('sharpe_like', 0.0):.2f} | {pct(r.get('max_drawdown'))} |")
        lines.append("")
    else:
        lines += [
            "## Splits",
            "_None configured — whole-period reporting only. Pristine OOS = forward demo/paper._",
            "",
        ]
    return "\n".join(lines)
