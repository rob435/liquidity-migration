"""The account reading the target producers size from.

Until the Python account owner was deleted this came from
``account_owner_health.json``, which that owner rewrote every few seconds
beside its journal. The producers read one number out of it, equity, and
blocked every entry when it was missing or stale.

The engine owns the account now, and says the same thing in its heartbeat:
what the venue last reported as equity and spare margin, and **when that
reading was taken at the venue** -- not when the file was written. The
distinction is the whole check. An engine whose loop keeps running while its
venue reads fail rewrites its heartbeat on time with `account_observed_ns`
standing still, so the number goes stale exactly when the account knowledge
does, which is the case a producer must not size against.

What the old receipt carried beyond equity -- a journal sequence, a state
hash, a systemd generation binding -- described the owner's own loop. There is
no such loop to describe, and the engine does not read the Python journal, so
inventing those fields would have meant writing down numbers that referred to
nothing. They are gone rather than faked.

One thing here is deliberately fail-closed and worth knowing about: the
account id. The producers pass the id from their account route, the heartbeat
carries the id the engine authenticated as, and a disagreement blocks entries
with both values in the message. If those two ever name the same account
differently, this is where it shows up, in one line, rather than as a fleet
that quietly stops trading.
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
    observed_ts_ns: int
    account_id: str


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
    observed = payload.get("account_observed_ns")
    account_id = payload.get("account_user_id")
    if equity is None or observed is None:
        raise ValueError(
            "engine heartbeat carries no account reading yet "
            "(account_equity_usdt/account_observed_ns are null)"
        )
    equity_usdt = float(equity)
    available_usdt = float(available) if available is not None else 0.0
    observed_ts_ns = int(observed)
    if not math.isfinite(equity_usdt) or equity_usdt <= 0.0:
        raise ValueError("engine heartbeat equity must be finite and positive")
    # Negative spare margin is an ordinary reading on a fully deployed account,
    # or one carrying hand-placed positions. It is not an error here; the risk
    # kernel is what refuses new risk on it.
    if not math.isfinite(available_usdt):
        raise ValueError("engine heartbeat available margin must be finite")
    if observed_ts_ns <= 0:
        raise ValueError("engine heartbeat account_observed_ns must be positive")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("engine heartbeat carries no account_user_id")
    return EngineAccountReading(
        equity_usdt=equity_usdt,
        available_usdt=available_usdt,
        observed_ts_ns=observed_ts_ns,
        account_id=account_id,
    )


def require_recent_engine_account(
    environment: str,
    *,
    max_age_ns: int,
    expected_account_id: str | None = None,
    now_ns: int | None = None,
    path: str | Path | None = None,
) -> EngineAccountReading:
    """The reading, if it is recent and about the account the caller means."""

    resolved = Path(path) if path is not None else engine_heartbeat_path(environment)
    reading = read_engine_account(resolved)
    if expected_account_id is not None and reading.account_id != expected_account_id:
        raise ValueError(
            "engine heartbeat is for a different account: "
            f"route says {expected_account_id!r}, engine authenticated as {reading.account_id!r}"
        )
    stamp_ns = time.time_ns() if now_ns is None else int(now_ns)
    age_ns = stamp_ns - reading.observed_ts_ns
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
