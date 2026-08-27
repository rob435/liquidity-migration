"""Stable LONG execution identities shared by research and the live runtime."""

from __future__ import annotations


LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID = "long_native_v11a_div_weekend_vol"
LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME = "LongV11aDivWeekendVol"

# v12 differs from v11a only in stop geometry. It gets its own identity because
# the string is a persisted target and attribution key, not a code reference:
# two profiles sharing one id would merge their positions under one component.
LONG_V12_WIDE_STOP_STRATEGY_ID = "long_native_v12_wide_stop"
LONG_V12_WIDE_STOP_PROFILE_NAME = "LongV12WideStop"

SUPPORTED_LONG_STRATEGY_IDS = frozenset(
    {LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID, LONG_V12_WIDE_STOP_STRATEGY_ID}
)

LONG_PROFILE_NAMES_BY_STRATEGY_ID = {
    LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID: LONG_V11A_DIV_WEEKEND_VOL_PROFILE_NAME,
    LONG_V12_WIDE_STOP_STRATEGY_ID: LONG_V12_WIDE_STOP_PROFILE_NAME,
}


def long_profile_display_name(strategy_id: str) -> str:
    """Human-readable profile name for a supported LONG execution identity."""

    name = LONG_PROFILE_NAMES_BY_STRATEGY_ID.get(str(strategy_id))
    if name is None:
        raise ValueError(f"unsupported LONG strategy id: {strategy_id!r}")
    return name


def long_trade_id(*, symbol: str, signal_ts_ms: int) -> str:
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol or int(signal_ts_ms) <= 0:
        raise ValueError("LONG trade identity requires symbol and positive signal_ts_ms")
    return f"long-{normalized_symbol}-{int(signal_ts_ms)}"
