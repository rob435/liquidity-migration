"""Private Bybit demo-account transport and credential boundary."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .account_owner_lease import DemoAccountMutationLease
from .bybit_errors import (
    BybitDataError,
    BybitRequestRejected,
    BybitSubmissionUncertain,
    is_rate_limit as _is_rate_limit,
)
from .bybit_market_data import (
    BybitRestRateLimiter as _BybitRestRateLimiter,
    _close_ws_client,
    _patch_pybit_daemon_ping_timer,
)

try:
    from pybit.unified_trading import HTTP, WebSocket
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None
    WebSocket = None


_logger_account = logging.getLogger("liquidity_migration.bybit.account")

__all__ = [
    "BybitDataError",
    "BybitPrivateClient",
    "BybitPrivateWebSocketStream",
    "BybitRequestRejected",
    "BybitSubmissionUncertain",
    "api_key_allows_order_submit",
    "resolve_demo_credentials",
    "validate_demo_order_permission",
]

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSEY_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})


def _env_flag(name: str) -> bool:
    """True when environment variable ``name`` is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def resolve_demo_credentials() -> tuple[str | None, str | None]:
    """Resolve only the Bybit demo credential pair.

    Cutover code has no mainnet credential-selection branch.  ``REAL_MONEY`` is
    still parsed so a stale or ambiguous operator setting fails closed, but this
    function never reads ``BYBIT_REAL_API_KEY`` or ``BYBIT_REAL_API_SECRET``.
    """

    _reject_ambiguous_flag("REAL_MONEY")
    _reject_ambiguous_flag("DEMO")
    if _env_flag("REAL_MONEY"):
        raise RuntimeError("Bybit account cutover is demo/paper only; REAL_MONEY must be unset or false")
    _logger_account.info("resolved account: demo")
    return (
        os.environ.get("BYBIT_DEMO_API_KEY"),
        os.environ.get("BYBIT_DEMO_API_SECRET"),
    )


def _reject_ambiguous_flag(name: str) -> None:
    """Raise if ``name`` is set to a value that is neither clearly true nor
    clearly false. Keeps the fail-safe direction (an unrecognised value never
    silently means 'on'), but surfaces a typo'd high-stakes toggle at startup
    instead of letting it coerce to the default."""
    raw = os.environ.get(name)
    if raw is None:
        return
    normalised = raw.strip().lower()
    if normalised in _TRUTHY_ENV_VALUES or normalised in _FALSEY_ENV_VALUES:
        return
    raise RuntimeError(
        f"{name}={raw!r} is not a recognised boolean. Use one of "
        f"{sorted(_TRUTHY_ENV_VALUES)} to enable or {sorted(_FALSEY_ENV_VALUES - {''})} "
        f"(or unset) to disable -- refusing to guess for a safety-critical toggle."
    )


def validate_demo_order_permission(*, confirm_demo_orders: bool) -> None:
    """Guard the demo account owner: explicit confirmation and demo credentials only."""
    if not confirm_demo_orders:
        raise RuntimeError("Refusing to submit orders without --confirm-demo-orders")
    resolve_demo_credentials()


def api_key_allows_order_submit(api_key_info: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether Bybit key metadata permits state-changing order actions.

    Bybit can report granular ContractTrade permissions while the whole key is
    still read-only. The readOnly flag is authoritative for this incident class:
    a read-only key can read wallet/positions, so ordinary liveness checks pass,
    but order-submitting daemons later fail on set_leverage/place_order.
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
    demo: bool = True
    api_key: str | None = None
    api_secret: str | None = None
    retries: int = 2
    retry_sleep_seconds: float = 0.5
    rate_limiter: _BybitRestRateLimiter | None = None
    # Read-only clients need no lease. Every state-changing call must carry a
    # currently held canonical lease bound to the authenticated demo account
    # and API credential. Mainnet mutation is categorically absent.
    mutation_lease: DemoAccountMutationLease | None = None
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.demo or self.testnet:
            raise RuntimeError(
                "BybitPrivateClient is api-demo-only; testnet and mainnet private access are not part of the account cutover"
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

    def _assert_submit_allowed(self, action: str) -> None:
        """Require live canonical authority at the request-signing boundary."""

        if not self.demo or self.testnet:
            raise RuntimeError(
                f"Refusing to {action}: only the Bybit api-demo mutation realm is supported"
            )
        lease = self.mutation_lease
        if type(lease) is not DemoAccountMutationLease:
            raise RuntimeError(
                f"Refusing to {action}: no canonical Bybit demo account mutation lease capability was provided"
            )
        lease.require_held_for(
            api_key=str(self.api_key or ""),
            environment="demo",
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
            # A duplicate-orderLinkId reject (110089) is NOT a failure: it means
            # Bybit already accepted an order under this idempotency key — almost
            # always our own prior submit (e.g. the trade router's WS submit
            # reached the venue, the ack was lost, and the REST fallback re-sent
            # the same orderLinkId). Resubmitting a second order would be wrong;
            # raising would orphan a LIVE position (the caller records an error
            # and writes no ledger row). Probe by orderLinkId and return the
            # existing order so the submit is idempotent end to end.
            if not _is_duplicate_order_link(exc):
                raise
            existing = self._lookup_order_by_link(
                symbol=params.get("symbol"),
                order_link_id=params["orderLinkId"],
            )
            if existing is None:
                # A duplicate id proves that some prior request reached Bybit,
                # but until order/trade history becomes readable its outcome is
                # unknown. Never turn this into a local rejection: that would
                # remove the command from working exposure and permit a second
                # order while the first may still be live.
                raise BybitSubmissionUncertain(
                    f"Bybit reports duplicate orderLinkId {params['orderLinkId']!r}, "
                    "but the existing order is not yet observable"
                ) from exc
            recovered = dict(existing)
            recovered["_idempotent_existing_order"] = True
            return recovered
        result = dict(payload.get("result", {}))
        # V5 exposes the server receipt timestamp at the response envelope,
        # outside ``result``. Preserve it for account-owner request/ack latency
        # evidence; dropping it makes one-way entry/response decomposition
        # impossible even when the operator supplies a clock-offset receipt.
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

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        self._assert_submit_allowed("cancel_order")
        payload = self._call("cancel_order", category=self.category, symbol=symbol, orderLinkId=order_link_id)
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
        for _ in range(max(1, int(max_pages))):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._call_optional(("get_order_history",), **page_params)
            if not payload:
                break
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return rows

    def get_trade_history(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        order_link_id: str | None = None,
        limit: int = 50,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
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
        for _ in range(max(1, int(max_pages))):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._call_optional(
                ("get_executions", "get_trade_history"),
                **page_params,
            )
            if not payload:
                break
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return rows

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

        Used by the orphan reconciler to backfill exit_price / realized_pnl on
        trades where the open position vanished from Bybit without our cycle
        recording the close (eg. a manual close on the venue, a stop-loss that
        fired between cycles, or a cycle crash mid-place-order whose order_link_id
        we lost). Without this backfill the reconciler closes the ledger row with
        no exit price and no PnL — accurate that the position is gone, but the
        ledger loses the trade outcome.

        Bybit caps closed-PnL at <=100 rows/page; a symbol re-entered several
        times over the reconciliation lookback can exceed one page, so follow
        ``nextPageCursor`` to the end. Without
        this the backfill could miss the actual closing record for a re-entered
        symbol. Returns the result.list rows (empty on a missing
        endpoint); ``max_pages`` bounds the loop defensively.
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
        for _ in range(max(1, int(max_pages))):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = (
                self._call("get_closed_pnl", **params) if strict else self._call_optional(("get_closed_pnl",), **params)
            )
            if not payload:
                if strict:
                    raise BybitDataError("Bybit get_closed_pnl returned no payload")
                break
            result = payload.get("result", {})
            page_rows = result.get("list") if isinstance(result, Mapping) else None
            if not isinstance(page_rows, list) or any(not isinstance(row, Mapping) for row in page_rows):
                if strict:
                    raise BybitDataError("Bybit get_closed_pnl returned an invalid result list")
                break
            rows.extend(dict(row) for row in page_rows)
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return rows

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
        for _ in range(max(1, int(max_pages))):
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
                break
            result = payload.get("result", {})
            page_rows = result.get("list") if isinstance(result, Mapping) else None
            if not isinstance(page_rows, list) or any(not isinstance(row, Mapping) for row in page_rows):
                if strict:
                    raise BybitDataError("Bybit get_transaction_log returned an invalid result list")
                break
            rows.extend(dict(row) for row in page_rows)
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return rows

    def set_leverage(
        self, *, symbol: str, buy_leverage: float = 1.0, sell_leverage: float | None = None
    ) -> dict[str, Any]:
        if buy_leverage <= 0.0:
            raise ValueError("buy_leverage must be positive")
        effective_sell = buy_leverage if sell_leverage is None else sell_leverage
        if effective_sell <= 0.0:
            raise ValueError("sell_leverage must be positive")
        self._assert_submit_allowed("set_leverage")
        # Retry a transient set_leverage failure rather than silently dropping an
        # otherwise-valid entry. _call_once (not _call) keeps the original error
        # text -- which carries the "110043 not modified" marker -- intact, and a
        # 110043 reject returns immediately without wasting retries.
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
                message = str(exc).lower()
                if "110043" in message or "not modified" in message:
                    return {
                        "symbol": symbol,
                        "buyLeverage": _leverage_text(buy_leverage),
                        "sellLeverage": _leverage_text(effective_sell),
                        "retCode": 110043,
                    }
                last_error = exc
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
        payload = self._call_once("set_trading_stop", **params)
        return payload.get("result", {})

    def _call_optional(self, method_names: Iterable[str], **params: Any) -> dict[str, Any] | None:
        for method_name in method_names:
            if hasattr(self._client, method_name):
                return self._call(method_name, **params)
        return None

    def _call_once(self, method_name: str, **params: Any) -> dict[str, Any]:
        # Every current single-shot private call is state-changing. Keep the
        # capability check here as well as on the public method so a future
        # caller cannot bypass it by reaching for this transport helper.
        self._assert_submit_allowed(method_name)
        method = getattr(self._client, method_name)
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            payload = method(**params)
            ret_code = payload.get("retCode")
            if ret_code != 0:
                raise BybitRequestRejected(f"Bybit {method_name} failed: {payload}")
            return payload
        except BybitDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
            if type(exc).__name__ == "InvalidRequestError":
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
                    # Re-prove the lease before every retry, not just before a
                    # potentially long transport/backoff sequence begins.
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
                # A non-zero retCode that is not a rate-limit is a definite venue
                # reject -- retrying the identical request only repeats it and
                # wastes the backoff. Transport errors and rate-limits still retry.
                if isinstance(exc, BybitDataError) and not _is_rate_limit(exc):
                    raise
                # pybit (5.x) raises InvalidRequestError for a non-zero retCode BEFORE our
                # retCode check ever runs, so the branch above never classified live venue
                # rejects -- they were retried with backoff and the final raise dropped the
                # retCode/retMsg. Match by class name to avoid a hard pybit dependency here.
                if type(exc).__name__ == "InvalidRequestError" and not _is_rate_limit(exc):
                    raise BybitDataError(f"Bybit {method_name} failed: {exc}") from exc
                if attempt + 1 >= self.retries:
                    break
                time.sleep(self.retry_sleep_seconds * (2**attempt))
        # Keep the venue's last words in the surfaced message -- callers ledger
        # str(exc), and __cause__ does not survive into those error columns.
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
    """True for Bybit's duplicate-orderLinkId reject (retCode 110089).

    Bybit returns 110089 / "orderLinkID exists" when an order has already been
    accepted under the same idempotency key. Treated as idempotent success at
    the submit layer, not a failure. Matched by code AND message text so a
    re-worded retMsg still classifies."""
    text = str(value).lower()
    return "110089" in text or ("orderlinkid" in text and "exist" in text)


@dataclass(slots=True)
class BybitPrivateWebSocketStream:
    category: str = "linear"
    testnet: bool = False
    demo: bool = True
    api_key: str | None = None
    api_secret: str | None = None
    _client: Any = field(init=False, repr=False)
    _control_lock: Any = field(init=False, repr=False)
    _acked_topics: set[str] = field(init=False, repr=False)
    _last_control_error: str = field(init=False, repr=False)

    _EXPECTED_TOPICS = frozenset({"execution", "order"})

    def __post_init__(self) -> None:
        if not self.demo or self.testnet:
            raise RuntimeError(
                "BybitPrivateWebSocketStream is api-demo-only; testnet and mainnet private access are not part of the account cutover"
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
            # Every authenticated connection generation must prove its own
            # resubscription acknowledgements; old ACKs cannot cross a reconnect.
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
