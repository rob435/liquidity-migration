"""Stable LONG execution identities shared by research and Rust."""

from __future__ import annotations


LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID = "long_native_v11a_div_weekend_vol"

# v12 differs from v11a only in stop geometry. It gets its own identity because
# the string is a persisted target and attribution key, not a code reference:
# two profiles sharing one id would merge their positions under one component.
LONG_V12_WIDE_STOP_STRATEGY_ID = "long_native_v12_wide_stop"

SUPPORTED_LONG_STRATEGY_IDS = frozenset(
    {LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID, LONG_V12_WIDE_STOP_STRATEGY_ID}
)
