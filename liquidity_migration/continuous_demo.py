"""Live CONTINUOUS-fade demo sleeve — signal + selection (the 'no 1h' core).

A SEPARATE demo-account sleeve (own ledger root `data/bybit-continuous-demo-event`, own
`lm-en-c-`/`lm-ux-c-` orderLinkId prefix) that forward-tests the continuous liquidity-migration
fade the backtest engine (`continuous_events.py`) measured. The backtest is the prior; the forward
demo is the only thing that can settle the open questions the audit flagged (OOS persistence,
borrow/short-availability, sub-hourly squeezes).

**No 1h.** The rolling features (rv_168h, vov, dist_low, xsret7/3) are inherently built from trailing
hourly closes, but the SIGNAL is recomputed continuously off the live ticker price as the in-progress
("current") bar — so a name entering/leaving the top fade decile is acted on the moment the live price
crosses, NOT on the hourly bar close. The decile pipeline is the SHARED, verified
`continuous_events.compute_continuous_decile_panel`, so the live signal == the backtest signal.

Live state-exit rule (no spell bookkeeping needed): hold a short iff the name is currently in the top
decile (D9). Enter when it is in D9 + liquid + not already held + capacity; exit when it leaves D9
(or hits its resting stop / max-hold). EXPLORATORY/forward-demo only; never real money.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, cast

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .config import DEFAULT_EXCLUDED_SYMBOLS
from .continuous_events import (
    FEATURES,
    compute_continuous_decile_panel,
    cross_sectional_decile,
    _btc_trend_returns,
    _entry_event_expr,
    per_symbol_timeseries_features,
)

CONTINUOUS_STRATEGY_ID = "continuous_fade_v1"
CONTINUOUS_ADDON_STRATEGY_ID = "continuous_fade_addon_v1"
CONTINUOUS_ENTRY_LINK_PREFIX = "en-c"   # lm-en-c-{base}-{ts36}  (5-part; distinct sleeve for ws_risk routing)
CONTINUOUS_EXIT_LINK_PREFIX = "ux-c"    # lm-ux-c-{base}-{ts36}
CONTINUOUS_ADDON_ENTRY_LINK_PREFIX = "en-ca"
CONTINUOUS_ADDON_EXIT_LINK_PREFIX = "ux-ca"
CONTINUOUS_DEMO_PROFILES = (
    "continuous_v1",
    "continuous_addon_v1",
    "continuous_rebalance_v1",  # DEPRECATED 2026-06-10: single-component turn4_pop4 (kept resolvable for old ledgers)
    "continuous_ensemble_v1",   # the validated winner_base 4-component ensemble (the live default)
)


@dataclass(frozen=True, slots=True)
class ContinuousDemoCycleConfig:
    """Live continuous-fade demo config (mirrors LongNativeDemoCycleConfig; sleeve-specific knobs)."""

    # --- selection (must match the backtest engine's promoted/validated cell) ---
    decile: int = 9                       # short the top composite decile
    # rmom-LOW gate: keep the lowest-residual-momentum THIRD within each ts (0.33). Tightened from the
    # 0.50 half by the 2026-06-02 alpha sweep (operator-directed) — the one robust cross-venue strategy
    # improvement found: engine MAR bybit 38.6→42.9 / binance 30.4→50.1, DD↓ both, neighbours hold; same
    # validated rmom squeeze-filter used tighter (NOT a new signal). ~−23% return = MAR-primary win.
    # docs/preregistration/alpha-sweep-2026-06-02.md. EXPLORATORY — forward demo is the arbiter.
    rmom_quantile: float = 0.33
    feature_set: tuple[str, ...] = FEATURES
    liq_turnover_min: float = 500_000.0   # liquid gate: signal-bar hourly turnover_quote (USD)
    side: str = "short"
    # --- live state window ---
    lookback_days: int = 45               # confirmed-1h history pulled from the store for the features
    # --- execution / book ---
    max_active: int = 25
    max_new_entries_per_cycle: int = 5
    max_hold_hours: int = 48              # force-exit cap if a name never leaves the decile
    # ENTRY TIMING (the alpha-sweep #1 finding, 2026-06-02): select ENTRIES from the CONFIRMED bar-close
    # decile + this many hours' delay, NOT the live intra-hour decile cross. The engine validated the
    # +1h point (d1) and it ~DOUBLES MAR vs intra-hour entry (d0) cross-venue — shorting intra-hour enters
    # into the first-hour continuation/squeeze. 1 = the validated +1h entry (default). 0 = legacy intra-hour
    # ("no-1h") entry off the live decile. EXITS stay tick-driven regardless (squeeze protection is good).
    # docs/preregistration/alpha-sweep-2026-06-02.md. EXPLORATORY — forward demo is the arbiter.
    entry_confirm_delay_hours: int = 1
    entry_event_trigger: str = "none"      # opt-in confirmed-hour event gate: none | fresh_pop25 | popX_gbY | ...
    btc_trend_gate: str = "off"            # off | uptrend | downtrend; causal prior-30d BTC return gate.
    # --- anti-thrash (a name oscillating on the D9 boundary would otherwise churn fees) ---
    # Hysteresis: ENTER on the top decile (`decile`), but only cover on the state-exit ("left
    # decile") once the name is CLEARLY out — its decile has dropped below `decile - exit_decile_buffer`.
    # buffer=0 reproduces the original exit-the-instant-it-leaves-D9 behaviour; buffer=1 (default)
    # holds through a one-decile wobble (D8) and covers at D7 or below.
    exit_decile_buffer: int = 1
    # Re-entry cooldown: after covering a name, do not re-open it for this many minutes even if it
    # is still/again in D9 — stops a boundary name from being re-shorted seconds after an exit.
    reentry_cooldown_minutes: int = 30
    # One signal window should normally produce at most one entry per symbol. The reentry_seq/id
    # machinery remains for backwards compatibility and explicit experiments, but the default aligns
    # live selection with the historical fresh-spell model and prevents cover-then-reopen churn inside
    # the same confirmed deciding bar.
    allow_same_signal_reentry: bool = False
    # Proactive stop-approach cover (tick-driven safety, NOT a new selection signal): cover a short
    # once its live loss reaches this fraction of the way to the server-side disaster stop
    # (loss >= stop_approach_frac * stop_loss_pct). Reacts on a ticker tick instead of waiting for
    # Bybit's market stop to fill into the gap. 0 disables (rely solely on the 0.25 server stop).
    stop_approach_frac: float = 0.8
    # Portfolio circuit breaker (correlated-squeeze defense): PAUSE new entries when the sleeve has had
    # >= entry_pause_after_adverse_exits adverse covers (stop_approach / failed_fade / any net-negative
    # cover — the footprint of a market-wide alt melt-up squeezing many shorts at once) within the last
    # entry_pause_window_minutes. Stateless (recomputed from the ledger each cycle), so the pause lifts
    # automatically as the cluster ages out of the window. NEVER adds risk — it only stops opening fresh
    # shorts into a squeeze. ENABLED at the engine-tested w24/n8 (8 adverse covers in 1440min=24h) by
    # operator direction 2026-06-02 as protective TAIL INSURANCE, NOT a validated MAR improvement: the
    # cb1 sweep (docs/preregistration/cb1-circuit-breaker-2026-06-02.md) showed it cuts drawdown but
    # costs ~20% of bybit return in-sample and n8 was a fragile cell — it is a deliberate de-risking
    # choice. Set entry_pause_after_adverse_exits=0 to disable.
    entry_pause_after_adverse_exits: int = 8
    entry_pause_window_minutes: int = 1440
    # DISASTER STOP (server-side, Bybit-managed via the entry order's stopLoss; fires even if the daemon
    # is down). Wide on purpose: a tight stop whipsaws the normal fade wiggle, but a live unstopped alt
    # short has unbounded squeeze risk and "leave-the-decile" is a PROFIT exit a squeezing name never
    # triggers. 0.25 = the operator's I-phase cap; the state exit + max-hold stay the primary exits.
    stop_loss_pct: float = 0.25
    # --- inherited-from-daily exits/gate (validated cross-venue beneficial in the engine ablation,
    # docs/continuous_sleeve_inheritance.md). Checked each cycle off live price + kline-reconstructed MFE. ---
    failed_fade_hours: int = 6            # cut a fade that hasn't worked after N hours (the daily's ff6)
    failed_fade_loss_pct: float = 0.04    # ...if down more than this
    failed_fade_min_mfe_pct: float = 0.01 # ...and never reached this favorable excursion
    breakeven_arm_pct: float = 0.10       # once MFE>=this, cover if price returns to entry (protect the winner)
    age_days_min: int = 30                # skip fresh-listing squeezers (mild floor; engine-neutral, cheap insurance)
    entry_leverage: float = 2.0
    per_position_notional_pct_equity: float = 2.0   # 2% per name (matches the proxy 2% weight)
    notional_multiplier: float = 1.0                # read by the shared daemon scaffolding (logging only)
    wallet_balance_fraction: float = 1.0
    fallback_equity_usdt: float = 10_000.0
    workers: int = 8
    # universe: 0/0 == match-the-backtest (full ~750-perp universe; the signal's liquid gate filters)
    universe_rank_end: int = 0
    universe_max_symbols: int = 0
    universe_min_turnover_24h: float = 0.0
    entry_order_type: str = "Market"
    exit_order_type: str = "Market"
    order_fill_confirm_seconds: float = 2.0
    order_fill_poll_interval_seconds: float = 0.2
    order_fill_fast_poll_interval_seconds: float = 0.05
    order_fill_fast_poll_seconds: float = 0.5
    # --- flags / wiring ---
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    record_dry_run: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "continuous-demo-event"
    # long-sleeve-5/-6: shared cross-sleeve control root (owned by ws_risk; lives in the
    # short/authority root). None => auto-resolve from this sleeve's root (a safe no-op,
    # since ws_risk writes the control row only to the authority root); set the short root
    # in deploy to make continuous consult it. Read-only, fail-open, NO-OP until seeded.
    cross_sleeve_account_root: str | None = None
    strategy_profile: str = "continuous_v1"
    paper_mode: bool = False
    # --- research-stage dual-sleeve adapter ---
    # OFF by default. When this runner is used as a paper/demo add-on sleeve,
    # consult the primary continuous ledger and skip same-symbol add-ons whose
    # active primary fade is underwater. This mirrors the accepted Binance
    # risk-mode research rule without changing the default single-sleeve path.
    addon_primary_pnl_gate: bool = False
    addon_primary_min_unrealized_return: float = 0.0
    addon_primary_data_root: str | None = None
    addon_primary_strategy_id: str = CONTINUOUS_STRATEGY_ID
    # Default-off add-on churn guard. The existing reentry_cooldown_minutes starts
    # after an exit; this starts after any add-on entry so a fast close or distinct
    # signal window cannot immediately re-hit the same symbol.
    addon_same_symbol_entry_cooldown_minutes: int = 0
    # --- research-stage daily-rebalance adapter ---
    # Default OFF. Submitted demo resizes route through the venue client and
    # mutate the ledger only after confirmed fills.
    daily_rebalance_enabled: bool = False
    daily_rebalance_realized_vol_window_days: int = 90
    daily_rebalance_target_daily_vol: float = 0.025
    daily_rebalance_max_scale: float = 4.0
    daily_rebalance_drawdown_half_threshold: float = -0.04
    daily_rebalance_resize_cost_bps: float = 10.0
    daily_rebalance_strategy_momentum_window_days: int = 180
    daily_rebalance_strategy_momentum_min_return: float = 0.02
    daily_rebalance_strategy_momentum_scale_when_below: float = 0.0
    # --- sniper add-on (S1, Tier-2 demo candidate Amendment 6, 2026-06-09) ---
    # On each fresh short entry, ALSO rest a Sell limit ABOVE entry at +sniper_wick_pct
    # for sniper_size_frac x the base notional — adding short into a squeeze wick where
    # the base fill was losing (per-fill +2-3% alpha; pooled +13% MAR demo-candidate).
    # Default OFF; the resting limit is reduce_only=False, cancelled at the trade exit.
    sniper_enabled: bool = False
    sniper_wick_pct: float = 0.08
    sniper_size_frac: float = 0.25
    # --- dynamic-exit FORWARD SHADOW (paper-only; P5 was an in-sample NULL) ---
    # Logs what the fade-completion exit WOULD have done per live entry into
    # continuous_dynexit_shadow.jsonl. Zero order impact; forward evidence is the
    # only path that can ever promote it (receipt continuous-dynexit-forward-shadow).
    dynexit_shadow_enabled: bool = True
    # --- winner_base 4-component ensemble (the validated research object) ---
    # (name, entry_event_trigger|"none", age_days_min, take_profit_pct, weight).
    # Non-empty => the cycle selects entries PER COMPONENT (each with its own event
    # trigger, age floor and venue-side TP) and sizes each entry by weight x the base
    # per-position notional — the live form of combine_continuous_components on the
    # frozen receipt weights (no re-estimation; R1 walk-forward falsifier).
    ensemble_components: tuple[tuple[str, str, int, float, float], ...] = ()
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    # --- live panel cache (Tier 2): compute the trailing closed-bar features once per bar close,
    # then refresh only the live-price term + re-rank on each wake. Provably np.allclose-equivalent
    # to the full recompute (tested); the full recompute stays the always-available fallback. ---
    live_panel_cache_enabled: bool = True
    # --- tick-driven protective-exit monitor (Tier 1): a fast loop that reacts to breakeven /
    # failed-fade / stop-approach on HELD names on every ticker tick (no panel recompute). ---
    protective_exit_enabled: bool = True
    protective_exit_min_interval_seconds: float = 2.0    # debounce floor between fast checks
    protective_exit_heartbeat_seconds: float = 10.0      # fallback wake if no ticks arrive
    # --- debounced ticker-batch entry wake (Tier 3): after this many ticker updates accumulate,
    # request a full cycle (entries re-evaluated). 0 = OFF (default) — for a fade that plays out
    # over hours, 60s->1s entry timing is likely noise; enable only if forward demo shows entries
    # are systematically late. Still throttled by the daemon's min-cycle-interval floor. ---
    ticker_batch_wake_threshold: int = 0
    # --- WS kline stream (same shape as the other sleeves) ---
    ws_klines_enabled: bool = True
    # Follow ANOTHER root's flushed WS kline snapshot (read-only) instead of running a
    # second WS pool — the paper shadow co-located with the demo sleeve sets this to the
    # demo root so both sleeves share one market-data plane AND decide on identical
    # signal inputs: the rmom gate is read from the followed root too (it is derived
    # purely from that root's klines). Empty = run the sleeve's own pool (default).
    klines_follow_root: str = ""
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 45
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


def continuous_dataset_names(config: ContinuousDemoCycleConfig) -> tuple[str, str, str]:
    """(trades, orders, cycles) dataset names — separate from the short/long sleeves."""
    if config.paper_mode:
        return ("continuous_fade_paper_trades", "continuous_fade_paper_orders", "continuous_fade_paper_cycles")
    return ("continuous_fade_demo_trades", "continuous_fade_demo_orders", "continuous_fade_demo_cycles")


def build_live_continuous_state(
    klines_recent: pl.DataFrame,
    current_prices: dict[str, float],
    rmom: pl.DataFrame,
    *,
    now_ts_ms: int,
    config: ContinuousDemoCycleConfig,
) -> pl.DataFrame:
    """Live cross-sectional decile at `now`, using confirmed 1h bars + the live ticker price.

    `klines_recent`: confirmed hourly bars from the WS kline store (cols incl. ts_ms, symbol, close,
    turnover_quote). `current_prices`: live last/mark price per symbol from the ticker cache. `rmom`:
    the daily residual-momentum table renamed to [symbol, day_ts, residual_momentum].

    Appends a synthetic in-progress ("current") bar per symbol at the current hour slot with
    close = live price (turnover carried from the last confirmed bar for the liquid gate), runs the
    SHARED verified decile pipeline, and returns the latest-bar rows:
    [symbol, decile, composite, turnover_quote]. This is the live analog of one backtest timestamp."""
    if klines_recent.is_empty() or not current_prices:
        return _empty_live_state()
    cur_ts = (int(now_ts_ms) // MS_PER_HOUR) * MS_PER_HOUR
    k = klines_recent.select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    if k.is_empty():
        return _empty_live_state()
    last_turn = (
        k.sort("ts_ms").group_by("symbol").agg(pl.col("turnover_quote").last().alias("lt"))
    )
    prices = pl.DataFrame(
        {"symbol": list(current_prices.keys()), "close": [float(v) for v in current_prices.values()]}
    ).filter(pl.col("close") > 0)
    cur = (
        prices.join(last_turn, on="symbol", how="inner")     # only symbols with confirmed history
        .with_columns(pl.lit(cur_ts).alias("ts_ms"), pl.col("lt").alias("turnover_quote"))
        .select("ts_ms", "symbol", "close", "turnover_quote")
    )
    if cur.is_empty():
        return _empty_live_state()
    combined = pl.concat([k, cur], how="vertical_relaxed")
    panel = compute_continuous_decile_panel(
        combined,
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=0,
        feature_set=config.feature_set,
    )
    return _select_live_state_columns(panel.filter(pl.col("ts_ms") == cur_ts))


def build_confirmed_entry_state(
    klines_recent: pl.DataFrame,
    rmom: pl.DataFrame,
    *,
    now_ts_ms: int,
    config: ContinuousDemoCycleConfig,
) -> pl.DataFrame:
    """ENTRY decile from the CONFIRMED bar-close (no live in-progress bar) at the +Nh-validated deciding
    bar — the alpha-sweep #1 fix. The engine validated entering 1h AFTER the deciding bar's close (d1),
    which ~doubles MAR vs the live intra-hour cross (d0); shorting intra-hour walks into the first-hour
    continuation/squeeze. So a name is an entry candidate iff it was in the top decile at the bar that
    closed `entry_confirm_delay_hours` ago. NO synthetic live bar → fully PIT/confirmed. Returns the same
    [symbol, decile, composite, turnover_quote] shape as build_live_continuous_state so the selector is a
    drop-in. (Used for ENTRIES only; EXITS keep the live tick-driven state.)"""
    delay = max(1, int(config.entry_confirm_delay_hours))
    if klines_recent.is_empty():
        return _empty_live_state()
    cur_ts = (int(now_ts_ms) // MS_PER_HOUR) * MS_PER_HOUR
    # deciding bar starts at ts_d, closes at ts_d+1h; entry is +delay h after that close, so the most
    # recent eligible deciding bar has ts_d = cur_ts - (1+delay)h (its +delay-after-close <= cur_ts <= now).
    deciding_ts = cur_ts - (1 + delay) * MS_PER_HOUR
    k = klines_recent.select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    if k.is_empty():
        return _empty_live_state()
    panel = compute_continuous_decile_panel(
        k,
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=0,
        feature_set=config.feature_set,
    )
    return _select_live_state_columns(panel.filter(pl.col("ts_ms") == deciding_ts))


def _empty_live_state() -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": pl.Series([], dtype=pl.String), "decile": pl.Series([], dtype=pl.Int64),
         "composite": pl.Series([], dtype=pl.Float64), "turnover_quote": pl.Series([], dtype=pl.Float64),
         "ret1": pl.Series([], dtype=pl.Float64), "max_ret168": pl.Series([], dtype=pl.Float64),
         "prior6_ret1_max": pl.Series([], dtype=pl.Float64),
         "giveback_from_prior6_high": pl.Series([], dtype=pl.Float64),
         "turnover_spike_168h": pl.Series([], dtype=pl.Float64)}
    )


def _select_live_state_columns(panel: pl.DataFrame) -> pl.DataFrame:
    base = ["symbol", "decile", "composite", "turnover_quote"]
    event_cols = ["ret1", "max_ret168", "prior6_ret1_max", "giveback_from_prior6_high", "turnover_spike_168h"]
    return panel.select(base + [c for c in event_cols if c in panel.columns])


class LivePanelCache:
    """Tier 2 — within-hour static-feature cache for the live continuous decile.

    `build_live_continuous_state` re-runs the FULL trailing-window feature pipeline (rv_168h, vov,
    dist_low, xsret7/3) over the whole ~45d×N-symbol history on every wake. But those windows are
    trailing aggregates over CONFIRMED hourly bars — they do not change between bar closes; only the
    in-progress ("current") bar's live-price term moves intra-hour. This cache computes the heavy
    per-symbol confirmed-bar part ONCE per bar close (`_refresh`) and, on each wake, computes only
    the current bar's features from the cached carry + the live price and re-ranks the cross-section
    (`_live_panel`). That turns the per-wake cost from a full O(N×history) recompute into an
    O(N) carry-update + O(N log N) re-rank, which is what makes sub-minute triggers affordable.

    EQUIVALENCE (the repo's `np.allclose` gate): the cross-sectional half is the SHARED
    `cross_sectional_decile`, and the per-symbol current-bar feature values are computed to reproduce
    exactly what `per_symbol_timeseries_features` produces for the appended synthetic bar — so
    `state(...)` is `np.allclose` on `composite` and exact on `decile`/membership vs
    `build_live_continuous_state(...)`. `tests/test_liquidity_migration_continuous_demo.py` pins it
    (incl. a young-symbol and a gapped-symbol case). The full recompute remains the always-available
    fallback in the cycle, so a cache bug degrades to "slower", never "wrong".
    """

    __slots__ = ("_rmom_quantile", "_feature_set", "_exclude", "_cur_ts", "_sig", "_carry", "refreshes", "live_updates")

    def __init__(
        self,
        *,
        rmom_quantile: float = 0.5,
        feature_set: tuple[str, ...] = FEATURES,
        exclude_symbols: tuple[str, ...] = (),
    ) -> None:
        self._rmom_quantile = float(rmom_quantile)
        self._feature_set = tuple(feature_set)
        self._exclude = tuple(exclude_symbols)
        self._cur_ts: int | None = None
        self._sig: tuple[int, int, float, float] | None = None
        self._carry: dict[str, dict[str, Any]] = {}
        self.refreshes = 0
        self.live_updates = 0

    def confirmed_hour(self) -> int | None:
        return self._cur_ts

    def _confirmed_signature(
        self, klines_recent: pl.DataFrame, cur_ts: int, config: ContinuousDemoCycleConfig,
    ) -> tuple[int, int, float, float]:
        """A cheap content fingerprint of the confirmed bars feeding the carry: (row count, max ts,
        close-sum, turnover-sum). It catches NOT JUST the hour rolling over but also a confirmed bar
        being BACKFILLED or RE-DELIVERED with corrected values mid-hour (Bybit re-pushes a just-closed
        bar after late trades; `KlineStore.add_bar` overwrites it in place) — without that, the carry
        would silently go stale within the hour. O(N) scan (no rolling), far cheaper than `_refresh`."""
        exclude = self._exclude or config.exclude_symbols
        lf = klines_recent.lazy().select("ts_ms", "symbol", "close", "turnover_quote").filter(
            pl.col("ts_ms") < cur_ts)
        if exclude:
            lf = lf.filter(~pl.col("symbol").is_in(list(exclude)))
        agg = lf.select(
            pl.len().alias("h"), pl.col("ts_ms").max().alias("mx"),
            pl.col("close").sum().alias("cs"), pl.col("turnover_quote").sum().alias("ts_sum"),
        ).collect().row(0)
        return (int(agg[0]), int(agg[1] or 0), float(agg[2] or 0.0), float(agg[3] or 0.0))

    def state(
        self,
        klines_recent: pl.DataFrame,
        current_prices: dict[str, float],
        rmom: pl.DataFrame,
        *,
        now_ts_ms: int,
        config: ContinuousDemoCycleConfig,
    ) -> pl.DataFrame:
        """Drop-in replacement for `build_live_continuous_state`: re-run the heavy `_refresh` only when
        the confirmed-bar content changes (hour roll-over OR a backfilled/re-delivered bar — detected
        by the cheap signature), then compute the live decile from the carry + live prices."""
        if klines_recent.is_empty() or not current_prices:
            return _empty_live_state()
        cur_ts = (int(now_ts_ms) // MS_PER_HOUR) * MS_PER_HOUR
        sig = self._confirmed_signature(klines_recent, cur_ts, config)
        if self._cur_ts != cur_ts or self._sig != sig or not self._carry:
            self._refresh(klines_recent, cur_ts, config)
            self._sig = sig
        return self._live_panel(current_prices, rmom, cur_ts, config)

    def _refresh(self, klines_recent: pl.DataFrame, cur_ts: int, config: ContinuousDemoCycleConfig) -> None:
        """HEAVY (once per bar close): compute the per-symbol confirmed-bar carry needed to
        reconstruct the synthetic current bar's features from a live price. `cur_ts` is the current
        hour slot; the confirmed bars are everything strictly before it (matches
        `build_live_continuous_state`)."""
        self._cur_ts = cur_ts
        self._carry = {}
        self.refreshes += 1
        k = klines_recent.select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
        exclude = self._exclude or config.exclude_symbols
        if exclude:
            k = k.filter(~pl.col("symbol").is_in(list(exclude)))
        if k.is_empty():
            return
        feats = per_symbol_timeseries_features(k).sort(["symbol", "ts_ms"])
        grouped = feats.group_by("symbol", maintain_order=True).agg(
            pl.col("close").alias("_close"),
            pl.col("ret1").alias("_ret1"),
            pl.col("rv_168h").alias("_rv"),
            pl.col("turnover_quote").last().alias("_turn"),
        )
        for row in grouped.iter_rows(named=True):
            closes = np.asarray(row["_close"], dtype=float)
            n = int(closes.size)
            if n == 0:
                continue
            ret1 = np.array([np.nan if v is None else float(v) for v in row["_ret1"]], dtype=float)
            rv = np.array([np.nan if v is None else float(v) for v in row["_rv"]], dtype=float)
            self._carry[str(row["symbol"])] = {
                "n": n,
                "prev_close": float(closes[-1]),
                # positional shift(72)/shift(168) at the synthetic bar (index n) == confirmed[n-72/-168]
                "close_72": float(closes[n - 72]) if n >= 72 else None,
                "close_168": float(closes[n - 168]) if n >= 168 else None,
                # rolling 720 window at the synthetic bar = last <=719 confirmed closes + the live bar
                "close_win": closes[max(0, n - 719):n],
                # rolling 168 ret1 window = last <=167 confirmed ret1 + the live ret1
                "ret1_win": ret1[max(0, n - 167):n],
                # rolling 720 rv_168h window = last <=719 confirmed rv_168h + the live rv_168h
                "rv_win": rv[max(0, n - 719):n],
                "turnover": float(row["_turn"]) if row["_turn"] is not None else 0.0,
            }

    def _live_panel(
        self,
        current_prices: dict[str, float],
        rmom: pl.DataFrame,
        cur_ts: int,
        config: ContinuousDemoCycleConfig,
    ) -> pl.DataFrame:
        """CHEAP (per wake): per-symbol current-bar features from cached carry + live price, then the
        SHARED cross-sectional decile. Mirrors the synthetic-bar arithmetic of
        `per_symbol_timeseries_features` exactly (rolling_std ddof=1, min_samples = non-null count,
        the `max>min` dist_low guard, positional return offsets)."""
        self.live_updates += 1
        rows: list[dict[str, Any]] = []
        for symbol, c in self._carry.items():
            price = current_prices.get(symbol)
            if price is None or price <= 0.0:
                continue
            price = float(price)
            prev = c["prev_close"]
            if prev <= 0.0:
                continue
            ret1_cur = price / prev - 1.0
            # rv_168h: rolling_std(window=168, min_samples=48) over the confirmed tail + the live ret1
            win = c["ret1_win"]
            win = win[~np.isnan(win)] if win.size else win
            rv_vals = np.concatenate([win, np.array([ret1_cur])]) if win.size else np.array([ret1_cur])
            rv_cur: float | None = float(np.std(rv_vals, ddof=1)) if rv_vals.size >= 48 else None
            max_ret168_cur: float | None = float(np.max(rv_vals)) if rv_vals.size >= 48 else None
            # min720/max720: rolling 720 min/max over confirmed close tail + the live close
            cw = c["close_win"]
            count = int(cw.size) + 1
            if count >= 168:
                mn: float | None = min(float(cw.min()), price) if cw.size else price
                mx: float | None = max(float(cw.max()), price) if cw.size else price
            else:
                mn = mx = None
            dist_low = (price - mn) / (mx - mn) if (mn is not None and mx is not None and mx > mn) else None
            # vov: rolling_std(window=720, min_samples=168) over confirmed rv tail + the live rv
            if rv_cur is None:
                vov_cur: float | None = None
            else:
                rw = c["rv_win"]
                rw = rw[~np.isnan(rw)] if rw.size else rw
                vov_vals = np.concatenate([rw, np.array([rv_cur])]) if rw.size else np.array([rv_cur])
                vov_cur = float(np.std(vov_vals, ddof=1)) if vov_vals.size >= 168 else None
            ret72_cur = (price / c["close_72"] - 1.0) if c["close_72"] else None
            ret168_cur = (price / c["close_168"] - 1.0) if c["close_168"] else None
            rows.append({
                "symbol": symbol, "ts_ms": cur_ts, "turnover_quote": c["turnover"],
                "rv_168h": rv_cur, "max_ret168": max_ret168_cur, "vov": vov_cur, "dist_low": dist_low,
                "ret72": ret72_cur, "ret168": ret168_cur,
            })
        if not rows:
            return _empty_live_state()
        frame = pl.DataFrame(
            rows,
            schema={"symbol": pl.String, "ts_ms": pl.Int64, "turnover_quote": pl.Float64,
                    "rv_168h": pl.Float64, "max_ret168": pl.Float64, "vov": pl.Float64, "dist_low": pl.Float64,
                    "ret72": pl.Float64, "ret168": pl.Float64},
        )
        panel = cross_sectional_decile(
            frame,
            rmom,
            rmom_quantile=self._rmom_quantile,
            feature_set=self._feature_set,
        )
        return panel.select("symbol", "decile", "composite", "turnover_quote")


def select_continuous_entries(
    live_state: pl.DataFrame,
    *,
    held_symbols: set[str],
    cooldown_symbols: set[str],
    open_count: int,
    config: ContinuousDemoCycleConfig,
    eligible_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fresh shorts: names currently in the top decile + liquid + not held + not in cooldown (+ age-
    eligible if `eligible_symbols` is supplied — the inherited fresh-listing-squeezer floor).

    Holding a position IS the 'still in the spell' state, so 'not held' implements fresh-entry; the
    exit (left-decile) is planned separately. Ranked by composite desc, capped by capacity."""
    if live_state.is_empty():
        return []
    room = max(0, config.max_active - open_count)
    if room <= 0:
        return []
    cand = live_state.filter(
        (pl.col("decile") == config.decile) & (pl.col("turnover_quote") >= config.liq_turnover_min)
    )
    if config.entry_event_trigger != "none":
        cand = cand.filter(_entry_event_expr(config.entry_event_trigger))
    if held_symbols:
        cand = cand.filter(~pl.col("symbol").is_in(list(held_symbols)))
    if cooldown_symbols:
        cand = cand.filter(~pl.col("symbol").is_in(list(cooldown_symbols)))
    if eligible_symbols is not None:
        cand = cand.filter(pl.col("symbol").is_in(list(eligible_symbols)))
    return cand.sort("composite", descending=True).head(min(config.max_new_entries_per_cycle, room)).to_dicts()


def plan_continuous_sniper_orders(
    entries: list[dict[str, Any]],
    *,
    config: ContinuousDemoCycleConfig,
    price_by_symbol: dict[str, float],
) -> list[dict[str, Any]]:
    """Pure planner for the sniper resting-limit leg (S1 Amendment 6).

    For each freshly-entered short, plan ONE resting Sell limit at
    ``entry_price * (1 + sniper_wick_pct)`` (above entry — fills only if the name
    squeezes up into the wick) sized to ``sniper_size_frac`` of the base entry
    notional. reduce_only=False (it ADDS short). Returns [] when disabled or when
    an entry has no usable price/notional. The caller submits + cancels-on-exit;
    this function does no I/O so it is unit-testable.
    """
    if not config.sniper_enabled or config.sniper_size_frac <= 0.0 or config.sniper_wick_pct <= 0.0:
        return []
    plans: list[dict[str, Any]] = []
    for entry in entries:
        symbol = str(entry.get("symbol", ""))
        entry_price = _float(entry.get("entry_price")) or price_by_symbol.get(symbol, 0.0)
        base_notional = _float(entry.get("notional_usdt"))
        base_qty = _float(entry.get("qty"))
        if not symbol or entry_price <= 0.0 or base_qty <= 0.0:
            continue
        limit_price = entry_price * (1.0 + config.sniper_wick_pct)
        plans.append({
            "symbol": symbol,
            "side": "Sell",
            "reduce_only": False,
            "order_type": "Limit",
            "limit_price": limit_price,
            "qty": base_qty * config.sniper_size_frac,
            "notional_usdt": base_notional * config.sniper_size_frac,
            "base_trade_id": str(entry.get("trade_id", "")),
            "signal_ts_ms": int(entry.get("signal_ts_ms") or 0),
            "tick_size": _float(entry.get("tick_size")),
            "qty_step": _float(entry.get("qty_step")),
            "reason": "sniper_wick_add",
        })
    return plans


SNIPER_REASON = "sniper_wick_add"
SNIPER_TRADE_SUFFIX = "-snipe"
# Bybit v5 terminal orderStatus values — the ONLY evidence that may terminalize a
# sniper order row. A missing/lagging history row or a non-terminal status (New,
# PartiallyFilled, Untriggered, Triggered) means "still alive somewhere": re-resolve
# next cycle instead of ghosting a resting order (empty-fetch-as-deletion class).
_SNIPER_TERMINAL_ORDER_STATUSES = frozenset(
    {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}
)


def _continuous_sniper_link_prefix(config: ContinuousDemoCycleConfig) -> str:
    return _continuous_entry_link_prefix(config) + "s"


def _sniper_round_qty(qty: float, qty_step: float) -> float:
    """Floor a snipe qty to the contract qty step (0 when it can't meet one step)."""
    if qty <= 0.0:
        return 0.0
    if qty_step <= 0.0:
        return qty
    steps = int(Decimal(str(qty)) / Decimal(str(qty_step)))
    return float(Decimal(steps) * Decimal(str(qty_step)))


def _sniper_trade_id(base_trade_id: str) -> str:
    return f"{base_trade_id}{SNIPER_TRADE_SUFFIX}"


def _execute_sniper_placements(
    exec_entries: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    demo: ContinuousDemoCycleConfig,
    now_ms: int,
    strategy_id: str,
    price_by_symbol: dict[str, float],
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Place the sniper resting limits for this cycle's fresh entries (S1 Amendment 6).

    One PostOnly Sell limit per fresh short at entry*(1+wick), quarter-size, with the
    book's disaster stop attached venue-side (a fill between cycles is protected even
    before its ledger row exists). Returns ORDER rows only — a snipe becomes a TRADE
    row when `reconcile_continuous_snipes` observes its fill. Dry-run records planned
    rows without submitting.
    """
    from .event_demo import _stop_price_for_entry

    plans = plan_continuous_sniper_orders(exec_entries, config=demo, price_by_symbol=price_by_symbol)
    order_rows: list[dict[str, Any]] = []
    for plan in plans:
        symbol = plan["symbol"]
        qty = _sniper_round_qty(_float(plan.get("qty")), _float(plan.get("qty_step")))
        if qty <= 0.0:
            continue
        tick = _float(plan.get("tick_size"))
        limit_price = _float(plan.get("limit_price"))
        if tick > 0.0:
            limit_price = float(Decimal(str(round(limit_price / tick))) * Decimal(str(tick)))
        stop_price = _stop_price_for_entry(
            entry_price=limit_price, side="short", stop_loss_pct=demo.stop_loss_pct, tick_size=tick
        )
        signal_ts = int(plan.get("signal_ts_ms") or now_ms)
        trade_id = _sniper_trade_id(str(plan.get("base_trade_id", "")))
        # Trade-id-hashed link: two components' snipes on the SAME symbol share signal_ts
        # (both candidates come from the same confirmed bar) — a shared link cross-wires
        # their fill attribution (see _continuous_suborder_link_id).
        link = _continuous_suborder_link_id(
            _continuous_sniper_link_prefix(demo), symbol=symbol, signal_ts_ms=signal_ts, trade_id=trade_id
        )
        qty_text = _decimal_text(Decimal(str(qty)))
        submit_mode, status, order_id, error = "dry_run", "resting", "", ""
        if demo.submit_orders:
            assert trading_client is not None
            if record_preflight is not None:
                record_preflight({
                    "order_link_id": link, "ts_ms": now_ms, "trade_id": trade_id,
                    "strategy_id": strategy_id, "symbol": symbol, "side": "Sell", "qty": qty_text,
                    "reduce_only": False, "submit_mode": "preflight", "status": "submitted",
                    "trade_side": "short", "sleeve": continuous_sleeve_name(demo),
                    "signal_ts_ms": signal_ts, "stop_price": stop_price, "reason": SNIPER_REASON,
                })
            try:
                result = trading_client.place_order(**_order_params(
                    symbol=symbol, side="Sell", qty=qty_text, order_type="Limit",
                    order_link_id=link, reduce_only=False, price=limit_price,
                    time_in_force="PostOnly", stop_loss=stop_price if stop_price > 0 else None,
                ))
                submit_mode = "submitted"
                order_id = str(result.get("orderId", ""))
            except Exception as exc:  # noqa: BLE001 — a failed snipe must never block the base entry
                submit_mode, status, error = "error", "failed", f"sniper place_order failed: {exc}"[:500]
        order_rows.append({
            "order_link_id": link, "ts_ms": now_ms, "trade_id": trade_id,
            "strategy_id": strategy_id, "symbol": symbol, "side": "Sell", "order_type": "Limit",
            "qty": qty_text, "reduce_only": False, "order_id": order_id, "submit_mode": submit_mode,
            "avg_price": 0.0, "notional_usdt": qty * limit_price, "status": status,
            "trade_side": "short", "signal_ts_ms": signal_ts, "tick_size": tick,
            "qty_step": _float(plan.get("qty_step")), "stop_price": stop_price,
            "stop_loss_pct": demo.stop_loss_pct, "error": error,
            "sleeve": continuous_sleeve_name(demo), "reason": SNIPER_REASON,
            "limit_price": limit_price, "base_trade_id": str(plan.get("base_trade_id", "")),
        })
    return order_rows


def _sniper_order_state(all_orders: pl.DataFrame) -> list[dict[str, Any]]:
    """Latest-state view of sniper orders: one dict per link still RESTING."""
    if all_orders.is_empty() or "reason" not in all_orders.columns:
        return []
    snipes = all_orders.filter(pl.col("reason") == SNIPER_REASON)
    if snipes.is_empty():
        return []
    resting: dict[str, dict[str, Any]] = {}
    terminal: set[str] = set()
    for row in snipes.sort("ts_ms").to_dicts():
        link = str(row.get("order_link_id", ""))
        status = str(row.get("status", ""))
        if status in ("filled", "cancelled", "closed", "failed"):
            terminal.add(link)
        elif status == "resting" and str(row.get("submit_mode")) in ("submitted", "dry_run"):
            resting[link] = row
    return [row for link, row in resting.items() if link not in terminal]


def reconcile_continuous_snipes(
    all_trades: pl.DataFrame,
    all_orders: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: ContinuousDemoCycleConfig,
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-cycle sniper lifecycle sweep. Returns (fill_trade_rows, order_update_rows, snipe_exit_plans).

    1. FILLS: a resting snipe no longer on the venue's open-order book is resolved via
       order history — executed qty becomes an OPEN short trade row (first-class: the
       normal exit machinery manages it from here), else a terminal cancel marker.
    2. CANCELS: a still-resting snipe whose BASE trade is no longer open is cancelled
       (the wick window died with the base thesis).
    3. BASE-EXIT: an open snipe TRADE whose base trade closed is planned for exit
       (reason ``base_exited``) — the research form's exit-with-base semantics.
    Dry-run mode does the bookkeeping without client calls.
    """
    open_base_ids: set[str] = set()
    open_snipe_trades: list[dict[str, Any]] = []
    have_trades = not all_trades.is_empty() and "status" in all_trades.columns
    if have_trades:
        for t in all_trades.filter(pl.col("status") == "open").to_dicts():
            tid = str(t.get("trade_id", ""))
            if tid.endswith(SNIPER_TRADE_SUFFIX):
                open_snipe_trades.append(t)
            else:
                open_base_ids.add(tid)
    known_trade_ids: set[str] = (
        set(all_trades["trade_id"].cast(pl.String).to_list()) if have_trades and "trade_id" in all_trades.columns else set()
    )

    fill_rows: list[dict[str, Any]] = []
    update_rows: list[dict[str, Any]] = []
    for row in _sniper_order_state(all_orders):
        link = str(row.get("order_link_id", ""))
        symbol = str(row.get("symbol", ""))
        base_id = str(row.get("base_trade_id", ""))
        trade_id = str(row.get("trade_id", "")) or _sniper_trade_id(base_id)
        base_open = base_id in open_base_ids
        if str(row.get("submit_mode")) != "submitted" or trading_client is None:
            if not base_open:  # dry-run bookkeeping: retire the planned snipe with its base
                update_rows.append({**row, "ts_ms": now_ms, "status": "cancelled", "submit_mode": "dry_run"})
            continue
        try:
            open_orders = trading_client.get_open_orders(symbol=symbol)
        except Exception:  # noqa: BLE001 — fail-safe: try again next cycle
            continue
        still_open = any(str(o.get("orderLinkId", "")) == link for o in open_orders)
        if still_open:
            if not base_open:
                try:
                    trading_client.cancel_order(symbol=symbol, order_link_id=link)
                    update_rows.append({**row, "ts_ms": now_ms, "status": "cancelled", "submit_mode": "cancel"})
                except Exception as exc:  # noqa: BLE001
                    update_rows.append({**row, "ts_ms": now_ms, "status": "resting",
                                        "error": f"sniper cancel failed: {exc}"[:300]})
            continue
        # no longer resting on the venue: resolve filled-vs-cancelled from history
        try:
            history = trading_client.get_order_history(symbol=symbol, order_link_id=link)
        except Exception:  # noqa: BLE001
            continue
        exec_qty = 0.0
        avg_price = 0.0
        exec_ts = now_ms
        history_status = ""
        for h in history:
            if str(h.get("orderLinkId", "")) != link:
                continue
            exec_qty = _float(h.get("cumExecQty"))
            avg_price = _float(h.get("avgPrice"))
            exec_ts = int(_float(h.get("updatedTime")) or now_ms)
            history_status = str(h.get("orderStatus", ""))
            break
        if (
            history_status in _SNIPER_TERMINAL_ORDER_STATUSES
            and exec_qty > 0.0
            and avg_price > 0.0
            and trade_id not in known_trade_ids
        ):
            fill_rows.append({
                "trade_id": trade_id, "strategy_id": str(row.get("strategy_id", "")),
                "symbol": symbol, "side": "short", "sleeve": continuous_sleeve_name(demo),
                "status": "open", "ts_ms": exec_ts, "entry_ts_ms": exec_ts, "opened_at_ms": exec_ts,
                "updated_at_ms": now_ms, "signal_ts_ms": int(_float(row.get("signal_ts_ms")) or exec_ts),
                "entry_price": avg_price, "qty": _decimal_text(Decimal(str(exec_qty))),
                "notional_usdt": abs(avg_price * exec_qty), "equity_usdt": 0.0,
                "entry_leverage": demo.entry_leverage, "tick_size": _float(row.get("tick_size")),
                "qty_step": _float(row.get("qty_step")), "stop_price": _float(row.get("stop_price")),
                "stop_loss_pct": demo.stop_loss_pct, "entry_order_link_id": link,
                "entry_order_id": str(row.get("order_id", "")), "submit_mode": "submitted",
                "base_trade_id": base_id, "reason": SNIPER_REASON,
            })
            update_rows.append({**row, "ts_ms": now_ms, "status": "filled", "avg_price": avg_price})
        elif history_status in _SNIPER_TERMINAL_ORDER_STATUSES:
            # Positive terminal evidence from history. An already-booked fill (trade_id
            # known) marks "filled", everything else "cancelled" — both terminal.
            terminal = "filled" if exec_qty > 0.0 else "cancelled"
            update_rows.append({**row, "ts_ms": now_ms, "status": terminal, "avg_price": avg_price})
        # else: empty/lagging history or a non-terminal orderStatus — the open-orders
        # snapshot may have been empty/partial (the empty-fetch-as-deletion class,
        # audit 2026-06-11: terminalizing here ghosted a still-resting PostOnly order
        # forever — no cancel ever issued, no trade row if it later filled). The fill
        # branch above requires terminal status too (audit 2026-06-12: a PartiallyFilled
        # order missing from a torn open-orders snapshot booked the partial AND
        # terminalized the row "filled" — the still-resting remainder became a ghost).
        # Leave the row as-is and re-resolve next cycle; never terminalize without
        # evidence. known_trade_ids dedup makes the eventual terminal booking idempotent.

    snipe_exit_plans = [
        {**t, "exit_reason": "base_exited", "exit_trigger_ts_ms": now_ms}
        for t in open_snipe_trades
        if str(t.get("base_trade_id", "")) not in open_base_ids
    ]
    return fill_rows, update_rows, snipe_exit_plans


def _trade_excursion(
    trade: dict[str, Any],
    *,
    now_ms: int,
    klines: pl.DataFrame | None,
    price_by_symbol: dict[str, float] | None,
    include_live_in_mfe: bool = False,
) -> tuple[int, float, float, float]:
    """Return ``(held_ms, entry_price, mfe, cur_ret)`` for one held short.

    ``mfe`` (max favorable excursion) = ``(entry - min_low_over_hold) / entry``, reconstructed from
    the kline lows over ``[entry, now]`` — the live analog of the engine's bar-by-bar excursion.
    When ``include_live_in_mfe`` is set (the tick loop) the live price is ALSO a candidate low, so an
    intra-hour dip counts immediately instead of waiting for the bar to close (the whole point of
    reacting on a tick). ``cur_ret`` = current short PnL fraction from the live ticker price."""
    symbol = str(trade.get("symbol", ""))
    entry_ts = int(trade.get("entry_ts_ms") or trade.get("opened_at_ms") or 0)
    entry_price = float(trade.get("entry_price") or 0.0)
    held_ms = now_ms - entry_ts if entry_ts else 0
    prices = price_by_symbol or {}
    cur = prices.get(symbol)
    cur_ret = ((entry_price - float(cur)) / entry_price) if (entry_price > 0 and cur) else 0.0
    mfe = 0.0
    if entry_price > 0:
        min_low: float | None = None
        if klines is not None and not klines.is_empty() and "low" in klines.columns:
            sub = klines.filter(
                (pl.col("symbol") == symbol) & (pl.col("ts_ms") >= entry_ts) & (pl.col("ts_ms") <= now_ms)
            )
            if not sub.is_empty():
                lo = sub["low"].min()
                if lo is not None:
                    lo_f = float(cast(float, lo))  # polars min() widens; "low" is numeric
                    if lo_f > 0:
                        min_low = lo_f
        if include_live_in_mfe and cur is not None and float(cur) > 0:
            min_low = float(cur) if min_low is None else min(min_low, float(cur))
        if min_low is not None:
            mfe = (entry_price - min_low) / entry_price
    return held_ms, entry_price, mfe, cur_ret


def active_primary_pnl_gate_allows_addon(
    open_primary_trades: list[dict[str, Any]],
    *,
    symbol: str,
    current_price: float | None,
    min_unrealized_return: float = 0.0,
) -> tuple[bool, float | None]:
    """Causal same-symbol add-on gate from open primary ledger rows + current mark.

    The research blend's best risk-adjusted Binance variant allows a `fresh_pop25`
    add-on only when an already-open same-symbol primary `fresh_pop15` fade is not
    underwater at the add-on entry. This helper is deliberately pure so live/paper
    plumbing can use ledger state plus the ticker mark; no realized PnL, Telegram
    approval, REST call, or post-entry path information is involved.

    Returns ``(allowed, worst_unrealized_return)``. If no same-symbol primary is
    active, the add-on is allowed and the return is ``None``. If a same-symbol
    primary is active but the current mark is missing/invalid, fail closed.
    """
    sym = str(symbol)
    same_symbol = [
        t for t in open_primary_trades
        if str(t.get("symbol") or "") == sym and str(t.get("status") or "open") == "open"
    ]
    if not same_symbol:
        return True, None
    mark = float(current_price or 0.0)
    if mark <= 0.0:
        return False, None
    unrealized: list[float] = []
    for trade in same_symbol:
        entry = float(trade.get("entry_price") or 0.0)
        if entry <= 0.0:
            continue
        side = str(trade.get("side") or "short").lower()
        if side in ("buy", "long"):
            unrealized.append(mark / entry - 1.0)
        else:
            unrealized.append(entry / mark - 1.0)
    if not unrealized:
        return False, None
    worst = min(unrealized)
    return worst >= float(min_unrealized_return), worst


def filter_addon_candidates_by_active_primary_pnl_gate(
    candidates: list[dict[str, Any]],
    open_primary_trades: list[dict[str, Any]],
    *,
    price_by_symbol: dict[str, float],
    min_unrealized_return: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the research-stage active-primary PnL gate to add-on candidates.

    This is the demo/paper adapter for the accepted Binance risk-mode rule. It
    is intentionally side-effect free: it reads only the candidate rows, open
    primary ledger rows, and current marks supplied by the caller. No order
    submission, Telegram approval, REST query, or realized future path is used.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for cand in candidates:
        symbol = str(cand.get("symbol") or "")
        mark = float(cand.get("live_price") or price_by_symbol.get(symbol) or 0.0)
        allowed, worst = active_primary_pnl_gate_allows_addon(
            open_primary_trades,
            symbol=symbol,
            current_price=mark,
            min_unrealized_return=min_unrealized_return,
        )
        if allowed:
            kept.append(cand)
        else:
            skipped.append({
                "symbol": symbol,
                "worst_primary_unrealized_return": worst,
                "current_price": mark if mark > 0.0 else None,
            })
    return kept, {
        "addon_primary_pnl_gate_skips": len(skipped),
        "addon_primary_pnl_gate_skip_symbols": [r["symbol"] for r in skipped],
        "addon_primary_pnl_gate_skipped": skipped,
    }


def _protective_exit_reason(
    *, held_ms: int, mfe: float, cur_ret: float, config: ContinuousDemoCycleConfig,
) -> str | None:
    """State-free protective-exit classification, priority order:
      - breakeven    : reached >= breakeven_arm_pct favorable then gave it back to entry (protect winner)
      - stop_approach: live loss reached stop_approach_frac of the way to the disaster stop (squeeze cut,
                       fires on a tick BEFORE Bybit's market stop gaps in)
      - failed_fade  : held >= ff hours, never reached ff_min_mfe, now down > ff_loss_pct (cut loser)
      - max_hold     : the force-exit time cap

    `left_decile` (the state/profit exit) is layered on separately by `plan_continuous_exits`; it needs
    the live decile panel and so is NOT part of the tick-driven protective path."""
    if config.breakeven_arm_pct > 0 and mfe >= config.breakeven_arm_pct and cur_ret <= 0.0:
        return "breakeven"
    if (config.stop_approach_frac > 0.0 and config.stop_loss_pct > 0.0
            and cur_ret <= -(config.stop_approach_frac * config.stop_loss_pct)):
        return "stop_approach"
    if (config.failed_fade_hours > 0 and held_ms >= config.failed_fade_hours * MS_PER_HOUR
            and mfe < config.failed_fade_min_mfe_pct and cur_ret <= -config.failed_fade_loss_pct):
        return "failed_fade"
    if config.max_hold_hours > 0 and held_ms >= config.max_hold_hours * MS_PER_HOUR:
        return "max_hold"
    return None


def plan_continuous_exits(
    open_trades: list[dict[str, Any]],
    decile_state: pl.DataFrame,
    *,
    now_ms: int,
    config: ContinuousDemoCycleConfig,
    klines: pl.DataFrame | None = None,
    price_by_symbol: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Plan covers for held shorts. Exit reasons (first match wins):
      - breakeven : reached >= breakeven_arm_pct favorable then gave it back to entry (protect winner)
      - left_decile: the name dropped CLEARLY out of the top fade decile — HYSTERESIS: its decile fell
                     below ``decile - exit_decile_buffer`` (buffer=1 holds through a one-decile wobble so
                     a name flickering on the D9 boundary is not churned) (the state/profit exit)
      - stop_approach/failed_fade: squeeze-defence loss cuts (see `_protective_exit_reason`)
      - max_hold  : the force-exit time cap

    continuous-6: ``decile_state`` is the SELECTION signal for the ``left_decile`` exit and MUST be the
    SAME confirmed-bar decile snapshot that selected the entry (the caller passes ``entry_state``), NOT
    the live intra-hour decile. ``left_decile`` is the inverse of the entry signal, so it must fire on
    the same signal at the same cadence the entry (and the validated backtest, continuous_events._run_trades)
    use — otherwise a name entered on the confirmed decile is evicted within the same hour on a noisier
    signal it was never entered on (a thrash the engine cannot have). The PROTECTIVE exits below
    (breakeven / stop_approach / failed_fade / max_hold) are PRICE-driven via ``_trade_excursion`` (live
    klines + ticker), so squeeze safety stays immediate and live regardless of the decile snapshot.

    breakeven/failed_fade are the daily-system exits validated cross-venue in the engine ablation; the
    MFE is reconstructed from the kline lows over [entry, now]; current PnL uses the live ticker price.
    The 25% disaster stop is the server-side resting order, not re-planned here."""
    if not open_trades:
        return []
    have_state = not decile_state.is_empty()
    # Hysteresis band: hold while the name's decile is still >= exit_decile_min; cover only once
    # clearly out. exit_decile_buffer=0 == cover the instant it leaves the top decile (legacy).
    exit_decile_min = config.decile - max(0, config.exit_decile_buffer)
    decile_by_symbol = (
        {str(s): int(d) for s, d in zip(decile_state["symbol"].to_list(), decile_state["decile"].to_list())}
        if have_state else {}
    )
    exits: list[dict[str, Any]] = []
    for t in open_trades:
        symbol = str(t.get("symbol", ""))
        held_ms, _entry, mfe, cur_ret = _trade_excursion(
            t, now_ms=now_ms, klines=klines, price_by_symbol=price_by_symbol)
        protective = _protective_exit_reason(held_ms=held_ms, mfe=mfe, cur_ret=cur_ret, config=config)
        if protective == "breakeven":
            reason = "breakeven"
        elif have_state and decile_by_symbol.get(symbol, -1) < exit_decile_min:
            reason = "left_decile"
        elif protective is not None:    # stop_approach / failed_fade / max_hold
            reason = protective
        else:
            continue
        exits.append({**t, "exit_reason": reason, "exit_trigger_ts_ms": now_ms})
    return exits


def plan_protective_exits(
    open_trades: list[dict[str, Any]],
    *,
    now_ms: int,
    config: ContinuousDemoCycleConfig,
    klines: pl.DataFrame | None = None,
    price_by_symbol: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Tier 1 — tick-driven protective covers, NO panel/state required.

    The fast-loop subset of `plan_continuous_exits` minus the `left_decile` state exit: breakeven /
    stop_approach / failed_fade / max_hold on held shorts, evaluated off the live ticker price (and a
    live-inclusive MFE). Running this on every ticker tick lets the daemon cut a squeeze within ~1-2s
    instead of waiting for the next 60s cycle, without recomputing the cross-sectional decile."""
    if not open_trades:
        return []
    exits: list[dict[str, Any]] = []
    for t in open_trades:
        held_ms, _entry, mfe, cur_ret = _trade_excursion(
            t, now_ms=now_ms, klines=klines, price_by_symbol=price_by_symbol, include_live_in_mfe=True)
        reason = _protective_exit_reason(held_ms=held_ms, mfe=mfe, cur_ret=cur_ret, config=config)
        if reason is None:
            continue
        exits.append({**t, "exit_reason": reason, "exit_trigger_ts_ms": now_ms})
    return exits


def _recent_exit_cooldown_symbols(
    all_trades: pl.DataFrame, *, now_ms: int, cooldown_minutes: int, strategy_id: str,
) -> set[str]:
    """Symbols whose most-recent CLOSED continuous trade exited within ``cooldown_minutes`` — the
    time-based re-entry cooldown (anti-thrash). Stateless: recomputed from the ledger each cycle, so it
    survives daemon restarts and never needs its own bookkeeping file."""
    if cooldown_minutes <= 0 or all_trades.is_empty() or "exit_ts_ms" not in all_trades.columns:
        return set()
    cutoff = now_ms - cooldown_minutes * 60_000
    recent = all_trades.filter(
        (pl.col("status") == "closed")
        & (pl.col("strategy_id") == strategy_id)
        & (pl.col("exit_ts_ms").is_not_null())
        & (pl.col("exit_ts_ms") >= cutoff)
    )
    return {str(s) for s in recent["symbol"].to_list()} if not recent.is_empty() else set()


def _row_ts_ms(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _recent_entry_cooldown_symbols(
    all_trades: pl.DataFrame,
    *,
    now_ms: int,
    cooldown_minutes: int,
    strategy_id: str,
) -> set[str]:
    """Symbols whose most-recent continuous entry is inside ``cooldown_minutes``.

    Ledger-derived and restart-safe, like the exit cooldown. The live cycle only
    wires this for the add-on profile unless explicitly changed later.
    """
    if cooldown_minutes <= 0 or all_trades.is_empty():
        return set()
    cutoff = now_ms - cooldown_minutes * 60_000
    out: set[str] = set()
    for row in all_trades.to_dicts():
        if str(row.get("strategy_id") or "") != strategy_id:
            continue
        symbol = str(row.get("symbol") or "")
        ts_ms = _row_ts_ms(row, "entry_ts_ms", "entry_exec_time_ms", "opened_at_ms", "signal_ts_ms")
        if symbol and ts_ms >= cutoff:
            out.add(symbol)
    return out


def _recent_adverse_exit_count(
    all_trades: pl.DataFrame, *, now_ms: int, window_minutes: int, strategy_id: str,
) -> int:
    """Count the sleeve's recent ADVERSE covers (the correlated-squeeze footprint): closed trades whose
    exit_reason is a loss-cut (stop_approach / failed_fade) OR whose net_return is negative, within the
    last `window_minutes`. Drives the entry-pause circuit breaker. Stateless (ledger-derived)."""
    if window_minutes <= 0 or all_trades.is_empty() or "exit_ts_ms" not in all_trades.columns:
        return 0
    cutoff = now_ms - window_minutes * 60_000
    df = all_trades.filter(
        (pl.col("status") == "closed")
        & (pl.col("strategy_id") == strategy_id)
        & (pl.col("exit_ts_ms").is_not_null())
        & (pl.col("exit_ts_ms") >= cutoff)
    )
    if df.is_empty():
        return 0
    adverse = pl.lit(False)
    if "exit_reason" in df.columns:
        adverse = adverse | pl.col("exit_reason").is_in(["stop_approach", "failed_fade"])
    # DEPLOY NOTE (continuous-4): the stored live `net_return` is gross-of-cost (gtr*notional_weight)
    # while the BACKTEST `net_return` is NET-of-cost (gross + cost_return + funding_return, see
    # volume_events._simulate_indexed_trade). The engine's adverse-cover proxy is `net_return<0`
    # NET-of-fees, so the breaker must compare a cost-consistent number. We do NOT mutate the stored
    # field (it is also authored by ws_risk via event_demo_exits with the same gross convention -- a
    # continuous-only rewrite would desync the multi-writer ledger); instead we subtract the realised
    # round-trip fees here, as a fraction of the trade's equity, to match the engine's net definition.
    # (Funding is not tracked per-trade live, so a funding residual remains: a cover whose loss is
    # funding-cost-driven but gross-of-funding positive can still be MISSED. The proxy is strictly
    # conservative only on the receive-funding side -- it never turns a fee-negative cover benign.)
    # Threshold (w24/n8) is unchanged.
    if "net_return" in df.columns:
        net_of_fees = pl.col("net_return").fill_null(0.0)
        if {"entry_fee_usdt", "exit_fee_usdt", "equity_usdt"} <= set(df.columns):
            fee_frac = (
                (pl.col("entry_fee_usdt").fill_null(0.0) + pl.col("exit_fee_usdt").fill_null(0.0))
                / pl.when(pl.col("equity_usdt").fill_null(0.0) > 0.0)
                  .then(pl.col("equity_usdt")).otherwise(None)
            ).fill_null(0.0)
            net_of_fees = net_of_fees - fee_frac
        adverse = adverse | (net_of_fees < 0.0)
    return int(df.filter(adverse).height)


def entry_circuit_breaker_tripped(
    all_trades: pl.DataFrame, *, now_ms: int, config: ContinuousDemoCycleConfig, strategy_id: str,
) -> tuple[bool, int]:
    """(tripped, recent_adverse_count) — pause new entries when a correlated alt-squeeze is cutting many
    of the sleeve's shorts at once. Disabled (never trips) when entry_pause_after_adverse_exits <= 0."""
    if config.entry_pause_after_adverse_exits <= 0:
        return False, 0
    count = _recent_adverse_exit_count(
        all_trades, now_ms=now_ms, window_minutes=config.entry_pause_window_minutes, strategy_id=strategy_id)
    return count >= config.entry_pause_after_adverse_exits, count


def _continuous_order_link_id(prefix: str, *, symbol: str, signal_ts_ms: int, reentry_seq: int = 0) -> str:
    """lm-{en-c|ux-c}-{base}-{ts36}[-{seq}] — the distinct continuous sleeve (ws_risk routing).
    Delegates to the single canonical builder so the format has ONE source of truth; the int() cast
    tolerates a non-int signal_ts (continuous-specific defensiveness). ``reentry_seq`` > 0 appends a
    same-signal-window re-entry suffix (continuous-2): seq=0 is byte-identical to the legacy 5-part
    form, so existing links are unchanged; decode_entry_order_link_id round-trips the seq."""
    from .event_demo import _order_link_id

    link = _order_link_id(prefix, symbol=symbol, signal_ts_ms=int(signal_ts_ms))
    if reentry_seq > 0:
        suffix = f"-{int(reentry_seq)}"
        link = f"{link[: 36 - len(suffix)]}{suffix}"  # truncate-safe append within Bybit's 36-char cap
    return link


def _continuous_suborder_link_id(prefix: str, *, symbol: str, signal_ts_ms: int, trade_id: str) -> str:
    """Order link with a ``-x{3×base36}`` trade-id hash — for the continuous order families that can
    place TWO same-symbol orders in the same second: the sniper base-exit + snipe exit in one sweep,
    multi-component same-symbol trades exiting/resizing together, and two components' snipes. Bybit
    accepts a REUSED link once the first order is terminal, and the WS execution router buffers rows
    per link — a shared link made _wait_for_execution_summary adopt the FIRST order's buffered fills
    as the second's, closing the second trade with the first order's price/fee (audit 2026-06-12).
    Deterministic per trade_id, so a crash-retry of the same logical order reuses the same link
    (idempotent). decode_entry_order_link_id strips the suffix (entry-prefix resize/sniper links
    still decode); exit links are prefix-matched only (is_exit_link) and never decoded."""
    link = _continuous_order_link_id(prefix, symbol=symbol, signal_ts_ms=signal_ts_ms)
    suffix = "-x" + _base36(zlib.crc32(str(trade_id).encode("utf-8")) % 46656).rjust(3, "0")
    return f"{link[: 36 - len(suffix)]}{suffix}"


def _continuous_trade_id(strategy_id: str, symbol: str, signal_ts_ms: int, reentry_seq: int = 0) -> str:
    """Deterministic continuous trade_id. seq=0 is byte-identical to the legacy
    ``{strategy}-{symbol}-{signal_ts}`` form (existing rows unchanged); a same-signal-window re-entry
    (after a cover within the deciding-bar hour) gets seq>0 -> a DISTINCT id, so the closed row is not
    overwritten by storage's trade_id dedup (continuous-2 ledger data loss). A crash-retry of the SAME
    open recomputes the same seq (the closed row count is unchanged) -> the same id+link -> idempotent.
    Round-trips with the orderLinkId seq via decode_entry_order_link_id + the ws_risk reconstruction."""
    base = f"{strategy_id}-{symbol}-{int(signal_ts_ms)}"
    return f"{base}-{int(reentry_seq)}" if reentry_seq > 0 else base


def _continuous_entry_candidates_with_signal_metadata(
    picks: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    all_orders: pl.DataFrame | None = None,
    signal_ts: int,
    strategy_id: str,
    price_by_symbol: dict[str, float],
    stop_loss_pct: float,
    allow_same_signal_reentry: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Attach signal metadata and optionally block same-window re-entry.

    ``continuous-2`` made same-window re-entry rows uniquely identifiable if they exist. The shipped
    selector should still default to the historical fresh-spell behavior: one symbol, one entry for
    a confirmed signal window. Explicit experiments can opt back into the seq-based path.
    """
    prior_by_symbol: dict[str, int] = {}
    if not all_trades.is_empty() and {"symbol", "signal_ts_ms"} <= set(all_trades.columns):
        frame = all_trades.filter(pl.col("signal_ts_ms") == signal_ts)
        if "strategy_id" in frame.columns:
            frame = frame.filter(pl.col("strategy_id") == strategy_id)
        prior_by_symbol = {
            str(r["symbol"]): int(r["_n"])
            for r in frame.group_by("symbol").agg(pl.len().alias("_n")).iter_rows(named=True)
        }
    if all_orders is not None and not all_orders.is_empty() and {"symbol", "signal_ts_ms"} <= set(all_orders.columns):
        order_frame = all_orders.filter(pl.col("signal_ts_ms") == signal_ts)
        if "strategy_id" in order_frame.columns:
            order_frame = order_frame.filter(pl.col("strategy_id") == strategy_id)
        if "reduce_only" in order_frame.columns:
            order_frame = order_frame.filter(~pl.col("reduce_only").fill_null(False))
        if not order_frame.is_empty():
            order_key = (
                "order_link_id"
                if "order_link_id" in order_frame.columns
                else ("trade_id" if "trade_id" in order_frame.columns else "")
            )
            if order_key:
                order_frame = order_frame.unique(subset=["symbol", order_key])
            for r in order_frame.group_by("symbol").agg(pl.len().alias("_n")).iter_rows(named=True):
                symbol = str(r["symbol"])
                prior_by_symbol[symbol] = max(prior_by_symbol.get(symbol, 0), int(r["_n"]))
    candidates: list[dict[str, Any]] = []
    skipped_same_signal_reentry = 0
    for c in picks:
        sym = str(c["symbol"])
        reentry_seq = prior_by_symbol.get(sym, 0)
        if reentry_seq > 0 and not allow_same_signal_reentry:
            skipped_same_signal_reentry += 1
            continue
        candidates.append(
            {
                **c,
                "signal_ts_ms": signal_ts,
                "stop_loss_pct": stop_loss_pct,
                "live_price": price_by_symbol.get(sym, 0.0),
                "reentry_seq": reentry_seq,
                "trade_id": _continuous_trade_id(strategy_id, sym, signal_ts, reentry_seq),
            }
        )
    return candidates, skipped_same_signal_reentry


def continuous_strategy_id(config: ContinuousDemoCycleConfig) -> str:
    base = CONTINUOUS_ADDON_STRATEGY_ID if config.strategy_profile == "continuous_addon_v1" else CONTINUOUS_STRATEGY_ID
    return base + ("_paper" if config.paper_mode else "")


def continuous_sleeve_name(config: ContinuousDemoCycleConfig) -> str:
    return "continuous_addon" if config.strategy_profile == "continuous_addon_v1" else "continuous"


def apply_continuous_demo_profile(config: ContinuousDemoCycleConfig) -> ContinuousDemoCycleConfig:
    """Resolve named demo profiles into explicit knobs.

    ``continuous_rebalance_v1`` is the current research-stage implementation
    target pinned in promoted.py. It stays outside PROFILES and remains
    default-off; selecting the profile makes a paper/demo cycle reproduce the
    candidate contract instead of the older de-promoted continuous sleeve.
    """
    if config.strategy_profile == "continuous_ensemble_v1":
        # The validated winner_base ensemble (frozen receipt weights; receipts:
        # continuous-winner-robustness-2026-06-09 + continuous-walkforward-allocator
        # [weights frozen, no re-estimation] + continuous-hedge-2f-engine-2026-06-10).
        # Research rule w90/tv0.045/max4/ddh-0.04, NO strategy-momentum hurdle (the
        # merged test's winning arm). Per-component age floors and TPs are enforced
        # per entry; venue-side TP attached at order placement.
        return replace(
            config,
            rmom_quantile=0.25,
            feature_set=("max_ret168",),
            liq_turnover_min=500_000.0,
            max_hold_hours=24,
            entry_confirm_delay_hours=1,
            entry_event_trigger="none",
            btc_trend_gate="uptrend",
            daily_rebalance_enabled=True,
            daily_rebalance_realized_vol_window_days=90,
            daily_rebalance_target_daily_vol=0.045,
            daily_rebalance_max_scale=4.0,
            daily_rebalance_drawdown_half_threshold=-0.04,
            daily_rebalance_resize_cost_bps=10.0,
            daily_rebalance_strategy_momentum_window_days=0,
            daily_rebalance_strategy_momentum_min_return=0.0,
            daily_rebalance_strategy_momentum_scale_when_below=0.0,
            ensemble_components=(
                ("p3", "turn3_pop3", 240, 0.10, 0.30),
                ("p4p3", "turn4_pop3", 240, 0.10, 0.20),
                ("p4p5", "turn4_pop5", 240, 0.10, 0.40),
                ("tp14", "none", 210, 0.14, 0.10),
            ),
        )
    if config.strategy_profile != "continuous_rebalance_v1":
        return config
    return replace(
        config,
        rmom_quantile=0.25,
        feature_set=("max_ret168",),
        liq_turnover_min=500_000.0,
        max_hold_hours=24,
        entry_confirm_delay_hours=1,
        entry_event_trigger="turn4_pop4",
        btc_trend_gate="uptrend",
        daily_rebalance_enabled=True,
        daily_rebalance_realized_vol_window_days=90,
        daily_rebalance_target_daily_vol=0.025,
        daily_rebalance_max_scale=4.0,
        daily_rebalance_drawdown_half_threshold=-0.04,
        daily_rebalance_resize_cost_bps=10.0,
        daily_rebalance_strategy_momentum_window_days=180,
        daily_rebalance_strategy_momentum_min_return=0.02,
        daily_rebalance_strategy_momentum_scale_when_below=0.0,
    )


def continuous_rebalance_rule(config: ContinuousDemoCycleConfig) -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=int(config.daily_rebalance_realized_vol_window_days),
        target_daily_vol=float(config.daily_rebalance_target_daily_vol),
        max_scale=float(config.daily_rebalance_max_scale),
        drawdown_half_threshold=float(config.daily_rebalance_drawdown_half_threshold),
        resize_cost_bps=float(config.daily_rebalance_resize_cost_bps),
        strategy_momentum_window_days=int(config.daily_rebalance_strategy_momentum_window_days),
        strategy_momentum_min_return=float(config.daily_rebalance_strategy_momentum_min_return),
        strategy_momentum_scale_when_below=float(config.daily_rebalance_strategy_momentum_scale_when_below),
    )


def _btc_trend_gate_allows_entries(
    klines: pl.DataFrame,
    *,
    signal_ts_ms: int,
    config: ContinuousDemoCycleConfig,
) -> bool:
    gate = config.btc_trend_gate
    if gate == "off":
        return True
    if gate not in ("uptrend", "downtrend"):
        raise ValueError(f"btc_trend_gate must be 'off', 'uptrend', or 'downtrend'; got {gate!r}")
    day = (int(signal_ts_ms) // MS_PER_DAY) * MS_PER_DAY
    trend = _btc_trend_returns(klines).get(day)
    if trend is None:
        return False
    return trend > 0.0 if gate == "uptrend" else trend <= 0.0


def _continuous_entry_link_prefix(config: ContinuousDemoCycleConfig) -> str:
    return CONTINUOUS_ADDON_ENTRY_LINK_PREFIX if continuous_sleeve_name(config) == "continuous_addon" else CONTINUOUS_ENTRY_LINK_PREFIX


def _continuous_exit_link_prefix(config: ContinuousDemoCycleConfig) -> str:
    return CONTINUOUS_ADDON_EXIT_LINK_PREFIX if continuous_sleeve_name(config) == "continuous_addon" else CONTINUOUS_EXIT_LINK_PREFIX


# ============================================================================
# Live cycle runner + short executors (clone of the long-sleeve order path,
# side=Sell, lm-en-c- prefix; reuses the shared event_demo execution helpers).
# ============================================================================
import logging  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable  # noqa: E402

from .bybit import BybitMarketData  # noqa: E402
from .continuous_rebalance import (  # noqa: E402
    compute_continuous_rebalance_scale,
    ContinuousRebalanceResizePlan,
    ContinuousRebalanceRule,
    ContinuousRebalanceScaleState,
    plan_continuous_rebalance_resizes,
)
from .config import ResearchConfig  # noqa: E402
from .event_demo import (  # noqa: E402
    EventDemoCycleConfig,
    _active_position_by_symbol,
    _base36,
    _build_private_client,
    _contract_lookup,
    _decimal_text,
    _float,
    _kline_window,
    _live_open_order_symbols,
    _order_params,
    _price_lookup_from_tickers_and_klines,
    _private_credentials_present,
    _resolve_cycle_universe,
    _resolve_private_snapshot,
    resolve_snapshot_equity,
    _ratio_or_zero,
    _stop_price_for_entry,
    _trade_return,
    _upsert_rows,
    _utc_now_ms,
    _wait_for_execution_summary,
    _yyyymmddhhmmss,
    order_quantity_for_notional,
)
from .event_demo_data import _download_recent_1h_klines  # noqa: E402
from .event_demo_exits import _partial_exit_trade_update  # noqa: E402
from .storage import exclusive_file_lock, read_dataset, write_dataset  # noqa: E402
from . import cross_sleeve as _cross_sleeve  # noqa: E402

_logger = logging.getLogger(__name__)


def _open_continuous_trades(all_trades: pl.DataFrame, strategy_id: str) -> pl.DataFrame:
    if all_trades.is_empty():
        return all_trades
    return all_trades.filter(
        (pl.col("status") == "open") & (pl.col("strategy_id") == strategy_id)
    )


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _continuous_rebalance_scale_state_from_cycles(
    cycles: pl.DataFrame,
    *,
    current_day_ts: int,
) -> ContinuousRebalanceScaleState:
    """Reconstruct rebalance scale state from prior persisted cycle rows.

    The live/paper daily-rebalance rule must size from state known before the
    current rebalance day. Cycle rows may be written multiple times per day, so
    use the latest row per prior ``rebalance_day_ts`` and ignore current/future
    rows entirely.
    """
    if cycles.is_empty():
        return ContinuousRebalanceScaleState(prior_raw_returns=())

    current_day = (int(current_day_ts) // MS_PER_DAY) * MS_PER_DAY
    latest_by_day: dict[int, tuple[tuple[int, int], float, dict[str, Any]]] = {}
    for idx, row in enumerate(cycles.to_dicts()):
        day_value = _finite_or_none(row.get("rebalance_day_ts"))
        raw_value = _finite_or_none(row.get("rebalance_raw_return"))
        if day_value is None or raw_value is None:
            continue
        day = (int(day_value) // MS_PER_DAY) * MS_PER_DAY
        if day >= current_day:
            continue
        ts_value = _finite_or_none(row.get("ts_ms"))
        order_key = (int(ts_value) if ts_value is not None else 0, idx)
        previous = latest_by_day.get(day)
        if previous is None or order_key > previous[0]:
            latest_by_day[day] = (order_key, raw_value, row)

    if not latest_by_day:
        return ContinuousRebalanceScaleState(prior_raw_returns=())

    days = sorted(latest_by_day)
    raw_returns = tuple(latest_by_day[day][1] for day in days)
    latest_row = latest_by_day[days[-1]][2]
    equity = _finite_or_none(latest_row.get("rebalance_scaled_equity"))
    peak = _finite_or_none(latest_row.get("rebalance_scaled_peak"))
    prior_equity = equity if equity is not None and equity > 0.0 else 1.0
    prior_peak = peak if peak is not None and peak > 0.0 else max(prior_equity, 1.0)
    return ContinuousRebalanceScaleState(
        prior_raw_returns=raw_returns,
        prior_scaled_equity=prior_equity,
        prior_scaled_peak=max(prior_peak, prior_equity),
    )


def _continuous_rebalance_mark_prices_json(mark_prices: dict[str, float]) -> str:
    clean: dict[str, float] = {}
    for key, value in mark_prices.items():
        mark = _finite_or_none(value)
        if str(key) and mark is not None and mark > 0.0:
            clean[str(key)] = mark
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _continuous_rebalance_parse_mark_prices(value: Any) -> dict[str, float]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in parsed.items():
        mark = _finite_or_none(raw)
        if str(key) and mark is not None and mark > 0.0:
            out[str(key)] = mark
    return out


def _continuous_rebalance_previous_mark_state(
    cycles: pl.DataFrame,
    *,
    current_day_ts: int,
) -> tuple[int, dict[str, float], float]:
    if cycles.is_empty():
        return 0, {}, 1.0
    current_day = (int(current_day_ts) // MS_PER_DAY) * MS_PER_DAY
    best_key: tuple[int, int, int] | None = None
    best_day = 0
    best_marks: dict[str, float] = {}
    best_scale = 1.0
    for idx, row in enumerate(cycles.to_dicts()):
        day_value = _finite_or_none(row.get("rebalance_day_ts"))
        if day_value is None:
            continue
        day = (int(day_value) // MS_PER_DAY) * MS_PER_DAY
        if day >= current_day:
            continue
        marks = _continuous_rebalance_parse_mark_prices(row.get("rebalance_mark_prices_json"))
        if not marks:
            continue
        ts_value = _finite_or_none(row.get("ts_ms"))
        key = (day, int(ts_value) if ts_value is not None else 0, idx)
        if best_key is None or key > best_key:
            best_key = key
            best_day = day
            best_marks = marks
            scale = _finite_or_none(row.get("rebalance_target_scale"))
            best_scale = scale if scale is not None and scale > 0.0 else 1.0
    return best_day, best_marks, best_scale


def _continuous_rebalance_cycle_fields(
    all_trades: pl.DataFrame,
    cycles: pl.DataFrame,
    *,
    price_by_symbol: dict[str, float],
    current_day_ts: int,
    now_ms: int,
    strategy_id: str,
    rule: ContinuousRebalanceRule,
) -> dict[str, Any]:
    """Build the daily-rebalance mark/return fields for a cycle payload.

    The first row bootstraps marks with raw return 0. Later rows measure raw
    return from the latest prior-day mark to the current mark/exit, then update
    the scaled equity state that tomorrow's scale decision will read.
    """
    current_day = (int(current_day_ts) // MS_PER_DAY) * MS_PER_DAY
    prior_state = _continuous_rebalance_scale_state_from_cycles(cycles, current_day_ts=current_day)
    target_scale = compute_continuous_rebalance_scale(prior_state, rule)
    previous_day, previous_marks, previous_scale = _continuous_rebalance_previous_mark_state(
        cycles,
        current_day_ts=current_day,
    )
    has_previous_marks = bool(previous_marks)
    mark_prices: dict[str, float] = {}
    scaled_observed_return = 0.0
    marked_trades = 0
    missing_marks = 0
    closed_contributors = 0

    if not all_trades.is_empty():
        for row in all_trades.to_dicts():
            if str(row.get("strategy_id") or "") != strategy_id:
                continue
            trade_id = str(row.get("trade_id") or "")
            symbol = str(row.get("symbol") or "")
            qty = abs(_float(row.get("qty")))
            entry_price = _float(row.get("entry_price"))
            equity_usdt = _float(row.get("equity_usdt"))
            if not trade_id or not symbol or qty <= 0.0 or entry_price <= 0.0 or equity_usdt <= 0.0:
                continue
            entry_ts = int(_float(row.get("entry_ts_ms") or row.get("ts_ms") or 0))
            status = str(row.get("status") or "")
            previous_price = previous_marks.get(trade_id)
            if previous_price is None:
                if has_previous_marks and entry_ts > previous_day:
                    previous_price = entry_price
                else:
                    previous_price = entry_price
                    if has_previous_marks:
                        # Open legacy row without a prior mark: seed today's mark, do not invent a return.
                        previous_price = 0.0

            if status == "open":
                current_price = _float(price_by_symbol.get(symbol))
                if current_price <= 0.0:
                    missing_marks += 1
                    continue
                mark_prices[trade_id] = current_price
                marked_trades += 1
                if has_previous_marks and previous_price > 0.0:
                    scaled_observed_return += _trade_return(previous_price, current_price, side="short") * _ratio_or_zero(
                        qty * previous_price,
                        equity_usdt,
                    )
            else:
                exit_ts = int(_float(row.get("exit_ts_ms") or row.get("closed_at_ms") or 0))
                exit_price = _float(row.get("exit_price"))
                if not has_previous_marks or exit_ts <= previous_day or exit_ts > int(now_ms) or exit_price <= 0.0:
                    continue
                if previous_price <= 0.0:
                    previous_price = entry_price if entry_ts > previous_day else 0.0
                if previous_price <= 0.0:
                    continue
                scaled_observed_return += _trade_return(previous_price, exit_price, side="short") * _ratio_or_zero(
                    qty * previous_price,
                    equity_usdt,
                )
                closed_contributors += 1

    raw_return = scaled_observed_return / previous_scale if has_previous_marks and previous_scale > 0.0 else 0.0
    scaled_return = raw_return * target_scale
    scaled_equity = prior_state.prior_scaled_equity * (1.0 + scaled_return)
    scaled_peak = max(prior_state.prior_scaled_peak, scaled_equity)
    return {
        "rebalance_day_ts": current_day,
        "rebalance_raw_return": raw_return,
        "rebalance_target_scale": target_scale,
        "rebalance_scaled_return": scaled_return,
        "rebalance_scaled_equity": scaled_equity,
        "rebalance_scaled_peak": scaled_peak,
        "rebalance_mark_prices_json": _continuous_rebalance_mark_prices_json(mark_prices),
        "rebalance_marked_trades": marked_trades,
        "rebalance_missing_marks": missing_marks,
        "rebalance_closed_contributors": closed_contributors,
    }


def _continuous_rebalance_resize_checked_today(cycles: pl.DataFrame, *, current_day_ts: int) -> bool:
    if cycles.is_empty():
        return False
    current_day = (int(current_day_ts) // MS_PER_DAY) * MS_PER_DAY
    for row in cycles.to_dicts():
        day_value = _finite_or_none(row.get("rebalance_day_ts"))
        if day_value is None or (int(day_value) // MS_PER_DAY) * MS_PER_DAY != current_day:
            continue
        value = row.get("rebalance_resize_checked")
        if value is True:
            return True
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
    return False


def _open_primary_trades_for_addon_gate(
    *,
    current_root: Path,
    current_trades: pl.DataFrame,
    trades_dataset: str,
    primary_data_root: str | None,
    primary_strategy_id: str,
) -> list[dict[str, Any]]:
    """Open primary rows for the add-on PnL gate.

    If the primary root is the current root, reuse the caller's in-memory ledger
    so same-cycle exits already upserted into ``current_trades`` are respected.
    Otherwise read the configured primary root's matching paper/demo dataset.
    """
    primary_root = Path(primary_data_root).expanduser() if primary_data_root else current_root
    try:
        same_root = primary_root.resolve() == current_root.resolve()
    except OSError:
        same_root = False
    rows = current_trades if same_root else read_dataset(primary_root, trades_dataset)
    return _open_continuous_trades(rows, primary_strategy_id).to_dicts()


def _execute_continuous_exits(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: ContinuousDemoCycleConfig,
    now_ms: int,
    execution_event_router: Any | None = None,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Close a held SHORT (buy-to-cover reduce-only). Mirrors _execute_long_exits, side=Buy."""
    if not exits:
        return [], []
    lookup = {str(r["trade_id"]): r for r in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for plan in exits:
        trade = dict(lookup.get(str(plan.get("trade_id", "")), plan))
        symbol = str(plan["symbol"])
        qty = str(plan.get("qty") or trade.get("qty") or "")
        if not qty or _float(qty) <= 0.0:
            continue
        # Trade-id-hashed link: the snipe exit and its base exit run in the SAME cycle at the
        # SAME now_ms for the SAME symbol (and multi-component same-symbol trades exit together)
        # — a shared link cross-wires fill attribution (see _continuous_suborder_link_id).
        exit_link = _continuous_suborder_link_id(
            _continuous_exit_link_prefix(demo), symbol=symbol, signal_ts_ms=now_ms,
            trade_id=str(trade.get("trade_id") or plan.get("trade_id") or symbol),
        )
        order_result: dict[str, Any] = {}
        submit_mode, status, error = "dry_run", "planned", ""
        exit_price = exit_fee = 0.0
        exit_exec_time_ms = 0
        filled_qty = _float(qty)
        if demo.submit_orders:
            assert trading_client is not None
            if record_preflight is not None:
                record_preflight({"order_link_id": exit_link, "ts_ms": now_ms, "trade_id": str(trade.get("trade_id", "")),
                                  "symbol": symbol, "side": "Buy", "qty": qty, "reduce_only": True,
                                  "submit_mode": "preflight", "status": "submitted", "trade_side": "short",
                                  "sleeve": str(trade.get("sleeve") or continuous_sleeve_name(demo))})
            try:
                order_result = trading_client.place_order(**_order_params(
                    symbol=symbol, side="Buy", qty=qty, order_type=demo.exit_order_type,
                    order_link_id=exit_link, reduce_only=True))
                submit_mode = "submitted"
            except Exception as exc:  # noqa: BLE001
                submit_mode, status, error, filled_qty = "error", "failed", f"place_order failed: {exc}"[:500], 0.0
            if submit_mode == "submitted":
                try:
                    summ = _wait_for_execution_summary(
                        trading_client, symbol=symbol, order_link_id=exit_link,
                        poll_seconds=demo.order_fill_confirm_seconds,
                        poll_interval_seconds=demo.order_fill_poll_interval_seconds,
                        fast_poll_interval_seconds=demo.order_fill_fast_poll_interval_seconds,
                        fast_poll_seconds=demo.order_fill_fast_poll_seconds,
                        execution_event_router=execution_event_router,
                        target_qty=_float(qty))
                except Exception as exc:  # noqa: BLE001
                    status, error, filled_qty = "submitted_unconfirmed", f"fill confirm failed: {exc}"[:500], 0.0
                else:
                    filled_qty = _float(summ.get("qty"))
                    exit_price = _float(summ.get("avg_price"))
                    exit_fee = _float(summ.get("fee"))
                    exit_exec_time_ms = int(_float(summ.get("exec_time_ms") or 0))
                    tgt = _float(qty)
                    status = "filled" if (tgt > 0 and filled_qty + max(tgt * 1e-8, 1e-12) >= tgt) else ("partial" if filled_qty > 0 else "submitted_unconfirmed")
                finally:
                    # Router contract: drop the WS buffer once this link is reconciled
                    # (or given up on) — never leave rows a later link could inherit.
                    if execution_event_router is not None:
                        execution_event_router.clear(exit_link)
        if not demo.submit_orders or filled_qty > 0.0 or status == "submitted_unconfirmed":
            upd = dict(trade)
            if status == "filled" or not demo.submit_orders:
                final_exit = exit_price or _float(trade.get("entry_price"))
                gtr = _trade_return(_float(trade.get("entry_price")), final_exit, side="short")
                nw = _ratio_or_zero(trade.get("notional_usdt"), trade.get("equity_usdt"))
                upd.update({"status": "closed", "exit_ts_ms": now_ms, "exit_price": final_exit,
                            "exit_fee_usdt": exit_fee, "exit_exec_time_ms": exit_exec_time_ms,
                            "gross_trade_return": gtr, "net_return": gtr * nw,
                            "exit_reason": str(plan.get("exit_reason", "left_decile")),
                            "exit_order_link_id": exit_link, "exit_order_id": order_result.get("orderId", ""),
                            "submit_mode": submit_mode, "closed_at_ms": now_ms, "updated_at_ms": now_ms})
            else:
                if status == "partial" and filled_qty > 0.0:
                    # Book the filled leg NOW (qty -= filled_qty + partial_exit_* stamp).
                    # ws_risk's reduce branch deducts only fills BEYOND the order row's
                    # recorded filled_qty, so an unbooked first leg overstated the open
                    # short by that leg until final close (audit 2026-06-11).
                    upd = _partial_exit_trade_update(
                        upd, plan, filled_qty=filled_qty, exit_price=exit_price,
                        order_link_id=exit_link, order_id=str(order_result.get("orderId", "")),
                        now_ms=now_ms,
                    )
                upd.update({"exit_order_link_id": exit_link, "submit_mode": submit_mode, "updated_at_ms": now_ms})
            rows.append(upd)
        order_rows.append({"order_link_id": exit_link, "ts_ms": now_ms, "trade_id": str(trade.get("trade_id", "")),
                           "symbol": symbol, "side": "Buy", "order_type": demo.exit_order_type, "qty": qty,
                           "reduce_only": True, "order_id": order_result.get("orderId", ""), "submit_mode": submit_mode,
                           "avg_price": exit_price, "fee_usdt": exit_fee, "exec_time_ms": exit_exec_time_ms,
                           "status": status, "trade_side": "short", "exit_reason": str(plan.get("exit_reason", "left_decile")),
                           "filled_qty": str(filled_qty) if filled_qty > 0 else "", "error": error,
                           "sleeve": str(trade.get("sleeve") or continuous_sleeve_name(demo))})
    return rows, order_rows


def _build_continuous_rebalance_resize_rows(
    plans: list[ContinuousRebalanceResizePlan],
    all_trades: pl.DataFrame,
    *,
    demo: ContinuousDemoCycleConfig,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    execution_by_trade_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build dry-run/paper ledger rows for daily-rebalance resize intents.

    This bridges the research rebalance planner into the paper/demo ledger
    vocabulary without submitting orders. Live submission must call the venue
    router and confirm fills before using the same ledger transforms.
    """
    if not plans or all_trades.is_empty():
        return [], []

    lookup = {str(r.get("trade_id") or ""): r for r in all_trades.to_dicts()}
    trade_rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []

    for plan in plans:
        trade = dict(lookup.get(str(plan.trade_id), {}))
        if not trade:
            continue
        symbol = str(plan.symbol or trade.get("symbol") or "")
        execution = (execution_by_trade_id or {}).get(str(plan.trade_id), {})
        price = _float(execution.get("avg_price")) or _float(price_by_symbol.get(symbol))
        entry_price = _float(trade.get("entry_price"))
        current_qty = abs(_float(trade.get("qty")))
        equity_usdt = _float(trade.get("equity_usdt"))
        if not symbol or price <= 0.0 or entry_price <= 0.0 or current_qty <= 0.0:
            continue

        contract = contract_by_symbol.get(symbol, {})
        qty_step = _float(contract.get("qty_step")) or _float(trade.get("qty_step")) or 0.001
        if execution:
            order_qty = abs(_float(execution.get("qty")))
        else:
            max_qty = _float(contract.get("max_market_order_qty")) or _float(contract.get("max_order_qty"))
            quantity = order_quantity_for_notional(
                notional_usdt=abs(plan.delta_notional_usdt),
                price=price,
                qty_step=qty_step,
                min_order_qty=_float(contract.get("min_order_qty")),
                min_notional_value=_float(contract.get("min_notional_value")),
                max_order_qty=max_qty,
            )
            if quantity is None:
                continue

            raw_qty_text, _actual_notional = quantity
            order_qty = _float(raw_qty_text)
        if plan.reduce_only:
            order_qty = min(order_qty, current_qty)
        if order_qty <= 0.0:
            continue

        order_qty_text = _decimal_text(Decimal(str(order_qty)))
        order_notional = order_qty * price
        link_prefix = _continuous_exit_link_prefix(demo) if plan.reduce_only else _continuous_entry_link_prefix(demo)
        order_link = str(execution.get("order_link_id") or _continuous_order_link_id(link_prefix, symbol=symbol, signal_ts_ms=now_ms))
        sleeve = str(trade.get("sleeve") or continuous_sleeve_name(demo))
        resize_reason = plan.reason or ("rebalance_reduce" if plan.reduce_only else "rebalance_increase")
        submit_mode = str(execution.get("submit_mode") or "dry_run")
        order_status = str(execution.get("status") or "planned")
        fee_usdt = _float(execution.get("fee_usdt"))
        exec_time_ms = int(_float(execution.get("exec_time_ms") or 0))
        order_id = str(execution.get("order_id") or "")
        error = str(execution.get("error") or "")

        upd = dict(trade)
        prior_realized = _float(trade.get("rebalance_realized_return"))
        if plan.reduce_only:
            realized_gross = _trade_return(entry_price, price, side="short")
            realized_weight = _ratio_or_zero(order_qty * entry_price, equity_usdt)
            realized_delta = realized_gross * realized_weight
            total_realized = prior_realized + realized_delta
            remaining_qty = max(current_qty - order_qty, 0.0)
            if remaining_qty <= qty_step * 0.5:
                upd.update(
                    {
                        "status": "closed",
                        "qty": "0",
                        "notional_usdt": 0.0,
                        "exit_ts_ms": now_ms,
                        "exit_price": price,
                        "exit_fee_usdt": fee_usdt,
                        "exit_exec_time_ms": exec_time_ms,
                        "gross_trade_return": realized_gross,
                        "net_return": total_realized,
                        "rebalance_realized_return": total_realized,
                        "exit_reason": "rebalance_zero",
                        "exit_order_link_id": order_link,
                        "exit_order_id": order_id,
                        "submit_mode": submit_mode,
                        "closed_at_ms": now_ms,
                        "updated_at_ms": now_ms,
                    }
                )
            else:
                upd.update(
                    {
                        "qty": _decimal_text(Decimal(str(remaining_qty))),
                        "notional_usdt": remaining_qty * entry_price,
                        "rebalance_realized_return": total_realized,
                        "last_rebalance_ts_ms": now_ms,
                        "last_rebalance_price": price,
                        "last_rebalance_reason": resize_reason,
                        "last_rebalance_order_link_id": order_link,
                        "last_rebalance_fee_usdt": fee_usdt,
                        "submit_mode": submit_mode,
                        "updated_at_ms": now_ms,
                    }
                )
        else:
            add_qty = order_qty
            new_qty = current_qty + add_qty
            old_cost_basis = current_qty * entry_price
            add_cost_basis = add_qty * price
            new_entry = (old_cost_basis + add_cost_basis) / new_qty
            upd.update(
                {
                    "qty": _decimal_text(Decimal(str(new_qty))),
                    "entry_price": new_entry,
                    "notional_usdt": old_cost_basis + add_cost_basis,
                    "last_rebalance_ts_ms": now_ms,
                    "last_rebalance_price": price,
                    "last_rebalance_reason": resize_reason,
                    "last_rebalance_order_link_id": order_link,
                    "last_rebalance_fee_usdt": fee_usdt,
                    "submit_mode": submit_mode,
                    "updated_at_ms": now_ms,
                }
            )

        trade_rows.append(upd)
        order_rows.append(
            {
                "order_link_id": order_link,
                "ts_ms": now_ms,
                "trade_id": str(trade.get("trade_id", "")),
                "strategy_id": strategy_id,
                "symbol": symbol,
                "side": plan.side,
                "order_type": "Market",
                "qty": order_qty_text,
                "reduce_only": bool(plan.reduce_only),
                "order_id": order_id,
                "submit_mode": submit_mode,
                "avg_price": price,
                "fee_usdt": fee_usdt,
                "exec_time_ms": exec_time_ms,
                "notional_usdt": order_notional,
                "status": order_status,
                "trade_side": "short",
                "signal_ts_ms": now_ms,
                "equity_usdt": equity_usdt,
                "qty_step": qty_step,
                "resize_reason": resize_reason,
                "target_notional_usdt": plan.target_notional_usdt,
                "current_notional_usdt": plan.current_notional_usdt,
                "delta_notional_usdt": plan.delta_notional_usdt,
                "filled_qty": order_qty_text,
                "error": error,
                "sleeve": sleeve,
            }
        )

    return trade_rows, order_rows


def _execute_continuous_rebalance_resizes(
    plans: list[ContinuousRebalanceResizePlan],
    all_trades: pl.DataFrame,
    *,
    trading_client: Any | None,
    demo: ContinuousDemoCycleConfig,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    execution_event_router: Any | None = None,
    record_preflight: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not plans:
        return [], []
    if not demo.submit_orders:
        return _build_continuous_rebalance_resize_rows(
            plans,
            all_trades,
            demo=demo,
            price_by_symbol=price_by_symbol,
            contract_by_symbol=contract_by_symbol,
            now_ms=now_ms,
            strategy_id=strategy_id,
        )

    assert trading_client is not None
    execution_by_trade_id: dict[str, dict[str, Any]] = {}
    failed_orders: list[dict[str, Any]] = []
    lookup = {str(r.get("trade_id") or ""): r for r in all_trades.to_dicts()} if not all_trades.is_empty() else {}
    for plan in plans:
        trade = dict(lookup.get(str(plan.trade_id), {}))
        symbol = str(plan.symbol or trade.get("symbol") or "")
        price = _float(price_by_symbol.get(symbol))
        if not trade or not symbol or price <= 0.0:
            continue
        contract = contract_by_symbol.get(symbol, {})
        qty_step = _float(contract.get("qty_step")) or _float(trade.get("qty_step")) or 0.001
        max_qty = _float(contract.get("max_market_order_qty")) or _float(contract.get("max_order_qty"))
        quantity = order_quantity_for_notional(
            notional_usdt=abs(plan.delta_notional_usdt),
            price=price,
            qty_step=qty_step,
            min_order_qty=_float(contract.get("min_order_qty")),
            min_notional_value=_float(contract.get("min_notional_value")),
            max_order_qty=max_qty,
        )
        if quantity is None:
            continue
        qty, planned_notional = quantity
        current_qty = abs(_float(trade.get("qty")))
        if plan.reduce_only and _float(qty) > current_qty:
            qty = _decimal_text(Decimal(str(current_qty)))
            planned_notional = current_qty * price
        if _float(qty) <= 0.0:
            continue

        link_prefix = _continuous_exit_link_prefix(demo) if plan.reduce_only else _continuous_entry_link_prefix(demo)
        # Trade-id-hashed link: multi-component same-symbol trades resize in the same cycle at
        # the same now_ms — a shared link cross-wires fill attribution; the increase side uses
        # the ENTRY prefix and decode_entry_order_link_id strips the hash suffix.
        order_link = _continuous_suborder_link_id(
            link_prefix, symbol=symbol, signal_ts_ms=now_ms, trade_id=str(plan.trade_id)
        )
        preflight = {
            "order_link_id": order_link,
            "ts_ms": now_ms,
            "trade_id": str(plan.trade_id),
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": plan.side,
            "order_type": "Market",
            "qty": qty,
            "reduce_only": bool(plan.reduce_only),
            "submit_mode": "preflight",
            "status": "submitted",
            "trade_side": "short",
            "signal_ts_ms": now_ms,
            "notional_usdt": planned_notional,
            "resize_reason": plan.reason,
            "sleeve": str(trade.get("sleeve") or continuous_sleeve_name(demo)),
        }
        if record_preflight is not None:
            record_preflight(preflight)

        order_result: dict[str, Any] = {}
        submit_mode = "submitted"
        status = "submitted_unconfirmed"
        error = ""
        filled_qty = 0.0
        avg_price = price
        fee_usdt = 0.0
        exec_time_ms = 0
        try:
            order_result = trading_client.place_order(
                **_order_params(
                    symbol=symbol,
                    side=plan.side,
                    qty=qty,
                    order_type="Market",
                    order_link_id=order_link,
                    reduce_only=bool(plan.reduce_only),
                )
            )
        except Exception as exc:  # noqa: BLE001
            submit_mode, status, error = "error", "failed", f"place_order failed: {exc}"[:500]
        else:
            try:
                summ = _wait_for_execution_summary(
                    trading_client,
                    symbol=symbol,
                    order_link_id=order_link,
                    poll_seconds=demo.order_fill_confirm_seconds,
                    poll_interval_seconds=demo.order_fill_poll_interval_seconds,
                    fast_poll_interval_seconds=demo.order_fill_fast_poll_interval_seconds,
                    fast_poll_seconds=demo.order_fill_fast_poll_seconds,
                    execution_event_router=execution_event_router,
                    target_qty=_float(qty),
                )
            except Exception as exc:  # noqa: BLE001
                error = f"fill confirm failed: {exc}"[:500]
            else:
                filled_qty = _float(summ.get("qty"))
                avg_price = _float(summ.get("avg_price")) or price
                fee_usdt = _float(summ.get("fee"))
                exec_time_ms = int(_float(summ.get("exec_time_ms") or 0))
                target_qty = _float(qty)
                status = (
                    "filled"
                    if target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty
                    else ("partial" if filled_qty > 0.0 else "submitted_unconfirmed")
                )
            finally:
                # Router contract: drop the reconciled link's WS buffer (see exits).
                if execution_event_router is not None:
                    execution_event_router.clear(order_link)

        execution_row = {
            "order_link_id": order_link,
            "qty": filled_qty,
            "avg_price": avg_price,
            "fee_usdt": fee_usdt,
            "exec_time_ms": exec_time_ms,
            "order_id": order_result.get("orderId", ""),
            "submit_mode": submit_mode,
            "status": status,
            "error": error,
        }
        if filled_qty > 0.0:
            execution_by_trade_id[str(plan.trade_id)] = execution_row
        else:
            failed_orders.append(
                {
                    **preflight,
                    "order_id": order_result.get("orderId", ""),
                    "submit_mode": submit_mode,
                    "status": status,
                    "avg_price": avg_price if status != "failed" else 0.0,
                    "fee_usdt": fee_usdt,
                    "exec_time_ms": exec_time_ms,
                    "filled_qty": "",
                    "error": error,
                }
            )

    filled_plan_ids = set(execution_by_trade_id)
    filled_plans = [plan for plan in plans if str(plan.trade_id) in filled_plan_ids]
    rows, orders = _build_continuous_rebalance_resize_rows(
        filled_plans,
        all_trades,
        demo=demo,
        price_by_symbol=price_by_symbol,
        contract_by_symbol=contract_by_symbol,
        now_ms=now_ms,
        strategy_id=strategy_id,
        execution_by_trade_id=execution_by_trade_id,
    )
    return rows, orders + failed_orders


def _execute_continuous_entries(
    candidates: list[dict[str, Any]],
    *,
    trading_client: Any | None,
    demo: ContinuousDemoCycleConfig,
    equity_usdt: float,
    order_notional_frac: float,
    price_by_symbol: dict[str, float],
    contract_by_symbol: dict[str, dict[str, Any]],
    now_ms: int,
    strategy_id: str,
    record_preflight: Callable[[dict[str, Any]], None] | None,
    execution_event_router: Any | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Open a SHORT (Sell, stop ABOVE entry). Mirrors _execute_single_long_entry, side=Sell."""
    rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    for cand in candidates:
        symbol = str(cand["symbol"])
        price = price_by_symbol.get(symbol, _float(cand.get("live_price")))
        contract = contract_by_symbol.get(symbol, {})
        if price <= 0.0:
            continue
        tick_size = _float(contract.get("tick_size")) or 0.0001
        qty_step = _float(contract.get("qty_step")) or 0.001
        component = str(cand.get("component") or "")
        component_weight = _float(cand.get("component_weight")) or 1.0
        capped_notional = equity_usdt * demo.wallet_balance_fraction * order_notional_frac * component_weight
        max_qty = _float(contract.get("max_market_order_qty")) or _float(contract.get("max_order_qty"))
        # NOTE (EXEC-3): like the short/long ENTRY paths, a single entry whose qty exceeds Bybit's
        # per-order maxMktOrderQty is CAPPED here (order_quantity_for_notional floors to max_qty), NOT
        # split into child orders. At 2% per-name notional the cap effectively never binds for the
        # liquid (>=$500k/h) names this fade trades, so the live-vs-backtest size gap is nil in practice;
        # the backtest models no per-order cap. If a cheap-priced name ever does bind it, the position is
        # silently under-sized vs backtest -- mirror ws_risk's exit-side _split_qty_for_max_order_size
        # into a multi-child entry here (one trade row, N sub-orders) before relying on the cap as alpha.
        quantity = order_quantity_for_notional(
            notional_usdt=capped_notional, price=price, qty_step=qty_step,
            min_order_qty=_float(contract.get("min_order_qty")),
            min_notional_value=_float(contract.get("min_notional_value")), max_order_qty=max_qty)
        if quantity is None:
            _logger.info("continuous entry sizing rejected symbol=%s notional=%.2f price=%.6g", symbol, capped_notional, price)
            continue
        qty, actual_notional = quantity
        stop_loss_pct = _float(cand.get("stop_loss_pct"))
        stop_price = _stop_price_for_entry(entry_price=price, side="short", stop_loss_pct=stop_loss_pct, tick_size=tick_size)
        take_profit_pct = _float(cand.get("take_profit_pct"))
        take_profit_price = 0.0
        if take_profit_pct > 0.0:
            raw_tp = price * (1.0 - take_profit_pct)
            take_profit_price = (
                float(Decimal(str(round(raw_tp / tick_size))) * Decimal(str(tick_size))) if tick_size > 0 else raw_tp
            )
        signal_ts_ms = int(cand.get("signal_ts_ms") or now_ms)
        reentry_seq = int(cand.get("reentry_seq") or 0)
        trade_id = _continuous_trade_id(strategy_id, symbol, signal_ts_ms, reentry_seq)
        link_prefix = _continuous_entry_link_prefix(demo)
        if component:
            trade_id = f"{trade_id}-{component}"
            link_prefix = f"{link_prefix}{component}"
        entry_link = _continuous_order_link_id(
            link_prefix, symbol=symbol, signal_ts_ms=signal_ts_ms, reentry_seq=reentry_seq
        )
        order_result: dict[str, Any] = {}
        submit_mode, order_status, error = "dry_run", "planned", ""
        filled_qty = _float(qty)
        entry_price = price
        filled_notional = actual_notional
        entry_fee = 0.0
        entry_exec_time_ms = 0
        if demo.submit_orders:
            assert trading_client is not None
            try:
                trading_client.set_leverage(symbol=symbol, buy_leverage=demo.entry_leverage, sell_leverage=demo.entry_leverage)
            except Exception as exc:  # noqa: BLE001
                submit_mode, order_status, error, filled_qty, filled_notional = "error", "failed", f"set_leverage failed: {exc}"[:500], 0.0, 0.0
            if not error:
                if record_preflight is not None:
                    record_preflight({"order_link_id": entry_link, "ts_ms": now_ms, "trade_id": trade_id,
                                      "strategy_id": strategy_id, "symbol": symbol, "side": "Sell", "qty": qty, "reduce_only": False,
                                      "submit_mode": "preflight", "status": "submitted", "trade_side": "short",
                                      "sleeve": continuous_sleeve_name(demo),
                                      "signal_ts_ms": signal_ts_ms, "stop_price": stop_price})
                try:
                    order_result = trading_client.place_order(**_order_params(
                        symbol=symbol, side="Sell", qty=qty, order_type=demo.entry_order_type,
                        order_link_id=entry_link, reduce_only=False, stop_loss=stop_price if stop_price > 0 else None,
                        take_profit=take_profit_price if take_profit_price > 0 else None))
                    submit_mode = "submitted"
                except Exception as exc:  # noqa: BLE001
                    submit_mode, order_status, error, filled_qty, filled_notional = "error", "submitted_unconfirmed", f"place_order failed: {exc}"[:500], 0.0, 0.0
            if submit_mode == "submitted":
                try:
                    summ = _wait_for_execution_summary(
                        trading_client, symbol=symbol, order_link_id=entry_link,
                        poll_seconds=demo.order_fill_confirm_seconds, poll_interval_seconds=demo.order_fill_poll_interval_seconds,
                        fast_poll_interval_seconds=demo.order_fill_fast_poll_interval_seconds,
                        fast_poll_seconds=demo.order_fill_fast_poll_seconds, execution_event_router=execution_event_router,
                        target_qty=_float(qty))
                except Exception as exc:  # noqa: BLE001
                    order_status, error, filled_qty, filled_notional = "submitted_unconfirmed", f"fill confirm failed: {exc}"[:500], 0.0, 0.0
                else:
                    filled_qty = _float(summ.get("qty"))
                    entry_price = _float(summ.get("avg_price")) or price
                    entry_fee = _float(summ.get("fee"))
                    entry_exec_time_ms = int(_float(summ.get("exec_time_ms") or 0))
                    filled_notional = abs(entry_price * filled_qty) if filled_qty > 0 else 0.0
                    tgt = _float(qty)
                    order_status = "filled" if (tgt > 0 and filled_qty + max(tgt * 1e-8, 1e-12) >= tgt) else ("partial" if filled_qty > 0 else "submitted_unconfirmed")
                finally:
                    # Router contract: drop the reconciled link's WS buffer (see exits).
                    if execution_event_router is not None:
                        execution_event_router.clear(entry_link)
        entry_qty = _decimal_text(Decimal(str(filled_qty))) if filled_qty > 0 else ""
        if not demo.submit_orders or filled_qty > 0.0:
            # trade_id (computed above via _continuous_trade_id) carries the re-entry seq so a same-
            # signal-window cover-then-re-enter gets a DISTINCT id+orderLinkId — without it, storage's
            # trade_id dedup would silently overwrite the closed row and Bybit would reject the dup link
            # (continuous-2 ledger data loss). seq=0 is byte-identical to the legacy form.
            rows.append({
                # Tag the sleeve explicitly (the exit row below inherits it via
                # dict(trade)). ws_risk backfills sleeve by root on read, but a
                # self-identifying row is robust if it is ever routed via a path
                # that doesn't backfill -- keeps the continuous ledger isolated.
                "trade_id": trade_id, "strategy_id": strategy_id, "symbol": symbol, "side": "short",
                "sleeve": continuous_sleeve_name(demo),
                "status": "open", "ts_ms": now_ms, "entry_ts_ms": now_ms, "opened_at_ms": now_ms, "updated_at_ms": now_ms,
                "signal_ts_ms": signal_ts_ms, "entry_price": entry_price, "qty": entry_qty or qty,
                "notional_usdt": filled_notional if demo.submit_orders else actual_notional, "equity_usdt": equity_usdt,
                "entry_leverage": demo.entry_leverage, "tick_size": tick_size, "qty_step": qty_step,
                "max_market_order_qty": max_qty, "stop_price": stop_price, "stop_loss_pct": stop_loss_pct,
                "take_profit_price": take_profit_price, "take_profit_pct": take_profit_pct,
                "component": component, "component_weight": component_weight,
                "entry_fee_usdt": entry_fee, "entry_exec_time_ms": entry_exec_time_ms,
                "decile": int(cand.get("decile") or 0), "composite": _float(cand.get("composite")),
                "entry_order_link_id": entry_link, "entry_order_id": order_result.get("orderId", ""), "submit_mode": submit_mode,
            })
        order_rows.append({
            "order_link_id": entry_link, "ts_ms": now_ms, "trade_id": trade_id,
            "strategy_id": strategy_id, "symbol": symbol, "side": "Sell", "order_type": demo.entry_order_type,
            "qty": qty, "reduce_only": False, "order_id": order_result.get("orderId", ""), "submit_mode": submit_mode,
            "avg_price": entry_price if filled_qty > 0 else 0.0, "fee_usdt": entry_fee, "exec_time_ms": entry_exec_time_ms,
            "notional_usdt": filled_notional, "status": order_status, "trade_side": "short", "signal_ts_ms": signal_ts_ms,
            "equity_usdt": equity_usdt, "tick_size": tick_size, "qty_step": qty_step, "stop_price": stop_price,
            "stop_loss_pct": stop_loss_pct, "error": error, "sleeve": continuous_sleeve_name(demo),
            # filled_qty/target_qty: ws_risk's pending-fill reconciler delta-adds
            # (venue cumulative − filled_qty); without filled_qty a "partial" entry's
            # already-booked leg double-added on the next reconcile (audit 2026-06-11).
            "filled_qty": str(filled_qty) if filled_qty > 0 else "", "target_qty": qty,
        })
    return rows, order_rows


def _signal_source_root(demo: ContinuousDemoCycleConfig, root: Path) -> Path:
    """Root serving the cycle's derived signal inputs (the rmom gate parquet).

    A follower sleeve (``klines_follow_root`` set — the paper shadow co-located with
    the demo sleeve) reads klines from the leader's store snapshot, so the rmom gate
    must come from the SAME root: it is derived purely from those klines, and reading
    the leader's copy keeps both sleeves deciding on identical inputs (and lets the
    follower's own dormant store skip the daily rmom rebuild entirely)."""
    return Path(demo.klines_follow_root).expanduser() if demo.klines_follow_root else root


def _load_rmom_table(root: Path) -> pl.DataFrame | None:
    path = root / "residual_momentum.parquet"
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path).rename({"ts_ms": "day_ts"})
    except Exception as exc:  # noqa: BLE001 - a corrupt rmom file must DEGRADE, not crash the cycle
        # A partially-written/corrupt parquet (mid-write crash, disk issue) or a
        # schema drift (missing ts_ms) would otherwise raise out of the cycle,
        # writing NO cycle row -- so the persisted max_rmom_day_ts telemetry never
        # updates and the watchdog cannot tell "cycle crashing on a bad rmom file"
        # from "merely quiet". Degrade to rmom-absent (the watchdog already pages
        # on the silent-blackout that produces), keeping the staleness guard
        # authoritative even on a corrupted file.
        _logger.warning("continuous: failed to load rmom table %s: %s; degrading to rmom-absent", path, exc)
        return None


def format_continuous_demo_cycle_summary(payload: dict[str, Any]) -> str:
    """Pretty-print a continuous-fade cycle payload (a FLAT dict) for stdout/journald. The continuous
    daemon subclasses the long daemon, whose `format_long_demo_cycle_summary` expects `payload['cycle']`;
    feeding it the flat continuous payload KeyError'd every cycle (audit 2026-06-02). This is the
    continuous-shaped override so the cycle summary prints (incl. the rmom-gate freshness)."""
    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return (
        "continuous-fade demo cycle "
        f"id={payload.get('cycle_id', '')} mode={payload.get('mode')} "
        f"symbols={payload.get('universe_symbols')} "
        f"rmom={'present' if payload.get('rmom_present') else 'MISSING'} "
        f"max_rmom_day_ts={payload.get('max_rmom_day_ts')} stale_days={payload.get('rmom_stale_days')} "
        f"d9={payload.get('live_d9_symbols')} cand={payload.get('candidates')} "
        f"entries={payload.get('entries')} exits={payload.get('exits')} open={payload.get('open_positions')} "
        f"sniper={payload.get('sniper_fills', 0)}/{payload.get('sniper_exits', 0)}/{payload.get('sniper_errors', 0)} "
        f"equity=${_f(payload.get('equity_usdt')):,.2f} paused={payload.get('entry_paused')} "
        f"same_signal_reentry_skips={payload.get('skipped_same_signal_reentry', 0)} "
        f"addon_entry_cooldown_symbols={payload.get('addon_same_symbol_entry_cooldown_symbols', 0)} "
        f"addon_pnl_gate_skips={payload.get('addon_primary_pnl_gate_skips', 0)} "
        # telegram OUTCOME on the journald line: the daemon prints this summary after the
        # cycle returns (telegram_sent/telegram_error are set by then) — a persistently
        # broken notify path was previously indistinguishable from a quiet book here
        # (audit 2026-06-12; the long sleeve and ws_risk persist the same fields).
        f"telegram={'sent' if payload.get('telegram_sent') else str(payload.get('telegram_error') or 'none')[:60]}"
    )


def _continuous_telegram_reason(
    payload: dict[str, Any],
    entry_rows: list[dict[str, Any]],
    exit_rows: list[dict[str, Any]],
) -> str:
    """Material-event filter for the continuous per-cycle telegram — quiet cycles send nothing.
    Mirrors long_native_event_demo._long_telegram_reason."""
    if any(
        str(r.get("submit_mode", "")) == "error" or str(r.get("status", "")) == "failed"
        for r in entry_rows
    ):
        return "continuous_entry_error"
    if any(str(r.get("submit_mode", "")) == "error" for r in exit_rows):
        return "continuous_exit_error"
    # Sniper lifecycle (payload counts — the rows live in separate lists): a snipe fill
    # is a NEW live short, an error is a stuck/ghost resting order. Until 2026-06-12
    # the armed sniper was invisible to this filter entirely.
    if int(payload.get("sniper_errors") or 0) > 0:
        return "continuous_sniper_error"
    if int(payload.get("sniper_fills") or 0) > 0:
        return "continuous_sniper_fill"
    if int(payload.get("entries") or 0) > 0:
        return "continuous_entry_executed"
    if int(payload.get("exits") or 0) > 0 or int(payload.get("sniper_exits") or 0) > 0:
        return "continuous_exit_executed"
    return ""


def format_continuous_telegram_status_message(
    payload: dict[str, Any],
    entry_rows: list[dict[str, Any]],
    exit_rows: list[dict[str, Any]],
    *,
    reason: str,
) -> str:
    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    lines = [
        "[Continuous sleeve] Bybit demo",
        f"reason={reason}",
        f"mode={payload.get('mode')} equity=${_f(payload.get('equity_usdt')):,.2f}",
        f"entries={payload.get('entries')} exits={payload.get('exits')} "
        f"open={payload.get('open_positions')} cand={payload.get('candidates')}",
        f"rmom={'present' if payload.get('rmom_present') else 'MISSING'} "
        f"paused={payload.get('entry_paused')}",
    ]
    sniper_counts = (
        int(payload.get("sniper_fills") or 0),
        int(payload.get("sniper_exits") or 0),
        int(payload.get("sniper_errors") or 0),
    )
    if any(sniper_counts):
        lines.append(
            f"sniper: fills={sniper_counts[0]} exits={sniper_counts[1]} errors={sniper_counts[2]}"
        )
    if entry_rows:
        lines.append("Entries:")
        for row in entry_rows[:6]:
            lines.append(
                f"- {row.get('symbol', '')} qty={_f(row.get('qty')):g} "
                f"@${_f(row.get('entry_price')):.6g} status={row.get('status', '')}"
            )
    if exit_rows:
        lines.append("Exits:")
        for row in exit_rows[:6]:
            lines.append(
                f"- {row.get('symbol', '')} reason={row.get('exit_reason', '')} "
                f"@${_f(row.get('exit_price')):.6g} mode={row.get('submit_mode', '')}"
            )
    return "\n".join(lines)[:3900]


def _maybe_continuous_notify(
    payload: dict[str, Any],
    entry_rows: list[dict[str, Any]],
    exit_rows: list[dict[str, Any]],
    *,
    enabled: bool,
) -> tuple[bool, str]:
    """Per-cycle telegram for the LIVE order-submitting continuous sleeve. Until 2026-06-11
    this sleeve sent NO trade notifications at all despite TELEGRAM_ENABLED=1 in its unit
    (only ws_risk's exit-class alerts fired). Fully exception-isolated — the formatter runs
    INSIDE the try so a malformed payload can never break the order path (EVE-2 pattern)."""
    if not enabled:
        return False, "disabled"
    try:
        reason = _continuous_telegram_reason(payload, entry_rows, exit_rows)
        if not reason:
            return False, "quiet_no_material_event"
        text = format_continuous_telegram_status_message(
            payload, entry_rows, exit_rows, reason=reason
        )
        from .telegram import send_telegram_message

        sent = send_telegram_message(text, enabled=True)
    except Exception as exc:  # noqa: BLE001 — telegram must never break the cycle
        return False, str(exc)[:500]
    if not sent:
        return False, "telegram env missing or Telegram API returned false"
    return True, ""


def _validate_continuous_demo_config(config: ContinuousDemoCycleConfig) -> None:
    """Guard the only continuous order-submitting path: explicit confirm flag + demo
    account, and refuse the paper_mode/submit_orders corruption combos. Mirrors
    long_native_event_demo._validate_long_demo_config — this sleeve was the one live
    order-submitter missing the repo's money-safety invariant (audit 2026-06-02 #1/#9)."""
    if config.paper_mode and config.submit_orders:
        raise ValueError("paper_mode=True is incompatible with submit_orders=True")
    if config.paper_mode and not config.record_dry_run:
        raise ValueError("paper_mode=True requires record_dry_run=True so the paper ledger is written")
    if config.daily_rebalance_enabled and not config.submit_orders and not config.record_dry_run:
        raise ValueError("daily_rebalance_enabled requires record_dry_run=True so resize rows are persisted")
    if config.strategy_profile not in CONTINUOUS_DEMO_PROFILES:
        raise ValueError(f"unknown continuous strategy_profile {config.strategy_profile!r}")
    if config.btc_trend_gate not in ("off", "uptrend", "downtrend"):
        raise ValueError(
            f"btc_trend_gate must be 'off', 'uptrend', or 'downtrend'; got {config.btc_trend_gate!r}"
        )
    if not config.feature_set:
        raise ValueError("feature_set must contain at least one causal feature")
    if config.addon_primary_pnl_gate and config.submit_orders:
        if config.strategy_profile != "continuous_addon_v1":
            raise ValueError("addon_primary_pnl_gate submit requires strategy_profile='continuous_addon_v1'")
        if not config.addon_primary_data_root:
            raise ValueError("addon_primary_pnl_gate submit requires addon_primary_data_root")
    if config.addon_same_symbol_entry_cooldown_minutes > 0 and config.strategy_profile != "continuous_addon_v1":
        raise ValueError("addon_same_symbol_entry_cooldown_minutes requires strategy_profile='continuous_addon_v1'")
    if config.entry_event_trigger != "none":
        if config.entry_confirm_delay_hours <= 0:
            raise ValueError("entry_event_trigger requires confirmed-bar entry timing")
        # Parse/validate now, before any order path or cycle work.
        _entry_event_expr(config.entry_event_trigger)
    from .bybit import validate_order_submit_allowed

    validate_order_submit_allowed(
        submit_orders=config.submit_orders,
        confirm_demo_orders=config.confirm_demo_orders,
    )


def _continuous_age_eligible_symbols(
    universe: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    age_days_min: int,
    now_ms: int,
) -> set[str] | None:
    """Symbols old enough to trade under the fresh-listing-squeezer floor.

    ``None`` means no age gate (floor <= 0). Prefers the AUTHORITATIVE per-cycle
    universe listing age (``listing_age_days`` = (now - Bybit v5 launchTime)/day,
    the same source the daily sleeve's ``pit_age_days`` gate uses), so the
    continuous sleeve no longer infers age from its rolling kline cache: a
    genuinely old coin whose cache only reaches back a few weeks would otherwise
    be wrongly gated out (the live-vs-PIT age definition debt). A null/unknown
    launch age is treated as ineligible (conservative, matches the universe-build
    age filter). Falls back to the kline-cache first bar only when the universe
    carries no listing age — correct only when the cache spans >= age_days_min days.
    """
    if age_days_min <= 0:
        return None
    if not universe.is_empty() and "listing_age_days" in universe.columns:
        eligible = universe.filter(
            pl.col("listing_age_days").is_not_null()
            & (pl.col("listing_age_days") >= float(age_days_min))
        )
        return set(eligible["symbol"].to_list())
    if not klines.is_empty():
        _logger.warning(
            "continuous age gate falling back to kline-cache first bar (universe has no "
            "listing_age_days); only correct when the cache spans >= %d days",
            age_days_min,
        )
        age_min_ms = age_days_min * MS_PER_DAY
        firsts = klines.group_by("symbol").agg(pl.col("ts_ms").min().alias("first"))
        return set(firsts.filter((now_ms - pl.col("first")) >= age_min_ms)["symbol"].to_list())
    return None


def run_continuous_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: ContinuousDemoCycleConfig | None = None,
    market_client: Any | None = None,
    private_client: Any | None = None,
    now_ms: int | None = None,
    execution_event_router: Any | None = None,
    kline_store: Any | None = None,
    private_state_cache: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
    panel_cache: "LivePanelCache | None" = None,
    reactivity_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One continuous-fade demo cycle: live decile (ticker-driven, no 1h gate) -> fresh-D9 shorts +
    left-decile/max-hold covers -> submit (gated) -> separate ledgers. Mirrors the long sleeve.

    When ``panel_cache`` is supplied (and ``live_panel_cache_enabled``) the live decile is sourced
    from the Tier-2 within-hour cache (heavy features once per bar close, cheap re-rank per wake); a
    cache failure transparently falls back to the full recompute, so the cache is purely a speedup."""
    demo = apply_continuous_demo_profile(demo_config or ContinuousDemoCycleConfig())
    _validate_continuous_demo_config(demo)
    strategy_id = continuous_strategy_id(demo)
    sleeve_name = continuous_sleeve_name(demo)
    trades_dataset, orders_dataset, cycles_dataset = continuous_dataset_names(demo)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = now_ms if now_ms is not None else _utc_now_ms()
    cur_day_ts = (cycle_now_ms // MS_PER_DAY) * MS_PER_DAY
    cycle_id = f"{_yyyymmddhhmmss(cycle_now_ms)}-{int(time.time_ns())}"

    with exclusive_file_lock(root / ".locks" / "continuous_demo_cycle.lock", stale_seconds=900):
        public = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        universe, symbols, tickers, ticker_source = _resolve_cycle_universe(
            public=public,
            # only universe-building fields (shared by both configs) are read
            demo=cast(EventDemoCycleConfig, demo),
            config=config, root=root, cycle_now_ms=cycle_now_ms,
            ticker_cache=ticker_cache, state_cache_stale_seconds=state_cache_stale_seconds)

        trading_client = private_client
        if trading_client is None and (demo.submit_orders or (demo.telegram and _private_credentials_present())):
            trading_client = _build_private_client(config)
        snapshot, _src = _resolve_private_snapshot(
            trading_client, demo, private_state_cache=private_state_cache, state_cache_stale_seconds=state_cache_stale_seconds)
        equity_usdt = resolve_snapshot_equity(snapshot, fallback_equity_usdt=demo.fallback_equity_usdt)
        raw_open_orders = snapshot.get("raw_open_orders", [])
        raw_positions = snapshot.get("raw_positions", [])
        errors = bool(snapshot.get("wallet_error") or snapshot.get("open_order_error") or snapshot.get("position_error"))
        live_entry_order_symbols = _live_open_order_symbols(raw_open_orders, reduce_only=False)
        live_position_symbols = set(_active_position_by_symbol(raw_positions))

        start_ms, end_ms = _kline_window(cycle_now_ms, lookback_days=demo.lookback_days)
        klines, kline_stats = _download_recent_1h_klines(
            symbols, start_ms=start_ms, end_ms=end_ms, config=config, workers=demo.workers,
            market_client=public if market_client is not None else None, cache_root=root, kline_store=kline_store)

        rmom = _load_rmom_table(_signal_source_root(demo, root))
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        contract_by_symbol = _contract_lookup(universe)

        live_state = pl.DataFrame()
        if rmom is not None and not klines.is_empty():
            if panel_cache is not None and demo.live_panel_cache_enabled:
                try:
                    live_state = panel_cache.state(
                        klines, price_by_symbol, rmom, now_ts_ms=cycle_now_ms, config=demo)
                except Exception:  # noqa: BLE001 — cache is a speedup; never let it break a cycle
                    _logger.exception("live panel cache failed; full-recompute fallback")
                    live_state = build_live_continuous_state(
                        klines, price_by_symbol, rmom, now_ts_ms=cycle_now_ms, config=demo)
            else:
                live_state = build_live_continuous_state(
                    klines, price_by_symbol, rmom, now_ts_ms=cycle_now_ms, config=demo)

        # ENTRY decile = the validated CONFIRMED-bar +1h state (alpha-sweep #1 fix). Entering on the live
        # intra-hour decile (entry_confirm_delay_hours=0) ~halves MAR (shorts into the first-hour squeeze).
        # 0 reverts to the legacy intra-hour entry.
        entry_state = live_state
        if demo.entry_confirm_delay_hours > 0 and rmom is not None and not klines.is_empty():
            entry_state = build_confirmed_entry_state(klines, rmom, now_ts_ms=cycle_now_ms, config=demo)

        all_trades = read_dataset(root, trades_dataset)
        all_orders = read_dataset(root, orders_dataset)
        all_cycles = read_dataset(root, cycles_dataset) if demo.daily_rebalance_enabled else pl.DataFrame()
        rebalance_rule = continuous_rebalance_rule(demo) if demo.daily_rebalance_enabled else None
        rebalance_target_scale = (
            compute_continuous_rebalance_scale(
                _continuous_rebalance_scale_state_from_cycles(all_cycles, current_day_ts=cur_day_ts),
                rebalance_rule,
            )
            if rebalance_rule is not None
            else 1.0
        )
        rebalance_resize_checked = (
            _continuous_rebalance_resize_checked_today(all_cycles, current_day_ts=cur_day_ts)
            if demo.daily_rebalance_enabled
            else False
        )
        open_trades = _open_continuous_trades(all_trades, strategy_id)
        held_symbols = set(open_trades["symbol"].to_list()) if not open_trades.is_empty() else set()

        cycle_trade_rows: list[dict[str, Any]] = []
        cycle_order_rows: list[dict[str, Any]] = []
        if demo.submit_orders or demo.record_dry_run:
            def _record_preflight(row: dict[str, Any]) -> None:
                write_dataset(pl.DataFrame([row], infer_schema_length=None), root, orders_dataset, partition_by=())
            preflight_cb: Callable[[dict[str, Any]], None] | None = _record_preflight
        else:
            preflight_cb = None

        # Exits: left-decile / breakeven / failed-fade / max-hold. continuous-6: the `left_decile`
        # SELECTION exit reads `entry_state` (the SAME confirmed-bar decile that selected the entry), so
        # a name is covered exactly when the system's real signal drops it out of the fade band — never
        # thrashed within the same hour on the noisier live intra-hour decile it was not entered on. The
        # protective exits stay PRICE-driven (MFE from kline lows, current PnL from the live ticker), so
        # squeeze safety remains immediate regardless of the decile snapshot.
        exit_plans = plan_continuous_exits(
            open_trades.to_dicts(), entry_state, now_ms=cycle_now_ms, config=demo,
            klines=klines, price_by_symbol=price_by_symbol)
        # don't double-submit a symbol that already has a live reduce-only order
        live_exit_syms = _live_open_order_symbols(raw_open_orders, reduce_only=True)
        exit_plans = [p for p in exit_plans if str(p["symbol"]) not in live_exit_syms]
        exec_exits, exit_orders = _execute_continuous_exits(
            exit_plans, all_trades, trading_client=trading_client, demo=demo, now_ms=cycle_now_ms,
            execution_event_router=execution_event_router, record_preflight=preflight_cb)
        if exec_exits:
            all_trades = _upsert_rows(all_trades, exec_exits, key="trade_id")
            if demo.submit_orders or demo.record_dry_run:
                cycle_trade_rows.extend(exec_exits)
                held_symbols -= {str(r["symbol"]) for r in exec_exits if r.get("status") == "closed"}
        if exit_orders and (demo.submit_orders or demo.record_dry_run):
            cycle_order_rows.extend(exit_orders)

        # Sniper lifecycle sweep (S1 Amendment 6, wired 2026-06-10): resolve fills of
        # resting snipes into first-class open trade rows, cancel snipes whose base
        # closed, and exit filled snipes alongside their base (reason base_exited).
        # Lists initialized here so the cycle payload/telegram can count sniper
        # activity even when the sweep is disabled or finds nothing (audit
        # 2026-06-12: armed sniper fills/errors were invisible to every notify path).
        snipe_fills: list[dict[str, Any]] = []
        snipe_updates: list[dict[str, Any]] = []
        snipe_exits: list[dict[str, Any]] = []
        snipe_exit_orders: list[dict[str, Any]] = []
        sniper_orders: list[dict[str, Any]] = []
        if demo.sniper_enabled:
            snipe_fills, snipe_updates, snipe_exit_plans = reconcile_continuous_snipes(
                all_trades, all_orders, trading_client=trading_client, demo=demo, now_ms=cycle_now_ms)
            if snipe_fills:
                all_trades = _upsert_rows(all_trades, snipe_fills, key="trade_id")
                if demo.submit_orders or demo.record_dry_run:
                    cycle_trade_rows.extend(snipe_fills)
                held_symbols |= {str(r["symbol"]) for r in snipe_fills}
            if snipe_updates and (demo.submit_orders or demo.record_dry_run):
                cycle_order_rows.extend(snipe_updates)
            if snipe_exit_plans:
                snipe_exits, snipe_exit_orders = _execute_continuous_exits(
                    snipe_exit_plans, all_trades, trading_client=trading_client, demo=demo,
                    now_ms=cycle_now_ms, execution_event_router=execution_event_router,
                    record_preflight=preflight_cb)
                if snipe_exits:
                    all_trades = _upsert_rows(all_trades, snipe_exits, key="trade_id")
                    if demo.submit_orders or demo.record_dry_run:
                        cycle_trade_rows.extend(snipe_exits)
                if snipe_exit_orders and (demo.submit_orders or demo.record_dry_run):
                    cycle_order_rows.extend(snipe_exit_orders)

        # Entries: fresh D9 shorts (not held, not in a live position/order, not in re-entry cooldown),
        # age-eligible, capacity-bounded.
        exit_cooldown_symbols = _recent_exit_cooldown_symbols(
            all_trades,
            now_ms=cycle_now_ms,
            cooldown_minutes=demo.reentry_cooldown_minutes,
            strategy_id=strategy_id,
        )
        addon_entry_cooldown_symbols = (
            _recent_entry_cooldown_symbols(
                all_trades,
                now_ms=cycle_now_ms,
                cooldown_minutes=demo.addon_same_symbol_entry_cooldown_minutes,
                strategy_id=strategy_id,
            )
            if continuous_sleeve_name(demo) == "continuous_addon"
            else set()
        )
        cooldown = set(held_symbols) | live_position_symbols | live_entry_order_symbols | exit_cooldown_symbols | addon_entry_cooldown_symbols
        open_count = len(held_symbols)
        # age floor (fresh-listing-squeezer defense): eligible = listed >= age_days_min ago.
        # Authoritative listing age from the v5 universe (matches the daily sleeve's pit_age_days
        # gate), not the rolling kline cache — see _continuous_age_eligible_symbols.
        eligible = _continuous_age_eligible_symbols(
            universe, klines, age_days_min=demo.age_days_min, now_ms=cycle_now_ms
        )
        # Portfolio circuit breaker: pause NEW entries during a correlated alt-squeeze (many recent
        # adverse covers) so the sleeve does not keep shorting into a melt-up. Exits are unaffected.
        entry_paused, recent_adverse = entry_circuit_breaker_tripped(
            all_trades, now_ms=cycle_now_ms, config=demo, strategy_id=strategy_id)
        if entry_paused:
            _logger.warning(
                "continuous entry circuit breaker TRIPPED: %d adverse covers in %dmin >= %d — pausing new entries",
                recent_adverse, demo.entry_pause_window_minutes, demo.entry_pause_after_adverse_exits)
        # long-sleeve-5: shrink-only IM-budget clamp off the shared control row (read-only,
        # fail-open). When the continuous sleeve is at/over its IM ceiling _eff_max == 0 and
        # selection is skipped this pass; NO-OP until the operator seeds a 'continuous' budget.
        _cs_root = (
            Path(demo.cross_sleeve_account_root).expanduser()
            if demo.cross_sleeve_account_root is not None
            else _cross_sleeve.shared_account_root(root)
        )
        _cs_state = _cross_sleeve.read_account_state(_cs_root)
        _eff_max, _cont_margin_clamped = _cross_sleeve.clamp_max_new_entries(
            demo.max_new_entries_per_cycle, sleeve=sleeve_name, state=_cs_state)
        skipped_continuous_margin_budget = demo.max_new_entries_per_cycle if _cont_margin_clamped else 0
        skipped_continuous_reservation = 0
        skipped_same_signal_reentry = 0
        addon_gate_stats: dict[str, Any] = {
            "addon_primary_pnl_gate_skips": 0,
            "addon_primary_pnl_gate_skip_symbols": [],
        }
        candidates: list[dict[str, Any]] = []
        cur_hour_ts = (cycle_now_ms // MS_PER_HOUR) * MS_PER_HOUR
        # signal ts = the deciding (confirmed) bar for the +1h entry, else the current hour (legacy).
        signal_ts = (cur_hour_ts - (1 + demo.entry_confirm_delay_hours) * MS_PER_HOUR
                     if demo.entry_confirm_delay_hours > 0 else cur_hour_ts)
        btc_trend_gate_allows_entry = _btc_trend_gate_allows_entries(
            klines,
            signal_ts_ms=signal_ts,
            config=demo,
        )
        if (
            not (errors and demo.submit_orders)
            and not entry_paused
            and _eff_max > 0
            and rebalance_target_scale > 0.0
            and btc_trend_gate_allows_entry
        ):
            if demo.ensemble_components:
                # winner_base ensemble: select PER COMPONENT (own trigger + age floor),
                # in declared order, capped at _eff_max total new entries this cycle.
                # The same symbol may enter under multiple components (the research
                # combine sums weighted exposures); trade ids/links carry the
                # component tag so the rows never collide.
                for comp_name, comp_trigger, comp_age, comp_tp, comp_weight in demo.ensemble_components:
                    if len(candidates) >= _eff_max:
                        break
                    cfg_c = replace(demo, entry_event_trigger=comp_trigger)
                    eligible_c = _continuous_age_eligible_symbols(
                        universe, klines, age_days_min=comp_age, now_ms=cycle_now_ms)
                    picks_c = select_continuous_entries(
                        entry_state, held_symbols=held_symbols, cooldown_symbols=cooldown,
                        open_count=open_count + len(candidates), config=cfg_c,
                        eligible_symbols=eligible_c)
                    cands_c, skipped_c = _continuous_entry_candidates_with_signal_metadata(
                        picks_c,
                        all_trades,
                        all_orders=all_orders,
                        signal_ts=signal_ts,
                        strategy_id=strategy_id,
                        price_by_symbol=price_by_symbol,
                        stop_loss_pct=demo.stop_loss_pct,
                        allow_same_signal_reentry=demo.allow_same_signal_reentry,
                    )
                    skipped_same_signal_reentry += skipped_c
                    for cand in cands_c:
                        if len(candidates) >= _eff_max:
                            break
                        candidates.append({**cand, "component": comp_name,
                                           "component_weight": comp_weight,
                                           "take_profit_pct": comp_tp})
            else:
                picks = select_continuous_entries(
                    entry_state, held_symbols=held_symbols, cooldown_symbols=cooldown, open_count=open_count,
                    config=demo, eligible_symbols=eligible)
                candidates, skipped_same_signal_reentry = _continuous_entry_candidates_with_signal_metadata(
                    picks,
                    all_trades,
                    all_orders=all_orders,
                    signal_ts=signal_ts,
                    strategy_id=strategy_id,
                    price_by_symbol=price_by_symbol,
                    stop_loss_pct=demo.stop_loss_pct,
                    allow_same_signal_reentry=demo.allow_same_signal_reentry,
                )
        if demo.addon_primary_pnl_gate and candidates:
            primary_open = _open_primary_trades_for_addon_gate(
                current_root=root,
                current_trades=all_trades,
                trades_dataset=trades_dataset,
                primary_data_root=demo.addon_primary_data_root,
                primary_strategy_id=demo.addon_primary_strategy_id,
            )
            candidates, addon_gate_stats = filter_addon_candidates_by_active_primary_pnl_gate(
                candidates,
                primary_open,
                price_by_symbol=price_by_symbol,
                min_unrealized_return=demo.addon_primary_min_unrealized_return,
            )
        # long-sleeve-6: claim each candidate symbol in the shared registry under the
        # control-row lock before submit; a symbol a sibling holds (active reservation or
        # live venue position) is dropped, closing the same-minute cross-process race.
        # Fail-open; submit-only (dry-run/paper never contend for the shared account).
        if demo.submit_orders and candidates:
            candidates, skipped_continuous_reservation = _cross_sleeve.partition_claimable(
                _cs_root, candidates, sleeve=sleeve_name, now_ms=cycle_now_ms,
                live_position_symbols=live_position_symbols,
            )
        order_notional_frac = demo.per_position_notional_pct_equity / 100.0
        if demo.daily_rebalance_enabled:
            order_notional_frac *= rebalance_target_scale
        exec_entries, entry_orders = _execute_continuous_entries(
            candidates, trading_client=trading_client, demo=demo, equity_usdt=equity_usdt,
            order_notional_frac=order_notional_frac, price_by_symbol=price_by_symbol,
            contract_by_symbol=contract_by_symbol, now_ms=cycle_now_ms, strategy_id=strategy_id,
            record_preflight=preflight_cb, execution_event_router=execution_event_router)
        if exec_entries and (demo.submit_orders or demo.record_dry_run):
            cycle_trade_rows.extend(exec_entries)
        if entry_orders and (demo.submit_orders or demo.record_dry_run):
            cycle_order_rows.extend(entry_orders)
        # Sniper placement (S1 Amendment 6): one resting PostOnly Sell limit per fresh
        # short at entry*(1+wick), quarter-size, disaster stop attached venue-side.
        if demo.sniper_enabled and exec_entries:
            sniper_orders = _execute_sniper_placements(
                exec_entries, trading_client=trading_client, demo=demo, now_ms=cycle_now_ms,
                strategy_id=strategy_id, price_by_symbol=price_by_symbol,
                record_preflight=preflight_cb)
            if sniper_orders and (demo.submit_orders or demo.record_dry_run):
                cycle_order_rows.extend(sniper_orders)
        sniper_errors = sum(
            1
            for r in (sniper_orders + snipe_updates + snipe_exit_orders)
            if str(r.get("submit_mode", "")) == "error" or str(r.get("error") or "")
        )

        # Dynamic-exit forward shadow (paper-only bookkeeping; can never affect orders).
        if demo.dynexit_shadow_enabled:
            try:
                from .continuous_dynexit_shadow import update_dynexit_shadow
                post_entry_ledger = all_trades
                if exec_entries:
                    post_entry_ledger = _upsert_rows(post_entry_ledger, exec_entries, key="trade_id")
                update_dynexit_shadow(
                    root, all_trades=post_entry_ledger,
                    fresh_entries=[r for r in exec_entries if r.get("status") == "open"] if exec_entries else [],
                    klines=klines, now_ms=cycle_now_ms)
            except Exception:  # noqa: BLE001 — the shadow must never break the live cycle
                _logger.exception("dynexit shadow sweep failed (non-fatal)")

        resize_rows: list[dict[str, Any]] = []
        resize_orders: list[dict[str, Any]] = []
        post_trade_ledger = all_trades
        if exec_entries:
            post_trade_ledger = _upsert_rows(post_trade_ledger, exec_entries, key="trade_id")
        if demo.daily_rebalance_enabled and not rebalance_resize_checked:
            resize_plans = plan_continuous_rebalance_resizes(
                _open_continuous_trades(post_trade_ledger, strategy_id).to_dicts(),
                price_by_symbol=price_by_symbol,
                equity_usdt=equity_usdt,
                base_notional_pct_equity=demo.per_position_notional_pct_equity,
                target_scale=rebalance_target_scale,
            )
            resize_rows, resize_orders = _execute_continuous_rebalance_resizes(
                resize_plans,
                post_trade_ledger,
                trading_client=trading_client,
                demo=demo,
                price_by_symbol=price_by_symbol,
                contract_by_symbol=contract_by_symbol,
                now_ms=cycle_now_ms,
                strategy_id=strategy_id,
                execution_event_router=execution_event_router,
                record_preflight=preflight_cb,
            )
            if resize_rows:
                post_trade_ledger = _upsert_rows(post_trade_ledger, resize_rows, key="trade_id")
                if demo.submit_orders or demo.record_dry_run:
                    cycle_trade_rows.extend(resize_rows)
            if resize_orders and (demo.submit_orders or demo.record_dry_run):
                cycle_order_rows.extend(resize_orders)

        rebalance_fields: dict[str, Any] = {}
        if demo.daily_rebalance_enabled:
            rebalance_fields = _continuous_rebalance_cycle_fields(
                post_trade_ledger,
                all_cycles,
                price_by_symbol=price_by_symbol,
                current_day_ts=cur_day_ts,
                now_ms=cycle_now_ms,
                strategy_id=strategy_id,
                rule=rebalance_rule if rebalance_rule is not None else continuous_rebalance_rule(demo),
            )
            rebalance_fields.update(
                {
                    "rebalance_resize_checked": True,
                    "rebalance_resize_orders": len(resize_orders),
                    "rebalance_resize_trade_rows": len(resize_rows),
                    "rebalance_resize_skipped_same_day": rebalance_resize_checked,
                }
            )

        # Ledger flush: orders first, then trades (crash-durability ordering).
        if cycle_order_rows:
            write_dataset(pl.DataFrame(cycle_order_rows, infer_schema_length=None), root, orders_dataset, partition_by=())
        if cycle_trade_rows:
            write_dataset(pl.DataFrame(cycle_trade_rows, infer_schema_length=None), root, trades_dataset, partition_by=())

        live_d9 = int(live_state.filter(pl.col("decile") == demo.decile).height) if not live_state.is_empty() else 0
        # Surface the rmom FRESHNESS (not just file-existence): a stale table silently empties the
        # decile join (the is_not_null filter drops every symbol) yet rmom_present would read True.
        # max_rmom_day_ts lets the watchdog distinguish "quiet market" from "stale signal gate".
        _rmom_day_max = (
            rmom["day_ts"].max()
            if (rmom is not None and not rmom.is_empty() and "day_ts" in rmom.columns)
            else None
        )
        max_rmom_day_ts = int(cast(int, _rmom_day_max)) if _rmom_day_max is not None else 0
        payload = {
            "cycle_id": cycle_id, "ts_ms": cycle_now_ms, "strategy_id": strategy_id, "mode": "submit" if demo.submit_orders else "dry_run",
            "strategy_profile": demo.strategy_profile,
            "feature_set": ",".join(demo.feature_set),
            "universe_symbols": len(symbols), "ticker_source": ticker_source, "kline_store_rows": kline_stats.get("store_rows", 0),
            "rmom_present": rmom is not None, "max_rmom_day_ts": max_rmom_day_ts,
            # 0-floored: the refresh seeds a today/tomorrow row (the daily-rollover guard) so the gate
            # day can lead cur_day -> a raw diff would read negative; "0 = fresh" is the meaningful floor.
            "rmom_stale_days": max(0, (cur_day_ts - max_rmom_day_ts) // MS_PER_DAY) if max_rmom_day_ts else None,
            "live_d9_symbols": live_d9, "open_positions": open_count,
            "entries": len([r for r in exec_entries if r.get("status") in ("filled", "partial", "planned")]),
            "exits": len(exec_exits), "candidates": len(candidates), "equity_usdt": equity_usdt,
            # Sniper lifecycle visibility: a snipe FILL is a NEW live short on the account
            # and an error is a stuck/ghost resting order — both must reach the cycle row,
            # the journald summary and the telegram reason filter (audit 2026-06-12).
            "sniper_fills": len(snipe_fills), "sniper_exits": len(snipe_exits),
            "sniper_errors": sniper_errors,
            "entry_signal_ts_ms": signal_ts,
            "btc_trend_gate": demo.btc_trend_gate,
            "btc_trend_gate_allows_entry": btc_trend_gate_allows_entry,
            "entry_paused": entry_paused, "recent_adverse_exits": recent_adverse,
            "skipped_continuous_margin_budget": skipped_continuous_margin_budget,  # ls-5 cross-sleeve IM clamp
            "skipped_continuous_reservation": skipped_continuous_reservation,  # ls-6 symbol taken by a sibling
            "skipped_same_signal_reentry": skipped_same_signal_reentry,
            "addon_same_symbol_entry_cooldown_symbols": len(addon_entry_cooldown_symbols),
            "addon_primary_pnl_gate": demo.addon_primary_pnl_gate,
            "addon_primary_pnl_gate_skips": addon_gate_stats["addon_primary_pnl_gate_skips"],
            "addon_primary_pnl_gate_skip_symbols": ",".join(addon_gate_stats["addon_primary_pnl_gate_skip_symbols"]),
        }
        payload.update(rebalance_fields)
        # Flatten the daemon's reactivity-loop counters (rx_*) into the persisted cycle so the watchdog
        # can see a dead/stale fast protective-exit loop instead of it failing silently.
        if reactivity_stats:
            payload.update({f"rx_{k}": v for k, v in reactivity_stats.items()})
        write_dataset(pl.DataFrame([payload], infer_schema_length=None), root, cycles_dataset, partition_by=())
        # Per-cycle telegram AFTER the ledger write (a notify failure can never cost a
        # row) and exception-isolated. Outcome rides only on the returned payload — the
        # persisted cycle row above keeps its schema.
        telegram_sent, telegram_error = _maybe_continuous_notify(
            payload, exec_entries, exec_exits + snipe_exits, enabled=demo.telegram
        )
        payload["telegram_sent"] = telegram_sent
        if telegram_error:
            payload["telegram_error"] = telegram_error
        return payload


def _live_prices_from_ticker_cache(ticker_cache: Any | None, symbols: set[str]) -> dict[str, float]:
    """Mark/last price per held symbol from the WS ticker cache (raw Bybit rows). Network-free — the
    tick loop must never block on REST. Returns {} if the cache is unavailable or has no prices."""
    if ticker_cache is None or not symbols:
        return {}
    out: dict[str, float] = {}
    try:
        for sym in symbols:
            row = ticker_cache.get(sym)
            if not row:
                continue
            price = (
                _float(row.get("markPrice")) or _float(row.get("lastPrice"))
                or _float(row.get("mark_price")) or _float(row.get("last_price"))
            )
            if price > 0.0:
                out[sym] = price
    except Exception:  # noqa: BLE001 — never let the fast loop crash on a cache read
        return out
    return out


def run_continuous_protective_exit_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: ContinuousDemoCycleConfig | None = None,
    trading_client: Any | None = None,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    private_state_cache: Any | None = None,
    execution_event_router: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Tier 1 — fast protective-exit pass: covers ONLY (breakeven / stop_approach / failed_fade /
    max_hold) on held continuous shorts, driven off live ticker prices. NO universe build, NO decile
    recompute, NO entries — so it is cheap enough to run on every ticker tick (the daemon's fast loop).

    Shares the continuous cycle file-lock so it never races the main cycle or ws_risk on the ledger;
    reads prices from the WS ticker cache and MFE klines from the WS store (never REST). The state
    (left-decile) exit is intentionally NOT here — that needs the panel and belongs to the main cycle."""
    demo = apply_continuous_demo_profile(demo_config or ContinuousDemoCycleConfig())
    if not (demo.submit_orders or demo.record_dry_run):
        return {"exits": 0, "open_positions": 0, "skipped": "no-submit"}
    if demo.submit_orders and trading_client is None:
        return {"exits": 0, "open_positions": 0, "skipped": "no-client"}
    strategy_id = continuous_strategy_id(demo)
    trades_dataset, orders_dataset, _cycles = continuous_dataset_names(demo)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    now = now_ms if now_ms is not None else _utc_now_ms()

    with exclusive_file_lock(root / ".locks" / "continuous_demo_cycle.lock", stale_seconds=900):
        all_trades = read_dataset(root, trades_dataset)
        open_trades = _open_continuous_trades(all_trades, strategy_id)
        if open_trades.is_empty():
            return {"exits": 0, "open_positions": 0}
        held = set(open_trades["symbol"].to_list())
        price_by_symbol = _live_prices_from_ticker_cache(ticker_cache, held)
        if not price_by_symbol:
            return {"exits": 0, "open_positions": len(held), "skipped": "no-prices"}
        # MFE klines for the held names from the WS store ONLY (no REST on the fast path). Absent
        # store coverage just means mfe=0 for that name → breakeven won't fire but stop_approach /
        # failed_fade (price+time only) still do; the next full cycle's REST fallback fills the gap.
        klines = pl.DataFrame()
        if kline_store is not None:
            lookback = (demo.max_hold_hours + 2) * MS_PER_HOUR
            try:
                klines = kline_store.get_klines(sorted(held), start_ms=now - lookback, end_ms=now)
            except Exception:  # noqa: BLE001
                klines = pl.DataFrame()
        # don't double-submit a symbol that already has a live reduce-only order in flight
        live_exit_syms: set[str] = set()
        if private_state_cache is not None:
            try:
                snap = private_state_cache.snapshot()
                live_exit_syms = _live_open_order_symbols(snap.get("raw_open_orders", []), reduce_only=True)
            except Exception:  # noqa: BLE001
                live_exit_syms = set()
        open_dicts = open_trades.to_dicts()
        # Ledger-based in-flight guard (WS-snapshot-independent): a held trade whose row already
        # carries an exit_order_link_id has a cover submitted but not yet confirmed closed. At the
        # fast loop's ~2s cadence the WS order event for the main cycle's (or our own prior) cover can
        # still be in flight, so `live_exit_syms` (from the WS snapshot) may miss it; this guard stops
        # a second reduce-only. Retry of a genuinely-failed cover is deferred to the slower main cycle
        # (whose snapshot has a REST fallback) — the server-side disaster stop covers the interim.
        in_flight = {str(t["symbol"]) for t in open_dicts if str(t.get("exit_order_link_id") or "")}
        exit_plans = plan_protective_exits(
            open_dicts, now_ms=now, config=demo, klines=klines, price_by_symbol=price_by_symbol)
        exit_plans = [
            p for p in exit_plans
            if str(p["symbol"]) not in live_exit_syms and str(p["symbol"]) not in in_flight
        ]
        if not exit_plans:
            return {"exits": 0, "open_positions": len(held)}

        def _record_preflight(row: dict[str, Any]) -> None:
            write_dataset(pl.DataFrame([row], infer_schema_length=None), root, orders_dataset, partition_by=())

        exec_exits, exit_orders = _execute_continuous_exits(
            exit_plans, all_trades, trading_client=trading_client, demo=demo, now_ms=now,
            execution_event_router=execution_event_router, record_preflight=_record_preflight)
        # orders first, then trades (crash-durability ordering, same as the main cycle)
        if exit_orders:
            write_dataset(pl.DataFrame(exit_orders, infer_schema_length=None), root, orders_dataset, partition_by=())
        if exec_exits:
            write_dataset(pl.DataFrame(exec_exits, infer_schema_length=None), root, trades_dataset, partition_by=())
        return {
            "exits": len(exec_exits), "open_positions": len(held),
            "reasons": sorted({str(p.get("exit_reason")) for p in exit_plans}),
        }
