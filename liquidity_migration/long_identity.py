"""Stable LONG execution identities shared by research, paper, and demo."""

from __future__ import annotations


LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID = "long_native_v11a_div_weekend_vol"


def long_trade_id(*, symbol: str, signal_ts_ms: int) -> str:
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol or int(signal_ts_ms) <= 0:
        raise ValueError("LONG trade identity requires symbol and positive signal_ts_ms")
    return f"long-{normalized_symbol}-{int(signal_ts_ms)}"
