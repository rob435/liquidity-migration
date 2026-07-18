"""Deterministic historical port of the deployed LONG and CONTINUOUS producers.

The comparator intentionally imports the production profile, selection,
publication, protection, account, and lifecycle owners.  Historical code owns
only the declared clock, frozen-close market, in-memory inbox, and trace ports.
It does not calculate a strategy return or an equity curve.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace as dataclass_replace
from pathlib import Path
from typing import Any, Protocol, cast

import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    PublishedTargetRequest,
    publish_exit_first_target_requests,
)
from .account_kernel import (
    AccountEvent,
    InstrumentRules,
    MarketInputRef,
)
from .account_route import AccountRoute
from .account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from .account_strategy_state import (
    CanonicalComponentExecutionAnchor,
    CanonicalReductionEvent,
    canonical_account_projection,
    canonical_adverse_reduction_events,
    canonical_strategy_trade_rows,
    terminal_entry_attempt_keys,
)
from .continuous_btc_risk import BTC_RISK_EVIDENCE_METADATA_KEY, btc_context_by_day
from .continuous_demo import (
    ContinuousDemoCycleConfig,
    _apply_btc_risk_sizing,
    _btc_trend_gate_allows_value,
    _btc_trend_gate_value,
    _continuous_age_eligible_symbols,
    _continuous_base_notional_pct_equity,
    _continuous_entry_candidates_with_signal_metadata,
    _continuous_entry_target_intents,
    _continuous_exit_target_intents,
    _continuous_target_reservations,
    _entry_event_expr,
    _open_continuous_trades,
    _validate_continuous_demo_config,
    continuous_managed_strategy_ids,
    continuous_strategy_id,
    entry_circuit_breaker_tripped,
    plan_continuous_exits,
    select_continuous_entries,
)
from .continuous_events import _btc_trend_returns
from .execution_adapters import ExecutionTwinConfig
from .entry_attempts import ENTRY_ATTEMPT_METADATA_KEY
from .historical_account_replay import (
    HistoricalAccountSession,
    historical_submission_feedback,
)
from .long_native import LongNativeConfig, long_pump_family
from .long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _compute_long_order_sizing,
    _count_long_target_reservations,
    _long_entry_target_intents,
    _long_exit_target_intents,
    _plan_time_stop_exits,
    _select_long_entry_candidates,
    _validate_long_demo_config,
)
from .protection_engine import (
    AccountProtectionEngine,
    _optional_fraction,
    _protection_price,
    _protection_trigger_reason,
)
from .strategy_funnel import DecisionFunnelObserver
from .strategy_runtime import SleeveTargetIntent


class HistoricalPricePort(Protocol):
    """Return the frozen close available at a whole-hour boundary."""

    def price(self, symbol: str, boundary_ts_ms: int) -> float: ...

    def prices(
        self,
        symbols: Sequence[str] | set[str],
        boundary_ts_ms: int,
    ) -> dict[str, float]: ...


class ComparatorTraceSink(Protocol):
    """Outcome-blind append port for comparator evidence."""

    def cycle(self, row: Mapping[str, Any]) -> None: ...

    def continuous_gates(self, frame: pl.DataFrame) -> None: ...

    def long_funnel(self, row: Mapping[str, Any]) -> None: ...

    def source_decision(self, row: Mapping[str, Any]) -> None: ...

    def request(self, row: Mapping[str, Any]) -> None: ...

    def request_intent(self, row: Mapping[str, Any]) -> None: ...


class _LongPriceRequirementProbe:
    """Record only production-selector reads before supplying real prices."""

    def __init__(self) -> None:
        self.symbols: set[str] = set()

    def get(self, key: object, _default: object = None) -> float:
        self.symbols.add(str(key).upper())
        return 1.0


def _long_price_required_symbols(
    *,
    features: pl.DataFrame,
    all_trades: pl.DataFrame,
    now_ms: int,
    strategy: LongNativeConfig,
) -> set[str]:
    """Discover strict price dependencies through the production selector.

    The probe run has no observer or mutation.  Its positive placeholder lets
    the selector reach every pre-price-eligible row; only calls to ``get`` are
    retained.  The authoritative selector runs again with strict frozen prices.
    """

    probe = _LongPriceRequirementProbe()
    _select_long_entry_candidates(
        features=features,
        all_trades=all_trades,
        now_ms=now_ms,
        strategy=strategy,
        price_by_symbol=cast(dict[str, float], probe),
        max_new_entries=max(features.height, 1),
        funnel_observer=None,
    )
    return probe.symbols


def _long_observer_price_symbols(
    *,
    features: pl.DataFrame,
    strategy: LongNativeConfig,
) -> set[str]:
    """Return the legacy raw-pump price population used only by the funnel."""

    symbols: set[str] = set()
    for row in features.iter_rows(named=True):
        pump = long_pump_family(row, strategy)
        if bool(pump["trigger_any"]):
            symbols.add(str(row["symbol"]).upper())
    return symbols


class NullComparatorTraceSink:
    def cycle(self, row: Mapping[str, Any]) -> None:
        del row

    def continuous_gates(self, frame: pl.DataFrame) -> None:
        del frame

    def long_funnel(self, row: Mapping[str, Any]) -> None:
        del row

    def source_decision(self, row: Mapping[str, Any]) -> None:
        del row

    def request(self, row: Mapping[str, Any]) -> None:
        del row

    def request_intent(self, row: Mapping[str, Any]) -> None:
        del row


class _LongFunnelPort(DecisionFunnelObserver):
    def __init__(self, sink: ComparatorTraceSink) -> None:
        self.sink = sink

    def observe(self, row: Mapping[str, Any]) -> None:
        self.sink.long_funnel(row)


class MemoryAccountIntentInbox(AccountIntentInbox):
    """Create-only synchronous inbox port with exact request immutability."""

    def __init__(self, route: AccountRoute) -> None:
        self.route = route
        self.root = route.inbox_path
        self._requests: dict[str, AccountTargetRequest] = {}

    def contains(self, request_id: str) -> bool:
        return request_id in self._requests

    def submit(self, request: AccountTargetRequest) -> Path:
        request.require_route(self.route)
        prior = self._requests.get(request.request_id)
        if prior is not None and prior.to_dict() != request.to_dict():
            raise ValueError(
                f"immutable request_id {request.request_id!r} changed content"
            )
        self._requests.setdefault(request.request_id, request)
        name = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest() + ".json"
        return self.root / "memory" / name

    @property
    def requests(self) -> tuple[AccountTargetRequest, ...]:
        return tuple(self._requests.values())


class HistoricalHourlyCloseProvider:
    """Read exact reconstructed `(symbol, bar-start)` closes one day at a time."""

    def __init__(self, kline_root: str | Path) -> None:
        self.root = Path(kline_root).expanduser().resolve(strict=True)
        self._cache_day = ""
        self._cache: dict[str, dict[int, float]] = {}
        self.consumed_paths: set[Path] = set()
        self.lookups = 0

    @staticmethod
    def _date_for_bar_start(bar_start_ms: int) -> str:
        return dt.datetime.fromtimestamp(
            bar_start_ms / 1000.0,
            tz=dt.timezone.utc,
        ).date().isoformat()

    def _symbol_day(self, symbol: str, date: str) -> dict[int, float]:
        if date != self._cache_day:
            self._cache_day = date
            self._cache = {}
        normalized = str(symbol).strip().upper()
        cached = self._cache.get(normalized)
        if cached is not None:
            return cached
        path = self.root / f"date={date}" / f"symbol={normalized}" / "part.parquet"
        if not path.is_file():
            raise RuntimeError(
                f"frozen hourly price is missing for {normalized} on {date}: {path}"
            )
        frame = pl.read_parquet(path, columns=["ts_ms", "close"])
        rows: dict[int, float] = {}
        for ts_ms, close in frame.iter_rows():
            if close is None:
                continue
            price = float(close)
            if not math.isfinite(price) or price <= 0.0:
                raise RuntimeError(
                    f"frozen hourly price is invalid for {normalized} at {int(ts_ms)}"
                )
            key = int(ts_ms)
            if key in rows:
                raise RuntimeError(
                    f"duplicate frozen hourly price for {normalized} at {key}"
                )
            rows[key] = price
        self._cache[normalized] = rows
        self.consumed_paths.add(path)
        return rows

    def price(self, symbol: str, boundary_ts_ms: int) -> float:
        if boundary_ts_ms <= 0 or boundary_ts_ms % MS_PER_HOUR:
            raise ValueError("historical price boundary must be a positive whole hour")
        bar_start = int(boundary_ts_ms) - MS_PER_HOUR
        date = self._date_for_bar_start(bar_start)
        rows = self._symbol_day(symbol, date)
        self.lookups += 1
        try:
            return rows[bar_start]
        except KeyError as exc:
            raise RuntimeError(
                f"frozen hourly close is missing for {str(symbol).upper()} "
                f"at boundary {boundary_ts_ms}"
            ) from exc

    def prices(
        self,
        symbols: Sequence[str] | set[str],
        boundary_ts_ms: int,
    ) -> dict[str, float]:
        return {
            symbol: self.price(symbol, boundary_ts_ms)
            for symbol in sorted({str(value).strip().upper() for value in symbols})
            if symbol
        }


@dataclass(frozen=True, slots=True)
class ComparatorClockOffsets:
    protection_ns: int = 0
    long_ns: int = 100_000
    continuous_ns: int = 200_000
    boundary_flat_ns: int = 900_000

    def __post_init__(self) -> None:
        values = (
            self.protection_ns,
            self.long_ns,
            self.continuous_ns,
            self.boundary_flat_ns,
        )
        if tuple(sorted(values)) != values or len(set(values)) != len(values):
            raise ValueError("comparator clock offsets must be unique and ordered")
        if values[0] < 0 or values[-1] >= 1_000_000:
            raise ValueError("comparator clock offsets must fit inside one millisecond")


@dataclass(frozen=True, slots=True)
class ComparatorRunConfig:
    equity_usdt: float
    long_source_start_ms: int
    continuous_source_start_ms: int
    source_end_ms: int
    clock_offsets: ComparatorClockOffsets = ComparatorClockOffsets()

    def __post_init__(self) -> None:
        if self.equity_usdt <= 0.0:
            raise ValueError("comparator equity must be positive")
        boundaries = (
            self.long_source_start_ms,
            self.continuous_source_start_ms,
            self.source_end_ms,
        )
        if any(value <= 0 or value % MS_PER_HOUR for value in boundaries):
            raise ValueError("comparator windows must be positive whole-hour timestamps")
        if not self.long_source_start_ms < self.source_end_ms:
            raise ValueError("LONG comparator window is empty")
        if not self.continuous_source_start_ms < self.source_end_ms:
            raise ValueError("CONTINUOUS comparator window is empty")


class ActiveRuntimeComparator:
    """Chronological production-function comparator on one shared account."""

    def __init__(
        self,
        *,
        route: AccountRoute,
        session: HistoricalAccountSession,
        instrument_rules: Mapping[str, InstrumentRules],
        execution_config: ExecutionTwinConfig,
        price_port: HistoricalPricePort,
        long_demo: LongNativeDemoCycleConfig,
        long_strategy: LongNativeConfig,
        continuous_demo: ContinuousDemoCycleConfig,
        btc_klines: pl.DataFrame,
        first_archive_day_by_symbol: Mapping[str, int],
        btc_state_root: str | Path,
        run_config: ComparatorRunConfig,
        trace_sink: ComparatorTraceSink | None = None,
    ) -> None:
        if route.account_id != session.account_id:
            raise ValueError("comparator route and account session do not match")
        latency = execution_config.latency
        if (
            execution_config.fee_bps != 0.0
            or execution_config.max_decision_age_ns != 1_000_000
            or execution_config.rate_limit_orders != 0
            or execution_config.residual_adverse_slippage_bps != 0.0
            or any(
                value != 0
                for value in (
                    latency.decision_to_socket_ns,
                    latency.order_entry_ns,
                    latency.order_response_ns,
                    latency.fill_spacing_ns,
                    latency.submit_to_first_fill_ns,
                    latency.fill_response_ns,
                )
            )
        ):
            raise ValueError(
                "comparator execution port must use zero cost/latency/rate limit "
                "and a one-millisecond decision-age allowance"
            )
        _validate_long_demo_config(long_demo, long_strategy)
        _validate_continuous_demo_config(continuous_demo)
        if long_demo.account_execution_root is None or continuous_demo.account_execution_root is None:
            raise ValueError("comparator profiles require explicit account roots")
        self.route = route
        self.session = session
        self.instrument_rules = {
            str(symbol).upper(): rule for symbol, rule in instrument_rules.items()
        }
        self.price_port = price_port
        self.long_demo = long_demo
        self.long_strategy = long_strategy
        self.continuous_demo = continuous_demo
        self.run_config = run_config
        self.trace_sink = trace_sink or NullComparatorTraceSink()
        self.long_funnel = _LongFunnelPort(self.trace_sink)
        self.btc_klines = btc_klines
        self.btc_context = btc_context_by_day(btc_klines)
        self.btc_trend_lookup = _btc_trend_returns(
            btc_klines,
            lookback_days=max(int(continuous_demo.btc_trend_lookback_days), 1),
        )
        self.first_archive_day_by_symbol = {
            str(symbol).upper(): int(value)
            for symbol, value in first_archive_day_by_symbol.items()
        }
        self.btc_state_root = Path(btc_state_root)

        self.publisher = AccountTargetPublisher(route)
        self.inbox = MemoryAccountIntentInbox(route)
        self.publisher.inbox = self.inbox

        self.long_strategy_id = long_strategy.execution_strategy_id
        self.continuous_strategy_id = continuous_strategy_id(continuous_demo)
        self.continuous_managed_strategy_ids = continuous_managed_strategy_ids(
            continuous_demo
        )
        self._events: tuple[AccountEvent, ...] = ()
        self._long_trades = pl.DataFrame()
        self._continuous_trades = pl.DataFrame()
        self._anchors: dict[str, CanonicalComponentExecutionAnchor] = {}
        self._continuous_adverse: tuple[CanonicalReductionEvent, ...] = ()
        self._long_terminal_attempts: frozenset[str] = frozenset()
        self._continuous_terminal_attempts: frozenset[str] = frozenset()
        self._projection_dirty = False
        self._last_hour_ms = 0
        self._request_ordinal = 0
        self._request_counts: Counter[str] = Counter()
        self._source_decision_counts: Counter[str] = Counter()
        self._cycle_count = 0
        self._protection_trigger_count = 0

    def _account_events(self) -> tuple[AccountEvent, ...]:
        if self.session.kernel is None:
            return ()
        return tuple(self.session.kernel.journal.events())

    def _refresh_projection(self, *, force: bool = False) -> None:
        if not force and not self._projection_dirty:
            return
        self._events = self._account_events()
        if not self._events:
            self._long_trades = pl.DataFrame()
            self._continuous_trades = pl.DataFrame()
            self._anchors = {}
            self._continuous_adverse = ()
            self._long_terminal_attempts = frozenset()
            self._continuous_terminal_attempts = frozenset()
        else:
            root = self.session.root
            if self.session.kernel is None:
                raise RuntimeError("account events exist before comparator kernel startup")
            projection = canonical_account_projection(
                root,
                account_events=self._events,
                trusted_account_state=self.session.kernel._state_ref(),
            )
            self._long_trades = canonical_strategy_trade_rows(
                root,
                sleeve=SleeveAdapterKind.LONG.value,
                strategy_ids=(self.long_strategy_id,),
                account_projection=projection,
            )
            self._continuous_trades = canonical_strategy_trade_rows(
                root,
                sleeve=SleeveAdapterKind.CONTINUOUS.value,
                strategy_ids=self.continuous_managed_strategy_ids,
                account_projection=projection,
            )
            self._anchors = dict(projection.execution_anchors)
            self._continuous_adverse = canonical_adverse_reduction_events(
                root,
                sleeve=SleeveAdapterKind.CONTINUOUS.value,
                strategy_ids=self.continuous_managed_strategy_ids,
                account_events=self._events,
            )
            self._long_terminal_attempts = terminal_entry_attempt_keys(
                root,
                sleeve=SleeveAdapterKind.LONG.value,
                strategy_ids=(self.long_strategy_id,),
                account_events=self._events,
            )
            self._continuous_terminal_attempts = terminal_entry_attempt_keys(
                root,
                sleeve=SleeveAdapterKind.CONTINUOUS.value,
                strategy_ids=self.continuous_managed_strategy_ids,
                account_events=self._events,
            )
        self._projection_dirty = False

    def _current_account_symbols(self) -> set[str]:
        if self.session.kernel is None:
            return set()
        state = self.session.kernel._state_ref()
        symbols = {
            str(target.get("symbol") or "").upper()
            for target in state.component_targets.values()
            if abs(float(target.get("signed_qty") or 0.0)) > 0.0
        }
        symbols.update(
            symbol
            for symbol, position in state.positions.items()
            if abs(position.signed_qty) > 0.0
        )
        symbols.update(state.working_symbols())
        return {symbol for symbol in symbols if symbol}

    @staticmethod
    def _request_owner(request: AccountTargetRequest) -> str:
        owners = {
            str(item.intent.target_key).split("/", 1)[0]
            for item in request.intents
        }
        return next(iter(owners)) if len(owners) == 1 else "mixed"

    def _record_request(
        self,
        published: PublishedTargetRequest,
        *,
        stage: str,
        accepted: bool,
        rejection_keys: Sequence[str],
        command_ids: Sequence[str],
    ) -> None:
        request = published.request
        self._request_ordinal += 1
        owner = self._request_owner(request)
        self._request_counts[f"{stage}:{owner}"] += 1
        self.trace_sink.request(
            {
                "request_ordinal": self._request_ordinal,
                "stage": stage,
                "owner_sleeve": owner,
                "request_id": request.request_id,
                "batch_id": request.batch_id,
                "created_ts_ns": request.created_ts_ns,
                "route_id": request.route_id,
                "account_id": request.account_id,
                "environment": request.environment,
                "content_hash": request.content_hash(),
                "intent_count": len(request.intents),
                "accepted": bool(accepted),
                "rejection_keys": list(rejection_keys),
                "command_ids": list(command_ids),
            }
        )
        for intent_ordinal, item in enumerate(request.intents):
            target = item.intent
            metadata = dict(target.metadata)
            evidence = metadata.get(BTC_RISK_EVIDENCE_METADATA_KEY)
            evidence_hash = (
                str(evidence.get("evidence_hash") or "")
                if isinstance(evidence, Mapping)
                else ""
            )
            predecessor_hash = (
                str(evidence.get("predecessor_state_hash") or "")
                if isinstance(evidence, Mapping)
                else ""
            )
            result_hash = (
                str(evidence.get("result_state_hash") or "")
                if isinstance(evidence, Mapping)
                else ""
            )
            self.trace_sink.request_intent(
                {
                    "request_ordinal": self._request_ordinal,
                    "intent_ordinal": intent_ordinal,
                    "request_id": request.request_id,
                    "batch_id": request.batch_id,
                    "adapter_kind": SleeveAdapterKind(item.adapter_kind).value,
                    "decision_key": target.decision_key,
                    "target_key": target.target_key,
                    "strategy_id": target.strategy_id,
                    "component_id": target.component_id,
                    "symbol": target.symbol,
                    "signed_notional_usdt": target.signed_notional_usdt,
                    "leverage": target.leverage,
                    "reason": target.reason,
                    "signal_ts_ms": int(metadata.get("signal_ts_ms") or 0),
                    "decision_reference_price": metadata.get(
                        "decision_reference_price"
                    ),
                    "btc_risk_evidence_hash": evidence_hash,
                    "btc_risk_predecessor_state_hash": predecessor_hash,
                    "btc_risk_result_state_hash": result_hash,
                }
            )

    def _process_requests(
        self,
        requests: Sequence[PublishedTargetRequest],
        *,
        stage: str,
        boundary_ts_ms: int,
    ) -> int:
        processed = 0
        for published in requests:
            request = published.request
            required = self._current_account_symbols()
            required.update(item.intent.symbol.upper() for item in request.intents)
            prices = self.price_port.prices(required, boundary_ts_ms)
            outputs = self.session.submit_request(
                request,
                equity_usdt=self.run_config.equity_usdt,
                market_prices=prices,
                market_observed_ts_ns=boundary_ts_ms * 1_000_000,
            )
            feedback = historical_submission_feedback(outputs)
            command_ids = [
                command.command_id
                for output in outputs
                for command in output.target_result.commands
            ]
            self._record_request(
                published,
                stage=stage,
                accepted=feedback.accepted,
                rejection_keys=feedback.rejection_keys,
                command_ids=command_ids,
            )
            if not feedback.accepted:
                if feedback.target_committed:
                    raise RuntimeError(
                        "active runtime comparator execution failed after target "
                        f"commit {request.batch_id!r}: {feedback.rejection_keys}"
                    )
            processed += 1
            self._projection_dirty = True
        return processed

    @staticmethod
    def _require_publication(publication: ExitFirstPublication, *, stage: str) -> None:
        if publication.errors:
            errors = [
                f"{error.stage}:{error.target_key}:{error.error_type}:{error.message}"
                for error in publication.errors
            ]
            raise RuntimeError(f"{stage} target publication failed: {errors}")

    def _protection_markets(self, boundary_ts_ms: int) -> dict[str, MarketInputRef]:
        symbols = self._current_account_symbols()
        prices = self.price_port.prices(symbols, boundary_ts_ms)
        timestamp_ns = boundary_ts_ms * 1_000_000
        sequence = boundary_ts_ms // MS_PER_HOUR
        return {
            symbol: MarketInputRef(
                input_key=f"historical-hourly-protection:{symbol}:{boundary_ts_ms}",
                symbol=symbol,
                exchange_ts_ns=timestamp_ns,
                local_receive_ts_ns=timestamp_ns,
                reference_price=price,
                bid_price=price,
                ask_price=price,
                book_sequence=sequence,
                source="frozen_hourly_close_no_intrabar_claim",
                metadata={"boundary_ts_ms": boundary_ts_ms},
            )
            for symbol, price in prices.items()
        }

    def _protection_may_trigger(
        self,
        markets: Mapping[str, MarketInputRef],
    ) -> bool:
        if self.session.kernel is None:
            return False
        state = self.session.kernel._state_ref()
        for target_key, target in state.component_targets.items():
            signed_qty = float(target.get("signed_qty") or 0.0)
            symbol = str(target.get("symbol") or "").upper()
            metadata = target.get("metadata") or {}
            anchor = self._anchors.get(target_key)
            market = markets.get(symbol)
            rule = self.instrument_rules.get(symbol)
            if (
                signed_qty == 0.0
                or not isinstance(metadata, Mapping)
                or anchor is None
                or not anchor.entry_fill_complete
                or anchor.entry_fill_vwap is None
                or anchor.entry_fill_vwap <= 0.0
                or anchor.entry_attribution_scope == "none"
                or market is None
                or rule is None
                or rule.tick_size <= 0.0
            ):
                continue
            stop = _protection_price(
                entry_fill_price=anchor.entry_fill_vwap,
                signed_qty=signed_qty,
                fraction=_optional_fraction(metadata.get("stop_loss_pct")),
                tick_size=rule.tick_size,
                is_stop=True,
            )
            take_profit = _protection_price(
                entry_fill_price=anchor.entry_fill_vwap,
                signed_qty=signed_qty,
                fraction=_optional_fraction(metadata.get("take_profit_pct")),
                tick_size=rule.tick_size,
                is_stop=False,
            )
            if _protection_trigger_reason(
                signed_qty=signed_qty,
                mark_price=market.reference_price,
                stop_price=stop,
                take_profit_price=take_profit,
            ):
                return True
        return False

    def _run_protection(self, boundary_ts_ms: int) -> int:
        if self.session.kernel is None or not self._current_account_symbols():
            return 0
        markets = self._protection_markets(boundary_ts_ms)
        if not self._protection_may_trigger(markets):
            return 0
        engine = AccountProtectionEngine(
            kernel=self.session.kernel,
            inbox=self.inbox,
            instrument_rules=self.instrument_rules,
        )
        requests = engine.evaluate(
            markets,
            account_events=self._events,
            verified_execution_anchors=self._anchors,
            trusted_account_state=self.session.kernel._state_ref(),
        )
        published = tuple(
            PublishedTargetRequest(
                request=request,
                path=self.inbox.submit(request),
            )
            for request in requests
        )
        processed = self._process_requests(
            published,
            stage="protection",
            boundary_ts_ms=boundary_ts_ms,
        )
        self._protection_trigger_count += processed
        if requests:
            self._projection_dirty = True
            self._refresh_projection(force=True)
        return processed

    def _long_candidate_prices(
        self,
        features: pl.DataFrame,
        *,
        boundary_ts_ms: int,
    ) -> dict[str, float]:
        required = _long_price_required_symbols(
            features=features,
            all_trades=self._long_trades,
            now_ms=boundary_ts_ms,
            strategy=self.long_strategy,
        )
        prices = self.price_port.prices(required, boundary_ts_ms)
        optional = _long_observer_price_symbols(
            features=features,
            strategy=self.long_strategy,
        ) - required
        for symbol in sorted(optional):
            try:
                prices[symbol] = self.price_port.price(symbol, boundary_ts_ms)
            except RuntimeError:
                # The production selector never reads this symbol. Preserve
                # observer continuity when a frozen price exists, but do not
                # turn an optional diagnostic field into an active dependency.
                continue
        return prices

    def _run_long(
        self,
        *,
        boundary_ts_ms: int,
        recent_features: pl.DataFrame,
    ) -> dict[str, int]:
        exits = _plan_time_stop_exits(
            self._long_trades,
            now_ms=boundary_ts_ms,
        )
        exit_intents = _long_exit_target_intents(
            exits,
            self._long_trades,
            strategy_id=self.long_strategy_id,
            now_ms=boundary_ts_ms,
            default_leverage=self.long_demo.entry_leverage,
        )

        candidates: list[dict[str, Any]] = []
        if (
            not recent_features.is_empty()
            and self.run_config.long_source_start_ms <= boundary_ts_ms
            <= self.run_config.source_end_ms
        ):
            price_by_symbol = self._long_candidate_prices(
                recent_features,
                boundary_ts_ms=boundary_ts_ms,
            )
            candidates, _skip_counts = _select_long_entry_candidates(
                features=recent_features,
                all_trades=self._long_trades,
                now_ms=boundary_ts_ms,
                strategy=self.long_strategy,
                price_by_symbol=price_by_symbol,
                max_new_entries=self.long_demo.max_new_entries_per_cycle,
                funnel_observer=self.long_funnel,
                funnel_venue="bybit",
            )
            free_slots = max(
                self.long_strategy.max_concurrent_positions
                - _count_long_target_reservations(self._long_trades),
                0,
            )
            candidates = candidates[:free_slots]
            order_notional, _vol_scale = _compute_long_order_sizing(
                demo=self.long_demo,
                strategy=self.long_strategy,
                features=recent_features,
                now_ms=boundary_ts_ms,
            )
            entry_intents = _long_entry_target_intents(
                candidates,
                demo=self.long_demo,
                equity_usdt=self.run_config.equity_usdt,
                order_notional_pct_equity=order_notional,
                price_by_symbol=price_by_symbol,
                now_ms=boundary_ts_ms,
                strategy_id=self.long_strategy_id,
            )
            entry_intents = [
                intent
                for intent in entry_intents
                if str(
                    intent.intent.metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or ""
                )
                not in self._long_terminal_attempts
            ]
        else:
            entry_intents = []
        long_targeted_component_ids = {
            intent.intent.component_id for intent in entry_intents
        }

        for row in exits:
            self._source_decision_counts["long:exit"] += 1
            self.trace_sink.source_decision(
                {
                    "sleeve": "long",
                    "action": "exit",
                    "cycle_ts_ms": boundary_ts_ms,
                    "signal_ts_ms": 0,
                    "trade_id": str(row.get("trade_id") or ""),
                    "component": "long",
                    "symbol": str(row.get("symbol") or "").upper(),
                    "reason": str(row.get("exit_reason") or ""),
                    "selected": True,
                    "boundary_only": False,
                }
            )
        for row in candidates:
            self._source_decision_counts["long:entry"] += 1
            self.trace_sink.source_decision(
                {
                    "sleeve": "long",
                    "action": "entry",
                    "cycle_ts_ms": boundary_ts_ms,
                    "signal_ts_ms": int(row.get("signal_ts_ms") or 0),
                    "trade_id": str(row.get("trade_id") or ""),
                    "component": "long",
                    "symbol": str(row.get("symbol") or "").upper(),
                    "reason": str(row.get("entry_reason") or ""),
                    "selected": True,
                    "target_published": (
                        str(row.get("trade_id") or "")
                        in long_targeted_component_ids
                    ),
                    "boundary_only": False,
                }
            )

        created_ns = (
            boundary_ts_ms * 1_000_000
            + self.run_config.clock_offsets.long_ns
        )
        publication = publish_exit_first_target_requests(
            self.publisher,
            batch_prefix=f"long-target/{self.long_strategy_id}/{boundary_ts_ms}",
            exit_intents=exit_intents,
            entry_intents=entry_intents,
            created_ts_ns=created_ns,
        )
        self._require_publication(publication, stage="long")
        exits_processed = self._process_requests(
            publication.exit_requests,
            stage="long_exit",
            boundary_ts_ms=boundary_ts_ms,
        )
        entries_processed = self._process_requests(
            publication.entry_requests,
            stage="long_entry",
            boundary_ts_ms=boundary_ts_ms,
        )
        return {
            "exit_plans": len(exits),
            "entry_candidates": len(candidates),
            "exit_requests": exits_processed,
            "entry_requests": entries_processed,
        }

    def _continuous_age_universe(
        self,
        frame: pl.DataFrame,
        *,
        signal_ts_ms: int,
    ) -> pl.DataFrame:
        signal_day = (int(signal_ts_ms) // MS_PER_DAY) * MS_PER_DAY
        symbols = [str(value).upper() for value in frame["symbol"].to_list()]
        ages = [
            (
                None
                if self.first_archive_day_by_symbol.get(symbol) is None
                else (
                    signal_day - self.first_archive_day_by_symbol[symbol]
                )
                / MS_PER_DAY
            )
            for symbol in symbols
        ]
        return pl.DataFrame(
            {
                "symbol": symbols,
                "listing_age_days": pl.Series(ages, dtype=pl.Float64),
            }
        )

    @staticmethod
    def _prior_signal_symbols(
        trades: pl.DataFrame,
        *,
        signal_ts_ms: int,
        strategy_id: str,
    ) -> set[str]:
        if trades.is_empty() or not {"symbol", "signal_ts_ms"} <= set(trades.columns):
            return set()
        frame = trades.filter(pl.col("signal_ts_ms") == signal_ts_ms)
        if "strategy_id" in frame.columns:
            frame = frame.filter(pl.col("strategy_id") == strategy_id)
        return {str(value).upper() for value in frame["symbol"].to_list()}

    def _continuous_gate_trace(
        self,
        frame: pl.DataFrame,
        *,
        boundary_ts_ms: int,
        signal_ts_ms: int,
        reserved_symbols: set[str],
        prior_signal_symbols: set[str],
        age_eligible: Mapping[int, set[str] | None],
        component_capacity: Mapping[str, int],
        component_reached: Mapping[str, bool],
        selected_candidates: Sequence[Mapping[str, Any]],
        final_candidates: Sequence[Mapping[str, Any]],
        entry_paused: bool,
        btc_trend_allows: bool,
        entry_capacity: int,
        btc_risk_blocked: bool,
    ) -> None:
        if frame.is_empty():
            return
        selected = {
            (str(row.get("component") or ""), str(row.get("symbol") or "").upper())
            for row in selected_candidates
        }
        targeted = {
            (str(row.get("component") or ""), str(row.get("symbol") or "").upper())
            for row in final_candidates
        }
        output = frame.with_columns(
            pl.lit(boundary_ts_ms).cast(pl.Int64).alias("cycle_ts_ms"),
            pl.lit(signal_ts_ms).cast(pl.Int64).alias("signal_ts_ms"),
            (pl.col("decile") == self.continuous_demo.decile).alias("gate_decile"),
            (pl.col("turnover_quote") >= self.continuous_demo.liq_turnover_min).alias(
                "gate_liquidity"
            ),
            (~pl.col("symbol").is_in(sorted(reserved_symbols))).alias(
                "gate_not_reserved"
            ),
            (~pl.col("symbol").is_in(sorted(prior_signal_symbols))).alias(
                "gate_not_prior_same_signal"
            ),
            pl.lit(not entry_paused).alias("gate_entry_not_paused"),
            pl.lit(btc_trend_allows).alias("gate_btc_trend"),
            pl.lit(entry_capacity > 0).alias("gate_shared_capacity"),
            pl.lit(not btc_risk_blocked).alias("gate_btc_risk_state"),
        )
        for component, trigger, age_days, _tp, _weight in self.continuous_demo.ensemble_components:
            eligible = age_eligible[age_days]
            selected_symbols = sorted(
                symbol for candidate_component, symbol in selected
                if candidate_component == component
            )
            targeted_symbols = sorted(
                symbol for candidate_component, symbol in targeted
                if candidate_component == component
            )
            output = output.with_columns(
                _entry_event_expr(trigger).alias(f"gate_event_{component}"),
                (
                    pl.lit(True)
                    if eligible is None
                    else pl.col("symbol").is_in(sorted(eligible))
                ).alias(f"gate_age_{component}"),
                pl.lit(bool(component_reached.get(component, False))).alias(
                    f"component_reached_{component}"
                ),
                pl.lit(int(component_capacity.get(component, 0))).cast(pl.Int64).alias(
                    f"capacity_before_{component}"
                ),
                pl.col("symbol").is_in(selected_symbols).alias(
                    f"selected_{component}"
                ),
                pl.col("symbol").is_in(targeted_symbols).alias(
                    f"targeted_{component}"
                ),
            )
        self.trace_sink.continuous_gates(output)

    def _run_continuous(
        self,
        *,
        boundary_ts_ms: int,
        entry_state: pl.DataFrame,
    ) -> dict[str, int]:
        expected_signal_ts = boundary_ts_ms - 2 * MS_PER_HOUR
        if not entry_state.is_empty():
            source_times = {
                int(value) for value in entry_state["ts_ms"].unique().to_list()
            }
            if source_times != {expected_signal_ts}:
                raise RuntimeError(
                    f"continuous entry-state clock mismatch: {source_times} != "
                    f"{{{expected_signal_ts}}}"
                )
            source_decision_ts = expected_signal_ts + MS_PER_HOUR
            if not (
                self.run_config.continuous_source_start_ms
                <= source_decision_ts
                < self.run_config.source_end_ms
            ):
                raise RuntimeError(
                    "continuous entry state escaped its registered source window"
                )

        open_trades = _open_continuous_trades(
            self._continuous_trades,
            self.continuous_managed_strategy_ids,
        )
        reservations = _continuous_target_reservations(
            self._continuous_trades,
            self.continuous_managed_strategy_ids,
        )
        reserved_symbols = (
            set(str(value).upper() for value in reservations["symbol"].to_list())
            if not reservations.is_empty()
            else set()
        )
        exits = plan_continuous_exits(
            open_trades.to_dicts(),
            now_ms=boundary_ts_ms,
            config=self.continuous_demo,
        )
        exit_intents = _continuous_exit_target_intents(
            exits,
            self._continuous_trades,
            strategy_id=self.continuous_strategy_id,
            now_ms=boundary_ts_ms,
            default_leverage=self.continuous_demo.entry_leverage,
        )

        entry_paused, _recent_adverse = entry_circuit_breaker_tripped(
            self._continuous_adverse,
            now_ms=boundary_ts_ms,
            config=self.continuous_demo,
        )
        btc_trend = _btc_trend_gate_value(
            self.btc_klines,
            signal_ts_ms=expected_signal_ts,
            config=self.continuous_demo,
            trend_lookup=self.btc_trend_lookup,
        )
        btc_trend_allows = _btc_trend_gate_allows_value(
            self.continuous_demo.btc_trend_gate,
            btc_trend,
        )
        entry_capacity = max(
            0,
            min(
                self.continuous_demo.max_new_entries_per_cycle,
                self.continuous_demo.max_active - reservations.height,
            ),
        )
        universe = (
            self._continuous_age_universe(
                entry_state,
                signal_ts_ms=expected_signal_ts,
            )
            if not entry_state.is_empty()
            else pl.DataFrame(
                {
                    "symbol": pl.Series([], dtype=pl.String),
                    "listing_age_days": pl.Series([], dtype=pl.Float64),
                }
            )
        )
        age_eligible: dict[int, set[str] | None] = {}
        for _component, _trigger, age_days, _tp, _weight in self.continuous_demo.ensemble_components:
            if age_days not in age_eligible:
                age_eligible[age_days] = _continuous_age_eligible_symbols(
                    universe,
                    pl.DataFrame(),
                    age_days_min=age_days,
                    now_ms=boundary_ts_ms,
                )

        prices: dict[str, float] = {}
        candidates: list[dict[str, Any]] = []
        component_capacity: dict[str, int] = {}
        component_reached: dict[str, bool] = {}
        if (
            not entry_state.is_empty()
            and not entry_paused
            and entry_capacity > 0
            and btc_trend_allows
        ):
            for component, trigger, age_days, take_profit_pct, component_weight in (
                self.continuous_demo.ensemble_components
            ):
                component_reached[component] = True
                component_capacity[component] = entry_capacity - len(candidates)
                if len(candidates) >= entry_capacity:
                    break
                component_config = dataclass_replace(
                    self.continuous_demo,
                    entry_event_trigger=trigger,
                )
                picks = select_continuous_entries(
                    entry_state,
                    held_symbols=reserved_symbols,
                    cooldown_symbols=reserved_symbols,
                    open_count=reservations.height + len(candidates),
                    config=component_config,
                    eligible_symbols=age_eligible[age_days],
                )
                pick_symbols = {str(row["symbol"]).upper() for row in picks}
                prices.update(self.price_port.prices(pick_symbols, boundary_ts_ms))
                component_candidates, _skipped = (
                    _continuous_entry_candidates_with_signal_metadata(
                        picks,
                        self._continuous_trades,
                        signal_ts=expected_signal_ts,
                        strategy_id=self.continuous_strategy_id,
                        price_by_symbol=prices,
                    )
                )
                for candidate in component_candidates:
                    if len(candidates) >= entry_capacity:
                        break
                    candidates.append(
                        {
                            **candidate,
                            "component": component,
                            "component_weight": component_weight,
                            "take_profit_pct": take_profit_pct,
                            "trade_id": f"{candidate['trade_id']}-{component}",
                        }
                    )
        selected_candidates = [dict(row) for row in candidates]
        btc_stats = (
            _apply_btc_risk_sizing(
                candidates,
                config=self.continuous_demo,
                root=self.btc_state_root,
                btc_klines=self.btc_klines,
                accepted_target_rows=self._continuous_trades,
                unresolved_entry_requests=0,
                accepted_state_authority=True,
                btc_context=self.btc_context,
            )
            if candidates
            else {"entry_blocked": False}
        )
        if bool(btc_stats["entry_blocked"]):
            candidates = []
        final_candidates = [dict(row) for row in candidates]
        entry_intents = _continuous_entry_target_intents(
            candidates,
            demo=self.continuous_demo,
            equity_usdt=self.run_config.equity_usdt,
            order_notional_frac=(
                _continuous_base_notional_pct_equity(self.continuous_demo) / 100.0
            ),
            price_by_symbol=prices,
            now_ms=boundary_ts_ms,
            strategy_id=self.continuous_strategy_id,
        )
        entry_intents = [
            intent
            for intent in entry_intents
            if str(intent.intent.metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or "")
            not in self._continuous_terminal_attempts
        ]
        targeted_component_ids = {
            intent.intent.component_id for intent in entry_intents
        }
        targeted_candidates = [
            row
            for row in final_candidates
            if str(row.get("trade_id") or "") in targeted_component_ids
        ]

        prior_symbols = self._prior_signal_symbols(
            self._continuous_trades,
            signal_ts_ms=expected_signal_ts,
            strategy_id=self.continuous_strategy_id,
        )
        self._continuous_gate_trace(
            entry_state,
            boundary_ts_ms=boundary_ts_ms,
            signal_ts_ms=expected_signal_ts,
            reserved_symbols=reserved_symbols,
            prior_signal_symbols=prior_symbols,
            age_eligible=age_eligible,
            component_capacity=component_capacity,
            component_reached=component_reached,
            selected_candidates=selected_candidates,
            final_candidates=targeted_candidates,
            entry_paused=entry_paused,
            btc_trend_allows=btc_trend_allows,
            entry_capacity=entry_capacity,
            btc_risk_blocked=bool(btc_stats["entry_blocked"]),
        )

        for row in exits:
            self._source_decision_counts["continuous:exit"] += 1
            self.trace_sink.source_decision(
                {
                    "sleeve": "continuous",
                    "action": "exit",
                    "cycle_ts_ms": boundary_ts_ms,
                    "signal_ts_ms": int(row.get("signal_ts_ms") or 0),
                    "trade_id": str(row.get("trade_id") or ""),
                    "component": str(row.get("component") or ""),
                    "symbol": str(row.get("symbol") or "").upper(),
                    "reason": str(row.get("exit_reason") or ""),
                    "selected": True,
                    "boundary_only": False,
                }
            )
        for row in final_candidates:
            evidence = row.get(BTC_RISK_EVIDENCE_METADATA_KEY)
            self._source_decision_counts["continuous:entry"] += 1
            self.trace_sink.source_decision(
                {
                    "sleeve": "continuous",
                    "action": "entry",
                    "cycle_ts_ms": boundary_ts_ms,
                    "signal_ts_ms": int(row.get("signal_ts_ms") or 0),
                    "trade_id": str(row.get("trade_id") or ""),
                    "component": str(row.get("component") or ""),
                    "symbol": str(row.get("symbol") or "").upper(),
                    "reason": str(row.get("entry_reason") or "continuous_entry"),
                    "selected": True,
                    "target_published": (
                        str(row.get("trade_id") or "") in targeted_component_ids
                    ),
                    "boundary_only": False,
                    "btc_risk_evidence_hash": (
                        str(evidence.get("evidence_hash") or "")
                        if isinstance(evidence, Mapping)
                        else ""
                    ),
                }
            )

        created_ns = (
            boundary_ts_ms * 1_000_000
            + self.run_config.clock_offsets.continuous_ns
        )
        publication = publish_exit_first_target_requests(
            self.publisher,
            batch_prefix=(
                f"continuous-target/{self.continuous_strategy_id}/{boundary_ts_ms}"
            ),
            exit_intents=exit_intents,
            entry_intents=entry_intents,
            created_ts_ns=created_ns,
            independent_entry_requests=True,
        )
        self._require_publication(publication, stage="continuous")
        exits_processed = self._process_requests(
            publication.exit_requests,
            stage="continuous_exit",
            boundary_ts_ms=boundary_ts_ms,
        )
        entries_processed = self._process_requests(
            publication.entry_requests,
            stage="continuous_entry",
            boundary_ts_ms=boundary_ts_ms,
        )
        return {
            "exit_plans": len(exits),
            "entry_candidates": len(final_candidates),
            "exit_requests": exits_processed,
            "entry_requests": entries_processed,
        }

    def process_hour(
        self,
        boundary_ts_ms: int,
        *,
        long_recent_features: pl.DataFrame | None = None,
        continuous_entry_state: pl.DataFrame | None = None,
    ) -> dict[str, Any]:
        """Process one frozen close in registered protection/LONG/CONT order."""

        boundary = int(boundary_ts_ms)
        if boundary <= self._last_hour_ms or boundary % MS_PER_HOUR:
            raise ValueError("comparator hours must be strictly increasing whole hours")
        if boundary > self.run_config.source_end_ms:
            raise ValueError("comparator hour exceeds the frozen raw-data boundary")
        self._last_hour_ms = boundary
        self._cycle_count += 1
        self._refresh_projection()
        protection_requests = self._run_protection(boundary)
        long_stats = self._run_long(
            boundary_ts_ms=boundary,
            recent_features=(
                long_recent_features
                if long_recent_features is not None
                else pl.DataFrame()
            ),
        )
        continuous_stats = self._run_continuous(
            boundary_ts_ms=boundary,
            entry_state=(
                continuous_entry_state
                if continuous_entry_state is not None
                else pl.DataFrame()
            ),
        )
        row: dict[str, Any] = {
            "cycle_ordinal": self._cycle_count,
            "cycle_ts_ms": boundary,
            "protection_requests": protection_requests,
            **{f"long_{key}": value for key, value in long_stats.items()},
            **{
                f"continuous_{key}": value
                for key, value in continuous_stats.items()
            },
            "account_projection_dirty": self._projection_dirty,
        }
        self.trace_sink.cycle(row)
        return row

    def boundary_flatten(self, boundary_ts_ms: int) -> int:
        """Create explicit risk-owned terminal zero targets and require flatness."""

        boundary = int(boundary_ts_ms)
        if boundary != self.run_config.source_end_ms:
            raise ValueError("boundary flatten must use the registered source end")
        if self._last_hour_ms != boundary:
            raise ValueError("boundary flatten requires the final hour to be processed first")
        self._refresh_projection(force=True)
        if self.session.kernel is None:
            return 0
        state = self.session.kernel._state_ref()
        intents: list[RequestedIntent] = []
        for target_key, target in sorted(state.component_targets.items()):
            signed_qty = float(target.get("signed_qty") or 0.0)
            if signed_qty == 0.0:
                continue
            symbol = str(target.get("symbol") or "").upper()
            owner_sleeve = str(target.get("sleeve") or target_key.split("/", 1)[0])
            strategy_id = str(target.get("strategy_id") or "").strip()
            component_id = str(target.get("component_id") or "").strip()
            if not symbol or not strategy_id or not component_id:
                raise RuntimeError(
                    f"boundary target {target_key!r} lacks canonical ownership"
                )
            price = self.price_port.price(symbol, boundary)
            intents.append(
                RequestedIntent(
                    adapter_kind=SleeveAdapterKind.RISK,
                    intent=SleeveTargetIntent(
                        decision_key=(
                            f"risk:{target.get('decision_key') or target_key}:"
                            "comparator_boundary_flat"
                        ),
                        target_key=target_key,
                        strategy_id=strategy_id,
                        component_id=component_id,
                        symbol=symbol,
                        signed_notional_usdt=0.0,
                        leverage=float(target.get("leverage") or 1.0),
                        reason="comparator_boundary_flat",
                        metadata={
                            "owner_sleeve": owner_sleeve,
                            "requested_by_strategy_id": "active-runtime-comparator",
                            "decision_reference_price": price,
                            "boundary_ts_ms": boundary,
                            "excluded_from_strategy_source_decisions": True,
                        },
                    ),
                )
            )
            self.trace_sink.source_decision(
                {
                    "sleeve": owner_sleeve,
                    "action": "boundary_flat",
                    "cycle_ts_ms": boundary,
                    "signal_ts_ms": 0,
                    "trade_id": component_id,
                    "component": component_id,
                    "symbol": symbol,
                    "reason": "comparator_boundary_flat",
                    "selected": True,
                    "boundary_only": True,
                }
            )
        if not intents:
            return 0
        created_ns = (
            boundary * 1_000_000
            + self.run_config.clock_offsets.boundary_flat_ns
        )
        publication = publish_exit_first_target_requests(
            self.publisher,
            batch_prefix=f"active-runtime-comparator/boundary/{boundary}",
            exit_intents=intents,
            entry_intents=(),
            created_ts_ns=created_ns,
        )
        self._require_publication(publication, stage="boundary_flat")
        processed = self._process_requests(
            publication.exit_requests,
            stage="boundary_flat",
            boundary_ts_ms=boundary,
        )
        self._refresh_projection(force=True)
        final_state = self.session.kernel._state_ref()
        nonzero_targets = {
            key: float(target.get("signed_qty") or 0.0)
            for key, target in final_state.component_targets.items()
            if abs(float(target.get("signed_qty") or 0.0)) > 1e-12
        }
        nonzero_positions = {
            symbol: position.signed_qty
            for symbol, position in final_state.positions.items()
            if abs(position.signed_qty) > 1e-12
        }
        if nonzero_targets or nonzero_positions or final_state.working_symbols(
            tolerance=1e-12
        ):
            raise RuntimeError(
                "active runtime comparator boundary flatten did not converge: "
                f"targets={nonzero_targets}, positions={nonzero_positions}, "
                f"working={sorted(final_state.working_symbols(tolerance=1e-12))}"
            )
        return processed

    def final_structural_summary(self) -> dict[str, Any]:
        """Return identities/counts only; never aggregate monetary outcomes."""

        self._refresh_projection(force=True)
        events = self._events
        grouped = Counter(event.event_type for event in events)
        final_state_hash = ""
        final_flat = True
        working_symbols: list[str] = []
        if self.session.kernel is not None:
            state = self.session.kernel._state_ref()
            final_state_hash = state.state_hash()
            working_symbols = sorted(state.working_symbols(tolerance=1e-12))
            final_flat = (
                not working_symbols
                and all(
                    abs(position.signed_qty) <= 1e-12
                    for position in state.positions.values()
                )
                and all(
                    abs(float(target.get("signed_qty") or 0.0)) <= 1e-12
                    for target in state.component_targets.values()
                )
            )
        btc_reconciliation = _apply_btc_risk_sizing(
            [],
            config=self.continuous_demo,
            root=self.btc_state_root,
            btc_klines=self.btc_klines,
            accepted_target_rows=self._continuous_trades,
            unresolved_entry_requests=0,
            accepted_state_authority=True,
            btc_context=self.btc_context,
        )
        if bool(btc_reconciliation["entry_blocked"]):
            raise RuntimeError(
                "final accepted-decision BTC-risk reconciliation failed: "
                f"{btc_reconciliation['blocking_reason']}"
            )
        return {
            "cycles": self._cycle_count,
            "requests": self._request_ordinal,
            "request_counts": dict(sorted(self._request_counts.items())),
            "source_decision_counts": dict(
                sorted(self._source_decision_counts.items())
            ),
            "protection_requests": self._protection_trigger_count,
            "account_events": len(events),
            "account_event_counts": dict(sorted(grouped.items())),
            "last_sequence": events[-1].sequence if events else 0,
            "last_event_hash": events[-1].event_hash if events else "",
            "final_state_hash": final_state_hash,
            "final_flat": final_flat,
            "working_symbols": working_symbols,
            "long_lifecycle_rows": self._long_trades.height,
            "continuous_lifecycle_rows": self._continuous_trades.height,
            "btc_risk_state_rows": int(btc_reconciliation["state_rows"]),
            "btc_risk_authoritative_rows": int(
                btc_reconciliation["accepted_authoritative_rows"]
            ),
            "btc_risk_reconciliation_error": int(btc_reconciliation["error"]),
            "monetary_outcomes_inspected": False,
        }

    @property
    def canonical_long_trades(self) -> pl.DataFrame:
        self._refresh_projection(force=True)
        return self._long_trades.clone()

    @property
    def canonical_continuous_trades(self) -> pl.DataFrame:
        self._refresh_projection(force=True)
        return self._continuous_trades.clone()
