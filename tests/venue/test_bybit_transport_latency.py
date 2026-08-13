"""Latency contracts of the private Bybit transport.

Two defects covered here, both on the path between deciding to trade and the
order leaving:

C1 -- pybit 5.16.0 retries a fixed set of retCodes INSIDE its request loop,
sleeping between attempts (the rate-limit case waits for the venue's advertised
reset). The repo classifies those responses instead of sleeping on them, so
``BybitPrivateClient`` strips the library retry loop: a rate-limited order
create must classify immediately as "not now", never block in library sleeps.

C2 -- the venue edge closes an idle HTTPS connection after tens of seconds, so
the first order after a quiet spell paid a full TLS handshake. The client now
re-touches its connection with an unsigned public server-time ping on a small
background thread.
"""

from __future__ import annotations

import dataclasses
import gc
import time
import weakref

import pytest

from liquidity_migration.venue import bybit
import liquidity_migration.account.account_owner_lease as owner_lease_module
from liquidity_migration.account.account_owner_lease import (
    DemoAccountIdentity,
    DemoAccountMutationLease,
)


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
                "userID": 910_000 + len(leases),
            },
        )
        lease = DemoAccountMutationLease(identity)
        lease.acquire()
        leases.append(lease)
        return lease

    yield acquire
    for lease in reversed(leases):
        lease.close()


class _FakeVenueResponse:
    """The slice of a requests.Response that pybit 5.16.0 reads."""

    def __init__(self, body: dict, headers: dict | None = None) -> None:
        self.status_code = 200
        self.headers = dict(headers or {})
        self.url = "https://api-demo.bybit.com/v5/mocked"
        self._body = dict(body)

    def json(self) -> dict:
        return dict(self._body)


# --------------------------------------------------------------------------
# C1: the library's sleep-retries are stripped; the repo classifies instead.
# --------------------------------------------------------------------------


def test_private_client_strips_pybit_sleep_retries_in_both_realms(monkeypatch) -> None:
    """Every constructed client, demo and mainnet, runs the pybit request loop
    single-shot with an empty retry table."""

    monkeypatch.delenv("REAL_MONEY", raising=False)
    demo = bybit.BybitPrivateClient(api_key="k", api_secret="s", keep_session_warm=False)
    # A real pybit transport, so these attributes are the load-bearing ones.
    assert type(demo._client).__module__.startswith("pybit")
    assert demo._client.retry_codes == set()
    assert demo._client.max_retries == 1
    assert demo._client.force_retry is False

    monkeypatch.setenv("REAL_MONEY", "true")
    mainnet = bybit.BybitPrivateClient(
        api_key="k", api_secret="s", demo=False, realm="mainnet", keep_session_warm=False
    )
    assert type(mainnet._client).__module__.startswith("pybit")
    assert mainnet._client.retry_codes == set()
    assert mainnet._client.max_retries == 1


def test_pybit_retry_attributes_still_exist_and_default_to_sleep_retries() -> None:
    """Drift guard against pybit 5.16.0, in both directions.

    The strip in ``BybitPrivateClient.__post_init__`` assigns ``retry_codes``
    and ``max_retries`` on the built session; if a pybit upgrade renames either
    attribute the strip becomes a silent no-op, and this test fails first.

    It also records the fails-without-fix baseline: the shipped defaults are
    three attempts over a six-code retry table that includes the rate limit
    (10006), and pybit's own ``__post_init__`` replaces a falsy ``retry_codes``
    with that table -- which is exactly why the constructor cannot express
    "retry nothing" and the attributes must be assigned after construction.
    """

    from pybit._http_manager import _V5HTTPManager

    names = {f.name for f in dataclasses.fields(_V5HTTPManager)}
    assert {"max_retries", "retry_codes", "force_retry", "retry_delay"} <= names

    stock = _V5HTTPManager()
    assert stock.max_retries == 3
    assert stock.retry_codes == {10002, 10006, 30034, 30035, 130035, 130150}
    assert stock.force_retry is False

    emptied = _V5HTTPManager(retry_codes=set())
    assert emptied.retry_codes == {10002, 10006, 30034, 30035, 130035, 130150}


def test_rate_limited_order_create_classifies_immediately_without_library_sleep(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """A mocked 10006 on the order path is one send, no sleep, classified
    "not now" so the reconciler's probe ladder keeps the command live."""

    monkeypatch.delenv("REAL_MONEY", raising=False)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
        keep_session_warm=False,
    )
    calls = {"n": 0}

    def rate_limited_send(request, timeout=None):
        calls["n"] += 1
        return _FakeVenueResponse({"retCode": 10006, "retMsg": "Too many visits!"})

    client._client.client.send = rate_limited_send

    started = time.monotonic()
    with pytest.raises(bybit.BybitSubmissionUncertain):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="lm-warm-limit-1",
        )
    elapsed = time.monotonic() - started

    assert calls["n"] == 1, "the library must not re-send on its own"
    assert elapsed < 0.1, f"a rate-limited order create blocked for {elapsed:.3f}s"


def test_stock_pybit_retry_config_sleeps_on_the_same_rate_limit_mock(
    monkeypatch, held_demo_mutation_lease
) -> None:
    """Fails-without-fix control: put pybit 5.16.0's shipped retry config back
    and the identical mocked 10006 re-sends three times, sleeping for the
    venue's advertised reset before each, before classification ever runs."""

    monkeypatch.delenv("REAL_MONEY", raising=False)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        mutation_lease=held_demo_mutation_lease("key"),
        keep_session_warm=False,
    )
    session = client._client
    session.retry_codes = {10002, 10006, 30034, 30035, 130035, 130150}
    session.max_retries = 3
    calls = {"n": 0}

    def rate_limited_send(request, timeout=None):
        calls["n"] += 1
        # The venue advertises a rate-limit reset 100ms out; stock pybit
        # sleeps until it on every attempt.
        reset_ms = int(time.time() * 1000) + 100
        return _FakeVenueResponse(
            {"retCode": 10006, "retMsg": "Too many visits!"},
            headers={"X-Bapi-Limit-Reset-Timestamp": str(reset_ms)},
        )

    session.client.send = rate_limited_send

    started = time.monotonic()
    with pytest.raises(bybit.BybitSubmissionUncertain):
        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            orderType="Market",
            qty="1",
            orderLinkId="lm-warm-limit-2",
        )
    elapsed = time.monotonic() - started

    assert calls["n"] == 3, "stock config re-sends until max_retries is exhausted"
    assert elapsed >= 0.25, "stock config sleeps for the venue reset before each attempt"


def test_rate_limited_read_stays_on_the_repo_ladder_with_no_library_sleeps(monkeypatch) -> None:
    """Reads keep the exception surface callers already handle: the repo's own
    short ladder retries the transient, then raises BybitDataError carrying the
    venue's evidence. The library adds no sends and no sleeps underneath."""

    monkeypatch.delenv("REAL_MONEY", raising=False)
    client = bybit.BybitPrivateClient(
        api_key="key",
        api_secret="secret",
        demo=True,
        retry_sleep_seconds=0.0,
        keep_session_warm=False,
    )
    calls = {"n": 0}

    def rate_limited_send(request, timeout=None):
        calls["n"] += 1
        return _FakeVenueResponse({"retCode": 10006, "retMsg": "Too many visits!"})

    client._client.client.send = rate_limited_send

    started = time.monotonic()
    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.get_wallet_balance()
    elapsed = time.monotonic() - started

    assert calls["n"] == client.retries, "one send per repo attempt: the library is single-shot"
    assert "10006" in str(excinfo.value)
    assert elapsed < 0.1


# --------------------------------------------------------------------------
# C2: the client keeps its own connection warm for the order path.
# --------------------------------------------------------------------------


class _WarmFakeHTTP:
    """Transport double that counts server-time pings on the same object the
    client's real requests use, so the identity of the warmed transport is
    part of every assertion."""

    def __init__(self, **_kwargs) -> None:
        self.pings = 0
        self.reads = 0

    def get_wallet_balance(self, **_params) -> dict:
        self.reads += 1
        return {"retCode": 0, "result": {"list": [{"coin": "USDT"}]}}

    def get_server_time(self) -> dict:
        self.pings += 1
        return {"retCode": 0, "result": {"timeSecond": "1"}}


def _wait_until(predicate, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)


def test_keep_warm_thread_starts_on_first_request_and_stops_clean(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "HTTP", _WarmFakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        keep_warm_interval_seconds=0.02,
        keep_warm_jitter_seconds=0.0,
    )
    # Construction is network-silent: no thread, no ping.
    assert client._warm_thread is None
    assert client._client.pings == 0

    client.get_wallet_balance()
    thread = client._warm_thread
    assert thread is not None
    assert thread.daemon is True

    _wait_until(lambda: client._client.pings >= 2)
    # The pings land on the very transport object the client's requests use.
    assert client._client.pings >= 2
    assert client._warm_pings >= 2

    client.close()
    assert client._warm_thread is None
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "close() must end the thread, not leak it"
    settled = client._client.pings
    time.sleep(0.06)
    assert client._client.pings == settled, "no pings after close"
    client.close()  # close twice is fine


def test_warm_keeper_does_not_ping_immediately_on_start(monkeypatch) -> None:
    """The request that starts the thread has just used the connection, so the
    first re-touch is due one interval later, not at thread start."""

    monkeypatch.setattr(bybit, "HTTP", _WarmFakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        keep_warm_interval_seconds=30.0,
        keep_warm_jitter_seconds=0.0,
    )
    client.get_wallet_balance()
    assert client._warm_thread is not None
    time.sleep(0.05)
    assert client._client.pings == 0
    # close() ends a thread parked deep inside a long wait promptly.
    client.close()


def test_keep_session_warm_off_never_starts_a_thread(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "HTTP", _WarmFakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        keep_session_warm=False,
        keep_warm_interval_seconds=0.01,
        keep_warm_jitter_seconds=0.0,
    )
    client.get_wallet_balance()
    time.sleep(0.05)
    assert client._warm_thread is None
    assert client._client.pings == 0


def test_a_failing_warm_ping_never_raises_and_never_blocks_real_requests(monkeypatch) -> None:
    class _FailingPingHTTP(_WarmFakeHTTP):
        def get_server_time(self) -> dict:
            self.pings += 1
            raise RuntimeError("edge reset the connection")

    monkeypatch.setattr(bybit, "HTTP", _FailingPingHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        keep_warm_interval_seconds=0.01,
        keep_warm_jitter_seconds=0.0,
    )
    client.get_wallet_balance()
    _wait_until(lambda: client._warm_ping_failures >= 2)

    assert client._warm_ping_failures >= 2
    thread = client._warm_thread
    assert thread is not None
    assert thread.is_alive(), "a failed ping must not end the keep-warm loop"
    # A concurrent real request is untouched by the failing pings.
    assert client.get_wallet_balance() == {"list": [{"coin": "USDT"}]}
    client.close()


def test_warm_thread_ends_when_the_client_is_discarded_without_close(monkeypatch) -> None:
    """The loop holds only a weak reference, so forgetting close() cannot leak
    a thread that pings the venue forever."""

    monkeypatch.setattr(bybit, "HTTP", _WarmFakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        keep_warm_interval_seconds=0.01,
        keep_warm_jitter_seconds=0.0,
    )
    client.get_wallet_balance()
    thread = client._warm_thread
    assert thread is not None
    assert thread.is_alive()

    client_ref = weakref.ref(client)
    del client
    gc.collect()
    assert client_ref() is None, "the loop must not hold the client alive"
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_keep_warm_wait_respects_interval_and_jitter_bounds() -> None:
    draws = [bybit._keep_warm_wait_seconds(35.0, 5.0) for _ in range(500)]
    assert all(30.0 <= draw <= 40.0 for draw in draws)
    assert max(draws) - min(draws) > 1.0, "the jitter must actually spread the pings"
    assert {bybit._keep_warm_wait_seconds(35.0, 0.0) for _ in range(5)} == {35.0}
    # Floored so a misconfigured pair can never busy-spin the thread.
    assert bybit._keep_warm_wait_seconds(0.0, 0.0) == 0.01
    assert bybit._keep_warm_wait_seconds(-5.0, 0.0) == 0.01


def test_pybit_server_time_ping_goes_through_the_same_requests_session() -> None:
    """Pins pybit 5.16.0: ``get_server_time`` sends through ``http.client`` --
    the one requests.Session every private call uses -- so the keep-warm ping
    re-touches the very connection an order will travel, and it is unsigned."""

    from pybit.unified_trading import HTTP

    http = HTTP(testnet=False, demo=True, api_key="k", api_secret="s")
    sent = []

    def record_send(request, timeout=None):
        sent.append(request)
        return _FakeVenueResponse(
            {"retCode": 0, "retMsg": "OK", "result": {"timeSecond": "1"}, "time": 1}
        )

    http.client.send = record_send
    payload = http.get_server_time()

    assert payload["retCode"] == 0
    assert len(sent) == 1
    assert "/v5/market/time" in sent[0].url
    assert "X-BAPI-SIGN" not in sent[0].headers, "the ping must stay an unsigned public read"
