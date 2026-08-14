"""The account reading the target producers size from.

Until the Python account owner was deleted this came from
``account_owner_health.json``, which that owner rewrote every few seconds
beside its journal. The producers read one number out of it, equity, and
blocked every entry when it was missing or stale.

The engine owns the account now, and says the same thing in its heartbeat:
what the venue last reported as equity and spare margin, and **when that
reading was taken at the venue** -- not when the file was written. The
distinction is the whole check. An engine whose loop keeps running while its
venue reads fail rewrites its heartbeat on time with the reading stamp
standing still, so the number goes stale exactly when the account knowledge
does, which is the case a producer must not size against.

**The stamp is on the wall clock, and that cost a live deploy to learn.** The
first version read `account_observed_ns` straight out of the file and compared
it against `time.time_ns()`. The engine's clock is monotonic -- it counts from
an arbitrary instant near its own boot -- so a healthy engine six seconds old
published a stamp of six seconds, and this read it as fifty-six thousand years
stale and blocked every entry on both sleeves. The engine now converts the
reading's *age* into a stamp on the same clock it writes `wall_ts_ms` with, and
both halves have a test that says which clock it is.

What the old receipt carried beyond equity -- a journal sequence, a state
hash, a systemd generation binding -- described the owner's own loop. There is
no such loop to describe, and the engine does not read the Python journal, so
inventing those fields would have meant writing down numbers that referred to
nothing. They are gone rather than faked.

**What is checked is the realm, not the account id, and that was a correction
made against the live host.** The first draft compared the producer's
``route.account_id`` with the id the engine authenticated as. Those are two
different id spaces: the route names an account logically
(``bybit-mainnet-unified``) and the engine reports the venue's own user number
(``552445993``, the same one its lease file is named after). They never match,
so that check would have blocked every entry on every cycle -- fail-closed,
silent, and exactly the outcome this module exists to prevent.

The realm is the comparison that carries the real risk anyway. What must never
happen is a demo heartbeat sizing a mainnet producer, and realm catches that
while being a value both halves genuinely share. The venue user id is still
read and still reported in errors, because when something is wrong it is the
number that identifies which account you are actually looking at.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from liquidity_migration.core.artifact_snapshot import read_stable_file

__all__ = [
    "ENGINE_HEARTBEAT_PATH_ENV",
    "EngineAccountReading",
    "engine_heartbeat_path",
    "read_engine_account",
    "require_recent_engine_account",
]

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
    return EngineAccountReading(
        equity_usdt=equity_usdt,
        available_usdt=available_usdt,
        observed_wall_ts_ms=observed_wall_ts_ms,
        account_user_id=account_user_id,
        realm=realm,
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
