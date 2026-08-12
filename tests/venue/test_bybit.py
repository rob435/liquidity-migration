from __future__ import annotations

import json
import logging
import sys
import threading
from types import SimpleNamespace

import pytest

from liquidity_migration.venue import bybit
from liquidity_migration.marketdata import bybit_errors
from liquidity_migration.marketdata import bybit_market_data
import liquidity_migration.account.account_owner_lease as owner_lease_module
from liquidity_migration.account.account_owner_lease import (
    DemoAccountIdentity,
    DemoAccountMutationLease,
)
from liquidity_migration.marketdata.bybit_market_data import BybitKlineStreamPool


@pytest.fixture
def held_demo_mutation_lease(tmp_path, monkeypatch):
    leases: list[DemoAccountMutationLease] = []
    monkeypatch.setattr(
        owner_lease_module,
        "canonical_demo_account_lease_path",
        lambda identity: tmp_path / f"bybit-{identity.environment}-{identity.user_id}.lock",
    )

    def acquire(api_key: str) -> DemoAccountMutationLease:
        identity = DemoAccountIdentity.from_api_key_info(
            api_key=api_key,
            api_key_info={
                "apiKey": api_key,
                "userID": 900_000 + len(leases),
            },
        )
        lease = DemoAccountMutationLease(identity)
        lease.acquire()
        leases.append(lease)
        return lease

    yield acquire
    for lease in reversed(leases):
        lease.close()


def test_pybit_rate_limit_log_filter_drops_10006_messages(caplog) -> None:
    # pybit logs the rate-limit retries at ERROR level on its _http_manager
    # logger; that produces tens of thousands of identical lines per minute on
    # the demo VPS at top-of-hour. liquidity_migration.venue.bybit installs a filter
    # on pybit._http_manager that drops only the 10006 lines.
    logger = logging.getLogger("pybit._http_manager")
    with caplog.at_level(logging.ERROR, logger="pybit._http_manager"):
        logger.error(
            "Too many visits. Exceeded the API Rate Limit. (ErrCode: 10006). "
            "Hit the API rate limit on https://api.bybit.com/v5/market/kline. "
            "Sleeping then trying again."
        )
        logger.error("API rate limit will reset at 13:00:09. Sleeping for 2000 ms. Retrying...")
        logger.error("connection reset by peer (ErrCode: 502). Retrying...")
    messages = [record.getMessage() for record in caplog.records]
    assert all("10006" not in m for m in messages), messages
    assert all("rate limit" not in m.lower() for m in messages), messages
    assert any("502" in m for m in messages), messages


def test_bybit_market_data_constructs_with_slotted_client(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)

    client = bybit_market_data.BybitMarketData(testnet=True)

    assert client._client.testnet is True


def test_bybit_market_data_can_select_public_demo_endpoint(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)

    client = bybit_market_data.BybitMarketData(demo=True)

    assert client._client.kwargs == {"testnet": False, "demo": True}


def test_bybit_market_data_rejects_demo_testnet_mix(monkeypatch) -> None:
    monkeypatch.setattr(bybit_market_data, "HTTP", object)

    with pytest.raises(ValueError, match="both testnet and demo"):
        bybit_market_data.BybitMarketData(testnet=True, demo=True)


def test_bybit_private_client_constructs_demo_session(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    assert client._client.kwargs["demo"] is True
    assert client._client.kwargs["api_key"] == "key"
    assert client._client.kwargs["api_secret"] == "secret"


def test_strict_accounting_queries_reject_malformed_payloads(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

        def get_transaction_log(self, **params):
            return {"retCode": 0, "result": {"list": None}}

        def get_closed_pnl(self, **params):
            return {"retCode": 0, "result": {"list": "not-a-list"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key", api_secret="secret", demo=True
    )

    with pytest.raises(bybit.BybitDataError, match="invalid result list"):
        client.get_account_transactions(
            transaction_type="TRADE",
            start_time_ms=1_000,
            end_time_ms=2_000,
            strict=True,
        )
    with pytest.raises(bybit.BybitDataError, match="invalid result list"):
        client.get_closed_pnl(
            start_time_ms=1_000,
            end_time_ms=2_000,
            strict=True,
        )


def test_get_closed_pnl_follows_pagination_cursor(monkeypatch) -> None:
    """A re-entered symbol's closures can exceed one 100-row page, so the orphan-close
    backfill must follow ``nextPageCursor``. Single-page behaviour is unchanged.
    """
    pages = {
        None: {"retCode": 0, "result": {"list": [{"orderId": "c1"}, {"orderId": "c2"}], "nextPageCursor": "p2"}},
        "p2": {"retCode": 0, "result": {"list": [{"orderId": "c3"}], "nextPageCursor": ""}},
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls: list[dict] = []

        def get_closed_pnl(self, **params):
            self.calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    rows = client.get_closed_pnl(symbol="FOOUSDT")

    assert [row["orderId"] for row in rows] == ["c1", "c2", "c3"]
    assert len(client._client.calls) == 2
    assert client._client.calls[0]["limit"] == 50
    assert client._client.calls[1]["cursor"] == "p2"

    client._client.calls.clear()
    client.get_closed_pnl(symbol="FOOUSDT", limit=200)
    assert client._client.calls[0]["limit"] == 100


def test_bybit_public_ticker_stream_subscribes_symbols(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.ticker_calls = []
            self.closed = False

        def ticker_stream(self, **params):
            self.ticker_calls.append(params)

        def exit(self):
            self.closed = True

    monkeypatch.setattr(bybit_market_data, "WebSocket", FakeWebSocket)

    client = bybit_market_data.BybitPublicTickerStream(testnet=True, demo=True)
    callback = object()
    client.subscribe_tickers(["BTCUSDT", "ETHUSDT"], callback)
    client.close()

    assert client._client.kwargs == {"testnet": True, "demo": True, "channel_type": "linear"}
    assert client._client.ticker_calls == [{"symbol": ["BTCUSDT", "ETHUSDT"], "callback": callback}]
    assert client._client.closed is True


def test_bybit_private_websocket_stream_subscribes_private_topics(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.auth = True
            self.connected = True
            self.subscriptions = {}
            self.control_messages = []

        def _subscribe(self, topic, call):
            req_id = f"req-{len(self.subscriptions) + 1}"
            self.subscriptions[req_id] = json.dumps(
                {"op": "subscribe", "req_id": req_id, "args": [topic]}
            )
            self.calls.append(call)

        def _process_subscription_message(self, message):
            self.control_messages.append(message)

        def _process_auth_message(self, message):
            self.auth = message.get("success") is True

        def acknowledge(self, topic, *, success=True, reason=""):
            req_id = next(
                req_id
                for req_id, raw in self.subscriptions.items()
                if topic in json.loads(raw)["args"]
            )
            self._process_subscription_message(
                {
                    "op": "subscribe",
                    "req_id": req_id,
                    "success": success,
                    "ret_msg": reason,
                }
            )

        def is_connected(self):
            return self.connected

        def order_stream(self, **params):
            self._subscribe("order", ("order", params))

        def execution_stream(self, **params):
            self._subscribe("execution", ("execution", params))

        def fast_execution_stream(self, **params):
            self._subscribe("execution.fast", ("fast_execution", params))

        def exit(self):
            self.connected = False

    monkeypatch.setattr(bybit, "WebSocket", FakeWebSocket)

    client = bybit.BybitPrivateWebSocketStream(api_key="key", api_secret="secret", demo=True)
    callback = object()
    client.subscribe_orders(callback)
    client.subscribe_executions(callback, fast=True)

    assert client.is_connected() is True
    assert client.is_ready() is False
    assert "positive subscription ACKs" in client.readiness_detail
    client._client.acknowledge("order")
    assert client.is_ready() is False
    client._client.acknowledge("execution.fast")
    assert client.is_ready() is True

    assert client._client.kwargs == {
        "testnet": False,
        "demo": True,
        "channel_type": "private",
        "api_key": "key",
        "api_secret": "secret",
    }
    assert client._client.calls == [
        ("order", {"callback": callback}),
        ("fast_execution", {"callback": callback}),
    ]

    # A new authentication generation must re-prove both subscription ACKs;
    # stale acknowledgements from the prior socket are not health evidence.
    client._client._process_auth_message({"op": "auth", "success": True})
    assert client.is_ready() is False


def test_private_websocket_negative_subscription_ack_stays_unhealthy(
    monkeypatch,
) -> None:
    class FakeWebSocket:
        def __init__(self, **_kwargs):
            self.auth = True
            self.connected = True
            self.subscriptions = {}

        def _process_subscription_message(self, _message):
            pass

        def _process_auth_message(self, message):
            self.auth = message.get("success") is True

        def _subscribe(self, topic):
            req_id = f"req-{topic}"
            self.subscriptions[req_id] = json.dumps(
                {"op": "subscribe", "req_id": req_id, "args": [topic]}
            )

        def order_stream(self, **_params):
            self._subscribe("order")

        def execution_stream(self, **_params):
            self._subscribe("execution")

        def is_connected(self):
            return self.connected

        def acknowledge(self, topic, *, success, reason=""):
            self._process_subscription_message(
                {
                    "op": "subscribe",
                    "req_id": f"req-{topic}",
                    "success": success,
                    "ret_msg": reason,
                }
            )

        def exit(self):
            self.connected = False

    monkeypatch.setattr(bybit, "WebSocket", FakeWebSocket)
    stream = bybit.BybitPrivateWebSocketStream(
        api_key="key",
        api_secret="secret",
        demo=True,
    )
    stream.subscribe_executions(object())
    stream.subscribe_orders(object())

    stream._client.acknowledge("execution", success=True)
    stream._client.acknowledge(
        "order",
        success=False,
        reason="permission denied",
    )

    assert stream.is_connected() is True
    assert stream.is_ready() is False
    assert "subscription rejected for order" in stream.readiness_detail
    assert "permission denied" in stream.readiness_detail


def test_bybit_pybit_ping_timer_patch_uses_daemon_timer(monkeypatch) -> None:
    class FakeManager:
        ping_interval = 1000

        def _send_custom_ping(self):
            pass

    monkeypatch.setitem(sys.modules, "pybit._websocket_stream", SimpleNamespace(_V5WebSocketManager=FakeManager))

    bybit._patch_pybit_daemon_ping_timer()
    manager = FakeManager()
    manager._send_initial_ping()

    assert manager._agc_ping_timer.daemon is True
    manager._agc_ping_timer.cancel()


def test_resolve_demo_credentials_has_no_mainnet_branch(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "real-k")
    monkeypatch.setenv("BYBIT_REAL_API_SECRET", "real-s")

    # No toggle set -> demo is the default.
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    assert bybit.resolve_demo_credentials() == ("demo-k", "demo-s")

    # DEMO=true -> demo account.
    monkeypatch.setenv("DEMO", "true")
    assert bybit.resolve_demo_credentials() == ("demo-k", "demo-s")

    # A stale real-money selection fails closed; real credentials are never read.
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("REAL_MONEY", "true")
    with pytest.raises(RuntimeError, match="refuses to run with REAL_MONEY armed"):
        bybit.resolve_demo_credentials()


def test_private_credentials_require_an_explicitly_named_realm(monkeypatch) -> None:
    """The realm is an argument with no default; mainnet is never a fallback."""

    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "real-k")
    monkeypatch.setenv("BYBIT_REAL_API_SECRET", "real-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("REAL_MONEY", raising=False)

    with pytest.raises(TypeError):
        bybit.resolve_private_credentials()  # type: ignore[call-arg]
    for bogus in ("", None, "live", "real", "prod", "paper"):
        with pytest.raises(ValueError, match="explicitly set to 'demo' or 'mainnet'"):
            bybit.resolve_private_credentials(realm=bogus)

    assert bybit.resolve_private_credentials(realm="demo") == ("demo-k", "demo-s")
    # Naming mainnet is not on its own authorization.
    with pytest.raises(RuntimeError, match="require REAL_MONEY to be explicitly armed"):
        bybit.resolve_private_credentials(realm="mainnet")

    monkeypatch.setenv("REAL_MONEY", "true")
    assert bybit.resolve_private_credentials(realm="mainnet") == ("real-k", "real-s")
    # The two realms read different variables, so a stale demo key in the
    # environment can never authenticate a mainnet run.
    monkeypatch.delenv("BYBIT_REAL_API_KEY", raising=False)
    assert bybit.resolve_private_credentials(realm="mainnet") == (None, "real-s")


def test_ambiguous_real_money_still_fails_startup_in_both_realms(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.setenv("REAL_MONEY", "yeah-ok")
    for realm in ("demo", "mainnet"):
        with pytest.raises(RuntimeError, match="not a recognised boolean"):
            bybit.resolve_private_credentials(realm=realm)


@pytest.mark.parametrize(
    ("testnet", "demo", "message"),
    [
        (False, False, "contradicts demo=False"),
        (True, True, "does not support testnet"),
        (True, False, "does not support testnet"),
    ],
)
def test_bybit_private_client_rejects_transport_flags_that_contradict_the_realm(
    monkeypatch,
    testnet: bool,
    demo: bool,
    message: str,
) -> None:
    """Flipping ``demo`` can never reach mainnet; only naming the realm can."""

    constructed: dict = {}

    class FakeHTTP:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match=message):
        bybit.BybitPrivateClient(
            api_key="k",
            api_secret="s",
            testnet=testnet,
            demo=demo,
        )
    assert constructed == {}


def test_bybit_private_client_defaults_to_demo_and_never_to_mainnet(monkeypatch) -> None:
    constructed: dict = {}

    class FakeHTTP:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s")

    assert client.realm is bybit.VenueRealm.DEMO
    assert constructed["demo"] is True


def test_mainnet_client_requires_real_money_to_be_armed(monkeypatch) -> None:
    constructed: dict = {}

    class FakeHTTP:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match="while REAL_MONEY is unset or false"):
        bybit.BybitPrivateClient(
            api_key="k", api_secret="s", demo=False, realm="mainnet"
        )
    assert constructed == {}

    monkeypatch.setenv("REAL_MONEY", "true")
    client = bybit.BybitPrivateClient(
        api_key="k", api_secret="s", demo=False, realm="mainnet"
    )
    assert client.realm is bybit.VenueRealm.MAINNET
    assert constructed["demo"] is False


def test_endpoint_assertion_is_symmetric_across_realms(monkeypatch) -> None:
    """The post-construction check asserts whichever realm was selected."""

    class FakePybitHTTP:
        __module__ = "pybit.unified_trading"

        def __init__(self, **kwargs):
            self.endpoint = "https://api.bybit.com" if kwargs["demo"] else "https://api-demo.bybit.com"

    monkeypatch.setattr(bybit, "HTTP", FakePybitHTTP)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    # A transport that silently resolves demo to the funded host is refused...
    with pytest.raises(RuntimeError, match="selected realm 'demo' but resolved to"):
        bybit.BybitPrivateClient(api_key="k", api_secret="s")
    # ...and so is the mirror image, which is the case that costs money.
    monkeypatch.setenv("REAL_MONEY", "true")
    with pytest.raises(RuntimeError, match="selected realm 'mainnet' but resolved to"):
        bybit.BybitPrivateClient(
            api_key="k", api_secret="s", demo=False, realm="mainnet"
        )


def test_bybit_private_websocket_rejects_testnet_demo_mix(monkeypatch) -> None:
    constructed: dict = {}

    class FakeWebSocket:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr(bybit, "WebSocket", FakeWebSocket)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match="does not support testnet"):
        bybit.BybitPrivateWebSocketStream(
            api_key="k",
            api_secret="s",
            testnet=True,
            demo=True,
        )
    with pytest.raises(RuntimeError, match="contradicts demo=False"):
        bybit.BybitPrivateWebSocketStream(api_key="k", api_secret="s", demo=False)
    with pytest.raises(RuntimeError, match="while REAL_MONEY is unset or false"):
        bybit.BybitPrivateWebSocketStream(
            api_key="k", api_secret="s", demo=False, realm="mainnet"
        )
    assert constructed == {}


def test_bybit_private_client_wraps_order_and_trade_history(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.order_history_calls = []
            self.execution_calls = []

        def get_order_history(self, **params):
            self.order_history_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"orderLinkId": params["orderLinkId"], "orderStatus": "Filled"}]}}

        def get_executions(self, **params):
            self.execution_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"orderLinkId": params["orderLinkId"], "execQty": "1"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    orders = client.get_order_history(symbol="BTCUSDT", order_link_id="lm-link")
    trades = client.get_trade_history(symbol="BTCUSDT", order_link_id="lm-link")

    assert orders[0]["orderStatus"] == "Filled"
    assert trades[0]["execQty"] == "1"
    assert client._client.order_history_calls == [
        {"category": "linear", "limit": 50, "symbol": "BTCUSDT", "orderLinkId": "lm-link"}
    ]
    assert client._client.execution_calls == [
        {"category": "linear", "limit": 50, "symbol": "BTCUSDT", "orderLinkId": "lm-link"}
    ]


def test_bybit_private_client_pages_account_order_history(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **_kwargs):
            self.calls: list[dict] = []

        def get_order_history(self, **params):
            self.calls.append(params)
            if "cursor" not in params:
                return {
                    "retCode": 0,
                    "result": {
                        "list": [{"orderId": "close-1"}],
                        "nextPageCursor": "next",
                    },
                }
            return {
                "retCode": 0,
                "result": {"list": [{"orderId": "close-2"}], "nextPageCursor": ""},
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    rows = client.get_order_history(
        settle_coin="USDT",
        start_time_ms=1_700_000_000_000,
        end_time_ms=1_700_000_100_000,
    )

    assert [row["orderId"] for row in rows] == ["close-1", "close-2"]
    assert client._client.calls[0] == {
        "category": "linear",
        "limit": 50,
        "settleCoin": "USDT",
        "startTime": 1_700_000_000_000,
        "endTime": 1_700_000_100_000,
    }
    assert client._client.calls[1]["cursor"] == "next"


def test_bybit_private_client_wraps_positions_by_settle(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.position_calls = []

        def get_positions(self, **params):
            self.position_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "1"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    positions = client.get_positions(settle_coin="USDT")

    assert positions[0]["symbol"] == "BTCUSDT"
    assert client._client.position_calls == [{"category": "linear", "limit": 200, "settleCoin": "USDT"}]


def test_bybit_private_client_wraps_open_orders_by_settle(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "orderStatus": "New"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    orders = client.get_open_orders()

    assert orders[0]["orderStatus"] == "New"
    assert client._client.open_order_calls == [{"category": "linear", "limit": 50, "settleCoin": "USDT"}]


def test_bybit_private_client_forwards_explicit_open_order_filter(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return {"retCode": 0, "result": {"list": []}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
    )

    assert client.get_open_orders(order_filter="StopOrder") == []
    assert client._client.open_order_calls == [{
        "category": "linear",
        "limit": 50,
        "settleCoin": "USDT",
        "orderFilter": "StopOrder",
    }]


def test_bybit_private_client_forwards_exact_recent_closed_order_query(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return {"retCode": 0, "result": {"list": []}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
    )

    assert client.get_open_orders(
        symbol="0GUSDT",
        settle_coin=None,
        order_id="order-1",
        order_link_id="probe-1",
        open_only=1,
        max_pages=2,
    ) == []
    assert client._client.open_order_calls == [{
        "category": "linear",
        "limit": 50,
        "symbol": "0GUSDT",
        "orderId": "order-1",
        "orderLinkId": "probe-1",
        "openOnly": 1,
    }]


@pytest.mark.parametrize("open_only", [3, 1.5, True, "1"])
def test_bybit_private_client_rejects_invalid_open_only(monkeypatch, open_only) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    with pytest.raises(ValueError, match="open_only"):
        client.get_open_orders(open_only=open_only)


def test_bybit_private_client_paginates_open_orders(monkeypatch) -> None:
    pages = {
        None: {"retCode": 0, "result": {"list": [{"orderId": "o1"}], "nextPageCursor": "p2"}},
        "p2": {"retCode": 0, "result": {"list": [{"orderId": "o2"}], "nextPageCursor": ""}},
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    orders = client.get_open_orders(settle_coin="USDT")

    assert [row["orderId"] for row in orders] == ["o1", "o2"]
    assert client._client.open_order_calls == [
        {"category": "linear", "limit": 50, "settleCoin": "USDT"},
        {"category": "linear", "limit": 50, "settleCoin": "USDT", "cursor": "p2"},
    ]


def test_bybit_private_client_rejects_truncated_open_order_pagination(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return {
                "retCode": 0,
                "result": {
                    "list": [{"orderId": f"o{len(self.open_order_calls)}"}],
                    "nextPageCursor": f"p{len(self.open_order_calls) + 1}",
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    with pytest.raises(bybit.BybitDataError, match="refusing an incomplete result"):
        client.get_open_orders(settle_coin="USDT", max_pages=2)

    assert len(client._client.open_order_calls) == 2


def test_bybit_private_client_rejects_non_advancing_open_order_cursor(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return {
                "retCode": 0,
                "result": {
                    "list": [{"orderId": "o1"}],
                    "nextPageCursor": "stuck",
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    with pytest.raises(bybit.BybitDataError, match="non-advancing pagination cursor"):
        client.get_open_orders(settle_coin="USDT")

    assert len(client._client.open_order_calls) == 2


def test_bybit_private_client_paginates_positions(monkeypatch) -> None:
    pages = {
        None: {"retCode": 0, "result": {"list": [{"symbol": "AAAUSDT"}], "nextPageCursor": "p2"}},
        "p2": {"retCode": 0, "result": {"list": [{"symbol": "BBBUSDT"}], "nextPageCursor": ""}},
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.position_calls = []

        def get_positions(self, **params):
            self.position_calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    positions = client.get_positions(settle_coin="USDT")

    assert [row["symbol"] for row in positions] == ["AAAUSDT", "BBBUSDT"]
    assert client._client.position_calls == [
        {"category": "linear", "limit": 200, "settleCoin": "USDT"},
        {"category": "linear", "limit": 200, "settleCoin": "USDT", "cursor": "p2"},
    ]


def test_bybit_private_client_sets_demo_leverage(monkeypatch, held_demo_mutation_lease) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.leverage_calls = []

        def set_leverage(self, **params):
            self.leverage_calls.append(params)
            return {"retCode": 0, "result": {"symbol": params["symbol"]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    result = client.set_leverage(symbol="BTCUSDT", buy_leverage=1.0, sell_leverage=1.0)

    assert result == {"symbol": "BTCUSDT"}
    assert client._client.leverage_calls == [
        {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1"}
    ]


def test_bybit_private_client_treats_existing_leverage_as_success(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.leverage_calls = []

        def set_leverage(self, **params):
            self.leverage_calls.append(params)
            return {"retCode": 110043, "retMsg": "leverage not modified", "result": {}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    result = client.set_leverage(symbol="BTCUSDT", buy_leverage=1.0, sell_leverage=1.0)

    assert result == {"symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1", "retCode": 110043}


def test_bybit_private_client_treats_pybit_existing_leverage_exception_as_success(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_leverage(self, **params):
            del params
            raise RuntimeError("110043: leverage not modified")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    result = client.set_leverage(symbol="BTCUSDT", buy_leverage=1.0, sell_leverage=1.0)

    assert result == {"symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1", "retCode": 110043}


def test_a_refused_stop_whose_price_contains_the_no_op_code_still_raises(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """34040 means "already installed". A price containing 34040 does not.

    The rendered refusal ends with the whole request body, stop price included,
    so a bare digit scan read every refusal of a stop at 134040 as a converged
    no-op. The caller journals that as ``status="active"``, clears the breach
    latch and blanks ``last_error`` — recording a naked position as protected,
    and the refusal that matters most is "the stop already crossed the mark".
    """

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_trading_stop(self, **params):
            return {
                "retCode": 34036,
                "retMsg": "the stop loss price is invalid",
                "result": {},
                "request": {"symbol": params["symbol"], "stopLoss": "134040"},
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )

    with pytest.raises(bybit_errors.BybitDataError):
        client.set_trading_stop(symbol="BTCUSDT", stop_loss="134040")


def test_a_genuine_not_modified_stop_is_still_a_converged_no_op(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_trading_stop(self, **params):
            del params
            raise RuntimeError("34040: not modified")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )

    assert client.set_trading_stop(symbol="BTCUSDT", stop_loss="0.5") == {
        "symbol": "BTCUSDT",
        "retCode": 34040,
    }


def test_bybit_private_client_wraps_trading_stop(monkeypatch, held_demo_mutation_lease) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.trading_stop_calls = []

        def set_trading_stop(self, **params):
            self.trading_stop_calls.append(params)
            return {"retCode": 0, "result": {"ok": True}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    result = client.set_trading_stop(
        symbol="BTCUSDT",
        stop_loss="120",
        take_profit="80",
        trailing_stop="2.5",
        active_price="95",
    )

    assert result == {"ok": True}
    assert client._client.trading_stop_calls == [
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "tpslMode": "Full",
            "positionIdx": 0,
            "stopLoss": "120",
            "takeProfit": "80",
            "trailingStop": "2.5",
            "activePrice": "95",
            "tpTriggerBy": "MarkPrice",
            "slTriggerBy": "MarkPrice",
        }
    ]


def test_kline_download_chunks_full_range_when_bybit_returns_newest_first(monkeypatch) -> None:
    interval_ms = bybit_market_data.INTERVAL_MS["60"]
    timestamps = [index * interval_ms for index in range(10)]

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = []

        def get_kline(self, **params):
            self.calls.append(params)
            start = int(params["start"])
            end = int(params["end"])
            limit = int(params["limit"])
            rows = [[str(ts), "1", "2", "0.5", "1.5", "10", "15"] for ts in timestamps if start <= ts <= end]
            return {"retCode": 0, "result": {"list": list(reversed(rows))[:limit]}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)

    client = bybit_market_data.BybitMarketData()
    # `end` is exclusive: request one interval past the last wanted bar.
    rows = client.get_klines("BTCUSDT", "60", timestamps[0], timestamps[-1] + interval_ms, limit=3)

    assert [int(row[0]) for row in rows] == timestamps
    assert len(client._client.calls) > 1
    assert max(int(call["end"]) - int(call["start"]) for call in client._client.calls) <= interval_ms * 2

    # The excluded bound is genuinely excluded: the 00:00 bar of the end day
    # must not be written into a date=<end> partition.
    exclusive = bybit_market_data.BybitMarketData()
    kept = exclusive.get_klines("BTCUSDT", "60", timestamps[0], timestamps[-1], limit=3)
    assert [int(row[0]) for row in kept] == timestamps[:-1]
    assert all(int(call["end"]) < timestamps[-1] for call in exclusive._client.calls)


def test_bybit_market_data_records_retry_and_rate_limit_stats(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = 0

        def get_tickers(self, **params):
            del params
            self.calls += 1
            if self.calls == 1:
                return {"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit."}
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)

    client = bybit_market_data.BybitMarketData(retry_sleep_seconds=0.0)
    rows = client.get_tickers()
    stats = client.stats()

    assert rows == [{"symbol": "BTCUSDT"}]
    assert stats["logical_calls"] == 1
    assert stats["http_calls"] == 2
    assert stats["retry_events"] == 1
    assert stats["rate_limit_events"] == 1
    assert stats["error_events"] == 1
    assert stats["backoff_events"] == 2
    assert "10006" in stats["last_error"]


def test_time_range_download_pages_backward_when_bybit_returns_newest_first(monkeypatch) -> None:
    timestamps = [index * 1000 for index in range(10)]

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.funding_calls = []
            self.oi_calls = []

        def get_funding_rate_history(self, **params):
            self.funding_calls.append(params)
            return _newest_first_page(timestamps, params, "fundingRateTimestamp", limit_key="limit")

        def get_open_interest(self, **params):
            self.oi_calls.append(params)
            return _newest_first_page(timestamps, params, "timestamp", limit_key="limit")

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)

    client = bybit_market_data.BybitMarketData()
    funding = client.get_funding_history("BTCUSDT", timestamps[0], timestamps[-1] + 1000, limit=3)
    oi = client.get_open_interest("BTCUSDT", "1h", timestamps[0], timestamps[-1] + 1000, limit=3)

    assert [int(row["fundingRateTimestamp"]) for row in funding] == timestamps
    assert [int(row["timestamp"]) for row in oi] == timestamps
    assert len(client._client.funding_calls) > 1
    assert len(client._client.oi_calls) > 1


def _newest_first_page(timestamps: list[int], params: dict, timestamp_key: str, *, limit_key: str) -> dict:
    start = int(params["startTime"])
    end = int(params["endTime"])
    limit = int(params[limit_key])
    rows = [
        {timestamp_key: str(ts), "fundingRate": "0.0001", "openInterest": "100"}
        for ts in timestamps
        if start <= ts <= end
    ]
    return {"retCode": 0, "result": {"list": list(reversed(rows))[:limit]}}


def test_bybit_rest_rate_limiter_throttles_within_window() -> None:
    import time as _time

    limiter = bybit_market_data.BybitRestRateLimiter(max_requests=3, per_seconds=0.2)
    started = _time.monotonic()
    for _ in range(6):
        limiter.acquire()
    elapsed = _time.monotonic() - started
    # 6 acquires at 3 per 0.2s must take at least one full window beyond the
    # first 3 immediate acquires; anything below 0.18s means the limiter is
    # silently letting bursts through.
    assert elapsed >= 0.18, f"limiter let burst through in {elapsed:.3f}s"
    stats = limiter.stats()
    assert stats["throttle_events"] >= 1
    assert stats["throttled_seconds"] > 0.0


def test_bybit_rest_rate_limiter_no_throttle_under_budget() -> None:
    limiter = bybit_market_data.BybitRestRateLimiter(max_requests=10, per_seconds=1.0)
    for _ in range(5):
        limiter.acquire()
    assert limiter.stats()["throttle_events"] == 0


def test_bybit_market_data_routes_get_through_rate_limiter(monkeypatch) -> None:
    """``BybitMarketData`` must call ``rate_limiter.acquire()`` before each pybit HTTP
    call -- the only way concurrent kline workers stay under Bybit's public REST
    budget.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_tickers(self, **_kwargs):
            self.calls += 1
            return {"retCode": 0, "result": {"list": []}}

    class RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self) -> None:
            self.acquires += 1

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    limiter = RecordingLimiter()
    client = bybit_market_data.BybitMarketData(rate_limiter=limiter)  # type: ignore[arg-type]

    client.get_tickers()
    client.get_tickers()

    assert limiter.acquires == 2
    assert client._client.calls == 2


def test_bybit_private_client_routes_call_through_rate_limiter(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """``BybitPrivateClient`` must acquire the shared rate limiter before every pybit
    HTTP call on BOTH the ``_call`` and ``_call_once`` paths.
    """

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = 0

        def place_order(self, **_kwargs):
            self.calls += 1
            return {"retCode": 0, "result": {"orderId": "x"}}

        def get_wallet_balance(self, **_kwargs):
            self.calls += 1
            return {"retCode": 0, "result": {"list": []}}

    class RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self) -> None:
            self.acquires += 1

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    limiter = RecordingLimiter()
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        mutation_lease=held_demo_mutation_lease("k"),
        rate_limiter=limiter,  # type: ignore[arg-type]
    )

    # place_order goes through _call_once (single-shot, no retries).
    client.place_order(symbol="AAAUSDT", side="Buy", qty="1", orderType="Market", orderLinkId="lm-test-1")
    # get_wallet_balance goes through _call (retry-capable).
    client.get_wallet_balance()

    assert limiter.acquires == 2
    assert client._client.calls == 2


def test_bybit_private_client_rate_limiter_acquires_each_retry(monkeypatch) -> None:
    """Every retry attempt must hit the limiter, or a tight retry burst escapes the budget."""
    attempt_counter = {"n": 0}

    class FlakyHTTP:
        def __init__(self, **kwargs):
            pass

        def get_wallet_balance(self, **_kwargs):
            attempt_counter["n"] += 1
            if attempt_counter["n"] < 2:
                return {"retCode": 10006, "retMsg": "rate limit"}
            return {"retCode": 0, "result": {"list": []}}

    class RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self) -> None:
            self.acquires += 1

    monkeypatch.setattr(bybit, "HTTP", FlakyHTTP)
    limiter = RecordingLimiter()
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        retries=3,
        retry_sleep_seconds=0.0,
        rate_limiter=limiter,  # type: ignore[arg-type]
    )

    client.get_wallet_balance()
    assert attempt_counter["n"] == 2
    assert limiter.acquires == 2


def test_api_key_allows_order_submit_rejects_read_only_key() -> None:
    allowed, reason = bybit.api_key_allows_order_submit(
        {
            "readOnly": 1,
            "permissions": {"ContractTrade": ["Order", "Position"]},
        }
    )
    assert allowed is False
    assert "readOnly=1" in reason


def test_api_key_allows_order_submit_requires_contract_trade_permissions() -> None:
    allowed, reason = bybit.api_key_allows_order_submit(
        {
            "readOnly": 0,
            "permissions": {"ContractTrade": ["Order"]},
        }
    )
    assert allowed is False
    assert "Position" in reason


def test_api_key_allows_order_submit_rejects_malformed_metadata() -> None:
    allowed, reason = bybit.api_key_allows_order_submit({"readOnly": 0})
    assert allowed is False
    assert "missing permissions" in reason

    allowed, reason = bybit.api_key_allows_order_submit({"permissions": {"ContractTrade": ["Order", "Position"]}})
    assert allowed is False
    assert "missing readOnly" in reason


def test_private_client_get_api_key_information(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_api_key_information(self):
            return {
                "retCode": 0,
                "result": {
                    "readOnly": 0,
                    "permissions": {"ContractTrade": ["Order", "Position"]},
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    info = client.get_api_key_information()
    assert info["readOnly"] == 0
    assert bybit.api_key_allows_order_submit(info) == (True, "")


def test_bybit_market_data_does_not_retry_definite_reject(monkeypatch) -> None:
    """EXC-3: a definite (non-rate-limit) venue reject must raise IMMEDIATELY, not burn
    the full retry budget + exponential backoff re-issuing the identical failing call."""
    import pytest as _pytest

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = 0

        def get_tickers(self, **params):
            del params
            self.calls += 1
            return {"retCode": 10001, "retMsg": "params error: invalid symbol"}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    client = bybit_market_data.BybitMarketData(retry_sleep_seconds=0.0, retries=3)
    with _pytest.raises(bybit.BybitDataError):
        client.get_tickers()
    stats = client.stats()
    assert stats["http_calls"] == 1, "a definite reject must NOT be retried"
    assert stats["retry_events"] == 0


def test_bybit_market_data_still_retries_rate_limit(monkeypatch) -> None:
    """EXC-3 guard: a rate-limit reject must STILL retry (only definite rejects short-circuit)."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = 0

        def get_tickers(self, **params):
            del params
            self.calls += 1
            if self.calls == 1:
                return {"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit."}
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    client = bybit_market_data.BybitMarketData(retry_sleep_seconds=0.0, retries=3)
    assert client.get_tickers() == [{"symbol": "BTCUSDT"}]
    assert client.stats()["http_calls"] == 2
    assert client.stats()["retry_events"] == 1


def test_private_call_definite_reject_no_retry_and_message_preserved(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """pybit 5.x raises ``InvalidRequestError`` for a non-zero retCode before the
    wrapper's own retCode check, so definite venue rejects would be retried with
    backoff and the final raise would drop retCode/retMsg. Rejects must raise
    immediately WITH the venue message.
    """
    InvalidRequestError = type("InvalidRequestError", (Exception,), {})  # pybit shape, no hard dep

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.cancel_calls = 0

        def cancel_order(self, **params):
            self.cancel_calls += 1
            raise InvalidRequestError("order not exists or too late to cancel (ErrCode: 110001)")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    client.retry_sleep_seconds = 0.0
    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.cancel_order(symbol="BTCUSDT", order_link_id="lm-x")
    assert client._client.cancel_calls == 1  # definite reject: NO retry
    assert "110001" in str(excinfo.value)  # venue message survives into str(exc)


def test_private_call_transport_retries_and_final_message_carries_cause(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """Transport errors still retry; the exhausted-retries raise now carries the last
    error's text (callers ledger f"{exc}" — __cause__ never reached those columns)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls = 0

        def cancel_order(self, **params):
            self.calls += 1
            raise ConnectionError("connection reset by venue")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    client.retry_sleep_seconds = 0.0
    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.cancel_order(symbol="BTCUSDT", order_link_id="lm-x")
    assert client._client.calls == client.retries  # transport: retried to exhaustion
    assert "connection reset by venue" in str(excinfo.value)


# ---------------------------------------------------------------------------
# is_rate_limit must classify on retCode/retMsg, not the whole payload.
# ---------------------------------------------------------------------------


def test_is_rate_limit_ignores_orderid_substring() -> None:
    # orderId contains "10006" but retCode is a definite reject -> NOT a rate limit.
    assert bybit_errors.is_rate_limit({"retCode": 110001, "result": {"orderId": "a10006b"}}) is False


def test_is_rate_limit_true_on_retcode_10006() -> None:
    assert bybit_errors.is_rate_limit({"retCode": 10006, "retMsg": "Too many visits"}) is True


def test_is_rate_limit_non_throttle_retmsg_not_matched_via_payload() -> None:
    # A risk/leverage reject whose retMsg legitimately contains "rate limit" must not
    # be misclassified when the code is a definite reject... but the retMsg text itself
    # is the venue's throttle signal, so a retMsg-level match is still honoured:
    assert bybit_errors.is_rate_limit({"retCode": 110043, "retMsg": "set leverage not modified"}) is False


def test_is_rate_limit_string_fallback() -> None:
    assert bybit_errors.is_rate_limit("rate limit exceeded") is True
    assert bybit_errors.is_rate_limit("position closed") is False




# --------------------------------------------------------------------------
# BybitPrivateClient.place_order idempotency
# --------------------------------------------------------------------------


def _make_private_client(
    monkeypatch,
    fake_http_cls,
    held_demo_mutation_lease,
) -> bybit.BybitPrivateClient:
    monkeypatch.setattr(bybit, "HTTP", fake_http_cls)
    return bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        mutation_lease=held_demo_mutation_lease("k"),
    )


def test_place_order_duplicate_link_returns_existing_open_order(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """A 110089 duplicate-orderLinkId reject must not raise: the order is already at
    Bybit under this idempotency key, so ``place_order`` probes by orderLinkId and
    returns the existing order rather than leaving an orphan position unledgered.
    """

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {
                "retCode": 0,
                "result": {
                    "list": [{"orderId": "live-1", "orderLinkId": "agc-1", "orderStatus": "New"}],
                    "nextPageCursor": "",
                },
            }

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    result = client.place_order(
        symbol="BTCUSDT",
        side="Buy",
        orderType="Market",
        qty="1",
        orderLinkId="agc-1",
    )
    assert result["orderId"] == "live-1"
    assert result["orderLinkId"] == "agc-1"
    assert result["_idempotent_existing_order"] is True


def test_place_order_preserves_v5_response_envelope_time(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {
                "retCode": 0,
                "result": {"orderId": "venue-1", "orderLinkId": "ack-time-1"},
                "time": 1_700_000_000_123,
            }

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    result = client.place_order(
        symbol="BTCUSDT",
        side="Buy",
        orderType="Market",
        qty="1",
        orderLinkId="ack-time-1",
    )

    assert result["orderId"] == "venue-1"
    assert result["_response_time_ms"] == 1_700_000_000_123


def test_place_order_duplicate_link_raises_when_order_not_findable(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """If Bybit reports a duplicate but the order is on neither open-orders nor
    history, surface the original reject -- a phantom success would be worse.
    """

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {"retCode": 0, "result": {"list": []}}

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    with pytest.raises(bybit.BybitSubmissionUncertain):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="agc-x",
        )


def test_place_order_non_duplicate_reject_still_raises(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """Only 110089 is treated as idempotent success; any other non-zero retCode must
    still raise so genuine rejects are not masked.
    """

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110007, "retMsg": "insufficient balance", "result": {}}

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    with pytest.raises(bybit.BybitRequestRejected, match="110007"):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="agc-y",
        )


def test_place_order_transport_failure_is_outcome_unknown(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            raise TimeoutError("response lost after socket write")

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    with pytest.raises(bybit.BybitSubmissionUncertain, match="outcome is unknown"):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="agc-timeout",
        )


def test_place_order_duplicate_link_uses_history_only_for_active_status(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """A Rejected/Cancelled history row does not count as present (the submit did not
    take), so the dup-link path must raise rather than return a dead order.
    """

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {
                "retCode": 0,
                "result": {"list": [{"orderId": "dead", "orderLinkId": "agc-z", "orderStatus": "Rejected"}]},
            }

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="agc-z",
        )


def test_place_order_duplicate_link_ignores_wrong_history_link(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """A history row is usable only when its orderLinkId matches the requested link."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {
                "retCode": 0,
                "result": {"list": [{"orderId": "other", "orderLinkId": "agc-other", "orderStatus": "Filled"}]},
            }

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="agc-z",
        )


def test_is_duplicate_order_link_matches_documented_code_and_both_wordings() -> None:
    """Classify by the documented 110072 code and by either duplicate wording.

    A bare 110089 must NOT classify: the official error table lists it as
    "Exceeds the maximum risk limit level", and treating a risk-limit refusal
    as a maybe-duplicate converted a definite reject into an uncertain
    outcome that wedged the command."""
    assert bybit._is_duplicate_order_link(
        "Bybit place_order failed: {'retCode': 110072, 'retMsg': 'OrderLinkedID is duplicate'}"
    )
    assert bybit._is_duplicate_order_link("Bybit place_order failed: {'retCode': 110072}")
    assert bybit._is_duplicate_order_link("orderLinkID exists, duplicate")
    assert bybit._is_duplicate_order_link("OrderLinkedID is duplicate")
    assert not bybit._is_duplicate_order_link(
        "Bybit place_order failed: {'retCode': 110089, 'retMsg': 'Exceeds the maximum risk limit level'}"
    )
    assert not bybit._is_duplicate_order_link("retCode 110007 insufficient balance")
    # Echoed request bodies must never classify: a pybit reject ends with the
    # whole order body, so the digits can be a BTC stop price and
    # 'orderLinkId' appears in every reject text.
    assert not bybit._is_duplicate_order_link(
        "Bybit place_order failed: (ErrCode: 110007) insufficient balance. "
        "Request → POST /v5/order/create: {'stopLoss': '110072.5', 'orderLinkId': 'abc'}"
    )
    assert not bybit._is_duplicate_order_link(
        "Bybit place_order failed: (ErrCode: 110007) no position exists to reduce. "
        "Request → POST /v5/order/create: {'orderLinkId': 'abc'}"
    )
    assert bybit._is_duplicate_order_link(
        "Bybit place_order failed: (ErrCode: 110072) OrderLinkedID is duplicate. "
        "Request → POST /v5/order/create: {'orderLinkId': 'abc'}"
    )
    # Batch rows classify by their own code, as a payload, not by digits.
    assert bybit._is_duplicate_order_link({"retCode": 110072, "retMsg": ""})
    assert not bybit._is_duplicate_order_link({"retCode": 110007, "retMsg": "insufficient"})


def test_place_orders_batch_parses_rows_and_refuses_unmapped_responses(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """Per-row outcomes come from retExtInfo.list, index-aligned with the
    request; a response that cannot be mapped row-for-row is ambiguous for
    every order and must refuse rather than guess."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.batch_bodies: list[dict] = []

        def place_batch_order(self, **kwargs):
            self.batch_bodies.append(kwargs)
            request = kwargs["request"]
            return {
                "retCode": 0,
                "retMsg": "OK",
                "time": 1_700_000_000_000,
                "result": {
                    "list": [
                        {"orderId": f"venue-{index}", "orderLinkId": row["orderLinkId"]}
                        for index, row in enumerate(request)
                    ]
                },
                "retExtInfo": {
                    "list": [
                        {"code": 0, "msg": "OK"}
                        if index != 1
                        else {"code": 110007, "msg": "insufficient balance"}
                        for index in range(len(request))
                    ]
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )

    rows = client.place_orders_batch(
        [
            {"symbol": "BUSDT", "side": "Buy", "orderType": "Market", "qty": "1", "orderLinkId": "cmd-1"},
            {"symbol": "BUSDT", "side": "Buy", "orderType": "Market", "qty": "1", "orderLinkId": "cmd-2"},
        ]
    )
    assert client._client.batch_bodies[0]["category"] == "linear"
    assert rows[0]["_row_code"] == 0
    assert rows[0]["orderId"] == "venue-0"
    assert rows[0]["_response_time_ms"] == 1_700_000_000_000
    assert rows[1]["_row_code"] == 110007

    with pytest.raises(ValueError, match="at most 20"):
        client.place_orders_batch(
            [{"symbol": "BUSDT", "orderLinkId": f"cmd-{index}"} for index in range(21)]
        )
    with pytest.raises(ValueError, match="orderLinkId is required"):
        client.place_orders_batch([{"symbol": "BUSDT"}])

    class MismatchedHTTP(FakeHTTP):
        def place_batch_order(self, **kwargs):
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"list": [{}]},
                "retExtInfo": {"list": []},
            }

    monkeypatch.setattr(bybit, "HTTP", MismatchedHTTP)
    mismatched = bybit.BybitPrivateClient(
        api_key="key2",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key2"),
    )
    with pytest.raises(bybit.BybitSubmissionUncertain, match="do not match"):
        mismatched.place_orders_batch(
            [
                {"symbol": "BUSDT", "orderLinkId": "cmd-1"},
                {"symbol": "BUSDT", "orderLinkId": "cmd-2"},
            ]
        )


def test_safe_int_degrades_on_malformed_error_code() -> None:
    """Malformed venue error codes must classify safely instead of raising."""

    assert bybit_errors._safe_int("10006") == 10006
    assert bybit_errors._safe_int("not-a-number") == 0
    assert bybit_errors._safe_int(None) == 0
    assert bybit_errors._safe_int({"retCode": "x"}) == 0


# --------------------------------------------------------------------------
# Shared BybitMarketData counters are lock-guarded
# --------------------------------------------------------------------------


def test_market_data_counters_no_lost_update_under_threads(monkeypatch) -> None:
    """The bootstrap pool shares one ``BybitMarketData`` across 16 threads; concurrent
    ``_get``/``_record_call`` must not lose counter increments, so the lock keeps
    logical_calls/http_calls/total_call_ms exact.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

        def get_tickers(self, **_kwargs):
            return {"retCode": 0, "result": {"list": []}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    market = bybit_market_data.BybitMarketData()

    workers = 16
    per_worker = 50
    barrier = threading.Barrier(workers)

    def hammer() -> None:
        barrier.wait()  # release all threads together to maximise contention
        for _ in range(per_worker):
            market.get_tickers()

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = market.stats()
    assert stats["logical_calls"] == workers * per_worker
    assert stats["http_calls"] == workers * per_worker


# --------------------------------------------------------------------------
# Get_instruments_info bounds its cursor walk
# --------------------------------------------------------------------------


def test_get_instruments_info_bounds_non_advancing_cursor(monkeypatch) -> None:
    """A stable, non-empty ``nextPageCursor`` must not loop forever: the walk breaks on a non-advancing cursor."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_instruments_info(self, **_params):
            self.calls += 1
            # Always returns the SAME non-empty cursor -> would loop forever.
            return {
                "retCode": 0,
                "result": {"list": [{"symbol": f"S{self.calls}"}], "nextPageCursor": "STUCK"},
            }

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    market = bybit_market_data.BybitMarketData()
    rows = market.get_instruments_info()
    # A stable cursor is detected as non-advancing on the SECOND fetch (the new
    # cursor equals the previous), so the walk stops at 2 calls rather than
    # looping forever; an unguarded `while True` never terminates.
    assert market._client.calls == 2
    assert len(rows) == 2


def test_get_instruments_info_caps_at_max_pages(monkeypatch) -> None:
    """Even with an always-advancing cursor the walk is capped at ``max_pages`` so a
    pathological venue response cannot spin unbounded.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_instruments_info(self, **_params):
            self.calls += 1
            return {
                "retCode": 0,
                "result": {
                    "list": [{"symbol": f"S{self.calls}"}],
                    "nextPageCursor": f"cursor-{self.calls}",  # always advances
                },
            }

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    market = bybit_market_data.BybitMarketData()
    rows = market.get_instruments_info(max_pages=3)
    assert market._client.calls == 3
    assert len(rows) == 3


def test_get_instruments_info_complete_mode_rejects_truncated_cursor(monkeypatch) -> None:
    """Evidence-grade population snapshots must not accept bounded truncation."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_instruments_info(self, **_params):
            self.calls += 1
            return {
                "retCode": 0,
                "result": {
                    "list": [{"symbol": f"S{self.calls}"}],
                    "nextPageCursor": f"cursor-{self.calls}",
                },
            }

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    market = bybit_market_data.BybitMarketData()
    with pytest.raises(bybit.BybitDataError, match="complete instrument coverage"):
        market.get_instruments_info(max_pages=3, require_complete=True)


def test_get_instruments_info_complete_mode_rejects_stuck_cursor(monkeypatch) -> None:
    """A repeated non-empty cursor is incomplete, not a successful last page."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

        def get_instruments_info(self, **_params):
            return {
                "retCode": 0,
                "result": {"list": [{"symbol": "S"}], "nextPageCursor": "STUCK"},
            }

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    market = bybit_market_data.BybitMarketData()
    with pytest.raises(bybit.BybitDataError, match="non-advancing cursor"):
        market.get_instruments_info(require_complete=True)


# --------------------------------------------------------------------------
# Throttle counted once + no busy-spin at window edge
# --------------------------------------------------------------------------


def test_rate_limiter_counts_throttle_once_per_blocked_acquire(monkeypatch) -> None:
    """A single blocked acquire records exactly one throttle event and accumulates the
    slept time once, even when the wait loop re-evaluates. A deterministic clock and
    sleep force the re-loop.
    """
    clock = {"t": 1000.0}
    slept: list[float] = []

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        # Advance the clock by LESS than requested on the first wake so the loop
        # re-evaluates while still inside the window (the inflation trigger),
        # then fully on the second.
        if len(slept) == 1:
            clock["t"] += seconds / 2.0
        else:
            clock["t"] += seconds

    monkeypatch.setattr(bybit_market_data.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bybit_market_data.time, "sleep", fake_sleep)

    limiter = bybit_market_data.BybitRestRateLimiter(max_requests=1, per_seconds=1.0)
    limiter.acquire()  # fills the single slot at t=1000
    limiter.acquire()  # blocks; first wake is early, re-loops, second wake claims

    stats = limiter.stats()
    # The block is ONE logical throttle even though the loop slept twice.
    assert stats["throttle_events"] == 1, stats
    assert stats["throttled_seconds"] > 0.0
    # No busy-spin: every wait the loop computed was a real sleep, not a spin.
    assert all(s > 0.0 for s in slept), slept


def test_rate_limiter_no_busy_spin_at_window_boundary(monkeypatch) -> None:
    """An oldest slot exactly AT the window cutoff is popped (<= cutoff), so the
    limiter never busy-spins waiting for the clock. The clock is held fixed at the
    boundary, so a spin would hang.
    """
    clock = {"t": 500.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:  # pragma: no cover - must not be reached
        raise AssertionError(f"unexpected sleep({seconds}); boundary slot should free immediately")

    monkeypatch.setattr(bybit_market_data.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bybit_market_data.time, "sleep", fake_sleep)

    limiter = bybit_market_data.BybitRestRateLimiter(max_requests=1, per_seconds=1.0)
    limiter.acquire()  # slot at t=500
    # Advance the clock to EXACTLY one window later: the slot is at the cutoff.
    clock["t"] = 501.0
    # With the strict `<` pop this would compute wait==0 and continue-spin
    # forever (fake_sleep raises if ever called). With `<=` the slot frees and
    # the acquire returns immediately without sleeping.
    limiter.acquire()
    assert limiter.stats()["throttle_events"] == 0


# --------------------------------------------------------------------------
# The signing client refuses real-money submits by default
# --------------------------------------------------------------------------


def test_private_client_refuses_every_real_money_mutation(monkeypatch) -> None:
    """The cutover client cannot even construct a mainnet private session."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):  # pragma: no cover - must not be reached
            raise AssertionError("real-money submit should have been blocked")

        def cancel_order(self, **_params):  # pragma: no cover - must not be reached
            raise AssertionError("real-money cancel should have been blocked")

        def set_leverage(self, **_params):  # pragma: no cover - must not be reached
            raise AssertionError("real-money leverage should have been blocked")

        def set_trading_stop(self, **_params):  # pragma: no cover - must not be reached
            raise AssertionError("real-money stop should have been blocked")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match="contradicts demo=False"):
        bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)


def test_private_client_real_money_reads_are_removed(monkeypatch) -> None:
    """No current caller justifies retaining a mainnet private read session."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_wallet_balance(self, **_params):
            return {"retCode": 0, "result": {"list": [{"coin": "USDT"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match="contradicts demo=False"):
        bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)


def test_private_client_real_money_mutation_rejects_demo_lease(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """Even a live demo capability cannot authorize a mainnet mutation."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "rm-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.setenv("REAL_MONEY", "true")
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=False,
        realm="mainnet",
        mutation_lease=held_demo_mutation_lease("k"),
    )
    with pytest.raises(RuntimeError, match="belongs to a different environment"):
        client.place_order(symbol="BTCUSDT", side="Buy", qty="1", orderLinkId="rm-1")


def test_private_client_demo_submit_requires_held_capability(
    monkeypatch, held_demo_mutation_lease
) -> None:

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        mutation_lease=held_demo_mutation_lease("k"),
    )
    result = client.place_order(
        symbol="BTCUSDT",
        side="Buy",
        orderType="Market",
        qty="1",
        orderLinkId="demo-1",
    )
    assert result["orderId"] == "demo-ok"


# --------------------------------------------------------------------------
# Malformed REAL_MONEY toggle fails loud
# --------------------------------------------------------------------------


def test_resolve_credentials_rejects_ambiguous_real_money(monkeypatch) -> None:
    """A set-but-unrecognised ``REAL_MONEY`` value (e.g. 'enabled') must raise at
    resolution rather than silently coercing to demo.
    """
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("REAL_MONEY", "enabled")
    with pytest.raises(RuntimeError, match="not a recognised boolean"):
        bybit.resolve_demo_credentials()


def test_resolve_credentials_accepts_recognised_falsey_values(monkeypatch) -> None:
    """Explicit falsey values (false/0/off/empty) stay demo without raising -- the
    fail-safe whitelist remains permissive.
    """
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    for falsey in ("false", "0", "off", "no", ""):
        monkeypatch.setenv("REAL_MONEY", falsey)
        assert bybit.resolve_demo_credentials() == ("demo-k", "demo-s")


def test_resolve_credentials_logs_resolved_account(monkeypatch, caplog) -> None:
    """Resolution emits a single INFO line naming the resolved account, so "which
    account did this process use" is answerable from the log.
    """
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with caplog.at_level("INFO", logger="liquidity_migration.venue.bybit.account"):
        bybit.resolve_demo_credentials()
    assert any("resolved account realm: demo" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Ping-timer patch cancels priors; _close_ws_client cleans either
# --------------------------------------------------------------------------


def test_ping_timer_patch_cancels_prior_timer_on_reconnect(monkeypatch) -> None:
    """A reconnect re-invokes ``_send_initial_ping``; the patched version cancels the
    prior timer before installing a new one so reconnects do not accumulate orphan
    daemon Timer threads.
    """

    class FakeManager:
        ping_interval = 1000

        def _send_custom_ping(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pybit._websocket_stream",
        SimpleNamespace(_V5WebSocketManager=FakeManager),
    )
    bybit._patch_pybit_daemon_ping_timer()
    manager = FakeManager()

    manager._send_initial_ping()
    first = manager._agc_ping_timer
    assert first.daemon is True
    # Mirror onto the stock attribute too, so pybit's own exit() can cancel it.
    assert manager.custom_ping_timer is first

    manager._send_initial_ping()  # simulate a reconnect
    second = manager._agc_ping_timer
    assert second is not first
    # The prior timer was cancelled (not orphaned): a cancelled Timer's thread
    # winds down promptly, so a short join completes. An uncancelled prior timer was
    # overwritten WITHOUT cancel and would have run its full ping_interval.
    first.join(timeout=2.0)
    assert not first.is_alive()
    second.cancel()
    second.join(timeout=2.0)


def test_close_ws_client_cancels_stock_custom_ping_timer() -> None:
    """``_close_ws_client`` must cancel the ping timer even when only the stock pybit
    attribute (``custom_ping_timer``) is set, so a pybit bump that stops calling our
    patched ``_send_initial_ping`` cannot turn the cancel into a no-op.
    """
    cancelled = {"v": False}

    class FakeTimer:
        def cancel(self) -> None:
            cancelled["v"] = True

    class FakeClient:
        def __init__(self) -> None:
            # Only the stock attribute is present (no _agc_ping_timer).
            self.custom_ping_timer = FakeTimer()

        def exit(self) -> None:
            pass

    client = FakeClient()
    bybit._close_ws_client(client, timeout_seconds=1.0)
    assert cancelled["v"] is True


# --------------------------------------------------------------------------
# A callback swap re-routes bars on already-subscribed connections
# --------------------------------------------------------------------------


class _SwapFakeWebSocket:
    def __init__(self) -> None:
        self.callback = None

    def kline_stream(self, *, interval, symbol, callback) -> None:
        del interval, symbol
        self.callback = callback

    def unsubscribe(self, *, topic: str) -> None:
        del topic

    def close(self) -> None:
        pass

    def inject_bar(self, symbol: str) -> None:
        assert self.callback is not None
        self.callback(
            {
                "topic": f"kline.60.{symbol}",
                "data": [{"start": 0, "confirm": True, "close": "1.0"}],
            }
        )


class _SwapFactory:
    def __init__(self) -> None:
        self.built: list[_SwapFakeWebSocket] = []

    def __call__(self, *, testnet: bool, demo: bool, channel_type: str) -> _SwapFakeWebSocket:
        del testnet, demo, channel_type
        ws = _SwapFakeWebSocket()
        self.built.append(ws)
        return ws


def test_callback_swap_reroutes_existing_connection() -> None:
    """After ``subscribe()`` replaces the callback, an already-subscribed connection
    must fire the NEW sink -- the closure dereferences ``self._on_bar`` live, not at
    build time.
    """
    factory = _SwapFactory()
    pool = BybitKlineStreamPool(
        interval_minutes=60,
        topics_per_connection=10,
        connection_spacing_seconds=0.0,
        websocket_factory=factory,
    )
    old_bars: list[str] = []
    new_bars: list[str] = []
    try:
        pool.subscribe(["BTCUSDT"], lambda s, b, c: old_bars.append(s))
        ws = factory.built[0]
        ws.inject_bar("BTCUSDT")
        assert old_bars == ["BTCUSDT"]

        # Swap the callback (same symbol set -> no new connection built).
        pool.subscribe(["BTCUSDT"], lambda s, b, c: new_bars.append(s))
        assert len(factory.built) == 1  # no new connection
        ws.inject_bar("BTCUSDT")  # fire on the SAME existing connection
        # The new sink got the bar; the old sink did not see a second one.
        assert new_bars == ["BTCUSDT"]
        assert old_bars == ["BTCUSDT"]
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# _paged_time_range mid-pagination empty-page guard. The Bybit funding-rate and
# open-interest paths walk ``endTime`` backwards; an empty list returned after a
# full page is a transient hole, and truncating there lets
# ``_download_symbol_dataset`` see ``frame.height > 0`` and write the
# full-requested-range completeness marker, a permanent silent gap
# ``_marked_complete`` never re-fetches. A mid-range empty page raises
# ``BybitDataError`` so the symbol-range is retried; a first-page empty (genuine
# "no data in range") still returns cleanly.
# ---------------------------------------------------------------------------


def _make_market_data(monkeypatch, responses_by_end_time):
    """Build a ``BybitMarketData`` whose FakeHTTP serves canned funding/OI pages.

    ``responses_by_end_time`` maps the per-request ``endTime`` (the backwards
    pagination cursor) to the ``result.list`` payload to return for that call.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls: list[dict] = []

        def _serve(self, **params):
            self.calls.append(params)
            end_time = int(params["endTime"])
            return {"retCode": 0, "result": {"list": responses_by_end_time[end_time]}}

        # Both funding-history and open-interest route through _paged_time_range.
        def get_funding_rate_history(self, **params):
            return self._serve(**params)

        def get_open_interest(self, **params):
            return self._serve(**params)

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    # retries=1 keeps the test fast and deterministic; the guard fires before
    # any retry/backoff because BybitDataError(non-rate-limit) raises immediately.
    return bybit_market_data.BybitMarketData(testnet=True, retries=1, retry_sleep_seconds=0.0)


def _funding_row(ts: int) -> dict[str, str]:
    return {"fundingRateTimestamp": str(ts), "fundingRate": "0.0001"}


def test_mid_range_empty_page_raises_instead_of_truncating(monkeypatch) -> None:
    """A full page followed by an empty page mid-range must raise, not truncate.

    limit=2, range [0, 10]. The first request (endTime=10) returns a FULL page
    (ts 9, 8) so pagination continues with endTime = 8 - 1 = 7; the second
    (endTime=7) returns [] -- a transient hole. Raising makes the downloader retry
    instead of writing a full-range marker over a short read.
    """
    responses = {
        10: [_funding_row(9), _funding_row(8)],  # full page -> keep paginating
        7: [],  # transient empty mid-range -> must NOT silently truncate
    }
    client = _make_market_data(monkeypatch, responses)

    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.get_funding_history("BTCUSDT", start=0, end=11, limit=2)

    assert "mid-range" in str(excinfo.value)
    # Both pages were actually requested before the guard fired.
    assert len(client._client.calls) == 2
    assert int(client._client.calls[1]["endTime"]) == 7


def test_first_page_empty_returns_cleanly_no_data_in_range(monkeypatch) -> None:
    """A genuinely empty range (first page empty) returns [] without raising."""
    responses = {10: []}
    client = _make_market_data(monkeypatch, responses)

    rows = client.get_funding_history("BTCUSDT", start=0, end=11, limit=2)

    assert rows == []
    # Only one request was made; no spurious retry/extra pagination.
    assert len(client._client.calls) == 1


def test_full_then_short_page_completes_normally(monkeypatch) -> None:
    """The happy multi-page path is unchanged: a full page then a short
    (< limit) page terminates cleanly and returns the union, ascending by ts."""
    responses = {
        10: [_funding_row(9), _funding_row(8)],  # full page (len == limit)
        7: [_funding_row(5)],  # short page (len < limit) -> natural end of data
    }
    client = _make_market_data(monkeypatch, responses)

    rows = client.get_funding_history("BTCUSDT", start=0, end=11, limit=2)

    assert [int(r["fundingRateTimestamp"]) for r in rows] == [5, 8, 9]
    assert len(client._client.calls) == 2


def test_open_interest_mid_range_empty_also_guarded(monkeypatch) -> None:
    """``open_interest`` shares ``_paged_time_range``, so the same guard applies; its timestamp key is "timestamp"."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls: list[dict] = []

        def get_open_interest(self, **params):
            self.calls.append(params)
            end_time = int(params["endTime"])
            pages = {
                10: [
                    {"timestamp": "9", "openInterest": "1"},
                    {"timestamp": "8", "openInterest": "2"},
                ],
                7: [],  # transient mid-range empty
            }
            return {"retCode": 0, "result": {"list": pages[end_time]}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    client = bybit_market_data.BybitMarketData(testnet=True, retries=1, retry_sleep_seconds=0.0)

    with pytest.raises(bybit.BybitDataError):
        client.get_open_interest("BTCUSDT", "5min", start=0, end=10, limit=2)


def test_demo_mutation_guard_requires_live_credential_bound_lease(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **_kwargs):
            self.calls = []

        def place_order(self, **params):
            self.calls.append(params)
            return {"retCode": 0, "result": {"orderId": "o1"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    unleased = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=True)
    with pytest.raises(RuntimeError, match="no canonical Bybit account mutation lease"):
        unleased.place_order(orderLinkId="unleased-1", symbol="BUSDT", side="Buy", orderType="Market", qty="1")
    assert unleased._client.calls == []

    lease = held_demo_mutation_lease("k")
    owner = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        mutation_lease=lease,
    )
    assert (
        owner.place_order(orderLinkId="owner-1", symbol="BUSDT", side="Buy", orderType="Market", qty="1")["orderId"]
        == "o1"
    )
    lease.close()
    with pytest.raises(RuntimeError, match="not currently held"):
        owner.place_order(orderLinkId="owner-2", symbol="BUSDT", side="Buy", orderType="Market", qty="1")


def test_demo_mutation_guard_rejects_capability_lookalike(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **_kwargs):
            self.calls = []

        def place_order(self, **params):  # pragma: no cover - guard must run first
            self.calls.append(params)
            return {"retCode": 0, "result": {"orderId": "o1"}}

    class LeaseLookalike:
        def require_held_for(self, **_kwargs):
            return None

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        mutation_lease=LeaseLookalike(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="no canonical Bybit account mutation lease"):
        client.place_order(
            orderLinkId="lookalike-1",
            symbol="BUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
        )
    assert client._client.calls == []


def test_demo_mutation_guard_rejects_lease_for_different_credential(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **_kwargs):
            self.calls = []

        def place_order(self, **params):  # pragma: no cover - guard must run first
            self.calls.append(params)
            return {"retCode": 0, "result": {"orderId": "o1"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="configured-key",
        api_secret="s",
        demo=True,
        mutation_lease=held_demo_mutation_lease("different-key"),
    )
    with pytest.raises(RuntimeError, match="different API credential"):
        client.place_order(
            orderLinkId="wrong-key-1",
            symbol="BUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
        )
    assert client._client.calls == []


def test_private_transport_fails_unknown_methods_into_mutation_boundary(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **_kwargs):
            self.calls: list[dict[str, str]] = []

        def amend_order(self, **params):
            self.calls.append(params)
            return {"retCode": 0, "result": {"orderId": "amended"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    read_only = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
    )
    with pytest.raises(RuntimeError, match="no canonical Bybit account mutation lease"):
        read_only._call("amend_order", orderId="o1")
    assert read_only._client.calls == []

    owner = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        mutation_lease=held_demo_mutation_lease("k"),
    )
    assert owner._call("amend_order", orderId="o1")["result"]["orderId"] == "amended"


def test_get_transaction_log_refuses_truncated_pagination(monkeypatch) -> None:
    """The funding reconciler advances its window past whatever this returns;
    silently truncating at max_pages would permanently skip settlements."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.page = 0

        def get_transaction_log(self, **params):
            self.page += 1
            return {
                "retCode": 0,
                "result": {
                    "list": [{"id": f"row-{self.page}"}],
                    "nextPageCursor": f"page-{self.page + 1}",
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    with pytest.raises(bybit.BybitDataError, match="exceeded max_pages"):
        client.get_account_transactions(
            transaction_type="SETTLEMENT",
            start_time_ms=1_000,
            end_time_ms=2_000,
            max_pages=3,
            strict=True,
        )


def test_get_trade_history_refuses_non_advancing_cursor(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls = 0

        def get_executions(self, **params):
            self.calls += 1
            return {
                "retCode": 0,
                "result": {
                    "list": [{"execId": f"dup-{self.calls}"}],
                    "nextPageCursor": "stuck",
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    with pytest.raises(bybit.BybitDataError, match="non-advancing"):
        client.get_trade_history(symbol="FOOUSDT", max_pages=10)


def test_bybit_private_client_treats_trading_stop_not_modified_retcode_as_success(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_trading_stop(self, **params):
            del params
            return {"retCode": 34040, "retMsg": "not modified"}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    result = client.set_trading_stop(symbol="TLMUSDT", stop_loss="0.0023054")

    assert result == {"symbol": "TLMUSDT", "retCode": 34040}


def test_bybit_private_client_treats_pybit_trading_stop_not_modified_exception_as_success(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_trading_stop(self, **params):
            del params
            raise RuntimeError("not modified (ErrCode: 34040) (ErrTime: 09:59:57)")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    result = client.set_trading_stop(symbol="TLMUSDT", stop_loss="0.0023054")

    assert result == {"symbol": "TLMUSDT", "retCode": 34040}


def test_bybit_private_client_still_rejects_other_trading_stop_errors(
    monkeypatch, held_demo_mutation_lease
) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_trading_stop(self, **params):
            del params
            return {"retCode": 10001, "retMsg": "params error"}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
    )
    with pytest.raises(bybit_errors.BybitRequestRejected, match="set_trading_stop failed"):
        client.set_trading_stop(symbol="TLMUSDT", stop_loss="0.0023054")


def test_kline_window_pager_raises_on_a_bracketed_empty_window(monkeypatch) -> None:
    """``get_klines``/``_get_price_index_klines`` need the mid-range hole guard that
    ``_paged_time_range`` and the Binance pagers have: a transient retCode-0 empty
    window silently drops up to limit x interval bars, and the downloader then seals
    the gap with a full-range completeness marker.
    """

    interval_ms = bybit_market_data.INTERVAL_MS["60"]
    # The hole must be wider than one window span (limit-1 intervals) so at
    # least one whole window comes back empty between two populated ones.
    present = {0, 1, 2, 12, 13, 14}

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls: list[dict] = []

        def get_kline(self, **params):
            self.calls.append(params)
            start, end = int(params["start"]), int(params["end"])
            rows = [
                [str(index * interval_ms), "1", "2", "0.5", "1.5", "10", "15"]
                for index in sorted(present)
                if start <= index * interval_ms <= end
            ]
            return {"retCode": 0, "result": {"list": list(reversed(rows))}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    client = bybit_market_data.BybitMarketData()
    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.get_klines("BTCUSDT", "60", 0, 15 * interval_ms, limit=3)
    assert "empty window mid-range" in str(excinfo.value)


def test_kline_window_pager_tolerates_leading_and_trailing_empty_windows(monkeypatch) -> None:
    """A symbol listed after `start` (or delisted before `end`) legitimately has
    empty windows at the edges; the guard must only fire on an interior hole."""

    interval_ms = bybit_market_data.INTERVAL_MS["60"]
    present = {3, 4, 5}

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls: list[dict] = []

        def get_kline(self, **params):
            self.calls.append(params)
            start, end = int(params["start"]), int(params["end"])
            rows = [
                [str(index * interval_ms), "1", "2", "0.5", "1.5", "10", "15"]
                for index in sorted(present)
                if start <= index * interval_ms <= end
            ]
            return {"retCode": 0, "result": {"list": list(reversed(rows))}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    client = bybit_market_data.BybitMarketData()
    rows = client.get_klines("BTCUSDT", "60", 0, 9 * interval_ms, limit=3)
    assert [int(row[0]) // interval_ms for row in rows] == [3, 4, 5]


def test_kline_window_pager_reretries_a_transient_empty_window(monkeypatch) -> None:
    interval_ms = bybit_market_data.INTERVAL_MS["60"]

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls: list[dict] = []
            self.blanked = False

        def get_kline(self, **params):
            self.calls.append(params)
            start, end = int(params["start"]), int(params["end"])
            rows = [
                [str(index * interval_ms), "1", "2", "0.5", "1.5", "10", "15"]
                for index in range(9)
                if start <= index * interval_ms <= end
            ]
            if start > 0 and not self.blanked:
                # One transient retCode-0 empty response mid-range.
                self.blanked = True
                return {"retCode": 0, "result": {"list": []}}
            return {"retCode": 0, "result": {"list": list(reversed(rows))}}

    monkeypatch.setattr(bybit_market_data, "HTTP", FakeHTTP)
    client = bybit_market_data.BybitMarketData()
    rows = client.get_klines("BTCUSDT", "60", 0, 9 * interval_ms, limit=3)
    assert [int(row[0]) // interval_ms for row in rows] == list(range(9))


def test_pybit_still_exposes_the_demo_endpoint() -> None:
    """``_require_demo_endpoint`` can only assert a host that pybit still reports, so a
    pybit upgrade that renames or drops ``endpoint`` has to fail here rather than
    turn the guard into a silent no-op.
    """

    from pybit.unified_trading import HTTP

    from liquidity_migration.venue.bybit import DEMO_REST_ENDPOINT

    demo = HTTP(testnet=False, demo=True, api_key="k", api_secret="s")
    assert str(getattr(demo, "endpoint")).rstrip("/") == DEMO_REST_ENDPOINT

    mainnet = HTTP(testnet=False, demo=False, api_key="k", api_secret="s")
    assert str(getattr(mainnet, "endpoint")).rstrip("/") != DEMO_REST_ENDPOINT


def test_private_client_refuses_a_transport_that_resolved_to_mainnet() -> None:
    """The failure this guard exists for: every local flag still reads 'demo' while the transport addresses mainnet."""

    import pytest

    from liquidity_migration.venue import bybit

    class _MainnetHTTP:
        __module__ = "pybit.unified_trading"

        def __init__(self, **kwargs: object) -> None:
            self.endpoint = "https://api.bybit.com"

    original = bybit.HTTP
    bybit.HTTP = _MainnetHTTP  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="only https://api-demo.bybit.com"):
            bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=True)
    finally:
        bybit.HTTP = original  # type: ignore[assignment]


def test_trade_history_stops_paging_once_a_page_predates_the_bound(monkeypatch) -> None:
    """The protection reconciler discards executions older than the activation
    it is checking against, and runs per held symbol every couple of seconds.
    Paging twenty deep past rows it will throw away is pure round trips."""

    pages = {
        None: {
            "retCode": 0,
            "result": {"list": [{"execId": "e1", "execTime": "5000"}], "nextPageCursor": "p2"},
        },
        "p2": {
            "retCode": 0,
            "result": {"list": [{"execId": "e2", "execTime": "1000"}], "nextPageCursor": "p3"},
        },
        "p3": {
            "retCode": 0,
            "result": {"list": [{"execId": "e3", "execTime": "10"}], "nextPageCursor": ""},
        },
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls: list[dict] = []

        def get_executions(self, **params):
            self.calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    # Page 2 is entirely older than the bound, so page 3 is never fetched.
    rows = client.get_trade_history(symbol="FOOUSDT", stop_before_ns=2_000 * 1_000_000)
    assert [row["execId"] for row in rows] == ["e1", "e2"]
    assert len(client._client.calls) == 2

    # Without a bound the walk still pages to exhaustion, exactly as before.
    client._client.calls.clear()
    rows = client.get_trade_history(symbol="FOOUSDT")
    assert [row["execId"] for row in rows] == ["e1", "e2", "e3"]
    assert len(client._client.calls) == 3


def test_a_busy_venue_on_a_close_is_uncertain_not_a_definite_reject(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """A close refused as "definite" terminalizes the command, and the position
    keeps running after its stop fired. A busy matching engine is "not now"."""

    from liquidity_migration.marketdata.bybit_errors import (
        BybitRequestRejected,
        BybitSubmissionUncertain,
    )

    payloads: dict = {"current": {}}

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

        def place_order(self, **params):
            return dict(payloads["current"])

    client = _make_private_client(monkeypatch, FakeHTTP, held_demo_mutation_lease)

    for transient in (
        {"retCode": 10016, "retMsg": "Server error"},
        {"retCode": 170007, "retMsg": "Timeout waiting for response"},
        {"retCode": 999, "retMsg": "System busy, please try again"},
    ):
        payloads["current"] = transient
        with pytest.raises(BybitSubmissionUncertain):
            client.place_order(symbol="FOOUSDT", orderLinkId="x", qty="1")

    # A real refusal stays a real refusal.
    for definite in (
        {"retCode": 110007, "retMsg": "Insufficient available balance"},
        {"retCode": 110017, "retMsg": "Reduce-only rule not satisfied"},
    ):
        payloads["current"] = definite
        with pytest.raises(BybitRequestRejected):
            client.place_order(symbol="FOOUSDT", orderLinkId="x", qty="1")


def test_transient_classifier_does_not_swallow_real_refusals() -> None:
    from liquidity_migration.marketdata.bybit_errors import is_transient_venue_fault

    assert is_transient_venue_fault({"retCode": 10006, "retMsg": "too many visits"})
    assert is_transient_venue_fault({"retCode": 10016, "retMsg": "Server error"})
    assert is_transient_venue_fault({"retCode": 0, "retMsg": "system busy"})
    assert not is_transient_venue_fault({"retCode": 110007, "retMsg": "Insufficient balance"})
    assert not is_transient_venue_fault({"retCode": 110043, "retMsg": "leverage not modified"})
    assert not is_transient_venue_fault({"retCode": 110017, "retMsg": "reduce-only rule"})


def test_a_definite_reject_stays_definite_when_the_order_body_carries_a_code_digit_run() -> None:
    """The classifier is handed the exception, not just ``retMsg``.

    ``str(exc)`` for a pybit ``InvalidRequestError`` ends with
    ``Request → POST /v5/order/create: {...}`` — the whole order body. Scanning
    it for a bare ``170146`` read an "insufficient balance" refusal on a stop
    price of ``100025.5`` as "not now", so a definite reject came back as
    ``BybitSubmissionUncertain`` and left the command to wedge.
    """

    from pybit.exceptions import InvalidRequestError

    from liquidity_migration.marketdata.bybit_errors import is_transient_venue_fault

    def _rejected(message: str, code: int, body: str) -> InvalidRequestError:
        return InvalidRequestError(
            request=f"POST /v5/order/create: {body}",
            message=message,
            status_code=code,
            time="2026-08-08T12:00:00Z",
            resp_headers=None,
        )

    # Every digit run below is a transient code appearing inside the body.
    assert not is_transient_venue_fault(
        _rejected("Insufficient balance", 110007, '{"symbol":"XUSDT","stopLoss":"100025.5"}')
    )
    assert not is_transient_venue_fault(
        _rejected("Reduce-only rule not satisfied", 110017, '{"orderLinkId":"lm-170146-3"}')
    )
    assert not is_transient_venue_fault(
        _rejected("Insufficient balance", 110007, '{"symbol":"PEPEUSDT","price":"0.0100025"}')
    )
    # A genuinely transient one still classifies, read off the venue's own code.
    assert is_transient_venue_fault(_rejected("System busy", 10016, '{"symbol":"XUSDT"}'))
