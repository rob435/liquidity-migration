"""Regression tests for audit bucket b01.

Covers the exec-router, ratelimit-rest, realmoney-safety, and ws-pool findings
against liquidity_migration.bybit / kline_follower / event_demo_data. Each test
is written so it would FAIL on the pre-fix code and PASS on the current fix.

The findings, by id:

  exec-router-2  duplicate-orderLinkId (110089) reject is idempotent success
  exec-router-4  strict-WS still probes on a lost-ack timeout before raising
  exec-router-5  probe row selection survives a malformed createdTime
  ratelimit-rest-2  shared BybitMarketData counters are lock-guarded
  ratelimit-rest-3  get_instruments_info bounds its cursor walk (no hang)
  ratelimit-rest-5  rate limiter counts a throttle once + no busy-spin at edge
  realmoney-safety-1  the signing client refuses real-money submits by default
  realmoney-safety-3  a malformed REAL_MONEY toggle fails loud at resolution
  ws-pool-3  ping-timer patch cancels priors; _close_ws_client cleans either attr
  ws-pool-4  a callback swap re-routes bars on already-subscribed connections
  ws-pool-6  follower _last_sig matches the generation actually merged
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from liquidity_migration import bybit, event_demo_data
from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.bybit import BybitKlineStreamPool
from liquidity_migration.kline_follower import FollowerKlineStreamManager
from liquidity_migration.kline_store import KlineStore


# --------------------------------------------------------------------------
# exec-router-2 / exec-router-5 : BybitPrivateClient.place_order idempotency
# --------------------------------------------------------------------------


def _make_private_client(monkeypatch, fake_http_cls) -> bybit.BybitPrivateClient:
    monkeypatch.setattr(bybit, "HTTP", fake_http_cls)
    return bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=True)


def test_place_order_duplicate_link_returns_existing_open_order(monkeypatch) -> None:
    """exec-router-2: a 110089 duplicate-orderLinkId reject must NOT raise; the
    order is already at Bybit under this idempotency key, so place_order probes
    by orderLinkId and returns the existing order. Pre-fix this raised
    BybitDataError -> the caller recorded an error and wrote no ledger row while
    the position was live (an orphan)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"orderId": "live-1", "orderLinkId": "agc-1", "orderStatus": "New"}
                    ],
                    "nextPageCursor": "",
                },
            }

    client = _make_private_client(monkeypatch, FakeHTTP)
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-1",
    )
    assert result["orderId"] == "live-1"
    assert result["orderLinkId"] == "agc-1"


def test_place_order_duplicate_link_raises_when_order_not_findable(monkeypatch) -> None:
    """exec-router-2: if Bybit reports a duplicate but the order cannot be found
    on either open-orders or history, surface the original reject rather than
    silently swallowing it (returning a phantom success would be worse)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {"retCode": 0, "result": {"list": []}}

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-x",
        )


def test_place_order_non_duplicate_reject_still_raises(monkeypatch) -> None:
    """exec-router-2: only 110089 is treated as idempotent success; any other
    non-zero retCode must still raise so genuine rejects are not masked."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110007, "retMsg": "insufficient balance", "result": {}}

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError, match="110007"):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-y",
        )


def test_place_order_duplicate_link_uses_history_only_for_active_status(monkeypatch) -> None:
    """exec-router-2: a Rejected/Cancelled history row does NOT count as present
    (the submit did not take), so the dup-link path must fall through to raise
    rather than returning a dead order."""

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
                "result": {
                    "list": [
                        {"orderId": "dead", "orderLinkId": "agc-z", "orderStatus": "Rejected"}
                    ]
                },
            }

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-z",
        )


def test_is_duplicate_order_link_matches_code_and_message() -> None:
    """exec-router-2: classify by retCode 110089 AND by message text so a
    re-worded retMsg still resolves as a duplicate."""
    assert bybit._is_duplicate_order_link("Bybit place_order failed: {'retCode': 110089}")
    assert bybit._is_duplicate_order_link("orderLinkID exists, duplicate")
    assert not bybit._is_duplicate_order_link("retCode 110007 insufficient balance")


def test_safe_int_degrades_on_malformed_timestamp() -> None:
    """exec-router-5: the probe's row selection (and any createdTime parse) must
    not raise on a non-numeric venue timestamp. _safe_int returns 0 instead of
    propagating a ValueError that would turn a recoverable fallback into a hard
    place_order crash."""
    assert bybit._safe_int("1700000000000") == 1700000000000
    assert bybit._safe_int("not-a-number") == 0
    assert bybit._safe_int(None) == 0
    assert bybit._safe_int({"createdTime": "x"}) == 0


# --------------------------------------------------------------------------
# exec-router-2 / exec-router-4 / exec-router-5 : BybitTradeRouter probe
# --------------------------------------------------------------------------


class _RecordingRest:
    """REST stand-in: records place_order calls and serves a probe response."""

    def __init__(self, *, open_orders=None, history=None) -> None:
        self._open_orders = open_orders or []
        self._history = history or []
        self.place_order_calls: list[dict] = []

    def place_order(self, **params):
        self.place_order_calls.append(params)
        return {"orderId": "rest-resubmit", "orderLinkId": params.get("orderLinkId")}

    def get_open_orders(self, **_params):
        return list(self._open_orders)

    def get_order_history(self, **_params):
        return list(self._history)


class _TimingOutWs:
    """WS stand-in whose place_order never acks (forces a router timeout)."""

    def place_order(self, _callback, **_params):
        return None  # ack never delivered -> router times out


def test_router_ws_timeout_probe_recovers_without_resubmit(monkeypatch) -> None:
    """exec-router-2: on a WS timeout the router probes by orderLinkId and, when
    the order is present at Bybit, returns it WITHOUT a REST resubmit (no
    double-submit). Pre-fix a timeout fell straight through to REST."""
    rest = _RecordingRest(
        open_orders=[{"orderId": "ws-took", "orderLinkId": "lnk-1", "orderStatus": "New"}]
    )
    router = bybit.BybitTradeRouter(
        rest_client=rest,
        ws_client=_TimingOutWs(),
        order_submit_mode="ws_then_rest",
        rest_fallback=True,
        ws_timeout_seconds=0.05,
    )
    result = router.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lnk-1",
    )
    assert result["orderId"] == "ws-took"
    assert rest.place_order_calls == []  # no resubmit
    stats = router.stats()
    assert stats["ws_timeout_probe_attempts"] == 1
    assert stats["ws_timeout_probe_recovered"] == 1


def test_router_ws_timeout_falls_back_to_rest_when_probe_empty(monkeypatch) -> None:
    """exec-router-2: when the probe finds nothing, the order genuinely did not
    take, so the default ws_then_rest mode resubmits via REST."""
    rest = _RecordingRest(open_orders=[], history=[])
    router = bybit.BybitTradeRouter(
        rest_client=rest,
        ws_client=_TimingOutWs(),
        order_submit_mode="ws_then_rest",
        rest_fallback=True,
        ws_timeout_seconds=0.05,
    )
    result = router.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lnk-2",
    )
    assert result["orderId"] == "rest-resubmit"
    assert len(rest.place_order_calls) == 1


def test_router_strict_ws_probes_before_raising(monkeypatch) -> None:
    """exec-router-4: strict-WS (rest_fallback=False) must still probe on a
    lost-ack timeout and recover the order before raising — otherwise it gives
    LESS orphan protection than the default mode. Pre-fix the
    `if not self._rest_fallback: raise` short-circuited before the probe."""
    rest = _RecordingRest(
        open_orders=[{"orderId": "ws-took", "orderLinkId": "lnk-3", "orderStatus": "New"}]
    )
    router = bybit.BybitTradeRouter(
        rest_client=rest,
        ws_client=_TimingOutWs(),
        order_submit_mode="ws",
        rest_fallback=False,
        ws_timeout_seconds=0.05,
    )
    result = router.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lnk-3",
    )
    assert result["orderId"] == "ws-took"
    assert rest.place_order_calls == []  # strict-WS never resubmits


def test_router_strict_ws_raises_when_probe_empty(monkeypatch) -> None:
    """exec-router-4: strict-WS still raises (never resubmits) when the probe
    finds no order — the recovery is the only behaviour change, not a fallback."""
    rest = _RecordingRest(open_orders=[], history=[])
    router = bybit.BybitTradeRouter(
        rest_client=rest,
        ws_client=_TimingOutWs(),
        order_submit_mode="ws",
        rest_fallback=False,
        ws_timeout_seconds=0.05,
    )
    with pytest.raises(bybit._RouterWsFailed):
        router.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lnk-4",
        )
    assert rest.place_order_calls == []


def test_router_probe_survives_malformed_created_time(monkeypatch) -> None:
    """exec-router-5: a non-numeric createdTime in a probe row must not raise out
    of _probe_existing_order. Pre-fix the bare int() in max(key=...) raised a
    ValueError that escaped place_order, converting a recoverable fallback into a
    hard crash."""
    rest = _RecordingRest(
        open_orders=[
            {"orderId": "a", "orderLinkId": "lnk-5", "orderStatus": "New", "createdTime": "garbage"},
            {"orderId": "b", "orderLinkId": "lnk-5", "orderStatus": "New", "createdTime": "1700000000001"},
        ]
    )
    router = bybit.BybitTradeRouter(
        rest_client=rest,
        ws_client=_TimingOutWs(),
        order_submit_mode="ws_then_rest",
        rest_fallback=True,
        ws_timeout_seconds=0.05,
    )
    # Must not raise; returns one of the matching rows.
    result = router.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="lnk-5",
    )
    assert result["orderLinkId"] == "lnk-5"
    assert rest.place_order_calls == []


# --------------------------------------------------------------------------
# ratelimit-rest-2 : shared BybitMarketData counters are lock-guarded
# --------------------------------------------------------------------------


def test_market_data_counters_no_lost_update_under_threads(monkeypatch) -> None:
    """ratelimit-rest-2: the bootstrap pool shares ONE BybitMarketData across 16
    threads. Concurrent _get / _record_call must not lose counter increments.
    Pre-fix the unlocked read-modify-write dropped increments; the lock makes
    logical_calls/http_calls/total_call_ms exact."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

        def get_tickers(self, **_kwargs):
            return {"retCode": 0, "result": {"list": []}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    market = bybit.BybitMarketData()

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
# ratelimit-rest-3 : get_instruments_info bounds its cursor walk
# --------------------------------------------------------------------------


def test_get_instruments_info_bounds_non_advancing_cursor(monkeypatch) -> None:
    """ratelimit-rest-3: a stable, non-empty nextPageCursor must NOT loop
    forever. The walk breaks on a non-advancing cursor. Pre-fix the unbounded
    `while True` hung whatever thread called it."""

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

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    market = bybit.BybitMarketData()
    rows = market.get_instruments_info()
    # A stable cursor is detected as non-advancing on the SECOND fetch (the new
    # cursor equals the previous), so the walk stops at 2 calls rather than
    # looping forever. Pre-fix this `while True` never terminated.
    assert market._client.calls == 2
    assert len(rows) == 2


def test_get_instruments_info_caps_at_max_pages(monkeypatch) -> None:
    """ratelimit-rest-3: even with an always-advancing cursor the walk is capped
    at max_pages so a pathological venue response cannot spin unbounded."""

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

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    market = bybit.BybitMarketData()
    rows = market.get_instruments_info(max_pages=3)
    assert market._client.calls == 3
    assert len(rows) == 3


# --------------------------------------------------------------------------
# ratelimit-rest-5 : throttle counted once + no busy-spin at window edge
# --------------------------------------------------------------------------


def test_rate_limiter_counts_throttle_once_per_blocked_acquire(monkeypatch) -> None:
    """ratelimit-rest-5: a single blocked acquire records exactly ONE
    throttle_event and accumulates the real slept time once. Pre-fix the
    per-loop counting inflated throttle_events/throttled_seconds when the loop
    re-evaluated still-full. We drive a deterministic clock + sleep so the
    re-loop is forced, proving the count stays at one."""
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

    monkeypatch.setattr(bybit.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bybit.time, "sleep", fake_sleep)

    limiter = bybit.BybitRestRateLimiter(max_requests=1, per_seconds=1.0)
    limiter.acquire()  # fills the single slot at t=1000
    limiter.acquire()  # blocks; first wake is early, re-loops, second wake claims

    stats = limiter.stats()
    # The block is ONE logical throttle even though the loop slept twice.
    assert stats["throttle_events"] == 1, stats
    assert stats["throttled_seconds"] > 0.0
    # No busy-spin: every wait the loop computed was a real sleep, not a spin.
    assert all(s > 0.0 for s in slept), slept


def test_rate_limiter_no_busy_spin_at_window_boundary(monkeypatch) -> None:
    """ratelimit-rest-5: an oldest slot exactly AT the window cutoff is popped
    (<= cutoff), so the limiter never enters the wait<=0 continue-loop that
    busy-spun until the clock advanced. We hold the clock fixed at the boundary
    and require the acquire to complete (a spin would hang)."""
    clock = {"t": 500.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:  # pragma: no cover - must not be reached
        raise AssertionError(f"unexpected sleep({seconds}); boundary slot should free immediately")

    monkeypatch.setattr(bybit.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bybit.time, "sleep", fake_sleep)

    limiter = bybit.BybitRestRateLimiter(max_requests=1, per_seconds=1.0)
    limiter.acquire()  # slot at t=500
    # Advance the clock to EXACTLY one window later: the slot is at the cutoff.
    clock["t"] = 501.0
    # With the strict `<` pop this would compute wait==0 and continue-spin
    # forever (fake_sleep raises if ever called). With `<=` the slot frees and
    # the acquire returns immediately without sleeping.
    limiter.acquire()
    assert limiter.stats()["throttle_events"] == 0


# --------------------------------------------------------------------------
# realmoney-safety-1 : the signing client refuses real-money submits by default
# --------------------------------------------------------------------------


def test_private_client_refuses_real_money_submit_by_default(monkeypatch) -> None:
    """realmoney-safety-1: a demo=False (real-money) client must refuse to
    place/cancel/set-leverage unless confirm_real_money=True was threaded in at
    construction. Pre-fix there was no per-submit account assertion at the
    signing layer; the demo-only invariant lived only at config validation."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):  # pragma: no cover - must not be reached
            raise AssertionError("real-money submit should have been blocked")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="rm-1",
        )
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.cancel_order(symbol="BTCUSDT", order_link_id="rm-1")


def test_private_client_real_money_reads_are_never_gated(monkeypatch) -> None:
    """realmoney-safety-1: the guard only blocks STATE-CHANGING submissions;
    read-only calls (get_*) on a real-money client must still work so reconcile
    / balance checks are unaffected."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_wallet_balance(self, **_params):
            return {"retCode": 0, "result": {"list": [{"coin": "USDT"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)
    balance = client.get_wallet_balance()
    assert balance["list"][0]["coin"] == "USDT"


def test_private_client_real_money_submit_allowed_with_explicit_optin(monkeypatch) -> None:
    """realmoney-safety-1: an explicit confirm_real_money=True opt-in lets a
    real-money client submit — the gate is a deliberate switch, not a hard wall,
    so a future validated real-money path can thread the opt-in through."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "rm-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k", api_secret="s", demo=False, confirm_real_money=True,
    )
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="rm-2",
    )
    assert result["orderId"] == "rm-ok"


def test_private_client_demo_submit_unaffected(monkeypatch) -> None:
    """realmoney-safety-1: the guard is a no-op for demo clients (the only mode
    that runs today)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=True)
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="demo-1",
    )
    assert result["orderId"] == "demo-ok"


# --------------------------------------------------------------------------
# realmoney-safety-3 : malformed REAL_MONEY toggle fails loud
# --------------------------------------------------------------------------


def test_resolve_credentials_rejects_ambiguous_real_money(monkeypatch) -> None:
    """realmoney-safety-3: a set-but-unrecognised REAL_MONEY value (e.g.
    'enabled') must raise at resolution rather than silently coercing to demo.
    Pre-fix the operator who typed REAL_MONEY=enabled got demo with no warning."""
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("REAL_MONEY", "enabled")
    with pytest.raises(RuntimeError, match="not a recognised boolean"):
        bybit.resolve_private_credentials()


def test_resolve_credentials_accepts_recognised_falsey_values(monkeypatch) -> None:
    """realmoney-safety-3: explicit falsey values (false/0/off/empty) stay demo
    without raising — the fail-safe whitelist remains permissive."""
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    for falsey in ("false", "0", "off", "no", ""):
        monkeypatch.setenv("REAL_MONEY", falsey)
        assert bybit.resolve_private_credentials() == ("demo-k", "demo-s", True)


def test_resolve_credentials_logs_resolved_account(monkeypatch, caplog) -> None:
    """realmoney-safety-3: resolution emits a single INFO line naming the
    resolved account so 'which account did this process use' is auditable from
    the log. Pre-fix there was no resolved-account telemetry anywhere."""
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with caplog.at_level("INFO", logger="liquidity_migration.bybit.account"):
        bybit.resolve_private_credentials()
    assert any("resolved account: demo" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# ws-pool-3 : ping-timer patch cancels priors; _close_ws_client cleans either
# --------------------------------------------------------------------------


def test_ping_timer_patch_cancels_prior_timer_on_reconnect(monkeypatch) -> None:
    """ws-pool-3: a reconnect re-invokes _send_initial_ping. The patched version
    must cancel the prior timer before installing a new one, so reconnects do
    not accumulate orphan daemon Timer threads. Pre-fix each reconnect overwrote
    _agc_ping_timer without cancelling it."""

    class FakeManager:
        ping_interval = 1000

        def _send_custom_ping(self):
            pass

    monkeypatch.setitem(
        sys.modules, "pybit._websocket_stream",
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
    # winds down promptly, so a short join completes. Pre-fix the prior timer was
    # overwritten WITHOUT cancel and would have run its full ping_interval.
    first.join(timeout=2.0)
    assert not first.is_alive()
    second.cancel()
    second.join(timeout=2.0)


def test_close_ws_client_cancels_stock_custom_ping_timer() -> None:
    """ws-pool-3: _close_ws_client must cancel the ping timer even when only the
    STOCK pybit attribute (custom_ping_timer) is set — so a pybit bump that
    stops calling our patched _send_initial_ping cannot silently turn the cancel
    into a no-op and reintroduce a shutdown-blocking timer thread."""
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
# ws-pool-4 : a callback swap re-routes bars on already-subscribed connections
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
    """ws-pool-4: after subscribe() replaces the callback, an ALREADY-subscribed
    connection must fire the NEW sink (the closure dereferences self._on_bar
    live, not at build time). Pre-fix the closure captured the old on_bar, so
    bars on the existing connection kept hitting the OLD sink — contradicting the
    documented 'replaces for every connection' guarantee."""
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


# --------------------------------------------------------------------------
# ws-pool-6 : follower _last_sig matches the generation actually merged
# --------------------------------------------------------------------------


def _ws_bar(ts_ms: int, *, close: float = 100.0) -> dict:
    return {
        "start": str(ts_ms),
        "open": str(close - 1.0),
        "high": str(close + 1.0),
        "low": str(close - 2.0),
        "close": str(close),
        "volume": "1000",
        "turnover": str(1000.0 * close),
    }


def _hour_floor_now_ms() -> int:
    return (int(time.time() * 1000) // MS_PER_HOUR) * MS_PER_HOUR


def test_follower_refresh_records_post_read_signature(tmp_path: Path) -> None:
    """ws-pool-6: if the leader flushes a NEWER generation between the follower's
    stat and recover_from_disk's read, the follower must record the signature of
    the generation it actually merged (post-read), not the stale pre-read one.
    Pre-fix _last_sig held the pre-read sig, so _snapshot_age_seconds lagged a
    generation and the next poll did a redundant re-read."""
    base = _hour_floor_now_ms() - 4 * MS_PER_HOUR

    leader = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for i in range(4):
        leader.add_bar("AAAUSDT", _ws_bar(base + i * MS_PER_HOUR, close=100.0 + i), confirmed=True)
    assert leader.flush_to_disk() > 0

    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)

    snapshot_path = tmp_path / ".cache" / "ws_klines" / "store.parquet"
    real_recover = follower._store.recover_from_disk

    def recover_then_leader_flushes_again() -> int:
        # Simulate the leader flushing a NEWER generation DURING our read: the
        # merge below sees whichever generation, but the on-disk file ends newer.
        rows = real_recover()
        time.sleep(0.01)  # ensure a distinct mtime_ns
        leader.add_bar("AAAUSDT", _ws_bar(base + 4 * MS_PER_HOUR, close=200.0), confirmed=True)
        leader.flush_to_disk()
        return rows

    follower._store.recover_from_disk = recover_then_leader_flushes_again  # type: ignore[assignment]

    follower._refresh()

    # _last_sig must equal the CURRENT on-disk signature (the post-read
    # generation), not a stale earlier one.
    current_sig = (snapshot_path.stat().st_mtime_ns, snapshot_path.stat().st_size)
    assert follower._last_sig == current_sig

    # And a follow-up refresh with no further leader writes is a no-op (the
    # recorded signature already matches the latest file -> no redundant re-read).
    follower._store.recover_from_disk = real_recover  # type: ignore[assignment]
    refreshes_before = follower._refreshes
    follower._refresh()
    assert follower._refreshes == refreshes_before


def test_follower_refresh_no_change_is_noop(tmp_path: Path) -> None:
    """ws-pool-6 guard: an unchanged snapshot is still a clean no-op (the
    re-stat-after-read change must not break the steady-state path)."""
    base = _hour_floor_now_ms() - 2 * MS_PER_HOUR
    leader = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for i in range(2):
        leader.add_bar("AAAUSDT", _ws_bar(base + i * MS_PER_HOUR, close=10.0 + i), confirmed=True)
    assert leader.flush_to_disk() > 0

    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    assert follower._refresh() is True  # first read merges
    refreshes = follower._refreshes
    assert follower._refresh() is False  # no change -> no-op
    assert follower._refreshes == refreshes


# --------------------------------------------------------------------------
# universe-pit-4 : stale-comment removal (doc-drift) — guard the live behaviour
# --------------------------------------------------------------------------


def test_build_demo_universe_comment_no_longer_references_erased_strategy() -> None:
    """universe-pit-4: the _build_demo_universe justification must not reference
    the erased SHORT strategy's prior7_liquidity_rank null-exclusion (it points
    at code removed 2026-06-11). The live age compensation is the continuous
    downstream gate; the comment must say so."""
    import inspect

    src = inspect.getsource(event_demo_data._build_demo_universe)
    assert "prior7_liquidity_rank" not in src.split("Historical note")[0]
    assert "_continuous_age_eligible_symbols" in src


def test_build_demo_universe_unlimited_drops_age_floor(monkeypatch) -> None:
    """universe-pit-4 guard: the actual behaviour the comment describes is
    unchanged — unlimited-universe mode (rank_end == max_symbols == 0) drops the
    local 30-day age floor (min_age_days=0) so the downstream continuous gate is
    authoritative; the legacy narrow-universe mode keeps the 30-day floor."""
    captured: list[int] = []

    def spy_build(instruments, tickers, *, universe_config, snapshot_ts_ms):
        del instruments, tickers, snapshot_ts_ms
        captured.append(universe_config.min_age_days)
        return pl.DataFrame()

    monkeypatch.setattr(event_demo_data, "build_current_universe_table", spy_build)
    empty = pl.DataFrame()

    unlimited = event_demo_data.EventDemoCycleConfig(
        universe_rank_end=0, universe_max_symbols=0,
    )
    event_demo_data._build_demo_universe(
        empty, empty, config=unlimited, snapshot_ts_ms=_hour_floor_now_ms(),
    )
    assert captured[-1] == 0  # age floor dropped in unlimited mode

    narrow = event_demo_data.EventDemoCycleConfig(
        universe_rank_end=200, universe_max_symbols=50,
    )
    event_demo_data._build_demo_universe(
        empty, empty, config=narrow, snapshot_ts_ms=_hour_floor_now_ms(),
    )
    assert captured[-1] == 30  # legacy floor preserved
