"""Regression tests for audit bucket b10.

Each test would FAIL on the original bug and PASS on the fix. Covers:
  - pit-data-6           : funding/OI Binance fallback is suppressed on a Bybit root
  - storage-concurrency-2: a reused-pid stale singleton lock is evictable via token
  - storage-concurrency-4: orphaned `.*.tmp` part files are swept on the next write
  - storage-concurrency-5: the parent dir is fsync'd after the atomic rename
  - event-demo-core-2    : risk-exit keeps a no-price trade OPEN (no fabricated 0 close)
  - event-demo-core-3    : orphan-close aggregation is capped at the trade's own qty
  - ws-risk-3            : a qty<=0 ledger row never reduce-only-flattens the netted size
  - universe-pit-1       : contract_type is a HARD required column + unconditional filter
  - universe-pit-3       : symbols dropped by the instruments x tickers join are observable
  - universe-pit-5       : null-launch_time_ms symbols are surfaced (observability)
  - archive-integrity-4  : a truncated/short archive body is rejected, not written thin
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import archive as archive_module
from liquidity_migration import storage
from liquidity_migration.archive import (
    ArchiveDownloadIncompleteError,
    download_archive_bytes,
    download_public_trade_archive,
)
from liquidity_migration.config import UniverseConfig
from liquidity_migration.event_demo import (
    EventRiskCycleConfig,
    _execute_risk_exits,
    _orphan_close_pnl_from_records,
    plan_risk_exits,
)
from liquidity_migration.storage import (
    dataset_lock_path,
    exclusive_file_lock,
    read_dataset,
    resolve_dataset_name,
    write_dataset,
)
from liquidity_migration.universe import build_current_universe_table
from liquidity_migration._common import MS_PER_DAY


# --------------------------------------------------------------------------- #
# pit-data-6 — funding/OI Binance fallback gated on a single-venue root           #
# --------------------------------------------------------------------------- #
def test_pitdata6_binance_funding_fallback_suppressed_on_bybit_root(tmp_path: Path) -> None:
    """A Bybit root (native klines_1h present) that happens to ALSO carry a
    binance_usdm_funding/ dir but lacks a canonical funding/ dir must NOT silently
    serve BINANCE funding to a Bybit funding request — funding is a modeled cost,
    and the wrong venue's curve mis-signs returns. Before the fix resolve_dataset_name
    returned 'binance_usdm_funding' (cross-venue substitution); after, it fail-safes
    to the canonical 'funding' (empty read -> funding_mode missing)."""
    # Native Bybit kline marker -> this is a Bybit root.
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "close": [1.0]}),
        tmp_path,
        "klines_1h",
        partition_by=(),
    )
    # A stray Binance funding dir on the same (mixed) root.
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.0009]}),
        tmp_path,
        "binance_usdm_funding",
        partition_by=(),
    )

    # The fallback must be suppressed: a Bybit funding request stays canonical.
    assert resolve_dataset_name(tmp_path, "funding") == "funding"
    # No canonical funding/ dir -> empty read (NOT the Binance funding rows).
    assert read_dataset(tmp_path, "funding").is_empty()
    # Open interest is gated identically.
    assert resolve_dataset_name(tmp_path, "open_interest") == "open_interest"


def test_pitdata6_pure_binance_root_still_resolves_fallback(tmp_path: Path) -> None:
    """The intended pure-Binance per-venue root (NO Bybit-native kline marker)
    still transparently resolves canonical funding -> binance_usdm_funding."""
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "funding_rate": [0.0009]}),
        tmp_path,
        "binance_usdm_funding",
        partition_by=(),
    )
    # No klines_1h/ etc. -> unambiguously a Binance root -> fallback fires.
    assert resolve_dataset_name(tmp_path, "funding") == "binance_usdm_funding"
    assert read_dataset(tmp_path, "funding").height == 1


# --------------------------------------------------------------------------- #
# storage-concurrency-2 — reused-pid stale singleton lock is evictable            #
# --------------------------------------------------------------------------- #
def test_storageconcurrency2_reused_pid_stale_lock_is_dead(tmp_path: Path) -> None:
    """A crashed daemon leaves a VALID-JSON lock bearing its pid + token. If a
    fast restart reuses the SAME pid, the successor reads pid==os.getpid() and,
    on a stale_seconds=0 singleton lock, would wedge forever (the age-eviction
    and None-self-heal paths are both disabled). The per-acquisition token is the
    tiebreaker: a token NOT in this process's live-owned set means the lock is a
    predecessor that merely reused our pid -> the owner is dead. Before the fix
    _lock_owner_is_dead short-circuited to False (alive) on pid==getpid()."""
    lock_path = dataset_lock_path(tmp_path, "klines_1h")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Plant a readable, valid-JSON lock with OUR pid but a foreign token (a dead
    # predecessor that reused our pid). _read_lock_text_safe returns the real text
    # here (NOT monkeypatched), so the None self-heal path cannot fire.
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created": time.time(), "token": "deadbeef" * 4}),
        encoding="utf-8",
    )
    assert storage._lock_owner_is_dead(lock_path) is True

    # A live-owned token (one we currently hold) must still read as ALIVE.
    live_token = os.urandom(16).hex()
    storage._register_owned_token(live_token)
    try:
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "created": time.time(), "token": live_token}),
            encoding="utf-8",
        )
        assert storage._lock_owner_is_dead(lock_path) is False
    finally:
        storage._unregister_owned_token(live_token)

    # A legacy payload with no token field is conservatively treated as our own
    # live lock (unchanged behaviour).
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created": time.time()}), encoding="utf-8")
    assert storage._lock_owner_is_dead(lock_path) is False


def test_storageconcurrency2_acquire_recovers_over_reused_pid_lock(tmp_path: Path) -> None:
    """End-to-end: exclusive_file_lock with stale_seconds=0 (the singleton-guard
    config) must ACQUIRE over a reused-pid foreign-token lock instead of wedging.
    Run in a thread with a join timeout so a regression cannot hang the suite."""
    lock_path = dataset_lock_path(tmp_path, "funding")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "created": time.time(), "token": "f00dface" * 4}),
        encoding="utf-8",
    )

    acquired = threading.Event()

    def _acquire() -> None:
        with exclusive_file_lock(lock_path, stale_seconds=0, poll_seconds=0.0):
            acquired.set()

    worker = threading.Thread(target=_acquire, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert acquired.is_set(), "acquire wedged on a reused-pid stale singleton lock"
    # Lock is released (our acquisition owned it, foreign token replaced on write).
    assert not lock_path.exists()


# --------------------------------------------------------------------------- #
# storage-concurrency-4 — orphaned `.*.tmp` part files are swept                  #
# --------------------------------------------------------------------------- #
def test_storageconcurrency4_orphaned_tmp_swept_on_next_write(tmp_path: Path) -> None:
    """A SIGKILL between write_parquet and the atomic rename orphans a
    `.{name}.{pid}.{ns}.tmp` file (the finally unlink never runs). The next
    write_dataset must sweep stale `.*.tmp` files so a Restart=always daemon does
    not accumulate orphans until the disk fills. Before the fix there was no sweep
    anywhere, so the orphan survived every subsequent write."""
    dataset = "event_demo_orders"
    # Seed the dataset so its dir exists.
    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "order_id": ["a"]}),
        tmp_path,
        dataset,
        partition_by=(),
    )
    dataset_dir = tmp_path / dataset
    # Simulate an orphaned temp left by a crash mid-write, aged past the threshold.
    orphan = dataset_dir / f".part.parquet.{os.getpid()}.123456789.tmp"
    orphan.write_bytes(b"truncated parquet fragment")
    old = time.time() - storage._STALE_TMP_SECONDS - 60
    os.utime(orphan, (old, old))
    # A FRESH temp (in-flight) must be preserved by the age gate.
    fresh = dataset_dir / f".part.parquet.{os.getpid()}.987654321.tmp"
    fresh.write_bytes(b"in-flight")

    # The next write sweeps stale temps (it holds the dataset lock).
    write_dataset(
        pl.DataFrame({"ts_ms": [2], "symbol": ["ETHUSDT"], "order_id": ["b"]}),
        tmp_path,
        dataset,
        partition_by=(),
    )

    assert not orphan.exists(), "stale orphaned .tmp should have been swept"
    assert fresh.exists(), "a fresh in-flight .tmp must NOT be swept"
    # The real data is intact and readers never saw the orphan.
    got = read_dataset(tmp_path, dataset)
    assert set(got["order_id"].to_list()) == {"a", "b"}


# --------------------------------------------------------------------------- #
# storage-concurrency-5 — parent directory fsync after the rename                 #
# --------------------------------------------------------------------------- #
def test_storageconcurrency5_parent_dir_fsynced_after_rename(tmp_path: Path, monkeypatch) -> None:
    """_write_part must fsync the PARENT DIRECTORY fd after temp_path.replace(path),
    not just the temp file — otherwise the rename is not power-loss durable. Pin it
    by recording every os.fsync target and asserting a directory fd is fsync'd.
    Before the fix only the temp file was fsync'd."""
    import stat as stat_module

    fsynced_kinds: list[str] = []
    real_fsync = os.fsync
    real_fstat = os.fstat

    def recording_fsync(fd: int) -> None:
        try:
            mode = real_fstat(fd).st_mode
            fsynced_kinds.append("dir" if stat_module.S_ISDIR(mode) else "file")
        except OSError:
            fsynced_kinds.append("unknown")
        real_fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", recording_fsync)

    write_dataset(
        pl.DataFrame({"ts_ms": [1], "symbol": ["BTCUSDT"], "close": [1.0]}),
        tmp_path,
        "klines_1h",
        partition_by=(),
    )

    assert "file" in fsynced_kinds, "the temp part file must still be fsync'd"
    assert "dir" in fsynced_kinds, "the parent directory must be fsync'd after the rename"


# --------------------------------------------------------------------------- #
# event-demo-core-2 — risk-exit keeps a no-price trade OPEN                        #
# --------------------------------------------------------------------------- #
def test_eventdemocore2_risk_exit_keeps_open_when_no_exit_price() -> None:
    """A max_hold exit on a delisted/no-ticker symbol in the dry-run risk path has
    no resolvable exit price (planned 0.0, no current price). The sibling cycle path
    guards this (BUG-5) and keeps the trade OPEN; the risk path must do the same
    rather than book a fabricated exit_price=0 / 0% return. Before the fix the
    unguarded `if fully_filled:` closed the trade at price 0."""
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "DEADUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )
    rows, _orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "DEADUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 0.0,  # no planned price (delisted)
                "planned_exit_ts_ms": 1_700_000_000_000,
            }
        ],
        all_trades,
        trading_client=None,
        risk=EventRiskCycleConfig(submit_orders=False, record_dry_run=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={},  # no current price either
        tick_size_by_symbol={},
    )
    # No fabricated close: the trade is kept OPEN for retry.
    assert rows == [], "a no-price risk exit must NOT close the trade at price 0"


def test_eventdemocore2_risk_exit_closes_when_price_resolvable() -> None:
    """Control: when a price IS resolvable the dry-run risk path still closes the
    trade (the guard only blocks the price<=0 fabrication)."""
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 1_000.0,
                "equity_usdt": 10_000.0,
            }
        ]
    )
    rows, _orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 95.0,
                "planned_exit_ts_ms": 1_700_000_000_000,
            }
        ],
        all_trades,
        trading_client=None,
        risk=EventRiskCycleConfig(submit_orders=False, record_dry_run=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 95.0},
        tick_size_by_symbol={},
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "closed"
    assert rows[0]["exit_price"] == 95.0
    # short return = (100 - 95) / 100 = 0.05
    assert rows[0]["gross_trade_return"] == pytest.approx(0.05)


# --------------------------------------------------------------------------- #
# event-demo-core-3 — orphan-close aggregation capped at the trade's own qty      #
# --------------------------------------------------------------------------- #
def test_eventdemocore3_orphan_close_caps_sibling_surplus_legs() -> None:
    """On the SHARED netted account get_closed_pnl is account-wide. If a SIBLING
    sleeve also closed the same side on this symbol since entry_ts_ms, those legs
    are returned here too. The matcher must cap aggregation at THIS trade's qty so
    a sibling's surplus close cannot reprice this leg. Trade qty=1; the venue shows
    our leg (size 1 @ 95) followed by a sibling's leg (size 4 @ 80). The exit must
    price off ONLY our leg (95.0), not the blended (95*1 + 80*4)/5 = 83.0."""
    trade = {
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "qty": "1",
        "entry_price": 100.0,
        "entry_ts_ms": 1_700_000_000_000,
        "notional_usdt": 1_000.0,
        "equity_usdt": 10_000.0,
    }
    records = [
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "95.0", "closedSize": "1",
         "execFee": "0.1", "orderId": "ours", "createdTime": "1700000040000"},
        # Sibling's same-side surplus close AFTER entry_ts_ms — must be excluded.
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "80.0", "closedSize": "4",
         "execFee": "0.4", "orderId": "sibling", "createdTime": "1700000050000"},
    ]
    backfill = _orphan_close_pnl_from_records(trade, now_ms=1_700_000_100_000, records=records)
    assert backfill["exit_price"] == pytest.approx(95.0), "sibling surplus leg must not reprice this trade"
    # Only our leg's fee is counted, not the sibling's.
    assert backfill["exit_fee_usdt"] == pytest.approx(0.1)
    # gross return prices off our 95.0: (100 - 95)/100 = 0.05.
    assert backfill["gross_trade_return"] == pytest.approx(0.05)


def test_eventdemocore3_orphan_close_aggregates_own_multi_leg() -> None:
    """Control: this trade's OWN multi-leg close (legs summing to the trade qty)
    is still fully aggregated — the cap only excludes legs BEYOND the ledgered qty."""
    trade = {
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "qty": "4",
        "entry_price": 100.0,
        "entry_ts_ms": 1_700_000_000_000,
        "notional_usdt": 1_000.0,
        "equity_usdt": 10_000.0,
    }
    records = [
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "96.0", "closedSize": "3",
         "execFee": "0.3", "orderId": "leg-1", "createdTime": "1700000040000"},
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "94.0", "closedSize": "1",
         "execFee": "0.1", "orderId": "leg-2", "createdTime": "1700000050000"},
    ]
    backfill = _orphan_close_pnl_from_records(trade, now_ms=1_700_000_100_000, records=records)
    # qty-weighted across BOTH legs: (3*96 + 1*94)/4 = 95.5
    assert backfill["exit_price"] == pytest.approx(95.5)
    assert backfill["exit_fee_usdt"] == pytest.approx(0.4)
    assert backfill["orphan_close_legs"] == 2


# --------------------------------------------------------------------------- #
# ws-risk-3 — a qty<=0 ledger row never reduce-only-flattens the netted size      #
# --------------------------------------------------------------------------- #
def test_wsrisk3_zero_qty_row_does_not_emit_netted_exit() -> None:
    """A cap-mode (sibling-sleeve configured) open trade whose ledger qty parses to
    0 must NOT fall back to the raw netted position.size when a stop fires — that
    reduce-only would flatten the sibling's leg too (the non-self-healing
    cross-sleeve over-close). Before the fix the else branch preferred the netted
    size and emitted qty='10'; after, it skips entirely."""
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "0",  # schema drift / adopted-hedge / partially-cleared row
                "stop_price": 112.0,
            }
        ]
    )
    position_by_symbol = {
        "AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "10", "markPrice": "113"},
    }
    price_by_symbol = {"AAAUSDT": 113.0}
    capped = plan_risk_exits(
        open_trades,
        position_by_symbol=position_by_symbol,
        price_by_symbol=price_by_symbol,
        now_ms=1_700_000_060_000,
        cap_qty_to_trade=True,
    )
    assert capped == [], "a qty<=0 ledger row must not flatten the full netted position in cap mode"


def test_wsrisk3_positive_qty_still_caps_to_trade() -> None:
    """Control: a normal qty>0 cap-mode trade still caps at min(trade_qty,
    position_size) (unchanged behaviour)."""
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )
    position_by_symbol = {"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "10", "markPrice": "113"}}
    capped = plan_risk_exits(
        open_trades,
        position_by_symbol=position_by_symbol,
        price_by_symbol={"AAAUSDT": 113.0},
        now_ms=1_700_000_060_000,
        cap_qty_to_trade=True,
    )
    assert len(capped) == 1
    assert capped[0]["qty"] == "1"


# --------------------------------------------------------------------------- #
# universe-pit-1 — contract_type is a HARD required column + unconditional filter #
# --------------------------------------------------------------------------- #
def _univ_instrument(symbol, launch_ms, *, contract_type="LinearPerpetual", settle_coin="USDT", **extra):
    row = {
        "ts_ms": 1, "symbol": symbol, "category": "linear", "contract_type": contract_type,
        "status": "Trading", "settle_coin": settle_coin, "launch_time_ms": launch_ms,
        "tick_size": 0.01, "qty_step": 0.001, "min_notional_value": 5.0, "is_prelisting": False,
    }
    row.update(extra)
    return row


def _univ_ticker(symbol, turnover):
    return {
        "ts_ms": 1, "symbol": symbol, "last_price": 1.0, "open_interest": 1_000.0,
        "open_interest_value": 1_000.0, "turnover_24h": turnover, "volume_24h": turnover,
        "funding_rate": 0.0,
    }


def test_universepit1_missing_contract_type_column_raises() -> None:
    """contract_type is now a HARD required column. A frame missing it must raise
    RuntimeError instead of silently no-op'ing the only perpetuals-only barrier.
    Before the fix the perp filter was `if "contract_type" in columns` and a frame
    lacking the column passed dated-delivery futures straight through."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([_univ_instrument("AAAUSDT", old)]).drop("contract_type")
    tickers = pl.DataFrame([_univ_ticker("AAAUSDT", 50_000_000.0)])
    with pytest.raises(RuntimeError, match="contract_type"):
        build_current_universe_table(
            instruments, tickers,
            universe_config=UniverseConfig(min_turnover_24h=5_000_000.0, min_age_days=30),
            snapshot_ts_ms=snapshot,
        )


def test_universepit1_dated_delivery_excluded_by_delivery_time() -> None:
    """Defense-in-depth: a row that slips the contract_type allow-list but carries a
    non-null/non-zero delivery_time_ms (a dated delivery contract) is excluded."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old, delivery_time_ms=0),  # perp -> kept
        # contract_type passes the allow-list (label says perpetual) but it has a
        # real delivery time -> dated contract, must be excluded by the belt check.
        _univ_instrument("FUT1231USDT", old, contract_type="LinearPerpetual",
                         delivery_time_ms=snapshot + 30 * MS_PER_DAY),
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("FUT1231USDT", 90_000_000.0),  # would rank first if not excluded
    ])
    table = build_current_universe_table(
        instruments, tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
        ),
        snapshot_ts_ms=snapshot,
    )
    assert table["symbol"].to_list() == ["AAAUSDT"]


# --------------------------------------------------------------------------- #
# universe-pit-3 — dropped-join symbols are observable                            #
# --------------------------------------------------------------------------- #
def test_universepit3_join_drop_is_logged(caplog) -> None:
    """A symbol present in instruments but missing from the tickers snapshot is
    silently dropped by the inner join. The drop count must be surfaced (info log)
    so a partial/throttled get_tickers fetch is observable per cycle."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("BBBUSDT", old),  # no ticker row -> dropped by the join
    ])
    tickers = pl.DataFrame([_univ_ticker("AAAUSDT", 50_000_000.0)])
    with caplog.at_level("INFO", logger="liquidity_migration.universe"):
        table = build_current_universe_table(
            instruments, tickers,
            universe_config=UniverseConfig(
                min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
            ),
            snapshot_ts_ms=snapshot,
        )
    assert table["symbol"].to_list() == ["AAAUSDT"]
    assert any("dropped" in rec.getMessage() and "ticker" in rec.getMessage() for rec in caplog.records)


# --------------------------------------------------------------------------- #
# universe-pit-5 — null launch_time_ms symbols are surfaced                       #
# --------------------------------------------------------------------------- #
def test_universepit5_null_launch_time_surfaced_in_unlimited_mode(caplog) -> None:
    """In unlimited-universe mode (no age gate) a symbol with null launch_time_ms
    has a null listing_age_days and passes through silently. The count of such
    unknown-age symbols must be surfaced so the operator sees how many entered the
    candidate pool."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("NULLAGEUSDT", None),  # null launch_time_ms -> unknown age
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("NULLAGEUSDT", 40_000_000.0),
    ])
    with caplog.at_level("INFO", logger="liquidity_migration.universe"):
        table = build_current_universe_table(
            instruments, tickers,
            # unlimited mode: no age gate active.
            universe_config=UniverseConfig(
                min_turnover_24h=5_000_000.0, min_age_days=0, max_age_days=0,
                rank_start=1, rank_end=0, max_symbols=0,
            ),
            snapshot_ts_ms=snapshot,
        )
    # Null-age symbol passes through in unlimited mode (the residual exposure).
    assert "NULLAGEUSDT" in table["symbol"].to_list()
    assert any("null launch_time_ms" in rec.getMessage() for rec in caplog.records)


def test_universepit5_null_launch_time_dropped_when_age_gated() -> None:
    """With an active age floor, a null-launch (unknown-age) symbol is conservatively
    dropped (the is_not_null guard), as before — the fix only adds observability."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("NULLAGEUSDT", None),
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("NULLAGEUSDT", 40_000_000.0),
    ])
    table = build_current_universe_table(
        instruments, tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
        ),
        snapshot_ts_ms=snapshot,
    )
    assert table["symbol"].to_list() == ["AAAUSDT"]


# --------------------------------------------------------------------------- #
# archive-integrity-4 — a truncated/short archive body is rejected, not thin      #
# --------------------------------------------------------------------------- #
class _FakeHttpResponse:
    """Minimal urlopen() context-manager stand-in carrying a body and an optional
    Content-Length header, matching the `with urlopen(...) as r: r.read(); r.getheader(...)`
    contract download_archive_bytes uses."""

    def __init__(self, body: bytes, content_length: int | None) -> None:
        self._body = body
        self._content_length = content_length

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str) -> str | None:
        if name == "Content-Length" and self._content_length is not None:
            return str(self._content_length)
        return None


def test_archiveintegrity4_short_body_vs_content_length_raises(monkeypatch) -> None:
    """download_archive_bytes must FAIL LOUD (retryable ArchiveDownloadIncompleteError)
    when the received body is shorter than the advertised Content-Length — a clean
    mid-stream socket close returns the partial bytes WITHOUT raising, which would
    otherwise be promoted as a silently-thin day. Before the integrity check this
    short read was accepted."""
    full = gzip.compress(b"timestamp,symbol,side\n1,BTCUSDT,Buy\n")

    def fake_urlopen(_url, **_kwargs):
        # Body truncated to half, but the server advertised the full length.
        return _FakeHttpResponse(full[: len(full) // 2], content_length=len(full))

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    with pytest.raises(ArchiveDownloadIncompleteError):
        download_archive_bytes("https://example.test/BTCUSDT2025-01-01.csv.gz")


def test_archiveintegrity4_complete_body_accepted(monkeypatch) -> None:
    """Control: a body whose length matches the advertised Content-Length is
    returned intact (the check is exact-match, not over-eager)."""
    full = gzip.compress(b"timestamp,symbol,side\n1,BTCUSDT,Buy\n")

    def fake_urlopen(_url, **_kwargs):
        return _FakeHttpResponse(full, content_length=len(full))

    monkeypatch.setattr(archive_module, "urlopen", fake_urlopen)
    assert download_archive_bytes("https://example.test/BTCUSDT2025-01-01.csv.gz") == full


def test_archiveintegrity4_corrupt_cached_gz_rejected_and_refetched(tmp_path, monkeypatch) -> None:
    """A cached `.gz` archive that is truncated/corrupt (decompresses partially) must
    NOT be re-served on a size-only hit — _archive_cache_is_complete fully drains the
    gzip and rejects it, so the download falls through to a fresh fetch. Before the
    integrity check a previously-written partial archive was served forever. The
    recovered file must be the valid body, with exactly one re-download."""
    valid_gz = gzip.compress(
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1735689600.0,BTCUSDT,Buy,0.001,90000.0,PlusTick,e1,9000,0.001,90.0\n"
    )
    corrupt_gz = valid_gz[: len(valid_gz) // 2]  # truncated -> fails to fully decompress
    out = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    out.write_bytes(corrupt_gz)  # pre-seed a CORRUPT non-empty cache (passes size-only)

    calls = {"n": 0}

    def fake_download(_url, *, timeout_seconds):
        calls["n"] += 1
        return valid_gz

    monkeypatch.setattr(archive_module, "download_archive_bytes", fake_download)
    monkeypatch.setattr(archive_module.time, "sleep", lambda _seconds: None)

    result = download_public_trade_archive(
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2025-01-01.csv.gz",
        out,
        retries=2,
        timeout_seconds=5,
    )
    assert result == out
    assert out.read_bytes() == valid_gz, "the corrupt cached body must be rejected, not re-served"
    assert calls["n"] == 1, "the corrupt cache must trigger exactly one fresh download"
    assert not list(tmp_path.glob("*.tmp")), "no temp file should leak"


def test_archiveintegrity4_valid_cache_is_served_without_refetch(tmp_path, monkeypatch) -> None:
    """Control: a VALID cached `.gz` passes the integrity check and is served from
    cache with NO re-download (the check is not over-eager)."""
    valid_gz = gzip.compress(
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1735689600.0,BTCUSDT,Buy,0.001,90000.0,PlusTick,e1,9000,0.001,90.0\n"
    )
    out = tmp_path / "BTCUSDT2025-01-01.csv.gz"
    out.write_bytes(valid_gz)

    def fail_download(_url, *, timeout_seconds):
        raise AssertionError("a valid cached archive must not be re-downloaded")

    monkeypatch.setattr(archive_module, "download_archive_bytes", fail_download)
    result = download_public_trade_archive("https://example.test/BTCUSDT2025-01-01.csv.gz", out)
    assert result == out
    assert out.read_bytes() == valid_gz
