"""Bybit demo providers for the single-owner account execution service."""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from .account_kernel import (
    AccountExecutionKernel,
    AccountRiskSnapshot,
    AccountState,
    InstrumentRules,
    MarketInputRef,
)
from .deterministic_serialization import canonical_json
from .deterministic_runtime import Clock, SystemClock
from .execution_adapters import (
    INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE,
    L2BookSnapshot,
    MarketOrderExecutionTwin,
)
from .market_capture import SequenceAwareMarketRecorder


def _finite(value: Any, *, label: str) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit {label} is missing/non-numeric") from exc
    if not math.isfinite(output):
        raise RuntimeError(f"Bybit {label} is non-finite")
    return output


class CapturedBybitMarketProvider:
    """Freeze reconstructed raw L2 at the account decision boundary."""

    def __init__(self, recorder: SequenceAwareMarketRecorder) -> None:
        self.recorder = recorder
        self._contexts: dict[str, L2BookSnapshot] = {}
        self._lock = threading.RLock()

    def current(self, symbols: Sequence[str], *, batch_id: str) -> Mapping[str, MarketInputRef]:
        output: dict[str, MarketInputRef] = {}
        for symbol in sorted(set(symbols)):
            record, book = self.recorder.capture_context(
                symbol=symbol,
                context_kind="account_service_decision",
                reference_key=batch_id,
            )
            market = book.market_ref(input_key=str(record["record_id"]), source="bybit_raw_l2")
            with self._lock:
                self._contexts[market.input_key] = book
                # Contexts are consumed promptly by the execution adapter; keep
                # a hard bound for rejected batches and crash/retry churn.
                while len(self._contexts) > 10_000:
                    self._contexts.pop(next(iter(self._contexts)))
            output[symbol] = replace(
                market,
                metadata={
                    **dict(market.metadata),
                    "capture_record_id": record["record_id"],
                    "update_id": record["update_id"],
                    "sequence_gap": record["sequence_gap"],
                    "sequence_gap_reason": record["sequence_gap_reason"],
                    **{
                        key: record[key]
                        for key in (
                            "capture_segment_path",
                            "capture_byte_offset",
                            "capture_byte_length",
                            "capture_record_sha256",
                        )
                        if key in record
                    },
                },
            )
        return output

    def execution_book(self, input_key: str) -> L2BookSnapshot:
        with self._lock:
            book = self._contexts.get(input_key)
        if book is None:
            raise RuntimeError(f"no captured execution book for market input {input_key}")
        return book


class CapturedPaperExecutionAdapter:
    """Uncalibrated integration port using the exact decision-boundary book."""

    name = f"paper_{INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE}"

    def __init__(
        self,
        *,
        market_provider: CapturedBybitMarketProvider,
        twin: MarketOrderExecutionTwin,
    ) -> None:
        self.market_provider = market_provider
        self.twin = twin

    def submit(self, command: Any, market_input: MarketInputRef) -> Any:
        book = self.market_provider.execution_book(market_input.input_key)
        self.twin.books[command.symbol.upper()] = book
        return self.twin.submit(command, market_input)


class BybitDemoAccountSnapshotProvider:
    """Read one fresh demo wallet snapshot; mainnet clients are refused."""

    def __init__(self, client: Any, *, clock: Clock | None = None) -> None:
        if not bool(getattr(client, "demo", False)):
            raise ValueError("demo account snapshot provider refuses a non-demo client")
        self.client = client
        self.clock = clock or SystemClock()

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        result = self.client.get_wallet_balance(account_type="UNIFIED", coin="USDT")
        rows = result.get("list") if isinstance(result, Mapping) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            raise RuntimeError("Bybit demo wallet response has no UNIFIED account row")
        account = rows[0]
        equity = _finite(account.get("totalEquity"), label="totalEquity")
        available_raw = account.get("totalAvailableBalance")
        if available_raw in (None, ""):
            margin_balance = _finite(account.get("totalMarginBalance"), label="totalMarginBalance")
            initial_margin = _finite(account.get("totalInitialMargin"), label="totalInitialMargin")
            available = margin_balance - initial_margin
        else:
            available = _finite(available_raw, label="totalAvailableBalance")
        if equity <= 0.0 or available < 0.0:
            raise RuntimeError("Bybit demo wallet snapshot has nonpositive equity or negative available margin")
        observed_ns = self.clock.wall_time_ns()
        material = {
            "batch_id": batch_id,
            "equity_usdt": equity,
            "available_margin_usdt": available,
            "observed_ts_ns": observed_ns,
        }
        return AccountRiskSnapshot(
            equity_usdt=equity,
            available_margin_usdt=available,
            snapshot_key="bybit-demo:" + hashlib.sha256(canonical_json(material)).hexdigest()[:20],
            snapshot_ts_ns=observed_ns,
        )


@dataclass(frozen=True, slots=True)
class UnownedBybitDemoOrder:
    symbol: str
    description: str


@dataclass(frozen=True, slots=True)
class BybitDemoOrderOwnershipSnapshot:
    journal_events_applied: int
    all_kinds_rows_observed: int
    conditional_rows_observed: int
    unique_orders_observed: int
    unowned_orders: tuple[UnownedBybitDemoOrder, ...]


def _validated_open_order_rows(value: Any, *, query_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"Bybit demo {query_name} open-order query returned a non-list payload")
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise RuntimeError(
                f"Bybit demo {query_name} open-order query returned a non-object row at index {index}"
            )
        symbol = row.get("symbol")
        if type(symbol) is not str or not symbol.strip():
            raise RuntimeError(
                f"Bybit demo {query_name} open-order row {index} lacks symbol"
            )
        order_id = row.get("orderId") or row.get("order_id")
        order_link_id = row.get("orderLinkId") or row.get("order_link_id")
        if not (
            (type(order_id) is str and bool(order_id.strip()))
            or (type(order_link_id) is str and bool(order_link_id.strip()))
        ):
            raise RuntimeError(
                f"Bybit demo {query_name} open-order row {index} lacks durable order identity"
            )
        rows.append(row)
    return tuple(rows)


def _open_order_identity(row: Mapping[str, Any]) -> str:
    order_id = str(row.get("orderId") or row.get("order_id") or "").strip()
    if order_id:
        return f"order:{order_id}"
    order_link_id = str(
        row.get("orderLinkId") or row.get("order_link_id") or ""
    ).strip()
    if order_link_id:
        return f"link:{order_link_id}"
    return "anonymous:" + hashlib.sha256(canonical_json(dict(row))).hexdigest()


def _kernel_working_order_owns_row(state: Any, row: Mapping[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip().upper()
    order_link_id = str(
        row.get("orderLinkId") or row.get("order_link_id") or ""
    ).strip()
    venue_order_id = str(
        row.get("orderId") or row.get("order_id") or ""
    ).strip()
    for command_id in state.working_order_ids:
        order = state.orders.get(command_id)
        if order is None:
            raise RuntimeError(f"account kernel working-order index references missing command {command_id}")
        link_matches = bool(order_link_id and order_link_id == command_id)
        venue_id_matches = bool(
            venue_order_id
            and order.venue_order_id
            and venue_order_id == order.venue_order_id
        )
        if not link_matches and not venue_id_matches:
            continue
        if not symbol or order.symbol.upper() != symbol:
            raise RuntimeError(
                "venue open-order identity matches a kernel command on a different symbol: "
                f"command={command_id} kernel={order.symbol} venue={symbol or '<missing>'}"
            )
        return True
    return False


def _describe_open_order(
    variants: Sequence[tuple[str, Mapping[str, Any]]],
) -> str:
    row = variants[-1][1]
    conditional = any(source == "conditional" for source, _row in variants) or bool(
        row.get("triggerPrice")
        or row.get("trigger_price")
        or row.get("stopOrderType")
        or row.get("stop_order_type")
    )
    symbol = str(row.get("symbol") or "<missing-symbol>").strip().upper()
    order_id = str(
        row.get("orderId") or row.get("order_id") or "<missing>"
    ).strip()
    order_link_id = str(
        row.get("orderLinkId") or row.get("order_link_id") or "<missing>"
    ).strip()
    return (
        f"{'conditional' if conditional else 'regular'} {symbol} "
        f"orderId={order_id} orderLinkId={order_link_id}"
    )


def _open_order_symbol(
    variants: Sequence[tuple[str, Mapping[str, Any]]],
) -> str:
    symbols = {
        str(row.get("symbol") or "").strip().upper()
        for _source, row in variants
        if str(row.get("symbol") or "")
    }
    if len(symbols) > 1:
        raise RuntimeError(
            "Bybit demo duplicated one open-order identity across conflicting symbols"
        )
    return next(iter(symbols), "")


def inspect_bybit_demo_order_ownership(
    *,
    client: Any,
    state: AccountState,
    native_order_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
) -> BybitDemoOrderOwnershipSnapshot:
    """Read all regular/conditional venue orders and classify durable ownership."""

    if not bool(getattr(client, "demo", False)):
        raise ValueError("venue-order ownership inspection refuses a non-demo client")

    def read(query_name: str, **params: Any) -> tuple[Mapping[str, Any], ...]:
        try:
            value = client.get_open_orders(settle_coin="USDT", **params)
        except Exception as exc:
            raise RuntimeError(
                "Bybit demo could not prove venue order ownership: "
                f"{query_name} open-order query failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _validated_open_order_rows(value, query_name=query_name)

    all_kinds = read("all-kinds")
    conditional = read("conditional", order_filter="StopOrder")
    grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for source, rows in (("all-kinds", all_kinds), ("conditional", conditional)):
        for row in rows:
            grouped.setdefault(_open_order_identity(row), []).append((source, row))

    unowned: list[UnownedBybitDemoOrder] = []
    for variants in grouped.values():
        symbol = _open_order_symbol(variants)
        description = _describe_open_order(variants)
        if state.events_applied == 0:
            unowned.append(
                UnownedBybitDemoOrder(symbol=symbol, description=description)
            )
            continue
        if any(_kernel_working_order_owns_row(state, row) for _source, row in variants):
            continue
        native_owned = False
        if native_order_verifier is not None:
            try:
                native_owned = any(
                    native_order_verifier(row) for _source, row in variants
                )
            except Exception as exc:
                raise RuntimeError(
                    "Bybit demo could not verify native-protection order ownership: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        if not native_owned:
            unowned.append(
                UnownedBybitDemoOrder(symbol=symbol, description=description)
            )

    return BybitDemoOrderOwnershipSnapshot(
        journal_events_applied=state.events_applied,
        all_kinds_rows_observed=len(all_kinds),
        conditional_rows_observed=len(conditional),
        unique_orders_observed=len(grouped),
        unowned_orders=tuple(unowned),
    )


def require_bybit_demo_order_ownership(
    *,
    client: Any,
    kernel: AccountExecutionKernel,
    native_order_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
) -> None:
    """Fail demo-owner startup unless every venue order has a durable owner.

    Bybit's omitted ``orderFilter`` is documented as all order kinds for linear
    accounts, but startup also issues an explicit ``StopOrder`` query so a
    wrapper/default change cannot hide conditional orders. Duplicate rows are
    folded by durable venue/client identity. An empty account journal cannot
    own any row. Restarts accept only a still-working kernel command or an
    exchange-native protection that the journal-backed protection verifier can
    identify; this function never cancels or adopts an order.
    """

    state = kernel.state()
    snapshot = inspect_bybit_demo_order_ownership(
        client=client,
        state=state,
        native_order_verifier=native_order_verifier,
    )
    if not snapshot.unowned_orders:
        return

    row_summary = "; ".join(
        order.description for order in snapshot.unowned_orders
    )
    if snapshot.journal_events_applied == 0:
        raise RuntimeError(
            "Bybit demo startup refused venue orders with an empty/new account journal: "
            + row_summary
        )
    raise RuntimeError(
        "Bybit demo startup refused unowned venue order(s): " + row_summary
    )


def instrument_rules_from_bybit_row(
    row: Mapping[str, Any],
    *,
    source: str,
    environment: str,
    observed_ts_ns: int,
) -> InstrumentRules:
    symbol = str(row.get("symbol") or "").upper()
    lot = row.get("lotSizeFilter") or {}
    price_filter = row.get("priceFilter") or {}
    leverage = row.get("leverageFilter") or {}
    if (
        not symbol
        or not isinstance(lot, Mapping)
        or not isinstance(price_filter, Mapping)
        or not isinstance(leverage, Mapping)
    ):
        raise RuntimeError("invalid Bybit instrument row")
    return InstrumentRules(
        symbol=symbol,
        qty_step=_finite(lot.get("qtyStep"), label=f"{symbol} qtyStep"),
        min_qty=_finite(lot.get("minOrderQty"), label=f"{symbol} minOrderQty"),
        min_notional=_finite(lot.get("minNotionalValue"), label=f"{symbol} minNotionalValue"),
        tick_size=_finite(price_filter.get("tickSize"), label=f"{symbol} tickSize"),
        max_order_qty=_finite(lot.get("maxMktOrderQty") or lot.get("maxOrderQty") or 0.0, label=f"{symbol} maxOrderQty"),
        max_leverage=_finite(leverage.get("maxLeverage") or 0.0, label=f"{symbol} maxLeverage"),
        source=source,
        environment=environment,
        observed_ts_ns=observed_ts_ns,
    )


class VerifiedBybitDemoRulesProvider:
    """Serve only rules verified against demo order behaviour.

    Public instruments-info is not silently treated as demo truth.  The caller
    must provide probed/verified demo rules for every traded symbol; this keeps
    demo-specific minimum notionals separate from eventual live-money sizing.
    """

    def __init__(self, rules: Mapping[str, InstrumentRules]) -> None:
        self.rules = {symbol.upper(): rule for symbol, rule in rules.items()}
        for symbol, rule in self.rules.items():
            if rule.symbol != symbol or rule.environment != "demo":
                raise ValueError(f"rule {symbol} is not explicitly verified for demo")

    def current(self, symbols: Sequence[str]) -> Mapping[str, InstrumentRules]:
        missing = sorted(set(symbols) - set(self.rules))
        if missing:
            raise RuntimeError(f"missing verified Bybit demo rules for {', '.join(missing)}")
        return {symbol: self.rules[symbol] for symbol in symbols}
