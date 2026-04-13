from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from uuid import uuid4

import numpy as np

from alerting import ExecutionPayload, TelegramNotifier
from config import Settings
from database import SignalDatabase
from exchange import BybitTradeClient, WalletBalance, round_decimal
from indicators import correlation_cluster_labels
from signal_engine import RankedSignal
from state import MarketState
from universe import ticker_cluster

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class OrderSubmission:
    status: str
    fill_price: float | None
    notes: str
    external_order_id: str | None = None


@dataclass(slots=True)
class ExecutionAction:
    ticker: str
    action: str
    signal_kind: str
    detail: str
    order_id: int | None = None
    position_id: int | None = None


class SimulatedExecutionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def enabled(self) -> bool:
        return not self.settings.execution_submit_orders

    async def submit_market_order(
        self,
        *,
        ticker: str,
        side: str,
        quantity: float,
        price_hint: float,
        reduce_only: bool,
    ) -> OrderSubmission:
        mode = "demo" if self.settings.demo_mode else "paper"
        return OrderSubmission(
            status="simulated_fill",
            fill_price=price_hint,
            notes=(
                f"Local {mode} fill only. Private Bybit order submission is disabled; "
                f"qty={quantity:.8f} reduce_only={str(reduce_only).lower()}."
            ),
        )


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        state: MarketState,
        database: SignalDatabase,
        notifier: TelegramNotifier | None = None,
        client: BybitTradeClient | SimulatedExecutionClient | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.database = database
        self.notifier = notifier
        self.client = client or SimulatedExecutionClient(settings)
        self.runtime_run_id: str | None = None

    def set_runtime_run_id(self, run_id: str | None) -> None:
        self.runtime_run_id = run_id

    def _using_live_venue(self) -> bool:
        enabled = getattr(self.client, "enabled", None)
        return bool(self.settings.execution_submit_orders and callable(enabled) and enabled())

    def _now(self, cycle_time_ms: int | None, stage: str) -> datetime:
        if cycle_time_ms is not None and (stage == "confirmed" or self.settings.backtest_mode):
            return datetime.fromtimestamp(cycle_time_ms / 1000, tz=timezone.utc)
        return datetime.now(timezone.utc)

    def _latest_price(self, ticker: str, stage: str) -> float | None:
        prices = self.state.get_prices(ticker, include_provisional=stage == "emerging")
        if prices.size == 0:
            return None
        return float(prices[-1])

    def _estimate_quantity(self, price: float) -> float:
        if price <= 0:
            raise ValueError("Cannot size an order with non-positive price")
        return round(self.settings.entry_notional_usd / price, 8)

    def _risk_notional_from_wallet(self, wallet_balance: WalletBalance) -> Decimal:
        if self.settings.stop_loss_pct <= 0:
            raise ValueError("STOP_LOSS_PCT must be positive for risk-based sizing")
        risk_budget_usd = wallet_balance.total_available_balance_usd * Decimal(
            str(self.settings.risk_per_trade_pct)
        )
        if risk_budget_usd <= 0:
            raise ValueError("Available balance is zero; cannot size a new trade")
        notional_usd = risk_budget_usd / Decimal(str(self.settings.stop_loss_pct))
        if notional_usd <= 0:
            raise ValueError("Computed risk notional is zero; cannot size a new trade")
        return notional_usd

    def _client_order_id(self, lifecycle: str, ticker: str) -> str:
        return f"{lifecycle}-{ticker.lower()}-{uuid4().hex[:12]}"

    def _infer_exit_event(self, entry_price: float, exit_price: float) -> str:
        return self._infer_exit_event_with_targets(
            entry_price=entry_price,
            exit_price=exit_price,
            take_profit_price=entry_price * (1 + self.settings.take_profit_pct),
            stop_loss_price=entry_price * (1 - self.settings.stop_loss_pct),
        )

    def _infer_exit_event_with_targets(
        self,
        *,
        entry_price: float,
        exit_price: float,
        take_profit_price: float,
        stop_loss_price: float,
    ) -> str:
        if exit_price >= take_profit_price:
            return "take_profit_exit"
        if exit_price <= stop_loss_price:
            if stop_loss_price >= entry_price:
                return "protected_profit_exit"
            return "stop_loss_exit"
        return "exchange_exit"

    def _position_take_profit_price(self, position) -> float:
        value = position["take_profit_price"]
        if value not in (None, ""):
            return float(value)
        return float(position["entry_price"]) * (1 + self.settings.take_profit_pct)

    def _position_stop_loss_price(self, position) -> float:
        value = position["stop_loss_price"]
        if value not in (None, ""):
            return float(value)
        return float(position["entry_price"]) * (1 - self.settings.stop_loss_pct)

    def _position_profit_protection_adjustments(self, position) -> int:
        value = position["profit_protection_adjustments"]
        if value in (None, ""):
            return 0
        return int(value)

    def _protected_exit_reason(self, position) -> str:
        if self._position_stop_loss_price(position) >= float(position["entry_price"]):
            return "protected_profit_hit"
        return "stop_loss_hit"

    def _profit_protection_signal_by_ticker(
        self,
        ranked_signals: list[RankedSignal],
    ) -> dict[str, RankedSignal]:
        return {
            signal.ticker: signal
            for signal in ranked_signals
            if signal.signal_kind == "entry_ready" and signal.rank <= self.settings.profit_protection_max_rank
        }

    def _eligible_strength_signal_by_ticker(
        self,
        ranked_signals: list[RankedSignal],
        *,
        max_rank: int,
    ) -> dict[str, RankedSignal]:
        return {
            signal.ticker: signal
            for signal in ranked_signals
            if signal.signal_kind == "entry_ready" and signal.rank <= max_rank
        }

    def _holding_minutes(self, opened_at: str, now_iso: str) -> float:
        return (datetime.fromisoformat(now_iso) - datetime.fromisoformat(opened_at)).total_seconds() / 60.0

    def _reentry_cooldown_active(self, closed_at_iso: str | None, now_iso: str) -> bool:
        if not closed_at_iso or self.settings.reentry_cooldown_after_profit_minutes <= 0:
            return False
        elapsed_minutes = self._holding_minutes(closed_at_iso, now_iso)
        return elapsed_minutes < self.settings.reentry_cooldown_after_profit_minutes

    def _reentry_improvement_satisfied(
        self,
        signal: RankedSignal,
        *,
        prior_exit_rank: int | None,
        prior_exit_composite_score: float | None,
    ) -> bool:
        rank_checks: list[bool] = []
        if (
            prior_exit_rank is not None
            and self.settings.reentry_after_profit_min_rank_improvement > 0
        ):
            target_rank = max(
                1,
                prior_exit_rank - self.settings.reentry_after_profit_min_rank_improvement,
            )
            rank_checks.append(signal.rank <= target_rank)
        composite_checks: list[bool] = []
        if (
            prior_exit_composite_score is not None
            and self.settings.reentry_after_profit_min_composite_improvement > 0
        ):
            composite_checks.append(
                signal.composite_score
                >= (
                    prior_exit_composite_score
                    + self.settings.reentry_after_profit_min_composite_improvement
                )
            )
        checks = rank_checks + composite_checks
        if not checks:
            return True
        return any(checks)

    def _minutes_to_first_profit_target(
        self,
        open_marks: list,
        *,
        opened_at_iso: str,
        threshold_pct: float,
    ) -> float | None:
        opened_at = datetime.fromisoformat(opened_at_iso)
        for mark in open_marks:
            if float(mark["favorable_excursion_pct"]) + 1e-12 < threshold_pct:
                continue
            timestamp = mark["timestamp"]
            if timestamp in (None, ""):
                continue
            return max(
                0.0,
                (
                    datetime.fromisoformat(str(timestamp)) - opened_at
                ).total_seconds()
                / 60.0,
            )
        return None

    def _should_exit_stale_winner(
        self,
        *,
        position,
        current_price: float,
        now_iso: str,
        strong_signal: RankedSignal | None,
    ) -> bool:
        if not self.settings.stale_winner_exit_enabled:
            return False
        if self.settings.stale_winner_require_profit_protection and self._position_profit_protection_adjustments(position) <= 0:
            return False
        holding_minutes = self._holding_minutes(str(position["opened_at"]), now_iso)
        if holding_minutes < self.settings.stale_winner_min_hold_minutes:
            return False
        entry_price = float(position["entry_price"])
        pnl_pct = self._directional_pnl_pct(str(position["side"]), entry_price, current_price)
        if pnl_pct < self.settings.stale_winner_min_profit_pct:
            return False
        return strong_signal is None

    def _directional_pnl_pct(self, side: str, entry_price: float, price: float) -> float:
        if entry_price <= 0:
            return 0.0
        if side.upper() == "SHORT":
            return 1.0 - (price / entry_price)
        return (price / entry_price) - 1.0

    def _directional_excursions(
        self,
        side: str,
        entry_price: float,
        price: float,
    ) -> tuple[float, float]:
        pnl_pct = self._directional_pnl_pct(side, entry_price, price)
        favorable = max(pnl_pct, 0.0)
        adverse = max(-pnl_pct, 0.0)
        return favorable, adverse

    def _rolling_volatility(self, prices: list[float]) -> float | None:
        if len(prices) < 3:
            return None
        returns = np.diff(np.log(np.asarray(prices, dtype=float)))
        if returns.size < 2:
            return None
        return float(np.std(returns, ddof=1))

    def _current_cluster_labels(self, stage: str) -> dict[str, str]:
        if self.settings.cluster_assignment_mode == "manual":
            return {
                symbol: f"manual:{ticker_cluster(symbol)}"
                for symbol in self.settings.universe
            }
        include_provisional = stage == "emerging"
        price_history_by_symbol = {
            symbol: self.state.get_prices(symbol, include_provisional=include_provisional)
            for symbol in self.settings.universe
            if self.state.get_prices(symbol, include_provisional=include_provisional).size
            >= max(self.settings.cluster_correlation_lookback_bars + 1, 4)
        }
        labels = correlation_cluster_labels(
            price_history_by_symbol,
            lookback_bars=max(self.settings.cluster_correlation_lookback_bars, 3),
            threshold=self.settings.cluster_correlation_threshold,
        )
        if self.settings.cluster_assignment_mode == "dynamic":
            return labels
        if self.settings.cluster_assignment_mode == "hybrid":
            resolved: dict[str, str] = {}
            for symbol in self.settings.universe:
                label = labels.get(symbol)
                if label is None or label.startswith("solo:"):
                    resolved[symbol] = f"manual:{ticker_cluster(symbol)}"
                else:
                    resolved[symbol] = label
            return resolved
        raise ValueError(f"Unsupported CLUSTER_ASSIGNMENT_MODE: {self.settings.cluster_assignment_mode}")

    async def _log_runtime_event(
        self,
        *,
        severity: str,
        event_type: str,
        detail: str,
        stage: str | None = None,
        ticker: str | None = None,
    ) -> None:
        await self.database.log_runtime_event(
            run_id=self.runtime_run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            severity=severity,
            component="execution",
            event_type=event_type,
            detail=detail,
            stage=stage,
            ticker=ticker,
        )

    def _open_cluster_counts(self, open_positions: dict[str, dict], *, stage: str) -> dict[str, int]:
        cluster_labels = self._current_cluster_labels(stage)
        counts: dict[str, int] = {}
        for ticker in open_positions:
            cluster = cluster_labels.get(str(ticker), f"manual:{ticker_cluster(str(ticker))}")
            counts[cluster] = counts.get(cluster, 0) + 1
        return counts

    async def _record_position_mark(
        self,
        *,
        position,
        stage: str,
        timestamp_iso: str,
        price: float,
        phase: str,
        bars_since_entry: int = 0,
        bars_since_exit: int = 0,
    ) -> None:
        if not self.settings.analytics_enabled or not self.settings.analytics_log_position_marks:
            return
        side = str(position["side"])
        entry_price = float(position["entry_price"])
        pnl_pct = self._directional_pnl_pct(side, entry_price, price)
        favorable_excursion_pct, adverse_excursion_pct = self._directional_excursions(
            side,
            entry_price,
            price,
        )
        existing_marks = await self.database.list_position_marks(int(position["id"]), phase=phase)
        prices = [float(mark["price"]) for mark in existing_marks] + [price]
        rolling_volatility = self._rolling_volatility(prices)
        if existing_marks:
            favorable_excursion_pct = max(
                favorable_excursion_pct,
                max(float(mark["favorable_excursion_pct"]) for mark in existing_marks),
            )
            adverse_excursion_pct = max(
                adverse_excursion_pct,
                max(float(mark["adverse_excursion_pct"]) for mark in existing_marks),
            )
        if phase == "open" and bars_since_entry == 0:
            bars_since_entry = len(existing_marks) + 1
        if phase == "post_exit" and bars_since_exit == 0:
            bars_since_exit = len(existing_marks) + 1
        await self.database.log_position_mark(
            position_id=int(position["id"]),
            ticker=str(position["ticker"]),
            side=side,
            phase=phase,
            timestamp=timestamp_iso,
            stage=stage,
            price=price,
            pnl_pct=pnl_pct,
            favorable_excursion_pct=favorable_excursion_pct,
            adverse_excursion_pct=adverse_excursion_pct,
            rolling_volatility=rolling_volatility,
            regime_score=self.state.global_state.btc_regime_score,
            dom_state=(
                "falling"
                if self.state.global_state.btcdom_state < 0
                else "rising"
                if self.state.global_state.btcdom_state > 0
                else "neutral"
            ),
            dom_change_pct=self.state.global_state.btcdom_change_pct,
            bars_since_entry=bars_since_entry,
            bars_since_exit=bars_since_exit,
        )

    async def _record_open_position_marks(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
    ) -> None:
        if not self.settings.analytics_enabled or not self.settings.analytics_log_position_marks:
            return
        timestamp_iso = self._now(cycle_time_ms=cycle_time_ms, stage=stage).isoformat()
        for position in await self.database.list_open_positions():
            price = self._latest_price(str(position["ticker"]), stage=stage)
            if price is None:
                continue
            await self._record_position_mark(
                position=position,
                stage=stage,
                timestamp_iso=timestamp_iso,
                price=price,
                phase="open",
            )

    async def _maybe_adjust_open_positions(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
        ranked_signals: list[RankedSignal],
    ) -> list[ExecutionAction]:
        if stage != "emerging" or not self.settings.profit_protection_enabled:
            return []
        eligible_signals = self._profit_protection_signal_by_ticker(ranked_signals)
        if not eligible_signals:
            return []
        now_iso = self._now(cycle_time_ms=cycle_time_ms, stage=stage).isoformat()
        actions: list[ExecutionAction] = []
        for position in await self.database.list_open_positions():
            ticker = str(position["ticker"])
            signal = eligible_signals.get(ticker)
            if signal is None:
                continue
            adjustment_count = self._position_profit_protection_adjustments(position)
            if adjustment_count >= self.settings.profit_protection_max_adjustments:
                continue
            entry_price = float(position["entry_price"])
            current_price = self._latest_price(ticker, stage=stage)
            if current_price is None:
                continue
            if current_price < entry_price * (1 + self.settings.profit_protection_trigger_pct):
                continue
            current_tp = self._position_take_profit_price(position)
            current_sl = self._position_stop_loss_price(position)
            target_tp = entry_price * (
                1
                + self.settings.take_profit_pct
                + self.settings.profit_protection_tp_extension_pct * (adjustment_count + 1)
            )
            target_sl = entry_price * (1 + self.settings.profit_protection_sl_lock_pct)
            new_tp = max(current_tp, target_tp)
            new_sl = max(current_sl, target_sl)
            if new_tp <= current_tp and new_sl <= current_sl:
                continue

            if self._using_live_venue():
                venue_position = await self.client.get_position(ticker)
                if venue_position is None or venue_position.size <= 0:
                    continue
                spec = await self.client.fetch_instrument_spec(ticker)
                tp_decimal = round_decimal(
                    Decimal(str(new_tp)),
                    spec.tick_size,
                    ROUND_HALF_UP,
                )
                sl_decimal = round_decimal(
                    Decimal(str(new_sl)),
                    spec.tick_size,
                    ROUND_HALF_UP,
                )
                await self.client.set_trading_stop(
                    symbol=ticker,
                    position_idx=venue_position.position_idx,
                    take_profit=tp_decimal,
                    stop_loss=sl_decimal,
                )
                new_tp = float(tp_decimal)
                new_sl = float(sl_decimal)

            note = (
                f"profit protection adjusted tp={new_tp:.6f} sl={new_sl:.6f} "
                f"signal_rank={signal.rank} composite={signal.composite_score:.3f}"
            )
            await self.database.update_position_protection(
                int(position["id"]),
                take_profit_price=new_tp,
                stop_loss_price=new_sl,
                profit_protection_adjustments=adjustment_count + 1,
                updated_at=now_iso,
                notes=note,
            )
            await self._log_runtime_event(
                severity="info",
                event_type="profit_protection_adjusted",
                detail=note,
                stage=stage,
                ticker=ticker,
            )
            actions.append(
                ExecutionAction(
                    ticker=ticker,
                    action="adjust_profit_protection",
                    signal_kind=signal.signal_kind,
                    detail=note,
                    position_id=int(position["id"]),
                )
            )
        return actions

    async def detect_position_drift(self) -> tuple[str, str]:
        if not self._using_live_venue():
            return ("disabled", "live venue not enabled")
        local_open = {str(row["ticker"]) for row in await self.database.list_open_positions()}
        venue_open: set[str] = set()
        for symbol in self.settings.universe:
            position = await self.client.get_position(symbol)
            if position is not None and position.size > 0:
                venue_open.add(symbol)
        local_only = sorted(local_open - venue_open)
        venue_only = sorted(venue_open - local_open)
        if not local_only and not venue_only:
            return ("ok", f"aligned local={len(local_open)} venue={len(venue_open)}")
        detail = (
            f"local_only={','.join(local_only) if local_only else 'none'} "
            f"venue_only={','.join(venue_only) if venue_only else 'none'}"
        )
        return ("mismatch", detail)

    async def _record_trade_analytics(
        self,
        *,
        position,
        exit_signal: RankedSignal | None,
        exit_stage: str,
        exit_signal_kind: str,
        exit_event: str,
        exit_reason: str,
        closed_at_iso: str,
        exit_price: float,
        take_profit_price_at_exit: float,
        stop_loss_price_at_exit: float,
    ) -> None:
        if not self.settings.analytics_enabled:
            return
        position_id = int(position["id"])
        open_marks = await self.database.list_position_marks(position_id, phase="open")
        prices = [float(mark["price"]) for mark in open_marks]
        side = str(position["side"])
        entry_price = float(position["entry_price"])
        quantity = float(position["quantity"])
        realized_pnl_pct = self._directional_pnl_pct(side, entry_price, exit_price)
        realized_pnl_usd = realized_pnl_pct * float(position["notional_usd"])
        holding_minutes = (
            datetime.fromisoformat(closed_at_iso) - datetime.fromisoformat(str(position["opened_at"]))
        ).total_seconds() / 60.0
        if open_marks:
            mfe_pct = max(float(mark["favorable_excursion_pct"]) for mark in open_marks)
            mae_pct = max(float(mark["adverse_excursion_pct"]) for mark in open_marks)
            favorable_mark = max(
                open_marks,
                key=lambda mark: float(mark["favorable_excursion_pct"]),
            )
            adverse_mark = max(
                open_marks,
                key=lambda mark: float(mark["adverse_excursion_pct"]),
            )
            peak_favorable_price = float(favorable_mark["price"])
            peak_adverse_price = float(adverse_mark["price"])
        else:
            favorable, adverse = self._directional_excursions(side, entry_price, exit_price)
            mfe_pct = favorable
            mae_pct = adverse
            peak_favorable_price = exit_price
            peak_adverse_price = exit_price
        minutes_to_first_profit_50bps = self._minutes_to_first_profit_target(
            open_marks,
            opened_at_iso=str(position["opened_at"]),
            threshold_pct=0.005,
        )
        minutes_to_first_profit_100bps = self._minutes_to_first_profit_target(
            open_marks,
            opened_at_iso=str(position["opened_at"]),
            threshold_pct=0.010,
        )
        await self.database.record_trade_analytics(
            position_id=position_id,
            ticker=str(position["ticker"]),
            side=side,
            entry_stage=str(position["entry_stage"]),
            entry_signal_kind=str(position["entry_signal_kind"]),
            exit_stage=exit_stage,
            exit_signal_kind=exit_signal_kind,
            exit_event=exit_event,
            exit_reason=exit_reason,
            opened_at=str(position["opened_at"]),
            closed_at=closed_at_iso,
            holding_minutes=holding_minutes,
            bars_held=len(open_marks),
            quantity=quantity,
            notional_usd=float(position["notional_usd"]),
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl_pct=realized_pnl_pct,
            realized_pnl_usd=realized_pnl_usd,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            peak_favorable_price=peak_favorable_price,
            peak_adverse_price=peak_adverse_price,
            intratrade_volatility=self._rolling_volatility(prices),
            regime_score_at_entry=position["regime_score_at_entry"],
            dom_state_at_entry=position["dom_state_at_entry"],
            dom_change_pct_at_entry=position["dom_change_pct_at_entry"],
            entry_rank=position["entry_rank"],
            entry_composite_score=position["entry_composite_score"],
            entry_momentum_z=position["entry_momentum_z"],
            entry_curvature=position["entry_curvature"],
            entry_hurst=position["entry_hurst"],
            exit_rank=exit_signal.rank if exit_signal is not None else None,
            exit_composite_score=exit_signal.composite_score if exit_signal is not None else None,
            exit_momentum_z=exit_signal.momentum_z if exit_signal is not None else None,
            exit_curvature=exit_signal.curvature if exit_signal is not None else None,
            exit_hurst=exit_signal.hurst if exit_signal is not None else None,
            take_profit_price_at_exit=take_profit_price_at_exit,
            stop_loss_price_at_exit=stop_loss_price_at_exit,
            profit_protection_adjustments=self._position_profit_protection_adjustments(position),
            minutes_to_first_profit_50bps=minutes_to_first_profit_50bps,
            minutes_to_first_profit_100bps=minutes_to_first_profit_100bps,
            notes=str(position["notes"] or ""),
            post_exit_bars_target=max(self.settings.analytics_post_exit_bars, 0),
            post_exit_bars_observed=0,
            post_exit_completed=int(self.settings.analytics_post_exit_bars <= 0),
            post_exit_tracking_started_at=closed_at_iso if self.settings.analytics_post_exit_bars > 0 else None,
            post_exit_tracking_completed_at=closed_at_iso if self.settings.analytics_post_exit_bars <= 0 else None,
            post_exit_last_price=exit_price,
            post_exit_best_price=exit_price,
            post_exit_worst_price=exit_price,
            post_exit_favorable_excursion_pct=0.0,
            post_exit_adverse_excursion_pct=0.0,
            post_exit_volatility=None,
        )
        day_summary = await self.database.summarize_trade_day(closed_at_iso[:10])
        LOGGER.info(
            "Daily trade summary %s trades=%s wins=%s losses=%s stop_losses=%s net_pnl_usd=%.2f",
            closed_at_iso[:10],
            int(day_summary["trades"]),
            int(day_summary["wins"]),
            int(day_summary["losses"]),
            int(day_summary["stop_losses"]),
            float(day_summary["net_pnl_usd"]),
        )

    async def _update_post_exit_analytics(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
    ) -> None:
        if not self.settings.analytics_enabled or self.settings.analytics_post_exit_bars <= 0:
            return
        trackers = await self.database.list_pending_post_exit_analytics()
        if not trackers:
            return
        timestamp_iso = self._now(cycle_time_ms=cycle_time_ms, stage=stage).isoformat()
        for tracker in trackers:
            price = self._latest_price(str(tracker["ticker"]), stage=stage)
            if price is None:
                continue
            side = str(tracker["side"])
            exit_price = float(tracker["exit_price"])
            favorable, adverse = self._directional_excursions(side, exit_price, price)
            existing_marks = await self.database.list_position_marks(int(tracker["position_id"]), phase="post_exit")
            prices = [float(mark["price"]) for mark in existing_marks] + [price]
            best_price = min([price] + [float(mark["price"]) for mark in existing_marks]) if side == "SHORT" else max([price] + [float(mark["price"]) for mark in existing_marks])
            worst_price = max([price] + [float(mark["price"]) for mark in existing_marks]) if side == "SHORT" else min([price] + [float(mark["price"]) for mark in existing_marks])
            await self.database.log_position_mark(
                position_id=int(tracker["position_id"]),
                ticker=str(tracker["ticker"]),
                side=side,
                phase="post_exit",
                timestamp=timestamp_iso,
                stage=stage,
                price=price,
                pnl_pct=self._directional_pnl_pct(side, exit_price, price),
                favorable_excursion_pct=max(
                    favorable,
                    float(tracker["post_exit_favorable_excursion_pct"]),
                ),
                adverse_excursion_pct=max(
                    adverse,
                    float(tracker["post_exit_adverse_excursion_pct"]),
                ),
                rolling_volatility=self._rolling_volatility(prices),
                regime_score=self.state.global_state.btc_regime_score,
                dom_state=(
                    "falling"
                    if self.state.global_state.btcdom_state < 0
                    else "rising"
                    if self.state.global_state.btcdom_state > 0
                    else "neutral"
                ),
                dom_change_pct=self.state.global_state.btcdom_change_pct,
                bars_since_entry=0,
                bars_since_exit=len(existing_marks) + 1,
            )
            observed = int(tracker["post_exit_bars_observed"]) + 1
            completed = observed >= int(tracker["post_exit_bars_target"])
            tracking_started_at = (
                str(tracker["post_exit_tracking_started_at"])
                if tracker["post_exit_tracking_started_at"] not in (None, "")
                else timestamp_iso
            )
            await self.database.update_trade_post_exit(
                position_id=int(tracker["position_id"]),
                post_exit_bars_observed=observed,
                post_exit_completed=completed,
                post_exit_tracking_started_at=tracking_started_at,
                post_exit_tracking_completed_at=timestamp_iso if completed else None,
                post_exit_last_price=price,
                post_exit_best_price=best_price,
                post_exit_worst_price=worst_price,
                post_exit_favorable_excursion_pct=max(
                    favorable,
                    float(tracker["post_exit_favorable_excursion_pct"]),
                ),
                post_exit_adverse_excursion_pct=max(
                    adverse,
                    float(tracker["post_exit_adverse_excursion_pct"]),
                ),
                post_exit_volatility=self._rolling_volatility(prices),
            )

    async def _maybe_log_portfolio_snapshot(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
        event: str,
        notes: str = "",
    ) -> None:
        if not self.settings.analytics_enabled or not self.settings.analytics_log_portfolio_snapshots:
            return
        if stage == "emerging" and not self.settings.analytics_portfolio_snapshot_on_emerging:
            return
        timestamp_iso = self._now(cycle_time_ms=cycle_time_ms, stage=stage).isoformat()
        open_positions = await self.database.list_open_positions()
        gross_notional_usd = sum(float(position["notional_usd"]) for position in open_positions)
        remaining_slots = None
        if self.settings.max_open_positions > 0:
            remaining_slots = max(self.settings.max_open_positions - len(open_positions), 0)
        wallet_equity_usd = None
        available_balance_usd = None
        risk_budget_usd = None
        target_notional_usd = None
        balance_position_capacity = None
        if self._using_live_venue():
            try:
                wallet = await self.client.get_wallet_balance()
                wallet_equity_usd = float(wallet.total_equity_usd)
                available_balance_usd = float(wallet.total_available_balance_usd)
                risk_budget_usd = available_balance_usd * self.settings.risk_per_trade_pct
                target_notional_usd = float(self._risk_notional_from_wallet(wallet))
                if target_notional_usd > 0:
                    balance_position_capacity = int(available_balance_usd // target_notional_usd)
            except Exception:
                LOGGER.exception("Portfolio snapshot wallet fetch failed")
        daily_stop_loss_count = await self.database.count_trade_exit_events_for_day(
            "stop_loss_exit",
            timestamp_iso[:10],
        )
        await self.database.log_portfolio_snapshot(
            timestamp=timestamp_iso,
            stage=stage,
            event=event,
            wallet_equity_usd=wallet_equity_usd,
            available_balance_usd=available_balance_usd,
            open_positions=len(open_positions),
            gross_notional_usd=gross_notional_usd,
            remaining_slots=remaining_slots,
            balance_position_capacity=balance_position_capacity,
            risk_budget_usd=risk_budget_usd,
            target_notional_usd=target_notional_usd,
            daily_stop_loss_count=daily_stop_loss_count,
            notes=notes,
        )

    async def _send_execution_notification(
        self,
        *,
        event: str,
        ticker: str,
        stage: str,
        signal_kind: str,
        quantity: float,
        entry_price: float | None = None,
        exit_price: float | None = None,
        current_price: float | None = None,
        pnl_pct: float | None = None,
        notes: str = "",
    ) -> None:
        if self.notifier is None or not self.notifier.enabled:
            return
        try:
            await self.notifier.send_execution(
                ExecutionPayload(
                    event=event,
                    ticker=ticker,
                    side="LONG",
                    stage=stage,
                    signal_kind=signal_kind,
                    quantity=quantity,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    notes=notes,
                )
            )
        except Exception:
            LOGGER.exception("Execution notification failed for %s %s", event, ticker)

    async def _submit_simulated_exit(
        self,
        *,
        position,
        ticker: str,
        stage: str,
        now_iso: str,
        exit_price: float,
        exit_reason: str,
        signal_kind: str = "none",
        exit_signal: RankedSignal | None = None,
        explicit_event: str | None = None,
    ) -> ExecutionAction:
        quantity = float(position["quantity"])
        submission = await self.client.submit_market_order(
            ticker=ticker,
            side="Sell",
            quantity=quantity,
            price_hint=exit_price,
            reduce_only=True,
        )
        order_id = await self.database.create_order(
            client_order_id=self._client_order_id("exit", ticker),
            ticker=ticker,
            side="Sell",
            order_type="Market",
            lifecycle="exit",
            status=submission.status,
            stage=stage,
            signal_kind=signal_kind,
            requested_qty=quantity,
            requested_notional_usd=float(position["notional_usd"]),
            requested_price=exit_price,
            venue="bybit",
            is_demo=self.settings.demo_mode,
            created_at=now_iso,
            updated_at=now_iso,
            fill_price=submission.fill_price,
            external_order_id=submission.external_order_id,
            filled_at=now_iso if submission.fill_price is not None else None,
            notes=submission.notes,
        )
        realized_price = float(submission.fill_price or exit_price)
        pnl_pct = (realized_price / float(position["entry_price"])) - 1.0
        take_profit_price_at_exit = self._position_take_profit_price(position)
        stop_loss_price_at_exit = self._position_stop_loss_price(position)
        exit_event = explicit_event or self._infer_exit_event_with_targets(
            entry_price=float(position["entry_price"]),
            exit_price=realized_price,
            take_profit_price=take_profit_price_at_exit,
            stop_loss_price=stop_loss_price_at_exit,
        )
        await self.database.close_position(
            int(position["id"]),
            exit_order_id=order_id,
            exit_price=realized_price,
            closed_at=now_iso,
            updated_at=now_iso,
            notes=exit_reason,
        )
        await self._record_trade_analytics(
            position=position,
            exit_signal=exit_signal,
            exit_stage=stage,
            exit_signal_kind=signal_kind,
            exit_event=exit_event,
            exit_reason=exit_reason,
            closed_at_iso=now_iso,
            exit_price=realized_price,
            take_profit_price_at_exit=take_profit_price_at_exit,
            stop_loss_price_at_exit=stop_loss_price_at_exit,
        )
        await self._send_execution_notification(
            event=exit_event,
            ticker=ticker,
            stage=stage,
            signal_kind=signal_kind,
            quantity=quantity,
            entry_price=float(position["entry_price"]),
            exit_price=realized_price,
            pnl_pct=pnl_pct,
            notes=submission.notes,
        )
        return ExecutionAction(
            ticker=ticker,
            action="exit_long",
            signal_kind=signal_kind,
            detail=exit_reason,
            order_id=order_id,
            position_id=int(position["id"]),
        )

    async def _process_risk_exits(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
        ranked_signals: list[RankedSignal],
    ) -> tuple[list[ExecutionAction], set[str]]:
        actions: list[ExecutionAction] = []
        closed_tickers: set[str] = set()
        now_iso = self._now(cycle_time_ms=cycle_time_ms, stage=stage).isoformat()
        signal_by_ticker = {signal.ticker: signal for signal in ranked_signals}
        strong_signal_by_ticker = self._eligible_strength_signal_by_ticker(
            ranked_signals,
            max_rank=self.settings.stale_winner_max_rank,
        )
        for position in await self.database.list_open_positions():
            ticker = str(position["ticker"])
            current_price = self._latest_price(ticker, stage=stage)
            if current_price is None:
                continue
            take_profit_price = self._position_take_profit_price(position)
            stop_loss_price = self._position_stop_loss_price(position)
            if current_price >= take_profit_price:
                actions.append(
                    await self._submit_simulated_exit(
                        position=position,
                        ticker=ticker,
                        stage=stage,
                        now_iso=now_iso,
                        exit_price=current_price,
                        exit_reason="take_profit_hit",
                        exit_signal=signal_by_ticker.get(ticker),
                    )
                )
                closed_tickers.add(ticker)
                continue
            if current_price <= stop_loss_price:
                actions.append(
                    await self._submit_simulated_exit(
                        position=position,
                        ticker=ticker,
                        stage=stage,
                        now_iso=now_iso,
                        exit_price=current_price,
                        exit_reason=self._protected_exit_reason(position),
                        exit_signal=signal_by_ticker.get(ticker),
                    )
                )
                closed_tickers.add(ticker)
                continue
            if self._should_exit_stale_winner(
                position=position,
                current_price=current_price,
                now_iso=now_iso,
                strong_signal=strong_signal_by_ticker.get(ticker),
            ):
                actions.append(
                    await self._submit_simulated_exit(
                        position=position,
                        ticker=ticker,
                        stage=stage,
                        now_iso=now_iso,
                        exit_price=current_price,
                        exit_reason="stale_winner_exit",
                        signal_kind=signal_by_ticker.get(ticker).signal_kind if signal_by_ticker.get(ticker) is not None else "none",
                        exit_signal=signal_by_ticker.get(ticker),
                        explicit_event="stale_winner_exit",
                    )
                )
                closed_tickers.add(ticker)
        return actions, closed_tickers

    async def _submit_live_reduce_only_exit(
        self,
        *,
        position,
        ticker: str,
        stage: str,
        now_iso: str,
        exit_reason: str,
    ) -> ExecutionAction | None:
        venue_position = await self.client.get_position(ticker)
        if venue_position is None or venue_position.size <= 0:
            return None
        order_link_id = self._client_order_id("exit-live", ticker)
        ack = await self.client.place_market_order(
            symbol=ticker,
            side="Sell",
            quantity=venue_position.size,
            order_link_id=order_link_id,
            reduce_only=True,
        )
        note = f"submitted reduce-only exit reason={exit_reason}"
        order_id = await self.database.create_order(
            client_order_id=order_link_id,
            ticker=ticker,
            side="Sell",
            order_type="Market",
            lifecycle="exit",
            status="submitted_live_reduce_only",
            stage=stage,
            signal_kind="none",
            requested_qty=float(venue_position.size),
            requested_notional_usd=float(venue_position.size * venue_position.avg_price),
            requested_price=float(venue_position.avg_price),
            venue="bybit",
            is_demo=self.settings.demo_mode,
            created_at=now_iso,
            updated_at=now_iso,
            external_order_id=ack.order_id,
            notes=note,
        )
        await self.database.set_position_pending_exit_reason(
            int(position["id"]),
            pending_exit_reason=exit_reason,
            updated_at=now_iso,
            notes=note,
        )
        await self._log_runtime_event(
            severity="info",
            event_type="live_exit_submitted",
            detail=note,
            stage=stage,
            ticker=ticker,
        )
        return ExecutionAction(
            ticker=ticker,
            action="exit_submitted",
            signal_kind="none",
            detail=exit_reason,
            order_id=order_id,
            position_id=int(position["id"]),
        )

    async def _process_live_stale_winner_exits(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
        ranked_signals: list[RankedSignal],
    ) -> tuple[list[ExecutionAction], set[str]]:
        if stage != "emerging":
            return [], set()
        actions: list[ExecutionAction] = []
        closed_tickers: set[str] = set()
        now_iso = self._now(cycle_time_ms=cycle_time_ms, stage=stage).isoformat()
        strong_signal_by_ticker = self._eligible_strength_signal_by_ticker(
            ranked_signals,
            max_rank=self.settings.stale_winner_max_rank,
        )
        for position in await self.database.list_open_positions():
            ticker = str(position["ticker"])
            current_price = self._latest_price(ticker, stage=stage)
            if current_price is None:
                continue
            if not self._should_exit_stale_winner(
                position=position,
                current_price=current_price,
                now_iso=now_iso,
                strong_signal=strong_signal_by_ticker.get(ticker),
            ):
                continue
            action = await self._submit_live_reduce_only_exit(
                position=position,
                ticker=ticker,
                stage=stage,
                now_iso=now_iso,
                exit_reason="stale_winner_exit",
            )
            if action is not None:
                actions.append(action)
                closed_tickers.add(ticker)
        return actions, closed_tickers

    async def _sync_live_closed_positions(
        self,
        *,
        stage: str,
    ) -> tuple[list[ExecutionAction], set[str]]:
        actions: list[ExecutionAction] = []
        closed_tickers: set[str] = set()
        if not self._using_live_venue():
            return actions, closed_tickers
        for position in await self.database.list_open_positions():
            ticker = str(position["ticker"])
            venue_position = await self.client.get_position(ticker)
            if venue_position is not None:
                continue
            closed = await self.client.get_latest_closed_pnl(ticker)
            if closed is None:
                continue
            opened_at_ms = int(datetime.fromisoformat(position["opened_at"]).timestamp() * 1000)
            if closed.updated_time_ms < opened_at_ms:
                continue
            now_iso = datetime.fromtimestamp(closed.updated_time_ms / 1000, tz=timezone.utc).isoformat()
            exit_price = float(closed.avg_exit_price)
            entry_price = float(position["entry_price"])
            pnl_pct = (exit_price / entry_price) - 1.0 if entry_price > 0 else None
            order_id = await self.database.create_order(
                client_order_id=self._client_order_id("exit-sync", ticker),
                ticker=ticker,
                side="Sell",
                order_type="Market",
                lifecycle="exit",
                status="filled_sync",
                stage=stage,
                signal_kind="none",
                requested_qty=float(position["quantity"]),
                requested_notional_usd=float(position["notional_usd"]),
                requested_price=exit_price,
                venue="bybit",
                is_demo=self.settings.demo_mode,
                created_at=now_iso,
                updated_at=now_iso,
                fill_price=exit_price,
                external_order_id=closed.order_id,
                filled_at=now_iso,
                notes="Synced from Bybit closed PnL",
            )
            await self.database.close_position(
                int(position["id"]),
                exit_order_id=order_id,
                exit_price=exit_price,
                closed_at=now_iso,
                updated_at=now_iso,
                notes="closed on Bybit venue",
            )
            pending_exit_reason = (
                str(position["pending_exit_reason"])
                if position["pending_exit_reason"] not in (None, "")
                else None
            )
            take_profit_price_at_exit = self._position_take_profit_price(position)
            stop_loss_price_at_exit = self._position_stop_loss_price(position)
            event = pending_exit_reason or self._infer_exit_event_with_targets(
                entry_price=entry_price,
                exit_price=exit_price,
                take_profit_price=take_profit_price_at_exit,
                stop_loss_price=stop_loss_price_at_exit,
            )
            await self._record_trade_analytics(
                position=position,
                exit_signal=None,
                exit_stage=stage,
                exit_signal_kind="none",
                exit_event=event,
                exit_reason=pending_exit_reason or "closed on Bybit venue",
                closed_at_iso=now_iso,
                exit_price=exit_price,
                take_profit_price_at_exit=take_profit_price_at_exit,
                stop_loss_price_at_exit=stop_loss_price_at_exit,
            )
            await self._send_execution_notification(
                event=event,
                ticker=ticker,
                stage=stage,
                signal_kind="none",
                quantity=float(position["quantity"]),
                entry_price=entry_price,
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                notes="Synced from Bybit closed PnL",
            )
            actions.append(
                ExecutionAction(
                    ticker=ticker,
                    action="exit_synced",
                    signal_kind="none",
                    detail=event,
                    order_id=order_id,
                    position_id=int(position["id"]),
                )
            )
            closed_tickers.add(ticker)
        return actions, closed_tickers

    async def process_cycle(
        self,
        *,
        stage: str,
        cycle_time_ms: int | None,
        ranked_signals: list[RankedSignal],
    ) -> list[ExecutionAction]:
        if not self.settings.execution_enabled:
            return []
        if self.settings.execution_submit_orders and not self._using_live_venue():
            raise RuntimeError(
                "EXECUTION_SUBMIT_ORDERS=true but the Bybit trade client is not ready. "
                "Check BYBIT_API_KEY, BYBIT_API_SECRET, and BYBIT_TRADE_BASE_URL."
            )
        await self._record_open_position_marks(stage=stage, cycle_time_ms=cycle_time_ms)
        if self._using_live_venue():
            sync_actions, closed_tickers = await self._sync_live_closed_positions(stage=stage)
        else:
            sync_actions, closed_tickers = await self._process_risk_exits(
                stage=stage,
                cycle_time_ms=cycle_time_ms,
                ranked_signals=ranked_signals,
            )
        if stage == "emerging":
            protection_actions = await self._maybe_adjust_open_positions(
                stage=stage,
                cycle_time_ms=cycle_time_ms,
                ranked_signals=ranked_signals,
            )
            if self._using_live_venue():
                stale_actions, stale_closed_tickers = await self._process_live_stale_winner_exits(
                    stage=stage,
                    cycle_time_ms=cycle_time_ms,
                    ranked_signals=ranked_signals,
                )
            else:
                stale_actions, stale_closed_tickers = ([], set())
            stage_actions = await self._process_entries(
                cycle_time_ms=cycle_time_ms,
                ranked_signals=ranked_signals,
                blocked_tickers=closed_tickers | stale_closed_tickers,
            )
            actions = sync_actions + protection_actions + stale_actions + stage_actions
            await self._maybe_log_portfolio_snapshot(
                stage=stage,
                cycle_time_ms=cycle_time_ms,
                event="cycle",
                notes="; ".join(f"{action.action}:{action.ticker}" for action in actions[:10]),
            )
            return actions
        if stage == "confirmed":
            await self._update_post_exit_analytics(stage=stage, cycle_time_ms=cycle_time_ms)
            actions = sync_actions
            await self._maybe_log_portfolio_snapshot(
                stage=stage,
                cycle_time_ms=cycle_time_ms,
                event="cycle",
                notes="; ".join(f"{action.action}:{action.ticker}" for action in actions[:10]),
            )
            return actions
        await self._maybe_log_portfolio_snapshot(
            stage=stage,
            cycle_time_ms=cycle_time_ms,
            event="cycle",
        )
        return sync_actions

    async def _process_entries(
        self,
        *,
        cycle_time_ms: int | None,
        ranked_signals: list[RankedSignal],
        blocked_tickers: set[str],
    ) -> list[ExecutionAction]:
        actions: list[ExecutionAction] = []
        now = self._now(cycle_time_ms=cycle_time_ms, stage="emerging")
        now_iso = now.isoformat()
        entered_this_rebalance = 0
        open_positions = {
            row["ticker"]: row for row in await self.database.list_open_positions()
        }
        cluster_counts = self._open_cluster_counts(open_positions, stage="emerging")
        daily_stop_loss_count = 0
        if self.settings.max_daily_stop_losses > 0:
            daily_stop_loss_count = await self.database.count_trade_exit_events_for_day(
                "stop_loss_exit",
                now_iso[:10],
            )
        candidates = [signal for signal in ranked_signals if signal.signal_kind == "entry_ready"]
        candidates.sort(key=lambda signal: signal.rank)
        for signal in candidates:
            if self.settings.operator_pause_new_entries:
                detail = "operator_pause_new_entries"
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="warning",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue
            if (
                self.settings.max_open_positions > 0
                and len(open_positions) >= self.settings.max_open_positions
            ):
                detail = (
                    "max_open_positions_reached "
                    f"count={len(open_positions)} "
                    f"limit={self.settings.max_open_positions}"
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="warning",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue
            if (
                self.settings.max_entries_per_rebalance > 0
                and entered_this_rebalance >= self.settings.max_entries_per_rebalance
            ):
                detail = (
                    "max_entries_per_rebalance_reached "
                    f"count={entered_this_rebalance} "
                    f"limit={self.settings.max_entries_per_rebalance}"
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="warning",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue
            cluster = signal.cluster_label or f"manual:{ticker_cluster(signal.ticker)}"
            if (
                self.settings.max_positions_per_cluster > 0
                and cluster_counts.get(cluster, 0) >= self.settings.max_positions_per_cluster
            ):
                detail = (
                    "max_positions_per_cluster_reached "
                    f"cluster={cluster} "
                    f"count={cluster_counts.get(cluster, 0)} "
                    f"limit={self.settings.max_positions_per_cluster}"
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="warning",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue
            if signal.ticker in blocked_tickers:
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail="ticker_closed_this_cycle",
                    )
                )
                continue
            if signal.ticker in open_positions:
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail="position_already_open",
                        position_id=int(open_positions[signal.ticker]["id"]),
                    )
                )
                continue
            latest_profitable_trade = await self.database.latest_profitable_trade_state_for_ticker(
                signal.ticker
            )
            latest_profitable_close = (
                str(latest_profitable_trade["closed_at"])
                if latest_profitable_trade is not None
                and latest_profitable_trade["closed_at"] not in (None, "")
                else None
            )
            if self._reentry_cooldown_active(latest_profitable_close, now_iso):
                detail = (
                    "reentry_cooldown_after_profit "
                    f"minutes={self.settings.reentry_cooldown_after_profit_minutes}"
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="info",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue
            if latest_profitable_trade is not None and not self._reentry_improvement_satisfied(
                signal,
                prior_exit_rank=(
                    int(latest_profitable_trade["exit_rank"])
                    if latest_profitable_trade["exit_rank"] not in (None, "")
                    else None
                ),
                prior_exit_composite_score=(
                    float(latest_profitable_trade["exit_composite_score"])
                    if latest_profitable_trade["exit_composite_score"] not in (None, "")
                    else None
                ),
            ):
                detail = (
                    "reentry_no_improvement_after_profit "
                    f"min_rank_improvement={self.settings.reentry_after_profit_min_rank_improvement} "
                    f"min_composite_improvement={self.settings.reentry_after_profit_min_composite_improvement:.3f}"
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="info",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue
            if self.settings.max_ticker_losing_trades_per_day > 0:
                ticker_losses = await self.database.count_ticker_losing_trades_for_day(
                    signal.ticker,
                    now_iso[:10],
                )
                if ticker_losses >= self.settings.max_ticker_losing_trades_per_day:
                    detail = (
                        "max_ticker_losing_trades_per_day_reached "
                        f"count={ticker_losses} limit={self.settings.max_ticker_losing_trades_per_day}"
                    )
                    actions.append(
                        ExecutionAction(
                            ticker=signal.ticker,
                            action="skip_entry",
                            signal_kind=signal.signal_kind,
                            detail=detail,
                        )
                    )
                    await self._log_runtime_event(
                        severity="info",
                        event_type="entry_blocked",
                        detail=detail,
                        stage="emerging",
                        ticker=signal.ticker,
                    )
                    continue
            if (
                self.settings.max_daily_stop_losses > 0
                and daily_stop_loss_count >= self.settings.max_daily_stop_losses
            ):
                detail = (
                    "max_daily_stop_losses_reached "
                    f"count={daily_stop_loss_count} limit={self.settings.max_daily_stop_losses}"
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="skip_entry",
                        signal_kind=signal.signal_kind,
                        detail=detail,
                    )
                )
                await self._log_runtime_event(
                    severity="warning",
                    event_type="entry_blocked",
                    detail=detail,
                    stage="emerging",
                    ticker=signal.ticker,
                )
                continue

            if self._using_live_venue():
                existing_venue_position = await self.client.get_position(signal.ticker)
                if existing_venue_position is not None and existing_venue_position.size > 0:
                    actions.append(
                        ExecutionAction(
                            ticker=signal.ticker,
                            action="skip_entry",
                            signal_kind=signal.signal_kind,
                            detail=(
                                "venue_position_already_open "
                                f"size={format(existing_venue_position.size.normalize(), 'f')}"
                            ),
                        )
                    )
                    continue
                spec = await self.client.fetch_instrument_spec(signal.ticker)
                wallet_balance = await self.client.get_wallet_balance()
                target_notional_usd = self._risk_notional_from_wallet(wallet_balance)
                qty = round_decimal(
                    target_notional_usd / Decimal(str(signal.current_price)),
                    spec.qty_step,
                    ROUND_DOWN,
                )
                if qty < spec.min_order_qty or qty <= 0:
                    actions.append(
                        ExecutionAction(
                            ticker=signal.ticker,
                            action="skip_entry",
                            signal_kind=signal.signal_kind,
                            detail=(
                                f"qty_below_min_order_qty qty={qty} min_order_qty={spec.min_order_qty}"
                            ),
                        )
                    )
                    continue
                order_link_id = self._client_order_id("entry", signal.ticker)
                try:
                    ack = await self.client.place_market_order(
                        symbol=signal.ticker,
                        side="Buy",
                        quantity=qty,
                        order_link_id=order_link_id,
                    )
                    venue_position = await self.client.wait_for_position(signal.ticker)
                    entry_price = venue_position.avg_price
                    tp_price = round_decimal(
                        entry_price * Decimal(str(1 + self.settings.take_profit_pct)),
                        spec.tick_size,
                        ROUND_HALF_UP,
                    )
                    sl_price = round_decimal(
                        entry_price * Decimal(str(1 - self.settings.stop_loss_pct)),
                        spec.tick_size,
                        ROUND_HALF_UP,
                    )
                    await self.client.set_trading_stop(
                        symbol=signal.ticker,
                        position_idx=venue_position.position_idx,
                        take_profit=tp_price,
                        stop_loss=sl_price,
                    )
                except Exception as exc:
                    order_id = await self.database.create_order(
                        client_order_id=order_link_id,
                        ticker=signal.ticker,
                        side="Buy",
                        order_type="Market",
                        lifecycle="entry",
                        status="rejected",
                        stage=signal.stage,
                        signal_kind=signal.signal_kind,
                        requested_qty=float(qty),
                        requested_notional_usd=float(target_notional_usd),
                        requested_price=signal.current_price,
                        venue="bybit",
                        is_demo=self.settings.demo_mode,
                        created_at=now_iso,
                        updated_at=now_iso,
                        notes=str(exc),
                    )
                    LOGGER.warning("Live entry submission failed for %s: %s", signal.ticker, exc)
                    actions.append(
                        ExecutionAction(
                            ticker=signal.ticker,
                            action="entry_rejected",
                            signal_kind=signal.signal_kind,
                            detail=str(exc),
                            order_id=order_id,
                        )
                    )
                    continue

                notes = (
                    f"Bybit live demo order placed. Risk={self.settings.risk_per_trade_pct * 100:.2f}% "
                    f"of available balance. Venue TP={format(tp_price.normalize(), 'f')} "
                    f"SL={format(sl_price.normalize(), 'f')}. {signal.entry_diagnostics}"
                )
                order_id = await self.database.create_order(
                    client_order_id=order_link_id,
                    ticker=signal.ticker,
                    side="Buy",
                    order_type="Market",
                    lifecycle="entry",
                    status="filled_live",
                    stage=signal.stage,
                    signal_kind=signal.signal_kind,
                    requested_qty=float(qty),
                    requested_notional_usd=float(target_notional_usd),
                    requested_price=signal.current_price,
                    venue="bybit",
                    is_demo=self.settings.demo_mode,
                    created_at=now_iso,
                    updated_at=now_iso,
                    fill_price=float(entry_price),
                    external_order_id=ack.order_id,
                    filled_at=now_iso,
                    notes=notes,
                )
                position_id = await self.database.open_position(
                    ticker=signal.ticker,
                    side="LONG",
                    opened_at=now_iso,
                    updated_at=now_iso,
                    entry_order_id=order_id,
                    entry_stage=signal.stage,
                    entry_signal_kind=signal.signal_kind,
                    quantity=float(venue_position.size),
                    notional_usd=float(venue_position.size * entry_price),
                    entry_price=float(entry_price),
                    take_profit_price=float(tp_price),
                    stop_loss_price=float(sl_price),
                    regime_score_at_entry=signal.regime_score,
                    dom_state_at_entry=(
                        "falling"
                        if self.state.global_state.btcdom_state < 0
                        else "rising"
                        if self.state.global_state.btcdom_state > 0
                        else "neutral"
                    ),
                    dom_change_pct_at_entry=self.state.global_state.btcdom_change_pct,
                    entry_rank=signal.rank,
                    entry_composite_score=signal.composite_score,
                    entry_momentum_z=signal.momentum_z,
                    entry_curvature=signal.curvature,
                    entry_hurst=signal.hurst,
                    notes=notes,
                )
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="enter_long",
                        signal_kind=signal.signal_kind,
                        detail=notes,
                        order_id=order_id,
                        position_id=position_id,
                    )
                )
                await self._send_execution_notification(
                    event="enter_long",
                    ticker=signal.ticker,
                    stage=signal.stage,
                    signal_kind=signal.signal_kind,
                    quantity=float(venue_position.size),
                    entry_price=float(entry_price),
                    current_price=float(entry_price),
                    notes=notes,
                )
                await self._record_position_mark(
                    position={
                        "id": position_id,
                        "ticker": signal.ticker,
                        "side": "LONG",
                        "entry_price": float(entry_price),
                    },
                    stage=signal.stage,
                    timestamp_iso=now_iso,
                    price=float(entry_price),
                    phase="open",
                )
                open_positions[signal.ticker] = {"id": position_id}
                entered_this_rebalance += 1
                continue

            quantity = self._estimate_quantity(signal.current_price)
            try:
                submission = await self.client.submit_market_order(
                    ticker=signal.ticker,
                    side="Buy",
                    quantity=quantity,
                    price_hint=signal.current_price,
                    reduce_only=False,
                )
            except Exception as exc:
                order_id = await self.database.create_order(
                    client_order_id=self._client_order_id("entry", signal.ticker),
                    ticker=signal.ticker,
                    side="Buy",
                    order_type="Market",
                    lifecycle="entry",
                    status="rejected",
                    stage=signal.stage,
                    signal_kind=signal.signal_kind,
                    requested_qty=quantity,
                    requested_notional_usd=self.settings.entry_notional_usd,
                    requested_price=signal.current_price,
                    venue="bybit",
                    is_demo=self.settings.demo_mode,
                    created_at=now_iso,
                    updated_at=now_iso,
                    notes=str(exc),
                )
                LOGGER.warning("Entry submission rejected for %s: %s", signal.ticker, exc)
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="entry_rejected",
                        signal_kind=signal.signal_kind,
                        detail=str(exc),
                        order_id=order_id,
                    )
                )
                continue

            order_id = await self.database.create_order(
                client_order_id=self._client_order_id("entry", signal.ticker),
                ticker=signal.ticker,
                side="Buy",
                order_type="Market",
                lifecycle="entry",
                status=submission.status,
                stage=signal.stage,
                signal_kind=signal.signal_kind,
                requested_qty=quantity,
                requested_notional_usd=self.settings.entry_notional_usd,
                requested_price=signal.current_price,
                venue="bybit",
                is_demo=self.settings.demo_mode,
                created_at=now_iso,
                updated_at=now_iso,
                fill_price=submission.fill_price,
                external_order_id=submission.external_order_id,
                filled_at=now_iso if submission.fill_price is not None else None,
                notes=(
                    submission.notes
                    if not signal.entry_diagnostics
                    else f"{submission.notes} {signal.entry_diagnostics}"
                ),
            )
            submission_note = (
                submission.notes
                if not signal.entry_diagnostics
                else f"{submission.notes} {signal.entry_diagnostics}"
            )
            if submission.fill_price is None:
                actions.append(
                    ExecutionAction(
                        ticker=signal.ticker,
                        action="entry_order_recorded",
                        signal_kind=signal.signal_kind,
                        detail=submission_note,
                        order_id=order_id,
                    )
                )
                continue

            position_id = await self.database.open_position(
                ticker=signal.ticker,
                side="LONG",
                opened_at=now_iso,
                updated_at=now_iso,
                entry_order_id=order_id,
                entry_stage=signal.stage,
                entry_signal_kind=signal.signal_kind,
                quantity=quantity,
                notional_usd=self.settings.entry_notional_usd,
                entry_price=submission.fill_price,
                take_profit_price=submission.fill_price * (1 + self.settings.take_profit_pct),
                stop_loss_price=submission.fill_price * (1 - self.settings.stop_loss_pct),
                regime_score_at_entry=signal.regime_score,
                dom_state_at_entry=(
                    "falling"
                    if self.state.global_state.btcdom_state < 0
                    else "rising"
                    if self.state.global_state.btcdom_state > 0
                    else "neutral"
                ),
                dom_change_pct_at_entry=self.state.global_state.btcdom_change_pct,
                entry_rank=signal.rank,
                entry_composite_score=signal.composite_score,
                entry_momentum_z=signal.momentum_z,
                entry_curvature=signal.curvature,
                entry_hurst=signal.hurst,
                notes=submission_note,
            )
            actions.append(
                ExecutionAction(
                    ticker=signal.ticker,
                    action="enter_long",
                    signal_kind=signal.signal_kind,
                    detail=submission_note,
                    order_id=order_id,
                    position_id=position_id,
                )
            )
            await self._send_execution_notification(
                event="enter_long",
                ticker=signal.ticker,
                stage=signal.stage,
                signal_kind=signal.signal_kind,
                quantity=quantity,
                entry_price=submission.fill_price,
                current_price=submission.fill_price,
                notes=submission_note,
            )
            await self._record_position_mark(
                position={
                    "id": position_id,
                    "ticker": signal.ticker,
                    "side": "LONG",
                    "entry_price": submission.fill_price,
                },
                stage=signal.stage,
                timestamp_iso=now_iso,
                price=submission.fill_price,
                phase="open",
            )
            open_positions[signal.ticker] = {"id": position_id}
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
            entered_this_rebalance += 1
        return actions
