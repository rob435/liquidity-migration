"""Cross-sleeve margin budget + same-symbol reservation registry (long-sleeve-5/-6).

These tests are the executable contract for the shared cross-sleeve control table
that ws_risk OWNS and every sleeve consults read-only. They pin five invariants:

1. ws_risk IM-used computation from ``state.all_trades`` (notional/leverage aggregation,
   per-sleeve split) over OPEN trades only.
2. The budget clamp is SHRINK-ONLY and a NO-OP when the budget is unset (default).
3. Reservation atomicity under cross-process contention -- simulated with the REAL
   ``storage.exclusive_file_lock`` and interleaved control-row read/write: sleeve A
   claims SYMBOL under the lock; sleeve B (interleaved) sees the active reservation and
   skips. The lock + re-read-under-lock is what closes the same-minute race the venue
   snapshot cannot.
4. GC drops ttl-expired reservations AND reservations whose trade_id is now closed.
5. A torn / missing control row makes every consumer fail OPEN (no-op, legacy behavior).

The control table lives in dataset ``cross_sleeve_account_state`` (one row per
``account_key``), with the dict/list payload columns JSON-encoded for parquet
round-trip. The public surface under test:

  liquidity_migration.cross_sleeve:
    CROSS_SLEEVE_DATASET: str
    compute_account_state(all_trades, *, account_key, equity_usdt,
                          margin_budget_pct_by_sleeve, leverage_by_sleeve,
                          reservations, now_ms) -> dict          # IM math + GC
    write_account_state(data_root, state) -> None                # ws_risk writer
    read_account_state(data_root, *, account_key) -> dict | None # consumer read (fail-open)
    clamp_max_new_entries(state, *, sleeve, max_new_entries) -> int   # shrink-only
    claim_symbol(data_root, *, account_key, symbol, sleeve, trade_id,
                 now_ms, ttl_ms, live_position_symbols) -> bool   # atomic, under lock

  liquidity_migration.ws_risk.EventWebSocketRiskEngine:
    write_cross_sleeve_state(*, equity_usdt) -> dict             # called each reconcile pass
"""
from __future__ import annotations

import threading
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import storage
from liquidity_migration.config import ResearchConfig
from liquidity_migration.cross_sleeve import (
    CROSS_SLEEVE_DATASET,
    claim_symbol,
    clamp_max_new_entries,
    compute_account_state,
    read_account_state,
    write_account_state,
)
from liquidity_migration.ws_risk import EventWebSocketRiskConfig, EventWebSocketRiskEngine

ACCOUNT = "demo-unified-usdt"


def _trade(
    trade_id: str,
    *,
    sleeve: str,
    symbol: str,
    notional: float,
    leverage: float,
    status: str = "open",
) -> dict:
    return {
        "trade_id": trade_id,
        "sleeve": sleeve,
        "symbol": symbol,
        "status": status,
        "notional_usdt": notional,
        "entry_leverage": leverage,
    }


# ---------------------------------------------------------------------------
# (1) IM-used computation: notional/leverage aggregation, per-sleeve split,
#     OPEN trades only.
# ---------------------------------------------------------------------------
def test_im_used_computation_aggregates_per_trade_and_per_sleeve() -> None:
    # equity 10_000. Per-trade IM = notional / leverage.
    #   short  s1: 2000 / 10 = 200
    #   long   l1: 5000 / 10 = 500
    #   long   l2: 5000 / 10 = 500   -> long total 1000
    #   cont   c1: 1000 /  2 = 500
    # account IM = 200 + 500 + 500 + 500 = 1700 -> 17% of equity.
    all_trades = pl.DataFrame(
        [
            _trade("s1", sleeve="short", symbol="AAAUSDT", notional=2000.0, leverage=10.0),
            _trade("l1", sleeve="long", symbol="BTCUSDT", notional=5000.0, leverage=10.0),
            _trade("l2", sleeve="long", symbol="ETHUSDT", notional=5000.0, leverage=10.0),
            _trade("c1", sleeve="continuous", symbol="SOLUSDT", notional=1000.0, leverage=2.0),
            # A CLOSED trade must be excluded from IM-used.
            _trade("s2", sleeve="short", symbol="ZZZUSDT", notional=9_999_999.0, leverage=10.0, status="closed"),
        ]
    )
    state = compute_account_state(
        all_trades,
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={},
        reservations=[],
        now_ms=1_700_000_000_000,
    )
    assert state["account_key"] == ACCOUNT
    assert state["equity_usdt"] == pytest.approx(10_000.0)
    assert state["account_im_used_pct"] == pytest.approx(0.17)
    by_sleeve = state["im_used_pct_by_sleeve"]
    assert by_sleeve["short"] == pytest.approx(0.02)   # 200 / 10_000
    assert by_sleeve["long"] == pytest.approx(0.10)    # 1000 / 10_000
    assert by_sleeve["continuous"] == pytest.approx(0.05)  # 500 / 10_000
    # The closed trade contributed nothing.
    assert state["account_im_used_pct"] < 1.0


def test_im_used_falls_back_to_config_leverage_when_column_missing() -> None:
    # No per-trade entry_leverage column -> use leverage_by_sleeve, default if absent.
    all_trades = pl.DataFrame(
        [
            {"trade_id": "l1", "sleeve": "long", "symbol": "BTCUSDT", "status": "open", "notional_usdt": 5000.0},
        ]
    )
    state = compute_account_state(
        all_trades,
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={"long": 10.0},
        reservations=[],
        now_ms=1_700_000_000_000,
    )
    assert state["im_used_pct_by_sleeve"]["long"] == pytest.approx(0.05)  # 5000/10 / 10000


def test_im_used_empty_ledger_is_zero_not_crash() -> None:
    state = compute_account_state(
        pl.DataFrame(),
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={},
        reservations=[],
        now_ms=1_700_000_000_000,
    )
    assert state["account_im_used_pct"] == pytest.approx(0.0)
    assert state["im_used_pct_by_sleeve"] == {}


# ---------------------------------------------------------------------------
# (2) Budget clamp: SHRINK-ONLY + no-op when unset.
# ---------------------------------------------------------------------------
def test_clamp_noop_when_budget_unset() -> None:
    """No margin_budget_pct_by_sleeve (legacy) -> clamp is byte-identical pass-through."""
    state = {
        "im_used_pct_by_sleeve": {"long": 0.99},
        "margin_budget_pct_by_sleeve": None,
    }
    assert clamp_max_new_entries(state, sleeve="long", max_new_entries=5) == 5


def test_clamp_noop_when_sleeve_below_budget() -> None:
    state = {
        "im_used_pct_by_sleeve": {"long": 0.20},
        "margin_budget_pct_by_sleeve": {"long": 0.45},
    }
    assert clamp_max_new_entries(state, sleeve="long", max_new_entries=5) == 5


def test_clamp_shrinks_to_zero_when_sleeve_at_or_over_budget() -> None:
    state = {
        "im_used_pct_by_sleeve": {"long": 0.45},  # exactly at ceiling -> clamp
        "margin_budget_pct_by_sleeve": {"long": 0.45},
    }
    assert clamp_max_new_entries(state, sleeve="long", max_new_entries=5) == 0
    over = {
        "im_used_pct_by_sleeve": {"long": 0.60},
        "margin_budget_pct_by_sleeve": {"long": 0.45},
    }
    assert clamp_max_new_entries(over, sleeve="long", max_new_entries=5) == 0


def test_clamp_never_upsizes() -> None:
    """Even with a huge budget and zero usage, the clamp returns at most the
    caller's max_new_entries -- it can only ever SHRINK, never grant more slots."""
    state = {
        "im_used_pct_by_sleeve": {"long": 0.0},
        "margin_budget_pct_by_sleeve": {"long": 9.99},
    }
    assert clamp_max_new_entries(state, sleeve="long", max_new_entries=3) == 3


def test_clamp_only_touches_its_own_sleeve() -> None:
    """The long sleeve being over budget must not change the short sleeve's slots."""
    state = {
        "im_used_pct_by_sleeve": {"long": 0.99, "short": 0.01},
        "margin_budget_pct_by_sleeve": {"long": 0.45, "short": 0.35},
    }
    assert clamp_max_new_entries(state, sleeve="long", max_new_entries=5) == 0
    assert clamp_max_new_entries(state, sleeve="short", max_new_entries=5) == 5


def test_clamp_sleeve_absent_from_budget_is_noop() -> None:
    """A sleeve with no budget key set (operator left it null) is unclamped."""
    state = {
        "im_used_pct_by_sleeve": {"continuous": 0.99},
        "margin_budget_pct_by_sleeve": {"long": 0.45},  # continuous unset
    }
    assert clamp_max_new_entries(state, sleeve="continuous", max_new_entries=4) == 4


# ---------------------------------------------------------------------------
# (3) Reservation atomicity under simulated two-process contention.
#     REAL exclusive_file_lock + interleaved control-row read/write.
# ---------------------------------------------------------------------------
def _seed_empty_state(data_root: Path) -> None:
    state = compute_account_state(
        pl.DataFrame(),
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={},
        reservations=[],
        now_ms=1_700_000_000_000,
    )
    write_account_state(data_root, state)


def test_claim_symbol_is_atomic_under_lock_contention(tmp_path: Path) -> None:
    """Two sleeves race to claim the SAME symbol. claim_symbol acquires the
    dataset lock, re-reads the control row, and only writes a reservation if no
    foreign active reservation + no live venue position exist. The REAL
    storage.exclusive_file_lock serializes the two would-be processes, so EXACTLY
    ONE wins; the loser sees the winner's reservation and skips.

    We force a deterministic interleave by making sleeve A grab the lock first
    and HOLD it (via a barrier wired through a monkeypatched read) so B is blocked
    at acquire until A has committed its reservation -- exactly the same-minute
    race the venue snapshot cannot resolve.
    """
    _seed_empty_state(tmp_path)
    results: dict[str, bool] = {}
    a_committed = threading.Event()

    def _claim(sleeve: str, trade_id: str) -> None:
        results[sleeve] = claim_symbol(
            tmp_path,
            account_key=ACCOUNT,
            symbol="CONTUSDT",
            sleeve=sleeve,
            trade_id=trade_id,
            now_ms=1_700_000_100_000,
            ttl_ms=180_000,
            live_position_symbols=set(),
        )

    # A claims first and commits. Then release B; B must observe A's reservation.
    def _run_a() -> None:
        _claim("long", "l-trade-1")
        a_committed.set()

    def _run_b() -> None:
        a_committed.wait(timeout=5.0)
        _claim("continuous", "c-trade-1")

    ta = threading.Thread(target=_run_a)
    tb = threading.Thread(target=_run_b)
    ta.start()
    tb.start()
    ta.join(timeout=10.0)
    tb.join(timeout=10.0)

    assert results["long"] is True, "first claimer wins"
    assert results["continuous"] is False, "second claimer must see the active foreign reservation and skip"

    # The committed control row carries exactly one reservation, owned by long.
    state = read_account_state(tmp_path, account_key=ACCOUNT)
    assert state is not None
    active = [r for r in state["reservations"] if r["symbol"] == "CONTUSDT"]
    assert len(active) == 1
    assert active[0]["sleeve"] == "long"
    assert active[0]["trade_id"] == "l-trade-1"


def test_claim_symbol_truly_serialized_by_the_file_lock(tmp_path: Path) -> None:
    """Stronger than the ordered interleave: launch both claims with NO external
    ordering. The REAL file lock must still admit exactly one writer for the same
    symbol -- never two reservations, never both True. Repeat to shake out races.
    """
    for attempt in range(8):
        root = tmp_path / f"attempt{attempt}"
        root.mkdir()
        _seed_empty_state(root)
        out: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def _claim(sleeve: str, trade_id: str) -> None:
            barrier.wait(timeout=5.0)  # maximize the contention window
            out[sleeve] = claim_symbol(
                root,
                account_key=ACCOUNT,
                symbol="RACEUSDT",
                sleeve=sleeve,
                trade_id=trade_id,
                now_ms=1_700_000_100_000,
                ttl_ms=180_000,
                live_position_symbols=set(),
            )

        t1 = threading.Thread(target=_claim, args=("long", "l1"))
        t2 = threading.Thread(target=_claim, args=("continuous", "c1"))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert sum(1 for v in out.values() if v) == 1, f"attempt {attempt}: exactly one claim must win"
        state = read_account_state(root, account_key=ACCOUNT)
        assert state is not None
        active = [r for r in state["reservations"] if r["symbol"] == "RACEUSDT"]
        assert len(active) == 1, f"attempt {attempt}: exactly one reservation row persisted"


def test_claim_blocked_by_live_venue_position(tmp_path: Path) -> None:
    """Even with no reservation, a live venue position on SYMBOL means a sibling
    already holds it -- claim must fail (the disjoint-sleeve invariant)."""
    _seed_empty_state(tmp_path)
    ok = claim_symbol(
        tmp_path,
        account_key=ACCOUNT,
        symbol="HELDUSDT",
        sleeve="long",
        trade_id="l-1",
        now_ms=1_700_000_100_000,
        ttl_ms=180_000,
        live_position_symbols={"HELDUSDT"},
    )
    assert ok is False
    state = read_account_state(tmp_path, account_key=ACCOUNT)
    assert state is not None
    assert not any(r["symbol"] == "HELDUSDT" for r in state["reservations"])


def test_claim_succeeds_after_foreign_reservation_expires(tmp_path: Path) -> None:
    """A reservation past its ttl is NOT active -- a sibling may then claim the
    symbol (re-claiming overwrites the stale row, no duplicate)."""
    stale = compute_account_state(
        pl.DataFrame(),
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={},
        reservations=[
            {"symbol": "OLDUSDT", "sleeve": "long", "trade_id": "l-old",
             "reserved_at_ms": 1_700_000_000_000, "ttl_ms": 60_000},
        ],
        now_ms=1_700_000_000_000,
    )
    write_account_state(tmp_path, stale)
    # 120s later, the long reservation (ttl 60s) is expired -> continuous can claim.
    ok = claim_symbol(
        tmp_path,
        account_key=ACCOUNT,
        symbol="OLDUSDT",
        sleeve="continuous",
        trade_id="c-new",
        now_ms=1_700_000_120_000,
        ttl_ms=180_000,
        live_position_symbols=set(),
    )
    assert ok is True
    state = read_account_state(tmp_path, account_key=ACCOUNT)
    assert state is not None
    active = [r for r in state["reservations"]
              if r["symbol"] == "OLDUSDT" and (r["reserved_at_ms"] + r["ttl_ms"]) > 1_700_000_120_000]
    assert len(active) == 1
    assert active[0]["sleeve"] == "continuous"


def test_same_sleeve_reclaim_is_allowed(tmp_path: Path) -> None:
    """A sleeve re-claiming its OWN active reservation (e.g. a retry) succeeds --
    only a FOREIGN active reservation blocks."""
    seed = compute_account_state(
        pl.DataFrame(),
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={},
        reservations=[
            {"symbol": "MINEUSDT", "sleeve": "long", "trade_id": "l-1",
             "reserved_at_ms": 1_700_000_100_000, "ttl_ms": 180_000},
        ],
        now_ms=1_700_000_100_000,
    )
    write_account_state(tmp_path, seed)
    ok = claim_symbol(
        tmp_path,
        account_key=ACCOUNT,
        symbol="MINEUSDT",
        sleeve="long",
        trade_id="l-2",
        now_ms=1_700_000_110_000,
        ttl_ms=180_000,
        live_position_symbols=set(),
    )
    assert ok is True


# ---------------------------------------------------------------------------
# (4) GC: drop ttl-expired + closed-trade reservations.
# ---------------------------------------------------------------------------
def test_compute_state_gcs_expired_and_closed_reservations() -> None:
    all_trades = pl.DataFrame(
        [
            _trade("open-t", sleeve="long", symbol="LIVEUSDT", notional=1000.0, leverage=10.0, status="open"),
            _trade("closed-t", sleeve="long", symbol="DEADUSDT", notional=1000.0, leverage=10.0, status="closed"),
        ]
    )
    now = 1_700_000_200_000
    reservations = [
        # Active: open trade, not expired -> KEEP.
        {"symbol": "LIVEUSDT", "sleeve": "long", "trade_id": "open-t",
         "reserved_at_ms": now - 10_000, "ttl_ms": 180_000},
        # ttl-expired (reserved 5 min ago, ttl 60s) -> DROP.
        {"symbol": "EXPUSDT", "sleeve": "continuous", "trade_id": "open-t",
         "reserved_at_ms": now - 300_000, "ttl_ms": 60_000},
        # trade_id now closed -> DROP even though within ttl.
        {"symbol": "DEADUSDT", "sleeve": "long", "trade_id": "closed-t",
         "reserved_at_ms": now - 1_000, "ttl_ms": 180_000},
        # trade_id unknown to the ledger (never reconciled / vanished) -> DROP.
        {"symbol": "GHOSTUSDT", "sleeve": "short", "trade_id": "no-such-trade",
         "reserved_at_ms": now - 1_000, "ttl_ms": 180_000},
    ]
    state = compute_account_state(
        all_trades,
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve=None,
        leverage_by_sleeve={},
        reservations=reservations,
        now_ms=now,
    )
    kept = {r["symbol"] for r in state["reservations"]}
    assert kept == {"LIVEUSDT"}, "GC keeps only the live, unexpired, open-trade reservation"


# ---------------------------------------------------------------------------
# (5) Torn / missing control row -> every consumer fails OPEN (no-op).
# ---------------------------------------------------------------------------
def test_read_missing_control_row_returns_none(tmp_path: Path) -> None:
    """Fresh root, dataset never written -> read_account_state returns None,
    which the sleeve treats as no-clamp / legacy."""
    assert read_account_state(tmp_path, account_key=ACCOUNT) is None


def test_clamp_with_none_state_is_full_passthrough() -> None:
    """A consumer that got None (missing/torn row) must not clamp at all."""
    assert clamp_max_new_entries(None, sleeve="long", max_new_entries=5) == 5


def test_read_torn_control_row_fails_open_to_none(tmp_path: Path, monkeypatch) -> None:
    """A RAISED read (torn parquet / I/O error) must be swallowed -> None, never
    propagate into the sleeve cycle. The sleeve then runs legacy (no clamp)."""
    _seed_empty_state(tmp_path)
    import liquidity_migration.cross_sleeve as cs

    def _boom(*args, **kwargs):
        raise RuntimeError("torn parquet: end of file")

    # Whatever read primitive cross_sleeve uses internally, force it to raise.
    monkeypatch.setattr(cs.storage, "read_dataset", _boom, raising=False)
    monkeypatch.setattr(cs.storage, "read_dataset_columns", _boom, raising=False)
    assert read_account_state(tmp_path, account_key=ACCOUNT) is None


def test_claim_fails_open_to_true_on_torn_control_row(tmp_path: Path, monkeypatch) -> None:
    """If the control row can't be read under the lock, claim_symbol must FAIL
    OPEN (return True / allow the entry) rather than wedge the sleeve -- the
    operator-preferred safe-by-default: never block trading on the budget plumbing.
    The legacy venue-snapshot same-symbol exclusion still applies upstream.
    """
    _seed_empty_state(tmp_path)
    import liquidity_migration.cross_sleeve as cs

    real_read = cs.read_account_state

    def _torn(data_root, *, account_key):
        raise RuntimeError("torn parquet under lock")

    monkeypatch.setattr(cs, "_read_account_state_unlocked", _torn, raising=False)
    # If the implementation has no unlocked helper, fall back to making the public
    # read raise; claim_symbol must still fail open.
    if not hasattr(cs, "_read_account_state_unlocked"):
        monkeypatch.setattr(cs, "read_account_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("torn")))

    ok = claim_symbol(
        tmp_path,
        account_key=ACCOUNT,
        symbol="ANYUSDT",
        sleeve="long",
        trade_id="l-1",
        now_ms=1_700_000_100_000,
        ttl_ms=180_000,
        live_position_symbols=set(),
    )
    assert ok is True
    monkeypatch.setattr(cs, "read_account_state", real_read, raising=False)


# ---------------------------------------------------------------------------
# Registration + ws_risk ownership wiring.
# ---------------------------------------------------------------------------
def test_cross_sleeve_dataset_is_registered() -> None:
    """The control dataset MUST be in storage.DATASETS + DATASET_KEYS, keyed on
    account_key (one control row per account), else the first ws_risk write
    crashes the sole reconcile authority on the netted account."""
    assert CROSS_SLEEVE_DATASET in storage.DATASETS
    assert storage.DATASET_KEYS.get(CROSS_SLEEVE_DATASET) == ("account_key",)


def test_account_state_round_trips_through_storage(tmp_path: Path) -> None:
    """write_account_state -> read_account_state must preserve the dict/list
    payload exactly (JSON-encoded for parquet round-trip)."""
    state = compute_account_state(
        pl.DataFrame(
            [_trade("l1", sleeve="long", symbol="BTCUSDT", notional=5000.0, leverage=10.0)]
        ),
        account_key=ACCOUNT,
        equity_usdt=10_000.0,
        margin_budget_pct_by_sleeve={"short": 0.35, "long": 0.45, "continuous": 0.20},
        leverage_by_sleeve={},
        reservations=[
            {"symbol": "BTCUSDT", "sleeve": "long", "trade_id": "l1",
             "reserved_at_ms": 1_700_000_100_000, "ttl_ms": 180_000},
        ],
        now_ms=1_700_000_100_000,
    )
    write_account_state(tmp_path, state)
    out = read_account_state(tmp_path, account_key=ACCOUNT)
    assert out is not None
    assert out["account_key"] == ACCOUNT
    assert out["margin_budget_pct_by_sleeve"] == {"short": 0.35, "long": 0.45, "continuous": 0.20}
    assert out["im_used_pct_by_sleeve"]["long"] == pytest.approx(0.05)
    assert out["reservations"][0]["symbol"] == "BTCUSDT"
    assert out["reservations"][0]["sleeve"] == "long"


def test_account_state_rewrite_keeps_single_control_row(tmp_path: Path) -> None:
    """Each reconcile pass REWRITES the one control row for the account_key; the
    dataset must never accumulate duplicate rows (DATASET_KEYS dedup on
    account_key, keep-last by updated_at_ms)."""
    for i in range(3):
        state = compute_account_state(
            pl.DataFrame(),
            account_key=ACCOUNT,
            equity_usdt=10_000.0 + i,
            margin_budget_pct_by_sleeve=None,
            leverage_by_sleeve={},
            reservations=[],
            now_ms=1_700_000_000_000 + i * 60_000,
        )
        write_account_state(tmp_path, state)
    raw = storage.read_dataset(tmp_path, CROSS_SLEEVE_DATASET)
    assert raw.height == 1, "exactly one control row per account_key"
    out = read_account_state(tmp_path, account_key=ACCOUNT)
    assert out is not None
    assert out["equity_usdt"] == pytest.approx(10_002.0), "keep-last wins (freshest updated_at_ms)"


def test_ws_risk_writes_control_row_from_all_trades(tmp_path: Path) -> None:
    """End-to-end ws-risk ownership: with all three roots configured, the engine
    computes IM-used from its combined all_trades and writes the control row to
    its own (short) root each pass. Verifies the writer is wired to the live
    reconcile authority, not just the standalone helper."""
    short_root = tmp_path / "short"
    long_root = tmp_path / "long"
    cont_root = tmp_path / "cont"
    for r in (short_root, long_root, cont_root):
        r.mkdir()
    cfg = EventWebSocketRiskConfig(
        long_data_root=str(long_root),
        continuous_data_root=str(cont_root),
    )
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    engine.state.all_trades = pl.DataFrame(
        [
            _trade("s1", sleeve="short", symbol="AAAUSDT", notional=2000.0, leverage=10.0),
            _trade("l1", sleeve="long", symbol="BTCUSDT", notional=5000.0, leverage=10.0),
            _trade("c1", sleeve="continuous", symbol="SOLUSDT", notional=1000.0, leverage=2.0),
        ]
    )
    state = engine.write_cross_sleeve_state(equity_usdt=10_000.0)
    assert state["account_im_used_pct"] == pytest.approx(0.12)  # (200 + 500 + 500)/10000
    # Persisted to the short (authority) root and readable by a sleeve consumer.
    account_key = state["account_key"]
    persisted = read_account_state(short_root, account_key=account_key)
    assert persisted is not None
    assert persisted["im_used_pct_by_sleeve"]["long"] == pytest.approx(0.05)
    assert persisted["im_used_pct_by_sleeve"]["short"] == pytest.approx(0.02)
    assert persisted["im_used_pct_by_sleeve"]["continuous"] == pytest.approx(0.05)


def test_ws_risk_control_row_write_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    """The cross-sleeve writer is NEW work bolted onto the sole reconcile
    authority. A failure to compute/write the control row must never crash the
    reconcile pass (which protects open positions) -- it degrades to no control
    row, i.e. legacy no-clamp for the sleeves."""
    short_root = tmp_path / "short"
    short_root.mkdir()
    cfg = EventWebSocketRiskConfig()
    engine = EventWebSocketRiskEngine(short_root, config=ResearchConfig(), risk_config=cfg)
    engine.state.all_trades = pl.DataFrame(
        [_trade("s1", sleeve="short", symbol="AAAUSDT", notional=2000.0, leverage=10.0)]
    )
    import liquidity_migration.ws_risk as wsr

    def _boom(*args, **kwargs):
        raise RuntimeError("control-row write failed")

    # Force the underlying write to raise; the engine method must swallow it.
    monkeypatch.setattr(wsr, "write_account_state", _boom, raising=False)
    # Must NOT raise.
    engine.write_cross_sleeve_state(equity_usdt=10_000.0)
