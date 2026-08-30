"""Durable and in-memory state for the CARRY producer.

The pure lifecycle reducer lives in :mod:`liquidity_migration.rules.carry_contract`.
This module imports the two legacy CARRY state files and writes one canonical
atomic reducer checkpoint. The exit-mask path remains a compatibility mirror;
the combined checkpoint is the authority after its first successful write.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path

import polars as pl

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.durable_file import durable_atomic_replace
from liquidity_migration.rules.carry_contract import CarryDecision, PriorState, anchor_sizing_state


def load_carry_exit_state(path: Path) -> dict[str, int]:
    """Read the durable per-decision exit mask."""

    if not path.is_absolute():
        raise ValueError("CARRY early-exit state path must be absolute")
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    snapshot = read_stable_file(
        path,
        label="CARRY early-exit state",
        reject_empty=True,
        require_single_link=True,
        max_bytes=1024 * 1024,
    )
    raw = json.loads(snapshot.data)
    if not isinstance(raw, dict) or set(raw) != {"fired"} or not isinstance(raw["fired"], dict):
        raise ValueError("CARRY early-exit state has invalid fields")
    fired: dict[str, int] = {}
    for symbol, ts in raw["fired"].items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.upper()
            or not symbol.isalnum()
            or isinstance(ts, bool)
            or not isinstance(ts, int)
            or ts <= 0
        ):
            raise ValueError("CARRY early-exit state contains an invalid row")
        fired[symbol] = ts
    return fired


def persist_carry_exit_state(path: Path, fired: Mapping[str, int]) -> None:
    """Durably replace the per-decision exit mask."""

    if not path.is_absolute():
        raise ValueError("CARRY early-exit state path must be absolute")
    durable_atomic_replace(
        path,
        canonical_json({"fired": dict(sorted(fired.items()))}) + b"\n",
        label="CARRY early-exit state",
    )


class CarryCycleState:
    """Daemon-owned caches plus the durable reducer-state adapter."""

    __slots__ = (
        "frozen_ahead_bar_ts_ms",
        "frozen_decisions",
        "funding_swept_hour_ts",
        "last_successful_decision_ts_ms",
        "sizing_equity_by_decision",
        "sizing_equity_usdt",
        "sizing_equity_decision_ts_ms",
        "sizing_anchor_path",
        "canonical_reducer_state",
        "early_exits",
        "drop_exits_logged",
        "whale_last_attempt_ms",
        "whale_store",
    )

    def __init__(self) -> None:
        self.frozen_decisions: dict[
            int,
            tuple[CarryDecision, dict[str, float], int, dict[str, str]],
        ] = {}
        self.frozen_ahead_bar_ts_ms: int | None = None
        self.funding_swept_hour_ts: int | None = None
        self.last_successful_decision_ts_ms: int | None = None
        self.sizing_equity_by_decision: dict[int, float] = {}
        self.sizing_equity_usdt: float | None = None
        self.sizing_equity_decision_ts_ms: int | None = None
        self.sizing_anchor_path: Path | None = None
        self.canonical_reducer_state = False
        self.whale_store: pl.DataFrame | None = None
        self.whale_last_attempt_ms: int | None = None
        self.early_exits: dict[str, int] | None = None
        self.drop_exits_logged: frozenset[str] = frozenset()

    def frozen_decision(
        self,
        decision_ts_ms: int,
    ) -> tuple[CarryDecision, dict[str, float], int, dict[str, str]] | None:
        return self.frozen_decisions.get(int(decision_ts_ms))

    def freeze_decision(
        self,
        *,
        decision_ts_ms: int,
        decision: CarryDecision,
        trail_by_symbol: dict[str, float],
        universe_eligible: int,
        input_evidence: Mapping[str, str] | None = None,
        frozen_ahead: bool = False,
    ) -> None:
        self.frozen_decisions[int(decision_ts_ms)] = (
            decision,
            dict(trail_by_symbol),
            int(universe_eligible),
            dict(input_evidence or {}),
        )
        while len(self.frozen_decisions) > 2:
            del self.frozen_decisions[min(self.frozen_decisions)]
        if frozen_ahead:
            self.frozen_ahead_bar_ts_ms = int(decision_ts_ms)

    def bind_sizing_anchors(self, path: Path) -> None:
        """Load the durable per-decision sizing anchors once per daemon."""

        if not path.is_absolute():
            raise ValueError("CARRY sizing-anchor path must be absolute")
        if self.sizing_anchor_path is not None:
            if self.sizing_anchor_path != path:
                raise RuntimeError("CarryCycleState cannot span two sizing-anchor paths")
            return
        self.sizing_anchor_path = path
        try:
            path.lstat()
        except FileNotFoundError:
            return
        snapshot = read_stable_file(
            path,
            label="CARRY sizing anchors",
            reject_empty=True,
            require_single_link=True,
            max_bytes=16 * 1024,
        )
        try:
            payload = json.loads(snapshot.data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CARRY sizing anchors are not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("CARRY sizing anchors have invalid fields")
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int:
            raise ValueError("CARRY sizing anchors have an unsupported schema")
        if schema_version == 1:
            if set(payload) != {"schema_version", "anchors"}:
                raise ValueError("CARRY sizing anchors have invalid fields")
        elif schema_version == 2:
            if set(payload) != {"schema_version", "anchors", "fired"}:
                raise ValueError("CARRY reducer checkpoint has invalid fields")
        else:
            raise ValueError("CARRY sizing anchors have an unsupported schema")
        if not isinstance(payload["anchors"], dict):
            raise ValueError("CARRY sizing anchors have an unsupported schema")
        loaded: dict[int, float] = {}
        for raw_key, raw_value in payload["anchors"].items():
            if (
                not isinstance(raw_key, str)
                or not raw_key.isascii()
                or not raw_key.isdigit()
                or raw_key.startswith("0")
                or isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
            ):
                raise ValueError("CARRY sizing anchors contain an invalid value")
            key = int(raw_key)
            value = float(raw_value)
            if key <= 0 or not math.isfinite(value) or value <= 0.0:
                raise ValueError("CARRY sizing anchors contain an invalid value")
            loaded[key] = value
        if len(loaded) > 2:
            raise ValueError("CARRY sizing anchors retain more than two decisions")
        self.sizing_equity_by_decision = loaded
        if schema_version == 2:
            if not isinstance(payload["fired"], dict):
                raise ValueError("CARRY reducer checkpoint fired exits must be an object")
            fired: dict[str, int] = {}
            for symbol, stamp in payload["fired"].items():
                if (
                    not isinstance(symbol, str)
                    or not symbol
                    or symbol != symbol.upper()
                    or not symbol.isalnum()
                    or isinstance(stamp, bool)
                    or not isinstance(stamp, int)
                    or stamp <= 0
                ):
                    raise ValueError("CARRY reducer checkpoint contains an invalid fired exit")
                fired[symbol] = stamp
            self.early_exits = fired
            self.canonical_reducer_state = True

    def bind_exit_state(self, path: Path) -> None:
        """Load the durable exit mask once per process state."""

        if self.early_exits is None:
            self.early_exits = load_carry_exit_state(path)

    def reducer_prior(self, *, exit_state_path: Path) -> PriorState:
        """Return the canonical pure-reducer state for this cycle."""

        self.bind_exit_state(exit_state_path)
        return PriorState(
            tuple(sorted(self.sizing_equity_by_decision.items())),
            tuple(sorted((self.early_exits or {}).items())),
        )

    def persist_reducer_state(self, *, exit_state_path: Path, state: PriorState) -> None:
        """Persist a reducer transition before its target book is published."""

        next_fired = state.fired_by_symbol()
        next_anchors = state.anchor_by_decision()
        if self.sizing_anchor_path is not None:
            if (
                not self.canonical_reducer_state
                or next_fired != (self.early_exits or {})
                or next_anchors != self.sizing_equity_by_decision
            ):
                self._persist_reducer_checkpoint(state)
                self.canonical_reducer_state = True
                self.early_exits = next_fired
                self.sizing_equity_by_decision = next_anchors
            try:
                persist_carry_exit_state(exit_state_path, next_fired)
            except (OSError, ValueError):
                # Schema v2 is authoritative. This legacy mirror cannot gate
                # the reducer transition or target-book publication.
                pass
            return
        if next_fired != (self.early_exits or {}):
            persist_carry_exit_state(exit_state_path, next_fired)
        self.early_exits = next_fired
        self.sizing_equity_by_decision = next_anchors

    def sizing_equity(self, *, decision_ts_ms: int, equity_usdt: float) -> float:
        """Compatibility adapter over the pure reducer's sizing anchor."""

        return self.anchor_frozen_decision(
            decision_ts_ms=decision_ts_ms,
            equity_usdt=equity_usdt,
        )

    def anchor_frozen_decision(self, *, decision_ts_ms: int, equity_usdt: float) -> float:
        """Persist the freeze-time anchor used by the next reducer call."""

        prior = PriorState(tuple(sorted(self.sizing_equity_by_decision.items())))
        next_state, anchor = anchor_sizing_state(
            prior,
            decision_ts_ms=int(decision_ts_ms),
            equity_usdt=float(equity_usdt),
        )
        if anchor is None:
            return equity_usdt
        next_anchors = next_state.anchor_by_decision()
        if next_anchors != self.sizing_equity_by_decision:
            if self.sizing_anchor_path is not None:
                self._persist_reducer_checkpoint(
                    PriorState(
                        tuple(sorted(next_anchors.items())),
                        tuple(sorted((self.early_exits or {}).items())),
                    )
                )
                self.canonical_reducer_state = True
            self.sizing_equity_by_decision = next_anchors
        self.sizing_equity_decision_ts_ms = int(decision_ts_ms)
        self.sizing_equity_usdt = float(anchor)
        return float(anchor)

    def note_reducer_sizing(self, *, decision_ts_ms: int, sizing_equity_usdt: float | None) -> None:
        if sizing_equity_usdt is None:
            return
        self.sizing_equity_decision_ts_ms = int(decision_ts_ms)
        self.sizing_equity_usdt = float(sizing_equity_usdt)

    def _persist_reducer_checkpoint(self, state: PriorState) -> None:
        path = self.sizing_anchor_path
        if path is None:
            return
        durable_atomic_replace(
            path,
            canonical_json(
                {
                    "schema_version": 2,
                    "anchors": {
                        str(key): value
                        for key, value in state.sizing_anchors
                    },
                    "fired": dict(state.fired_exits),
                }
            )
            + b"\n",
            label="CARRY reducer checkpoint",
        )
