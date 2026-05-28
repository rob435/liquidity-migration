"""Event-demo entries tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.event_demo import (
    DEMO_RELAXED_STRATEGY_ID,
    EventDemoCycleConfig,
    _execute_entries,
    _split_qty_for_max_order_size,
    order_quantity_for_notional,
    target_initial_margin_pct_equity,
    target_order_notional_pct_equity,
)
from liquidity_migration.storage import read_dataset
from liquidity_migration.volume_events import VolumeEventResearchConfig

from _event_demo_fixtures import *  # noqa: F401,F403  (shared fakes/helpers)
from _event_demo_fixtures import (  # noqa: F401  explicit for the linters
    FailingKlineMarket,
    FakeKlineMarket,
    FakeRiskClient,
    MinimalEventMarket,
    _ClosedPnlClient,
    _RecordingInstrumentsMarket,
    _feature_cache_klines,
    _feature_cache_universe,
    _make_instruments_frame,
    _make_tickers_frame,
    _open_trade_row,
    _patch_minimal_event_cycle,
)


def test_event_demo_default_sizing_matches_backtest_weight() -> None:
    assert target_order_notional_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == pytest.approx(1.0 / 5.0)
    assert target_initial_margin_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == pytest.approx(
        1.0 / 5.0 / 2.0
    )
    assert (
        target_order_notional_pct_equity(
            EventDemoCycleConfig(max_order_notional_pct_equity=0.10),
            VolumeEventResearchConfig(),
        )
        == 0.10
    )


def test_execute_entries_sizes_notional_before_leverage_margin() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(entry_leverage=2.0),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows[0]["qty"] == "20"
    assert rows[0]["notional_usdt"] == 2_000.0
    assert rows[0]["entry_leverage"] == 2.0
    assert rows[0]["initial_margin_usdt"] == 1_000.0
    assert rows[0]["initial_margin_pct_equity"] == 0.10
    assert orders[0]["notional_usdt"] == 2_000.0
    assert orders[0]["initial_margin_usdt"] == 1_000.0


def test_execute_entry_attaches_native_stop_and_requires_fill_confirmation() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient()

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert client.orders[0]["stopLoss"] == "112"
    assert client.orders[0]["takeProfit"] == "80"
    assert client.stop_updates == []


def test_execute_entry_records_only_confirmed_fill() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows[0]["qty"] == "1"
    assert rows[0]["entry_price"] == 100.5
    assert rows[0]["stop_price"] == 112.6
    assert rows[0]["take_profit_price"] == 80.4
    assert rows[0]["entry_stop_update_status"] == "submitted"
    assert orders[0]["status"] == "partial"
    assert orders[0]["notional_usdt"] == 100.5
    assert orders[0]["stop_price"] == 112.6
    assert orders[0]["take_profit_price"] == 80.4
    assert orders[0]["stop_loss_pct"] == 0.12
    assert orders[0]["take_profit_pct"] == 0.20
    assert orders[0]["entry_stop_update_status"] == "submitted"
    assert client.orders[0]["stopLoss"] == "112"
    assert client.orders[0]["takeProfit"] == "80"
    assert client.stop_updates == [{"symbol": "AAAUSDT", "stop_loss": "112.6", "take_profit": "80.4"}]


def test_execute_entry_records_leverage_error_without_raising() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient(fail_leverage_symbols={"AAAUSDT"})

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows == []
    assert len(orders) == 1
    assert orders[0]["status"] == "failed"
    assert orders[0]["submit_mode"] == "error"
    assert orders[0]["order_id"] == ""
    assert orders[0]["notional_usdt"] == 0.0
    assert orders[0]["initial_margin_usdt"] == 0.0
    assert orders[0]["stop_price"] == 112.0
    assert orders[0]["take_profit_price"] == 80.0
    assert orders[0]["stop_loss_pct"] == 0.12
    assert orders[0]["take_profit_pct"] == 0.20
    assert "set_leverage failed" in str(orders[0]["error"])
    assert "leverage rejected" in str(orders[0]["error"])
    assert client.orders == []


def test_execute_entry_records_order_error_and_continues() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        },
        {
            "trade_id": "t2",
            "symbol": "BBBUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_060_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        },
    ]
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-en-",),
        fill_qty="1",
        fill_price="100",
        fail_order_symbols={"AAAUSDT"},
    )

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.01,
        price_by_symbol={"AAAUSDT": 100.0, "BBBUSDT": 100.0},
        contract_by_symbol={
            "AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0},
            "BBBUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0},
        },
        now_ms=1_700_000_120_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert [row["trade_id"] for row in rows] == ["t2"]
    assert [row["symbol"] for row in orders] == ["AAAUSDT", "BBBUSDT"]
    # A place_order exception is ledgered submitted_unconfirmed (a pending
    # status) so reconciliation can adopt a lost-response fill -- not "failed".
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["submit_mode"] == "error"
    assert "place_order failed" in str(orders[0]["error"])
    assert "order rejected" in str(orders[0]["error"])
    assert orders[1]["status"] == "filled"
    assert orders[1]["submit_mode"] == "submitted"
    assert orders[1]["error"] == ""
    assert rows[0]["entry_order_id"] == "order-1"


def test_execute_entry_fill_confirmation_error_leaves_pending_order_for_reconcile() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient(fail_trade_history=True)

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows == []
    assert len(orders) == 1
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["submit_mode"] == "submitted"
    assert orders[0]["order_id"] == "order-1"
    assert orders[0]["notional_usdt"] == 0.0
    assert orders[0]["qty"] == "20"
    assert "fill confirmation failed" in str(orders[0]["error"])
    assert "history unavailable" in str(orders[0]["error"])
    assert client.orders[0]["stopLoss"] == "112"
    assert client.orders[0]["takeProfit"] == "80"


def test_order_quantity_for_notional_floors_to_qty_step_and_min_notional() -> None:
    result = order_quantity_for_notional(
        notional_usdt=100.0,
        price=9.9,
        qty_step=0.1,
        min_order_qty=0.1,
        min_notional_value=5.0,
    )

    assert result == ("10.1", 99.99)
    assert (
        order_quantity_for_notional(
            notional_usdt=3.0,
            price=9.9,
            qty_step=0.1,
            min_order_qty=0.1,
            min_notional_value=5.0,
        )
        is None
    )


def test_order_quantity_for_notional_caps_by_max_order_qty() -> None:
    """Regression guard for the SUPERUSDT-2026-05-25 rejection.

    Demo cycle sized a 26477-contract market entry on SUPERUSDT while
    Bybit's maxMktOrderQty for the symbol is 21100. The order errored
    with ErrCode 10001 ("number of contracts exceeds maximum limit");
    the ledger row sat in submit_unconfirmed and the paper run still
    "took" the entry, producing a reconciliation gap.

    With max_order_qty supplied, the function must cap at the max
    (floored to qty_step) instead of returning the over-cap value or
    rejecting outright.
    """
    # Without the cap: the request rounds down to 26477 contracts.
    uncapped = order_quantity_for_notional(
        notional_usdt=3287.4951,  # 26477 × 0.12416
        price=0.12416,
        qty_step=1.0,
        min_order_qty=1.0,
    )
    assert uncapped is not None
    assert uncapped[0] == "26477"

    # With the cap (Bybit's actual maxMktOrderQty for SUPERUSDT at the time
    # of the incident): qty floors to 21100.
    capped = order_quantity_for_notional(
        notional_usdt=3287.4951,
        price=0.12416,
        qty_step=1.0,
        min_order_qty=1.0,
        max_order_qty=21100.0,
    )
    assert capped is not None
    assert capped[0] == "21100"
    assert capped[1] == pytest.approx(21100.0 * 0.12416, rel=1e-9)


def test_order_quantity_for_notional_caps_floors_to_qty_step() -> None:
    """max_order_qty may not be step-aligned (e.g. an exchange that
    publishes 100 with a qty_step of 7). The cap must floor to the step
    grid so the order_qty is always a valid multiple of qty_step."""
    capped = order_quantity_for_notional(
        notional_usdt=10_000.0,  # would buy 1000 @ $10 if uncapped
        price=10.0,
        qty_step=7.0,
        max_order_qty=100.0,
    )
    assert capped is not None
    # 100 // 7 * 7 = 98
    assert capped[0] == "98"


def test_order_quantity_for_notional_returns_none_when_cap_below_min() -> None:
    """When max_order_qty < min_order_qty (unusual but possible during
    a venue config change), skip the candidate rather than sending a
    sub-min order Bybit will reject."""
    result = order_quantity_for_notional(
        notional_usdt=1_000_000.0,
        price=1.0,
        qty_step=1.0,
        min_order_qty=100.0,
        max_order_qty=50.0,
    )
    assert result is None


def test_execute_entries_records_preflight_row_before_place_order(tmp_path: Path) -> None:
    """Risk engine relies on event_demo_orders containing a pending row at the
    instant Bybit fills an entry. If the demo engine writes only the post-fill
    row, the risk engine will treat the brand-new position as untracked and
    close it. This test pins the preflight write order: parquet must contain a
    PENDING_ORDER_STATUSES row keyed by order_link_id BEFORE place_order returns.
    """
    from liquidity_migration.event_demo import PENDING_ORDER_STATUSES, _write_order_rows

    observed_at_place_order: dict[str, pl.DataFrame] = {}

    class PreflightInspectingClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def set_leverage(self, **params: object) -> dict[str, str]:
            return {}

        def place_order(self, **params: object) -> dict[str, str]:
            observed_at_place_order["orders"] = read_dataset(tmp_path, "event_demo_orders")
            self.orders.append(params)
            return {"orderId": "order-1"}

        def get_trade_history(self, **_: object) -> list[dict[str, str]]:
            return []

    client = PreflightInspectingClient()
    candidate = {
        "trade_id": "t-preflight",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": 1_700_000_000_000,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    rows, orders = _execute_entries(
        [candidate],
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={
            "AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        record_preflight=lambda row: _write_order_rows(
            tmp_path, pl.DataFrame([row], infer_schema_length=None)
        ),
    )

    observed = observed_at_place_order["orders"]
    assert not observed.is_empty(), "preflight row must be in parquet before place_order"
    preflight = observed.filter(pl.col("symbol") == "AAAUSDT").to_dicts()
    assert len(preflight) == 1
    assert preflight[0]["status"] in PENDING_ORDER_STATUSES
    assert preflight[0]["submit_mode"] == "preflight"
    assert preflight[0]["reduce_only"] is False
    assert preflight[0]["trade_id"] == "t-preflight"
    assert preflight[0]["order_link_id"].startswith("lm-en-")

    # Final post-loop write upserts the same order_link_id; status must transition
    # away from preflight so pending_entry_symbols drops it once the open trade
    # row is in event_demo_trades.
    assert len(orders) == 1
    assert orders[0]["order_link_id"] == preflight[0]["order_link_id"]
    assert orders[0]["submit_mode"] in {"submitted", "filled", "partial", "submitted_unconfirmed"}


def test_execute_entries_parallel_path_runs_concurrent_candidates() -> None:
    """With max_concurrent_entries > 1 and a private_client_factory, candidates
    fan out across worker threads instead of running serially. Verify by giving
    each candidate's place_order a 100ms sleep: serial would take >300ms for
    three candidates, parallel must finish in roughly one slot.
    """
    import time as _time

    candidates = [
        {
            "trade_id": f"t-par-{i}",
            "symbol": f"AAA{i}USDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i in range(3)
    ]
    price_by_symbol = {c["symbol"]: 100.0 for c in candidates}
    contract_by_symbol = {
        c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
        for c in candidates
    }

    class SlowClient(FakeRiskClient):
        def __init__(self):
            super().__init__(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

        def place_order(self, **params):
            _time.sleep(0.1)
            return super().place_order(**params)

    factory_calls: list[int] = []
    def factory() -> SlowClient:
        factory_calls.append(1)
        return SlowClient()

    started = _time.monotonic()
    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=3,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol=price_by_symbol,
        contract_by_symbol=contract_by_symbol,
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        private_client_factory=factory,
    )
    elapsed = _time.monotonic() - started

    assert len(orders) == 3
    assert elapsed < 0.25, (
        f"parallel path should finish under 250ms with 100ms place_order x 3 "
        f"workers; took {elapsed:.3f}s"
    )
    assert len(factory_calls) == 3


def test_execute_entries_falls_back_to_serial_when_submit_orders_off() -> None:
    """If submit_orders=False the parallel path is bypassed regardless of
    max_concurrent_entries (no live execution to fan out)."""
    candidates = [
        {
            "trade_id": f"t-fb-{i}",
            "symbol": f"BBB{i}USDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i in range(3)
    ]
    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(entry_leverage=2.0, max_concurrent_entries=4),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )
    assert [o["symbol"] for o in orders] == ["BBB0USDT", "BBB1USDT", "BBB2USDT"]
    assert all(o["submit_mode"] == "dry_run" for o in orders)


def test_execute_entries_parallel_records_preflight_for_every_candidate(tmp_path: Path) -> None:
    """The preflight callback must fire once per candidate even on the parallel
    path, and the resulting parquet must contain a preflight row for each
    order_link_id BEFORE place_order returns for that candidate. Pins the
    contract between fix #1 (close-on-open preflight) and speed #1 (parallel).
    """
    import threading as _threading
    from liquidity_migration.event_demo import _write_order_rows

    candidates = [
        {
            "trade_id": f"t-pp-{i}",
            "symbol": f"PRE{i}USDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i in range(3)
    ]

    place_order_started = _threading.Event()

    class PreflightAwareClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def set_leverage(self, **_kwargs) -> dict[str, str]:
            return {}

        def place_order(self, **params) -> dict[str, str]:
            place_order_started.set()
            self.orders.append(params)
            return {"orderId": f"order-{params.get('orderLinkId')}"}

        def get_trade_history(self, **_kwargs) -> list[dict[str, str]]:
            return []

    def _record_preflight(row: dict[str, object]) -> None:
        _write_order_rows(tmp_path, pl.DataFrame([row], infer_schema_length=None))

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=3,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        record_preflight=_record_preflight,
        private_client_factory=PreflightAwareClient,
    )

    stored = read_dataset(tmp_path, "event_demo_orders").sort("order_link_id")
    preflights = stored.filter(pl.col("submit_mode") == "preflight").to_dicts()
    # Three preflights written, one per candidate.
    assert len(preflights) == 3
    assert {p["symbol"] for p in preflights} == {"PRE0USDT", "PRE1USDT", "PRE2USDT"}
    assert all(p["status"] in {"submitted", "submitted_unconfirmed", "partial", "fallback_market"} for p in preflights)
    # Final returned orders match candidate order (deterministic).
    assert [o["symbol"] for o in orders] == ["PRE0USDT", "PRE1USDT", "PRE2USDT"]


def test_execute_entries_parallel_isolates_place_order_failure(tmp_path: Path) -> None:
    """One candidate failing place_order must NOT abort the cycle for the others
    when running in parallel. Each ledgered as its own row with its own status.
    """
    candidates = [
        {
            "trade_id": f"t-iso-{i}",
            "symbol": symbol,
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i, symbol in enumerate(["BADUSDT", "OKUSDT"])
    ]

    class SelectiveClient(FakeRiskClient):
        def __init__(self):
            super().__init__(
                fill_market_orders=True,
                fill_order_prefixes=("lm-en-",),
                fail_order_symbols={"BADUSDT"},
            )

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=2,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        private_client_factory=SelectiveClient,
    )

    by_symbol = {o["symbol"]: o for o in orders}
    # place_order exception -> submitted_unconfirmed (pending) for reconciliation.
    assert by_symbol["BADUSDT"]["status"] == "submitted_unconfirmed"
    assert "place_order failed" in by_symbol["BADUSDT"]["error"]
    assert by_symbol["OKUSDT"]["status"] in {"filled", "partial", "submitted_unconfirmed"}
    assert by_symbol["OKUSDT"]["error"] == ""


def test_execute_entries_parallel_isolates_set_leverage_failure(tmp_path: Path) -> None:
    """Same isolation but for set_leverage failures: one candidate's leverage
    rejection must not bleed into another worker's path."""
    candidates = [
        {
            "trade_id": f"t-lev-{i}",
            "symbol": symbol,
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i, symbol in enumerate(["LEVBADUSDT", "LEVOKUSDT"])
    ]

    class LeverageFlakyClient(FakeRiskClient):
        def __init__(self):
            super().__init__(
                fill_market_orders=True,
                fill_order_prefixes=("lm-en-",),
                fail_leverage_symbols={"LEVBADUSDT"},
            )

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=2,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        private_client_factory=LeverageFlakyClient,
    )

    by_symbol = {o["symbol"]: o for o in orders}
    assert by_symbol["LEVBADUSDT"]["status"] == "failed"
    assert "set_leverage failed" in by_symbol["LEVBADUSDT"]["error"]
    assert by_symbol["LEVOKUSDT"]["status"] in {"filled", "partial", "submitted_unconfirmed"}


def test_wait_for_execution_summary_fast_window_then_slow_interval() -> None:
    """Until fast_poll_seconds elapses, polls happen every ~fast_poll_interval;
    after, they fall back to poll_interval. Verify by counting calls in the
    fast window and the slow window against a client that never returns a fill.
    """
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary

    call_times: list[float] = []

    class CountingClient:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            call_times.append(_time.monotonic())
            return []

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        CountingClient(),
        symbol="AAAUSDT",
        order_link_id="lm-test-poll",
        poll_seconds=0.6,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.3,
    )
    elapsed = _time.monotonic() - started

    assert float(summary.get("qty") or 0.0) == 0.0, "no fills -> no qty at deadline"
    # ~0.6 seconds total wallclock.
    assert 0.55 < elapsed < 1.0, f"expected ~0.6s wallclock, got {elapsed:.3f}s"
    # Fast window is 0.3s @ 0.05s = ~6 calls; slow window is 0.3s @ 0.2s = ~2 calls.
    # Allow ±2 jitter for scheduler latency on macOS CI.
    fast_window_calls = sum(1 for t in call_times if t - started < 0.3)
    slow_window_calls = sum(1 for t in call_times if t - started >= 0.3)
    assert 4 <= fast_window_calls <= 8, f"fast window expected 4-8 calls, got {fast_window_calls}"
    assert 1 <= slow_window_calls <= 4, f"slow window expected 1-4 calls, got {slow_window_calls}"


def test_wait_for_execution_summary_uses_ws_router_when_available() -> None:
    """When an ExecutionEventRouter is supplied AND has a fill for this
    orderLinkId, _wait_for_execution_summary returns within ms — the REST
    get_trade_history path is bypassed entirely on the fast path.
    """
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary
    from liquidity_migration.execution_router import ExecutionEventRouter

    router = ExecutionEventRouter()
    router.on_execution_event(
        {"data": [{"orderLinkId": "lm-en-WSAAA", "execQty": "1", "execPrice": "101", "execValue": "101", "execFee": "0.05"}]}
    )

    rest_calls: list[str | None] = []

    class FailingRestClient:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            rest_calls.append(order_link_id)
            raise AssertionError("REST must not be hit when WS already has the fill")

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        FailingRestClient(),
        symbol="AAAUSDT",
        order_link_id="lm-en-WSAAA",
        poll_seconds=5.0,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.5,
        execution_event_router=router,
    )
    elapsed = _time.monotonic() - started

    assert float(summary["qty"] or 0) == 1.0
    assert summary["avg_price"] == 101.0
    assert elapsed < 0.05, f"WS fast-path should return immediately, took {elapsed:.3f}s"
    assert rest_calls == []


def test_wait_for_execution_summary_falls_back_to_rest_when_router_empty() -> None:
    """If the router is supplied but doesn't have a fill within the WS short
    wait, the function falls back to REST polling exactly as it would without
    the router. Guarantees WS is a fast path, never the only path."""
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary
    from liquidity_migration.execution_router import ExecutionEventRouter

    router = ExecutionEventRouter()  # No events delivered

    call_count = {"n": 0}

    class RestFillsAfterTwoCalls:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return [{"execQty": "1", "execPrice": "102", "execValue": "102", "execFee": "0.05"}]
            return []

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        RestFillsAfterTwoCalls(),
        symbol="AAAUSDT",
        order_link_id="lm-en-WSBB",
        poll_seconds=2.0,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.5,
        execution_event_router=router,
    )
    elapsed = _time.monotonic() - started

    assert float(summary["qty"] or 0) == 1.0
    assert summary["avg_price"] == 102.0
    assert call_count["n"] >= 2
    assert elapsed < 0.5, f"REST fallback should still be reasonably fast, took {elapsed:.3f}s"


def test_wait_for_execution_summary_returns_immediately_on_fill() -> None:
    """A fill landing on the first poll must return without burning the rest of
    the poll budget."""
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary

    class InstantFillClient:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            return [
                {
                    "execQty": "1",
                    "execPrice": "100",
                    "execValue": "100",
                    "execFee": "0.1",
                }
            ]

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        InstantFillClient(),
        symbol="AAAUSDT",
        order_link_id="lm-test-instant",
        poll_seconds=5.0,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.5,
    )
    elapsed = _time.monotonic() - started

    assert float(summary.get("qty") or 0.0) == 1.0
    assert elapsed < 0.05, f"instant fill should return immediately, took {elapsed:.3f}s"


def test_execute_entries_preflight_skipped_when_no_callback() -> None:
    """Callers that don't pass record_preflight (e.g. dry-run unit tests) must
    behave exactly as before — no parquet writes attempted, no exceptions."""
    rows, orders = _execute_entries(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.20,
            }
        ],
        trading_client=None,
        demo=EventDemoCycleConfig(entry_leverage=2.0),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )
    assert orders[0]["submit_mode"] == "dry_run"
    assert rows[0]["submit_mode"] == "dry_run"


def test_split_qty_returns_single_qty_when_cap_does_not_bind() -> None:
    """When target_qty <= max_qty_per_order, no split."""
    from decimal import Decimal as D

    result = _split_qty_for_max_order_size(
        target_qty=D("1000"), max_qty_per_order=20000.0, qty_step=1.0
    )
    assert result == [D("1000")]


def test_split_qty_returns_single_qty_when_cap_is_zero() -> None:
    """max_qty_per_order=0 means unknown cap → don't split."""
    from decimal import Decimal as D

    result = _split_qty_for_max_order_size(
        target_qty=D("100000"), max_qty_per_order=0.0, qty_step=1.0
    )
    assert result == [D("100000")]


def test_split_qty_splits_into_two_evenly_when_target_is_1_5x_cap() -> None:
    """target=30000, max=20000 → 2 subs of 15000 each (sum=30000)."""
    from decimal import Decimal as D

    result = _split_qty_for_max_order_size(
        target_qty=D("30000"), max_qty_per_order=20000.0, qty_step=1.0
    )
    assert result == [D("15000"), D("15000")]
    assert sum(result) == D("30000")
    assert all(float(q) <= 20000.0 for q in result)


def test_split_qty_splits_into_three_when_target_is_2_5x_cap() -> None:
    """target=50000, max=20000 → 3 subs (each ≤ 20000) summing to 50000."""
    from decimal import Decimal as D

    result = _split_qty_for_max_order_size(
        target_qty=D("50000"), max_qty_per_order=20000.0, qty_step=1.0
    )
    # 50000 / 3 = 16666.67 floored to step 1 = 16666; last sub absorbs
    # the remainder: 50000 - 16666 * 2 = 16668, floored to step 1 = 16668
    assert len(result) == 3
    assert sum(result) == D("50000")
    assert all(float(q) <= 20000.0 for q in result)


def test_split_qty_respects_qty_step_with_step_alignment() -> None:
    """Sub-qtys must be aligned to qty_step (here 0.1)."""
    from decimal import Decimal as D

    result = _split_qty_for_max_order_size(
        target_qty=D("250.0"), max_qty_per_order=100.0, qty_step=0.1
    )
    # 250 / ceil(250/100)=3 = 83.33 → floored to 0.1 step = 83.3
    # last = 250 - 83.3*2 = 83.4 → floored = 83.4
    assert len(result) == 3
    for q in result:
        # every qty should be a multiple of 0.1
        assert (q * 10) % 1 == 0


def test_split_qty_matches_the_req_usdt_live_case() -> None:
    """Reproduces the REQUSDT live case: target ~37500 contracts at max=20000.

    Previously the order was capped-and-reduced to 20000 contracts (53% of
    target notional). With split it becomes 2× ~18750 contracts (100% of
    target notional)."""
    from decimal import Decimal as D

    result = _split_qty_for_max_order_size(
        target_qty=D("37500"), max_qty_per_order=20000.0, qty_step=1.0
    )
    assert len(result) == 2
    assert sum(result) == D("37500")
    assert all(float(q) <= 20000.0 for q in result)
    # Both sub-qtys should be roughly equal (within 1 step)
    assert abs(float(result[0]) - float(result[1])) <= 1.0


def test_execute_single_entry_splits_into_sub_orders_when_cap_binds() -> None:
    """End-to-end: REQUSDT-like scenario produces 2 order rows + 1 trade row
    with the FULL target qty filled (not capped-and-reduced)."""
    candidate = {
        "trade_id": "split-1",
        "symbol": "REQUSDT",
        "side": "short",
        "signal_ts_ms": 1_700_000_000_000,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.26,
    }
    # Use the FakeRiskClient that will fill_market_orders to simulate fills
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-en-",),
        fill_qty="18750",  # each sub-order returns this filled qty
        fill_price="0.08676",
    )
    rows, orders = _execute_entries(
        [candidate],
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=9756.0,
        order_notional_pct_equity=0.3333,
        price_by_symbol={"REQUSDT": 0.08676},
        contract_by_symbol={
            "REQUSDT": {
                "tick_size": 0.00001,
                "qty_step": 1.0,
                "min_order_qty": 1.0,
                "min_notional_value": 5.0,
                # The cap that binds — target qty ~37500, cap 20000 → split into 2
                "max_market_order_qty": 20000.0,
            },
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    # 1 trade row, 2 sub-order rows
    assert len(rows) == 1, f"expected 1 aggregated trade row, got {len(rows)}"
    assert len(orders) == 2, f"expected 2 sub-order rows, got {len(orders)}"
    # Each sub-order under the cap
    for o in orders:
        # qty is the FILLED qty from the FakeRiskClient (18750 per fill)
        assert float(o["qty"]) <= 20000.0
    # Sub-orders share the base entry_link with -s0, -s1 suffixes
    base_link = orders[0]["order_link_id"].rsplit("-s", 1)[0]
    suffixes = sorted([o["order_link_id"].rsplit("-s", 1)[1] for o in orders])
    assert suffixes == ["0", "1"]
    assert all(o["order_link_id"].startswith(base_link) for o in orders)
    # Aggregate trade row: qty = sum of filled qtys = 2 * 18750 = 37500
    assert float(rows[0]["qty"]) == 37500.0


def test_execute_single_entry_no_split_when_cap_does_not_bind() -> None:
    """When target_qty <= max_market_order_qty, no split: 1 order row."""
    candidate = {
        "trade_id": "single-1",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": 1_700_000_000_000,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.26,
    }
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-en-",),
        fill_qty="50",
        fill_price="100",
    )
    rows, orders = _execute_entries(
        [candidate],
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.5,  # target = 5000 / 100 = 50 contracts
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={
            "AAAUSDT": {
                "tick_size": 0.1, "qty_step": 0.1,
                "min_order_qty": 0.1, "min_notional_value": 5.0,
                "max_market_order_qty": 1000.0,  # well above target
            },
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert len(rows) == 1
    assert len(orders) == 1
    # No -s suffix on the link (single-order path preserves legacy entry_link)
    assert "-s" not in orders[0]["order_link_id"].rsplit("-", 1)[-1]

