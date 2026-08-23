"""The account reading the target producers size from.

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

**What is checked is the realm, not the account id.** A route names an account
logically (``bybit-mainnet-unified``); the engine reports the venue's own user
number (``552445993``, the same one its lease file is named after). They are
two id spaces and never match, so comparing them would block every entry on
every cycle -- fail-closed and silent. Realm is the comparison that carries the
real risk anyway: what must never happen is a demo heartbeat sizing a mainnet
producer. The venue user id is still read and still reported in errors, because
when something is wrong it is the number that identifies which account you are
actually looking at.
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
    "ENGINE_HEARTBEAT_PATH_ENV",
    "TARGET_PRODUCER_HEALTH_MAX_AGE_NS",
    "engine_held_symbols",
    "engine_entry_blockers",
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
    #: Why the engine is not opening each name a producer asked for, from the
    #: same beat: symbol -> reason. Empty means nothing is blocked -- or that
    #: an older engine, which publishes no such field, said nothing at all.
    entry_blockers: Mapping[str, str]


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
    if equity is None or observed is None:
        raise ValueError(
            "engine heartbeat carries no account reading yet "
            "(account_equity_usdt/account_observed_wall_ts_ms are null)"
        )
    equity_usdt = float(equity)
    available_usdt = float(available) if available is not None else 0.0
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
    if not isinstance(realm, str) or not realm:
        raise ValueError("engine heartbeat carries no realm")
    held_symbols: frozenset[str] | None = None
    holdings: dict[str, tuple[str, float, float]] = {}
    if isinstance(positions, list):
        named: set[str] = set()
        for row in positions:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            named.add(symbol)
            qty = row.get("qty")
            if not isinstance(qty, (int, float)):
                continue
            side = str(row.get("side") or "")
            entry_px = row.get("entry_px")
            holdings[symbol] = (
                side,
                float(qty),
                float(entry_px) if isinstance(entry_px, (int, float)) else 0.0,
            )
        held_symbols = frozenset(named)

    entry_blockers: dict[str, str] = {}
    raw_blockers = payload.get("entry_blockers")
    if isinstance(raw_blockers, list):
        for row in raw_blockers:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            reason = str(row.get("reason") or "")
            if symbol and reason:
                entry_blockers[symbol] = reason
    return EngineAccountReading(
        equity_usdt=equity_usdt,
        available_usdt=available_usdt,
        observed_wall_ts_ms=observed_wall_ts_ms,
        account_user_id=account_user_id,
        realm=realm,
        held_symbols=held_symbols,
        holdings=holdings,
        entry_blockers=entry_blockers,
    )


def require_recent_engine_account(
    environment: str,
    *,
    max_age_ns: int,
    now_ns: int | None = None,
    path: str | Path | None = None,
) -> EngineAccountReading:
    """The reading, if it is recent and about the realm the caller means."""

    resolved = Path(path) if path is not None else engine_heartbeat_path(environment)
    reading = read_engine_account(resolved)
    if reading.realm != environment:
        raise ValueError(
            f"engine heartbeat is for the {reading.realm!r} realm, not {environment!r} "
            f"(that engine is on venue account {reading.account_user_id})"
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


def engine_held_symbols(
    environment: str,
    *,
    max_age_ns: int,
    path: str | Path | None = None,
) -> frozenset[str] | None:
    """What the engine says is held, or `None` when it did not say.

    `None` is the answer for every kind of not-knowing: no heartbeat, an
    unreadable one, a stale reading, an engine too old to publish positions.
    A producer must treat that as "no news" and leave its record alone --
    reading it as "holds nothing" would drop every open name at once, which is
    exactly the mistake this exists to prevent in the other direction.
    """

    try:
        return require_recent_engine_account(
            environment, max_age_ns=max_age_ns, path=path
        ).held_symbols
    except (OSError, RuntimeError, ValueError):
        # RuntimeError is read_stable_file's "changed while it was read" — the
        # engine rewrites the heartbeat every few seconds, so a mid-read
        # replacement is ordinary not-knowing, not a crash.
        return None


def engine_entry_blockers(
    environment: str,
    *,
    max_age_ns: int,
    path: str | Path | None = None,
) -> dict[str, str]:
    """Why the engine is not opening each asked-for name, or `{}` when it did not say.

    Same not-knowing as :func:`engine_held_symbols`: no heartbeat, a stale
    one, or an engine too old to publish the field all read as "no news",
    which is not the same as "nothing is blocked".
    """

    try:
        return dict(
            require_recent_engine_account(
                environment, max_age_ns=max_age_ns, path=path
            ).entry_blockers
        )
    except (OSError, RuntimeError, ValueError):
        return {}
