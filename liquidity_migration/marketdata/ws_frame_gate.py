"""Decide whether a raw Bybit WebSocket frame is worth decoding.

Every frame arrives as a JSON string. pybit calls json.loads on all of them
before anything can say whether the payload is wanted, and the producers want
about one kline frame per symbol per hour out of a stream that pushes a partial
bar every second. Reading the raw string with a substring test is roughly
fifteen times cheaper than decoding it.

Both gates fail open: a frame that does not match a known drop pattern is
always passed through, so a venue format change costs the saving and never a
bar.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


CONFIRM_TRUE = '"confirm":true'
CONFIRM_FALSE = '"confirm":false'
TICKER_TOPIC_PREFIX = '{"topic":"tickers.'
TICKER_DELTA_TYPE = '"type":"delta"'


def kline_frame_needs_decode(raw: Any) -> bool:
    """False only for a kline frame whose every bar is still open.

    Bybit marks a closed bar with ``confirm: true`` and KlineStore.add_bar
    rejects anything else. A frame that carries a closing bar next to the
    newly opened one contains both markers, so it is kept.
    """
    if not isinstance(raw, str):
        return True
    if CONFIRM_FALSE not in raw:
        return True
    return CONFIRM_TRUE in raw


class KlineFrameGate:
    """Drops unconfirmed kline frames and counts what it saw.

    ``on_dropped_frame`` exists because the pool's watchdog measures a
    connection's liveness from delivered messages. Without a stamp on the drop
    path a healthy connection looks silent between hourly bars and gets torn
    down every few minutes. Only the drop path calls it: a delivered frame is
    still stamped by the pool callback, and pongs and subscription acks must
    keep stamping nothing, because a socket that answers pings while the venue
    has stopped pushing is what the watchdog exists to catch.
    """

    __slots__ = ("frames_seen", "frames_dropped", "on_dropped_frame")

    def __init__(self, on_dropped_frame: Callable[[], None] | None = None) -> None:
        self.frames_seen = 0
        self.frames_dropped = 0
        self.on_dropped_frame = on_dropped_frame

    def accepts(self, raw: Any) -> bool:
        self.frames_seen += 1
        if kline_frame_needs_decode(raw):
            return True
        self.frames_dropped += 1
        hook = self.on_dropped_frame
        if hook is not None:
            hook()
        return False

    def stats(self) -> dict[str, int]:
        return {"frames_seen": self.frames_seen, "frames_dropped": self.frames_dropped}


class TickerFrameSampler:
    """Passes at most one ticker delta per symbol per ``min_interval_seconds``.

    Snapshot frames always pass: pybit rebuilds its per-topic row from them,
    and a dropped snapshot would leave the next delta writing into an empty
    list. The per-symbol map is bounded by the subscribed universe.
    """

    __slots__ = (
        "min_interval_seconds",
        "frames_seen",
        "frames_dropped",
        "_monotonic",
        "_last_by_symbol",
    )

    def __init__(
        self,
        *,
        min_interval_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval_seconds < 0.0:
            raise ValueError("min_interval_seconds must be non-negative")
        self.min_interval_seconds = float(min_interval_seconds)
        self.frames_seen = 0
        self.frames_dropped = 0
        self._monotonic = monotonic
        self._last_by_symbol: dict[str, float] = {}

    def accepts(self, raw: Any) -> bool:
        self.frames_seen += 1
        if self.min_interval_seconds <= 0.0 or not isinstance(raw, str):
            return True
        if not raw.startswith(TICKER_TOPIC_PREFIX):
            return True
        if TICKER_DELTA_TYPE not in raw:
            return True
        end = raw.find('"', len(TICKER_TOPIC_PREFIX))
        if end < 0:
            return True
        symbol = raw[len(TICKER_TOPIC_PREFIX) : end]
        now = self._monotonic()
        last = self._last_by_symbol.get(symbol)
        if last is not None and now - last < self.min_interval_seconds:
            self.frames_dropped += 1
            return False
        self._last_by_symbol[symbol] = now
        return True

    def stats(self) -> dict[str, int]:
        return {"frames_seen": self.frames_seen, "frames_dropped": self.frames_dropped}
