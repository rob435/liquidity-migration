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

VALID_SLEEVES = ("short", "long", "continuous")


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
    """Budget split decodes to None (= no clamp) when absent/empty, else a dict."""
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
        "margin_budget_pct_by_sleeve": (
            json.dumps(state.margin_budget_pct_by_sleeve, sort_keys=True)
            if state.margin_budget_pct_by_sleeve is not None
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
) -> None:
    """ws_risk's per-pass control-row write — UNDER-LOCK read-modify-write so it never
    clobbers a reservation a sleeve claimed concurrently (long-sleeve-6 safety). Reads
    the latest row under the dataset lock, updates ONLY the IM/equity fields, PRESERVES
    the operator-set ``margin_budget_pct_by_sleeve`` (ws_risk never sets it), GCs
    reservations (drop expired + any whose trade_id is now closed), and writes under the
    held lock. Fail-safe: any error is swallowed + logged — a write failure can never
    break the reconcile loop (the sleeves just keep reading the last-good row)."""
    closed = {str(t) for t in closed_trade_ids}
    try:
        root = ensure_data_root(account_root)
        with exclusive_file_lock(dataset_lock_path(root, CROSS_SLEEVE_DATASET), stale_seconds=21_600, poll_seconds=0.01):
            prior = _read_state_locked(root)
            kept = [
                r for r in prior.active_reservations(now_ms=now_ms)
                if str(r.get("trade_id", "")) not in closed
            ]
            _write_state_locked(root, CrossSleeveAccountState(
                account_key=prior.account_key or account_key,
                equity_usdt=float(equity_usdt),
                account_im_used_pct=float(account_im_used_pct),
                im_used_pct_by_sleeve=dict(im_used_pct_by_sleeve),
                margin_budget_pct_by_sleeve=prior.margin_budget_pct_by_sleeve,  # operator-owned
                reservations=kept,
                updated_at_ms=int(now_ms),
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
    clear the clamp (back to legacy no-op)."""
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
            updated_at_ms=int(now_ms),
        ))


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
    foreign reservation OR a live venue position).

    Atomicity: takes the dataset lock ONCE and does the whole read-modify-write inside
    it, so two processes waking in the same ~60s window are SERIALIZED — the loser sees
    the winner's reservation and is rejected, closing the same-minute race the lagging
    venue snapshot cannot. Safe-by-default: no control dataset yet (ws_risk not deployed)
    -> GRANT (legacy venue-snapshot exclusion remains); any error -> GRANT (fail-open) +
    a warning, so a bug here can never silently halt the live book."""
    live = live_position_symbols or set()
    try:
        root = ensure_data_root(account_root)
        with exclusive_file_lock(dataset_lock_path(root, CROSS_SLEEVE_DATASET), stale_seconds=21_600, poll_seconds=0.01):
            path = root / CROSS_SLEEVE_DATASET
            if not (path.exists() and sorted(path.glob("**/*.parquet"))):
                return True  # no writer yet -> legacy exclusion only
            state = _read_state_locked(root)
            if symbol in live:
                return False
            if state.symbol_reserved_by_other(symbol, sleeve=sleeve, now_ms=now_ms):
                return False
            kept = [
                r for r in state.active_reservations(now_ms=now_ms)
                if not (str(r.get("symbol")) == symbol and str(r.get("sleeve")) == sleeve)
            ]
            kept.append({
                "symbol": symbol, "sleeve": sleeve, "trade_id": trade_id,
                "reserved_at_ms": int(now_ms), "ttl_ms": int(ttl_ms),
            })
            _write_state_locked(root, CrossSleeveAccountState(
                account_key=state.account_key,
                equity_usdt=state.equity_usdt,
                account_im_used_pct=state.account_im_used_pct,
                im_used_pct_by_sleeve=state.im_used_pct_by_sleeve,
                margin_budget_pct_by_sleeve=state.margin_budget_pct_by_sleeve,
                reservations=kept,
                updated_at_ms=int(now_ms),
            ))
            return True
    except Exception as exc:  # noqa: BLE001 - reservation must never halt the live book
        _logger.warning("cross_sleeve claim failed for %s/%s, failing open: %s", sleeve, symbol, exc)
        return True
