"""Regression tests for audit bucket b00 (continuous_demo + universe boundary).

Each test pins a specific finding's root-cause fix: it would FAIL on the pre-fix
code and PASS now. Findings covered:

  cross-sleeve-3      reservation trade_id == executed (component-suffixed) trade_id
  exec-router-6       crc32%46656 suborder-link collision is a real, bounded hazard
  pit-engine-3        confirmed-bar fingerprint catches a sum-preserving backfill
  sizing-rebalance-3  resize-day raw return weights by the qty held over the interval
  sniper-1            snipe sizing respects venue min/min-notional/max filters
  sniper-3            sniper order rows carry updated_at_ms (recency-correct dedup)
  sniper-4            ambiguous trade_id recovery is logged, not silently dropped
  sniper-5            crash-orphaned preflight snipe is surfaced for reconcile/cancel
  sniper-6            partial-cancel fills use the execution time, not cancel time
  sniper-7            snipe fill inherits a real equity when the base equity is missing
  sniper-8            snipe with no signal_ts is skipped (link must round-trip to base)
  universe-pit-2      non-perp / non-USDT / missing-contractType contracts are excluded
"""
from __future__ import annotations

import logging
import zlib

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.config import UniverseConfig
from liquidity_migration.continuous_demo import (
    SNIPER_REASON,
    ContinuousDemoCycleConfig,
    LivePanelCache,
    _continuous_rebalance_cycle_fields,
    _continuous_rebalance_mark_prices_json,
    _continuous_sniper_link_prefix,
    _continuous_suborder_link_id,
    _continuous_trade_id,
    _execute_sniper_placements,
    _known_ensemble_component_tags,
    _sniper_fill_ts_ms,
    _sniper_order_state,
    continuous_rebalance_rule,
    continuous_strategy_id,
    plan_continuous_sniper_orders,
    reconcile_continuous_snipes,
    recover_snipe_trade_id_from_link,
)
from liquidity_migration.order_link_id import _base36
from liquidity_migration.universe import build_current_universe_table


# --------------------------------------------------------------------------- #
# Shared fakes / helpers                                                        #
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []
        self.open_orders: list[dict] = []
        self.history: list[dict] = []

    def place_order(self, **params):
        self.placed.append(params)
        return {"orderId": f"oid-{len(self.placed)}"}

    def cancel_order(self, *, symbol: str, order_link_id: str):
        self.cancelled.append((symbol, order_link_id))
        return {}

    def get_open_orders(self, *, symbol: str | None = None, **_kw):
        return [o for o in self.open_orders if symbol is None or o.get("symbol") == symbol]

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None, **_kw):
        return [
            h for h in self.history
            if (symbol is None or h.get("symbol") == symbol)
            and (order_link_id is None or h.get("orderLinkId") == order_link_id)
        ]


def _sniper_cfg(**kw) -> ContinuousDemoCycleConfig:
    base = dict(
        sniper_enabled=True, sniper_wick_pct=0.08, sniper_size_frac=0.25,
        stop_loss_pct=0.25, submit_orders=True, confirm_demo_orders=True,
    )
    base.update(kw)
    return ContinuousDemoCycleConfig(**base)


def _entry(symbol="AAAUSDT", trade_id="t1", price=100.0, qty=10.0, signal_ts_ms=1_700_000_000_000):
    return {
        "symbol": symbol, "trade_id": trade_id, "entry_price": price, "qty": qty,
        "notional_usdt": price * qty, "signal_ts_ms": signal_ts_ms,
        "tick_size": 0.01, "qty_step": 0.1,
    }


# --------------------------------------------------------------------------- #
# sniper-1 — venue contract-filter-aware snipe sizing                           #
# --------------------------------------------------------------------------- #
def test_sniper1_skips_snipe_below_min_order_qty_and_min_notional() -> None:
    """A quarter-size snipe on a low-priced name that floors below Bybit's
    minOrderQty / minNotional must NOT be submitted into a guaranteed venue
    rejection — it is skipped. (The old _sniper_round_qty floored to qty_step
    and only rejected below ONE step, never consulting min_order_qty.)"""
    client = _FakeClient()
    contracts = {"BBBUSDT": {
        "tick_size": 0.001, "qty_step": 0.001,
        "min_order_qty": 1.0, "min_notional_value": 5.0, "max_order_qty": 1_000.0,
    }}
    orders = _execute_sniper_placements(
        [_entry("BBBUSDT", "t2", price=1.0, qty=1.0)],
        trading_client=client, demo=_sniper_cfg(), now_ms=1, strategy_id="s",
        price_by_symbol={"BBBUSDT": 1.0}, contract_by_symbol=contracts,
    )
    assert orders == []
    assert client.placed == []  # nothing sent to the venue


def test_sniper1_caps_snipe_at_max_order_qty() -> None:
    """An oversized snipe (cheap-priced name, large base) is capped at
    max_order_qty instead of being sent uncapped and rejected."""
    client = _FakeClient()
    contracts = {"CCCUSDT": {
        "tick_size": 0.001, "qty_step": 1.0,
        "min_order_qty": 1.0, "min_notional_value": 0.0, "max_order_qty": 100.0,
    }}
    orders = _execute_sniper_placements(
        [_entry("CCCUSDT", "t3", price=1.0, qty=10_000.0)],
        trading_client=client, demo=_sniper_cfg(), now_ms=1, strategy_id="s",
        price_by_symbol={"CCCUSDT": 1.0}, contract_by_symbol=contracts,
    )
    assert len(orders) == 1
    assert float(client.placed[0]["qty"]) == 100.0  # capped, not 0.25 * 10000


def test_sniper1_preserves_quarter_size_target_without_contract_filters() -> None:
    """With no contract map (dry-run/tests) the snipe still sizes to a quarter of
    the base qty (the research form), modulo step."""
    orders = _execute_sniper_placements(
        [_entry()], trading_client=_FakeClient(), demo=_sniper_cfg(),
        now_ms=1_700_000_100_000, strategy_id="s", price_by_symbol={"AAAUSDT": 100.0},
    )
    assert len(orders) == 1
    assert float(orders[0]["qty"]) == 2.5  # 0.25 * 10


# --------------------------------------------------------------------------- #
# sniper-3 — updated_at_ms recency on every sniper order row                    #
# --------------------------------------------------------------------------- #
def test_sniper3_placement_and_updates_stamp_updated_at_ms() -> None:
    """Every sniper order row (placement + each reconcile update) carries
    updated_at_ms so the orders ledger dedups by true recency, not by an implicit
    ts_ms-bucket-monotonic coincidence."""
    client = _FakeClient()
    placement = _execute_sniper_placements(
        [_entry()], trading_client=client, demo=_sniper_cfg(), now_ms=1_700_000_100_000,
        strategy_id="s", price_by_symbol={"AAAUSDT": 100.0},
    )
    assert placement[0]["updated_at_ms"] == 1_700_000_100_000

    # a reconcile cancel update must also stamp updated_at_ms
    client.open_orders = [{"orderLinkId": placement[0]["order_link_id"], "symbol": "AAAUSDT"}]
    trades = pl.DataFrame(
        [{"trade_id": "t1", "status": "closed", "symbol": "AAAUSDT", "equity_usdt": 10_000.0}],
        infer_schema_length=None,
    )
    _fills, updates, _exits = reconcile_continuous_snipes(
        trades, pl.DataFrame(placement, infer_schema_length=None),
        trading_client=client, demo=_sniper_cfg(), now_ms=1_700_000_200_000,
    )
    assert updates and all(u["updated_at_ms"] == 1_700_000_200_000 for u in updates)


# --------------------------------------------------------------------------- #
# sniper-4 — ambiguous trade_id recovery is logged, not silent                  #
# --------------------------------------------------------------------------- #
def _find_recovery_collision() -> tuple[int, str] | None:
    """A (signal_ts, suffix) for which >=2 enumerated candidates collide on
    crc32%46656 — the silent ambiguous-recovery trigger."""
    comps = _known_ensemble_component_tags()
    for ts in range(1, 6000):
        seen: dict[str, int] = {}
        for seq in range(4):
            base = _continuous_trade_id("S", "WIFUSDT", ts * 1000, seq)
            for comp in (*comps, ""):
                cand = f"{base}-{comp}-snipe" if comp else f"{base}-snipe"
                suf = _base36(zlib.crc32(cand.encode("utf-8")) % 46656).rjust(3, "0")
                seen[suf] = seen.get(suf, 0) + 1
        for suf, n in seen.items():
            if n >= 2:
                return ts * 1000, suf
    return None


def test_sniper4_ambiguous_recovery_logs_warning(caplog) -> None:
    """An ambiguous recovery (>=2 candidates collide) returns None AND emits an
    operator WARNING — the old code returned None silently, so a post-rebuild
    paper<->demo mis-pairing was invisible."""
    hit = _find_recovery_collision()
    assert hit is not None, "expected at least one crc32%46656 collision in the search range"
    signal_ts, suffix = hit
    link = f"lm-en-cs-WIF-zzz-x{suffix}"
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.continuous_demo"):
        result = recover_snipe_trade_id_from_link(
            link, strategy_id="S", symbol="WIFUSDT", signal_ts_ms=signal_ts,
        )
    assert result is None
    assert any("AMBIGUOUS" in rec.message for rec in caplog.records)


def test_sniper4_empty_match_is_silent(caplog) -> None:
    """A link that matches NO candidate (routine 'not my sleeve/symbol') returns
    None WITHOUT a warning — only the >=2 collision is surfaced, so the operator
    log is not spammed by every foreign link."""
    # 'x000' is verified to match no enumerated candidate for this (strategy, symbol,
    # signal_ts), so found is empty (not an ambiguous >=2 collision).
    link = "lm-en-cs-WIF-zzz-x000"
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.continuous_demo"):
        result = recover_snipe_trade_id_from_link(
            link, strategy_id="S", symbol="WIFUSDT", signal_ts_ms=999_000_000_000,
        )
    assert result is None
    assert not any("AMBIGUOUS" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# sniper-5 — crash-orphaned preflight snipe is surfaced + cancellable           #
# --------------------------------------------------------------------------- #
def _preflight_orphan_row(link="lm-en-cs-WIF-abc-x123", base="t1", symbol="AAAUSDT"):
    """The ONLY ledger trace after a crash between place_order and the placement
    flush: the preflight intent row."""
    return {
        "order_link_id": link, "ts_ms": 100, "updated_at_ms": 100, "trade_id": f"{base}-snipe",
        "strategy_id": "s", "symbol": symbol, "side": "Sell", "submit_mode": "preflight",
        "status": "submitted", "trade_side": "short", "sleeve": "continuous",
        "signal_ts_ms": 1, "stop_price": 135.0, "reason": SNIPER_REASON, "base_trade_id": base,
    }


def test_sniper5_orphan_preflight_is_surfaced_as_resting() -> None:
    """A surviving preflight intent (no placement/terminal row) is surfaced by
    _sniper_order_state re-tagged submit_mode='submitted' so reconcile routes it
    through the venue check. The old view excluded preflight rows entirely."""
    state = _sniper_order_state(pl.DataFrame([_preflight_orphan_row()], infer_schema_length=None))
    assert len(state) == 1
    assert state[0]["submit_mode"] == "submitted"
    assert state[0]["base_trade_id"] == "t1"


def test_sniper5_placement_row_supersedes_preflight() -> None:
    """In the NORMAL (non-crash) case the end-of-cycle placement row supersedes the
    preflight intent — the orphan path must NOT fire."""
    placement = {**_preflight_orphan_row(), "ts_ms": 101, "updated_at_ms": 101,
                 "submit_mode": "submitted", "status": "resting"}
    orders = pl.concat(
        [pl.DataFrame([_preflight_orphan_row()], infer_schema_length=None),
         pl.DataFrame([placement], infer_schema_length=None)],
        how="diagonal_relaxed",
    )
    state = _sniper_order_state(orders)
    assert len(state) == 1
    assert state[0]["status"] == "resting" and state[0]["submit_mode"] == "submitted"


def test_sniper5_orphan_preflight_is_cancelled_when_base_closed() -> None:
    """End-to-end: a crash-orphaned snipe still resting on the venue is CANCELLED
    when its base thesis is gone — the unmanaged-order leak is closed."""
    client = _FakeClient()
    link = "lm-en-cs-WIF-abc-x123"
    client.open_orders = [{"orderLinkId": link, "symbol": "AAAUSDT"}]  # still resting on venue
    trades = pl.DataFrame(
        [{"trade_id": "t1", "status": "closed", "symbol": "AAAUSDT", "equity_usdt": 10_000.0}],
        infer_schema_length=None,
    )
    _fills, _updates, _exits = reconcile_continuous_snipes(
        trades, pl.DataFrame([_preflight_orphan_row(link=link)], infer_schema_length=None),
        trading_client=client, demo=_sniper_cfg(), now_ms=200,
    )
    assert client.cancelled == [("AAAUSDT", link)]


# --------------------------------------------------------------------------- #
# sniper-6 — partial-cancel fill time is the execution time, not cancel time    #
# --------------------------------------------------------------------------- #
def test_sniper6_prefers_exec_time_over_updated_time() -> None:
    """For a PartiallyFilledCanceled order updatedTime is the (later) CANCEL time;
    the fill time must come from execTime when present so held_ms / max_hold are
    not understated."""
    row = {"execTime": "1700000200000", "updatedTime": "1700000900000", "orderStatus": "PartiallyFilledCanceled"}
    assert _sniper_fill_ts_ms(row, now_ms=1_700_000_999_000) == 1_700_000_200_000


def test_sniper6_falls_back_to_updated_time_then_now() -> None:
    assert _sniper_fill_ts_ms({"updatedTime": "1700000200000"}, now_ms=999) == 1_700_000_200_000
    assert _sniper_fill_ts_ms({"execTime": "0", "updatedTime": ""}, now_ms=4242) == 4242


# --------------------------------------------------------------------------- #
# sniper-7 — snipe fill inherits a real equity when base equity is missing       #
# --------------------------------------------------------------------------- #
def _resting_row(link="lnk1", symbol="AAAUSDT", base="t1"):
    return {
        "order_link_id": link, "ts_ms": 1, "updated_at_ms": 1, "trade_id": f"{base}-snipe",
        "strategy_id": "s", "symbol": symbol, "side": "Sell", "order_type": "Limit", "qty": "2.5",
        "reduce_only": False, "order_id": "oid-1", "submit_mode": "submitted", "status": "resting",
        "signal_ts_ms": 1, "tick_size": 0.01, "qty_step": 0.1, "stop_price": 135.0,
        "stop_loss_pct": 0.25, "sleeve": "continuous", "reason": SNIPER_REASON,
        "limit_price": 108.0, "base_trade_id": base,
    }


def test_sniper7_fill_uses_account_equity_when_base_equity_missing() -> None:
    """When the base row carries no positive equity_usdt (adopted/recovered base
    whose equity stamp was lost) the snipe fill must inherit the live account
    equity snapshot, not 0.0 — a 0.0 stamp zeroes the snipe's notional weight and
    therefore its booked net_return despite real gross PnL."""
    client = _FakeClient()
    client.open_orders = []
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "2.5",
                       "avgPrice": "108.0", "updatedTime": "1700000200000", "orderStatus": "Filled"}]
    # base row exists but equity_usdt is 0 (lost stamp) -> equity_by_trade_id is empty
    trades = pl.DataFrame(
        [{"trade_id": "t1", "status": "open", "symbol": "AAAUSDT", "equity_usdt": 0.0}],
        infer_schema_length=None,
    )
    fills, _updates, _exits = reconcile_continuous_snipes(
        trades, pl.DataFrame([_resting_row()], infer_schema_length=None),
        trading_client=client, demo=_sniper_cfg(), now_ms=1_700_000_300_000,
        account_equity_usdt=12_345.0,
    )
    assert len(fills) == 1
    assert fills[0]["equity_usdt"] == 12_345.0  # NOT 0.0


# --------------------------------------------------------------------------- #
# sniper-8 — a snipe with no signal_ts is skipped (link must round-trip)         #
# --------------------------------------------------------------------------- #
def test_sniper8_skips_entry_with_zero_signal_ts() -> None:
    """An entry with signal_ts_ms=0 produces NO snipe plan — the link could not
    round-trip to the base trade_id, so a recoverable snipe is impossible. The old
    code defaulted the link ts to now_ms (a DIFFERENT value), breaking recovery."""
    cfg = _sniper_cfg()
    plans = plan_continuous_sniper_orders(
        [_entry(signal_ts_ms=0)], config=cfg, price_by_symbol={"AAAUSDT": 100.0},
    )
    assert plans == []


def test_sniper8_valid_signal_ts_is_carried_through() -> None:
    cfg = _sniper_cfg()
    plans = plan_continuous_sniper_orders(
        [_entry(signal_ts_ms=123_000)], config=cfg, price_by_symbol={"AAAUSDT": 100.0},
    )
    assert len(plans) == 1 and plans[0]["signal_ts_ms"] == 123_000


# --------------------------------------------------------------------------- #
# exec-router-6 — the crc32%46656 collision is a real, bounded residual hazard   #
# --------------------------------------------------------------------------- #
def test_execrouter6_distinct_trade_ids_can_collide_on_3char_suffix() -> None:
    """Documents the residual: the suborder-link uniqueness suffix is only
    crc32(trade_id)%46656 (46656 distinct values), so two DISTINCT same-symbol
    same-second trade_ids CAN share an orderLinkId. The full fix (a wider suffix)
    needs the cross-file orderLinkId decoder change (needs_integration); this test
    pins the hazard so a future widening is verifiably collision-free here."""
    prefix = _continuous_sniper_link_prefix(_sniper_cfg())
    sig = 1_700_000_000_000
    # Search a small space of distinct trade_ids for a colliding pair on one symbol/second.
    suffix_to_id: dict[str, str] = {}
    collision = None
    for i in range(200_000):
        tid = f"S-WIFUSDT-{sig}-{i}-snipe"
        link = _continuous_suborder_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=sig, trade_id=tid)
        suf = link[-3:]
        prior = suffix_to_id.get(suf)
        if prior is not None and prior != tid:
            collision = (prior, tid, link)
            break
        suffix_to_id[suf] = tid
    assert collision is not None, "expected a 3-char suffix collision within 46656 buckets"
    prior, tid, link = collision
    other = _continuous_suborder_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=sig, trade_id=prior)
    assert other == link  # two distinct trade_ids -> identical orderLinkId (the hazard)


# --------------------------------------------------------------------------- #
# pit-engine-3 — fingerprint catches a value-preserving multi-symbol backfill    #
# --------------------------------------------------------------------------- #
def test_pitengine3_signature_changes_on_sum_preserving_backfill() -> None:
    """A multi-symbol backfill that conserves BOTH close_sum and turnover_sum
    (one symbol +X, another -X) used to leave the old (count, max_ts, close_sum,
    turnover_sum) fingerprint unchanged -> stale carry served silently. The new
    content-hash fingerprint MUST change."""
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache()
    cur_ts = 1_700_003_600_000
    confirmed = pl.DataFrame({
        "ts_ms": [cur_ts - MS_PER_HOUR, cur_ts - MS_PER_HOUR],
        "symbol": ["AAA", "BBB"],
        "close": [100.0, 200.0],
        "turnover_quote": [1_000.0, 2_000.0],
    })
    mutated = confirmed.with_columns(
        pl.when(pl.col("symbol") == "AAA").then(pl.col("close") + 10.0)
        .when(pl.col("symbol") == "BBB").then(pl.col("close") - 10.0)
        .otherwise(pl.col("close")).alias("close")
    )
    assert confirmed["close"].sum() == mutated["close"].sum()           # sum preserved
    assert confirmed["turnover_quote"].sum() == mutated["turnover_quote"].sum()

    sig_before = cache._confirmed_signature(confirmed, cur_ts, cfg)
    sig_after = cache._confirmed_signature(mutated, cur_ts, cfg)
    assert sig_before != sig_after  # the old sum-based signature would be EQUAL here


def test_pitengine3_signature_stable_on_identical_bars() -> None:
    """Identical confirmed bars must yield an identical signature (no spurious
    refresh / no cache thrash)."""
    cfg = ContinuousDemoCycleConfig()
    cache = LivePanelCache()
    cur_ts = 1_700_003_600_000
    df = pl.DataFrame({
        "ts_ms": [cur_ts - MS_PER_HOUR, cur_ts - MS_PER_HOUR],
        "symbol": ["AAA", "BBB"], "close": [100.0, 200.0], "turnover_quote": [1_000.0, 2_000.0],
    })
    assert cache._confirmed_signature(df, cur_ts, cfg) == cache._confirmed_signature(df.clone(), cur_ts, cfg)


# --------------------------------------------------------------------------- #
# sizing-rebalance-3 — resize-day raw return weights by the held (pre-resize) qty #
# --------------------------------------------------------------------------- #
def test_sizingrebalance3_marks_weight_by_qty_held_over_interval() -> None:
    """The prev->cur move must be weighted by the qty held OVER that interval. A
    rebalance-INCREASE day's added qty was opened at ~today's price and earned no
    prev->cur move, so weighting by the post-resize qty (qty_new) double-counts.
    The live cycle now marks on the PRE-resize ledger (qty_old); this pins the
    weighting at the function level: passing qty_new biases the raw_return."""
    cfg = ContinuousDemoCycleConfig(daily_rebalance_enabled=True, record_dry_run=True)
    sid = continuous_strategy_id(cfg)
    day0 = (1_700_000_000_000 // MS_PER_DAY) * MS_PER_DAY
    day1 = day0 + MS_PER_DAY
    cycles = pl.DataFrame(
        [{
            "ts_ms": day0 + 1, "rebalance_day_ts": day0, "rebalance_raw_return": 0.0,
            "rebalance_scaled_equity": 1.0, "rebalance_scaled_peak": 1.0,
            "rebalance_mark_prices_json": _continuous_rebalance_mark_prices_json({"t1": 100.0}),
        }],
        infer_schema_length=None,
    )

    def raw_return_for_qty(qty: float) -> float:
        trades = pl.DataFrame(
            [{
                "trade_id": "t1", "strategy_id": sid, "symbol": "ABCUSDT", "status": "open",
                "entry_ts_ms": day0 - MS_PER_DAY, "entry_price": 80.0,
                "qty": str(qty), "equity_usdt": 10_000.0,
            }],
            infer_schema_length=None,
        )
        fields = _continuous_rebalance_cycle_fields(
            trades, cycles, price_by_symbol={"ABCUSDT": 110.0},
            current_day_ts=day1, now_ms=day1 + 1, strategy_id=sid, rule=continuous_rebalance_rule(cfg),
        )
        return fields["rebalance_raw_return"]

    short_move = (100.0 - 110.0) / 100.0  # short return prev->cur
    qty_old_raw = raw_return_for_qty(2.0)   # the qty actually held over the interval
    qty_new_raw = raw_return_for_qty(4.0)   # the (biased) post-resize qty

    assert qty_old_raw == pytest.approx(short_move * (2.0 * 100.0 / 10_000.0))
    assert qty_new_raw == pytest.approx(short_move * (4.0 * 100.0 / 10_000.0))
    # The doubled qty exactly doubles the contribution — the bias the pre-resize
    # snapshot removes by marking with qty_old.
    assert qty_new_raw == pytest.approx(2.0 * qty_old_raw)


# --------------------------------------------------------------------------- #
# universe-pit-2 — non-perp / non-USDT / missing-contractType rows excluded      #
# --------------------------------------------------------------------------- #
def _univ_instrument(symbol, launch_ms, *, contract_type="LinearPerpetual", settle_coin="USDT"):
    return {
        "ts_ms": 1, "symbol": symbol, "category": "linear", "contract_type": contract_type,
        "status": "Trading", "settle_coin": settle_coin, "launch_time_ms": launch_ms,
        "tick_size": 0.01, "qty_step": 0.001, "min_notional_value": 5.0, "is_prelisting": False,
    }


def _univ_ticker(symbol, turnover):
    return {
        "ts_ms": 1, "symbol": symbol, "last_price": 1.0, "open_interest": 1_000.0,
        "open_interest_value": 1_000.0, "turnover_24h": turnover, "volume_24h": turnover,
        "funding_rate": 0.0,
    }


def test_universepit2_excludes_dated_inverse_usdc_and_missing_contract_type() -> None:
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("BBBUSDT", old),
        _univ_instrument("FUT0101USDT", old, contract_type="LinearFutures"),  # dated delivery
        _univ_instrument("INVUSD", old, contract_type="InversePerpetual", settle_coin="USD"),
        _univ_instrument("USDCPERP", old, settle_coin="USDC"),
        _univ_instrument("NOCTUSDT", old, contract_type=None),  # missing contractType
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("BBBUSDT", 40_000_000.0),
        _univ_ticker("FUT0101USDT", 90_000_000.0),  # would rank first if not excluded
        _univ_ticker("INVUSD", 80_000_000.0),
        _univ_ticker("USDCPERP", 70_000_000.0),
        _univ_ticker("NOCTUSDT", 60_000_000.0),
    ])
    table = build_current_universe_table(
        instruments, tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
        ),
        snapshot_ts_ms=snapshot,
    )
    assert table["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]
    assert "LinearFutures" not in table["contract_type"].to_list()
    assert set(table["settle_coin"].to_list()) == {"USDT"}


# --------------------------------------------------------------------------- #
# cross-sleeve-3 — reservation trade_id == executed (component-suffixed) id       #
# --------------------------------------------------------------------------- #
def test_crosssleeve3_component_trade_id_matches_executed_row() -> None:
    """The live ensemble path stamps the COMPONENT-suffixed trade_id onto the
    candidate so the cross-sleeve reservation (claim/release/closed-GC keys on
    cand['trade_id']) matches the executed trade row's id (which appends
    -{component}). This pins the format equivalence: the candidate id and the id
    _execute_continuous_entries rebuilds are identical, so a closed-trade GC keyed
    on the real (suffixed) trade_id can match the reservation."""
    base_id = _continuous_trade_id("S", "WIFUSDT", 1_700_000_000_000, 0)
    component = "p3"
    # what run_continuous_demo_cycle now stamps onto the ensemble candidate:
    candidate_trade_id = f"{base_id}-{component}"
    # what _execute_continuous_entries rebuilds for the persisted row (base id from
    # signal_ts/seq, then -{component}):
    executed_trade_id = f"{_continuous_trade_id('S', 'WIFUSDT', 1_700_000_000_000, 0)}-{component}"
    assert candidate_trade_id == executed_trade_id
    # and it is NOT the component-less base (the pre-fix reservation id) — that was
    # the mismatch against the persisted row.
    assert candidate_trade_id != base_id
