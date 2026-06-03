"""Cross-sleeve account-state control table (long-sleeve-5 / -6).

Pins the safe-by-default budget clamp, the fail-open reads, and the UNDER-LOCK
read-modify-write that lets ws_risk (IM owner) and the sleeves (reservation claimers)
share ONE row without clobbering each other.
"""
from __future__ import annotations

from pathlib import Path

from liquidity_migration.cross_sleeve import (
    CrossSleeveAccountState,
    claim_symbol_reservation,
    clamp_max_new_entries,
    read_account_state,
    seed_margin_budget,
    write_account_state,
)


# --- budget clamp (long-sleeve-5) ------------------------------------------
def test_clamp_no_budget_is_noop() -> None:
    st = CrossSleeveAccountState()  # neutral: margin_budget_pct_by_sleeve None
    assert clamp_max_new_entries(5, sleeve="long", state=st) == (5, False)


def test_clamp_shrinks_to_zero_at_or_over_budget_only() -> None:
    st = CrossSleeveAccountState(
        margin_budget_pct_by_sleeve={"long": 0.45},
        im_used_pct_by_sleeve={"long": 0.45},  # exactly at ceiling
    )
    assert clamp_max_new_entries(5, sleeve="long", state=st) == (0, True)
    under = CrossSleeveAccountState(
        margin_budget_pct_by_sleeve={"long": 0.45}, im_used_pct_by_sleeve={"long": 0.30},
    )
    assert clamp_max_new_entries(5, sleeve="long", state=under) == (5, False)  # under -> unchanged
    # a sibling with no budget key is unclamped
    assert clamp_max_new_entries(5, sleeve="short", state=st) == (5, False)


def test_clamp_never_upsizes() -> None:
    st = CrossSleeveAccountState(margin_budget_pct_by_sleeve={"long": 0.45}, im_used_pct_by_sleeve={"long": 0.0})
    eff, _ = clamp_max_new_entries(3, sleeve="long", state=st)
    assert eff == 3  # only ever shrinks; here unchanged


# --- fail-open reads -------------------------------------------------------
def test_read_missing_dataset_is_neutral_noop(tmp_path: Path) -> None:
    st = read_account_state(tmp_path)
    assert st.margin_budget_pct_by_sleeve is None and st.reservations == []
    assert clamp_max_new_entries(7, sleeve="long", state=st) == (7, False)


def test_seed_and_read_budget_roundtrip(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"short": 0.35, "long": 0.45, "continuous": 0.20}, now_ms=1000)
    st = read_account_state(tmp_path)
    assert st.budget_for("long") == 0.45 and st.budget_for("continuous") == 0.20
    # clearing the budget -> back to no-op
    seed_margin_budget(tmp_path, None, now_ms=2000)
    assert read_account_state(tmp_path).margin_budget_pct_by_sleeve is None


# --- ws_risk write (IM owner) preserves operator budget + concurrent reservations ---
def test_write_account_state_preserves_seeded_budget_and_claimed_reservation(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"long": 0.45}, now_ms=1000)
    # a sleeve claims a symbol (writes a reservation) ...
    assert claim_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="long", trade_id="t1", now_ms=1100) is True
    # ... then ws_risk does its per-pass IM write. It must NOT clobber the budget or the reservation.
    write_account_state(
        tmp_path, equity_usdt=10_000.0, account_im_used_pct=0.4,
        im_used_pct_by_sleeve={"long": 0.4}, now_ms=1200,
    )
    st = read_account_state(tmp_path)
    assert st.budget_for("long") == 0.45               # operator budget preserved
    assert st.im_used_for("long") == 0.4               # ws_risk IM applied
    syms = {r["symbol"] for r in st.active_reservations(now_ms=1250)}
    assert "AAAUSDT" in syms                            # the claim survived ws_risk's write


def test_write_account_state_gcs_expired_and_closed_reservations(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"long": 0.45}, now_ms=1000)
    claim_symbol_reservation(tmp_path, symbol="OLDUSDT", sleeve="long", trade_id="t-old", now_ms=1100, ttl_ms=50)
    claim_symbol_reservation(tmp_path, symbol="CLOSEDUSDT", sleeve="long", trade_id="t-closed", now_ms=2000)
    claim_symbol_reservation(tmp_path, symbol="LIVEUSDT", sleeve="long", trade_id="t-live", now_ms=2000)
    # ws_risk pass at now=2100: OLDUSDT expired (ttl 50), t-closed trade is now closed.
    write_account_state(
        tmp_path, equity_usdt=10_000.0, account_im_used_pct=0.4,
        im_used_pct_by_sleeve={"long": 0.4}, now_ms=2100, closed_trade_ids={"t-closed"},
    )
    syms = {r["symbol"] for r in read_account_state(tmp_path).active_reservations(now_ms=2150)}
    assert syms == {"LIVEUSDT"}  # expired + closed dropped; the live one kept


# --- reservation claim atomicity (long-sleeve-6) ---------------------------
def test_claim_rejects_active_foreign_reservation_but_grants_own_reclaim(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"long": 0.45}, now_ms=1000)  # ensure dataset exists
    assert claim_symbol_reservation(tmp_path, symbol="XUSDT", sleeve="long", trade_id="t1", now_ms=1100) is True
    # a DIFFERENT sleeve is rejected on the same symbol...
    assert claim_symbol_reservation(tmp_path, symbol="XUSDT", sleeve="short", trade_id="t2", now_ms=1150) is False
    # ...the SAME sleeve may re-claim (idempotent)...
    assert claim_symbol_reservation(tmp_path, symbol="XUSDT", sleeve="long", trade_id="t1", now_ms=1160) is True
    # ...and a live venue position blocks even a free symbol.
    assert claim_symbol_reservation(
        tmp_path, symbol="YUSDT", sleeve="long", trade_id="t3", now_ms=1200,
        live_position_symbols={"YUSDT"},
    ) is False


def test_claim_grants_and_fails_open_when_no_control_dataset(tmp_path: Path) -> None:
    # No ws_risk writer yet (no dataset) -> claim GRANTS (legacy venue-snapshot exclusion only).
    assert claim_symbol_reservation(tmp_path, symbol="ZUSDT", sleeve="long", trade_id="t1", now_ms=1) is True


def test_expired_foreign_reservation_no_longer_blocks(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"long": 0.45}, now_ms=1000)
    claim_symbol_reservation(tmp_path, symbol="WUSDT", sleeve="long", trade_id="t1", now_ms=1100, ttl_ms=50)
    # well past the 50ms ttl: the foreign reservation is inactive -> short may now claim.
    assert claim_symbol_reservation(tmp_path, symbol="WUSDT", sleeve="short", trade_id="t2", now_ms=9999) is True
