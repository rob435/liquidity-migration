"""The Rust engine account reading target producers size from.

The engine owns the account and says this in its heartbeat: what the venue last
reported as equity and spare margin, and **when that reading was taken at the
venue** -- not when the file was written. The distinction is the whole check.
An engine whose loop keeps running while its venue reads fail rewrites its
heartbeat on time with the reading stamp standing still, so the number goes
stale exactly when the account knowledge does, which is the case a producer
must not size against.

**The stamp is on the wall clock.** The engine's own clock is monotonic -- it
counts from an arbitrary instant near its boot -- so it converts the reading's
*age* into a stamp on the same clock it writes ``wall_ts_ms`` with, and both
halves have a test that says which clock it is.

The expected venue user id can be bound independently from the logical realm;
both checks fail closed before a producer sizes new risk.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from liquidity_migration.core.artifact_snapshot import read_stable_file

__all__ = [
    "EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV",
    "ENGINE_HEARTBEAT_PATH_ENV",
    "TARGET_PRODUCER_HEALTH_MAX_AGE_NS",
    "EngineAccountReading",
    "engine_heartbeat_path",
    "read_engine_account",
    "require_recent_engine_account",
]

#: How old a venue reading a producer will size from. Tighter than the
#: operator-facing watchdog: the engine refreshes this every few seconds.
TARGET_PRODUCER_HEALTH_MAX_AGE_NS = 30_000_000_000

#: Override for the engine heartbeat this producer reads. The unit sets it when
#: the engine writes somewhere other than the per-realm default below.
ENGINE_HEARTBEAT_PATH_ENV = "ENGINE_ACCOUNT_HEARTBEAT_FILE"

#: Venue user id the producer is allowed to size against. Realm alone is not
#: an account identity: two funded accounts can both report ``mainnet``.
EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV = "EXPECTED_ENGINE_ACCOUNT_USER_ID"

#: One heartbeat per realm, because one engine owns one account. These match
#: `StateDirectory` in the two engine units; two realms sharing a path would
#: have each engine overwrite the other's reading.
DEFAULT_ENGINE_HEARTBEAT: dict[str, Path] = {
    "demo": Path("/var/lib/liquidity-migration-engine/heartbeat.json"),
    "mainnet": Path("/var/lib/liquidity-migration-engine-mainnet/heartbeat.json"),
}


@dataclass(frozen=True, slots=True)
class EngineAccountReading:
    """What the venue last said about the account, and when it said it."""

    equity_usdt: float
    available_usdt: float
    #: When the venue reading was taken, in unix milliseconds.
    observed_wall_ts_ms: int
    #: The venue's own user number, for saying which account this was.
    account_user_id: str
    #: "demo" or "mainnet", as the engine resolved it.
    realm: str
    #: Symbols the venue says are held, upper-cased.
    #:
    #: A producer writes an *absolute* book -- it says what it wants held -- so
    #: the one thing it cannot work out alone is whether a name it asked for is
    #: actually there. `None` means the engine did not say: an older engine, or
    #: one that has not read the venue. That is not the same as holding nothing,
    #: and a producer must not act on it.
    held_symbols: frozenset[str] | None
    #: What the venue says is held, by name: symbol -> (side, qty, entry_px).
    #: Empty when the engine said nothing about positions; the side is the
    #: venue's own "long"/"short" spelling.
    holdings: Mapping[str, tuple[str, float, float]]
    #: Unique Rust fill-ledger owner for each venue holding. ``None`` means the
    #: position is manual, inherited, or shared and cannot be claimed by a
    #: producer.
    holding_strategies: Mapping[str, str | None]
    #: Symbols with an unfinished opening order, grouped by owning sleeve.
    working_entries_by_strategy: Mapping[str, frozenset[str]]
    #: Why each configured sleeve cannot open each requested name.
    entry_blockers_by_strategy: Mapping[str, Mapping[str, str]]
    #: Configured stable Rust sleeve names carried by this heartbeat.
    strategies: frozenset[str]

    def holdings_for_strategy(self, strategy: str) -> dict[str, tuple[str, float, float]]:
        """Venue holdings uniquely attributable to one configured sleeve."""

        sleeve = str(strategy)
        if sleeve not in self.strategies:
            raise ValueError(f"engine heartbeat does not configure strategy {sleeve!r}")
        return {
            symbol: row
            for symbol, row in self.holdings.items()
            if self.holding_strategies.get(symbol) == sleeve
        }

    def entry_blockers_for_strategy(self, strategy: str) -> dict[str, str]:
        """Entry refusals scoped to one configured sleeve."""

        sleeve = str(strategy)
        if sleeve not in self.strategies:
            raise ValueError(f"engine heartbeat does not configure strategy {sleeve!r}")
        return dict(self.entry_blockers_by_strategy.get(sleeve, {}))

    def working_entries_for_strategy(self, strategy: str) -> frozenset[str]:
        """Unfinished opening-order symbols attributable to one sleeve."""

        sleeve = str(strategy)
        if sleeve not in self.strategies:
            raise ValueError(f"engine heartbeat does not configure strategy {sleeve!r}")
        return self.working_entries_by_strategy.get(sleeve, frozenset())


def engine_heartbeat_path(environment: str) -> Path:
    """Where this environment's engine writes its heartbeat."""

    override = os.environ.get(ENGINE_HEARTBEAT_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    try:
        return DEFAULT_ENGINE_HEARTBEAT[environment]
    except KeyError:
        raise ValueError(
            f"no engine heartbeat default for environment {environment!r}; "
            f"set {ENGINE_HEARTBEAT_PATH_ENV}"
        ) from None


def read_engine_account(path: str | Path) -> EngineAccountReading:
    """Read one heartbeat, or raise.

    Every absent or unusable field raises rather than defaulting. A producer
    that sized from a defaulted zero would be sizing from a guess, and a
    producer that sized from a null equity would be sizing from ``None``.
    """

    snapshot = read_stable_file(
        Path(path),
        label="engine heartbeat",
        reject_empty=True,
        require_single_link=True,
        max_bytes=1024 * 1024,
    )
    try:
        payload = json.loads(snapshot.data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"engine heartbeat is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("engine heartbeat is not a JSON object")

    equity = payload.get("account_equity_usdt")
    available = payload.get("account_available_usdt")
    observed = payload.get("account_observed_wall_ts_ms")
    account_user_id = payload.get("account_user_id")
    realm = payload.get("realm")
    positions = payload.get("positions")
    raw_strategies = payload.get("strategies")
    raw_working_entries = payload.get("working_entries")
    if equity is None or available is None or observed is None:
        raise ValueError(
            "engine heartbeat carries no account reading yet "
            "(equity/available/observed fields must all be present)"
        )
    if (
        isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or isinstance(available, bool)
        or not isinstance(available, (int, float))
    ):
        raise ValueError("engine heartbeat equity and available margin must be JSON numbers")
    if isinstance(observed, bool) or not isinstance(observed, int):
        raise ValueError("engine heartbeat account_observed_wall_ts_ms must be an integer")
    equity_usdt = float(equity)
    available_usdt = float(available)
    observed_wall_ts_ms = int(observed)
    if not math.isfinite(equity_usdt) or equity_usdt <= 0.0:
        raise ValueError("engine heartbeat equity must be finite and positive")
    # Negative spare margin is an ordinary reading on a fully deployed account,
    # or one carrying hand-placed positions. It is not an error here; the risk
    # kernel is what refuses new risk on it.
    if not math.isfinite(available_usdt):
        raise ValueError("engine heartbeat available margin must be finite")
    if observed_wall_ts_ms <= 0:
        raise ValueError("engine heartbeat account_observed_wall_ts_ms must be positive")
    if not isinstance(account_user_id, str) or not account_user_id:
        raise ValueError("engine heartbeat carries no account_user_id")
    if not isinstance(realm, str) or realm not in DEFAULT_ENGINE_HEARTBEAT:
        raise ValueError("engine heartbeat carries no supported realm")
    if not isinstance(raw_strategies, list):
        raise ValueError("engine heartbeat strategies must be an array")
    strategies: set[str] = set()
    for index, strategy in enumerate(raw_strategies):
        if not isinstance(strategy, str) or not strategy or strategy in strategies:
            raise ValueError(f"engine heartbeat strategy {index} is invalid or duplicated")
        strategies.add(strategy)
    if not strategies:
        raise ValueError("engine heartbeat configures no strategies")
    if not isinstance(raw_working_entries, list):
        raise ValueError("engine heartbeat working_entries must be an array")
    working_entries_by_strategy: dict[str, set[str]] = {
        strategy: set() for strategy in strategies
    }
    working_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(raw_working_entries):
        if not isinstance(row, Mapping):
            raise ValueError(f"engine heartbeat working entry {index} is not an object")
        if set(row) != {"strategy", "symbol"}:
            raise ValueError(f"engine heartbeat working entry {index} has invalid fields")
        strategy = row["strategy"]
        symbol = row["symbol"]
        if (
            not isinstance(strategy, str)
            or strategy not in strategies
            or not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.upper()
            or not symbol.isalnum()
        ):
            raise ValueError(f"engine heartbeat working entry {index} is invalid")
        key = (strategy, symbol)
        if key in working_keys:
            raise ValueError(f"engine heartbeat repeats working entry for {strategy}:{symbol}")
        working_keys.add(key)
        working_entries_by_strategy[strategy].add(symbol)
    held_symbols: frozenset[str] | None = None
    holdings: dict[str, tuple[str, float, float]] = {}
    holding_strategies: dict[str, str | None] = {}
    if positions is not None:
        if not isinstance(positions, list):
            raise ValueError("engine heartbeat positions must be an array or null")
        named: set[str] = set()
        for index, row in enumerate(positions):
            if not isinstance(row, Mapping):
                raise ValueError(f"engine heartbeat position {index} is not an object")
            symbol = row.get("symbol")
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol != symbol.upper()
                or not symbol.isalnum()
            ):
                raise ValueError(f"engine heartbeat position {index} has invalid symbol")
            if symbol in named:
                raise ValueError(f"engine heartbeat repeats position {symbol}")
            qty = row.get("qty")
            if (
                isinstance(qty, bool)
                or not isinstance(qty, (int, float))
                or not math.isfinite(float(qty))
                or float(qty) <= 0.0
            ):
                raise ValueError(f"engine heartbeat position {symbol} has invalid qty")
            side = row.get("side")
            if not isinstance(side, str) or side not in {"long", "short"}:
                raise ValueError(f"engine heartbeat position {symbol} has invalid side")
            entry_px = row.get("entry_px")
            if (
                isinstance(entry_px, bool)
                or not isinstance(entry_px, (int, float))
                or not math.isfinite(float(entry_px))
                or float(entry_px) <= 0.0
            ):
                raise ValueError(f"engine heartbeat position {symbol} has invalid entry_px")
            if "strategy" not in row:
                raise ValueError(f"engine heartbeat position {symbol} has no strategy attribution")
            owner = row["strategy"]
            if owner is not None and (not isinstance(owner, str) or owner not in strategies):
                raise ValueError(
                    f"engine heartbeat position {symbol} has invalid strategy attribution"
                )
            named.add(symbol)
            holdings[symbol] = (
                side,
                float(qty),
                float(entry_px),
            )
            holding_strategies[symbol] = owner
        held_symbols = frozenset(named)

    entry_blockers_by_strategy: dict[str, dict[str, str]] = {
        strategy: {} for strategy in strategies
    }
    raw_blockers = payload.get("entry_blockers")
    if not isinstance(raw_blockers, list):
        raise ValueError("engine heartbeat entry_blockers must be an array")
    blocker_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(raw_blockers):
        if not isinstance(row, Mapping):
            raise ValueError(f"engine heartbeat entry blocker {index} is not an object")
        blocker_symbol = row.get("symbol")
        reason = row.get("reason")
        strategy = row.get("strategy")
        if (
            not isinstance(blocker_symbol, str)
            or not blocker_symbol
            or blocker_symbol != blocker_symbol.upper()
            or not blocker_symbol.isalnum()
            or not isinstance(reason, str)
            or not reason
            or not isinstance(strategy, str)
            or strategy not in strategies
        ):
            raise ValueError(f"engine heartbeat entry blocker {index} is invalid")
        key = (strategy, blocker_symbol)
        if key in blocker_keys:
            raise ValueError(
                f"engine heartbeat repeats entry blocker for {strategy}:{blocker_symbol}"
            )
        blocker_keys.add(key)
        entry_blockers_by_strategy[strategy][blocker_symbol] = reason
    return EngineAccountReading(
        equity_usdt=equity_usdt,
        available_usdt=available_usdt,
        observed_wall_ts_ms=observed_wall_ts_ms,
        account_user_id=account_user_id,
        realm=realm,
        held_symbols=held_symbols,
        holdings=holdings,
        holding_strategies=holding_strategies,
        working_entries_by_strategy={
            strategy: frozenset(symbols)
            for strategy, symbols in working_entries_by_strategy.items()
        },
        entry_blockers_by_strategy=entry_blockers_by_strategy,
        strategies=frozenset(strategies),
    )


def require_recent_engine_account(
    environment: str,
    *,
    max_age_ns: int,
    now_ns: int | None = None,
    path: str | Path | None = None,
    expected_account_user_id: str | None = None,
) -> EngineAccountReading:
    """Return a recent reading for the exact realm and venue account."""

    resolved = Path(path) if path is not None else engine_heartbeat_path(environment)
    reading = read_engine_account(resolved)
    if reading.realm != environment:
        raise ValueError(
            f"engine heartbeat is for the {reading.realm!r} realm, not {environment!r} "
            f"(that engine is on venue account {reading.account_user_id})"
        )
    expected_user_id = str(
        expected_account_user_id
        if expected_account_user_id is not None
        else os.environ.get(EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV, "")
    ).strip()
    if not expected_user_id:
        raise ValueError(
            f"{EXPECTED_ENGINE_ACCOUNT_USER_ID_ENV} must name the venue account "
            "the producer is allowed to size against"
        )
    if reading.account_user_id != expected_user_id:
        raise ValueError(
            f"engine heartbeat is for venue account {reading.account_user_id!r}, "
            f"not expected account {expected_user_id!r}"
        )
    stamp_ns = time.time_ns() if now_ns is None else int(now_ns)
    age_ns = stamp_ns - reading.observed_wall_ts_ms * 1_000_000
    if age_ns < 0:
        raise ValueError(
            f"engine heartbeat account reading is {-age_ns}ns in the future; clocks disagree"
        )
    if age_ns > int(max_age_ns):
        raise ValueError(
            f"engine heartbeat account reading is {age_ns / 1e9:.1f}s old "
            f"(bound {int(max_age_ns) / 1e9:.1f}s); the engine is not reading the venue"
        )
    return reading
