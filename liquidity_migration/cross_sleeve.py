"""Shared cross-sleeve account-state control table (long-sleeve-5 / -6).

ONE on-disk control row per netted demo account, OWNED + rewritten by ws_risk each
reconcile pass (it is the only component that reads all three sleeve roots). The three
separate sleeve processes (short event_demo, long long_native_event_demo, continuous
continuous_demo) consult it READ-ONLY before sizing and claim same-symbol reservations
through it under the dataset lock. Everything here is safe-by-default: a missing row, a
read error, or a null budget split is a NO-OP (legacy behavior).

This module is the SINGLE encode/decode site so the writer (ws_risk), the sleeve readers,
and the under-lock reservation/IM read-modify-write can never disagree on the on-disk
shape. The dict/list fields are JSON-string columns (not native list/struct) so a partial
or all-null write can never trip a polars schema-drift error on read; the scalars
(account_key, account_im_used_pct, equity_usdt, updated_at_ms) are plain columns.

CONCURRENCY: ALL writers (ws_risk per-pass `write_account_state`, sleeve
`claim_symbol_reservation`, and any operator budget seed) do a read-modify-write UNDER
the single dataset lock, so they are SERIALIZED — ws_risk never clobbers a reservation a
sleeve just claimed, and a claim never clobbers ws_risk's IM update. ``_write_part``
dedups the single-row table on ``account_key`` keeping the freshest ``updated_at_ms``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import polars as pl

from .storage import (
    _collect_ledger_files,
    _write_dataset_unlocked,
    dataset_lock_path,
    ensure_data_root,
    exclusive_file_lock,
    read_dataset,
)

_logger = logging.getLogger("liquidity_migration.cross_sleeve")

# Registered in storage.DATASETS + DATASET_KEYS. Single control row keyed by account_key.
CROSS_SLEEVE_DATASET = "cross_sleeve_account_state"

# One netted demo account => one stable key shared by the writer and all readers.
DEFAULT_ACCOUNT_KEY = "bybit-demo"

# The short/account root carries the shared control row (= ws_risk self.root). Sleeve
# roots are siblings under data/; resolve the shared root from any sleeve's own root.
SHARED_ACCOUNT_ROOT_DIRNAME = "bybit-demo-event"

# Reservation TTL: ~3 cycles at the 60s sleeve cadence. A claim older than this is
# treated as released (ws_risk also GCs it). Long enough to cover submit+fill+the next
# reconcile pass; short enough that a crashed claimant never wedges a symbol.
RESERVATION_TTL_MS = 180_000

# The sleeves that may hold a budget share / be IM-clamped. MUST stay in sync with the
# names ws_risk._active_sleeves() can emit and with the sleeve tag the live books write to
# the ledger (continuous_demo tags its add-on trades sleeve="continuous_addon"). Omitting a
# routed sleeve here is silently unsafe: equal_split_budget drops it from the denominator
# (the surviving sleeves' shares inflate and over-commit the shared account) while
# compute_im_used still counts its IM and clamp_max_new_entries never throttles it
# (budget_for is None). CS cross-sleeve-1.
VALID_SLEEVES = ("short", "long", "continuous", "continuous_addon")


class _Preserve:
    """Sentinel default for write_account_state's budget arg: keep the prior on-disk budget
    (legacy behavior) rather than overwrite it. Distinct from None, which CLEARS the budget."""

    __slots__ = ()


_PRESERVE = _Preserve()


def equal_split_budget(active_sleeves: Iterable[str]) -> dict[str, float]:
    """The cross-sleeve IM-budget equation: split the account equally across the ACTIVE
    sleeves — n active ⇒ 1/n of equity each (3→0.333…, 2→0.5, 1→1.0; 0→{} = no clamp).
    Unknown names are dropped (only VALID_SLEEVES are budgeted) and duplicates collapse, so
    the result is deterministic. ws_risk recomputes + writes this EVERY reconcile pass, so
    the split self-adjusts the instant a sleeve is toggled on/off — no operator reseed."""
    seen = [s for s in dict.fromkeys(active_sleeves) if s in VALID_SLEEVES]
    if not seen:
        return {}
    share = 1.0 / len(seen)
    return {s: share for s in seen}


def shared_account_root(sleeve_data_root: str | Path) -> Path:
    """Resolve the shared account/control root from a sleeve's own data root (mirrors
    cli.py's sibling convention). If the sleeve IS the account root, returns it."""
    root = Path(sleeve_data_root).expanduser()
    if root.name == SHARED_ACCOUNT_ROOT_DIRNAME:
        return root
    return root.parent / SHARED_ACCOUNT_ROOT_DIRNAME


@dataclass(frozen=True)
class CrossSleeveAccountState:
    """Decoded control row. A neutral instance (all defaults) is the safe NO-OP state
    returned whenever there is no row or the read fails."""

    account_key: str = DEFAULT_ACCOUNT_KEY
    equity_usdt: float = 0.0
    account_im_used_pct: float = 0.0
    im_used_pct_by_sleeve: dict[str, float] = field(default_factory=dict)
    margin_budget_pct_by_sleeve: dict[str, float] | None = None
    reservations: list[dict[str, Any]] = field(default_factory=list)
    updated_at_ms: int = 0

    def budget_for(self, sleeve: str) -> float | None:
        if not self.margin_budget_pct_by_sleeve:
            return None
        val = self.margin_budget_pct_by_sleeve.get(sleeve)
        return float(val) if val is not None else None

    def im_used_for(self, sleeve: str) -> float:
        return float(self.im_used_pct_by_sleeve.get(sleeve, 0.0) or 0.0)

    def active_reservations(self, *, now_ms: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.reservations:
            reserved_at = int(r.get("reserved_at_ms") or 0)
            ttl = int(r.get("ttl_ms") or RESERVATION_TTL_MS)
            if reserved_at > 0 and (now_ms - reserved_at) <= ttl:
                out.append(r)
        return out

    def symbol_reserved_by_other(self, symbol: str, *, sleeve: str, now_ms: int) -> bool:
        """True iff an ACTIVE reservation on `symbol` is held by a DIFFERENT sleeve. A
        sleeve's own active reservation does NOT block it (idempotent re-claim)."""
        for r in self.active_reservations(now_ms=now_ms):
            if str(r.get("symbol", "")) == symbol and str(r.get("sleeve", "")) != sleeve:
                return True
        return False


def _loads_dict(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


def _loads_list(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    try:
        out = json.loads(value)
        return [r for r in out if isinstance(r, dict)] if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def _loads_budget(value: Any) -> dict[str, float] | None:
    """Budget split decodes to None (= no clamp) when absent/empty, else a dict.

    CS cross-sleeve-5: an empty dict ``{}`` is the canonical "no sleeves budgeted" case
    (``equal_split_budget([])``), which means exactly "no clamp" — i.e. None. ``encode_control_row``
    normalizes ``{}`` to the empty string on write so the round-trip is symmetric (write {} -> read
    None), and a stale ``'{}'`` persisted by an older writer still decodes to None here."""
    if value is None or value == "":
        return None
    d = _loads_dict(value)
    return {k: float(v) for k, v in d.items()} if d else None


def _row_to_state(row: dict[str, Any]) -> CrossSleeveAccountState:
    return CrossSleeveAccountState(
        account_key=str(row.get("account_key") or DEFAULT_ACCOUNT_KEY),
        equity_usdt=float(row.get("equity_usdt") or 0.0),
        account_im_used_pct=float(row.get("account_im_used_pct") or 0.0),
        im_used_pct_by_sleeve={k: float(v) for k, v in _loads_dict(row.get("im_used_pct_by_sleeve")).items()},
        margin_budget_pct_by_sleeve=_loads_budget(row.get("margin_budget_pct_by_sleeve")),
        reservations=_loads_list(row.get("reservations")),
        updated_at_ms=int(row.get("updated_at_ms") or 0),
    )


def encode_control_row(state: CrossSleeveAccountState) -> dict[str, Any]:
    """Encode a state to the on-disk row shape — the SINGLE encode site."""
    return {
        "account_key": state.account_key,
        "equity_usdt": float(state.equity_usdt),
        "account_im_used_pct": float(state.account_im_used_pct),
        "im_used_pct_by_sleeve": json.dumps(state.im_used_pct_by_sleeve, sort_keys=True),
        # CS cross-sleeve-5: a None OR empty-dict budget both mean "no clamp", so both write
        # the empty string. This keeps the write/read round-trip symmetric — without it an
        # explicit {} persisted as '{}' would read back as None (write({}) != read()), a silent
        # normalization that surprises an equality-based audit. {} canonicalizes to None.
        "margin_budget_pct_by_sleeve": (
            json.dumps(state.margin_budget_pct_by_sleeve, sort_keys=True)
            if state.margin_budget_pct_by_sleeve
            else ""
        ),
        "reservations": json.dumps(state.reservations),
        "updated_at_ms": int(state.updated_at_ms),
    }


def _read_state_locked(root: Path) -> CrossSleeveAccountState:
    """Read the latest control row WITHOUT taking the lock (caller holds it). Neutral
    state when absent/empty/torn."""
    path = root / CROSS_SLEEVE_DATASET
    files = sorted(path.glob("**/*.parquet")) if path.exists() else []
    if not files:
        return CrossSleeveAccountState()
    try:
        df = _collect_ledger_files(files, dataset=CROSS_SLEEVE_DATASET, columns=None)
        if df.is_empty():
            return CrossSleeveAccountState()
        return _row_to_state(df.sort("updated_at_ms").tail(1).to_dicts()[0])
    except Exception as exc:  # noqa: BLE001
        _logger.debug("cross_sleeve locked-read failed (neutral): %s", exc)
        return CrossSleeveAccountState()


def _write_state_locked(root: Path, state: CrossSleeveAccountState) -> None:
    """Write the control row WITHOUT taking the lock (caller holds it)."""
    _write_dataset_unlocked(
        pl.DataFrame([encode_control_row(state)], infer_schema_length=None),
        root,
        CROSS_SLEEVE_DATASET,
        partition_by=(),
        append=True,
    )


def read_account_state(account_root: str | Path) -> CrossSleeveAccountState:
    """Read the control row READ-ONLY, swallowing every error -> neutral NO-OP state.
    Used by all sleeves before sizing. Never raises."""
    try:
        df = read_dataset(account_root, CROSS_SLEEVE_DATASET)
    except Exception as exc:  # noqa: BLE001 - read MUST never break a sleeve cycle
        _logger.debug("cross_sleeve read failed (legacy/no-op): %s", exc)
        return CrossSleeveAccountState()
    if df.is_empty():
        return CrossSleeveAccountState()
    try:
        return _row_to_state(df.sort("updated_at_ms").tail(1).to_dicts()[0])
    except Exception as exc:  # noqa: BLE001
        _logger.debug("cross_sleeve decode failed (no-op): %s", exc)
        return CrossSleeveAccountState()


def write_account_state(
    account_root: str | Path,
    *,
    equity_usdt: float,
    account_im_used_pct: float,
    im_used_pct_by_sleeve: dict[str, float],
    now_ms: int,
    closed_trade_ids: Iterable[str] = (),
    account_key: str = DEFAULT_ACCOUNT_KEY,
    margin_budget_pct_by_sleeve: dict[str, float] | None | _Preserve = _PRESERVE,
) -> None:
    """ws_risk's per-pass control-row write — UNDER-LOCK read-modify-write so it never
    clobbers a reservation a sleeve claimed concurrently (long-sleeve-6 safety). Reads
    the latest row under the dataset lock, updates the IM/equity fields, SETS the
    ``margin_budget_pct_by_sleeve`` (ws_risk computes the equal split = 1/n_active each
    pass; pass the sentinel default to PRESERVE the prior budget instead, or None to clear
    it), GCs reservations (drop expired + any whose trade_id is now closed), and writes
    under the held lock. Fail-safe: any error is swallowed + logged — a write failure can
    never break the reconcile loop (the sleeves just keep reading the last-good row)."""
    closed = {str(t) for t in closed_trade_ids}
    try:
        root = ensure_data_root(account_root)
        with exclusive_file_lock(dataset_lock_path(root, CROSS_SLEEVE_DATASET), stale_seconds=21_600, poll_seconds=0.01):
            prior = _read_state_locked(root)
            kept = [
                r for r in prior.active_reservations(now_ms=now_ms)
                if str(r.get("trade_id", "")) not in closed
            ]
            budget = (
                prior.margin_budget_pct_by_sleeve
                if isinstance(margin_budget_pct_by_sleeve, _Preserve)
                else margin_budget_pct_by_sleeve
            )
            _write_state_locked(root, CrossSleeveAccountState(
                account_key=prior.account_key or account_key,
                equity_usdt=float(equity_usdt),
                account_im_used_pct=float(account_im_used_pct),
                im_used_pct_by_sleeve=dict(im_used_pct_by_sleeve),
                margin_budget_pct_by_sleeve=budget,  # ws_risk-computed equal split (or preserved)
                reservations=kept,
                # CS-1: monotonic — the LAST committer under the lock must win the single-row
                # dedup (keep=last by updated_at_ms), regardless of wall-clock skew between a
                # sleeve's stale cycle_now_ms and ws_risk's fresh _now_ms(). Else a fresh IM
                # write could clobber a just-claimed reservation while the claim returned True.
                updated_at_ms=max(prior.updated_at_ms + 1, int(now_ms)),
            ))
    except Exception as exc:  # noqa: BLE001 - control-row write must never break reconcile
        _logger.warning("cross_sleeve write_account_state failed (skipped this pass): %s", exc)


def seed_margin_budget(
    account_root: str | Path,
    margin_budget_pct_by_sleeve: dict[str, float] | None,
    *,
    now_ms: int,
    account_key: str = DEFAULT_ACCOUNT_KEY,
) -> None:
    """Operator seed of the pre-registered IM split — UNDER-LOCK RMW that sets ONLY the
    budget split and preserves ws_risk's IM + the sleeves' reservations. Pass None to
    clear the clamp (back to legacy no-op).

    CS cross-sleeve-4: this is the INTENDED operator seam for turning the budget clamp on.
    There is deliberately no CLI/daemon caller yet — wiring one is gated on a pre-registered,
    sleeve-WEIGHTED allocation decision (AGENTS.md parameter pre-registration; an equal 1/n
    split would starve the over-subscribed sleeves, see ws_risk._refresh_cross_sleeve_account_state).
    Unlike the per-cycle writers (write_account_state / claim / release) this is NOT wrapped in
    swallow-and-log: a one-shot operator action MUST fail loud so the operator sees a failed seed,
    whereas a per-reconcile write must never break the loop. The divergent error contract is
    therefore correct by design, not an oversight."""
    root = ensure_data_root(account_root)
    with exclusive_file_lock(dataset_lock_path(root, CROSS_SLEEVE_DATASET), stale_seconds=21_600, poll_seconds=0.01):
        prior = _read_state_locked(root)
        _write_state_locked(root, CrossSleeveAccountState(
            account_key=prior.account_key or account_key,
            equity_usdt=prior.equity_usdt,
            account_im_used_pct=prior.account_im_used_pct,
            im_used_pct_by_sleeve=prior.im_used_pct_by_sleeve,
            margin_budget_pct_by_sleeve=margin_budget_pct_by_sleeve,
            reservations=prior.reservations,
            updated_at_ms=max(prior.updated_at_ms + 1, int(now_ms)),  # CS-1: monotonic last-writer-wins
        ))


def account_key(*, account_type: str = "UNIFIED", settle_coin: str = "USDT") -> str:
    """Stable per-netted-account key (one account => one control row). The exact value is
    cosmetic for a single netted account (read_account_state reads the sole row), but
    keying it on account_type+settle_coin keeps it correct if accounts ever coexist."""
    return f"{account_type}-{settle_coin}"


def _trade_im_usdt(trade: dict[str, Any], *, sleeve_leverage: dict[str, float]) -> float:
    """Initial margin (USDT) for one OPEN trade. Prefer a stored ``initial_margin_usdt``
    (the long sleeve computes it); else ``notional_usdt / leverage`` where leverage is the
    trade's own ``entry_leverage``, the sleeve's configured leverage, or — when unknown — a
    CONSERVATIVE 1.0 (IM = full notional, so the budget clamp triggers EARLIER, never
    later; over-counting margin is fail-safe, under-counting is not)."""
    stored = float(trade.get("initial_margin_usdt") or 0.0)
    notional = float(trade.get("notional_usdt") or 0.0)
    sleeve = str(trade.get("sleeve") or "")
    known_lev = (
        float(trade.get("entry_leverage") or 0.0)
        or float(sleeve_leverage.get(sleeve, 0.0) or 0.0)
    )  # 0.0 == unknown (no per-trade leverage and no sleeve-map entry)
    if stored > 0.0:
        # CS-8: floor a (possibly stale/low) stored IM at the leverage-implied minimum
        # notional/lev, but ONLY when leverage is reliably known — never inflate via the
        # 1.0 unknown-leverage fallback (that would over-count a high-leverage sleeve like
        # long ~10x). Stale-low stored => floor (fail-safe); accurate stored => no-op.
        if notional > 0.0 and known_lev > 0.0:
            return max(stored, notional / known_lev)
        return stored
    if notional <= 0.0:
        return 0.0
    # No stored IM: notional/leverage, unknown leverage => CONSERVATIVE 1.0 (full notional,
    # clamps EARLIER not later; over-counting margin is fail-safe, under-counting is not).
    return notional / max(known_lev or 1.0, 1.0)


def compute_im_used(
    open_trades: "pl.DataFrame | None",
    *,
    equity_usdt: float,
    sleeve_leverage: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Aggregate initial-margin-used as a fraction of equity across OPEN trades, total +
    per sleeve. Returns (account_im_used_pct, im_used_pct_by_sleeve). equity<=0 or no open
    trades => (0.0, {}) — no clamp basis. ws_risk calls this each reconcile pass; the
    sleeve of each trade is its ``sleeve`` ledger column (short/long/continuous)."""
    if equity_usdt <= 0.0 or open_trades is None or open_trades.is_empty():
        return 0.0, {}
    by_sleeve: dict[str, float] = {}
    total = 0.0
    for t in open_trades.to_dicts():
        im = _trade_im_usdt(t, sleeve_leverage=sleeve_leverage)
        if im <= 0.0:
            continue
        sleeve = str(t.get("sleeve") or "")
        by_sleeve[sleeve] = by_sleeve.get(sleeve, 0.0) + im
        total += im
    eq = float(equity_usdt)
    return total / eq, {s: v / eq for s, v in by_sleeve.items()}


def clamp_max_new_entries(
    max_new_entries: int, *, sleeve: str, state: CrossSleeveAccountState,
) -> tuple[int, bool]:
    """long-sleeve-5: SHRINK-ONLY budget clamp. Returns (effective_max, clamped). No
    budget for `sleeve` -> unchanged (legacy). At/over the IM ceiling -> clamp to 0.
    Only ever shrinks; never upsizes, never touches a sibling."""
    budget = state.budget_for(sleeve)
    if budget is None:
        return max_new_entries, False
    if state.im_used_for(sleeve) >= budget:
        return 0, max_new_entries > 0
    return max_new_entries, False


def partition_claimable(
    account_root: str | Path,
    candidates: list[dict[str, Any]],
    *,
    sleeve: str,
    now_ms: int,
    live_position_symbols: set[str] | None = None,
    ttl_ms: int = RESERVATION_TTL_MS,
) -> tuple[list[dict[str, Any]], int]:
    """long-sleeve-6 pre-submit gate for the short/continuous sleeves: claim each
    candidate's symbol (dict with 'symbol' + 'trade_id') through the shared registry and
    return (granted_candidates, skipped_count). A candidate whose symbol is taken by a
    sibling (active foreign reservation OR live venue position) is dropped. Fail-open:
    on any error / no-writer the whole batch is GRANTED, so this never blocks a legitimate
    entry. Call ONLY on a real submit (dry-run/paper must not reserve).

    CS cross-sleeve-6: resolves ALL candidates against one state snapshot under a SINGLE
    lock+read+write cycle (was N lock/read/fsync/rename cycles, one per candidate) so the
    submit hot path does not accrue per-candidate fsync churn when many symbols (or the
    3-component ensemble re-claiming a symbol across components) fire in one cycle."""
    granted, skipped, _ = claim_symbols_reservation(
        account_root,
        candidates,
        sleeve=sleeve,
        now_ms=now_ms,
        live_position_symbols=live_position_symbols,
        ttl_ms=ttl_ms,
    )
    return granted, skipped


def claim_symbols_reservation(
    account_root: str | Path,
    candidates: list[dict[str, Any]],
    *,
    sleeve: str,
    now_ms: int,
    live_position_symbols: set[str] | None = None,
    ttl_ms: int = RESERVATION_TTL_MS,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """long-sleeve-6 BATCH claim: take the control-row dataset lock ONCE, read the state
    ONCE, resolve every candidate against that single in-memory snapshot (granting/rejecting
    against reservations as they accumulate so an own-sleeve re-claim within the batch is
    idempotent and a sibling/live-position conflict still rejects), append all granted
    reservations, and write ONCE. Returns (granted_candidates, skipped_count, denied).

    Same per-candidate contract as claim_symbol_reservation: a candidate whose symbol is in
    a live venue position OR reserved by a DIFFERENT sleeve is rejected; the sleeve's own
    symbol is a re-claim (grant). Safe-by-default: no control dataset yet -> GRANT all (legacy
    venue-snapshot exclusion remains); ANY error -> GRANT all (fail-open) + a warning, so a bug
    here can never silently halt the live book. An empty candidate list never takes the lock."""
    if not candidates:
        return [], 0, []
    live = live_position_symbols or set()
    granted: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    try:
        root = ensure_data_root(account_root)
        with exclusive_file_lock(dataset_lock_path(root, CROSS_SLEEVE_DATASET), stale_seconds=21_600, poll_seconds=0.01):
            path = root / CROSS_SLEEVE_DATASET
            if not (path.exists() and sorted(path.glob("**/*.parquet"))):
                return list(candidates), 0, []  # no writer yet -> legacy exclusion only
            state = _read_state_locked(root)
            # Evolve ONE reservation set across the batch (active only) so intra-batch
            # re-claims and conflicts resolve exactly as N sequential single-claims would.
            kept = list(state.active_reservations(now_ms=now_ms))
            for cand in candidates:
                symbol = str(cand.get("symbol", ""))
                trade_id = str(cand.get("trade_id", ""))
                taken = symbol in live or any(
                    str(r.get("symbol")) == symbol and str(r.get("sleeve")) != sleeve
                    for r in kept
                )
                if taken:
                    denied.append(cand)
                    continue
                # own re-claim: drop this sleeve's prior row for the symbol, then re-append
                kept = [
                    r for r in kept
                    if not (str(r.get("symbol")) == symbol and str(r.get("sleeve")) == sleeve)
                ]
                kept.append({
                    "symbol": symbol, "sleeve": sleeve, "trade_id": trade_id,
                    "reserved_at_ms": int(now_ms), "ttl_ms": int(ttl_ms),
                })
                granted.append(cand)
            if granted:
                _write_state_locked(root, CrossSleeveAccountState(
                    account_key=state.account_key,
                    equity_usdt=state.equity_usdt,
                    account_im_used_pct=state.account_im_used_pct,
                    im_used_pct_by_sleeve=state.im_used_pct_by_sleeve,
                    margin_budget_pct_by_sleeve=state.margin_budget_pct_by_sleeve,
                    reservations=kept,
                    updated_at_ms=max(state.updated_at_ms + 1, int(now_ms)),  # CS-1: monotonic last-writer-wins
                ))
            return granted, len(denied), denied
    except Exception as exc:  # noqa: BLE001 - reservation must never halt the live book
        _logger.warning("cross_sleeve batch claim failed for %s (%d cands), failing open: %s", sleeve, len(candidates), exc)
        return list(candidates), 0, []


def claim_symbol_reservation(
    account_root: str | Path,
    *,
    symbol: str,
    sleeve: str,
    trade_id: str,
    now_ms: int,
    live_position_symbols: set[str] | None = None,
    ttl_ms: int = RESERVATION_TTL_MS,
) -> bool:
    """long-sleeve-6: atomically claim `symbol` for `sleeve` under the control-row
    dataset lock. Returns True if granted (caller may submit), False if TAKEN (active
    foreign reservation OR a live venue position). Thin single-candidate wrapper over
    claim_symbols_reservation for the long sleeve's sequential path.

    Atomicity: takes the dataset lock ONCE and does the whole read-modify-write inside
    it, so two processes waking in the same ~60s window are SERIALIZED — the loser sees
    the winner's reservation and is rejected, closing the same-minute race the lagging
    venue snapshot cannot. Safe-by-default: no control dataset yet (ws_risk not deployed)
    -> GRANT (legacy venue-snapshot exclusion remains); any error -> GRANT (fail-open) +
    a warning, so a bug here can never silently halt the live book."""
    granted, _, _ = claim_symbols_reservation(
        account_root,
        [{"symbol": symbol, "trade_id": trade_id}],
        sleeve=sleeve,
        now_ms=now_ms,
        live_position_symbols=live_position_symbols,
        ttl_ms=ttl_ms,
    )
    return bool(granted)


def release_symbol_reservation(
    account_root: str | Path,
    *,
    symbol: str,
    sleeve: str,
    trade_id: str,
    now_ms: int,
) -> bool:
    """LON-9: drop the active reservation `sleeve` claimed for `symbol`/`trade_id` once an
    entry FAILED or went unconfirmed (no trade opened), so a sibling sleeve isn't blocked
    from that symbol for the full RESERVATION_TTL_MS. Matches on (symbol, sleeve, trade_id)
    so it only ever releases THIS sleeve's own claim, never another's. Returns True iff a
    reservation was dropped. Safe-by-default: no control dataset yet -> no-op; ANY error ->
    swallow + warn (a release bug must never halt the book — the TTL is still the backstop).
    The TTL-expiry / closed_trade_ids GC in write_account_state remains the fallback."""
    try:
        root = ensure_data_root(account_root)
        with exclusive_file_lock(dataset_lock_path(root, CROSS_SLEEVE_DATASET), stale_seconds=21_600, poll_seconds=0.01):
            path = root / CROSS_SLEEVE_DATASET
            if not (path.exists() and sorted(path.glob("**/*.parquet"))):
                return False  # nothing persisted yet
            state = _read_state_locked(root)
            kept = [
                r for r in state.reservations
                if not (
                    str(r.get("symbol")) == symbol
                    and str(r.get("sleeve")) == sleeve
                    and str(r.get("trade_id")) == trade_id
                )
            ]
            if len(kept) == len(state.reservations):
                return False  # nothing matched (already GC'd / never claimed)
            _write_state_locked(root, CrossSleeveAccountState(
                account_key=state.account_key,
                equity_usdt=state.equity_usdt,
                account_im_used_pct=state.account_im_used_pct,
                im_used_pct_by_sleeve=state.im_used_pct_by_sleeve,
                margin_budget_pct_by_sleeve=state.margin_budget_pct_by_sleeve,
                reservations=kept,
                updated_at_ms=max(state.updated_at_ms + 1, int(now_ms)),  # CS-1: monotonic last-writer-wins
            ))
            return True
    except Exception as exc:  # noqa: BLE001 - a release bug must never halt the book; TTL is the backstop
        _logger.warning("cross_sleeve release failed for %s/%s (TTL still frees it): %s", sleeve, symbol, exc)
        return False
