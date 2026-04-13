from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from alerting import AlertPayload, TelegramNotifier
from config import Settings
from database import SignalDatabase, SignalRecord
from indicators import (
    btc_regime_score,
    clip_value,
    close_only_atr,
    correlation_cluster_labels,
    cross_sectional_zscores,
    curvature_signal,
    hurst_exponent,
    log_returns,
    path_efficiency,
    volatility_adjusted_momentum,
)
from state import MarketState
from universe import ticker_cluster

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TickerMetrics:
    ticker: str
    current_price: float
    momentum_raw: float
    curvature_raw: float
    hurst: float
    cluster_label: str = ""
    momentum_reference_label: str = "absolute"
    momentum_z: float = 0.0
    curvature_z: float = 0.0
    composite_score: float = 0.0
    rank: int = 0


@dataclass(slots=True)
class RankedSignal:
    stage: str
    signal_kind: str
    ticker: str
    current_price: float
    momentum_z: float
    curvature: float
    hurst: float
    regime_score: int
    composite_score: float
    rank: int
    persistence_hits: int
    alerted: bool
    cluster_label: str = ""
    momentum_reference_label: str = "absolute"
    entry_diagnostics: str = ""


@dataclass(slots=True)
class RankedTicker:
    ticker: str
    rank: int
    composite_score: float
    momentum_z: float
    curvature_z: float
    hurst: float


@dataclass(slots=True)
class IntradayRegimeContext:
    lookback_bars: int
    breadth_pct: float
    basket_return_pct: float
    trend_efficiency: float
    leadership_persistence: float
    volatility_expansion_ratio: float | None
    pass_count: int
    total_checks: int
    tradeable: bool
    blockers: tuple[str, ...]


class SignalEngine:
    def __init__(
        self,
        settings: Settings,
        state: MarketState,
        database: SignalDatabase,
        notifier: TelegramNotifier,
    ) -> None:
        self.settings = settings
        self.state = state
        self.database = database
        self.notifier = notifier
        self.last_ranked_tickers: dict[str, list[RankedTicker]] = {
            "confirmed": [],
            "emerging": [],
        }
        self.last_intraday_regime: dict[str, IntradayRegimeContext | None] = {
            "confirmed": None,
            "emerging": None,
        }
        self.last_cluster_labels: dict[str, dict[str, str]] = {
            "confirmed": {},
            "emerging": {},
        }

    def _dominance_label(self, dominance_state: int) -> str:
        if dominance_state < 0:
            return "falling"
        if dominance_state > 0:
            return "rising"
        return "neutral"

    def _default_intraday_regime_context(self) -> IntradayRegimeContext:
        total_checks = 5 if self.settings.volatility_expansion_filter_enabled else 4
        return IntradayRegimeContext(
            lookback_bars=max(self.settings.intraday_regime_lookback_bars, 2),
            breadth_pct=1.0,
            basket_return_pct=0.0,
            trend_efficiency=1.0,
            leadership_persistence=1.0,
            volatility_expansion_ratio=1.0,
            pass_count=total_checks,
            total_checks=total_checks,
            tradeable=True,
            blockers=(),
        )

    def _cluster_labels(
        self,
        raw_prices_by_symbol: dict[str, np.ndarray],
    ) -> dict[str, str]:
        mode = self.settings.cluster_assignment_mode
        if mode == "manual":
            return {
                symbol: f"manual:{ticker_cluster(symbol)}"
                for symbol in raw_prices_by_symbol
            }
        labels = correlation_cluster_labels(
            raw_prices_by_symbol,
            lookback_bars=max(self.settings.cluster_correlation_lookback_bars, 3),
            threshold=self.settings.cluster_correlation_threshold,
        )
        if mode == "dynamic":
            return labels
        if mode == "hybrid":
            resolved: dict[str, str] = {}
            for symbol, label in labels.items():
                if label.startswith("solo:"):
                    resolved[symbol] = f"manual:{ticker_cluster(symbol)}"
                else:
                    resolved[symbol] = label
            return resolved
        raise ValueError(f"Unsupported CLUSTER_ASSIGNMENT_MODE: {mode}")

    def _basket_relative_prices(
        self,
        ticker: str,
        raw_prices_by_symbol: dict[str, np.ndarray],
    ) -> np.ndarray | None:
        if len(raw_prices_by_symbol) < 3:
            return None
        target = raw_prices_by_symbol.get(ticker)
        if target is None or target.size == 0 or np.any(target <= 0):
            return None
        basket_components: list[np.ndarray] = []
        for symbol, prices in raw_prices_by_symbol.items():
            if symbol == ticker:
                continue
            if prices.size != target.size or np.any(prices <= 0):
                return None
            basket_components.append(prices / prices[0])
        if not basket_components:
            return None
        basket = np.mean(np.vstack(basket_components), axis=0)
        if np.any(basket <= 0):
            return None
        return target / basket

    def _cluster_relative_prices(
        self,
        ticker: str,
        raw_prices_by_symbol: dict[str, np.ndarray],
        cluster_labels: dict[str, str],
    ) -> np.ndarray | None:
        target = raw_prices_by_symbol.get(ticker)
        if target is None or target.size == 0 or np.any(target <= 0):
            return None
        label = cluster_labels.get(ticker, f"solo:{ticker}")
        cluster_components: list[np.ndarray] = []
        for symbol, prices in raw_prices_by_symbol.items():
            if symbol == ticker or cluster_labels.get(symbol) != label:
                continue
            if prices.size != target.size or np.any(prices <= 0):
                return None
            cluster_components.append(prices / prices[0])
        if not cluster_components:
            return None
        cluster_basket = np.mean(np.vstack(cluster_components), axis=0)
        if np.any(cluster_basket <= 0):
            return None
        return target / cluster_basket

    def _hybrid_relative_prices(
        self,
        ticker: str,
        raw_prices_by_symbol: dict[str, np.ndarray],
        *,
        include_provisional: bool,
    ) -> np.ndarray | None:
        raw_prices = raw_prices_by_symbol.get(ticker)
        if raw_prices is None or raw_prices.size == 0 or np.any(raw_prices <= 0):
            return None
        btc_prices = self.state.get_prices("BTCUSDT", include_provisional=include_provisional)
        basket_relative = self._basket_relative_prices(ticker, raw_prices_by_symbol)
        if (
            basket_relative is None
            or btc_prices.size != raw_prices.size
            or np.any(btc_prices <= 0)
        ):
            return basket_relative if basket_relative is not None else raw_prices
        btc_normalized = btc_prices / btc_prices[0]
        basket_reference = raw_prices / basket_relative
        btc_weight = float(np.clip(self.settings.momentum_reference_blend_btc_weight, 0.0, 1.0))
        basket_weight = 1.0 - btc_weight
        blended_reference = (btc_normalized**btc_weight) * (basket_reference**basket_weight)
        if np.any(blended_reference <= 0):
            return raw_prices
        return raw_prices / blended_reference

    def _momentum_signal_prices(
        self,
        ticker: str,
        raw_prices_by_symbol: dict[str, np.ndarray],
        cluster_labels: dict[str, str],
        *,
        include_provisional: bool,
    ) -> tuple[np.ndarray | None, str]:
        raw_prices = raw_prices_by_symbol.get(ticker)
        if raw_prices is None or raw_prices.size == 0:
            return None, "missing"
        mode = self.settings.momentum_reference_mode
        if mode == "absolute":
            return raw_prices, "absolute"
        if mode == "btc_relative":
            btc_prices = self.state.get_prices("BTCUSDT", include_provisional=include_provisional)
            if btc_prices.size != raw_prices.size or np.any(btc_prices <= 0):
                return raw_prices, "absolute:fallback"
            return raw_prices / btc_prices, "btc_relative"
        if mode == "basket_relative":
            basket_relative = self._basket_relative_prices(ticker, raw_prices_by_symbol)
            if basket_relative is None:
                return raw_prices, "absolute:fallback"
            return basket_relative, "basket_relative"
        if mode == "cluster_relative":
            cluster_relative = self._cluster_relative_prices(
                ticker,
                raw_prices_by_symbol,
                cluster_labels,
            )
            if cluster_relative is not None:
                return cluster_relative, f"cluster_relative:{cluster_labels.get(ticker, 'unknown')}"
            basket_relative = self._basket_relative_prices(ticker, raw_prices_by_symbol)
            if basket_relative is not None:
                return basket_relative, "basket_relative:fallback"
            return raw_prices, "absolute:fallback"
        if mode == "hybrid_relative":
            hybrid = self._hybrid_relative_prices(
                ticker,
                raw_prices_by_symbol,
                include_provisional=include_provisional,
            )
            if hybrid is None:
                return raw_prices, "absolute:fallback"
            return hybrid, "hybrid_relative"
        raise ValueError(f"Unsupported MOMENTUM_REFERENCE_MODE: {mode}")

    def _compute_metrics(self, stage: str) -> dict[str, TickerMetrics]:
        include_provisional = stage == "emerging"
        raw_prices_by_symbol: dict[str, np.ndarray] = {}
        for symbol in self.settings.universe:
            prices = self.state.get_prices(symbol, include_provisional=include_provisional)
            if prices.size < self.settings.state_window:
                continue
            raw_prices_by_symbol[symbol] = prices
        cluster_labels = self._cluster_labels(raw_prices_by_symbol)
        self.last_cluster_labels[stage] = dict(cluster_labels)
        metrics: dict[str, TickerMetrics] = {}
        for symbol, prices in raw_prices_by_symbol.items():
            momentum_prices, reference_label = self._momentum_signal_prices(
                symbol,
                raw_prices_by_symbol,
                cluster_labels,
                include_provisional=include_provisional,
            )
            if momentum_prices is None or np.any(momentum_prices <= 0):
                continue
            returns = log_returns(momentum_prices)
            momentum = volatility_adjusted_momentum(
                prices=momentum_prices,
                returns=returns,
                lookback=self.settings.momentum_lookback,
                skip=self.settings.momentum_skip,
                min_volatility=self.settings.min_volatility,
            )
            raw_returns = log_returns(prices)
            curvature = curvature_signal(
                returns=raw_returns,
                ma_window=self.settings.curvature_ma_window,
                signal_window=self.settings.curvature_signal_window,
            )
            hurst = hurst_exponent(prices[-self.settings.hurst_window :])
            if not np.isfinite(momentum) or not np.isfinite(curvature) or not np.isfinite(hurst):
                continue
            metrics[symbol] = TickerMetrics(
                ticker=symbol,
                current_price=float(prices[-1]),
                momentum_raw=momentum,
                curvature_raw=curvature,
                hurst=hurst,
                cluster_label=cluster_labels.get(symbol, f"manual:{ticker_cluster(symbol)}"),
                momentum_reference_label=reference_label,
            )
        if len(metrics) < 2:
            return {}

        momentum_z = cross_sectional_zscores(
            {ticker: item.momentum_raw for ticker, item in metrics.items()}
        )
        curvature_z = cross_sectional_zscores(
            {ticker: item.curvature_raw for ticker, item in metrics.items()}
        )
        for ticker, item in metrics.items():
            item.momentum_z = clip_value(
                momentum_z[ticker],
                -self.settings.momentum_z_clip,
                self.settings.momentum_z_clip,
            )
            item.curvature_z = clip_value(
                curvature_z[ticker],
                -self.settings.curvature_z_clip,
                self.settings.curvature_z_clip,
            )
            item.composite_score = (
                (self.settings.momentum_weight * item.momentum_z)
                + (self.settings.curvature_weight * item.curvature_z)
            )

        ranked = sorted(metrics.values(), key=lambda item: item.composite_score, reverse=True)
        for idx, item in enumerate(ranked, start=1):
            item.rank = idx
        self.last_ranked_tickers[stage] = [
            RankedTicker(
                ticker=item.ticker,
                rank=item.rank,
                composite_score=item.composite_score,
                momentum_z=item.momentum_z,
                curvature_z=item.curvature_z,
                hurst=item.hurst,
            )
            for item in ranked
        ]
        return {item.ticker: item for item in ranked}

    def format_top_rankings(self, stage: str = "confirmed", top_n: int | None = None) -> str:
        limit = self.settings.ranking_log_top_n if top_n is None else top_n
        ranked_source = self.last_ranked_tickers
        if isinstance(ranked_source, dict):
            ranked_tickers = ranked_source.get(stage, [])
        else:
            ranked_tickers = ranked_source
        if limit <= 0 or not ranked_tickers:
            return ""
        leaders = ranked_tickers[:limit]
        return " | ".join(
            f"#{item.rank} {item.ticker} score={item.composite_score:.2f} "
            f"mom_z={item.momentum_z:.2f} cur_z={item.curvature_z:.2f} hurst={item.hurst:.2f}"
            for item in leaders
        )

    def get_ranked_tickers(self, stage: str = "confirmed") -> list[RankedTicker]:
        return list(self.last_ranked_tickers.get(stage, []))

    def _compute_regime(self, include_provisional: bool = False) -> tuple[int, int]:
        regime_score = btc_regime_score(
            btc_daily_closes=self.state.global_state.btc_daily_closes,
            vol_lookback=self.settings.btc_vol_lookback,
            vol_threshold=self.settings.btc_realized_vol_threshold,
        )
        self.state.global_state.btc_regime_score = regime_score
        dominance_state = self.state.global_state.btcdom_state
        self.state.global_state.btc_dominance_series = self.state.global_state.btcdom_closes
        self.state.global_state.dom_falling = int(dominance_state < 0)
        return regime_score, dominance_state

    def _passes_core_filters(
        self,
        item: TickerMetrics,
        min_score: float | None,
    ) -> bool:
        return bool(item.hurst > self.settings.hurst_cutoff and (min_score is None or item.composite_score >= min_score))

    def _dominance_score_adjustment(self, dominance_state: int) -> float:
        return self.settings.dominance_score_adjustments.get(dominance_state, 0.0)

    def _leadership_persistence(self, leaders: list[str]) -> float:
        limit = max(self.settings.intraday_regime_top_n, 1)
        scores: list[float] = []
        for ticker in leaders[:limit]:
            observations = list(self.state.intrabar_observations[ticker])
            if not observations:
                continue
            qualified_hits = sum(
                1 for observation in observations if observation.rank <= self.settings.entry_ready_top_n
            )
            scores.append(qualified_hits / len(observations))
        if not scores:
            return 1.0
        return float(np.mean(scores))

    def _compute_intraday_regime(
        self,
        metrics: dict[str, TickerMetrics],
        *,
        include_provisional: bool,
    ) -> IntradayRegimeContext:
        lookback_bars = max(self.settings.intraday_regime_lookback_bars, 2)
        normalized_paths: list[np.ndarray] = []
        symbol_returns: list[float] = []
        for ticker in self.settings.universe:
            prices = self.state.get_prices(ticker, include_provisional=include_provisional)
            if prices.size < lookback_bars + 1:
                continue
            window = np.asarray(prices[-(lookback_bars + 1) :], dtype=float)
            if np.any(window <= 0):
                continue
            normalized_paths.append(window / window[0])
            symbol_returns.append(float((window[-1] / window[0]) - 1.0))
        if len(normalized_paths) < 3 or not metrics:
            total_checks = 5 if self.settings.volatility_expansion_filter_enabled else 4
            return IntradayRegimeContext(
                lookback_bars=lookback_bars,
                breadth_pct=1.0,
                basket_return_pct=0.0,
                trend_efficiency=1.0,
                leadership_persistence=1.0,
                volatility_expansion_ratio=1.0,
                pass_count=total_checks,
                total_checks=total_checks,
                tradeable=True,
                blockers=(),
            )

        basket_path = np.mean(np.vstack(normalized_paths), axis=0)
        breadth_pct = float(np.mean(np.asarray(symbol_returns, dtype=float) > 0.0))
        basket_return_pct = float(basket_path[-1] - 1.0)
        trend_efficiency = path_efficiency(basket_path)
        current_leaders = list(metrics)[: max(self.settings.intraday_regime_top_n, 1)]
        leadership_persistence = self._leadership_persistence(current_leaders)
        volatility_expansion_ratio: float | None = None
        if self.settings.volatility_expansion_filter_enabled:
            short_window = max(self.settings.volatility_expansion_short_bars, 1)
            long_window = max(self.settings.volatility_expansion_long_bars, short_window)
            short_atr = close_only_atr(basket_path, short_window)
            long_atr = close_only_atr(basket_path, long_window)
            if np.isfinite(short_atr) and np.isfinite(long_atr):
                if long_atr == 0.0:
                    volatility_expansion_ratio = 1.0 if short_atr == 0.0 else float("inf")
                else:
                    volatility_expansion_ratio = float(short_atr / long_atr)
        blockers: list[str] = []
        pass_count = 0
        checks: list[tuple[str, bool]] = [
            (
                "breadth",
                breadth_pct >= self.settings.intraday_regime_min_breadth,
            ),
            (
                "efficiency",
                trend_efficiency >= self.settings.intraday_regime_min_efficiency,
            ),
            (
                "basket_return",
                basket_return_pct >= self.settings.intraday_regime_min_basket_return,
            ),
            (
                "leadership_persistence",
                leadership_persistence >= self.settings.intraday_regime_min_leadership_persistence,
            ),
        ]
        if self.settings.volatility_expansion_filter_enabled:
            checks.append(
                (
                    "volatility_expansion",
                    volatility_expansion_ratio is not None
                    and volatility_expansion_ratio <= self.settings.volatility_expansion_max_ratio,
                )
            )
        for blocker, passed in checks:
            if passed:
                pass_count += 1
            else:
                blockers.append(blocker)
        tradeable = True
        if self.settings.intraday_regime_filter_enabled:
            tradeable = pass_count >= max(self.settings.intraday_regime_min_pass_count, 1)
            if (
                self.settings.volatility_expansion_filter_enabled
                and self.settings.volatility_expansion_hard_gate
                and "volatility_expansion" in blockers
            ):
                tradeable = False
        return IntradayRegimeContext(
            lookback_bars=lookback_bars,
            breadth_pct=breadth_pct,
            basket_return_pct=basket_return_pct,
            trend_efficiency=trend_efficiency,
            leadership_persistence=leadership_persistence,
            volatility_expansion_ratio=volatility_expansion_ratio,
            pass_count=pass_count,
            total_checks=len(checks),
            tradeable=tradeable,
            blockers=tuple(blockers),
        )

    def _is_strengthening_intrabar(
        self,
        ticker: str,
        min_observations: int | None = None,
        min_rank_improvement: int | None = None,
        min_composite_gain: float = 0.0,
    ) -> bool:
        observations = list(self.state.intrabar_observations[ticker])
        min_observations = (
            self.settings.emerging_min_observations
            if min_observations is None
            else min_observations
        )
        min_rank_improvement = (
            self.settings.emerging_min_rank_improvement
            if min_rank_improvement is None
            else min_rank_improvement
        )
        if len(observations) < min_observations:
            return False
        recent = observations[-min_observations:]
        rank_gain = recent[0].rank - recent[-1].rank
        if rank_gain < min_rank_improvement:
            return False
        if any(later.rank > earlier.rank for earlier, later in zip(recent, recent[1:])):
            return False
        if any(
            later.composite_score <= earlier.composite_score
            for earlier, later in zip(recent, recent[1:])
        ):
            return False
        if recent[-1].composite_score - recent[0].composite_score < min_composite_gain:
            return False
        return True

    def _intrabar_progress(
        self,
        ticker: str,
        *,
        min_observations: int,
    ) -> tuple[int, float]:
        observations = list(self.state.intrabar_observations[ticker])
        if len(observations) < min_observations:
            return 0, 0.0
        recent = observations[-min_observations:]
        return recent[0].rank - recent[-1].rank, recent[-1].composite_score - recent[0].composite_score

    def _entry_diagnostics(
        self,
        ticker: str,
        item: TickerMetrics,
        intraday_regime: IntradayRegimeContext,
    ) -> str:
        rank_gain, composite_gain = self._intrabar_progress(
            ticker,
            min_observations=self.settings.entry_ready_min_observations,
        )
        blockers = ",".join(intraday_regime.blockers) if intraday_regime.blockers else "none"
        vei = (
            f"{intraday_regime.volatility_expansion_ratio:.2f}"
            if intraday_regime.volatility_expansion_ratio is not None
            else "n/a"
        )
        return (
            f"ref={item.momentum_reference_label} "
            f"cluster={item.cluster_label} "
            f"rank={item.rank} "
            f"rank_gain={rank_gain} "
            f"composite_gain={composite_gain:.4f} "
            f"mom_raw={item.momentum_raw:.4f} "
            f"mom_z={item.momentum_z:.2f} "
            f"curv={item.curvature_raw:.6f} "
            f"hurst={item.hurst:.3f} "
            f"vei={vei} "
            f"regime_passes={intraday_regime.pass_count}/{intraday_regime.total_checks} "
            f"blockers={blockers}"
        )

    def _classify_emerging_signal(
        self,
        ticker: str,
        item: TickerMetrics,
        observed_at_ms: int,
        min_score: float | None,
        intraday_regime: IntradayRegimeContext,
    ) -> str:
        if not self._passes_core_filters(item, min_score):
            self.state.reset_intrabar(ticker)
            return "none"
        if item.rank > self.settings.watchlist_top_n:
            self.state.reset_intrabar(ticker)
            return "none"
        self.state.record_intrabar_observation(
            symbol=ticker,
            observed_at_ms=observed_at_ms,
            rank=item.rank,
            composite_score=item.composite_score,
        )
        if (
            item.rank <= self.settings.entry_ready_top_n
            and self._is_strengthening_intrabar(
                ticker,
                min_observations=self.settings.entry_ready_min_observations,
                min_rank_improvement=self.settings.entry_ready_min_rank_improvement,
                min_composite_gain=self.settings.entry_ready_min_composite_gain,
            )
        ):
            if intraday_regime.tradeable:
                return "entry_ready"
        if item.rank <= self.settings.emerging_top_n and self._is_strengthening_intrabar(ticker):
            return "emerging"
        return "watchlist"

    async def process(
        self,
        cycle_time_ms: int | None = None,
        stage: str = "confirmed",
    ) -> list[RankedSignal]:
        if stage != "emerging":
            return []
        include_provisional = stage == "emerging"
        metrics = self._compute_metrics(stage=stage)
        if not metrics:
            return []
        persist_signal_rows = not (
            self.settings.backtest_mode and self.settings.backtest_research_fast
        )

        regime_score, dominance_state = self._compute_regime(include_provisional=include_provisional)
        min_score = self.settings.regime_thresholds.get(regime_score)
        dominance_adjustment = self._dominance_score_adjustment(dominance_state)
        if min_score is not None:
            min_score = min_score + dominance_adjustment
        if self.settings.backtest_mode and self.settings.backtest_research_fast:
            if stage == "confirmed" or not self.settings.intraday_regime_filter_enabled:
                intraday_regime = self._default_intraday_regime_context()
            else:
                intraday_regime = self._compute_intraday_regime(
                    metrics,
                    include_provisional=include_provisional,
                )
        else:
            intraday_regime = self._compute_intraday_regime(
                metrics,
                include_provisional=include_provisional,
            )
        self.last_intraday_regime[stage] = intraday_regime
        ranked_signals: list[RankedSignal] = []
        records: list[SignalRecord] = []
        now = (
            datetime.fromtimestamp(cycle_time_ms / 1000, tz=timezone.utc)
            if cycle_time_ms is not None and (stage == "confirmed" or self.settings.backtest_mode)
            else datetime.now(timezone.utc)
        )
        observed_at_ms = int(now.timestamp() * 1000)
        dom_falling = dominance_state < 0
        dom_state = self._dominance_label(dominance_state)
        dom_change_pct = self.state.global_state.btcdom_change_pct
        for ticker, item in metrics.items():
            persistence_hits = 0
            if stage == "emerging":
                signal_kind = self._classify_emerging_signal(
                    ticker=ticker,
                    item=item,
                    observed_at_ms=observed_at_ms,
                    min_score=min_score,
                    intraday_regime=intraday_regime,
                )
                should_signal = signal_kind != "none"
                previous_kind = self.state.intrabar_state.get(ticker, "neutral")
                alert_key = (ticker, signal_kind)
                last_alerted = self.state.last_intrabar_alerted.get(alert_key)
                cooldown = timedelta(
                    minutes=(
                        self.settings.watchlist_cooldown_minutes
                        if signal_kind == "watchlist"
                        else self.settings.entry_ready_cooldown_minutes
                        if signal_kind == "entry_ready"
                        else self.settings.emerging_cooldown_minutes
                    )
                )
                eligible_to_alert = bool(
                    should_signal
                    and signal_kind != previous_kind
                    and (last_alerted is None or now - last_alerted >= cooldown)
                )
                if not self.settings.telegram_signal_alerts_enabled:
                    eligible_to_alert = False
                if signal_kind == "watchlist" and not self.settings.watchlist_telegram_enabled:
                    eligible_to_alert = False
                self.state.intrabar_state[ticker] = signal_kind if should_signal else "neutral"
            alerted = False

            if not should_signal:
                self.state.intrabar_state[ticker] = "neutral"
                if persist_signal_rows:
                    records.append(
                        SignalRecord(
                            timestamp=now.isoformat(),
                            stage=stage,
                            signal_kind="none",
                            ticker=ticker,
                            momentum_z=item.momentum_z,
                            curvature=item.curvature_raw,
                            hurst=item.hurst,
                            regime_score=regime_score,
                            composite_score=item.composite_score,
                            alerted=False,
                            price=item.current_price,
                            rank=item.rank,
                            persistence_hits=persistence_hits,
                            dom_falling=dom_falling,
                            dom_state=dom_state,
                            dom_change_pct=dom_change_pct,
                        )
                    )
                continue

            signal = RankedSignal(
                stage=stage,
                signal_kind=signal_kind,
                ticker=ticker,
                current_price=item.current_price,
                momentum_z=item.momentum_z,
                curvature=item.curvature_raw,
                hurst=item.hurst,
                regime_score=regime_score,
                composite_score=item.composite_score,
                rank=item.rank,
                persistence_hits=persistence_hits,
                alerted=False,
                cluster_label=item.cluster_label,
                momentum_reference_label=item.momentum_reference_label,
                entry_diagnostics=(
                    self._entry_diagnostics(ticker, item, intraday_regime)
                    if signal_kind == "entry_ready"
                    else ""
                ),
            )
            ranked_signals.append(signal)
            if eligible_to_alert:
                try:
                    alerted = await self.notifier.send(
                        AlertPayload(
                            stage=stage,
                            signal_kind=signal_kind,
                            ticker=ticker,
                            composite_score=item.composite_score,
                            momentum_z=item.momentum_z,
                            curvature=item.curvature_raw,
                            hurst=item.hurst,
                            current_price=item.current_price,
                            regime_score=regime_score,
                            rank=item.rank,
                            persistence_hits=persistence_hits,
                        )
                    )
                except Exception:
                    LOGGER.exception("Alert delivery failed for %s", ticker)
                    alerted = False
                signal.alerted = alerted
                if alerted:
                    self.state.last_intrabar_alerted[(ticker, signal_kind)] = now
                    self.state.last_alerted[ticker] = now
            if persist_signal_rows:
                records.append(
                    SignalRecord(
                        timestamp=now.isoformat(),
                        stage=stage,
                        signal_kind=signal_kind,
                        ticker=ticker,
                        momentum_z=item.momentum_z,
                        curvature=item.curvature_raw,
                        hurst=item.hurst,
                        regime_score=regime_score,
                        composite_score=item.composite_score,
                        alerted=alerted,
                        price=item.current_price,
                        rank=item.rank,
                        persistence_hits=persistence_hits,
                        dom_falling=dom_falling,
                        dom_state=dom_state,
                        dom_change_pct=dom_change_pct,
                    )
                )
        if persist_signal_rows and records:
            await self.database.log_signals(records)
        return ranked_signals
