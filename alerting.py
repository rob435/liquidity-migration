from __future__ import annotations

from dataclasses import dataclass

import aiohttp


@dataclass(slots=True)
class AlertPayload:
    stage: str
    signal_kind: str
    ticker: str
    composite_score: float
    momentum_z: float
    curvature: float
    hurst: float
    current_price: float
    regime_score: int
    rank: int | None = None
    persistence_hits: int | None = None
    persistence_window: int | None = None
    persistence_min_hits: int | None = None


@dataclass(slots=True)
class ExecutionPayload:
    event: str
    ticker: str
    side: str
    stage: str
    signal_kind: str
    quantity: float
    entry_price: float | None = None
    exit_price: float | None = None
    current_price: float | None = None
    pnl_pct: float | None = None
    notes: str = ""


class TelegramNotifier:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        bot_token: str | None,
        chat_id: str | None,
    ) -> None:
        self.session = session
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def _send_text(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        async with self.session.post(
            url,
            json={"chat_id": self.chat_id, "text": text},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            response.raise_for_status()
        return True

    async def send(self, payload: AlertPayload) -> bool:
        labels = {
            "watchlist": "watchlist candidate",
            "emerging": "emerging setup",
            "entry_ready": "tradeable intrabar entry candidate",
        }
        label = labels.get(payload.signal_kind, "signal candidate")
        persistence_line = ""
        if payload.persistence_hits is not None and payload.persistence_window is not None:
            if payload.persistence_min_hits is not None:
                persistence_line = (
                    f"Persistence: {payload.persistence_hits}/{payload.persistence_window} "
                    f"(strong at {payload.persistence_min_hits}/{payload.persistence_window})\n"
                )
            else:
                persistence_line = (
                    f"Persistence: {payload.persistence_hits}/{payload.persistence_window}\n"
                )
        text = (
            f"{payload.ticker} {label}\n"
            f"Stage: {payload.stage.upper()}\n"
            f"Signal: {payload.signal_kind.upper()}\n"
            f"Composite: {payload.composite_score:.2f}\n"
            f"Momentum z: {payload.momentum_z:.2f}\n"
            f"Curvature: {payload.curvature:.6f}\n"
            f"Hurst: {payload.hurst:.3f}\n"
            f"Price: {payload.current_price:.6f}\n"
            f"BTC regime: {payload.regime_score}\n"
            f"{persistence_line}"
            f"Rank: {payload.rank if payload.rank is not None else 'n/a'}"
        )
        return await self._send_text(text)

    async def send_execution(self, payload: ExecutionPayload) -> bool:
        labels = {
            "enter_long": "entered new long position",
            "take_profit_exit": "take-profit exit",
            "stop_loss_exit": "stop-loss exit",
            "protected_profit_exit": "profit-protection exit",
            "stale_winner_exit": "stale-winner exit",
            "exchange_exit": "venue-managed exit",
        }
        price_lines = []
        if payload.entry_price is not None:
            price_lines.append(f"Entry price: {payload.entry_price:.6f}")
        if payload.exit_price is not None:
            price_lines.append(f"Exit price: {payload.exit_price:.6f}")
        elif payload.current_price is not None:
            price_lines.append(f"Price: {payload.current_price:.6f}")
        pnl_line = (
            f"\nPnL: {payload.pnl_pct * 100:.2f}%"
            if payload.pnl_pct is not None
            else ""
        )
        notes_line = f"\nNotes: {payload.notes}" if payload.notes else ""
        text = (
            f"{payload.ticker} {labels.get(payload.event, payload.event)}\n"
            f"Stage: {payload.stage.upper()}\n"
            f"Signal: {payload.signal_kind.upper()}\n"
            f"Side: {payload.side}\n"
            f"Quantity: {payload.quantity:.8f}\n"
            f"{chr(10).join(price_lines)}"
            f"{pnl_line}"
            f"{notes_line}"
        )
        return await self._send_text(text)
