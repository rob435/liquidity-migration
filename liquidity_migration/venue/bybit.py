"""Read-only Bybit account access for diagnostics and reconciliation."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from liquidity_migration.core.env_flags import env_flag, reject_ambiguous_flag
from liquidity_migration.core.venue_realm import (
    REALM_CREDENTIAL_VARIABLES,
    REALM_REST_ENDPOINTS,
    VenueRealm,
    venue_realm,
)
from liquidity_migration.marketdata.bybit_errors import BybitDataError
from liquidity_migration.marketdata.bybit_market_data import BybitRestRateLimiter as _BybitRestRateLimiter

try:
    from pybit.unified_trading import HTTP
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None


__all__ = [
    "BybitAccountReader",
    "BybitDataError",
    "resolve_demo_credentials",
    "resolve_private_credentials",
]

DEMO_REST_ENDPOINT = REALM_REST_ENDPOINTS[VenueRealm.DEMO]
_LOGGER = logging.getLogger("liquidity_migration.venue.bybit.account_reader")
_READ_METHODS = frozenset(
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


def _require_realm_endpoint(client: Any, *, realm: VenueRealm) -> None:
    if not type(client).__module__.startswith("pybit"):
        return
    expected = REALM_REST_ENDPOINTS[realm]
    endpoint = str(getattr(client, "endpoint", "") or "").rstrip("/")
    if endpoint != expected:
        raise RuntimeError(
            f"Bybit account reader selected {realm.value!r} but resolved to "
            f"{endpoint or 'an unknown host'}; expected {expected}"
        )


def resolve_private_credentials(*, realm: VenueRealm | str) -> tuple[str | None, str | None]:
    selected = venue_realm(realm)
    reject_ambiguous_flag("REAL_MONEY")
    reject_ambiguous_flag("DEMO")
    real_money = env_flag("REAL_MONEY")
    if selected is VenueRealm.DEMO and real_money:
        raise RuntimeError("Bybit demo access refuses to run with REAL_MONEY armed")
    if selected is VenueRealm.MAINNET and not real_money:
        raise RuntimeError("Bybit mainnet account reads require REAL_MONEY to be explicitly armed")
    key_name, secret_name = REALM_CREDENTIAL_VARIABLES[selected]
    _LOGGER.info("resolved read-only account realm: %s", selected.value)
    return os.environ.get(key_name), os.environ.get(secret_name)


def resolve_demo_credentials() -> tuple[str | None, str | None]:
    return resolve_private_credentials(realm=VenueRealm.DEMO)


def _execution_time_ns(row: Mapping[str, Any]) -> int:
    try:
        millis = float(row.get("execTime") or row.get("exec_time") or 0.0)
    except (TypeError, ValueError):
        return 0
    return int(millis * 1_000_000) if millis > 0.0 else 0


@dataclass(slots=True)
class BybitAccountReader:
    """Authenticated account reads with no order or position mutation API."""

    category: str = "linear"
    testnet: bool = False
    demo: bool = True
    realm: VenueRealm | str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    retries: int = 2
    retry_sleep_seconds: float = 0.5
    rate_limiter: _BybitRestRateLimiter | None = None
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.realm = VenueRealm.DEMO if self.realm is None else venue_realm(self.realm)
        if self.testnet:
            raise RuntimeError("BybitAccountReader does not support testnet")
        if bool(self.demo) is not (self.realm is VenueRealm.DEMO):
            raise RuntimeError(
                f"BybitAccountReader realm {self.realm.value!r} contradicts demo={self.demo!r}"
            )
        if self.realm is VenueRealm.MAINNET:
            reject_ambiguous_flag("REAL_MONEY")
            if not env_flag("REAL_MONEY"):
                raise RuntimeError("Bybit mainnet account reads require REAL_MONEY to be armed")
        if HTTP is None:
            raise RuntimeError("pybit is required for BybitAccountReader")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit account reads require API key and secret")
        self._client = HTTP(
            testnet=False,
            demo=self.demo,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        _require_realm_endpoint(self._client, realm=self.realm)

    def close(self) -> None:
        """Release the SDK session when its transport exposes a close hook."""

        for name in ("close", "exit"):
            close = getattr(self._client, name, None)
            if callable(close):
                close()
                return

    def _call(self, method_name: str, **params: Any) -> dict[str, Any]:
        if method_name not in _READ_METHODS:
            raise RuntimeError(f"BybitAccountReader refuses mutating method {method_name!r}")
        method = getattr(self._client, method_name, None)
        if not callable(method):
            raise BybitDataError(f"installed pybit has no {method_name}")
        last_error: Exception | None = None
        for attempt in range(max(1, int(self.retries))):
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire()
                payload = method(**params)
                if not isinstance(payload, Mapping) or payload.get("retCode") != 0:
                    raise BybitDataError(f"Bybit {method_name} failed: {payload}")
                return dict(payload)
            except Exception as exc:  # noqa: BLE001 - pybit raises transport-specific errors
                last_error = exc
                if isinstance(exc, BybitDataError) or attempt + 1 >= max(1, int(self.retries)):
                    break
                time.sleep(max(0.0, self.retry_sleep_seconds) * (2**attempt))
        raise BybitDataError(f"Bybit {method_name} failed after retries: {last_error}") from last_error

    def _call_optional(self, method_names: Sequence[str], **params: Any) -> dict[str, Any] | None:
        for name in method_names:
            if callable(getattr(self._client, name, None)):
                return self._call(name, **params)
        return None

    def _paged(
        self,
        method_names: Sequence[str],
        params: Mapping[str, Any],
        *,
        max_pages: int,
        optional: bool = False,
        stop_before_ns: int = 0,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        limit = max(1, int(max_pages))
        for _ in range(limit):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = (
                self._call_optional(method_names, **page_params)
                if optional
                else self._call(method_names[0], **page_params)
            )
            if payload is None:
                return rows
            result = payload.get("result")
            page = result.get("list") if isinstance(result, Mapping) else None
            if not isinstance(page, list) or any(not isinstance(row, Mapping) for row in page):
                raise BybitDataError(f"Bybit {method_names[0]} returned an invalid result list")
            rows.extend(dict(row) for row in page)
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if stop_before_ns > 0 and page:
                newest = max((_execution_time_ns(row) for row in page), default=0)
                if 0 < newest < stop_before_ns:
                    return rows
            if next_cursor in seen:
                raise BybitDataError(
                    f"Bybit {method_names[0]} returned a non-advancing pagination cursor"
                )
            seen.add(next_cursor)
            cursor = next_cursor
        raise BybitDataError(
            f"Bybit {method_names[0]} pagination exceeded max_pages={limit}; refusing an incomplete result"
        )

    def get_wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> dict[str, Any]:
        return self._call("get_wallet_balance", accountType=account_type, coin=coin).get("result", {})

    def get_api_key_information(self) -> dict[str, Any]:
        return self._call("get_api_key_information").get("result", {})

    def get_instruments_info(self, *, max_pages: int = 50) -> list[dict[str, Any]]:
        return self._paged(
            ("get_instruments_info",),
            {"category": self.category, "limit": 1000},
            max_pages=max_pages,
        )

    def get_tickers(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        result = self._call("get_tickers", **params).get("result", {})
        return list(result.get("list", [])) if isinstance(result, Mapping) else []

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
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(limit, 50))}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        for key, value in (
            ("orderId", order_id),
            ("orderLinkId", order_link_id),
            ("openOnly", open_only),
            ("orderFilter", order_filter),
        ):
            if value is not None and value != "":
                params[key] = value
        return self._paged(("get_open_orders",), params, max_pages=max_pages)

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
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(limit, 50))}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        for key, value in (
            ("orderId", order_id),
            ("orderLinkId", order_link_id),
            ("startTime", start_time_ms),
            ("endTime", end_time_ms),
        ):
            if value is not None and value != "":
                params[key] = value
        return self._paged(("get_order_history",), params, max_pages=max_pages, optional=True)

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
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(limit, 100))}
        for key, value in (("symbol", symbol), ("orderId", order_id), ("orderLinkId", order_link_id)):
            if value:
                params[key] = value
        return self._paged(
            ("get_executions", "get_trade_history"),
            params,
            max_pages=max_pages,
            optional=True,
            stop_before_ns=stop_before_ns,
        )

    def get_positions(
        self,
        *,
        symbol: str | None = None,
        settle_coin: str | None = None,
        limit: int = 200,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(limit, 200))}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        return self._paged(("get_positions",), params, max_pages=max_pages)

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
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(limit, 100))}
        for key, value in (("symbol", symbol), ("startTime", start_time_ms), ("endTime", end_time_ms)):
            if value is not None and value != "":
                params[key] = value
        return self._paged(("get_closed_pnl",), params, max_pages=max_pages, optional=not strict)

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
        kind = str(transaction_type).upper()
        if kind not in {"TRADE", "SETTLEMENT"}:
            raise ValueError("transaction_type must be TRADE or SETTLEMENT")
        start = int(start_time_ms)
        end = int(end_time_ms)
        if start <= 0 or end <= start or end - start > 7 * 24 * 60 * 60 * 1000:
            raise ValueError("transaction-log window must be positive, increasing, and at most seven days")
        params = {
            "accountType": "UNIFIED",
            "category": self.category,
            "type": kind,
            "limit": max(1, min(limit, 50)),
            "startTime": start,
            "endTime": end,
        }
        return self._paged(
            ("get_transaction_log",),
            params,
            max_pages=max_pages,
            optional=not strict,
        )
