"""Account-level loss halt.

Account-level rather than per-sleeve: the failure that matters is the whole
book moving together, which per-position venue stops cannot see. CARRY holds a
basket of small-cap alts selected because their funding is extreme, so their
moves are far from independent.

``OK``
    Trade normally.
``BLOCKED``
    Take no new risk; leave existing positions under their venue stops. Entered
    when equity is too stale to judge. Flattening on missing data is itself a
    blind risky action, and the public feed drops for minutes at a time in
    normal operation, so a staleness flatten would fire constantly.
``TRIPPED``
    The daily loss ceiling was breached against a *fresh* reading. Flatten and
    stop. Never clears on its own.

The anchor is the day's opening equity, not its high-water mark: a trailing
variant would ratchet the threshold up after a profitable morning and stop out
on ordinary give-back. It is snapshotable so a restart cannot refresh the day's
loss budget and turn a crash-loop into unlimited daily loss.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

__all__ = [
    "LOSS_GUARD_BLOCKED",
    "LOSS_GUARD_OK",
    "LOSS_GUARD_TRIPPED",
    "AccountLossGuard",
]

LOSS_GUARD_OK = "ok"
LOSS_GUARD_BLOCKED = "blocked"
LOSS_GUARD_TRIPPED = "tripped"

#: Equity older than this is not evidence about the account now. Well above the
#: 2s reconcile cadence so a feed hiccup does not block trading, well below any
#: horizon over which a real drawdown could develop unseen.
DEFAULT_MAX_EQUITY_STALENESS_NS = 120 * 1_000_000_000


def _utc_day(ts_ns: int) -> str:
    return dt.datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=dt.timezone.utc).strftime(
        "%Y-%m-%d"
    )


class AccountLossGuard:
    """Halt the account on a daily loss ceiling, and fail closed on blindness."""

    __slots__ = (
        "max_daily_loss_usdt",
        "max_equity_staleness_ns",
        "_day",
        "_opening_equity",
        "_tripped_detail",
        "_last_equity",
    )

    def __init__(
        self,
        *,
        max_daily_loss_usdt: float | None,
        max_equity_staleness_ns: int = DEFAULT_MAX_EQUITY_STALENESS_NS,
    ) -> None:
        if max_daily_loss_usdt is not None and not (max_daily_loss_usdt > 0.0):
            raise ValueError("max_daily_loss_usdt must be positive when set")
        self.max_daily_loss_usdt = (
            None if max_daily_loss_usdt is None else float(max_daily_loss_usdt)
        )
        self.max_equity_staleness_ns = max(int(max_equity_staleness_ns), 0)
        self._day: str | None = None
        self._opening_equity: float | None = None
        self._tripped_detail: str = ""
        self._last_equity: float | None = None

    # -- state ---------------------------------------------------------------

    @property
    def tripped(self) -> bool:
        return bool(self._tripped_detail)

    @property
    def tripped_detail(self) -> str:
        """Why the ceiling tripped, or "". Survives a restart via ``restore``."""

        return self._tripped_detail

    @property
    def opening_equity(self) -> float | None:
        return self._opening_equity

    def snapshot(self) -> dict[str, Any]:
        """Serialisable anchor. Persist this; a forgotten anchor is unlimited loss."""
        return {
            "day": self._day,
            "opening_equity": self._opening_equity,
            "tripped_detail": self._tripped_detail,
        }

    def restore(self, state: Mapping[str, Any] | None) -> None:
        if not state:
            return
        day = state.get("day")
        opening = state.get("opening_equity")
        self._day = str(day) if day else None
        self._opening_equity = (
            float(opening) if isinstance(opening, (int, float)) and opening > 0.0 else None
        )
        self._tripped_detail = str(state.get("tripped_detail") or "")

    def reset(self) -> None:
        """Clear a trip. Only an explicit operator action may call this."""
        self._tripped_detail = ""
        self._day = None
        self._opening_equity = None

    # -- evaluation ----------------------------------------------------------

    def evaluate(
        self,
        *,
        equity_usdt: float | None,
        equity_ts_ns: int | None,
        now_ns: int,
    ) -> tuple[str, str]:
        """Return ``(state, detail)`` for this moment.

        ``equity_usdt``/``equity_ts_ns`` are the most recent successful account
        equity reading and when it was taken, or ``None`` if there has never
        been one.
        """

        if self._tripped_detail:
            return LOSS_GUARD_TRIPPED, self._tripped_detail

        if equity_usdt is None or equity_ts_ns is None or equity_usdt <= 0.0:
            return LOSS_GUARD_BLOCKED, "no account equity reading yet"

        age_ns = int(now_ns) - int(equity_ts_ns)
        if age_ns < 0:
            # A backwards clock is unknown safety-critical state.
            return LOSS_GUARD_BLOCKED, "account equity timestamp is in the future"
        if age_ns > self.max_equity_staleness_ns:
            return (
                LOSS_GUARD_BLOCKED,
                f"account equity is {age_ns / 1_000_000_000:.0f}s stale",
            )

        self._last_equity = float(equity_usdt)
        day = _utc_day(int(equity_ts_ns))
        if self._day != day or self._opening_equity is None:
            self._day = day
            self._opening_equity = float(equity_usdt)
            return LOSS_GUARD_OK, ""

        if self.max_daily_loss_usdt is None:
            return LOSS_GUARD_OK, ""

        loss = self._opening_equity - float(equity_usdt)
        if loss >= self.max_daily_loss_usdt:
            self._tripped_detail = (
                f"daily loss {loss:,.2f} USDT reached the {self.max_daily_loss_usdt:,.2f} "
                f"ceiling ({day} open {self._opening_equity:,.2f} -> {equity_usdt:,.2f})"
            )
            return LOSS_GUARD_TRIPPED, self._tripped_detail

        return LOSS_GUARD_OK, ""
