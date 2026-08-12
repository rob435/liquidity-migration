"""Private Bybit demo-account transport and credential boundary."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from liquidity_migration.account.account_owner_lease import DemoAccountMutationLease
from liquidity_migration.core.env_flags import env_flag, reject_ambiguous_flag
from liquidity_migration.core.venue_realm import (
    REALM_CREDENTIAL_VARIABLES,
    REALM_REST_ENDPOINTS,
    VenueRealm,
    venue_realm,
)
from liquidity_migration.marketdata.bybit_errors import (
    renders_ret_code,
    BybitDataError,
    BybitRequestRejected,
    BybitSubmissionUncertain,
    is_transient_venue_fault as _is_transient_venue_fault,
)
from liquidity_migration.marketdata.bybit_market_data import (
    BybitRestRateLimiter as _BybitRestRateLimiter,
    _close_ws_client,
    _patch_pybit_daemon_ping_timer,
)

try:
    from pybit.unified_trading import HTTP, WebSocket
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None
    WebSocket = None


_logger_account = logging.getLogger("liquidity_migration.venue.bybit.account")

__all__ = [
    "BybitDataError",
    "BybitPrivateClient",
    "BybitPrivateWebSocketStream",
    "BybitRequestRejected",
    "BybitSubmissionUncertain",
    "api_key_allows_order_submit",
    "resolve_demo_credentials",
    "resolve_private_credentials",
]

#: The only REST host addressed with demo credentials.
DEMO_REST_ENDPOINT = REALM_REST_ENDPOINTS[VenueRealm.DEMO]


def _exec_time_ns(row: Mapping[str, Any]) -> int:
    """Execution time of one venue row, in nanoseconds; 0 when unreadable."""

    try:
        millis = float(row.get("execTime") or row.get("exec_time") or 0.0)
    except (TypeError, ValueError):
        return 0
    return int(millis * 1_000_000) if millis > 0.0 else 0


def _require_realm_endpoint(client: Any, what: str, *, realm: VenueRealm) -> None:
    """Assert the realm actually resolved to, not the one asked for.

    Every other realm check reads ``self.realm``/``self.testnet`` -- the values
    passed in. The host is chosen inside pybit's ``demo=`` kwarg contract, so a
    pybit upgrade that renamed or dropped the kwarg, or a shim replacing
    ``HTTP``, would address the wrong host while every local guard still read
    what it asked for. The check is symmetric: the resolved endpoint must equal
    the selected realm's, for both realms.

    Scoped by module to real pybit transports so hand-rolled test doubles stay
    usable. ``test_pybit_still_exposes_the_demo_endpoint`` pins the other half:
    a genuine pybit ``HTTP`` still carries ``endpoint``.
    """

    if not type(client).__module__.startswith("pybit"):
        return
    expected = REALM_REST_ENDPOINTS[realm]
    endpoint = str(getattr(client, "endpoint", "") or "").rstrip("/")
    if endpoint != expected:
        raise RuntimeError(
            f"{what} selected realm {realm.value!r} but resolved to "
            f"{endpoint or 'an unknown host'}; only {expected} is permitted for "
            f"private Bybit access in that realm"
        )


def resolve_private_credentials(*, realm: VenueRealm | str) -> tuple[str | None, str | None]:
    """Resolve the credential pair for one explicitly named realm.

    ``realm`` is keyword-only with no default, and the two realms read different
    environment variables. ``REAL_MONEY`` is the arming switch and a typo'd
    value fails startup rather than coercing to a default: demo refuses to run
    while it is armed, mainnet refuses unless it is.
    """

    selected = venue_realm(realm)
    reject_ambiguous_flag("REAL_MONEY")
    reject_ambiguous_flag("DEMO")
    real_money = env_flag("REAL_MONEY")
    key_variable, secret_variable = REALM_CREDENTIAL_VARIABLES[selected]
    if selected is VenueRealm.DEMO:
        if real_money:
            raise RuntimeError(
                "Bybit demo access refuses to run with REAL_MONEY armed; "
                "unset it or select the mainnet realm explicitly"
            )
    elif not real_money:
        raise RuntimeError(
            "Bybit mainnet credentials require REAL_MONEY to be explicitly armed; "
            "naming the mainnet realm is not on its own authorization to trade real capital"
        )
    _logger_account.info("resolved account realm: %s", selected.value)
    return (os.environ.get(key_variable), os.environ.get(secret_variable))


def resolve_demo_credentials() -> tuple[str | None, str | None]:
    """Resolve only the Bybit demo credential pair.

    Retained because the demo owner, the rule probe, and the accounting tools
    all name demo unconditionally and should not be able to name anything else.
    """

    return resolve_private_credentials(realm=VenueRealm.DEMO)


def api_key_allows_order_submit(api_key_info: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether Bybit key metadata permits state-changing order actions.

    Bybit reports granular ContractTrade permissions even on a read-only key, so
    readOnly is authoritative here: such a key passes ordinary liveness checks
    but fails later on set_leverage/place_order.
    """
    raw_read_only = api_key_info.get("readOnly", api_key_info.get("readonly"))
    if raw_read_only is None:
        return False, "Bybit API key metadata is missing readOnly"
    read_only = str(raw_read_only).strip().lower()
    if read_only in {"1", "true", "yes", "on"}:
        return False, "Bybit API key metadata reports readOnly=1"
    if read_only not in {"0", "false", "no", "off"}:
        return False, f"Bybit API key metadata has unrecognised readOnly={raw_read_only!r}"
    permissions = api_key_info.get("permissions")
    if not isinstance(permissions, Mapping):
        return False, "Bybit API key metadata is missing permissions"
    contract = permissions.get("ContractTrade")
    if isinstance(contract, str):
        contract_perms = {contract}
    elif isinstance(contract, Iterable):
        contract_perms = {str(item) for item in contract}
    else:
        contract_perms = set()
    missing = {"Order", "Position"}.difference(contract_perms)
    if missing:
        return False, f"Bybit ContractTrade permissions missing {sorted(missing)}"
    return True, ""


@dataclass(slots=True)
class BybitPrivateClient:
    category: str = "linear"
    testnet: bool = False
    # ``demo`` is the pybit transport kwarg, read elsewhere via
    # ``getattr(client, "demo")``. ``realm`` is the authority and defaults to
    # DEMO when omitted.
    demo: bool = True
    realm: VenueRealm | str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    retries: int = 2
    retry_sleep_seconds: float = 0.5
    rate_limiter: _BybitRestRateLimiter | None = None
    # Read-only clients need no lease. Every state-changing call needs a held
    # canonical lease bound to the authenticated account, credential, and realm.
    mutation_lease: DemoAccountMutationLease | None = None
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.realm = VenueRealm.DEMO if self.realm is None else venue_realm(self.realm)
        if self.testnet:
            raise RuntimeError("BybitPrivateClient does not support testnet")
        expected_demo = self.realm is VenueRealm.DEMO
        if bool(self.demo) is not expected_demo:
            raise RuntimeError(
                f"BybitPrivateClient realm {self.realm.value!r} contradicts demo={self.demo!r}"
            )
        if self.realm is VenueRealm.MAINNET and not env_flag("REAL_MONEY"):
            # A mainnet transport signs requests against the funded account, so
            # naming the realm is not on its own authorization.
            reject_ambiguous_flag("REAL_MONEY")
            raise RuntimeError(
                "Refusing to build a mainnet BybitPrivateClient while REAL_MONEY is unset or false"
            )
        if HTTP is None:
            raise RuntimeError("pybit is required for BybitPrivateClient")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit private execution requires API key and secret")
        self._client = HTTP(
            testnet=self.testnet,
            demo=self.demo,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        _require_realm_endpoint(self._client, "BybitPrivateClient", realm=self.realm)

    def _assert_submit_allowed(self, action: str) -> None:
        """Require live canonical authority at the request-signing boundary."""

        realm = venue_realm(self.realm)
        if self.testnet or (realm is VenueRealm.DEMO) is not bool(self.demo):
            raise RuntimeError(
                f"Refusing to {action}: the client's realm and transport flags disagree"
            )
        lease = self.mutation_lease
        if type(lease) is not DemoAccountMutationLease:
            raise RuntimeError(
                f"Refusing to {action}: no canonical Bybit account mutation lease capability was provided"
            )
        lease.require_held_for(
            api_key=str(self.api_key or ""),
            environment=realm.value,
            action=action,
        )

    def get_wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> dict[str, Any]:
        payload = self._call("get_wallet_balance", accountType=account_type, coin=coin)
        return payload.get("result", {})

    def get_api_key_information(self) -> dict[str, Any]:
        payload = self._call("get_api_key_information")
        return payload.get("result", {})

    def get_instruments_info(self, *, max_pages: int = 50) -> list[dict[str, Any]]:
        """Read structural rules through the authenticated demo endpoint."""

        return self._cursor_result_list(
            "get_instruments_info",
            {"category": self.category, "limit": 1000},
            max_pages=max_pages,
        )

    def get_tickers(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        payload = self._call("get_tickers", **params)
        return payload.get("result", {}).get("list", [])

    def place_order(self, **params: Any) -> dict[str, Any]:
        if "orderLinkId" not in params:
            raise ValueError("orderLinkId is required for idempotent Bybit order submission")
        self._assert_submit_allowed("place_order")
        try:
            payload = self._call_once("place_order", category=self.category, **params)
        except BybitDataError as exc:
            # A duplicate-orderLinkId reject (110072, "OrderLinkedID is
            # duplicate") means Bybit already accepted an order under this
            # idempotency key, normally our own prior submit whose ack was
            # lost. Resubmitting would double the order and raising would
            # orphan a live position, so probe by orderLinkId and return the
            # existing order.
            if not _is_duplicate_order_link(exc):
                raise
            existing = self._lookup_order_by_link(
                symbol=params.get("symbol"),
                order_link_id=params["orderLinkId"],
            )
            if existing is None:
                # The duplicate id proves a prior request reached Bybit, but
                # its outcome is unreadable. A local rejection would drop the
                # command from working exposure and allow a second order.
                raise BybitSubmissionUncertain(
                    f"Bybit reports duplicate orderLinkId {params['orderLinkId']!r}, "
                    "but the existing order is not yet observable"
                ) from exc
            recovered = dict(existing)
            recovered["_idempotent_existing_order"] = True
            return recovered
        result = dict(payload.get("result", {}))
        # V5 puts the server receipt timestamp on the response envelope,
        # outside ``result``; it is the request/ack latency evidence.
        response_time_ms = payload.get("time")
        if response_time_ms is not None:
            result["_response_time_ms"] = response_time_ms
        return result

    def _lookup_order_by_link(
        self,
        *,
        symbol: str | None,
        order_link_id: str,
    ) -> dict[str, Any] | None:
        """Return a place_order-shaped dict for an order already at Bybit under
        ``order_link_id`` (open orders first, then order history), else None.
        Used to make a duplicate-link reject idempotent."""
        if not order_link_id:
            return None
        try:
            open_rows = self.get_open_orders(symbol=symbol) if symbol else self.get_open_orders()
        except Exception:  # noqa: BLE001 - probe must never make the reject worse
            open_rows = []
        for row in open_rows or []:
            if str(row.get("orderLinkId") or "") == order_link_id:
                return dict(row)
        try:
            history = self.get_order_history(
                symbol=symbol,
                order_link_id=order_link_id,
                limit=10,
            )
        except Exception:  # noqa: BLE001
            history = []
        for row in history or []:
            if (
                str(row.get("orderLinkId") or "") == order_link_id
                and str(row.get("orderStatus") or row.get("order_status") or "").lower() in _PROBE_PRESENT_STATUSES
            ):
                return dict(row)
        return None

    def place_orders_batch(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Create up to 20 orders in one signed request, with per-row outcomes.

        Returns one dict per request row, index-aligned: the venue's result
        row plus ``_row_code``/``_row_msg`` from the per-row status list and
        the envelope timestamp as ``_response_time_ms``. Bybit answers a
        partial failure with top-level retCode 0 and per-row codes, so pybit
        raises nothing for failed rows — callers read ``_row_code``. A
        duplicate-link row is probed by orderLinkId and returned as the
        existing order tagged ``_idempotent_existing_order``, exactly like
        the single-order path. The request itself is single-shot: a transport
        failure leaves every row's outcome unknown and raises
        BybitSubmissionUncertain. Rate limits count the rows, not the request,
        so batching buys latency, never quota.
        """

        if not rows:
            raise ValueError("cannot place an empty order batch")
        if len(rows) > 20:
            raise ValueError(f"Bybit accepts at most 20 linear orders per batch, got {len(rows)}")
        for row in rows:
            if "orderLinkId" not in row:
                raise ValueError("orderLinkId is required on every batch row")
        self._assert_submit_allowed("place_batch_order")
        payload = self._call_once(
            "place_batch_order",
            category=self.category,
            request=[dict(row) for row in rows],
        )
        result_rows = payload.get("result", {}).get("list") or []
        status_rows = (payload.get("retExtInfo") or {}).get("list") or []
        if len(result_rows) != len(rows) or len(status_rows) != len(rows):
            # The two response lists are index-aligned with the request by
            # contract; anything else means we cannot map outcomes to orders.
            raise BybitSubmissionUncertain(
                "Bybit batch response rows do not match the request, so every "
                f"row's outcome is unknown: sent {len(rows)}, "
                f"result {len(result_rows)}, status {len(status_rows)}"
            )
        response_time_ms = payload.get("time")
        merged: list[dict[str, Any]] = []
        for request_row, result_row, status_row in zip(rows, result_rows, status_rows):
            row = dict(result_row)
            code = status_row.get("code")
            message = str(status_row.get("msg") or "")
            row["_row_code"] = code
            row["_row_msg"] = message
            if response_time_ms is not None:
                row["_response_time_ms"] = response_time_ms
            if code not in (0, None) and _is_duplicate_order_link(f"{code} {message}"):
                existing = self._lookup_order_by_link(
                    symbol=str(request_row.get("symbol") or "") or None,
                    order_link_id=str(request_row["orderLinkId"]),
                )
                if existing is None:
                    raise BybitSubmissionUncertain(
                        "Bybit reports duplicate orderLinkId "
                        f"{request_row['orderLinkId']!r} in a batch row, but the "
                        "existing order is not yet observable"
                    )
                row = dict(existing)
                row["_idempotent_existing_order"] = True
                row["_row_code"] = 0
                row["_row_msg"] = message
            merged.append(row)
        return merged

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        self._assert_submit_allowed("cancel_order")
        payload = self._call("cancel_order", category=self.category, symbol=symbol, orderLinkId=order_link_id)
        return payload.get("result", {})

    def amend_order(self, *, symbol: str, order_link_id: str, price: str) -> dict[str, Any]:
        """Reprice a working order in place, keeping its id and queue identity.

        Single-shot on purpose: a blind retry could reprice an order that
        filled between attempts, and the caller re-reads order state on any
        rejection anyway.
        """
        payload = self._call_once(
            "amend_order",
            category=self.category,
            symbol=symbol,
            orderLinkId=order_link_id,
            price=price,
        )
        return payload.get("result", {})

    def get_open_orders(
        self,
        *,
        symbol: str | None = None,
        settle_coin: str | None = "USDT",
        order_id: str | None = None,
        order_link_id: str | None = None,
        open_only: int | None = None,
        order_filter: str | None = None,
        limit: int = 50,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(int(limit), 50))}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id
        if open_only is not None:
            if type(open_only) is not int or open_only not in {0, 1, 2}:
                raise ValueError("open_only must be 0, 1, or 2")
            params["openOnly"] = open_only
        if order_filter:
            params["orderFilter"] = order_filter
        return self._cursor_result_list("get_open_orders", params, max_pages=max_pages)

    def get_order_history(
        self,
        *,
        symbol: str | None = None,
        settle_coin: str | None = None,
        order_id: str | None = None,
        order_link_id: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 50,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "category": self.category,
            "limit": max(1, min(int(limit), 50)),
        }
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_limit = max(1, int(max_pages))
        for _ in range(page_limit):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._call_optional(("get_order_history",), **page_params)
            if not payload:
                return rows
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if next_cursor in seen_cursors:
                raise BybitDataError(
                    "Bybit get_order_history returned a non-advancing pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        # Recovery reconstructs accounting from these rows, so a silently
        # truncated tail is worse than a failed pass.
        raise BybitDataError(
            f"Bybit get_order_history pagination exceeded max_pages={page_limit}; "
            "refusing an incomplete result"
        )

    def get_trade_history(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        order_link_id: str | None = None,
        limit: int = 50,
        max_pages: int = 20,
        stop_before_ns: int = 0,
    ) -> list[dict[str, Any]]:
        """Recent executions, newest first.

        ``stop_before_ns`` stops paging once a page is entirely older than that
        instant. Bybit returns these newest-first, so every remaining page is
        older still and a caller that discards old rows would drop them all.
        Pass it only when the caller really does ignore rows that old; left at
        zero the walk pages to exhaustion as before.
        """

        params: dict[str, Any] = {
            "category": self.category,
            "limit": max(1, min(int(limit), 100)),
        }
        if symbol:
            params["symbol"] = symbol
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_limit = max(1, int(max_pages))
        for _ in range(page_limit):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._call_optional(
                ("get_executions", "get_trade_history"),
                **page_params,
            )
            if not payload:
                return rows
            result = payload.get("result", {})
            page = result.get("list", [])
            rows.extend(page)
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if stop_before_ns > 0 and page:
                newest_on_page = max(
                    (_exec_time_ns(row) for row in page),
                    default=0,
                )
                if 0 < newest_on_page < stop_before_ns:
                    return rows
            if next_cursor in seen_cursors:
                raise BybitDataError(
                    "Bybit get_trade_history returned a non-advancing pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        # Fill recovery reconstructs position truth from these rows, so a
        # silently truncated tail is worse than a failed pass.
        raise BybitDataError(
            f"Bybit get_trade_history pagination exceeded max_pages={page_limit}; "
            "refusing an incomplete result"
        )

    def get_positions(
        self,
        *,
        symbol: str | None = None,
        settle_coin: str | None = None,
        limit: int = 200,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(int(limit), 200))}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        return self._cursor_result_list("get_positions", params, max_pages=max_pages)

    def _cursor_result_list(
        self, method_name: str, base_params: Mapping[str, Any], *, max_pages: int
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_limit = max(1, int(max_pages))
        for _ in range(page_limit):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = self._call(method_name, **params)
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if next_cursor in seen_cursors:
                raise BybitDataError(
                    f"Bybit {method_name} returned a non-advancing pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise BybitDataError(
            f"Bybit {method_name} pagination exceeded max_pages={page_limit}; "
            "refusing an incomplete result"
        )

    def get_closed_pnl(
        self,
        *,
        symbol: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 50,
        max_pages: int = 50,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """Closed-PnL records for the account.

        The orphan reconciler backfills exit_price / realized_pnl from these
        when a position vanished from Bybit without a cycle recording the close
        (a manual close on the venue, a stop that fired between cycles, a crash
        mid-place-order that lost the order_link_id). Without the backfill the
        ledger row closes with no exit price and no PnL.

        Bybit caps closed-PnL at 100 rows/page and a symbol re-entered several
        times over the reconciliation lookback can exceed one page, so the
        cursor is followed to the end or the actual closing record is missed.
        """
        base_params: dict[str, Any] = {"category": self.category, "limit": max(1, min(int(limit), 100))}
        if symbol:
            base_params["symbol"] = symbol
        if start_time_ms is not None:
            base_params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            base_params["endTime"] = int(end_time_ms)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_limit = max(1, int(max_pages))
        for _ in range(page_limit):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = (
                self._call("get_closed_pnl", **params) if strict else self._call_optional(("get_closed_pnl",), **params)
            )
            if not payload:
                if strict:
                    raise BybitDataError("Bybit get_closed_pnl returned no payload")
                return rows
            result = payload.get("result", {})
            page_rows = result.get("list") if isinstance(result, Mapping) else None
            if not isinstance(page_rows, list) or any(not isinstance(row, Mapping) for row in page_rows):
                if strict:
                    raise BybitDataError("Bybit get_closed_pnl returned an invalid result list")
                return rows
            rows.extend(dict(row) for row in page_rows)
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if next_cursor in seen_cursors:
                raise BybitDataError(
                    "Bybit get_closed_pnl returned a non-advancing pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        # Truncation would drop the actual closing record; non-strict mode
        # tolerates missing endpoints, never missing tails.
        raise BybitDataError(
            f"Bybit get_closed_pnl pagination exceeded max_pages={page_limit}; "
            "refusing an incomplete result"
        )

    def get_account_transactions(
        self,
        *,
        transaction_type: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 50,
        max_pages: int = 50,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        """Read one explicit Bybit transaction-log type over a bounded window.

        Acceptance evidence uses ``strict=True`` so an absent SDK method,
        transport error, or malformed response cannot be confused with a
        successful query containing zero rows.
        """

        normalized_type = str(transaction_type).upper()
        if normalized_type not in {"TRADE", "SETTLEMENT"}:
            raise ValueError("transaction_type must be TRADE or SETTLEMENT")
        start_ms = int(start_time_ms)
        end_ms = int(end_time_ms)
        if start_ms <= 0 or end_ms <= start_ms:
            raise ValueError("transaction-log window must be positive and increasing")
        if end_ms - start_ms > 7 * 24 * 60 * 60 * 1000:
            raise ValueError("Bybit transaction-log window cannot exceed seven days")
        base_params: dict[str, Any] = {
            "accountType": "UNIFIED",
            "category": self.category,
            "type": normalized_type,
            "limit": max(1, min(int(limit), 50)),
            "startTime": start_ms,
            "endTime": end_ms,
        }
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_limit = max(1, int(max_pages))
        for _ in range(page_limit):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = (
                self._call("get_transaction_log", **params)
                if strict
                else self._call_optional(("get_transaction_log",), **params)
            )
            if not payload:
                if strict:
                    raise BybitDataError("Bybit get_transaction_log returned no payload")
                return rows
            result = payload.get("result", {})
            page_rows = result.get("list") if isinstance(result, Mapping) else None
            if not isinstance(page_rows, list) or any(not isinstance(row, Mapping) for row in page_rows):
                if strict:
                    raise BybitDataError("Bybit get_transaction_log returned an invalid result list")
                return rows
            rows.extend(dict(row) for row in page_rows)
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if next_cursor in seen_cursors:
                raise BybitDataError(
                    "Bybit get_transaction_log returned a non-advancing pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        # The funding reconciler advances its window past whatever this
        # returns, so a truncated settlement tail is skipped permanently.
        raise BybitDataError(
            f"Bybit get_transaction_log pagination exceeded max_pages={page_limit}; "
            "refusing an incomplete result"
        )

    def set_leverage(
        self, *, symbol: str, buy_leverage: float = 1.0, sell_leverage: float | None = None
    ) -> dict[str, Any]:
        if buy_leverage <= 0.0:
            raise ValueError("buy_leverage must be positive")
        effective_sell = buy_leverage if sell_leverage is None else sell_leverage
        if effective_sell <= 0.0:
            raise ValueError("sell_leverage must be positive")
        self._assert_submit_allowed("set_leverage")
        # Retry a transient set_leverage failure rather than dropping a valid
        # entry. _call_once (not _call) keeps the original error text, which
        # carries the "110043 not modified" marker, so that case returns at once.
        attempts = max(self.retries, 1)
        last_error: BybitDataError = BybitDataError("Bybit set_leverage failed")
        for attempt in range(attempts):
            try:
                payload = self._call_once(
                    "set_leverage",
                    category=self.category,
                    symbol=symbol,
                    buyLeverage=_leverage_text(buy_leverage),
                    sellLeverage=_leverage_text(effective_sell),
                )
            except BybitDataError as exc:
                # Anchored on the venue's own code, never a bare digit scan:
                # the rendered error carries the whole request body.
                if renders_ret_code(exc, 110043):
                    return {
                        "symbol": symbol,
                        "buyLeverage": _leverage_text(buy_leverage),
                        "sellLeverage": _leverage_text(effective_sell),
                        "retCode": 110043,
                    }
                last_error = exc
                # A definite refusal will refuse the identical request again.
                # Retrying it slept half a second and resent a signed POST that
                # could only fail, on the path between deciding to trade and
                # the order leaving — nine times over for a nine-slice batch.
                if isinstance(exc, BybitRequestRejected) and not _is_transient_venue_fault(exc):
                    raise
                if attempt + 1 >= attempts:
                    raise
                time.sleep(self.retry_sleep_seconds * (2**attempt))
                continue
            return payload.get("result", {})
        raise last_error  # pragma: no cover - the loop always returns or raises

    def set_trading_stop(
        self,
        *,
        symbol: str,
        tpsl_mode: str = "Full",
        position_idx: int = 0,
        stop_loss: str | float | None = None,
        take_profit: str | float | None = None,
        trailing_stop: str | float | None = None,
        active_price: str | float | None = None,
        tp_trigger_by: str | None = "MarkPrice",
        sl_trigger_by: str | None = "MarkPrice",
    ) -> dict[str, Any]:
        self._assert_submit_allowed("set_trading_stop")
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "tpslMode": tpsl_mode,
            "positionIdx": position_idx,
        }
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if trailing_stop is not None:
            params["trailingStop"] = str(trailing_stop)
        if active_price is not None:
            params["activePrice"] = str(active_price)
        if tp_trigger_by:
            params["tpTriggerBy"] = tp_trigger_by
        if sl_trigger_by:
            params["slTriggerBy"] = sl_trigger_by
        try:
            payload = self._call_once("set_trading_stop", **params)
        except BybitDataError as exc:
            # ErrCode 34040 "not modified": the requested stop levels are
            # already installed, so this is a converged no-op, not a failure --
            # the same classification set_leverage gives its 110043 twin.
            # Anchored on the code, never a bare digit scan: the rendered error
            # ends with the request body, so a stop price of 134040 or
            # 0.0034040 made every refusal of that install a silent success --
            # and the caller journals "active" and clears the breach latch on
            # it, recording a naked position as protected.
            if not renders_ret_code(exc, 34040):
                raise
            return {"symbol": symbol, "retCode": 34040}
        return payload.get("result", {})

    def _call_optional(self, method_names: Iterable[str], **params: Any) -> dict[str, Any] | None:
        for method_name in method_names:
            if hasattr(self._client, method_name):
                return self._call(method_name, **params)
        return None

    def _call_once(self, method_name: str, **params: Any) -> dict[str, Any]:
        # Every single-shot private call is state-changing, so the capability
        # check lives here too and not only on the public method.
        self._assert_submit_allowed(method_name)
        method = getattr(self._client, method_name)
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            payload = method(**params)
            ret_code = payload.get("retCode")
            if ret_code != 0:
                # "Not now" is not "no". A busy matching engine answering a
                # reduce-only close used to arrive here as a definite reject,
                # which terminalizes the command — the stop had fired and the
                # position kept running. Uncertain instead: the owner keeps the
                # command live, reconciliation probes the orderLinkId, and
                # convergence re-plans what the venue never took.
                if _is_transient_venue_fault(payload):
                    raise BybitSubmissionUncertain(
                        f"Bybit {method_name} hit a transient venue fault: {payload}"
                    )
                raise BybitRequestRejected(f"Bybit {method_name} failed: {payload}")
            return payload
        except BybitDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
            if type(exc).__name__ == "InvalidRequestError":
                if _is_transient_venue_fault(exc):
                    raise BybitSubmissionUncertain(
                        f"Bybit {method_name} hit a transient venue fault: {exc}"
                    ) from exc
                raise BybitRequestRejected(f"Bybit {method_name} failed: {exc}") from exc
            raise BybitSubmissionUncertain(
                f"Bybit {method_name} outcome is unknown after transport failure: {exc}"
            ) from exc

    def _call(self, method_name: str, **params: Any) -> dict[str, Any]:
        requires_mutation_lease = method_name not in _PRIVATE_READ_ONLY_METHODS
        if requires_mutation_lease:
            self._assert_submit_allowed(method_name)
        method = getattr(self._client, method_name)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                if requires_mutation_lease:
                    # Re-prove the lease before every retry, not just once
                    # before a long transport/backoff sequence.
                    self._assert_submit_allowed(method_name)
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire()
                payload = method(**params)
                ret_code = payload.get("retCode")
                if ret_code != 0:
                    raise BybitDataError(f"Bybit {method_name} failed: {payload}")
                return payload
            except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
                last_error = exc
                # A non-zero retCode that is neither a rate-limit nor a venue
                # "not now" is a definite reject; retrying the identical request
                # just wastes backoff. The transient class matters most on a
                # close: treating a busy matching engine as a final refusal
                # leaves a position running after its stop has fired.
                if isinstance(exc, BybitDataError) and not _is_transient_venue_fault(exc):
                    raise
                # pybit 5.x raises InvalidRequestError for a non-zero retCode
                # before the branch above ever sees it. Match by class name to
                # avoid a hard pybit dependency.
                if type(exc).__name__ == "InvalidRequestError" and not _is_transient_venue_fault(exc):
                    raise BybitDataError(f"Bybit {method_name} failed: {exc}") from exc
                if attempt + 1 >= self.retries:
                    break
                time.sleep(self.retry_sleep_seconds * (2**attempt))
        # Keep the venue's message inline: callers ledger str(exc) and
        # __cause__ does not survive into those error columns.
        raise BybitDataError(f"Bybit {method_name} failed after retries: {last_error}") from last_error


_PRIVATE_READ_ONLY_METHODS = frozenset(
    {
        "get_api_key_information",
        "get_closed_pnl",
        "get_executions",
        "get_instruments_info",
        "get_open_orders",
        "get_order_history",
        "get_positions",
        "get_tickers",
        "get_trade_history",
        "get_transaction_log",
        "get_wallet_balance",
    }
)


def _leverage_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _is_duplicate_order_link(value: Any) -> bool:
    """True for Bybit's duplicate-orderLinkId reject.

    The official error table lists 110072 ("OrderLinkedID is duplicate");
    traffic has also carried an "orderLinkID exists" wording. Matched on the
    documented code plus message text covering both wordings — and nothing
    else. A bare code with no duplicate wording must NOT classify: 110089 in
    particular is documented as "Exceeds the maximum risk limit level", and
    classifying a risk-limit refusal as a maybe-duplicate turned a definite
    reject into an uncertain outcome that wedged the command."""
    text = str(value).lower()
    if "110072" in text:
        return True
    return "orderlink" in text and ("exist" in text or "duplicate" in text)


@dataclass(slots=True)
class BybitPrivateWebSocketStream:
    category: str = "linear"
    testnet: bool = False
    demo: bool = True
    realm: VenueRealm | str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    _client: Any = field(init=False, repr=False)
    _control_lock: Any = field(init=False, repr=False)
    _acked_topics: set[str] = field(init=False, repr=False)
    _last_control_error: str = field(init=False, repr=False)

    _EXPECTED_TOPICS = frozenset({"execution", "order"})

    def __post_init__(self) -> None:
        self.realm = VenueRealm.DEMO if self.realm is None else venue_realm(self.realm)
        if self.testnet:
            raise RuntimeError("BybitPrivateWebSocketStream does not support testnet")
        if bool(self.demo) is not (self.realm is VenueRealm.DEMO):
            raise RuntimeError(
                f"BybitPrivateWebSocketStream realm {self.realm.value!r} contradicts demo={self.demo!r}"
            )
        if self.realm is VenueRealm.MAINNET and not env_flag("REAL_MONEY"):
            reject_ambiguous_flag("REAL_MONEY")
            raise RuntimeError(
                "Refusing to open a mainnet private websocket while REAL_MONEY is unset or false"
            )
        if WebSocket is None:
            raise RuntimeError("pybit is required for BybitPrivateWebSocketStream")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit private websocket stream requires API key and secret")
        self._control_lock = threading.RLock()
        self._acked_topics = set()
        self._last_control_error = ""
        _patch_pybit_daemon_ping_timer()
        self._client = WebSocket(
            testnet=self.testnet,
            demo=self.demo,
            channel_type="private",
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        self._install_control_ack_tracking()

    @staticmethod
    def _account_topic(topic: str) -> str:
        if topic == "order":
            return "order"
        if topic == "execution" or topic.startswith("execution."):
            return "execution"
        return ""

    def _ack_topics(self, message: Mapping[str, Any]) -> set[str]:
        request_id = str(message.get("req_id") or "")
        subscriptions = getattr(self._client, "subscriptions", None)
        if not request_id or not isinstance(subscriptions, Mapping):
            return set()
        raw = subscriptions.get(request_id)
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return set()
        if not isinstance(payload, Mapping):
            return set()
        args = payload.get("args")
        if not isinstance(args, list):
            return set()
        return {
            account_topic
            for value in args
            if (account_topic := self._account_topic(str(value)))
        }

    def _observe_subscription_ack(self, message: Mapping[str, Any]) -> None:
        success = message.get("success")
        if type(success) is not bool:
            return
        topics = self._ack_topics(message)
        response = str(
            message.get("ret_msg")
            or message.get("retMsg")
            or "venue returned no reason"
        )
        with self._control_lock:
            if success:
                self._acked_topics.update(topics)
                if self._EXPECTED_TOPICS.issubset(self._acked_topics):
                    self._last_control_error = ""
                return
            self._acked_topics.difference_update(topics)
            topic_text = ",".join(sorted(topics)) or "uncorrelated"
            self._last_control_error = (
                f"private websocket subscription rejected for {topic_text}: {response}"
            )[:500]

    def _observe_auth_ack(self, message: Mapping[str, Any]) -> None:
        success = message.get("success")
        is_error = message.get("type") == "error"
        if type(success) is not bool and not is_error:
            return
        with self._control_lock:
            # Each connection generation proves its own resubscription ACKs;
            # old ACKs cannot cross a reconnect.
            self._acked_topics.clear()
            if success is True and not is_error:
                self._last_control_error = ""
                return
            response = str(
                message.get("ret_msg")
                or message.get("retMsg")
                or message
            )
            self._last_control_error = (
                f"private websocket authentication rejected: {response}"
            )[:500]

    def _install_control_ack_tracking(self) -> None:
        subscription_handler = getattr(
            self._client,
            "_process_subscription_message",
            None,
        )
        auth_handler = getattr(self._client, "_process_auth_message", None)
        if (
            not callable(subscription_handler)
            or not callable(auth_handler)
            or not hasattr(self._client, "auth")
        ):
            _close_ws_client(self._client)
            raise RuntimeError(
                "pinned pybit private websocket lacks control-ack hooks"
            )

        def tracked_subscription(message: Mapping[str, Any]) -> Any:
            self._observe_subscription_ack(message)
            return subscription_handler(message)

        def tracked_auth(message: Mapping[str, Any]) -> Any:
            self._observe_auth_ack(message)
            return auth_handler(message)

        self._client._process_subscription_message = tracked_subscription
        self._client._process_auth_message = tracked_auth

    def _mark_subscription_pending(self, topic: str) -> None:
        with self._control_lock:
            self._acked_topics.discard(topic)
            self._last_control_error = ""

    def subscribe_wallet(self, callback: Any) -> bool:
        """Push account balance changes instead of polling for them.

        The socket is already open and authenticated, so this costs nothing per
        message and replaces a REST read that sat between the decision to trade
        and the order leaving. Returns False when the client cannot serve the
        topic, so the caller can keep the REST path rather than fail closed on
        an optional accelerator. Wallet is a read-only topic: it is deliberately
        NOT registered as a mutation-topic ack, so readiness still means orders
        and executions.
        """

        stream = getattr(self._client, "wallet_stream", None)
        if not callable(stream):
            return False
        try:
            stream(callback=callback)
        except Exception as exc:  # noqa: BLE001 - an optional feed must not kill the owner
            with self._control_lock:
                self._last_control_error = (
                    f"private websocket wallet subscription failed: "
                    f"{type(exc).__name__}: {exc}"
                )[:500]
            return False
        return True

    def subscribe_positions(self, callback: Any) -> bool:
        """Push position rows instead of polling for them.

        The venue emits the position, stop loss included, within milliseconds
        of a fill. Entry-attached stop verification used to read that back over
        REST and retry when the position was not yet visible -- two round trips
        after every market entry. Same read-only accelerator contract as
        ``subscribe_wallet``: False means the caller keeps the REST path.
        """

        stream = getattr(self._client, "position_stream", None)
        if not callable(stream):
            return False
        try:
            stream(callback=callback)
        except Exception as exc:  # noqa: BLE001 - an optional feed must not kill the owner
            with self._control_lock:
                self._last_control_error = (
                    f"private websocket position subscription failed: "
                    f"{type(exc).__name__}: {exc}"
                )[:500]
            return False
        return True

    def subscribe_orders(self, callback: Any) -> None:
        self._mark_subscription_pending("order")
        try:
            self._client.order_stream(callback=callback)
        except Exception as exc:
            with self._control_lock:
                self._last_control_error = (
                    f"private websocket order subscription failed: "
                    f"{type(exc).__name__}: {exc}"
                )[:500]
            raise

    def subscribe_executions(self, callback: Any, *, fast: bool = False) -> None:
        self._mark_subscription_pending("execution")
        try:
            if fast and hasattr(self._client, "fast_execution_stream"):
                self._client.fast_execution_stream(callback=callback)
                return
            self._client.execution_stream(callback=callback)
        except Exception as exc:
            with self._control_lock:
                self._last_control_error = (
                    f"private websocket execution subscription failed: "
                    f"{type(exc).__name__}: {exc}"
                )[:500]
            raise

    def is_connected(self) -> bool | None:
        """Socket-level liveness of the private stream. pybit's WebSocket subclasses
        _WebSocketManager, whose is_connected() reads ``ws.sock.connected`` — a TRUE
        connection signal independent of data flow, so a watchdog can distinguish a
        DEAD socket from a merely-quiet account (the private stream only pushes on
        position/order changes). Returns None when the client doesn't expose it (older
        pybit) so the caller can stay conservative and not force a reconnect."""
        probe = getattr(self._client, "is_connected", None)
        if not callable(probe):
            return None
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a liveness probe must never raise into the cycle
            return None

    def is_ready(self) -> bool | None:
        """Require socket, positive authentication, and both mutation-topic ACKs."""

        connected = self.is_connected()
        if connected is not True:
            return connected
        if getattr(self._client, "auth", None) is not True:
            return False
        with self._control_lock:
            return self._EXPECTED_TOPICS.issubset(self._acked_topics)

    @property
    def readiness_detail(self) -> str:
        connected = self.is_connected()
        if connected is False:
            return "private execution websocket is disconnected"
        if connected is None:
            return "private execution websocket liveness is unavailable"
        with self._control_lock:
            error = self._last_control_error
            missing = sorted(self._EXPECTED_TOPICS - self._acked_topics)
        if getattr(self._client, "auth", None) is not True:
            return error or "private execution websocket authentication is not confirmed"
        if missing:
            return error or (
                "private execution websocket awaits positive subscription ACKs: "
                + ", ".join(missing)
            )
        return ""

    def close(self) -> None:
        _close_ws_client(self._client)


# Order statuses that mean a probed orderLinkId is genuinely working at the venue
# (so a WS-fallback resubmit should be suppressed). Terminal-bad statuses
# (Rejected/Cancelled/Deactivated/Expired/PartiallyFilledCanceled) mean the submit
# did not take effect and may be resubmitted.
_PROBE_PRESENT_STATUSES = frozenset({"new", "partiallyfilled", "filled", "untriggered", "triggered"})
