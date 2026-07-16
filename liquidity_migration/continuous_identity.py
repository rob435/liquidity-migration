from __future__ import annotations


def continuous_trade_id(strategy_id: str, symbol: str, signal_ts_ms: int) -> str:
    """Return the deterministic identity for one symbol and signal window."""

    return f"{strategy_id}-{symbol}-{int(signal_ts_ms)}"
