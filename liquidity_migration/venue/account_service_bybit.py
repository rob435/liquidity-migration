"""Bybit account providers for the single-owner account execution service."""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from liquidity_migration.account.account_contracts import (
    AccountRiskSnapshot,
    AccountState,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.account.execution_adapters import (
    L2BookSnapshot,
)
from liquidity_migration.account.market_capture import SequenceAwareMarketRecorder
from liquidity_migration.core.venue_realm import VenueRealm, venue_realm

def require_named_realm(client: Any, *, label: str) -> VenueRealm:
    """Read the realm a private client names, refusing an unnamed or unparsable one."""

    try:
        return venue_realm(getattr(client, "realm", None))
    except ValueError as exc:
        raise ValueError(
            f"{label} requires a client naming venue realm 'demo' or 'mainnet'"
        ) from exc


def _finite(value: Any, *, label: str) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit {label} is missing/non-numeric") from exc
    if not math.isfinite(output):
        raise RuntimeError(f"Bybit {label} is non-finite")
    return output


def _finite_or_none(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _usdt_coin_row(account: Mapping[str, Any]) -> Mapping[str, Any] | None:
    coins = account.get("coin")
    if not isinstance(coins, list):
        return None
    for row in coins:
        if isinstance(row, Mapping) and str(row.get("coin") or "").upper() == "USDT":
            return row
    return None


def _account_equity(account: Mapping[str, Any], *, realm: VenueRealm) -> float:
    """Account equity, preferring the venue's account-wide figure.

    The venue blanks the account-wide aggregates in some unified-account
    margin modes (observed live 2026-08-04 on the funded account) while the
    per-coin row stays populated, so each rung falls through only on blank
    fields — a rung never mixes numeric and defaulted values.
    """

    equity = _finite_or_none(account.get("totalEquity"))
    if equity is not None:
        return equity
    wallet = _finite_or_none(account.get("totalWalletBalance"))
    perp_upl = _finite_or_none(account.get("totalPerpUPL"))
    if wallet is not None and perp_upl is not None:
        return wallet + perp_upl
    coin = _usdt_coin_row(account)
    if coin is not None:
        coin_equity = _finite_or_none(coin.get("equity"))
        if coin_equity is not None:
            return coin_equity
    raise RuntimeError(
        f"Bybit {realm.value} UNIFIED wallet carries no numeric equity "
        "(totalEquity, totalWalletBalance+totalPerpUPL, and the USDT coin row all blank)"
    )


def _available_margin(account: Mapping[str, Any], *, realm: VenueRealm) -> float:
    available = _finite_or_none(account.get("totalAvailableBalance"))
    if available is not None:
        return available
    margin_balance = _finite_or_none(account.get("totalMarginBalance"))
    initial_margin = _finite_or_none(account.get("totalInitialMargin"))
    if margin_balance is not None and initial_margin is not None:
        return margin_balance - initial_margin
    coin = _usdt_coin_row(account)
    if coin is not None:
        wallet = _finite_or_none(coin.get("walletBalance"))
        order_im = _finite_or_none(coin.get("totalOrderIM"))
        position_im = _finite_or_none(coin.get("totalPositionIM"))
        locked = _finite_or_none(coin.get("locked"))
        upl = _finite_or_none(coin.get("unrealisedPnl"))
        if (
            wallet is not None
            and order_im is not None
            and position_im is not None
            and locked is not None
            and upl is not None
        ):
            # Charge unrealized losses, never count unrealized gains.
            return wallet + min(0.0, upl) - order_im - position_im - locked
    raise RuntimeError(
        f"Bybit {realm.value} UNIFIED wallet carries no numeric available margin "
        "(totalAvailableBalance, totalMarginBalance-totalInitialMargin, "
        "and the USDT coin row all blank)"
    )


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
                # Bound for rejected batches and crash/retry churn; the adapter
                # consumes contexts promptly.
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


class BybitAccountSnapshotProvider:
    """Read one fresh wallet snapshot for the realm the client names."""

    def __init__(self, client: Any, *, clock: Clock | None = None) -> None:
        self.realm = require_named_realm(client, label="account snapshot provider")
        self.client = client
        self.clock = clock or SystemClock()

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        result = self.client.get_wallet_balance(account_type="UNIFIED", coin="USDT")
        rows = result.get("list") if isinstance(result, Mapping) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            raise RuntimeError(f"Bybit {self.realm.value} wallet response has no UNIFIED account row")
        account = rows[0]
        equity = _account_equity(account, realm=self.realm)
        available = _available_margin(account, realm=self.realm)
        if equity <= 0.0 or available < 0.0:
            # Carry the numbers: a hand-opened isolated-margin position can
            # absorb the whole wallet as position margin, and the bare message
            # gave the operator nothing to diagnose that with (2026-08-06).
            raise RuntimeError(
                f"Bybit {self.realm.value} wallet snapshot has nonpositive equity "
                f"or negative available margin "
                f"(equity={equity:.2f} USDT, available={available:.2f} USDT)"
            )
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
            snapshot_key=f"bybit-{self.realm.value}:"
            + hashlib.sha256(canonical_json(material)).hexdigest()[:20],
            snapshot_ts_ns=observed_ns,
        )


@dataclass(frozen=True, slots=True)
class UnownedVenueOrder:
    symbol: str
    description: str


@dataclass(frozen=True, slots=True)
class BybitOrderOwnershipSnapshot:
    journal_events_applied: int
    all_kinds_rows_observed: int
    conditional_rows_observed: int
    unique_orders_observed: int
    unowned_orders: tuple[UnownedVenueOrder, ...]


def _validated_open_order_rows(
    value: Any,
    *,
    realm: VenueRealm,
    query_name: str,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(
            f"Bybit {realm.value} {query_name} open-order query returned a non-list payload"
        )
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise RuntimeError(
                f"Bybit {realm.value} {query_name} open-order query returned a non-object row at index {index}"
            )
        symbol = row.get("symbol")
        if type(symbol) is not str or not symbol.strip():
            raise RuntimeError(
                f"Bybit {realm.value} {query_name} open-order row {index} lacks symbol"
            )
        order_id = row.get("orderId") or row.get("order_id")
        order_link_id = row.get("orderLinkId") or row.get("order_link_id")
        if not (
            (type(order_id) is str and bool(order_id.strip()))
            or (type(order_link_id) is str and bool(order_link_id.strip()))
        ):
            raise RuntimeError(
                f"Bybit {realm.value} {query_name} open-order row {index} lacks durable order identity"
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
    *,
    realm: VenueRealm,
) -> str:
    symbols = {
        str(row.get("symbol") or "").strip().upper()
        for _source, row in variants
        if str(row.get("symbol") or "")
    }
    if len(symbols) > 1:
        raise RuntimeError(
            f"Bybit {realm.value} duplicated one open-order identity across conflicting symbols"
        )
    return next(iter(symbols), "")


def inspect_bybit_order_ownership(
    *,
    client: Any,
    state: AccountState,
    native_order_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
) -> BybitOrderOwnershipSnapshot:
    """Read all regular/conditional venue orders and classify durable ownership."""

    realm = require_named_realm(client, label="venue-order ownership inspection")

    def read(query_name: str, **params: Any) -> tuple[Mapping[str, Any], ...]:
        try:
            value = client.get_open_orders(settle_coin="USDT", **params)
        except Exception as exc:
            raise RuntimeError(
                f"Bybit {realm.value} could not prove venue order ownership: "
                f"{query_name} open-order query failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _validated_open_order_rows(value, realm=realm, query_name=query_name)

    all_kinds = read("all-kinds")
    conditional = read("conditional", order_filter="StopOrder")
    grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for source, rows in (("all-kinds", all_kinds), ("conditional", conditional)):
        for row in rows:
            grouped.setdefault(_open_order_identity(row), []).append((source, row))

    unowned: list[UnownedVenueOrder] = []
    for variants in grouped.values():
        symbol = _open_order_symbol(variants, realm=realm)
        description = _describe_open_order(variants)
        if state.events_applied == 0:
            unowned.append(UnownedVenueOrder(symbol=symbol, description=description))
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
                    f"Bybit {realm.value} could not verify native-protection order ownership: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        if not native_owned:
            unowned.append(UnownedVenueOrder(symbol=symbol, description=description))

    return BybitOrderOwnershipSnapshot(
        journal_events_applied=state.events_applied,
        all_kinds_rows_observed=len(all_kinds),
        conditional_rows_observed=len(conditional),
        unique_orders_observed=len(grouped),
        unowned_orders=tuple(unowned),
    )


def require_bybit_order_ownership(
    *,
    client: Any,
    kernel: AccountExecutionKernel,
    native_order_verifier: Callable[[Mapping[str, Any]], bool] | None = None,
) -> None:
    """Fail owner startup unless every venue order has a durable owner.

    An omitted ``orderFilter`` covers all linear order kinds, but startup also
    issues an explicit ``StopOrder`` query so a wrapper default cannot hide
    conditional orders; duplicates fold by venue/client identity. An empty
    journal owns nothing. Only a still-working kernel command or a
    verifier-identified native protection is accepted, and nothing here cancels
    or adopts an order.
    """

    realm = require_named_realm(client, label="venue-order ownership inspection")
    state_ref = getattr(kernel, "_state_ref", None)
    state = state_ref() if callable(state_ref) else kernel.state()
    snapshot = inspect_bybit_order_ownership(
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
            f"Bybit {realm.value} startup refused venue orders with an empty/new account journal: "
            + row_summary
        )
    raise RuntimeError(
        f"Bybit {realm.value} startup refused unowned venue order(s): " + row_summary
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
    """Serve only rules bound to one explicitly named realm.

    The demo owner requires probed demo rules, which carry demo's empirical
    minimum notionals; the mainnet owner gets venue-declared instruments-info
    rules, a weaker standard recorded on each rule's ``source`` and
    ``environment``. A rule whose environment is not the owner's realm is
    refused outright.
    """

    def __init__(
        self,
        rules: Mapping[str, InstrumentRules],
        *,
        environment: str = "demo",
    ) -> None:
        self.environment = str(environment).strip().lower()
        if not self.environment:
            raise ValueError("rules provider requires an explicit environment")
        self.rules = {symbol.upper(): rule for symbol, rule in rules.items()}
        for symbol, rule in self.rules.items():
            if rule.symbol != symbol or rule.environment != self.environment:
                raise ValueError(
                    f"rule {symbol} is not explicitly verified for {self.environment}"
                )

    def current(self, symbols: Sequence[str]) -> Mapping[str, InstrumentRules]:
        missing = sorted(set(symbols) - set(self.rules))
        if missing:
            raise RuntimeError(
                f"missing verified Bybit {self.environment} rules for {', '.join(missing)}"
            )
        return {symbol: self.rules[symbol] for symbol in symbols}
