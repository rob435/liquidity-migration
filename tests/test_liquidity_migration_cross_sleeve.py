"""Cross-sleeve account-state control table (long-sleeve-5 / -6).

Pins the safe-by-default budget clamp, the fail-open reads, and the UNDER-LOCK
read-modify-write that lets ws_risk (IM owner) and the sleeves (reservation claimers)
share ONE row without clobbering each other.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.cross_sleeve import (
    CrossSleeveAccountState,
    VALID_SLEEVES,
    claim_symbol_reservation,
    claim_symbols_reservation,
    clamp_max_new_entries,
    encode_control_row,
    equal_split_budget,
    partition_claimable,
    read_account_state,
    release_symbol_reservation,
    seed_margin_budget,
    write_account_state,
)


# --- equal-split budget equation (dynamic 1/n_active) ----------------------
def test_equal_split_budget_divides_equally_by_active_count() -> None:
    assert equal_split_budget(["short", "long", "continuous"]) == {
        "short": 1 / 3, "long": 1 / 3, "continuous": 1 / 3,
    }
    assert equal_split_budget(["short", "long"]) == {"short": 0.5, "long": 0.5}
    assert equal_split_budget(["short"]) == {"short": 1.0}
    assert equal_split_budget([]) == {}  # nothing active -> no clamp


def test_equal_split_budget_drops_unknowns_and_dedups() -> None:
    # unknown sleeve names are not budgeted; duplicates collapse (deterministic denominator)
    assert equal_split_budget(["short", "bogus", "short", "long"]) == {"short": 0.5, "long": 0.5}
    assert equal_split_budget(["bogus"]) == {}


def test_write_account_state_sets_preserves_and_clears_budget(tmp_path: Path) -> None:
    """ws_risk writes the computed equal split; the sentinel default PRESERVES the prior
    budget; an explicit None CLEARS it (back to no-clamp)."""
    # SET: ws_risk writes a computed split
    write_account_state(tmp_path, equity_usdt=10_000.0, account_im_used_pct=0.2,
                        im_used_pct_by_sleeve={"short": 0.2}, now_ms=1_000,
                        margin_budget_pct_by_sleeve={"short": 0.5, "long": 0.5})
    assert read_account_state(tmp_path).margin_budget_pct_by_sleeve == {"short": 0.5, "long": 0.5}
    # PRESERVE: a legacy call (no budget arg) keeps the prior split, updates IM only
    write_account_state(tmp_path, equity_usdt=10_000.0, account_im_used_pct=0.3,
                        im_used_pct_by_sleeve={"short": 0.3}, now_ms=2_000)
    st = read_account_state(tmp_path)
    assert st.margin_budget_pct_by_sleeve == {"short": 0.5, "long": 0.5}
    assert st.im_used_for("short") == 0.3  # IM did update
    # CLEAR: explicit None removes the clamp
    write_account_state(tmp_path, equity_usdt=10_000.0, account_im_used_pct=0.3,
                        im_used_pct_by_sleeve={"short": 0.3}, now_ms=3_000,
                        margin_budget_pct_by_sleeve=None)
    assert read_account_state(tmp_path).margin_budget_pct_by_sleeve is None


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


def test_claim_is_atomic_under_true_thread_contention(tmp_path: Path) -> None:
    """long-sleeve-6 atomicity under REAL contention: two threads race to claim the same
    symbol through the actual exclusive_file_lock. Exactly one must win and exactly one
    reservation row may persist — never two. Repeated to shake out interleavings."""
    import threading

    for attempt in range(8):
        root = tmp_path / f"attempt{attempt}"
        root.mkdir()
        seed_margin_budget(root, {"long": 0.45}, now_ms=1_700_000_000_000)  # create the dataset
        out: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def _claim(sleeve: str, trade_id: str, root: Path = root, out: dict = out, barrier=barrier) -> None:
            barrier.wait(timeout=5.0)  # maximize the contention window
            out[sleeve] = claim_symbol_reservation(
                root, symbol="RACEUSDT", sleeve=sleeve, trade_id=trade_id,
                now_ms=1_700_000_100_000, ttl_ms=180_000,
            )

        t1 = threading.Thread(target=_claim, args=("long", "l1"))
        t2 = threading.Thread(target=_claim, args=("continuous", "c1"))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert sum(1 for v in out.values() if v) == 1, f"attempt {attempt}: exactly one claim wins"
        active = [r for r in read_account_state(root).reservations if r["symbol"] == "RACEUSDT"]
        assert len(active) == 1, f"attempt {attempt}: exactly one reservation row persisted"


def test_compute_im_used_aggregates_per_sleeve_with_fallbacks() -> None:
    """ws_risk IM computation: prefer stored initial_margin_usdt (long), else
    notional/leverage (sleeve-leverage map or per-trade entry_leverage), aggregated per
    sleeve + total / equity. Unknown leverage -> conservative 1.0 (over-count)."""
    import polars as pl

    from liquidity_migration.cross_sleeve import compute_im_used

    trades = pl.DataFrame([
        {"sleeve": "long", "notional_usdt": 1000.0, "initial_margin_usdt": 100.0, "entry_leverage": None},
        {"sleeve": "short", "notional_usdt": 1000.0, "initial_margin_usdt": 0.0, "entry_leverage": None},   # lev from map=2 -> 500
        {"sleeve": "continuous", "notional_usdt": 600.0, "initial_margin_usdt": 0.0, "entry_leverage": 2.0},  # per-trade lev 2 -> 300
    ], infer_schema_length=None)
    account_pct, by_sleeve = compute_im_used(trades, equity_usdt=10_000.0, sleeve_leverage={"short": 2.0})
    assert by_sleeve["long"] == 100.0 / 10_000      # stored IM
    assert by_sleeve["short"] == 500.0 / 10_000     # 1000/2 via sleeve map
    assert by_sleeve["continuous"] == 300.0 / 10_000  # 600/2 via per-trade leverage
    assert abs(account_pct - (900.0 / 10_000)) < 1e-9
    # equity 0 -> no clamp basis
    assert compute_im_used(trades, equity_usdt=0.0, sleeve_leverage={}) == (0.0, {})


def test_trade_im_floors_stale_stored_only_when_leverage_known() -> None:
    """CS-8: a stale/low stored ``initial_margin_usdt`` is floored at notional/leverage when
    leverage is KNOWN (fail-safe over-count), but must NOT be inflated via the 1.0
    unknown-leverage fallback — that would over-count a high-leverage sleeve (long ~10x)."""
    import polars as pl

    from liquidity_migration.cross_sleeve import compute_im_used

    trades = pl.DataFrame([
        # stale-low stored IM (50) but KNOWN leverage 5 -> floored to 1000/5 = 200.
        {"sleeve": "short", "notional_usdt": 1000.0, "initial_margin_usdt": 50.0, "entry_leverage": 5.0},
        # stored IM (100) with UNKNOWN leverage -> trusted as-is, NOT inflated to full notional.
        {"sleeve": "long", "notional_usdt": 1000.0, "initial_margin_usdt": 100.0, "entry_leverage": None},
    ], infer_schema_length=None)
    _, by_sleeve = compute_im_used(trades, equity_usdt=10_000.0, sleeve_leverage={})
    assert by_sleeve["short"] == 200.0 / 10_000   # floored to notional / known leverage
    assert by_sleeve["long"] == 100.0 / 10_000    # unknown leverage -> stored trusted, not inflated


def test_claim_survives_ws_risk_write_with_higher_now_ms(tmp_path: Path) -> None:
    """CS-1: ws_risk stamps a FRESH ``_now_ms()`` on its IM row while a sleeve's claim carries
    a STALE ``cycle_now_ms`` (captured seconds earlier at cycle start). The single-row dedup
    keeps ``max(updated_at_ms)``, so a naive stamp lets the fresh IM row clobber the just-claimed
    reservation — the claim returns True yet the reservation vanishes, silently defeating the
    same-minute-race guard. Monotonic ``updated_at_ms`` makes the last committer under the lock win."""
    # ws_risk writes the IM row at a HIGH now_ms (fresh clock)...
    write_account_state(tmp_path, equity_usdt=10_000.0, account_im_used_pct=0.40,
                        im_used_pct_by_sleeve={"long": 0.40}, now_ms=2_000)
    # ...then a sleeve claims at a LOWER now_ms (its stale cycle_now_ms).
    assert claim_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="long",
                                    trade_id="t1", now_ms=1_000) is True
    st = read_account_state(tmp_path)
    assert any(r["symbol"] == "AAAUSDT" for r in st.reservations), "CS-1: reservation must not be clobbered"
    assert st.im_used_pct_by_sleeve.get("long") == 0.40  # IM fields the claim preserved are intact


_SUBPROCESS_CLAIM_WORKER = r"""
import json
import os
import sys
import time
from pathlib import Path

from liquidity_migration.cross_sleeve import claim_symbol_reservation

root = Path(sys.argv[1])
start_file = Path(sys.argv[2])
out_file = Path(sys.argv[3])
symbol = sys.argv[4]
sleeve = sys.argv[5]
trade_id = sys.argv[6]
now_ms = int(sys.argv[7])

deadline = time.monotonic() + 10.0
while not start_file.exists():
    if time.monotonic() > deadline:
        raise TimeoutError("start gate did not open")
    time.sleep(0.005)

ok = claim_symbol_reservation(
    root,
    symbol=symbol,
    sleeve=sleeve,
    trade_id=trade_id,
    now_ms=now_ms,
    ttl_ms=180_000,
)
tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")
tmp_file.write_text(json.dumps({"sleeve": sleeve, "ok": ok}), encoding="utf-8")
os.replace(tmp_file, out_file)
"""


def _run_claim_subprocesses(root: Path, tmp_path: Path, attempt: int) -> list[dict[str, object]]:
    """Run claim workers in plain subprocesses.

    This intentionally avoids ``multiprocessing``. The test needs OS-process
    isolation for the file lock, not multiprocessing.Queue/resource-tracker
    machinery, which is fragile in constrained test runners.
    """
    import json
    import os
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    prior_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(repo_root)
        if not prior_pythonpath
        else str(repo_root) + os.pathsep + prior_pythonpath
    )
    start_file = tmp_path / f"claim-start-{attempt}.flag"
    procs: list[tuple[str, Path, subprocess.Popen[str]]] = []
    for sleeve in ("long", "continuous", "short"):
        out_file = tmp_path / f"claim-result-{attempt}-{sleeve}.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SUBPROCESS_CLAIM_WORKER,
                str(root),
                str(start_file),
                str(out_file),
                "RACEUSDT",
                sleeve,
                f"{sleeve}-1",
                "1700000100000",
            ],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append((sleeve, out_file, proc))

    results: list[dict[str, object]] = []
    try:
        start_file.write_text("go\n", encoding="ascii")
        for sleeve, out_file, proc in procs:
            try:
                stdout, stderr = proc.communicate(timeout=15.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=5.0)
                pytest.fail(
                    f"{sleeve} claim subprocess timed out; stdout={stdout!r} stderr={stderr!r}"
                )
            if proc.returncode != 0:
                pytest.fail(
                    f"{sleeve} claim subprocess exited {proc.returncode}; "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
            if not out_file.exists():
                pytest.fail(f"{sleeve} claim subprocess wrote no result file")
            results.append(json.loads(out_file.read_text(encoding="utf-8")))
    finally:
        for _, _, proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
    return results


def _running_under_codex_tool_runner() -> bool:
    import os

    return bool(os.environ.get("CODEX_THREAD_ID")) and not (
        os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")
    )


def test_claim_is_atomic_under_true_process_contention(tmp_path: Path) -> None:
    """CS-3: production races are 3 SEPARATE PROCESSES. The thread test serializes on the
    in-process thread lock and never exercises the O_EXCL cross-process path; here N real
    processes race the same symbol through the actual file lock — exactly one wins and exactly
    one reservation row may persist (never two)."""
    if _running_under_codex_tool_runner():
        pytest.skip(
            "Codex tool runner kills pytest nodes that launch several child Python "
            "processes; this cross-process lock check still runs in normal terminals and CI."
        )

    for attempt in range(2):
        root = tmp_path / f"attempt{attempt}"
        root.mkdir()
        seed_margin_budget(root, {"long": 0.45}, now_ms=1_700_000_000_000)  # create the dataset
        results = _run_claim_subprocesses(root, tmp_path, attempt)
        assert sum(1 for row in results if row["ok"]) == 1, f"attempt {attempt}: exactly one process wins"
        active = [r for r in read_account_state(root).reservations if r["symbol"] == "RACEUSDT"]
        assert len(active) == 1, f"attempt {attempt}: exactly one reservation row persisted"


def test_release_symbol_reservation_frees_symbol_for_sibling(tmp_path: Path) -> None:
    """LON-9: releasing a failed long entry's reservation frees the symbol for a sibling
    sleeve immediately, instead of blocking it for the full TTL. Matches only on
    (symbol, sleeve, trade_id) so it never drops another sleeve's claim."""
    # Seed the control dataset so claims persist (an absent dataset fails open).
    seed_margin_budget(tmp_path, {"long": 0.45}, now_ms=1_000)
    now = 1_700_000_000_000

    assert claim_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="long", trade_id="t1", now_ms=now) is True
    # short is blocked while long holds it
    assert claim_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="short", trade_id="t2", now_ms=now) is False

    # long's entry failed -> release frees the symbol
    assert release_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="long", trade_id="t1", now_ms=now) is True
    assert claim_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="short", trade_id="t2", now_ms=now) is True

    # releasing something never claimed is a harmless no-op
    assert release_symbol_reservation(tmp_path, symbol="ZZZUSDT", sleeve="long", trade_id="x", now_ms=now) is False
    # a wrong-trade_id release must NOT drop short's live claim
    assert release_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="short", trade_id="WRONG", now_ms=now) is False
    assert claim_symbol_reservation(tmp_path, symbol="AAAUSDT", sleeve="long", trade_id="t3", now_ms=now) is False


# ======================================================================================
# Relocated from tests/test_audit_fix_b08.py (audit bucket b08).
# ======================================================================================


# --------------------------------------------------------------------------------------
# cross-sleeve-1: continuous_addon is budgeted and clamped consistently
# --------------------------------------------------------------------------------------
def test_continuous_addon_is_a_valid_budgeted_sleeve() -> None:
    assert "continuous_addon" in VALID_SLEEVES


def test_equal_split_includes_continuous_addon_in_denominator() -> None:
    # PRE-FIX: 'continuous_addon' was dropped, so this returned {'long':0.5,'continuous':0.5}
    # (denominator 2) and the three sleeves over-committed the one shared account.
    split = equal_split_budget(["long", "continuous", "continuous_addon"])
    assert split == {"long": 1 / 3, "continuous": 1 / 3, "continuous_addon": 1 / 3}
    assert sum(split.values()) == pytest.approx(1.0)
    # addon alone -> 100%, not dropped to {} (which the pre-fix code did, leaving long at 100%)
    assert equal_split_budget(["continuous_addon"]) == {"continuous_addon": 1.0}


def test_clamp_fires_for_an_at_ceiling_continuous_addon() -> None:
    # PRE-FIX: budget_for('continuous_addon') was None (never budgeted) so clamp returned
    # (5, False) -> the addon ran UNCAPPED even at 99% IM.
    state = CrossSleeveAccountState(
        margin_budget_pct_by_sleeve={"long": 1 / 3, "continuous": 1 / 3, "continuous_addon": 1 / 3},
        im_used_pct_by_sleeve={"continuous_addon": 0.40},  # over its 1/3 ceiling
    )
    assert clamp_max_new_entries(5, sleeve="continuous_addon", state=state) == (0, True)
    # a still-under sibling is unchanged
    assert clamp_max_new_entries(5, sleeve="long", state=state) == (5, False)


# --------------------------------------------------------------------------------------
# cross-sleeve-5: empty-dict budget {} round-trips symmetrically (write {} -> read None)
# --------------------------------------------------------------------------------------
def test_empty_dict_budget_round_trips_symmetrically(tmp_path: Path) -> None:
    # equal_split_budget([]) -> {} ; persisting it must NOT read back differently from how it
    # was written. PRE-FIX: encode wrote '{}' but _loads_budget decoded '{}' -> None, so
    # write({}) != read(). Now {} canonicalizes to None on BOTH sides (== no clamp).
    seed_margin_budget(tmp_path, equal_split_budget([]), now_ms=1_000)
    assert read_account_state(tmp_path).margin_budget_pct_by_sleeve is None


def test_encode_control_row_normalizes_empty_budget_to_blank() -> None:
    # The on-disk encoding of an empty-dict budget must equal the encoding of a None budget,
    # so the round-trip cannot silently distinguish "no sleeves budgeted" from "no budget".
    none_row = encode_control_row(CrossSleeveAccountState(margin_budget_pct_by_sleeve=None))
    empty_row = encode_control_row(CrossSleeveAccountState(margin_budget_pct_by_sleeve={}))
    assert none_row["margin_budget_pct_by_sleeve"] == ""
    assert empty_row["margin_budget_pct_by_sleeve"] == ""
    # a NON-empty budget still encodes to a JSON string (unchanged behavior)
    real_row = encode_control_row(CrossSleeveAccountState(margin_budget_pct_by_sleeve={"long": 0.5}))
    assert real_row["margin_budget_pct_by_sleeve"] == '{"long": 0.5}'


# --------------------------------------------------------------------------------------
# cross-sleeve-6: batch claim takes the control-row lock ONCE, preserving the per-candidate
# contract. Equivalence with N sequential single-claims + a single lock acquisition.
# --------------------------------------------------------------------------------------
def _count_lock_acquisitions(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap cross_sleeve.exclusive_file_lock to count how many times the control-row lock is
    actually acquired. partition_claimable must take it ONCE per call regardless of N."""
    from liquidity_migration import cross_sleeve as cs_module

    counter = {"n": 0}
    real_lock = cs_module.exclusive_file_lock

    def counting_lock(*args, **kwargs):  # noqa: ANN002, ANN003
        counter["n"] += 1
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(cs_module, "exclusive_file_lock", counting_lock)
    return counter


def test_partition_claimable_takes_lock_once_for_many_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_margin_budget(tmp_path, {"continuous": 0.5}, now_ms=1_000)  # create the dataset (1 lock)
    counter = _count_lock_acquisitions(monkeypatch)
    candidates = [{"symbol": f"S{i}USDT", "trade_id": f"t{i}"} for i in range(6)]

    granted, skipped = partition_claimable(
        tmp_path, candidates, sleeve="continuous", now_ms=1_700_000_000_000
    )

    assert {c["symbol"] for c in granted} == {c["symbol"] for c in candidates}
    assert skipped == 0
    # PRE-FIX: one lock cycle PER candidate (6). The batch claim takes it exactly once.
    assert counter["n"] == 1
    # the reservations were persisted by the single write
    reserved = {r["symbol"] for r in read_account_state(tmp_path).reservations}
    assert reserved == {c["symbol"] for c in candidates}


def test_batch_claim_rejects_foreign_and_live_but_grants_own_reclaim(tmp_path: Path) -> None:
    seed_margin_budget(tmp_path, {"long": 0.5}, now_ms=1_000)
    now = 1_700_000_000_000
    # a sibling already holds BBBUSDT
    assert claim_symbol_reservation(tmp_path, symbol="BBBUSDT", sleeve="short", trade_id="s1", now_ms=now) is True
    # continuous also already holds CCCUSDT (its own -> later re-claim must grant)
    assert claim_symbol_reservation(tmp_path, symbol="CCCUSDT", sleeve="continuous", trade_id="c0", now_ms=now) is True

    candidates = [
        {"symbol": "AAAUSDT", "trade_id": "c1"},   # free -> grant
        {"symbol": "BBBUSDT", "trade_id": "c2"},   # foreign reservation -> deny
        {"symbol": "CCCUSDT", "trade_id": "c3"},   # own re-claim -> grant
        {"symbol": "DDDUSDT", "trade_id": "c4"},   # live venue position -> deny
    ]
    granted, skipped, denied = claim_symbols_reservation(
        tmp_path, candidates, sleeve="continuous", now_ms=now,
        live_position_symbols={"DDDUSDT"},
    )
    assert {c["symbol"] for c in granted} == {"AAAUSDT", "CCCUSDT"}
    assert {c["symbol"] for c in denied} == {"BBBUSDT", "DDDUSDT"}
    assert skipped == 2
    # the sibling's BBBUSDT reservation survived (batch only ever appends its own grants)
    syms = {(r["symbol"], r["sleeve"]) for r in read_account_state(tmp_path).reservations}
    assert ("BBBUSDT", "short") in syms
    assert ("AAAUSDT", "continuous") in syms


def test_batch_claim_with_no_candidates_never_takes_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_margin_budget(tmp_path, {"long": 0.5}, now_ms=1_000)
    counter = _count_lock_acquisitions(monkeypatch)
    granted, skipped, denied = claim_symbols_reservation(tmp_path, [], sleeve="long", now_ms=1)
    assert (granted, skipped, denied) == ([], 0, [])
    assert counter["n"] == 0  # empty batch short-circuits before touching the lock


def test_batch_claim_fails_open_when_no_control_dataset(tmp_path: Path) -> None:
    # No ws_risk writer yet -> the whole batch is GRANTED (legacy venue-snapshot exclusion only).
    candidates = [{"symbol": "XUSDT", "trade_id": "t1"}, {"symbol": "YUSDT", "trade_id": "t2"}]
    granted, skipped, denied = claim_symbols_reservation(tmp_path, candidates, sleeve="long", now_ms=1)
    assert {c["symbol"] for c in granted} == {"XUSDT", "YUSDT"}
    assert skipped == 0 and denied == []
