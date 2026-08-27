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

from collections.abc import Callable
from typing import Any


CONFIRM_TRUE = '"confirm":true'
CONFIRM_FALSE = '"confirm":false'
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
